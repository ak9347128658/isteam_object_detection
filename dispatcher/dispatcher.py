"""
dispatcher.py — concurrency-capped launcher for the detection workers.

Enforces the rule: at most MAX_CONCURRENCY (default 3) worker containers run at
once. It is the ONLY thing that pulls from SQS. When a container exits, its slot
is freed and — if the queue still has messages — the next one is launched
immediately. When the queue is empty, it idles cheaply on SQS long-polling.

    running < 3  AND  queue has data   ->  launch one more
    a container exits                  ->  free a slot -> refill from queue

Launch backends (LAUNCH_BACKEND env):
  docker  (default)  -> `docker run` the worker image on this host.
  ecs                -> ECS RunTask on a cluster (Fargate/EC2), no local Docker.

Environment variables:
  SQS_QUEUE_URL        (required)  queue to drain.
  MAX_CONCURRENCY      (optional)  max simultaneous containers. Default 3.
  AWS_DEFAULT_REGION   (optional)  region for SQS/ECS. Default us-east-1.
  SQS_VISIBILITY       (optional)  message visibility timeout secs. Default 1800.
  POLL_WAIT_SECONDS    (optional)  SQS long-poll wait. Default 20.

  # passed through to every worker container:
  CALLBACK_URL         default callback if a message omits one.
  OUTPUT_BUCKET, OUTPUT_PREFIX, OUTPUT_REGION, OUTPUT_PUBLIC
  SERPAPI_API_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY

  # docker backend:
  WORKER_IMAGE         image tag. Default "object-detection-process".
  DOCKER_GPUS          e.g. "all" to add `--gpus all`. Optional.

  # ecs backend:
  ECS_CLUSTER          (required for ecs) cluster name/arn.
  ECS_TASK_DEFINITION  (required for ecs) task def family[:revision].
  ECS_CONTAINER_NAME   container name in the task def. Default "worker".
  ECS_SUBNETS          comma list of subnet ids (Fargate awsvpc).
  ECS_SECURITY_GROUPS  comma list of security-group ids (Fargate awsvpc).
  ECS_LAUNCH_TYPE      FARGATE (default) or EC2.
  ECS_ASSIGN_PUBLIC_IP ENABLED (default) or DISABLED.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Optional

import boto3


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
VISIBILITY = int(os.getenv("SQS_VISIBILITY", "1800"))
POLL_WAIT = int(os.getenv("POLL_WAIT_SECONDS", "20"))
LAUNCH_BACKEND = os.getenv("LAUNCH_BACKEND", "docker").strip().lower()

# Values forwarded to every worker container.
PASSTHROUGH_ENV = [
    "CALLBACK_URL",
    "OUTPUT_BUCKET",
    "OUTPUT_PREFIX",
    "OUTPUT_REGION",
    "OUTPUT_PUBLIC",
    "SKIP_MATCHING",
    "SERPAPI_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_DEFAULT_REGION",
]


def _worker_env(msg: dict) -> dict:
    """Build the environment a single worker container needs from a job message."""
    env = {
        "VIDEO_S3_URI": msg.get("s3_uri") or msg.get("video_s3_uri", ""),
        "JOB_ID": msg.get("job_id") or uuid.uuid4().hex,
        "CALLBACK_URL": msg.get("callback_url", os.getenv("CALLBACK_URL", "")),
        "OUTPUT_BUCKET": msg.get("output_bucket", os.getenv("OUTPUT_BUCKET", "")),
        "SKIP_MATCHING": str(msg.get("skip_matching", os.getenv("SKIP_MATCHING", "false"))).lower(),
    }
    # Fill any passthrough values not already set from the message.
    for k in PASSTHROUGH_ENV:
        env.setdefault(k, os.getenv(k, ""))
    return {k: v for k, v in env.items() if v != ""}


# ---------------------------------------------------------------------------
# Launch backends. Each returns a "handle" that .poll()s to detect exit.
# ---------------------------------------------------------------------------

class Slot:
    """A running worker plus how to check whether it has finished."""

    def __init__(self, job_id: str, receipt: str, checker):
        self.job_id = job_id
        self.receipt = receipt
        self._checker = checker  # callable -> (done: bool, ok: bool)

    def poll(self) -> Optional[bool]:
        """Return None if still running, True if exited-ok, False if exited-fail."""
        done, ok = self._checker()
        return None if not done else ok


class DockerBackend:
    """Launch each worker as a local `docker run` process."""

    def __init__(self):
        self.image = os.getenv("WORKER_IMAGE", "object-detection-process")
        self.gpus = os.getenv("DOCKER_GPUS", "").strip()

    def launch(self, msg: dict, receipt: str) -> Slot:
        env = _worker_env(msg)
        cmd = ["docker", "run", "--rm"]
        if self.gpus:
            cmd += ["--gpus", self.gpus]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd.append(self.image)

        proc = subprocess.Popen(cmd)
        print(f"[dispatcher] docker launched job {env['JOB_ID']} (pid {proc.pid})")

        def _check():
            rc = proc.poll()
            if rc is None:
                return (False, False)
            return (True, rc == 0)

        return Slot(env["JOB_ID"], receipt, _check)


class EcsBackend:
    """Launch each worker as an ECS RunTask and poll task status."""

    def __init__(self):
        self.ecs = boto3.client("ecs", region_name=REGION)
        self.cluster = os.environ["ECS_CLUSTER"]
        self.task_def = os.environ["ECS_TASK_DEFINITION"]
        self.container = os.getenv("ECS_CONTAINER_NAME", "worker")
        self.launch_type = os.getenv("ECS_LAUNCH_TYPE", "FARGATE")
        self.subnets = [s for s in os.getenv("ECS_SUBNETS", "").split(",") if s]
        self.sgs = [s for s in os.getenv("ECS_SECURITY_GROUPS", "").split(",") if s]
        self.public_ip = os.getenv("ECS_ASSIGN_PUBLIC_IP", "ENABLED")

    def launch(self, msg: dict, receipt: str) -> Slot:
        env = _worker_env(msg)
        overrides = {
            "containerOverrides": [
                {
                    "name": self.container,
                    "environment": [{"name": k, "value": v} for k, v in env.items()],
                }
            ]
        }
        params = {
            "cluster": self.cluster,
            "taskDefinition": self.task_def,
            "count": 1,
            "launchType": self.launch_type,
            "overrides": overrides,
        }
        if self.launch_type == "FARGATE":
            params["networkConfiguration"] = {
                "awsvpcConfiguration": {
                    "subnets": self.subnets,
                    "securityGroups": self.sgs,
                    "assignPublicIp": self.public_ip,
                }
            }
        resp = self.ecs.run_task(**params)
        tasks = resp.get("tasks", [])
        if not tasks:
            failures = resp.get("failures", [])
            raise RuntimeError(f"ECS RunTask launched no task: {failures}")
        task_arn = tasks[0]["taskArn"]
        print(f"[dispatcher] ecs launched job {env['JOB_ID']} task {task_arn}")

        def _check():
            d = self.ecs.describe_tasks(cluster=self.cluster, tasks=[task_arn])
            items = d.get("tasks", [])
            if not items:
                return (True, False)
            status = items[0].get("lastStatus", "")
            if status != "STOPPED":
                return (False, False)
            # Exit code of our container, if present.
            code = 1
            for c in items[0].get("containers", []):
                if c.get("name") == self.container:
                    code = c.get("exitCode", 1)
            return (True, code == 0)

        return Slot(env["JOB_ID"], receipt, _check)


def _make_backend():
    if LAUNCH_BACKEND == "ecs":
        return EcsBackend()
    if LAUNCH_BACKEND == "docker":
        return DockerBackend()
    raise ValueError(f"Unknown LAUNCH_BACKEND {LAUNCH_BACKEND!r} (use docker or ecs).")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    if not QUEUE_URL:
        print("[dispatcher] SQS_QUEUE_URL is required.")
        return 2

    sqs = boto3.client("sqs", region_name=REGION)
    backend = _make_backend()
    running: list[Slot] = []

    print(f"[dispatcher] backend={LAUNCH_BACKEND} max_concurrency={MAX_CONCURRENCY} "
          f"queue={QUEUE_URL}")

    while True:
        # 1) Reap finished workers -> free their slots and settle the SQS message.
        still: list[Slot] = []
        for slot in running:
            result = slot.poll()
            if result is None:
                still.append(slot)
                continue
            if result:
                # Success: delete the message so it isn't re-processed.
                try:
                    sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=slot.receipt)
                except Exception as e:
                    print(f"[dispatcher] delete_message failed for {slot.job_id}: {e}")
                print(f"[dispatcher] job {slot.job_id} completed; slot freed "
                      f"({len(still)}/{MAX_CONCURRENCY} busy)")
            else:
                # Failure: leave the message so SQS redelivers / sends to DLQ.
                print(f"[dispatcher] job {slot.job_id} failed; message left for retry/DLQ")
        running = still

        # 2) Fill free slots from the queue (never exceed MAX_CONCURRENCY).
        launched_any = False
        while len(running) < MAX_CONCURRENCY:
            resp = sqs.receive_message(
                QueueUrl=QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=POLL_WAIT,
                VisibilityTimeout=VISIBILITY,
            )
            messages = resp.get("Messages", [])
            if not messages:
                break  # queue empty right now
            m = messages[0]
            receipt = m["ReceiptHandle"]
            try:
                body = json.loads(m["Body"])
            except Exception:
                body = {}
            if not (body.get("s3_uri") or body.get("video_s3_uri")):
                print("[dispatcher] message missing s3_uri; deleting.")
                sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=receipt)
                continue
            try:
                slot = backend.launch(body, receipt)
                running.append(slot)
                launched_any = True
            except Exception as e:
                print(f"[dispatcher] launch failed: {e}; leaving message for retry")
                break

        # 3) If nothing running and nothing launched, avoid a busy spin.
        if not running and not launched_any:
            time.sleep(1.0)
        else:
            time.sleep(2.0)


if __name__ == "__main__":
    raise SystemExit(main())

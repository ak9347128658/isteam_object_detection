# Step 4 — Create an EC2 machine to run the dispatcher

Step 3 defined the ECS cluster + worker task. The **dispatcher** is the small,
always-on process that reads SQS and launches worker tasks (max 3 at a time). It
does no heavy work, so a tiny EC2 instance is plenty. This step creates that EC2
box, gives it the right permissions, installs the code, and runs the dispatcher
as a **systemd service** so it restarts on reboot/crash.

```
EC2 (t3.small)  ->  dispatcher.py  --reads-->  SQS: isteam-object-detection-jobs
                                    --RunTask-> ECS worker tasks (cap 3)
```

Prereqs done: Steps 1–3 (queue receiving messages, ECS cluster + task def
`isteam-object-detection-worker`, task/execution roles).

---

## 1. Create an IAM role for the EC2 instance (instance profile)

The dispatcher uses the instance's role for AWS calls — no access keys on the
box.

1. IAM → **Roles** → **Create role**.
2. Trusted entity: **AWS service** → use case **EC2** → **Next**.
3. Skip managed policies → **Next** → Name: `isteam-dispatcher-ec2-role` →
   **Create role**.
4. Open the role → **Add permissions** → **Create inline policy** → **JSON**:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ReadQueue",
         "Effect": "Allow",
         "Action": [
           "sqs:ReceiveMessage",
           "sqs:DeleteMessage",
           "sqs:GetQueueAttributes"
         ],
         "Resource": "arn:aws:sqs:us-east-1:598886663176:isteam-object-detection-jobs"
       },
       {
         "Sid": "LaunchWorkers",
         "Effect": "Allow",
         "Action": ["ecs:RunTask", "ecs:DescribeTasks"],
         "Resource": "*"
       },
       {
         "Sid": "PassTaskRoles",
         "Effect": "Allow",
         "Action": "iam:PassRole",
         "Resource": [
           "arn:aws:iam::598886663176:role/isteam-detection-task-role",
           "arn:aws:iam::598886663176:role/isteam-detection-task-execution-role"
         ]
       }
     ]
   }
   ```

5. **Next** → **Policy name**: `dispatcher-sqs-ecs` → **Create policy**.

   (Replace region + account id. `iam:PassRole` is required because RunTask
   hands the two task roles from Step 3 to the launched task.)

---

## 2. Launch the EC2 instance

1. AWS Console → **EC2** → **Launch instance**.
2. **Name**: `isteam-detection-dispatcher`.
3. **AMI**: **Amazon Linux 2023** (has Python 3.9+ and dnf).
4. **Instance type**: **t3.small** (dispatcher is light; t3.micro also works).
5. **Key pair**: pick an existing one or **Create new key pair** (download the
   `.pem` so you can SSH in). Free-tier eligible types are fine.
6. **Network settings** → **Edit**:
   - VPC: your default VPC.
   - **Auto-assign public IP**: **Enable** (so you can SSH + it can reach AWS).
   - **Security group**: create one named `isteam-dispatcher-sg` with a single
     inbound rule: **SSH (22)** from **My IP** (not 0.0.0.0/0). Outbound: leave
     default (all allowed) — the dispatcher needs outbound to SQS/ECS.
7. **Advanced details** → **IAM instance profile**: select
   `isteam-dispatcher-ec2-role` (the role from step 1).
8. **Storage**: default 8 GB is enough.
9. **Launch instance**.

---

## 3. Connect to the instance

Either use the console (**EC2 → Instances → select → Connect → EC2 Instance
Connect**) for a browser terminal, or SSH:

```bash
ssh -i path/to/your-key.pem ec2-user@<public-ip>
```

---

## 4. Install Python + boto3 and get the code on the box

On the instance:

```bash
sudo dnf update -y
sudo dnf install -y python3 python3-pip git
python3 -m pip install --user boto3
```

Now create `dispatcher.py` **directly on the box** using the Linux CLI. Paste
the whole block below into the SSH/console terminal — the quoted `'PYEOF'`
heredoc writes every line verbatim (no shell expansion), so the file is created
exactly as-is:

```bash
cat > /home/ec2-user/dispatcher.py <<'PYEOF'
"""
dispatcher.py — concurrency-capped launcher for the detection workers.

Enforces the rule: at most MAX_CONCURRENCY (default 3) worker containers run at
once. It is the ONLY thing that pulls from SQS. When a container exits, its slot
is freed and — if the queue still has messages — the next one is launched
immediately. When the queue is empty, it idles cheaply on SQS long-polling.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from typing import Optional

import boto3


REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
QUEUE_URL = os.environ.get("SQS_QUEUE_URL", "")
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "3"))
VISIBILITY = int(os.getenv("SQS_VISIBILITY", "1800"))
POLL_WAIT = int(os.getenv("POLL_WAIT_SECONDS", "20"))
LAUNCH_BACKEND = os.getenv("LAUNCH_BACKEND", "docker").strip().lower()

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
    env = {
        "VIDEO_S3_URI": msg.get("s3_uri") or msg.get("video_s3_uri", ""),
        "JOB_ID": msg.get("job_id") or uuid.uuid4().hex,
        "CALLBACK_URL": msg.get("callback_url", os.getenv("CALLBACK_URL", "")),
        "OUTPUT_BUCKET": msg.get("output_bucket", os.getenv("OUTPUT_BUCKET", "")),
        "SKIP_MATCHING": str(msg.get("skip_matching", os.getenv("SKIP_MATCHING", "false"))).lower(),
    }
    for k in PASSTHROUGH_ENV:
        env.setdefault(k, os.getenv(k, ""))
    return {k: v for k, v in env.items() if v != ""}


class Slot:
    def __init__(self, job_id: str, receipt: str, checker):
        self.job_id = job_id
        self.receipt = receipt
        self._checker = checker

    def poll(self) -> Optional[bool]:
        done, ok = self._checker()
        return None if not done else ok


class DockerBackend:
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
        still: list[Slot] = []
        for slot in running:
            result = slot.poll()
            if result is None:
                still.append(slot)
                continue
            if result:
                try:
                    sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=slot.receipt)
                except Exception as e:
                    print(f"[dispatcher] delete_message failed for {slot.job_id}: {e}")
                print(f"[dispatcher] job {slot.job_id} completed; slot freed "
                      f"({len(still)}/{MAX_CONCURRENCY} busy)")
            else:
                print(f"[dispatcher] job {slot.job_id} failed; message left for retry/DLQ")
        running = still

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
                break
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

        if not running and not launched_any:
            time.sleep(1.0)
        else:
            time.sleep(2.0)


if __name__ == "__main__":
    raise SystemExit(main())
PYEOF
```

Verify it was written and is valid Python:

```bash
python3 -m py_compile /home/ec2-user/dispatcher.py && echo "dispatcher.py OK"
```

The dispatcher is a single self-contained file; boto3 (installed above) is its
only dependency.

> This is the ECS-mode essentials of the repo's `dispatcher/dispatcher.py`. If
> you'd rather pull the full commented version, use git instead:
> `sudo dnf install -y git && git clone <your-repo-url> && cp isteam_object_detection/dispatcher/dispatcher.py /home/ec2-user/dispatcher.py`

---

## 5. Configure environment variables

Create an env file the service will load. On the instance:

```bash
sudo tee /etc/isteam-dispatcher.env >/dev/null <<'EOF'
LAUNCH_BACKEND=ecs
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/598886663176/isteam-object-detection-jobs
MAX_CONCURRENCY=3
AWS_DEFAULT_REGION=us-east-1

ECS_CLUSTER=isteam-object-detection-cluster
ECS_TASK_DEFINITION=isteam-object-detection-worker
ECS_CONTAINER_NAME=worker
ECS_LAUNCH_TYPE=FARGATE
ECS_SUBNETS=subnet-aaaa,subnet-bbbb
ECS_SECURITY_GROUPS=sg-cccc
ECS_ASSIGN_PUBLIC_IP=ENABLED

CALLBACK_URL=https://.../callback
OUTPUT_BUCKET=isteam-video-output
EOF
```

Replace the queue URL, account id, subnets, security group, and callback URL
with your real values (subnets/SG are the same ones from Step 3 — VPC console →
Subnets / Security groups). No AWS keys go here; the instance role provides
credentials automatically.

Quick manual test before making it a service:

```bash
set -a; source /etc/isteam-dispatcher.env; set +a
python3 dispatcher.py
```

You should see: `backend=ecs max_concurrency=3 queue=...`. Upload a video and
watch it print `ecs launched job ... task ...`. Press Ctrl+C to stop, then set up
the service below so it runs unattended.

---

## 6. Run it as a systemd service (auto-restart, survives reboot)

Assuming `dispatcher.py` is at `/home/ec2-user/dispatcher.py`:

```bash
sudo tee /etc/systemd/system/isteam-dispatcher.service >/dev/null <<'EOF'
[Unit]
Description=iSteam detection dispatcher (SQS -> ECS, max 3)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user
EnvironmentFile=/etc/isteam-dispatcher.env
ExecStart=/usr/bin/python3 /home/ec2-user/dispatcher.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now isteam-dispatcher
```

Check it:

```bash
sudo systemctl status isteam-dispatcher
sudo journalctl -u isteam-dispatcher -f     # live logs
```

`Restart=always` restarts the dispatcher if it crashes; `enable` makes it start
on boot.

---

## 7. Verify end to end

1. `sudo journalctl -u isteam-dispatcher -f` on the EC2 box.
2. Upload a video to `s3://isteam-video-uploader/uploads/`.
3. You should see the dispatcher log `ecs launched job ... task ...`, and in the
   ECS console a `worker` task starts. Confirm results land in
   `isteam-video-output` and the callback Lambda logs the completion (same as
   Step 3's test).
4. Concurrency: upload 5 videos, confirm the dispatcher never has more than 3
   tasks running at once and refills as they finish.

---

## Managing / updating

- **Update the code**: copy a new `dispatcher.py`, then
  `sudo systemctl restart isteam-dispatcher`.
- **Change config**: edit `/etc/isteam-dispatcher.env`, then restart the service.
- **Stop temporarily**: `sudo systemctl stop isteam-dispatcher`.
- **Save cost when idle**: the dispatcher is cheap to leave running (t3.small),
  but you can **Stop** the EC2 instance when you don't expect uploads; **Start**
  it again when needed (the service auto-starts on boot).

---

## Troubleshooting

- **`Unable to locate credentials`**: the instance profile isn't attached.
  EC2 → Instance → **Actions → Security → Modify IAM role** →
  `isteam-dispatcher-ec2-role`.
- **`AccessDenied` on RunTask / PassRole**: the inline policy (step 1) is missing
  or the role ARNs are wrong. PassRole must list BOTH task roles from Step 3.
- **`AccessDenied` on ReceiveMessage**: the queue ARN in the policy doesn't match
  (region/account/name).
- **Dispatcher runs but no tasks start**: check the queue actually has messages
  (Step 2), and that `ECS_CLUSTER` / `ECS_TASK_DEFINITION` names match Step 3.
- **Tasks start but immediately stop**: that's a worker/task issue, not the
  dispatcher — see Step 3 troubleshooting (ECR pull, task role S3 perms,
  networking).
- **Service won't start**: `sudo journalctl -u isteam-dispatcher -n 50` to see
  the error (usually a missing env var or wrong file path in ExecStart).

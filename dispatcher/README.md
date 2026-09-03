# dispatcher — max-3 concurrency launcher

Keeps **at most `MAX_CONCURRENCY` (default 3)** worker containers running at
once. It is the only component that pulls from SQS. When a container exits, its
slot frees and the next queued job launches immediately; when the queue is
empty, it idles on SQS long-polling.

```
running < 3  AND  queue has data   ->  launch one more worker
a container exits                  ->  free a slot -> refill from queue
```

## Run (docker backend — single box / GPU host)

Build the worker image first (`../object_detection_process`):

```bash
docker build -t object-detection-process ../object_detection_process
```

Then run the dispatcher (needs Docker + AWS creds + boto3):

```bash
pip install boto3
set MAX_CONCURRENCY=3
set SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/123/detection-jobs
set WORKER_IMAGE=object-detection-process
set OUTPUT_BUCKET=isteam-video-output
set SERPAPI_API_KEY=...
python dispatcher.py
```

Add `set DOCKER_GPUS=all` to give each worker the GPU.

## Run (ecs backend — Fargate/ECS cluster)

```bash
set LAUNCH_BACKEND=ecs
set SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/123/detection-jobs
set ECS_CLUSTER=detection-cluster
set ECS_TASK_DEFINITION=object-detection-worker
set ECS_CONTAINER_NAME=worker
set ECS_SUBNETS=subnet-aaa,subnet-bbb
set ECS_SECURITY_GROUPS=sg-123
python dispatcher.py
```

The dispatcher calls ECS `RunTask` per job and watches task status to know when
a slot frees. Concurrency is still capped at 3 regardless of queue depth.

## Alternative: native ECS Service

If you prefer no dispatcher, run the worker image as an ECS **Service** with
`desiredCount = 3`, each task started with `worker.py --poll`. ECS keeps exactly
3 pollers alive and restarts any that exit. Same 3-at-a-time effect.

## Environment variables

| Var | Default | Meaning |
|---|---|---|
| `SQS_QUEUE_URL` | — (required) | Queue to drain. |
| `MAX_CONCURRENCY` | `3` | Max simultaneous worker containers. |
| `LAUNCH_BACKEND` | `docker` | `docker` or `ecs`. |
| `SQS_VISIBILITY` | `1800` | In-flight message visibility timeout (secs). |
| `POLL_WAIT_SECONDS` | `20` | SQS long-poll wait. |
| `WORKER_IMAGE` | `object-detection-process` | (docker) image to run. |
| `DOCKER_GPUS` | — | (docker) e.g. `all` to add `--gpus all`. |
| `ECS_CLUSTER` / `ECS_TASK_DEFINITION` | — | (ecs) required. |
| `ECS_SUBNETS` / `ECS_SECURITY_GROUPS` | — | (ecs, Fargate) awsvpc networking. |
| passthrough | — | `CALLBACK_URL`, `OUTPUT_BUCKET`, `SERPAPI_API_KEY`, `AWS_*` are forwarded to each worker. |

## IAM

The dispatcher needs `sqs:ReceiveMessage`, `sqs:DeleteMessage` on the queue, and
either local Docker (docker backend) or `ecs:RunTask` + `ecs:DescribeTasks`
(+ `iam:PassRole` for the task/execution roles) for the ecs backend.

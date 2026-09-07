# 07 — Deployment

Two supported shapes:

- **A. Single box (Docker Compose)** — API + dispatcher + Redis on one host; the
  dispatcher launches up to 2 worker containers via the host Docker socket.
- **B. Cloud (ECS/Fargate + SQS)** — managed queue, dispatcher (or native ECS
  Service with `desiredCount=2`) launches Fargate tasks.

Both enforce the same rule: **at most 2 workers at a time**, no storage of
input/output, results delivered by callback.

---

## A. Single box with Docker Compose (recommended to start)

### `docker-compose.yml`

```yaml
services:
  redis:
    image: redis:7-alpine
    restart: unless-stopped
    # AOF persistence so queued jobs survive a restart
    command: ["redis-server", "--appendonly", "yes"]
    volumes:
      - redis-data:/data

  api:
    image: isteam-detection-api:latest        # FastAPI: validate -> enqueue -> 202
    restart: unless-stopped
    environment:
      QUEUE_BACKEND: "redis"
      REDIS_URL: "redis://redis:6379/0"
      MAX_QUEUE_DEPTH: "0"
      CALLBACK_HOST_ALLOWLIST: "caller.example.com"
    ports:
      - "8000:8000"
    depends_on: [redis]

  dispatcher:
    image: isteam-detection-dispatcher:latest  # the max-2 gatekeeper
    restart: unless-stopped
    environment:
      QUEUE_BACKEND: "redis"
      REDIS_URL: "redis://redis:6379/0"
      MAX_CONCURRENCY: "2"                      # <<< the cap
      LAUNCH_BACKEND: "docker"
      WORKER_IMAGE: "object-detection-process:latest"
      WORKER_MEMORY: "6g"
      WORKER_CPUS: "4"
      SERPAPI_API_KEY: "${SERPAPI_API_KEY}"
      # For GPU: WORKER_GPUS: "all"
    volumes:
      # dispatcher launches worker containers on the host Docker engine
      - /var/run/docker.sock:/var/run/docker.sock
    depends_on: [redis]

volumes:
  redis-data:
```

Notes:

- **Workers are NOT a compose service.** The dispatcher runs them on demand with
  `docker run --rm` (max 2 live at once), so they are transient. Compose only
  runs the long-lived services (redis, api, dispatcher).
- Mounting `/var/run/docker.sock` lets the dispatcher launch sibling containers.
  On a hardened host, prefer the ECS path (B) or a rootless/again-scoped Docker
  API proxy instead of the raw socket.
- **No job volumes.** Worker temp data lives inside each worker container and is
  discarded on `--rm` — this is what enforces no storage. Add
  `--tmpfs /tmp/job:size=2g` per worker for RAM-only temp if desired.

### Bring it up

```powershell
# build the three images
docker build -t object-detection-process:latest object_detection_process
docker build -t isteam-detection-api:latest .\api          # FastAPI /detect service
docker build -t isteam-detection-dispatcher:latest .\dispatcher

# run
$env:SERPAPI_API_KEY = "..."
docker compose up -d

# submit a job
curl -X POST http://localhost:8000/detect `
  -H "Content-Type: application/json" `
  -d '{ "video_url":"https://cdn.example.com/clips/abc.mp4", "callback_url":"https://caller.example.com/hooks/detections", "video_id":"abc-123" }'
# -> 202 { "status":"processing_started", "video_id":"abc-123", "job_id":"..." }
```

The result arrives later as a POST to `https://caller.example.com/hooks/detections`.

---

## B. Cloud: ECS/Fargate + SQS

```
Caller ──POST /detect──▶ API (ECS service / Lambda+API GW) ──SendMessage──▶ SQS
Dispatcher (ECS service, 1 task) ──RunTask (cap 2)──▶ Worker tasks (Fargate)
   OR  native: Worker ECS Service desiredCount=2, each task --poll one SQS msg
Worker ──POST callback──▶ Caller ;  task exits ;  slot frees ;  next job runs
```

- **Queue:** SQS standard queue + a **DLQ** (`maxReceiveCount` ~3). Visibility
  timeout > max job duration (e.g. 1800s).
- **Concurrency = 2:** either the dispatcher enforces it with ECS `RunTask`
  (tracking `RUNNING` task count), or run the worker as an ECS **Service** with
  `desiredCount = 2` in `--poll` mode so ECS keeps exactly two pollers alive.
- **No storage:** workers use container-local `/tmp` (ephemeral Fargate task
  storage); nothing is uploaded for `detections.*`. Task exits → storage gone.
- **IAM (task role, least privilege):**
  - `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`
  - (only if crop-matching option A) `s3:PutObject`, `s3:DeleteObject` on the
    scratch bucket + `s3:GetObject` for presign
  - `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`,
    `ecr:GetDownloadUrlForLayer` to pull the image
- **Secrets:** `SERPAPI_API_KEY` via SSM Parameter Store / Secrets Manager, not
  env in plaintext.

Build & push the worker image to ECR using the existing guide:
[`../object_detection_process/STEPS.md`](../object_detection_process/STEPS.md).

---

## Deploy checklist

1. **Build the worker image** with weights baked in
   (`docker build object_detection_process`). Push to ECR for cloud.
2. **Build the API image** (FastAPI `POST /detect` → validate → enqueue → 202).
3. **Build the dispatcher image** (`MAX_CONCURRENCY=2`, launches workers).
4. **Provision the queue** (Redis with AOF, or SQS + DLQ).
5. **Configure the callback allow-list** and secrets (`SERPAPI_API_KEY`, AWS).
6. **Decide the Google-Lens crop strategy** (option A ephemeral presigned, or
   `SKIP_MATCHING=true`) — see [`04-worker-and-no-storage.md`](04-worker-and-no-storage.md).
7. **Size the host** for 2 concurrent workers — see
   [`05-system-requirements.md`](05-system-requirements.md).
8. **Verify end-to-end:** submit a job, confirm the 202, then confirm the
   callback receives `{video_id, detections_json, detections_vtt, status}` and
   that no temp files remain on the host after the worker exits.

---

## Operational verification (the "no storage" proof)

After a job completes on a single box:

```powershell
# no worker containers should linger (they run with --rm)
docker ps -a --filter "ancestor=object-detection-process:latest"

# no job temp dirs left on the host (workers use container-local /tmp)
# (nothing to check on the host because temp lived inside the --rm container)
```

The only place the result exists afterward is wherever the **caller** saved it
from the callback POST. That is the intended design.

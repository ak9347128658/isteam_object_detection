# Architecture — Event-Driven Video Object Detection

This document describes **Architecture 1**: a fully event-driven, one-shot
containerized pipeline that turns an uploaded video into `detections.json` +
`detections.vtt`, stores the results back in S3, and notifies a callback URL.

The heavy CV/ML logic is unchanged from the notebook / `backend/` service
(`pipeline_utils.py`: ingest → frame sampling → YOLOE detection → CLIP dedup →
S3 crop upload → Google Lens match → metadata). This architecture only changes
**how the work is triggered and where it runs**: instead of a long-running API,
each video gets its **own short-lived container** that starts, processes one
video, uploads, calls back, and shuts down.

---

## High-level flow

```
        (1) upload video
   ┌──────────────────────────┐
   │  Client / Uploader        │
   └─────────────┬────────────┘
                 │  PUT s3://<input-bucket>/uploads/<video>.mp4
                 ▼
        ┌───────────────────┐
        │   S3 input bucket  │
        └─────────┬─────────┘
                  │  (2) s3:ObjectCreated event
                  ▼
        ┌───────────────────┐
        │   Lambda (trigger) │   enqueue.py
        └─────────┬─────────┘
                  │  (3) SendMessage { s3_uri, callback_url, job_id }
                  ▼
        ┌───────────────────┐
        │   SQS queue        │   detection-jobs
        └─────────┬─────────┘
                  │  (4) dispatcher pulls a message ONLY when a slot is free
                  ▼
        ┌────────────────────────────────────────────────┐
        │  Dispatcher (concurrency cap = MAX_CONCURRENCY=3)│  dispatcher.py
        │  - tracks running containers                     │
        │  - running < 3 AND queue not empty -> launch one │
        │  - a container exits -> free a slot -> refill     │
        └─────────┬───────────────┬───────────────┬───────┘
                  │ slot 1        │ slot 2        │ slot 3     (never more than 3)
                  ▼               ▼               ▼
   ┌──────────────────────────────────────────────────────────┐
   │  object_detection_process  (Docker image, one-shot task)   │
   │                                                            │
   │  env: VIDEO_S3_URI, CALLBACK_URL, JOB_ID, OUTPUT_BUCKET…   │
   │                                                            │
   │  ingest → frames → detect → dedup → crops→S3 → match       │
   │        → write detections.json / .vtt                      │
   │  (5) upload detections.json + .vtt → s3://<output-bucket>  │
   │  (6) POST callback_url { video, json, vtt, job_id, status} │
   │  (7) exit  → slot freed → dispatcher refills from queue     │
   └──────────────────────────────────────────────────────────┘
                  │ (5)                        │ (6)
                  ▼                            ▼
        ┌───────────────────┐        ┌───────────────────┐
        │  S3 output bucket  │        │  Callback service  │
        │  detections/<sfx>  │        │  (your backend)    │
        └───────────────────┘        └───────────────────┘
```

---

## Step-by-step

### 1. Upload
A client uploads a video to the **input S3 bucket** (e.g.
`s3://isteam-video-input/uploads/my-clip.mp4`). Nothing else is required from
the client.

### 2. S3 event → Lambda
The input bucket has an **S3 event notification** (`s3:ObjectCreated:*`, filtered
to the `uploads/` prefix and video extensions) wired to a small Lambda function
(`lambda/enqueue.py`). The Lambda is intentionally tiny — it does **no** video
processing, so it stays well within Lambda's time/memory limits.

### 3. Lambda → SQS
The Lambda builds a job message and calls `sqs:SendMessage` on the
**detection-jobs** queue:

```json
{
  "job_id": "3f9c2a...",
  "s3_uri": "s3://isteam-video-input/uploads/my-clip.mp4",
  "bucket": "isteam-video-input",
  "key": "uploads/my-clip.mp4",
  "callback_url": "https://api.example.com/detections/callback",
  "output_bucket": "isteam-video-output",
  "skip_matching": false,
  "enqueued_at": "2026-09-03T10:00:00Z"
}
```

The `callback_url` and `output_bucket` come from Lambda environment variables so
they are configured once at deploy time (they can also be overridden per-object
via S3 object metadata / tags).

SQS gives us durability, retries, and back-pressure: while all 3 slots are busy,
messages simply wait in the queue until the dispatcher frees a slot.

### 4. SQS → dispatcher (max 3 concurrent) → one container per video
A **dispatcher** owns concurrency. It is the only thing that pulls from SQS, and
it enforces a hard cap of **`MAX_CONCURRENCY` (default 3)** running containers:

- It tracks how many worker containers are currently running.
- **While** `running < MAX_CONCURRENCY` **and** the queue has messages, it
  receives one message and launches one container for it, passing the video S3
  link as the `VIDEO_S3_URI` environment variable.
- When it already has 3 running, it **stops pulling** — messages stay safely in
  SQS (invisible only for the ones in flight).
- When any container **exits**, that frees a slot. The dispatcher immediately
  checks the queue and, if anything is waiting, launches the next one. So there
  are never more than 3 at a time, and a new one starts the moment one finishes
  as long as work remains.

This is exactly the requested behavior: *at most 3 Docker containers run at
once; when one stops, if the queue still has data, another starts.*

`dispatcher.py` supports two backends for actually launching the container:

- **`docker`** (single box / EC2 / GPU host): runs `docker run` per job. Simple,
  great for one machine with 3 GPUs or 3 CPU slots.
- **`ecs`** (Fargate / ECS cluster): calls ECS `RunTask` per job and watches task
  status to know when a slot frees. No servers to manage.

Alternatively, the same cap can be expressed **natively in ECS** by running the
worker as an ECS **Service** with `desiredCount = 3` and each task in `--poll`
mode; ECS keeps exactly 3 pollers alive and replaces any that exit. The
dispatcher is the portable option that also works with plain Docker.

The video S3 link is passed to the container as an **environment variable**
(`VIDEO_S3_URI`), exactly as requested. The container itself is unchanged and
knows nothing about concurrency — the dispatcher is the sole gatekeeper.

### 5. Process + upload results to S3
Inside the container, `worker.py` runs the standard pipeline stages and writes:

- `detections.json` — timestamped products + recommendations
- `detections.vtt`  — WebVTT track for HTML5 `<video>` overlay

Both are uploaded to the **output bucket** under a key that carries a **unique
suffix** so results never collide:

```
s3://<output-bucket>/detections/<suffix>/detections_<suffix>.json
s3://<output-bucket>/detections/<suffix>/detections_<suffix>.vtt
```

The unique suffix = `<video_id>-<8charJobId>-<UTCtimestamp>`, and it appears in
BOTH the folder and the filenames (`detections_<suffix>.json`), so every file is
uniquely named on its own. Product **crop** images are uploaded during matching
(needed for Google Lens) exactly as today.

### 6. Callback (POST)
After a successful upload, the container sends a **POST** to `CALLBACK_URL`:

```json
{
  "job_id": "3f9c2a...",
  "status": "completed",
  "video_s3_uri": "s3://isteam-video-input/uploads/my-clip.mp4",
  "detections_json_s3_uri": "s3://isteam-video-output/detections/my-clip-3f9c2a1b-20260903T100200Z/detections.json",
  "detections_vtt_s3_uri":  "s3://isteam-video-output/detections/my-clip-3f9c2a1b-20260903T100200Z/detections.vtt",
  "detections_json_url": "https://<presigned or public url>",
  "detections_vtt_url":  "https://<presigned or public url>",
  "product_count": 3,
  "unique_suffix": "my-clip-3f9c2a1b-20260903T100200Z",
  "finished_at": "2026-09-03T10:02:00Z"
}
```

On failure the same endpoint receives `{"status": "failed", "error": "..."}` so
the caller always hears back. The POST itself is retried with backoff.

### 7. Shutdown
Once the callback returns 2xx (or retries are exhausted), the container exits
with code `0` on success (or non-zero on failure so the orchestrator can retry /
send the SQS message to a dead-letter queue). The container is then destroyed —
no idle cost.

---

## Why this shape

| Concern | How it's handled |
|---|---|
| **Spiky load** | SQS absorbs bursts; the dispatcher drains the queue 3 at a time. |
| **Bounded concurrency** | Dispatcher hard-caps running containers at `MAX_CONCURRENCY` (=3), so you never overload the GPU/host or SerpApi. A finished container immediately frees a slot and the next queued job starts. |
| **Long jobs** | Video processing runs in a container (minutes), not Lambda. |
| **Cold model loads** | Weights are **baked into the image** via `prefetch_models.py` at build time, so no download at runtime. |
| **Failure isolation** | One bad video can't take down others; each has its own container. |
| **At-least-once delivery** | SQS visibility timeout + DLQ; callback is idempotent via `job_id` + unique suffix. |
| **Cost** | No always-on server (docker/ecs mode); pay only while up to 3 videos are processing. |

---

## Components in this repo

| Path | Role |
|---|---|
| `lambda/enqueue.py` | S3-triggered Lambda that puts a job on SQS. |
| `dispatcher/dispatcher.py` | Pulls from SQS and keeps **at most 3** worker containers running; refills a slot as soon as one exits. |
| `object_detection_process/` | The one-shot worker Docker image. |
| `object_detection_process/worker.py` | Entrypoint: read env → process → upload → callback → exit. |
| `object_detection_process/pipeline_utils.py` | The unchanged CV/ML pipeline. |
| `object_detection_process/scripts/prefetch_models.py` | Bakes model weights into the image at build time. |
| `object_detection_process/Dockerfile` | Builds the worker image (models prefetched in a layer). |

### Concurrency control (the "max 3" rule)

The dispatcher is a tiny loop:

```
loop forever:
    reap any containers that have exited        # frees slots
    while running_count < MAX_CONCURRENCY:
        msg = sqs.receive_message(wait=20s)      # long poll
        if no msg: break                         # queue empty, wait
        launch_worker(msg)                       # docker run / ecs RunTask
        running_count += 1
    sleep briefly
```

- Never more than `MAX_CONCURRENCY` (3) containers exist at any instant.
- The instant a worker exits, `reap` frees its slot and the `while` loop pulls
  the next queued message — so a new container starts as soon as one stops,
  provided the queue is non-empty.
- If the queue is empty, the dispatcher idles cheaply on SQS long-polling and
  starts nothing until a new message arrives.

---

## Environment variables (container)

| Var | Required | Meaning |
|---|---|---|
| `VIDEO_S3_URI` | yes* | `s3://bucket/key` of the input video. Passed by the launcher. |
| `CALLBACK_URL` | yes | URL that receives the POST when done. |
| `JOB_ID` | no | Correlation id; auto-generated if absent. |
| `OUTPUT_BUCKET` | yes | Bucket for `detections.json` / `.vtt` (defaults to `s3.bucket` in config). |
| `OUTPUT_PREFIX` | no | Key prefix for outputs (default `detections`). |
| `SKIP_MATCHING` | no | `true` to skip S3 crop upload + Google Lens. |
| `SQS_QUEUE_URL` | poll mode | If set with `--poll`, the worker long-polls SQS instead of using `VIDEO_S3_URI`. |
| `AWS_*` / `SERPAPI_API_KEY` | as needed | Credentials for S3 + Google Lens. |

\* Required unless running in `--poll` mode, where each SQS message supplies it.

---

## Deploy checklist

1. Create the two S3 buckets (input, output) and the SQS queue (+ DLQ).
2. Build & push the image:
   `docker build -t <ecr>/object-detection-process object_detection_process`
   (model weights are prefetched during the build).
3. Deploy `lambda/enqueue.py` with env `QUEUE_URL`, `CALLBACK_URL`,
   `OUTPUT_BUCKET`; add the S3 `ObjectCreated` trigger on the input bucket.
4. Run the dispatcher with `MAX_CONCURRENCY=3` (docker mode on a box, or ecs
   mode against your cluster). It maps each SQS message to container env
   (`VIDEO_S3_URI`, `CALLBACK_URL`, `JOB_ID`, `OUTPUT_BUCKET`) and enforces the
   3-at-a-time cap. Alternatively run an ECS Service with `desiredCount=3` of
   the worker in `--poll` mode.
5. Give the task/dispatcher role S3 read (input), S3 write (output), SQS
   receive/delete, and outbound HTTPS for the callback + Google Lens.

# Architecture — On-Demand Video Object Detection (API-triggered)

This document describes **Architecture 001**: a lightweight, API-triggered
pipeline that turns an uploaded video into `detections.json` +
`detections.vtt` and delivers those results straight back to a callback URL
(your backend service). Results are streamed inline in the callback body — the
pipeline holds them in memory for the lifetime of the job and hands them off
directly, so there is no intermediate object store to manage.

The heavy CV/ML logic is unchanged from the notebook / `backend/` service
(`pipeline_utils.py`: ingest → frame sampling → YOLOE detection → CLIP dedup →
Google Lens match → metadata). This architecture defines **how work is
triggered, how concurrency is bounded, and how results are returned**: a single
POST request kicks off a processor, and a queue guarantees that **at most two
videos are processed at the same time**.

---

## High-level flow

```
        (1) POST /jobs  { video_url, callback_url }
   ┌──────────────────────────┐
   │  Client / Uploader        │
   └─────────────┬────────────┘
                 │  HTTPS POST (video URL + callback URL)
                 ▼
        ┌───────────────────────┐
        │  Lambda (enqueue API)  │   enqueue.py
        │  - validates payload   │
        │  - creates job_id      │
        └─────────┬─────────────┘
                  │  (2) enqueue job { job_id, video_url, callback_url }
                  ▼
        ┌───────────────────────┐
        │   Job queue           │   detection-jobs
        └─────────┬─────────────┘
                  │  (3) dispatcher pulls a job ONLY when a slot is free
                  ▼
        ┌────────────────────────────────────────────────┐
        │  Dispatcher (concurrency cap = MAX_CONCURRENCY=2)│  dispatcher.py
        │  - tracks running processors                     │
        │  - running < 2 AND queue not empty -> launch one │
        │  - a processor exits -> free a slot -> refill     │
        └─────────┬──────────────────────┬────────────────┘
                  │ slot 1                │ slot 2       (never more than 2)
                  ▼                       ▼
   ┌──────────────────────────────────────────────────────────┐
   │  object_detection_process  (one-shot processor)            │
   │                                                            │
   │  env: VIDEO_URL, CALLBACK_URL, JOB_ID                      │
   │                                                            │
   │  ingest → frames → detect → dedup → match                  │
   │        → build detections.json / .vtt (in memory)          │
   │  (4) POST callback_url { job_id, status, json, vtt }       │
   │  (5) exit  → slot freed → dispatcher refills from queue     │
   └────────────────────────────┬─────────────────────────────┘
                                 │ (4)
                                 ▼
                       ┌───────────────────┐
                       │  Callback service  │
                       │  (your backend)    │
                       └───────────────────┘
```

---

## Step-by-step

### 1. Trigger — POST the video URL
When a video is uploaded, the uploading service sends a single **POST** to the
enqueue API with the video's URL and the callback URL that should receive the
results:

```http
POST /jobs HTTP/1.1
Content-Type: application/json

{
  "video_url": "https://cdn.example.com/uploads/my-clip.mp4",
  "callback_url": "https://api.example.com/detections/callback",
  "skip_matching": false
}
```

Nothing else is required from the caller. The API responds immediately with a
`job_id` so the caller can correlate the later callback:

```json
{ "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33", "status": "queued" }
```

### 2. API → queue
The enqueue Lambda (`lambda/enqueue.py`) is intentionally tiny — it does **no**
video processing, so it stays well within the platform's time/memory limits. It
validates the request, mints a `job_id`, and places a job message on the
**detection-jobs** queue:

```json
{
  "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33",
  "video_url": "https://cdn.example.com/uploads/my-clip.mp4",
  "callback_url": "https://api.example.com/detections/callback",
  "skip_matching": false,
  "enqueued_at": "2026-09-07T10:00:00Z"
}
```

The queue provides durability, retries, and back-pressure: while both slots are
busy, jobs simply wait until the dispatcher frees a slot.

### 3. Queue → dispatcher (max 2 concurrent) → one processor per video
A **dispatcher** owns concurrency. It is the only thing that pulls from the
queue, and it enforces a hard cap of **`MAX_CONCURRENCY` (default 2)** running
processors:

- It tracks how many processors are currently running.
- **While** `running < MAX_CONCURRENCY` **and** the queue has jobs, it takes one
  job and launches one processor for it, passing the video URL as the
  `VIDEO_URL` environment variable.
- When it already has 2 running, it **stops pulling** — jobs stay safely in the
  queue.
- When any processor **exits**, that frees a slot. The dispatcher immediately
  checks the queue and, if anything is waiting, launches the next one. So there
  are never more than 2 at a time, and a new one starts the moment one finishes
  as long as work remains.

This is the requested behavior: *at most 2 processors run at once; when one
stops, if the queue still has data, another starts.*

`dispatcher.py` launches each job as a one-shot processor (a local subprocess or
container, depending on where it runs) and watches its exit to know when a slot
frees. The processor itself is unchanged and knows nothing about concurrency —
the dispatcher is the sole gatekeeper.

The video URL is passed to the processor as an **environment variable**
(`VIDEO_URL`), along with `CALLBACK_URL` and `JOB_ID`.

### 4. Process + return results via callback
Inside the processor, the standard pipeline stages run and produce two artifacts
held in memory:

- `detections.json` — timestamped products + recommendations
- `detections.vtt`  — WebVTT track for HTML5 `<video>` overlay

As soon as processing completes, the processor sends a **POST** to
`CALLBACK_URL` carrying both artifacts inline in the body:

```json
{
  "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33",
  "status": "completed",
  "video_url": "https://cdn.example.com/uploads/my-clip.mp4",
  "product_count": 3,
  "detections_json": { "video": "my-clip.mp4", "products": [ ... ] },
  "detections_vtt": "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n...",
  "finished_at": "2026-09-07T10:02:00Z"
}
```

- `detections_json` is the full detection document as a JSON object.
- `detections_vtt` is the WebVTT track as a plain string.

On failure the same endpoint receives `{"status": "failed", "error": "..."}` so
the caller always hears back. The POST is retried with backoff, and the callback
is idempotent via `job_id`.

### 5. Shutdown
Once the callback returns 2xx (or retries are exhausted), the processor exits
with code `0` on success (or non-zero on failure so the dispatcher can retry or
route the job to a dead-letter queue). The processor is then torn down — no idle
cost between jobs.

---

## Why this shape

| Concern | How it's handled |
|---|---|
| **Simple trigger** | A single POST with the video URL starts everything — no bucket wiring or event plumbing. |
| **Spiky load** | The queue absorbs bursts; the dispatcher drains it 2 at a time. |
| **Bounded concurrency** | Dispatcher hard-caps running processors at `MAX_CONCURRENCY` (=2), so you never overload the GPU/host or the matching backend. A finished processor immediately frees a slot and the next queued job starts. |
| **Long jobs** | Video processing runs in a dedicated processor (minutes), not the enqueue API. |
| **Cold model loads** | Weights are **baked into the image** via `prefetch_models.py` at build time, so no download at runtime. |
| **Failure isolation** | One bad video can't take down others; each has its own processor. |
| **Direct delivery** | Results are streamed back inline to the callback URL, so the caller gets `detections.json` + `.vtt` in one hop with nothing extra to fetch. |
| **At-least-once delivery** | Queue visibility + DLQ; callback is idempotent via `job_id`. |

---

## Components in this repo

| Path | Role |
|---|---|
| `lambda/enqueue.py` | API endpoint that validates the POST and puts a job on the queue. |
| `dispatcher/dispatcher.py` | Pulls from the queue and keeps **at most 2** processors running; refills a slot as soon as one exits. |
| `object_detection_process/` | The one-shot processor. |
| `object_detection_process/worker.py` | Entrypoint: read env → process → callback → exit. |
| `object_detection_process/pipeline_utils.py` | The unchanged CV/ML pipeline. |
| `object_detection_process/scripts/prefetch_models.py` | Bakes model weights into the image at build time. |
| `object_detection_process/Dockerfile` | Builds the processor image (models prefetched in a layer). |

### Concurrency control (the "max 2" rule)

The dispatcher is a tiny loop:

```
loop forever:
    reap any processors that have exited        # frees slots
    while running_count < MAX_CONCURRENCY:
        job = queue.receive(wait=20s)            # long poll
        if no job: break                         # queue empty, wait
        launch_worker(job)                       # subprocess / container
        running_count += 1
    sleep briefly
```

- Never more than `MAX_CONCURRENCY` (2) processors exist at any instant.
- The instant a processor exits, `reap` frees its slot and the `while` loop
  pulls the next queued job — so a new processor starts as soon as one stops,
  provided the queue is non-empty.
- If the queue is empty, the dispatcher idles cheaply on long-polling and starts
  nothing until a new job arrives.

---

## Environment variables (processor)

| Var | Required | Meaning |
|---|---|---|
| `VIDEO_URL` | yes* | URL of the input video. Passed by the dispatcher. |
| `CALLBACK_URL` | yes | URL that receives the POST with the results when done. |
| `JOB_ID` | no | Correlation id; auto-generated if absent. |
| `SKIP_MATCHING` | no | `true` to skip Google Lens matching. |
| `QUEUE_URL` | poll mode | If set with `--poll`, the processor long-polls the queue instead of using `VIDEO_URL`. |
| `SERPAPI_API_KEY` | as needed | Credential for Google Lens matching. |

\* Required unless running in `--poll` mode, where each queued job supplies it.

---

## Deploy checklist

1. Create the job queue (+ DLQ).
2. Build & push the processor image:
   `docker build -t <registry>/object-detection-process object_detection_process`
   (model weights are prefetched during the build).
3. Deploy `lambda/enqueue.py` with env `QUEUE_URL`; expose it as an HTTP POST
   endpoint (`POST /jobs`).
4. Run the dispatcher with `MAX_CONCURRENCY=2`. It maps each queued job to
   processor env (`VIDEO_URL`, `CALLBACK_URL`, `JOB_ID`) and enforces the
   2-at-a-time cap.
5. Give the processor/dispatcher role outbound HTTPS for downloading the video,
   posting the callback, and Google Lens.

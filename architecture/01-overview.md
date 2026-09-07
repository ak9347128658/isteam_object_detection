# Overview & High-Level Flow

## Goal

Turn a video (referenced by a URL) into two artifacts — `detections.json`
(timestamped products + ecommerce recommendations) and `detections.vtt` (a
WebVTT track for HTML5 `<video>` overlay) — and hand them **directly** to the
caller's `callback_url`, tagged with the caller's `video_id`. No storage, and
never more than two videos processed at once.

Work is **triggered by a synchronous API** and results are **delivered by a
direct callback POST** to the caller. The detection logic itself is the same
proven pipeline; this document focuses on how a job flows through the system
end to end.

---

## High-level flow

```
 (1) POST /detect
     { video_url, callback_url, video_id }
┌──────────────────────────┐
│   Caller / Client backend │◀──────────────────────────┐
└─────────────┬────────────┘                            │
              │                                          │ (7) POST callback
              ▼                                          │     { video_id, status,
     ┌───────────────────────────────┐                  │       detections_json,
     │      Backend API (FastAPI)     │                  │       detections_vtt }
     │  - validate input              │                  │
     │  - (2) return 202 "started"    │──────────────────┘
     │  - (3) enqueue { job }         │
     └─────────────┬─────────────────┘
                   │  (3) put job on the queue
                   ▼
        ┌────────────────────────┐
        │        Queue           │   in-proc queue | Redis | SQS
        │  (durable, FIFO-ish)   │
        └───────────┬────────────┘
                    │  (4) dispatcher pulls a job ONLY when a slot is free
                    ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Dispatcher  (concurrency cap = MAX_CONCURRENCY = 2)        │
   │  - tracks running worker containers                        │
   │  - running < 2 AND queue not empty -> launch ONE worker    │
   │  - a worker exits -> free a slot -> pull the next job       │
   └───────────┬───────────────────────────┬───────────────────┘
               │ slot 1                     │ slot 2      (never more than 2)
               ▼                            ▼
   ┌───────────────────────────┐  ┌───────────────────────────┐
   │  Docker worker (one-shot)  │  │  Docker worker (one-shot)  │
   │  env: VIDEO_URL,           │  │  env: VIDEO_URL,           │
   │       CALLBACK_URL,        │  │       CALLBACK_URL,        │
   │       VIDEO_ID             │  │       VIDEO_ID             │
   │                            │  │                            │
   │  (5) download video → RAM/ │  │  ...                       │
   │      temp dir              │  │                            │
   │  detect → dedup → match    │  │                            │
   │  build detections.json/vtt │  │                            │
   │  (6) POST both to callback │  │                            │
   │  (8) rm -rf temp; exit 0   │  │                            │
   └───────────────────────────┘  └───────────────────────────┘
```

## Step-by-step

### 1. Caller submits a job
The caller sends `POST /detect` with a JSON body:

```json
{
  "video_url": "https://cdn.example.com/clips/abc.mp4",
  "callback_url": "https://caller.example.com/hooks/detections",
  "video_id": "abc-123"
}
```

`video_url` can be any direct http(s) link (hosted videos also work).

### 2. API answers immediately
The API validates the three fields, generates an internal `job_id` for tracing,
enqueues the job, and returns **HTTP 202 Accepted** right away:

```json
{ "status": "PROCESSING_STARTED", "video_id": "abc-123", "job_id": "9f3c..." }
```

The caller does **not** wait for processing. It will hear the result later on
its `callback_url`.

#### Job status values (enum)

`status` is always one of a fixed set of values, used both in the immediate API
response and in the later callback:

| Status | Where it appears | Meaning |
|---|---|---|
| `PROCESSING_STARTED` | API response | Job accepted, validated, and queued. Work has not necessarily begun yet. |
| `QUEUED` | (internal / `GET /stats`) | Job is waiting in the queue for a free worker slot. |
| `PROCESSING` | (internal / `GET /stats`) | A worker has picked up the job and is actively processing the video. |
| `COMPLETED` | Callback | Processing finished successfully; `detections_json` + `detections_vtt` are included. |
| `FAILED` | Callback | Processing could not complete (bad `video_url`, decode error, pipeline error); an `error` field is included. |
| `REJECTED` | API response | The request was refused before queueing (invalid input, callback host not allowed, or queue full). |

```python
# Reference enum (shared by API + worker)
from enum import Enum

class JobStatus(str, Enum):
    PROCESSING_STARTED = "PROCESSING_STARTED"  # 202 response: accepted + queued
    QUEUED             = "QUEUED"              # waiting for a worker slot
    PROCESSING         = "PROCESSING"          # worker is running the pipeline
    COMPLETED          = "COMPLETED"           # success; results in callback
    FAILED             = "FAILED"              # failure; error in callback
    REJECTED           = "REJECTED"            # refused before queueing
```

The full request/response and callback contracts (including the `COMPLETED` /
`FAILED` callback payloads) are covered in the API contract document.

### 3. Enqueue
The job (`video_url`, `callback_url`, `video_id`, `job_id`) is placed on the
**queue**. The queue is the buffer that absorbs bursts: if both worker slots are
busy, jobs simply wait here.

### 4. Dispatcher gates concurrency (max 2)
A dispatcher is the **only** consumer of the queue and the **only** thing that
launches workers. It enforces a hard cap of **2** running workers:

- while `running < 2` **and** the queue is non-empty → pull one job, launch one
  worker;
- when a worker exits → a slot frees → immediately pull and launch the next
  queued job.

So there are never more than two workers, and a new one starts the moment one
finishes as long as work remains — exactly the requirement.

### 5. Worker processes ONE video
Each worker is a short-lived Docker container. It reads `VIDEO_URL`,
`CALLBACK_URL`, `VIDEO_ID` from its environment, downloads the video into a
**per-job temp directory** (never a shared/persistent volume), and runs the
pipeline: frame sampling → YOLOE detection → CLIP dedup → Google Lens match →
build `detections.json` + `detections.vtt` **in that temp dir**.

### 6 & 7. Direct callback (no storage)
The worker reads the two generated files and **POSTs them to `callback_url`**
along with `video_id` and status. It does **not** persist them anywhere — the
callback is the only delivery mechanism.

### 8. Clean up + exit
After the callback is delivered (or retries are exhausted), the worker
**deletes its temp directory** and exits. Success = exit 0; failure = non-zero
so the orchestrator can retry / dead-letter the job. No files, crops, or video
remain.

---

## Downstream: who stores and serves the `.json` / `.vtt`

This detection system is **stateless by design** — it produces the two files and
hands them off via the callback, then forgets everything. Persistence and
playback are owned by other teams:

- **iSteam backend team — storage & serving.** When the callback POST arrives,
  the **iSteam backend** stores the `detections.json` and `detections.vtt`
  against the `video_id` (in its own database / object store). This is the
  system of record for detections; our workers keep nothing. At **video-start**
  in the player, the iSteam backend **serves** the matching `.json` / `.vtt` for
  that `video_id` to the frontend (e.g. as a `<track>` source and/or an API
  response).

- **Frontend team — display.** The **frontend** consumes the `.vtt` / `.json`
  to render **product recommendations synchronized to the video timeline**. Both
  files carry **timestamped product recommendations**, so as the video plays the
  UI can surface the right shoppable products at the right moment: the `.vtt`
  drives the native HTML5 `<video>` overlay cues, and the `.json` provides the
  richer per-product data (label, occurrence timeline, and ecommerce
  recommendations with match scores) for a custom overlay/panel.

```
Worker ──callback {video_id, detections_json, detections_vtt}──▶ iSteam backend
                                                                    │  stores by video_id
                                                    (on video start)│  serves .json/.vtt
                                                                    ▼
                                                              Frontend player
                                                     (overlays timestamped product
                                                      recommendations on the timeline)
```

> In this architecture, the caller's `callback_url` **is** the iSteam backend
> endpoint. "No storage" applies only to *this* detection system; the iSteam
> backend deliberately persists the results so the frontend can replay them on
> every view without re-processing the video.

---

## Why this approach is best: process once at upload, not on every stream

The core reason for this design is **cost**. Today, detection runs on the
**streaming path** — every time a video is streamed/played, the backend runs the
detection pipeline again for that stream. That means the expensive work
(YOLOE inference, CLIP dedup, and especially the **SerpApi / Google Lens**
calls) is repeated **per view**. As traffic grows, the cost scales with the
number of *streams*, not the number of *videos* — the same video watched 10,000
times pays for detection and SerpApi 10,000 times.

This architecture moves detection **off the streaming path and onto the upload
path**: a video is processed **exactly once**, when it is uploaded (or first
registered), and the resulting `.json` / `.vtt` are stored by the iSteam backend
and simply **served** on every subsequent play. Playback becomes a cheap static
file read — no GPU, no model inference, no SerpApi call.

### Current (detect on every stream) vs. this (detect once at upload)

| | Current: detect during streaming | This: detect once at upload |
|---|---|---|
| **When detection runs** | On every stream / playback. | One time per video, at upload. |
| **Cost driver** | Number of **views** (streams). | Number of **videos**. |
| **SerpApi (Google Lens) spend** | One set of lookups **per view** — repeated for the same product every time. | One set of lookups **per video**, then reused for all views. |
| **GPU / compute load** | Grows with concurrent viewers; a popular video multiplies the load. | Bounded and predictable; capped at **2** concurrent jobs regardless of how many people are watching. |
| **Playback latency** | Viewer waits for detection + matching before overlays appear. | Overlays are already computed; the player just loads the stored `.vtt` / `.json`. |
| **Scaling risk** | A traffic spike on a viral video can overload the detection service and blow through SerpApi rate limits/quota. | Streaming scales independently of detection; a view spike only hits static-file serving. |

### Why this is a big saving

- **SerpApi is billed per query.** Running Lens once per video instead of once
  per view is the largest single cost reduction — often orders of magnitude for
  any video with meaningful watch counts.
- **Detection is redundant on the stream path.** The same video always yields
  the same products and timestamps, so re-detecting per view produces identical
  results at repeated GPU cost. Computing it once and caching the output removes
  that waste entirely.
- **Predictable capacity.** Because detection is decoupled from viewing and
  capped at 2 concurrent workers, compute and third-party spend stay flat and
  forecastable even as viewership grows.
- **Better viewer experience.** Recommendations are ready before playback
  starts, so there is no per-view processing delay.

In short: **do the expensive work once, at upload; serve the cheap result many
times, at stream.** That is why the upload-time, callback-delivered,
process-once model is the right shape for iSteam.

---

## Component responsibilities

| Component | Responsibility | Does NOT do |
|---|---|---|
| **Backend API** | Validate, return 202 immediately, enqueue. | No CV work, no waiting, no storage. |
| **Queue** | Durable buffer + at-least-once delivery + retries/DLQ. | No processing. |
| **Dispatcher** | Enforce "max 2 workers", launch/reap containers, refill slots. | No CV work; doesn't touch the callback. |
| **Worker (Docker)** | Download → detect → match → build files → POST callback → delete → exit. | No output persistence, no long-lived state. |
| **iSteam backend (callback endpoint)** | Receive `{video_id, json, vtt, status}`, **store** it by `video_id`, and **serve** it to the frontend at video-start. | (Owned by the iSteam backend team, outside this system.) |
| **Frontend** | Use the `.vtt` / `.json` to display **timestamped product recommendations** synced to the video timeline. | (Owned by the frontend team, outside this system.) |

---

## Why this shape

| Concern | How it's handled |
|---|---|
| **Fast API response** | The API only validates + enqueues, so it returns in milliseconds; heavy work is offloaded to workers. |
| **No storage** | The worker keeps everything in a per-job temp dir and deletes it on exit; results go only to the callback. Privacy-friendly and zero storage cost. |
| **Bounded load** | The dispatcher hard-caps workers at 2, protecting CPU/GPU/RAM and the SerpApi rate limit. |
| **Spiky traffic** | The queue absorbs bursts; excess jobs wait instead of overloading the box. |
| **Failure isolation** | One bad video only fails its own container; others are unaffected. |
| **At-least-once** | The queue re-delivers on worker crash; the callback is idempotent via `video_id` + `job_id`. |
| **No cold model loads** | Model weights are baked into the worker image at build time, so there are no runtime downloads. |

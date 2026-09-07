# Architecture 2 — API-Triggered, No-Storage, Queue-Bounded Object Detection

This folder documents **Architecture 2**: a synchronous API front door that
accepts a job, immediately answers `"processing started"`, runs the video
through the existing detection pipeline inside a Docker worker, and **POSTs the
generated `.json` + `.vtt` straight to the caller's `callback_url`** — keyed by
`video_id`.

The defining rules of this architecture (from the requirements):

1. **Backend API** receives `callback_url`, `video_url`, `video_id`.
2. When the API is hit it **immediately returns a "process started" message**
   and enqueues the job (fire-and-forget for the caller).
3. Work runs inside the **existing Docker worker** (`object_detection_process/`),
   which does ingest → frame sampling → YOLOE detection → CLIP dedup → Google
   Lens match → `detections.json` + `detections.vtt`.
4. The generated `.vtt` and `.json` are **POSTed directly to `callback_url`**
   together with `video_id`.
5. **Nothing is stored.** No video, no crops, no output files persist anywhere
   after a job finishes. Everything lives in a per-job temp dir that is deleted.
6. A **queue** feeds the workers, and **at most 2 Docker processes run at a
   time**. When one finishes, if the queue still has work, the next one starts.

> This is a different trigger + delivery model than
> [`../architecture.md`](../architecture.md) (Architecture 1), which is
> S3-upload → Lambda → SQS and stores results back in S3. Here the trigger is a
> **synchronous HTTP API** and delivery is a **direct callback with no storage**.

---

## Documents in this folder

| File | What it covers |
|---|---|
| [`01-overview.md`](01-overview.md) | High-level flow diagram + component responsibilities + why this shape. |
| [`02-api-contract.md`](02-api-contract.md) | The `POST /detect` request/response, the callback payload, and error semantics. |
| [`03-queue-and-concurrency.md`](03-queue-and-concurrency.md) | The queue, the "max 2 workers" gate, backpressure, retries, and idempotency. |
| [`04-worker-and-no-storage.md`](04-worker-and-no-storage.md) | How the worker runs one video and guarantees nothing is persisted. |
| [`05-system-requirements.md`](05-system-requirements.md) | Concrete hardware/OS/software sizing for 2 concurrent CPU (or GPU) workers. |
| [`06-configuration.md`](06-configuration.md) | Every environment variable + `config.yaml` key this architecture depends on. |
| [`07-deployment.md`](07-deployment.md) | Docker Compose (single box) and ECS/Fargate deployment recipes + checklist. |

---

## The 60-second version

```
Caller ──POST /detect {video_url, callback_url, video_id}──▶ Backend API
Backend API ──"processing started" (202)──▶ Caller           (returns instantly)
Backend API ──enqueue job──▶ Queue
Dispatcher ──(only while running < 2)──▶ launches Docker worker
Worker: download video ▶ detect ▶ dedup ▶ match ▶ build .json + .vtt (in RAM/temp)
Worker ──POST {video_id, detections_json, detections_vtt, status}──▶ callback_url
Worker deletes its temp dir and exits ▶ frees a slot ▶ dispatcher starts the next queued job
```

No S3 for input/output, no database of results, no lingering files. The only
state that survives a job is whatever the **caller** stores when it receives the
callback.

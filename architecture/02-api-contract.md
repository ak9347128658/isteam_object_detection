# 02 — API Contract & Callback Contract

Two contracts matter in this architecture:

1. **Inbound** — what the caller sends to our Backend API (`POST /detect`).
2. **Outbound** — what the worker POSTs to the caller's `callback_url`.

---

## 1. Inbound: `POST /detect`

### Request

`Content-Type: application/json`

```json
{
  "video_url": "https://cdn.example.com/clips/abc.mp4",
  "callback_url": "https://caller.example.com/hooks/detections",
  "video_id": "abc-123"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `video_url` | string (URL) | yes | Direct http(s) link to a video. Also accepts video-host URLs (worker uses `yt-dlp`). |
| `callback_url` | string (URL) | yes | Where the result is POSTed. Must be reachable from the workers. |
| `video_id` | string | yes | The caller's own id. Echoed back in the callback so the caller can correlate. Opaque to us. |

Validation performed by the API:

- All three fields present and non-empty.
- `video_url` and `callback_url` are syntactically valid http/https URLs.
- Optional allow-list check on `callback_url` host (recommended, to prevent the
  system being used to POST to arbitrary internal addresses — see SSRF note in
  [`05-system-requirements.md`](05-system-requirements.md)).

### Response — success (enqueued)

**HTTP 202 Accepted** (returned immediately, before any processing):

```json
{
  "status": "processing_started",
  "video_id": "abc-123",
  "job_id": "9f3c2a1b7d5e4c6a",
  "message": "Job accepted and queued for processing."
}
```

- `job_id` — an internal correlation id we generate; useful for log tracing and
  for the caller to match the later callback if it wants (it is also included in
  the callback payload).
- The response is **not** a result. The actual detections arrive later via the
  callback.

### Response — rejected

| HTTP | When | Body |
|---|---|---|
| `400 Bad Request` | Missing/invalid field, bad URL. | `{ "status": "rejected", "error": "video_url is required" }` |
| `403 Forbidden` | `callback_url` host not on the allow-list (if enabled). | `{ "status": "rejected", "error": "callback host not allowed" }` |
| `429 Too Many Requests` | Queue is at its configured max depth (backpressure). | `{ "status": "rejected", "error": "queue full, retry later" }` |
| `503 Service Unavailable` | Dispatcher/queue not ready. | `{ "status": "rejected", "error": "service not ready" }` |

> Note: `429`/backpressure is optional. If you want the queue to be effectively
> unbounded, drop the max-depth check. The "max 2 workers" cap still holds
> regardless — extra jobs just wait longer.

### FastAPI request/response model (reference)

```python
# schemas.py (Architecture 2 additions)
from pydantic import BaseModel, HttpUrl

class DetectRequest(BaseModel):
    video_url: HttpUrl
    callback_url: HttpUrl
    video_id: str

class DetectAccepted(BaseModel):
    status: str = "processing_started"
    video_id: str
    job_id: str
    message: str = "Job accepted and queued for processing."
```

```python
# app.py (Architecture 2 endpoint — validate, enqueue, return 202)
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
import uuid

app = FastAPI(title="iSteam Detection API (Architecture 2)")

@app.post("/detect", status_code=202)
def detect(req: DetectRequest) -> DetectAccepted:
    job_id = uuid.uuid4().hex[:16]
    job = {
        "job_id": job_id,
        "video_id": req.video_id,
        "video_url": str(req.video_url),
        "callback_url": str(req.callback_url),
    }
    if not queue.enqueue(job):            # returns False if queue is full
        raise HTTPException(status_code=429, detail="queue full, retry later")
    return DetectAccepted(video_id=req.video_id, job_id=job_id)
```

The key property: **`detect()` does no CV work and never blocks on processing.**
It validates, pushes onto the queue, and returns.

### Optional endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness/readiness: models loaded, queue reachable, running-worker count. |
| `GET /stats` | Observability: queue depth, running workers (0–2), jobs processed/failed. |

There is **no** `GET /jobs/{id}` result endpoint, because results are not stored
— they are delivered only to the callback.

---

## 2. Outbound: the callback POST

When processing finishes, the worker sends a **POST** to the caller's
`callback_url`. Because nothing is stored, the artifacts themselves are included
in the payload.

### Success payload

`Content-Type: application/json`

```json
{
  "video_id": "abc-123",
  "job_id": "9f3c2a1b7d5e4c6a",
  "status": "completed",
  "product_count": 3,
  "detections_json": { "...": "the full detections.json object (inlined)" },
  "detections_vtt": "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n{...}\n",
  "finished_at": "2026-09-07T10:02:00Z"
}
```

| Field | Type | Meaning |
|---|---|---|
| `video_id` | string | The caller's id, echoed back for correlation. |
| `job_id` | string | Our internal id (also for correlation / idempotency). |
| `status` | string | `completed` or `failed`. |
| `product_count` | int | Number of distinct products detected. |
| `detections_json` | object | The **full** `detections.json` content, inlined (not a link). |
| `detections_vtt` | string | The **full** WebVTT text, inlined (not a link). |
| `finished_at` | string | UTC ISO-8601 timestamp. |

### Delivery format options for the `.vtt` / `.json`

Because there is no storage, the files are sent **inside** the callback. Two
supported shapes (pick one and document it for your callers):

1. **Inline JSON (default, shown above)** — `detections_json` is a JSON object
   and `detections_vtt` is a string. Simplest for the caller to parse. Best when
   payloads are modest (typical: a few KB–low MB).
2. **Multipart upload** — `multipart/form-data` with a `meta` JSON part plus two
   file parts (`detections.json`, `detections.vtt`). Better for large `.vtt`
   files or if the caller prefers to stream files to disk. The worker chooses
   this when the combined size exceeds `CALLBACK_MULTIPART_THRESHOLD_BYTES`.

> If you truly need URLs instead of inlined content, that reintroduces storage
> (a bucket + presigned URLs) and is therefore Architecture 1, not this one.

### Failure payload

If the job fails (download error, decode error, pipeline error), the worker
still calls back so the caller always hears something:

```json
{
  "video_id": "abc-123",
  "job_id": "9f3c2a1b7d5e4c6a",
  "status": "failed",
  "error": "could not download video_url (HTTP 404)",
  "finished_at": "2026-09-07T10:02:00Z"
}
```

### Callback delivery guarantees

- **Retries with backoff.** The worker retries the POST on network errors or
  `5xx`/`429` responses using exponential backoff (`network.max_retries`,
  `network.backoff_base_seconds` in `config.yaml`).
- **Success = HTTP 2xx** from the callback endpoint.
- **Idempotency.** The caller should treat `video_id` + `job_id` as an
  idempotency key, because at-least-once queue delivery means a callback can (in
  rare crash-retry cases) arrive more than once.
- **Timeout.** Each POST attempt uses `network.request_timeout_seconds`.

### Sample detections.json (shape of the inlined object)

```json
{
  "video": { "id": "abc", "fps": 30.0, "duration_seconds": 42.0, "width": 1920, "height": 1080 },
  "products": [
    {
      "product_id": "p1",
      "label": "sneaker",
      "first_seen": 1.0,
      "last_seen": 8.0,
      "occurrences": [{ "timestamp": 1.0, "bbox": [x1,y1,x2,y2], "confidence": 0.82 }],
      "recommendations": [
        { "title": "...", "url": "https://amazon...", "source": "amazon.com",
          "price": "$79", "thumbnail": "https://...", "score": 0.95, "backend": "google_lens" }
      ]
    }
  ]
}
```

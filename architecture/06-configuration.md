# 06 — Configuration Reference

Every knob this architecture depends on, grouped by the component that reads it.

---

## 1. Backend API (env)

| Var | Default | Meaning |
|---|---|---|
| `QUEUE_BACKEND` | `redis` | `inproc` \| `redis` \| `sqs`. Which queue the API enqueues to. |
| `REDIS_URL` | `redis://redis:6379/0` | When `QUEUE_BACKEND=redis`. |
| `SQS_QUEUE_URL` | — | When `QUEUE_BACKEND=sqs`. |
| `MAX_QUEUE_DEPTH` | `0` (unbounded) | If > 0, API returns `429` when the queue holds this many waiting jobs. |
| `CALLBACK_HOST_ALLOWLIST` | — | Comma-separated hostnames allowed as `callback_url` hosts (SSRF guard). Empty = allow all (not recommended for public deployments). |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | Bind address. |

The API's only job is validate → enqueue → return 202. It loads no models.

---

## 2. Dispatcher (env)

| Var | Default | Meaning |
|---|---|---|
| `MAX_CONCURRENCY` | `2` | **The core cap.** Never run more than this many workers. |
| `QUEUE_BACKEND` / `REDIS_URL` / `SQS_QUEUE_URL` | — | Same queue the API writes to. |
| `LAUNCH_BACKEND` | `docker` | `docker` (one box) \| `ecs` (Fargate/ECS RunTask). |
| `WORKER_IMAGE` | `object-detection-process:latest` | Image the dispatcher launches. |
| `WORKER_MEMORY` | `6g` | Per-container memory cap passed to `docker run --memory`. |
| `WORKER_CPUS` | `4` | Per-container CPU cap passed to `docker run --cpus`. |
| `WORKER_GPUS` | — | e.g. `all` or `device=0` for GPU hosts (`--gpus`). |
| `SQS_VISIBILITY` | `1800` | (SQS) visibility timeout in seconds; must exceed max job time. |
| `RECEIVE_WAIT_SECONDS` | `20` | Long-poll wait when pulling from the queue. |
| `POLL_INTERVAL_SECONDS` | `0.5` | Reap/refill loop tick. |

The dispatcher passes each job to the worker as env:
`VIDEO_URL`, `CALLBACK_URL`, `VIDEO_ID`, `JOB_ID` (+ shared secrets like
`SERPAPI_API_KEY`, `AWS_*`).

---

## 3. Worker (env)

| Var | Required | Meaning |
|---|---|---|
| `VIDEO_URL` | yes | Direct http(s) link to the input video. |
| `CALLBACK_URL` | yes | Where `.json` + `.vtt` are POSTed. |
| `VIDEO_ID` | yes | Caller's id, echoed in the callback. |
| `JOB_ID` | no | Correlation id; auto-generated if absent. |
| `SKIP_MATCHING` | no (`false`) | `true` = no crops/Google Lens = fully storage-free. |
| `SERPAPI_API_KEY` | if matching | Google Lens via SerpApi. |
| `SCRATCH_BUCKET` | if matching (option A) | Ephemeral bucket for Lens crop URLs (auto-expiry lifecycle rule). |
| `AWS_*` | if matching (option A) / SQS | Credentials (prefer IAM task role on ECS). |
| `CALLBACK_MULTIPART_THRESHOLD_BYTES` | no | Above this combined size, POST multipart instead of inline JSON. |
| `WORK_DIR` | no (`/tmp`) | Root for the per-job temp dir (use a `tmpfs` mount for RAM-only). |
| `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` / `YOLO_OFFLINE` / `ULTRALYTICS_AUTOUPDATE` | set in image | Force fully-offline runtime (weights are baked in). |

There is intentionally **no** `OUTPUT_BUCKET` for `detections.*` in this mode.

---

## 4. `config.yaml` keys that matter here

The same `object_detection_process/config.yaml` drives the CV pipeline. The
keys most relevant to this architecture:

### Input
```yaml
input:
  source_type: "url"        # worker overrides to "url" from VIDEO_URL
  url: ""                   # set at runtime from VIDEO_URL
  work_dir: "workdir"       # overridden to the per-job temp dir
```

### Detection performance / startup
```yaml
detection:
  device: "cpu"             # "cuda:0" on a GPU host
  backend: "yoloe"
  model_weights: "yoloe-11l-seg.pt"
  use_builtin_vocab: true   # set false to cut CPU start-up time significantly
  product_prompts_file: "product_prompts.txt"   # trim to speed start-up
  confidence_threshold: 0.35
```

### Frames (throughput vs. thoroughness)
```yaml
frames:
  sample_every_seconds: 1.0   # raise for faster/coarser, lower for thorough
  sharpest_window: 6          # blur defense; costs a few extra frame reads
  max_frames: 0               # cap for very long videos (0 = no cap)
```

### Crops / super-resolution
```yaml
crops:
  super_resolution:
    enabled: false            # keep off on CPU; consider true on GPU
    device: "cpu"
```

### Matching (Google Lens) — and the "90%" threshold
```yaml
matching:
  min_match_score: 0.90       # keep recommendations at/above this
  max_results_per_product: 5
  serpapi:
    lens_type: "all"
    country: "us"
    language: "en"
```

### Networking (also governs callback + SerpApi retries)
```yaml
network:
  max_retries: 5
  backoff_base_seconds: 1.5
  backoff_max_seconds: 60
  min_interval_seconds: 1.0    # politeness between SerpApi calls
  request_timeout_seconds: 30  # also used for the callback POST
```

### Storage-related keys — DISABLED in this architecture
```yaml
s3:
  enabled: false              # do NOT persist detections.json/.vtt
metadata:
  emit_webvtt: true           # still generate .vtt (delivered via callback)
```

> For crop-based matching option A (ephemeral presigned crops), a **scratch**
> bucket is still used transiently with a short lifecycle/expiry and per-object
> deletion after matching. That is transient, not persistent storage.

---

## 5. Precedence & overrides

1. **Env vars set by the dispatcher** (`VIDEO_URL`, `CALLBACK_URL`, `VIDEO_ID`)
   are authoritative for I/O.
2. The worker builds a **per-job deep copy** of `config.yaml` and overrides
   `input.*`, `metadata.output_path`, `metadata.webvtt_path`, and `work_dir` to
   point at the per-job temp dir (mirrors what the current `worker.py` already
   does).
3. `config.yaml` supplies everything else (thresholds, prompts, matching, retry
   policy).
4. `_pick_device()` in the worker downgrades `cuda:0` → `cpu` automatically when
   no GPU is present, so the same image is safe on CPU-only hosts.

# object_detection_process — one-shot detection worker (no storage)

A **self-contained, single-video** Docker worker. Given the **URL** of a video
(via an environment variable), it downloads the video into a **per-job temp
directory**, runs the full detection pipeline, POSTs `detections.json` +
`detections.vtt` **inline** to a callback URL (tagged with the caller's
`video_id`), **deletes the temp directory**, and **exits**. The container is
then destroyed. Nothing is stored — the callback is the only delivery mechanism.

This is the compute half of **Architecture 2** (see
`../architecture/04-worker-and-no-storage.md`):

```
POST /detect -> Queue -> Dispatcher (max 2) -> [this container, one per video]
   -> download URL -> detect -> match -> build files -> POST callback -> delete -> exit 0
```

The CV/ML logic (`pipeline_utils.py`, `config.yaml`, `product_prompts.txt`) is
the same code used by the notebook and the `backend/` API. Only the driver
(`worker.py`) is different: one video, deliver via callback, then shut down.

## Files

- `worker.py` — entrypoint: read env → download URL → process → POST callback → delete temp → exit.
- `pipeline_utils.py` — ingest / detect / dedup / Google Lens match / metadata.
- `paths.py` — model-cache paths (weights live in `models/`).
- `config.yaml` — all tunables (thresholds, prompts, the "90%" match).
- `product_prompts.txt` — open-vocab product names.
- `scripts/prefetch_models.py` — downloads YOLOE + CLIP + Real-ESRGAN (run at image build).
- `Dockerfile` — builds the image with weights **baked in** (no runtime download).
- `.env.example` — copy to `.env` for local runs.

## Environment variables

| Var | Required | Meaning |
|---|---|---|
| `VIDEO_URL` | yes (one-shot) | Direct http(s) link to the input video. Video-host URLs also work (yt-dlp). |
| `CALLBACK_URL` | yes | URL that receives the inline `.json` + `.vtt` POST when done. |
| `VIDEO_ID` | yes (one-shot) | Caller's own id, echoed back in the callback for correlation. |
| `JOB_ID` | no | Correlation id; auto-generated if absent. |
| `SKIP_MATCHING` | no | `true` = skip Google Lens (fully storage-free). `false` = force matching (needs `SCRATCH_BUCKET`). Unset = match only if `SCRATCH_BUCKET` is set. |
| `SCRATCH_BUCKET` | no | Ephemeral S3 bucket used only to give Google Lens a fetchable crop URL. Crops are **deleted right after matching**. |
| `SCRATCH_REGION` / `SCRATCH_PREFIX` | no | Region / key prefix for the scratch bucket. |
| `SQS_QUEUE_URL` | poll only | With `--poll`, long-poll this queue instead of `VIDEO_URL`. |
| `SERPAPI_API_KEY`, `AWS_*` | as needed | Google Lens key + credentials for the optional scratch bucket. |

> There is **no** `OUTPUT_BUCKET` in this architecture — results are never
> uploaded, only POSTed inline to the callback.

### The no-storage guarantee

- Input video, sampled frames, crops, and `detections.json` / `.vtt` all live
  under a per-job temp dir (`/tmp/job-<id>/`) that is removed in a `finally:`
  block on both success and failure.
- Run with `--rm` so the container filesystem is discarded on exit anyway.
- The only (optional) external touch is the **ephemeral scratch bucket** for
  Google Lens crop URLs — those objects are deleted immediately after matching.
  Omit `SCRATCH_BUCKET` for zero external storage (matching is then skipped).

## Build (weights baked in)

```bash
docker build -t object-detection-process .
```

The build runs `scripts/prefetch_models.py`, so the model weights ship inside
the image and nothing is downloaded at runtime. For GPU, see the header of the
`Dockerfile`.

## Run one video (one-shot)

Storage-free (no recommendations):

```bash
docker run --rm \
  -e VIDEO_URL="https://cdn.example.com/clips/abc.mp4" \
  -e CALLBACK_URL="https://api.example.com/detections/callback" \
  -e VIDEO_ID="abc-123" \
  -e SKIP_MATCHING=true \
  object-detection-process
```

With Google Lens recommendations (ephemeral scratch bucket, auto-cleaned):

```bash
docker run --rm \
  -e VIDEO_URL="https://cdn.example.com/clips/abc.mp4" \
  -e CALLBACK_URL="https://api.example.com/detections/callback" \
  -e VIDEO_ID="abc-123" \
  -e SCRATCH_BUCKET="isteam-lens-scratch" \
  -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... -e AWS_DEFAULT_REGION=us-east-1 \
  -e SERPAPI_API_KEY=... \
  object-detection-process
```

The container processes the video, POSTs the two files inline to the callback,
deletes its temp dir, and exits `0` (or non-zero on failure).

## Callback payload

Success (`.json` object + `.vtt` string are inlined — no links):

```json
{
  "video_id": "abc-123",
  "job_id": "9f3c2a1b7d5e4c6a",
  "status": "completed",
  "product_count": 3,
  "detections_json": { "video": { "...": "..." }, "products": [ "..." ] },
  "detections_vtt": "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n[p0000] sneaker\n",
  "finished_at": "2026-09-07T10:02:00Z"
}
```

Failure:

```json
{
  "video_id": "abc-123",
  "job_id": "9f3c2a1b7d5e4c6a",
  "status": "failed",
  "error": "could not download video_url (HTTP 404)",
  "finished_at": "2026-09-07T10:02:00Z"
}
```

## Poll mode (single box / EC2)

Instead of one container per video, run a long-lived poller that pulls one
message at a time from SQS. Each message body carries
`{ video_url, callback_url, video_id, job_id }`:

```bash
docker run --rm \
  -e SQS_QUEUE_URL="https://sqs.us-east-1.amazonaws.com/123/detection-jobs" \
  -e SERPAPI_API_KEY=... \
  object-detection-process --poll
```

## Local run (no Docker)

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
copy .env.example .env   # then edit values (VIDEO_URL, CALLBACK_URL, VIDEO_ID)
python scripts/prefetch_models.py
python worker.py
```

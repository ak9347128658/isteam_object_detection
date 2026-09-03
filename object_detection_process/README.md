# object_detection_process — one-shot detection worker

A **self-contained, single-video** Docker worker. Given the S3 link of a video
(via an environment variable), it runs the full detection pipeline, uploads
`detections.json` + `detections.vtt` to S3 under a **unique suffix**, POSTs a
callback with all the S3 links, and **exits**. The container is then destroyed.

This is the compute half of **Architecture 1** (see `../architecture.md`):

```
S3 upload -> Lambda -> SQS -> [this container, one per video] -> S3 + callback -> exit
```

The CV/ML logic (`pipeline_utils.py`, `config.yaml`, `product_prompts.txt`) is
the same code used by the notebook and the `backend/` API. Only the driver
(`worker.py`) is different: batch, one video, then shut down.

## Files

- `worker.py` — entrypoint: read env → process → upload → callback → exit.
- `pipeline_utils.py` — ingest / detect / dedup / S3 / Google Lens match / metadata.
- `paths.py` — model-cache paths (weights live in `models/`).
- `config.yaml` — all tunables (thresholds, prompts, S3, the "90%" match).
- `product_prompts.txt` — open-vocab product names.
- `scripts/prefetch_models.py` — downloads YOLOE + CLIP + Real-ESRGAN (run at image build).
- `Dockerfile` — builds the image with weights **baked in** (no runtime download).
- `.env.example` — copy to `.env` for local runs.

## Environment variables

| Var | Required | Meaning |
|---|---|---|
| `VIDEO_S3_URI` | yes (one-shot) | `s3://bucket/key` of the input video. |
| `CALLBACK_URL` | yes | URL that receives the POST when done. |
| `JOB_ID` | no | Correlation id; auto-generated if absent. |
| `OUTPUT_BUCKET` | yes | Bucket for `detections.json` / `.vtt` (defaults to `s3.bucket`). |
| `OUTPUT_PREFIX` | no | Key prefix for outputs (default `detections`). |
| `OUTPUT_REGION` | no | Region of the output bucket (defaults to `s3.region`). |
| `OUTPUT_PUBLIC` | no | `true` → public URL, else a presigned URL. |
| `SKIP_MATCHING` | no | `true` to skip S3 crop upload + Google Lens. |
| `SQS_QUEUE_URL` | poll only | With `--poll`, long-poll this queue instead of `VIDEO_S3_URI`. |
| `SERPAPI_API_KEY`, `AWS_*` | as needed | Google Lens + S3 credentials. |

## Build (weights baked in)

```bash
docker build -t object-detection-process .
```

The build runs `scripts/prefetch_models.py`, so the model weights ship inside
the image and nothing is downloaded at runtime. For GPU, see the header of the
`Dockerfile`.

## Run one video (one-shot)

```bash
docker run --rm \
  -e VIDEO_S3_URI="s3://isteam-video-input/uploads/clip.mp4" \
  -e CALLBACK_URL="https://api.example.com/detections/callback" \
  -e OUTPUT_BUCKET="isteam-video-output" \
  -e AWS_ACCESS_KEY_ID=... -e AWS_SECRET_ACCESS_KEY=... -e AWS_DEFAULT_REGION=us-east-1 \
  -e SERPAPI_API_KEY=... \
  object-detection-process
```

The container processes the video, uploads the two output files, POSTs the
callback, and exits `0` (or non-zero on failure).

## Callback payload

```json
{
  "job_id": "3f9c2a1b...",
  "status": "completed",
  "video_s3_uri": "s3://isteam-video-input/uploads/clip.mp4",
  "unique_suffix": "clip-3f9c2a1b-20260903T100200Z",
  "product_count": 3,
  "detections_json_s3_uri": "s3://isteam-video-output/detections/clip-3f9c2a1b-20260903T100200Z/detections.json",
  "detections_vtt_s3_uri":  "s3://isteam-video-output/detections/clip-3f9c2a1b-20260903T100200Z/detections.vtt",
  "detections_json_url": "https://...",
  "detections_vtt_url":  "https://...",
  "finished_at": "2026-09-03T10:02:00Z"
}
```

## Poll mode (single box / EC2)

Instead of one container per video, run a long-lived poller that pulls one
message at a time from SQS:

```bash
docker run --rm \
  -e SQS_QUEUE_URL="https://sqs.us-east-1.amazonaws.com/123/detection-jobs" \
  -e OUTPUT_BUCKET="isteam-video-output" \
  -e SERPAPI_API_KEY=... \
  object-detection-process --poll
```

## Local run (no Docker)

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
copy .env.example .env   # then edit values
python scripts/prefetch_models.py
python worker.py
```

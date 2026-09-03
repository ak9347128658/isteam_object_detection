# Product Detection API (standalone)

This folder is a **self-contained** FastAPI service. It does not import or read
any files from a parent repository. You can copy this directory anywhere and run it.

Included here:

- `app.py` — FastAPI routes
- `pipeline_utils.py` — detection / dedup / S3 / matching
- `config.yaml` — all tunables
- `product_prompts.txt` — open-vocab product names
- `.env.example` — copy to `.env` and fill in keys
- `scripts/prefetch_models.py` — download YOLOE, CLIP, Real-ESRGAN

## Setup (inside this folder)

```bash
cd backend
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
copy .env.example .env   # then edit SERPAPI_API_KEY / AWS_*
python scripts/prefetch_models.py
python __main__.py
```

Docs: http://127.0.0.1:8000/docs

Weights land in `models/` (`yolo/`, `clip/`, `realesrgan/`). Jobs write under `workdir/api-jobs/`.

## API

`POST /detect` accepts exactly one of: video file, `s3_url`, or `url`.

```bash
curl -X POST http://127.0.0.1:8000/detect -F "s3_url=s3://my-bucket/videos/clip.mp4"
curl http://127.0.0.1:8000/jobs/<job_id>
curl -O http://127.0.0.1:8000/jobs/<job_id>/detections.json
curl -O http://127.0.0.1:8000/jobs/<job_id>/detections.vtt
```

Wait and download a zip of both files:

```bash
curl -X POST "http://127.0.0.1:8000/detect?wait=true" -F "video=@video.mp4" -o detections.zip
```

`skip_matching=true` skips S3 crop upload + Google Lens and still returns json/vtt.

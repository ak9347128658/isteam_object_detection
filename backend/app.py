"""
FastAPI entrypoint for product detection.

POST /detect  — upload a video OR pass s3_url / url, returns a job id.
GET  /jobs/{id} — poll until completed, then download detections.json / .vtt.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from paths import (
    BACKEND_DIR,
    CONFIG_PATH,
    ENV_PATH,
    JOBS_DIR,
    MODELS_DIR,
    apply_model_cache_env,
    bootstrap,
)

bootstrap()
apply_model_cache_env()
os.chdir(BACKEND_DIR)

import pipeline_utils as pu  # noqa: E402
from jobs import Job, store  # noqa: E402
from pipeline_runner import classify_source, run_job  # noqa: E402
from schemas import HealthResponse, JobCreated, JobInfo  # noqa: E402

ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}

_state: dict = {
    "cfg": None,
    "detector": None,
    "embedder": None,
    "device": "cpu",
    "models_loaded": False,
}
_infer_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detect")


def _pick_device(requested: str) -> str:
    try:
        import torch
        if str(requested).startswith("cuda") and torch.cuda.is_available():
            return requested
    except Exception:
        pass
    return "cpu"


def _load_models() -> None:
    pu.load_env(ENV_PATH)
    cfg = pu.load_config(CONFIG_PATH)
    device = _pick_device(pu.get(cfg, "detection.device", "cuda:0"))
    cfg.setdefault("detection", {})["device"] = device
    cfg.setdefault("dedup", {})["device"] = device
    cfg.setdefault("crops", {}).setdefault("super_resolution", {})["device"] = device

    prompts_file = pu.get(cfg, "detection.product_prompts_file")
    if prompts_file and not Path(prompts_file).is_absolute():
        cfg["detection"]["product_prompts_file"] = str(BACKEND_DIR / prompts_file)

    print(f"[api] Loading detector on {device} (weights={pu.get(cfg, 'detection.model_weights')})")
    detector = pu.Detector(cfg)
    print("[api] Loading CLIP embedder")
    embedder = pu.Embedder(cfg, section="dedup")
    _state.update(
        cfg=cfg,
        detector=detector,
        embedder=embedder,
        device=device,
        models_loaded=True,
    )
    print("[api] Models ready")


@asynccontextmanager
async def lifespan(app: FastAPI):
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    _load_models()
    yield
    _executor.shutdown(wait=False)


app = FastAPI(
    title="iSteam Object Detection API",
    description=(
        "Submit a video file or an S3/HTTP URL. The pipeline detects products "
        "and returns `detections.json` + `detections.vtt`."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _job_urls(job_id: str) -> dict:
    return {
        "status_url": f"/jobs/{job_id}",
        "json_url": f"/jobs/{job_id}/detections.json",
        "vtt_url": f"/jobs/{job_id}/detections.vtt",
        "zip_url": f"/jobs/{job_id}/detections.zip",
    }


def _to_info(job: Job) -> JobInfo:
    urls = _job_urls(job.job_id) if job.status == "completed" else {}
    return JobInfo(
        job_id=job.job_id,
        status=job.status,
        source=job.source,
        skip_matching=job.skip_matching,
        product_count=job.product_count,
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        json_url=urls.get("json_url"),
        vtt_url=urls.get("vtt_url"),
        zip_url=urls.get("zip_url"),
        message=job.message,
    )


def _require_job(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job


def _submit(job: Job) -> None:
    def _run() -> None:
        try:
            run_job(
                job,
                _state["cfg"],
                _state["detector"],
                _state["embedder"],
                _infer_lock,
            )
        except Exception as e:
            print(f"[api] job {job.job_id} failed: {e}")

    _executor.submit(_run)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    cfg = _state.get("cfg") or {}
    try:
        import torch
        cuda = bool(torch.cuda.is_available())
    except Exception:
        cuda = False
    sr_on = bool(pu.get(cfg, "crops.super_resolution.enabled", False)) if cfg else False
    return HealthResponse(
        ok=True,
        cuda=cuda,
        device=_state.get("device", "cpu"),
        models_loaded=bool(_state.get("models_loaded")),
        yolo_weights=str(pu.get(cfg, "detection.model_weights", "")),
        clip_model=str(pu.get(cfg, "dedup.clip_model", "")),
        realesrgan=sr_on,
        models_dir=str(MODELS_DIR),
    )


@app.post("/detect", response_model=None)
async def detect(
    video: Optional[UploadFile] = File(
        None, description="Video file (mp4/mov/mkv/webm/avi)"
    ),
    s3_url: Optional[str] = Form(
        None, description="s3://bucket/key or https S3 object URL"
    ),
    url: Optional[str] = Form(
        None, description="Direct http(s) video URL (also accepts S3 https URLs)"
    ),
    skip_matching: bool = Form(
        False, description="If true, skip S3 crop upload + Google Lens matching"
    ),
    wait: bool = Query(
        False,
        description="If true, wait for processing and return detections.zip "
                    "(json + vtt). If false, return a job id immediately.",
    ),
) -> Union[JobCreated, FileResponse]:
    if _state.get("detector") is None:
        raise HTTPException(status_code=503, detail="Models are still loading")

    has_video = video is not None and bool(video.filename)
    has_s3 = bool(s3_url and s3_url.strip())
    has_url = bool(url and url.strip())
    if sum(bool(x) for x in (has_video, has_s3, has_url)) != 1:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of: video file upload, s3_url, or url.",
        )

    video_path: Optional[Path] = None
    if has_video:
        suffix = Path(video.filename or "input.mp4").suffix.lower() or ".mp4"
        if suffix not in ALLOWED_VIDEO_EXTS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported video type {suffix}. Use: {sorted(ALLOWED_VIDEO_EXTS)}",
            )
        # Create job first so we have a directory to save into.
        job = store.create(source=video.filename or "upload", skip_matching=skip_matching)
        video_path = job.job_dir / f"input{suffix}"
        with video_path.open("wb") as f:
            shutil.copyfileobj(video.file, f)
        if video_path.stat().st_size == 0:
            raise HTTPException(status_code=400, detail="Uploaded video is empty")
        source_type, value = classify_source(video_path, None, None)
        store.update(job.job_id, source_type=source_type, source_value=value)
    else:
        source_type, value = classify_source(None, s3_url if has_s3 else None, url if has_url else None)
        job = store.create(
            source=value,
            skip_matching=skip_matching,
            source_type=source_type,
            source_value=value,
        )

    _submit(job)
    if wait:
        while True:
            current = store.get(job.job_id)
            if current is None:
                raise HTTPException(status_code=500, detail="Job disappeared")
            if current.status == "completed":
                return FileResponse(
                    current.zip_path,
                    media_type="application/zip",
                    filename="detections.zip",
                )
            if current.status == "failed":
                raise HTTPException(
                    status_code=500,
                    detail=current.error or "Detection failed",
                )
            await asyncio.sleep(1.0)
    urls = _job_urls(job.job_id)
    return JobCreated(job_id=job.job_id, status=job.status, **urls)


@app.get("/jobs/{job_id}", response_model=JobInfo)
def job_status(job_id: str) -> JobInfo:
    return _to_info(_require_job(job_id))


def _file_if_ready(job: Job, path: Path, media_type: str, filename: str) -> FileResponse:
    if job.status == "failed":
        raise HTTPException(status_code=500, detail=job.error or "Job failed")
    if job.status != "completed" or not path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Job is {job.status}. Poll GET /jobs/{job.job_id} until completed.",
        )
    return FileResponse(path, media_type=media_type, filename=filename)


@app.get("/jobs/{job_id}/detections.json")
def job_json(job_id: str) -> FileResponse:
    job = _require_job(job_id)
    return _file_if_ready(job, job.json_path, "application/json", "detections.json")


@app.get("/jobs/{job_id}/detections.vtt")
def job_vtt(job_id: str) -> FileResponse:
    job = _require_job(job_id)
    return _file_if_ready(job, job.vtt_path, "text/vtt", "detections.vtt")


@app.get("/jobs/{job_id}/detections.zip")
def job_zip(job_id: str) -> FileResponse:
    job = _require_job(job_id)
    return _file_if_ready(job, job.zip_path, "application/zip", "detections.zip")

"""Run the colab_check pipeline for one API job and write detections.json / .vtt."""

from __future__ import annotations

import copy
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

from jobs import Job, store
from paths import BACKEND_DIR, bootstrap

bootstrap()
import pipeline_utils as pu  # noqa: E402


def _video_info(video_path: Path, video_id: str) -> dict[str, Any]:
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    fc = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    info = {
        "id": video_id,
        "path": str(video_path),
        "fps": fps,
        "frame_count": fc,
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
        "duration_seconds": (fc / fps) if fps else None,
    }
    cap.release()
    return info


def _write_zip(job: Job) -> None:
    with zipfile.ZipFile(job.zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if job.json_path.exists():
            zf.write(job.json_path, "detections.json")
        if job.vtt_path.exists():
            zf.write(job.vtt_path, "detections.vtt")


def run_job(
    job: Job,
    cfg: dict,
    detector: pu.Detector,
    embedder: pu.Embedder,
    infer_lock,
) -> None:
    """
    Same stages as colab_check.ipynb:
    ingest -> frames -> detect -> dedup -> (optional S3) -> match -> metadata.
    Models are reused from app startup (prefetched).
    """
    store.update(
        job.job_id,
        status="running",
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        message="Starting pipeline",
    )
    os.chdir(BACKEND_DIR)
    job_cfg = copy.deepcopy(cfg)
    job_cfg["input"]["work_dir"] = str(job.job_dir / "workdir")
    job_cfg["input"]["source_type"] = job.source_type
    if job.source_type == "local":
        job_cfg["input"]["local_path"] = job.source_value
    elif job.source_type == "s3":
        job_cfg["input"]["s3_uri"] = job.source_value
    else:
        job_cfg["input"]["url"] = job.source_value
    job_cfg["s3"]["local_crops_dir"] = str(job.job_dir / "crops")
    job_cfg["metadata"]["output_path"] = str(job.json_path)
    job_cfg["metadata"]["webvtt_path"] = str(job.vtt_path)
    job_cfg["metadata"]["emit_webvtt"] = True
    job_cfg["network"]["cache_dir"] = str(BACKEND_DIR / "cache" / "search")

    try:
        store.update(job.job_id, message="Ingesting video")
        video_path = pu.ingest_video(job_cfg)
        video_id = pu.slugify(video_path.stem)

        store.update(job.job_id, message="Sampling frames")
        frames = pu.sample_frames(video_path, job_cfg)
        if not frames:
            raise RuntimeError("No frames sampled — check the video / time window settings.")

        crops_dir = pu.get(job_cfg, "s3.local_crops_dir", str(job.job_dir / "crops"))
        all_detections: list = []
        store.update(job.job_id, message=f"Detecting products in {len(frames)} frames")
        with infer_lock:
            for i, fr in enumerate(frames):
                if i == 0 or (i + 1) % 10 == 0 or i + 1 == len(frames):
                    store.update(
                        job.job_id,
                        message=f"Detecting products ({i + 1}/{len(frames)} frames)",
                    )
                for d in detector.detect_frame(fr):
                    detector.save_crop(fr, d, crops_dir)
                    all_detections.append(d)

        store.update(job.job_id, message="Deduplicating products")
        with infer_lock:
            embeddings = embedder.embed_image_paths(
                [d.crop_path for d in all_detections]
            )
        products = pu.dedup_products(all_detections, embeddings, job_cfg)

        do_match = not job.skip_matching
        uploader = pu.S3Uploader(job_cfg, video_id=video_id)
        if do_match and uploader.enabled:
            store.update(job.job_id, message="Uploading crops to S3")
            try:
                uploader.preflight()
                for p in products:
                    ext = os.path.splitext(p.representative_crop)[1] or ".png"
                    suffix = f"{p.product_id}_{pu.slugify(p.label)}{ext}"
                    p.s3_url = uploader.upload(p.representative_crop, key_suffix=suffix)
            except Exception as e:
                print(f"[api] S3 upload skipped: {e}")
                do_match = False

        if do_match:
            store.update(job.job_id, message="Matching products (Google Lens)")
            matcher = pu.Matcher(job_cfg, embedder=embedder)
            for p in products:
                try:
                    p.recommendations = matcher.match(p, image_url=p.s3_url or None)
                except Exception as e:
                    print(f"[api] match {p.product_id} ({p.label}): {e}")
                    p.recommendations = []

        store.update(job.job_id, message="Writing detections.json and detections.vtt")
        pu.write_metadata(products, _video_info(video_path, video_id), job_cfg)
        _write_zip(job)

        store.update(
            job.job_id,
            status="completed",
            product_count=len(products),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            message="Done",
        )
    except Exception as e:
        store.update(
            job.job_id,
            status="failed",
            error=str(e),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            message="Failed",
        )
        raise


def classify_source(
    video_path: Optional[Path],
    s3_url: Optional[str],
    url: Optional[str],
) -> tuple[str, str]:
    """Return (source_type, value) for pipeline_utils.ingest_video."""
    if video_path is not None:
        return "local", str(video_path)
    raw = (s3_url or url or "").strip()
    if not raw:
        raise ValueError("Provide a video file, s3_url, or url.")
    if (
        raw.startswith("s3://")
        or ".s3." in raw
        or "amazonaws.com" in raw
    ):
        return "s3", raw
    if raw.startswith("http://") or raw.startswith("https://"):
        return "url", raw
    raise ValueError(
        "s3_url/url must be an s3:// URI, an S3 https URL, or a direct video URL."
    )

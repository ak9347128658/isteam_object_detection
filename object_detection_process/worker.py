"""
worker.py — one-shot containerized detection worker (Architecture 1).

Lifecycle of a single container:
  1. Read the input video S3 link + callback URL from ENVIRONMENT variables
     (the launcher / SQS message supplies them).
  2. Run the standard pipeline (ingest -> frames -> detect -> dedup ->
     crops->S3 -> Google Lens match -> detections.json / .vtt).
  3. Upload detections.json + detections.vtt to the OUTPUT S3 bucket under a
     UNIQUE suffix so results never collide.
  4. POST a callback to CALLBACK_URL with the video link + the S3 links of the
     generated files (json/vtt) + status.
  5. Exit 0 on success, non-zero on failure — the container is then destroyed.

Environment variables (see architecture.md):
  VIDEO_S3_URI    s3://bucket/key of the input video   (required unless --poll)
  CALLBACK_URL    URL to POST the result to            (required)
  JOB_ID          correlation id (auto-generated if absent)
  OUTPUT_BUCKET   bucket for detections.json/.vtt      (defaults to s3.bucket)
  OUTPUT_PREFIX   key prefix for outputs               (default "detections")
  OUTPUT_REGION   region of OUTPUT_BUCKET               (defaults to s3.region)
  SKIP_MATCHING   "true" to skip crop upload + Lens     (default false)
  OUTPUT_PUBLIC   "true" -> public URL, else presigned  (default: s3.public_read)

Poll mode (single GPU box / EC2):
  python worker.py --poll     # long-polls SQS_QUEUE_URL, one video per message
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

from paths import (
    BACKEND_DIR as WORKER_DIR,
    CONFIG_PATH,
    ENV_PATH,
    MODELS_DIR,
    apply_model_cache_env,
    bootstrap,
)

bootstrap()
apply_model_cache_env()
os.chdir(WORKER_DIR)

import pipeline_utils as pu  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _pick_device(requested: str) -> str:
    try:
        import torch
        if str(requested).startswith("cuda") and torch.cuda.is_available():
            return requested
    except Exception:
        pass
    return "cpu"


def _load_cfg() -> dict:
    pu.load_env(ENV_PATH)
    cfg = pu.load_config(CONFIG_PATH)
    device = _pick_device(pu.get(cfg, "detection.device", "cuda:0"))
    cfg.setdefault("detection", {})["device"] = device
    cfg.setdefault("dedup", {})["device"] = device
    cfg.setdefault("crops", {}).setdefault("super_resolution", {})["device"] = device
    prompts_file = pu.get(cfg, "detection.product_prompts_file")
    if prompts_file and not Path(prompts_file).is_absolute():
        cfg["detection"]["product_prompts_file"] = str(WORKER_DIR / prompts_file)
    return cfg


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


def _unique_suffix(video_id: str, job_id: str) -> str:
    """Collision-proof suffix: <video_id>-<8charJobId>-<UTCtimestamp>."""
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{video_id}-{job_id[:8]}-{ts}"


# ---------------------------------------------------------------------------
# Output upload (detections.json / .vtt) to the OUTPUT bucket
# ---------------------------------------------------------------------------

class OutputUploader:
    """Uploads the generated detections files and returns their S3 links."""

    def __init__(self, cfg: dict):
        import boto3

        self.bucket = os.getenv("OUTPUT_BUCKET") or pu.get(cfg, "s3.bucket")
        self.region = (
            os.getenv("OUTPUT_REGION")
            or pu.get(cfg, "s3.region", "us-east-1")
        )
        self.prefix = (os.getenv("OUTPUT_PREFIX", "detections")).strip("/")
        self.public = _env_bool("OUTPUT_PUBLIC", bool(pu.get(cfg, "s3.public_read", False)))
        self.presign_ttl = int(pu.get(cfg, "s3.presign_expiry_seconds", 7 * 24 * 3600))
        if not self.bucket:
            raise RuntimeError(
                "OUTPUT_BUCKET is not set and s3.bucket is empty in config.yaml."
            )
        self.client = boto3.client("s3", region_name=self.region)

    def _presigned_url(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.presign_ttl,
        )

    def upload(self, local_path: Path, suffix: str, filename: str) -> dict:
        from botocore.exceptions import ClientError

        key = f"{self.prefix}/{suffix}/{filename}"
        ctype = "application/json" if filename.endswith(".json") else "text/vtt"
        extra = {"ContentType": ctype}
        s3_uri = f"s3://{self.bucket}/{key}"

        if self.public:
            try:
                self.client.upload_file(
                    str(local_path), self.bucket, key,
                    ExtraArgs={**extra, "ACL": "public-read"},
                )
                url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"
                return {"s3_uri": s3_uri, "url": url}
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code not in (
                    "AccessDenied", "AccessControlListNotSupported",
                    "InvalidBucketAclWithObjectOwnership",
                ):
                    raise
                print("[output] public ACL not allowed; using presigned URL.")

        self.client.upload_file(str(local_path), self.bucket, key, ExtraArgs=extra)
        return {"s3_uri": s3_uri, "url": self._presigned_url(key)}


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

def send_callback(callback_url: str, payload: dict, cfg: dict) -> None:
    """POST the result to the callback URL, with retry/backoff."""
    if not callback_url:
        print("[callback] No CALLBACK_URL set; skipping callback.")
        return
    import requests

    def _post():
        resp = requests.post(callback_url, json=payload, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"callback returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.status_code

    try:
        status = pu.with_retries(_post, cfg, what=f"POST {callback_url}")
        print(f"[callback] delivered to {callback_url} (HTTP {status})")
    except Exception as e:
        print(f"[callback] FAILED to deliver to {callback_url}: {e}")


# ---------------------------------------------------------------------------
# Core: process one video end to end
# ---------------------------------------------------------------------------

def process_one(
    video_s3_uri: str,
    callback_url: str,
    job_id: str,
    skip_matching: bool,
    cfg: dict,
    detector: "pu.Detector",
    embedder: "pu.Embedder",
) -> dict:
    """Run the full pipeline for a single video and deliver the callback."""
    work_root = Path(os.getenv("WORK_DIR", WORKER_DIR / "workdir")) / job_id
    work_root.mkdir(parents=True, exist_ok=True)
    json_path = work_root / "detections.json"
    vtt_path = work_root / "detections.vtt"

    job_cfg = copy.deepcopy(cfg)
    job_cfg["input"]["source_type"] = "s3"
    job_cfg["input"]["s3_uri"] = video_s3_uri
    job_cfg["input"]["work_dir"] = str(work_root / "ingest")
    job_cfg["s3"]["local_crops_dir"] = str(work_root / "crops")
    job_cfg["metadata"]["output_path"] = str(json_path)
    job_cfg["metadata"]["webvtt_path"] = str(vtt_path)
    job_cfg["metadata"]["emit_webvtt"] = True
    job_cfg["network"]["cache_dir"] = str(WORKER_DIR / "cache" / "search")

    print(f"[job {job_id}] ingesting {video_s3_uri}")
    video_path = pu.ingest_video(job_cfg)
    video_id = pu.slugify(video_path.stem)

    print(f"[job {job_id}] sampling frames")
    frames = pu.sample_frames(video_path, job_cfg)
    if not frames:
        raise RuntimeError("No frames sampled — check the video / time window settings.")

    crops_dir = pu.get(job_cfg, "s3.local_crops_dir")
    all_detections: list = []
    print(f"[job {job_id}] detecting products in {len(frames)} frames")
    for fr in frames:
        for d in detector.detect_frame(fr):
            detector.save_crop(fr, d, crops_dir)
            all_detections.append(d)

    print(f"[job {job_id}] deduplicating {len(all_detections)} detections")
    embeddings = embedder.embed_image_paths([d.crop_path for d in all_detections])
    products = pu.dedup_products(all_detections, embeddings, job_cfg)

    do_match = not skip_matching
    uploader = pu.S3Uploader(job_cfg, video_id=video_id)
    if do_match and uploader.enabled:
        print(f"[job {job_id}] uploading {len(products)} crops to S3")
        try:
            uploader.preflight()
            for p in products:
                ext = os.path.splitext(p.representative_crop)[1] or ".png"
                suffix = f"{p.product_id}_{pu.slugify(p.label)}{ext}"
                p.s3_url = uploader.upload(p.representative_crop, key_suffix=suffix)
        except Exception as e:
            print(f"[job {job_id}] S3 crop upload skipped: {e}")
            do_match = False

    if do_match:
        print(f"[job {job_id}] matching products via Google Lens")
        matcher = pu.Matcher(job_cfg, embedder=embedder)
        for p in products:
            try:
                p.recommendations = matcher.match(p, image_url=p.s3_url or None)
            except Exception as e:
                print(f"[job {job_id}] match {p.product_id} ({p.label}): {e}")
                p.recommendations = []

    print(f"[job {job_id}] writing detections.json / detections.vtt")
    pu.write_metadata(products, _video_info(video_path, video_id), job_cfg)

    # Upload the two output files. Both the folder AND the filenames carry the
    # unique suffix, so files are like detections_<suffix>.json / .vtt.
    suffix = _unique_suffix(video_id, job_id)
    out = OutputUploader(cfg)
    print(f"[job {job_id}] uploading outputs under suffix {suffix}")
    json_res = out.upload(json_path, suffix, f"detections_{suffix}.json")
    vtt_res = out.upload(vtt_path, suffix, f"detections_{suffix}.vtt")

    payload = {
        "job_id": job_id,
        "status": "completed",
        "video_s3_uri": video_s3_uri,
        "unique_suffix": suffix,
        "product_count": len(products),
        "detections_json_s3_uri": json_res["s3_uri"],
        "detections_vtt_s3_uri": vtt_res["s3_uri"],
        "detections_json_url": json_res["url"],
        "detections_vtt_url": vtt_res["url"],
        "finished_at": _now(),
    }
    send_callback(callback_url, payload, cfg)
    print(f"[job {job_id}] DONE — {len(products)} products")
    return payload


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_from_env(cfg: dict, detector, embedder) -> int:
    """One-shot mode: everything comes from environment variables."""
    video_s3_uri = os.getenv("VIDEO_S3_URI") or os.getenv("VIDEO_S3_LINK")
    callback_url = os.getenv("CALLBACK_URL", "")
    job_id = os.getenv("JOB_ID") or uuid.uuid4().hex
    skip_matching = _env_bool("SKIP_MATCHING", False)

    if not video_s3_uri:
        print("[worker] VIDEO_S3_URI is required in one-shot mode.", file=sys.stderr)
        return 2

    try:
        process_one(video_s3_uri, callback_url, job_id, skip_matching,
                    cfg, detector, embedder)
        return 0
    except Exception as e:
        print(f"[worker] job {job_id} FAILED: {e}", file=sys.stderr)
        send_callback(
            callback_url,
            {
                "job_id": job_id,
                "status": "failed",
                "video_s3_uri": video_s3_uri,
                "error": str(e),
                "finished_at": _now(),
            },
            cfg,
        )
        return 1


def run_poll(cfg: dict, detector, embedder) -> int:
    """Poll mode: long-poll SQS_QUEUE_URL and process one video per message."""
    import boto3

    queue_url = os.getenv("SQS_QUEUE_URL")
    if not queue_url:
        print("[worker] SQS_QUEUE_URL is required for --poll mode.", file=sys.stderr)
        return 2
    region = os.getenv("AWS_DEFAULT_REGION", pu.get(cfg, "s3.region", "us-east-1"))
    sqs = boto3.client("sqs", region_name=region)
    default_callback = os.getenv("CALLBACK_URL", "")
    print(f"[worker] polling {queue_url}")

    while True:
        resp = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=int(os.getenv("SQS_VISIBILITY", "1800")),
        )
        messages = resp.get("Messages", [])
        if not messages:
            continue
        msg = messages[0]
        receipt = msg["ReceiptHandle"]
        try:
            body = json.loads(msg["Body"])
        except Exception:
            body = {}
        video_s3_uri = body.get("s3_uri") or body.get("video_s3_uri", "")
        callback_url = body.get("callback_url", default_callback)
        job_id = body.get("job_id") or uuid.uuid4().hex
        skip_matching = bool(body.get("skip_matching", False))

        if not video_s3_uri:
            print("[worker] message missing s3_uri; deleting.")
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            continue

        try:
            process_one(video_s3_uri, callback_url, job_id, skip_matching,
                        cfg, detector, embedder)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
        except Exception as e:
            print(f"[worker] job {job_id} failed: {e}", file=sys.stderr)
            send_callback(
                callback_url,
                {"job_id": job_id, "status": "failed",
                 "video_s3_uri": video_s3_uri, "error": str(e),
                 "finished_at": _now()},
                cfg,
            )
            # Leave the message so SQS retries / sends it to the DLQ.


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot video detection worker")
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Long-poll SQS_QUEUE_URL instead of reading VIDEO_S3_URI once.",
    )
    args = parser.parse_args()

    cfg = _load_cfg()
    device = pu.get(cfg, "detection.device", "cpu")
    print(f"[worker] loading models on {device} "
          f"(weights={pu.get(cfg, 'detection.model_weights')}, models_dir={MODELS_DIR})")
    detector = pu.Detector(cfg)
    embedder = pu.Embedder(cfg, section="dedup")
    print("[worker] models ready")

    if args.poll:
        return run_poll(cfg, detector, embedder) or 0
    return run_from_env(cfg, detector, embedder)


if __name__ == "__main__":
    raise SystemExit(main())

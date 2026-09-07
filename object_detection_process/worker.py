"""
worker.py — one-shot containerized detection worker (Architecture 2, no storage).

Lifecycle of a single container:
  1. Read VIDEO_URL, CALLBACK_URL, VIDEO_ID (+ optional JOB_ID) from the
     ENVIRONMENT (the launcher / queue message supplies them).
  2. Create a private per-job temp directory (container-local, never a shared /
     persistent volume) and download VIDEO_URL into it.
  3. Run the standard pipeline entirely inside that temp dir:
     ingest -> frames -> detect (YOLOE) -> dedup (CLIP) -> (optional Google Lens
     match) -> build detections.json + detections.vtt.
  4. Read the two generated files and POST them INLINE to CALLBACK_URL, tagged
     with VIDEO_ID + JOB_ID + status. Nothing is uploaded to S3; the callback is
     the only delivery mechanism.
  5. Delete the temp directory (always, in a finally: block).
  6. Exit 0 on success, non-zero on failure — the container is then destroyed.

Environment variables (see architecture/04-worker-and-no-storage.md):
  VIDEO_URL      direct http(s) link to the input video   (required, one-shot)
  CALLBACK_URL   URL to POST the result to                (required)
  VIDEO_ID       caller's id, echoed back in the callback (required, one-shot)
  JOB_ID         correlation id (auto-generated if absent)
  SKIP_MATCHING  "true" to skip crops + Google Lens        (default: auto*)
  SCRATCH_BUCKET ephemeral S3 bucket for Lens crop URLs    (optional; enables match)
  SCRATCH_REGION region of SCRATCH_BUCKET                  (defaults to us-east-1)

  * Google Lens needs a publicly fetchable image URL for each crop. To honor the
    no-storage guarantee this worker only runs matching when SCRATCH_BUCKET is
    provided (option A: upload crop -> presigned URL -> match -> delete crop).
    Without SCRATCH_BUCKET (and unless SKIP_MATCHING is explicitly "false"),
    matching is skipped and detections.json/.vtt still contain products +
    timestamps, just no ecommerce recommendations.

Queue/poll mode (single box / EC2):
  python worker.py --poll     # long-polls SQS_QUEUE_URL, one video per message
                              # each message: { video_url, callback_url, video_id, job_id }
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import time
import uuid
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
        "fps": fps,
        "frame_count": fc,
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0),
        "duration_seconds": (fc / fps) if fps else None,
    }
    cap.release()
    return info


# ---------------------------------------------------------------------------
# Ephemeral crop hosting for Google Lens (option A: transient, auto-cleaned)
# ---------------------------------------------------------------------------

class ScratchCropStore:
    """
    Uploads product crops to a SCRATCH S3 bucket ONLY to obtain a short-lived
    presigned URL that Google Lens can fetch, then DELETES every object again.

    This is the "option A" from the architecture: there is no persistent
    storage of any artifact — the crops live in the scratch bucket for the few
    seconds of the Lens lookup and are removed in `cleanup()` before the worker
    exits. If no SCRATCH_BUCKET is configured, matching is skipped entirely.
    """

    def __init__(self, cfg: dict):
        import boto3

        self.bucket = os.getenv("SCRATCH_BUCKET", "").strip()
        self.region = (
            os.getenv("SCRATCH_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or pu.get(cfg, "s3.region", "us-east-1")
        )
        self.prefix = os.getenv("SCRATCH_PREFIX", "lens-scratch").strip("/")
        self.presign_ttl = int(pu.get(cfg, "s3.presign_expiry_seconds", 3600))
        self.enabled = bool(self.bucket)
        self._keys: list[str] = []
        self.client = boto3.client("s3", region_name=self.region) if self.enabled else None

    def upload(self, local_path: str, key_suffix: str) -> str:
        """Upload one crop and return a presigned GET URL for Google Lens."""
        if not self.enabled:
            return ""
        real_ext = Path(local_path).suffix.lower() or ".png"
        key = f"{self.prefix}/{Path(key_suffix).with_suffix(real_ext)}"
        content_types = {".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".webp": "image/webp"}
        self.client.upload_file(
            local_path, self.bucket, key,
            ExtraArgs={"ContentType": content_types.get(real_ext, "image/png")},
        )
        self._keys.append(key)
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.presign_ttl,
        )

    def cleanup(self) -> None:
        """Delete every scratch object we created (best-effort)."""
        if not self.enabled or not self._keys:
            return
        try:
            self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": k} for k in self._keys],
                        "Quiet": True},
            )
            print(f"[scratch] deleted {len(self._keys)} temporary crop object(s)")
        except Exception as e:
            print(f"[scratch] cleanup warning: {e}")
        finally:
            self._keys = []


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

def send_callback(callback_url: str, payload: dict, cfg: dict) -> None:
    """POST the result INLINE to the callback URL, with retry/backoff."""
    if not callback_url:
        print("[callback] No CALLBACK_URL set; skipping callback.")
        return
    import requests

    timeout = int(pu.get(cfg, "network.request_timeout_seconds", 30))

    def _post():
        resp = requests.post(callback_url, json=payload, timeout=timeout)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"callback returned HTTP {resp.status_code}: {resp.text[:200]}")
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
    video_url: str,
    callback_url: str,
    video_id: str,
    job_id: str,
    skip_matching: Optional[bool],
    cfg: dict,
    detector: "pu.Detector",
    embedder: "pu.Embedder",
) -> dict:
    """Run the full pipeline for a single video and deliver the callback.

    Everything is written to a per-job temp directory that is deleted before
    returning (both on success and failure). No artifact is persisted anywhere
    outside the callback POST.
    """
    work_root = Path(os.getenv("WORK_DIR", "/tmp")) / f"job-{job_id}"
    work_root.mkdir(parents=True, exist_ok=True)
    scratch = ScratchCropStore(cfg)

    try:
        json_path = work_root / "detections.json"
        vtt_path = work_root / "detections.vtt"

        # Build a per-job config: URL input, all outputs inside the temp dir.
        job_cfg = copy.deepcopy(cfg)
        job_cfg.setdefault("input", {})
        job_cfg["input"]["source_type"] = "url"
        job_cfg["input"]["url"] = video_url
        job_cfg["input"]["work_dir"] = str(work_root / "ingest")
        job_cfg.setdefault("s3", {})["local_crops_dir"] = str(work_root / "crops")
        # No S3 output in this architecture.
        job_cfg["s3"]["enabled"] = False
        job_cfg.setdefault("metadata", {})["output_path"] = str(json_path)
        job_cfg["metadata"]["webvtt_path"] = str(vtt_path)
        job_cfg["metadata"]["emit_webvtt"] = True
        job_cfg.setdefault("network", {})["cache_dir"] = str(work_root / "cache")

        print(f"[job {job_id}] ingesting {video_url}")
        video_path = pu.ingest_video(job_cfg)

        print(f"[job {job_id}] sampling frames")
        frames = pu.sample_frames(video_path, job_cfg)
        if not frames:
            raise RuntimeError(
                "No frames sampled — check the video / time window settings.")

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

        # Decide whether to run Google Lens matching.
        #   - SKIP_MATCHING=true  -> never match (fully storage-free)
        #   - SKIP_MATCHING=false -> match if a SerpApi key is present; needs a
        #                            reachable crop URL, so a SCRATCH_BUCKET is
        #                            required (else we warn and skip)
        #   - unset               -> match only when SCRATCH_BUCKET is provided
        if skip_matching is True:
            do_match = False
        elif skip_matching is False:
            do_match = True
        else:
            do_match = scratch.enabled

        if do_match and not scratch.enabled:
            print(f"[job {job_id}] SKIP_MATCHING=false but no SCRATCH_BUCKET set; "
                  f"Google Lens needs a fetchable crop URL. Skipping matching.")
            do_match = False

        if do_match:
            print(f"[job {job_id}] uploading {len(products)} crops to scratch bucket")
            try:
                for p in products:
                    ext = os.path.splitext(p.representative_crop)[1] or ".png"
                    suffix = f"{job_id}/{p.product_id}_{pu.slugify(p.label)}{ext}"
                    p.s3_url = scratch.upload(p.representative_crop, suffix)
            except Exception as e:
                print(f"[job {job_id}] scratch crop upload skipped: {e}")
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

        # Free the scratch crops immediately after matching (no lingering storage).
        scratch.cleanup()

        print(f"[job {job_id}] writing detections.json / detections.vtt")
        pu.write_metadata(products, _video_info(video_path, video_id), job_cfg)

        # Read the two generated files and deliver them INLINE via the callback.
        detections_json = json.loads(json_path.read_text(encoding="utf-8"))
        detections_vtt = vtt_path.read_text(encoding="utf-8")

        payload = {
            "video_id": video_id,
            "job_id": job_id,
            "status": "completed",
            "product_count": len(products),
            "detections_json": detections_json,
            "detections_vtt": detections_vtt,
            "finished_at": _now(),
        }
        send_callback(callback_url, payload, cfg)
        print(f"[job {job_id}] DONE — {len(products)} products")
        return payload
    finally:
        # No storage: always remove the scratch crops and the temp dir.
        scratch.cleanup()
        shutil.rmtree(work_root, ignore_errors=True)
        print(f"[job {job_id}] cleaned up temp dir {work_root}")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def run_from_env(cfg: dict, detector, embedder) -> int:
    """One-shot mode: everything comes from environment variables."""
    video_url = os.getenv("VIDEO_URL") or os.getenv("VIDEO_LINK")
    callback_url = os.getenv("CALLBACK_URL", "")
    video_id = os.getenv("VIDEO_ID", "")
    job_id = os.getenv("JOB_ID") or uuid.uuid4().hex
    skip_matching = None
    if os.getenv("SKIP_MATCHING") is not None:
        skip_matching = _env_bool("SKIP_MATCHING", False)

    if not video_url:
        print("[worker] VIDEO_URL is required in one-shot mode.", file=sys.stderr)
        return 2
    if not video_id:
        print("[worker] VIDEO_ID is required in one-shot mode.", file=sys.stderr)
        return 2

    try:
        process_one(video_url, callback_url, video_id, job_id, skip_matching,
                    cfg, detector, embedder)
        return 0
    except Exception as e:
        print(f"[worker] job {job_id} FAILED: {e}", file=sys.stderr)
        send_callback(
            callback_url,
            {
                "video_id": video_id,
                "job_id": job_id,
                "status": "failed",
                "error": str(e),
                "finished_at": _now(),
            },
            cfg,
        )
        return 1


def run_poll(cfg: dict, detector, embedder) -> int:
    """Poll mode: long-poll SQS_QUEUE_URL and process one video per message.

    Each message body carries: video_url, callback_url, video_id, job_id.
    """
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
        video_url = body.get("video_url") or body.get("url", "")
        callback_url = body.get("callback_url", default_callback)
        video_id = body.get("video_id", "")
        job_id = body.get("job_id") or uuid.uuid4().hex
        skip_matching = body.get("skip_matching")

        if not video_url:
            print("[worker] message missing video_url; deleting.")
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            continue

        try:
            process_one(video_url, callback_url, video_id, job_id, skip_matching,
                        cfg, detector, embedder)
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
        except Exception as e:
            print(f"[worker] job {job_id} failed: {e}", file=sys.stderr)
            send_callback(
                callback_url,
                {"video_id": video_id, "job_id": job_id, "status": "failed",
                 "error": str(e), "finished_at": _now()},
                cfg,
            )
            # Leave the message so SQS retries / sends it to the DLQ.


def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot video detection worker")
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Long-poll SQS_QUEUE_URL instead of reading VIDEO_URL once.",
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

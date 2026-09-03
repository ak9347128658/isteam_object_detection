"""
pipeline_utils.py
=================
Reusable building blocks for the product-detection & recommendation pipeline.

The Jupyter notebook (product_pipeline.ipynb) imports from here so that the
notebook cells stay short and readable while the real logic is testable.

Pipeline stages
---------------
1. ingest      -> get a local video file from local path / URL / S3
2. frames      -> sample frames (with scene-change filtering)
3. detect      -> open-vocabulary detection (YOLOE / YOLO-World), ignore humans
4. dedup       -> collapse the same product across frames via CLIP embeddings
5. match       -> find EXACT products on ecommerce via a pluggable backend
6. s3 upload   -> push crops to S3
7. metadata    -> write timestamped JSON (+ optional WebVTT) for video overlay

Everything is driven by config.yaml. Nothing is hard-coded.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(path: str | Path = "config.yaml") -> dict:
    """Load the YAML config into a plain dict."""
    import yaml
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env(dotenv_path: str | Path = ".env") -> None:
    """Load environment variables from a .env file if present."""
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path)
    except Exception:
        # dotenv is optional; env vars may already be set another way.
        pass


def get(cfg: dict, dotted_key: str, default: Any = None) -> Any:
    """Safe nested lookup: get(cfg, 'matching.min_match_score', 0.9)."""
    node: Any = cfg
    for part in dotted_key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = re.sub(r"[^\w\-]+", "-", text.strip().lower())
    return re.sub(r"-{2,}", "-", text).strip("-") or "video"


def load_product_prompts(cfg: dict) -> list[str]:
    """
    Build the open-vocabulary prompt list from:
      1. detection.product_prompts_file  (one name per line, '#' comments ignored)
      2. detection.product_prompts       (inline extras, appended)
    Duplicates are removed while preserving order.
    """
    prompts: list[str] = []
    prompts_file = get(cfg, "detection.product_prompts_file")
    if prompts_file and Path(prompts_file).exists():
        for line in Path(prompts_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                prompts.append(line)
    prompts.extend(get(cfg, "detection.product_prompts", []) or [])

    seen: set[str] = set()
    unique: list[str] = []
    for p in prompts:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _patch_torchvision_functional_tensor() -> None:
    """basicsr still imports torchvision.transforms.functional_tensor (removed)."""
    import sys
    import types
    try:
        import torchvision.transforms.functional_tensor  # noqa: F401
    except ModuleNotFoundError:
        import torchvision.transforms.functional as _F
        _m = types.ModuleType("torchvision.transforms.functional_tensor")
        _m.rgb_to_grayscale = _F.rgb_to_grayscale
        sys.modules["torchvision.transforms.functional_tensor"] = _m


def _resolve_yolo_weights(weights: str) -> str:
    """Prefer a prefetched local checkpoint; otherwise let Ultralytics download."""
    p = Path(weights)
    if p.is_file():
        return str(p.resolve())
    env_dir = os.getenv("ISTEAM_MODELS_DIR")
    candidates = []
    if env_dir:
        candidates.append(Path(env_dir) / "yolo" / p.name)
        candidates.append(Path(env_dir) / p.name)
    candidates.extend([
        Path("models") / "yolo" / p.name,
        Path("models") / p.name,
        Path(p.name),
    ])
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    return weights


def _resolve_realesrgan_weights(model_name: str, model_urls: dict[str, str]) -> str:
    """Prefer a prefetched local .pth; otherwise return the download URL."""
    fname = f"{model_name}.pth"
    env_dir = os.getenv("ISTEAM_MODELS_DIR")
    candidates = []
    if env_dir:
        candidates.append(Path(env_dir) / "realesrgan" / fname)
        candidates.append(Path(env_dir) / fname)
    candidates.extend([
        Path("models") / "realesrgan" / fname,
        Path("models") / fname,
        Path(fname),
    ])
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    return model_urls.get(model_name, model_urls["RealESRGAN_x4plus"])


def sha1_of_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def hhmmss(seconds: float) -> str:
    """Format seconds as HH:MM:SS.mmm for WebVTT / human reading."""
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Detection:
    """A single raw detection in a single frame."""
    frame_index: int
    timestamp: float                 # seconds into the video
    label: str
    confidence: float
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    crop_path: str = ""              # local path to the saved crop
    embedding_id: int = -1           # index into the CLIP embedding matrix


@dataclass
class Recommendation:
    """One ecommerce product recommended for a detected item."""
    title: str
    url: str
    source: str            # domain / merchant
    price: Optional[str]
    thumbnail: Optional[str]
    score: float           # 0..1 match score (the "90%" filter compares to this)
    backend: str           # which search backend produced it


@dataclass
class Product:
    """A distinct product (after dedup) plus its recommendations & timeline."""
    product_id: str
    label: str
    representative_crop: str
    s3_url: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    occurrences: list[dict] = field(default_factory=list)   # {timestamp, bbox, confidence}
    recommendations: list[Recommendation] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ===========================================================================
# STAGE 1 - INGEST
# ===========================================================================

def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Return (bucket, key) from an s3:// URI or a virtual-hosted https URL."""
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):]
        bucket, _, key = rest.partition("/")
        return bucket, key
    # https://bucket.s3.region.amazonaws.com/key  OR  https://s3.region.amazonaws.com/bucket/key
    parsed = urllib.parse.urlparse(uri)
    host = parsed.netloc
    path = parsed.path.lstrip("/")
    m = re.match(r"^(?P<bucket>[^.]+)\.s3[.-]", host)
    if m:
        return m.group("bucket"), path
    # path-style
    bucket, _, key = path.partition("/")
    return bucket, key


def ingest_video(cfg: dict) -> Path:
    """
    Resolve the configured input into a local file path.

    Supports:
      - local: copy/verify a path on disk
      - url:   download with yt-dlp (handles direct files AND video-host URLs)
      - s3:    download via boto3 (public or credentialed), or via https for public
    """
    source_type = get(cfg, "input.source_type", "local")
    work_dir = ensure_dir(get(cfg, "input.work_dir", "workdir"))

    if source_type == "local":
        src = Path(get(cfg, "input.local_path"))
        if not src.exists():
            raise FileNotFoundError(f"Local video not found: {src}")
        return src.resolve()

    if source_type == "url":
        url = get(cfg, "input.url")
        return _download_with_ytdlp(url, work_dir)

    if source_type == "s3":
        s3_uri = get(cfg, "input.s3_uri")
        return _download_from_s3(s3_uri, work_dir, cfg)

    raise ValueError(f"Unknown input.source_type: {source_type!r}")


def _download_with_ytdlp(url: str, work_dir: Path) -> Path:
    """Robust download for both direct media links and hosted videos."""
    import yt_dlp
    out_tmpl = str(work_dir / "%(id)s.%(ext)s")
    ydl_opts = {
        "outtmpl": out_tmpl,
        "quiet": True,
        "noprogress": True,
        "format": "bv*+ba/b",           # best video+audio, fall back to best single
        "merge_output_format": "mp4",
        "retries": 5,
        "fragment_retries": 5,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
        if not path.exists():
            mp4 = path.with_suffix(".mp4")
            if mp4.exists():
                return mp4.resolve()
        return path.resolve()


def _download_from_s3(s3_uri: str, work_dir: Path, cfg: dict) -> Path:
    """Download an S3 object. Works for public objects and credentialed access."""
    import boto3
    from botocore import UNSIGNED
    from botocore.client import Config as BotoConfig

    bucket, key = _parse_s3_uri(s3_uri)
    dest = work_dir / Path(key).name
    region = get(cfg, "s3.region") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")

    have_creds = bool(os.getenv("AWS_ACCESS_KEY_ID"))
    if have_creds:
        s3 = boto3.client("s3", region_name=region)
    else:
        # Anonymous access for public buckets (no credentials required).
        s3 = boto3.client("s3", region_name=region,
                          config=BotoConfig(signature_version=UNSIGNED))
    s3.download_file(bucket, key, str(dest))
    return dest.resolve()


# ===========================================================================
# STAGE 2 - FRAME SAMPLING
# ===========================================================================

@dataclass
class Frame:
    index: int
    timestamp: float
    image: Any   # numpy BGR array (from OpenCV)


def sample_frames(video_path: str | Path, cfg: dict) -> list[Frame]:
    """
    Sample frames at the configured interval, optionally within a time window,
    optionally skipping near-identical consecutive frames (scene-change filter).
    """
    import cv2
    import imagehash
    from PIL import Image

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

    every_s = float(get(cfg, "frames.sample_every_seconds", 1.0))
    step = max(1, int(round(fps * every_s)))
    max_frames = int(get(cfg, "frames.max_frames", 0) or 0)
    start_t = get(cfg, "frames.start_time", None)
    end_t = get(cfg, "frames.end_time", None)
    skip_similar = bool(get(cfg, "frames.skip_similar_frames", True))
    max_dist = int(get(cfg, "frames.similar_frame_hash_distance", 4))

    # At each sample point, look at a small window of consecutive frames and
    # KEEP THE SHARPEST one. This avoids grabbing a motion-blurred frame, which
    # is the #1 cause of blurry crops (you can't un-blur later).
    sharp_window = int(get(cfg, "frames.sharpest_window", 5))
    sharp_window = max(1, sharp_window)

    def _sharpness(bgr) -> float:
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    frames: list[Frame] = []
    last_hash = None
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step == 0:
            ts = idx / fps
            in_window = (start_t is None or ts >= start_t) and (end_t is None or ts <= end_t)
            if in_window:
                ok, img = cap.retrieve()
                if ok and img is not None:
                    # Scan the next (sharp_window-1) frames and pick the sharpest.
                    best_img, best_idx, best_score = img, idx, _sharpness(img)
                    for _ in range(sharp_window - 1):
                        if not cap.grab():
                            break
                        idx += 1
                        ok2, cand = cap.retrieve()
                        if not ok2 or cand is None:
                            continue
                        sc = _sharpness(cand)
                        if sc > best_score:
                            best_img, best_idx, best_score = cand, idx, sc
                    img = best_img

                    keep = True
                    if skip_similar:
                        ph = imagehash.phash(Image.fromarray(img[:, :, ::-1]))
                        if last_hash is not None and (ph - last_hash) <= max_dist:
                            keep = False
                        last_hash = ph
                    if keep:
                        frames.append(Frame(index=best_idx, timestamp=best_idx / fps,
                                            image=img))
                        if max_frames and len(frames) >= max_frames:
                            break
        idx += 1
        if total and idx > total:
            break

    cap.release()
    return frames


# ===========================================================================
# SUPER-RESOLUTION (optional) - AI upscaler to genuinely recover detail
# ===========================================================================

class Upscaler:
    """
    Optional GPU super-resolution using Real-ESRGAN. Unlike plain resize, this
    reconstructs real detail, which is the only way to actually *increase*
    quality of small/soft crops rather than just enlarge them.

    Enabled via config crops.super_resolution.enabled. If the package or weights
    are unavailable, it degrades gracefully (returns None so callers fall back
    to classic resize) and warns once.

    Install (GPU):
        pip install realesrgan basicsr
    """

    _warned = False

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.enabled = bool(get(cfg, "crops.super_resolution.enabled", False))
        self.scale = int(get(cfg, "crops.super_resolution.scale", 4))
        self.model_name = get(cfg, "crops.super_resolution.model",
                              "RealESRGAN_x4plus")
        self.tile = int(get(cfg, "crops.super_resolution.tile", 256))
        self.device = get(cfg, "crops.super_resolution.device",
                          get(cfg, "detection.device", "cuda:0"))
        self._engine = None
        if self.enabled:
            self._engine = self._build_engine()

    def _build_engine(self):
        _patch_torchvision_functional_tensor()
        try:
            import torch
            from realesrgan import RealESRGANer
            from basicsr.archs.rrdbnet_arch import RRDBNet
        except Exception as e:  # package missing
            if not Upscaler._warned:
                print(f"[super-res] Real-ESRGAN not available ({e}); falling back "
                      f"to classic upscaling. Install with: pip install realesrgan basicsr")
                Upscaler._warned = True
            self.enabled = False
            return None

        # Model + pretrained weights URL for the common x4 general model.
        model_urls = {
            "RealESRGAN_x4plus": (
                "https://github.com/xinntao/Real-ESRGAN/releases/download/"
                "v0.1.0/RealESRGAN_x4plus.pth"),
            "RealESRNet_x4plus": (
                "https://github.com/xinntao/Real-ESRGAN/releases/download/"
                "v0.1.1/RealESRNet_x4plus.pth"),
        }
        arch = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                       num_block=23, num_grow_ch=32, scale=4)
        try:
            half = str(self.device).startswith("cuda") and torch.cuda.is_available()
            model_path = _resolve_realesrgan_weights(
                self.model_name, model_urls)
            return RealESRGANer(
                scale=4,
                model_path=model_path,
                model=arch,
                tile=self.tile,
                tile_pad=10,
                pre_pad=0,
                half=half,
                device=self.device,
            )
        except Exception as e:
            if not Upscaler._warned:
                print(f"[super-res] Could not initialize Real-ESRGAN ({e}); "
                      f"falling back to classic upscaling.")
                Upscaler._warned = True
            self.enabled = False
            return None

    def enhance(self, bgr):
        """Return a super-resolved BGR image, or None if SR is unavailable."""
        if not self.enabled or self._engine is None:
            return None
        try:
            out, _ = self._engine.enhance(bgr, outscale=self.scale)
            return out
        except Exception as e:
            print(f"[super-res] enhance failed ({e}); using classic upscaling.")
            return None


# ===========================================================================
# STAGE 3 - DETECTION (open-vocabulary, ignore humans)
# ===========================================================================

class Detector:
    """
    Open-vocabulary detector wrapper around Ultralytics YOLOE / YOLO-World.

    YOLOE ("Real-Time Seeing Anything") accepts text prompts at inference and
    ships a large built-in vocabulary, so it can localize arbitrary products.
    """

    def __init__(self, cfg: dict):
        from ultralytics import YOLO
        self.cfg = cfg
        self.backend = get(cfg, "detection.backend", "yoloe")
        self.device = get(cfg, "detection.device", "cuda:0")
        self.conf = float(get(cfg, "detection.confidence_threshold", 0.35))
        self.iou = float(get(cfg, "detection.iou_threshold", 0.50))
        self.min_crop = int(get(cfg, "detection.min_crop_size", 40))
        # Minimum Laplacian-variance sharpness; detections below this (blurry)
        # are skipped. 0 disables the check.
        self.min_sharpness = float(get(cfg, "detection.min_sharpness", 0.0))
        self.ignore = {s.lower() for s in get(cfg, "detection.ignore_labels", [])}
        # Optional AI super-resolution for crops (built once, reused per crop).
        self.upscaler = Upscaler(cfg)
        self.prompts = load_product_prompts(cfg)   # from file + inline extras
        self.use_builtin = bool(get(cfg, "detection.use_builtin_vocab", True))

        weights = _resolve_yolo_weights(
            get(cfg, "detection.model_weights", "yoloe-11l-seg.pt")
        )
        self.model = YOLO(weights)

        # Set the open-vocabulary text classes when we have prompts and are not
        # relying solely on the built-in vocabulary.
        if self.prompts and not self.use_builtin:
            try:
                self.model.set_classes(self.prompts, self.model.get_text_pe(self.prompts))
            except Exception:
                # YOLO-World uses a simpler API.
                try:
                    self.model.set_classes(self.prompts)
                except Exception:
                    pass
        elif self.prompts and self.use_builtin:
            # Bias detection toward our prompts while keeping the broad vocab.
            try:
                self.model.set_classes(self.prompts, self.model.get_text_pe(self.prompts))
            except Exception:
                pass

    def _is_ignored(self, label: str) -> bool:
        lab = label.lower()
        return any(bad == lab or bad in lab.split() for bad in self.ignore)

    def detect_frame(self, frame: Frame) -> list[Detection]:
        results = self.model.predict(
            frame.image, conf=self.conf, iou=self.iou,
            device=self.device, verbose=False,
        )
        dets: list[Detection] = []
        if not results:
            return dets
        r = results[0]
        names = r.names if hasattr(r, "names") else {}
        if r.boxes is None:
            return dets
        for b in r.boxes:
            cls_id = int(b.cls[0])
            label = names.get(cls_id, str(cls_id))
            if self._is_ignored(label):
                continue                      # <-- humans/body-parts filtered out
            conf = float(b.conf[0])
            x1, y1, x2, y2 = (int(v) for v in b.xyxy[0].tolist())
            if (x2 - x1) < self.min_crop or (y2 - y1) < self.min_crop:
                continue
            # Reject motion-blurred detections (low Laplacian variance).
            if self.min_sharpness > 0:
                region = frame.image[max(0, y1):y2, max(0, x1):x2]
                if region.size == 0 or self._sharpness(region) < self.min_sharpness:
                    continue
            dets.append(Detection(
                frame_index=frame.index, timestamp=frame.timestamp,
                label=label, confidence=conf, bbox=(x1, y1, x2, y2),
            ))
        return dets

    @staticmethod
    def _sharpness(bgr) -> float:
        import cv2
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    def save_crop(self, frame: Frame, det: Detection, crops_dir: str | Path) -> str:
        """
        Save the crop at the best achievable quality. Key principle: you CANNOT
        recover detail a low-res frame never had, so we:
          - add a margin so we don't clip the product,
          - upscale ONLY up to a capped factor (no mushy blow-ups),
          - use CUBIC interpolation (clean, no ringing),
          - apply a very mild sharpen only AFTER real upscaling,
          - save LOSSLESS PNG by default (JPEG re-compression softens crops).
        """
        import cv2
        import numpy as np

        crops_dir = ensure_dir(crops_dir)
        H, W = frame.image.shape[:2]
        x1, y1, x2, y2 = det.bbox

        # 1) margin (padding) around the detection
        margin = float(get(self.cfg, "crops.margin_ratio", 0.10))
        bw, bh = (x2 - x1), (y2 - y1)
        px, py = int(bw * margin), int(bh * margin)
        x1 = max(0, x1 - px); y1 = max(0, y1 - py)
        x2 = min(W, x2 + px); y2 = min(H, y2 + py)
        crop = frame.image[y1:y2, x1:x2]
        if crop.size == 0:
            crop = frame.image[max(0, det.bbox[1]):det.bbox[3],
                               max(0, det.bbox[0]):det.bbox[2]]

        min_side = int(get(self.cfg, "crops.min_output_side", 640))
        max_upscale = float(get(self.cfg, "crops.max_upscale", 3.0))
        h, w = crop.shape[:2]
        did_upscale = False
        used_sr = False

        # 2a) AI super-resolution (recovers REAL detail). Only run it when the
        #     crop is actually small enough to benefit, to save GPU time.
        if getattr(self, "upscaler", None) and self.upscaler.enabled \
                and h and w and min(h, w) < min_side:
            sr = self.upscaler.enhance(crop)
            if sr is not None:
                crop = sr
                used_sr = True
                # Real-ESRGAN gives a fixed x4; scale back down to the target so
                # we don't store needlessly huge files.
                h2, w2 = crop.shape[:2]
                if min(h2, w2) > min_side:
                    s = min_side / float(min(h2, w2))
                    crop = cv2.resize(crop, (int(round(w2 * s)), int(round(h2 * s))),
                                      interpolation=cv2.INTER_AREA)

        # 2b) classic upscale fallback (no SR), capped so we don't create mush.
        if not used_sr:
            h, w = crop.shape[:2]
            if h and w and min(h, w) < min_side:
                scale = min(min_side / float(min(h, w)), max_upscale)
                if scale > 1.01:
                    new_w, new_h = int(round(w * scale)), int(round(h * scale))
                    crop = cv2.resize(crop, (new_w, new_h),
                                      interpolation=cv2.INTER_CUBIC)
                    did_upscale = True

        # 3) mild unsharp-mask. Skip it when SR was used (SR already sharp);
        #    only apply after a classic upscale, and keep it gentle.
        strength = float(get(self.cfg, "crops.sharpen_strength", 0.35))
        if did_upscale and not used_sr and strength > 0:
            blur = cv2.GaussianBlur(crop, (0, 0), sigmaX=1.2)
            crop = cv2.addWeighted(crop, 1 + strength, blur, -strength, 0)
            crop = np.clip(crop, 0, 255).astype("uint8")

        # 4) write. PNG (lossless) by default so we don't add JPEG blur.
        fmt = str(get(self.cfg, "crops.format", "png")).lower()
        if fmt == "png":
            fname = f"f{det.frame_index:07d}_{slugify(det.label)}_{x1}-{y1}.png"
            path = str(Path(crops_dir) / fname)
            cv2.imwrite(path, crop, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        else:
            quality = int(get(self.cfg, "crops.jpeg_quality", 98))
            fname = f"f{det.frame_index:07d}_{slugify(det.label)}_{x1}-{y1}.jpg"
            path = str(Path(crops_dir) / fname)
            cv2.imwrite(path, crop, [cv2.IMWRITE_JPEG_QUALITY, quality,
                                     cv2.IMWRITE_JPEG_OPTIMIZE, 1])

        det.crop_path = path
        return path


# ===========================================================================
# STAGE 4 - DEDUP (distinct products via CLIP embeddings)
# ===========================================================================

class Embedder:
    """CLIP image embedder used for dedup and (optionally) offline matching."""

    def __init__(self, cfg: dict, section: str = "dedup"):
        import torch
        import open_clip
        model_name = get(cfg, f"{section}.clip_model", "ViT-B-32")
        pretrained = get(cfg, f"{section}.clip_pretrained", "openai")
        self.device = get(cfg, f"{section}.device", "cuda:0")
        self.torch = torch
        self.model, _, self.preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained, device=self.device
        )
        self.model.eval()

    def embed_image_paths(self, paths: list[str]) -> "Any":
        import numpy as np
        from PIL import Image
        if not paths:
            return np.zeros((0, 512), dtype="float32")
        tensors = []
        for p in paths:
            img = Image.open(p).convert("RGB")
            tensors.append(self.preprocess(img))
        batch = self.torch.stack(tensors).to(self.device)
        with self.torch.no_grad():
            feats = self.model.encode_image(batch)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats.cpu().numpy().astype("float32")


def dedup_products(detections: list[Detection], embeddings, cfg: dict) -> list[Product]:
    """
    Group detections into distinct products using cosine similarity on CLIP
    embeddings. Greedy clustering: each detection joins the first cluster whose
    representative is similar enough, otherwise it starts a new cluster.
    """
    import numpy as np
    threshold = float(get(cfg, "dedup.same_product_similarity", 0.92))
    representative = get(cfg, "dedup.representative", "largest")

    clusters: list[list[int]] = []          # each is a list of detection indices
    cluster_vecs: list[np.ndarray] = []

    for i, det in enumerate(detections):
        vec = embeddings[i]
        best_j, best_sim = -1, -1.0
        for j, cvec in enumerate(cluster_vecs):
            # only merge within the same detected label to avoid odd cross-merges
            rep_idx = clusters[j][0]
            if detections[rep_idx].label != det.label:
                continue
            sim = float(np.dot(vec, cvec))
            if sim > best_sim:
                best_sim, best_j = sim, j
        if best_j >= 0 and best_sim >= threshold:
            clusters[best_j].append(i)
        else:
            clusters.append([i])
            cluster_vecs.append(vec)

    products: list[Product] = []
    for k, members in enumerate(clusters):
        member_dets = [detections[m] for m in members]

        def area(d: Detection) -> int:
            x1, y1, x2, y2 = d.bbox
            return (x2 - x1) * (y2 - y1)

        if representative == "sharpest":
            rep = _sharpest(member_dets)
        else:
            rep = max(member_dets, key=area)

        timestamps = [d.timestamp for d in member_dets]
        prod = Product(
            product_id=f"p{k:04d}",
            label=rep.label,
            representative_crop=rep.crop_path,
            first_seen=min(timestamps),
            last_seen=max(timestamps),
            occurrences=[
                {"timestamp": d.timestamp, "bbox": list(d.bbox),
                 "confidence": round(d.confidence, 4)}
                for d in sorted(member_dets, key=lambda x: x.timestamp)
            ],
        )
        products.append(prod)
    return products


def _sharpest(dets: list[Detection]) -> Detection:
    """Pick the crop with the highest Laplacian variance (sharpness)."""
    import cv2
    best, best_score = dets[0], -1.0
    for d in dets:
        img = cv2.imread(d.crop_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        score = cv2.Laplacian(img, cv2.CV_64F).var()
        if score > best_score:
            best, best_score = d, score
    return best


# ===========================================================================
# STAGE 5 - MATCHING (pluggable ecommerce search backends)
# ===========================================================================

class RateLimiter:
    """Enforce a minimum interval between calls (rate-limit safety)."""
    def __init__(self, min_interval: float):
        self.min_interval = float(min_interval)
        self._last = 0.0

    def wait(self) -> None:
        now = time.time()
        delta = now - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.time()


def with_retries(fn: Callable, cfg: dict, what: str = "request"):
    """Run fn() with exponential backoff. Returns fn() result or raises last error."""
    max_retries = int(get(cfg, "network.max_retries", 5))
    base = float(get(cfg, "network.backoff_base_seconds", 1.5))
    cap = float(get(cfg, "network.backoff_max_seconds", 60))
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:                # noqa: BLE001 - we want broad retry
            last_err = e
            if attempt >= max_retries:
                break
            delay = min(cap, base ** (attempt + 1))
            print(f"[retry] {what} failed ({e}); backing off {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries})")
            time.sleep(delay)
    raise RuntimeError(f"{what} failed after {max_retries} retries: {last_err}")


class SearchCache:
    """Disk cache keyed by image content hash + backend params (avoids re-billing)."""
    def __init__(self, cache_dir: str | Path, enabled: bool = True):
        self.enabled = enabled
        self.dir = ensure_dir(cache_dir) if enabled else None

    def _key(self, image_path: str, extra: str) -> str:
        h = sha1_of_file(image_path)
        return hashlib.sha1(f"{h}|{extra}".encode()).hexdigest()

    def get(self, image_path: str, extra: str):
        if not self.enabled:
            return None
        f = self.dir / f"{self._key(image_path, extra)}.json"
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        return None

    def put(self, image_path: str, extra: str, value) -> None:
        if not self.enabled:
            return
        f = self.dir / f"{self._key(image_path, extra)}.json"
        f.write_text(json.dumps(value), encoding="utf-8")


class Matcher:
    """
    Google-only matcher: reverse image search via SerpApi Google Lens.

    Google Lens is the single source of ecommerce matches. `exact_matches`
    yields the SAME product (the 'exact watch', not just 'a watch').
    Applies the trusted-domain filter and the min_match_score ("90%") threshold.
    """

    def __init__(self, cfg: dict, embedder: Optional[Embedder] = None):
        self.cfg = cfg
        self.min_score = float(get(cfg, "matching.min_match_score", 0.90))
        self.max_results = int(get(cfg, "matching.max_results_per_product", 5))
        self.trusted = [d.lower() for d in get(cfg, "matching.trusted_domains", []) or []]
        # Non-ecommerce sources (social / video / forums / news) that should NEVER
        # show up as product recommendations, even when trusted_domains is empty.
        # This is what stops YouTube, Instagram, TikTok, etc. from leaking in.
        self.blocked = [d.lower() for d in get(cfg, "matching.blocked_domains", []) or []]
        # When true, drop anything that is not on the trusted_domains allow-list.
        # (Only takes effect if trusted_domains is non-empty.)
        self.ecommerce_only = bool(get(cfg, "matching.ecommerce_only", True))
        self.limiter = RateLimiter(get(cfg, "network.min_interval_seconds", 1.0))
        self.cache = SearchCache(
            get(cfg, "network.cache_dir", "cache/search"),
            enabled=bool(get(cfg, "network.cache_enabled", True)),
        )
        # embedder is unused by the Google backend; kept for signature compatibility.
        self.embedder = embedder

    # -- domain / score filtering ------------------------------------------
    def _domain_ok(self, source: str, url: str) -> bool:
        # Build a haystack from the source name, raw url, and the extracted
        # hostname, since SerpApi 'link' can be a google redirect and 'source'
        # a display name.
        hay = f"{source} {url}".lower()
        host = ""
        try:
            host = urllib.parse.urlparse(url).netloc.lower()
        except Exception:
            pass
        hay = f"{hay} {host}"

        # 1) Hard block non-ecommerce sources (social media, video sites,
        #    forums, news, etc.). Applied ALWAYS, so YouTube/Instagram/TikTok
        #    never appear as recommendations even with an empty allow-list.
        if any(b.strip(".").lower() in hay for b in self.blocked):
            return False

        # 2) If a trusted allow-list is configured, keep only those domains.
        if self.trusted:
            return any(t.strip(".").lower() in hay for t in self.trusted)

        # 3) No allow-list: accept everything that survived the block-list.
        return True

    def _finalize(self, recs: list[Recommendation], verbose: bool = True) -> list[Recommendation]:
        n_raw = len(recs)
        after_score = [r for r in recs if r.score >= self.min_score]
        after_domain = [r for r in after_score
                        if self._domain_ok(r.source or "", r.url or "")]
        after_domain.sort(key=lambda r: r.score, reverse=True)
        result = after_domain[: self.max_results]
        if verbose and n_raw and not result:
            print(f"   [match] {n_raw} raw results -> {len(after_score)} passed "
                  f"score>={self.min_score} -> {len(after_domain)} passed the "
                  f"ecommerce domain filter (non-ecommerce sources like YouTube/"
                  f"Instagram are dropped). Nothing kept. Lower "
                  f"matching.min_match_score, or trim matching.trusted_domains/"
                  f"matching.blocked_domains to loosen.")
        return result

    # -- public entry point ------------------------------------------------
    def match(self, product: Product, image_url: Optional[str] = None) -> list[Recommendation]:
        recs = self._match_serpapi(product.representative_crop, image_url)
        return self._finalize(recs)

    # -- SerpApi Google Lens ----------------------------------------------
    def _match_serpapi(self, image_path: str, image_url: Optional[str]) -> list[Recommendation]:
        """
        Google Lens via SerpApi. Lens needs a publicly reachable image URL, so
        pass the S3 URL of the crop (upload happens before matching in the
        notebook). exact_matches yields the SAME product; that is what gives us
        the 'exact watch', not just 'a watch'.
        """
        from serpapi import GoogleSearch
        api_key = os.getenv("SERPAPI_API_KEY")
        if not api_key:
            raise RuntimeError("SERPAPI_API_KEY is not set (see .env).")
        if not image_url:
            raise RuntimeError(
                "Google Lens requires a public image URL. Upload the crop to S3 "
                "with s3.public_read: true before matching (the notebook does "
                "this automatically)."
            )

        lens_type = get(self.cfg, "matching.serpapi.lens_type", "exact_matches")
        params = {
            "engine": "google_lens",
            "url": image_url,
            "type": lens_type,
            "country": get(self.cfg, "matching.serpapi.country", "us"),
            "hl": get(self.cfg, "matching.serpapi.language", "en"),
            "api_key": api_key,
        }
        extra = json.dumps({k: v for k, v in params.items() if k != "api_key"},
                           sort_keys=True)
        cached = self.cache.get(image_path, extra)
        if cached is not None:
            data = cached
        else:
            self.limiter.wait()
            data = with_retries(lambda: GoogleSearch(params).get_dict(),
                                self.cfg, what="serpapi google_lens")
            self.cache.put(image_path, extra, data)

        if data.get("error"):
            print(f"   [match] SerpApi error: {data['error']}")
            return []

        # Collect results from every relevant section Google Lens can return,
        # tagging each with the section it came from (drives the score).
        recs: list[Recommendation] = []
        sections = [
            ("exact_matches", data.get("exact_matches") or []),
            ("products", data.get("products_results")
                or data.get("shopping_results") or []),
            ("visual_matches", data.get("visual_matches") or []),
        ]
        for section, items in sections:
            for it in items:
                price = None
                if isinstance(it.get("price"), dict):
                    price = (it["price"].get("value")
                             or it["price"].get("extracted_value"))
                elif it.get("price"):
                    price = str(it.get("price"))
                pos = it.get("position", 1)
                score = _serpapi_score(section, pos)
                recs.append(Recommendation(
                    title=it.get("title", ""),
                    url=it.get("link", "") or it.get("source_url", ""),
                    source=it.get("source", "") or it.get("displayed_link", ""),
                    price=price,
                    thumbnail=it.get("thumbnail"),
                    score=score,
                    backend="serpapi",
                ))

        # De-duplicate by URL, keeping the highest score.
        best: dict[str, Recommendation] = {}
        for r in recs:
            key = r.url or r.title
            if key and (key not in best or r.score > best[key].score):
                best[key] = r
        out = list(best.values())
        if not out:
            print(f"   [match] Google Lens returned no matches for this image. "
                  f"If the crop is blurry/tiny or the S3 URL is not reachable, "
                  f"Lens can't match it.")
        return out


def _serpapi_score(section: str, position: int) -> float:
    """
    Map a Google Lens result section + rank -> a 0..1 confidence proxy.
    exact_matches are the SAME product (highest). products are shoppable
    matches. visual_matches are look-alikes but still often the right product,
    so they start high enough to survive a reasonable threshold. Rank decays
    gently so deep results still stay above a 0.90 cutoff for the first several.
    """
    base = {"exact_matches": 0.98, "products": 0.95, "visual_matches": 0.93}.get(
        section, 0.92)
    decay = 0.005 * max(0, position - 1)
    return max(0.0, round(base - decay, 4))


# ===========================================================================
# STAGE 6 - S3 UPLOAD
# ===========================================================================

class S3Uploader:
    """
    Uploads crops to S3 and returns a URL Google Lens can fetch.

    Two URL strategies:
      - public_read: false (recommended) -> object stays private; we return a
        time-limited PRESIGNED URL. Works on ANY bucket, no public-access config,
        no ACLs. This avoids the classic AccessDenied caused by sending a
        public-read ACL to a modern bucket that has ACLs disabled.
      - public_read: true -> we try to set a public-read ACL and return a plain
        public URL. This ONLY works if the bucket has ACLs enabled AND Block
        Public Access turned off. If the ACL is rejected, we automatically fall
        back to a presigned URL instead of crashing.
    """

    def __init__(self, cfg: dict, video_id: str):
        import boto3
        self.cfg = cfg
        self.enabled = bool(get(cfg, "s3.enabled", True))
        self.bucket = get(cfg, "s3.bucket")
        self.region = get(cfg, "s3.region", "us-east-1")
        self.prefix = get(cfg, "s3.key_prefix", "product-detections/{video_id}").format(
            video_id=video_id)
        self.public = bool(get(cfg, "s3.public_read", False))
        self.presign_ttl = int(get(cfg, "s3.presign_expiry_seconds", 7 * 24 * 3600))
        self._acl_disabled = False   # set true once we learn ACLs aren't allowed
        self.client = boto3.client("s3", region_name=self.region) if self.enabled else None

    # -- preflight: verify the bucket is reachable & writable, clear errors ----
    def preflight(self) -> None:
        """Fail fast with an actionable message instead of a deep boto3 trace."""
        if not self.enabled:
            return
        from botocore.exceptions import ClientError, NoCredentialsError
        if not self.bucket or self.bucket == "my-output-bucket":
            raise RuntimeError(
                "s3.bucket is not set to a real bucket you own (it is "
                f"{self.bucket!r}). Edit config.yaml -> s3.bucket."
            )
        if not os.getenv("AWS_ACCESS_KEY_ID") and not os.getenv("AWS_PROFILE"):
            raise RuntimeError(
                "No AWS credentials found. Set AWS_ACCESS_KEY_ID / "
                "AWS_SECRET_ACCESS_KEY (or a profile / IAM role)."
            )
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except NoCredentialsError:
            raise RuntimeError("AWS credentials are missing or invalid.")
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchBucket"):
                raise RuntimeError(
                    f"Bucket {self.bucket!r} does not exist. Create it or fix "
                    "s3.bucket in config.yaml."
                )
            if code in ("403", "AccessDenied"):
                raise RuntimeError(
                    f"Access denied to bucket {self.bucket!r}. Either it is not "
                    "yours, or your IAM user/role lacks s3:PutObject / "
                    "s3:ListBucket on it. Check the bucket name and IAM policy."
                )
            if code in ("301", "PermanentRedirect", "AuthorizationHeaderMalformed"):
                raise RuntimeError(
                    f"Bucket {self.bucket!r} is in a different region than "
                    f"{self.region!r}. Set s3.region to the bucket's real region."
                )
            raise

    def _presigned_url(self, key: str) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.presign_ttl)

    def upload(self, local_path: str, key_suffix: Optional[str] = None) -> str:
        if not self.enabled:
            return ""
        from botocore.exceptions import ClientError

        # Derive the key from the requested suffix but force the REAL extension
        # of the local file, so a PNG crop is not served as .jpg (which softens
        # / mis-decodes it for viewers and Google Lens).
        real_ext = Path(local_path).suffix.lower() or ".png"
        if key_suffix:
            suffix = str(Path(key_suffix).with_suffix(real_ext))
        else:
            suffix = Path(local_path).name
        key = f"{self.prefix}/{suffix}"
        content_types = {".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".webp": "image/webp"}
        extra = {"ContentType": content_types.get(real_ext, "application/octet-stream")}

        # Try a public ACL only if requested and we haven't already learned it's
        # disabled on this bucket. Fall back cleanly to a private upload.
        if self.public and not self._acl_disabled:
            try:
                self.client.upload_file(
                    local_path, self.bucket, key,
                    ExtraArgs={**extra, "ACL": "public-read"})
                return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code in ("AccessDenied", "AccessControlListNotSupported",
                            "InvalidBucketAclWithObjectOwnership"):
                    # Bucket has ACLs disabled / Block Public Access on.
                    # Stop trying ACLs and use presigned URLs for the rest.
                    print("[s3] Public ACL not allowed on this bucket; using "
                          "presigned URLs instead (this is fine for Google Lens).")
                    self._acl_disabled = True
                else:
                    raise

        # Private upload + presigned URL (works on any bucket).
        self.client.upload_file(local_path, self.bucket, key, ExtraArgs=extra)
        return self._presigned_url(key)


# ===========================================================================
# STAGE 7 - METADATA OUTPUT (timestamped, for video overlay)
# ===========================================================================

def write_metadata(products: list[Product], video_info: dict, cfg: dict) -> dict:
    """Write the timestamped detections JSON (and optional WebVTT)."""
    out_path = get(cfg, "metadata.output_path", "output/detections.json")
    ensure_dir(Path(out_path).parent)

    payload = {
        "video": video_info,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": {
            "min_match_score": get(cfg, "matching.min_match_score"),
            "matching_backend": "google_lens",
            "lens_type": get(cfg, "matching.serpapi.lens_type"),
            "detection_backend": get(cfg, "detection.backend"),
        },
        "product_count": len(products),
        "products": [p.to_dict() for p in products],
    }
    Path(out_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if bool(get(cfg, "metadata.emit_webvtt", True)):
        _write_webvtt(products, cfg)

    return payload


def _write_webvtt(products: list[Product], cfg: dict) -> None:
    """
    Emit a WebVTT track so recommendations can be overlaid on an HTML5 <video>
    exactly at the timestamps where each product appears.
    """
    vtt_path = get(cfg, "metadata.webvtt_path", "output/detections.vtt")
    ensure_dir(Path(vtt_path).parent)
    lines = ["WEBVTT", ""]
    for p in products:
        top = p.recommendations[0] if p.recommendations else None
        label = p.label
        rec_txt = f" -> {top.title} ({top.price or ''}) {top.url}" if top else ""
        # one cue spanning first_seen..last_seen; split per-occurrence if you prefer
        start = hhmmss(p.first_seen)
        end = hhmmss(max(p.last_seen, p.first_seen + 1.0))
        lines.append(f"{start} --> {end}")
        lines.append(f"[{p.product_id}] {label}{rec_txt}")
        lines.append("")
    Path(vtt_path).write_text("\n".join(lines), encoding="utf-8")

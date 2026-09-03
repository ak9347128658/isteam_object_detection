"""Local paths for this service. Everything lives under this folder."""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
MODELS_DIR = Path(os.getenv("ISTEAM_MODELS_DIR", BACKEND_DIR / "models"))
YOLO_DIR = MODELS_DIR / "yolo"
CLIP_DIR = MODELS_DIR / "clip"
REALESRGAN_DIR = MODELS_DIR / "realesrgan"
JOBS_DIR = BACKEND_DIR / "workdir" / "api-jobs"
CONFIG_PATH = Path(os.getenv("ISTEAM_CONFIG", BACKEND_DIR / "config.yaml"))
ENV_PATH = BACKEND_DIR / ".env"


def bootstrap() -> None:
    """Make this folder importable no matter where it was copied."""
    root = str(BACKEND_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)


def apply_model_cache_env() -> None:
    """Point torch / ultralytics / open_clip at the prefetched models/ tree."""
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    YOLO_DIR.mkdir(parents=True, exist_ok=True)
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    REALESRGAN_DIR.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("ISTEAM_MODELS_DIR", str(MODELS_DIR))
    os.environ.setdefault("TORCH_HOME", str(CLIP_DIR))
    os.environ.setdefault("ULTRALYTICS_HOME", str(YOLO_DIR))
    os.environ.setdefault("HF_HOME", str(CLIP_DIR / "hf"))
    os.environ.setdefault("TRANSFORMERS_CACHE", str(CLIP_DIR / "hf"))

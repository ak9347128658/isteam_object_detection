"""
Prefetch YOLOE, CLIP, and Real-ESRGAN weights into backend/models.

Run from this folder:

    python scripts/prefetch_models.py

Or from anywhere:

    python /path/to/backend/scripts/prefetch_models.py --device cpu
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.request
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))
os.chdir(_BACKEND_DIR)

from paths import (  # noqa: E402
    BACKEND_DIR,
    CLIP_DIR,
    CONFIG_PATH,
    ENV_PATH,
    MODELS_DIR,
    REALESRGAN_DIR,
    YOLO_DIR,
    apply_model_cache_env,
    bootstrap,
)

bootstrap()
apply_model_cache_env()

import pipeline_utils as pu  # noqa: E402

REALESRGAN_URLS = {
    "RealESRGAN_x4plus": (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/"
        "v0.1.0/RealESRGAN_x4plus.pth"
    ),
    "RealESRNet_x4plus": (
        "https://github.com/xinntao/Real-ESRGAN/releases/download/"
        "v0.1.1/RealESRNet_x4plus.pth"
    ),
}


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0:
        print(f"  already present: {dest} ({dest.stat().st_size} bytes)")
        return
    print(f"  downloading {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)
    print(f"  saved {dest} ({dest.stat().st_size} bytes)")


def _pick_device(requested: str) -> str:
    try:
        import torch
        if requested.startswith("cuda") and torch.cuda.is_available():
            return requested
        if requested == "auto":
            return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        pass
    if requested == "auto":
        return "cpu"
    return requested


def main() -> int:
    parser = argparse.ArgumentParser(description="Prefetch detection / CLIP / Real-ESRGAN weights")
    parser.add_argument(
        "--device",
        default="auto",
        help="cuda:0, cpu, or auto (default). Used when instantiating the models.",
    )
    args = parser.parse_args()

    pu.load_env(ENV_PATH)
    cfg = pu.load_config(CONFIG_PATH)
    device = _pick_device(args.device)
    cfg.setdefault("detection", {})["device"] = device
    cfg.setdefault("dedup", {})["device"] = device
    cfg.setdefault("crops", {}).setdefault("super_resolution", {})["device"] = device

    prompts_file = pu.get(cfg, "detection.product_prompts_file")
    if prompts_file and not Path(prompts_file).is_absolute():
        cfg["detection"]["product_prompts_file"] = str(BACKEND_DIR / prompts_file)

    print(f"Backend dir  : {BACKEND_DIR}")
    print(f"Models dir   : {MODELS_DIR}")
    print(f"Device       : {device}")
    print(f"YOLO cache   : {YOLO_DIR}")
    print(f"CLIP cache   : {CLIP_DIR}")
    print(f"Real-ESRGAN  : {REALESRGAN_DIR}")
    print()

    yolo_weights = pu.get(cfg, "detection.model_weights", "yoloe-11l-seg.pt")
    print(f"[1/3] YOLOE weights ({yolo_weights})")
    from ultralytics import YOLO
    YOLO(yolo_weights)
    name = Path(yolo_weights).name
    dest = YOLO_DIR / name
    if not dest.is_file():
        for cand in (
            Path(yolo_weights),
            BACKEND_DIR / name,
            Path.cwd() / name,
            YOLO_DIR / "weights" / name,
        ):
            if cand.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                if cand.resolve() != dest.resolve():
                    shutil.copy2(cand, dest)
                    print(f"  copied to {dest}")
                break
    print("  YOLOE ready")

    clip_name = pu.get(cfg, "dedup.clip_model", "ViT-B-32")
    clip_pre = pu.get(cfg, "dedup.clip_pretrained", "openai")
    print(f"[2/3] CLIP ({clip_name}, {clip_pre})")
    import open_clip
    open_clip.create_model_and_transforms(clip_name, pretrained=clip_pre, device=device)
    print("  CLIP ready")

    # YOLOE text-prompt encoder (Ultralytics `clip`). YOLOE's open-vocab prompt
    # path (get_text_pe/set_classes) needs the `clip` package + its text-encoder
    # weights. Warm it here so both are baked into the image and NOTHING is
    # downloaded at runtime. Do it explicitly (not swallowed) so the build fails
    # loudly if the offline setup is incomplete.
    use_builtin = bool(pu.get(cfg, "detection.use_builtin_vocab", True))
    prompts = pu.load_product_prompts(cfg)
    if prompts:
        print(f"[2b/3] YOLOE text-prompt encoder (clip) for {len(prompts)} prompts")
        import clip  # noqa: F401  (Ultralytics CLIP text encoder; must be importable)
        from ultralytics import YOLO
        m = YOLO(pu._resolve_yolo_weights(yolo_weights))
        # Downloads + caches the CLIP text weights YOLOE uses for prompt PE.
        m.set_classes(prompts, m.get_text_pe(prompts))
        print("  YOLOE text encoder ready")

    clip_name = pu.get(cfg, "dedup.clip_model", "ViT-B-32")
    clip_pre = pu.get(cfg, "dedup.clip_pretrained", "openai")
    print(f"[3/3] CLIP embedder ({clip_name}, {clip_pre})")
    import open_clip
    open_clip.create_model_and_transforms(clip_name, pretrained=clip_pre, device=device)
    print("  CLIP embedder ready")

    sr_name = pu.get(cfg, "crops.super_resolution.model", "RealESRGAN_x4plus")
    if bool(pu.get(cfg, "crops.super_resolution.enabled", False)):
        print(f"[extra] Real-ESRGAN ({sr_name})")
        url = REALESRGAN_URLS.get(sr_name, REALESRGAN_URLS["RealESRGAN_x4plus"])
        _download(url, REALESRGAN_DIR / f"{sr_name}.pth")
    else:
        print(f"[extra] Real-ESRGAN disabled in config; downloading {sr_name} anyway "
              f"so enabling it later needs no runtime download")
        url = REALESRGAN_URLS.get(sr_name, REALESRGAN_URLS["RealESRGAN_x4plus"])
        _download(url, REALESRGAN_DIR / f"{sr_name}.pth")

    print()
    print("Warming Detector + Embedder (same path as the worker)...")
    pu.Detector(cfg)
    pu.Embedder(cfg, section="dedup")
    print("Prefetch complete. Run one video with: python worker.py "
          "(set VIDEO_S3_URI + CALLBACK_URL), or poll SQS with: python worker.py --poll")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

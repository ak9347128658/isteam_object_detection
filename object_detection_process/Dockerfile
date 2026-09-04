# ============================================================================
# One-shot video object-detection worker (Architecture 1).
#
# The image bakes the model weights in at BUILD time (prefetch_models.py) so the
# container never downloads models at runtime — it starts, processes ONE video,
# uploads results to S3, POSTs the callback, and exits.
#
# CPU build (default):
#   docker build -t object-detection-process .
#
# GPU build: switch the base image to an NVIDIA CUDA runtime + install a CUDA
# torch wheel, e.g. base "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04" and
#   pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
# then run with `--gpus all` and set detection.device=cuda:0 in config.yaml.
# ============================================================================

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ISTEAM_MODELS_DIR=/app/models

# --- OS deps for OpenCV / video decoding ---
# `git` is needed ONLY at build time so we can pip-install the Ultralytics CLIP
# text-encoder package (git+https://github.com/ultralytics/CLIP.git). Baking it
# in now means the container never tries to pip-install it at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        ffmpeg \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps (CPU torch by default; see header for GPU) ---
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

# --- Ultralytics CLIP text encoder (for YOLOE text prompts / get_text_pe) ---
# YOLOE's open-vocabulary prompt path imports `clip`. If it's missing at runtime,
# Ultralytics tries to `pip install git+.../CLIP.git` on the fly (and fails when
# offline). Install it + its deps at BUILD time so runtime is fully offline.
RUN pip install ftfy regex \
    && pip install "git+https://github.com/ultralytics/CLIP.git"

# --- App code ---
COPY paths.py pipeline_utils.py worker.py config.yaml product_prompts.txt ./
COPY scripts ./scripts

# --- Bake ALL model weights into the image (no runtime downloads) ---
# With `clip` installed above, prefetch's Detector warm-up also downloads and
# caches the YOLOE text-prompt embeddings, so nothing is fetched at runtime.
RUN python scripts/prefetch_models.py --device cpu

# --- Force fully-offline behavior at RUNTIME -------------------------------
# Weights are already baked in above; these flags stop every library from
# phoning home (HF Hub, Ultralytics AutoUpdate) while processing a video.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    YOLO_OFFLINE=1 \
    ULTRALYTICS_AUTOUPDATE=false

# One-shot entrypoint: reads VIDEO_S3_URI / CALLBACK_URL / JOB_ID from env,
# processes the video, uploads outputs, POSTs the callback, then exits.
ENTRYPOINT ["python", "worker.py"]

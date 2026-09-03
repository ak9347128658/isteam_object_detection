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
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps (CPU torch by default; see header for GPU) ---
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

# --- App code ---
COPY paths.py pipeline_utils.py worker.py config.yaml product_prompts.txt ./
COPY scripts ./scripts

# --- Bake model weights into the image (no runtime downloads) ---
RUN python scripts/prefetch_models.py --device cpu

# One-shot entrypoint: reads VIDEO_S3_URI / CALLBACK_URL / JOB_ID from env,
# processes the video, uploads outputs, POSTs the callback, then exits.
ENTRYPOINT ["python", "worker.py"]

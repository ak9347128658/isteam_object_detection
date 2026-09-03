# object_detection_process — CPU Setup & Model Prefetch

This document records every step taken to make `object_detection_process` run
**without a GPU** and to **prefetch the model weights** locally.

Platform used: Windows (PowerShell), Python 3.12.10 in the local `venv`.

---

## 1. Goal

- Make the worker run on CPU only (no CUDA GPU required).
- Prefetch YOLOE, CLIP, and Real-ESRGAN model weights so runs don't download at runtime.

---

## 2. CPU configuration changes (`config.yaml`)

The runtime driver (`worker.py`) already auto-detects hardware: its
`_pick_device()` downgrades any `cuda:0` request to `cpu` when no CUDA GPU is
present, and applies that to detection, dedup, and super-resolution. The
`Dockerfile` also installs the CPU torch wheel by default and prefetches with
`--device cpu`.

The only GPU-oriented parts were the defaults in `config.yaml`. These were
changed so CPU is the explicit default:

| Setting | Before | After |
|---|---|---|
| `detection.device` | `cuda:0` | `cpu` |
| `dedup.device` | `cuda:0` | `cpu` |
| `crops.super_resolution.device` | `cuda:0` | `cpu` |
| `crops.super_resolution.enabled` | `true` | `false` |

**Why disable super-resolution?** Real-ESRGAN runs a heavy model on every crop.
On CPU it is extremely slow, so a single video could take a very long time.
With it off, crops fall back to the fast classic upscaler, which works fine on
CPU. It can be turned back on if a GPU is added later.

Notes:
- No active (non-comment) `cuda:0` lines remain in `config.yaml`.
- The remaining `cuda:0` strings in `pipeline_utils.py` are only fallback
  defaults used when a config key is missing; the explicit `cpu` values in
  `config.yaml` take precedence, and `_pick_device()` guards against GPU use.

---

## 3. Environment setup (venv)

Environment checked:

```powershell
.\venv\Scripts\python.exe --version        # Python 3.12.10
.\venv\Scripts\python.exe -m pip --version  # pip 25.0.1
```

### 3a. Install CPU torch + torchvision

```powershell
.\venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
```

Installed: `torch 2.14.0+cpu`, `torchvision 0.29.0+cpu` (plus deps: numpy,
pillow, sympy, networkx, etc.).

Verified CPU-only:

```powershell
.\venv\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available())"
# -> 2.14.0+cpu  False
```

### 3b. Install the rest of the requirements

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

Key versions installed: `ultralytics 8.4.138`, `open-clip-torch 3.3.0`,
`realesrgan 0.3.0`, `basicsr 1.4.2`, `opencv-python(-headless) 5.0.0.93`,
`boto3 1.43.87`, `google-search-results 2.4.2`, `yt-dlp`, `pyyaml`,
`python-dotenv`.

Note: `torchvision 0.29` removed `functional_tensor`, which `basicsr` imports.
`pipeline_utils.py` already handles this via `_patch_torchvision_functional_tensor()`.

---

## 4. Prefetch the models

```powershell
.\venv\Scripts\python.exe scripts\prefetch_models.py --device cpu
```

Stages reported by the script:

1. **[1/3] YOLOE weights** (`yoloe-11l-seg.pt`) — downloaded, "YOLOE ready".
2. **[2/3] CLIP** (`ViT-B-32`, `openai`) — downloaded, "CLIP ready".
   - A harmless `QuickGELU mismatch` warning is printed by open_clip.
3. **[3/3] Real-ESRGAN** (`RealESRGAN_x4plus`) — downloaded / already present.
4. **Warm-up**: builds the Detector + Embedder (same path as the API).
   - During warm-up, ultralytics auto-installed a small CLIP package from git
     (`git+https://github.com/ultralytics/CLIP.git`) that YOLOE needs for
     open-vocabulary text prompts. It succeeded.
   - It also downloaded a ~572 MB MobileCLIP text-prompt model used by YOLOE.
   - Finished with: `Prefetch complete.`

**Warm-up is slow on CPU** because `product_prompts.txt` has ~2,318 prompts and
`use_builtin_vocab: true`, so text-prompt embeddings are built for all of them
plus YOLOE's built-in vocabulary. This is a one-time cost per process start
(not per video). To speed startup: trim the prompt list or set
`detection.use_builtin_vocab: false`.

---

## 5. Verification — weights on disk

```powershell
Get-ChildItem -Recurse models -File
```

Prefetched weights (under `object_detection_process/models/`):

| Model | Path | Size |
|---|---|---|
| YOLOE detector | `models/yolo/yoloe-11l-seg.pt` | ~67.7 MB |
| Real-ESRGAN | `models/realesrgan/RealESRGAN_x4plus.pth` | ~63.9 MB |
| CLIP + YOLOE text model | `models/clip/` (tree) | ~1.15 GB total |

---

## 6. How to run (CPU, no Docker)

```powershell
# one-shot: reads VIDEO_S3_URI + CALLBACK_URL from env
.\venv\Scripts\python.exe worker.py

# poll mode: long-poll SQS_QUEUE_URL
.\venv\Scripts\python.exe worker.py --poll
```

For Docker, the default `docker build -t object-detection-process .` already
produces a CPU image (no GPU flags needed).

---

## 7. Optional follow-ups (not yet applied)

- Add `git+https://github.com/ultralytics/CLIP.git` to `requirements.txt` to make
  the YOLOE text-prompt dependency explicit rather than auto-installed.
- Reduce `product_prompts.txt` or set `use_builtin_vocab: false` to cut CPU
  startup time.

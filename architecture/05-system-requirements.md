# 05 — System Requirements & Sizing

This architecture runs **at most two workers at once**, so the host must
comfortably fit **two** full pipeline processes plus the API + dispatcher. The
sizing below is derived from what the pipeline actually loads (measured from
`CPU_SETUP.md` and the image build): YOLOE (~68 MB), Real-ESRGAN (~64 MB), and
CLIP + YOLOE's text-prompt model (~1.15 GB), plus OpenCV frame buffers and torch
runtime.

---

## 1. What one worker consumes

| Resource | One worker (CPU mode) | Notes |
|---|---|---|
| **Model weights on disk** | ~1.3 GB, **baked into the image** | YOLOE + Real-ESRGAN + CLIP + YOLOE text model. Downloaded at build, not runtime. |
| **RAM (resident)** | ~2.5–4 GB | torch CPU runtime + loaded models + frame buffers. Grows with video resolution (1080p frames are big numpy arrays). |
| **CPU** | 2–4 vCPU effective | YOLOE inference per frame is the hot path on CPU. |
| **Disk (temp, per job)** | ~0.5–2 GB transient | Downloaded video + crops in the per-job temp dir; deleted on exit. Use `tmpfs`/RAM disk if you want to avoid disk I/O entirely. |
| **Startup cost** | Tens of seconds | Building text-prompt embeddings for ~2,318 prompts + built-in vocab (one-time per process). See tuning below. |
| **Network out** | SerpApi (Google Lens) + the callback POST | Rate-limited by `network.min_interval_seconds`. |

> **GPU is optional but strongly recommended for throughput.** On CPU a single
> video takes minutes (model load + per-frame YOLOE + CLIP). On a modern GPU the
> same job is much faster and lets you enable AI super-resolution
> (`crops.super_resolution.enabled: true`, disabled by default for CPU).

---

## 2. Host sizing for MAX_CONCURRENCY = 2

Because two workers can run at once, budget for two, plus headroom for the OS,
API, and dispatcher.

### CPU-only host (baseline, no GPU)

| Component | Recommended |
|---|---|
| **vCPU** | **8 vCPU** (4 per worker) minimum; 16 vCPU for good throughput. |
| **RAM** | **16 GB** minimum (2 × ~4 GB workers + OS + API + burst headroom). 32 GB comfortable. |
| **Disk** | **40 GB** SSD: OS + one Docker image (~4–6 GB with baked weights) + transient temp for 2 jobs. |
| **OS** | Linux (Ubuntu 22.04 LTS recommended) for production; Docker Desktop on Windows/macOS for dev. |
| **Network** | Stable outbound HTTPS (download videos, SerpApi, callback). |

Example instances: AWS `c7i.2xlarge` (8 vCPU / 16 GB) or `m7i.2xlarge`
(8 vCPU / 32 GB); a 8-core / 16 GB VM or bare-metal box on-prem.

### GPU host (recommended for real throughput)

| Component | Recommended |
|---|---|
| **GPU** | 1× NVIDIA GPU with **≥ 12 GB VRAM** (e.g. T4 16 GB, L4, A10). Two workers share one GPU; ~4–6 GB VRAM each with `tile` bounding Real-ESRGAN memory. |
| **vCPU** | 8 vCPU (decode + pre/post-processing). |
| **RAM** | 16–32 GB. |
| **Disk** | 60 GB SSD (CUDA image is larger). |
| **Driver/toolkit** | NVIDIA driver + CUDA 12.x + `nvidia-container-toolkit` so Docker can use `--gpus`. |

Example instances: AWS `g5.xlarge` (1× A10G 24 GB) comfortably runs two workers;
`g4dn.xlarge` (1× T4 16 GB) for lighter load. On ECS use a GPU-capable capacity
provider.

> If you prefer strict isolation, give **each** worker its own GPU (2 GPUs) — but
> one ≥16 GB GPU shared by two workers is usually fine for this model set.

---

## 3. Software prerequisites

| Software | Version | Where |
|---|---|---|
| **Docker Engine** | 24+ | Host that runs workers + dispatcher. |
| **Docker Compose** | v2 | Single-box deployment (see [`07-deployment.md`](07-deployment.md)). |
| **Python** | 3.11 (image) / 3.11–3.12 (dev) | API + dispatcher. |
| **NVIDIA Container Toolkit** | latest | Only for the GPU host. |
| **AWS CLI v2** | latest | If using SQS and/or the ephemeral scratch bucket for Lens crops. |

Runtime services the workers reach out to:

- **SerpApi** (Google Lens) — needs `SERPAPI_API_KEY`. Skippable via
  `SKIP_MATCHING=true`.
- **The caller's `callback_url`** — must be reachable from the workers' network.
- **Ephemeral scratch bucket** (only if using crop-matching option A in
  [`04-worker-and-no-storage.md`](04-worker-and-no-storage.md)).

---

## 4. Throughput & capacity planning

- **Concurrency:** fixed at 2 by design.
- **Throughput ≈ `2 / T`** videos per unit time, where `T` = seconds to process
  one video end-to-end (dominated by model start-up + per-frame inference).
- **Reduce `T`:**
  - Use a **GPU** (`detection.device: cuda:0`).
  - **Trim `product_prompts.txt`** or set `detection.use_builtin_vocab: false` —
    the ~2,318-prompt text-embedding build is a large chunk of CPU start-up time.
  - Increase `frames.sample_every_seconds` (fewer frames) for faster, coarser
    passes.
  - Keep `crops.super_resolution.enabled: false` on CPU.
- **Queue depth** should be sized to your peak arrival rate × `T / 2`; if peak
  demand outstrips two workers for long periods, either raise `MAX_CONCURRENCY`
  (and the host size to match) or scale horizontally with more hosts each
  capped at 2 (or run more ECS tasks — the "2" cap is per dispatcher, so
  multiple dispatchers = multiples of 2).

---

## 5. Security requirements

| Concern | Requirement |
|---|---|
| **SSRF via `callback_url` / `video_url`** | Validate URLs; ideally allow-list callback hosts and block link-local/metadata addresses (`169.254.169.254`, RFC-1918 ranges) so the system can't be used to reach internal services. |
| **Secrets** | `SERPAPI_API_KEY`, AWS creds → environment / secrets manager, never baked into the image or logged. |
| **Least privilege** | Worker/dispatcher IAM role: only what's needed (SQS receive/delete if SQS; scratch-bucket put/delete if crop-matching option A). No broad S3 access. |
| **Outbound TLS** | Callback and SerpApi over HTTPS only. |
| **Egress control** | Restrict worker egress to the callback host, SerpApi, and the video CDN where possible. |
| **Resource caps** | Set `--memory` and `--cpus` (and `--gpus`) per container so one worker can't starve the other or the host. |
| **Data handling** | With no storage, the video/crops never persist — a privacy plus. Ensure logs don't dump frame data or full payloads. |

# Build the object_detection_process image (models baked in) & push to Amazon ECR

This guide builds the Docker image for `object_detection_process` **with all
model weights baked in** (YOLOE + CLIP + Real-ESRGAN + YOLOE's text-prompt
model), then pushes it to a private **Amazon ECR** repository.

> All commands are run from inside the `object_detection_process/` folder unless
> stated otherwise.

---

## 0. How models get into the image

You do **not** copy your local `models/` folder into the image. The
`Dockerfile` runs `scripts/prefetch_models.py` **during the build**, so the
weights are downloaded and baked into the image layer. At runtime the container
never downloads models.

- `.dockerignore` intentionally excludes `models/`, `.env`, `workdir/`,
  `cache/`, `output/`, and video files.
- Result: a self-contained image that starts, processes one video, uploads
  results, POSTs a callback, and exits.

---

## 1. Prerequisites

- **Docker** installed and running (`docker version`).
- **AWS CLI v2** installed (`aws --version`).
- AWS credentials configured with ECR permissions:
  ```powershell
  aws configure
  # or set env vars: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION
  ```
- Confirm identity:
  ```powershell
  aws sts get-caller-identity
  ```

Set reusable variables (PowerShell). Replace values as needed:

```powershell
$AWS_REGION   = "us-east-1"
$ACCOUNT_ID   = (aws sts get-caller-identity --query Account --output text)
$REPO_NAME    = "object-detection-process"
$IMAGE_TAG    = "latest"
$ECR_URI      = "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO_NAME"
```

---

## 2. Create the ECR repository (one-time)

```powershell
aws ecr create-repository `
  --repository-name $REPO_NAME `
  --region $AWS_REGION `
  --image-scanning-configuration scanOnPush=true
```

If it already exists, this errors harmlessly — skip it. Verify:

```powershell
aws ecr describe-repositories --repository-names $REPO_NAME --region $AWS_REGION
```

---

## 3. Authenticate Docker to ECR

```powershell
aws ecr get-login-password --region $AWS_REGION | `
  docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"
```

Expected: `Login Succeeded`.

---

## 4. Build the image (CPU, models baked in)

The current `Dockerfile` builds a **CPU** image by default (installs the CPU
torch wheel and runs `prefetch_models.py --device cpu`).

```powershell
docker build -t "$REPO_NAME`:$IMAGE_TAG" .
```

Notes:
- The build runs `scripts/prefetch_models.py`, which downloads ~1.3 GB of
  weights (YOLOE ~68 MB, Real-ESRGAN ~64 MB, CLIP + YOLOE text model ~1.15 GB).
  The final image is large; the build takes a while on first run.
- To avoid the "prefetch runs but layer cache is cold every time" problem, the
  weights are cached inside the image layer as long as the earlier layers
  (requirements) don't change.

Quick local sanity check (optional):
```powershell
docker run --rm "$REPO_NAME`:$IMAGE_TAG" --help
```

### (Optional) GPU image
To build for GPU instead, edit the `Dockerfile`:
- base image -> `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04`
- torch install -> `pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124`
- prefetch -> `python scripts/prefetch_models.py --device cuda:0`
- set `detection.device: cuda:0` in `config.yaml`, run with `--gpus all`.

---

## 5. Tag the image for ECR

```powershell
docker tag "$REPO_NAME`:$IMAGE_TAG" "$ECR_URI`:$IMAGE_TAG"
```

Optionally also tag with a version / git SHA for traceability:

```powershell
$GIT_SHA = (git rev-parse --short HEAD)
docker tag "$REPO_NAME`:$IMAGE_TAG" "$ECR_URI`:$GIT_SHA"
```

---

## 6. Push to ECR

```powershell
docker push "$ECR_URI`:$IMAGE_TAG"
# and, if you tagged a SHA:
docker push "$ECR_URI`:$GIT_SHA"
```

Verify the pushed image:

```powershell
aws ecr describe-images --repository-name $REPO_NAME --region $AWS_REGION
```

---

## 7. Run the pushed image (verification)

Pull-and-run on any Docker host that is authenticated to ECR (step 3):

```powershell
docker run --rm `
  -e VIDEO_S3_URI="s3://isteam-video-input/uploads/clip.mp4" `
  -e CALLBACK_URL="https://api.example.com/detections/callback" `
  -e OUTPUT_BUCKET="isteam-video-output" `
  -e OUTPUT_REGION="$AWS_REGION" `
  -e AWS_ACCESS_KEY_ID="..." `
  -e AWS_SECRET_ACCESS_KEY="..." `
  -e AWS_DEFAULT_REGION="$AWS_REGION" `
  -e SERPAPI_API_KEY="..." `
  "$ECR_URI`:$IMAGE_TAG"
```

On ECS/Fargate or EC2, prefer an **IAM task role / instance profile** over
passing `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` as env vars.

Environment variables the container reads (see `.env.example`):
`VIDEO_S3_URI`, `CALLBACK_URL`, `JOB_ID`, `OUTPUT_BUCKET`, `OUTPUT_PREFIX`,
`OUTPUT_REGION`, `OUTPUT_PUBLIC`, `SKIP_MATCHING`, `SQS_QUEUE_URL`,
`SQS_VISIBILITY`, `SERPAPI_API_KEY`, `AWS_*`.

---

## 8. IAM permissions needed

**To push (developer/CI):**
- `ecr:GetAuthorizationToken`
- `ecr:BatchCheckLayerAvailability`, `ecr:InitiateLayerUpload`,
  `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`, `ecr:PutImage`
- `ecr:CreateRepository`, `ecr:DescribeRepositories` (if creating the repo)

**To pull (the task role / runtime host):**
- `ecr:GetAuthorizationToken`
- `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`,
  `ecr:BatchCheckLayerAvailability`

**Runtime (S3 + optional SQS):**
- `s3:GetObject` on the input bucket, `s3:PutObject` on the output bucket
- `sqs:ReceiveMessage`, `sqs:DeleteMessage` (only for `--poll` mode)

---

## 9. One-shot copy/paste (PowerShell)

```powershell
# --- config ---
$AWS_REGION = "us-east-1"
$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text)
$REPO_NAME  = "object-detection-process"
$IMAGE_TAG  = "latest"
$ECR_URI    = "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$REPO_NAME"

# --- repo (ignore error if it exists) ---
aws ecr create-repository --repository-name $REPO_NAME --region $AWS_REGION --image-scanning-configuration scanOnPush=true

# --- login ---
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

# --- build, tag, push (run from object_detection_process/) ---
docker build -t "$REPO_NAME`:$IMAGE_TAG" .
docker tag "$REPO_NAME`:$IMAGE_TAG" "$ECR_URI`:$IMAGE_TAG"
docker push "$ECR_URI`:$IMAGE_TAG"
```

---

## 10. Troubleshooting

- **`no basic auth credentials` on push** — re-run step 3 (ECR login tokens
  expire after 12 hours).
- **Build fails during prefetch (network)** — re-run the build; downloads
  resume from cached layers where possible.
- **Image too large / slow push** — expected (~several GB due to torch + CLIP
  text model). Push over a fast connection; subsequent pushes only send changed
  layers.
- **`denied: ...` from ECR** — check the IAM permissions in section 8 and that
  `$AWS_REGION` / `$ACCOUNT_ID` match your account.
- **Docker not running** — start Docker Desktop, confirm with `docker info`.

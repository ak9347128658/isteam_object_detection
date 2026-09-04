# Updating the worker after pushing a new image to ECR

Whenever you change the worker code (`object_detection_process/`) and push a new
image to ECR, ECS does **not** automatically use it — unless the task definition
points at a **mutable tag** like `:latest` AND the running task is a fresh
launch. This doc explains how to make ECS pick up a new image, both the one-time
setup and the day-to-day flow.

Key facts:
- **Task definitions are immutable.** You never "edit" a revision; you create a
  new revision. But if the image is referenced by the `:latest` tag, you often
  don't need a new revision at all — the next task launch pulls the current
  `:latest`.
- **Pinning by digest (`@sha256:...`) breaks on re-push.** When you push over
  `:latest`, the old digest is discarded and a task def pinned to that digest
  fails with `CannotPullContainerError: ... not found`. Use the tag, not the
  digest.
- Our dispatcher launches by **family name** (`ECS_TASK_DEFINITION=isteam-object-detection-worker`,
  no `:revision`), so it always uses the newest ACTIVE revision automatically.

---

## A. One-time: make the task definition use the `:latest` tag

Do this once so future image pushes need zero ECS changes. If your task def
already references `...:latest` (not `@sha256:...`), skip to section B.

### GUI steps
1. AWS Console → **ECS** → **Task definitions** → click
   `isteam-object-detection-worker`.
2. Select the latest revision → **Create new revision**.
3. Scroll to **Container - 1** → click the `Main` container to expand it.
4. **Image URI** — replace any `@sha256:...` value with the tag form:
   ```
   598886663176.dkr.ecr.us-east-1.amazonaws.com/isteam-object-detection-process:latest
   ```
5. Leave everything else unchanged → **Create**.

That new revision now points at `:latest`. Because the dispatcher uses the
family name, it will use this revision on the next launch.

---

## B. Day-to-day: push a new image, then roll it out

### 1. Build & push (from `object_detection_process/`)
```powershell
$AWS_REGION = "us-east-1"
$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text)
$ECR_URI    = "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/isteam-object-detection-process"

aws ecr get-login-password --region $AWS_REGION | `
  docker login --username AWS --password-stdin "$ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com"

docker build -t "isteam-object-detection-process:latest" .
docker tag  "isteam-object-detection-process:latest" "$ECR_URI`:latest"
docker push "$ECR_URI`:latest"
```

> Recommended: also push an immutable version tag so you can always roll back
> and never point at a deleted digest:
> ```powershell
> docker tag  "isteam-object-detection-process:latest" "$ECR_URI`:v3"
> docker push "$ECR_URI`:v3"
> ```

### 2. Confirm the push landed in ECR (GUI)
1. AWS Console → **ECR** → **Repositories** → `isteam-object-detection-process`.
2. Check the **Image tags** column shows `latest` with a fresh **Pushed at**
   timestamp (and your `v3` tag if you added one).

### 3. Roll it out

**If the task def points at `:latest` (section A done):**
- There's nothing to change in ECS. The **next** worker task the dispatcher
  launches (i.e. the next uploaded video) pulls the new `:latest` automatically.
- Any task already running keeps the old image until it finishes — that's fine,
  each job is one short-lived task.

**If you pushed a new immutable tag (e.g. `:v3`) and want to pin to it:**
1. ECS → **Task definitions** → `isteam-object-detection-worker` →
   **Create new revision**.
2. Edit the `Main` container → **Image URI** → set it to
   `...isteam-object-detection-process:v3` → **Create**.
3. The dispatcher (family-name based) uses this newest revision on the next
   launch. No dispatcher change needed.

> Our workers are launched per-video by the dispatcher (`RunTask`), so there is
> **no ECS Service to update**. If you had run it as a Service instead, you'd
> click the service → **Update service** → check **Force new deployment** to
> replace running tasks. That does not apply to the dispatcher setup.

---

## C. Verify the new image is actually running

After the next video launches a task, grab its id from the dispatcher log
(`ecs launched job ... task .../<TASK_ID>`) and check the image it pulled:

```bash
MSYS_NO_PATHCONV=1 aws ecs describe-tasks \
  --cluster isteam-object-detection-cluster \
  --tasks <TASK_ID> \
  --region us-east-1 \
  --query "tasks[0].{status:lastStatus,stopCode:stopCode,image:containers[0].image}"
```

- `image` should end in `:latest` (or `:v3`) — not a stale `@sha256:...`.
- `stopCode` should NOT be `TaskFailedToStart` (that's the pull error).

Then confirm the code change took effect in the worker logs:

```bash
MSYS_NO_PATHCONV=1 aws logs tail "/ecs/isteam-object-detection-worker" \
  --since 15m --region us-east-1
```

Look for the behavior you changed (e.g. crops uploading to `isteam-video-output`,
no "No AWS credentials found", no `git+.../CLIP.git` install line).

---

## D. Force a brand-new launch to test immediately

You don't have to wait — just upload a video to
`s3://isteam-video-uploader/uploads/` to trigger a fresh task with the new image.

---

## Troubleshooting

- **`CannotPullContainerError: ... @sha256:... not found`** — the task def is
  pinned to a digest you overwrote. Do section A (switch to `:latest`) or point
  it at a still-existing tag.
- **Old behavior still shows in logs** — the task launched before your push, or
  the task def points at an old tag/digest. Re-check section C's `image` value.
- **`no basic auth credentials` on push** — ECR login expired; re-run the
  `get-login-password | docker login` command (tokens last 12h).
- **New env var / CPU / memory change not applied** — those live in the task
  definition, not the image. You must create a new revision (section A steps 1-5,
  changing the relevant field) even if the image tag is unchanged.

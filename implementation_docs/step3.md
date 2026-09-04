# Step 3 — Consume the queue: run workers on ECS, max 3 at a time

Steps 1–2 put a job message on `isteam-object-detection-jobs` whenever a video
is uploaded. Now we stand up the **consumer**: something that pulls a message,
launches the ECR worker image to process that one video, and enforces the rule
**at most 3 workers run at once; when one exits, if the queue still has data,
start another**.

```
SQS: isteam-object-detection-jobs
        │  (dispatcher pulls only when a slot is free)
        ▼
Dispatcher (MAX_CONCURRENCY=3)  --ECS RunTask-->  worker task (1 video) --> S3 + callback --> exit
        ▲                                                                                   │
        └──────────────── slot freed when a task exits, refill from queue ──────────────────┘
```

We use the **dispatcher in ECS mode** (`dispatcher/dispatcher.py`). It already
implements the exact "max 3, refill on exit" logic and launches each job as an
ECS Fargate task from your ECR image.

Prereqs done:
- Worker image pushed to ECR (`object-detection-process`), Step in
  `object_detection_process/STEPS.md`.
- Queue `isteam-object-detection-jobs` receiving messages (Step 2).
- Private buckets `isteam-video-uploader` (input) and `isteam-video-output`.

---

## 1. Create an ECS cluster (Fargate)

1. AWS Console → search **ECS** → **Clusters** → **Create cluster**.
2. **Cluster name**: `isteam-object-detection-cluster`.
3. **Infrastructure**: **AWS Fargate (serverless)** (leave checked).
4. **Create**.

---

## 2. Create the two IAM roles the task needs

A Fargate task uses two roles:

- **Execution role** — lets ECS pull the image from ECR and write logs.
- **Task role** — the permissions your worker code uses at runtime (S3 + calling
  the callback goes over the internet, no IAM needed for that).

### 2a. Execution role

1. IAM → **Roles** → **Create role**.
2. Trusted entity: **AWS service** → use case **Elastic Container Service** →
   **Elastic Container Service Task** → **Next**.
3. Attach policy: **AmazonECSTaskExecutionRolePolicy** → **Next**.
4. Name: `isteam-detection-task-execution-role` → **Create role**.

### 2b. Task role

1. IAM → **Roles** → **Create role** → same trusted entity
   (**Elastic Container Service Task**) → **Next** → **Next** (no managed policy).
2. Name: `isteam-detection-task-role` → **Create role**.
3. Open the role → **Add permissions** → **Create inline policy** → **JSON**:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ReadInputVideos",
         "Effect": "Allow",
         "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::isteam-video-uploader/*"
       },
       {
         "Sid": "WriteDetections",
         "Effect": "Allow",
         "Action": ["s3:PutObject", "s3:GetObject"],
         "Resource": "arn:aws:s3:::isteam-video-output/*"
       }
     ]
   }
   ```

   (The worker also uploads product crops to `isteam-video-output` and generates
   presigned URLs for them, which is why it needs `GetObject` there too.)
4. Name it `detection-worker-s3` → **Create policy**.

---

## 3. Store the SerpApi key (so it isn't a plaintext env var)

1. AWS Console → **Systems Manager** → **Parameter Store** → **Create parameter**.
2. **Name**: `/isteam/detection/serpapi_api_key`.
3. **Type**: **SecureString**.
4. **Value**: your SerpApi key → **Create parameter**.
5. Add read access to the **execution role** (so ECS can inject it): open
   `isteam-detection-task-execution-role` → inline policy → JSON:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["ssm:GetParameters"],
       "Resource": "arn:aws:ssm:us-east-1:598886663176:parameter/isteam/detection/serpapi_api_key"
     }]
   }
   ```

   Then **Next** → **Policy name**: `read-serpapi-key` → **Create policy**.

   (Replace region + account id. Skip this section entirely if you set
   `SKIP_MATCHING=true` and don't use Google Lens yet.)

---

## 4. Create the worker task definition

1. ECS → **Task definitions** → **Create new task definition**.
2. **Task definition family**: `isteam-object-detection-worker`.
3. **Launch type**: **AWS Fargate**.
4. **Operating system/Architecture**: Linux/X86_64.
5. **Task size**: CPU **4 vCPU**, Memory **16 GB** (this is a heavy CV/ML image;
   start here and adjust down if it runs comfortably).
6. **Task role**: `isteam-detection-task-role`.
7. **Task execution role**: `isteam-detection-task-execution-role`.
8. **Container**:
   - **Name**: `worker`  ← must match `ECS_CONTAINER_NAME` used by the dispatcher.
   - **Image URI**: `598886663176.dkr.ecr.us-east-1.amazonaws.com/object-detection-process:latest`
     (your ECR URI from `STEPS.md`).
   - **Essential**: yes.
   - **Environment variables** — the *static* ones (per-video values are injected
     by the dispatcher at RunTask time, so DON'T set VIDEO_S3_URI here):

     | Key | Value |
     |---|---|
     | `OUTPUT_BUCKET` | `isteam-video-output` |
     | `OUTPUT_REGION` | `us-east-1` |
     | `AWS_DEFAULT_REGION` | `us-east-1` |
     | `SKIP_MATCHING` | `false` (or `true` to skip Google Lens for now) |

   - **Secrets** (only if using Google Lens): add `SERPAPI_API_KEY` →
     ValueFrom `arn:aws:ssm:us-east-1:598886663176:parameter/isteam/detection/serpapi_api_key`.
9. **Logging**: leave **Use log collection** ON (creates a CloudWatch log group
   `/ecs/isteam-object-detection-worker`). This is where each worker's
   processing logs go.
10. **Create**.

---

## 5. Run the dispatcher (ECS mode, cap = 3)

The dispatcher is the only thing that reads SQS. It calls ECS `RunTask` per job,
injecting the per-video env (`VIDEO_S3_URI`, `JOB_ID`, `CALLBACK_URL`, etc.), and
never exceeds `MAX_CONCURRENCY`.

You can run it on any small always-on host that has AWS creds + Python (a tiny
EC2 `t3.small`, or your own machine for testing). It does no heavy work — it just
launches tasks and polls their status.

Find your VPC **subnets** and a **security group** (default VPC is fine): VPC
console → Subnets (copy 2 subnet ids), Security groups (copy the default sg id).
The SG only needs outbound access (default allows all outbound).

```powershell
pip install boto3

$env:LAUNCH_BACKEND       = "ecs"
$env:SQS_QUEUE_URL        = "https://sqs.us-east-1.amazonaws.com/598886663176/isteam-object-detection-jobs"
$env:MAX_CONCURRENCY      = "3"
$env:AWS_DEFAULT_REGION   = "us-east-1"

$env:ECS_CLUSTER          = "isteam-object-detection-cluster"
$env:ECS_TASK_DEFINITION  = "isteam-object-detection-worker"
$env:ECS_CONTAINER_NAME   = "worker"
$env:ECS_LAUNCH_TYPE      = "FARGATE"
$env:ECS_SUBNETS          = "subnet-aaaa,subnet-bbbb"
$env:ECS_SECURITY_GROUPS  = "sg-cccc"
$env:ECS_ASSIGN_PUBLIC_IP = "ENABLED"   # needed so the task can reach S3/callback/SerpApi in a public subnet

# forwarded to each worker task (defaults if a message omits them):
$env:CALLBACK_URL         = "https://.../callback"
$env:OUTPUT_BUCKET        = "isteam-video-output"

python dispatcher\dispatcher.py
```

The dispatcher needs IAM permissions (attach to the EC2 instance profile or your
CLI user): `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`
on the queue; `ecs:RunTask`, `ecs:DescribeTasks`; and `iam:PassRole` for the two
task roles.

> `ECS_ASSIGN_PUBLIC_IP=ENABLED` + public subnets is the simplest networking. In
> private subnets you'd instead add a NAT gateway (and/or S3 gateway endpoint) so
> tasks can reach S3, the callback, and SerpApi.

---

## 6. Test end to end

1. Make sure the dispatcher is running (step 5) and printing
   `backend=ecs max_concurrency=3 queue=...`.
2. Upload a video to `s3://isteam-video-uploader/uploads/`.
3. Watch it flow:
   - **enqueue Lambda** logs `[enqueue] queued job ...` (CloudWatch).
   - **dispatcher** prints `ecs launched job ... task arn:...`.
   - **ECS** → Cluster → **Tasks** shows a running `worker` task.
   - **Worker logs**: CloudWatch `/ecs/isteam-object-detection-worker` shows
     `ingesting... sampling frames... detecting... writing detections... uploading...`.
   - **Callback**: CloudWatch
     `/aws/lambda/isteam-object-detection-process-callback-lambda` shows the
     completed payload with the `detections_*_s3_uri` links.
   - **Output bucket** contains `detections/<suffix>/detections_<suffix>.json`
     and `.vtt`.
4. **Concurrency check**: upload 5 videos quickly. Confirm ECS never shows more
   than **3** running `worker` tasks at once, and that a 4th starts only after
   one of the first three stops.

---

## Troubleshooting

- **Task stops immediately / `CannotPullContainerError`**: execution role missing
  ECR pull perms, or the image URI/tag is wrong. Check step 2a and the URI.
- **Worker log: AccessDenied on S3**: task role missing `s3:GetObject`
  (input) or `s3:PutObject` (output). See step 2b.
- **Dispatcher: `AccessDeniedException` on RunTask / PassRole**: add `ecs:RunTask`
  and `iam:PassRole` (for both task roles) to whatever identity runs the
  dispatcher.
- **Task can't reach S3/callback (times out)**: networking. Use public subnets
  with `ECS_ASSIGN_PUBLIC_IP=ENABLED`, or add a NAT/endpoint for private subnets.
- **More than 3 tasks running**: only the dispatcher should launch tasks — make
  sure you didn't also attach the queue as an ECS/Lambda event source elsewhere.
- **SerpApi errors / empty recommendations**: set `SKIP_MATCHING=true` on the
  task definition to validate detection first, then wire the SerpApi secret.

---

## Alternative (no dispatcher): ECS Service with desiredCount=3

If you'd rather not run a dispatcher process, run the worker image as an ECS
**Service** with `desiredCount = 3`, each task started with `worker.py --poll`
(set `SQS_QUEUE_URL` on the task). ECS keeps exactly 3 pollers alive and restarts
any that exit — same 3-at-a-time effect, no separate launcher. Trade-off: the 3
tasks are always on (each pulls the next message when free), vs. the dispatcher
which starts a fresh task per video and scales to zero when the queue is empty.

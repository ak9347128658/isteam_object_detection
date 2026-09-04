# Step 2 — SQS queue + enqueue the job (replace "log only")

Step 1 proved the S3 upload triggers the Lambda and it can log the event + a
presigned URL. Now we make it useful: create an **SQS queue** and change the
Lambda so each upload **puts a job message on the queue** instead of just
logging. A worker (next steps) will consume those messages.

```
[upload video] -> S3 (isteam-video-uploader) --ObjectCreated--> enqueue Lambda
                                                                     |
                                                            SendMessage
                                                                     v
                                                   SQS: isteam-object-detection-jobs
```

Prereqs done:
- `isteam-video-uploader` bucket + trigger Lambda
  (`isteam-object-detection-process-enqueue-lambda`) working (Step 1).
- Callback Lambda `isteam-object-detection-process-callback-lambda` created, and
  ideally an API Gateway route in front of it — you'll need its URL below.

---

## 1. Create the dead-letter queue (DLQ) first

A DLQ catches messages that repeatedly fail so they don't loop forever.

1. AWS Console → search **SQS** → **Create queue**.
2. **Type**: **Standard**.
3. **Name**: `isteam-object-detection-jobs-dlq`.
4. Leave defaults → **Create queue**.

---

## 2. Create the main queue

1. SQS → **Create queue**.
2. **Type**: **Standard**.
3. **Name**: `isteam-object-detection-jobs`.
4. **Visibility timeout**: `1800` seconds (30 min) — longer than a video takes to
   process, so a message isn't redelivered while a worker is still on it.
5. **Message retention period**: leave `4 days` (or raise if you want a bigger
   backlog buffer).
6. Scroll to **Dead-letter queue** → **Enabled**:
   - **Dead-letter queue**: choose `isteam-object-detection-jobs-dlq`.
   - **Maximum receives**: `3` (after 3 failed attempts, move to DLQ).
7. **Create queue**.
8. Open the queue and **copy its URL** (looks like
   `https://sqs.us-east-1.amazonaws.com/598886663176/isteam-object-detection-jobs`).
   You'll paste it into the Lambda env below.

---

## 3. Give the enqueue Lambda permission to send to SQS

1. Lambda → `isteam-object-detection-process-enqueue-lambda` →
   **Configuration** → **Permissions** → click the **Execution role**.
2. In IAM: **Add permissions** → **Create inline policy** → **JSON** → paste
   (replace the ARN with your queue's ARN, shown on the queue's detail page):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "sqs:SendMessage",
         "Resource": "arn:aws:sqs:us-east-1:598886663176:isteam-object-detection-jobs"
       }
     ]
   }
   ```

3. **Next** → name `send-to-detection-jobs` → **Create policy**.

The Lambda already has `s3:GetObject` from Step 1 (needed to sign the presigned
URL), so no other permission changes are required.

---

## 4. Create the output bucket `isteam-video-output` (PRIVATE)

This bucket holds `detections.json` / `detections.vtt` (and product crops). Keep
it **private** — same reasoning as the input bucket. The worker returns
**presigned URLs** for these files in its callback, so nothing here needs to be
public. Making it public would expose every detection result to anyone.

1. AWS Console → **S3** → **Create bucket**.
2. **Bucket name**: `isteam-video-output`.
3. **Region**: same region as everything else (e.g. `us-east-1`).
4. **Block Public Access**: leave **all blocked** (checked) — private bucket.
5. Leave the rest as defaults → **Create bucket**.

> Why not public? The worker uploads results here, then generates time-limited
> presigned links and sends them in the callback. Downstream opens those links
> to fetch the files. The bucket staying private means only holders of a valid
> (short-lived) link — or your credentialed services — can read the results.

> Note: this is the bucket the **worker** writes to. This enqueue Lambda does
> not write to it; it only passes the name along in the job message so the
> worker knows where to put outputs.

---

## 5. Set the Lambda environment variables

1. Lambda function → **Configuration** → **Environment variables** → **Edit** →
   add:

   | Key | Value |
   |---|---|
   | `QUEUE_URL` | your queue URL from step 2.8 |
   | `CALLBACK_URL` | your callback endpoint (API Gateway route or Function URL) |
   | `OUTPUT_BUCKET` | `isteam-video-output` |
   | `SKIP_MATCHING` | `false` (or `true` to skip Google Lens for now) |

2. (Optional) `ALLOWED_EXTS` = `mp4,mov,mkv,webm,avi,m4v`,
   `PRESIGN_TTL_SECONDS` = `604800`.
3. **Save**.

---

## 6. Replace the Lambda code with the enqueue version

1. Lambda → **Code** tab → open `lambda_function.py`.
2. Replace its contents with the code from this repo:
   **`lambda/enqueue.py`** (copy the whole file).
3. The entry function is `lambda_handler`, matching the default handler
   `lambda_function.lambda_handler` — no handler change needed.
4. **Deploy**.

What the new code sends to SQS per uploaded video:

```json
{
  "job_id": "9c1f...",
  "s3_uri": "s3://isteam-video-uploader/uploads/clip.mp4",
  "presigned_url": "https://isteam-video-uploader.s3.amazonaws.com/uploads/clip.mp4?AWSAccessKeyId=...&Signature=...&Expires=...",
  "bucket": "isteam-video-uploader",
  "key": "uploads/clip.mp4",
  "callback_url": "https://.../callback",
  "output_bucket": "isteam-video-output",
  "skip_matching": false
}
```

It also skips non-video keys (via `ALLOWED_EXTS`) and URL-decodes the key, so an
upload named `12+Th+FAIL...mp4` becomes `12 Th FAIL...mp4` — same handling you
saw work in Step 1.

---

## 7. Test

1. S3 → `isteam-video-uploader/uploads/` → **Upload** a small `.mp4`.
2. Check the Lambda's CloudWatch logs — you should now see:

   ```
   [enqueue] queued job 9c1f... for s3://isteam-video-uploader/uploads/clip.mp4
   ```

3. Confirm the message reached the queue:
   - SQS → `isteam-object-detection-jobs` → **Send and receive messages** →
     **Poll for messages**.
   - You should see one message. Click it to view the JSON body (the job above).
   - **Important:** polling here *receives* the message. If you don't want to
     consume it, don't delete it — it returns to the queue after the visibility
     timeout. For a clean test it's fine to just view it.

---

## Troubleshooting

- **AccessDenied on SendMessage** (in logs): the inline SQS policy is missing or
  the ARN is wrong. Recheck step 3 (region + account id + queue name).
- **KeyError: 'QUEUE_URL' / 'CALLBACK_URL' / 'OUTPUT_BUCKET'**: an env var isn't
  set — see step 4.
- **Nothing queued, log says "skipping non-video key"**: the upload's extension
  isn't in `ALLOWED_EXTS`. Upload an mp4 or adjust the list.
- **No message in the queue but log shows "queued"**: you're polling a different
  queue or region. Match the console region and queue name.

---

## Next step

Step 3: stand up the consumer — either the **dispatcher** (`dispatcher/`) in
docker mode capped at 3 concurrent workers, or an ECS setup — pointed at
`isteam-object-detection-jobs`, so a queued job actually launches the ECR worker
image, produces `detections.json`/`.vtt`, and calls back.

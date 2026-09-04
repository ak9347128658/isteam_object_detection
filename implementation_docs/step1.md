# Step 1 — Input bucket + S3 event → trigger Lambda (log only)

Goal of this step: create the video-upload S3 bucket
(`isteam-video-uploader`) as a **private** bucket, create a Lambda that (for now)
**only console-logs** the S3 event **and a publicly reachable presigned URL** for
the uploaded object, and wire an S3 **ObjectCreated** notification so uploading a
video fires the Lambda. You verify success in CloudWatch.

This is the first live piece of Architecture 1:

```
[upload video] -> S3 (isteam-video-uploader, PRIVATE) --ObjectCreated--> Lambda
                                                       (logs event + presigned URL)
```

### Private bucket, but a "public" (reachable) URL

The bucket stays **private** — Block Public Access stays ON. Downstream (the
worker container) still needs a URL it can actually fetch the video from, so the
Lambda generates a **presigned URL**: a time-limited HTTPS link signed with the
Lambda's credentials. Anyone with the link can download the object until it
expires, without the bucket being public. This is the recommended way to expose
a private object.

Later steps replace the "log only" Lambda body with the real enqueue-to-SQS code
(`lambda/enqueue.py`), but for now we just prove the trigger works end to end.

Prereqs already done:
- Callback Lambda created (`isteam-object-detection-process-callback-lambda`).
- Worker Docker image pushed to ECR.

---

## 1. Create the S3 bucket `isteam-video-uploader`

1. AWS Console → search **S3** → open it.
2. Click **Create bucket**.
3. **Bucket name**: `isteam-video-uploader`
   (bucket names are globally unique; if taken, add a suffix like
   `isteam-video-uploader-<your-initials>` and use that everywhere below).
4. **AWS Region**: pick your working region, e.g. **US East (N. Virginia)
   us-east-1**. Keep this region consistent across all resources.
5. **Block Public Access**: leave **all blocked** (checked). Uploads are private;
   the pipeline uses credentials/presigned URLs, so no public access is needed.
6. Leave the rest as defaults → **Create bucket**.
7. (Optional but recommended) Inside the bucket, create a folder named
   `uploads/` (Console → the bucket → **Create folder** → `uploads`). We'll
   filter events to this prefix so only real uploads trigger the Lambda.

---

## 2. Create the trigger Lambda (console log only)

1. AWS Console → **Lambda** → **Create function**.
2. **Author from scratch**.
3. **Function name**: `isteam-object-detection-process-enqueue-lambda`.
4. **Runtime**: **Python 3.12**.
5. **Architecture**: `x86_64`.
6. **Create function**.

### Paste the log-only code

1. On the function page → **Code** tab → open `lambda_function.py`.
2. Replace its contents with the code below → **Deploy**.

```python
import json
import os
import urllib.parse

import boto3

# Presigned URL lifetime in seconds (default 7 days = 604800).
PRESIGN_TTL = int(os.getenv("PRESIGN_TTL_SECONDS", "604800"))

s3 = boto3.client("s3")


def lambda_handler(event, context):
    # For now this Lambda ONLY logs the S3 event + a presigned (publicly
    # reachable) URL for each uploaded object, so we can confirm the trigger
    # fires. Later it will enqueue a job to SQS (see lambda/enqueue.py).
    print("=== S3 event received ===")
    print(json.dumps(event))

    for record in event.get("Records", []):
        s3rec = record.get("s3", {})
        bucket = s3rec.get("bucket", {}).get("name")
        # S3 event keys are URL-encoded (spaces -> '+', etc.). Decode them.
        raw_key = s3rec.get("object", {}).get("key", "")
        key = urllib.parse.unquote_plus(raw_key)
        size = s3rec.get("object", {}).get("size")
        event_name = record.get("eventName")
        region = record.get("awsRegion")

        s3_uri = f"s3://{bucket}/{key}"

        # Generate a time-limited HTTPS URL for the PRIVATE object. The bucket
        # stays private; this signed link is what downstream can fetch.
        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=PRESIGN_TTL,
        )

        print(f"eventName={event_name} region={region} bucket={bucket} "
              f"key={key} size={size}")
        print(f"s3_uri={s3_uri}")
        print(f"presigned_url={presigned_url}")

    print("=== end S3 event ===")
    return {"statusCode": 200, "recordCount": len(event.get("Records", []))}
```

The default handler `lambda_function.lambda_handler` already matches this file,
so no handler change is needed. `boto3` is preinstalled in the Lambda Python
runtime — no packaging needed.

### Bump the timeout (optional)

**Configuration** → **General configuration** → **Edit** → Timeout `10 sec`,
Memory `128 MB` → **Save**.

### Grant the Lambda permission to read the object (needed for presigning)

A presigned GET URL is signed with the Lambda's own credentials, so the Lambda
role must be allowed `s3:GetObject` on this bucket — otherwise the link exists
but returns AccessDenied when opened.

1. Lambda function page → **Configuration** → **Permissions**.
2. Click the **Execution role** name (opens the role in IAM).
3. **Add permissions** → **Create inline policy** → **JSON** tab → paste:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "s3:GetObject",
         "Resource": "arn:aws:s3:::isteam-video-uploader/*"
       }
     ]
   }
   ```

4. **Next** → name it `read-isteam-video-uploader` → **Create policy**.

> The object stays private. This only lets the Lambda *sign* URLs for it; the
> bucket's Block Public Access remains ON.

---

## 3. Add the S3 trigger

You can do this from either side. The Lambda side is simplest.

1. On the Lambda function page, click **+ Add trigger**.
2. **Select a source**: **S3**.
3. **Bucket**: `isteam-video-uploader`.
4. **Event types**: **All object create events**
   (`s3:ObjectCreated:*`).
5. **Prefix** (optional): `uploads/` — only fire for objects under `uploads/`.
6. **Suffix** (optional): `.mp4` — only fire for mp4 uploads. (Leave blank to
   accept any file while testing.)
7. Check the acknowledgment box about recursive invocation (safe here — the
   Lambda writes nothing back to this bucket).
8. **Add**.

This automatically grants S3 permission to invoke the Lambda (adds a resource
policy). No manual IAM change is needed for the trigger itself.

> If you prefer the S3 side: bucket → **Properties** → **Event notifications** →
> **Create event notification** → name it, set prefix/suffix, choose
> **All object create events**, **Destination = Lambda function** →
> `isteam-object-detection-process-enqueue-lambda` → **Save changes**.

---

## 4. Test it

1. Go to **S3** → `isteam-video-uploader` → `uploads/` folder.
2. **Upload** any small `.mp4` (or any file if you left the suffix blank).
3. Wait a few seconds.

---

## 5. Confirm in CloudWatch

1. Lambda function page → **Monitor** tab → **View CloudWatch logs**.
   (Log group: `/aws/lambda/isteam-object-detection-process-enqueue-lambda`.)
2. Open the newest **Log stream**. You should see:

```
=== S3 event received ===
{ ... full S3 event JSON ... }
eventName=ObjectCreated:Put region=us-east-1 bucket=isteam-video-uploader key=uploads/your-file.mp4 size=1048576
s3_uri=s3://isteam-video-uploader/uploads/your-file.mp4
presigned_url=https://isteam-video-uploader.s3.amazonaws.com/uploads/your-file.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=604800&X-Amz-Signature=...
=== end S3 event ===
```

If you see those lines, the trigger works. Copy the `presigned_url` into a
browser — it should download the video even though the bucket is private. Step 1
is complete.

---

## Troubleshooting

- **No log stream appears**: the event didn't reach the Lambda. Recheck the
  trigger's bucket, prefix, and suffix. If you set suffix `.mp4`, uploading a
  `.txt` won't fire it.
- **AccessDenied / not invoked**: re-add the trigger from the Lambda page so the
  invoke permission (resource policy) is created automatically.
- **Wrong region**: the bucket, Lambda, and log group must be in the same region
  you're viewing in the console (top-right region selector).
- **Uploaded to bucket root, not `uploads/`**: if you set a `uploads/` prefix,
  put the file under that folder.
- **Presigned URL opens but returns AccessDenied**: the Lambda role is missing
  `s3:GetObject`. Add the inline policy from step 2. (The bucket being private is
  fine — the signature is what grants access.)
- **Presigned URL says "Request has expired"**: it outlived `PRESIGN_TTL_SECONDS`
  (default 7 days). Re-upload or raise the TTL env var.

---

## Next step

Once logging is confirmed, Step 2 will:
- create the SQS queue (+ dead-letter queue), and
- replace this Lambda's body with the real enqueue code (`lambda/enqueue.py`)
  so each upload sends a job message to SQS.

# callback — simple result-logger Lambda

A tiny AWS Lambda the worker POSTs to when a video finishes. It does **one
thing: log the payload to CloudWatch** so you can watch results come in. It
returns HTTP 200 so the worker considers the callback delivered.

- `handler.py` — the function. Entry point is `lambda_handler` (paste into the
  console's `lambda_function.py`, so the handler stays the default
  `lambda_function.lambda_handler`).
- No dependencies — pure standard library, so no packaging/zip build needed.

The worker's `CALLBACK_URL` should be set to this Lambda's **Function URL**.

---

## Create it in the AWS Console (GUI), step by step

### 1. Create the function
1. Open the **AWS Console** → search **Lambda** → open it.
2. Click **Create function**.
3. Choose **Author from scratch**.
4. **Function name**: `isteam-object-detection-process-callback-lambda`.
5. **Runtime**: **Python 3.12** (3.11 is fine too).
6. **Architecture**: leave `x86_64`.
7. Click **Create function**.

### 2. Paste the code
1. On the function page, scroll to the **Code** tab (the inline editor).
2. Open the file `lambda_function.py` in the editor and delete its contents.
3. Copy everything from this repo's `callback/handler.py` and paste it in.
4. Our entry function is named `lambda_handler`, which matches Lambda's default
   handler `lambda_function.lambda_handler` — so **no handler change is needed**.
5. Click **Deploy** (the orange button) to save the code.

### 3. Give it a public HTTPS URL (Function URL)
1. Go to the **Configuration** tab → **Function URL** (left menu).
2. Click **Create function URL**.
3. **Auth type**: choose **NONE** (the worker just POSTs JSON; there's no secret
   data here, only logs). For a locked-down setup pick **AWS_IAM** instead and
   sign the request — NONE is simplest to start.
4. (Optional) Expand **Configure cross-origin resource sharing (CORS)** and
   leave defaults.
5. Click **Save**. Copy the **Function URL** shown (looks like
   `https://abc123....lambda-url.us-east-1.on.aws/`).

### 4. Point the worker at it
Set the worker's `CALLBACK_URL` to that Function URL. For a local run:

```powershell
$env:CALLBACK_URL = "https://abc123....lambda-url.us-east-1.on.aws/"
```

For the dispatcher, set `CALLBACK_URL` in its environment (it forwards it to
every worker), or the Lambda `enqueue.py` puts it in each SQS message.

### 5. Bump the timeout a little (optional but recommended)
1. **Configuration** tab → **General configuration** → **Edit**.
2. Set **Timeout** to `10 sec`, **Memory** `128 MB` is plenty.
3. **Save**.

---

## See the logs in CloudWatch

Every callback is logged. To view them:

1. In the Lambda function page, open the **Monitor** tab → **View CloudWatch
   logs** (opens the log group
   `/aws/lambda/isteam-object-detection-process-callback-lambda`).
2. Or go to **CloudWatch** → **Log groups** →
   `/aws/lambda/isteam-object-detection-process-callback-lambda` → open the
   newest **Log stream**.

You'll see entries like:

```
=== detection callback received ===
method=POST source_ip=…
  job_id = 3f9c2a1b…
  status = completed
  video_s3_uri = s3://isteam-video-input/uploads/clip.mp4
  product_count = 3
  detections_json_s3_uri = s3://isteam-video-output/detections/clip-3f9c2a1b-…/detections.json
  detections_vtt_s3_uri  = s3://isteam-video-output/detections/clip-3f9c2a1b-…/detections.vtt
  finished_at = 2026-09-03T10:02:00Z
raw_payload={"job_id": "3f9c2a1b…", "status": "completed", …}
=== end callback ===
```

---

## Quick test without the worker

On the Lambda page, use the **Test** tab with this event to see a log line:

```json
{
  "body": "{\"job_id\":\"test123\",\"status\":\"completed\",\"video_s3_uri\":\"s3://in/clip.mp4\",\"product_count\":2}"
}
```

Click **Test**, then check the CloudWatch log group for the entry.

Or hit the Function URL from your machine:

```powershell
curl -Method POST "https://abc123....lambda-url.us-east-1.on.aws/" `
  -Body '{"job_id":"test123","status":"completed","product_count":2}' `
  -ContentType "application/json"
```

---

## Test with Postman (via API Gateway)

The handler already understands API Gateway events (it reads `event["body"]`
and handles base64), so it works behind API Gateway with no code changes.

**Request**
- **Method:** `POST`
- **URL:** your API Gateway invoke URL + route, e.g.
  - HTTP API (`$default` stage): `https://<api-id>.execute-api.<region>.amazonaws.com/callback`
  - REST API (`prod` stage): `https://<api-id>.execute-api.<region>.amazonaws.com/prod/callback`
- **Headers:** `Content-Type: application/json`
- **Body → raw → JSON:**

```json
{
  "job_id": "test-123",
  "status": "completed",
  "video_s3_uri": "s3://isteam-video-input/uploads/clip.mp4",
  "unique_suffix": "clip-test123-20260903T100200Z",
  "product_count": 3,
  "detections_json_s3_uri": "s3://isteam-video-output/detections/clip-test123-20260903T100200Z/detections.json",
  "detections_vtt_s3_uri": "s3://isteam-video-output/detections/clip-test123-20260903T100200Z/detections.vtt",
  "detections_json_url": "https://example-presigned-url/detections.json",
  "detections_vtt_url": "https://example-presigned-url/detections.vtt",
  "finished_at": "2026-09-03T10:02:00Z"
}
```

**Expected response:** `200 OK`

```json
{ "ok": true, "job_id": "test-123" }
```

Then confirm the entry in CloudWatch log group
`/aws/lambda/isteam-object-detection-process-callback-lambda`.

### Import the ready-made collection
Import `callback/postman_collection.json` into Postman. It has two requests
(**completed** and **failed**) and two collection variables:

- `base_url` → set to `https://<api-id>.execute-api.<region>.amazonaws.com/<stage>`
- `route` → set to your resource path (default `/callback`)

Edit those two variables (Collection → Variables), then **Send**.

> API Gateway integration tip: use **Lambda proxy integration** (HTTP API's
> default, or "Use Lambda Proxy integration" on REST API). That passes the raw
> body through as `event["body"]`, which is exactly what this handler expects.

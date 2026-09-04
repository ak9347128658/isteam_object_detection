# Callback API — examples

Concrete request/response examples for the callback Lambda
(`isteam-object-detection-process-callback-lambda`) behind API Gateway.

The Lambda simply returns `200` and logs the payload to CloudWatch log group
`/aws/lambda/isteam-object-detection-process-callback-lambda`. The worker POSTs
here when a video finishes.

---

## Endpoint

```
POST https://f9u39iuej4.execute-api.us-east-1.amazonaws.com/default/isteam-object-detection-process-callback-lambda/callback
Content-Type: application/json
```

- HTTP API (`$default` stage): `https://f9u39iuej4.execute-api.us-east-1.amazonaws.com/default/isteam-object-detection-process-callback-lambda/callback`
- REST API (`prod` stage):     `https://f9u39iuej4.execute-api.us-east-1.amazonaws.com/default/isteam-object-detection-process-callback-lambda/prod/callback`

Replace `a1b2c3d4e5` / `us-east-1` / stage with your own values.

---

## Example 1 — successful detection (raw HTTP)

Request:

```http
POST /prod/callback HTTP/1.1
Host: a1b2c3d4e5.execute-api.us-east-1.amazonaws.com
Content-Type: application/json

{
  "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33",
  "status": "completed",
  "video_s3_uri": "s3://isteam-video-input/uploads/summer-lookbook.mp4",
  "unique_suffix": "summer-lookbook-3f9c2a1b-20260903T100200Z",
  "product_count": 4,
  "detections_json_s3_uri": "s3://isteam-video-output/detections/summer-lookbook-3f9c2a1b-20260903T100200Z/detections.json",
  "detections_vtt_s3_uri": "s3://isteam-video-output/detections/summer-lookbook-3f9c2a1b-20260903T100200Z/detections.vtt",
  "detections_json_url": "https://isteam-video-output.s3.us-east-1.amazonaws.com/detections/summer-lookbook-3f9c2a1b-20260903T100200Z/detections.json?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=604800&X-Amz-Signature=abcd1234",
  "detections_vtt_url": "https://isteam-video-output.s3.us-east-1.amazonaws.com/detections/summer-lookbook-3f9c2a1b-20260903T100200Z/detections.vtt?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Expires=604800&X-Amz-Signature=efgh5678",
  "finished_at": "2026-09-03T10:02:00Z"
}
```

Response:

```http
HTTP/1.1 200 OK
Content-Type: application/json

{ "ok": true, "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33" }
```

---

## Example 2 — successful detection (curl / PowerShell)

```powershell
curl.exe -X POST "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com/prod/callback" `
  -H "Content-Type: application/json" `
  -d '{
        "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33",
        "status": "completed",
        "video_s3_uri": "s3://isteam-video-input/uploads/summer-lookbook.mp4",
        "unique_suffix": "summer-lookbook-3f9c2a1b-20260903T100200Z",
        "product_count": 4,
        "detections_json_s3_uri": "s3://isteam-video-output/detections/summer-lookbook-3f9c2a1b-20260903T100200Z/detections.json",
        "detections_vtt_s3_uri": "s3://isteam-video-output/detections/summer-lookbook-3f9c2a1b-20260903T100200Z/detections.vtt",
        "finished_at": "2026-09-03T10:02:00Z"
      }'
```

Response:

```json
{ "ok": true, "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33" }
```

---

## Example 3 — bash curl (Linux/macOS)

```bash
curl -X POST "https://a1b2c3d4e5.execute-api.us-east-1.amazonaws.com/prod/callback" \
  -H "Content-Type: application/json" \
  -d '{
        "job_id": "8a7b6c5d4e3f2109",
        "status": "completed",
        "video_s3_uri": "s3://isteam-video-input/uploads/watch-review.mp4",
        "unique_suffix": "watch-review-8a7b6c5d-20260903T113000Z",
        "product_count": 2,
        "detections_json_s3_uri": "s3://isteam-video-output/detections/watch-review-8a7b6c5d-20260903T113000Z/detections.json",
        "detections_vtt_s3_uri": "s3://isteam-video-output/detections/watch-review-8a7b6c5d-20260903T113000Z/detections.vtt",
        "finished_at": "2026-09-03T11:30:00Z"
      }'
```

---

## Example 4 — failed job

Request:

```http
POST /prod/callback HTTP/1.1
Host: a1b2c3d4e5.execute-api.us-east-1.amazonaws.com
Content-Type: application/json

{
  "job_id": "c1d2e3f4a5b6c7d8",
  "status": "failed",
  "video_s3_uri": "s3://isteam-video-input/uploads/corrupt-clip.mp4",
  "error": "No frames sampled - check the video / time window settings.",
  "finished_at": "2026-09-03T12:15:00Z"
}
```

Response:

```json
{ "ok": true, "job_id": "c1d2e3f4a5b6c7d8" }
```

The Lambda always returns `200` (even for `status: failed`) — its only job is to
log the callback. The `failed` state lives inside the payload.

---

## What lands in CloudWatch

For Example 1, the log stream in
`/aws/lambda/isteam-object-detection-process-callback-lambda` shows:

```
=== detection callback received ===
method=POST source_ip=54.221.10.32
  job_id = 3f9c2a1b7d4e4f8a9c10ee55aa22bb33
  status = completed
  video_s3_uri = s3://isteam-video-input/uploads/summer-lookbook.mp4
  unique_suffix = summer-lookbook-3f9c2a1b-20260903T100200Z
  product_count = 4
  detections_json_s3_uri = s3://isteam-video-output/detections/summer-lookbook-3f9c2a1b-20260903T100200Z/detections.json
  detections_vtt_s3_uri = s3://isteam-video-output/detections/summer-lookbook-3f9c2a1b-20260903T100200Z/detections.vtt
  detections_json_url = https://isteam-video-output.s3.us-east-1.amazonaws.com/detections/...
  detections_vtt_url = https://isteam-video-output.s3.us-east-1.amazonaws.com/detections/...
  finished_at = 2026-09-03T10:02:00Z
raw_payload={"job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33", "status": "completed", ...}
=== end callback ===
```

---

## Notes

- Use **Lambda proxy integration** on API Gateway so the raw JSON body is passed
  through as `event["body"]` (what this handler reads). HTTP API does this by
  default; on REST API tick "Use Lambda Proxy integration".
- `Content-Type: application/json` is expected. The handler also tolerates a
  base64-encoded body (API Gateway sets `isBase64Encoded`) and decodes it.
- Auth: examples assume an open route for testing. For production put an API key,
  IAM auth, or a shared-secret header in front and have the worker send it.

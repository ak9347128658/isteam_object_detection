# Callback API — examples

Concrete request/response examples for the callback Lambda
(`isteam-object-detection-process-callback-lambda`) behind API Gateway.

The Lambda simply returns `200` and logs the payload to CloudWatch log group
`/aws/lambda/isteam-object-detection-process-callback-lambda`. The worker POSTs
here when a video finishes.

> **No storage (Architecture 2):** the worker keeps nothing. It POSTs the
> `detections.json` and `detections.vtt` **inline** in the callback body — as a
> JSON object (`detections_json`) and a WebVTT string (`detections_vtt`) — tagged
> with the caller's `video_id`. There are no S3 links.

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
  "video_id": "summer-lookbook-42",
  "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33",
  "status": "completed",
  "product_count": 4,
  "detections_json": {
    "video": { "id": "summer-lookbook-42", "fps": 30.0, "duration_seconds": 42.0, "width": 1920, "height": 1080 },
    "product_count": 4,
    "products": [
      {
        "product_id": "p0000",
        "label": "sneaker",
        "first_seen": 1.0,
        "last_seen": 8.0,
        "occurrences": [{ "timestamp": 1.0, "bbox": [10, 20, 110, 220], "confidence": 0.82 }],
        "recommendations": [
          { "title": "Retro Runner", "url": "https://amazon.com/...", "source": "amazon.com",
            "price": "$79", "thumbnail": "https://...", "score": 0.95, "backend": "google_lens" }
        ]
      }
    ]
  },
  "detections_vtt": "WEBVTT\n\n00:00:01.000 --> 00:00:08.000\n[p0000] sneaker -> Retro Runner ($79) https://amazon.com/...\n",
  "finished_at": "2026-09-07T10:02:00Z"
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
        "video_id": "summer-lookbook-42",
        "job_id": "3f9c2a1b7d4e4f8a9c10ee55aa22bb33",
        "status": "completed",
        "product_count": 4,
        "detections_json": { "video": { "id": "summer-lookbook-42" }, "products": [] },
        "detections_vtt": "WEBVTT\n\n00:00:01.000 --> 00:00:08.000\n[p0000] sneaker\n",
        "finished_at": "2026-09-07T10:02:00Z"
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
        "video_id": "watch-review-7",
        "job_id": "8a7b6c5d4e3f2109",
        "status": "completed",
        "product_count": 2,
        "detections_json": { "video": { "id": "watch-review-7" }, "products": [] },
        "detections_vtt": "WEBVTT\n\n00:00:03.000 --> 00:00:09.000\n[p0000] watch\n",
        "finished_at": "2026-09-07T11:30:00Z"
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
  "video_id": "corrupt-clip-9",
  "job_id": "c1d2e3f4a5b6c7d8",
  "status": "failed",
  "error": "could not download video_url (HTTP 404)",
  "finished_at": "2026-09-07T12:15:00Z"
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
  video_id = summer-lookbook-42
  job_id = 3f9c2a1b7d4e4f8a9c10ee55aa22bb33
  status = completed
  product_count = 4
  finished_at = 2026-09-07T10:02:00Z
  detections_json = <inline object, products=4>
  detections_vtt = <inline WebVTT, 812 chars>
raw_payload={"video_id": "summer-lookbook-42", "job_id": "3f9c2a1b...", "status": "completed", ...}
=== end callback ===
```

The inline `detections_json` / `detections_vtt` are logged by size (not full
content) to keep the log readable; the full payload is still in `raw_payload`.

---

## Notes

- Use **Lambda proxy integration** on API Gateway so the raw JSON body is passed
  through as `event["body"]` (what this handler reads). HTTP API does this by
  default; on REST API tick "Use Lambda Proxy integration".
- `Content-Type: application/json` is expected. The handler also tolerates a
  base64-encoded body (API Gateway sets `isBase64Encoded`) and decodes it.
- **Payload size:** inline delivery means the body can be a few KB to low MB.
  API Gateway caps the request body at ~10 MB and Lambda at 6 MB (sync). For very
  large `.vtt` outputs, deliver via multipart or raise the threshold on the
  worker side.
- Auth: examples assume an open route for testing. For production put an API key,
  IAM auth, or a shared-secret header in front and have the worker send it.

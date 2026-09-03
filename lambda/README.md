# lambda/enqueue.py — S3 → SQS trigger

Tiny Lambda that fires on `s3:ObjectCreated:*` for the **input** bucket and
enqueues one detection job per uploaded video. It does no processing.

## Deploy

- Runtime: Python 3.11, handler `enqueue.handler`.
- Env vars: `QUEUE_URL`, `CALLBACK_URL`, `OUTPUT_BUCKET` (required);
  `SKIP_MATCHING`, `ALLOWED_EXTS` (optional).
- IAM: `sqs:SendMessage` on the queue.
- Trigger: add an S3 `ObjectCreated` notification on the input bucket
  (recommend a prefix filter such as `uploads/`).

## Message shape (what the worker consumes)

```json
{
  "job_id": "3f9c2a1b...",
  "s3_uri": "s3://isteam-video-input/uploads/clip.mp4",
  "bucket": "isteam-video-input",
  "key": "uploads/clip.mp4",
  "callback_url": "https://api.example.com/detections/callback",
  "output_bucket": "isteam-video-output",
  "skip_matching": false
}
```

The launcher (ECS `RunTask` or `worker.py --poll`) maps this to the container
env: `VIDEO_S3_URI=s3_uri`, `CALLBACK_URL=callback_url`, `JOB_ID=job_id`,
`OUTPUT_BUCKET=output_bucket`.

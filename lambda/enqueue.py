"""
enqueue.py — S3-triggered Lambda that puts a detection job on SQS.

Architecture 1, step 2-3:
    S3 (video uploaded)  --ObjectCreated-->  this Lambda  --SendMessage-->  SQS

This function does NO video processing (it must stay tiny and fast). It only
builds a job message and drops it on the queue. A downstream launcher (ECS
RunTask, or the worker in --poll mode) then runs one container per message with
the video S3 link injected as an environment variable.

Environment variables:
    QUEUE_URL        (required)  SQS queue to send jobs to.
    CALLBACK_URL     (required)  URL the worker will POST results to.
    OUTPUT_BUCKET    (required)  Bucket where detections.json/.vtt are stored.
    SKIP_MATCHING    (optional)  "true" to skip S3 crop upload + Google Lens.
    ALLOWED_EXTS     (optional)  Comma list, default "mp4,mov,mkv,webm,avi,m4v".

IAM: this Lambda needs sqs:SendMessage on QUEUE_URL and read on the input bucket
notification. No S3 read of the object body is required.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import uuid

import boto3

_sqs = boto3.client("sqs")

_DEFAULT_EXTS = "mp4,mov,mkv,webm,avi,m4v"


def _allowed(key: str) -> bool:
    exts = {
        e.strip().lower().lstrip(".")
        for e in os.getenv("ALLOWED_EXTS", _DEFAULT_EXTS).split(",")
        if e.strip()
    }
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    return ext in exts


def _job_message(bucket: str, key: str) -> dict:
    return {
        "job_id": uuid.uuid4().hex,
        "s3_uri": f"s3://{bucket}/{key}",
        "bucket": bucket,
        "key": key,
        "callback_url": os.environ["CALLBACK_URL"],
        "output_bucket": os.environ["OUTPUT_BUCKET"],
        "skip_matching": os.getenv("SKIP_MATCHING", "false").strip().lower()
        in ("1", "true", "yes", "on"),
    }


def handler(event, context):
    """Entry point. Handles standard S3 ObjectCreated notifications."""
    queue_url = os.environ["QUEUE_URL"]
    enqueued = []
    skipped = []

    for record in event.get("Records", []):
        s3 = record.get("s3", {})
        bucket = s3.get("bucket", {}).get("name")
        raw_key = s3.get("object", {}).get("key", "")
        # S3 event keys are URL-encoded (spaces -> '+', etc.).
        key = urllib.parse.unquote_plus(raw_key)
        if not bucket or not key:
            continue
        if not _allowed(key):
            print(f"[enqueue] skipping non-video key: {key}")
            skipped.append(key)
            continue

        msg = _job_message(bucket, key)
        _sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(msg))
        print(f"[enqueue] queued job {msg['job_id']} for s3://{bucket}/{key}")
        enqueued.append(msg["job_id"])

    return {
        "statusCode": 200,
        "enqueued": enqueued,
        "skipped": skipped,
    }

"""
handler.py — simple callback receiver Lambda.

The worker container POSTs its result here when a video finishes. This function
does one job: LOG the payload to CloudWatch so you can see every callback.

Exposed via a Lambda Function URL (a plain HTTPS endpoint), so the worker's
CALLBACK_URL points straight at it — no API Gateway needed.

It always returns HTTP 200 so the worker treats delivery as successful.
Everything it receives shows up in the CloudWatch log group:
    /aws/lambda/isteam-object-detection-process-callback-lambda
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _extract_body(event: dict) -> tuple[str, dict]:
    """Return (raw_body_string, parsed_dict) from a Function URL / API GW event."""
    raw = event.get("body")

    # Function URLs can base64-encode the body.
    if raw is not None and event.get("isBase64Encoded"):
        import base64
        try:
            raw = base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            pass

    # Direct test invokes (no HTTP wrapper) pass the JSON as the event itself.
    if raw is None:
        return json.dumps(event), event if isinstance(event, dict) else {}

    if isinstance(raw, (dict, list)):
        return json.dumps(raw), (raw if isinstance(raw, dict) else {})

    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {}
    return raw, (parsed if isinstance(parsed, dict) else {})


def lambda_handler(event, context):
    raw_body, body = _extract_body(event)

    # HTTP context (present for Function URL / API Gateway invocations).
    http = (event.get("requestContext") or {}).get("http", {})
    method = http.get("method", "?")
    source_ip = http.get("sourceIp", "?")

    logger.info("=== detection callback received ===")
    logger.info("method=%s source_ip=%s", method, source_ip)

    # Log the well-known fields the worker sends, when present.
    for key in (
        "job_id",
        "status",
        "video_s3_uri",
        "unique_suffix",
        "product_count",
        "detections_json_s3_uri",
        "detections_vtt_s3_uri",
        "detections_json_url",
        "detections_vtt_url",
        "error",
        "finished_at",
    ):
        if key in body:
            logger.info("  %s = %s", key, body[key])

    # And always log the full raw payload so nothing is missed.
    logger.info("raw_payload=%s", raw_body)
    logger.info("=== end callback ===")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"ok": True, "job_id": body.get("job_id")}),
    }

# 04 — Worker Lifecycle & the No-Storage Guarantee

The worker is the existing one-shot Docker image
(`object_detection_process/`), with its driver adapted so that:

- input comes from **`VIDEO_URL`** (a direct http(s) link), not `VIDEO_S3_URI`;
- results are **POSTed inline to `CALLBACK_URL`** with `VIDEO_ID`, not uploaded
  to S3;
- everything is written to a **per-job temp directory that is deleted on exit**.

The CV/ML pipeline (`pipeline_utils.py`) is untouched.

---

## Lifecycle of a single worker

```
1. Read env: VIDEO_URL, CALLBACK_URL, VIDEO_ID, JOB_ID
2. Create a private temp dir:  /tmp/job-<JOB_ID>/   (tmpfs / container-local)
3. Download VIDEO_URL into that temp dir (yt-dlp / streaming download)
4. Run pipeline (all outputs inside the temp dir):
     sample_frames -> detect (YOLOE) -> save crops -> dedup (CLIP)
     -> Google Lens match -> write detections.json + detections.vtt
5. Read the two generated files into memory
6. POST { video_id, job_id, status, detections_json, detections_vtt } to CALLBACK_URL
     (retry with backoff; success = HTTP 2xx)
7. Delete the temp dir  (shutil.rmtree, in a finally: block)
8. Exit 0 on success, non-zero on failure
```

Steps 2 and 7 are what make this "no storage". Step 6 replaces the S3 upload of
Architecture 1.

---

## The no-storage guarantee — concretely

| Artifact | Where it lives during the job | After the job |
|---|---|---|
| Input video | `/tmp/job-<id>/ingest/<name>` (container-local temp) | **Deleted** with the temp dir. |
| Sampled frames | In RAM (numpy arrays) | Gone when the process exits. |
| Product crops | `/tmp/job-<id>/crops/` | **Deleted** with the temp dir. |
| `detections.json` / `.vtt` | `/tmp/job-<id>/` | **Deleted** after being POSTed. |

Enforcement details:

1. **Container-local temp only.** The worker writes to a directory under the
   container's own writable layer or a `tmpfs` mount — **never** a bind-mount or
   named volume that would persist on the host. In the run command there are no
   `-v host:container` mounts for job data.
2. **`--rm` on `docker run`.** The container filesystem is discarded when the
   container exits, so even if cleanup were skipped, nothing survives.
3. **Explicit cleanup in a `finally:` block.** `shutil.rmtree(temp_dir,
   ignore_errors=True)` runs on both success and failure paths, so the temp dir
   is removed before exit regardless of outcome.
4. **No S3 output path.** `SKIP_S3_OUTPUT=true` (or simply not configuring an
   output bucket) — the worker delivers via callback and never calls
   `PutObject` for `detections.json` / `.vtt`.
5. **No database.** There is no results table, no job-result store. The only
   record of a result is what the caller persists when the callback arrives.

> **Crop upload caveat (important):** the existing Google-Lens matching step
> uploads each product **crop** to S3 to obtain a URL that Google Lens can fetch
> (`pipeline_utils.S3Uploader`). "No storage" for input/output files does **not**
> automatically remove this. You have three options — see below.

### Handling the crop → Google Lens dependency

Google Lens needs a publicly fetchable image URL for each crop. To honor "no
storage" you must choose one:

| Option | How | Storage? | Trade-off |
|---|---|---|---|
| **A. Ephemeral presigned S3 (recommended)** | Upload crops to a scratch bucket with a **short TTL / lifecycle rule (e.g. delete after 1 hour)**; use a **presigned URL** for Lens; **delete the crop object right after matching**. | Transient only; auto-expired. | Small, self-cleaning; needs one scratch bucket. This is the pragmatic "no persistent storage" answer. |
| **B. Skip matching** | Set `SKIP_MATCHING=true`. Detects + dedups + timestamps, no ecommerce recommendations. | None at all. | You lose product recommendations; `detections.json` still has products + timestamps. |
| **C. Alternative image host** | POST the crop bytes to any temporary image endpoint you control and delete after. | Transient, your infra. | Custom; same shape as A. |

If **truly zero** external storage is required and recommendations are still
wanted, only option C (an endpoint you own that deletes on read) satisfies both,
because Google Lens must fetch the image from *somewhere*. Document which option
you deploy; the default assumption in this architecture is **A** with automatic
expiry, or **B** where recommendations are not needed.

---

## Driver changes vs. the current `worker.py`

The current `worker.py` reads `VIDEO_S3_URI` and uploads outputs to S3
(`OutputUploader`). Architecture 2 needs a thin variant (call it
`worker_callback.py` or a `MODE=callback` branch) that changes only I/O:

```python
# --- input: URL instead of S3 ---
video_url = os.getenv("VIDEO_URL")
video_id  = os.getenv("VIDEO_ID")
job_cfg["input"]["source_type"] = "url"
job_cfg["input"]["url"] = video_url

# ... unchanged pipeline: frames -> detect -> dedup -> (match) -> write files ...

# --- output: inline callback instead of S3 upload ---
detections_json = json.loads(Path(json_path).read_text(encoding="utf-8"))
detections_vtt  = Path(vtt_path).read_text(encoding="utf-8")

payload = {
    "video_id": video_id,
    "job_id": job_id,
    "status": "completed",
    "product_count": len(products),
    "detections_json": detections_json,
    "detections_vtt": detections_vtt,
    "finished_at": _now(),
}
send_callback(callback_url, payload, cfg)   # existing retry/backoff helper

# --- no storage: always clean up ---
finally:
    shutil.rmtree(work_root, ignore_errors=True)
```

Everything between input and output — `sample_frames`, `Detector`, `Embedder`,
`dedup_products`, `Matcher`, `write_metadata` — is reused verbatim from
`pipeline_utils.py`.

### Environment variables (Architecture 2 worker)

| Var | Required | Meaning |
|---|---|---|
| `VIDEO_URL` | yes | Direct http(s) link to the input video (replaces `VIDEO_S3_URI`). |
| `CALLBACK_URL` | yes | Where the inline `.json` + `.vtt` are POSTed. |
| `VIDEO_ID` | yes | Caller's id, echoed back in the callback. |
| `JOB_ID` | no | Correlation id; auto-generated if absent. |
| `SKIP_MATCHING` | no | `true` to skip crops + Google Lens (fully storage-free). |
| `SERPAPI_API_KEY` | if matching | Google Lens via SerpApi. |
| `SCRATCH_BUCKET` / `AWS_*` | if matching via option A | Ephemeral crop bucket for Lens URLs (auto-expiry). |
| `CALLBACK_MULTIPART_THRESHOLD_BYTES` | no | Above this combined size, POST as multipart instead of inline JSON. |

There is deliberately **no** `OUTPUT_BUCKET` for `detections.*` in this mode.

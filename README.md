# Video Product Detection & Ecommerce Recommendation Pipeline

Take a video, find the **distinct products** in it (ignoring people), match each to the
**exact product** on trusted ecommerce sites at a **configurable confidence threshold**,
upload the detected crops to **S3**, and emit a **timestamped metadata file** so you can
overlay recommendations on the video timeline.

Everything runs from one Jupyter notebook: **`product_pipeline.ipynb`**.
All behavior is controlled by **`config.yaml`** — no code edits needed to tune it.

---

## Read this first (honest expectations)

- **"100% accuracy" is not achievable** with any detector or visual-match system. This uses
  state-of-the-art open models (YOLOE open-vocabulary detection + CLIP embeddings) and makes
  every threshold configurable so you can push precision as high as your data allows.
- **Getting the *exact* product** (the specific watch, not just "a watch") needs a visual-search
  backend that indexes ecommerce catalogs. The default is **SerpApi Google Lens `exact_matches`**,
  which is the reliable, no-ban path to real listings with links and prices.
- **Scraping Google Images directly gets blocked** — that is Google's anti-bot policy, not a code
  problem. This pipeline avoids rate limits and crashes with a proper API, plus on-disk caching,
  a rate limiter, and exponential-backoff retries.

---

## Architecture

```
video (local | url | s3)
      -> frame sampling (scene-change filtered)
      -> open-vocabulary detection (YOLOE / YOLO-World)   [humans ignored]
      -> CLIP-embedding dedup                              [distinct products]
      -> S3 upload of crops                                [public/presigned URL]
      -> Google Lens visual search (via SerpApi)
         filtered by trusted domains + min_match_score (the "90%")
      -> timestamped metadata: output/detections.json (+ .vtt for <video> overlay)
```

Files:
- `config.yaml` — all settings (thresholds, prompts, backends, S3, output).
- `pipeline_utils.py` — all reusable logic (ingest, detect, dedup, match, S3, metadata).
- `product_pipeline.ipynb` — the notebook you run.
- `.env` — your API keys and AWS creds (copy from `.env.example`).
- `docs/serpapi-setup.md` — how to get the Google Lens (SerpApi) key.

---

## Setup

1. **GPU torch** (recommended — you have a GPU). Install a CUDA build matching your driver:
   ```bash
   pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
   ```
2. **Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   (The notebook's first cell also runs this.)
3. **Secrets:** copy `.env.example` to `.env` and fill in values you need:
   - `SERPAPI_API_KEY` — for Google Lens search (see `docs/serpapi-setup.md`; free tier available).
   - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_DEFAULT_REGION` — for S3 (or use an IAM role / `~/.aws/credentials`).

---

## Input: three modes (all work)

Set `input.source_type` in `config.yaml`:

| Mode    | Set this                     | Notes |
|---------|------------------------------|-------|
| `local` | `input.local_path`           | A file in the project (e.g. `videos/input.mp4`) or an absolute path. |
| `url`   | `input.url`                  | Any direct video link or hosted video (downloaded via `yt-dlp`). |
| `s3`    | `input.s3_uri`               | `s3://bucket/key` or `https://bucket.s3.region.amazonaws.com/key`. Public buckets work with no credentials (anonymous access); private buckets use your AWS creds. |

---

## Run

Open `product_pipeline.ipynb` and run top to bottom. It will:
1. Ingest the video and sample frames.
2. Detect products (people/hands/faces are dropped via `detection.ignore_labels`).
3. Dedup into distinct products with CLIP.
4. Upload crops to S3 (needed *before* matching, because Google Lens fetches the image by URL).
5. Match each product and keep only results `>= matching.min_match_score` from `matching.trusted_domains`.
6. Write `output/detections.json` and `output/detections.vtt`.

---

## The "90% match" and other tunables

The 90% requirement lives at **`matching.min_match_score: 0.90`**. Only recommendations scoring
at or above it are kept. Other useful knobs:

| Goal | Change in `config.yaml` |
|------|--------------------------|
| Stricter "exact" matches | Raise `matching.min_match_score` (e.g. `0.95`); keep `serpapi.lens_type: exact_matches`. |
| Catch more products | Lower `detection.confidence_threshold`; lower `frames.sample_every_seconds`. |
| Fewer duplicate products | Lower `dedup.same_product_similarity`. |
| More distinct variants kept separate | Raise `dedup.same_product_similarity`. |
| Restrict to specific stores | Edit `matching.trusted_domains`. |
| Which products to look for | Edit `detection.product_prompts` (open-vocabulary — add anything). |
| Rate-limit safety | Tune `network.min_interval_seconds`, `network.max_retries`, `network.backoff_base_seconds`. |

---

## Matching: Google Lens (via SerpApi)

Matching uses **Google only** — Google Lens through SerpApi. Set the Lens tab with
`matching.serpapi.lens_type`:

- **`exact_matches`** (default) — returns the *same* product (the exact watch, not just "a watch").
- **`products`** — shoppable product cards.
- **`visual_matches`** — visually similar look-alikes (looser).

Reliable, no bans, real ecommerce links + prices. Setup: `docs/serpapi-setup.md`.

---

## Output

`output/detections.json`:
```json
{
  "video": { "id": "...", "duration_seconds": 42.0, "fps": 30, ... },
  "config": { "min_match_score": 0.9, "matching_backend": "serpapi" },
  "product_count": 3,
  "products": [
    {
      "product_id": "p0000",
      "label": "watch",
      "s3_url": "https://.../p0000_watch.jpg",
      "first_seen": 3.0,
      "last_seen": 11.0,
      "occurrences": [ { "timestamp": 3.0, "bbox": [x1,y1,x2,y2], "confidence": 0.87 } ],
      "recommendations": [
        { "title": "...", "url": "https://amazon...", "price": "$129",
          "source": "amazon.com", "score": 0.97, "backend": "serpapi" }
      ]
    }
  ]
}
```

`output/detections.vtt` — a WebVTT track. Overlay it directly on an HTML5 video:
```html
<video controls src="your-video.mp4">
  <track default kind="metadata" src="output/detections.vtt" />
</video>
```
Each cue spans a product's `first_seen -> last_seen`, so you can show the recommendation exactly
when the product is on screen.

---

## Notes on reliability

- **Caching:** every search is cached on disk (`cache/search`) keyed by image hash + params, so
  re-running the notebook never re-queries the API (and never re-bills you).
- **Retries:** transient failures retry with exponential backoff (`network.*`).
- **Rate limiting:** a minimum interval is enforced between API calls.
- **Cost control:** for `serpapi`, matching only fires once per *distinct* product (after dedup),
  not once per frame.

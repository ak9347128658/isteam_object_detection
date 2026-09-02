# Getting a SerpApi Key (Google Lens backend)

SerpApi is the **default and recommended** matching backend for this pipeline. It powers
the Google Lens search that finds the *exact* product (e.g. the specific watch) on real
ecommerce sites, returning titles, links, prices, and thumbnails.

This is the reliable, no-ban path. Scraping Google directly gets you blocked; SerpApi
handles that on their side.

---

## 1. Create an account

1. Go to **https://serpapi.com/users/sign_up**
2. Sign up with email (or Google/GitHub).
3. Verify your **email** and **phone number** (both are required before the key becomes active).
4. When prompted, select the **Free** plan and subscribe.

> **Free tier:** 100 searches per month at time of writing. Paid plans scale up from there.
> Source: [SerpApi Google Lens blog](https://serpapi.com/blog/uploading-images-and-searching-with-google-lens-via-serpapi/)
> (content rephrased for compliance with licensing restrictions).

---

## 2. Copy your API key

1. After signing in, open **https://serpapi.com/manage-api-key**
2. Copy the **Private API Key** shown there.

---

## 3. Put the key where the pipeline expects it

### Local (Jupyter / this repo)

Edit your `.env` file (copy it from `.env.example` if you haven't):

```dotenv
SERPAPI_API_KEY=your_real_key_here
```

The notebook loads it automatically via `python-dotenv`.

### Google Colab

Use Colab **Secrets** (the key icon in the left sidebar). Add a secret named exactly:

```
SERPAPI_API_KEY
```

The `colab_check.ipynb` notebook reads it from there automatically.

---

## 4. Confirm it works

Quick standalone test (run in a notebook cell or a Python shell):

```python
import os
from serpapi import GoogleSearch

params = {
    "engine": "google_lens",
    "type": "exact_matches",   # exact_matches | products | visual_matches | all
    "url": "https://i.imgur.com/HBrB8p0.png",   # any public image URL
    "api_key": os.environ["SERPAPI_API_KEY"],
}
results = GoogleSearch(params).get_dict()
print(list(results.keys()))
print(results.get("exact_matches", results.get("visual_matches"))[:2])
```

If you get results (and no `error` key), the key is working.

---

## 5. How the pipeline uses it

In `config.yaml`:

```yaml
matching:
  backend: "serpapi"
  min_match_score: 0.90          # the "90%" filter
  serpapi:
    lens_type: "exact_matches"   # same product, not just look-alikes
    country: "us"
    language: "en"
```

- **`exact_matches`** returns the *same* product across the web (best for "the exact watch").
- **`products`** returns shoppable product cards.
- **`visual_matches`** returns visually similar items (looser).

> **Important:** Google Lens needs a **publicly reachable image URL**. The pipeline uploads
> each detected crop to S3 with `public_read: true` *before* matching, then passes that public
> URL to SerpApi. Make sure your S3 settings are filled in, or the exact/visual match step
> won't have an image to search.

---

## Cost control built into the pipeline

- **Matching runs once per _distinct_ product** (after dedup), not once per frame.
- **Results are cached on disk** (`cache/search/`) keyed by image hash + params, so re-running
  the notebook never re-queries the API and never re-bills you.
- A **rate limiter** and **exponential-backoff retries** are applied (see the `network:` section
  of `config.yaml`) so you don't trip rate limits or crash on transient errors.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SERPAPI_API_KEY is not set` | Add it to `.env` (local) or Colab Secrets, and re-run the setup cell. |
| `error: Invalid API key` | Re-copy from the manage-api-key page; check for trailing spaces. |
| `error: ...run out of searches` | You hit the free-tier limit (100/mo). Wait for reset or upgrade. |
| Matching says it needs a public image URL | Enable S3 with `public_read: true` so Google Lens can fetch the crop. |
| No matches returned | Lower `matching.min_match_score`, or try `lens_type: visual_matches`. |

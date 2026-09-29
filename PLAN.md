# immich-geotag — Project Plan

Fill in missing GPS coordinates for images in an Immich library by recognizing
where the photo was taken (Google Cloud Vision landmarks, with a Google Lens
fallback via SerpApi), reviewing the proposals in a CSV, and writing the
approved coordinates back to Immich.

This document is the complete plan: current state, target design, what is
needed, and the build steps. Written 2026-09-19.

---

## 1. Current state

Already built and working (in this folder):

- `geotag.py` — single-file, zero-dependency Python CLI (stdlib only,
  Python 3.9+). Current version runs a one-shot pipeline: list missing-GPS
  images → recognize → print (dry-run default, `--apply` writes).
- `.env.example` — config template.
- Smoke-tested with fake API payloads (19 checks, all passing): Vision
  response parsing, missing-GEO filtering, multipart upload body, Lens
  match-to-geocode flow, `.env` loading. **Not yet tested against a live
  Immich server or live Google/SerpApi APIs** (no credentials available
  during development).

The rework described below turns the one-shot pipeline into a three-command,
CSV-based workflow. Much of the existing code (HTTP helpers, Immich client,
Vision, Lens, Nominatim) is reused as-is.

## 2. Target design

Three decoupled commands, one CSV as the review buffer:

```
scan       list missing-GPS images from Immich      -> CSV rows (status=pending)
recognize  run recognition on pending rows          -> fills coordinates (no writes)
apply      write approved rows back to Immich       -> status=applied (idempotent)
```

Why a CSV between recognize and apply:

- Recognition costs API quota and time; apply must be reviewable first.
- The CSV is human-editable: fix coordinates, delete bad rows, hand-fill
  rows located manually.
- Every step is resumable and idempotent — an interrupted run loses nothing.

### Commands

```
python3 geotag.py scan --out geotag.csv [--limit N]
    List IMAGE assets whose exif has no latitude/longitude.
    One row per asset, status=pending, coordinates empty.

python3 geotag.py recognize geotag.csv [--min-score 0.5] [--thumbs DIR]
    For each pending row without coordinates:
      fetch the 1440px preview JPEG (via API, or read from DIR if --thumbs),
      try Cloud Vision landmark detection,
      fall back to SerpApi Google Lens if SERPAPI_KEY is set.
    Fills latitude/longitude/place/source/score/maps_url,
    sets status=located or status=not_found.
    Never writes to Immich. Rows that already have coordinates are skipped,
    so re-running only spends quota on new rows.

python3 geotag.py apply geotag.csv [--dry-run]
    For each row with coordinates and status != applied:
      PUT latitude/longitude to Immich, set status=applied.
    --dry-run prints what would be written without changing anything.
    Re-running skips applied rows (idempotent).
```

### CSV schema

```csv
asset_id,file_name,latitude,longitude,place,source,score,status,maps_url
<uuid>,IMG_2041.jpg,48.85837,2.294481,Eiffel Tower,vision,0.91,located,https://...
```

- `asset_id` is the join key. **Not filename** — Immich allows duplicate
  filenames (same name in different folders/albums); filename matching could
  silently write coordinates to the wrong asset. `file_name` stays as a
  human-readable column only.
- `source` is `vision`, `lens`, or `manual` (hand-edited rows).
- `status` transitions: `pending` → `located` | `not_found` → `applied`.
  A row the user deletes from the CSV is simply never applied.

### Review workflow (per batch)

1. `scan --out geotag.csv` (or append to an existing CSV).
2. `recognize geotag.csv` — run in batches, resumable.
3. Open the CSV in a spreadsheet: click `maps_url` for each `located` row,
   fix or delete wrong ones, hand-fill rows you located yourself
   (set `source=manual`).
4. `apply geotag.csv --dry-run` → sanity-check the summary.
5. `apply geotag.csv`.
6. Re-run `scan` to confirm the missing-GPS count dropped.

## 3. What is needed

### Credentials / accounts

| Item | Where | Cost | Required |
|---|---|---|---|
| Immich server URL + API key | Immich UI → Account Settings → API Keys | free | yes |
| Google Cloud Vision API key | console.cloud.google.com → enable Cloud Vision API → create API key | free tier 1000 units/month | yes |
| SerpApi key | serpapi.com | paid, has a free trial | optional (enables Lens fallback) |
| Nominatim | no key; set `NOMINATIM_EMAIL` in config as courtesy | free | used by Lens path |

Config lives in `.env` next to the script (copy from `.env.example`).
Real environment variables override `.env`.

### Runtime

- Python 3.9+ (stdlib only, no pip installs).
- Network access from wherever the script runs to: the Immich server,
  `vision.googleapis.com`, optionally `serpapi.com` and
  `nominatim.openstreetmap.org`.

### Open decision: where the script runs

- **From the Mac over the network (default):** preview JPEGs are fetched via
  the Immich API (~200KB each; fine on a LAN — recognition calls dominate
  runtime anyway).
- **On the server itself:** add `--thumbs DIR` so `recognize` reads the
  already-generated preview files straight from Immich's thumbnail directory
  instead of downloading them. Zero HTTP for image transfer.
  Thumb path layout must be verified against the installed Immich version
  before implementing (sharded layout, roughly
  `thumbs/<id[0:2]>/<id[2:4]>/<assetId>.jpeg`).

## 4. Verified API reference (research done 2026-09-19)

Endpoints confirmed against the OpenAPI spec in `immich-app/immich` `main`
(`open-api/immich-openapi-specs.json`):

- `POST /api/search/metadata` — body: `{page, size, withExif: true,
  type: "IMAGE"}`. Response: `{assets: {items: [...], nextPage, total}}`.
  Older server versions used GET with query params — the client already
  falls back to GET on 404/405.
- Missing GPS = `exifInfo.latitude` or `exifInfo.longitude` is null
  (or `exifInfo` absent entirely).
- `GET /api/assets/{id}/thumbnail?size=preview` — 1440px JPEG used for
  recognition. Fallback: `GET /api/assets/{id}/original`.
- `PUT /api/assets/{id}` — body `{latitude, longitude}` (UpdateAssetDto).
  This is the write path for apply.
- `GET /api/assets/{id}` — single asset incl. `exifInfo`.

External APIs:

- Cloud Vision: `POST https://vision.googleapis.com/v1/images:annotate?key=KEY`
  with `{"requests":[{"image":{"content":"<base64>"},"features":
  [{"type":"LANDMARK_DETECTION","maxResults":5}]}]}`. Best landmark:
  `responses[0].landmarkAnnotations[]` → `description`, `score`,
  `locations[0].latLng.{latitude,longitude}`. Returns exact coordinates,
  but only for recognizable landmarks.
- SerpApi Lens (fallback): `POST https://serpapi.com/image` (multipart,
  field `image`, max 500KB, plus `api_key`) → `image_id`; then
  `GET https://serpapi.com/search.json?engine=google_lens&image_id=...&
  type=visual_matches&hl=en&api_key=...` → `visual_matches[].title`.
  Lens gives matching page titles, not coordinates — geocode the cleaned
  title via Nominatim (`GET https://nominatim.openstreetmap.org/search?
  q=...&format=jsonv2&limit=1`, max 1 req/sec, needs a User-Agent).
  Title cleanup: strip site suffixes ("Brandenburg Gate - Wikipedia" →
  "Brandenburg Gate").

## 5. Build steps

1. **Refactor `geotag.py` to subcommands** (`scan`, `recognize`, `apply`;
   keep `--asset-id` as a `scan` filter). Keep the existing functions:
   HTTP helpers, `Immich` class, `vision_landmark`, `lens_locate`,
   `nominatim_search`, `clean_match_title`, `load_env`.
   **DONE** - committed in `vibe/subcommands-csv-tests-6b8f90`
2. **CSV layer:** small read/write helpers (stdlib `csv` module). Read is
   tolerant: unknown columns ignored, missing optional columns treated as
   empty. Write preserves column order. UTF-8.
   **DONE** - committed in `vibe/subcommands-csv-tests-6b8f90`
3. **`scan`:** reuse `assets_missing_geo()`; write rows with empty
   coordinates, `status=pending`. `--out` defaults to `geotag.csv`;
   appending to an existing CSV must not duplicate `asset_id` rows.
   **DONE** - committed in `vibe/subcommands-csv-tests-6b8f90`
4. **`recognize`:** iterate rows where `status=pending` and coordinates
   empty; reuse `preview_jpeg()` (or `--thumbs` read), `vision_landmark()`,
   `lens_locate()`. Update rows in place, rewrite the CSV after each row
   (crash-safe: quota spent is never lost). Print a per-row line and an
   end summary (located / not_found / errors).
   **DONE** - committed in `vibe/subcommands-csv-tests-6b8f90`
5. **`apply`:** for rows with coordinates and `status != applied`, call
   `set_location()`; mark `status=applied` only after a successful PUT.
   `--dry-run` prints the plan without writes. Summary at the end.
   **DONE** - committed in `vibe/subcommands-csv-tests-6b8f90`
6. **`--thumbs DIR` mode (only if running on the server):** verify the
   thumbnail path layout against the installed Immich version first, then
   add a local-read branch in the preview fetch.
   **DONE** - committed in `vibe/subcommands-csv-tests-6b8f90`
7. **Tests:** extend the fake-payload smoke tests to the CSV layer
   (round-trip, resume-skip, apply idempotency, duplicate asset_id guard).
   **DONE** - committed in `vibe/subcommands-csv-tests-6b8f90` (45 tests)
8. **Live validation:** first real run with `scan --limit 5` +
   `recognize` against the actual server; eyeball every `maps_url` before
   the first `apply`.

## 6. Caveats and known limits

- **Lens coordinates are fuzzy.** Lens returns matching web pages; the
  geocoded result is the location of the match's subject, not the exact
  camera position. Always review `maps_url` before applying. Vision
  landmarks are exact but only cover recognizable landmarks.
- Confidence control: `--min-score` (Vision score threshold, default 0.5;
  use ~0.8 for high-confidence-only runs). Lens rows have no score — treat
  them as "review always".
- Images leave the network: previews go to Google (Vision) and, with the
  fallback enabled, to SerpApi. Confirm this is acceptable for the library
  in question.
- `apply` writes to Immich's DB via the API; it does not touch the original
  files or sidecar XMP unless Immich is configured to write back.
- SerpApi upload limit is 500KB; the 1440px preview normally fits, and
  oversized previews are skipped with a note rather than failing the run.
- Nominatim is rate-limited to 1 request/second (the code already sleeps
  between geocode attempts).

## 7. Suggested first session checklist

1. Create the Google Cloud project + Vision API key; put it in `.env`.
2. `scan --out geotag.csv --limit 5` → confirm the CSV lists real images.
3. `recognize geotag.csv` → check the proposals and `maps_url` links.
4. Fix anything wrong in the CSV by hand; then `apply --dry-run`, then `apply`.
5. Re-run `scan` and confirm those 5 no longer appear.
6. If the hit rate on non-landmark photos is too low, get a SerpApi key,
   add it to `.env`, and re-run `recognize` (only `not_found` rows remain
   pending).

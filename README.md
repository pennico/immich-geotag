# immich-geotag

Fill in missing GPS coordinates in an Immich library by recognizing where
a photo was taken (Google Cloud Vision landmark detection, optional Google
Lens fallback via SerpApi). Coordinates are only ever *added* — photos are
never modified or deleted. Design and history: `PLAN.md`.

## Manual flow (review before writing)

Three commands around one CSV review buffer:

```bash
cp .env.example .env          # IMMICH_URL, IMMICH_API_KEY, GOOGLE_VISION_API_KEY
python3 geotag.py scan out.csv          # list missing-GPS images (newest first)
python3 geotag.py recognize out.csv     # fill coordinates for pending rows
python3 geotag.py apply out.csv         # write located rows to Immich (idempotent)
```

`apply` writes located rows to Immich and is idempotent; use
`apply --dry-run` to preview. `--thumbs DIR` works on local files
without Immich.
Recognize is resumable (crash-safe rewrite, only `pending` rows are spent).

## Monthly runner (automated, free-tier budgeted)

`monthly_geotag.py` spends exactly Google Vision's 1,000 free
landmark-detection units per calendar month, automatically:

- Tracks spend in `geotag-data/usage.json`; refuses to run once the
  month's allowance is used.
- Never re-spends quota: every asset ever recognized is recorded in the
  `geotag-data/state.csv` ledger; `not_found` photos are skipped forever.
- Scans Immich newest-first, recognizes the next unprocessed batch, and
  applies coordinates directly (the user-approved automation of the
  manual review flow).
- Notifies via ntfy (`NTFY_SERVER`/`NTFY_TOPIC`); `--dry` plans without
  API calls, `--limit N` caps a run.
- Writes a per-month markdown report `geotag-data/reports/YYYY-MM.md`
  (counts + the locations found); on the homelab it is published to
  Nextcloud (`immich-geotag/`) so results are readable from the phone
  over Tailscale.

```bash
python3 monthly_geotag.py --dry         # plan only
python3 monthly_geotag.py --limit 5     # small live test
python3 monthly_geotag.py               # up to the free budget
```

Manual runs of `geotag.py` against the same Vision key are invisible to
the usage ledger — add them to `geotag-data/usage.json` by hand.

Deployment (cron schedule, container networking, seeded ledger) is
documented in pennico-ops `homelab/README.md` ("Monthly geotag job").

## Gemini candidate pass (gemini_candidates.py)

Second-chance pass for photos the Vision scan marked `not_found`
(Vision's landmark index misses photos that consumer Lens recognizes,
e.g. full-view monument shots). Sends batches of preview JPEGs to
`gemini-3.8-flash` (project policy: 3.8 or newer only), geocodes each
identified place via Nominatim, and appends rows to
`geotag-data/gemini_candidates.csv`:

- **Never writes to Immich** — candidates are owner-reviewed first;
  approved ones go through the normal `apply` flow.
- Resume-safe: re-running skips asset_ids already in the CSV; stops
  cleanly on Gemini 429 (free-tier daily limit) and resumes later.
- One Gemini request per `--batch-size` images (default 8), so a
  ~300-image window costs ~40 requests.

```bash
python3 gemini_candidates.py --after 2026-03-01 --before 2026-05-01
python3 gemini_candidates.py --asset <uuid>     # single-image test
```

`--allow-bbox` / `--airport-bbox` (repeatable) enforce the trip-geography
rule: a geocode outside the allowed regions is disregarded and the next
query variant is tried. This stops generic landmark names from resolving
to same-named places on other continents (e.g. "Santo Domingo" church ->
Dominican Republic instead of Oaxaca).

## Applying candidates (gemini_apply.py)

Approves and applies Gemini candidates to Immich. All decisions are
persisted (`gemini_candidates.csv`, `asset_times.csv`), so nothing is ever
re-run. Rules, in order:

- **Recover**: high-confidence proposals whose geocode failed at scan
  time are re-geocoded with the trip-geography validator.
- **Confirm**: uncertain proposals (between `--floor` and
  `--min-confidence`) are applied only when the photo's OWN proposal is
  corroborated by neighboring located photos in time AND place
  (`--hours` 2, `--km` 10). The proposal's coordinates are always used,
  never a neighbor's. No proposal or a very low match leaves the geotag
  blank.
- **Flag**: small place-groups (`--max-group` 2) temporally isolated
  (`--isolation-hours` 12) get `status=flagged` / `flag=outlier` for
  owner review instead of being applied.
- **Apply**: merges into `state.csv` and runs the normal `geotag.py`
  apply flow (the only code path that writes to Immich).

```bash
python3 gemini_apply.py --dry-run    # plan only
python3 gemini_apply.py --allow-bbox 14.5,-118.0,33.0,-86.0 \
  --airport-bbox 24.0,-125.0,50.0,-66.0 --airport-bbox 51.0,-0.8,52.0,0.5
```

## Owner review export (gemini_review_export.py)

Rebuilds `geotag-data/gemini-review/` from the candidates ledger: preview
image per photo, the full CSV, and `index.html` with three sections —
applied (green/blue), flagged outliers (orange), identified-but-
ungeocoded (grey). Photos the owner approved carry `flag=reviewed` and
are never re-flagged by later runs.

```bash
python3 gemini_review_export.py               # full rebuild
python3 gemini_review_export.py --no-images   # HTML + CSV only
```

## Location audit (gemini_audit.py)

QA sample of photos that already have a location: every Nth located
photo (`--sample-every`, default 100) is shown to Gemini, the answer is
geocoded, and the distance to the existing tag is classified:
match (≤25 km), check (25–100 km), mismatch (>100 km), no_id,
no_geocode. Never writes to Immich. Resume-safe. Output:
`geotag-data/gemini-audit/` (audit.csv + index.html + images/).

```bash
python3 gemini_audit.py                  # sample every 100th located photo
python3 gemini_audit.py --limit 8       # small test run
```

## Data files

`geotag-data/` (gitignored) holds the run state: `state.csv` (ledger),
`usage.json` (monthly spend), `batch.csv` (current run), and
`reports/` (one markdown report per month). CSVs with
personal photo data are never committed.

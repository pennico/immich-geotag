# immich-geotag

[![CI](https://github.com/pennico/immich-geotag/actions/workflows/ci.yml/badge.svg)](https://github.com/pennico/immich-geotag/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)

Fill in missing GPS coordinates in an [Immich](https://immich.app) photo
library by recognizing *where* each photo was taken — Google Cloud Vision
landmark detection, with optional Google Lens, Gemini, and
visual-similarity passes. Coordinates are only ever **added**: photos
that already have a location are never touched, and nothing is modified
or deleted.

```
photo without GPS  →  landmark recognition (Vision / Lens / Gemini)
                  →  geocode (Nominatim)
                  →  CSV review buffer
                  →  apply to Immich (idempotent, add-only)
```

Design and history: `PLAN.md`.

## Status

In production use on the author's own Immich library. The base pipeline
is stable; the monthly runner is deployed as a cron job and stays inside
the Vision free tier; the visual-similarity, time-proximity and Gemini
extension passes are active. Coordinates are add-only — nothing is ever
modified or deleted.

Tests: 241 unit tests for the tool (329 including the `dispatcher/`
automation), all offline with fake payloads and synthetic fixtures; CI
runs them on Python 3.9 and 3.12 together with `ruff`.

## Requirements and install

- Python 3.9+ — stdlib only, no third-party packages to install.
- An Immich server and an API key.
- A Google Cloud Vision API key (free tier: 1,000 landmark
  detections/month).

```bash
git clone https://github.com/pennico/immich-geotag.git
cd immich-geotag
cp .env.example .env    # fill in IMMICH_URL, IMMICH_API_KEY, GOOGLE_VISION_API_KEY
```

## Quick start: the base pipeline

Three commands around one CSV review buffer:

```bash
python3 geotag.py scan --out out.csv     # list missing-GPS images (newest first)
python3 geotag.py recognize out.csv     # fill coordinates for pending rows
python3 geotag.py apply out.csv         # write located rows to Immich (idempotent)
```

- `apply` is idempotent and add-only; `apply --dry-run` previews.
- `recognize` is resumable: crash-safe rewrite, only `pending` rows are
  spent.
- `--thumbs DIR` reads local thumbnail files instead of downloading
  previews over the API.
- `--asset-id`, `--limit`, and `--min-score` keep runs small while
  testing.
- With a `SERPAPI_KEY` set, `recognize` also tries Google Lens for
  images Cloud Vision does not recognize.

## Matching extensions (optional)

The base pipeline recognizes landmarks. Three optional extensions fill
in what landmark recognition misses — pick the ones that fit your
library:

| Pass | Needs | Catches | Writes to Immich? |
|---|---|---|---|
| (a) Cloud Vision + Lens | Vision API key | named landmarks | only via `apply` |
| (b) similarity autotag | Immich ML + postgres read access | photos of places you already photographed | confident matches only |
| (c) similarity + time | same as (b) | same, guarded by capture time | confident matches only |
| (d) review app + `--apply-approved` | same as (b) | — | proposals you approve |
| (e) time-proximity + similarity groups | Immich API (+ CLIP dump for similarity) | photos shot minutes from a located one, or visually matching a location group | groups you approve in the review app |

### (a) Cloud Vision landmark detection — the base pipeline

The quick start above. Every result lands in the CSV review buffer
first; nothing reaches Immich until you run `apply`. The Lens fallback
(`SERPAPI_KEY`) catches consumer-recognizable shots the Vision landmark
index misses.

### (b) Similarity autotag — `cluster_fill.py`

Fills GPS for photos landmark recognition misses, using Immich's own
CLIP embeddings: photos of the same place land close together in
embedding space, so an unlocated photo can inherit the coordinates of
visually similar located photos. Requires the Immich machine-learning
container (the `smart_search` table) and read access to the Immich
postgres.

```bash
# 1. Dump the similar located matches for every unlocated image
#    (run on the host running the Immich postgres container, read-only)
docker exec -i immich_postgres psql -U postgres -d immich -At \
  < cluster_matches.sql > geotag-data/cluster/matches.csv

# 2. Preview, then apply the confident matches
python3 cluster_fill.py geotag-data/cluster/matches.csv --dry-run
python3 cluster_fill.py geotag-data/cluster/matches.csv
```

A match is applied only when it is **confident**:

- at least 2 matches with similarity >= `--min-sim` (default 0.90) that
  agree within `--agree-m` (default 100 m) of each other, or
- a single match with similarity >= `--solo-sim` (default 0.96).

Everything else becomes a **proposal** (status `review`) — shown on a
review page, never applied automatically. Raise `--min-sim` /
`--solo-sim` for fewer, safer matches; lower them for more proposals to
review. Every applied edit is appended to
`geotag-data/cluster/cluster_edits.csv` so any edit can be traced, and
assets already in the ledger are skipped on re-runs (`--redo`
re-processes them).

### (c) Similarity + time — the time-coherence rule

Visual similarity alone can be wrong: the same landmark photographed on
another trip looks identical but belongs somewhere else in time. The
time-coherence rule guards against this: a proposal is only kept when
the photo and its best match were taken within `--max-dt-min` minutes
(default 10) of each other — the same shooting session.

- Proposal inside the window → stays `review` for you to judge.
- Proposal outside the window → `rejected` (visual similarity alone,
  different session).
- Either photo missing an EXIF date → stays `review`; you judge it on
  the review page.

### (d) Interactive review app and `--apply-approved`

`cluster_review_app.py` builds a self-contained `index.html` from the
proposals ledger: side-by-side cards of each photo and its best match
(plus the full match cluster), a status filter, a similarity slider,
text search, and Google Maps / Immich links.

```bash
python3 cluster_review_app.py                # full build (downloads thumbnails)
python3 cluster_review_app.py --skip-images  # HTML only
```

Approve the good proposals by trimming
`geotag-data/cluster/proposals.csv` down to the rows you want to keep
(the app's checkbox selection copies the selected asset IDs), then
apply:

```bash
python3 cluster_fill.py --apply-approved --dry-run   # preview
python3 cluster_fill.py --apply-approved             # apply to Immich
```

`--apply-approved` applies every ledger row still marked `review`,
marks it `applied`, logs the edit, and is idempotent — re-running
applies nothing new. No match CSV is needed for this step.

### (e) Time-proximity and similarity groups — `time_fill.py`, `similar_fill.py`, `time_fill_review_app.py`

`time_fill.py` needs no recognition at all: photos from one shooting
session are taken minutes apart, so a photo without GPS inherits the
coordinates of the nearest-in-time located photo within
`--window-min` (default 10) minutes. Proposals go to
`geotag-data/time-fill/proposals.csv`; nothing is written at scan time.
Messaging-app copies (e.g. WhatsApp file names) are excluded, because
their timestamps are transfer times, not capture times.

```bash
python3 time_fill.py                              # scan + write proposals
python3 time_fill.py --apply-approved --dry-run   # preview after culling the CSV
python3 time_fill.py --apply-approved             # apply to Immich
```

`similar_fill.py` turns the CLIP match dump from (b) into **location
groups**: donor photos are grouped by coordinates, and an unlocated
photo joins a group only when its matches agree on that one place
(>= 2 matches at similarity >= `--min-sim` 0.90, or one at
`--solo-sim` 0.96), so a group never mixes places. Optional time
(`--max-dt-min`) and linkage (`--complete-linkage`) constraints give
stricter variants.

`time_fill_review_app.py` shows either kind of group as a card — the
photos to tag, the located photos donating the geotag, and how far
apart the donors are — and with `--serve` adds an **Apply** button
that writes one geotag per selected group via
`time_fill.apply_clusters` (ledger + edit log, idempotent). Groups whose
donors are spread over more than 10 km are shown as "mixed" and cannot
be applied.

```bash
python3 time_fill_review_app.py --serve                    # time groups, http://127.0.0.1:8788/
python3 time_fill_review_app.py --source similar --serve --port 8789
python3 time_fill_review_app.py --skip-images              # static index.html only
```

The server binds `127.0.0.1` only. Its `POST /apply` endpoint refuses
cross-site requests: it requires a random per-run token that the server
embeds in the page it serves, a JSON content type, a loopback `Host`
header, and a same-origin `Origin`. Other web pages open in the same
browser therefore cannot trigger writes. Open the URL the server prints
(not the `index.html` file) to use the Apply button.

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
  (counts + the locations found) — copy it anywhere you like to read
  the results from your phone.

```bash
python3 monthly_geotag.py --dry         # plan only
python3 monthly_geotag.py --limit 5     # small live test
python3 monthly_geotag.py               # up to the free budget
```

Manual runs of `geotag.py` against the same Vision key are invisible to
the usage ledger — add them to `geotag-data/usage.json` by hand. Run the
monthly runner from cron (or any scheduler) to backfill a library month
by month at zero cost.

## Gemini candidate pass (gemini_candidates.py)

Second-chance pass for photos the Vision scan marked `not_found`
(Vision's landmark index misses photos that consumer Lens recognizes,
e.g. full-view monument shots). Sends batches of preview JPEGs to
`gemini-3.8-flash`, geocodes each identified place via Nominatim, and
appends rows to `geotag-data/gemini_candidates.csv`:

- **Never writes to Immich** — candidates are owner-reviewed first;
  approved ones go through the normal `apply` flow.
- Resume-safe: re-running skips asset_ids already in the CSV; stops
  cleanly on Gemini 429 (free-tier daily limit) and resumes later.
- One Gemini request per `--batch-size` images (default 8), so a
  ~300-image window costs ~40 requests.

```bash
python3 gemini_candidates.py --after 2024-06-01 --before 2024-08-01
python3 gemini_candidates.py --asset <uuid>     # single-image test
```

`--allow-bbox` / `--airport-bbox` (repeatable) enforce the trip-geography
rule: a geocode outside the allowed regions is disregarded and the next
query variant is tried. This stops generic landmark names from resolving
to same-named places on other continents (e.g. a "Notre-Dame" church
photographed in Paris resolving to Montreal).

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
# example: a trip in France, with a connecting flight through Germany
python3 gemini_apply.py --allow-bbox 41.3,-5.2,51.1,9.6 \
  --airport-bbox 47.2,5.8,55.1,15.1
```

## Review export (gemini_review_export.py)

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

## Configuration

Environment variables (or a `.env` file next to the scripts — see
`.env.example`):

| Variable | Required | Used by |
|---|---|---|
| `IMMICH_URL` | yes | all |
| `IMMICH_API_KEY` | yes | all |
| `GOOGLE_VISION_API_KEY` | yes | geotag, monthly runner |
| `SERPAPI_KEY` | optional | Lens fallback |
| `GEMINI_API_KEY` | Gemini pass | gemini_* scripts |
| `NOMINATIM_EMAIL` | polite | geocoding |
| `IMMICH_PUBLIC_URL` | optional | review-page links |
| `NTFY_SERVER` / `NTFY_TOPIC` / `NTFY_TOKEN` | optional | monthly runner notification |

## Data files

`geotag-data/` (gitignored) holds the run state: `state.csv` (ledger),
`usage.json` (monthly spend), `batch.csv` (current run), and
`reports/` (one markdown report per month). CSVs with personal photo
data are never committed.

## How this repo is built (agent workflow)

This project is developed mostly by AI coding agents, in the open. The
files that drive that process are public on purpose:

- [`AGENTS.md`](AGENTS.md) — instructions every agent session loads:
  scope, hard constraints (no live API calls, stdlib only, add-only,
  no personal data), and the handoff protocol.
- [`STATUS.md`](STATUS.md) — the short current-state file each session
  reads first; [`PLAN.md`](PLAN.md) — the frozen original design;
  [`ROADMAP.md`](ROADMAP.md) — the original build units.
- [`dispatcher/`](dispatcher/) — the scheduler (synced from the
  maintainer's private ops repo) that turns issues and roadmap items into
  headless agent sessions, runs QA and review passes on the resulting
  PRs, and merges them once CI is green. It acts only on issues, PRs and
  comments from the maintainer's account and the repo's own scheduled
  GitHub Actions. `.vibe/` holds the agent CLI's local guard
  configuration.
- Agent-written commits are attributed to the agent (a bot author or a
  `Co-Authored-By` trailer). Agents never run the live pipeline against
  the real photo library — the maintainer does.

The `public-release` branch is an automatic copy of `main` without the
agent files, rebuilt on every merge; `main` is the canonical branch.
Human contributions go through normal pull requests and are reviewed by
the maintainer — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Development

```bash
pip install -r requirements-dev.txt   # pinned pytest + ruff; no runtime deps
pytest -q                             # all tests, offline
ruff check .                          # lint (config in pyproject.toml)
```

## Contributing, security, license

- Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md) and the
  [Code of Conduct](CODE_OF_CONDUCT.md).
- Reporting a vulnerability: see [SECURITY.md](SECURITY.md).
- License: MIT — see [LICENSE](LICENSE).

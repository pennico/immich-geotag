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

## Data files

`geotag-data/` (gitignored) holds the run state: `state.csv` (ledger),
`usage.json` (monthly spend), `batch.csv` (current run), and
`reports/` (one markdown report per month). CSVs with
personal photo data are never committed.

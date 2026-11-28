# immich-geotag — instructions for coding sessions

Fill in missing GPS coordinates for images in an Immich library: scan
missing-GPS assets → recognize locations (Google Cloud Vision landmarks,
SerpApi Google Lens fallback geocoded via Nominatim) → review in a CSV →
apply approved coordinates back to Immich.

The unattended agent workflow (dispatcher, roles, `blocker`/`answered`
issues) was retired on 2026-11-22; work here happens in sessions the owner
starts. `STATUS.md` is the short current-state file — read it first.
`PLAN.md` is the frozen original spec and architecture: read it in
targeted ranges (grep / line ranges), not in full, and do not redesign.

## Working rules

- One PR per coherent chunk against `main`, CI green; update `STATUS.md`
  in the same PR whenever it changes what is done or in flight.
- API-shape doubts: follow the "Verified API reference" section of
  PLAN.md, not external memory.
- Design choices that change what results mean (what is counted,
  excluded, grouped or named) are the owner's: ask before building.
- Never invent live-test results; mark anything untested as untested.

## Hard constraints

- **No live API calls from coding sessions.** A local `.env` may hold a
  working Immich URL and Google Vision API key. Never run any script here
  against them — the live pipeline is the owner's manual flow. In
  particular, never execute `monthly_geotag.py`: a no-args or mistyped
  invocation launches the real job and spends Vision quota. All tests use
  fake payloads and mocked HTTP.
- Use a virtualenv for dependencies, never the system Python.
- **Never commit personal data**: `.env`, CSVs (`*.csv` may contain real
  filenames and locations), and `testdata/` are gitignored — keep them out.
- **`apply` is the only write path** to the photo library. It must stay
  behind the CSV review flow and `--dry-run`. Never add a flag or code
  path that writes to Immich outside `apply`.
- Stdlib only, Python 3.9+, single-file CLI. No new dependencies.
- Keep the existing CLI behaviors working (`--asset-id`, `--limit`,
  `--min-score`).

## Live validation (owner only)

The owner runs live passes locally: `scan --limit 5` → `recognize` →
eyeball `maps_url` → `apply --dry-run` → `apply`.

## Private-pattern scan (before every push)

Run `scripts/scan-private.sh` (tree) and `scripts/scan-private.sh --history origin/main..HEAD` (new commits and their
authors) before pushing. The extra list lives in the git-ignored `private/patterns.txt`; CI reads the same list from
the repository secret `PRIVATE_PATTERNS`. Commit only as the GitHub noreply identity
(`pennico <313404961+pennico@users.noreply.github.com>`); the scan rejects any other author or committer.

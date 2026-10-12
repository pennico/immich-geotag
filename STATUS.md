# immich-geotag — status

Current state, kept short on purpose: this is the file routine agent
sessions read first. `PLAN.md` is the frozen design/history reference —
read it in ranges (grep / line ranges), not in full.

Last updated: 2026-10-12.

## Done and merged

- Base pipeline (ROADMAP.md all checked, PRs #1–#5): `geotag.py`
  `scan` / `recognize` / `apply` subcommands around the CSV review
  buffer, `--thumbs DIR` local-read mode, 66-test fake-payload suite
  at PR #5 (since grown to 161 repo-wide).
- `monthly_geotag.py` — self-running monthly backfill inside the
  Vision free tier (usage ledger, never-retry state, ntfy summary);
  deployed as the homelab monthly cron.
- Extensions beyond the original plan: `cluster_fill.py` +
  `cluster_review_app.py` (visual-similarity clustering pass) and the
  Gemini passes (`gemini_candidates.py`, `gemini_audit.py`,
  `gemini_review_export.py`, `gemini_apply.py`).
- Publication prep (PR #7): README/docs extensions, MIT license,
  community files (CONTRIBUTING, SECURITY).
- PR #8: dispatcher engine sync (quota pause + session cost controls
  from pennico-ops #15/#16).
- PR #16 (issue #15): `monthly_geotag.py` argparse — `--help` prints
  help, unknown flags exit 2 before any work, only no-args / `--dry` /
  `--limit N` launch the job; AGENTS.md network note corrected (live
  credentials ARE present; agents never run the live scripts).
  9 tests in `test_monthly_geotag.py`.
- Public-release blocker (issue #20) resolved by PR #21: AGENTS.md and
  STATUS.md are stripped from the published tree; the rebuild workflow
  has run green on every merge since (PRs #21–#23). Verified 2026-10-12:
  `origin/public-release` tree contains neither file and the last
  rebuild run succeeded.

## In flight

- Open owner action: add a conservative 2026-10 estimate to
  `geotag-data/usage.json` for the interrupted verification runs
  (owner-only, gitignored file).

## Runtime data (never committed)

`geotag-data/` — cluster matches, Gemini audit/review CSVs and
images, `testdata/`, `.env`. Gitignored; agents never read images
into context.

## Rules for this file

- One screenful: if it grows past ~150 lines, move detail into
  README or docs and leave the summary here.
- Update it in the same PR that changes what is done or in flight.

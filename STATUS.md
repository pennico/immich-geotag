# immich-geotag — status

Current state, kept short on purpose: this is the file routine agent
sessions read first. `PLAN.md` is the frozen design/history reference —
read it in ranges (grep / line ranges), not in full.

Last updated: 2026-10-17.

## Done and merged

- Base pipeline (ROADMAP.md all checked, PRs #1–#5): `geotag.py`
  `scan` / `recognize` / `apply` subcommands around the CSV review
  buffer, `--thumbs DIR` local-read mode, fake-payload test suite.
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
- Public visibility (issue #20, PR #21; claim corrected 2026-10-17): the
  `public-release` workflow builds a separate branch without AGENTS.md,
  STATUS.md, `.vibe/` and `dispatcher/`, but `main` is the public
  default branch, so those files ARE public. That is deliberate: the
  agent workflow is documented in the README ("How this repo is built")
  instead of being hidden. Never put anything in these files that must
  stay private.
- Issue #27 (delivery-verification gap): the dispatcher test suite is
  hermetic — the `env` fixture mocks `memory.block`/`memory.save` so no
  test reads or writes the live agent-memory service.
- Open-source hygiene: synthetic fixtures (no real places/dates from the
  owner's library in tests or docs), `pyproject.toml` with pinned dev
  tools, CI on Python 3.9 + 3.12, review-app CSRF/Origin/Host guard on
  `POST /apply`, community files.
- Post-merge check (#41): 375 tests pass, `ruff check` clean; pytest bumped
  to 9.0.3 (Python ≥3.10; 3.9 CI job stays on 8.4.2); dispatcher test
  made independent of the ambient `AGENT_BACKEND`.

## In flight

Location sources beyond the base pipeline. All are add-only and go
through owner review (review app or ledger) before anything is written;
run statistics stay in the gitignored `geotag-data/` reports, not here.

- Time-proximity fill (`time_fill.py` + `time_fill_review_app.py`): a
  missing-GPS photo inherits the coordinates of the nearest-in-time
  located photo within 10 minutes. Clusters C001… in
  `geotag-data/time-fill/clusters.csv`; edit log
  `geotag-data/time-fill/edits.csv`. Messaging-app copies (WhatsApp
  file names) are excluded: their timestamps are transfer times, not
  capture times. Large scan/import batches that mix many places cannot
  be tagged by time.
- Similarity fill (`similar_fill.py`, review app `--source similar`):
  CLIP pass for photos the time rule cannot pin. Donors grouped by
  coordinates (single linkage, 1 km); a photo joins one location group
  when >= 2 matches with similarity >= 0.90 agree there, or one match
  reaches 0.96. Clusters S001…. Input: `geotag-data/cluster/
  matches.csv`, dumped read-only from the Immich postgres via
  `cluster_matches.sql` (slow — run it in a background process).
- Further CLIP attempts, each with its own cluster prefix and review
  app `--source`, all with complete linkage at 300 m:
  `similar-time` (T…, similarity >= 0.85 + within 10 min),
  `similar-loose` (U…, >= 0.80 from `cluster_matches_loose.sql`),
  `similar-vloose` (V…, >= 0.70 from `cluster_matches_vloose.sql`),
  `similar-sameday` (W…, >= 0.70 + same UTC day),
  `similar-wa` (X…, messaging-app send batches that share one file
  timestamp, proposed only when all pooled same-day matches agree).
  Photos rejected by one rule may reappear under another; the owner
  reviews them visually.
- Permanent mixed-cluster guard: `time_fill.MIXED_SPREAD_M` (10 km) —
  `apply_clusters` refuses clusters whose donors are spread wider; the
  review app shows them as "mixed — not applyable".
- Open owner action: add a conservative estimate for the interrupted
  verification runs to `geotag-data/usage.json` (owner-only,
  gitignored file).

## Runtime data (never committed)

`geotag-data/` — cluster matches, Gemini audit/review CSVs and
images, `testdata/`, `.env`. Gitignored; agents never read images
into context.

## Rules for this file

- One screenful: if it grows past ~150 lines, move detail into
  README or docs and leave the summary here.
- Update it in the same PR that changes what is done or in flight.

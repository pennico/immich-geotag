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

- Time-proximity fill (`time_fill.py` + `time_fill_review_app.py`,
  `test_time_fill.py`): fourth location source — a missing-GPS photo
  inherits the coordinates of the nearest-in-time located photo within
  10 minutes. Owner-reviewed 2026-10-12: 271 applied (257 clean batch
  + 14 via the cluster app's Apply button), 227 rejected (WhatsApp
  bulk-send timestamps are transfer times, not capture times).
  After the owner deleted trash photos: rescan found 15,271 images,
  4,712 missing GPS; 35 time clusters (C001…, `geotag-data/time-fill/
  clusters.csv`), 740 photos pending (47 in 32 small groups + 693 in
  three giant scan batches C001/C003/C009 that mix many places —
  untaggable by time). Review app served on port 8788; edit log
  `geotag-data/time-fill/edits.csv`.
- Similarity fill (`similar_fill.py`, `test_similar_fill.py`): CLIP
  pass for the photos the time rule cannot pin. Donor photos are
  grouped by coordinates (single linkage, 1 km) and each unlocated
  photo joins one location group when >= 2 matches with similarity
  >= 0.90 agree there, or a single match reaches 0.96 — so photos
  from different places are never mixed into one cluster. Clusters
  (S001…) go to `geotag-data/similar-fill/clusters.csv` (same schema
  as time clusters) and the review app serves them with
  `--source similar` (port 8789). Input: `geotag-data/cluster/
  matches.csv`, dumped read-only from the Immich postgres via
  `cluster_matches.sql` (top 10 located matches per unlocated image,
  cosine sim >= 0.85; the dump takes tens of minutes — run it in a
  background process, not a foreground ssh).
- Similarity+time fill (`similar_fill.run_time`, served with
  `--source similar-time` on port 8790): second CLIP attempt —
  similarity >= 0.85 (lower bar) but the photo and its match must be
  within 10 minutes of each other, and donors are grouped with
  complete linkage at 300 m (no chaining, extent <= 300 m by
  construction). Cluster IDs T001… in `geotag-data/similar-time/
  clusters.csv` (T prefix so they cannot be confused with the S IDs).
  2026-10-12 run: 75 photos to tag in 20 clusters; 1,392 photos had
  no match inside the 10-min window, 100 disagree on location, 52
  already applied. Zero overlap with the pending S037-S042. All 75
  applied by the owner the same day.
- Loose similarity fill (`similar_fill.run_loose`, served with
  `--source similar-loose` on port 8791): third CLIP attempt — same
  rules as run_time but matches only need similarity >= 0.80, from
  the looser dump (`cluster_matches_loose.sql` ->
  `geotag-data/cluster/matches_loose.csv`, 18,361 rows for 2,614
  unlocated photos). Cluster IDs U001… in `geotag-data/similar-loose/
  clusters.csv`. 2026-10-12 run: 97 photos to tag in 27 clusters
  (max donor extent 293 m), zero overlap with the applied T set;
  2,344 photos had no match inside the 10-min window, 173 disagree.
  All 97 applied by the owner; re-run after the owner tagged more
  photos: 47 new photos in 16 clusters (max extent 164 m), unlocated
  pool down to 2,482.
- Very loose similarity fill (`similar_fill.run_vloose`, served with
  `--source similar-vloose` on port 8792): fourth CLIP attempt —
  same rules but matches only need similarity >= 0.70, from
  `cluster_matches_vloose.sql` -> `geotag-data/cluster/
  matches_vloose.csv` (36,536 rows for 3,900 unlocated photos).
  Cluster IDs V001… in `geotag-data/similar-vloose/clusters.csv`.
  2026-10-12 run: 42 photos in 15 clusters (max extent 293 m);
  3,661 outside the 10-min window, 197 disagree. Three old WhatsApp
  time-rule rejections reappear here under the similarity rule
  (deliberate: a different proposal type, owner reviews visually).
- Same-day similarity fill (`similar_fill.run_sameday`, served with
  `--source similar-sameday` on port 8793): fifth CLIP attempt —
  similarity >= 0.70 with the window widened from 10 minutes to
  "both photos on the same UTC calendar day" (`same_day=True`),
  complete linkage at 300 m kept. Cluster IDs W001… in
  `geotag-data/similar-sameday/clusters.csv`. 2026-10-12 run:
  132 photos in 57 clusters (max donor extent 285 m); 3,378 matches
  on a different day, 348 disagree, 42 already applied (the V set).
  Eight clusters (W018-W020, W022, W023, W040, W043, W051) contain
  13 WhatsApp photos rejected in earlier attempts — flagged to the
  owner, review before applying. Owner applied 128 of the 132; the
  2026-10-12 rescan (fresh vloose dump, 3,733 unlocated left)
  re-proposes 46 photos in 26 clusters (max extent 200 m, W IDs
  renumbered); the 4 unapplied leftovers all landed in W002, one old
  rejection reappears in W026.
- WhatsApp send-batch fill (`similar_fill.run_wa` / `assign_wa`, served
  with `--source similar-wa` on port 8794): sixth attempt — WhatsApp
  photos sent in one bulk share an identical file timestamp, so they
  form batches; each batch pools its members' located matches
  (similarity >= 0.70, same UTC day as the send) and is proposed only
  when all pooled matches agree within one 300 m complete-linkage
  group. Solo WhatsApp photos are left to the regular runs. Cluster
  IDs X001… in `geotag-data/similar-wa/clusters.csv`. 2026-10-12
  run: 173 photos in 25 clusters (max donor extent 284 m); 910 batch
  photos had no same-day match, 141 disagreed, 8 already applied.
  35 previously rejected WhatsApp photos resurface in X001, X004,
  X006, X007, X010, X021; X003 overlaps the pending W002 pair. All
  898 ledger-applied photos verified to carry GPS in Immich.
- Permanent mixed-cluster guard: `time_fill.MIXED_SPREAD_M` (10 km) —
  `apply_clusters` refuses clusters whose donors are spread wider
  (different places must never share one geotag); the review app
  shows such clusters as "mixed — not applyable" with the checkbox
  disabled.
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

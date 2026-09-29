# immich-geotag — agent instructions

Fill in missing GPS coordinates for images in an Immich library: scan
missing-GPS assets → recognize locations (Google Cloud Vision landmarks,
SerpApi Google Lens fallback geocoded via Nominatim) → review in a CSV →
apply approved coordinates back to Immich.

`PLAN.md` is the complete, approved spec AND architecture: target design
(three subcommands, CSV as review buffer), verified API reference, build
steps 1–7, caveats. Read it first and follow it — do not redesign. The
only roles needed here are Implementation and QA (spec/architecture are
done); your session prompt names your role.

## Work (from PLAN.md build steps, in order)

1. Refactor `geotag.py` into `scan` / `recognize` / `apply` subcommands,
   keeping the existing functions (HTTP helpers, `Immich`, `vision_landmark`,
   `lens_locate`, `nominatim_search`, `clean_match_title`, `load_env`).
2. CSV layer (stdlib `csv`): tolerant read, order-preserving write, UTF-8.
3. `scan` → pending rows; append must not duplicate `asset_id`.
4. `recognize` → fills coordinates, never writes to Immich, rewrites the
   CSV after each row (crash-safe), skips rows that already have
   coordinates.
5. `apply` → PUT only rows with coordinates and `status != applied`;
   `--dry-run` default behavior per plan; idempotent.
6. `--thumbs DIR` local-read mode (verify Immich thumbnail path layout
   against the OpenAPI spec / installed version notes in PLAN.md first).
7. Committed test suite: the 19 ad-hoc smoke checks from development were
   never committed — rebuild them as a real fake-payload test file, plus
   CSV round-trip, resume-skip, apply idempotency, duplicate-asset_id
   guard tests.

## Decision defaults (do not stop to ask)

You may run unattended (headless dispatcher); a stopped session wastes the
whole run. When a question is not answered by PLAN.md:

- Follow PLAN.md literally; where it is silent, prefer the smallest
  change that satisfies the build step and record the choice in the PR's
  Handoff section.
- API-shape doubts: follow the "Verified API reference" section of PLAN.md,
  not external memory.
- Never invent live-test results; mark anything untested as untested.
- Only stop (open a GitHub issue) if proceeding would write to Immich
  outside the apply flow or break existing CLI behavior.

## Hard constraints

- **No live API calls.** The sandbox has no credentials and no network
  path to the owner's Immich server. All tests use fake payloads and
  mocked HTTP. Never fabricate "live validation" results.
- **Never pip install into the system Python.** Sessions may run inside
  the agent-runner container, whose Python is shared with the `vibe`
  CLI itself — installing or upgrading packages there breaks the agent
  loop. If you need dependencies, use a venv:
  `python3 -m venv /tmp/venv && /tmp/venv/bin/pip install ...`.
- **Never commit personal data**: `.env`, CSVs (`*.csv` may contain real
  filenames and locations), and `testdata/` (the owner copies real photos
  there for local testing) are gitignored — keep them out.
- **`apply` is the only write path** to the owner's photo library. It
  must stay behind the CSV review flow and `--dry-run`. Never add a flag
  or code path that writes to Immich outside `apply`.
- Stdlib only, Python 3.9+, single-file CLI. No new dependencies.
- Keep the existing CLI behaviors working (`--asset-id`, `--limit`,
  `--min-score`).
- One PR per coherent chunk (e.g. subcommands+CSV, then recognize/apply,
  then tests). Update `PLAN.md` with a short "what changed" note per PR.

## Live validation (owner only, never the agent)

The owner runs the first live pass locally: `scan --limit 5` →
`recognize` → eyeball `maps_url` → `apply --dry-run` → `apply`. Make this
checklist easy to find (README or PLAN section 7 already has it).

## Handoff protocol (unattended sessions)

All roles run headless, launched by the dispatcher; you cannot launch,
resume, or signal another session. The repo is the only coordination bus.

- End every PR with a **Handoff** section: what is done and verified, what
  the next session should pick up, and any decision the owner must make.
- If you are blocked (question only the owner can answer, live validation
  needed, work outside the build steps), **open a GitHub issue labeled
  `blocker`** (`gh issue create --label blocker --title ... --body ...`)
  and stop cleanly. The owner gets a push notification, answers in a
  comment, and adds the `answered` label — the dispatcher then resumes
  with your issue and the owner's answer quoted in the prompt. Close the
  issue once your resumed session has resolved it.
- Never wait idly for another agent — either proceed with work that is
  within your role, or stop with the issue filed.

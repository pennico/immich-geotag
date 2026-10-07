# Unattended agent dispatcher

Runs the multi-agent workflow headless on an always-on runner (homelab
container — see `pennico-ops/homelab/`) so the project progresses without
anyone launching sessions manually.

## What a tick does

1. Abort if the working tree is dirty; checkout main; pull --ff-only.
2. Blocker issues (label `blocker`): a new one notifies the owner once and
   pauses the repo; one with the `answered` label (owner commented +
   labeled) or closed by the owner resumes the loop with the answer quoted
   in the session prompt.
3. Open PRs? -> merge approved + green ones first (squash); any
   remaining without the `approved` label trigger a QA session (two QA
   passes without approval -> blocker issue + pause).
4. No PRs -> Implementation session on the first unchecked ROADMAP.md item
   (every chunk is an [impl] session).
5. Roadmap complete -> idle + one "roadmap done" notification.
6. Delivery verification: once per idle period, a verification session
   asks "Has the project fulfilled its scope and are all the
   deliverables completed, and stored correctly?" — gaps come back as `[verify]` task
   issues the loop schedules fixes for; a clean pass notifies the owner.
   An approved PR with red CI gets one automatic fix session (the fix
   re-enters the review pipeline) before the dispatcher escalates to a
   blocker.

## Modes

- `python3 dispatcher/dispatch.py --dry` — print decisions, do nothing.
- `python3 dispatcher/dispatch.py` — one tick (one session at most).
- `dispatcher/run.sh --loop` — back-to-back: after each session, re-evaluate
  and launch the next immediately. Stops only when the roadmap is
  complete, the repo is blocked, or the failure budget trips. This is what
  the homelab supervisor runs.

## Failure budget

`dispatcher/state.json` (gitignored) tracks consecutive failed sessions
(non-zero exit or timeout) per roadmap item. 2 strikes on the same item ->
the dispatcher files a blocker issue and pauses the repo; the owner's
answer on that issue resumes with a fresh budget. QA has the same budget:
2 QA passes on the same PR without approval -> blocker issue + pause.

## Context diet (per-session data pull)

Cached tokens dominate usage, and the measured top filler is the same
text re-fetched by every session (one PR diff was pulled 5+ times across
a single rework cycle on 2026-10-06). The engine now:

- **Writes per-PR briefing files** (`dispatcher/briefings/pr-<n>.md`,
  gitignored) before each review/rework/CI-fix session — diff +
  comments + metadata fetched once per tick — and appends a prompt note
  pointing the session at them instead of `gh pr diff` /
  `gh pr view --comments`. Best-effort: a failed fetch falls back to
  the session fetching itself. Briefings for closed PRs are removed
  each tick.
- **Rotates session logs**: transcripts gzip after 7 days, are deleted
  after 30 (`rotate_logs()` at dispatcher start).

The matching session norms (targeted reads, one full doc read per
session, short status docs) are in `AGENTIC_WORK_STANDARDS.md` under
"Context discipline".

## Notifications

`dispatcher/notify.py` sends push notifications via ntfy (outbound HTTPS
only — nothing is exposed inbound). Set `NTFY_TOPIC` in the runner's
environment (optionally `NTFY_SERVER` to self-host). Without a topic,
notifications are a silent no-op. Events: blocker filed (high), failure
budget hit (high), roadmap done (high), PR opened/approved/merged
(default). Failures to send are logged, never fatal — GitHub issues are
the durable record.

## Cost controls

- Sessions run with `--max-price $DISPATCH_MAX_PRICE` (default 10) plus a
  90-minute timeout. The cap is a runaway guard, not a budget target;
  after the first supervised runs, calibrate it to roughly 2x the
  observed cost of a normal session (token usage is in the session
  JSON logs).
- Implementation sessions run `--max-turns 150`; qa/adversarial/verify
  sessions run `--max-turns $DISPATCH_MAX_TURNS_REVIEW` (default 60).
- Every session launch checks and increments a shared daily counter
  (`<projects>/.session-budget.json`, all repos, one budget). At
  `DISPATCH_DAILY_SESSIONS` (default 14) no sessions launch for the rest
  of the day; the owner is notified once.
- A session that dies on an API-limit signature (HTTP 402/429, quota,
  rate limit, ...) is not a real failure: no strike, no rework, no
  blocker — the repo pauses for `DISPATCH_QUOTA_COOLDOWN_S` (default
  6h) and retries automatically. GitHub's own rate limits (gh CLI,
  api.github.com) are the exception (issue #9): they clear in minutes,
  so they count a normal strike — after 2 strikes the diagnosis issue
  reaches the owner — and never trigger the 6-hour pause.

## Guardrails

- `.vibe/hooks.toml` + `.vibe/guard.py` deny destructive bash commands
  (force-push, reset --hard, rm -rf, sudo, pipe-to-shell) and any agent
  access to `.env`. Loaded because the dispatcher passes `--trust`.
- Sessions run with `--auto-approve --max-turns ... --max-price ...` and
  a 90-minute timeout; output is captured, never interactive.
- The dispatcher never force-pushes, never merges unapproved PRs, and
  aborts on a dirty tree.

## Logs

`dispatcher/logs/` (gitignored): one JSON file per session, an `.exit`
file with its return code, and the runner's cron/supervisor log.

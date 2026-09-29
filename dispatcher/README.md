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
3. Open PRs? -> any without the `approved` label triggers a QA session
   (two QA passes without approval -> blocker issue + pause); all approved
   + green checks -> squash-merge.
4. No PRs -> Implementation session on the first unchecked ROADMAP.md item
   (every chunk is an [impl] session).
5. Roadmap complete -> idle + one "roadmap done" notification.

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

## Notifications

`dispatcher/notify.py` sends push notifications via ntfy (outbound HTTPS
only — nothing is exposed inbound). Set `NTFY_TOPIC` in the runner's
environment (optionally `NTFY_SERVER` to self-host). Without a topic,
notifications are a silent no-op. Events: blocker filed (high), failure
budget hit (high), roadmap done (high), PR opened/approved/merged
(default). Failures to send are logged, never fatal — GitHub issues are
the durable record.

## Cost cap

Sessions run with `--max-price $DISPATCH_MAX_PRICE` (default 10) plus
`--max-turns 150` and a 90-minute timeout. The cap is a runaway guard, not
a budget target; after the first supervised runs, calibrate it to roughly
2x the observed cost of a normal session (token usage is in the session
JSON logs).

## Guardrails

- `.vibe/hooks.toml` + `.vibe/guard.py` deny destructive bash commands
  (force-push, reset --hard, rm -rf, sudo, pipe-to-shell) and any agent
  access to `.env`. Loaded because the dispatcher passes `--trust`.
- Sessions run with `--auto-approve --max-turns 150 --max-price ...` and a
  90-minute timeout; output is captured, never interactive.
- The dispatcher never force-pushes, never merges unapproved PRs, and
  aborts on a dirty tree.

## Logs

`dispatcher/logs/` (gitignored): one JSON file per session, an `.exit`
file with its return code, and the runner's cron/supervisor log.

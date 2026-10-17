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

## Session cost controls

The engine (this repo is the canonical copy; the project repos carry
the same code) caps session spend three ways:

- **Shared daily session budget.** Every session launch, in every
  repo, checks and increments `<projects>/.session-budget.json` (next
  to `.publish-flags`, runtime data, never committed), keyed by date,
  under an exclusive file lock — concurrent repos cannot overshoot
  the cap or lose counts. The counter is written atomically (temp
  file + rename), so a concurrent reader never sees a half-written
  file. When the count reaches `DISPATCH_DAILY_SESSIONS` (default
  14), no sessions launch for the rest of the day, the owner gets one
  notification, and ticks stay cheap (git/gh only). A missing or
  corrupt counter file never blocks a tick — it is recreated at zero,
  **loudly**: the tick log says so, because a counter that vanishes
  mid-day unmeters every repo (on 2026-10-06 agent sessions deleted
  the live file twice and the budget silently restarted; issue #21).
  The counter is live shared state — never delete or hand-edit it. A
  reserved slot whose launch never started (vibe missing) is given
  back, so a broken launch cannot burn the shared budget.
- **Review strike cap.** A failed review round (the reviewer did not
  add its label) counts one strike per PR in `qa_strikes`; the strike
  is followed by a rework session. Strikes accumulate across BOTH
  reviewers — a passed review does not reset them; only a merge does.
  At `QA_STRIKES` (default 2) strikes the next failed round files a
  blocker and pauses the repo (engine-sync PRs get a cap of 1). A
  rework that could not run (API-quota pause or spent budget) undoes
  its round's strike; a completed rework keeps it, and a rework that
  pushes nothing escalates immediately instead of looping.
- **API-quota pause.** A session that dies on an API-limit signature
  (HTTP 402/429, quota, rate limit, ... — matched on the session's
  stderr error line, never the stdout transcript, whose tool output
  would false-match) is not a real failure: no strike, no rework, no
  blocker, no diagnosis issue. The repo pauses for
  `DISPATCH_QUOTA_COOLDOWN_S` (default 6h, `quota_pause_until` in
  state.json) with one notification, then retries automatically.
- **Role-based turn caps.** Implementation sessions run with
  `--max-turns 150`; qa/adversarial/verify sessions with
  `DISPATCH_MAX_TURNS_REVIEW` (default 60) — reviews rarely need the
  cached-token tail of a full impl conversation.

Delivery verification runs once per idle period; the per-merge
verification is off by default (`DISPATCH_VERIFY_PER_MERGE=1` restores
it).

## Engine roles

`impl`, `qa`, `adv`, and `verify` are the roles the loop launches.
`arch` (architecture) exists only in tts-ereader, where ROADMAP items
can be tagged `[arch]`; its roadmap work is done, but the role stays
defined there. The turn-cap line (`impl`/`arch` get the full 150
turns) mentions `arch` in every engine copy so the engine text stays
identical across repos; in repos without the role it can never
launch.

## Agent backend (vibe / Claude / both)

`dispatcher/agent_backend.py` picks the CLI per role. `AGENT_BACKEND`:

| value | impl, arch, qa | adv, verify, overseer | fallback |
|---|---|---|---|
| `vibe` (default) | vibe | vibe | none |
| `claude` | Claude | Claude | none |
| `both` | vibe | Claude | the other backend, once |

`AGENT_BACKEND_<ROLE>=vibe|claude` (IMPL, ARCH, QA, ADV, VERIFY) pins one role. In `both` mode a session that dies on an API
limit (HTTP 402/429 signature) or whose CLI is missing is retried once
on the other backend **in the same budget slot**; the repo enters the
quota cooldown only when both died. A real failure (bad exit, timeout)
is not retried.

Claude sessions run `claude -p … --permission-mode dontAsk
--allowedTools <list> --max-turns N --max-budget-usd
$DISPATCH_MAX_PRICE --output-format json --no-session-persistence
--settings <guard hook>`:

- no prompts, and anything outside the allow-list
  (`DISPATCH_CLAUDE_ALLOWED_TOOLS`, default in `agent_backend.py`: file
  tools plus git/gh/python3/pytest/ruff and read-only shell tools) is
  denied. The allow-list stops stray commands (curl, rm, …), but it is
  **not a sandbox**: `python3` can do anything;
- `.vibe/guard.py` runs as a PreToolUse hook (`--claude` output
  mode), so Claude gets the same destructive-command and `.env` denies
  as vibe;
- never `--dangerously-skip-permissions`: GH_TOKEN and API keys are in
  the environment;
- auth: `CLAUDE_CODE_OAUTH_TOKEN` (`claude setup-token`) or
  `ANTHROPIC_API_KEY`. Vibe sessions get these removed from their
  environment;
- `DISPATCH_CLAUDE_MODEL` overrides the model (unset = CLI default);
- logs: `<ts>-claude-<role>.json|.err|.exit`. The JSON is the result
  object (reply, `total_cost_usd`, usage), and the cost is logged per
  session.

## Tests

`pytest dispatcher -q` (no network, no gh/git/vibe). The fixtures
point `STATE_FILE`, `BUDGET_FILE`, `LOGDIR`, `PUBLISH_FLAGS`, and
`ROADMAP` at `tmp_path`: tests and any ad-hoc script that exercises
the engine must never read or write the real runtime state — on
2026-10-06 engine checks run by review sessions launched `run_session`
against the real `LOGDIR` and left fake session logs in
`dispatcher/logs/` (issue #21). `test_run_session_creates_nothing_outside_tmp`
guards this.

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

Delivery rules:
- `notify()` retries network errors, 5xx and 429 responses up to 3
  times. A 4xx (auth or ACL) response fails at once.
- A blocker goes into `seen_blockers` only after ntfy accepted its
  announcement. A failed send is retried on the next tick.
- An open, unanswered blocker is re-announced every 24 h
  (`blocker_reminded` in state.json, `BLOCKER_REMIND_S`).
- An issue that gets the `blocker` label again (failed task or run
  job) is dropped from `seen_blockers` and `resumed_blockers`, so it is
  announced again and a new `answered` resumes it.
- `blocker_issues()` returns None when gh fails, and the tick aborts.
  A gh failure is not read as "no blockers".

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

**Trusted authors.** The project repos are public, so anyone can open a
fork PR, file an issue, or comment. The tick only acts on input from
`DISPATCH_TRUSTED_AUTHORS` (comma-separated logins, default: the owner,
the agent account `pennico-agent` and GitHub Actions):

- open PRs: only same-repo branches by a trusted author (fork PRs are
  never reviewed, tested, or merged);
- `task` issues: only when opened by a trusted author;
- task-issue comments and PR briefings: only trusted authors' comments
  reach a session prompt.

**Owner-only.** Agent sessions act as their own machine user
(`pennico-agent`), the owner as `pennico` (`DISPATCH_OWNER_AUTHORS`).
Only the owner can answer a blocker (the answer is the owner's last
comment; an `answered` label without an owner comment is removed and
ignored) or open a `run` issue (also allowed: scheduled GitHub Actions).
The Telegram bridge posts the owner's replies with `OWNER_GH_TOKEN`, so
they count as the owner.

Residual: a session that falls back to `gh pr view --comments` itself
still sees every comment. Keep repo interaction limits on if a PR thread
gets hostile, and add any new bot account to the variable.

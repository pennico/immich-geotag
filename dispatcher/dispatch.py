#!/usr/bin/env python3
"""Headless agent dispatcher for immich-geotag.

One invocation = one tick; with --loop the dispatcher re-evaluates after
each session and keeps going back-to-back until the repo is idle, blocked,
or aborted. run.sh prevents overlapping invocations. State machine per tick:

  1. If the working tree is dirty (interrupted session), stash -u, notify,
     and continue. Checkout main, pull --ff-only.
  2. Blocker issues (label `blocker`) are the human-in-the-loop channel:
       - new blocker -> notify the owner once (push); repo pauses;
       - blocker with the `answered` label (owner commented + labeled) or
         closed by the owner -> resume: launch a session with the issue
         number and the owner's answer quoted in the prompt.
  3. Run issues (open, label `run`) are direct script jobs — no session.
     Executed BEFORE the blocker gate and the quota/budget pauses:
     calendar jobs (monthly backfills) must fire on schedule even
     while the repo waits on an unanswered blocker; they carry their
     own budgets. A run issue that failed already carries the `blocker`
     label and is skipped here, so it is not retried every tick.
  4. Task issues (open, label `task`) are owner-filed work orders — the
     way to launch work from the phone. First unlaunched task -> one
     session with the issue body and comments as the prompt. Two failed
     task sessions -> the issue gains the `blocker` label and the repo
     pauses.
  5. Main-branch CI: the latest run on main is checked every tick. A
     red run gets one automatic fix session (pushed straight to main);
     FAIL_STRIKES consecutive red fixes -> blocker + pause. A green run
     resets the budget.
  6. If open PRs exist:
       - merge PRs with BOTH review labels (`approved` + `approved-adversarial`)
         and green checks first (squash, delete branch);
       - two-reviewer pipeline, one review session per tick: a PR lacking
         `approved` gets a correctness (QA) review; a PR with `approved`
         but lacking `approved-adversarial` gets an adversarial review.
         A failed review (label not added) triggers a rework session on
         the PR (impl role, addresses the review comments) and invalidates
         prior approvals. Two failed review rounds on the same PR ->
         blocker + pause.
  7. Else: run an Implementation session on the first unchecked ROADMAP.md
     item (a role tag in the item text selects the prompt; default impl).
  8. Else: idle (roadmap complete) -> notify once.

Delivery verification: when the loop considers the delivery complete —
once per idle period (per-merge verification is available behind
DISPATCH_VERIFY_PER_MERGE, default off) — a verification session asks:
"Has the project fulfilled its scope and are all the deliverables
completed, and stored correctly?" Gaps are filed as `[verify]` task
issues the loop picks up next tick; a clean pass notifies the owner.
An approved PR whose CI stays red gets one automatic fix session (the
fix re-enters the review pipeline) before the dispatcher escalates to
a blocker.

Cost controls: every session launch checks and increments a shared
daily counter (<projects>/.session-budget.json, one budget for all
repos; DISPATCH_DAILY_SESSIONS, default 14). When it is spent, no
sessions launch for the rest of the day and the owner is notified
once; ticks stay cheap. A session that dies on an API-limit signature
(HTTP 402/429, quota, rate limit, ...) is not a real failure: no
strike, no rework, no blocker — the repo pauses for a cooldown
(state.json `quota_pause_until`, DISPATCH_QUOTA_COOLDOWN_S, default
6h) and retries automatically. Implementation sessions run with
--max-turns 150; qa/adv/verify sessions with DISPATCH_MAX_TURNS_REVIEW
(default 60).

Failure budget: dispatcher/state.json counts consecutive failed sessions
(non-zero exit or timeout) per roadmap item. 2 strikes on the same item ->
the dispatcher files a blocker issue and pauses the repo; the owner's
answer on that issue resumes the loop with a fresh budget.

Sessions run: vibe -p <prompt> --auto-approve --trust --max-turns 150
(impl/arch) or 60 (qa/adv/verify) --max-price $DISPATCH_MAX_PRICE
(default 10) --output json, 90-minute timeout, output captured to
dispatcher/logs/.

Gentle shutdown: on SIGTERM (container stop, reboot) the running session
is terminated, its uncommitted work is stashed, and the loop exits 0.
The stash is flagged in state.json so the next session's prompt points
the agent at it — an interrupted item resumes from the stashed work
instead of starting from scratch.

Usage: python3 dispatcher/dispatch.py [--dry] [--loop]
  --dry prints decisions and exits: no sessions, no issues, no labels,
       no notifications, no state changes.
  --loop repeats ticks until the repo is idle, blocked, or aborted.
"""
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify

try:
    import feedback
except ImportError:  # older engine copies without feedback.py
    feedback = None

try:
    import briefing
except ImportError:  # older engine copies without briefing.py
    briefing = None

REPO = Path(__file__).resolve().parent.parent
ROADMAP = REPO / "ROADMAP.md"
LOGDIR = REPO / "dispatcher" / "logs"
STATE_FILE = REPO / "dispatcher" / "state.json"
LOCK_FILE = REPO / "dispatcher" / ".lock"
PUBLISH_FLAGS = REPO.parent / ".publish-flags"
BUDGET_FILE = REPO.parent / ".session-budget.json"
MAX_TURNS = "150"
MAX_TURNS_REVIEW = os.environ.get("DISPATCH_MAX_TURNS_REVIEW", "60")
DAILY_SESSIONS = int(os.environ.get("DISPATCH_DAILY_SESSIONS", "14"))
QUOTA_COOLDOWN_S = int(os.environ.get("DISPATCH_QUOTA_COOLDOWN_S",
                                      str(6 * 3600)))
# Allowlist: any other value — including the natural way to say "off" —
# must leave the default-off behavior alone.
VERIFY_PER_MERGE = os.environ.get("DISPATCH_VERIFY_PER_MERGE", "").lower() \
    in ("1", "true", "yes")
MAX_PRICE = os.environ.get("DISPATCH_MAX_PRICE", "10")
SESSION_TIMEOUT_S = 5400
RUN_TIMEOUT_S = 3600    # direct script jobs (`run` issues), no LLM session
BLOCKER_REMIND_S = 24 * 3600  # re-notify an unanswered blocker daily
FAIL_STRIKES = 2   # failed sessions per roadmap item before pausing
QA_STRIKES = 2     # QA passes per PR without approval before pausing
LOOP_SETTLE_S = 30  # pause between sessions in --loop mode
LOG_ROTATE_DAYS = 7   # gzip session logs older than this
LOG_DELETE_DAYS = 30  # delete rotated logs older than this
REPO_NAME = "immich-geotag"
ADV_LABEL = "approved-adversarial"

# Only these GitHub authors may steer the loop: open PRs that get reviewed
# and merged, file `run`/`task` issues, or have their comments fed into a
# session prompt or taken as a blocker answer. The repos are public, so any
# GitHub user can open a fork PR or comment; without this gate a stranger's
# PR reached a QA session running tests with --auto-approve on the homelab,
# and a stranger's comment could become "the owner's answer". The owner's
# Telegram replies are posted as the owner, so they pass. Scheduled
# Actions file `run` issues as app/github-actions.
TRUSTED_AUTHORS = {
    a.strip() for a in os.environ.get(
        "DISPATCH_TRUSTED_AUTHORS",
        "pennico,app/github-actions,github-actions").split(",") if a.strip()}

# Appended to every dispatcher-filed blocker: the owner is a non-developer
# reading these on a phone; the answer mechanism must be spelled out.
BLOCKER_HOWTO = ("\n\n---\n\n**How to answer:** comment below, then add the "
                 "`answered` label (gear icon → Labels → answered) — or "
                 "just reply to the Telegram notification. The loop "
                 "resumes within ~5 min of your answer.")

# Appended by run_session() to EVERY session prompt, every role, every
# repo: the one binding point that guarantees spending discipline
# reaches the model even if it never opens the standards doc. Kept
# short on purpose — it is re-sent on every turn of every session.
SPENDING_DISCIPLINE = """

Spending discipline (binding, every session): every turn re-sends this
whole conversation, so anything pulled into it is re-paid on every step.
- Pull small: grep/sed line ranges and head -N budgets; --stat before
  any full diff; never cat a large file whole.
- Web research: fetch truncated (head -c 20000), save the full page to
  a file, quote only the relevant part; one page, then think — do not
  fetch three.
- Externalize: write findings, plans, and status to files (docs,
  notes, STATUS.md) instead of carrying them in the conversation, and
  keep the project's STATUS.md current as you go.
- Re-read nothing this session already read; work from what is in the
  conversation.
- Prefer finishing in few turns over exploring broadly."""

IMPL_PROMPT = """You are the Implementation agent for this repository (unattended headless run).
Read AGENTS.md first, then PLAN.md — it is the complete approved spec.
Follow it literally; do not redesign.

Roadmap item for this session: {item}

Follow the hard constraints and decision defaults in AGENTS.md. Work on a
new branch, commit incrementally, open a draft PR with a Handoff section,
and tick the matching checkbox in ROADMAP.md as part of your PR. If the
work involves a design choice that changes what the results mean (what
is counted, excluded, grouped, or named), ask the owner before building:
file a blocker with the options instead of guessing and building. If you are
blocked (question only the owner can answer, live validation needed, work
outside the build steps), open a GitHub issue labeled `blocker`
(`gh issue create --label blocker`) and stop cleanly. Write blockers in
plain language for a non-developer on a phone: "What I was doing /
What I need from you / the options if any", and end with: "How to
answer: comment here and add the `answered` label, or reply to the
Telegram notification." When done: push the
branch, then `git checkout main` so the working tree is left clean."""

QA_PROMPT = """You are the QA (correctness) agent for this repository (unattended headless run).
Read AGENTS.md first. Open pull requests need correctness review. For each open PR
lacking the `approved` label (`gh pr list`, `gh pr view N`, `gh pr diff N`):
- Check the PR against AGENTS.md (hard constraints, style, decision defaults)
  and PLAN.md's target design. Pay special attention to: apply must stay
  behind the dry-run/CSV review flow, no live API calls in tests, no
  personal data (.env, CSVs, photos) committed.
- Run the project's tests if present; keep total runtime under ~25 minutes.
- Reproduce first: report only problems you can demonstrate (command +
  output in the comment). Speculative concerns go in a clearly marked
  "notes" section, never as findings.
- If the PR is good: `gh pr ready N` (if still a draft), then
  `gh label create approved --color 0E8A16 2>/dev/null || true` and
  `gh pr edit N --add-label approved`.
- If the PR has problems: `gh pr review N --comment --body "..."` with
  concrete, reproduced findings, and do NOT add the label. If the problems
  need an owner decision, say so explicitly in the comment.
Do not merge PRs yourself. Do not modify code. When done: `git checkout main`
so the working tree is left clean."""

ADV_PROMPT = """You are the Adversarial reviewer for this repository (unattended headless run).
Your job is to break the PR, not to admire it. Read AGENTS.md first. For each
open PR that has the `approved` label but lacks `approved-adversarial`
(`gh pr list`, `gh pr view N`, `gh pr diff N`):
- Hunt for what a correctness review plausibly missed: edge cases (empty
  or unicode input, huge values, concurrent runs, missing files), error
  handling and failure paths, security (injection, path traversal, unsafe
  deserialization, secret handling), resource leaks, and violations of the
  AGENTS.md hard constraints.
- Reproduce first: every finding must include the command you ran and its
  actual output. If you cannot reproduce it, it is a "note", not a finding.
- If the PR survives: `gh label create approved-adversarial --color 5319E7
  2>/dev/null || true` and `gh pr edit N --add-label approved-adversarial`.
- If you found reproduced problems: `gh pr review N --comment --body "..."`
  with the reproductions, and do NOT add the label. If the problems need
  an owner decision, say so explicitly in the comment.
Do not merge PRs yourself. Do not modify code. When done: `git checkout main`
so the working tree is left clean."""

REWORK_PROMPT = """You are the Implementation agent for this repository (unattended headless run).
PR #{number} "{title}" failed review. Read AGENTS.md first. Then read the
review findings: `gh pr view {number} --comments` and `gh pr diff {number}`.
- `gh pr checkout {number}` to get the PR branch.
- Reproduce each reported finding locally, fix it, and push to the PR
  branch. Keep the fix minimal and focused on the findings; no drive-by
  refactors.
- Do not remove review labels, do not add labels, do not merge.
When done: push the branch, then `git checkout main` so the working tree is
left clean."""

CI_FIX_PROMPT = """You are the Implementation agent for this repository (unattended headless run).
PR #{number} "{title}" passed both reviews, but its CI checks are failing,
so it cannot merge.
- `gh pr checkout {number}` to get the PR branch.
- Find the failure: `gh run list --branch "$(git branch --show-current)" \
--limit 1` then `gh run view <run-id> --log-failed`; reproduce it locally
with the same commands the workflow runs.
- Fix it with a minimal change pushed to the PR branch. If the code is
  correct but the CI environment lacks something (a system dependency, a
  missing step in the workflow), fix the workflow instead. If the code is
  wrong, fix the code.
- Do not remove review labels, do not merge. Your fix re-enters the
  review pipeline automatically.
When done: push the branch, then `git checkout main` so the working tree is
left clean."""

MAIN_CI_FIX_PROMPT = """You are the Implementation agent for this repository (unattended headless run).
The latest CI run on main ("{title}", run id {run_id}) is failing.
- Find the failure: `gh run view {run_id} --log-failed`; reproduce it
  locally with the same commands the workflow runs.
- Fix it with a minimal change pushed directly to main. If the code is
  correct but the CI environment lacks something (a system dependency,
  a missing step in the workflow), fix the workflow instead. If the code
  is wrong, fix the code.
- Do not open a PR and do not touch open PR branches; fix main only.
When done: push main and leave the working tree clean (`git status`
empty)."""

VERIFY_PROMPT = """You are the Delivery Verification agent for this repository (unattended headless run).
The loop considers the delivery complete: {delivered}.
Answer one question, with evidence:

  Has the project fulfilled its scope, and are all the deliverables
  completed and stored correctly?

Read AGENTS.md first. Establish the scope: ROADMAP.md / plan docs, and
the recently merged PRs and closed issues (`gh pr list --state merged
--limit 5`, `gh issue list --state closed --limit 10`). Then verify —
inspect and run things; never trust PR descriptions:
- Scope: everything promised as delivered actually is; nothing accepted
  is silently missing or half-done.
- Deliverables: the artifacts exist where this project's standards
  require them (source committed, outputs under data/out/ with the
  agreed layout, docs updated in the same change).
- Storage: right place, right names; nothing stranded on unmerged
  branches, stashes, or untracked files (`git status`, `git stash list`,
  `git branch --no-merged main`).
- Publishing to Nextcloud is asynchronous (host watcher, ~5 min after
  the publish flag): check data/out in the repo, not the mirror.

Verdict — exactly one of:
- All complete and stored correctly: take no action (no issue, no
  comment).
- Gaps found: file ONE GitHub issue labeled `task` with a title starting
  with `[verify]`, listing each gap in plain language: what is missing,
  where it should be, and what done looks like. The loop schedules the
  fix automatically.
Do not modify code, do not merge, do not close or label existing issues.
When done: `git checkout main` so the working tree is left clean."""

RESUME_PROMPT = """You are the Implementation agent for this repository (unattended headless run).
The owner has answered blocker issue #{number}: "{title}"
Owner's answer: {answer}

Read AGENTS.md first, then `gh issue view {number}` (and its comments) for
the full context. Address the blocker — it may be a question to incorporate,
a decision, or failed work to retry. Then continue the normal workflow:
follow PLAN.md, work on a new branch, open a draft PR with a Handoff
section, tick the matching ROADMAP.md checkbox if the item completes, and
close issue #{number} once the blocker is resolved. When done: push the
branch, then `git checkout main` so the working tree is left clean."""

TASK_PROMPT = """You are the Implementation agent for this repository (unattended headless run).
The owner has filed issue #{number}: "{title}"

Issue body:
{body}
{comments}
Investigate and resolve it: inspect the code and docs, reproduce or verify
the problem, and fix it. If a fix is not warranted, comment your findings
on issue #{number} and close it instead. Follow the hard constraints and
decision defaults in AGENTS.md. Work on a new branch, commit incrementally,
open a draft PR with a Handoff section referencing issue #{number}, then
close issue #{number} with a short comment linking the PR. If the task
involves a design choice that changes what the results mean (what is
counted, excluded, grouped, or named), ask the owner before building:
comment the options on issue #{number}, add the `blocker` label, and
stop cleanly instead of guessing and building. If you are
blocked (question only the owner can answer), comment on issue #{number}
in plain language for a non-developer on a phone — "What I was doing /
What I need from you / the options if any", ending with: "How to answer:
comment here and add the `answered` label, or reply to the Telegram
notification." — then add the `blocker` label
(`gh issue edit {number} --add-label blocker`),
and stop cleanly. When done: push the branch, then `git checkout main` so
the working tree is left clean."""

PROMPTS = {"impl": IMPL_PROMPT, "qa": QA_PROMPT}

STASH_NOTE = ("\n\nNote: a previous session on this repo was interrupted. Its "
              "uncommitted work, if any, is in `git stash list` (most recent "
              "entry), and it may have left commits on an unpushed local "
              "branch (`git branch --no-merged main`, branches without an "
              "upstream). Inspect both and reuse anything useful before "
              "redoing work from scratch.")

# --- gentle shutdown (SIGTERM) machinery ---
_session_proc = None
_shutdown = {"requested": False}


class Shutdown(Exception):
    """Raised when SIGTERM arrived; main() stashes and exits cleanly."""


def _request_shutdown(signum, frame):
    _shutdown["requested"] = True
    if _session_proc is not None:
        _session_proc.terminate()


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def run(cmd, timeout=120):
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)


def gh_json(cmd, timeout=120):
    r = run(cmd, timeout=timeout)
    if r.returncode != 0:
        log(f"ABORT: {' '.join(cmd[:4])} failed: {r.stderr.strip()[:200]}")
        return None
    return json.loads(r.stdout or "null")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except ValueError:
            log("state.json unreadable; starting fresh")
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def maybe_save(state, dry):
    if not dry:
        save_state(state)


def ensure_labels():
    for name, color in (("approved", "0E8A16"), ("blocker", "D93F0B"),
                        ("answered", "FBCA04"), ("task", "1D76DB"),
                        ("run", "D4C5F9"),
                        ("approved-adversarial", "5319E7")):
        run(["gh", "label", "create", name, "--color", color])


def is_trusted(item):
    """True if a PR/issue/comment (gh JSON) was authored by a trusted login."""
    return (item.get("author") or {}).get("login") in TRUSTED_AUTHORS


def trusted_comments(comments):
    return [c for c in comments or [] if is_trusted(c)]


def open_prs():
    """Open PRs from trusted authors on branches of this repo, never forks."""
    prs = gh_json(["gh", "pr", "list", "--json",
                   "number,title,labels,url,isDraft,author,isCrossRepository"])
    if prs is None:
        return None
    kept = []
    for p in prs:
        if p.get("isCrossRepository") or not is_trusted(p):
            log(f"ignoring PR #{p.get('number')} by "
                f"{(p.get('author') or {}).get('login', '?')}: not a trusted "
                "same-repo PR")
            continue
        kept.append(p)
    return kept


def pr_labels(number):
    data = gh_json(["gh", "pr", "view", str(number), "--json", "labels"])
    if data is None:
        return None
    return {l["name"] for l in data.get("labels", [])}


def pr_files(number):
    data = gh_json(["gh", "pr", "view", str(number), "--json", "files"])
    if data is None:
        return None
    return [f.get("path", "") for f in data.get("files", [])]


def is_engine_sync(number):
    """True for PRs that only sync the dispatcher engine (dispatcher/ files).

    The same code was already reviewed in pennico-ops; running the full
    two-reviewer pipeline with rework rounds per project repo is what
    churned grocery-analysis for nothing on 2026-10-06. Sync PRs get a
    fast lane instead: one QA pass, no adversarial session, one rework
    round before escalating to the owner.
    """
    files = pr_files(number)
    if not files:
        return False
    return all(f.startswith("dispatcher/") for f in files)


def pr_head(number):
    data = gh_json(["gh", "pr", "view", str(number), "--json", "headRefOid"])
    return (data or {}).get("headRefOid")


def checks_state(number):
    """'green', 'pending', or 'failed' for a PR's checks."""
    r = run(["gh", "pr", "checks", str(number)])
    if r.returncode == 0:
        return "green"
    # Repos without CI: "no checks reported" counts as green.
    if "no checks reported" in (r.stderr or ""):
        return "green"
    if "fail" in (r.stdout or "").lower():
        return "failed"
    return "pending"


def merge(number, title, is_draft=False):
    if is_draft:
        ready = run(["gh", "pr", "ready", str(number)])
        if ready.returncode != 0:
            log(f"could not mark PR #{number} ready: {ready.stderr.strip()[:200]}")
            return False
    r = run(["gh", "pr", "merge", str(number), "--squash", "--delete-branch"], timeout=180)
    if r.returncode == 0:
        log(f"merged PR #{number}: {title}")
        return True
    log(f"merge of PR #{number} failed: {r.stderr.strip()[:200]}")
    return False


def blocker_issues():
    """All blocker issues (open and closed); None when gh failed — the
    caller aborts the tick instead of reading a failure as "no blockers"."""
    return gh_json(["gh", "issue", "list", "--label", "blocker", "--state", "all",
                    "--limit", "500",
                    "--json", "number,title,state,labels,url"])

def task_issues():
    return [i for i in gh_json(["gh", "issue", "list", "--label", "task",
                                "--state", "open", "--json",
                                "number,title,author"]) or []
            if is_trusted(i)]



def run_issues():
    """Open issues labeled `run`: direct script jobs, no LLM session."""
    issues = gh_json(["gh", "issue", "list", "--label", "run",
                      "--state", "open",
                      "--json", "number,title,body,labels,author"]) or []
    kept = []
    for i in issues:
        if not is_trusted(i):
            log(f"ignoring run issue #{i.get('number')} by "
                f"{(i.get('author') or {}).get('login', '?')}: untrusted author")
            continue
        kept.append(i)
    return kept


def execute_run_issue(issue, dry):
    """Run the command in a `run`-labeled issue directly — no vibe session.

    GitHub is the only trigger bus: calendar jobs (e.g. the monthly
    geotag backfill) used to be host cron entries; they are now
    scheduled GitHub Actions that file `run` issues, and this executor
    reacts within one tick. The issue body's first non-empty line is the
    command, run in the repo root. The output tail is commented back on
    the issue; success closes it, failure escalates to `blocker`.
    Returns the tick status.
    """
    n = issue["number"]
    body = (issue.get("body") or "").strip()
    cmd = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    if not cmd or cmd.startswith("#"):
        log(f"run issue #{n}: no command in body; closing as invalid")
        if not dry:
            run(["gh", "issue", "comment", str(n), "--body",
                 "No command found: the first non-empty line of the body "
                 "must be the command to run. Closing."])
            run(["gh", "issue", "close", str(n)])
        return "worked"
    log(f"run issue #{n}: {cmd}")
    if dry:
        log("would execute run issue")
        return "worked"
    notify.notify(f"{REPO_NAME}: run job started",
                  f"#{n} {issue['title']}\n\n`{cmd}` — no agent session, "
                  "direct execution. No action needed.", tags="rocket")
    try:
        argv = shlex.split(cmd)
        r = subprocess.run(argv, cwd=REPO, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT_S)
        code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        code, out = 1, str(e)
    tail = "\n".join(out.splitlines()[-40:]) or "(no output)"
    if code == 0:
        run(["gh", "issue", "comment", str(n), "--body",
             f"`{cmd}` finished (exit 0). Output tail:\n\n```\n{tail}\n```"])
        run(["gh", "issue", "close", str(n)])
        notify.notify(f"{REPO_NAME}: run job done",
                      f"#{n} {issue['title']}", tags="ok")
    else:
        run(["gh", "issue", "comment", str(n), "--body",
             f"`{cmd}` failed (exit {code}). Output tail:\n\n```\n{tail}\n```\n\n"
             "The dispatcher paused this repo; comment here and add the "
             "`answered` label to resume."])
        run(["gh", "issue", "edit", str(n), "--add-label", "blocker"])
        state = load_state()
        reblock(state, n)
        if notify.notify(f"{REPO_NAME}: run job failed — needs your attention",
                         f"#{n} {issue['title']}\n\n"
                         "Comment on the issue and add the `answered` label "
                         "(or reply here) to resume.",
                         priority="high", tags="warning"):
            state.setdefault("seen_blockers", []).append(n)
        save_state(state)
    return "worked"


def issue_comments(number):
    data = gh_json(["gh", "issue", "view", str(number), "--json", "comments"])
    return (data or {}).get("comments", [])


def file_blocker(title, body):
    body = body + BLOCKER_HOWTO
    r = run(["gh", "issue", "create", "--label", "blocker",
             "--title", title, "--body", body])
    if r.returncode != 0:
        log(f"ABORT: could not file blocker issue: {r.stderr.strip()[:200]}")
        return None
    m = re.search(r"/issues/(\d+)", r.stdout.strip())
    return int(m.group(1)) if m else None


def remind_blockers(state, issues):
    """Re-notify open, unanswered, already-announced blockers once per
    BLOCKER_REMIND_S: a push lost on the way (phone off, Telegram
    bridge down) must not leave the repo paused silently for days."""
    now = time.time()
    for issue in issues:
        key = str(issue["number"])
        if issue["number"] not in state["seen_blockers"]:
            continue  # not announced yet: the gate retries that itself
        last = state["blocker_reminded"].get(key)
        if last is None:  # announced before reminders existed
            state["blocker_reminded"][key] = now
            continue
        if now - last < BLOCKER_REMIND_S:
            continue
        if notify.notify(f"{REPO_NAME}: still waiting for your answer",
                         f"#{issue['number']} {issue['title']}\n\n"
                         "This repo stays paused until you answer: reply "
                         "here (Telegram) or comment on the issue and add "
                         "the `answered` label.",
                         priority="high", tags="hourglass",
                         url=issue.get("url", "")):
            state["blocker_reminded"][key] = now


def reblock(state, n):
    """An issue that gets the `blocker` label (again): forget that it was
    announced or resumed, so the gate announces it and the owner's next
    `answered` resumes it — a previously resumed number would otherwise
    be skipped by the gate forever."""
    for key in ("seen_blockers", "resumed_blockers"):
        if n in state.get(key, []):
            state[key].remove(n)
    state.get("blocker_reminded", {}).pop(str(n), None)


def first_roadmap_item():
    """Returns (item_text, role) for the first unchecked box, or (None, None)."""
    if not ROADMAP.exists():
        return None, None
    for line in ROADMAP.read_text().splitlines():
        m = re.match(r"\s*- \[ \]\s*(.+)", line)
        if m:
            text = m.group(1).strip()
            role = "impl"
            tag = re.search(r"\[(\w+)\]", text)
            if tag and tag.group(1) in PROMPTS:
                role = tag.group(1)
            return text, role
    return None, None


def interrupted_branches():
    """Unpushed local branches with commits not on main.

    Sessions commit incrementally on a branch and push only at the end,
    so an interrupted session's work usually sits here rather than in the
    working tree.
    """
    r = run(["git", "branch", "--no-merged", "main", "--format",
             "%(refname:short)|%(upstream)"])
    branches = []
    for line in (r.stdout or "").splitlines():
        name, _, upstream = line.partition("|")
        name = name.strip()
        if name and name != "main" and not upstream.strip():
            branches.append(name)
    return branches


def recover_dirty_tree():
    """Preserve work left by an interrupted session (crash or SIGTERM).

    Stashes uncommitted changes and notes unpushed local branches; the
    pending flag in state.json makes the next implementation session's
    prompt point at both. Each branch triggers exactly one notification
    (idle repos re-tick every few minutes; leftovers must not spam).
    Returns True if interrupted work exists.
    """
    stashed = False
    status = run(["git", "status", "--porcelain"])
    if status.stdout.strip():
        r = run(["git", "stash", "-u"])
        if r.returncode != 0:
            log(f"could not stash dirty tree: {r.stderr.strip()[:200]}")
        else:
            log("stashed uncommitted changes from an interrupted session")
            stashed = True
    branches = interrupted_branches()
    state = load_state()
    known = state.setdefault("notified_branches", [])
    new_branches = [b for b in branches if b not in known]
    if new_branches:
        log(f"unpushed branches from an interrupted session: {new_branches}")
    if stashed or new_branches:
        state["stash_note_pending"] = True
        state["notified_branches"] = known + new_branches
        save_state(state)
        notify.notify(f"{REPO_NAME}: recovered interrupted session",
                      "Stashed uncommitted changes and/or found unpushed "
                      "branches from an interrupted session; the next "
                      "session resumes from them. No action needed.",
                      tags="warning")
    elif branches:
        # known leftovers: keep the note flag fresh, stay quiet
        state["stash_note_pending"] = True
        save_state(state)
    return stashed or bool(branches)


class SessionResult:
    """Outcome of one session launch attempt.

    code: the vibe exit code (124 on timeout), None if vibe never started.
    quota: the failure carries an API-limit signature (issue #15) — not a
        real failure; the loop pauses instead of burning failure budgets.
    budget: the shared daily session budget was spent; nothing launched.
    """

    def __init__(self, code, quota=False, budget=False):
        self.code = code
        self.quota = quota
        self.budget = budget


# API-limit signatures in a dying session's error output (case-insensitive).
# A session that exits non-zero carrying one of these hit the Mistral
# subscription limit, not a real failure (issue #15). The numeric codes are
# anchored to an HTTP/status/error prefix or their status text, and words
# that also occur in ordinary failures ("exceeded", "insufficient",
# "credit") only match inside API-limit phrases — a line number, a token
# count or "max turns exceeded" is never a quota death.
QUOTA_SIG = re.compile(
    r"\bhttps?(?:/1\.[01])?\s*[\"']?\s*(402|429)\b"
    r"|\b(?:status|error|exit|response)(?:\s+code)?\s*[\"']?:?\s*(402|429)\b"
    r"|\b(402|429)\s*[-:]\s*(?:too\s+many\s+requests|payment\s+required)\b"
    r"|\b(402|429)\s+(?:too\s+many\s+requests|payment\s+required)\b"
    r"|\bquota\b"
    r"|\brate[ _-]?limits?\b"
    r"|\binsufficient\s+(?:credit|balance|fund)"
    r"|\bpayment\s+required\b"
    r"|\bcredits?\s+(?:exhausted|depleted|ran\s+out)\b"
    r"|\bout\s+of\s+credits?\b"
    r"|\bbalance\s+(?:is\s+)?(?:too\s+low|exhausted)\b"
    r"|\b(?:monthly|daily|weekly|usage|api|request|subscription)\s+limits?\b",
    re.IGNORECASE)

# GitHub's own rate limits (gh CLI, api.github.com) are much shorter-lived
# than the provider quota and must not trigger the 6-hour pause (blocker
# #9, owner option 1): a GitHub rate limit is a normal failure — it
# counts a strike, and two strikes reach the owner through the diagnosis
# issue. Every signature is anchored to a GitHub-specific marker (gh CLI
# error prefix, api.github.com, "secondary rate limit", GitHub's abuse
# detector) so a provider quota death — which never mentions gh/GitHub —
# still pauses. When both signatures appear in the same tail the GitHub
# one wins: the safe direction is a strike (which surfaces to the owner
# after 2 strikes), not a wrong-provider 6-hour pause.
GITHUB_RATE_SIG = re.compile(
    r"\bsecondary\s+rate\s+limit\b"
    r"|\bapi\.github\.com\b[^.\n]{0,120}\brate[ _-]?limits?\b"
    r"|\brate[ _-]?limits?\b[^.\n]{0,120}\bapi\.github\.com\b"
    r"|\bgh\b\s*:[^.\n]{0,120}\brate[ _-]?limits?\b"
    r"|\bgithub\b[^.\n]{0,80}\brate[ _-]?limits?\b"
    r"|\brate[ _-]?limits?\b[^.\n]{0,80}\bgithub\b"
    r"|\babuse\s+detection\s+mechanism\b",
    re.IGNORECASE)

# Only this many final characters of the CLI's error output are scanned
# for a quota signature: the signature must come from the session's dying
# moments (a CLI error, a traceback), not from text that merely quoted
# quota-related content — this file's own source, a plan, a review.
OUTPUT_TAIL_S = 2000


def is_quota_failure(code, output):
    """True when a non-zero session exit carries an API-limit signature.

    Scans the final OUTPUT_TAIL_S characters of the CLI's own error
    output: run_session passes stderr only, never the session transcript
    (stdout) — a session implementing or reviewing the quota feature says
    "quota" constantly, and its transcript must not turn a real crash into
    a quota death. A signature that lands outside stderr falls back to
    normal failure handling (strike + diagnosis), the safe direction.

    A GitHub rate limit is never a quota death (blocker #9): the agent
    sessions run gh constantly and GitHub's limits clear in minutes, so
    they count a normal strike instead of pausing the repo for hours.
    """
    if code in (0, 124, None):
        return False
    tail = output[-OUTPUT_TAIL_S:]
    if GITHUB_RATE_SIG.search(tail):
        return False
    return bool(QUOTA_SIG.search(tail))


def budget_read():
    """(today, entry) from the shared counter; corrupt/missing -> fresh.

    The counter file is shared by all repos' dispatchers (one subscription,
    one budget) and lives outside the repos, next to .publish-flags. A
    missing or unreadable file must never block a tick: start at zero.
    A valid-JSON file whose `sessions` value has the wrong type (null,
    object, ...) is the same corruption: reset it to 0 here so neither
    budget_spent() nor budget_reserve() can crash on it.
    """
    today = time.strftime("%Y-%m-%d")
    try:
        data = json.loads(BUDGET_FILE.read_text())
        if not isinstance(data, dict):
            raise ValueError("not an object")
        entry = data.get(today)
        if not isinstance(entry, dict):
            entry = {}
    except FileNotFoundError:
        # Loud on purpose (issue #21): a counter that vanishes mid-day
        # restarts the shared budget at zero and unmeters every repo.
        # On 2026-10-06 agent sessions deleted the live file twice,
        # silently. Missing is only normal before the first launch
        # ever; the first budget_reserve() recreates the file, so this
        # logs at most once per deletion episode.
        log("session budget counter MISSING; restarting the day at "
            f"zero sessions — if this was not just created, find what "
            f"deletes {BUDGET_FILE}")
        entry = {}
    except (OSError, ValueError):
        log("session budget counter unreadable (corrupt); recreating "
            "today at zero sessions")
        entry = {}
    sessions = entry.get("sessions")
    if not isinstance(sessions, int) or isinstance(sessions, bool):
        entry["sessions"] = 0
    return today, entry


def budget_spent():
    """True when today's shared session count reached the daily limit.

    Cheap unlocked read for the tick gate; the authoritative check is
    the locked budget_reserve() at launch time.
    """
    if DAILY_SESSIONS <= 0:
        return False
    _, entry = budget_read()
    return int(entry.get("sessions", 0)) >= DAILY_SESSIONS


def _budget_write(today, entry):
    """Atomically replace the counter file (callers hold its lock).

    write_text truncates in place, so a concurrent unlocked reader
    (budget_spent on every tick) can catch a half-written file, treat
    it as corrupt, and silently restart the day at zero. Write to a
    temp file and os.replace() it: readers see the old or the new
    content, never a partial file.
    """
    tmp = BUDGET_FILE.with_name(BUDGET_FILE.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump({today: entry}, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, BUDGET_FILE)


def budget_reserve():
    """Check the shared daily budget and count one launch, atomically.

    Returns True when the session may launch (its slot counted), False
    when today's budget is spent. The whole check-count-write runs
    under an exclusive flock on a lock file next to the counter: the
    counter is shared by all repos' dispatchers, and an unlocked
    read-modify-write loses updates — concurrent repos could each pass
    the check and overshoot the cap, and the one-time `notified` flag
    can be lost. A missing or unwritable counter never blocks a tick:
    fail open, like budget_read.
    """
    if DAILY_SESSIONS <= 0:
        return True
    just_spent = False
    try:
        BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BUDGET_FILE.parent / (BUDGET_FILE.name + ".lock"),
                  "w") as lockfh:
            fcntl.flock(lockfh, fcntl.LOCK_EX)
            try:
                today, entry = budget_read()
                if int(entry.get("sessions", 0)) >= DAILY_SESSIONS:
                    return False
                entry["sessions"] = int(entry.get("sessions", 0)) + 1
                just_spent = (entry["sessions"] >= DAILY_SESSIONS
                              and not entry.get("notified"))
                if just_spent:
                    entry["notified"] = True
                _budget_write(today, entry)
            finally:
                fcntl.flock(lockfh, fcntl.LOCK_UN)
    except OSError as e:
        log(f"could not update session budget: {e}")
        return True
    if just_spent:
        notify.notify("agent loop: daily session budget spent",
                      f"{entry['sessions']}/{DAILY_SESSIONS} headless "
                      "sessions have run today across all repos (the "
                      "budget is shared). No more sessions launch today; "
                      "the loop resumes tomorrow. No action needed — tune "
                      "DISPATCH_DAILY_SESSIONS if this is too tight.",
                      tags="warning")
    return True


def budget_release():
    """Give back a slot taken by budget_reserve() when the launch never
    started (vibe missing): a launch that never runs must not burn the
    shared daily budget — otherwise every tick spends one more slot and
    the "budget spent" notification masks the real failure. Same lock
    as budget_reserve(); a stale `notified` flag is cleared when the
    count drops back below the cap so a real crossing still notifies.
    """
    if DAILY_SESSIONS <= 0:
        return
    try:
        BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(BUDGET_FILE.parent / (BUDGET_FILE.name + ".lock"),
                  "w") as lockfh:
            fcntl.flock(lockfh, fcntl.LOCK_EX)
            try:
                today, entry = budget_read()
                entry["sessions"] = max(0, entry.get("sessions", 0) - 1)
                if entry["sessions"] < DAILY_SESSIONS:
                    entry.pop("notified", None)
                _budget_write(today, entry)
            finally:
                fcntl.flock(lockfh, fcntl.LOCK_UN)
    except OSError as e:
        log(f"could not update session budget: {e}")


def enter_quota_pause(state):
    """Set the API-quota cooldown in state.json; notify the owner once."""
    if state.get("quota_pause_until", 0) > time.time():
        return  # already paused: one notification per episode
    until = time.time() + QUOTA_COOLDOWN_S
    state["quota_pause_until"] = until
    resume = datetime.fromtimestamp(until).strftime("%H:%M")
    log(f"API quota/limit exhausted; sessions paused until {resume}")
    notify.notify(f"{REPO_NAME}: paused — API quota/limit exhausted",
                  f"A session died on an API limit (HTTP 402/429 or "
                  f"similar). No sessions launch until {resume}, then the "
                  "loop retries automatically. No action needed.",
                  tags="warning")


def pause_for_quota_or_budget(state, res, dry):
    """A session could not run (API-limit signature or spent daily
    budget): pause the repo without counting a failure. Returns
    'blocked' — the next ticks stay cheap until the cooldown expires or
    the day rolls over."""
    if res.quota:
        enter_quota_pause(state)
    maybe_save(state, dry)
    return "blocked"


def rotate_logs():
    """Gzip session logs older than LOG_ROTATE_DAYS; delete older than
    LOG_DELETE_DAYS. Best-effort: never raises into the tick.

    Session transcripts are large (up to ~2 MB each) and accumulate
    fast on busy days; the logs dir reached ~55 MB across four repos
    within a week of operation.
    """
    now = time.time()
    rotate_after = LOG_ROTATE_DAYS * 86400
    delete_after = LOG_DELETE_DAYS * 86400
    try:
        for f in LOGDIR.iterdir():
            age = now - f.stat().st_mtime
            if f.suffix == ".gz":
                if age > delete_after:
                    f.unlink()
                continue
            if age > rotate_after:
                import gzip
                with gzip.open(f.with_suffix(f.suffix + ".gz"), "wb") as out:
                    out.write(f.read_bytes())
                f.unlink()
    except OSError as e:
        log(f"log rotation skipped: {e}")


def run_session(role, prompt):
    """Launch one vibe session; returns a SessionResult, None if vibe is
    missing. Raises Shutdown if SIGTERM arrived (before or during the
    session).

    The shared daily budget is checked and counted here — the one
    choke point every role and repo goes through, atomically under the
    shared counter's lock. A reserved slot whose launch never started
    (vibe missing) is given back: a launch that never runs must not
    burn the shared daily budget. Reviews and verifications run with a
    lower turn cap than implementation sessions (their conversations
    rarely need the full tail).
    """
    global _session_proc
    if _shutdown["requested"]:
        raise Shutdown()
    if not budget_reserve():
        log(f"daily session budget spent; not launching {role} session")
        return SessionResult(None, budget=True)
    LOGDIR.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    logfile = LOGDIR / f"{ts}-{role}.json"
    log(f"starting {role} session (log: {logfile.name})")
    max_turns = MAX_TURNS if role in ("impl", "arch") else MAX_TURNS_REVIEW
    # The discipline preamble rides along on every launch: the binding
    # point that reaches the model without depending on it reading
    # AGENTS.md or the standards doc first.
    import memory  # local import: dispatcher dir is on sys.path; best-effort
    scope = "ops." + REPO_NAME
    cmd = ["vibe", "-p", prompt + SPENDING_DISCIPLINE + memory.block(scope),
           "--auto-approve", "--trust",
           "--max-turns", max_turns, "--max-price", MAX_PRICE,
           "--output", "json"]
    try:
        proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    except OSError as e:
        # missing, not executable (broken update, noexec mount), ... —
        # the launch never happened, so give the reserved unit back or
        # every occurrence permanently shrinks the shared daily budget.
        log(f"ABORT: could not launch vibe CLI: {e}")
        budget_release()
        return None
    _session_proc = proc
    try:
        stdout, stderr = proc.communicate(timeout=SESSION_TIMEOUT_S)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        (LOGDIR / f"{ts}-{role}.exit").write_text("timeout\n")
        log(f"{role} session timed out after {SESSION_TIMEOUT_S}s")
        return SessionResult(124)
    finally:
        _session_proc = None
    logfile.write_text(stdout or "")
    if code != 0 and (stderr or "").strip():
        (LOGDIR / f"{ts}-{role}.err").write_text(stderr)
    (LOGDIR / f"{ts}-{role}.exit").write_text(str(code) + "\n")
    # Quota classification reads the CLI's own error output (stderr) only:
    # the transcript (stdout) routinely discusses quotas/rate limits and
    # must not mask a real crash as an API-limit death.
    quota = is_quota_failure(code, stderr or "")
    log(f"{role} session finished, exit {code}"
        + (" (API quota/limit signature)" if quota else ""))
    memory.save(scope, "session", f"{role} exit {code}"
                + (" (quota)" if quota else ""))
    if _shutdown["requested"]:
        raise Shutdown()
    return SessionResult(code, quota=quota)


def request_publish():
    """Ask the host-side watcher to publish this repo's outputs.

    Drops a flag file in <projects>/.publish-flags/ (outside the repo, on
    the bind mount); the watcher cron runs publish_<repo>_outputs.sh and
    notifies the owner. Best-effort: never fails a tick.
    """
    try:
        PUBLISH_FLAGS.mkdir(exist_ok=True)
        (PUBLISH_FLAGS / REPO_NAME).touch()
    except OSError as e:
        log(f"could not request publish: {e}")


def report_session_failure(source, title):
    """Feed a failed session back into the loop as a diagnosis task issue.

    First failure of a signature files a `task` issue (an agent session
    picks it up next tick); recurrences comment on it and eventually
    escalate to `blocker`. No-op when feedback.py is not present.
    """
    if feedback is None:
        return
    exits = sorted(LOGDIR.glob(f"*-{source}.exit"))
    tail = ""
    errfile = LOGDIR / (exits[-1].stem + ".err") if exits else None
    if errfile is not None and errfile.exists():
        tail = "\n".join(errfile.read_text(errors="replace").splitlines()[-40:])
    elif exits:
        logfile = LOGDIR / (exits[-1].stem + ".json")
        if logfile.exists():
            tail = "\n".join(logfile.read_text(errors="replace").splitlines()[-5:])
    feedback.report(f"session:{REPO_NAME}:{source}", REPO_NAME,
                   f"{source} session failed: {title}", tail or "(no output)")


def verify_delivery(delivered, dry, state):
    """Run the delivery-verification session and report the verdict.

    Asks the verification question ("has the project fulfilled its scope
    and are all deliverables completed and stored correctly?") as a
    session. The session files a `[verify]` task issue for gaps (the
    loop picks it up next tick); silence means pass. Never blocks the
    tick: a failed session is fed to the feedback loop, not the budget.

    Returns True when a session actually ran (a verdict was attempted),
    False when it could not run (API quota, spent budget, missing vibe)
    — the caller keeps its verification flag so it retries later.
    """
    if dry:
        log(f"would run delivery verification: {delivered}")
        return True
    before = {i["number"] for i in task_issues()}
    res = run_session("verify", VERIFY_PROMPT.format(delivered=delivered))
    if res is None:
        log("verification session could not start (vibe missing)")
        return False
    if res.quota or res.budget:
        if res.quota:
            enter_quota_pause(state)
        return False
    if res.code != 0:
        log(f"verification session failed (exit {res.code}); no verdict")
        report_session_failure("verify", delivered)
        return True
    gaps = [i for i in task_issues() if i["number"] not in before]
    if gaps:
        log(f"delivery verification found gaps: {[i['title'] for i in gaps]}")
        notify.notify(f"{REPO_NAME}: delivery check found gaps",
                      "\n\n".join(i["title"] for i in gaps) +
                      "\n\nA fix session was queued automatically; "
                      "no action needed.",
                      tags="warning")
    else:
        log("delivery verification passed")
        notify.notify(f"{REPO_NAME}: delivery verified",
                      "Scope fulfilled; deliverables complete and stored "
                      "correctly. No action needed.",
                      tags="ok")
    return True


def tick(dry):
    """One pass over the state machine.

    Returns 'worked' (did something; loop again), 'idle' (roadmap complete),
    'blocked' (waiting for the owner), 'waiting' (approved PR, checks not
    green yet), or 'abort'.
    """
    if dry:
        if run(["git", "status", "--porcelain"]).stdout.strip():
            log("ABORT: working tree not clean")
            return "abort"
    else:
        recover_dirty_tree()  # stash + unpushed-branch check; no-op when clean
    run(["git", "checkout", "main"])
    pull = run(["git", "pull", "--ff-only"])
    if pull.returncode != 0:
        log(f"ABORT: pull failed: {pull.stderr.strip()[:200]}")
        return "abort"

    state = load_state()
    for key, default in (("failures", {}), ("qa_strikes", {}),
                         ("seen_blockers", []), ("resumed_blockers", []),
                         ("seen_prs", []), ("approved_prs", []),
                         ("launched_tasks", []), ("merge_failed_prs", []),
                         ("ci_fix_tried", []), ("ci_escalated", []),
                         ("main_ci_fixed_runs", []), ("main_ci_strikes", 0),
                         ("main_ci_escalated_runs", []),
                         ("verified_merges", []), ("verified_idle", False),
                         ("notified_done", False),
                         ("blocker_reminded", {})):
        state.setdefault(key, default)

    if not dry:
        ensure_labels()

    # --- blocker issues: the human-in-the-loop channel ---
    blockers = blocker_issues()
    if blockers is None:
        return "abort"
    for issue in blockers:
        if issue["number"] not in state["seen_blockers"]:
            log(f"blocker issue #{issue['number']} ({issue['state'].lower()}): {issue['title']}")
            if dry:
                continue
            # Seen only once the owner was actually told: a failed send
            # (ntfy down) is retried next tick instead of being lost.
            sent = True
            if issue["state"] == "OPEN":
                sent = notify.notify(f"{REPO_NAME}: needs your answer",
                                     f"#{issue['number']} {issue['title']}\n\n"
                                     "Reply to this message (Telegram) or comment "
                                     "on the issue and add the `answered` label — "
                                     "the loop resumes within ~5 min.",
                                     priority="high", tags="warning", url=issue.get("url"))
            if sent:
                state["seen_blockers"].append(issue["number"])
                state["blocker_reminded"][str(issue["number"])] = time.time()
    # --- run issues: direct script jobs, no LLM session ---
    # Before the blocker gate and the session pauses: calendar jobs
    # (monthly backfills) must fire on schedule even while the repo
    # waits on an unanswered blocker — they run no agent session and
    # carry their own budgets. A failed run issue already carries the
    # `blocker` label (added by execute_run_issue) and is skipped here
    # so it is not retried every tick.
    for issue in run_issues():
        if "blocker" in {l["name"] for l in issue.get("labels", [])}:
            continue
        maybe_save(state, dry)
        return execute_run_issue(issue, dry)
    actionable, open_unanswered = [], []
    for issue in blockers:
        labels = {l["name"] for l in issue["labels"]}
        if issue["number"] in state["resumed_blockers"]:
            continue
        if "answered" in labels or issue["state"] == "CLOSED":
            actionable.append(issue)
        elif issue["state"] == "OPEN":
            open_unanswered.append(issue)
    if open_unanswered:
        log(f"blocked: {len(open_unanswered)} unanswered blocker issue(s); "
            "waiting for the owner")
        if not dry:
            remind_blockers(state, open_unanswered)
        maybe_save(state, dry)
        return "blocked"
    # --- session-launch pauses: API-quota cooldown, daily budget ---
    # While either is active no session launches at all; the tick stays
    # cheap (git pull + gh queries + blocker notifications only).
    if state.get("quota_pause_until", 0) > time.time() or budget_spent():
        reason = ("API quota cooldown" if state.get("quota_pause_until", 0)
                  > time.time() else "daily session budget spent")
        log(f"sessions paused ({reason}); cheap tick, no launches")
        maybe_save(state, dry)
        return "blocked"
    if actionable:
        issue = actionable[0]
        if dry:
            log(f"would resume after blocker #{issue['number']} was answered")
            return "worked"
        comments = trusted_comments(issue_comments(issue["number"]))
        answer = (comments[-1].get("body", "").strip() if comments
                  else "The owner closed the issue without a comment; "
                       "use your best judgment.")
        log(f"blocker #{issue['number']} answered; resuming")
        run(["gh", "issue", "edit", str(issue["number"]), "--remove-label", "answered"])
        state["resumed_blockers"].append(issue["number"])
        state["verified_idle"] = False  # new work: re-verify before idle again
        state["failures"] = {}  # fresh failure budget after owner input
        state["ci_fix_tried"] = []     # fresh CI-fix budget too
        state["ci_escalated"] = []
        state["main_ci_strikes"] = 0   # fresh main-CI budget as well
        maybe_save(state, dry)
        note = STASH_NOTE if state.get("stash_note_pending") else ""
        res = run_session("impl", RESUME_PROMPT.format(
            number=issue["number"], title=issue["title"], answer=answer) + note)
        if res is None:
            return "abort"
        if res.quota or res.budget:
            return pause_for_quota_or_budget(state, res, dry)
        if res.code == 0:
            state.pop("stash_note_pending", None)
            request_publish()
            maybe_save(state, dry)
        return "worked"

    # --- main-branch CI: self-fix red runs before they reach the owner ---
    # Direct pushes and squash-merges land on main without a PR gate,
    # so a red main is only noticed via GitHub's failure emails. The
    # loop checks the latest run each tick: one fix session per failed
    # run, FAIL_STRIKES consecutive red fixes -> blocker + pause.
    runs = gh_json(["gh", "run", "list", "--branch", "main", "--limit", "1",
                    "--json", "databaseId,status,conclusion,displayTitle"])
    if runs:
        r = runs[0]
        if r.get("status") == "completed" and r.get("conclusion") == "success":
            state["main_ci_strikes"] = 0
        elif (r.get("status") == "completed"
              and r.get("conclusion") == "failure"
              and r["databaseId"] not in state["main_ci_fixed_runs"]):
            rid = r["databaseId"]
            if state["main_ci_strikes"] >= FAIL_STRIKES:
                if rid not in state["main_ci_escalated_runs"]:
                    state["main_ci_escalated_runs"].append(rid)
                    log(f"main CI run {rid} still red after "
                        f"{FAIL_STRIKES} fix attempts; escalating")
                    if dry:
                        log("would file blocker: CI keeps failing on main")
                    else:
                        b = file_blocker(
                            f"CI keeps failing on main: "
                            f"{r.get('displayTitle', 'latest run')}",
                            "The latest commits on main keep failing their "
                            "automated checks (CI) even after automatic "
                            "fix attempts (see this repo's Actions tab). "
                            "The dispatcher paused this repo so nothing "
                            "else runs on a broken main. Please take a "
                            "look, then comment here and add the "
                            "`answered` label to resume.")
                        if b:
                            if notify.notify(
                                f"{REPO_NAME}: paused — CI keeps failing on main",
                                f"#{b} {r.get('displayTitle', '')}\n\n"
                                "Automatic fixes did not clear it. Comment "
                                "on the issue and add the `answered` "
                                "label (or reply here) to resume.",
                                priority="high", tags="warning"):
                                state["seen_blockers"].append(b)
                    maybe_save(state, dry)
            else:
                state["main_ci_fixed_runs"].append(rid)
                state["main_ci_strikes"] += 1
                log(f"main CI run {rid} red; launching fix session "
                    f"(strike {state['main_ci_strikes']}/{FAIL_STRIKES})")
                maybe_save(state, dry)
                if dry:
                    log("would run main-CI fix session")
                else:
                    notify.notify(f"{REPO_NAME}: CI red on main — auto-fix running",
                                  f"{r.get('displayTitle', '')}\n\n"
                                  "The dispatcher is trying to fix it; "
                                  "you'll be notified again only if it "
                                  "cannot.", tags="warning")
                    res = run_session("impl", MAIN_CI_FIX_PROMPT.format(
                        run_id=rid, title=r.get("displayTitle", "")))
                    if res is None:
                        return "abort"
                    if res.quota or res.budget:
                        # the fix session never ran; do not burn a strike
                        state["main_ci_fixed_runs"].remove(rid)
                        state["main_ci_strikes"] -= 1
                        return pause_for_quota_or_budget(state, res, dry)
                    if res.code == 0:
                        request_publish()
                    maybe_save(state, dry)
                    return "worked"

    # --- task issues: owner-filed work orders (launch from the phone) ---
    tasks = gh_json(["gh", "issue", "list", "--label", "task",
                     "--state", "open", "--json", "number,title,url,author"])
    if tasks is None:
        return "abort"
    for issue in (t for t in tasks if is_trusted(t)):
        if issue["number"] in state["launched_tasks"]:
            continue
        n = issue["number"]
        if dry:
            log(f"would run task session for issue #{n}: {issue['title']}")
            return "worked"
        data = gh_json(["gh", "issue", "view", str(n),
                        "--json", "body,comments"]) or {}
        body = (data.get("body") or "").strip() or "(no body)"
        comments = trusted_comments(data.get("comments"))
        ctext = ""
        if comments:
            ctext = "\nIssue comments:\n" + "\n".join(
                f"- {c.get('author', {}).get('login', '?')}: "
                f"{(c.get('body') or '').strip()}" for c in comments)
        state["launched_tasks"].append(n)
        state["verified_idle"] = False  # new work: re-verify before idle again
        maybe_save(state, dry)
        log(f"task issue #{n}: {issue['title']}")
        notify.notify(f"{REPO_NAME}: task launched",
                      f"#{n} {issue['title']}\n\n"
                      "Implementation session running (up to 90 min). "
                      "No action needed — next update when a PR opens.",
                      tags="rocket", url=issue.get("url"))
        note = STASH_NOTE if state.get("stash_note_pending") else ""
        res = run_session("impl", TASK_PROMPT.format(
            number=n, title=issue["title"], body=body, comments=ctext) + note)
        if res is None:
            return "abort"
        if res.quota or res.budget:
            state["launched_tasks"].remove(n)  # retry after the pause
            return pause_for_quota_or_budget(state, res, dry)
        key = f"task #{n}"
        if res.code == 0:
            state["failures"].pop(key, None)
            state.pop("stash_note_pending", None)
            request_publish()
        else:
            fails = state["failures"].get(key, 0) + 1
            state["failures"][key] = fails
            log(f"task session failed (exit {res.code}); failures: {fails}")
            if fails < FAIL_STRIKES:
                # Feed the failure back: a diagnosis task issue gives the
                # loop one self-fix attempt before human escalation.
                report_session_failure("impl", issue["title"])
            if fails >= FAIL_STRIKES:
                run(["gh", "issue", "comment", str(n), "--body",
                     "The dispatcher could not complete this task in two "
                     "sessions (see dispatcher/logs/). Pausing this repo; "
                     "comment here and add the `answered` label to resume."])
                run(["gh", "issue", "edit", str(n), "--add-label", "blocker"])
                reblock(state, n)
                if notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                 f"#{n} {issue['title']}\n\n"
                                 "Two sessions failed on this task. Comment on "
                                 "the issue and add the `answered` label (or "
                                 "reply here) to resume.",
                                 priority="high", tags="warning", url=issue.get("url")):
                    state["seen_blockers"].append(n)
                maybe_save(state, dry)
                return "blocked"
        maybe_save(state, dry)
        return "worked"

    # --- pull requests ---
    prs = open_prs()
    if prs is None:
        return "abort"
    if briefing:
        briefing.cleanup_briefings([p["number"] for p in prs])
    for p in prs:
        if p["number"] not in state["seen_prs"]:
            state["seen_prs"].append(p["number"])
            state["verified_idle"] = False  # new work arrived
            log(f"new PR #{p['number']}: {p['title']}")
            if not dry:
                notify.notify(f"{REPO_NAME}: PR opened",
                              f"#{p['number']} {p['title']}\n\n"
                              "No action needed — QA and adversarial "
                              "reviews run automatically; you'll be "
                              "notified when it merges.",
                              tags="envelope", url=p.get("url"))
    if prs:
        # Merge PRs with BOTH review labels + green checks first (cheap gh
        # calls, no session), so ready work is not held up by PRs still
        # under review.
        waiting = False
        for p in prs:
            labels = {l["name"] for l in p["labels"]}
            if "approved" not in labels or ADV_LABEL not in labels:
                continue
            if p["number"] not in state["approved_prs"]:
                state["approved_prs"].append(p["number"])
                log(f"PR #{p['number']} approved by both reviewers")
                if not dry:
                    notify.notify(f"{REPO_NAME}: PR approved",
                                  f"#{p['number']} {p['title']}\n\n"
                                  "Both reviews passed; merging as soon as "
                                  "CI is green. No action needed.",
                                  tags="ok", url=p.get("url"))
            state_ = checks_state(p["number"])
            if state_ == "failed":
                if p["number"] not in state.get("ci_failed_prs", []):
                    state.setdefault("ci_failed_prs", []).append(p["number"])
                    if not dry:
                        notify.notify(f"{REPO_NAME}: CI failed on approved PR",
                                      f"#{p['number']} {p['title']}\n\n"
                                      "CI is red, so the merge is on hold. "
                                      "The loop will try to fix it "
                                      "automatically; you'll be notified "
                                      "if it needs you.",
                                      priority="high", tags="warning",
                                      url=p.get("url"))
                if p["number"] not in state["ci_fix_tried"]:
                    # One self-fix attempt before human escalation.
                    state["ci_fix_tried"].append(p["number"])
                    log(f"PR #{p['number']}: CI red after approval; "
                        "launching fix session")
                    maybe_save(state, dry)
                    if dry:
                        log(f"would run CI fix session for PR #{p['number']}")
                    else:
                        ci_note = (briefing.prepare_briefings([p["number"]])
                                   if briefing else "")
                        res = run_session("impl", CI_FIX_PROMPT.format(
                            number=p["number"], title=p["title"]) + ci_note)
                        if res is not None and (res.quota or res.budget):
                            # the fix session never ran; do not burn the
                            # one-shot budget or invalidate approvals
                            state["ci_fix_tried"].remove(p["number"])
                            return pause_for_quota_or_budget(state, res, dry)
                        # The fix pushed new code: re-verify correctness
                        # before it can merge (same rule as rework).
                        run(["gh", "pr", "edit", str(p["number"]),
                             "--remove-label", "approved"])
                elif p["number"] not in state["ci_escalated"]:
                    state["ci_escalated"].append(p["number"])
                    log(f"PR #{p['number']}: CI still red after fix attempt; "
                        "escalating to the owner")
                    if dry:
                        log(f"would file blocker: CI keeps failing on PR #{p['number']}")
                    else:
                        b = file_blocker(
                            f"CI keeps failing on approved PR #{p['number']}: {p['title']}",
                            f"PR #{p['number']} passed both reviews, but its "
                            "automated checks (CI) keep failing even after "
                            "an automatic fix attempt (see the PR's Checks "
                            "tab, link below). The dispatcher paused this "
                            "repo so nothing merges broken. Please take a "
                            "look at the PR, then comment here and add the "
                            "`answered` label to resume.")
                        if b:
                            if notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                             f"#{b} CI keeps failing on PR #{p['number']}\n\n"
                                             "The loop could not fix it automatically. "
                                             "Comment on the issue and add the "
                                             "`answered` label (or reply here) "
                                             "to resume.",
                                             priority="high", tags="warning",
                                             url=p.get("url")):
                                state["seen_blockers"].append(b)
                waiting = True
                continue
            if state_ != "green":
                log(f"PR #{p['number']} approved but checks pending; waiting")
                waiting = True
                continue
            log(f"PR #{p['number']} approved + green: merge")
            if dry:
                continue
            if merge(p["number"], p["title"], p.get("isDraft", False)):
                request_publish()
                state["qa_strikes"].pop(str(p["number"]), None)
                state["approved_prs"] = [n for n in state["approved_prs"]
                                         if n != p["number"]]
                state["ci_failed_prs"] = [n for n in state.get("ci_failed_prs", [])
                                          if n != p["number"]]
                notify.notify(f"{REPO_NAME}: PR merged",
                              f"#{p['number']} {p['title']}\n\n"
                              "Outputs will be published to Nextcloud "
                              "Documents/04_projects/ within ~5 min. "
                              "No action needed.",
                              tags="white_check_mark", url=p.get("url"))
                # Merged work: re-verify before the repo goes idle again.
                state["verified_idle"] = False
                # Per-merge verification is off by default (cost control,
                # issue #16): the once-per-idle verification covers it.
                # Set DISPATCH_VERIFY_PER_MERGE=1 to restore it.
                if VERIFY_PER_MERGE and p["number"] not in state["verified_merges"]:
                    state["verified_merges"].append(p["number"])
                    maybe_save(state, dry)
                    verify_delivery(
                        f"PR #{p['number']} ({p['title']}) just merged to main",
                        dry, state)
            else:
                waiting = True
                if p["number"] not in state["merge_failed_prs"]:
                    state["merge_failed_prs"].append(p["number"])
                    notify.notify(f"{REPO_NAME}: merge failed",
                                  f"#{p['number']} {p['title']}\n\n"
                                  "Merge error — the loop retries on its "
                                  "next tick; if this repeats, check the PR.",
                                  priority="high", tags="warning", url=p.get("url"))
        # Two-reviewer pipeline: one review session per tick. Correctness
        # (QA) first; adversarial only after correctness approved.
        in_review = [p for p in prs
                     if "approved" not in {l["name"] for l in p["labels"]}
                     or ADV_LABEL not in {l["name"] for l in p["labels"]}]
        if in_review:
            corr = [p for p in in_review
                    if "approved" not in {l["name"] for l in p["labels"]}]
            if corr:
                role, prompt, label = "qa", QA_PROMPT, "approved"
                targets = corr
            else:
                role, prompt, label = "adv", ADV_PROMPT, ADV_LABEL
                # Fast lane: engine syncs carry code already adversarially
                # reviewed in pennico-ops — no second review session per
                # project repo, just the label so the PR can merge.
                auto = [p for p in in_review if is_engine_sync(p["number"])]
                for p in auto:
                    log(f"PR #{p['number']}: engine sync — fast lane, "
                        "no adversarial session")
                    if not dry:
                        run(["gh", "pr", "edit", str(p["number"]),
                             "--add-label", ADV_LABEL])
                targets = [p for p in in_review if p not in auto]
                if not targets:
                    maybe_save(state, dry)
                    return "worked"
            log(f"PRs needing {role} review: {[p['number'] for p in targets]}")
            if dry:
                maybe_save(state, dry)
                return "worked"
            maybe_save(state, dry)
            review_note = briefing.prepare_briefings(
                [p["number"] for p in targets]) if briefing else ""
            res = run_session(role, prompt + review_note)
            if res is None:
                return "abort"
            if res.quota or res.budget:
                # a quota death is not a failed review: no strikes, no
                # rework, no blocker — pause and retry after the cooldown
                return pause_for_quota_or_budget(state, res, dry)
            pause = False
            for p in targets:
                n = p["number"]
                after = pr_labels(n)
                if after is None:
                    return "abort"
                if label in after:
                    # A passed review does NOT clear this PR's strike
                    # count: strikes accumulate across both reviewers
                    # until the PR merges (only the merge pops them).
                    # Resetting on a pass let the qa-approves /
                    # adversarial-fails alternation loop forever past
                    # the cap (2026-10-06, issue #21).
                    if label == "approved" and is_engine_sync(n):
                        log(f"PR #{n}: engine sync passed QA — fast lane to merge")
                        run(["gh", "pr", "edit", str(n), "--add-label", ADV_LABEL])
                    continue
                strikes = state["qa_strikes"].get(str(n), 0) + 1
                state["qa_strikes"][str(n)] = strikes
                cap = 1 if is_engine_sync(n) else QA_STRIKES
                if strikes > cap:
                    log(f"PR #{n} failed review after rework rounds; pausing")
                    b = file_blocker(
                        f"PR #{n} failed review after rework rounds: {p['title']}",
                        f"This PR failed {cap} review round(s) (label "
                        f"`{label}` not added; see the PR review comments), "
                        "each followed by a rework session. The dispatcher "
                        "paused this repo. Please take a look, then comment "
                        "here and add the `answered` label to resume.")
                    if b:
                        if notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                         f"#{b} {p['title']}\n\n"
                                         f"PR #{n} failed {QA_STRIKES} review "
                                         "rounds. Comment on the issue and add "
                                         "the `answered` label (or reply here) "
                                         "to resume.",
                                         priority="high", tags="warning", url=p.get("url")):
                            state["seen_blockers"].append(b)
                    pause = True
                    continue
                log(f"PR #{n} failed {role} review (strike {strikes}); launching rework")
                head_before = pr_head(n)
                rework_note = (briefing.prepare_briefings([n])
                               if briefing else "")
                res = run_session("impl", REWORK_PROMPT.format(
                    number=n, title=p["title"]) + rework_note)
                if res is not None and (res.quota or res.budget):
                    # rework could not run: undo this round's strike (the
                    # round restarts after the pause) and stop the tick
                    state["qa_strikes"][str(n)] = strikes - 1
                    return pause_for_quota_or_budget(state, res, dry)
                if (res is not None and res.code == 0
                        and pr_head(n) == head_before):
                    # The rework session finished but pushed nothing:
                    # another round would repeat the same review on the
                    # same code — the loop is going in circles (this is
                    # the "running for nothing" pattern from 2026-10-06).
                    log(f"PR #{n}: rework made no progress; escalating")
                    b = file_blocker(
                        f"PR #{n} rework made no progress: {p['title']}",
                        f"The rework session after a failed review finished "
                        f"without pushing any new commits to PR #{n}. "
                        "Repeating the review would loop on the same code, "
                        "so the dispatcher paused this repo. Please take a "
                        "look at the PR and the review comments, then "
                        "comment here and add the `answered` label to "
                        "resume.")
                    if b:
                        if notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                         f"#{b} {p['title']}\n\n"
                                         f"PR #{n}: rework pushed no changes. "
                                         "Comment on the issue and add the "
                                         "`answered` label (or reply here) "
                                         "to resume.",
                                         priority="high", tags="warning", url=p.get("url")):
                            state["seen_blockers"].append(b)
                    pause = True
                    continue
                if "approved" in after:
                    run(["gh", "pr", "edit", str(n), "--remove-label", "approved"])
                    log(f"PR #{n}: removed `approved` (code changed in rework)")
            maybe_save(state, dry)
            if pause:
                return "blocked"
            return "worked"
        maybe_save(state, dry)
        return "waiting" if waiting else "worked"

    # --- next roadmap item ---
    item, role = first_roadmap_item()
    if item:
        strikes = state["failures"].get(item, 0)
        if strikes >= FAIL_STRIKES:
            # Safety net: normally the blocker is filed at failure time.
            if dry:
                log(f"would file blocker: item failed {strikes} times: {item}")
                return "blocked"
            n = file_blocker(
                f"Roadmap item failed twice: {item}",
                "This roadmap item's sessions failed twice (non-zero exit or "
                "timeout; see dispatcher/logs/). The dispatcher paused this "
                "repo. Please take a look, then comment here and add the "
                "`answered` label to resume.")
            if n:
                if notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                 f"#{n} roadmap item failed twice\n\n"
                                 f"Item: {item}\n\n"
                                 "Comment on the issue and add the `answered` "
                                 "label (or reply here) to resume.",
                                 priority="high", tags="warning"):
                    state["seen_blockers"].append(n)
                maybe_save(state, dry)
                return "blocked"
            return "abort"
        log(f"next roadmap item ({role}): {item}")
        if dry:
            return "worked"
        maybe_save(state, dry)
        note = STASH_NOTE if state.get("stash_note_pending") else ""
        res = run_session(role, PROMPTS[role].format(item=item) + note)
        if res is None:
            return "abort"
        if res.quota or res.budget:
            return pause_for_quota_or_budget(state, res, dry)
        if res.code == 0:
            state["failures"].pop(item, None)
            state.pop("stash_note_pending", None)
            request_publish()
        else:
            fails = state["failures"].get(item, 0) + 1
            state["failures"][item] = fails
            log(f"session failed (exit {res.code}); failures for this item: {fails}")
            if fails < FAIL_STRIKES:
                # Feed the failure back: a diagnosis task issue gives the
                # loop one self-fix attempt before human escalation.
                report_session_failure(role, item)
            if fails >= FAIL_STRIKES:
                n = file_blocker(
                    f"Roadmap item failed twice: {item}",
                    "This roadmap item's sessions failed twice (non-zero exit "
                    "or timeout; see dispatcher/logs/). The dispatcher paused "
                    "this repo. Please take a look, then comment here and add "
                    "the `answered` label to resume.")
                if n:
                    if notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                     f"#{n} roadmap item failed twice\n\n"
                                     f"Item: {item}\n\n"
                                     "Comment on the issue and add the `answered` "
                                     "label (or reply here) to resume.",
                                     priority="high", tags="warning"):
                        state["seen_blockers"].append(n)
                    maybe_save(state, dry)
                    return "blocked"
                return "abort"
        maybe_save(state, dry)
        return "worked"

    # --- idle ---
    log("idle: no open PRs, roadmap complete")
    if not state.get("notified_done"):
        state["notified_done"] = True
        if not dry:
            notify.notify(f"{REPO_NAME}: roadmap done",
                          "All ROADMAP.md items are checked; the dispatcher "
                          "is idle. No action needed — file new work via a "
                          "`task` issue or /task on Telegram.",
                          priority="high", tags="tada")
    # Delivery considered complete for the whole repo: verify once per
    # idle period (the flag is cleared whenever new work arrives).
    if not state.get("verified_idle"):
        state["verified_idle"] = True
        maybe_save(state, dry)
        if not verify_delivery(
                "the roadmap is complete, all PRs are merged, and the repo is idle",
                dry, state):
            state["verified_idle"] = False  # session did not run; retry later
            maybe_save(state, dry)
    maybe_save(state, dry)
    return "idle"


def acquire_lock():
    """Non-blocking flock; returns the held handle or None if taken."""
    LOCK_FILE.parent.mkdir(exist_ok=True)
    fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main():
    dry = "--dry" in sys.argv
    loop = "--loop" in sys.argv
    signal.signal(signal.SIGTERM, _request_shutdown)
    LOGDIR.mkdir(exist_ok=True)
    rotate_logs()
    lock = acquire_lock()
    if lock is None:
        log("another dispatcher tick is already running; skipping")
        return 0
    status = None
    try:
        while True:
            if _shutdown["requested"]:
                raise Shutdown()
            status = tick(dry)
            if dry or not loop or status != "worked":
                break
            log(f"--loop: cycle done; re-evaluating in {LOOP_SETTLE_S}s")
            time.sleep(LOOP_SETTLE_S)
    except Shutdown:
        log("SIGTERM: gentle shutdown")
        if not dry:
            recover_dirty_tree()
            run(["git", "checkout", "main"])
        status = "worked"
    log(f"tick result: {status}")
    return 0 if status != "abort" else 1


if __name__ == "__main__":
    sys.exit(main())

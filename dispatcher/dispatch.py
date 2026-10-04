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
  3. Task issues (open, label `task`) are owner-filed work orders — the
     way to launch work from the phone. First unlaunched task -> one
     session with the issue body and comments as the prompt. Two failed
     task sessions -> the issue gains the `blocker` label and the repo
     pauses.
  4. Main-branch CI: the latest run on main is checked every tick. A
     red run gets one automatic fix session (pushed straight to main);
     FAIL_STRIKES consecutive red fixes -> blocker + pause. A green run
     resets the budget.
  5. If open PRs exist:
       - merge PRs with BOTH review labels (`approved` + `approved-adversarial`)
         and green checks first (squash, delete branch);
       - two-reviewer pipeline, one review session per tick: a PR lacking
         `approved` gets a correctness (QA) review; a PR with `approved`
         but lacking `approved-adversarial` gets an adversarial review.
         A failed review (label not added) triggers a rework session on
         the PR (impl role, addresses the review comments) and invalidates
         prior approvals. Two failed review rounds on the same PR ->
         blocker + pause.
  6. Else: run an Implementation session on the first unchecked ROADMAP.md
     item (a role tag in the item text selects the prompt; default impl).
  7. Else: idle (roadmap complete) -> notify once.

Delivery verification: when the loop considers the delivery complete —
after a PR merges, and once when a repo goes idle — a verification
session asks: "Has the project fulfilled its scope and are all the
deliverables completed, and stored correctly?" Gaps are filed as
`[verify]` task issues the loop picks up next tick; a clean pass notifies
the owner. An approved PR whose CI stays red gets one automatic fix
session (the fix re-enters the review pipeline) before the dispatcher
escalates to a blocker.

Failure budget: dispatcher/state.json counts consecutive failed sessions
(non-zero exit or timeout) per roadmap item. 2 strikes on the same item ->
the dispatcher files a blocker issue and pauses the repo; the owner's
answer on that issue resumes the loop with a fresh budget.

Sessions run: vibe -p <prompt> --auto-approve --trust --max-turns 150
--max-price $DISPATCH_MAX_PRICE (default 10) --output json, 90-minute
timeout, output captured to dispatcher/logs/.

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

REPO = Path(__file__).resolve().parent.parent
ROADMAP = REPO / "ROADMAP.md"
LOGDIR = REPO / "dispatcher" / "logs"
STATE_FILE = REPO / "dispatcher" / "state.json"
LOCK_FILE = REPO / "dispatcher" / ".lock"
PUBLISH_FLAGS = REPO.parent / ".publish-flags"
MAX_TURNS = "150"
MAX_PRICE = os.environ.get("DISPATCH_MAX_PRICE", "10")
SESSION_TIMEOUT_S = 5400
FAIL_STRIKES = 2   # failed sessions per roadmap item before pausing
QA_STRIKES = 2     # QA passes per PR without approval before pausing
LOOP_SETTLE_S = 30  # pause between sessions in --loop mode
REPO_NAME = "immich-geotag"
ADV_LABEL = "approved-adversarial"

# Appended to every dispatcher-filed blocker: the owner is a non-developer
# reading these on a phone; the answer mechanism must be spelled out.
BLOCKER_HOWTO = ("\n\n---\n\n**How to answer:** comment below, then add the "
                 "`answered` label (gear icon → Labels → answered) — or "
                 "just reply to the Telegram notification. The loop "
                 "resumes within ~5 min of your answer.")

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
                        ("approved-adversarial", "5319E7")):
        run(["gh", "label", "create", name, "--color", color])


def open_prs():
    return gh_json(["gh", "pr", "list", "--json", "number,title,labels,url,isDraft"])


def pr_labels(number):
    data = gh_json(["gh", "pr", "view", str(number), "--json", "labels"])
    if data is None:
        return None
    return {l["name"] for l in data.get("labels", [])}


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
    return gh_json(["gh", "issue", "list", "--label", "blocker", "--state", "all",
                    "--json", "number,title,state,labels,url"]) or []

def task_issues():
    return gh_json(["gh", "issue", "list", "--label", "task",
                    "--state", "open", "--json", "number,title"]) or []



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


def run_session(role, prompt):
    """Returns the session exit code, 124 on timeout, None if vibe is missing.

    Raises Shutdown if SIGTERM arrived (before or during the session).
    """
    global _session_proc
    if _shutdown["requested"]:
        raise Shutdown()
    LOGDIR.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    logfile = LOGDIR / f"{ts}-{role}.json"
    log(f"starting {role} session (log: {logfile.name})")
    cmd = ["vibe", "-p", prompt, "--auto-approve", "--trust",
           "--max-turns", MAX_TURNS, "--max-price", MAX_PRICE,
           "--output", "json"]
    try:
        proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        log("ABORT: vibe CLI not found on PATH")
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
        return 124
    finally:
        _session_proc = None
    logfile.write_text(stdout or "")
    if code != 0 and (stderr or "").strip():
        (LOGDIR / f"{ts}-{role}.err").write_text(stderr)
    (LOGDIR / f"{ts}-{role}.exit").write_text(str(code) + "\n")
    log(f"{role} session finished, exit {code}")
    if _shutdown["requested"]:
        raise Shutdown()
    return code


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


def verify_delivery(delivered, dry):
    """Run the delivery-verification session and report the verdict.

    Asks the verification question ("has the project fulfilled its scope
    and are all deliverables completed and stored correctly?") as a
    session. The session files a `[verify]` task issue for gaps (the
    loop picks it up next tick); silence means pass. Never blocks the
    tick: a failed session is fed to the feedback loop, not the budget.
    """
    if dry:
        log(f"would run delivery verification: {delivered}")
        return
    before = {i["number"] for i in task_issues()}
    code = run_session("verify", VERIFY_PROMPT.format(delivered=delivered))
    if code is None or code != 0:
        log(f"verification session failed (exit {code}); no verdict")
        report_session_failure("verify", delivered)
        return
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
                         ("notified_done", False)):
        state.setdefault(key, default)

    if not dry:
        ensure_labels()

    # --- blocker issues: the human-in-the-loop channel ---
    blockers = blocker_issues()
    if blockers is None:
        return "abort"
    for issue in blockers:
        if issue["number"] not in state["seen_blockers"]:
            state["seen_blockers"].append(issue["number"])
            log(f"blocker issue #{issue['number']} ({issue['state'].lower()}): {issue['title']}")
            if not dry and issue["state"] == "OPEN":
                notify.notify(f"{REPO_NAME}: needs your answer",
                              f"#{issue['number']} {issue['title']}\n\n"
                              "Reply to this message (Telegram) or comment "
                              "on the issue and add the `answered` label — "
                              "the loop resumes within ~5 min.",
                              priority="high", tags="warning", url=issue.get("url"))
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
        maybe_save(state, dry)
        return "blocked"
    if actionable:
        issue = actionable[0]
        if dry:
            log(f"would resume after blocker #{issue['number']} was answered")
            return "worked"
        comments = issue_comments(issue["number"]) or []
        answer = (comments[-1].get("body", "").strip() if comments
                  else "The owner closed the issue without a comment; "
                       "use your best judgment.")
        log(f"blocker #{issue['number']} answered; resuming")
        run(["gh", "issue", "edit", str(issue["number"]), "--remove-label", "answered"])
        state["resumed_blockers"].append(issue["number"])
        state["verified_idle"] = False  # new work: re-verify before idle again
        state["ci_fix_tried"] = []     # fresh CI-fix budget too
        state["ci_escalated"] = []
        state["main_ci_strikes"] = 0   # fresh main-CI budget as well
        state["failures"] = {}  # fresh failure budget after owner input
        maybe_save(state, dry)
        note = STASH_NOTE if state.get("stash_note_pending") else ""
        code = run_session("impl", RESUME_PROMPT.format(
            number=issue["number"], title=issue["title"], answer=answer) + note)
        if code is None:
            return "abort"
        if code == 0:
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
                            state["seen_blockers"].append(b["number"])
                            notify.notify(
                                f"{REPO_NAME}: paused — CI keeps failing on main",
                                f"{r.get('displayTitle', '')}\n\n"
                                "Automatic fixes did not clear it. Comment "
                                "on the issue and add the `answered` "
                                "label (or reply here) to resume.",
                                priority="high", tags="warning")
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
                    code = run_session("impl", MAIN_CI_FIX_PROMPT.format(
                        run_id=rid, title=r.get("displayTitle", "")))
                    if code is None:
                        return "abort"
                    if code == 0:
                        request_publish()
                    maybe_save(state, dry)
                    return "worked"

    # --- task issues: owner-filed work orders (launch from the phone) ---
    tasks = gh_json(["gh", "issue", "list", "--label", "task",
                     "--state", "open", "--json", "number,title,url"])
    if tasks is None:
        return "abort"
    for issue in tasks:
        if issue["number"] in state["launched_tasks"]:
            continue
        n = issue["number"]
        if dry:
            log(f"would run task session for issue #{n}: {issue['title']}")
            return "worked"
        data = gh_json(["gh", "issue", "view", str(n),
                        "--json", "body,comments"]) or {}
        body = (data.get("body") or "").strip() or "(no body)"
        comments = data.get("comments") or []
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
        code = run_session("impl", TASK_PROMPT.format(
            number=n, title=issue["title"], body=body, comments=ctext) + note)
        if code is None:
            return "abort"
        key = f"task #{n}"
        if code == 0:
            state["failures"].pop(key, None)
            state.pop("stash_note_pending", None)
            request_publish()
        else:
            fails = state["failures"].get(key, 0) + 1
            state["failures"][key] = fails
            log(f"task session failed (exit {code}); failures: {fails}")
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
                state["seen_blockers"].append(n)
                notify.notify(f"{REPO_NAME}: paused — needs your attention",
                              f"#{n} {issue['title']}\n\n"
                              "Two sessions failed on this task. Comment on "
                              "the issue and add the `answered` label (or "
                              "reply here) to resume.",
                              priority="high", tags="warning", url=issue.get("url"))
                maybe_save(state, dry)
                return "blocked"
        maybe_save(state, dry)
        return "worked"

    # --- pull requests ---
    prs = open_prs()
    if prs is None:
        return "abort"
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
                        run_session("impl", CI_FIX_PROMPT.format(
                            number=p["number"], title=p["title"]))
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
                            state["seen_blockers"].append(b)
                            notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                          f"#{b} CI keeps failing on PR #{p['number']}\n\n"
                                          "The loop could not fix it automatically. "
                                          "Comment on the issue and add the "
                                          "`answered` label (or reply here) "
                                          "to resume.",
                                          priority="high", tags="warning",
                                          url=p.get("url"))
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
                notify.notify(f"{REPO_NAME}: PR merged",
                              f"#{p['number']} {p['title']}\n\n"
                              "Outputs will be published to Nextcloud "
                              "Documents/04_projects/ within ~5 min. "
                              "No action needed.",
                              tags="white_check_mark", url=p.get("url"))
                # Delivery complete for this PR: verify scope + storage.
                if p["number"] not in state["verified_merges"]:
                    state["verified_merges"].append(p["number"])
                    state["verified_idle"] = False
                    maybe_save(state, dry)
                    verify_delivery(
                        f"PR #{p['number']} ({p['title']}) just merged to main",
                        dry)
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
                targets = in_review
            log(f"PRs needing {role} review: {[p['number'] for p in targets]}")
            if dry:
                maybe_save(state, dry)
                return "worked"
            maybe_save(state, dry)
            code = run_session(role, prompt)
            if code is None:
                return "abort"
            pause = False
            for p in targets:
                n = p["number"]
                after = pr_labels(n)
                if after is None:
                    return "abort"
                if label in after:
                    state["qa_strikes"].pop(str(n), None)
                    continue
                strikes = state["qa_strikes"].get(str(n), 0) + 1
                state["qa_strikes"][str(n)] = strikes
                if strikes > QA_STRIKES:
                    log(f"PR #{n} failed review after rework rounds; pausing")
                    b = file_blocker(
                        f"PR #{n} failed review after rework rounds: {p['title']}",
                        f"This PR failed {QA_STRIKES} review rounds (label "
                        f"`{label}` not added; see the PR review comments), "
                        "each followed by a rework session. The dispatcher "
                        "paused this repo. Please take a look, then comment "
                        "here and add the `answered` label to resume.")
                    if b:
                        state["seen_blockers"].append(b)
                        notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                      f"#{b} {p['title']}\n\n"
                                      f"PR #{n} failed {QA_STRIKES} review "
                                      "rounds. Comment on the issue and add "
                                      "the `answered` label (or reply here) "
                                      "to resume.",
                                      priority="high", tags="warning", url=p.get("url"))
                    pause = True
                    continue
                log(f"PR #{n} failed {role} review (strike {strikes}); launching rework")
                run_session("impl", REWORK_PROMPT.format(
                    number=n, title=p["title"]))
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
                state["seen_blockers"].append(n)
                notify.notify(f"{REPO_NAME}: paused — needs your attention",
                              f"#{n} roadmap item failed twice\n\n"
                              f"Item: {item}\n\n"
                              "Comment on the issue and add the `answered` "
                              "label (or reply here) to resume.",
                              priority="high", tags="warning")
                maybe_save(state, dry)
                return "blocked"
            return "abort"
        log(f"next roadmap item ({role}): {item}")
        if dry:
            return "worked"
        maybe_save(state, dry)
        note = STASH_NOTE if state.get("stash_note_pending") else ""
        code = run_session(role, PROMPTS[role].format(item=item) + note)
        if code is None:
            return "abort"
        if code == 0:
            state["failures"].pop(item, None)
            state.pop("stash_note_pending", None)
            request_publish()
        else:
            fails = state["failures"].get(item, 0) + 1
            state["failures"][item] = fails
            log(f"session failed (exit {code}); failures for this item: {fails}")
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
                    state["seen_blockers"].append(n)
                    notify.notify(f"{REPO_NAME}: paused — needs your attention",
                                  f"#{n} roadmap item failed twice\n\n"
                                  f"Item: {item}\n\n"
                                  "Comment on the issue and add the `answered` "
                                  "label (or reply here) to resume.",
                                  priority="high", tags="warning")
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
        verify_delivery(
            "the roadmap is complete, all PRs are merged, and the repo is idle",
            dry)
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

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
  3. If open PRs exist:
       - merge approved PRs with green checks first (squash, delete branch);
       - any remaining PR without the `approved` label -> run a QA session.
         Two QA passes on the same PR without approval -> blocker + pause.
  4. Else: run an Implementation session on the first unchecked ROADMAP.md
     item (a role tag in the item text selects the prompt; default impl).
  5. Else: idle (roadmap complete) -> notify once.

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
import notify  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ROADMAP = REPO / "ROADMAP.md"
LOGDIR = REPO / "dispatcher" / "logs"
STATE_FILE = REPO / "dispatcher" / "state.json"
LOCK_FILE = REPO / "dispatcher" / ".lock"
MAX_TURNS = "150"
MAX_PRICE = os.environ.get("DISPATCH_MAX_PRICE", "10")
SESSION_TIMEOUT_S = 5400
FAIL_STRIKES = 2   # failed sessions per roadmap item before pausing
QA_STRIKES = 2     # QA passes per PR without approval before pausing
LOOP_SETTLE_S = 30  # pause between sessions in --loop mode
REPO_NAME = "immich-geotag"

IMPL_PROMPT = """You are the Implementation agent for this repository (unattended headless run).
Read AGENTS.md first, then PLAN.md — it is the complete approved spec.
Follow it literally; do not redesign.

Roadmap item for this session: {item}

Follow the hard constraints and decision defaults in AGENTS.md. Work on a
new branch, commit incrementally, open a draft PR with a Handoff section,
and tick the matching checkbox in ROADMAP.md as part of your PR. If you are
blocked (question only the owner can answer, live validation needed, work
outside the build steps), open a GitHub issue labeled `blocker`
(`gh issue create --label blocker`) and stop cleanly. When done: push the
branch, then `git checkout main` so the working tree is left clean."""

QA_PROMPT = """You are the QA agent for this repository (unattended headless run).
Read AGENTS.md first. Open pull requests need review. For each open PR
(`gh pr list`, `gh pr view N`, `gh pr diff N`):
- Check the PR against AGENTS.md (hard constraints, style, decision defaults)
  and PLAN.md's target design. Pay special attention to: apply must stay
  behind the dry-run/CSV review flow, no live API calls in tests, no
  personal data (.env, CSVs, photos) committed.
- Run the project's tests if present; keep total runtime under ~25 minutes.
- If the PR is good: `gh pr ready N` (if still a draft), then
  `gh label create approved --color 0E8A16 2>/dev/null || true` and
  `gh pr edit N --add-label approved`.
- If the PR has problems: `gh pr review N --comment --body "..."` with
  concrete findings, and do NOT add the label. If the problems need an
  owner decision, say so explicitly in the comment.
Do not merge PRs yourself. Do not modify code. When done: `git checkout main`
so the working tree is left clean."""

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
                        ("answered", "FBCA04")):
        run(["gh", "label", "create", name, "--color", color])


def open_prs():
    return gh_json(["gh", "pr", "list", "--json", "number,title,labels,url,isDraft"])


def checks_pass(number):
    r = run(["gh", "pr", "checks", str(number)])
    if r.returncode == 0:
        return True
    # Repos without CI: "no checks reported" counts as green.
    return "no checks reported" in (r.stderr or "")


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


def issue_comments(number):
    data = gh_json(["gh", "issue", "view", str(number), "--json", "comments"])
    return (data or {}).get("comments", [])


def file_blocker(title, body):
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
        notify.notify(f"{REPO_NAME}: recovered work from an interrupted session",
                      "Stashed uncommitted changes and/or found unpushed "
                      "branches; see `git stash list` and `git branch` in "
                      "the clone.",
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
                         ("merge_failed_prs", []), ("notified_done", False)):
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
                notify.notify(f"{REPO_NAME}: blocked",
                              f"#{issue['number']} {issue['title']} — answer to resume",
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
        state["failures"] = {}  # fresh failure budget after owner input
        maybe_save(state, dry)
        note = STASH_NOTE if state.get("stash_note_pending") else ""
        code = run_session("impl", RESUME_PROMPT.format(
            number=issue["number"], title=issue["title"], answer=answer) + note)
        if code is None:
            return "abort"
        if code == 0:
            state.pop("stash_note_pending", None)
            maybe_save(state, dry)
        return "worked"

    # --- pull requests ---
    prs = open_prs()
    if prs is None:
        return "abort"
    for p in prs:
        if p["number"] not in state["seen_prs"]:
            state["seen_prs"].append(p["number"])
            log(f"new PR #{p['number']}: {p['title']}")
            if not dry:
                notify.notify(f"{REPO_NAME}: PR opened",
                              f"#{p['number']} {p['title']}",
                              tags="envelope", url=p.get("url"))
    if prs:
        # Merge approved + green PRs first (cheap gh calls, no session),
        # so ready work is not held up by PRs still under review.
        waiting = False
        for p in prs:
            if "approved" not in {l["name"] for l in p["labels"]}:
                continue
            if p["number"] not in state["approved_prs"]:
                state["approved_prs"].append(p["number"])
                log(f"PR #{p['number']} approved")
                if not dry:
                    notify.notify(f"{REPO_NAME}: PR approved",
                                  f"#{p['number']} {p['title']} — will merge",
                                  tags="ok", url=p.get("url"))
            if not checks_pass(p["number"]):
                log(f"PR #{p['number']} approved but checks not green; waiting")
                waiting = True
                continue
            log(f"PR #{p['number']} approved + green: merge")
            if dry:
                continue
            if merge(p["number"], p["title"], p.get("isDraft", False)):
                state["qa_strikes"].pop(str(p["number"]), None)
                state["approved_prs"] = [n for n in state["approved_prs"]
                                         if n != p["number"]]
                notify.notify(f"{REPO_NAME}: PR merged",
                              f"#{p['number']} {p['title']}",
                              tags="white_check_mark", url=p.get("url"))
            else:
                waiting = True
                if p["number"] not in state["merge_failed_prs"]:
                    state["merge_failed_prs"].append(p["number"])
                    notify.notify(f"{REPO_NAME}: merge failed",
                                  f"#{p['number']} {p['title']} — merge error, needs a look",
                                  priority="high", tags="warning", url=p.get("url"))
        unapproved = [p for p in prs
                      if "approved" not in {l["name"] for l in p["labels"]}]
        if unapproved:
            pause = False
            for p in unapproved:
                strikes = state["qa_strikes"].get(str(p["number"]), 0) + 1
                state["qa_strikes"][str(p["number"])] = strikes
                if strikes > QA_STRIKES:
                    log(f"PR #{p['number']} not approved after {QA_STRIKES} QA passes; pausing")
                    if not dry:
                        n = file_blocker(
                            f"PR #{p['number']} failed QA twice: {p['title']}",
                            "The QA agent reviewed this PR twice without adding the "
                            "`approved` label (see the PR review comments). The "
                            "dispatcher paused this repo. Please take a look, then "
                            "comment here and add the `answered` label to resume.")
                        if n:
                            state["seen_blockers"].append(n)
                            notify.notify(f"{REPO_NAME}: paused",
                                          f"PR #{p['number']} failed QA twice — needs a look",
                                          priority="high", tags="warning", url=p.get("url"))
                    pause = True
            if pause:
                maybe_save(state, dry)
                return "blocked"
            log(f"PRs needing QA: {[p['number'] for p in unapproved]}")
            maybe_save(state, dry)
            if not dry:
                run_session("qa", QA_PROMPT)
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
                notify.notify(f"{REPO_NAME}: paused",
                              f"item failed twice, needs a look: {item}",
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
        else:
            fails = state["failures"].get(item, 0) + 1
            state["failures"][item] = fails
            log(f"session failed (exit {code}); failures for this item: {fails}")
            if fails >= FAIL_STRIKES:
                n = file_blocker(
                    f"Roadmap item failed twice: {item}",
                    "This roadmap item's sessions failed twice (non-zero exit "
                    "or timeout; see dispatcher/logs/). The dispatcher paused "
                    "this repo. Please take a look, then comment here and add "
                    "the `answered` label to resume.")
                if n:
                    state["seen_blockers"].append(n)
                    notify.notify(f"{REPO_NAME}: paused",
                                  f"item failed twice, needs a look: {item}",
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
                          "All ROADMAP.md items are checked; dispatcher is idle.",
                          priority="high", tags="tada")
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

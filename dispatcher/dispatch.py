#!/usr/bin/env python3
"""Headless agent dispatcher for immich-geotag.

One invocation = one tick (run from cron every few hours; run.sh prevents
overlaps). State machine:

  1. Abort if the working tree is dirty. Checkout main, pull --ff-only.
  2. If open PRs exist:
       - any PR without the 'approved' label  -> run a QA session.
       - all approved -> merge those with green checks (squash, delete branch).
  3. Else: run an Implementation session on the first unchecked ROADMAP.md
     item.
  4. Else: idle.

Sessions run: vibe -p <prompt> --auto-approve --trust --max-turns 150
  --output json, 90-minute timeout, output captured to dispatcher/logs/.

Usage: python3 dispatcher/dispatch.py [--dry]
  --dry prints the decision and exits without launching anything.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ROADMAP = REPO / "ROADMAP.md"
LOGDIR = REPO / "dispatcher" / "logs"
MAX_TURNS = "150"
SESSION_TIMEOUT_S = 5400

IMPL_PROMPT = """You are the Implementation agent for this repository (unattended headless run).
Read AGENTS.md first, then PLAN.md — it is the complete approved spec.
Follow it literally; do not redesign.

Roadmap item for this session: {item}

Follow the hard constraints and decision defaults in AGENTS.md. Work on a
new branch, commit incrementally, open a draft PR with a Handoff section,
and tick the matching checkbox in ROADMAP.md as part of your PR. When done:
push the branch, then `git checkout main` so the working tree is left clean."""

QA_PROMPT = """You are the QA agent for this repository (unattended headless run).
Read AGENTS.md first. Open pull requests need review. For each open PR
(`gh pr list`, `gh pr view N`, `gh pr diff N`):
- Check the PR against AGENTS.md (hard constraints, style, decision defaults)
  and PLAN.md's target design. Pay special attention to: apply must stay
  behind the dry-run/CSV review flow, no live API calls in tests, no
  personal data (.env, CSVs, photos) committed.
- Run the project's tests if present; keep total runtime under ~25 minutes.
- If the PR is good: `gh label create approved --color 0E8A16 2>/dev/null || true`
  then `gh pr edit N --add-label approved`.
- If the PR has problems: `gh pr review N --comment --body "..."` with
  concrete findings, and do NOT add the label.
Do not merge PRs yourself. Do not modify code. When done: `git checkout main`
so the working tree is left clean."""


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def run(cmd, timeout=120):
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)


def open_prs():
    r = run(["gh", "pr", "list", "--json", "number,title,labels"])
    if r.returncode != 0:
        log(f"ABORT: gh pr list failed: {r.stderr.strip()[:200]}")
        return None
    return json.loads(r.stdout or "[]")


def checks_pass(number):
    return run(["gh", "pr", "checks", str(number)]).returncode == 0


def merge(number, title):
    r = run(["gh", "pr", "merge", str(number), "--squash", "--delete-branch"], timeout=180)
    if r.returncode == 0:
        log(f"merged PR #{number}: {title}")
    else:
        log(f"merge of PR #{number} failed: {r.stderr.strip()[:200]}")


def first_roadmap_item():
    if not ROADMAP.exists():
        return None
    for line in ROADMAP.read_text().splitlines():
        m = re.match(r"\s*- \[ \]\s*(.+)", line)
        if m:
            return m.group(1).strip()
    return None


def run_session(role, prompt):
    LOGDIR.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    logfile = LOGDIR / f"{ts}-{role}.json"
    log(f"starting {role} session (log: {logfile.name})")
    try:
        r = subprocess.run(
            ["vibe", "-p", prompt, "--auto-approve", "--trust",
             "--max-turns", MAX_TURNS, "--output", "json"],
            cwd=REPO, capture_output=True, text=True, timeout=SESSION_TIMEOUT_S)
        logfile.write_text(r.stdout or "")
        (LOGDIR / f"{ts}-{role}.exit").write_text(str(r.returncode) + "\n")
        log(f"{role} session finished, exit {r.returncode}")
    except subprocess.TimeoutExpired:
        log(f"{role} session timed out after {SESSION_TIMEOUT_S}s")
    except FileNotFoundError:
        log("ABORT: vibe CLI not found on PATH")


def main():
    dry = "--dry" in sys.argv
    LOGDIR.mkdir(exist_ok=True)

    status = run(["git", "status", "--porcelain"])
    if status.stdout.strip():
        log("ABORT: working tree not clean")
        return 1

    run(["git", "checkout", "main"])
    pull = run(["git", "pull", "--ff-only"])
    if pull.returncode != 0:
        log(f"ABORT: pull failed: {pull.stderr.strip()[:200]}")
        return 1

    prs = open_prs()
    if prs is None:
        return 1

    if prs:
        unapproved = [p for p in prs
                      if "approved" not in {l["name"] for l in p["labels"]}]
        if unapproved:
            log(f"PRs needing QA: {[p['number'] for p in unapproved]}")
            if not dry:
                run_session("qa", QA_PROMPT)
            return 0
        for p in prs:
            if checks_pass(p["number"]):
                log(f"PR #{p['number']} approved + green: merge")
                if not dry:
                    merge(p["number"], p["title"])
            else:
                log(f"PR #{p['number']} approved but checks not green; waiting")
        return 0

    item = first_roadmap_item()
    if item:
        log(f"next roadmap item: {item}")
        if not dry:
            run_session("impl", IMPL_PROMPT.format(item=item))
        return 0

    log("idle: no open PRs, roadmap complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())

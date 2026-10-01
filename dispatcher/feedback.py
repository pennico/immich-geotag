#!/usr/bin/env python3
"""Failure feedback loop: failures become agent work, not just notifications.

report: a failure (cron job, overseer tick, dispatcher session) files a
`task` issue on the affected repo with the error context. The repo's
dispatcher picks it up on its next tick and an agent session diagnoses
and fixes it; the fix is evaluated by the normal PR review pipeline.
Recurrences of the same failure signature comment on the existing
issue; after --max-auto occurrences the issue gains `blocker` (the
human-in-the-loop channel) and the owner gets a high-priority
notification.

resolve: the failure source has succeeded — close any open auto-filed
issues for it and clear the state. This is the success evaluation: the
loop only stays closed when the underlying job actually works again.

State: dispatcher/feedback_state.json (gitignored), keyed by
signature = source + normalized error line.

Usage (inside the agent-runner container, or anywhere with gh auth):
  feedback.py report --source tts-e2e --repo tts-ereader \
      --title "E2E slice failed" [--log FILE | --tail -]
  feedback.py resolve --source tts-e2e
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify

STATE_FILE = Path(__file__).resolve().parent / "feedback_state.json"
AUTO_TAG = "[auto]"
TAIL_LINES = 40
DEFAULT_MAX_AUTO = 3


def log(msg):
    print(f"[feedback] {msg}", flush=True)


def load_state():
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                data.setdefault("signatures", {})
                return data
        except ValueError:
            pass
    return {"signatures": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def error_line(tail):
    """The most informative line of a failure tail, normalized."""
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if re.search(r"(Error|Exception|Traceback|FAILED|failed|exit \d+)", ln):
            return re.sub(r"\d{4}-\d{2}-\d{2}[T ]\S+", "", ln)[:200]
    return lines[-1][:200] if lines else "(no output)"


def signature(source, tail):
    return f"{source}: {error_line(tail)}"


def issue_state(repo, number):
    r = run(["gh", "issue", "view", str(number), "--repo", repo, "--json", "state"])
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)["state"]
    except (ValueError, KeyError):
        return None


def file_issue(repo, title, body):
    r = run(["gh", "issue", "create", "--repo", repo, "--label", "task",
             "--title", title, "--body", body])
    if r.returncode != 0:
        log(f"could not file issue on {repo}: {r.stderr.strip()[:200]}")
        return None
    m = re.search(r"/issues/(\d+)", r.stdout)
    return int(m.group(1)) if m else None


def report(source, repo, title, tail, max_auto=DEFAULT_MAX_AUTO):
    """File or update the auto-issue for a failure. Returns issue number."""
    state = load_state()
    sig = signature(source, tail)
    entry = state["signatures"].get(sig)
    if entry and issue_state(entry["repo"], entry["issue"]) == "OPEN":
        entry["attempts"] += 1
        attempts = entry["attempts"]
        log(f"recurrence {attempts} of '{sig}' -> comment on "
            f"{entry['repo']}#{entry['issue']}")
        run(["gh", "issue", "comment", str(entry["issue"]), "--repo",
             entry["repo"], "--body",
             f"Recurrence {attempts} of the same failure:\n\n"
             f"```\n{tail}\n```"])
        if attempts >= max_auto:
            run(["gh", "issue", "edit", str(entry["issue"]), "--repo",
                 entry["repo"], "--add-label", "blocker"])
            log(f"escalated {entry['repo']}#{entry['issue']} to blocker "
                f"after {attempts} attempts")
            notify.notify(f"{repo}: auto-fix gave up",
                          f"#{entry['issue']} {error_line(tail)}\n\n"
                          f"The same failure recurred {attempts} times; "
                          "auto-fix gave up. Comment on the issue and add "
                          "the `answered` label (or reply here) to resume.",
                          priority="high", tags="warning")
        save_state(state)
        return entry["issue"]

    body = (f"{AUTO_TAG} Filed automatically by the failure feedback loop.\n\n"
            f"Source: `{source}`\nFailure: `{error_line(tail)}`\n\n"
            f"Recent output:\n```\n{tail}\n```\n\n"
            "Diagnose the failure from the output above and the source's own "
            "logs, fix the root cause, and open a PR per AGENTS.md (reviews "
            "evaluate the fix). If the fix needs an owner decision, add the "
            "`blocker` label and stop cleanly. This issue closes "
            "automatically when the source next succeeds.")
    n = file_issue(repo, f"{AUTO_TAG} {title}", body)
    if n:
        state["signatures"][sig] = {
            "attempts": 1, "issue": n, "repo": repo, "source": source}
        save_state(state)
        log(f"filed {repo}#{n} for '{sig}'")
        notify.notify(f"{repo}: failure auto-reported",
                      f"#{n} {title}\n\n"
                      "A diagnosis issue was filed; an agent session will "
                      "pick it up and self-fix. No action needed unless "
                      "this repeats.",
                      tags="gear")
    return n


def resolve(source):
    """The source succeeded: close its open auto-issues, clear state."""
    state = load_state()
    closed = 0
    for sig, entry in list(state["signatures"].items()):
        if entry.get("source") != source:
            continue
        if issue_state(entry["repo"], entry["issue"]) == "OPEN":
            run(["gh", "issue", "close", str(entry["issue"]), "--repo",
                 entry["repo"], "--comment",
                 "The failure source has since succeeded; closing "
                 "automatically."])
            closed += 1
        del state["signatures"][sig]
    save_state(state)
    log(f"resolved source '{source}': closed {closed} issue(s)")


def full_repo(repo):
    """gh wants OWNER/REPO; accept bare names and prefix the login."""
    if "/" in repo:
        return repo
    r = run(["gh", "api", "user", "--jq", ".login"])
    owner = r.stdout.strip() if r.returncode == 0 else ""
    return f"{owner}/{repo}" if owner else repo


def read_tail(args):
    if args.log:
        text = Path(args.log).read_text(errors="replace")
    elif args.tail == "-":
        text = sys.stdin.read()
    else:
        text = args.tail or ""
    return "\n".join(text.splitlines()[-TAIL_LINES:])


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="report a failure")
    rp.add_argument("--source", required=True,
                    help="failure source name, e.g. tts-e2e, overseer")
    rp.add_argument("--repo", required=True, help="repo to file the issue on")
    rp.add_argument("--title", required=True)
    rp.add_argument("--log", help="log file to take the tail from")
    rp.add_argument("--tail", help="tail text, or '-' to read stdin")
    rp.add_argument("--max-auto", type=int, default=DEFAULT_MAX_AUTO,
                    help="recurrences before blocker escalation")
    sp = sub.add_parser("resolve", help="mark a failure source as recovered")
    sp.add_argument("--source", required=True)
    args = p.parse_args()
    if args.cmd == "report":
        report(args.source, full_repo(args.repo), args.title,
               read_tail(args), args.max_auto)
    else:
        resolve(args.source)


if __name__ == "__main__":
    main()

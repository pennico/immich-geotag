"""Per-PR briefing cache: fetch a PR once per tick, not once per session.

Every review/rework/CI-fix session used to start from zero and re-fetch
the PR diff and comments from GitHub (`gh pr diff N`, `gh pr view N
--comments`) — the measured top context filler on 2026-10-06 (the same
PR diff was pulled 5+ times across one rework cycle, ~430 KB). The
dispatcher knows which PR a session will work on, so it writes one
briefing file per target PR just before launching the session and points
the prompt at it. The session reads the local file instead of running
gh; the PR is fetched exactly once per tick.

All calls are best-effort: a failed briefing write must never block a
tick — the session falls back to fetching from GitHub itself.
"""
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
BRIEF_DIR = REPO / "dispatcher" / "briefings"

# Same env var and default as dispatch.TRUSTED_AUTHORS (kept separate:
# engine copies may ship without this module; a test pins them equal).
_OWNERS = {a.strip() for a in os.environ.get(
    "DISPATCH_OWNER_AUTHORS", "pennico").split(",") if a.strip()}
TRUSTED_AUTHORS = {
    a.strip() for a in os.environ.get(
        "DISPATCH_TRUSTED_AUTHORS",
        ",".join(sorted(_OWNERS | {"app/github-actions", "github-actions",
                                   "pennico-agent"}))).split(",")
    if a.strip()}

HEADER = ("<!-- briefing: pr {number} | head {head} | generated {generated} "
          "-->\n")


def _run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        # gh missing, not on PATH, or hung: best-effort, never raise
        return SimpleNamespace(returncode=127, stdout="", stderr="")


def _trusted_comment_text(raw):
    """Comments + review bodies from trusted authors only: this file is fed
    to sessions, and the repos are public, so a stranger's comment must
    not reach a prompt."""
    import json
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return "(comments unavailable)"
    items = (data.get("comments") or []) + (data.get("reviews") or [])
    items.sort(key=lambda c: c.get("createdAt") or c.get("submittedAt") or "")
    lines = []
    for c in items:
        login = (c.get("author") or {}).get("login", "?")
        body = (c.get("body") or "").strip()
        if login in TRUSTED_AUTHORS and body:
            lines.append(f"- {login}: {body}")
    return "\n".join(lines) or "(no comments)"


def write_briefing(number):
    """(Re)generate briefings/pr-<n>.md: diff + comments + metadata.

    Returns the briefing path, or None if any gh call failed (the
    session then fetches from GitHub itself). Always regenerates when
    called — callers invoke it once per tick per PR, and a rework push
    must be reflected in the next briefing.
    """
    meta = _run(["gh", "pr", "view", str(number),
                 "--json", "number,title,headRefOid,labels,isDraft"])
    diff = _run(["gh", "pr", "diff", str(number)])
    comments = _run(["gh", "pr", "view", str(number), "--json", "comments,reviews"])
    if meta.returncode != 0 or diff.returncode != 0:
        return None
    try:
        import json
        m = json.loads(meta.stdout or "{}")
    except ValueError:
        return None
    labels = ", ".join(l["name"] for l in m.get("labels", [])) or "(none)"
    BRIEF_DIR.mkdir(exist_ok=True)
    path = BRIEF_DIR / f"pr-{number}.md"
    parts = [
        HEADER.format(number=number, head=m.get("headRefOid", "?"),
                      generated=time.strftime("%Y-%m-%d %H:%M:%S")),
        f"# Briefing: PR #{number} — {m.get('title', '')}\n",
        f"State: {'draft' if m.get('isDraft') else 'ready for review'}"
        f" | Labels: {labels}\n",
        "\n## Comments (reviews and discussion)\n\n",
        _trusted_comment_text(comments.stdout) + "\n",
        "\n## Diff\n\n```diff\n",
        (diff.stdout or "(empty diff)").strip() + "\n",
        "```\n",
    ]
    path.write_text("".join(parts))
    return path


def briefing_note(numbers):
    """Prompt text pointing a session at fresh briefing files.

    Appended to the session prompt; overrides the prompt's own
    `gh pr diff` / `gh pr view --comments` instructions.
    """
    if not numbers:
        return ""
    listing = "\n".join(f"- PR #{n}: dispatcher/briefings/pr-{n}.md"
                       for n in numbers)
    return ("\n\n---\n\n**Briefing files (fetched for you just now):**\n"
            f"{listing}\n"
            "Read these local files instead of running `gh pr diff` or "
            "`gh pr view --comments` — they contain the current diff and "
            "all comments. Only fetch from GitHub if a briefing file is "
            "missing or clearly out of date. Keep other reads targeted "
            "(grep / line ranges, `--stat` before full diffs) — context "
            "is re-sent on every turn and is the main cost driver.")


def prepare_briefings(numbers):
    """Write briefings for the given PRs; returns the prompt note listing
    only the PRs whose briefing was actually written. Sessions for PRs
    without a briefing fall back to fetching from GitHub themselves.
    """
    written = []
    for n in numbers:
        if write_briefing(n):
            written.append(n)
    return briefing_note(written)


def cleanup_briefings(open_numbers):
    """Delete briefing files for PRs that are no longer open."""
    if not BRIEF_DIR.is_dir():
        return
    keep = {f"pr-{n}.md" for n in open_numbers}
    for f in BRIEF_DIR.glob("pr-*.md"):
        if f.name not in keep:
            try:
                f.unlink()
            except OSError:
                pass

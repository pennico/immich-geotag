"""Tests for the context-diet machinery (issue #22): per-PR briefing
cache and session-log rotation.

Everything gh-shaped is mocked: no network, no repo side effects.
On-disk paths are pointed at tmp_path.
"""
import gzip
import os
import time
from pathlib import Path
from types import SimpleNamespace

import briefing
import dispatch
import pytest


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


META = ('{"number": 14, "title": "Engine sync", "headRefOid": "abc123",'
        ' "labels": [{"name": "approved"}], "isDraft": false}')


@pytest.fixture()
def benv(tmp_path, monkeypatch):
    """Isolated briefing module: tmp briefings dir, mocked gh."""
    monkeypatch.setattr(briefing, "BRIEF_DIR", tmp_path / "briefings")
    e = SimpleNamespace()
    e.calls = []

    def fake_run(cmd, timeout=120):
        e.calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"] and "--json" in cmd:
            return Result(0, META)
        if cmd[:3] == ["gh", "pr", "diff"]:
            return Result(0, "+diff line\n")
        if cmd[:3] == ["gh", "pr", "view"]:
            return Result(0, "reviewer: looks good overall")
        return Result(1, "", "no such PR")

    monkeypatch.setattr(briefing, "_run", fake_run)
    return e


def test_write_briefing_creates_file(benv):
    path = briefing.write_briefing(14)
    assert path is not None and path.exists()
    text = path.read_text()
    assert "PR #14" in text and "Engine sync" in text
    assert "+diff line" in text
    assert "looks good overall" in text
    assert "abc123" in text  # head sha in the header


def test_write_briefing_gh_failure_returns_none(benv, monkeypatch):
    monkeypatch.setattr(briefing, "_run",
                        lambda cmd, timeout=120: Result(1, "", "boom"))
    assert briefing.write_briefing(14) is None
    assert not list(Path(briefing.BRIEF_DIR).glob("pr-*.md"))


def test_write_briefing_survives_missing_gh(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(briefing.subprocess, "run", boom)
    assert briefing.write_briefing(14) is None


def test_prepare_briefings_lists_only_written(benv, monkeypatch):
    def flaky(cmd, timeout=120):
        if cmd[:3] == ["gh", "pr", "view"] and "--json" in cmd:
            return Result(0, META) if "14" in cmd else Result(1, "", "404")
        return Result(0, "x")

    monkeypatch.setattr(briefing, "_run", flaky)
    note = briefing.prepare_briefings([14, 15])
    assert "pr-14.md" in note
    assert "pr-15.md" not in note
    assert "Read these local files" in note


def test_briefing_note_empty_without_prs():
    assert briefing.briefing_note([]) == ""


def test_cleanup_briefings_removes_closed_only(benv):
    briefing.BRIEF_DIR.mkdir(exist_ok=True)
    keep = briefing.BRIEF_DIR / "pr-14.md"
    gone = briefing.BRIEF_DIR / "pr-15.md"
    keep.write_text("a")
    gone.write_text("b")
    briefing.cleanup_briefings([14])
    assert keep.exists() and not gone.exists()


# --- log rotation ---


def test_rotate_logs_gzips_old_deletes_ancient_keeps_fresh(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(dispatch, "LOGDIR", tmp_path)
    now = time.time()
    fresh = tmp_path / "20261006-120000-impl.json"
    old = tmp_path / "20261001-120000-impl.json"
    ancient = tmp_path / "20260901-120000-impl.json.gz"
    fresh.write_text("{}")
    old.write_text("{}")
    ancient.write_text("x")
    os.utime(fresh, (now - 3600, now - 3600))
    os.utime(old, (now - 8 * 86400, now - 8 * 86400))
    os.utime(ancient, (now - 40 * 86400, now - 40 * 86400))

    dispatch.rotate_logs()

    assert fresh.exists()                       # recent: untouched
    assert not old.exists()                     # rotated away
    assert (tmp_path / "20261001-120000-impl.json.gz").exists()
    with gzip.open(tmp_path / "20261001-120000-impl.json.gz") as f:
        assert f.read() == b"{}"
    assert not ancient.exists()                 # past the delete horizon


def test_rotate_logs_never_raises_on_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "LOGDIR", tmp_path / "nope")
    dispatch.rotate_logs()  # must not raise


# --- hook wiring: tick must pass the briefing note into sessions ---


def test_review_session_prompt_carries_briefing_note(tmp_path, monkeypatch):
    e = SimpleNamespace()
    e.sent = []
    monkeypatch.setattr(dispatch, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(dispatch, "LOGDIR", tmp_path / "logs")
    monkeypatch.setattr(dispatch, "ROADMAP", tmp_path / "ROADMAP.md")
    monkeypatch.setattr(dispatch.notify, "notify",
                        lambda *a, **k: e.sent.append(a) or True)
    monkeypatch.setattr(dispatch, "run",
                        lambda cmd, timeout=120: Result())
    e.launches = []
    e.head = {"14": "sha1"}

    def fake_gh_json(cmd, timeout=120):
        if cmd[:3] == ["gh", "issue", "list"]:
            return []
        if cmd[:3] == ["gh", "run", "list"]:
            return []
        if cmd[:3] == ["gh", "pr", "list"]:
            return [{"number": 14, "title": "Engine sync", "labels": [],
                     "url": "u", "isDraft": False}]
        if cmd[:3] == ["gh", "pr", "view"]:
            what = cmd[cmd.index("--json") + 1]
            if what == "labels":
                return {"labels": []}
            if what == "headRefOid":
                return {"headRefOid": e.head["14"]}
        return None

    monkeypatch.setattr(dispatch, "gh_json", fake_gh_json)

    def fake_run_session(role, prompt):
        e.launches.append((role, prompt))
        if role == "impl":
            e.head["14"] = "sha2"  # rework pushes a new commit
        return dispatch.SessionResult(0)

    monkeypatch.setattr(dispatch, "run_session", fake_run_session)
    monkeypatch.setattr(dispatch, "briefing", SimpleNamespace(
        prepare_briefings=lambda nums: f"BRIEFING[{nums}]",
        cleanup_briefings=lambda nums: None))

    status = dispatch.tick(dry=False)
    assert status == "worked"
    role, prompt = e.launches[0]
    assert role == "qa"
    assert "BRIEFING[[14]]" in prompt

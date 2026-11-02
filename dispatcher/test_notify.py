"""notify(): an event is on disk, whole, before True is returned.

A blocker counts as announced once notify() returns True, so a False must
mean nothing was queued and a True must survive a crash of everything else.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import notify
import pytest


@pytest.fixture()
def spool(tmp_path, monkeypatch):
    d = tmp_path / "spool"
    monkeypatch.setenv("NOTIFY_SPOOL_DIR", str(d))
    monkeypatch.delenv("PROJECTS_DIR", raising=False)
    return d


def _events(d):
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


def test_event_has_what_the_reader_needs(spool):
    assert notify.notify("r: needs your answer", "#5 x\n\nmore", priority="high",
                         tags="warning", url="https://github.com/x/5") is True
    [ev] = _events(spool)
    assert ev["title"] == "r: needs your answer"
    assert ev["message"] == "#5 x\n\nmore"
    assert ev["priority"] == 4
    assert ev["url"] == "https://github.com/x/5"
    assert isinstance(ev["time"], float)


@pytest.mark.parametrize("given,stored", [("min", 1), ("default", 3),
                                          ("high", 4), ("nonsense", 3)])
def test_priorities(spool, given, stored):
    notify.notify("t", "b", priority=given)
    assert _events(spool)[0]["priority"] == stored


def test_unicode_survives(spool):
    # Regression 2026-10-07: an em-dash in a title lost the notification.
    title = "PR #28 failed — pausing \U0001f525 \udc80"
    assert notify.notify(title, "café") is True
    assert _events(spool)[0]["title"] == title


def test_names_sort_oldest_first_and_never_collide(spool):
    for i in range(50):
        notify.notify(f"t{i}", "b")
    assert [e["title"] for e in _events(spool)] == [f"t{i}" for i in range(50)]


def test_no_temp_file_left_and_readable_by_other_users(spool):
    notify.notify("t", "b")
    [p] = list(spool.iterdir())
    assert p.name.endswith(".json") and not p.name.startswith(".")
    assert p.stat().st_mode & 0o777 == 0o644


def test_failed_write_returns_false_and_leaves_nothing(spool, monkeypatch, capsys):
    spool.mkdir()

    def boom(fd):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(notify.os, "fsync", boom)
    assert notify.notify("t", "b") is False
    assert list(spool.iterdir()) == []
    assert "No space left" in capsys.readouterr().err


def test_unwritable_location_returns_false(tmp_path, monkeypatch):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    monkeypatch.setenv("NOTIFY_SPOOL_DIR", str(blocker / "spool"))
    assert notify.notify("t", "b") is False


def test_not_configured_is_a_silent_noop(monkeypatch, capsys):
    monkeypatch.delenv("NOTIFY_SPOOL_DIR", raising=False)
    monkeypatch.delenv("PROJECTS_DIR", raising=False)
    assert notify.notify("t", "b") is False
    assert capsys.readouterr().err == ""


def test_default_location_follows_projects_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFY_SPOOL_DIR", raising=False)
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    assert notify.notify("t", "b") is True
    assert len(_events(tmp_path / ".notify-spool")) == 1


def _cli(*args, env_extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("NOTIFY_SPOOL_DIR", "PROJECTS_DIR")}
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(Path(notify.__file__)), *args],
        env=env, capture_output=True, text=True)


def test_command_line_form(spool):
    r = _cli("--priority", "high", "--", "t —", "-starts with a dash",
             env_extra={"NOTIFY_SPOOL_DIR": str(spool)})
    assert r.returncode == 0, r.stderr
    [ev] = _events(spool)
    assert ev["title"] == "t —"
    assert ev["message"] == "-starts with a dash"
    assert ev["priority"] == 4


def test_command_line_reports_failure():
    r = _cli("--", "t", "b", env_extra={})
    assert r.returncode == 1
    assert "no spool configured" in r.stderr

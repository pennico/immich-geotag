"""Comms stability: blocker announcements must not be lost (ntfy down,
lost pushes, re-labelled blockers, gh failures). Uses the isolated
dispatcher fixture from test_dispatch.py."""
import json

import dispatch
import test_dispatch

env = test_dispatch.env      # the isolated-dispatcher fixture
state = test_dispatch.state


def _blocker(n=5, state_="OPEN", labels=()):
    return {"number": n, "title": "need input", "state": state_,
            "labels": [{"name": "blocker"}] + [{"name": x} for x in labels],
            "url": f"https://github.com/x/issues/{n}"}


def test_blocker_announce_failure_is_retried_next_tick(env, monkeypatch):
    e = env
    e.shelves.blockers = [_blocker()]
    monkeypatch.setattr(dispatch.notify, "notify", lambda *a, **k: False)
    assert dispatch.tick(False) == "blocked"
    assert 5 not in state(e)["seen_blockers"]      # ntfy down: not "seen"
    monkeypatch.setattr(dispatch.notify, "notify",
                        lambda *a, **k: e.sent.append(a) or True)
    assert dispatch.tick(False) == "blocked"
    assert 5 in state(e)["seen_blockers"]
    assert [a[0] for a in e.sent] == [f"{dispatch.REPO_NAME}: needs your answer"]


def test_unanswered_blocker_is_reminded_daily(env, monkeypatch):
    e = env
    e.shelves.blockers = [_blocker()]
    assert dispatch.tick(False) == "blocked"
    assert dispatch.tick(False) == "blocked"
    assert len(e.sent) == 1                        # no reminder yet
    st = state(e)
    st["blocker_reminded"]["5"] -= dispatch.BLOCKER_REMIND_S + 1
    dispatch.STATE_FILE.write_text(json.dumps(st))
    assert dispatch.tick(False) == "blocked"
    assert e.sent[-1][0] == f"{dispatch.REPO_NAME}: still waiting for your answer"
    assert dispatch.tick(False) == "blocked"
    assert len(e.sent) == 2                        # once per day


def test_blocker_list_failure_aborts(env, monkeypatch):
    real = dispatch.gh_json
    monkeypatch.setattr(dispatch, "gh_json", lambda cmd, timeout=120:
                        None if "blocker" in cmd else real(cmd, timeout))
    assert dispatch.tick(False) == "abort"
    assert env.launches == []


def test_reblock_makes_a_resumed_issue_answerable_again():
    st = {"seen_blockers": [5, 6], "resumed_blockers": [5],
          "blocker_reminded": {"5": 1.0}}
    dispatch.reblock(st, 5)
    assert st == {"seen_blockers": [6], "resumed_blockers": [],
                  "blocker_reminded": {}}


def test_main_ci_escalation_files_and_announces_blocker(env, monkeypatch):
    e = env
    dispatch.STATE_FILE.write_text(json.dumps({"main_ci_strikes": 2}))
    e.shelves.runs = [{"databaseId": 99, "status": "completed",
                       "conclusion": "failure", "displayTitle": "red"}]
    monkeypatch.setattr(dispatch, "file_blocker", lambda t, b: 12)
    dispatch.tick(False)                           # used to TypeError
    st = state(e)
    assert 12 in st["seen_blockers"]
    title, body = next(a[:2] for a in e.sent
                       if "CI keeps failing on main" in a[0])
    assert body.splitlines()[0].startswith("#12 ")  # Telegram reply target

"""Tests for the GitHub-only trigger model: engine-sync fast lane,
rework no-progress guard, and `run`-labeled direct script jobs.

Everything gh/git/vibe-shaped is mocked: no network, no repo side
effects. On-disk state is pointed at tmp_path.
"""
from types import SimpleNamespace

import dispatch
import pytest


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated dispatcher with a stateful fake GitHub."""
    e = SimpleNamespace()
    e.tmp = tmp_path
    monkeypatch.setattr(dispatch, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(dispatch, "LOGDIR", tmp_path / "logs")
    monkeypatch.setattr(dispatch, "ROADMAP", tmp_path / "ROADMAP.md")
    monkeypatch.setattr(dispatch.notify, "notify",
                        lambda *a, **k: e.sent.append(a) or True)
    e.sent = []
    e.gh_cmds = []
    e.runs = []          # non-gh commands (git etc.)
    e.launches = []      # (role, prompt) per run_session call
    e.results = {}       # role -> SessionResult or callable
    e.blockers = []      # file_blocker calls
    e.prs = {}           # number -> {"labels": set, "files": [...], "head": sha}
    e.issues = {"blocker": [], "task": [], "run": []}

    def fake_run(cmd, timeout=120):
        e.runs.append(cmd)
        return Result()

    monkeypatch.setattr(dispatch, "run", fake_run)

    def fake_gh_json(cmd, timeout=120):
        e.gh_cmds.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            label = cmd[cmd.index("--label") + 1]
            return e.issues.get(label, [])
        if cmd[:3] == ["gh", "run", "list"]:
            return []
        if cmd[:3] == ["gh", "pr", "list"]:
            return [{"number": n, "title": p["title"], "labels": [
                {"name": l} for l in p["labels"]],
                "url": f"u{n}", "isDraft": False}
                for n, p in e.prs.items()]
        if cmd[:3] == ["gh", "pr", "view"]:
            n = int(cmd[3])
            p = e.prs[n]
            what = cmd[cmd.index("--json") + 1]
            if what == "labels":
                return {"labels": [{"name": l} for l in p["labels"]]}
            if what == "files":
                return {"files": [{"path": f} for f in p["files"]]}
            if what == "headRefOid":
                return {"headRefOid": p["head"]}
        return None

    monkeypatch.setattr(dispatch, "gh_json", fake_gh_json)

    def fake_run_session(role, prompt):
        e.launches.append((role, prompt))
        res = e.results.get(role, dispatch.SessionResult(0))
        if callable(res):
            res = res(role, prompt)
        return res

    monkeypatch.setattr(dispatch, "run_session", fake_run_session)

    def fake_blocker(title, body):
        e.blockers.append(title)
        return 90 + len(e.blockers)

    monkeypatch.setattr(dispatch, "file_blocker", fake_blocker)

    # briefing module stubbed out: no real gh calls from tests
    monkeypatch.setattr(dispatch, "briefing", SimpleNamespace(
        prepare_briefings=lambda nums: "",
        cleanup_briefings=lambda nums: None))

    def recording_run(cmd, timeout=120):
        e.runs.append(cmd)
        if cmd[:3] == ["gh", "pr", "edit"] and "--add-label" in cmd:
            n = int(cmd[3])
            e.prs[n]["labels"].add(cmd[cmd.index("--add-label") + 1])
        return Result()

    monkeypatch.setattr(dispatch, "run", recording_run)
    return e


def add_pr(e, n, files, labels=(), head="sha1", title="T"):
    e.prs[n] = {"labels": set(labels), "files": files, "head": head,
                "title": title}


# --- is_engine_sync ---


def test_sync_detected_for_dispatcher_only_diff(env):
    add_pr(env, 14, ["dispatcher/dispatch.py", "dispatcher/README.md"])
    assert dispatch.is_engine_sync(14) is True


def test_sync_rejected_for_mixed_diff(env):
    add_pr(env, 15, ["dispatcher/dispatch.py", "src/app.py"])
    assert dispatch.is_engine_sync(15) is False


def test_sync_rejected_when_gh_fails(env):
    add_pr(env, 16, [])
    assert dispatch.is_engine_sync(16) is False


# --- fast lane: QA pass on a sync PR adds the adversarial label ---


def test_sync_qa_pass_fast_lanes_to_merge(env):
    add_pr(env, 14, ["dispatcher/dispatch.py"])

    def qa_approves(role, prompt):
        env.prs[14]["labels"].add("approved")  # review passed
        return dispatch.SessionResult(0)

    env.results = {"qa": qa_approves}
    status = dispatch.tick(dry=False)
    assert status == "worked"
    assert env.launches[0][0] == "qa"
    assert "approved-adversarial" in env.prs[14]["labels"]


def test_normal_pr_qa_pass_does_not_fast_lane(env):
    add_pr(env, 15, ["src/app.py"])
    dispatch.tick(dry=False)
    assert "approved" not in env.prs[15]["labels"]  # session did not approve
    assert "approved-adversarial" not in env.prs[15]["labels"]


def test_sync_with_approved_gets_adversarial_without_session(env):
    add_pr(env, 14, ["dispatcher/dispatch.py"], labels={"approved"})
    status = dispatch.tick(dry=False)
    assert status == "worked"
    assert env.launches == []  # no review session for a sync PR
    assert "approved-adversarial" in env.prs[14]["labels"]


# --- strike cap: sync PRs get one rework round, then a blocker ---


def test_sync_second_failure_escalates(env):
    add_pr(env, 14, ["dispatcher/dispatch.py"])
    state = {"qa_strikes": {"14": 1}}  # one round already burned
    dispatch.STATE_FILE.write_text(dispatch.json.dumps(state))
    status = dispatch.tick(dry=False)
    assert status == "blocked"
    assert env.blockers  # escalated instead of a second rework round


def test_normal_pr_uses_full_strike_budget(env):
    add_pr(env, 15, ["src/app.py"])

    def rework_pushed(role, prompt):
        env.prs[15]["head"] = "sha2"  # rework pushed: no no-progress blocker
        return dispatch.SessionResult(0)

    env.results = {"impl": rework_pushed}
    state = {"qa_strikes": {"15": 1}}
    dispatch.STATE_FILE.write_text(dispatch.json.dumps(state))
    status = dispatch.tick(dry=False)
    assert status == "worked"  # strike 2 of 2: rework still allowed
    assert not env.blockers


# --- no-progress guard ---


def test_rework_without_new_commits_escalates(env):
    add_pr(env, 14, ["src/app.py"])
    # QA fails (no label added), rework "succeeds" but pushes nothing
    # (head stays sha1) -> blocker, not another round
    status = dispatch.tick(dry=False)
    assert status == "blocked"
    assert any("no progress" in b for b in env.blockers)


def test_rework_with_new_commits_continues(env):
    add_pr(env, 14, ["src/app.py"])

    def rework_pushed(role, prompt):
        env.prs[14]["head"] = "sha2"  # rework pushed a new commit
        return dispatch.SessionResult(0)

    env.results = {"impl": rework_pushed}
    status = dispatch.tick(dry=False)
    assert status == "worked"
    assert not env.blockers


# --- run issues: direct script jobs ---


def test_run_issue_success_closes(env, monkeypatch):
    monkeypatch.setattr(dispatch.subprocess, "run",
                        lambda cmd, **k: Result(0, "all good"))
    issue = {"number": 40, "title": "monthly job", "body": "python3 job.py"}
    status = dispatch.execute_run_issue(issue, dry=False)
    assert status == "worked"
    closed = [c for c in env.runs if c[:3] == ["gh", "issue", "close"]]
    assert closed


def test_run_issue_failure_escalates_to_blocker(env, monkeypatch):
    monkeypatch.setattr(dispatch.subprocess, "run",
                        lambda cmd, **k: Result(3, "", "boom"))
    issue = {"number": 41, "title": "monthly job", "body": "python3 job.py"}
    dispatch.execute_run_issue(issue, dry=False)
    labeled = [c for c in env.runs
               if c[:3] == ["gh", "issue", "edit"] and "blocker" in c]
    assert labeled


def test_run_issue_invalid_body_closes(env, monkeypatch):
    monkeypatch.setattr(dispatch.subprocess, "run",
                        lambda cmd, **k: Result(0))
    issue = {"number": 42, "title": "broken", "body": "# just a comment"}
    dispatch.execute_run_issue(issue, dry=False)
    assert not any(c[:3] == ["gh", "issue", "edit"] and "blocker" in c
                   for c in env.runs)


def test_tick_executes_run_issue_before_tasks(env):
    env.issues["run"] = [{"number": 43, "title": "job",
                          "body": "python3 job.py"}]
    env.issues["task"] = [{"number": 44, "title": "task work"}]
    # execute_run_issue is dispatched before task sessions; verify the
    # tick short-circuits on the run issue without launching a session
    status = dispatch.tick(dry=True)
    assert status == "worked"
    assert env.launches == []

"""Tests for the agent backend switch (agent_backend.py): mode/role resolution,
the Claude argv (permission posture, caps), credential scrubbing and the
structured quota classification of Claude's result JSON."""
import json

import agent_backend as backend
import dispatch
import pytest


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("AGENT_BACKEND", "DISPATCH_CLAUDE_ALLOWED_TOOLS",
              "DISPATCH_CLAUDE_MODEL",
              *(f"AGENT_BACKEND_{r.upper()}" for r in backend.BOTH_ROLES)):
        monkeypatch.delenv(k, raising=False)


def test_default_is_vibe_only():
    for role in backend.BOTH_ROLES:
        assert backend.resolve(role) == ["vibe"]


@pytest.mark.parametrize("value", ["", "mistral", "BOTH ", "Claude"])
def test_mode_parsing(monkeypatch, value):
    monkeypatch.setenv("AGENT_BACKEND", value)
    expected = {"BOTH ": "both", "Claude": "claude"}.get(value, "vibe")
    assert backend.mode() == expected


def test_claude_mode_has_no_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "claude")
    assert backend.resolve("impl") == ["claude"]
    assert backend.resolve("adv") == ["claude"]


def test_both_mode_splits_roles_with_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "both")
    assert backend.resolve("impl") == ["vibe", "claude"]
    assert backend.resolve("qa") == ["vibe", "claude"]
    assert backend.resolve("adv") == ["claude", "vibe"]
    assert backend.resolve("verify") == ["claude", "vibe"]
    assert backend.resolve("overseer") == ["claude", "vibe"]


def test_role_override(monkeypatch):
    monkeypatch.setenv("AGENT_BACKEND", "both")
    monkeypatch.setenv("AGENT_BACKEND_QA", "claude")
    assert backend.resolve("qa") == ["claude", "vibe"]
    monkeypatch.setenv("AGENT_BACKEND", "vibe")
    monkeypatch.setenv("AGENT_BACKEND_ADV", "claude")
    assert backend.resolve("adv") == ["claude"]
    assert backend.resolve("impl") == ["vibe"]
    monkeypatch.setenv("AGENT_BACKEND_ADV", "gpt")  # invalid: ignored
    assert backend.resolve("adv") == ["vibe"]


def test_vibe_cmd_unchanged():
    assert backend.build_cmd("vibe", "p", "150", "10") == [
        "vibe", "-p", "p", "--auto-approve", "--trust",
        "--max-turns", "150", "--max-price", "10", "--output", "json"]


def test_claude_cmd_posture_and_caps():
    cmd = backend.build_cmd("claude", "p", "60", "10")
    assert cmd[:3] == ["claude", "-p", "p"]
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert cmd[cmd.index("--max-turns") + 1] == "60"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "10"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert not any("dangerously" in a or a == "bypassPermissions"
                   for a in cmd)
    tools = cmd[cmd.index("--allowedTools") + 1:cmd.index("--max-turns")]
    assert "Bash(git:*)" in tools and "Read" in tools
    assert not any(t in ("Bash", "Bash(*)", "WebFetch") for t in tools)


def test_claude_tools_and_model_from_env(monkeypatch):
    monkeypatch.setenv("DISPATCH_CLAUDE_ALLOWED_TOOLS", "Read Grep")
    monkeypatch.setenv("DISPATCH_CLAUDE_MODEL", "claude-opus-5-5")
    cmd = backend.build_cmd("claude", "p", "60", "10", add_dirs=["/x"])
    tools = cmd[cmd.index("--allowedTools") + 1:cmd.index("--max-turns")]
    assert tools == ["Read", "Grep"]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert cmd[-2:] == ["--add-dir", "/x"]


def test_vibe_never_sees_claude_credentials(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("GH_TOKEN", "gh")
    v = backend.session_env("vibe")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in v and "ANTHROPIC_API_KEY" not in v
    assert v["GH_TOKEN"] == "gh"
    assert backend.session_env("claude")["CLAUDE_CODE_OAUTH_TOKEN"] == "secret"


def _result(**kw):
    base = {"type": "result", "is_error": False, "result": "ok",
            "api_error_status": None, "total_cost_usd": 0.42}
    base.update(kw)
    return json.dumps(base)


@pytest.mark.parametrize("code,out,expected", [
    (1, _result(is_error=True, api_error_status=429, result="x"), True),
    (1, _result(is_error=True, api_error_status=402, result="x"), True),
    (1, _result(is_error=True, result="Usage limit exceeded"), True),
    (1, _result(is_error=True, result="error_max_turns"), False),
    (1, _result(is_error=False, result="the quota exceeded test"), False),
    (0, _result(is_error=True, api_error_status=429), False),
    (124, "", False),
    (1, "not json", False),
])
def test_claude_quota(code, out, expected):
    assert backend.claude_quota(code, out, dispatch.QUOTA_SIG) is expected


def test_claude_cost_and_result_text():
    out = "warning line\n" + _result(result=" report ")
    assert backend.claude_cost(out) == 0.42
    assert backend.result_text("claude", out) == "report"
    assert backend.claude_cost("garbage") is None
    assert backend.result_text("vibe", " text \n") == "text"


class _Proc:
    def __init__(self, code, out=""):
        self.returncode, self.out = code, out

    def communicate(self, timeout=None):
        return self.out, ""


def test_overseer_both_mode_claude_report_then_fallback(tmp_path, monkeypatch):
    overseer = pytest.importorskip("overseer")  # pennico-ops only
    monkeypatch.setattr(overseer, "LOGDIR", tmp_path)
    monkeypatch.setenv("AGENT_BACKEND", "both")
    seen = []
    procs = [_Proc(1, '{"is_error":true,"result":"boom"}'),
             _Proc(0, "vibe report\n")]
    monkeypatch.setattr(overseer.subprocess, "Popen",
                        lambda cmd, **k: seen.append(cmd) or procs.pop(0))
    assert overseer.run_session() == (0, "vibe report")
    assert [c[0] for c in seen] == ["claude", "vibe"]
    assert seen[0].count("--add-dir") == len(overseer.PROJECT_REPOS)


def test_overseer_claude_report_from_result_json(tmp_path, monkeypatch):
    overseer = pytest.importorskip("overseer")  # pennico-ops only
    monkeypatch.setattr(overseer, "LOGDIR", tmp_path)
    monkeypatch.setenv("AGENT_BACKEND", "claude")
    monkeypatch.setattr(overseer.subprocess, "Popen",
                        lambda cmd, **k: _Proc(0, _result(result="# Report")))
    assert overseer.run_session() == (0, "# Report")


def test_claude_cmd_registers_the_guard_hook():
    cmd = backend.build_cmd("claude", "p", "60", "10")
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    hook = settings["hooks"]["PreToolUse"][0]
    assert "Bash" in hook["matcher"] and "Read" in hook["matcher"]
    assert ".vibe/guard.py" in hook["hooks"][0]["command"]
    assert hook["hooks"][0]["command"].endswith("--claude")


def _guard(payload, *args):
    import subprocess
    import sys
    from pathlib import Path
    guard = Path(__file__).resolve().parent.parent / ".vibe" / "guard.py"
    r = subprocess.run([sys.executable, str(guard), *args],
                       input=json.dumps(payload), capture_output=True,
                       text=True, timeout=10)
    return json.loads(r.stdout) if r.stdout.strip() else None


def test_guard_claude_mode_denies_in_claude_format():
    out = _guard({"tool_name": "Bash",
                  "tool_input": {"command": "git push --force origin main"}},
                 "--claude")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    out = _guard({"tool_name": "Read", "tool_input": {"file_path": "/r/.env"}},
                 "--claude")
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _guard({"tool_name": "Bash", "tool_input": {"command": "git status"}},
                  "--claude") is None


def test_guard_vibe_mode_unchanged():
    out = _guard({"tool_name": "bash", "tool_input": {"command": "sudo ls"}})
    assert out["decision"] == "deny"
    assert _guard({"tool_name": "Bash", "tool_input": {"command": "sudo ls"}}) is None


def test_claude_gets_bash_rules_and_plumbing_tools(monkeypatch):
    monkeypatch.delenv("DISPATCH_CLAUDE_ALLOWED_TOOLS", raising=False)
    cmd = backend.build_cmd("claude", "do it", 10, 1)
    rules = cmd[cmd.index("--append-system-prompt") + 1]
    assert "ONE plain command" in rules and "do not stop" in rules
    tools = cmd[cmd.index("--allowedTools") + 1:cmd.index("--max-turns")]
    assert "Bash(cd:*)" in tools and "Bash(cut:*)" in tools
    assert not any(t.startswith(("Bash(env", "Bash(timeout", "Bash(bash", "Bash(sh"))
                   for t in tools)


def test_vibe_gets_no_claude_flags():
    cmd = backend.build_cmd("vibe", "do it", 10, 1)
    assert "--append-system-prompt" not in cmd

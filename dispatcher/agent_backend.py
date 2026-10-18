"""Agent backend switch: which CLI runs a session (Mistral vibe or Claude).

AGENT_BACKEND selects the mode for every role:
  vibe   (default) every session runs on vibe, as before;
  claude every session runs on Claude Code (`claude -p`);
  both   cross-model: vibe implements (impl/arch) and does the QA pass,
         Claude does the short adversarial + delivery-verification
         passes and the overseer. When one backend dies on an API limit
         or its CLI is missing, the session is retried once on the
         other backend.
AGENT_BACKEND_<ROLE> (IMPL, ARCH, QA, ADV, VERIFY, OVERSEER) pins one role
to vibe or claude; in `both` mode the other backend stays the fallback.

Claude runs with the repo's .vibe/guard.py as a PreToolUse hook (same
denies as vibe), --permission-mode dontAsk and an explicit tool
allow-list (DISPATCH_CLAUDE_ALLOWED_TOOLS): anything not listed is
denied, never prompted. Never --dangerously-skip-permissions: the
session environment carries GH_TOKEN and API keys. Cost parity with
vibe: the same --max-turns cap, DISPATCH_MAX_PRICE as --max-budget-usd,
and the result JSON's total_cost_usd is logged.

Auth for Claude: CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`) or
ANTHROPIC_API_KEY in the environment. Vibe sessions get neither.
"""
import json
import os

BACKENDS = ("vibe", "claude")
MODES = BACKENDS + ("both",)
# `both`: the long implementation work on the cheaper model, the short
# review passes on the other one (cross-model review catches more).
BOTH_ROLES = {"impl": "vibe", "arch": "vibe", "qa": "vibe",
              "adv": "claude", "verify": "claude", "overseer": "claude"}
CLAUDE_CREDS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
# What sessions actually do: read/edit the repo, git, gh, tests, lint.
# Bash entries are prefix rules; a command outside them is denied.
DEFAULT_CLAUDE_TOOLS = ("Read Edit Write Glob Grep TodoWrite "
                        "Bash(git:*) Bash(gh:*) Bash(python3:*) "
                        "Bash(/tmp/venv/bin/*) Bash(pytest:*) Bash(ruff:*) "
                        "Bash(ls:*) Bash(cat:*) Bash(head:*) Bash(tail:*) "
                        "Bash(wc:*) Bash(grep:*) Bash(find:*) Bash(sed:*) "
                        "Bash(mkdir:*) Bash(cp:*) Bash(mv:*) Bash(diff:*) "
                        # shell plumbing models chain into their commands;
                        # none of these can start another program
                        "Bash(cd:*) Bash(echo:*) Bash(cut:*) Bash(sort:*) "
                        "Bash(uniq:*) Bash(tr:*) Bash(pwd:*) Bash(jq:*) "
                        "Bash(test:*) Bash(true:*)")

# Appended to Claude's system prompt in every headless session. In
# dontAsk mode a command outside the allow-list is denied without a
# prompt, and every part of a compound command must match; sessions used
# to write `for r in ...; do cd ...; gh ... | cut ...; done`, get one
# denial and give up (2026-10-18: overseer, verify and task sessions).
CLAUDE_BASH_RULES = (
    "Unattended run: every Bash command is checked against an allow-list "
    "and anything else is denied without asking. Run ONE plain command per "
    "Bash call. No for/while loops, no subshells, no `cd ... &&` or `;` "
    "chains. Your working directory is already the repository; for other "
    "repositories use `git -C <dir>` and `gh ... -R pennico/<repo>`. Pipes "
    "only into head, tail, grep, sort, uniq, cut, tr, wc or jq. If a command "
    "is denied, do not stop: split it into simple commands and continue.")


# The repo's unattended-run guard (.vibe/guard.py: destructive bash, .env
# access) as a Claude PreToolUse hook — vibe loads it via --trust and
# .vibe/hooks.toml; Claude needs it passed explicitly.
GUARD_SETTINGS = {"hooks": {"PreToolUse": [{
    "matcher": "Bash|Read|Write|Edit|MultiEdit",
    "hooks": [{"type": "command", "timeout": 10,
               "command": 'python3 "$CLAUDE_PROJECT_DIR/.vibe/guard.py" '
                          "--claude"}]}]}}


def mode():
    m = os.environ.get("AGENT_BACKEND", "vibe").strip().lower()
    return m if m in MODES else "vibe"


def resolve(role):
    """Backends to try for a role, in order: [primary] or, in `both`
    mode, [primary, fallback]."""
    m = mode()
    pinned = os.environ.get(f"AGENT_BACKEND_{role.upper()}", "").strip().lower()
    if pinned in BACKENDS:
        primary = pinned
    elif m == "both":
        primary = BOTH_ROLES.get(role, "vibe")
    else:
        primary = m
    if m != "both":
        return [primary]
    return [primary, "claude" if primary == "vibe" else "vibe"]


def build_cmd(backend, prompt, max_turns, max_price, output="json",
              add_dirs=()):
    """argv for one headless session. `output` is vibe's --output value;
    Claude always emits JSON (result_text() extracts the reply)."""
    if backend == "claude":
        tools = os.environ.get("DISPATCH_CLAUDE_ALLOWED_TOOLS",
                               DEFAULT_CLAUDE_TOOLS).split()
        cmd = ["claude", "-p", prompt,
               "--permission-mode", "dontAsk",
               "--allowedTools", *tools,
               "--max-turns", str(max_turns),
               "--max-budget-usd", str(max_price),
               "--output-format", "json",
               "--no-session-persistence",
               "--settings", json.dumps(GUARD_SETTINGS),
               "--append-system-prompt", CLAUDE_BASH_RULES]
        model = os.environ.get("DISPATCH_CLAUDE_MODEL", "").strip()
        if model:
            cmd += ["--model", model]
    else:
        cmd = ["vibe", "-p", prompt, "--auto-approve", "--trust",
               "--max-turns", str(max_turns), "--max-price", str(max_price),
               "--output", output]
    for d in add_dirs:
        cmd += ["--add-dir", str(d)]
    return cmd


def session_env(backend):
    """Environment for the session: vibe never sees Claude credentials."""
    env = dict(os.environ)
    if backend == "vibe":
        for k in CLAUDE_CREDS:
            env.pop(k, None)
    return env


def _claude_result(stdout):
    """The `claude -p --output-format json` result object, {} if absent."""
    try:
        data = json.loads((stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claude_quota(code, stdout, sig):
    """API-limit death from Claude's structured result: the HTTP status
    (api_error_status 402/429), else the error result text matched
    against the dispatcher's quota signature. Only error results count:
    a successful reply that talks about quotas is not a quota death."""
    if code in (0, 124, None):
        return False
    res = _claude_result(stdout)
    if res.get("api_error_status") in (402, 429):
        return True
    return bool(res.get("is_error")) and bool(
        sig.search(str(res.get("result", ""))))


def claude_cost(stdout):
    """total_cost_usd from the result JSON, None when absent."""
    cost = _claude_result(stdout).get("total_cost_usd")
    return cost if isinstance(cost, (int, float)) else None


def result_text(backend, stdout):
    """The session's final reply text (the overseer posts it)."""
    if backend == "claude":
        return str(_claude_result(stdout).get("result", "")).strip()
    return (stdout or "").strip()


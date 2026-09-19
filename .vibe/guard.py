#!/usr/bin/env python3
"""pre_tool guard for unattended runs: deny destructive bash commands and
agent access to .env files. Receives the hook payload on stdin; emits a
structured deny response on stdout, or exits 0 silently to allow."""
import json
import re
import sys

DENY_BASH = [
    r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)",
    r"\bgit\s+push\b.*(--force|-f\b)",
    r"\bgit\s+reset\s+--hard",
    r"\bgit\s+clean\s+-[a-z]*f",
    r"\bgit\s+checkout\s+--\s+\S+\s*$",
    r"\bsudo\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bcurl\b[^|]*\|\s*(ba)?sh",
    r"\bwget\b[^|]*\|\s*(ba)?sh",
]
ENV_PATH = re.compile(r"(^|/)\.env($|\.)")


def deny(reason):
    print(json.dumps({"decision": "deny", "reason": reason}))


def main():
    try:
        hook = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail open on malformed input
    tool = hook.get("tool_name", "")
    inp = hook.get("tool_input") or {}
    if tool in ("read_file", "write_file", "edit"):
        path = str(inp.get("file_path", ""))
        if ENV_PATH.search(path):
            deny(".env files contain secrets and must not be read or edited by agents.")
            return
    if tool == "bash":
        cmd = str(inp.get("command", ""))
        for pat in DENY_BASH:
            if re.search(pat, cmd):
                deny(f"Blocked by the unattended-run guard (pattern: {pat}).")
                return
    sys.exit(0)  # allow


if __name__ == "__main__":
    main()

"""Client for the homelab agent-memory service (agent-runner side).

Scope model: worker sessions read/write "ops.<repo>" with MEMORY_TOKEN
(agent role); the daily overseer reads across scopes with
MEMORY_OVERSEER_TOKEN (read-any role, no write). job-bot uses its own
token for the "jobs" scope and is not handled here.

All calls are best-effort, mirroring briefing.py: a memory outage must
never block a tick — every function degrades to "" (blocks) or False
(save). The server enforces the hard caps (entry 500 chars, recent 20
entries / 4 KB); formatting here truncates again so prompts stay small.
"""
import json
import os
import time
import urllib.request

MEMORY_URL = os.environ.get("MEMORY_URL", "")
MEMORY_TOKEN = os.environ.get("MEMORY_TOKEN", "")
OVERSEER_TOKEN = os.environ.get("MEMORY_OVERSEER_TOKEN", "")
TIMEOUT_S = 5
BLOCK_MAX_ENTRIES = 20
BLOCK_MAX_CHARS = 4000


def _request(path, token, payload=None):
    if not (MEMORY_URL and token):
        return None
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        MEMORY_URL + path, data=data,
        method="POST" if data is not None else "GET",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read() or b"{}")
    except Exception:
        return None


def save(scope, kind, text):
    """Best-effort append; returns True if the server stored it."""
    return _request("/save", MEMORY_TOKEN,
                    {"scope": scope, "kind": kind, "text": text}) is not None


def _format(entries, include_scope):
    if not entries:
        return ""
    lines = ["", "Recent memory from earlier sessions (what was tried and "
             "what failed — do not redo failed approaches, do not re-read "
             "these entries):"]
    total = 0
    count = 0
    for e in entries:
        ts = time.strftime("%m-%d", time.localtime(e.get("ts", 0)))
        text = str(e.get("text", "")).replace("\n", " ")[:200]
        prefix = "[" + e.get("scope", "") + "] " if include_scope else ""
        line = "- {} {}({}) {}".format(ts, prefix, e.get("kind", "note"), text)
        if total + len(line) > BLOCK_MAX_CHARS or count >= BLOCK_MAX_ENTRIES:
            break
        lines.append(line)
        total += len(line)
        count += 1
    return "\n".join(lines) + "\n" if count else ""


def block(scope, n=BLOCK_MAX_ENTRIES):
    """'Recent memory' block for a worker session prompt, or ""."""
    entries = (_request("/recent?scope=" + scope + "&n=" + str(n),
                        MEMORY_TOKEN) or {}).get("entries", [])
    return _format(entries, include_scope=False)


def overseer_block(n=BLOCK_MAX_ENTRIES):
    """Cross-scope recent memory for the overseer prompt, or ""."""
    entries = (_request("/recent?scope=any&n=" + str(n),
                        OVERSEER_TOKEN) or {}).get("entries", [])
    return _format(entries, include_scope=True)

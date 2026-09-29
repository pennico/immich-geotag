#!/usr/bin/env python3
"""Best-effort push notifications via ntfy (outbound HTTPS only).

Set NTFY_TOPIC in the environment (optionally NTFY_SERVER to self-host).
Without a topic, notify() is a silent no-op. Failures are logged to stderr
and never raised: notifications are best-effort, GitHub issues are the
durable record.
"""
import os
import sys
import urllib.request

SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
TOPIC = os.environ.get("NTFY_TOPIC", "")
PRIORITIES = {"min": "1", "default": "3", "high": "4"}


def notify(title, body, priority="default", tags="", url=""):
    """Send one push notification. Returns True if ntfy accepted it."""
    if not TOPIC:
        return False
    headers = {
        "X-Title": title,
        "X-Priority": PRIORITIES.get(priority, PRIORITIES["default"]),
    }
    if tags:
        headers["X-Tags"] = tags
    if url:
        headers["Actions"] = f"view, Open, {url}"
    req = urllib.request.Request(
        f"{SERVER}/{TOPIC}", data=body.encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        print(f"notify: send failed: {exc}", file=sys.stderr, flush=True)
        return False

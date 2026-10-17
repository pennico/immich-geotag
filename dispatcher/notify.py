#!/usr/bin/env python3
"""Best-effort push notifications via ntfy (outbound HTTPS only).

Set NTFY_TOPIC in the environment (optionally NTFY_SERVER to self-host,
NTFY_TOKEN if the instance requires auth). Without a topic, notify() is a
silent no-op. Failures are logged to stderr and never raised: notifications
are best-effort, GitHub issues are the durable record. A send that fails on
the network, a 5xx or a 429 is retried (RETRY_DELAYS_S); a 4xx is final
(auth/ACL errors do not heal by retrying). Callers that must not lose a
notification (blocker announcements) check the return value and retry on
a later tick.
"""
import os
import sys
import time
import urllib.error
import urllib.request

SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
TOPIC = os.environ.get("NTFY_TOPIC", "")
TOKEN = os.environ.get("NTFY_TOKEN", "")
PRIORITIES = {"min": "1", "default": "3", "high": "4"}
RETRY_DELAYS_S = (2, 5)   # pauses between the up-to-3 attempts
_sleep = time.sleep       # patched in tests

# HTTP headers are latin-1; titles and tags carry agent prose with
# em-dashes and curly quotes that would crash the send (2026-10-07:
# "'latin-1' codec can't encode character '\\u2014'"). Transliterate
# the common punctuation, replace anything else.
_ASCII_MAP = {"\u2014": "--", "\u2013": "-", "\u2018": "'",
              "\u2019": "'", "\u201c": '"', "\u201d": '"',
              "\u2026": "...", "\u2192": "->", "\u00a0": " "}


def _header(value):
    """Latin-1-safe header value: transliterate, then replace the rest."""
    out = "".join(_ASCII_MAP.get(ch, ch) for ch in value)
    return out.encode("latin-1", "replace").decode("latin-1")


def notify(title, body, priority="default", tags="", url=""):
    """Send one push notification. Returns True if ntfy accepted it."""
    if not TOPIC:
        return False
    headers = {
        "X-Title": _header(title),
        "X-Priority": PRIORITIES.get(priority, PRIORITIES["default"]),
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if tags:
        headers["X-Tags"] = _header(tags)
    if url:
        # Click: tap target in the app and the field the Telegram bridge
        # forwards; Actions: the explicit "Open" button.
        headers["Click"] = _header(url)
        headers["Actions"] = _header(f"view, Open, {url}")
    for attempt in range(len(RETRY_DELAYS_S) + 1):
        req = urllib.request.Request(
            f"{SERVER}/{TOPIC}", data=body.encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            final = 400 <= exc.code < 500 and exc.code != 429
            print(f"notify: send failed: {exc}", file=sys.stderr, flush=True)
            if final:
                return False
        except Exception as exc:
            print(f"notify: send failed: {exc}", file=sys.stderr, flush=True)
        if attempt < len(RETRY_DELAYS_S):
            _sleep(RETRY_DELAYS_S[attempt])
    return False

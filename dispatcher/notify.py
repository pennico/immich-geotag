#!/usr/bin/env python3
"""Durable notifications: one JSON file per event in a spool directory.

notify() writes the event atomically and returns True once it is on disk.
A separate process (the Telegram bridge in this setup) drains the directory
and deletes each file after delivery, so an event raised while that process
or its network is down is delivered later. Nothing here needs a network or a
credential.

The directory is NOTIFY_SPOOL_DIR, else $PROJECTS_DIR/.notify-spool. With
neither set, notify() is a silent no-op that returns False. A failed write is
logged to stderr and never raised: GitHub issues are the durable record.
Callers that must not lose a notification (blocker announcements) check the
return value and retry on a later tick.

Shell jobs use the command line form:
    python3 notify.py --priority high -- "title" "body"
which exits non-zero if the event could not be written.
"""
import argparse
import json
import os
import secrets
import sys
import tempfile
import time

PRIORITIES = {"min": 1, "default": 3, "high": 4}


def spool_dir():
    """The spool directory from the environment, or "" when not configured."""
    explicit = os.environ.get("NOTIFY_SPOOL_DIR", "")
    if explicit:
        return explicit
    projects = os.environ.get("PROJECTS_DIR", "")
    return os.path.join(projects, ".notify-spool") if projects else ""


def notify(title, body, priority="default", tags="", url=""):
    """Queue one notification. Returns True once it is safely on disk."""
    spool = spool_dir()
    if not spool:
        return False
    event = {"title": title, "message": body, "tags": tags, "url": url,
             "priority": PRIORITIES.get(priority, PRIORITIES["default"]),
             "time": time.time()}
    # Sorted by name is oldest first. The rename publishes the file whole,
    # and the reader skips the dot-prefixed temp name.
    name = f"{time.time_ns():020d}-{os.getpid()}-{secrets.token_hex(4)}.json"
    tmp = None
    try:
        os.makedirs(spool, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=spool)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            json.dump(event, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.rename(tmp, os.path.join(spool, name))
        tmp = None
        dir_fd = os.open(spool, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        print(f"notify: could not queue {title!r}: {exc}",
              file=sys.stderr, flush=True)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False
    return True


def main(argv=None):
    p = argparse.ArgumentParser(description="Queue one notification.")
    p.add_argument("title")
    p.add_argument("body")
    p.add_argument("--priority", choices=sorted(PRIORITIES), default="default")
    p.add_argument("--url", default="")
    args = p.parse_args(argv)
    if notify(args.title, args.body, priority=args.priority, url=args.url):
        return 0
    if not spool_dir():
        print("notify: no spool configured (NOTIFY_SPOOL_DIR or PROJECTS_DIR)",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

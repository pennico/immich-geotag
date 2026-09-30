#!/usr/bin/env python3
"""Monthly geotag runner: chip away at unlocated Immich photos, free.

Google Cloud Vision gives 1,000 free landmark-detection units per
CALENDAR month (the allowance resets each month; it is not a rolling
window). This runner spends that allowance automatically:

  1. Count units already used this calendar month (geotag-data/usage.json)
     and compute the remaining free budget. Nothing runs once the budget
     is spent.
  2. Scan Immich for missing-GPS images (newest first) and pick the first
     N never-processed ones (the ledger geotag-data/state.csv records
     every asset ever run through recognition, so not_found photos are
     never re-spend quota on).
  3. Recognize them via `geotag.py recognize` (same engine, same scoring).
  4. Apply every located row back to Immich via `geotag.py apply` —
     coordinates only; photos are never modified or deleted.
  5. Record the spend and notify via ntfy (NTFY_TOPIC in the
     environment; silent no-op without one).

Safe to re-run at any time: resumable, idempotent, and it never exceeds
the monthly budget. Manual runs of geotag.py against the same Vision key
are NOT seen by the usage ledger — add them to geotag-data/usage.json by
hand or use --limit to stay safe.

Usage:
  python3 monthly_geotag.py             # process up to the free budget
  python3 monthly_geotag.py --limit 5   # testing: at most 5 images
  python3 monthly_geotag.py --dry       # scan + plan only, no API calls

Environment (beyond geotag.py's own): GEOTAG_MONTHLY_FREE (default 1000),
NTFY_SERVER / NTFY_TOPIC / NTFY_TOKEN for the summary notification.
"""
import csv
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import geotag  # noqa: E402

DATA_DIR = SCRIPT_DIR / "geotag-data"
STATE_CSV = DATA_DIR / "state.csv"
USAGE_FILE = DATA_DIR / "usage.json"
BATCH_CSV = DATA_DIR / "batch.csv"
REPORTS_DIR = DATA_DIR / "reports"
FREE_PER_MONTH = int(os.environ.get("GEOTAG_MONTHLY_FREE", "1000"))


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def month_key():
    return datetime.now().strftime("%Y-%m")


def load_usage():
    if USAGE_FILE.exists():
        try:
            return json.loads(USAGE_FILE.read_text())
        except ValueError:
            log("usage.json unreadable; starting fresh")
    return {}


def save_usage(usage):
    DATA_DIR.mkdir(exist_ok=True)
    USAGE_FILE.write_text(json.dumps(usage, indent=2, sort_keys=True) + "\n")


def load_state_ids():
    """asset_ids ever processed (any status) — the never-retry ledger."""
    if not STATE_CSV.exists():
        return set()
    with open(STATE_CSV) as f:
        return {r["asset_id"] for r in csv.DictReader(f)}


def merge_batch_into_state():
    """Fold recognition results from batch.csv into the state ledger."""
    with open(BATCH_CSV) as f:
        batch = {r["asset_id"]: r for r in csv.DictReader(f)}
    if STATE_CSV.exists():
        with open(STATE_CSV) as f:
            rows = list(csv.DictReader(f))
    else:
        rows = []
    seen = set()
    for row in rows:
        aid = row["asset_id"]
        if aid in batch:
            rows[rows.index(row)] = batch[aid]
        seen.add(aid)
    rows += [r for aid, r in batch.items() if aid not in seen]
    with open(STATE_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=geotag.CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def write_report(results, processed, located, applied, usage_total):
    """Write this month's markdown report (one per calendar month).

    Published to Nextcloud by the homelab's publish_geotag_outputs.sh
    so results are readable from the phone over Tailscale.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Geotag monthly run — {month_key()}",
        "",
        f"- Processed: {processed} images",
        f"- Located: {located}",
        f"- Applied to Immich: {applied}",
        f"- Vision units used this month: {usage_total} of {FREE_PER_MONTH}",
        "",
    ]
    found = [r for r in results if r.get("status") in ("located", "applied")]
    if found:
        lines += ["## Locations found", "",
                  "| Image | Place | Score | Maps |",
                  "|---|---|---|---|"]
        for r in found:
            place = r.get("place") or "—"
            maps = (f"[map]({r['maps_url']})" if r.get("maps_url") else "—")
            lines.append(f"| {r.get('file_name', '')} | {place} "
                         f"| {r.get('score', '')} | {maps} |")
    path = REPORTS_DIR / f"{month_key()}.md"
    path.write_text("\n".join(lines) + "\n")
    log(f"report written: {path}")


def notify(title, body, priority="default"):
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    topic = os.environ.get("NTFY_TOPIC", "")
    if not topic:
        return
    headers = {"X-Title": title,
               "X-Priority": {"min": "1", "default": "3",
                              "high": "4"}.get(priority, "3")}
    token = os.environ.get("NTFY_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{server}/{topic}",
                                 data=body.encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:
        log(f"ntfy notify failed (not fatal): {exc}")


def main():
    dry = "--dry" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    geotag.load_env()
    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")
    if not (immich_url and immich_key):
        sys.exit("IMMICH_URL / IMMICH_API_KEY not configured (see .env.example)")
    if not os.environ.get("GOOGLE_VISION_API_KEY", ""):
        sys.exit("GOOGLE_VISION_API_KEY not configured (see .env.example)")

    usage = load_usage()
    used = usage.get(month_key(), 0)
    budget = max(0, FREE_PER_MONTH - used)
    if limit is not None:
        budget = min(budget, limit)
    log(f"Vision units used in {month_key()}: {used}; free budget: {budget}")
    if budget == 0:
        notify("immich-geotag: monthly budget spent",
               f"{used} units already used in {month_key()}; "
               "nothing to do this month.")
        return 0

    immich = geotag.Immich(immich_url, immich_key)
    done = load_state_ids()
    log(f"ledger: {len(done)} assets already processed; scanning Immich "
        "(newest first) for unprocessed ones...")

    batch_rows, remaining_after = [], None
    for asset in immich.assets_missing_geo():
        aid = asset["id"]
        if aid in done:
            continue
        if len(batch_rows) < budget:
            batch_rows.append({
                "asset_id": aid,
                "file_name": asset.get("originalFileName", aid),
                "latitude": "", "longitude": "", "place": "",
                "source": "", "score": "",
                "status": "pending", "maps_url": "",
            })
        else:
            remaining_after = True
            break
    if not batch_rows:
        log("nothing to do: no unprocessed missing-GPS images")
        notify("immich-geotag: nothing to do",
               "No unprocessed missing-GPS images found in Immich.")
        return 0
    log(f"selected {len(batch_rows)} images "
        f"({remaining_after and 'more remain after this batch' or 'this is the last batch'})")

    DATA_DIR.mkdir(exist_ok=True)
    with open(BATCH_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=geotag.CSV_COLUMNS)
        w.writeheader()
        w.writerows(batch_rows)

    if dry:
        log("dry run: batch written, stopping before any API call")
        for r in batch_rows[:5]:
            log(f"  would process: {r['file_name']}")
        return 0

    r = subprocess.run([sys.executable, str(SCRIPT_DIR / "geotag.py"),
                        "recognize", str(BATCH_CSV)])
    if r.returncode != 0:
        log("recognize failed; merging partial results and stopping")
    merge_batch_into_state()

    with open(BATCH_CSV) as f:
        results = list(csv.DictReader(f))
    located = sum(1 for r in results if r["status"] == "located")
    processed = sum(1 for r in results if r["status"] != "pending")

    r = subprocess.run([sys.executable, str(SCRIPT_DIR / "geotag.py"),
                        "apply", str(STATE_CSV)])
    applied = 0
    if STATE_CSV.exists():
        with open(STATE_CSV) as f:
            applied = sum(1 for r in csv.DictReader(f)
                          if r["status"] == "applied"
                          and r["asset_id"] in {x["asset_id"] for x in results})

    usage[month_key()] = used + processed
    save_usage(usage)
    log(f"done: {processed} processed, {located} located, "
        f"{applied} applied; usage {month_key()}: {usage[month_key()]}")
    write_report(results, processed, located, applied, usage[month_key()])
    notify("immich-geotag: monthly run done",
           f"{processed} images processed, {located} located, "
           f"{applied} locations written to Immich.\n"
           f"Vision units used in {month_key()}: {usage[month_key()]} "
           f"of {FREE_PER_MONTH}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

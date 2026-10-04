#!/usr/bin/env python3
"""gemini_apply: approve and apply Gemini location candidates to Immich.

Companion to gemini_candidates.py. Three stages, all persisted so nothing
is ever re-run:

  1. Enrich: fetch each candidate's photo timestamp from Immich and store
     it in the candidates CSV (taken_at column) - fetched once, kept
     forever.
  2. Infer: for photos with no confident match, apply the owner's
     temporal-coherence rule - if photos taken within a couple of hours
     before/after show the same location (or a nearby, coherent one),
     infer that location; otherwise leave as no_match. Inferred rows are
     marked (status=inferred, inferred_from=<anchor asset_id>) and never
     serve as anchors for further inference.
  3. Apply: merge candidate + inferred rows into state.csv as located
     rows and run geotag.py's apply subcommand - the only code path that
     writes to Immich (idempotent, crash-safe, --dry-run supported).

Usage:
  python3 gemini_apply.py --dry-run     # plan only, no writes
  python3 gemini_apply.py               # infer + apply
  python3 gemini_apply.py --no-infer    # apply confident candidates only
"""

import argparse
import csv
import math
import os
import sys
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import geotag  # noqa: E402
from gemini_candidates import CANDIDATE_COLUMNS  # noqa: E402

STATE_PATH = os.path.join(SCRIPT_DIR, "geotag-data", "state.csv")
CANDIDATES_PATH = os.path.join(SCRIPT_DIR, "geotag-data", "gemini_candidates.csv")
TIMES_PATH = os.path.join(SCRIPT_DIR, "geotag-data", "asset_times.csv")

APPLY_STATUSES = ("candidate", "inferred", "located", "ready")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_time(value):
    """Parse an Immich timestamp into an aware datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km between two points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def load_times_cache(path):
    """Load the persistent asset_id -> taken_at cache."""
    cache = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("asset_id") and row.get("taken_at"):
                    cache[row["asset_id"]] = row["taken_at"]
    return cache


def save_times_cache(path, cache):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["asset_id", "taken_at"])
        w.writeheader()
        w.writerows({"asset_id": k, "taken_at": v} for k, v in sorted(cache.items()))


def fetch_taken_at(immich, asset_ids, cache):
    """Return {asset_id: taken_at}, fetching only what the cache lacks."""
    result = {}
    for asset_id in asset_ids:
        if asset_id in cache:
            result[asset_id] = cache[asset_id]
            continue
        a = immich.get_asset(asset_id)
        exif = a.get("exifInfo") or {}
        taken_at = exif.get("dateTimeOriginal") or a.get("fileCreatedAt") or ""
        cache[asset_id] = taken_at
        result[asset_id] = taken_at
        time.sleep(0.05)  # be gentle on the Immich API
    return result


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------

def infer_locations(rows, hours=2.0, km=10.0):
    """Fill temporal-coherence inferences into a list of candidate rows.

    Anchors are rows that have coordinates from recognition (candidate or
    applied Gemini rows) - never rows inferred here. A no_match row gets
    its nearest-in-time anchor's location when every anchor within
    +-hours is within km of that anchor (one coherent stop, e.g. two
    nearby beaches). Mutates and returns rows.
    """
    anchors = [r for r in rows
               if r.get("latitude") and r.get("longitude")
               and r.get("status") in ("candidate", "applied")]
    for a in anchors:
        a["_time"] = parse_time(a.get("taken_at"))
        a["_lat"] = float(a["latitude"])
        a["_lon"] = float(a["longitude"])
    anchors = [a for a in anchors if a["_time"] is not None]

    inferred = 0
    for row in rows:
        if row.get("status") != "no_match":
            continue
        t = parse_time(row.get("taken_at"))
        if t is None:
            continue
        nearby = [a for a in anchors if abs((a["_time"] - t).total_seconds()) <= hours * 3600]
        if not nearby:
            continue
        nearest = min(nearby, key=lambda a: abs((a["_time"] - t).total_seconds()))
        coherent = all(
            haversine_km(nearest["_lat"], nearest["_lon"], a["_lat"], a["_lon"]) <= km
            for a in nearby)
        if not coherent:
            continue
        row["latitude"] = nearest["latitude"]
        row["longitude"] = nearest["longitude"]
        row["place"] = nearest.get("place", "")
        row["geocoded_name"] = nearest.get("geocoded_name", "")
        row["maps_url"] = nearest.get("maps_url") or (
            "https://www.google.com/maps?q={:.5f},{:.5f}".format(
                float(row["latitude"]), float(row["longitude"])))
        row["confidence"] = nearest.get("confidence", "")
        row["status"] = "inferred"
        row["inferred_from"] = nearest["asset_id"]
        inferred += 1
    return rows, inferred


# ---------------------------------------------------------------------------
# State ledger merge
# ---------------------------------------------------------------------------

def merge_into_state(state_rows, cand_rows):
    """Copy candidate/inferred coordinates into state.csv rows (located).

    Returns (state_rows, merged_count). Existing applied rows are never
    touched.
    """
    by_id = {r.get("asset_id"): r for r in state_rows}
    merged = 0
    for c in cand_rows:
        if c.get("status") not in ("candidate", "inferred"):
            continue
        if not c.get("latitude") or not c.get("longitude"):
            continue
        s = by_id.get(c.get("asset_id"))
        if s is None:
            continue
        if s.get("status") == "applied":
            continue
        s["latitude"] = c["latitude"]
        s["longitude"] = c["longitude"]
        s["place"] = c.get("place", "")
        s["source"] = "gemini-inferred" if c["status"] == "inferred" else "gemini"
        s["score"] = c.get("confidence", "")
        s["status"] = "located"
        merged += 1
    return state_rows, merged


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--candidates", default=CANDIDATES_PATH)
    parser.add_argument("--state", default=STATE_PATH)
    parser.add_argument("--times-cache", default=TIMES_PATH)
    parser.add_argument("--hours", type=float, default=2.0,
                        help="temporal window for inference (default 2)")
    parser.add_argument("--km", type=float, default=10.0,
                        help="coherence radius for inference (default 10)")
    parser.add_argument("--no-infer", action="store_true",
                        help="skip the temporal-coherence inference")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only: no writes to Immich")
    args = parser.parse_args()

    geotag.load_env()
    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")
    if not immich_url or not immich_key:
        sys.exit("Missing IMMICH_URL / IMMICH_API_KEY configuration")
    immich = geotag.Immich(immich_url, immich_key)

    cand_rows = geotag.read_csv(args.candidates)
    state_rows = geotag.read_csv(args.state)
    if not cand_rows:
        sys.exit("No rows in {}".format(args.candidates))

    # 1. Enrich: persistent timestamp cache
    cache = load_times_cache(args.times_cache)
    needed = [r["asset_id"] for r in cand_rows if not r.get("taken_at")]
    # state.csv applied rows are potential anchors too - cache their times
    needed += [r["asset_id"] for r in state_rows
               if r.get("status") == "applied" and r["asset_id"] not in cache]
    if needed:
        print("fetching {} photo timestamps from Immich...".format(
            len(set(needed))))
        times = fetch_taken_at(immich, [a for a in dict.fromkeys(needed)], cache)
        for row in cand_rows:
            if not row.get("taken_at"):
                row["taken_at"] = times.get(row["asset_id"], "")
        save_times_cache(args.times_cache, cache)
    # state.csv applied anchors: attach times via cache for inference
    for s in state_rows:
        if s.get("status") == "applied" and s["asset_id"] in cache:
            s["taken_at"] = cache[s["asset_id"]]

    # 2. Infer
    inferred = 0
    if not args.no_infer:
        n_no_match = sum(1 for r in cand_rows if r.get("status") == "no_match")
        # anchors include Vision-applied state rows (with cached times)
        anchor_rows = [dict(s, latitude=s.get("latitude", ""),
                            longitude=s.get("longitude", ""),
                            status="applied", place=s.get("place", ""),
                            confidence=s.get("score", ""))
                       for s in state_rows
                       if s.get("status") == "applied" and s.get("taken_at")]
        combined = cand_rows + anchor_rows
        combined, inferred = infer_locations(combined, args.hours, args.km)
        cand_rows = combined[:len(cand_rows)]
        print("inference: {} of {} no_match rows located from nearby photos".format(
            inferred, n_no_match))

    # 3. Persist candidates CSV, merge into state, apply via geotag.py
    geotag.write_csv(args.candidates, cand_rows, CANDIDATE_COLUMNS)
    state_rows, merged = merge_into_state(state_rows, cand_rows)
    geotag.write_csv(args.state, state_rows)

    n_candidates = sum(1 for r in cand_rows if r.get("status") == "candidate")
    print("ready to apply: {} confident candidates, {} inferred, "
          "{} merged into state.csv".format(n_candidates, inferred, merged))

    apply_args = argparse.Namespace(csv=args.state, dry_run=args.dry_run)
    geotag.cmd_apply(apply_args)

    # 4. Sync candidates CSV statuses with what apply actually wrote
    if not args.dry_run:
        state_after = {r.get("asset_id"): r for r in geotag.read_csv(args.state)}
        for row in cand_rows:
            s = state_after.get(row["asset_id"])
            if s and s.get("status") == "applied" and row.get("status") in APPLY_STATUSES:
                row["status"] = "applied"
        geotag.write_csv(args.candidates, cand_rows, CANDIDATE_COLUMNS)
        save_times_cache(args.times_cache, cache)
    print("done")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""gemini_apply: approve and apply Gemini location candidates to Immich.

Companion to gemini_candidates.py. Stages, all persisted so nothing is
ever re-run:

  1. Enrich: fetch each candidate's photo timestamp from Immich and store
     it in the candidates CSV (taken_at column) - fetched once, kept
     forever.
  2. Recover: high-confidence proposals (>= --min-confidence) whose geocode
     failed at scan time are re-geocoded with the trip-geography validator.
  3. Confirm: uncertain proposals (--floor <= confidence < --min-confidence)
     are applied ONLY when the photo's own Gemini-proposed location is
     confirmed by neighboring photos - every located photo within +-hours
     must sit within km of the proposal. The proposal's own coordinates
     are used, never the neighbor's. No proposal or a very low match
     means the geotag stays blank.
  4. Flag: small groups (<= --max-group photos of one place) that are
     temporally isolated from all other located photos are flagged for
     owner review instead of applied. Candidates that sit farther than
     --geo-km from ALL located photos taken within +-hours are flagged
     too (geography coherence: a generic place name can geocode to a
     same-named place on another continent).
  5. Apply: merge candidate/confirmed rows into state.csv and run
     geotag.py's apply subcommand - the only code path that writes to
     Immich (idempotent, crash-safe, --dry-run supported).

Usage:
  python3 gemini_apply.py --dry-run     # plan only, no writes
  python3 gemini_apply.py               # recover + confirm + apply
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
from gemini_candidates import (  # noqa: E402
    CANDIDATE_COLUMNS,
    geocode_place,
    parse_bbox,
    geo_allowed,
)

STATE_PATH = os.path.join(SCRIPT_DIR, "geotag-data", "state.csv")
CANDIDATES_PATH = os.path.join(SCRIPT_DIR, "geotag-data", "gemini_candidates.csv")
TIMES_PATH = os.path.join(SCRIPT_DIR, "geotag-data", "asset_times.csv")

APPLY_STATUSES = ("candidate", "confirmed", "located", "ready")


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
# Recovery + confirmation
# ---------------------------------------------------------------------------

def _anchors(rows):
    """Located rows that can confirm an uncertain proposal."""
    anchors = []
    for r in rows:
        if not r.get("latitude") or not r.get("longitude"):
            continue
        if r.get("status") not in ("candidate", "applied"):
            continue
        t = parse_time(r.get("taken_at"))
        if t is None:
            continue
        anchors.append((t, float(r["latitude"]), float(r["longitude"])))
    return anchors


def recover_and_confirm(rows, email, min_confidence, floor, hours, km, validator):
    """Fill in locations for no_match rows that carry a Gemini proposal.

    Two paths, both using the photo's OWN proposal (never a neighbor's
    coordinates):
    - confidence >= min_confidence: re-geocode (these failed geocoding at
      scan time); geography-validated, no coherence needed.
    - floor <= confidence < min_confidence: uncertain - additionally
      require every located photo within +-hours to sit within km of the
      proposal (similar place AND time to previous/following pictures).
    No proposal, or below floor: stays blank.
    """
    anchors = _anchors(rows)
    geocode_cache = {}
    recovered = confirmed = 0

    for row in rows:
        if row.get("status") != "no_match" or not row.get("place"):
            continue
        try:
            conf = float(row.get("confidence") or 0)
        except ValueError:
            continue
        if conf < floor:
            continue

        key = (row["place"], row.get("city", ""), row.get("country", ""))
        if key not in geocode_cache:
            geocode_cache[key] = geocode_place(
                row["place"], row.get("city", ""), row.get("country", ""),
                email, validate=validator)
        geo = geocode_cache[key]
        if not geo:
            continue
        lat, lon, name = geo

        if conf < min_confidence:
            # Uncertain: the proposal must be corroborated by neighboring
            # photos in both time and place.
            t = parse_time(row.get("taken_at"))
            if t is None:
                continue
            nearby = [a for a in anchors
                      if abs((a[0] - t).total_seconds()) <= hours * 3600]
            if not nearby:
                continue
            if not all(haversine_km(lat, lon, a[1], a[2]) <= km for a in nearby):
                continue

        row["latitude"] = str(lat)
        row["longitude"] = str(lon)
        row["geocoded_name"] = name
        row["maps_url"] = "https://www.google.com/maps?q={:.5f},{:.5f}".format(lat, lon)
        row["inferred_from"] = ""
        if conf >= min_confidence:
            row["status"] = "candidate"
            recovered += 1
        else:
            row["status"] = "confirmed"
            confirmed += 1
    return rows, recovered, confirmed


# ---------------------------------------------------------------------------
# Outlier flagging
# ---------------------------------------------------------------------------

def flag_outliers(rows, max_group=2, isolation_hours=12):
    """Flag small place-groups that are temporally isolated (owner's rule).

    "If Gemini proposes only a few pictures in an otherwise unmatched
    location, flag it for review." A group of <= max_group photos of one
    place is an outlier when no located photo OUTSIDE the group was taken
    within +-isolation_hours of it. Flagged rows are not applied; already
    applied ones keep their status but carry flag=outlier for the review
    page. Rows the owner already reviewed (flag=reviewed) are never
    re-flagged.
    """
    located = [r for r in rows
               if r.get("latitude") and r.get("status") in
               ("candidate", "confirmed", "applied")]
    times = {}
    for r in rows:
        t = parse_time(r.get("taken_at"))
        if t is not None:
            times[id(r)] = t

    groups = {}
    for r in located:
        if r.get("flag") == "reviewed":
            continue  # owner already approved - never re-flag
        groups.setdefault(r.get("place", ""), []).append(r)

    flagged = 0
    for place, members in groups.items():
        if len(members) > max_group:
            continue
        member_ids = {id(r) for r in members}
        isolated = True
        for m in members:
            mt = times.get(id(m))
            if mt is None:
                continue
            for other in located:
                if id(other) in member_ids:
                    continue
                ot = times.get(id(other))
                if ot is None:
                    continue
                if abs((ot - mt).total_seconds()) <= isolation_hours * 3600:
                    isolated = False
                    break
            if not isolated:
                break
        if isolated:
            for m in members:
                m["flag"] = "outlier"
                if m["status"] in ("candidate", "confirmed"):
                    m["status"] = "flagged"
                flagged += 1
    return rows, flagged


# ---------------------------------------------------------------------------
# Geography-coherence outliers
# ---------------------------------------------------------------------------

def flag_geo_outliers(rows, hours=12, km=100):
    """Flag located rows whose coordinates contradict every located photo
    taken within +-hours (the owner's outlier rule, generalized to
    distance instead of place names).

    Outside a known trip region there is no geography bound, and a
    generic place name can geocode to a same-named place on another
    continent ("Golden Triangle Park" -> Ottawa instead of Thailand).
    A candidate that sits more than km from ALL its temporal neighbors
    is flagged for owner review instead of applied. Rows with no located
    neighbors within the window are left alone - the place-name
    isolation flagger covers lonely photos. Owner-reviewed rows
    (flag=reviewed) are never flagged.
    """
    located = [r for r in rows
               if r.get("latitude") and r.get("status") in
               ("candidate", "confirmed", "applied")]
    times = {}
    for r in located:
        t = parse_time(r.get("taken_at"))
        if t is not None:
            times[id(r)] = t

    flagged = 0
    for r in located:
        if r.get("flag") == "reviewed":
            continue
        t = times.get(id(r))
        if t is None:
            continue
        neighbors = [o for o in located
                     if o is not r and id(o) in times
                     and abs((times[id(o)] - t).total_seconds()) <= hours * 3600]
        if not neighbors:
            continue
        lat, lon = float(r["latitude"]), float(r["longitude"])
        if all(haversine_km(lat, lon, float(o["latitude"]),
                            float(o["longitude"])) > km for o in neighbors):
            r["flag"] = "geo-outlier"
            if r["status"] in ("candidate", "confirmed"):
                r["status"] = "flagged"
            flagged += 1
    return rows, flagged


# ---------------------------------------------------------------------------
# State ledger merge
# ---------------------------------------------------------------------------

def merge_into_state(state_rows, cand_rows):
    """Copy candidate/confirmed coordinates into state.csv rows (located).

    Returns (state_rows, merged_count). Existing applied rows are never
    touched.
    """
    by_id = {r.get("asset_id"): r for r in state_rows}
    merged = 0
    for c in cand_rows:
        if c.get("status") not in ("candidate", "confirmed"):
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
        s["source"] = "gemini-confirmed" if c["status"] == "confirmed" else "gemini"
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
    parser.add_argument("--min-confidence", type=float, default=0.5,
                        help="confidence at or above which a proposal is trusted")
    parser.add_argument("--floor", type=float, default=0.3,
                        help="proposals below this confidence are left blank")
    parser.add_argument("--hours", type=float, default=2.0,
                        help="temporal window for confirmation (default 2)")
    parser.add_argument("--km", type=float, default=10.0,
                        help="coherence radius for confirmation (default 10)")
    parser.add_argument("--max-group", type=int, default=2,
                        help="place groups this small may be outliers (default 2)")
    parser.add_argument("--isolation-hours", type=float, default=12.0,
                        help="hours of solitude before a small group is flagged (default 12)")
    parser.add_argument("--geo-hours", type=float, default=12.0,
                        help="temporal window for geography coherence (default 12)")
    parser.add_argument("--geo-km", type=float, default=100.0,
                        help="a candidate farther than this from ALL its "
                             "temporal neighbors is flagged (default 100)")
    parser.add_argument("--allow-bbox", action="append", default=[],
                        metavar="LAT_MIN,LON_MIN,LAT_MAX,LON_MAX",
                        help="allowed trip region; geocodes outside are disregarded")
    parser.add_argument("--airport-bbox", action="append", default=[],
                        metavar="LAT_MIN,LON_MIN,LAT_MAX,LON_MAX",
                        help="extra allowed region for airport-named places")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan only: no writes to Immich")
    args = parser.parse_args()

    geotag.load_env()
    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")
    email = os.environ.get("NOMINATIM_EMAIL")
    if not immich_url or not immich_key:
        sys.exit("Missing IMMICH_URL / IMMICH_API_KEY configuration")
    immich = geotag.Immich(immich_url, immich_key)

    allow_bboxes = [b for b in (parse_bbox(x) for x in args.allow_bbox) if b]
    airport_bboxes = [b for b in (parse_bbox(x) for x in args.airport_bbox) if b]
    validator = None
    if allow_bboxes or airport_bboxes:
        def validator(lat, lon, name):  # noqa: F811
            return geo_allowed(lat, lon, "", name, allow_bboxes, airport_bboxes)

    cand_rows = geotag.read_csv(args.candidates)
    state_rows = geotag.read_csv(args.state)
    if not cand_rows:
        sys.exit("No rows in {}".format(args.candidates))

    # 1. Enrich: persistent timestamp cache
    cache = load_times_cache(args.times_cache)
    needed = [r["asset_id"] for r in cand_rows if not r.get("taken_at")]
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
    for s in state_rows:
        if s.get("status") == "applied" and s["asset_id"] in cache:
            s["taken_at"] = cache[s["asset_id"]]

    # 2-3. Recover failed geocodes + confirm uncertain proposals
    cand_rows, recovered, confirmed = recover_and_confirm(
        cand_rows, email, args.min_confidence, args.floor,
        args.hours, args.km, validator)
    print("recovered {} high-confidence proposals, confirmed {} uncertain ones".format(
        recovered, confirmed))

    # 4. Flag outliers
    cand_rows, flagged = flag_outliers(
        cand_rows, args.max_group, args.isolation_hours)
    print("flagged {} photos in small isolated groups for review".format(flagged))
    cand_rows, geo_flagged = flag_geo_outliers(
        cand_rows, args.geo_hours, args.geo_km)
    print("flagged {} photos contradicting their temporal neighbors".format(
        geo_flagged))

    # 5. Persist candidates CSV, merge into state, apply via geotag.py
    geotag.write_csv(args.candidates, cand_rows, CANDIDATE_COLUMNS)
    state_rows, merged = merge_into_state(state_rows, cand_rows)
    geotag.write_csv(args.state, state_rows)

    n_ready = sum(1 for r in cand_rows if r.get("status") in ("candidate", "confirmed"))
    print("ready to apply: {} rows ({} merged into state.csv)".format(n_ready, merged))

    apply_args = argparse.Namespace(csv=args.state, dry_run=args.dry_run)
    geotag.cmd_apply(apply_args)

    # 6. Sync candidates CSV statuses with what apply actually wrote
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

#!/usr/bin/env python3
"""time_fill: fill missing GPS from photos taken nearby in time.

Fourth location source for immich-geotag, after Cloud Vision/Lens, the
Gemini pass and the CLIP cluster pass. No recognition at all: photos from
the same shooting session are taken seconds or minutes apart, so a photo
without GPS can simply inherit the coordinates of the nearest-in-time
photo that has them.

Rule (conservative, add-only):
  proposal  an IMAGE asset with no latitude/longitude whose timestamp is
            within --window-min (10) minutes of at least one located
            photo. The located photo closest in time donates its
            coordinates. Nothing is written to Immich at scan time;
            proposals land in geotag-data/time-fill/proposals.csv with
            status "review".

Every applied edit is appended to geotag-data/time-fill/edits.csv
(timestamp, asset, source neighbor, coordinates) so any edit can be
traced and adjusted later. Existing locations are never touched: only
assets with no latitude/longitude are ever proposed.

Usage:
  python3 time_fill.py                     # scan + write proposals
  python3 time_fill.py --window-min 5      # tighter time window
  python3 time_fill.py --apply-approved --dry-run
  python3 time_fill.py --apply-approved    # after owner culls the CSV
"""
import argparse
import bisect
import csv
import math
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import geotag  # noqa: E402

DATA = os.path.join(SCRIPT_DIR, "geotag-data", "time-fill")
LEDGER = os.path.join(DATA, "proposals.csv")
EDITS = os.path.join(DATA, "edits.csv")
CLUSTERS = os.path.join(DATA, "clusters.csv")

LEDGER_COLUMNS = [
    "asset_id", "file_name", "taken_at", "latitude", "longitude",
    "neighbor_id", "neighbor_name", "neighbor_taken_at", "delta_seconds",
    "n_neighbors", "spread_m", "status", "maps_url", "immich_url",
    "run_date",
]

EDIT_COLUMNS = [
    "run_date", "asset_id", "file_name", "neighbor_id", "neighbor_name",
    "delta_seconds", "latitude", "longitude",
]

CLUSTER_COLUMNS = [
    "cluster_id", "asset_id", "role", "file_name", "taken_at",
    "latitude", "longitude", "donor_id", "geotag_lat", "geotag_lon",
    "spread_m",
]

# Clusters whose located photos sit farther apart than this mix
# different places (scan/import batches with unreliable timestamps)
# and must never be applied as one geotag.
MIXED_SPREAD_M = 10_000.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two lat/lon points."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_epoch(value):
    """Immich timestamp string -> epoch seconds (UTC), or None.

    Handles the formats Immich returns for exifInfo.dateTimeOriginal and
    fileCreatedAt: ISO 8601 with Z or offset, and 'YYYY-MM-DD HH:MM:SS'
    with optional fractional seconds. Naive timestamps are treated as UTC.
    """
    if not value:
        return None
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def maps_url(lat, lon):
    return f"https://www.google.com/maps?q={lat},{lon}"


# ---------------------------------------------------------------------------
# Scan and propose
# ---------------------------------------------------------------------------

def scan_library(immich):
    """All IMAGE assets as (id, file_name, epoch, lat, lon, taken_raw).

    Read-only. epoch is the EXIF original date when present, else the
    file created date; None when neither parses. lat/lon are None when
    missing. taken_raw is the original timestamp string, kept for the
    ledger and the review app.
    """
    assets = []
    page = 1
    while True:
        data = immich._search_metadata(page, 1000)
        items = (data.get("assets") or {}).get("items") or []
        for a in items:
            if a.get("type") != "IMAGE":
                continue
            exif = a.get("exifInfo") or {}
            taken_raw = (exif.get("dateTimeOriginal")
                        or a.get("fileCreatedAt") or "")
            epoch = parse_epoch(exif.get("dateTimeOriginal"))
            if epoch is None:
                epoch = parse_epoch(a.get("fileCreatedAt"))
            assets.append((a["id"], a.get("originalFileName", ""), epoch,
                           exif.get("latitude"), exif.get("longitude"),
                           taken_raw))
        if not items or (data.get("assets") or {}).get("nextPage") is None:
            return assets
        page += 1


def build_proposals(assets, window_s=600.0):
    """Propose coordinates for missing-GPS assets from near-in-time photos.

    Returns (proposals, stats). Each proposal is a dict with the ledger
    columns minus status/maps_url/immich_url/run_date. The neighbor is
    the located photo closest in time within the window; spread_m is the
    distance from it to the farthest other located photo in the window
    (0.0 when it is the only one) - a large spread means the located
    photos in the window do not agree, e.g. taken while moving.
    """
    located = sorted((a for a in assets
                      if a[2] is not None and a[3] is not None
                      and a[4] is not None),
                     key=lambda a: a[2])
    epochs = [a[2] for a in located]
    proposals = []
    stats = {"located": len(located), "missing": 0, "no_time": 0,
             "no_neighbor": 0, "proposals": 0}
    for a in assets:
        if a[3] is not None and a[4] is not None:
            continue
        stats["missing"] += 1
        if a[2] is None:
            stats["no_time"] += 1
            continue
        i = bisect.bisect_left(epochs, a[2] - window_s)
        j = bisect.bisect_right(epochs, a[2] + window_s)
        if i == j:
            stats["no_neighbor"] += 1
            continue
        window = located[i:j]
        best = min(window, key=lambda n: abs(n[2] - a[2]))
        spread = max(haversine_m(best[3], best[4], n[3], n[4])
                    for n in window)
        proposals.append({
            "asset_id": a[0], "file_name": a[1], "taken_at": a[5],
            "latitude": best[3], "longitude": best[4],
            "neighbor_id": best[0], "neighbor_name": best[1],
            "neighbor_taken_at": best[5],
            "delta_seconds": abs(best[2] - a[2]),
            "n_neighbors": len(window), "spread_m": spread,
        })
    stats["proposals"] = len(proposals)
    return proposals, stats


def run(window_min=10.0):
    geotag.load_env()
    immich = geotag.Immich(os.environ["IMMICH_URL"],
                           os.environ["IMMICH_API_KEY"])
    assets = scan_library(immich)
    proposals, stats = build_proposals(assets, window_min * 60.0)

    ledger = geotag.read_csv(LEDGER)
    decided = {r["asset_id"] for r in ledger
               if r.get("status") in ("applied", "rejected")}
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_url = os.environ.get("IMMICH_URL", "").rstrip("/")

    rows = [r for r in ledger
            if r.get("status") in ("applied", "rejected")]
    for p in proposals:
        if p["asset_id"] in decided:
            continue
        r = {col: "" for col in LEDGER_COLUMNS}
        r.update({
            "asset_id": p["asset_id"], "file_name": p["file_name"],
            "taken_at": p["taken_at"],
            "latitude": "{:.6f}".format(p["latitude"]),
            "longitude": "{:.6f}".format(p["longitude"]),
            "neighbor_id": p["neighbor_id"],
            "neighbor_name": p["neighbor_name"],
            "neighbor_taken_at": p["neighbor_taken_at"],
            "delta_seconds": "{:.0f}".format(p["delta_seconds"]),
            "n_neighbors": str(p["n_neighbors"]),
            "spread_m": "{:.0f}".format(p["spread_m"]),
            "status": "review",
            "maps_url": maps_url(p["latitude"], p["longitude"]),
            "immich_url": f"{base_url}/photos/{p['asset_id']}",
            "run_date": run_date,
        })
        rows.append(r)

    os.makedirs(DATA, exist_ok=True)
    geotag.write_csv(LEDGER, rows, LEDGER_COLUMNS)

    print(f"scanned {len(assets)} images: {stats['located']} with GPS, "
          f"{stats['missing']} without")
    print(f"missing GPS: {stats['no_time']} without a usable timestamp, "
          f"{stats['no_neighbor']} with no located photo within "
          f"{window_min:g} min")
    print(f"proposals: {stats['proposals']} "
          f"({len(decided)} applied/rejected rows kept) -> "
          f"{os.path.relpath(LEDGER, SCRIPT_DIR)}")
    return stats


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def append_edits(rows):
    """Append applied edits to the edit log (create with header)."""
    write_header = not os.path.exists(EDITS)
    os.makedirs(DATA, exist_ok=True)
    with open(EDITS, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EDIT_COLUMNS,
                                extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def apply_approved(dry_run=False, max_spread_m=None):
    """Apply owner-approved proposals from the ledger.

    Every ledger row with status "review" is written to Immich, its
    status becomes "applied", and each edit is appended to the edit log.
    Idempotent: re-running applies nothing new. The owner culls rejected
    rows from proposals.csv first (or ticks the good ones in the review
    app and pastes back the list). With max_spread_m only proposals
    whose located-photo window agrees within that many meters are
    applied; the rest stay review.
    """
    ledger = geotag.read_csv(LEDGER)
    approved = [r for r in ledger if r.get("status") == "review"
                and (max_spread_m is None
                     or float(r.get("spread_m") or 0) <= max_spread_m)]
    if not approved:
        print("no approved proposals in the ledger")
        return
    print(f"applying {len(approved)} approved proposals...")
    geotag.load_env()
    immich = None
    if not dry_run:
        immich = geotag.Immich(os.environ["IMMICH_URL"],
                               os.environ["IMMICH_API_KEY"])
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    edits = []
    for r in approved:
        if not dry_run:
            immich.set_location(r["asset_id"], float(r["latitude"]),
                                float(r["longitude"]))
        r["status"] = "applied"
        r["run_date"] = run_date
        edits.append({
            "run_date": run_date, "asset_id": r["asset_id"],
            "file_name": r["file_name"], "neighbor_id": r["neighbor_id"],
            "neighbor_name": r["neighbor_name"],
            "delta_seconds": r["delta_seconds"],
            "latitude": r["latitude"], "longitude": r["longitude"],
        })
    if dry_run:
        print(f"dry run: {len(edits)} locations would be applied")
        return
    geotag.write_csv(LEDGER, ledger, LEDGER_COLUMNS)
    append_edits(edits)
    print(f"applied {len(edits)} locations to Immich "
          f"(edit log: {os.path.relpath(EDITS, SCRIPT_DIR)})")


# ---------------------------------------------------------------------------
# Cluster apply (group review flow)
# ---------------------------------------------------------------------------

def read_clusters(path=CLUSTERS):
    """clusters.csv -> ordered {cluster_id: cluster dict}.

    One row per member photo; cluster summary columns repeat on every
    row. Members keep file order (time order within the cluster).
    """
    rows = geotag.read_csv(path)
    groups = {}
    for r in rows:
        cid = r["cluster_id"]
        g = groups.setdefault(cid, {
            "cluster_id": cid, "members": [], "donor_id": r["donor_id"],
            "geotag_lat": float(r["geotag_lat"]),
            "geotag_lon": float(r["geotag_lon"]),
            "spread_m": float(r["spread_m"]),
        })
        g["members"].append({
            "asset_id": r["asset_id"], "role": r["role"],
            "file_name": r["file_name"], "taken_at": r["taken_at"],
        })
    return groups


def apply_clusters(cluster_ids, clusters_path=CLUSTERS, dry_run=False,
                   source="time-cluster"):
    """Apply one geotag per selected cluster (group review flow).

    For each cluster ID, every member photo still awaiting review gets
    the cluster geotag written to Immich, its ledger row becomes
    "applied", and the edit is appended to the edit log with the cluster
    ID as source. Idempotent: already-applied photos are skipped.
    Clusters whose located photos are spread wider than
    MIXED_SPREAD_M are refused: they mix different places and one
    geotag would be wrong for most of them.
    Returns (n_applied, {cluster_id: n_applied}).
    """
    groups = read_clusters(clusters_path)
    unknown = [c for c in cluster_ids if c not in groups]
    if unknown:
        raise SystemExit("unknown cluster IDs: " + ", ".join(unknown))
    mixed = [c for c in cluster_ids
             if groups[c]["spread_m"] > MIXED_SPREAD_M]
    if mixed:
        raise SystemExit(
            "refusing mixed-location clusters (spread > "
            "{:.0f} km): {}".format(MIXED_SPREAD_M / 1000.0,
                                    ", ".join(mixed)))
    ledger = geotag.read_csv(LEDGER)
    by_id = {r["asset_id"]: r for r in ledger}
    todo = []
    for cid in cluster_ids:
        g = groups[cid]
        for m in g["members"]:
            if m["role"] != "missing":
                continue
            r = by_id.get(m["asset_id"])
            if r is not None and r.get("status") == "applied":
                continue
            todo.append((cid, m, g))
    if not todo:
        print("nothing to apply")
        return 0, {}
    n_clusters = len({t[0] for t in todo})
    print(f"applying {len(todo)} photos across {n_clusters} clusters...")
    geotag.load_env()
    immich = None
    if not dry_run:
        immich = geotag.Immich(os.environ["IMMICH_URL"],
                               os.environ["IMMICH_API_KEY"])
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    edits = []
    per_cluster = {}
    for cid, m, g in todo:
        if not dry_run:
            immich.set_location(m["asset_id"], g["geotag_lat"],
                                g["geotag_lon"])
        r = by_id.get(m["asset_id"])
        if r is not None:
            r["status"] = "applied"
            r["run_date"] = run_date
        per_cluster[cid] = per_cluster.get(cid, 0) + 1
        edits.append({
            "run_date": run_date, "asset_id": m["asset_id"],
            "file_name": m["file_name"], "neighbor_id": cid,
            "neighbor_name": source, "delta_seconds": "",
            "latitude": "{:.6f}".format(g["geotag_lat"]),
            "longitude": "{:.6f}".format(g["geotag_lon"]),
        })
    if dry_run:
        print(f"dry run: {len(todo)} locations would be applied "
              f"across {n_clusters} clusters")
        return len(todo), per_cluster
    geotag.write_csv(LEDGER, ledger, LEDGER_COLUMNS)
    append_edits(edits)
    print(f"applied {len(todo)} locations to Immich "
          f"(edit log: {os.path.relpath(EDITS, SCRIPT_DIR)})")
    return len(todo), per_cluster


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window-min", type=float, default=10.0,
                    help="time window around a missing-GPS photo to look "
                         "for located photos, in minutes (default 10)")
    ap.add_argument("--apply-approved", action="store_true",
                    help="apply proposals with status review from the "
                         "ledger (after owner review)")
    ap.add_argument("--apply-clusters", metavar="IDS", default=None,
                    help="comma-separated cluster IDs from the group "
                         "review app; applies one geotag per cluster")
    ap.add_argument("--clusters", default=CLUSTERS,
                    help="clusters.csv path for --apply-clusters")
    ap.add_argument("--max-spread-m", type=float, default=None,
                    help="with --apply-approved: only apply proposals "
                         "whose located-photo window agrees within this "
                         "many meters")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --apply-approved: print what would be "
                         "applied without writing")
    args = ap.parse_args()
    if args.apply_clusters is not None:
        ids = [x.strip() for x in args.apply_clusters.split(",") if x.strip()]
        apply_clusters(ids, clusters_path=args.clusters,
                       dry_run=args.dry_run)
    elif args.apply_approved:
        apply_approved(dry_run=args.dry_run, max_spread_m=args.max_spread_m)
    else:
        run(window_min=args.window_min)


if __name__ == "__main__":
    main()

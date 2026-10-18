#!/usr/bin/env python3
"""Similarity-based geotag clusters for photos missing GPS.

Consumes the CLIP match dump (cluster_matches.sql run on the Immich
host) and groups unlocated photos by visual similarity. Donor photos
are grouped by their coordinates (single linkage, 1 km), and every
unlocated photo that can be pinned to one location group joins that
group's cluster: at least 2 matches with similarity >= 0.90 in the
same location group, or a single match with similarity >= 0.96.

A photo assigned to a different location always lands in a different
cluster - clusters are location groups by construction, so a cluster
can never mix places (unlike the time-based groups).

Writes geotag-data/similar-fill/clusters.csv (same schema as the
time-fill clusters) so the existing review app and apply path work
unchanged, and adds review rows to the time-fill ledger for photos
not already in it. Nothing is written to Immich from here; apply
happens through the review app.

Usage:
  python3 similar_fill.py [--matches PATH] [--min-sim 0.90]
      [--solo-sim 0.96] [--loc-group-m 1000]
"""
import argparse
import os
import re
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import cluster_fill  # noqa: E402
import geotag  # noqa: E402
import time_fill  # noqa: E402

DATA = os.path.join(SCRIPT_DIR, "geotag-data", "similar-fill")
CLUSTERS = os.path.join(DATA, "clusters.csv")
TIME_DATA = os.path.join(SCRIPT_DIR, "geotag-data", "similar-time")
TIME_CLUSTERS = os.path.join(TIME_DATA, "clusters.csv")
LOOSE_DATA = os.path.join(SCRIPT_DIR, "geotag-data", "similar-loose")
LOOSE_CLUSTERS = os.path.join(LOOSE_DATA, "clusters.csv")
VLOOSE_DATA = os.path.join(SCRIPT_DIR, "geotag-data", "similar-vloose")
VLOOSE_CLUSTERS = os.path.join(VLOOSE_DATA, "clusters.csv")
SAMEDAY_DATA = os.path.join(SCRIPT_DIR, "geotag-data", "similar-sameday")
SAMEDAY_CLUSTERS = os.path.join(SAMEDAY_DATA, "clusters.csv")
WA_DATA = os.path.join(SCRIPT_DIR, "geotag-data", "similar-wa")
WA_CLUSTERS = os.path.join(WA_DATA, "clusters.csv")
MATCHES = os.path.join(SCRIPT_DIR, "geotag-data", "cluster", "matches.csv")
MATCHES_LOOSE = os.path.join(SCRIPT_DIR, "geotag-data", "cluster",
                             "matches_loose.csv")
MATCHES_VLOOSE = os.path.join(SCRIPT_DIR, "geotag-data", "cluster",
                             "matches_vloose.csv")


# ---------------------------------------------------------------------------
# Location grouping
# ---------------------------------------------------------------------------

def _union_find(nodes):
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    return find, union


def group_locations(candidates_by_asset, loc_group_m=1000.0, complete=False):
    """Grouping of donor photos by coordinates.

    candidates_by_asset: {asset_id: [match dicts]}. Returns (find, coords):
    find(match_id) -> stable location-group key, coords maps
    match_id -> (lat, lon).

    Default is single linkage: donors within loc_group_m of any other
    donor join its group (chains can span farther than loc_group_m).
    With complete=True a donor joins a group only when it is within
    loc_group_m of *every* member, so a group can never chain - its
    extent stays under loc_group_m at the cost of some fragmentation.
    """
    coords = {}
    for matches in candidates_by_asset.values():
        for m in matches:
            coords[m["match_id"]] = (m["latitude"], m["longitude"])
    if complete:
        groups = {}   # representative id -> set of member ids
        rep_of = {}
        for d in sorted(coords):
            best_rep, best_near = None, None
            for rep, members in groups.items():
                dists = [cluster_fill.haversine_m(
                    coords[d][0], coords[d][1],
                    coords[m][0], coords[m][1]) for m in members]
                if max(dists) <= loc_group_m:
                    near = min(dists)
                    if best_near is None or near < best_near:
                        best_rep, best_near = rep, near
            if best_rep is not None:
                groups[best_rep].add(d)
                rep_of[d] = best_rep
            else:
                groups[d] = {d}
                rep_of[d] = d

        def find(match_id, _rep=rep_of):
            return _rep[match_id]

        return find, coords
    ids = sorted(coords, key=lambda i: coords[i][0])
    find, union = _union_find(ids)
    # 1 degree of latitude is ~111 km; skip pairs that cannot be close.
    lat_span = loc_group_m / 111_000.0
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if coords[b][0] - coords[a][0] > lat_span:
                break
            if cluster_fill.haversine_m(coords[a][0], coords[a][1],
                                       coords[b][0], coords[b][1]) \
                    <= loc_group_m:
                union(a, b)
    return find, coords


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

def _utc_date(epoch):
    """Epoch seconds -> UTC calendar date (None when no timestamp)."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).date()


def assign(groups, min_sim=0.90, solo_sim=0.96, loc_group_m=1000.0,
           max_dt_s=None, complete=False, same_day=False):
    """Pin each unlocated photo to one location group, or leave it out.

    Returns (assignments, find, coords, stats). assignments maps
    asset_id -> {"match": best match dict, "n_agree": candidates in the
    same location group}. A photo is assigned when its best-similarity
    match's location group holds at least 2 of its candidates, or when
    the best match alone reaches solo_sim.

    Time constraint: with max_dt_s set, a match only counts when both
    photos carry a timestamp and the two are at most max_dt_s apart
    (the 10-minute rule). With same_day=True the rule widens to "both
    timestamps fall on the same UTC calendar day" instead. Photos
    without a usable timestamp are left out entirely either way.
    """
    candidates = {}
    stats = {"unlocated": len(groups), "no_candidates": 0,
             "outside_window": 0, "disagree": 0, "assigned": 0}
    for aid, g in groups.items():
        ms = [m for m in g["matches"] if m["similarity"] >= min_sim]
        if not ms:
            stats["no_candidates"] += 1
            continue
        if same_day:
            ms = [m for m in ms
                  if _utc_date(g["taken_at"]) is not None
                  and _utc_date(m["match_taken_at"]) is not None
                  and _utc_date(g["taken_at"])
                  == _utc_date(m["match_taken_at"])]
            if not ms:
                stats["outside_window"] += 1
                continue
        elif max_dt_s is not None:
            ms = [m for m in ms
                  if g["taken_at"] is not None
                  and m["match_taken_at"] is not None
                  and abs(g["taken_at"] - m["match_taken_at"]) <= max_dt_s]
            if not ms:
                stats["outside_window"] += 1
                continue
        candidates[aid] = ms
    find, coords = group_locations(candidates, loc_group_m, complete)
    assignments = {}
    for aid, ms in candidates.items():
        ms = sorted(ms, key=lambda m: -m["similarity"])
        best = ms[0]
        key = find(best["match_id"])
        agreeing = [m for m in ms if find(m["match_id"]) == key]
        if len(agreeing) >= 2 or best["similarity"] >= solo_sim:
            assignments[aid] = {"match": best, "n_agree": len(agreeing),
                                "group": key, "agreeing": agreeing}
            stats["assigned"] += 1
        else:
            stats["disagree"] += 1
    return assignments, find, coords, stats


WA_NAME_RE = re.compile(r"IMG-?\d{8}-WA\d+", re.IGNORECASE)


def assign_wa(groups, min_sim=0.70, loc_group_m=300.0):
    """Pin whole WhatsApp send batches to one location, or skip them.

    WhatsApp photos sent in one bulk share an identical file
    timestamp (the send time), so they form batches keyed by exact
    timestamp. Each batch pools the located matches of all its
    members (similarity >= min_sim, same UTC calendar day as the
    send) and is proposed only when every pooled match falls in a
    single complete-linkage location group: one batch, one spot.
    Batches of a single photo are left to the regular runs, and so
    are photos without a usable timestamp.

    Returns (assignments, find, coords, stats) with one assignment
    per member photo of an assigned batch.
    """
    batches = {}
    stats = {"unlocated_wa": 0, "no_timestamp": 0, "solo": 0,
             "batches": 0, "outside_window": 0, "disagree": 0,
             "assigned_batches": 0, "assigned": 0}
    for aid, g in groups.items():
        if not WA_NAME_RE.search(g["file_name"]):
            continue
        stats["unlocated_wa"] += 1
        if g["taken_at"] is None:
            stats["no_timestamp"] += 1
            continue
        batches.setdefault(g["taken_at"], []).append(aid)
    candidates = {}
    for ts, members in batches.items():
        if len(members) < 2:
            stats["solo"] += len(members)
            continue
        stats["batches"] += 1
        day = _utc_date(ts)
        pooled = {}
        for aid in members:
            for m in groups[aid]["matches"]:
                if m["similarity"] < min_sim:
                    continue
                if _utc_date(m["match_taken_at"]) != day:
                    continue
                old = pooled.get(m["match_id"])
                if old is None or m["similarity"] > old["similarity"]:
                    pooled[m["match_id"]] = m
        if not pooled:
            stats["outside_window"] += len(members)
            continue
        candidates[ts] = list(pooled.values())
    find, coords = group_locations(candidates, loc_group_m, complete=True)
    assignments = {}
    for ts, ms in candidates.items():
        keys = {find(m["match_id"]) for m in ms}
        if len(keys) != 1:
            stats["disagree"] += len(batches[ts])
            continue
        key = keys.pop()
        ms = sorted(ms, key=lambda m: -m["similarity"])
        best = ms[0]
        stats["assigned_batches"] += 1
        for aid in batches[ts]:
            assignments[aid] = {"match": best, "n_agree": len(ms),
                                "group": key, "agreeing": ms}
            stats["assigned"] += 1
    return assignments, find, coords, stats


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------

def build_clusters(groups, assignments, find, coords, prefix="S"):
    """One cluster per location group with at least one assigned photo.

    Members: the assigned (missing) photos plus the donor photos that
    backed the assignments (role located). The cluster geotag comes
    from its strongest donor; spread_m is how far the other donors in
    the group sit from it.
    """
    by_group = {}
    for aid, a in assignments.items():
        g = by_group.setdefault(a["group"], {"members": [], "donors": {}})
        src = groups[aid]
        g["members"].append({
            "asset_id": aid, "role": "missing",
            "file_name": src["file_name"],
            "taken_at": cluster_fill._fmt_epoch(src["taken_at"]),
            "epoch": src["taken_at"],
            "match": a["match"], "n_agree": a["n_agree"],
        })
        for m in a["agreeing"]:
            g["donors"][m["match_id"]] = m

    clusters = []
    for key, g in by_group.items():
        donors = sorted(g["donors"].values(),
                        key=lambda m: (-m["similarity"], m["match_id"]))
        donor = donors[0]
        spread = max(
            [cluster_fill.haversine_m(donor["latitude"], donor["longitude"],
                                      d["latitude"], d["longitude"])
             for d in donors[1:]] or [0.0])
        members = sorted(g["members"],
                         key=lambda m: (m["epoch"] is None,
                                        m["epoch"] or 0.0))
        for m in members:
            m["lat"] = m["lon"] = None
        for d in donors:
            members.append({
                "asset_id": d["match_id"], "role": "located",
                "file_name": d["match_name"],
                "taken_at": cluster_fill._fmt_epoch(d["match_taken_at"]),
                "epoch": d["match_taken_at"],
                "lat": d["latitude"], "lon": d["longitude"],
            })
        clusters.append({
            "members": members, "donor_id": donor["match_id"],
            "donor_name": donor["match_name"],
            "geotag_lat": donor["latitude"],
            "geotag_lon": donor["longitude"],
            "spread_m": spread,
        })
    clusters.sort(key=lambda c: (-sum(1 for m in c["members"]
                                      if m["role"] == "missing"),
                                c["donor_id"]))
    for i, c in enumerate(clusters, 1):
        c["cluster_id"] = "%s%03d" % (prefix, i)
    return clusters


def write_clusters(clusters, path=CLUSTERS):
    """Persist clusters to clusters.csv (one row per member photo)."""
    rows = []
    for c in clusters:
        for m in c["members"]:
            rows.append({
                "cluster_id": c["cluster_id"], "asset_id": m["asset_id"],
                "role": m["role"], "file_name": m["file_name"],
                "taken_at": m["taken_at"],
                "latitude": "{:.6f}".format(m["lat"])
                if m.get("lat") is not None else "",
                "longitude": "{:.6f}".format(m["lon"])
                if m.get("lon") is not None else "",
                "donor_id": c["donor_id"],
                "geotag_lat": "{:.6f}".format(c["geotag_lat"]),
                "geotag_lon": "{:.6f}".format(c["geotag_lon"]),
                "spread_m": "{:.0f}".format(c["spread_m"]),
            })
    os.makedirs(os.path.dirname(path), exist_ok=True)
    geotag.write_csv(path, rows, time_fill.CLUSTER_COLUMNS)


# ---------------------------------------------------------------------------
# Ledger sync
# ---------------------------------------------------------------------------

def sync_ledger(clusters):
    """Add review rows to the time-fill ledger for photos not in it.

    The ledger is the single record of missing-GPS photos and their
    status; cluster apply marks rows applied there. Rows added here
    carry the similarity match as neighbor and the cluster geotag as
    the proposed coordinates. Existing rows are left untouched.
    Returns the number of rows added.
    """
    ledger = geotag.read_csv(time_fill.LEDGER)
    known = {r["asset_id"] for r in ledger}
    geotag.load_env()
    base_url = os.environ.get("IMMICH_URL", "").rstrip("/")
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    added = 0
    for c in clusters:
        for m in c["members"]:
            if m["role"] != "missing" or m["asset_id"] in known:
                continue
            match = m["match"]
            row = {col: "" for col in time_fill.LEDGER_COLUMNS}
            row.update({
                "asset_id": m["asset_id"], "file_name": m["file_name"],
                "taken_at": m["taken_at"],
                "latitude": "{:.6f}".format(c["geotag_lat"]),
                "longitude": "{:.6f}".format(c["geotag_lon"]),
                "neighbor_id": match["match_id"],
                "neighbor_name": match["match_name"],
                "n_neighbors": str(m["n_agree"]),
                "spread_m": "{:.0f}".format(c["spread_m"]),
                "status": "review",
                "maps_url": time_fill.maps_url(c["geotag_lat"],
                                               c["geotag_lon"]),
                "immich_url": f"{base_url}/photos/{m['asset_id']}",
                "run_date": run_date,
            })
            ledger.append(row)
            known.add(m["asset_id"])
            added += 1
    if added:
        geotag.write_csv(time_fill.LEDGER, ledger, time_fill.LEDGER_COLUMNS)
    return added


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

def split_clusters(cluster_ids, sub_m=300.0, clusters_path=CLUSTERS,
                   matches_path=MATCHES, min_sim=0.90):
    """Split chained clusters into per-hotspot sub-clusters.

    Single-linkage location grouping can chain nearby spots into one
    cluster (each donor within 1 km of the next, the ends much farther
    apart). This re-splits the given clusters: donors are regrouped at
    sub_m, every missing photo follows its strongest match into that
    match's sub-group, and each sub-group becomes its own cluster with
    its own geotag and a fresh ID (numbering continues after the
    highest existing one). Returns the new clusters.
    """
    groups = cluster_fill.parse_matches(matches_path)
    rows = geotag.read_csv(clusters_path)
    keep = [r for r in rows if r["cluster_id"] not in cluster_ids]
    n = max((int(r["cluster_id"][1:]) for r in rows
             if r["cluster_id"].startswith("S") and r["cluster_id"][1:].isdigit()),
            default=0)

    new_clusters = []
    for cid in cluster_ids:
        crows = [r for r in rows if r["cluster_id"] == cid]
        donors = {r["asset_id"]: (float(r["latitude"]),
                                  float(r["longitude"]),
                                  r["file_name"], r["taken_at"])
                  for r in crows if r["role"] == "located"}
        missing = [r for r in crows if r["role"] == "missing"]
        if not donors or not missing:
            continue
        # regroup the donors at sub_m
        find, union = _union_find(sorted(donors))
        ids = sorted(donors)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if cluster_fill.haversine_m(donors[a][0], donors[a][1],
                                           donors[b][0], donors[b][1]) \
                        <= sub_m:
                    union(a, b)
        # strongest similarity seen for each donor in this cluster
        strength = {d: 0.0 for d in donors}
        best = {}
        for r in missing:
            ms = [m for m in groups[r["asset_id"]]["matches"]
                  if m["similarity"] >= min_sim
                  and m["match_id"] in donors]
            if not ms:
                continue
            ms.sort(key=lambda m: -m["similarity"])
            best[r["asset_id"]] = ms[0]
            for m in ms:
                strength[m["match_id"]] = max(
                    strength[m["match_id"]], m["similarity"])
        by_group = {}
        for aid, m in best.items():
            by_group.setdefault(find(m["match_id"]), []).append(aid)
        for key, aids in by_group.items():
            sub_donors = sorted(
                (d for d in donors if find(d) == key),
                key=lambda d: (-strength[d], d))
            donor = sub_donors[0]
            dlat, dlon = donors[donor][0], donors[donor][1]
            spread = max(
                [cluster_fill.haversine_m(dlat, dlon, donors[d][0],
                                          donors[d][1])
                 for d in sub_donors[1:]] or [0.0])
            members = []
            for aid in aids:
                src = groups[aid]
                members.append({
                    "asset_id": aid, "role": "missing",
                    "file_name": next(r["file_name"] for r in missing
                                      if r["asset_id"] == aid),
                    "taken_at": cluster_fill._fmt_epoch(src["taken_at"]),
                    "epoch": src["taken_at"],
                    "match": best[aid],
                    "n_agree": sum(
                        1 for m in groups[aid]["matches"]
                        if m["similarity"] >= min_sim
                        and m["match_id"] in donors
                        and find(m["match_id"]) == key),
                    "lat": None, "lon": None,
                })
            members.sort(key=lambda m: (m["epoch"] is None,
                                        m["epoch"] or 0.0))
            for d in sub_donors:
                members.append({
                    "asset_id": d, "role": "located",
                    "file_name": donors[d][2],
                    "taken_at": donors[d][3],
                    "epoch": None,
                    "lat": donors[d][0], "lon": donors[d][1],
                })
            n += 1
            new_clusters.append({
                "cluster_id": "S%03d" % n, "members": members,
                "donor_id": donor, "donor_name": donors[donor][2],
                "geotag_lat": dlat, "geotag_lon": dlon,
                "spread_m": spread,
            })
    if not new_clusters:
        return []
    rows_out = keep
    for c in new_clusters:
        for m in c["members"]:
            rows_out.append({
                "cluster_id": c["cluster_id"], "asset_id": m["asset_id"],
                "role": m["role"], "file_name": m["file_name"],
                "taken_at": m["taken_at"],
                "latitude": "{:.6f}".format(m["lat"])
                if m.get("lat") is not None else "",
                "longitude": "{:.6f}".format(m["lon"])
                if m.get("lon") is not None else "",
                "donor_id": c["donor_id"],
                "geotag_lat": "{:.6f}".format(c["geotag_lat"]),
                "geotag_lon": "{:.6f}".format(c["geotag_lon"]),
                "spread_m": "{:.0f}".format(c["spread_m"]),
            })
    geotag.write_csv(clusters_path, rows_out, time_fill.CLUSTER_COLUMNS)
    return new_clusters


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run(matches_path=MATCHES, min_sim=0.90, solo_sim=0.96,
        loc_group_m=1000.0, max_dt_s=None, complete=False, same_day=False,
        clusters_path=CLUSTERS, prefix="S"):
    """Build the similarity clusters and sync the ledger.

    Photos already applied (ledger status "applied") are left out:
    they are done and must not be proposed again.
    """
    groups = cluster_fill.parse_matches(matches_path)
    assignments, find, coords, stats = assign(groups, min_sim, solo_sim,
                                              loc_group_m, max_dt_s,
                                              complete, same_day)
    ledger = geotag.read_csv(time_fill.LEDGER)
    applied = {r["asset_id"] for r in ledger
               if r.get("status") == "applied"}
    stats["already_applied"] = sum(1 for a in assignments if a in applied)
    assignments = {a: v for a, v in assignments.items()
                   if a not in applied}
    clusters = build_clusters(groups, assignments, find, coords,
                              prefix=prefix)
    write_clusters(clusters, clusters_path)
    n_missing = sum(1 for c in clusters for m in c["members"]
                    if m["role"] == "missing")
    print(f"unlocated photos in dump: {stats['unlocated']}")
    print(f"  no match with similarity >= {min_sim}: "
          f"{stats['no_candidates']}")
    if max_dt_s is not None:
        print(f"  matches outside the {max_dt_s / 60:.0f} min window: "
              f"{stats['outside_window']}")
    if same_day:
        print(f"  matches on a different day: {stats['outside_window']}")
    print(f"  matches disagree on location: {stats['disagree']}")
    print(f"  already applied elsewhere: {stats['already_applied']}")
    print(f"  assigned to a location: {stats['assigned']}")
    print(f"clusters: {len(clusters)} covering {n_missing} photos "
          f"to tag -> {os.path.relpath(clusters_path, SCRIPT_DIR)}")
    added = sync_ledger(clusters)
    print(f"ledger rows added: {added} "
          f"({os.path.relpath(time_fill.LEDGER, SCRIPT_DIR)})")
    return clusters, stats


def run_time():
    """Second attempt: lower threshold plus the 10-minute rule.

    Matches only need similarity >= 0.85 (down from 0.90) but the
    photo and its match must be within 10 minutes of each other, and
    donors are grouped with complete linkage at 300 m, so a cluster
    can never chain across different spots. Cluster IDs use the T
    prefix so they cannot be confused with the first attempt's S IDs.
    """
    return run(min_sim=0.85, solo_sim=0.96, loc_group_m=300.0,
               max_dt_s=600.0, complete=True,
               clusters_path=TIME_CLUSTERS, prefix="T")


def run_loose():
    """Third attempt: the 10-minute rule at an even lower score.

    Same rules as run_time (10-minute window, complete linkage at
    300 m) but matches only need similarity >= 0.80, using the looser
    match dump (cluster_matches_loose.sql). Cluster IDs use the U
    prefix.
    """
    return run(matches_path=MATCHES_LOOSE, min_sim=0.80, solo_sim=0.96,
               loc_group_m=300.0, max_dt_s=600.0, complete=True,
               clusters_path=LOOSE_CLUSTERS, prefix="U")


def run_vloose():
    """Fourth attempt: the 10-minute rule at similarity >= 0.70.

    Same rules as run_time (10-minute window, complete linkage at
    300 m) but matches only need similarity >= 0.70, from the very
    loose dump (cluster_matches_vloose.sql). The visual signal is
    weak at this score - the 10-minute window and the location
    agreement do the real work. Cluster IDs use the V prefix.
    """
    return run(matches_path=MATCHES_VLOOSE, min_sim=0.70, solo_sim=0.96,
               loc_group_m=300.0, max_dt_s=600.0, complete=True,
               clusters_path=VLOOSE_CLUSTERS, prefix="V")


def run_sameday():
    """Fifth attempt: similarity >= 0.70 within the same calendar day.

    Same rules as run_vloose (similarity >= 0.70, complete linkage at
    300 m) but the 10-minute window widens to "both photos fall on
    the same UTC calendar day". Cluster IDs use the W prefix.
    """
    return run(matches_path=MATCHES_VLOOSE, min_sim=0.70, solo_sim=0.96,
               loc_group_m=300.0, same_day=True, complete=True,
               clusters_path=SAMEDAY_CLUSTERS, prefix="W")


def run_wa(matches_path=MATCHES_VLOOSE, min_sim=0.70, loc_group_m=300.0,
           clusters_path=WA_CLUSTERS, prefix="X"):
    """Sixth attempt: whole WhatsApp send batches, same-day rule.

    WhatsApp bulk sends share one file timestamp, so every photo in
    a batch comes from the same moment. Each batch pools its
    members' located matches (similarity >= 0.70, same UTC day as
    the send) and is proposed only when all pooled matches agree
    within one 300 m complete-linkage group. Photos already applied
    are left out. Cluster IDs use the X prefix.
    """
    groups = cluster_fill.parse_matches(matches_path)
    assignments, find, coords, stats = assign_wa(groups, min_sim,
                                                  loc_group_m)
    ledger = geotag.read_csv(time_fill.LEDGER)
    applied = {r["asset_id"] for r in ledger
               if r.get("status") == "applied"}
    stats["already_applied"] = sum(1 for a in assignments if a in applied)
    assignments = {a: v for a, v in assignments.items()
                   if a not in applied}
    clusters = build_clusters(groups, assignments, find, coords,
                              prefix=prefix)
    write_clusters(clusters, clusters_path)
    n_missing = sum(1 for c in clusters for m in c["members"]
                    if m["role"] == "missing")
    print(f"unlocated WhatsApp photos in dump: {stats['unlocated_wa']}")
    print(f"  without a usable timestamp: {stats['no_timestamp']}")
    print(f"  sent alone (no batch): {stats['solo']}")
    print(f"  multi-photo batches: {stats['batches']}")
    print(f"  batch photos with no same-day match: "
          f"{stats['outside_window']}")
    print(f"  batch photos whose matches disagree on location: "
          f"{stats['disagree']}")
    print(f"  already applied elsewhere: {stats['already_applied']}")
    print(f"  assigned to a location: {stats['assigned']}")
    print(f"clusters: {len(clusters)} covering {n_missing} photos "
          f"to tag -> {os.path.relpath(clusters_path, SCRIPT_DIR)}")
    added = sync_ledger(clusters)
    print(f"ledger rows added: {added} "
          f"({os.path.relpath(time_fill.LEDGER, SCRIPT_DIR)})")
    return clusters, stats


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--matches", default=MATCHES,
                    help="CLIP match dump (positional CSV from "
                         "cluster_matches.sql)")
    ap.add_argument("--min-sim", type=float, default=0.90,
                    help="similarity an agreeing match must reach (0.90)")
    ap.add_argument("--solo-sim", type=float, default=0.96,
                    help="similarity a single match must reach (0.96)")
    ap.add_argument("--loc-group-m", type=float, default=1000.0,
                    help="distance that donors must be within to share "
                         "a location group, in meters (1000)")
    ap.add_argument("--max-dt-min", type=float, default=None,
                    help="only keep matches whose timestamps are at "
                         "most this many minutes apart (default: no "
                         "time constraint)")
    ap.add_argument("--complete-linkage", action="store_true",
                    help="a donor joins a location group only when "
                         "within the threshold of every member (no "
                         "chaining)")
    ap.add_argument("--clusters-out", default=CLUSTERS,
                    help="clusters.csv to write (default: the "
                         "similar-fill one)")
    ap.add_argument("--split", default=None,
                    help="comma-separated cluster IDs to re-split into "
                         "per-hotspot sub-clusters (e.g. S001,S002)")
    ap.add_argument("--sub-m", type=float, default=300.0,
                    help="donor distance for the sub-grouping used by "
                         "--split, in meters (300)")
    args = ap.parse_args()
    if args.split:
        ids = [s.strip() for s in args.split.split(",") if s.strip()]
        new = split_clusters(ids, sub_m=args.sub_m)
        for c in new:
            n_miss = sum(1 for m in c["members"] if m["role"] == "missing")
            print(f"{c['cluster_id']}: {n_miss} to tag, geotag "
                  f"{c['geotag_lat']:.5f},{c['geotag_lon']:.5f}, "
                  f"spread {c['spread_m']:.0f} m")
        return
    run(args.matches, args.min_sim, args.solo_sim, args.loc_group_m,
        None if args.max_dt_min is None else args.max_dt_min * 60.0,
        args.complete_linkage, args.clusters_out)


if __name__ == "__main__":
    main()

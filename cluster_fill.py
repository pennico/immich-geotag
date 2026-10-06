#!/usr/bin/env python3
"""cluster_fill: fill missing GPS from visually similar located photos.

Third location source for immich-geotag, after Cloud Vision/Lens and the
Gemini pass. Immich's machine-learning container computes a CLIP
embedding for every image (smart_search table); photos of the same place
land close together in that space. pgvector does the similarity search
(the SQL lives in cluster_matches.sql, run by the host job against the
Immich postgres); this script consumes the match CSV, applies the
confirmation rules, writes coordinates to Immich for confident matches,
and builds a review page for the rest.

Confirmation rules (conservative, add-only):
  confident  >= 2 matches with similarity >= --min-sim (0.90) that agree
              within --agree-m (100 m), or a single match with
              similarity >= --solo-sim (0.96).
  review     everything else, provided the photo and its best match were
              taken within --max-dt-min (10) minutes of each other (same
              shooting session); when either EXIF date is missing the
              proposal stays review. Proposed but NOT applied; the owner
              approves on the review page (mark status=candidate in the
              ledger and re-run to apply).
  rejected   a proposal whose best match was taken further than the time
              window away - visual similarity alone, different session.

Every applied edit is appended to geotag-data/cluster/cluster_edits.csv
(timestamp, asset, source match, coordinates) so any edit can be traced
and adjusted later. Existing locations are never touched: the match dump
only contains assets with no latitude/longitude.

Usage:
  python3 cluster_fill.py geotag-data/cluster/matches.csv --dry-run
  python3 cluster_fill.py geotag-data/cluster/matches.csv
  python3 cluster_fill.py geotag-data/cluster/matches.csv --redo
"""
import argparse
import csv
import html
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import geotag  # noqa: E402

DATA = os.path.join(SCRIPT_DIR, "geotag-data", "cluster")
REVIEW = os.path.join(SCRIPT_DIR, "geotag-data", "cluster-review")

LEDGER = os.path.join(DATA, "proposals.csv")
EDITS = os.path.join(DATA, "cluster_edits.csv")

LEDGER_COLUMNS = [
    "asset_id", "file_name", "latitude", "longitude", "similarity",
    "match_id", "match_name", "n_matches", "spread_m", "status",
    "maps_url", "immich_url", "run_date", "taken_at", "match_taken_at",
]

EDIT_COLUMNS = [
    "run_date", "asset_id", "file_name", "match_id", "match_name",
    "similarity", "latitude", "longitude",
]

# Match CSV columns, positional (COPY ... TO STDOUT, no header):
# asset_id, file_name, taken_at, match_id, match_name, match_taken_at,
# similarity, latitude, longitude (taken_at = epoch seconds, NULL when
# the EXIF date is missing)
MATCH_COLUMNS = ["asset_id", "file_name", "taken_at", "match_id",
                 "match_name", "match_taken_at", "similarity",
                 "latitude", "longitude"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two coordinates."""
    from math import asin, cos, radians, sin, sqrt
    r = 6371000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (sin(dlat / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
    return 2 * r * asin(sqrt(a))


def parse_matches(path):
    """Read the pgvector match dump -> {asset_id: {name, matches: [...]}}.

    Matches arrive sorted by similarity (descending) per asset; the parser
    does not rely on the order. Timestamps (epoch seconds) are optional:
    an empty/invalid value becomes None and the row is still kept.
    """
    groups = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) != len(MATCH_COLUMNS):
                continue
            rec = dict(zip(MATCH_COLUMNS, row))
            try:
                rec["similarity"] = float(rec["similarity"])
                rec["latitude"] = float(rec["latitude"])
                rec["longitude"] = float(rec["longitude"])
            except ValueError:
                continue
            rec["taken_at"] = _opt_float(rec["taken_at"])
            rec["match_taken_at"] = _opt_float(rec["match_taken_at"])
            g = groups.setdefault(rec["asset_id"],
                                   {"file_name": rec["file_name"],
                                    "taken_at": rec["taken_at"],
                                    "matches": []})
            g["matches"].append(rec)
    return groups


def _opt_float(value):
    """Parse a float that may be empty/NULL -> None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_epoch(value):
    """Epoch seconds -> 'YYYY-MM-DD HH:MM' (UTC) or ''."""
    if value is None:
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


def confirm(matches, min_sim=0.90, agree_m=100.0, solo_sim=0.96,
            taken_at=None, max_dt=600.0):
    """Apply the confirmation rules to one asset's matches.

    Returns (status, best, n_agree, spread_m):
      status    "confident", "review" or "rejected"
      best      the highest-similarity match (coordinates source)
      n_agree   how many >= min_sim matches agree with best within agree_m
      spread_m  max distance of the agreeing matches from best

    Time-coherence rule for proposals that fail the spatial rules: the
    photo and its best match must have been taken within max_dt seconds
    of each other (same shooting session). Proposals outside the window
    are "rejected"; when either EXIF date is missing the proposal stays
    "review" (the owner judges it on the review page).
    """
    best = max(matches, key=lambda m: m["similarity"])
    agreeing = [m for m in matches if m["similarity"] >= min_sim]
    close = [m for m in agreeing
             if haversine_m(best["latitude"], best["longitude"],
                            m["latitude"], m["longitude"]) <= agree_m]
    spread = max((haversine_m(best["latitude"], best["longitude"],
                             m["latitude"], m["longitude"])
                  for m in close), default=0.0)
    if len(close) >= 2 or best["similarity"] >= solo_sim:
        return "confident", best, len(close), round(spread)
    if (taken_at is not None and best.get("match_taken_at") is not None
            and abs(taken_at - best["match_taken_at"]) > max_dt):
        return "rejected", best, len(close), round(spread)
    return "review", best, len(close), round(spread)


def load_ledger():
    if not os.path.exists(LEDGER):
        return {}
    with open(LEDGER, newline="", encoding="utf-8") as f:
        return {r["asset_id"]: r for r in csv.DictReader(f)}


def append_edits(rows):
    """Append applied edits to the permanent edit log (one row per asset)."""
    exists = os.path.exists(EDITS)
    with open(EDITS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=EDIT_COLUMNS)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def write_ledger(rows):
    os.makedirs(DATA, exist_ok=True)
    with open(LEDGER, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def maps_url(lat, lon):
    return f"https://www.google.com/maps?q={lat},{lon}"


# ---------------------------------------------------------------------------
# Review page
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>immich-geotag — cluster fill results</title>
<style>
 body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f5f5f7; }}
 header {{ padding: 24px; background: #1c1c1e; color: #fff; }}
 h1 {{ margin: 0 0 8px; font-size: 22px; }}
 header p {{ margin: 0 0 6px; color: #aeaeb2; font-size: 14px; }}
 .section {{ padding: 24px 16px 0; font-size: 18px; }}
 .note {{ padding: 0 16px; color: #8e8e93; font-size: 13px; margin: 4px 0 0; }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
          gap: 16px; padding: 16px; }}
 .card {{ background: #fff; border-radius: 12px; overflow: hidden;
          box-shadow: 0 1px 4px rgba(0,0,0,.15); }}
 .pair {{ display: grid; grid-template-columns: 1fr 1fr; }}
 .pair img {{ width: 100%; display: block; aspect-ratio: 4/3; object-fit: cover; }}
 .pair .lbl {{ position: absolute; }}
 .meta {{ padding: 12px 16px; }}
 h2 {{ font-size: 16px; margin: 0 0 4px; }}
 .file {{ color: #8e8e93; font-size: 12px; margin: 0 0 8px;
          font-family: ui-monospace, monospace; }}
 .geo {{ color: #555; font-size: 12px; }}
 .meta p {{ margin: 4px 0; font-size: 13px; }}
 a {{ color: #0a84ff; }}
 .badge {{ color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 8px; }}
</style></head>
<body>
<header>
 <h1>Cluster fill — results</h1>
 <p>{} photos located from visually similar located photos and written to
 Immich. {} proposed but held for review (not written). {} photos without a
 location were searched.</p>
 <p>Green = applied (coordinates from the best similar located photo).
 Orange = proposal only - review and approve the good ones in
 proposals.csv (status -> candidate), then re-run to apply.</p>
</header>
{}
</body></html>"""


def card(row, color, label, img, match_img):
    return """
<div class="card">
  <div class="pair">
    <img src="{}" loading="lazy" alt="{}">
    <img src="{}" loading="lazy" alt="match">
  </div>
  <div class="meta">
    <h2>similarity {:.2f} &middot; {} agreeing</h2>
    <p class="file">{} &middot; matched {}</p>
    <p>{:.5f},{:.5f}</p><p class="geo">spread {} m</p>
    <p><span class="badge" style="background:{}">{}</span></p>
    <p><a href="{}">Google Maps</a> &middot; <a href="{}">Open in Immich</a></p>
  </div>
</div>""".format(html.escape(img), html.escape(row["file_name"]),
                 html.escape(match_img),
                 float(row["similarity"]), row["n_matches"],
                 html.escape(row["file_name"]), html.escape(row["match_name"]),
                 float(row["latitude"]), float(row["longitude"]),
                 row["spread_m"], color, label,
                 html.escape(row["maps_url"]), html.escape(row["immich_url"]))


def build_review(rows, searched, max_images=300):
    """Review page: applied (green) and review (orange) cards, newest first."""
    os.makedirs(os.path.join(REVIEW, "images"), exist_ok=True)
    geotag.load_env()
    immich = geotag.Immich(os.environ["IMMICH_URL"],
                           os.environ["IMMICH_API_KEY"])

    applied = [r for r in rows if r["status"] == "applied"]
    review = [r for r in rows if r["status"] == "review"]

    def cards(subset, color, label):
        out = []
        for r in subset[:max_images]:
            img = "images/{}_{}.jpg".format(r["asset_id"][:8],
                                           os.path.splitext(r["file_name"])[0][:40])
            mimg = "images/m_{}_{}.jpg".format(
                r["match_id"][:8], os.path.splitext(r["match_name"])[0][:40])
            for path, aid in ((img, r["asset_id"]), (mimg, r["match_id"])):
                full = os.path.join(REVIEW, path)
                if not os.path.exists(full):
                    try:
                        with open(full, "wb") as fh:
                            fh.write(immich.preview_jpeg(aid))
                    except Exception:
                        pass
            out.append(card(r, color, label, img, mimg))
        return out

    sections = ""
    if applied:
        sections += ("<h2 class='section'>Applied to Immich</h2>"
                     + "".join(cards(applied, "#30d158", "applied"))
                     .join(["<div class='grid'>", "</div>"]))
    if review:
        sections += ("<h2 class='section'>Proposed - review needed</h2>"
                     + "".join(cards(review, "#ff9f0a", "proposal, not applied"))
                     .join(["<div class='grid'>", "</div>"]))
    with open(os.path.join(REVIEW, "index.html"), "w", encoding="utf-8") as f:
        f.write(PAGE.format(len(applied), len(review), searched, sections))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(matches_path, dry_run=False, redo=False, min_sim=0.90,
        agree_m=100.0, solo_sim=0.96, max_dt=600.0):
    groups = parse_matches(matches_path)
    ledger = {} if redo else load_ledger()
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows, edits, applied_n, review_n, rejected_n, skipped_n = \
        [], [], 0, 0, 0, 0
    for asset_id, g in groups.items():
        if asset_id in ledger:
            rows.append(ledger[asset_id])
            skipped_n += 1
            continue
        status, best, n_agree, spread = confirm(g["matches"], min_sim,
                                                agree_m, solo_sim,
                                                g.get("taken_at"), max_dt)
        row = {
            "asset_id": asset_id,
            "file_name": g["file_name"],
            "latitude": str(best["latitude"]),
            "longitude": str(best["longitude"]),
            "similarity": "{:.4f}".format(best["similarity"]),
            "match_id": best["match_id"],
            "match_name": best["match_name"],
            "n_matches": len(g["matches"]),
            "spread_m": spread,
            "status": "review",
            "maps_url": maps_url(best["latitude"], best["longitude"]),
            "immich_url": "{}/photos/{}".format(
                os.environ.get("IMMICH_PUBLIC_URL",
                               os.environ.get("IMMICH_URL", "")).rstrip("/"),
                asset_id),
            "run_date": run_date,
            "taken_at": _fmt_epoch(g.get("taken_at")),
            "match_taken_at": _fmt_epoch(best.get("match_taken_at")),
        }
        if status == "confident":
            if not dry_run:
                immich = geotag.Immich(os.environ["IMMICH_URL"],
                                       os.environ["IMMICH_API_KEY"])
                immich.set_location(asset_id, best["latitude"],
                                    best["longitude"])
                row["status"] = "applied"
                edits.append({
                    "run_date": run_date, "asset_id": asset_id,
                    "file_name": g["file_name"], "match_id": best["match_id"],
                    "match_name": best["match_name"],
                    "similarity": row["similarity"],
                    "latitude": row["latitude"], "longitude": row["longitude"],
                })
            applied_n += 1
        elif status == "rejected":
            row["status"] = "rejected"
            rejected_n += 1
        else:
            review_n += 1
        rows.append(row)

    print("-" * 78)
    print(f"searched {len(groups)} unlocated photos")
    print(f"confident: {applied_n}  review: {review_n}  "
          f"rejected (time window): {rejected_n}  "
          f"already processed: {skipped_n}")
    if dry_run:
        print("dry run: nothing written, no review page built")
        return

    write_ledger(rows)
    if edits:
        append_edits(edits)
    build_review(rows, len(groups))
    print(f"applied {applied_n} locations to Immich "
          f"(edit log: {os.path.relpath(EDITS, SCRIPT_DIR)})")
    print(f"review page: {os.path.relpath(REVIEW, SCRIPT_DIR)}/index.html")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("matches", help="match CSV from cluster_matches.sql")
    p.add_argument("--dry-run", action="store_true",
                   help="report counts only; write nothing")
    p.add_argument("--redo", action="store_true",
                   help="re-process assets already in the ledger")
    p.add_argument("--min-sim", type=float, default=0.90,
                   help="similarity an agreeing match must reach (0.90)")
    p.add_argument("--agree-m", type=float, default=100.0,
                   help="max distance between agreeing matches (100 m)")
    p.add_argument("--solo-sim", type=float, default=0.96,
                   help="a single match at this similarity is confident (0.96)")
    p.add_argument("--max-dt-min", type=float, default=10.0,
                   help="review proposals must have been taken within this "
                        "many minutes of their best match (10)")
    args = p.parse_args()
    geotag.load_env()
    for key in ("IMMICH_URL", "IMMICH_API_KEY"):
        if not os.environ.get(key):
            sys.exit(f"Missing configuration: {key} (set it in the environment "
                     f"or in {SCRIPT_DIR}/.env - see .env.example)")
    run(args.matches, dry_run=args.dry_run, redo=args.redo,
        min_sim=args.min_sim, agree_m=args.agree_m, solo_sim=args.solo_sim,
        max_dt=args.max_dt_min * 60.0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Group review app for time-fill proposals.

Reads the proposal ledger, clusters the pending non-WhatsApp photos by
time (photos taken within 10 minutes of each other chain into one
group), and builds a self-contained index.html showing each cluster as
a card: the photos still missing GPS, the located photos in the same
time span, the proposed geotag (from the located photo closest in time
to the group's median), and how far apart the located photos are.

Cluster IDs (C001...) are assigned once, stored in
geotag-data/time-fill/clusters.csv, and reused on every rebuild unless
--recluster is passed, so an ID always refers to the same group.

With --serve the app also runs a local HTTP server (127.0.0.1): the
Apply button in the page POSTs the selected cluster IDs to it and the
server writes one geotag per selected cluster to Immich via
time_fill.apply_clusters (ledger + edit log updated, idempotent).
Without the server the Apply button falls back to copying the selected
cluster IDs.

The server only binds 127.0.0.1 and guards POST /apply against
cross-site requests from other pages open in the same browser: the
request must carry the per-run token the server injects into the page it
serves (X-Apply-Token header), a JSON Content-Type, a loopback Host
header (DNS-rebinding guard), and - when the browser sends one - an
Origin of this server.

Usage:
  python3 time_fill_review_app.py [--recluster] [--limit N] [--skip-images]
  python3 time_fill_review_app.py --serve [--port 8788]
  python3 time_fill_review_app.py --source similar --serve [--port 8789]
  python3 time_fill_review_app.py --source similar-time --serve [--port 8790]
  python3 time_fill_review_app.py --source similar-loose --serve [--port 8791]
  python3 time_fill_review_app.py --source similar-vloose --serve [--port 8792]
  python3 time_fill_review_app.py --source similar-sameday --serve [--port 8793]
  python3 time_fill_review_app.py --source similar-wa --serve [--port 8794]
"""
import argparse
import concurrent.futures
import hmac
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import geotag  # noqa: E402
import time_fill  # noqa: E402

DEFAULT_OUT = os.path.join(SCRIPT_DIR, "geotag-data", "time-fill-review-app")
SIMILAR_OUT = os.path.join(SCRIPT_DIR, "geotag-data",
                           "similar-fill-review-app")
SIMILAR_TIME_OUT = os.path.join(SCRIPT_DIR, "geotag-data",
                               "similar-time-review-app")
SIMILAR_LOOSE_OUT = os.path.join(SCRIPT_DIR, "geotag-data",
                                 "similar-loose-review-app")
SIMILAR_VLOOSE_OUT = os.path.join(SCRIPT_DIR, "geotag-data",
                                  "similar-vloose-review-app")
SIMILAR_SAMEDAY_OUT = os.path.join(SCRIPT_DIR, "geotag-data",
                                   "similar-sameday-review-app")
SIMILAR_WA_OUT = os.path.join(SCRIPT_DIR, "geotag-data",
                              "similar-wa-review-app")
WA_RE = re.compile(r"-WA\d+|WhatsApp", re.I)

TIME_BLURB = """Each card is one time group: photos taken within 10 minutes of each
 other. Orange = still missing GPS (would be tagged), green = already
 located, blue border = the photo donating the proposed geotag. Spread
 is how far apart the located photos in the group are - a big spread
 means the timestamps are unreliable (scan or import batches); groups
 spread wider than 10 km are marked mixed and cannot be applied.
 Tick the good groups and press Apply: every orange photo in the
 selected groups gets the group's proposed geotag."""

SIMILAR_BLURB = """Each card is one location group: photos missing GPS that visually
 match (CLIP similarity) photos already tagged at that place. Orange =
 still missing GPS (would be tagged), green = the located donors that
 backed the match, blue border = the photo donating the proposed
 geotag. Spread is how far apart the donors are. Groups are built per
 location, so photos from different places are never mixed into one
 group. Tick the good groups and press Apply: every orange photo in
 the selected groups gets the group's proposed geotag."""

SIMILAR_TIME_BLURB = """Each card is one location group from the second similarity attempt:
 photos missing GPS that visually match (CLIP similarity >= 0.85, a
 lower bar than the first attempt) a photo already tagged within 10
 minutes of the same moment. Orange = still missing GPS (would be
 tagged), green = the located donors that backed the match, blue
 border = the photo donating the proposed geotag. Donors are grouped
 with complete linkage at 300 m, so a group can never chain across
 different spots. Note: WhatsApp file timestamps are send times, so
 the 10-minute check is weaker for those. Tick the good groups and
 press Apply: every orange photo in the selected groups gets the
 group's proposed geotag."""

SIMILAR_LOOSE_BLURB = """Each card is one location group from the third similarity attempt:
 same rules as the second attempt (a match must be within 10 minutes
 and donors are grouped with complete linkage at 300 m, so a group can
 never chain across different spots) but matches only need CLIP
 similarity >= 0.80 - a looser visual bar, so check these cards more
 carefully. Orange = still missing GPS (would be tagged), green = the
 located donors that backed the match, blue border = the photo
 donating the proposed geotag. WhatsApp file timestamps are send
 times, so the 10-minute check is weaker for those. Tick the good
 groups and press Apply: every orange photo in the selected groups
 gets the group's proposed geotag."""

SIMILAR_VLOOSE_BLURB = """Each card is one location group from the fourth similarity attempt:
 same rules as before (a match must be within 10 minutes and donors
 are grouped with complete linkage at 300 m, so a group can never
 chain across different spots) but matches only need CLIP
 similarity >= 0.70 - a very loose visual bar. The 10-minute window
 and the location agreement do the real work here; check each card
 carefully before applying. Orange = still missing GPS (would be
 tagged), green = the located donors that backed the match, blue
 border = the photo donating the proposed geotag. WhatsApp file
 timestamps are send times, so the 10-minute check is weaker for
 those. Tick the good groups and press Apply: every orange photo in
 the selected groups gets the group's proposed geotag."""

SIMILAR_SAMEDAY_BLURB = """Each card is one location group from the fifth
 similarity attempt: same rules as the very loose attempt (CLIP
 similarity >= 0.70, donors grouped with complete linkage at 300 m,
 so a group can never chain across different spots) but the window
 widens from 10 minutes to "both photos fall on the same calendar
 day". With a whole day as the window the visual and location checks
 matter most - check each card carefully before applying. Orange =
 still missing GPS (would be tagged), green = the located donors
 that backed the match, blue border = the photo donating the
 proposed geotag. WhatsApp file timestamps are send times, so the
 same-day check is weaker for those. Tick the good groups and press
 Apply: every orange photo in the selected groups gets the group's
 proposed geotag."""

SIMILAR_WA_BLURB = """Each card is one WhatsApp send batch from the sixth
 attempt. Photos sent in one WhatsApp bulk share an identical file
 timestamp, so they come from the same moment; each batch pools the
 located matches of all its members (CLIP similarity >= 0.70, same
 calendar day as the send) and is shown only when every pooled match
 agrees within one 300 m complete-linkage group - one batch, one
 spot. Orange = the WhatsApp photos that would be tagged, green =
 the located donors that backed the batch, blue border = the photo
 donating the proposed geotag. The batch timestamp is the WhatsApp
 send time, not the capture time, so check the cards carefully -
 especially the ones where only a single donor backs a whole batch.
 Tick the good batches and press Apply: every orange photo in the
 selected batches gets the batch's proposed geotag."""


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def build_clusters(assets, ledger, window_s=600.0):
    """Single-linkage time clustering of pending non-WhatsApp photos.

    assets: (id, file_name, epoch, lat, lon, taken_raw) tuples from
    time_fill.scan_library. ledger: proposal rows. Nodes are the pending
    non-WhatsApp proposals (role missing) plus every non-WhatsApp located
    photo (role located); consecutive photos more than window_s apart
    split the timeline. Clusters without a pending photo are dropped.

    The proposed geotag is the location of the located photo closest in
    time to the cluster's median missing-photo time; spread_m is the
    distance from it to the farthest located photo in the cluster.
    Returns clusters sorted by start time, IDs not yet assigned.
    """
    pending_ids = {r["asset_id"] for r in ledger
                   if r.get("status") == "review"
                   and not WA_RE.search(r["file_name"])
                   and not WA_RE.search(r["neighbor_name"])}
    nodes = []
    for aid, name, epoch, lat, lon, raw in assets:
        if epoch is None or WA_RE.search(name):
            continue
        if aid in pending_ids:
            nodes.append({"epoch": epoch, "asset_id": aid,
                          "file_name": name, "role": "missing",
                          "taken_at": raw})
        elif lat is not None and lon is not None:
            nodes.append({"epoch": epoch, "asset_id": aid,
                          "file_name": name, "role": "located",
                          "taken_at": raw, "lat": lat, "lon": lon})
    nodes.sort(key=lambda n: n["epoch"])
    groups, cur = [], []
    for n in nodes:
        if cur and n["epoch"] - cur[-1]["epoch"] > window_s:
            groups.append(cur)
            cur = []
        cur.append(n)
    if cur:
        groups.append(cur)

    clusters = []
    for g in groups:
        missing = [n for n in g if n["role"] == "missing"]
        located = [n for n in g if n["role"] == "located"]
        if not missing or not located:
            continue
        med = statistics.median(n["epoch"] for n in missing)
        donor = min(located, key=lambda n: abs(n["epoch"] - med))
        spread = max(time_fill.haversine_m(donor["lat"], donor["lon"],
                                           n["lat"], n["lon"])
                     for n in located)
        clusters.append({
            "members": g, "donor_id": donor["asset_id"],
            "donor_name": donor["file_name"],
            "geotag_lat": donor["lat"], "geotag_lon": donor["lon"],
            "spread_m": spread,
        })
    clusters.sort(key=lambda c: c["members"][0]["epoch"])
    for i, c in enumerate(clusters, 1):
        c["cluster_id"] = "C%03d" % i
    return clusters


def write_clusters(clusters, path):
    """Persist clusters to clusters.csv (one row per member photo)."""
    rows = []
    for c in clusters:
        for m in c["members"]:
            rows.append({
                "cluster_id": c["cluster_id"], "asset_id": m["asset_id"],
                "role": m["role"], "file_name": m["file_name"],
                "taken_at": m["taken_at"],
                "latitude": "{:.6f}".format(m["lat"]) if m.get("lat") is not None else "",
                "longitude": "{:.6f}".format(m["lon"]) if m.get("lon") is not None else "",
                "donor_id": c["donor_id"],
                "geotag_lat": "{:.6f}".format(c["geotag_lat"]),
                "geotag_lon": "{:.6f}".format(c["geotag_lon"]),
                "spread_m": "{:.0f}".format(c["spread_m"]),
            })
    geotag.write_csv(path, rows, time_fill.CLUSTER_COLUMNS)


def load_clusters(path=time_fill.CLUSTERS):
    """clusters.csv -> display-ready cluster list (IDs stable from file)."""
    groups = time_fill.read_clusters(path)
    clusters = []
    for cid in sorted(groups):
        g = groups[cid]
        for m in g["members"]:
            m["epoch"] = time_fill.parse_epoch(m["taken_at"])
        g["members"].sort(key=lambda m: m["epoch"] or 0)
        g["donor_name"] = next((m["file_name"] for m in g["members"]
                                if m["asset_id"] == g["donor_id"]), "")
        clusters.append(g)
    return clusters


# ---------------------------------------------------------------------------
# Page data
# ---------------------------------------------------------------------------

def _fmt_span(minutes):
    """Human-readable duration for the card header."""
    if minutes < 90:
        return f"{minutes:.0f} min"
    if minutes < 2880:
        return f"{minutes / 60.0:.1f} h"
    if minutes < 43200:
        return f"{minutes / 1440.0:.1f} d"
    return f"{minutes / 525600.0:.1f} y"


def compact_clusters(clusters, ledger):
    """Clusters -> compact JSON tuples for the page."""
    status_by_id = {r["asset_id"]: r.get("status") for r in ledger}
    out = []
    for c in clusters:
        missing = [m for m in c["members"] if m["role"] == "missing"]
        located = [m for m in c["members"] if m["role"] == "located"]
        applied = all(status_by_id.get(m["asset_id"]) == "applied"
                      for m in missing)
        epochs = [m["epoch"] for m in c["members"] if m["epoch"]]
        start = datetime.fromtimestamp(min(epochs), timezone.utc)
        span_min = (max(epochs) - min(epochs)) / 60.0
        members = [[m["asset_id"], m["file_name"], m["role"],
                    m["taken_at"], 1 if m["asset_id"] == c["donor_id"] else 0]
                   for m in c["members"]]
        out.append([
            c["cluster_id"], start.strftime("%Y-%m-%d %H:%M"),
            _fmt_span(span_min), len(missing), len(located),
            round(c["spread_m"]),
            round(c["geotag_lat"], 6), round(c["geotag_lon"], 6),
            c["donor_name"],
            time_fill.maps_url(c["geotag_lat"], c["geotag_lon"]),
            "applied" if applied else "pending",
            members,
        ])
    return out


def image_ids(clusters):
    ids = set()
    for c in clusters:
        for m in c["members"]:
            ids.add(m["asset_id"])
    return sorted(ids)


def download_images(immich, ids, images_dir, workers=8):
    """Download thumbnail-size JPEGs; returns (ok, failed) counts."""
    os.makedirs(images_dir, exist_ok=True)
    todo = [i for i in ids
            if not os.path.exists(os.path.join(images_dir, i + ".jpg"))]
    ok = len(ids) - len(todo)
    failed = []

    def fetch(asset_id):
        url = immich._url(f"/assets/{asset_id}/thumbnail?size=thumbnail")
        data = geotag.http_bytes("GET", url, headers=immich.headers)
        with open(os.path.join(images_dir, asset_id + ".jpg"), "wb") as f:
            f.write(data)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, i): i for i in todo}
        for fut in concurrent.futures.as_completed(futures):
            try:
                fut.result()
                ok += 1
            except Exception:
                failed.append(futures[fut])
    return ok, failed


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
 body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f5f5f7; }}
 header {{ padding: 20px 24px; background: #1c1c1e; color: #fff; }}
 h1 {{ margin: 0 0 6px; font-size: 20px; }}
 header p {{ margin: 0; color: #aeaeb2; font-size: 13px; }}
 .bar {{ position: sticky; top: 0; z-index: 10; display: flex; flex-wrap: wrap;
        gap: 10px; align-items: center; padding: 10px 16px;
        background: #fff; border-bottom: 1px solid #d9d9de; }}
 .bar label {{ font-size: 12px; color: #555; }}
 .bar input[type=text], .bar select {{ padding: 6px 8px; border: 1px solid #c7c7cc;
        border-radius: 8px; font-size: 13px; }}
 #count {{ font-size: 12px; color: #8e8e93; margin-left: auto; }}
 .grid {{ display: grid; grid-template-columns: 1fr; gap: 16px; padding: 16px;
         max-width: 1100px; margin: 0 auto; }}
 .cluster {{ background: #fff; border-radius: 12px; overflow: hidden;
            box-shadow: 0 1px 4px rgba(0,0,0,.15); }}
 .chead {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
          padding: 12px 16px 8px; }}
 .cid {{ font-size: 17px; font-weight: 700; font-family: ui-monospace, monospace; }}
 .cdate {{ font-size: 13px; color: #555; }}
 .counts {{ font-size: 12px; color: #8e8e93; }}
 .badge {{ color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 8px; }}
 .geo {{ padding: 0 16px 8px; font-size: 12px; color: #555; }}
 .geo a {{ color: #0a84ff; text-decoration: none; }}
 .strip {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 0 16px 14px; }}
 .ph {{ position: relative; width: 96px; }}
 .ph img {{ width: 96px; height: 72px; object-fit: cover; display: block;
           border-radius: 6px; border: 2px solid #c7c7cc; background: #e5e5ea; }}
 .ph.missing img {{ border-color: #ff9f0a; }}
 .ph.located img {{ border-color: #30d158; }}
 .ph.donor img {{ border-color: #0a84ff; }}
 .ph span {{ position: absolute; bottom: 2px; left: 2px;
            background: rgba(0,0,0,.6); color: #fff; font-size: 9px;
            padding: 1px 4px; border-radius: 4px; max-width: 88px;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
 .sel {{ width: 18px; height: 18px; }}
 #selbar {{ display: none; position: fixed; bottom: 16px; left: 50%;
           transform: translateX(-50%); background: #1c1c1e; color: #fff;
           padding: 10px 18px; border-radius: 12px; font-size: 13px;
           box-shadow: 0 4px 16px rgba(0,0,0,.4); z-index: 20; }}
 #selbar button {{ margin-left: 12px; background: #0a84ff; color: #fff;
                  border: 0; border-radius: 8px; padding: 6px 12px;
                  font-size: 13px; cursor: pointer; }}
 #selbar button.apply {{ background: #30d158; }}
</style></head>
<body>
<header>
 <h1>{h1}</h1>
 <p id="summary"></p>
 <p>{blurb}</p>
</header>
<div class="bar">
 <label>Status
  <select id="fStatus">
   <option value="pending" selected>pending</option>
   <option value="applied">applied</option>
   <option value="all">all</option>
  </select></label>
 <label>Max spread
  <select id="fSpread">
   <option value="0" selected>any</option>
   <option value="1000">≤ 1 km</option>
   <option value="10000">≤ 10 km</option>
  </select></label>
 <label>Search
  <input type="text" id="fText" placeholder="file name…"></label>
 <label>Sort
  <select id="fSort">
   <option value="time" selected>start time</option>
   <option value="miss-desc">photos to tag ↓</option>
   <option value="spread-asc">spread ↑</option>
  </select></label>
 <span id="count"></span>
</div>
<div class="grid" id="grid"></div>
<div id="selbar"><span id="selcount"></span>
 <button id="copyBtn">Copy selected IDs</button>
 <button id="applyBtn" class="apply">Apply selected groups</button>
</div>
<script>
const CLUSTERS = {rows};
const SOURCE = "{source}";
const SERVE_HINT = "{serve_hint}";
// Per-run anti-CSRF token; only present when the page is served by --serve.
const TOKEN_META = document.querySelector('meta[name="apply-token"]');
const APPLY_TOKEN = TOKEN_META ? TOKEN_META.content : "";
let selected = new Set();

function esc(s) {{
  const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
}}

function spreadColor(s) {{
  if (s <= 1000) return "#30d158";
  if (s <= 10000) return "#ff9f0a";
  return "#ff453a";
}}

function clusterCard(c) {{
  const [cid, start, span, nMiss, nLoc, spread, lat, lon, donor, maps,
         status, members] = c;
  const mixed = spread > 10000;
  const checked = selected.has(cid) && !mixed ? "checked" : "";
  const box = mixed
    ? `<input type="checkbox" class="sel" disabled title="mixed locations - not applyable">`
    : `<input type="checkbox" class="sel" data-id="${{cid}}" ${{checked}}>`;
  const stBadge = status === "applied"
    ? `<span class="badge" style="background:#30d158">applied</span>`
    : `<span class="badge" style="background:#ff9f0a">pending</span>`;
  const strip = members.map(m =>
    `<div class="ph ${{m[2] === "missing" ? "missing" : "located"}}${{m[4] ? " donor" : ""}}"
         title="${{esc(m[1])}} — ${{esc(m[3])}}${{m[4] ? " (donor)" : ""}}">
       <img loading="lazy" src="images/${{m[0]}}.jpg"
            onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E'">
       <span>${{esc(m[1])}}</span>
     </div>`).join("");
  return `<div class="cluster" id="card-${{cid}}">
    <div class="chead">
      ${{box}}
      <span class="cid">${{cid}}</span>
      <span class="cdate">${{start}} UTC · ${{span}}</span>
      <span class="badge" style="background:${{spreadColor(spread)}}">spread ${{spread.toLocaleString()}} m</span>
      ${{mixed ? `<span class="badge" style="background:#ff453a">mixed — not applyable</span>` : ``}}
      <span class="counts">${{nMiss}} to tag · ${{nLoc}} located</span>
      ${{stBadge}}
    </div>
    <div class="geo">proposed geotag: ${{lat}}, ${{lon}} from
      <b>${{esc(donor)}}</b> · <a href="${{maps}}" target="_blank">maps</a></div>
    <div class="strip">${{strip}}</div>
  </div>`;
}}

function visible() {{
  const status = document.getElementById("fStatus").value;
  const maxSpread = parseInt(document.getElementById("fSpread").value);
  const text = document.getElementById("fText").value.toLowerCase();
  const sort = document.getElementById("fSort").value;
  let rows = CLUSTERS.filter(c =>
    (status === "all" || c[10] === status) &&
    (!maxSpread || c[5] <= maxSpread) &&
    (!text || c[11].some(m =>
      m[1].toLowerCase().includes(text) || m[0].includes(text))));
  if (sort === "miss-desc") rows.sort((a, b) => b[3] - a[3]);
  else if (sort === "spread-asc") rows.sort((a, b) => a[5] - b[5]);
  else rows.sort((a, b) => a[1].localeCompare(b[1]));
  return rows;
}}

function render() {{
  const rows = visible();
  document.getElementById("grid").innerHTML = rows.map(clusterCard).join("");
  document.getElementById("count").textContent =
    rows.length + " of " + CLUSTERS.length + " groups shown";
  document.querySelectorAll(".sel").forEach(cb => {{
    cb.addEventListener("change", () => {{
      if (cb.checked) selected.add(cb.dataset.id);
      else selected.delete(cb.dataset.id);
      updateSelbar();
    }});
  }});
}}

function updateSelbar() {{
  const bar = document.getElementById("selbar");
  bar.style.display = selected.size ? "block" : "none";
  const photos = CLUSTERS.filter(c => selected.has(c[0]))
    .reduce((n, c) => n + c[3], 0);
  document.getElementById("selcount").textContent =
    selected.size + " groups · " + photos + " photos selected";
}}

function markApplied(ids) {{
  ids.forEach(id => {{
    const c = CLUSTERS.find(x => x[0] === id);
    if (c) c[10] = "applied";
  }});
  selected.clear();
  render();
  updateSelbar();
}}

document.getElementById("copyBtn").addEventListener("click", () => {{
  navigator.clipboard.writeText([...selected].join(", "))
    .then(() => alert("Copied: " + [...selected].join(", ")));
}});

document.getElementById("applyBtn").addEventListener("click", () => {{
  if (!selected.size) return;
  const ids = [...selected];
  fetch("/apply", {{
    method: "POST",
    headers: {{"Content-Type": "application/json",
               "X-Apply-Token": APPLY_TOKEN}},
    body: JSON.stringify({{clusters: ids}}),
  }}).then(r => r.json()).then(res => {{
    if (res.error) {{
      alert("Apply failed: " + res.error);
      return;
    }}
    alert("Applied " + res.applied + " photos across "
          + Object.keys(res.clusters).length + " groups.");
    markApplied(ids);
  }}).catch(() => {{
    navigator.clipboard.writeText(ids.join(", "))
      .then(() => alert("No apply server running - copied the cluster IDs "
        + "instead. Open " + SERVE_HINT + " in the browser (not the "
        + "index.html file), or start it with: "
        + "python3 time_fill_review_app.py --serve"));
  }});
}});

["fStatus", "fSpread", "fText", "fSort"].forEach(id =>
  document.getElementById(id).addEventListener("input", render));

const pending = CLUSTERS.filter(c => c[10] === "pending");
const pendingPhotos = pending.reduce((n, c) => n + c[3], 0);
document.getElementById("summary").textContent =
  CLUSTERS.length + " " + SOURCE + " groups, " + pendingPhotos +
  " photos still to tag. Nothing is written until you press Apply.";
render();
</script>
</body></html>"""


def render_page(clusters, ledger, source="time", port=8788):
    """Return the full index.html for the given compact clusters."""
    # Escape "<" so a filename can never break out of the script block.
    data = json.dumps(compact_clusters(clusters, ledger)) \
        .replace("<", "\\u003c")
    if source == "similar":
        title, h1, blurb = ("immich-geotag — similar fill clusters",
                            "Similar fill — location groups",
                            SIMILAR_BLURB)
    elif source == "similar-time":
        title, h1, blurb = ("immich-geotag — similar+time fill clusters",
                            "Similar + 10 min — location groups",
                            SIMILAR_TIME_BLURB)
    elif source == "similar-loose":
        title, h1, blurb = ("immich-geotag — loose similarity clusters",
                            "Loose similarity (0.80) + 10 min — location "
                            "groups",
                            SIMILAR_LOOSE_BLURB)
    elif source == "similar-vloose":
        title, h1, blurb = ("immich-geotag — very loose similarity clusters",
                            "Very loose similarity (0.70) + 10 min — "
                            "location groups",
                            SIMILAR_VLOOSE_BLURB)
    elif source == "similar-sameday":
        title, h1, blurb = ("immich-geotag — same-day similarity clusters",
                            "Very loose similarity (0.70) + same day — "
                            "location groups",
                            SIMILAR_SAMEDAY_BLURB)
    elif source == "similar-wa":
        title, h1, blurb = ("immich-geotag — WhatsApp send batches",
                            "WhatsApp send batches (0.70 + same day) — "
                            "location groups",
                            SIMILAR_WA_BLURB)
    else:
        title, h1, blurb = ("immich-geotag — time fill clusters",
                            "Time fill — clusters", TIME_BLURB)
    return PAGE.format(rows=data, title=title, h1=h1, blurb=blurb,
                       source=source,
                       serve_hint=f"http://127.0.0.1:{port}/")


# ---------------------------------------------------------------------------
# Build and serve
# ---------------------------------------------------------------------------

def build(args):
    ledger = geotag.read_csv(time_fill.LEDGER)
    if args.source == "similar":
        import similar_fill
        if args.recluster or not os.path.exists(args.clusters):
            similar_fill.run()
        clusters = load_clusters(args.clusters)
        print(f"clusters: {len(clusters)} "
              f"(similar fill, --recluster to recompute)")
    elif args.source == "similar-time":
        import similar_fill
        if args.recluster or not os.path.exists(args.clusters):
            similar_fill.run_time()
        clusters = load_clusters(args.clusters)
        print(f"clusters: {len(clusters)} "
              f"(similar+time fill, --recluster to recompute)")
    elif args.source == "similar-loose":
        import similar_fill
        if args.recluster or not os.path.exists(args.clusters):
            similar_fill.run_loose()
        clusters = load_clusters(args.clusters)
        print(f"clusters: {len(clusters)} "
              f"(loose similarity fill, --recluster to recompute)")
    elif args.source == "similar-vloose":
        import similar_fill
        if args.recluster or not os.path.exists(args.clusters):
            similar_fill.run_vloose()
        clusters = load_clusters(args.clusters)
        print(f"clusters: {len(clusters)} "
              f"(very loose similarity fill, --recluster to recompute)")
    elif args.source == "similar-sameday":
        import similar_fill
        if args.recluster or not os.path.exists(args.clusters):
            similar_fill.run_sameday()
        clusters = load_clusters(args.clusters)
        print(f"clusters: {len(clusters)} "
              f"(same-day similarity fill, --recluster to recompute)")
    elif args.source == "similar-wa":
        import similar_fill
        if args.recluster or not os.path.exists(args.clusters):
            similar_fill.run_wa()
        clusters = load_clusters(args.clusters)
        print(f"clusters: {len(clusters)} "
              f"(WhatsApp batch fill, --recluster to recompute)")
    elif args.recluster or not os.path.exists(args.clusters):
        geotag.load_env()
        immich = geotag.Immich(os.environ["IMMICH_URL"],
                               os.environ["IMMICH_API_KEY"])
        assets = time_fill.scan_library(immich)
        clusters = build_clusters(assets, ledger,
                                  window_s=args.window_min * 60.0)
        write_clusters(clusters, args.clusters)
        print(f"clusters: {len(clusters)} -> "
              f"{os.path.relpath(args.clusters, SCRIPT_DIR)}")
    else:
        clusters = load_clusters(args.clusters)
        print(f"clusters: {len(clusters)} (reused, --recluster to recompute)")

    if args.limit:
        clusters = clusters[:args.limit]

    if not args.skip_images:
        geotag.load_env()
        immich = geotag.Immich(os.environ["IMMICH_URL"],
                               os.environ["IMMICH_API_KEY"])
        ids = image_ids(clusters)
        ok, failed = download_images(immich, ids,
                                     os.path.join(args.out, "images"))
        print(f"thumbnails: {ok} ok, {len(failed)} failed, "
              f"{len(ids) - ok - len(failed)} skipped")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "index.html"), "w",
              encoding="utf-8") as f:
        f.write(render_page(clusters, ledger, source=args.source,
                            port=args.port))
    print("review app: " + os.path.join(args.out, "index.html"))
    return clusters


LOOPBACK_NAMES = ("127.0.0.1", "localhost")
TOKEN_HEADER = "X-Apply-Token"


def inject_token(html_bytes, token):
    """Insert the per-run apply token as a <meta> tag right after <head>."""
    meta = f'<meta name="apply-token" content="{token}">'.encode("ascii")
    return html_bytes.replace(b"<head>", b"<head>" + meta, 1)


def check_apply_request(headers, port, token):
    """Validate a POST /apply request; return (status, error) or None if OK.

    Blocks cross-site request forgery from any other page open in the
    browser: such a page can send a "simple" POST without a preflight, but
    it cannot read the token embedded in our page, cannot set a custom
    header without a CORS preflight (which this server never answers), and
    cannot make the browser send a matching Origin. The Host check stops
    DNS-rebinding attacks that would otherwise make our origin look like
    the attacker's.
    """
    allowed_hosts = {f"{h}:{port}" for h in LOOPBACK_NAMES}
    host = (headers.get("Host") or "").strip().lower()
    if host not in allowed_hosts:
        return 403, "forbidden host"
    origin = headers.get("Origin")
    if origin is not None and origin.strip().lower() not in {
            f"http://{h}" for h in allowed_hosts}:
        return 403, "cross-origin request refused"
    ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return 415, "Content-Type must be application/json"
    sent = headers.get(TOKEN_HEADER) or ""
    if not token or not hmac.compare_digest(sent.encode(), token.encode()):
        return 403, "missing or invalid apply token"
    return None


def make_server(args, token, host="127.0.0.1"):
    """Build (but do not start) the review-app HTTP server.

    Serves args.out/index.html (with the apply token injected) and
    args.out/images/, and exposes the guarded POST /apply.
    """
    import http.server
    import urllib.parse

    out_dir = os.path.abspath(args.out)
    index = os.path.join(out_dir, "index.html")

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                with open(index, "rb") as f:
                    body = inject_token(f.read(), token)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path.startswith("/images/"):
                name = os.path.basename(path)
                fp = os.path.join(out_dir, "images", name)
                if os.path.isfile(fp):
                    with open(fp, "rb") as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send(404, '{"error": "not found"}')
            else:
                self._send(404, '{"error": "not found"}')

        def do_POST(self):
            if urllib.parse.urlparse(self.path).path != "/apply":
                self._send(404, '{"error": "not found"}')
                return
            refused = check_apply_request(
                self.headers, self.server.server_address[1], token)
            if refused:
                self._send(refused[0], json.dumps({"error": refused[1]}))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                ids = payload.get("clusters") or []
                if not ids:
                    raise ValueError("no clusters selected")
                source = {"similar": "similar-cluster",
                          "similar-time": "similar-time-cluster",
                          "similar-loose": "similar-loose-cluster",
                          "similar-vloose": "similar-vloose-cluster",
                          "similar-sameday": "similar-sameday-cluster",
                          "similar-wa": "similar-wa-cluster"
                          }.get(args.source, "time-cluster")
                total, per = time_fill.apply_clusters(
                    ids, clusters_path=args.clusters, source=source)
                self._send(200, json.dumps(
                    {"applied": total, "clusters": per}))
            except SystemExit as e:
                self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))

        def log_message(self, fmt, *a):
            print("%s - %s" % (self.address_string(), fmt % a))

    return http.server.ThreadingHTTPServer((host, args.port), Handler)


def serve(args):
    """Serve the review app locally and expose POST /apply."""
    import secrets

    build(args)
    server = make_server(args, secrets.token_urlsafe(32))
    print(f"serving review app at http://127.0.0.1:{args.port}/ "
          f"(Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nserver stopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=None,
                    help="output directory (default: time-fill-review-app, "
                         "or similar-fill-review-app with --source similar)")
    ap.add_argument("--source", choices=["time", "similar", "similar-time",
                                         "similar-loose", "similar-vloose",
                                         "similar-sameday", "similar-wa"],
                    default="time",
                    help="which clusters to show: time groups (default), "
                         "similarity location groups, the lower-"
                         "threshold + 10-minute similarity groups, or "
                         "the loose (0.80) + 10-minute groups")
    ap.add_argument("--clusters", default=None,
                    help="clusters.csv to show (default: the clusters "
                         "belonging to --source)")
    ap.add_argument("--recluster", action="store_true",
                    help="recompute clusters (time: fresh scan, similar: "
                         "rebuild from the CLIP match dump; cluster IDs "
                         "may change)")
    ap.add_argument("--window-min", type=float, default=10.0,
                    help="time gap that splits two groups, in minutes "
                         "(default 10)")
    ap.add_argument("--limit", type=int, default=None,
                    help="only include the first N clusters (testing)")
    ap.add_argument("--skip-images", action="store_true",
                    help="rebuild the page without downloading thumbnails")
    ap.add_argument("--serve", action="store_true",
                    help="serve the app locally with an Apply button "
                         "endpoint (127.0.0.1)")
    ap.add_argument("--port", type=int, default=8788,
                    help="port for --serve (default 8788)")
    args = ap.parse_args()
    if args.out is None:
        args.out = {"similar": SIMILAR_OUT,
                    "similar-time": SIMILAR_TIME_OUT,
                    "similar-loose": SIMILAR_LOOSE_OUT,
                    "similar-vloose": SIMILAR_VLOOSE_OUT,
                    "similar-sameday": SIMILAR_SAMEDAY_OUT,
                    "similar-wa": SIMILAR_WA_OUT,
                    "time": DEFAULT_OUT}[args.source]
    if args.clusters is None:
        import similar_fill
        args.clusters = {"similar": similar_fill.CLUSTERS,
                         "similar-time": similar_fill.TIME_CLUSTERS,
                         "similar-loose": similar_fill.LOOSE_CLUSTERS,
                         "similar-vloose": similar_fill.VLOOSE_CLUSTERS,
                         "similar-sameday": similar_fill.SAMEDAY_CLUSTERS,
                         "similar-wa": similar_fill.WA_CLUSTERS,
                         "time": time_fill.CLUSTERS}[args.source]
    if args.serve:
        serve(args)
    else:
        build(args)


if __name__ == "__main__":
    main()

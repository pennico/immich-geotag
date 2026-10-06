#!/usr/bin/env python3
"""Interactive review app for cluster-fill proposals.

Reads geotag-data/cluster/proposals.csv, downloads thumbnail-size JPEGs
for every proposed photo and its best match, and builds a self-contained
index.html (data embedded as JSON, vanilla JS) with:

- status filter (applied / proposal), similarity slider, text search
- sorting by similarity or file name
- side-by-side comparison cards with Google Maps / Immich links
- checkbox selection with a copy-selected-asset-IDs helper, so reviewed
  proposals can be pasted back for approval

Usage:
  python3 cluster_review_app.py [--data PATH] [--out DIR] [--limit N]
                               [--skip-images]

Defaults: data=geotag-data/cluster/proposals.csv,
out=geotag-data/cluster-review-app (both relative to this script).
"""

import argparse
import concurrent.futures
import json
import os
import sys

import cluster_fill
import geotag

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA = os.path.join(SCRIPT_DIR, "geotag-data", "cluster", "proposals.csv")
DEFAULT_MATCHES = os.path.join(SCRIPT_DIR, "geotag-data", "cluster", "matches.csv")
DEFAULT_OUT = os.path.join(SCRIPT_DIR, "geotag-data", "cluster-review-app")

# Confirmation thresholds (mirror cluster_fill.confirm defaults).
MIN_SIM = 0.90
AGREE_M = 100.0
MAX_CLUSTER_SHOWN = 6

# (asset_id, file_name, lat, lon, similarity, match_id, match_name,
#  n_matches, spread_m, status, maps_url, immich_url, cluster)
FIELDS = ("asset_id", "file_name", "latitude", "longitude", "similarity",
          "match_id", "match_name", "n_matches", "spread_m", "status",
          "maps_url", "immich_url")


def build_clusters(rows, matches_path, max_shown=MAX_CLUSTER_SHOWN):
    """Per proposal row, the cluster of similar located photos considered.

    Returns {asset_id: [[match_id, match_name, similarity, agree], ...]}
    sorted by similarity (desc), capped at max_shown. "agree" mirrors the
    confirmation rule: similarity >= MIN_SIM and within AGREE_M of the
    proposed coordinates. Empty dict when matches.csv is missing.
    """
    if not os.path.exists(matches_path):
        return {}
    groups = cluster_fill.parse_matches(matches_path)
    clusters = {}
    for r in rows:
        g = groups.get(r["asset_id"])
        if not g:
            continue
        lat, lon = float(r["latitude"]), float(r["longitude"])
        entries = []
        for m in sorted(g["matches"], key=lambda m: -m["similarity"]):
            agree = (m["similarity"] >= MIN_SIM
                     and cluster_fill.haversine_m(lat, lon, m["latitude"],
                                                  m["longitude"]) <= AGREE_M)
            entries.append([m["match_id"], m["match_name"],
                            round(m["similarity"], 4), int(agree)])
        clusters[r["asset_id"]] = entries[:max_shown]
    return clusters


def compact_rows(rows, clusters=None):
    """Project CSV rows into compact tuples for embedding as JSON."""
    clusters = clusters or {}
    out = []
    for r in rows:
        out.append([
            r["asset_id"], r["file_name"],
            round(float(r["latitude"]), 6), round(float(r["longitude"]), 6),
            round(float(r["similarity"]), 4),
            r["match_id"], r["match_name"],
            int(r["n_matches"]), int(float(r["spread_m"])),
            r["status"], r["maps_url"], r["immich_url"],
            clusters.get(r["asset_id"], []),
        ])
    return out


def image_ids(rows, clusters=None):
    """Unique asset ids needed as thumbnails (photo + cluster, deduped)."""
    clusters = clusters or {}
    ids = set()
    for r in rows:
        ids.add(r["asset_id"])
        ids.add(r["match_id"])
        for m in clusters.get(r["asset_id"], []):
            ids.add(m[0])
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


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>immich-geotag — cluster fill review</title>
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
 .bar input[type=range] {{ width: 140px; }}
 #count {{ font-size: 12px; color: #8e8e93; margin-left: auto; }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
         gap: 16px; padding: 16px; }}
 .card {{ background: #fff; border-radius: 12px; overflow: hidden;
         box-shadow: 0 1px 4px rgba(0,0,0,.15); }}
 .pair {{ display: grid; grid-template-columns: 1fr 1fr; position: relative; }}
 .pair img {{ width: 100%; display: block; aspect-ratio: 4/3; object-fit: cover;
             background: #e5e5ea; }}
 .tag {{ position: absolute; top: 8px; left: 8px; color: #fff; font-size: 11px;
        padding: 2px 8px; border-radius: 8px; background: rgba(0,0,0,.55); }}
 .meta {{ padding: 10px 14px; }}
 .meta h2 {{ font-size: 14px; margin: 0 0 4px; font-weight: 600; }}
 .file {{ color: #8e8e93; font-size: 11px; margin: 0 0 6px;
         font-family: ui-monospace, monospace; word-break: break-all; }}
 .meta p {{ margin: 3px 0; font-size: 12px; color: #555; }}
 a {{ color: #0a84ff; text-decoration: none; }}
 .badge {{ color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 8px; }}
 .sel {{ position: absolute; top: 8px; right: 8px; width: 22px; height: 22px; }}
 .strip {{ display: flex; gap: 6px; padding: 0 14px 4px; flex-wrap: wrap; }}
 .thumb {{ position: relative; width: 72px; }}
 .thumb img {{ width: 72px; height: 54px; object-fit: cover; display: block;
              border-radius: 6px; border: 2px solid #c7c7cc; background: #e5e5ea; }}
 .thumb.agree img {{ border-color: #30d158; }}
 .thumb span {{ position: absolute; bottom: 2px; right: 2px;
               background: rgba(0,0,0,.6); color: #fff; font-size: 9px;
               padding: 1px 4px; border-radius: 4px; }}
 .cluhead {{ font-size: 11px; color: #8e8e93; padding: 8px 14px 4px; margin: 0; }}
 #selbar {{ display: none; position: fixed; bottom: 16px; left: 50%;
           transform: translateX(-50%); background: #1c1c1e; color: #fff;
           padding: 10px 18px; border-radius: 12px; font-size: 13px;
           box-shadow: 0 4px 16px rgba(0,0,0,.4); z-index: 20; }}
 #selbar button {{ margin-left: 12px; background: #0a84ff; color: #fff;
                  border: 0; border-radius: 8px; padding: 6px 12px;
                  font-size: 13px; cursor: pointer; }}
</style></head>
<body>
<header>
 <h1>Cluster fill — review</h1>
 <p id="summary"></p>
 <p>Green = already applied to Immich. Orange = proposal only. Each card shows
 the photo, its best match, and the cluster of similar located photos that
 voted (green border = agrees within 100 m). Tick the good proposals and copy
 their asset IDs to send back for approval.</p>
</header>
<div class="bar">
 <label>Status
  <select id="fStatus">
   <option value="all">all</option>
   <option value="review" selected>proposals</option>
   <option value="applied">applied</option>
  </select></label>
 <label>Min similarity <span id="simVal">0.90</span>
  <input type="range" id="fSim" min="0.80" max="1.00" step="0.01" value="0.90"></label>
 <label>Search
  <input type="text" id="fText" placeholder="file name…"></label>
 <label>Sort
  <select id="fSort">
   <option value="sim-desc">similarity ↓</option>
   <option value="sim-asc">similarity ↑</option>
   <option value="name">file name</option>
  </select></label>
 <span id="count"></span>
</div>
<div class="grid" id="grid"></div>
<div id="selbar"><span id="selcount"></span><button id="copyBtn">Copy selected IDs</button></div>
<script>
const ROWS = {rows};
const STATUS_COLOR = {{"applied": "#30d158", "review": "#ff9f0a"}};
const STATUS_LABEL = {{"applied": "applied", "review": "proposal"}};
let selected = new Set();

function esc(s) {{
  const d = document.createElement("div"); d.textContent = s; return d.innerHTML;
}}

function card(r) {{
  const [aid, name, lat, lon, sim, mid, mname, n, spread, status, maps, immich, cluster] = r;
  const checked = selected.has(aid) ? "checked" : "";
  const strip = cluster.map(m =>
    `<div class="thumb ${{m[3] ? "agree" : ""}}" title="${{esc(m[1])}} (sim ${{m[2]}})${{m[3] ? " - agrees" : ""}}">
       <img loading="lazy" src="images/${{m[0]}}.jpg"
            onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E'">
       <span>${{m[2].toFixed(2)}}</span>
     </div>`).join("");
  return `<div class="card">
    <div class="pair">
      <img loading="lazy" src="images/${{aid}}.jpg" alt="${{esc(name)}}"
           onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E'">
      <img loading="lazy" src="images/${{mid}}.jpg" alt="match"
           onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22/%3E'">
      <span class="tag">${{esc(name).slice(0, 28)}}</span>
      <input type="checkbox" class="sel" data-aid="${{aid}}" ${{checked}}
             onchange="toggleSel('${{aid}}', this.checked)">
    </div>
    <div class="meta">
      <h2>similarity ${{sim.toFixed(2)}} &middot; ${{n}} agreeing &middot; spread ${{spread}} m</h2>
      <p class="file">${{esc(name)}} &middot; matched ${{esc(mname)}}</p>
      <p>${{lat.toFixed(5)}}, ${{lon.toFixed(5)}}</p>
      <p><span class="badge" style="background:${{STATUS_COLOR[status]}}">${{STATUS_LABEL[status]}}</span></p>
      <p><a href="${{maps}}" target="_blank">Google Maps</a> &middot;
         <a href="${{immich}}" target="_blank">Open in Immich</a></p>
    </div>
    ${{cluster.length ? `<p class="cluhead">Cluster - similar located photos (green border = agrees within 100 m):</p><div class="strip">${{strip}}</div>` : ""}}
  </div>`;
}}

function toggleSel(aid, on) {{
  if (on) selected.add(aid); else selected.delete(aid);
  updateSelbar();
}}

function updateSelbar() {{
  const bar = document.getElementById("selbar");
  if (selected.size === 0) {{ bar.style.display = "none"; return; }}
  bar.style.display = "block";
  document.getElementById("selcount").textContent =
    selected.size + " selected";
}}

function applyFilters() {{
  const status = document.getElementById("fStatus").value;
  const minSim = parseFloat(document.getElementById("fSim").value);
  const text = document.getElementById("fText").value.toLowerCase();
  const sort = document.getElementById("fSort").value;
  let rows = ROWS.filter(r => (status === "all" || r[9] === status)
    && r[4] >= minSim
    && (!text || r[1].toLowerCase().includes(text)));
  if (sort === "sim-desc") rows.sort((a, b) => b[4] - a[4]);
  else if (sort === "sim-asc") rows.sort((a, b) => a[4] - b[4]);
  else rows.sort((a, b) => a[1].localeCompare(b[1]));
  document.getElementById("grid").innerHTML = rows.map(card).join("");
  document.getElementById("count").textContent =
    rows.length + " of " + ROWS.length + " shown";
}}

document.getElementById("fStatus").onchange = applyFilters;
document.getElementById("fSim").oninput = () => {{
  document.getElementById("simVal").textContent =
    document.getElementById("fSim").value;
  applyFilters();
}};
document.getElementById("fText").oninput = applyFilters;
document.getElementById("fSort").onchange = applyFilters;
document.getElementById("copyBtn").onclick = () => {{
  const text = Array.from(selected).join(",");
  navigator.clipboard.writeText(text).then(() => {{
    document.getElementById("copyBtn").textContent = "Copied!";
    setTimeout(() => document.getElementById("copyBtn").textContent =
      "Copy selected IDs", 1500);
  }});
}};
document.getElementById("summary").textContent =
  `${{ROWS.filter(r => r[9] === "applied").length}} applied, ` +
  `${{ROWS.filter(r => r[9] === "review").length}} proposals for review.`;
applyFilters();
</script>
</body></html>"""


def render_page(rows, clusters=None):
    """Return the full index.html for the given compact rows."""
    # Escape "<" so a filename can never break out of the script block.
    data = json.dumps(compact_rows(rows, clusters)).replace("<", "\\u003c")
    return PAGE.format(rows=data)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", default=DEFAULT_DATA)
    ap.add_argument("--matches", default=DEFAULT_MATCHES,
                    help="pgvector match dump used to show full clusters")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--limit", type=int, default=None,
                    help="only include the first N rows (testing)")
    ap.add_argument("--skip-images", action="store_true",
                    help="rebuild the page without downloading thumbnails")
    args = ap.parse_args()

    rows = geotag.read_csv(args.data)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        sys.exit("no rows in " + args.data)

    clusters = build_clusters(rows, args.matches)

    if not args.skip_images:
        geotag.load_env()
        immich = geotag.Immich(os.environ["IMMICH_URL"],
                               os.environ["IMMICH_API_KEY"])
        ids = image_ids(rows, clusters)
        ok, failed = download_images(immich, ids,
                                     os.path.join(args.out, "images"))
        print(f"thumbnails: {ok} ok, {len(failed)} failed, "
              f"{len(ids) - ok - len(failed)} skipped")

    os.makedirs(args.out, exist_ok=True)
    page = render_page(rows, clusters)
    with open(os.path.join(args.out, "index.html"), "w",
              encoding="utf-8") as f:
        f.write(page)
    print("review app: " + os.path.join(args.out, "index.html"))


if __name__ == "__main__":
    main()

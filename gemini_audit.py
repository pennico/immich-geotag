#!/usr/bin/env python3
"""gemini_audit: QA sample of photos that already have a location.

The owner's rule: every Nth located photo (--sample-every, default 100)
is shown to Gemini, which names the place it depicts; the answer is
geocoded and compared with the existing GPS tag. Verdicts:

  match       proposal within 25 km of the existing tag
  check       25-100 km apart - worth a look
  mismatch    more than 100 km apart - tag or identification is wrong
  no_id       nothing identifiable in the photo (fine - GPS stays)
  no_geocode  identified, but the geocoder could not resolve it

Writes geotag-data/gemini-audit/ (audit.csv + index.html + images/).
Resume-safe: re-running skips asset_ids already in audit.csv. Never
writes to Immich.

Usage:
  python3 gemini_audit.py                    # sample every 100th photo
  python3 gemini_audit.py --sample-every 50 --limit 20
"""

import argparse
import csv
import html
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import geotag  # noqa: E402
from gemini_apply import haversine_km  # noqa: E402
from gemini_candidates import gemini_identify, geocode_place  # noqa: E402
from geotag import http_json  # noqa: E402

AUDIT_COLUMNS = [
    "asset_id", "file_name", "taken_at", "existing_lat", "existing_lon",
    "place", "city", "country", "confidence", "proposed_lat",
    "proposed_lon", "geocoded_name", "distance_km", "verdict",
    "immich_url", "run_date",
]

VERDICT_BADGE = {"match": "#30d158", "check": "#ff9f0a",
                 "mismatch": "#ff453a", "no_id": "#8e8e93",
                 "no_geocode": "#8e8e93"}


def sample_every(items, every):
    """Systematic sample: every Nth item, starting at the first."""
    if every <= 0:
        return []
    return items[::every]


def verdict_for(distance_km):
    """Classify the distance between existing tag and proposal."""
    if distance_km is None:
        return "no_geocode"
    if distance_km <= 25:
        return "match"
    if distance_km <= 100:
        return "check"
    return "mismatch"


def located_images(immich):
    """Yield IMAGE assets that carry GPS, in Immich's search order."""
    page = 1
    while True:
        data = http_json("POST", immich._url("/search/metadata"),
                         headers=immich.headers,
                         payload={"page": page, "size": 250, "type": "IMAGE",
                                  "withExif": True})
        assets = (data.get("assets") or {}).get("items") or []
        for a in assets:
            exif = a.get("exifInfo") or {}
            if exif.get("latitude") is not None and \
                    exif.get("longitude") is not None:
                yield a
        if not assets or (data.get("assets") or {}).get("nextPage") is None:
            return
        page += 1


def build_page(rows, sampled_of, every):
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in
                       sorted(counts.items())) or "nothing yet"
    cards = []
    for r in rows:
        dist = ("{:.1f} km apart".format(float(r["distance_km"]))
                if r.get("distance_km") else "-")
        cards.append("""
<div class="card">
  <img src="images/{short}_{name}" loading="lazy" alt="{name}">
  <div class="meta">
    <h2>{place}</h2>
    <p class="file">{name} &middot; {taken}</p>
    <p>tag: {elat},{elon} &middot; proposed: {plat},{plon}</p>
    <p class="geo">{dist} &middot; {geo}</p>
    <p><span class="badge" style="background:{color}">{verdict}</span></p>
    <p><a href="{url}">Open in Immich</a></p>
  </div>
</div>""".format(short=r["asset_id"][:8], name=html.escape(r["file_name"]),
                 place=html.escape(r["place"] or "not identified"),
                 taken=html.escape((r.get("taken_at") or "")[:16]),
                 elat=r["existing_lat"], elon=r["existing_lon"],
                 plat=r["proposed_lat"] or "-", plon=r["proposed_lon"] or "-",
                 dist=dist, geo=html.escape(r["geocoded_name"] or ""),
                 color=VERDICT_BADGE.get(r["verdict"], "#8e8e93"),
                 verdict=r["verdict"], url=html.escape(r["immich_url"])))
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>immich-geotag — location audit</title>
<style>
 body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f5f5f7; }}
 header {{ padding: 24px; background: #1c1c1e; color: #fff; }}
 h1 {{ margin: 0 0 8px; font-size: 22px; }}
 header p {{ margin: 0 0 6px; color: #aeaeb2; font-size: 14px; }}
 .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
          gap: 16px; padding: 16px; }}
 .card {{ background: #fff; border-radius: 12px; overflow: hidden;
          box-shadow: 0 1px 4px rgba(0,0,0,.15); }}
 .card img {{ width: 100%; display: block; aspect-ratio: 4/3; object-fit: cover; }}
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
 <h1>Location audit — sampled 1 in {every} located photos</h1>
 <p>{n} photos checked of {total} located. {summary}</p>
 <p>Green = proposal matches the tag. Orange = 25-100 km apart, worth a
 look. Red = more than 100 km apart, the tag or the identification is
 wrong. Grey = nothing identifiable or geocode failed (not a problem).</p>
</header>
<div class="grid">{cards}</div>
</body></html>""".format(every=every, n=len(rows), total=sampled_of,
                         summary=summary, cards="".join(cards))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=os.path.join(
        SCRIPT_DIR, "geotag-data", "gemini-audit"))
    parser.add_argument("--sample-every", type=int, default=100,
                        help="sample every Nth located photo (default 100)")
    parser.add_argument("--limit", type=int, default=None,
                        help="max photos this run")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--backoff", type=int, default=20)
    args = parser.parse_args()

    geotag.load_env()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")
    email = os.environ.get("NOMINATIM_EMAIL")
    public_url = os.environ.get("IMMICH_PUBLIC_URL", immich_url).rstrip("/")
    if not api_key or not immich_url or not immich_key:
        sys.exit("Missing GEMINI_API_KEY / IMMICH_URL / IMMICH_API_KEY")
    immich = geotag.Immich(immich_url, immich_key)

    os.makedirs(os.path.join(args.out, "images"), exist_ok=True)
    csv_path = os.path.join(args.out, "audit.csv")
    done = set()
    rows = []
    if os.path.exists(csv_path):
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
            done = {r["asset_id"] for r in rows}

    print("enumerating located photos...")
    located = [a for a in located_images(immich) if a["id"] not in done]
    sampled = sample_every(located, args.sample_every)
    if args.limit:
        sampled = sampled[:args.limit]
    print(f"{len(located) + len(done)} located photos, {len(done)} already audited, {len(sampled)} sampled this run")
    if not sampled:
        print("nothing to do")
        return

    geocode_cache = {}
    for start in range(0, len(sampled), args.batch_size):
        batch = sampled[start:start + args.batch_size]
        previews = []
        kept = []
        for a in batch:
            try:
                previews.append(immich.preview_jpeg(a["id"]))
                kept.append(a)
            except geotag.ApiError as e:
                print("{} ERROR {}".format(a.get("originalFileName", ""), e))
        if not kept:
            continue
        try:
            results = gemini_identify(previews, api_key, args.model,
                                      retries=args.retries,
                                      backoff=args.backoff)
        except geotag.ApiError as e:
            print(f"Gemini error, stopping cleanly: {e}")
            break

        for a, res in zip(kept, results):
            exif = a.get("exifInfo") or {}
            row = {
                "asset_id": a["id"],
                "file_name": a.get("originalFileName", ""),
                "taken_at": exif.get("dateTimeOriginal") or "",
                "existing_lat": str(exif.get("latitude")),
                "existing_lon": str(exif.get("longitude")),
                "place": res["place"] or "", "city": res["city"] or "",
                "country": res["country"] or "",
                "confidence": "{:.2f}".format(res["confidence"]),
                "proposed_lat": "", "proposed_lon": "",
                "geocoded_name": "", "distance_km": "",
                "verdict": "no_id", "immich_url": "{}/photos/{}".format(
                    public_url, a["id"]),
                "run_date": time.strftime("%Y-%m-%d"),
            }
            if res["place"] and res["confidence"] >= args.min_confidence:
                key = (res["place"], res["city"], res["country"])
                if key not in geocode_cache:
                    geocode_cache[key] = geocode_place(
                        res["place"], res["city"], res["country"], email)
                geo = geocode_cache[key]
                if geo:
                    lat, lon, name = geo
                    d = haversine_km(exif["latitude"], exif["longitude"],
                                     lat, lon)
                    row.update({
                        "proposed_lat": str(lat), "proposed_lon": str(lon),
                        "geocoded_name": name,
                        "distance_km": f"{d:.1f}",
                        "verdict": verdict_for(d),
                    })
                else:
                    row["verdict"] = "no_geocode"
            rows.append(row)
            print("{:40.40}  {}  {}".format(
                row["file_name"], row["verdict"],
                row["place"] or "-"))
        geotag.write_csv(csv_path, rows, AUDIT_COLUMNS)

        for a in kept:
            dest = os.path.join(args.out, "images",
                                "{}_{}".format(a["id"][:8],
                                               a.get("originalFileName", "")))
            if not os.path.exists(dest):
                with open(dest, "wb") as f:
                    f.write(immich.preview_jpeg(a["id"]))

    geotag.write_csv(csv_path, rows, AUDIT_COLUMNS)
    with open(os.path.join(args.out, "index.html"), "w",
              encoding="utf-8") as f:
        f.write(build_page(rows, len(located) + len(done),
                           args.sample_every))
    print(f"done: {len(rows)} audited rows, output: {args.out}")


if __name__ == "__main__":
    main()

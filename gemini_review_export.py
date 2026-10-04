#!/usr/bin/env python3
"""gemini_review_export: owner review folder for the Gemini passes.

Builds geotag-data/gemini-review/ from the candidates ledger:
  images/<short-id>_<file_name>   the picture
  gemini_candidates.csv           the full ledger
  index.html                      timeline with status badges

Sections:
  1. located + applied to Immich (green / blue badges)
  2. flagged outliers - small isolated groups, owner review (orange)
  3. identified but NOT geocoded - confident Gemini proposals the geocoder
     could not resolve; no coordinates were written (grey)

Usage:
  python3 gemini_review_export.py            # rebuild the folder
  python3 gemini_review_export.py --no-images  # HTML + CSV only
"""

import argparse
import csv
import html
import os
import shutil
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import geotag  # noqa: E402
from gemini_candidates import CANDIDATE_COLUMNS  # noqa: E402

DATA = os.path.join(SCRIPT_DIR, "geotag-data")

BADGE = {"applied": ("#30d158", "applied to Immich"),
         "confirmed": ("#0a84ff", "confirmed via nearby photos, applied"),
         "candidate": ("#0a84ff", "candidate (not yet applied)")}


def load_rows():
    with open(os.path.join(DATA, "gemini_candidates.csv"), newline="",
              encoding="utf-8") as f:
        return list(csv.DictReader(f))


def split_rows(all_rows):
    located = [r for r in all_rows if r.get("latitude")
               and r["status"] in ("applied", "candidate", "confirmed")]
    flagged = [r for r in all_rows
               if r["status"] == "flagged"
               or r.get("flag") in ("outlier", "geo-outlier")]
    ungeocoded = [r for r in all_rows
                  if r["status"] == "no_match" and r.get("place")
                  and float(r.get("confidence") or 0) >= 0.5]
    return located, flagged, ungeocoded


def card(r, color, label):
    img = "images/{}_{}".format(r["asset_id"][:8], r["file_name"])
    conf = (" &middot; confidence {:.2f}".format(float(r["confidence"]))
            if r.get("confidence") else "")
    if r.get("latitude"):
        coords = ("<p>{:.5f},{:.5f}{}</p><p class='geo'>{}</p>".format(
            float(r["latitude"]), float(r["longitude"]), conf,
            html.escape(r["geocoded_name"])))
    elif r.get("confidence"):
        coords = "<p>confidence {:.2f}</p>".format(float(r["confidence"]))
    else:
        coords = ""
    maps = ("<a href='{}'>Google Maps</a> &middot; ".format(
        html.escape(r["maps_url"])) if r.get("maps_url") else "")
    return """
<div class="card">
  <img src="{}" loading="lazy" alt="{}">
  <div class="meta">
    <h2>{}</h2>
    <p class="file">{} &middot; {}</p>
    {}
    <p><span class="badge" style="background:{}">{}</span></p>
    <p>{}<a href="{}">Open in Immich</a></p>
  </div>
</div>""".format(html.escape(img), html.escape(r["file_name"]),
                 html.escape(r["place"]),
                 html.escape(r["file_name"]),
                 html.escape((r.get("taken_at") or "")[:16]),
                 coords, color, label, maps,
                 html.escape(r["immich_url"]))


def section(title, cards_html, note=""):
    if not cards_html:
        return ""
    out = "<h2 class='section'>{}</h2>".format(title)
    if note:
        out += "<p class='note'>{}</p>".format(note)
    return out + "<div class='grid'>{}</div>".format("".join(cards_html))


def build_page(located, flagged, ungeocoded, no_match):
    located.sort(key=lambda r: r.get("taken_at") or "")
    cards = [card(r, *BADGE.get(r["status"], ("#8e8e93", r["status"])))
             for r in located]
    flag_cards = [card(r, "#ff9f0a",
                       "flagged for review - small isolated group")
                  for r in flagged]
    ungeo_cards = [card(r, "#8e8e93",
                        "identified but not geocoded - no location written")
                   for r in ungeocoded]
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>immich-geotag — Gemini pass results</title>
<style>
 body {{ font-family: -apple-system, sans-serif; margin: 0; background: #f5f5f7; }}
 header {{ padding: 24px; background: #1c1c1e; color: #fff; }}
 h1 {{ margin: 0 0 8px; font-size: 22px; }}
 header p {{ margin: 0 0 6px; color: #aeaeb2; font-size: 14px; }}
 .section {{ padding: 24px 16px 0; font-size: 18px; }}
 .note {{ padding: 0 16px; color: #8e8e93; font-size: 13px; margin: 4px 0 0; }}
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
 <h1>Gemini pass — results</h1>
 <p>{} photos located and written to Immich (chronological).
 {} flagged as isolated outliers, {} identified but
 left blank (geocoder could not resolve them). {} photos without a
 location in total.</p>
 <p>Green/blue = applied (own Gemini proposal, trip-geography checked where
 a trip region was given). Orange = flagged for your review. Grey = confident
 identification, no coordinates written - reply with the ones that are
 correct and they will be geocoded manually. Wrong one? Note the file name
 and reply on Telegram.</p>
</header>
{}
{}
{}
</body></html>""".format(len(located), len(flagged), len(ungeocoded),
                         no_match,
                         section("Located and applied", cards),
                         section("Flagged for review (isolated small groups)",
                                 flag_cards,
                                 "Few photos of one place, far away in time "
                                 "from everything else - per your rule these "
                                 "need a manual look before they count."),
                         section("Identified but not geocoded (left blank)",
                                 ungeo_cards,
                                 "Gemini confidently recognized the place but "
                                 "the geocoder could not resolve it, so no "
                                 "location was written. Review and reply with "
                                 "the ones that are correct."))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-images", action="store_true",
                        help="skip downloading preview images")
    args = parser.parse_args()

    geotag.load_env()
    immich = geotag.Immich(os.environ["IMMICH_URL"],
                           os.environ["IMMICH_API_KEY"])

    all_rows = load_rows()
    located, flagged, ungeocoded = split_rows(all_rows)
    no_match = sum(1 for r in all_rows if r["status"] == "no_match")
    print("located: {}, flagged: {}, identified-but-ungeocoded: {}, "
          "no_match total: {}".format(len(located), len(flagged),
                                      len(ungeocoded), no_match))

    out = os.path.join(DATA, "gemini-review")
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "images"), exist_ok=True)

    if not args.no_images:
        for r in located + flagged + ungeocoded:
            dest = os.path.join(out, "images",
                                "{}_{}".format(r["asset_id"][:8],
                                               r["file_name"]))
            with open(dest, "wb") as f:
                f.write(immich.preview_jpeg(r["asset_id"]))
        print("images: {}".format(len(os.listdir(
            os.path.join(out, "images")))))

    with open(os.path.join(out, "gemini_candidates.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CANDIDATE_COLUMNS)
        w.writeheader()
        w.writerows(all_rows)

    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_page(located, flagged, ungeocoded, no_match))
    print("wrote {}".format(os.path.join(out, "index.html")))


if __name__ == "__main__":
    main()

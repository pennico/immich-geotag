#!/usr/bin/env python3
"""gemini_candidates: Gemini-based location candidates for unlocated photos.

Companion to geotag.py. Targets images that Vision + Lens failed to locate
(status=not_found in state.csv), asks Gemini (3.8 or newer, per project
policy) to identify the place shown in each photo, geocodes the answer via
Nominatim, and writes the result to a candidates CSV for owner review.

This script NEVER writes to Immich. Applying approved candidates goes
through the existing geotag.py review flow.

Configuration (environment or .env next to geotag.py):
  GEMINI_API_KEY   Gemini API key                (required)
  IMMICH_URL       e.g. https://immich.example   (required)
  IMMICH_API_KEY   Immich API key                (required)
  NOMINATIM_EMAIL    contact email for Nominatim   (optional)
  IMMICH_PUBLIC_URL  browser-facing Immich URL for review links
                     (optional, defaults to IMMICH_URL)

Usage:
  python3 gemini_candidates.py --after 2026-03-01 --before 2026-05-01
  python3 gemini_candidates.py --asset 73a6b604-... --out geotag-data/gemini_candidates.csv
"""

import argparse
import base64
import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from geotag import (  # noqa: E402
    ApiError,
    Immich,
    http_json,
    load_env,
    nominatim_search,
    read_csv,
    write_csv,
)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

CANDIDATE_COLUMNS = [
    "asset_id", "file_name", "place", "city", "country", "confidence",
    "latitude", "longitude", "geocoded_name", "maps_url", "immich_url",
    "status", "run_date", "taken_at", "inferred_from",
]

PROMPT_TEMPLATE = (
    "You are shown {n} photographs, numbered 1 to {n} in the order they appear.\n"
    "For EACH photograph, identify the specific place where it was taken:\n"
    "- a landmark, monument, building, or identifiable site (preferred), or\n"
    "- the city/town if no specific site is identifiable.\n"
    "Rules:\n"
    "- Only name a place you are confident about; never guess from generic scenery.\n"
    "- Do not name people, objects, food, or interiors as places.\n"
    "- Return ONLY a JSON array with exactly {n} objects, one per photograph:\n"
    '  [{{"index": 1, "place": "Chichen Itza", "city": "Tinum", '
    '"country": "Mexico", "confidence": 0.9}}, ...]\n'
    '- If a photograph has no identifiable place, use '
    '{{"index": i, "place": null, "confidence": 0}}.'
)


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

def gemini_identify(images, api_key, model, retries=3, backoff=20):
    """Send a batch of JPEG byte strings to Gemini, return parsed results.

    Returns a list of dicts {index, place, city, country, confidence},
    one per image, in order. Missing/malformed entries become place=None.
    Transient server errors (HTTP 5xx) are retried with a backoff.
    """
    n = len(images)
    parts = [{"text": PROMPT_TEMPLATE.format(n=n)}]
    for img in images:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(img).decode("ascii"),
            }
        })
    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    for attempt in range(retries + 1):
        try:
            data = http_json("POST", GEMINI_URL.format(model),
                             headers={"x-goog-api-key": api_key}, payload=payload,
                             timeout=180)
            break
        except ApiError as e:
            if attempt < retries and "HTTP 5" in str(e):
                print("Gemini transient error (attempt {}/{}), retrying in {}s: {}".format(
                    attempt + 1, retries + 1, backoff, str(e)[:120]))
                time.sleep(backoff)
                continue
            raise
    text = ""
    for cand in data.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            text += part.get("text") or ""
    return parse_gemini_json(text, n)


def parse_gemini_json(text, n):
    """Parse Gemini's JSON answer into n normalized result dicts."""
    results = [{"index": i, "place": None, "city": None,
                "country": None, "confidence": 0} for i in range(1, n + 1)]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return results
    if not isinstance(data, list):
        return results
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (ValueError, TypeError):
            continue
        if not 1 <= idx <= n:
            continue
        place = item.get("place")
        results[idx - 1] = {
            "index": idx,
            "place": place.strip() if isinstance(place, str) and place.strip() else None,
            "city": (item.get("city") or "").strip() or None if isinstance(item.get("city"), str) else None,
            "country": (item.get("country") or "").strip() or None if isinstance(item.get("country"), str) else None,
            "confidence": max(0.0, min(1.0, float(item.get("confidence") or 0))),
        }
    return results


# ---------------------------------------------------------------------------
# Asset selection
# ---------------------------------------------------------------------------

def _to_datetime(value):
    """Normalize a date or datetime string to Immich's expected format.

    Immich's search validation rejects bare dates ("2026-03-01"); it wants
    a full datetime. Dates are interpreted as UTC midnight.
    """
    if value and "T" not in value:
        return value + "T00:00:00.000Z"
    return value


def window_assets(immich, after, before, not_found_ids):
    """Yield missing-GEO IMAGE assets in the [after, before) photo-date window
    whose asset_id is in not_found_ids."""
    page = 1
    after, before = _to_datetime(after), _to_datetime(before)
    while True:
        payload = {"page": page, "size": 250, "withExif": True, "type": "IMAGE"}
        if after:
            payload["takenAfter"] = after
        if before:
            payload["takenBefore"] = before
        data = http_json("POST", immich._url("/search/metadata"),
                         headers=immich.headers, payload=payload)
        assets = (data.get("assets") or {}).get("items") or []
        for a in assets:
            if a.get("type") != "IMAGE":
                continue
            exif = a.get("exifInfo") or {}
            if exif.get("latitude") is not None and exif.get("longitude") is not None:
                continue
            if a.get("id") in not_found_ids:
                yield a
        if not assets or (data.get("assets") or {}).get("nextPage") is None:
            return
        page += 1


def geocode_place(place, city, country, email):
    """Geocode an identified place via Nominatim, trying simpler queries first.

    Nominatim treats commas as address components, so a full
    "landmark, city, country" query often fails while parts of it resolve.
    Try progressively simpler forms; first hit wins. When the place name
    itself is comma-separated ("El Castillo, Chichen Itza"), try its
    segments in reverse order — the trailing parent site is usually the
    landmark Nominatim knows.
    """
    queries = []
    for candidate in (", ".join(p for p in (place, city, country) if p),
                      ", ".join(p for p in (place, country) if p),
                      place):
        if candidate and candidate not in queries:
            queries.append(candidate)
    segments = [s.strip() for s in place.split(",") if s.strip()]
    for segment in reversed(segments):
        for candidate in (segment, ", ".join(p for p in (segment, country) if p)):
            if candidate and candidate not in queries:
                queries.append(candidate)
    for query in queries:
        hit = nominatim_search(query, email)
        if hit:
            return hit
        time.sleep(1)  # Nominatim usage policy: max 1 req/sec
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", default=os.path.join(SCRIPT_DIR, "geotag-data", "state.csv"),
                        help="state.csv from geotag.py (source of not_found asset_ids)")
    parser.add_argument("--out", default=os.path.join(SCRIPT_DIR, "geotag-data", "gemini_candidates.csv"),
                        help="output candidates CSV (resume-safe)")
    parser.add_argument("--after", default=None, help="photo date window start, ISO (inclusive)")
    parser.add_argument("--before", default=None, help="photo date window end, ISO (exclusive)")
    parser.add_argument("--asset", action="append", default=[],
                        help="force specific asset_id (repeatable, for testing)")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="max images this run")
    parser.add_argument("--model", default="gemini-3.8-flash")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--retries", type=int, default=3,
                        help="Gemini 5xx retries per batch (default 3)")
    parser.add_argument("--backoff", type=int, default=20,
                        help="seconds between Gemini retries (default 20)")
    args = parser.parse_args()

    load_env()
    api_key = os.environ.get("GEMINI_API_KEY", "")
    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")
    email = os.environ.get("NOMINATIM_EMAIL")
    public_url = os.environ.get("IMMICH_PUBLIC_URL", immich_url).rstrip("/")
    missing = [k for k, v in [("GEMINI_API_KEY", api_key),
                              ("IMMICH_URL", immich_url),
                              ("IMMICH_API_KEY", immich_key)] if not v]
    if missing:
        sys.exit("Missing configuration: {} (set them in the environment or in "
                 "{}/.env - see .env.example)".format(", ".join(missing), SCRIPT_DIR))
    version = args.model.replace("gemini-", "").split("-")[0]
    try:
        model_ok = tuple(int(x) for x in version.split(".")[:2]) >= (3, 8)
    except ValueError:
        model_ok = False
    if not model_ok:
        sys.exit("Model policy: gemini-3.8 or newer only, got {}".format(args.model))

    immich = Immich(immich_url, immich_key)

    # Resume: skip asset_ids already present in the output CSV
    done = {r.get("asset_id") for r in read_csv(args.out)}

    # Target selection
    if args.asset:
        targets = []
        for aid in args.asset:
            a = immich.get_asset(aid)
            exif = a.get("exifInfo") or {}
            if a.get("type") == "IMAGE" and (exif.get("latitude") is None or exif.get("longitude") is None):
                targets.append(a)
            else:
                print("skip {}: not an IMAGE or already has GPS".format(aid))
    else:
        not_found_ids = {r.get("asset_id") for r in read_csv(args.state)
                         if r.get("status") == "not_found"}
        if not not_found_ids:
            sys.exit("No not_found rows in {}".format(args.state))
        targets = list(window_assets(immich, args.after, args.before, not_found_ids))

    targets = [a for a in targets if a.get("id") not in done]
    if args.limit:
        targets = targets[:args.limit]
    if not targets:
        print("nothing to do ({} already processed)".format(len(done)))
        return

    print("{} target images, batch size {}, model {}".format(
        len(targets), args.batch_size, args.model))

    rows = read_csv(args.out)
    geocode_cache = {}
    candidates = no_match = 0

    for start in range(0, len(targets), args.batch_size):
        batch = targets[start:start + args.batch_size]
        previews = []
        kept = []
        for a in batch:
            try:
                previews.append(immich.preview_jpeg(a["id"]))
                kept.append(a)
            except ApiError as e:
                print("{:40.40}  ERROR  {}".format(a.get("originalFileName", a["id"]), e))
        if not kept:
            continue
        try:
            results = gemini_identify(previews, api_key, args.model,
                                      retries=args.retries, backoff=args.backoff)
        except ApiError as e:
            msg = str(e)
            if "HTTP 429" in msg:
                print("Gemini quota reached, stopping cleanly (resume with same command): {}".format(msg))
                break
            print("Gemini error, stopping cleanly: {}".format(msg))
            break

        for a, res in zip(kept, results):
            row = {
                "asset_id": a["id"],
                "file_name": a.get("originalFileName", ""),
                "place": res["place"] or "",
                "city": res["city"] or "",
                "country": res["country"] or "",
                "confidence": "{:.2f}".format(res["confidence"]),
                "latitude": "", "longitude": "", "geocoded_name": "",
                "maps_url": "",
                "immich_url": "{}/photos/{}".format(public_url, a["id"]),
                "status": "no_match", "run_date": time.strftime("%Y-%m-%d"),
                "taken_at": a.get("fileCreatedAt", ""),
                "inferred_from": "",
            }
            if res["place"] and res["confidence"] >= args.min_confidence:
                cache_key = (res["place"], res["city"], res["country"])
                if cache_key not in geocode_cache:
                    geocode_cache[cache_key] = geocode_place(
                        res["place"], res["city"], res["country"], email)
                geo = geocode_cache[cache_key]
                if geo:
                    lat, lon, name = geo
                    row.update({
                        "latitude": str(lat), "longitude": str(lon),
                        "geocoded_name": name,
                        "maps_url": "https://www.google.com/maps?q={:.5f},{:.5f}".format(lat, lon),
                        "status": "candidate",
                    })
                    candidates += 1
                    print("{:40.40}  [gemini] {} ({:.2f}) -> {:.5f},{:.5f}".format(
                        row["file_name"], res["place"], res["confidence"], lat, lon))
                else:
                    print("{:40.40}  [gemini] {} identified but geocode failed".format(
                        row["file_name"], res["place"]))
            else:
                no_match += 1
            rows.append(row)
        write_csv(args.out, rows, CANDIDATE_COLUMNS)  # crash-safe: write after each batch

    print("-" * 78)
    print("done: {} candidates, {} no_match, {} processed of {} targets".format(
        candidates, no_match, candidates + no_match, len(targets)))
    print("output: {}".format(args.out))


if __name__ == "__main__":
    main()

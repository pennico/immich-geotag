#!/usr/bin/env python3
"""immich-geotag: fill in missing GPS coordinates in Immich.

Three subcommands for a CSV-based workflow:

  scan       list missing-GPS images from Immich      -> CSV rows (status=pending)
  recognize  run recognition on pending rows          -> fills coordinates (no writes)
  apply      write approved rows back to Immich       -> status=applied (idempotent)

Recognition backends, tried in order:
  1. Google Cloud Vision landmark detection (exact lat/lng, landmarks only)
  2. SerpApi Google Lens (visual matches, geocoded via Nominatim) - only
     enabled when SERPAPI_KEY is set

Configuration is read from environment variables and/or a .env file next to
this script (see .env.example):
  IMMICH_URL             e.g. https://immich.example.com  (required)
  IMMICH_API_KEY         Immich API key                  (required)
  GOOGLE_VISION_API_KEY  Google Cloud Vision API key     (required)
  SERPAPI_KEY            SerpApi key, enables Lens fallback (optional)
  NOMINATIM_EMAIL        contact email for Nominatim usage policy (optional)

Usage:
  python3 geotag.py scan --out geotag.csv [--limit N] [--asset-id UUID]
  python3 geotag.py recognize geotag.csv [--min-score 0.5] [--thumbs DIR]
  python3 geotag.py apply geotag.csv [--dry-run]
"""

import argparse
import base64
import csv
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LENS_MAX_UPLOAD = 500 * 1024  # SerpApi image upload limit

# CSV schema column names
CSV_COLUMNS = [
    "asset_id", "file_name", "latitude", "longitude",
    "place", "source", "score", "status", "maps_url",
]


class ApiError(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def http_request(method, url, headers=None, body=None, timeout=90):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except urllib.error.URLError as e:
        raise ApiError(f"network error: {e.reason}")


def http_json(method, url, headers=None, payload=None, timeout=90):
    headers = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    status, data = http_request(method, url, headers, body, timeout)
    if status >= 400:
        raise ApiError("{} {} -> HTTP {}: {}".format(
            method, url.split("?")[0], status, data[:300].decode("utf-8", "replace")))
    if not data:
        return {}
    return json.loads(data.decode("utf-8"))


def http_bytes(method, url, headers=None, timeout=120):
    status, data = http_request(method, url, headers, None, timeout)
    if status >= 400:
        raise ApiError("{} {} -> HTTP {}".format(method, url.split("?")[0], status))
    return data


def multipart_post(url, field, filename, content, extra_fields):
    boundary = "----immichgeotag" + uuid.uuid4().hex
    parts = []
    for name, value in extra_fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{filename}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n".encode())
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)
    status, data = http_request(
        "POST", url,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        body=body)
    if status >= 400:
        raise ApiError("POST {} -> HTTP {}: {}".format(
            url.split("?")[0], status, data[:300].decode("utf-8", "replace")))
    return json.loads(data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_env():
    """Load .env next to the script; real environment variables win."""
    path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# CSV layer
# ---------------------------------------------------------------------------

def read_csv(path):
    """Read a CSV file tolerantly. Returns list of dicts.

    Unknown columns are ignored. Missing optional columns are treated as empty.
    UTF-8.
    """
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def write_csv(path, rows, columns=None):
    """Write rows to CSV, preserving column order. UTF-8."""
    if columns is None:
        columns = CSV_COLUMNS
    # Ensure all column keys exist in each row
    for row in rows:
        for col in columns:
            if col not in row:
                row[col] = ""
    # Write preserving order
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def get_row_by_asset_id(rows, asset_id):
    """Return the first row with matching asset_id, or None."""
    for row in rows:
        if row.get("asset_id") == asset_id:
            return row
    return None


def asset_id_in_rows(rows, asset_id):
    """Check if asset_id exists in rows."""
    return get_row_by_asset_id(rows, asset_id) is not None


# ---------------------------------------------------------------------------
# Local thumbnail reading (--thumbs DIR)
# ---------------------------------------------------------------------------

def read_local_thumb(thumbs_dir, asset_id):
    """Read an asset's preview image from a local Immich thumbs directory.

    Candidate layouts, verified against the Immich server source
    (server/src/cores/storage.core.ts, tags v2.0.0 and v3.2.4; see PLAN.md
    build step 6), newest first:

      v3.x:  thumbs/<ownerId>/<id[0:2]>/<id[2:4]>/<id>_preview.<jpeg|webp>
      v2.x:  thumbs/<ownerId>/<id[0:2]>/<id[2:4]>/<id>-preview.<jpeg|webp>
      v1.x:  thumbs/<id[0:2]>/<id[2:4]>/<id>-preview.<jpeg|webp>

    The CSV does not carry the ownerId, so the owner folder is matched with
    a glob. The preview format is configurable on current servers, so both
    .jpeg and .webp are tried. Returns the file bytes, or None when no
    candidate exists.
    """
    if not thumbs_dir or not asset_id:
        return None
    shard1, shard2 = asset_id[0:2], asset_id[2:4]
    exact = [
        os.path.join(thumbs_dir, shard1, shard2,
                     f"{asset_id}_preview.jpeg"),
        os.path.join(thumbs_dir, shard1, shard2,
                     f"{asset_id}_preview.webp"),
        os.path.join(thumbs_dir, shard1, shard2,
                     f"{asset_id}-preview.jpeg"),
        os.path.join(thumbs_dir, shard1, shard2,
                     f"{asset_id}-preview.webp"),
        os.path.join(thumbs_dir, shard1, shard2, asset_id + ".jpeg"),
    ]
    patterns = [
        os.path.join(thumbs_dir, "*", shard1, shard2,
                     f"{asset_id}_preview.jpeg"),
        os.path.join(thumbs_dir, "*", shard1, shard2,
                     f"{asset_id}_preview.webp"),
        os.path.join(thumbs_dir, "*", shard1, shard2,
                     f"{asset_id}-preview.jpeg"),
        os.path.join(thumbs_dir, "*", shard1, shard2,
                     f"{asset_id}-preview.webp"),
    ]
    for path in exact:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read()
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)):
            with open(path, "rb") as f:
                return f.read()
    return None


# ---------------------------------------------------------------------------
# Immich client
# ---------------------------------------------------------------------------

class Immich:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.headers = {"x-api-key": api_key, "Accept": "application/json"}

    def _url(self, path):
        return self.base_url + "/api" + path

    def _search_metadata(self, page, size):
        """POST on current servers; fall back to GET for older releases."""
        payload = {"page": page, "size": size, "withExif": True, "type": "IMAGE"}
        try:
            return http_json("POST", self._url("/search/metadata"),
                             headers=self.headers, payload=payload)
        except ApiError as e:
            if "HTTP 404" in str(e) or "HTTP 405" in str(e):
                qs = urllib.parse.urlencode(
                    {"page": page, "size": size, "withExif": "true", "type": "IMAGE"})
                return http_json("GET", self._url("/search/metadata?" + qs),
                                 headers=self.headers)
            raise

    def assets_missing_geo(self, limit=None, asset_id=None):
        """Yield IMAGE assets whose exif has no latitude/longitude.

        If asset_id is provided, yield only that single asset (if missing geo).
        """
        if asset_id:
            a = self.get_asset(asset_id)
            exif = a.get("exifInfo") or {}
            if a.get("type") == "IMAGE" and (exif.get("latitude") is None or exif.get("longitude") is None):
                yield a
            return

        page, yielded = 1, 0
        while True:
            data = self._search_metadata(page, 1000)
            assets = (data.get("assets") or {}).get("items") or []
            for a in assets:
                if a.get("type") != "IMAGE":
                    continue
                exif = a.get("exifInfo") or {}
                if exif.get("latitude") is None or exif.get("longitude") is None:
                    yield a
                    yielded += 1
                    if limit and yielded >= limit:
                        return
            if not assets or (data.get("assets") or {}).get("nextPage") is None:
                return
            page += 1

    def get_asset(self, asset_id):
        return http_json("GET", self._url("/assets/" + asset_id), headers=self.headers)

    def preview_jpeg(self, asset_id, thumbs_dir=None):
        """Return the preview-size JPEG (1440px) used for recognition.

        If thumbs_dir is provided, read the local thumbnail file first
        (see read_local_thumb) and fall back to the API when it is missing.
        """
        if thumbs_dir:
            data = read_local_thumb(thumbs_dir, asset_id)
            if data is not None:
                return data

        url = self._url(f"/assets/{asset_id}/thumbnail?size=preview")
        try:
            return http_bytes("GET", url, headers=self.headers)
        except ApiError:
            return http_bytes("GET", self._url(f"/assets/{asset_id}/original"),
                              headers=self.headers)

    def set_location(self, asset_id, lat, lon):
        http_json("PUT", self._url("/assets/" + asset_id), headers=self.headers,
                  payload={"latitude": lat, "longitude": lon})


# ---------------------------------------------------------------------------
# Recognition backend 1: Google Cloud Vision landmark detection
# ---------------------------------------------------------------------------

def vision_landmark(image_bytes, api_key, min_score):
    """Return (name, score, lat, lon) for the best landmark, or None."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "requests": [{
            "image": {"content": b64},
            "features": [{"type": "LANDMARK_DETECTION", "maxResults": 5}],
        }]
    }
    url = "https://vision.googleapis.com/v1/images:annotate?key=" + urllib.parse.quote(api_key)
    data = http_json("POST", url, payload=payload)
    annotations = (data.get("responses") or [{}])[0].get("landmarkAnnotations") or []
    best = None
    for ann in annotations:
        score = ann.get("score", 0)
        if best is None or score > best[1]:
            loc = (ann.get("locations") or [{}])[0].get("latLng") or {}
            best = (ann.get("description", "?"), score,
                    loc.get("latitude"), loc.get("longitude"))
    if best and best[1] >= min_score and best[2] is not None and best[3] is not None:
        return best
    return None


# ---------------------------------------------------------------------------
# Geocoding (Nominatim)
# ---------------------------------------------------------------------------

def nominatim_search(query, email=None):
    """Return (lat, lon, display_name) for a free-text query, or None."""
    headers = {"User-Agent": "immich-geotag/1.0" + (" (" + email + ")" if email else "")}
    qs = urllib.parse.urlencode({"q": query, "format": "jsonv2", "limit": 1})
    results = http_json("GET", "https://nominatim.openstreetmap.org/search?" + qs,
                        headers=headers)
    if isinstance(results, list) and results:
        first = results[0]
        return float(first["lat"]), float(first["lon"]), first.get("display_name", "")
    return None


def clean_match_title(title):
    """Strip site-name suffixes Lens appends to match titles.

    'Eiffel Tower - Wikipedia' -> 'Eiffel Tower'
    """
    return re.split(r"\s+[-|]\s+", title.strip(), maxsplit=1)[0].strip()


# ---------------------------------------------------------------------------
# Recognition backend 2: SerpApi Google Lens
# ---------------------------------------------------------------------------

def lens_locate(image_bytes, serpapi_key, email=None, max_matches=3):
    """Search Lens by uploaded image, geocode the top visual matches.

    Returns (match_title, lat, lon, geocoded_name) or None.
    """
    if len(image_bytes) > LENS_MAX_UPLOAD:
        return None
    up = multipart_post("https://serpapi.com/image", "image", "image.jpg",
                        image_bytes, {"api_key": serpapi_key})
    image_id = up.get("image_id")
    if not image_id:
        raise ApiError(f"SerpApi upload returned no image_id: {up}")
    qs = urllib.parse.urlencode({
        "engine": "google_lens", "image_id": image_id,
        "type": "visual_matches", "hl": "en", "api_key": serpapi_key,
    })
    data = http_json("GET", "https://serpapi.com/search.json?" + qs)
    if data.get("error"):
        raise ApiError("SerpApi lens error: {}".format(data["error"]))
    matches = data.get("visual_matches") or []
    for m in matches[:max_matches]:
        query = clean_match_title(m.get("title", ""))
        if not query:
            continue
        hit = nominatim_search(query, email)
        if hit:
            return query, hit[0], hit[1], hit[2]
        time.sleep(1.1)  # Nominatim usage policy: max 1 request/second
    return None


# ---------------------------------------------------------------------------
# Subcommand: scan
# ---------------------------------------------------------------------------

def cmd_scan(args):
    """List IMAGE assets whose exif has no latitude/longitude -> CSV rows."""
    load_env()
    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")

    missing = [k for k, v in [("IMMICH_URL", immich_url),
                              ("IMMICH_API_KEY", immich_key)] if not v]
    if missing:
        sys.exit("Missing configuration: {} (set them in the environment or in "
                 "{}/.env - see .env.example)".format(", ".join(missing), SCRIPT_DIR))

    immich = Immich(immich_url, immich_key)

    out_path = args.out or "geotag.csv"

    # Read existing CSV to avoid duplicates
    existing_rows = read_csv(out_path)

    new_rows = []
    for asset in immich.assets_missing_geo(limit=args.limit, asset_id=args.asset_id):
        aid = asset["id"]
        # Skip if already in CSV
        if asset_id_in_rows(existing_rows, aid):
            continue
        row = {
            "asset_id": aid,
            "file_name": asset.get("originalFileName", aid),
            "latitude": "",
            "longitude": "",
            "place": "",
            "source": "",
            "score": "",
            "status": "pending",
            "maps_url": "",
        }
        new_rows.append(row)

    # Append new rows to existing
    all_rows = existing_rows + new_rows

    if not new_rows:
        print("No new missing-GPS assets found.")
    else:
        write_csv(out_path, all_rows)
        print(f"Wrote {len(new_rows)} new rows to {out_path} (total: {len(all_rows)})")


# ---------------------------------------------------------------------------
# Subcommand: recognize
# ---------------------------------------------------------------------------

def cmd_recognize(args):
    """Run recognition on pending rows, fill coordinates. Never writes to Immich."""
    load_env()
    vision_key = os.environ.get("GOOGLE_VISION_API_KEY", "")
    serpapi_key = os.environ.get("SERPAPI_KEY", "")
    email = os.environ.get("NOMINATIM_EMAIL")

    missing = [k for k, v in [("GOOGLE_VISION_API_KEY", vision_key)] if not v]
    if missing:
        sys.exit("Missing configuration: {} (set them in the environment or in "
                 "{}/.env - see .env.example)".format(", ".join(missing), SCRIPT_DIR))

    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")
    immich = Immich(immich_url, immich_key) if immich_url and immich_key else None

    csv_path = args.csv
    if not os.path.exists(csv_path):
        sys.exit(f"CSV file not found: {csv_path}")

    rows = read_csv(csv_path)
    located = not_found = errors = skipped = 0

    for row in rows:
        asset_id = row.get("asset_id", "")
        status = row.get("status", "")
        lat = row.get("latitude", "")
        lon = row.get("longitude", "")

        # Skip rows that already have coordinates
        if lat and lon:
            skipped += 1
            continue
        # Only process pending rows
        if status != "pending":
            skipped += 1
            continue

        # Get the preview image
        preview = None
        if immich:
            try:
                preview = immich.preview_jpeg(asset_id, thumbs_dir=args.thumbs)
            except ApiError as e:
                print("{:40.40}  ERROR  {}".format(row.get("file_name", asset_id), e))
                errors += 1
                continue
        else:
            # No Immich config: --thumbs is the only image source
            if args.thumbs:
                preview = read_local_thumb(args.thumbs, asset_id)

        if preview is None:
            print("{:40.40}  ERROR  cannot fetch preview image".format(row.get("file_name", asset_id)))
            errors += 1
            continue

        # Try Vision first
        try:
            hit = vision_landmark(preview, vision_key, args.min_score)
        except ApiError as e:
            print("{:40.40}  ERROR  {}".format(row.get("file_name", asset_id), e))
            errors += 1
            continue
        if hit:
            name, score, lat_val, lon_val = hit
            row["latitude"] = str(lat_val) if lat_val is not None else ""
            row["longitude"] = str(lon_val) if lon_val is not None else ""
            row["place"] = name
            row["source"] = "vision"
            row["score"] = str(score) if score is not None else ""
            row["status"] = "located"
            row["maps_url"] = f"https://www.google.com/maps?q={lat_val:.5f},{lon_val:.5f}" if lat_val is not None and lon_val is not None else ""
            write_csv(csv_path, rows)  # crash-safe: write after each row
            located += 1
            print("{:40.40}  [vision] {} score={:.2f} -> {:.5f},{:.5f}".format(
                row.get("file_name", asset_id), name, score, lat_val, lon_val))
            continue

        # Try Lens fallback
        if serpapi_key:
            try:
                hit = lens_locate(preview, serpapi_key, email, args.max_lens_matches)
            except ApiError as e:
                print("{:40.40}  ERROR  {}".format(row.get("file_name", asset_id), e))
                errors += 1
                continue
            if hit:
                name, lat_val, lon_val, detail = hit
                row["latitude"] = str(lat_val) if lat_val is not None else ""
                row["longitude"] = str(lon_val) if lon_val is not None else ""
                row["place"] = name
                row["source"] = "lens"
                row["score"] = ""
                row["status"] = "located"
                row["maps_url"] = f"https://www.google.com/maps?q={lat_val:.5f},{lon_val:.5f}" if lat_val is not None and lon_val is not None else ""
                write_csv(csv_path, rows)  # crash-safe: write after each row
                located += 1
                print("{:40.40}  [lens] {} -> {:.5f},{:.5f}".format(
                    row.get("file_name", asset_id), name, lat_val, lon_val))
                print(f"    geocoded as: {detail}")
                continue

        # No match found
        row["status"] = "not_found"
        write_csv(csv_path, rows)  # crash-safe: write after each row
        not_found += 1
        print("{:40.40}  no location found".format(row.get("file_name", asset_id)))

    print("-" * 78)
    print(f"done: {located} located, {not_found} not_found, {errors} errors, {skipped} skipped")


# ---------------------------------------------------------------------------
# Subcommand: apply
# ---------------------------------------------------------------------------

def cmd_apply(args):
    """Write approved rows back to Immich. Idempotent; --dry-run prints only."""
    load_env()
    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")

    missing = [k for k, v in [("IMMICH_URL", immich_url),
                              ("IMMICH_API_KEY", immich_key)] if not v]
    if missing:
        sys.exit("Missing configuration: {} (set them in the environment or in "
                 "{}/.env - see .env.example)".format(", ".join(missing), SCRIPT_DIR))

    immich = Immich(immich_url, immich_key)

    csv_path = args.csv
    if not os.path.exists(csv_path):
        sys.exit(f"CSV file not found: {csv_path}")

    rows = read_csv(csv_path)
    dry_run = args.dry_run
    written = errors = skipped = 0

    for row in rows:
        asset_id = row.get("asset_id", "")
        status = row.get("status", "")
        lat_str = row.get("latitude", "")
        lon_str = row.get("longitude", "")

        # Skip rows without coordinates
        if not lat_str or not lon_str:
            skipped += 1
            continue

        # Skip already applied rows
        if status == "applied":
            skipped += 1
            continue

        try:
            lat = float(lat_str)
            lon = float(lon_str)
        except ValueError:
            print("{:40.40}  ERROR  invalid coordinates: {},{}".format(
                row.get("file_name", asset_id), lat_str, lon_str))
            errors += 1
            continue

        if dry_run:
            maps = row.get("maps_url", "")
            source = row.get("source", "")
            place = row.get("place", "")
            print("WOULD WRITE: {:40.40}  [{}] {} -> {:.5f},{:.5f} {}".format(
                row.get("file_name", asset_id), source, place, lat, lon, maps))
            written += 1
        else:
            try:
                immich.set_location(asset_id, lat, lon)
                row["status"] = "applied"
                write_csv(csv_path, rows)  # crash-safe: persist after each write
                print("WRITTEN:   {:40.40}  -> {:.5f},{:.5f}".format(
                    row.get("file_name", asset_id), lat, lon))
                written += 1
            except ApiError as e:
                print("FAILED:    {:40.40}  {}".format(
                    row.get("file_name", asset_id), e))
                errors += 1

    print("-" * 78)
    if dry_run:
        print(f"dry run: {written} would be written, {errors} errors, {skipped} skipped")
        print("(re-run without --dry-run to write to Immich)")
    else:
        print(f"done: {written} written, {errors} errors, {skipped} skipped")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fill in missing GPS coordinates in Immich.")
    subparsers = parser.add_subparsers(dest="command", help="subcommand")

    # scan subcommand
    scan_parser = subparsers.add_parser("scan", help="List missing-GPS assets")
    scan_parser.add_argument("--out", default=None,
                             help="output CSV path (default: geotag.csv)")
    scan_parser.add_argument("--limit", type=int, default=None,
                             help="process at most N images")
    scan_parser.add_argument("--asset-id", default=None,
                             help="process a single asset by id")

    # recognize subcommand
    rec_parser = subparsers.add_parser("recognize", help="Run recognition on CSV rows")
    rec_parser.add_argument("csv", help="CSV file path")
    rec_parser.add_argument("--min-score", type=float, default=0.5,
                             help="minimum Cloud Vision landmark score (default 0.5)")
    rec_parser.add_argument("--max-lens-matches", type=int, default=3,
                             help="Lens matches to try geocoding (default 3)")
    rec_parser.add_argument("--thumbs", default=None,
                             help="local Immich thumbs/ directory: read preview "
                                  "images from disk instead of the API (for runs "
                                  "on the server itself)")

    # apply subcommand
    app_parser = subparsers.add_parser("apply", help="Write coordinates to Immich")
    app_parser.add_argument("csv", help="CSV file path")
    app_parser.add_argument("--dry-run", action="store_true",
                            help="print what would be written without changing anything")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "recognize":
        cmd_recognize(args)
    elif args.command == "apply":
        cmd_apply(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()

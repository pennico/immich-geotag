#!/usr/bin/env python3
"""immich-geotag: fill in missing GPS coordinates in Immich.

Finds images in your Immich library that have no GPS data, tries to
recognize where they were taken, and (with --apply) writes the coordinates
back to the asset.

Recognition backends, tried in order:
  1. Google Cloud Vision landmark detection (exact lat/lng, landmarks only)
  2. SerpApi Google Lens (visual matches, geocoded via Nominatim) - only
     enabled when SERPAPI_KEY is set

By default the tool runs in dry-run mode: it only prints the proposed
coordinates. Use --apply to actually write them to Immich.

Usage:
  python3 geotag.py                          # dry run over all images
  python3 geotag.py --limit 5                # dry run, first 5
  python3 geotag.py --asset-id <uuid>        # dry run, single asset
  python3 geotag.py --apply                  # write coordinates
  python3 geotag.py --apply --min-score 0.8  # only high-confidence landmarks

Configuration is read from environment variables and/or a .env file next to
this script (see .env.example):
  IMMICH_URL             e.g. https://immich.example.com  (required)
  IMMICH_API_KEY         Immich API key                  (required)
  GOOGLE_VISION_API_KEY  Google Cloud Vision API key     (required)
  SERPAPI_KEY            SerpApi key, enables Lens fallback (optional)
  NOMINATIM_EMAIL        contact email for Nominatim usage policy (optional)
"""

import argparse
import base64
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
        raise ApiError("network error: {}".format(e.reason))


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
            "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(
                boundary, name, value).encode("utf-8"))
    parts.append(
        "--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n".format(
            boundary, field, filename).encode("utf-8"))
    parts.append(content)
    parts.append("\r\n--{}--\r\n".format(boundary).encode("utf-8"))
    body = b"".join(parts)
    status, data = http_request(
        "POST", url,
        headers={"Content-Type": "multipart/form-data; boundary={}".format(boundary)},
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
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


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

    def assets_missing_geo(self, limit=None):
        """Yield IMAGE assets whose exif has no latitude/longitude."""
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

    def preview_jpeg(self, asset_id):
        """Return the preview-size JPEG (1440px) used for recognition."""
        url = self._url("/assets/{}/thumbnail?size=preview".format(asset_id))
        try:
            return http_bytes("GET", url, headers=self.headers)
        except ApiError:
            return http_bytes("GET", self._url("/assets/{}/original".format(asset_id)),
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
        raise ApiError("SerpApi upload returned no image_id: {}".format(up))
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
# Main
# ---------------------------------------------------------------------------

def process_asset(asset, preview, vision_key, serpapi_key, email, min_score,
                  max_lens_matches):
    """Return a result dict for one asset."""
    result = {
        "asset": asset,
        "backend": None, "name": None, "score": None,
        "lat": None, "lon": None, "detail": None, "error": None,
    }
    try:
        hit = vision_landmark(preview, vision_key, min_score)
        if hit:
            result.update(backend="vision", name=hit[0], score=hit[1],
                          lat=hit[2], lon=hit[3])
            return result
        if serpapi_key:
            hit = lens_locate(preview, serpapi_key, email, max_lens_matches)
            if hit:
                result.update(backend="lens", name=hit[0], lat=hit[1], lon=hit[2],
                             detail=hit[3])
        return result
    except ApiError as e:
        result["error"] = str(e)
        return result


def main():
    parser = argparse.ArgumentParser(
        description="Fill in missing GPS coordinates in Immich "
                    "(dry-run by default).")
    parser.add_argument("--apply", action="store_true",
                        help="write coordinates to Immich (default: dry run)")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N images")
    parser.add_argument("--asset-id", default=None,
                        help="process a single asset by id")
    parser.add_argument("--min-score", type=float, default=0.5,
                        help="minimum Cloud Vision landmark score (default 0.5)")
    parser.add_argument("--max-lens-matches", type=int, default=3,
                        help="Lens matches to try geocoding (default 3)")
    args = parser.parse_args()

    load_env()
    immich_url = os.environ.get("IMMICH_URL", "")
    immich_key = os.environ.get("IMMICH_API_KEY", "")
    vision_key = os.environ.get("GOOGLE_VISION_API_KEY", "")
    serpapi_key = os.environ.get("SERPAPI_KEY", "")
    email = os.environ.get("NOMINATIM_EMAIL")

    missing = [k for k, v in [("IMMICH_URL", immich_url),
                              ("IMMICH_API_KEY", immich_key),
                              ("GOOGLE_VISION_API_KEY", vision_key)] if not v]
    if missing:
        sys.exit("Missing configuration: {} (set them in the environment or in "
                 "{}/.env - see .env.example)".format(", ".join(missing), SCRIPT_DIR))

    immich = Immich(immich_url, immich_key)

    if args.asset_id:
        assets = [immich.get_asset(args.asset_id)]
    else:
        assets = immich.assets_missing_geo(limit=args.limit)

    mode = "APPLY" if args.apply else "DRY RUN"
    print("immich-geotag [{}]  (vision{}{})".format(
        mode, ", lens fallback" if serpapi_key else " only",
        ", min_score={}".format(args.min_score)))
    print("-" * 78)

    found = applied = errors = 0
    for asset in assets:
        aid = asset["id"]
        name = asset.get("originalFileName", aid)
        try:
            preview = immich.preview_jpeg(aid)
        except ApiError as e:
            print("{:40.40}  ERROR  {}".format(name, e))
            errors += 1
            continue

        r = process_asset(asset, preview, vision_key, serpapi_key, email,
                          args.min_score, args.max_lens_matches)
        if r["error"]:
            print("{:40.40}  ERROR  {}".format(name, r["error"]))
            errors += 1
            continue

        if r["lat"] is None:
            print("{:40.40}  no location found".format(name))
            continue

        found += 1
        score = " score={:.2f}".format(r["score"]) if r["score"] is not None else ""
        maps = "https://www.google.com/maps?q={:.5f},{:.5f}".format(r["lat"], r["lon"])
        print("{:40.40}  [{}] {}{} -> {:.5f},{:.5f}".format(
            name, r["backend"], r["name"] or "", score, r["lat"], r["lon"]))
        if r["detail"]:
            print("    geocoded as: {}".format(r["detail"]))
        print("    check: {}".format(maps))

        if args.apply:
            try:
                immich.set_location(aid, r["lat"], r["lon"])
                applied += 1
                print("    written to Immich")
            except ApiError as e:
                print("    FAILED to write: {}".format(e))
                errors += 1

    print("-" * 78)
    print("done: {} located, {} written, {} errors{}".format(
        found, applied, errors,
        "" if args.apply else "  (dry run - nothing was written; use --apply)"))


if __name__ == "__main__":
    main()

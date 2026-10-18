#!/usr/bin/env python3
"""immich-geotag: fake-payload test suite.

No live API calls. All tests use mocked HTTP responses and fake payloads.
Tests cover: CSV layer, scan, recognize, apply, resume-skip, idempotency,
duplicate asset_id guard.

Run: python3 test_geotag.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# Add the repo root to path so we can import geotag
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gemini_apply
import gemini_audit
import gemini_candidates

# Import functions from geotag.py
from geotag import (
    CSV_COLUMNS,
    ApiError,
    Immich,
    asset_id_in_rows,
    clean_match_title,
    get_row_by_asset_id,
    lens_locate,
    load_env,
    multipart_post,
    nominatim_search,
    read_csv,
    read_local_thumb,
    vision_landmark,
    write_csv,
)

# ---------------------------------------------------------------------------
# Fake payloads
# ---------------------------------------------------------------------------

FAKE_ASSET_MISSING_GEO = {
    "id": "test-asset-001",
    "type": "IMAGE",
    "originalFileName": "IMG_0001.jpg",
    "exifInfo": {"latitude": None, "longitude": None},
}

FAKE_ASSET_WITH_GEO = {
    "id": "test-asset-002",
    "type": "IMAGE",
    "originalFileName": "IMG_0002.jpg",
    "exifInfo": {"latitude": 48.85837, "longitude": 2.294481},
}

FAKE_ASSET_NO_EXIF = {
    "id": "test-asset-003",
    "type": "IMAGE",
    "originalFileName": "IMG_0003.jpg",
}

FAKE_SEARCH_RESPONSE = {
    "assets": {
        "items": [FAKE_ASSET_MISSING_GEO, FAKE_ASSET_WITH_GEO, FAKE_ASSET_NO_EXIF],
        "nextPage": None,
        "total": 3,
    }
}

FAKE_VISION_RESPONSE_LANDMARK = {
    "responses": [{
        "landmarkAnnotations": [{
            "description": "Eiffel Tower",
            "score": 0.95,
            "locations": [{
                "latLng": {
                    "latitude": 48.85837,
                    "longitude": 2.294481,
                }
            }],
        }]
    }]
}

FAKE_VISION_RESPONSE_NO_MATCH = {
    "responses": [{
        "landmarkAnnotations": []
    }]
}

FAKE_VISION_RESPONSE_LOW_SCORE = {
    "responses": [{
        "landmarkAnnotations": [{
            "description": "Some Place",
            "score": 0.4,
            "locations": [{
                "latLng": {
                    "latitude": 1.0,
                    "longitude": 2.0,
                }
            }],
        }]
    }]
}

FAKE_SERPAPI_UPLOAD = {
    "image_id": "test-image-id-123"
}

FAKE_SERPAPI_LENS_RESPONSE = {
    "visual_matches": [
        {"title": "Eiffel Tower - Wikipedia"},
        {"title": "Tour Eiffel - Paris Info"},
        {"title": "Some Other Thing"},
    ]
}

FAKE_NOMINATIM_EIFFEL = [
    {
        "lat": "48.85837",
        "lon": "2.294481",
        "display_name": "Eiffel Tower, Paris, France",
    }
]

FAKE_NOMINATIM_NOT_FOUND = []

FAKE_PREVIEW_JPEG = b"fake jpeg content"


# ---------------------------------------------------------------------------
# Test CSV layer
# ---------------------------------------------------------------------------

class TestCSVLayer(unittest.TestCase):
    """CSV read/write, round-trip, order preservation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_read_csv_empty_file(self):
        path = os.path.join(self.tmpdir, "empty.csv")
        with open(path, "w"):
            pass
        rows = read_csv(path)
        self.assertEqual(rows, [])

    def test_read_csv_missing_file(self):
        rows = read_csv("/nonexistent/path.csv")
        self.assertEqual(rows, [])

    def test_read_csv_basic(self):
        path = os.path.join(self.tmpdir, "basic.csv")
        with open(path, "w", newline="") as f:
            f.write("asset_id,file_name,latitude,longitude,place,source,score,status,maps_url\n")
            f.write("a1,IMG_001.jpg,48.85837,2.294481,Eiffel Tower,vision,0.95,located,https://...\n")
            f.write("a2,IMG_002.jpg,,,," "," ",pending,\n")
        rows = read_csv(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["asset_id"], "a1")
        self.assertEqual(rows[0]["latitude"], "48.85837")
        self.assertEqual(rows[1]["status"], "pending")

    def test_write_csv_basic(self):
        path = os.path.join(self.tmpdir, "out.csv")
        rows = [
            {"asset_id": "a1", "file_name": "IMG_001.jpg", "latitude": "48.85837",
             "longitude": "2.294481", "place": "Eiffel Tower", "source": "vision",
             "score": "0.95", "status": "located", "maps_url": "https://maps..."},
        ]
        write_csv(path, rows)
        with open(path, newline="") as f:
            content = f.read()
        self.assertIn("asset_id,file_name,latitude", content)
        self.assertIn("a1,IMG_001.jpg,48.85837", content)

    def test_csv_round_trip(self):
        path = os.path.join(self.tmpdir, "roundtrip.csv")
        original = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "1.0",
             "longitude": "2.0", "place": "Place 1", "source": "vision",
             "score": "0.9", "status": "located", "maps_url": "url1"},
            {"asset_id": "a2", "file_name": "f2.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(path, original)
        read_back = read_csv(path)
        self.assertEqual(len(read_back), 2)
        self.assertEqual(read_back[0]["asset_id"], "a1")
        self.assertEqual(read_back[0]["latitude"], "1.0")
        self.assertEqual(read_back[1]["status"], "pending")

    def test_csv_order_preserved(self):
        path = os.path.join(self.tmpdir, "order.csv")
        rows = [
            {"asset_id": "a", "file_name": "a.jpg", "latitude": "", "longitude": "",
             "place": "", "source": "", "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(path, rows)
        with open(path, newline="") as f:
            header = f.readline().strip()
        columns = header.split(",")
        self.assertEqual(columns, CSV_COLUMNS)

    def test_csv_utf8(self):
        path = os.path.join(self.tmpdir, "utf8.csv")
        rows = [
            {"asset_id": "a1", "file_name": "Törö.jpg", "latitude": "1.0",
             "longitude": "2.0", "place": "Paris", "source": "vision",
             "score": "0.9", "status": "located", "maps_url": ""},
        ]
        write_csv(path, rows)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Törö.jpg", content)

    def test_asset_id_in_rows(self):
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg"},
            {"asset_id": "a2", "file_name": "f2.jpg"},
        ]
        self.assertTrue(asset_id_in_rows(rows, "a1"))
        self.assertTrue(asset_id_in_rows(rows, "a2"))
        self.assertFalse(asset_id_in_rows(rows, "a3"))

    def test_get_row_by_asset_id(self):
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg"},
            {"asset_id": "a2", "file_name": "f2.jpg"},
        ]
        row = get_row_by_asset_id(rows, "a1")
        self.assertEqual(row["file_name"], "f1.jpg")
        row = get_row_by_asset_id(rows, "nonexistent")
        self.assertIsNone(row)


# ---------------------------------------------------------------------------
# Test helper functions
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_clean_match_title_wikipedia(self):
        self.assertEqual(clean_match_title("Eiffel Tower - Wikipedia"), "Eiffel Tower")

    def test_clean_match_title_pipe(self):
        self.assertEqual(clean_match_title("Eiffel Tower | Paris Info"), "Eiffel Tower")

    def test_clean_match_title_no_separator(self):
        self.assertEqual(clean_match_title("Eiffel Tower"), "Eiffel Tower")

    def test_clean_match_title_extra_spaces(self):
        self.assertEqual(clean_match_title("  Eiffel Tower - Wikipedia  "), "Eiffel Tower")


class TestLoadEnv(unittest.TestCase):
    """load_env reads SCRIPT_DIR/.env; real environment variables win."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_env(self, text):
        with open(os.path.join(self.tmpdir, ".env"), "w", encoding="utf-8") as f:
            f.write(text)

    def test_load_env_sets_keys_from_file(self):
        self._write_env("GEOTAG_TEST_A=one\nGEOTAG_TEST_B=two\n")
        with mock.patch("geotag.SCRIPT_DIR", self.tmpdir):
            with mock.patch.dict(os.environ, {}, clear=False):
                load_env()
                self.assertEqual(os.environ["GEOTAG_TEST_A"], "one")
                self.assertEqual(os.environ["GEOTAG_TEST_B"], "two")

    def test_load_env_real_env_wins(self):
        self._write_env("GEOTAG_TEST_A=from-file\n")
        with mock.patch("geotag.SCRIPT_DIR", self.tmpdir):
            with mock.patch.dict(os.environ, {"GEOTAG_TEST_A": "from-env"},
                                 clear=False):
                load_env()
                self.assertEqual(os.environ["GEOTAG_TEST_A"], "from-env")

    def test_load_env_skips_comments_blanks_strips_quotes(self):
        self._write_env(
            "# a comment\n"
            "\n"
            "GEOTAG_TEST_A='quoted'\n"
            "GEOTAG_TEST_B=\"double\"\n"
            "not-an-assignment\n"
            "GEOTAG_TEST_C=plain\n"
        )
        with mock.patch("geotag.SCRIPT_DIR", self.tmpdir):
            with mock.patch.dict(os.environ, {}, clear=False):
                load_env()
                self.assertEqual(os.environ["GEOTAG_TEST_A"], "quoted")
                self.assertEqual(os.environ["GEOTAG_TEST_B"], "double")
                self.assertEqual(os.environ["GEOTAG_TEST_C"], "plain")

    def test_load_env_missing_file_is_noop(self):
        # No .env in SCRIPT_DIR: load_env must not raise.
        with mock.patch("geotag.SCRIPT_DIR", self.tmpdir):
            with mock.patch.dict(os.environ, {}, clear=False):
                load_env()


class TestMultipartPost(unittest.TestCase):
    """multipart_post builds a valid multipart body (SerpApi upload)."""

    @mock.patch("geotag.http_request")
    def test_multipart_body_structure(self, mock_request):
        mock_request.return_value = (200, b'{"image_id": "abc"}')

        result = multipart_post(
            "https://serpapi.com/image", "image", "preview.jpg",
            b"jpeg-bytes", {"api_key": "k1"})

        self.assertEqual(result, {"image_id": "abc"})
        self.assertEqual(mock_request.call_count, 1)
        self.assertEqual(mock_request.call_args[0][0], "POST")
        self.assertEqual(mock_request.call_args[0][1], "https://serpapi.com/image")

        headers = mock_request.call_args[1]["headers"]
        body = mock_request.call_args[1]["body"]

        # Boundary is consistent between the header and the body
        self.assertIn("multipart/form-data; boundary=", headers["Content-Type"])
        boundary = headers["Content-Type"].split("boundary=")[1]
        self.assertIn(boundary.encode("utf-8"), body)

        # Extra fields are sent as form parts
        self.assertIn(b'name="api_key"', body)
        self.assertIn(b"\r\n\r\nk1\r\n", body)

        # File part: field name, filename, content type, then the bytes
        self.assertIn(b'name="image"; filename="preview.jpg"', body)
        self.assertIn(b"Content-Type: application/octet-stream", body)
        self.assertIn(b"jpeg-bytes", body)

        # Terminating boundary
        self.assertTrue(body.endswith(
            f"--{boundary}--\r\n".encode()))

    @mock.patch("geotag.http_request")
    def test_multipart_post_http_error_raises(self, mock_request):
        mock_request.return_value = (500, b"server error")
        with self.assertRaises(ApiError):
            multipart_post(
                "https://serpapi.com/image", "image", "preview.jpg",
                b"jpeg-bytes", {"api_key": "k1"})


# ---------------------------------------------------------------------------
# Test Immich client (with mocked HTTP)
# ---------------------------------------------------------------------------

class TestImmichClient(unittest.TestCase):

    def setUp(self):
        self.immich = Immich("https://immich.example.com", "test-api-key")

    def test_url_construction(self):
        self.assertEqual(
            self.immich._url("/assets/abc"),
            "https://immich.example.com/api/assets/abc"
        )

    @mock.patch("geotag.http_json")
    def test_search_metadata_post(self, mock_http_json):
        mock_http_json.return_value = FAKE_SEARCH_RESPONSE
        result = self.immich._search_metadata(1, 1000)
        self.assertEqual(result, FAKE_SEARCH_RESPONSE)
        mock_http_json.assert_called_once()
        call_args = mock_http_json.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertIn("search/metadata", call_args[0][1])

    @mock.patch("geotag.http_json")
    def test_search_metadata_fallback_get(self, mock_http_json):
        mock_http_json.side_effect = [
            ApiError("HTTP 404"),
            FAKE_SEARCH_RESPONSE,
        ]
        result = self.immich._search_metadata(1, 1000)
        self.assertEqual(result, FAKE_SEARCH_RESPONSE)
        self.assertEqual(mock_http_json.call_count, 2)

    @mock.patch("geotag.http_json")
    def test_assets_missing_geo(self, mock_http_json):
        mock_http_json.return_value = FAKE_SEARCH_RESPONSE
        assets = list(self.immich.assets_missing_geo())
        # FAKE_SEARCH_RESPONSE has 3 assets: one missing geo, one with geo, one no exif
        self.assertEqual(len(assets), 2)  # missing_geo and no_exif

    @mock.patch("geotag.http_json")
    def test_assets_missing_geo_with_limit(self, mock_http_json):
        mock_http_json.return_value = FAKE_SEARCH_RESPONSE
        assets = list(self.immich.assets_missing_geo(limit=1))
        self.assertEqual(len(assets), 1)

    @mock.patch("geotag.http_json")
    def test_assets_missing_geo_single_asset(self, mock_http_json):
        mock_http_json.return_value = FAKE_ASSET_MISSING_GEO
        assets = list(self.immich.assets_missing_geo(asset_id="test-asset-001"))
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["id"], "test-asset-001")

    @mock.patch("geotag.http_json")
    def test_assets_missing_geo_single_asset_with_geo(self, mock_http_json):
        mock_http_json.return_value = FAKE_ASSET_WITH_GEO
        assets = list(self.immich.assets_missing_geo(asset_id="test-asset-002"))
        self.assertEqual(len(assets), 0)  # has geo, so skipped

    @mock.patch("geotag.http_bytes")
    def test_preview_jpeg(self, mock_http_bytes):
        mock_http_bytes.return_value = FAKE_PREVIEW_JPEG
        result = self.immich.preview_jpeg("test-asset-id")
        self.assertEqual(result, FAKE_PREVIEW_JPEG)

    @mock.patch("geotag.http_bytes")
    def test_preview_jpeg_fallback_to_original(self, mock_http_bytes):
        mock_http_bytes.side_effect = [
            ApiError("HTTP 404"),
            FAKE_PREVIEW_JPEG,
        ]
        result = self.immich.preview_jpeg("test-asset-id")
        self.assertEqual(result, FAKE_PREVIEW_JPEG)
        self.assertEqual(mock_http_bytes.call_count, 2)

    @mock.patch("geotag.http_json")
    def test_set_location(self, mock_http_json):
        self.immich.set_location("test-asset-id", 48.85837, 2.294481)
        mock_http_json.assert_called_once()
        call_args = mock_http_json.call_args
        self.assertEqual(call_args[0][0], "PUT")
        self.assertIn("assets/test-asset-id", call_args[0][1])
        self.assertEqual(call_args[1]["payload"]["latitude"], 48.85837)
        self.assertEqual(call_args[1]["payload"]["longitude"], 2.294481)

    @mock.patch("geotag.http_bytes")
    def test_preview_jpeg_thumbs_dir(self, mock_http_bytes):
        mock_http_bytes.return_value = FAKE_PREVIEW_JPEG
        tmpdir = tempfile.mkdtemp()
        try:
            # Create thumb directory structure matching geotag.py logic
            # asset_id = "test-asset-001", id_prefix = "testasset001"
            # thumb path: thumbs/te/st/test-asset-001.jpeg
            thumb_dir = os.path.join(tmpdir, "thumbs")
            sub_dir = os.path.join(thumb_dir, "te", "st")
            os.makedirs(sub_dir, exist_ok=True)
            # filename uses asset_id WITH dashes
            thumb_path = os.path.join(sub_dir, "test-asset-001.jpeg")
            with open(thumb_path, "wb") as f:
                f.write(FAKE_PREVIEW_JPEG)

            immich = Immich("https://immich.example.com", "test-api-key")
            result = immich.preview_jpeg("test-asset-001", thumbs_dir=thumb_dir)
            self.assertEqual(result, FAKE_PREVIEW_JPEG)
            # http_bytes should not be called since we found the thumb locally
            mock_http_bytes.assert_not_called()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @mock.patch("geotag.http_bytes")
    def test_preview_jpeg_thumbs_dir_missing_falls_back_to_http(self, mock_http_bytes):
        """A missing local thumb falls back to the API preview download."""
        mock_http_bytes.return_value = FAKE_PREVIEW_JPEG
        tmpdir = tempfile.mkdtemp()
        try:
            immich = Immich("https://immich.example.com", "test-api-key")
            result = immich.preview_jpeg(
                "test-asset-001", thumbs_dir=os.path.join(tmpdir, "thumbs"))
            self.assertEqual(result, FAKE_PREVIEW_JPEG)
            mock_http_bytes.assert_called_once()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Test --thumbs DIR local-read mode (layouts verified against Immich source)
# ---------------------------------------------------------------------------

class TestReadLocalThumb(unittest.TestCase):
    """read_local_thumb finds the preview file across Immich versions.

    Layouts verified against server/src/cores/storage.core.ts at tags
    v2.0.0 and v3.2.4 (see PLAN.md build step 6):
      v3.x:  thumbs/<ownerId>/<id[0:2]>/<id[2:4]>/<id>_preview.<jpeg|webp>
      v2.x:  thumbs/<ownerId>/<id[0:2]>/<id[2:4]>/<id>-preview.<jpeg|webp>
      v1.x:  thumbs/<id[0:2]>/<id[2:4]>/<id>-preview.<jpeg|webp>
    """

    ASSET_ID = "test-asset-001"  # shard dirs: "te" / "st"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.thumbs_dir = os.path.join(self.tmpdir, "thumbs")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_thumb(self, relpath, content=FAKE_PREVIEW_JPEG):
        path = os.path.join(self.thumbs_dir, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return path

    def test_v3_layout_with_owner_folder(self):
        self._write_thumb(os.path.join("owner-1", "te", "st",
                                      self.ASSET_ID + "_preview.jpeg"))
        data = read_local_thumb(self.thumbs_dir, self.ASSET_ID)
        self.assertEqual(data, FAKE_PREVIEW_JPEG)

    def test_v3_layout_webp(self):
        self._write_thumb(os.path.join("owner-1", "te", "st",
                                      self.ASSET_ID + "_preview.webp"))
        data = read_local_thumb(self.thumbs_dir, self.ASSET_ID)
        self.assertEqual(data, FAKE_PREVIEW_JPEG)

    def test_v2_layout_with_owner_folder(self):
        self._write_thumb(os.path.join("owner-1", "te", "st",
                                      self.ASSET_ID + "-preview.jpeg"))
        data = read_local_thumb(self.thumbs_dir, self.ASSET_ID)
        self.assertEqual(data, FAKE_PREVIEW_JPEG)

    def test_v1_classic_layout(self):
        self._write_thumb(os.path.join("te", "st",
                                      self.ASSET_ID + "-preview.jpeg"))
        data = read_local_thumb(self.thumbs_dir, self.ASSET_ID)
        self.assertEqual(data, FAKE_PREVIEW_JPEG)

    def test_legacy_layout(self):
        # Layout from the original implementation (PLAN.md's rough note),
        # kept as a candidate for compatibility.
        self._write_thumb(os.path.join("te", "st", self.ASSET_ID + ".jpeg"))
        data = read_local_thumb(self.thumbs_dir, self.ASSET_ID)
        self.assertEqual(data, FAKE_PREVIEW_JPEG)

    def test_missing_thumb_returns_none(self):
        self.assertIsNone(read_local_thumb(self.thumbs_dir, self.ASSET_ID))

    def test_empty_args_return_none(self):
        self.assertIsNone(read_local_thumb(None, self.ASSET_ID))
        self.assertIsNone(read_local_thumb(self.thumbs_dir, ""))


# ---------------------------------------------------------------------------
# Test recognition backends (with mocked HTTP)
# ---------------------------------------------------------------------------

class TestVisionLandmark(unittest.TestCase):

    @mock.patch("geotag.http_json")
    def test_vision_landmark_hit(self, mock_http_json):
        mock_http_json.return_value = FAKE_VISION_RESPONSE_LANDMARK
        result = vision_landmark(FAKE_PREVIEW_JPEG, "test-key", 0.5)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "Eiffel Tower")
        self.assertEqual(result[1], 0.95)
        self.assertEqual(result[2], 48.85837)
        self.assertEqual(result[3], 2.294481)

    @mock.patch("geotag.http_json")
    def test_vision_landmark_no_match(self, mock_http_json):
        mock_http_json.return_value = FAKE_VISION_RESPONSE_NO_MATCH
        result = vision_landmark(FAKE_PREVIEW_JPEG, "test-key", 0.5)
        self.assertIsNone(result)

    @mock.patch("geotag.http_json")
    def test_vision_landmark_low_score(self, mock_http_json):
        mock_http_json.return_value = FAKE_VISION_RESPONSE_LOW_SCORE
        result = vision_landmark(FAKE_PREVIEW_JPEG, "test-key", 0.5)
        self.assertIsNone(result)  # score 0.4 < min_score 0.5

    @mock.patch("geotag.http_json")
    def test_vision_landmark_meets_min_score(self, mock_http_json):
        mock_http_json.return_value = FAKE_VISION_RESPONSE_LOW_SCORE
        result = vision_landmark(FAKE_PREVIEW_JPEG, "test-key", 0.3)
        self.assertIsNotNone(result)  # score 0.4 >= min_score 0.3


class TestNominatimSearch(unittest.TestCase):

    @mock.patch("geotag.http_json")
    def test_nominatim_search_hit(self, mock_http_json):
        mock_http_json.return_value = FAKE_NOMINATIM_EIFFEL
        result = nominatim_search("Eiffel Tower", "test@example.com")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 48.85837)
        self.assertEqual(result[1], 2.294481)
        self.assertEqual(result[2], "Eiffel Tower, Paris, France")

    @mock.patch("geotag.http_json")
    def test_nominatim_search_not_found(self, mock_http_json):
        mock_http_json.return_value = FAKE_NOMINATIM_NOT_FOUND
        result = nominatim_search("Nonexistent Place", "test@example.com")
        self.assertIsNone(result)


class TestLensLocate(unittest.TestCase):

    @mock.patch("geotag.multipart_post")
    @mock.patch("geotag.http_json")
    @mock.patch("geotag.nominatim_search")
    def test_lens_locate_hit(self, mock_nominatim, mock_http_json, mock_multipart):
        mock_multipart.return_value = FAKE_SERPAPI_UPLOAD
        mock_http_json.return_value = FAKE_SERPAPI_LENS_RESPONSE
        mock_nominatim.return_value = (48.85837, 2.294481, "Eiffel Tower, Paris")

        result = lens_locate(FAKE_PREVIEW_JPEG, "test-serpapi-key", "test@example.com")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "Eiffel Tower")  # cleaned title
        self.assertEqual(result[1], 48.85837)
        self.assertEqual(result[2], 2.294481)

    @mock.patch("geotag.multipart_post")
    @mock.patch("geotag.http_json")
    @mock.patch("geotag.nominatim_search")
    def test_lens_locate_no_match(self, mock_nominatim, mock_http_json, mock_multipart):
        mock_multipart.return_value = FAKE_SERPAPI_UPLOAD
        mock_http_json.return_value = FAKE_SERPAPI_LENS_RESPONSE
        mock_nominatim.return_value = None

        result = lens_locate(FAKE_PREVIEW_JPEG, "test-serpapi-key", "test@example.com")
        self.assertIsNone(result)

    def test_lens_locate_too_large(self):
        large_bytes = b"x" * (500 * 1024 + 1)  # > 500KB
        result = lens_locate(large_bytes, "test-serpapi-key", "test@example.com")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Integration tests for scan/recognize/apply workflow
# ---------------------------------------------------------------------------

class TestScanWorkflow(unittest.TestCase):
    """Test scan subcommand behavior with mocked Immich."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @mock.patch("geotag.Immich")
    def test_scan_creates_csv(self, mock_immich_class):
        mock_immich = mock_immich_class.return_value
        mock_immich.assets_missing_geo.return_value = iter([FAKE_ASSET_MISSING_GEO])

        import argparse

        from geotag import cmd_scan
        args = argparse.Namespace(out=os.path.join(self.tmpdir, "test.csv"),
                                  limit=None, asset_id=None)

        # Mock environment
        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                cmd_scan(args)

        csv_path = os.path.join(self.tmpdir, "test.csv")
        self.assertTrue(os.path.exists(csv_path))
        rows = read_csv(csv_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset_id"], "test-asset-001")
        self.assertEqual(rows[0]["status"], "pending")

    @mock.patch("geotag.Immich")
    def test_scan_append_no_duplicate(self, mock_immich_class):
        mock_immich = mock_immich_class.return_value
        mock_immich.assets_missing_geo.return_value = iter([FAKE_ASSET_MISSING_GEO])

        import argparse

        from geotag import cmd_scan
        csv_path = os.path.join(self.tmpdir, "test.csv")

        # First scan
        args1 = argparse.Namespace(out=csv_path, limit=None, asset_id=None)
        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                cmd_scan(args1)

        # Second scan with same asset
        args2 = argparse.Namespace(out=csv_path, limit=None, asset_id=None)
        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                cmd_scan(args2)

        rows = read_csv(csv_path)
        self.assertEqual(len(rows), 1)  # No duplicate


class TestRecognizeWorkflow(unittest.TestCase):
    """Test recognize subcommand behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_recognize_skips_coordinates(self):
        """Rows with coordinates are skipped."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "48.85837",
             "longitude": "2.294481", "place": "Eiffel Tower", "source": "vision",
             "score": "0.95", "status": "located", "maps_url": "url1"},
            {"asset_id": "a2", "file_name": "f2.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_recognize
        args = argparse.Namespace(
            csv=csv_path,
            min_score=0.5,
            max_lens_matches=3,
            thumbs=None,
        )

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.preview_jpeg.return_value = FAKE_PREVIEW_JPEG
                    with mock.patch("geotag.vision_landmark") as mock_vision:
                        mock_vision.return_value = None  # No match
                        cmd_recognize(args)

        # Read back and verify a1 was not modified
        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")
        self.assertEqual(rows_after[0]["latitude"], "48.85837")

    def test_recognize_skips_non_pending(self):
        """Rows with status != pending are skipped."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "applied", "maps_url": ""},
            {"asset_id": "a2", "file_name": "f2.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_recognize
        args = argparse.Namespace(
            csv=csv_path,
            min_score=0.5,
            max_lens_matches=3,
            thumbs=None,
        )

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.preview_jpeg.return_value = FAKE_PREVIEW_JPEG
                    with mock.patch("geotag.vision_landmark") as mock_vision:
                        mock_vision.return_value = None
                        cmd_recognize(args)

        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "applied")  # unchanged

    def _recognize_args(self, csv_path):
        import argparse
        return argparse.Namespace(
            csv=csv_path,
            min_score=0.5,
            max_lens_matches=3,
            thumbs=None,
        )

    def test_recognize_vision_locates_row(self):
        """Vision hit fills coordinates, status=located, and persists to CSV."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        from geotag import cmd_recognize
        args = self._recognize_args(csv_path)

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.preview_jpeg.return_value = FAKE_PREVIEW_JPEG
                    with mock.patch("geotag.vision_landmark") as mock_vision:
                        mock_vision.return_value = (
                            "Eiffel Tower", 0.95, 48.85837, 2.294481)
                        cmd_recognize(args)

        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")
        self.assertEqual(rows_after[0]["latitude"], "48.85837")
        self.assertEqual(rows_after[0]["longitude"], "2.294481")
        self.assertEqual(rows_after[0]["place"], "Eiffel Tower")
        self.assertEqual(rows_after[0]["source"], "vision")
        self.assertEqual(rows_after[0]["score"], "0.95")
        self.assertIn("48.85837,2.29448", rows_after[0]["maps_url"])

    def test_recognize_thumbs_without_immich_config(self):
        """With no Immich API config, --thumbs is the only image source."""
        asset_id = "test-asset-001"
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": asset_id, "file_name": "f1.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        # Local thumb in the v3.x layout: thumbs/<owner>/<te>/<st>/<id>_preview.jpeg
        thumbs_dir = os.path.join(self.tmpdir, "thumbs")
        thumb_path = os.path.join(thumbs_dir, "owner-1", "te", "st",
                                  asset_id + "_preview.jpeg")
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        with open(thumb_path, "wb") as f:
            f.write(FAKE_PREVIEW_JPEG)

        import argparse

        from geotag import cmd_recognize
        args = argparse.Namespace(
            csv=csv_path,
            min_score=0.5,
            max_lens_matches=3,
            thumbs=thumbs_dir,
        )

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
            "IMMICH_URL": "",
            "IMMICH_API_KEY": "",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.vision_landmark") as mock_vision:
                    mock_vision.return_value = (
                        "Eiffel Tower", 0.95, 48.85837, 2.294481)
                    cmd_recognize(args)

        # Vision must have received the bytes read from the local thumb file
        mock_vision.assert_called_once_with(
            FAKE_PREVIEW_JPEG, "test-vision-key", 0.5)
        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")
        self.assertEqual(rows_after[0]["latitude"], "48.85837")

    def test_recognize_not_found(self):
        """No Vision match and no SerpApi key -> status=not_found."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        from geotag import cmd_recognize
        args = self._recognize_args(csv_path)

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
            "SERPAPI_KEY": "",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.preview_jpeg.return_value = FAKE_PREVIEW_JPEG
                    with mock.patch("geotag.vision_landmark") as mock_vision:
                        mock_vision.return_value = None
                        cmd_recognize(args)

        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "not_found")
        self.assertEqual(rows_after[0]["latitude"], "")

    def test_recognize_lens_fallback(self):
        """Vision miss + SERPAPI_KEY -> Lens fallback fills source=lens."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        from geotag import cmd_recognize
        args = self._recognize_args(csv_path)

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
            "SERPAPI_KEY": "test-serpapi-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.preview_jpeg.return_value = FAKE_PREVIEW_JPEG
                    with mock.patch("geotag.vision_landmark") as mock_vision:
                        mock_vision.return_value = None
                        with mock.patch("geotag.lens_locate") as mock_lens:
                            mock_lens.return_value = (
                                "Eiffel Tower", 48.85837, 2.294481,
                                "Eiffel Tower, Paris, France")
                            cmd_recognize(args)

        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")
        self.assertEqual(rows_after[0]["source"], "lens")
        self.assertEqual(rows_after[0]["latitude"], "48.85837")
        self.assertEqual(rows_after[0]["score"], "")

    def test_recognize_error_keeps_row_pending(self):
        """A per-row error counts as an error and leaves the row pending (retryable)."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        from geotag import cmd_recognize
        args = self._recognize_args(csv_path)

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.preview_jpeg.side_effect = ApiError("HTTP 500")
                    cmd_recognize(args)

        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "pending")
        # No stray columns added to the schema
        with open(csv_path, encoding="utf-8", newline="") as f:
            header = f.readline().strip()
        self.assertEqual(header.split(","), CSV_COLUMNS)

    def test_recognize_crash_safe_rewrite(self):
        """CSV is rewritten after each row: a crash mid-run loses no completed rows."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
            {"asset_id": "a2", "file_name": "f2.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        from geotag import cmd_recognize
        args = self._recognize_args(csv_path)

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.preview_jpeg.return_value = FAKE_PREVIEW_JPEG
                    with mock.patch("geotag.vision_landmark") as mock_vision:
                        mock_vision.side_effect = [
                            ("Eiffel Tower", 0.95, 48.85837, 2.294481),
                            RuntimeError("simulated crash"),
                        ]
                        with self.assertRaises(RuntimeError):
                            cmd_recognize(args)

        # Row a1 was completed and persisted before the crash; a2 untouched
        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")
        self.assertEqual(rows_after[0]["latitude"], "48.85837")
        self.assertEqual(rows_after[1]["status"], "pending")


class TestApplyWorkflow(unittest.TestCase):
    """Test apply subcommand behavior."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_apply_dry_run(self):
        """Dry run doesn't modify CSV or call Immich."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "48.85837",
             "longitude": "2.294481", "place": "Eiffel Tower", "source": "vision",
             "score": "0.95", "status": "located", "maps_url": "url1"},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_apply
        args = argparse.Namespace(csv=csv_path, dry_run=True)

        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    cmd_apply(args)

        # Immich.set_location should not be called
        mock_immich.set_location.assert_not_called()

        # CSV should be unchanged
        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")

    def test_apply_skips_no_coordinates(self):
        """Rows without coordinates are skipped."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_apply
        args = argparse.Namespace(csv=csv_path, dry_run=False)

        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    cmd_apply(args)

        mock_immich.set_location.assert_not_called()

    def test_apply_skips_applied(self):
        """Already applied rows are skipped."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "48.85837",
             "longitude": "2.294481", "place": "Eiffel Tower", "source": "vision",
             "score": "0.95", "status": "applied", "maps_url": "url1"},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_apply
        args = argparse.Namespace(csv=csv_path, dry_run=False)

        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    cmd_apply(args)

        mock_immich.set_location.assert_not_called()

    def test_apply_writes_and_marks_applied(self):
        """Apply writes to Immich and marks status=applied."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "48.85837",
             "longitude": "2.294481", "place": "Eiffel Tower", "source": "vision",
             "score": "0.95", "status": "located", "maps_url": "url1"},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_apply
        args = argparse.Namespace(csv=csv_path, dry_run=False)

        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    cmd_apply(args)

        # Should have called set_location
        mock_immich.set_location.assert_called_once()
        call_args = mock_immich.set_location.call_args
        self.assertEqual(call_args[0][0], "a1")
        self.assertEqual(call_args[0][1], 48.85837)
        self.assertEqual(call_args[0][2], 2.294481)

        # CSV should have status=applied
        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "applied")

    def test_apply_idempotent(self):
        """Re-running apply on applied rows doesn't re-write."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "48.85837",
             "longitude": "2.294481", "place": "Eiffel Tower", "source": "vision",
             "score": "0.95", "status": "applied", "maps_url": "url1"},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_apply
        args = argparse.Namespace(csv=csv_path, dry_run=False)

        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    # First run
                    cmd_apply(args)
                    call_count_1 = mock_immich.set_location.call_count
                    # Second run
                    cmd_apply(args)
                    call_count_2 = mock_immich.set_location.call_count

        self.assertEqual(call_count_1, 0)  # Already applied, skipped
        self.assertEqual(call_count_2, 0)  # Still skipped

    def test_apply_put_failure_not_marked_applied(self):
        """A failed PUT leaves the row un-applied so a re-run retries it."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "48.85837",
             "longitude": "2.294481", "place": "Eiffel Tower", "source": "vision",
             "score": "0.95", "status": "located", "maps_url": "url1"},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_apply
        args = argparse.Namespace(csv=csv_path, dry_run=False)

        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.set_location.side_effect = ApiError("HTTP 500")
                    cmd_apply(args)

        mock_immich.set_location.assert_called_once()
        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")  # not marked applied

    def test_apply_invalid_coordinates(self):
        """Rows with unparseable coordinates are counted as errors, not written."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "not-a-number",
             "longitude": "2.294481", "place": "", "source": "manual",
             "score": "", "status": "located", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_apply
        args = argparse.Namespace(csv=csv_path, dry_run=False)

        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    cmd_apply(args)

        mock_immich.set_location.assert_not_called()
        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")


class TestDuplicateAssetIdGuard(unittest.TestCase):
    """Test that scan doesn't create duplicate asset_id rows."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_scan_duplicate_guard(self):
        """Multiple scan runs don't duplicate asset_id."""
        csv_path = os.path.join(self.tmpdir, "test.csv")

        import argparse

        from geotag import cmd_scan

        args = argparse.Namespace(out=csv_path, limit=None, asset_id=None)

        with mock.patch.dict(os.environ, {
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    # Both scans return the same asset
                    mock_immich.assets_missing_geo.return_value = iter([
                        FAKE_ASSET_MISSING_GEO
                    ])

                    # First scan
                    cmd_scan(args)
                    rows_after_1 = read_csv(csv_path)
                    self.assertEqual(len(rows_after_1), 1)

                    # Second scan with same asset
                    cmd_scan(args)
                    rows_after_2 = read_csv(csv_path)
                    self.assertEqual(len(rows_after_2), 1)  # Still 1, no duplicate


class TestResumeSkip(unittest.TestCase):
    """Test that recognize skips rows that already have coordinates."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_recognize_resume_skip(self):
        """Recognize skips rows with coordinates on resume."""
        csv_path = os.path.join(self.tmpdir, "test.csv")
        rows = [
            {"asset_id": "a1", "file_name": "f1.jpg", "latitude": "48.85837",
             "longitude": "2.294481", "place": "Eiffel Tower", "source": "vision",
             "score": "0.95", "status": "located", "maps_url": "url1"},
            {"asset_id": "a2", "file_name": "f2.jpg", "latitude": "",
             "longitude": "", "place": "", "source": "",
             "score": "", "status": "pending", "maps_url": ""},
        ]
        write_csv(csv_path, rows)

        import argparse

        from geotag import cmd_recognize
        args = argparse.Namespace(
            csv=csv_path,
            min_score=0.5,
            max_lens_matches=3,
            thumbs=None,
        )

        with mock.patch.dict(os.environ, {
            "GOOGLE_VISION_API_KEY": "test-vision-key",
            "IMMICH_URL": "https://immich.example.com",
            "IMMICH_API_KEY": "test-key",
        }):
            with mock.patch("geotag.load_env"):
                with mock.patch("geotag.Immich") as mock_immich_class:
                    mock_immich = mock_immich_class.return_value
                    mock_immich.preview_jpeg.return_value = FAKE_PREVIEW_JPEG
                    with mock.patch("geotag.vision_landmark") as mock_vision:
                        mock_vision.return_value = (
                            "Some Place", 0.9, 1.0, 2.0
                        )
                        cmd_recognize(args)

        # a1 should be unchanged (skipped)
        # a2 should be processed
        rows_after = read_csv(csv_path)
        self.assertEqual(rows_after[0]["status"], "located")
        self.assertEqual(rows_after[1]["status"], "located")

        # preview_jpeg should only be called for a2
        self.assertEqual(mock_immich.preview_jpeg.call_count, 1)


# ---------------------------------------------------------------------------
# gemini_candidates
# ---------------------------------------------------------------------------

class TestParseGeminiJson(unittest.TestCase):
    """parse_gemini_json: normalize Gemini's JSON answer, never crash."""

    def test_valid_full_answer(self):
        text = json.dumps([
            {"index": 1, "place": "Fort Alpha", "city": "Alphaville",
             "country": "Testland", "confidence": 0.9},
            {"index": 2, "place": None, "confidence": 0},
        ])
        results = gemini_candidates.parse_gemini_json(text, 2)
        self.assertEqual(results[0]["place"], "Fort Alpha")
        self.assertEqual(results[0]["confidence"], 0.9)
        self.assertIsNone(results[1]["place"])

    def test_missing_entry_becomes_null(self):
        # Gemini returned only 1 object for 3 images
        text = json.dumps([{"index": 2, "place": "Colosseum", "confidence": 0.8}])
        results = gemini_candidates.parse_gemini_json(text, 3)
        self.assertIsNone(results[0]["place"])
        self.assertEqual(results[1]["place"], "Colosseum")
        self.assertIsNone(results[2]["place"])

    def test_malformed_json_all_null(self):
        results = gemini_candidates.parse_gemini_json("not json at all", 2)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["place"] is None for r in results))

    def test_non_list_answer_all_null(self):
        results = gemini_candidates.parse_gemini_json('{"error": "x"}', 2)
        self.assertTrue(all(r["place"] is None for r in results))

    def test_out_of_range_index_ignored(self):
        text = json.dumps([{"index": 7, "place": "Nowhere", "confidence": 1.0}])
        results = gemini_candidates.parse_gemini_json(text, 2)
        self.assertTrue(all(r["place"] is None for r in results))

    def test_confidence_clamped(self):
        text = json.dumps([{"index": 1, "place": "X", "confidence": 5}])
        results = gemini_candidates.parse_gemini_json(text, 1)
        self.assertEqual(results[0]["confidence"], 1.0)

    def test_empty_place_string_is_null(self):
        text = json.dumps([{"index": 1, "place": "  ", "confidence": 0.9}])
        results = gemini_candidates.parse_gemini_json(text, 1)
        self.assertIsNone(results[0]["place"])


class TestGeocodePlace(unittest.TestCase):
    """geocode_place: fall back to simpler queries when the full one fails."""

    def test_full_query_hits_first(self):
        with mock.patch.object(gemini_candidates, "nominatim_search") as mock_geo:
            mock_geo.return_value = (1.0, 2.0, "Somewhere")
            out = gemini_candidates.geocode_place("Fort Alpha", "Alphaville", "Testland", None)
        self.assertEqual(out, (1.0, 2.0, "Somewhere"))
        self.assertEqual(mock_geo.call_count, 1)
        mock_geo.assert_called_with("Fort Alpha, Alphaville, Testland", None)

    def test_falls_back_through_comma_segments(self):
        # Full queries and the comma-containing place fail; the trailing
        # parent site segment resolves. Query order: (place,city,country),
        # (place,city), (place,country), place, then reversed place
        # segments - "Fort Alpha" is the 5th.
        with mock.patch.object(gemini_candidates, "nominatim_search") as mock_geo:
            mock_geo.side_effect = [None, None, None, None,
                                    (-27.00, -16.99, "Fort Alpha")]
            with mock.patch.object(gemini_candidates.time, "sleep"):
                out = gemini_candidates.geocode_place(
                    "North Tower, Fort Alpha", "Alphaville", "Testland", None)
        self.assertEqual(out, (-27.00, -16.99, "Fort Alpha"))
        self.assertEqual(mock_geo.call_count, 5)
        self.assertEqual(mock_geo.call_args_list[4][0][0], "Fort Alpha")

    def test_all_fail_returns_none(self):
        with mock.patch.object(gemini_candidates, "nominatim_search") as mock_geo:
            mock_geo.return_value = None
            with mock.patch.object(gemini_candidates.time, "sleep"):
                self.assertIsNone(
                    gemini_candidates.geocode_place("Nowhere", "X", "Y", None))

    def test_validator_rejects_wrong_continent_and_tries_next(self):
        # First hit resolves to a same-named place on another continent; the
        # validator (trip bbox) rejects it and the next query variant finds
        # the one inside the trip region.
        # Latitude alone cannot tell them apart - the longitude can.
        trip_region = lambda lat, lon, name: (  # noqa: E731
            -30.0 < lat < -24.0 and -20.0 < lon < -14.0)
        calls = [(-26.50, 30.00, "St. Mary's Church, Farland"),
                 (-26.20, -16.40, "Gammaton, Testland")]
        with mock.patch.object(gemini_candidates, "nominatim_search") as mock_geo:
            mock_geo.side_effect = calls
            with mock.patch.object(gemini_candidates.time, "sleep"):
                out = gemini_candidates.geocode_place(
                    "St. Mary's Church", "Gammaton", "Testland",
                    None, validate=trip_region)
        self.assertEqual(out, (-26.20, -16.40, "Gammaton, Testland"))
        self.assertEqual(mock_geo.call_count, 2)

    def test_validator_accepts_first_hit(self):
        trip_region = lambda lat, lon, name: (  # noqa: E731
            -30.0 < lat < -24.0 and -20.0 < lon < -14.0)
        with mock.patch.object(gemini_candidates, "nominatim_search") as mock_geo:
            mock_geo.return_value = (-27.00, -17.00, "Fort Alpha, Testland")
            out = gemini_candidates.geocode_place(
                "Fort Alpha", "Alphaville", "Testland", None, validate=trip_region)
        self.assertEqual(out, (-27.00, -17.00, "Fort Alpha, Testland"))
        self.assertEqual(mock_geo.call_count, 1)


class TestToDatetime(unittest.TestCase):
    def test_bare_date_gets_time(self):
        self.assertEqual(gemini_candidates._to_datetime("2001-05-01"),
                         "2001-05-01T00:00:00.000Z")

    def test_datetime_unchanged(self):
        self.assertEqual(gemini_candidates._to_datetime("2001-05-01T10:00:00.000Z"),
                         "2001-05-01T10:00:00.000Z")

    def test_none_unchanged(self):
        self.assertIsNone(gemini_candidates._to_datetime(None))


class TestWindowAssets(unittest.TestCase):
    """window_assets: filter by type, missing geo, and not_found membership."""

    def _run(self, pages, not_found_ids):
        immich = gemini_candidates.Immich("https://immich.example.com", "k")
        with mock.patch.object(gemini_candidates, "http_json") as mock_http:
            mock_http.side_effect = pages
            return list(gemini_candidates.window_assets(
                immich, "2001-05-01", "2001-07-01", not_found_ids))

    def test_filters(self):
        page = {"assets": {"items": [
            {"id": "a1", "type": "IMAGE", "exifInfo": {"latitude": None, "longitude": None}},
            {"id": "a2", "type": "VIDEO", "exifInfo": {"latitude": None, "longitude": None}},
            {"id": "a3", "type": "IMAGE", "exifInfo": {"latitude": 1.0, "longitude": 2.0}},
            {"id": "a4", "type": "IMAGE", "exifInfo": {"latitude": None, "longitude": None}},
        ], "nextPage": None}}
        out = self._run([page], {"a1", "a4", "a9"})
        self.assertEqual([a["id"] for a in out], ["a1", "a4"])

    def test_pagination_stops(self):
        p1 = {"assets": {"items": [
            {"id": "a1", "type": "IMAGE", "exifInfo": {}}], "nextPage": 2}}
        p2 = {"assets": {"items": [], "nextPage": None}}
        out = self._run([p1, p2], {"a1"})
        self.assertEqual(len(out), 1)


# ---------------------------------------------------------------------------
# gemini_apply
# ---------------------------------------------------------------------------

class TestHaversineAndTime(unittest.TestCase):
    def test_haversine_known_distance(self):
        # Fort Alpha -> Port Beta is roughly 180 km
        d = gemini_apply.haversine_km(-27.00, -17.00, -26.00, -15.60)
        self.assertTrue(150 < d < 220, d)

    def test_haversine_zero(self):
        self.assertAlmostEqual(gemini_apply.haversine_km(10.0, 20.0, 10.0, 20.0), 0.0)

    def test_parse_time_z_suffix(self):
        t = gemini_apply.parse_time("2001-06-08T15:17:43.403Z")
        self.assertIsNotNone(t)

    def test_parse_time_offset(self):
        t = gemini_apply.parse_time("2001-06-08T15:17:43+00:00")
        self.assertIsNotNone(t)

    def test_parse_time_garbage(self):
        self.assertIsNone(gemini_apply.parse_time("not a time"))
        self.assertIsNone(gemini_apply.parse_time(""))


class TestParseBbox(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(gemini_candidates.parse_bbox("-30.0,-20.0,-24.0,-14.0"),
                         (-30.0, -20.0, -24.0, -14.0))

    def test_wrong_arity(self):
        self.assertIsNone(gemini_candidates.parse_bbox("1,2,3"))

    def test_garbage(self):
        self.assertIsNone(gemini_candidates.parse_bbox("a,b,c,d"))

    def test_in_bbox(self):
        bbox = (-30.0, -20.0, -24.0, -14.0)
        self.assertTrue(gemini_candidates.in_bbox(-27.00, -17.00, bbox))
        self.assertFalse(gemini_candidates.in_bbox(48.85, 2.29, bbox))


class TestGeoAllowed(unittest.TestCase):
    """Trip-geography rule: one allowed region + airports in a second region."""

    TRIP = (-30.0, -20.0, -24.0, -14.0)
    AIRPORTS = (-40.0, -10.0, -34.0, -4.0)

    def test_inside_allow_bbox(self):
        self.assertTrue(gemini_candidates.geo_allowed(
            -27.00, -17.00, "Fort Alpha", "Fort Alpha, Alphaville, Testland",
            [self.TRIP]))

    def test_outside_all_disallowed(self):
        # same-named church on another continent - the failure this guards
        self.assertFalse(gemini_candidates.geo_allowed(
            -26.50, 30.00, "St. Mary's Church",
            "St. Mary's Church, Farland", [self.TRIP]))

    def test_airport_inside_airport_bbox(self):
        self.assertTrue(gemini_candidates.geo_allowed(
            -37.00, -7.00, "Delta Airport", "Delta International Airport",
            [self.TRIP], [self.AIRPORTS]))

    def test_non_airport_outside_allow_bbox_rejected(self):
        # "airport" not in the name -> airport bboxes do not apply
        self.assertFalse(gemini_candidates.geo_allowed(
            -37.00, -7.00, "Some Diner", "Diner, Delta City",
            [self.TRIP], [self.AIRPORTS]))

    def test_airport_outside_airport_bbox_rejected(self):
        self.assertFalse(gemini_candidates.geo_allowed(
            10.00, 60.00, "Epsilon Airport", "Epsilon International Airport",
            [self.TRIP], [self.AIRPORTS]))


class TestRecoverAndConfirm(unittest.TestCase):
    """Recover failed geocodes; confirm uncertain proposals via neighbors.

    The photo's OWN proposal is always used - never a neighbor's coords.
    """

    def _row(self, aid, status, taken_at, lat="", lon="", place="",
             city="", country="", confidence=""):
        return {"asset_id": aid, "status": status, "taken_at": taken_at,
                "latitude": lat, "longitude": lon, "place": place,
                "city": city, "country": country, "confidence": confidence,
                "geocoded_name": "", "maps_url": "", "inferred_from": ""}

    def _run(self, rows, **kw):
        with mock.patch.object(gemini_apply, "geocode_place") as mock_geo:
            mock_geo.return_value = kw.pop("geo", (-27.01, -17.01, "Fort Alpha"))
            out = gemini_apply.recover_and_confirm(
                rows, None, kw.pop("min_confidence", 0.5), kw.pop("floor", 0.3),
                kw.pop("hours", 2), kw.pop("km", 10), None)
        return out

    def test_high_confidence_recovered_without_coherence(self):
        # No anchors at all - high confidence still recovers
        rows = [self._row("a1", "no_match", "2001-06-08T10:00:00Z",
                          place="Old Courtyard", confidence="0.95")]
        rows, recovered, confirmed = self._run(rows)
        self.assertEqual((recovered, confirmed), (1, 0))
        self.assertEqual(rows[0]["status"], "candidate")
        self.assertEqual(rows[0]["latitude"], "-27.01")
        self.assertEqual(rows[0]["inferred_from"], "")

    def test_uncertain_confirmed_by_coherent_neighbors(self):
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z",
                      "-27.00", "-17.00", "Fort Alpha"),
            self._row("a2", "no_match", "2001-06-08T10:45:00Z",
                      place="Old Courtyard", confidence="0.4"),
            self._row("a3", "candidate", "2001-06-08T11:30:00Z",
                      "-26.998", "-16.999", "Fort Alpha"),
        ]
        rows, recovered, confirmed = self._run(rows)
        self.assertEqual((recovered, confirmed), (0, 1))
        self.assertEqual(rows[1]["status"], "confirmed")
        # the proposal's own geocode, not the neighbor's coords
        self.assertEqual(rows[1]["latitude"], "-27.01")

    def test_uncertain_scattered_neighbors_not_confirmed(self):
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z",
                      "-27.00", "-17.00", "Fort Alpha"),
            self._row("a2", "no_match", "2001-06-08T10:45:00Z",
                      place="Old Courtyard", confidence="0.4"),
            # second anchor ~180 km away within the window -> not coherent
            self._row("a3", "candidate", "2001-06-08T11:30:00Z",
                      "-26.00", "-15.60", "Port Beta"),
        ]
        rows, recovered, confirmed = self._run(rows)
        self.assertEqual((recovered, confirmed), (0, 0))
        self.assertEqual(rows[1]["status"], "no_match")

    def test_uncertain_no_neighbors_not_confirmed(self):
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z",
                      "-27.00", "-17.00", "Fort Alpha"),
            self._row("a2", "no_match", "2001-06-08T18:00:00Z",
                      place="Old Courtyard", confidence="0.4"),
        ]
        rows, recovered, confirmed = self._run(rows)
        self.assertEqual((recovered, confirmed), (0, 0))

    def test_below_floor_left_blank(self):
        rows = [self._row("a1", "no_match", "2001-06-08T10:00:00Z",
                          place="Maybe a beach", confidence="0.2")]
        rows, recovered, confirmed = self._run(rows)
        self.assertEqual((recovered, confirmed), (0, 0))
        self.assertEqual(rows[0]["status"], "no_match")

    def test_geocode_still_fails_stays_no_match(self):
        rows = [self._row("a1", "no_match", "2001-06-08T10:00:00Z",
                          place="Ungeocodable", confidence="0.9")]
        rows, recovered, confirmed = self._run(rows, geo=None)
        self.assertEqual((recovered, confirmed), (0, 0))
        self.assertEqual(rows[0]["status"], "no_match")

    def test_no_proposal_untouched(self):
        rows = [self._row("a1", "no_match", "2001-06-08T10:00:00Z")]
        rows, recovered, confirmed = self._run(rows)
        self.assertEqual((recovered, confirmed), (0, 0))

    def test_applied_rows_anchor(self):
        rows = [
            self._row("a1", "applied", "2001-06-08T10:00:00Z",
                      "-27.00", "-17.00", "Fort Alpha"),
            self._row("a2", "no_match", "2001-06-08T10:30:00Z",
                      place="Old Courtyard", confidence="0.4"),
        ]
        rows, recovered, confirmed = self._run(rows)
        self.assertEqual((recovered, confirmed), (0, 1))


class TestFlagOutliers(unittest.TestCase):
    """Small isolated place-groups are flagged for review, not applied."""

    def _row(self, aid, status, taken_at, lat="-27.00", lon="-17.00", place="X"):
        return {"asset_id": aid, "status": status, "taken_at": taken_at,
                "latitude": lat, "longitude": lon, "place": place,
                "geocoded_name": "", "maps_url": "", "confidence": "",
                "inferred_from": ""}

    def test_small_isolated_group_flagged(self):
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z", place="Stadium Zeta"),
            # nearest other located photo is days away
            self._row("a2", "candidate", "2001-06-20T10:00:00Z", place="Fort"),
            self._row("a3", "candidate", "2001-06-20T11:00:00Z", place="Fort"),
            self._row("a4", "candidate", "2001-06-20T12:00:00Z", place="Fort"),
        ]
        rows, flagged = gemini_apply.flag_outliers(rows, max_group=2, isolation_hours=12)
        self.assertEqual(flagged, 1)
        self.assertEqual(rows[0]["status"], "flagged")
        self.assertEqual(rows[0]["flag"], "outlier")

    def test_small_group_with_nearby_photos_not_flagged(self):
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z", place="Stadium Zeta"),
            self._row("a2", "candidate", "2001-06-08T12:00:00Z", place="Fort"),
            self._row("a3", "candidate", "2001-06-08T14:00:00Z", place="Fort"),
            self._row("a4", "candidate", "2001-06-08T16:00:00Z", place="Fort"),
        ]
        rows, flagged = gemini_apply.flag_outliers(rows, max_group=2, isolation_hours=12)
        self.assertEqual(flagged, 0)
        self.assertEqual(rows[0]["status"], "candidate")

    def test_big_group_not_flagged(self):
        rows = [self._row(f"a{i}", "candidate",
                          f"2001-06-08T10:0{i}:00Z", place="Fort")
                for i in range(1, 4)]
        rows, flagged = gemini_apply.flag_outliers(rows, max_group=2, isolation_hours=12)
        self.assertEqual(flagged, 0)

    def test_applied_keeps_status_but_flagged(self):
        rows = [
            self._row("a1", "applied", "2001-06-08T10:00:00Z", place="Stadium Zeta"),
            self._row("a2", "candidate", "2001-06-20T10:00:00Z", place="Fort"),
            self._row("a3", "candidate", "2001-06-20T11:00:00Z", place="Fort"),
            self._row("a4", "candidate", "2001-06-20T12:00:00Z", place="Fort"),
        ]
        rows, flagged = gemini_apply.flag_outliers(rows, max_group=2, isolation_hours=12)
        self.assertEqual(flagged, 1)
        self.assertEqual(rows[0]["status"], "applied")
        self.assertEqual(rows[0]["flag"], "outlier")

    def test_flag_column_persisted(self):
        # flag_outliers sets row["flag"]; the CSV schema must carry it
        self.assertIn("flag", gemini_candidates.CANDIDATE_COLUMNS)

    def test_reviewed_rows_never_reflagged(self):
        rows = [
            self._row("a1", "applied", "2001-06-08T10:00:00Z", place="Stadium Zeta"),
            self._row("a2", "candidate", "2001-06-20T10:00:00Z", place="Fort"),
            self._row("a3", "candidate", "2001-06-20T11:00:00Z", place="Fort"),
            self._row("a4", "candidate", "2001-06-20T12:00:00Z", place="Fort"),
        ]
        rows[0]["flag"] = "reviewed"  # owner approved it on the review page
        rows, flagged = gemini_apply.flag_outliers(rows, max_group=2, isolation_hours=12)
        self.assertEqual(flagged, 0)
        self.assertEqual(rows[0]["flag"], "reviewed")

    def test_reviewed_rows_still_anchor_isolation(self):
        # A reviewed photo nearby must still prevent a false outlier flag.
        rows = [
            self._row("a1", "applied", "2001-06-08T10:00:00Z", place="Capital City"),
            self._row("a2", "candidate", "2001-06-08T11:00:00Z", place="Stadium Zeta"),
        ]
        rows[0]["flag"] = "reviewed"
        rows, flagged = gemini_apply.flag_outliers(rows, max_group=2, isolation_hours=12)
        self.assertEqual(flagged, 0)


class TestMergeIntoState(unittest.TestCase):
    def _state(self, status="not_found"):
        return [{"asset_id": "a1", "file_name": "f1.jpg", "latitude": "",
                 "longitude": "", "place": "", "source": "", "score": "",
                 "status": status, "maps_url": ""}]

    def test_merge_candidate_marks_located_gemini(self):
        cand = [{"asset_id": "a1", "status": "candidate",
                 "latitude": "-27.00", "longitude": "-17.00",
                 "place": "Fort Alpha", "confidence": "0.99"}]
        state, merged = gemini_apply.merge_into_state(self._state(), cand)
        self.assertEqual(merged, 1)
        self.assertEqual(state[0]["status"], "located")
        self.assertEqual(state[0]["source"], "gemini")

    def test_merge_confirmed_marks_located_gemini_confirmed(self):
        cand = [{"asset_id": "a1", "status": "confirmed",
                 "latitude": "-27.00", "longitude": "-17.00",
                 "place": "Old Courtyard", "confidence": "0.4"}]
        state, merged = gemini_apply.merge_into_state(self._state(), cand)
        self.assertEqual(merged, 1)
        self.assertEqual(state[0]["source"], "gemini-confirmed")

    def test_merge_never_touches_applied(self):
        state = [{"asset_id": "a1", "file_name": "f1.jpg", "latitude": "1.0",
                  "longitude": "2.0", "place": "Old", "source": "vision",
                  "score": "0.9", "status": "applied", "maps_url": ""}]
        cand = [{"asset_id": "a1", "status": "candidate",
                 "latitude": "-27.00", "longitude": "-17.00",
                 "place": "Fort Alpha", "confidence": "0.99"}]
        state, merged = gemini_apply.merge_into_state(state, cand)
        self.assertEqual(merged, 0)
        self.assertEqual(state[0]["place"], "Old")

    def test_merge_skips_no_match_and_flagged(self):
        for status in ("no_match", "flagged"):
            cand = [{"asset_id": "a1", "status": status,
                     "latitude": "-27.00", "longitude": "-17.00"}]
            state, merged = gemini_apply.merge_into_state(self._state(), cand)
            self.assertEqual(merged, 0)
            self.assertEqual(state[0]["status"], "not_found")


class TestFlagGeoOutliers(unittest.TestCase):
    """Geography coherence: a candidate far from ALL its temporal
    neighbors is a wrong-continent geocode, not a location."""

    def _row(self, aid, status, taken_at, lat, lon, place="X", flag=""):
        return {"asset_id": aid, "status": status, "taken_at": taken_at,
                "latitude": lat, "longitude": lon, "place": place,
                "geocoded_name": "", "maps_url": "", "confidence": "",
                "inferred_from": "", "flag": flag}

    def test_wrong_continent_candidate_flagged(self):
        # "Harbor Lighthouse Park" geocoded to a same-named place on another
        # continent; same-day photos are all at the real Harbor Lighthouse.
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z",
                      "5.000", "70.000", "Harbor Lighthouse Park"),
            self._row("a2", "candidate", "2001-06-08T10:30:00Z",
                      "-27.000", "-17.000", "Harbor Lighthouse Viewpoint"),
            self._row("a3", "candidate", "2001-06-08T11:00:00Z",
                      "-27.000", "-17.000", "Harbor Lighthouse Viewpoint"),
        ]
        rows, flagged = gemini_apply.flag_geo_outliers(rows, hours=12, km=100)
        self.assertEqual(flagged, 1)
        self.assertEqual(rows[0]["status"], "flagged")
        self.assertEqual(rows[0]["flag"], "geo-outlier")
        self.assertEqual(rows[1]["status"], "candidate")

    def test_coherent_candidate_not_flagged(self):
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z",
                      "-27.000", "-17.000", "Harbor Lighthouse Park"),
            self._row("a2", "candidate", "2001-06-08T10:30:00Z",
                      "-27.012", "-16.976", "Harbor Lighthouse Viewpoint"),
        ]
        rows, flagged = gemini_apply.flag_geo_outliers(rows, hours=12, km=100)
        self.assertEqual(flagged, 0)

    def test_no_neighbors_left_alone(self):
        # Lonely photos are the isolation flagger's job, not this one's.
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z",
                      "5.000", "70.000"),
            self._row("a2", "candidate", "2001-06-20T10:00:00Z",
                      "-27.000", "-17.000"),
        ]
        rows, flagged = gemini_apply.flag_geo_outliers(rows, hours=12, km=100)
        self.assertEqual(flagged, 0)

    def test_reviewed_rows_exempt(self):
        rows = [
            self._row("a1", "candidate", "2001-06-08T10:00:00Z",
                      "5.000", "70.000", flag="reviewed"),
            self._row("a2", "candidate", "2001-06-08T10:30:00Z",
                      "-27.000", "-17.000"),
        ]
        rows, flagged = gemini_apply.flag_geo_outliers(rows, hours=12, km=100)
        # a1 is exempt; a2 contradicts its only neighbor and is flagged
        self.assertEqual(rows[0]["status"], "candidate")
        self.assertEqual(rows[0]["flag"], "reviewed")
        self.assertEqual(rows[1]["status"], "flagged")

    def test_applied_keeps_status_but_flagged(self):
        rows = [
            self._row("a1", "applied", "2001-06-08T10:00:00Z",
                      "5.000", "70.000"),
            self._row("a2", "candidate", "2001-06-08T10:30:00Z",
                      "-27.000", "-17.000"),
        ]
        rows, flagged = gemini_apply.flag_geo_outliers(rows, hours=12, km=100)
        # both contradict each other: applied keeps its status, candidate
        # is held back for review
        self.assertEqual(flagged, 2)
        self.assertEqual(rows[0]["status"], "applied")
        self.assertEqual(rows[0]["flag"], "geo-outlier")
        self.assertEqual(rows[1]["status"], "flagged")


# ---------------------------------------------------------------------------
# gemini_audit
# ---------------------------------------------------------------------------

class TestSampleEvery(unittest.TestCase):
    def test_every_100th(self):
        items = list(range(350))
        out = gemini_audit.sample_every(items, 100)
        self.assertEqual(out, [0, 100, 200, 300])

    def test_fewer_than_every(self):
        self.assertEqual(gemini_audit.sample_every([1, 2, 3], 100), [1])

    def test_zero_and_negative(self):
        self.assertEqual(gemini_audit.sample_every([1, 2, 3], 0), [])
        self.assertEqual(gemini_audit.sample_every([1, 2, 3], -1), [])


class TestVerdictFor(unittest.TestCase):
    def test_match(self):
        self.assertEqual(gemini_audit.verdict_for(0.5), "match")
        self.assertEqual(gemini_audit.verdict_for(25.0), "match")

    def test_check(self):
        self.assertEqual(gemini_audit.verdict_for(25.1), "check")
        self.assertEqual(gemini_audit.verdict_for(100.0), "check")

    def test_mismatch(self):
        self.assertEqual(gemini_audit.verdict_for(100.1), "mismatch")
        self.assertEqual(gemini_audit.verdict_for(8000.0), "mismatch")

    def test_none_is_no_geocode(self):
        self.assertEqual(gemini_audit.verdict_for(None), "no_geocode")


class TestAuditWorkflow(unittest.TestCase):
    """located_images: paginate, keep only photos that carry GPS."""

    def test_located_images_filters_and_paginates(self):
        immich = gemini_audit.geotag.Immich("https://immich.example.com", "k")
        p1 = {"assets": {"items": [
            {"id": "a1", "exifInfo": {"latitude": 1.0, "longitude": 2.0}},
            {"id": "a2", "exifInfo": {"latitude": None, "longitude": None}},
            {"id": "a3", "exifInfo": {}},
        ], "nextPage": 2}}
        p2 = {"assets": {"items": [
            {"id": "a4", "exifInfo": {"latitude": 3.0, "longitude": 4.0}},
        ], "nextPage": None}}
        with mock.patch.object(gemini_audit, "http_json") as mock_http:
            mock_http.side_effect = [p1, p2]
            out = list(gemini_audit.located_images(immich))
        self.assertEqual([a["id"] for a in out], ["a1", "a4"])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()

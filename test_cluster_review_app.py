#!/usr/bin/env python3
"""Tests for cluster_review_app: row compaction, image ids, page render.

Fake-payload style: no network, no Immich, no real files outside tmpdir.
Run: python3 test_cluster_review_app.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cluster_review_app


def row(aid="a1", name="f1.jpg", lat="45.123456", lon="9.654321",
        sim="0.9123", mid="m1", mname="match1.jpg", n="3",
        spread="12.5", status="review"):
    return {"asset_id": aid, "file_name": name, "latitude": lat,
            "longitude": lon, "similarity": sim, "match_id": mid,
            "match_name": mname, "n_matches": n, "spread_m": spread,
            "status": status, "maps_url": "https://maps.example/?q=45,9",
            "immich_url": "https://immich.example/photos/" + aid}


class TestCompactRows(unittest.TestCase):
    def test_fields_and_order(self):
        out = cluster_review_app.compact_rows([row()])[0]
        self.assertEqual(out[0], "a1")
        self.assertEqual(out[1], "f1.jpg")
        self.assertEqual(out[2], 45.123456)
        self.assertEqual(out[3], 9.654321)
        self.assertEqual(out[4], 0.9123)
        self.assertEqual(out[5], "m1")
        self.assertEqual(out[6], "match1.jpg")
        self.assertEqual(out[7], 3)
        self.assertEqual(out[8], 12)
        self.assertEqual(out[9], "review")
        self.assertEqual(len(out), len(cluster_review_app.FIELDS))

    def test_empty(self):
        self.assertEqual(cluster_review_app.compact_rows([]), [])


class TestImageIds(unittest.TestCase):
    def test_dedupes_photo_and_match(self):
        rows = [row(aid="a1", mid="m1"), row(aid="a2", mid="m1"),
                row(aid="a3", mid="m2")]
        self.assertEqual(cluster_review_app.image_ids(rows),
                         ["a1", "a2", "a3", "m1", "m2"])


class TestRenderPage(unittest.TestCase):
    def test_page_contains_rows_and_controls(self):
        rows = [row(status="applied"), row(aid="a2", name="f2.jpg",
                                          status="review")]
        page = cluster_review_app.render_page(rows)
        self.assertIn("Cluster fill", page)
        self.assertIn("fStatus", page)
        self.assertIn("fSim", page)
        self.assertIn("fText", page)
        self.assertIn("copyBtn", page)
        # embedded JSON carries both rows
        self.assertIn("f1.jpg", page)
        self.assertIn("f2.jpg", page)
        # links survive
        self.assertIn("https://maps.example/?q=45,9", page)
        self.assertIn("https://immich.example/photos/a1", page)

    def test_embedded_json_is_valid(self):
        rows = [row(), row(aid="a2", sim="0.88")]
        page = cluster_review_app.render_page(rows)
        start = page.index("const ROWS = ") + len("const ROWS = ")
        end = page.index(";\n", start)
        data = json.loads(page[start:end])
        self.assertEqual(len(data), 2)
        self.assertEqual(data[1][0], "a2")

    def test_html_escapes_filenames(self):
        page = cluster_review_app.render_page(
            [row(name='<script>alert(1)</script>.jpg')])
        self.assertNotIn("<script>alert", page)


class TestMainSkipImages(unittest.TestCase):
    def test_skip_images_writes_page_without_network(self):
        import shutil
        tmp = tempfile.mkdtemp()
        try:
            data = os.path.join(tmp, "proposals.csv")
            import csv
            with open(data, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=cluster_review_app.FIELDS)
                w.writeheader()
                w.writerow(row())
            out = os.path.join(tmp, "app")
            sys.argv = ["cluster_review_app.py", "--data", data,
                        "--out", out, "--skip-images"]
            cluster_review_app.main()
            self.assertTrue(os.path.exists(os.path.join(out, "index.html")))
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for cluster_fill: confirmation rules, parsing, ledger, dry-run.

Fake-payload style: no network, no Immich, no real files outside tmpdir.
Run: python3 test_cluster_fill.py
"""
import csv
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cluster_fill


def match(aid="a1", name="f1", mid="m1", mname="m", sim=0.95,
          lat=45.0, lon=9.0):
    return {"asset_id": aid, "file_name": name, "match_id": mid,
            "match_name": mname, "similarity": sim,
            "latitude": lat, "longitude": lon}


class TestHaversine(unittest.TestCase):
    def test_zero_distance(self):
        self.assertEqual(cluster_fill.haversine_m(45.0, 9.0, 45.0, 9.0), 0.0)

    def test_known_distance(self):
        # ~111 km per degree of latitude
        d = cluster_fill.haversine_m(45.0, 9.0, 46.0, 9.0)
        self.assertAlmostEqual(d, 111_000, delta=1_000)


class TestConfirm(unittest.TestCase):
    def test_two_agreeing_matches_are_confident(self):
        m1 = match(sim=0.93, lat=45.0, lon=9.0)
        m2 = match(mid="m2", sim=0.91, lat=45.0005, lon=9.0005)  # ~70 m apart
        status, best, n, spread = cluster_fill.confirm([m1, m2])
        self.assertEqual(status, "confident")
        self.assertEqual(best["match_id"], "m1")
        self.assertEqual(n, 2)
        self.assertLessEqual(spread, 100)

    def test_disagreeing_matches_go_to_review(self):
        m1 = match(sim=0.93, lat=45.0, lon=9.0)
        m2 = match(mid="m2", sim=0.92, lat=46.0, lon=10.0)  # far away
        status, best, n, _ = cluster_fill.confirm([m1, m2])
        self.assertEqual(status, "review")


class TestConfirmTimeWindow(unittest.TestCase):
    def test_time_close_proposal_stays_review(self):
        m = match(sim=0.93)
        m["match_taken_at"] = 1000.0
        status, _, _, _ = cluster_fill.confirm([m], taken_at=1000.0,
                                               max_dt=600.0)
        self.assertEqual(status, "review")

    def test_time_far_proposal_is_rejected(self):
        m = match(sim=0.93)
        m["match_taken_at"] = 1000.0
        status, _, _, _ = cluster_fill.confirm([m], taken_at=1000.0 + 7200,
                                               max_dt=600.0)
        self.assertEqual(status, "rejected")

    def test_missing_timestamps_stay_review(self):
        m = match(sim=0.93)  # no match_taken_at
        status, _, _, _ = cluster_fill.confirm([m], taken_at=None,
                                               max_dt=600.0)
        self.assertEqual(status, "review")
        m2 = match(sim=0.93)
        m2["match_taken_at"] = 1000.0
        status, _, _, _ = cluster_fill.confirm([m2], taken_at=None,
                                               max_dt=600.0)
        self.assertEqual(status, "review")

    def test_confident_ignores_time_window(self):
        m1 = match(sim=0.95, lat=45.0, lon=9.0)
        m1["match_taken_at"] = 1000.0
        m2 = match(mid="m2", sim=0.91, lat=45.0005, lon=9.0005)
        m2["match_taken_at"] = 1000.0 + 99999  # years apart, still confident
        status, _, n, _ = cluster_fill.confirm([m1, m2], taken_at=5000.0)
        self.assertEqual(status, "confident")
        self.assertEqual(n, 2)  # both agree spatially; time is irrelevant

    def test_single_very_high_match_is_confident(self):
        status, best, n, _ = cluster_fill.confirm([match(sim=0.97)])
        self.assertEqual(status, "confident")
        self.assertEqual(n, 1)

    def test_single_mid_match_goes_to_review(self):
        status, _, _, _ = cluster_fill.confirm([match(sim=0.93)])
        self.assertEqual(status, "review")

    def test_low_matches_go_to_review(self):
        m1 = match(sim=0.87, lat=45.0, lon=9.0)
        m2 = match(mid="m2", sim=0.86, lat=45.0, lon=9.0)
        status, best, n, _ = cluster_fill.confirm([m1, m2])
        self.assertEqual(status, "review")
        self.assertEqual(n, 0)  # below min_sim, nothing counts


class TestParseMatches(unittest.TestCase):
    def test_parse_groups_and_types(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                         delete=False) as f:
            w = csv.writer(f)
            w.writerow(["a1", "one.jpg", "1000", "m1", "m.jpg", "995",
                        "0.9500", "45.1000", "9.1000"])
            w.writerow(["a1", "one.jpg", "1000", "m2", "m2.jpg", "990",
                        "0.9100", "45.2000", "9.2000"])
            w.writerow(["a2", "two.jpg", "1000", "m1", "m.jpg", "870",
                        "0.8700", "45.1000", "9.1000"])
            path = f.name
        try:
            groups = cluster_fill.parse_matches(path)
            self.assertEqual(set(groups), {"a1", "a2"})
            self.assertEqual(len(groups["a1"]["matches"]), 2)
            m = groups["a1"]["matches"][0]
            self.assertIsInstance(m["similarity"], float)
            self.assertIsInstance(m["latitude"], float)
        finally:
            os.unlink(path)

    def test_malformed_rows_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                         delete=False) as f:
            f.write("a1,one.jpg,m1\n")  # too few columns
            f.write("a1,one.jpg,,m1,m.jpg,,notafloat,45.0,9.0\n")
            path = f.name
        try:
            self.assertEqual(cluster_fill.parse_matches(path), {})
        finally:
            os.unlink(path)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._paths = (cluster_fill.DATA, cluster_fill.LEDGER,
                       cluster_fill.EDITS, cluster_fill.REVIEW)
        cluster_fill.DATA = os.path.join(self.tmp, "cluster")
        cluster_fill.LEDGER = os.path.join(cluster_fill.DATA, "proposals.csv")
        cluster_fill.EDITS = os.path.join(cluster_fill.DATA, "edits.csv")
        cluster_fill.REVIEW = os.path.join(self.tmp, "review")
        self.matches = os.path.join(self.tmp, "matches.csv")
        with open(self.matches, "w") as f:
            w = csv.writer(f)
            # confident: two agreeing
            w.writerow(["a1", "one.jpg", "1000", "m1", "m.jpg", "995",
                        "0.9500", "45.1000", "9.1000"])
            w.writerow(["a1", "one.jpg", "1000", "m2", "m2.jpg", "990",
                        "0.9200", "45.1005", "9.1005"])
            # review: single mid match, taken 5 s apart
            w.writerow(["a2", "two.jpg", "1000", "m1", "m.jpg", "995",
                        "0.9000", "45.1000", "9.1000"])

    def tearDown(self):
        (cluster_fill.DATA, cluster_fill.LEDGER, cluster_fill.EDITS,
         cluster_fill.REVIEW) = self._paths
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_writes_nothing(self):
        cluster_fill.run(self.matches, dry_run=True)
        self.assertFalse(os.path.exists(cluster_fill.LEDGER))
        self.assertFalse(os.path.exists(cluster_fill.EDITS))

    def test_ledger_and_edits_written(self):
        applied = []

        def fake_set_location(self, asset_id, lat, lon):
            applied.append(asset_id)

        orig = cluster_fill.geotag.Immich.set_location
        cluster_fill.geotag.Immich.set_location = fake_set_location
        try:
            os.environ["IMMICH_URL"] = "http://test"
            os.environ["IMMICH_API_KEY"] = "k"
            cluster_fill.run(self.matches)
        finally:
            cluster_fill.geotag.Immich.set_location = orig

        self.assertEqual(applied, ["a1"])
        with open(cluster_fill.LEDGER) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        by_id = {r["asset_id"]: r for r in rows}
        self.assertEqual(by_id["a1"]["status"], "applied")
        self.assertEqual(by_id["a2"]["status"], "review")
        with open(cluster_fill.EDITS) as f:
            edits = list(csv.DictReader(f))
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["asset_id"], "a1")
        self.assertEqual(edits[0]["match_id"], "m1")

    def test_second_run_skips_processed(self):
        def fake_set_location(self, asset_id, lat, lon):
            pass

        orig = cluster_fill.geotag.Immich.set_location
        cluster_fill.geotag.Immich.set_location = fake_set_location
        try:
            os.environ["IMMICH_URL"] = "http://test"
            os.environ["IMMICH_API_KEY"] = "k"
            cluster_fill.run(self.matches)
            cluster_fill.run(self.matches)
        finally:
            cluster_fill.geotag.Immich.set_location = orig
        with open(cluster_fill.LEDGER) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)  # no duplicates


class TestApplyApproved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._paths = (cluster_fill.DATA, cluster_fill.LEDGER,
                       cluster_fill.EDITS, cluster_fill.REVIEW)
        cluster_fill.DATA = os.path.join(self.tmp, "cluster")
        cluster_fill.LEDGER = os.path.join(self.tmp, "proposals.csv")
        cluster_fill.EDITS = os.path.join(self.tmp, "edits.csv")
        cluster_fill.REVIEW = os.path.join(self.tmp, "review")
        os.makedirs(cluster_fill.DATA)
        with open(cluster_fill.LEDGER, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cluster_fill.LEDGER_COLUMNS)
            w.writeheader()
            w.writerow({"asset_id": "a1", "file_name": "one.jpg",
                        "latitude": "45.1", "longitude": "9.1",
                        "similarity": "0.9300", "match_id": "m1",
                        "match_name": "m.jpg", "n_matches": "1",
                        "spread_m": "0", "status": "review",
                        "maps_url": "u", "immich_url": "u",
                        "run_date": "d"})
            w.writerow({"asset_id": "a2", "file_name": "two.jpg",
                        "latitude": "45.2", "longitude": "9.2",
                        "similarity": "0.9500", "match_id": "m2",
                        "match_name": "m2.jpg", "n_matches": "1",
                        "spread_m": "0", "status": "applied",
                        "maps_url": "u", "immich_url": "u",
                        "run_date": "d"})

    def tearDown(self):
        (cluster_fill.DATA, cluster_fill.LEDGER, cluster_fill.EDITS,
         cluster_fill.REVIEW) = self._paths
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_applies_review_rows_only(self):
        applied = []

        def fake_set_location(self, asset_id, lat, lon):
            applied.append((asset_id, lat, lon))

        orig = cluster_fill.geotag.Immich.set_location
        cluster_fill.geotag.Immich.set_location = fake_set_location
        try:
            os.environ["IMMICH_URL"] = "http://test"
            os.environ["IMMICH_API_KEY"] = "k"
            cluster_fill.apply_approved()
        finally:
            cluster_fill.geotag.Immich.set_location = orig

        self.assertEqual(applied, [("a1", 45.1, 9.1)])
        with open(cluster_fill.LEDGER) as f:
            rows = {r["asset_id"]: r for r in csv.DictReader(f)}
        self.assertEqual(rows["a1"]["status"], "applied")
        self.assertEqual(rows["a2"]["status"], "applied")  # untouched
        with open(cluster_fill.EDITS) as f:
            edits = list(csv.DictReader(f))
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["asset_id"], "a1")

    def test_idempotent(self):
        def fake_set_location(self, asset_id, lat, lon):
            pass

        orig = cluster_fill.geotag.Immich.set_location
        cluster_fill.geotag.Immich.set_location = fake_set_location
        try:
            os.environ["IMMICH_URL"] = "http://test"
            os.environ["IMMICH_API_KEY"] = "k"
            cluster_fill.apply_approved()
            cluster_fill.apply_approved()
        finally:
            cluster_fill.geotag.Immich.set_location = orig
        with open(cluster_fill.EDITS) as f:
            self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_dry_run_writes_nothing(self):
        os.environ["IMMICH_URL"] = "http://test"
        os.environ["IMMICH_API_KEY"] = "k"
        cluster_fill.apply_approved(dry_run=True)
        self.assertFalse(os.path.exists(cluster_fill.EDITS))
        with open(cluster_fill.LEDGER) as f:
            rows = {r["asset_id"]: r for r in csv.DictReader(f)}
        self.assertEqual(rows["a1"]["status"], "review")


if __name__ == "__main__":
    unittest.main(verbosity=2)

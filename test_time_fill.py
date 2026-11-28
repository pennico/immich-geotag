#!/usr/bin/env python3
"""Tests for time_fill: parsing, proposals, ledger merge, apply flow.

Fake-payload style: no network, no Immich, no real files outside tmpdir.
Run: python3 test_time_fill.py
"""
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import geotag
import time_fill
import time_fill_review_app


def asset(aid="a1", name="f1.jpg", epoch=1000.0, lat=None, lon=None,
          taken_raw="2024-01-01T10:00:00.000Z"):
    return (aid, name, epoch, lat, lon, taken_raw)


class TestParseEpoch(unittest.TestCase):
    def test_iso_z(self):
        self.assertEqual(
            time_fill.parse_epoch("2024-01-01T10:00:00.000Z"),
            datetime(2024, 1, 1, 10, tzinfo=timezone.utc).timestamp())

    def test_space_format(self):
        self.assertEqual(
            time_fill.parse_epoch("2024-01-01 10:00:00"),
            datetime(2024, 1, 1, 10, tzinfo=timezone.utc).timestamp())

    def test_offset(self):
        self.assertEqual(
            time_fill.parse_epoch("2024-01-01T11:00:00+01:00"),
            datetime(2024, 1, 1, 10, tzinfo=timezone.utc).timestamp())

    def test_garbage_and_empty(self):
        self.assertIsNone(time_fill.parse_epoch("not a date"))
        self.assertIsNone(time_fill.parse_epoch(""))
        self.assertIsNone(time_fill.parse_epoch(None))


class TestBuildProposals(unittest.TestCase):
    def test_nearest_in_time_wins(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="m2", epoch=1100.0, lat=46.0, lon=10.0),
            asset(aid="x1", epoch=1090.0),  # 90 s from m1, 10 s from m2
        ]
        proposals, stats = time_fill.build_proposals(assets, window_s=600.0)
        self.assertEqual(stats["proposals"], 1)
        p = proposals[0]
        self.assertEqual(p["asset_id"], "x1")
        self.assertEqual(p["neighbor_id"], "m2")  # closest in time
        self.assertEqual(p["delta_seconds"], 10.0)
        self.assertEqual(p["n_neighbors"], 2)

    def test_window_excludes_far_photos(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=1601.0),  # 601 s away: outside 10 min
        ]
        proposals, stats = time_fill.build_proposals(assets, window_s=600.0)
        self.assertEqual(proposals, [])
        self.assertEqual(stats["no_neighbor"], 1)

    def test_boundary_is_inclusive(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=1600.0),  # exactly 600 s
        ]
        proposals, _ = time_fill.build_proposals(assets, window_s=600.0)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["delta_seconds"], 600.0)

    def test_missing_timestamp_not_proposed(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=None, taken_raw=""),
        ]
        proposals, stats = time_fill.build_proposals(assets)
        self.assertEqual(proposals, [])
        self.assertEqual(stats["no_time"], 1)

    def test_located_photo_without_timestamp_ignored(self):
        assets = [
            asset(aid="m1", epoch=None, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=1000.0),
        ]
        proposals, stats = time_fill.build_proposals(assets)
        self.assertEqual(proposals, [])
        self.assertEqual(stats["no_neighbor"], 1)

    def test_spread_zero_for_single_neighbor(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=1005.0),
        ]
        proposals, _ = time_fill.build_proposals(assets)
        self.assertEqual(proposals[0]["spread_m"], 0.0)

    def test_partial_gps_counts_as_missing(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=1005.0, lat=None, lon=9.0),
        ]
        proposals, stats = time_fill.build_proposals(assets)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(len(proposals), 1)


class TestLedgerAndApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "proposals.csv")
        self.edits = os.path.join(self.tmp.name, "edits.csv")
        time_fill.DATA = self.tmp.name
        time_fill.LEDGER = self.ledger
        time_fill.EDITS = self.edits

    def _write_ledger(self, rows):
        geotag.write_csv(self.ledger, rows, time_fill.LEDGER_COLUMNS)

    def test_apply_approved_writes_and_logs(self):
        row = {col: "" for col in time_fill.LEDGER_COLUMNS}
        row.update({"asset_id": "x1", "file_name": "f1.jpg",
                    "latitude": "45.0", "longitude": "9.0",
                    "neighbor_id": "m1", "neighbor_name": "m.jpg",
                    "delta_seconds": "30", "status": "review"})
        self._write_ledger([row])

        calls = []

        class FakeImmich:
            def set_location(self, asset_id, lat, lon):
                calls.append((asset_id, lat, lon))

        original = geotag.Immich
        geotag.Immich = lambda url, key: FakeImmich()
        try:
            with unittest.mock.patch.dict(
                    os.environ, {"IMMICH_URL": "http://x",
                                 "IMMICH_API_KEY": "k"}):
                time_fill.apply_approved(dry_run=False)
        finally:
            geotag.Immich = original

        self.assertEqual(calls, [("x1", 45.0, 9.0)])
        rows = geotag.read_csv(self.ledger)
        self.assertEqual(rows[0]["status"], "applied")
        edits = geotag.read_csv(self.edits)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["asset_id"], "x1")

        # idempotent: nothing left in review
        calls.clear()
        original = geotag.Immich
        geotag.Immich = lambda url, key: FakeImmich()
        try:
            with unittest.mock.patch.dict(
                    os.environ, {"IMMICH_URL": "http://x",
                                 "IMMICH_API_KEY": "k"}):
                time_fill.apply_approved(dry_run=False)
        finally:
            geotag.Immich = original
        self.assertEqual(calls, [])

    def test_apply_dry_run_touches_nothing(self):
        row = {col: "" for col in time_fill.LEDGER_COLUMNS}
        row.update({"asset_id": "x1", "latitude": "45.0",
                    "longitude": "9.0", "status": "review"})
        self._write_ledger([row])
        with unittest.mock.patch.dict(
                os.environ, {"IMMICH_URL": "http://x",
                             "IMMICH_API_KEY": "k"}):
            time_fill.apply_approved(dry_run=True)
        rows = geotag.read_csv(self.ledger)
        self.assertEqual(rows[0]["status"], "review")
        self.assertFalse(os.path.exists(self.edits))

    def test_apply_respects_max_spread(self):
        def row(aid, spread):
            r = {col: "" for col in time_fill.LEDGER_COLUMNS}
            r.update({"asset_id": aid, "latitude": "45.0",
                      "longitude": "9.0", "spread_m": spread,
                      "status": "review"})
            return r
        self._write_ledger([row("tight", "50"), row("loose", "5000")])

        calls = []

        class FakeImmich:
            def set_location(self, asset_id, lat, lon):
                calls.append(asset_id)

        geotag.Immich = lambda url, key: FakeImmich()
        with unittest.mock.patch.dict(
                os.environ, {"IMMICH_URL": "http://x",
                             "IMMICH_API_KEY": "k"}):
            time_fill.apply_approved(dry_run=False, max_spread_m=100.0)

        self.assertEqual(calls, ["tight"])
        rows = geotag.read_csv(self.ledger)
        by_id = {r["asset_id"]: r for r in rows}
        self.assertEqual(by_id["tight"]["status"], "applied")
        self.assertEqual(by_id["loose"]["status"], "review")


class TestBuildClusters(unittest.TestCase):
    @staticmethod
    def ledger_row(aid, name="f.jpg", status="review", neighbor="n.jpg"):
        return {"asset_id": aid, "file_name": name, "status": status,
                "neighbor_name": neighbor}

    def test_gap_splits_clusters(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=1005.0),
            asset(aid="m2", epoch=5000.0, lat=46.0, lon=10.0),  # >10 min away
            asset(aid="x2", epoch=5005.0),
        ]
        ledger = [self.ledger_row("x1"), self.ledger_row("x2")]
        clusters = time_fill_review_app.build_clusters(assets, ledger)
        self.assertEqual(len(clusters), 2)
        self.assertEqual([c["cluster_id"] for c in clusters],
                         ["C001", "C002"])
        self.assertEqual(clusters[0]["geotag_lat"], 45.0)

    def test_whatsapp_excluded_both_sides(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=1005.0),  # pending, clean
            asset(aid="x2", name="IMG-1-WA0001.jpg", epoch=1006.0),
            asset(aid="m2", name="IMG-2-WA0002.jpg", epoch=1007.0,
                  lat=46.0, lon=10.0),
        ]
        ledger = [self.ledger_row("x1"), self.ledger_row("x2"),
                  self.ledger_row("x3", neighbor="IMG-2-WA0002.jpg")]
        clusters = time_fill_review_app.build_clusters(assets, ledger)
        self.assertEqual(len(clusters), 1)
        members = clusters[0]["members"]
        self.assertEqual({m["asset_id"] for m in members}, {"m1", "x1"})

    def test_cluster_without_pending_dropped(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="m2", epoch=1010.0, lat=45.0, lon=9.0),
        ]
        clusters = time_fill_review_app.build_clusters(assets, [])
        self.assertEqual(clusters, [])

    def test_donor_is_nearest_to_median(self):
        assets = [
            asset(aid="mA", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="mB", epoch=1100.0, lat=46.0, lon=10.0),
            asset(aid="x1", epoch=1095.0),  # 95 s from mA, 5 s from mB
        ]
        ledger = [self.ledger_row("x1")]
        clusters = time_fill_review_app.build_clusters(assets, ledger)
        self.assertEqual(clusters[0]["donor_id"], "mB")
        self.assertEqual(clusters[0]["geotag_lat"], 46.0)
        self.assertAlmostEqual(clusters[0]["spread_m"],  # ~130 km
                               130_000, delta=10_000)

    def test_applied_rows_not_pending(self):
        assets = [
            asset(aid="m1", epoch=1000.0, lat=45.0, lon=9.0),
            asset(aid="x1", epoch=1005.0),
        ]
        ledger = [self.ledger_row("x1", status="applied")]
        clusters = time_fill_review_app.build_clusters(assets, ledger)
        self.assertEqual(clusters, [])


class TestApplyClusters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "proposals.csv")
        self.edits = os.path.join(self.tmp.name, "edits.csv")
        self.clusters = os.path.join(self.tmp.name, "clusters.csv")
        time_fill.DATA = self.tmp.name
        time_fill.LEDGER = self.ledger
        time_fill.EDITS = self.edits
        time_fill.CLUSTERS = self.clusters

    def _write(self):
        def row(aid, name, status):
            r = {col: "" for col in time_fill.LEDGER_COLUMNS}
            r.update({"asset_id": aid, "file_name": name,
                      "latitude": "0", "longitude": "0",
                      "status": status})
            return r
        geotag.write_csv(self.ledger, [
            row("x1", "a.jpg", "review"), row("x2", "b.jpg", "review"),
            row("x3", "c.jpg", "applied"),
        ], time_fill.LEDGER_COLUMNS)
        rows = []
        for cid, aid, role in [("C001", "x1", "missing"),
                               ("C001", "x2", "missing"),
                               ("C001", "x3", "missing"),
                               ("C001", "m1", "located"),
                               ("C002", "x9", "missing")]:
            rows.append({"cluster_id": cid, "asset_id": aid, "role": role,
                         "file_name": aid + ".jpg", "taken_at": "t",
                         "latitude": "", "longitude": "",
                         "donor_id": "m1", "geotag_lat": "45.5",
                         "geotag_lon": "9.5", "spread_m": "10"})
        geotag.write_csv(self.clusters, rows, time_fill.CLUSTER_COLUMNS)

    def test_apply_clusters_writes_geotag_and_skips_applied(self):
        self._write()
        calls = []

        class FakeImmich:
            def set_location(self, asset_id, lat, lon):
                calls.append((asset_id, lat, lon))

        geotag.Immich = lambda url, key: FakeImmich()
        with unittest.mock.patch.dict(
                os.environ, {"IMMICH_URL": "http://x",
                              "IMMICH_API_KEY": "k"}):
            total, per = time_fill.apply_clusters(["C001"],
                                                  clusters_path=self.clusters)
        # x3 is already applied in the ledger -> skipped
        self.assertEqual(sorted(calls), [("x1", 45.5, 9.5),
                                         ("x2", 45.5, 9.5)])
        self.assertEqual(total, 2)
        self.assertEqual(per, {"C001": 2})
        rows = geotag.read_csv(self.ledger)
        by_id = {r["asset_id"]: r for r in rows}
        self.assertEqual(by_id["x1"]["status"], "applied")
        self.assertEqual(by_id["x2"]["status"], "applied")
        self.assertEqual(by_id["x3"]["status"], "applied")
        edits = geotag.read_csv(self.edits)
        self.assertEqual(len(edits), 2)
        self.assertEqual(edits[0]["neighbor_id"], "C001")
        self.assertEqual(edits[0]["neighbor_name"], "time-cluster")

        # idempotent
        calls.clear()
        geotag.Immich = lambda url, key: FakeImmich()
        with unittest.mock.patch.dict(
                os.environ, {"IMMICH_URL": "http://x",
                              "IMMICH_API_KEY": "k"}):
            time_fill.apply_clusters(["C001"], clusters_path=self.clusters)
        self.assertEqual(calls, [])

    def test_unknown_cluster_raises(self):
        self._write()
        with self.assertRaises(SystemExit):
            time_fill.apply_clusters(["C999"], clusters_path=self.clusters)

    def test_mixed_spread_cluster_refused(self):
        self._write()
        rows = geotag.read_csv(self.clusters)
        for r in rows:
            if r["cluster_id"] == "C002":
                r["spread_m"] = "50000"  # donors 50 km apart
        geotag.write_csv(self.clusters, rows, time_fill.CLUSTER_COLUMNS)
        with self.assertRaises(SystemExit) as ctx:
            time_fill.apply_clusters(["C002"], clusters_path=self.clusters,
                                     dry_run=True)
        self.assertIn("mixed", str(ctx.exception))

    def test_tight_cluster_still_applies(self):
        self._write()
        total, per = time_fill.apply_clusters(["C002"],
                                              clusters_path=self.clusters,
                                              dry_run=True)
        self.assertEqual(total, 1)


if __name__ == "__main__":
    unittest.main()

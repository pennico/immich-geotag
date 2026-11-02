#!/usr/bin/env python3
"""Tests for similar_fill: location grouping, assignment, clusters.

Fake-data style: no network, no Immich, no real files outside tmpdir.
Run: python3 test_similar_fill.py
"""
import csv
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cluster_fill
import geotag
import similar_fill
import time_fill


def m_row(asset_id, match_id, sim, lat, lon, match_name="d.jpg",
          asset_name="a.jpg", taken_at=1000, match_taken_at=900):
    return [asset_id, asset_name, taken_at, match_id, match_name,
            match_taken_at, sim, lat, lon]


def write_matches(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


class TestAssign(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.matches = os.path.join(self.tmp.name, "matches.csv")

    def _run(self, rows, **kw):
        write_matches(self.matches, rows)
        groups = __import__("cluster_fill").parse_matches(self.matches)
        return similar_fill.assign(groups, **kw)

    def test_two_agreeing_matches_assign(self):
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.91, 45.0, 9.0),
            m_row("a1", "d2", 0.90, 45.001, 9.001),  # ~150 m from d1
        ])
        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(a["a1"]["match"]["match_id"], "d1")
        self.assertEqual(a["a1"]["n_agree"], 2)

    def test_single_match_below_solo_not_assigned(self):
        a, find, coords, stats = self._run([m_row("a1", "d1", 0.95,
                                                  45.0, 9.0)])
        self.assertEqual(stats["assigned"], 0)
        self.assertEqual(stats["disagree"], 1)

    def test_single_match_at_solo_assigned(self):
        a, find, coords, stats = self._run([m_row("a1", "d1", 0.96,
                                                  45.0, 9.0)])
        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(a["a1"]["n_agree"], 1)

    def test_matches_at_different_locations_disagree(self):
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.93, 45.0, 9.0),
            m_row("a1", "d2", 0.92, 45.9, 9.0),  # ~100 km away
        ])
        self.assertEqual(stats["assigned"], 0)
        self.assertEqual(stats["disagree"], 1)

    def test_below_min_sim_ignored(self):
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.85, 45.0, 9.0),
            m_row("a1", "d2", 0.84, 45.001, 9.001),
        ])
        self.assertEqual(stats["no_candidates"], 1)
        self.assertEqual(stats["assigned"], 0)

    def test_location_groups_chain(self):
        # d1-d2 ~555 m, d2-d3 ~555 m, d1-d3 ~1110 m: single linkage
        # joins all three into one location group.
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.95, 45.0, 9.0),
            m_row("a1", "d2", 0.94, 45.005, 9.0),
            m_row("a1", "d3", 0.93, 45.01, 9.0),
        ])
        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(a["a1"]["n_agree"], 3)

    def test_complete_linkage_does_not_chain(self):
        # same chain as above: with complete linkage at 300 m, d3
        # (445 m from d1) cannot join the d1+d2 group.
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.95, 45.0, 9.0),
            m_row("a1", "d2", 0.94, 45.002, 9.0),
            m_row("a1", "d3", 0.93, 45.004, 9.0),
        ], loc_group_m=300.0, complete=True)
        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(a["a1"]["n_agree"], 2)  # d3 is its own group
        self.assertNotEqual(find("d3"), find("d1"))

    def test_max_dt_filters_matches(self):
        # a1 was taken at 1000; d1 at 1300 (5 min later, inside),
        # d2 at 2000 (far outside the 10 min window). d1 alone must
        # still clear the solo bar to assign.
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.96, 45.0, 9.0, match_taken_at=1300),
            m_row("a1", "d2", 0.94, 45.001, 9.0, match_taken_at=2000),
        ], max_dt_s=600.0)
        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(a["a1"]["n_agree"], 1)
        self.assertEqual(a["a1"]["match"]["match_id"], "d1")

    def test_max_dt_without_timestamp_drops_photo(self):
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.95, 45.0, 9.0, taken_at=""),
        ], max_dt_s=600.0)
        self.assertEqual(stats["outside_window"], 1)
        self.assertEqual(stats["assigned"], 0)

    def test_same_day_allows_far_match_on_one_day(self):
        # a1 at epoch 1000 (1970-01-01 UTC), d1 at 50000: almost 14
        # hours later, far outside 10 minutes but the same UTC day.
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.96, 45.0, 9.0, match_taken_at=50000),
        ], same_day=True)
        self.assertEqual(stats["outside_window"], 0)
        self.assertEqual(stats["assigned"], 1)
        self.assertEqual(a["a1"]["match"]["match_id"], "d1")

    def test_same_day_rejects_match_on_next_day(self):
        # d1 at 90000 is 1970-01-02 UTC: a different calendar day.
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.96, 45.0, 9.0, match_taken_at=90000),
        ], same_day=True)
        self.assertEqual(stats["outside_window"], 1)
        self.assertEqual(stats["assigned"], 0)

    def test_same_day_without_timestamp_drops_photo(self):
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.95, 45.0, 9.0, taken_at=""),
        ], same_day=True)
        self.assertEqual(stats["outside_window"], 1)
        self.assertEqual(stats["assigned"], 0)


class TestAssignWA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.matches = os.path.join(self.tmp.name, "matches.csv")

    def _run(self, rows, **kw):
        write_matches(self.matches, rows)
        groups = cluster_fill.parse_matches(self.matches)
        return similar_fill.assign_wa(groups, **kw)

    def _wa(self, asset_id, match_id, sim, lat, lon, **kw):
        kw.setdefault("asset_name", "IMG-20200101-WA0001.jpg")
        return m_row(asset_id, match_id, sim, lat, lon, **kw)

    def test_batch_pools_member_matches(self):
        # w1 and w2 share the send timestamp (epoch 1000); each has
        # one match, together they back one location.
        a, find, coords, stats = self._run([
            self._wa("w1", "d1", 0.75, 45.0, 9.0, match_taken_at=1000),
            self._wa("w2", "d2", 0.72, 45.001, 9.0,
                     asset_name="IMG-20200101-WA0002.jpg",
                     match_taken_at=1100),
        ])
        self.assertEqual(stats["assigned"], 2)
        self.assertEqual(a["w1"]["n_agree"], 2)
        self.assertEqual(a["w1"]["match"]["match_id"], "d1")
        self.assertEqual(a["w2"]["match"]["match_id"], "d1")

    def test_match_on_a_different_day_is_excluded(self):
        a, find, coords, stats = self._run([
            self._wa("w1", "d1", 0.75, 45.0, 9.0, match_taken_at=90000),
            self._wa("w2", "d2", 0.72, 45.0, 9.0,
                     asset_name="IMG-20200101-WA0002.jpg",
                     match_taken_at=90000),
        ])
        self.assertEqual(stats["outside_window"], 2)
        self.assertEqual(stats["assigned"], 0)

    def test_disagreeing_batch_is_skipped(self):
        a, find, coords, stats = self._run([
            self._wa("w1", "d1", 0.75, 45.0, 9.0, match_taken_at=1000),
            self._wa("w2", "d2", 0.72, 46.0, 9.0,
                     asset_name="IMG-20200101-WA0002.jpg",
                     match_taken_at=1000),
        ])
        self.assertEqual(stats["disagree"], 2)
        self.assertEqual(stats["assigned"], 0)

    def test_solo_whatsapp_photo_left_to_regular_runs(self):
        a, find, coords, stats = self._run([
            self._wa("w1", "d1", 0.75, 45.0, 9.0, match_taken_at=1000),
        ])
        self.assertEqual(stats["solo"], 1)
        self.assertEqual(stats["assigned"], 0)

    def test_photo_without_timestamp_skipped(self):
        a, find, coords, stats = self._run([
            self._wa("w1", "d1", 0.75, 45.0, 9.0, taken_at=""),
            self._wa("w2", "d2", 0.72, 45.0, 9.0,
                     asset_name="IMG-20200101-WA0002.jpg", taken_at=""),
        ])
        self.assertEqual(stats["no_timestamp"], 2)
        self.assertEqual(stats["assigned"], 0)

    def test_non_whatsapp_photos_ignored(self):
        a, find, coords, stats = self._run([
            m_row("a1", "d1", 0.95, 45.0, 9.0),
        ])
        self.assertEqual(stats["unlocated_wa"], 0)
        self.assertEqual(stats["assigned"], 0)


class TestBuildClusters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.matches = os.path.join(self.tmp.name, "matches.csv")

    def _clusters(self, rows):
        write_matches(self.matches, rows)
        import cluster_fill
        groups = cluster_fill.parse_matches(self.matches)
        a, find, coords, stats = similar_fill.assign(groups)
        return similar_fill.build_clusters(groups, a, find, coords), stats

    def test_different_locations_are_different_clusters(self):
        clusters, stats = self._clusters([
            m_row("a1", "d1", 0.95, 45.0, 9.0),
            m_row("a1", "d2", 0.94, 45.001, 9.0),
            m_row("a2", "d3", 0.95, 41.0, 12.0),
            m_row("a2", "d4", 0.94, 41.001, 12.0),
        ])
        self.assertEqual(len(clusters), 2)
        geotags = {(round(c["geotag_lat"], 4), round(c["geotag_lon"], 4))
                   for c in clusters}
        self.assertEqual(geotags, {(45.0, 9.0), (41.0, 12.0)})
        for c in clusters:
            missing = [m for m in c["members"] if m["role"] == "missing"]
            self.assertEqual(len(missing), 1)
            # every member sits at the cluster's own location
            for m in c["members"]:
                if m["role"] == "located":
                    self.assertLessEqual(cluster_fill.haversine_m(
                        m["lat"], m["lon"],
                        c["geotag_lat"], c["geotag_lon"]), 1000.0)

    def test_members_and_ids(self):
        clusters, stats = self._clusters([
            m_row("a1", "d1", 0.95, 45.0, 9.0),
            m_row("a1", "d2", 0.94, 45.001, 9.0),
            m_row("a2", "d1", 0.95, 45.0, 9.0),
            m_row("a2", "d2", 0.93, 45.001, 9.0),
        ])
        self.assertEqual(len(clusters), 1)
        c = clusters[0]
        self.assertTrue(c["cluster_id"].startswith("S"))
        roles = sorted(m["role"] for m in c["members"])
        self.assertEqual(roles, ["located", "located", "missing", "missing"])
        self.assertEqual(c["donor_id"], "d1")
        # d1 and d2 are ~150 m apart -> spread ~150 m
        self.assertGreater(c["spread_m"], 100.0)

    def test_write_and_read_roundtrip(self):
        clusters, stats = self._clusters([
            m_row("a1", "d1", 0.95, 45.0, 9.0),
            m_row("a1", "d2", 0.94, 45.001, 9.0),
        ])
        path = os.path.join(self.tmp.name, "clusters.csv")
        similar_fill.write_clusters(clusters, path)
        groups = time_fill.read_clusters(path)
        self.assertIn(clusters[0]["cluster_id"], groups)
        g = groups[clusters[0]["cluster_id"]]
        self.assertEqual(g["donor_id"], "d1")
        self.assertEqual(len(g["members"]), 3)


class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.matches = os.path.join(self.tmp.name, "matches.csv")
        self.clusters = os.path.join(self.tmp.name, "clusters.csv")
        self.ledger = os.path.join(self.tmp.name, "proposals.csv")
        time_fill.LEDGER = self.ledger

    def test_run_excludes_applied_photos(self):
        write_matches(self.matches, [
            m_row("a1", "d1", 0.95, 45.0, 9.0),
            m_row("a1", "d2", 0.94, 45.001, 9.0),
            m_row("a2", "d1", 0.95, 45.0, 9.0),
            m_row("a2", "d2", 0.94, 45.001, 9.0),
        ])
        row = {col: "" for col in time_fill.LEDGER_COLUMNS}
        row.update({"asset_id": "a1", "file_name": "a.jpg",
                    "status": "applied"})
        geotag.write_csv(self.ledger, [row], time_fill.LEDGER_COLUMNS)
        with unittest.mock.patch.object(geotag, "load_env"):
            with unittest.mock.patch.dict(
                    os.environ, {"IMMICH_URL": "http://x"}):
                clusters, stats = similar_fill.run(
                    matches_path=self.matches, clusters_path=self.clusters)
        self.assertEqual(stats["already_applied"], 1)
        ids = {m["asset_id"] for c in clusters for m in c["members"]
               if m["role"] == "missing"}
        self.assertEqual(ids, {"a2"})


class TestRunLoose(unittest.TestCase):
    def test_run_loose_parameters(self):
        with unittest.mock.patch.object(similar_fill, "run") as m:
            similar_fill.run_loose()
        m.assert_called_once_with(
            matches_path=similar_fill.MATCHES_LOOSE, min_sim=0.80,
            solo_sim=0.96, loc_group_m=300.0, max_dt_s=600.0,
            complete=True, clusters_path=similar_fill.LOOSE_CLUSTERS,
            prefix="U")

    def test_run_vloose_parameters(self):
        with unittest.mock.patch.object(similar_fill, "run") as m:
            similar_fill.run_vloose()
        m.assert_called_once_with(
            matches_path=similar_fill.MATCHES_VLOOSE, min_sim=0.70,
            solo_sim=0.96, loc_group_m=300.0, max_dt_s=600.0,
            complete=True, clusters_path=similar_fill.VLOOSE_CLUSTERS,
            prefix="V")

    def test_run_sameday_parameters(self):
        with unittest.mock.patch.object(similar_fill, "run") as m:
            similar_fill.run_sameday()
        m.assert_called_once_with(
            matches_path=similar_fill.MATCHES_VLOOSE, min_sim=0.70,
            solo_sim=0.96, loc_group_m=300.0, same_day=True,
            complete=True, clusters_path=similar_fill.SAMEDAY_CLUSTERS,
            prefix="W")


class TestRunWA(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.matches = os.path.join(self.tmp.name, "matches.csv")
        self.clusters = os.path.join(self.tmp.name, "clusters.csv")
        self.ledger = os.path.join(self.tmp.name, "proposals.csv")
        time_fill.LEDGER = self.ledger

    def test_run_wa_excludes_applied_and_writes_clusters(self):
        write_matches(self.matches, [
            m_row("w1", "d1", 0.75, 45.0, 9.0,
                  asset_name="IMG-20200101-WA0001.jpg", match_taken_at=1000),
            m_row("w2", "d2", 0.72, 45.001, 9.0,
                  asset_name="IMG-20200101-WA0002.jpg",
                  match_taken_at=1100),
            m_row("w3", "d3", 0.90, 45.0, 9.0,
                  asset_name="IMG-20200101-WA0003.jpg", match_taken_at=1000),
        ])
        row = {col: "" for col in time_fill.LEDGER_COLUMNS}
        row.update({"asset_id": "w3", "file_name": "IMG-20200101-WA0003.jpg",
                    "status": "applied"})
        geotag.write_csv(self.ledger, [row], time_fill.LEDGER_COLUMNS)
        clusters, stats = similar_fill.run_wa(
            matches_path=self.matches, clusters_path=self.clusters)
        self.assertEqual(stats["already_applied"], 1)
        ids = {m["asset_id"] for c in clusters for m in c["members"]
               if m["role"] == "missing"}
        self.assertEqual(ids, {"w1", "w2"})
        self.assertTrue(all(c["cluster_id"].startswith("X")
                            for c in clusters))


class TestSyncLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = os.path.join(self.tmp.name, "proposals.csv")
        time_fill.LEDGER = self.ledger

    def test_adds_rows_only_for_unknown_photos(self):
        row = {col: "" for col in time_fill.LEDGER_COLUMNS}
        row.update({"asset_id": "a1", "file_name": "a.jpg",
                    "status": "review"})
        geotag.write_csv(self.ledger, [row], time_fill.LEDGER_COLUMNS)

        cluster = {
            "cluster_id": "S001", "donor_id": "d1",
            "geotag_lat": 45.0, "geotag_lon": 9.0, "spread_m": 150.0,
            "members": [
                {"asset_id": "a1", "role": "missing", "file_name": "a.jpg",
                 "taken_at": "2024-01-01 10:00", "match": {
                     "match_id": "d1", "match_name": "d.jpg"},
                 "n_agree": 2},
                {"asset_id": "a2", "role": "missing", "file_name": "b.jpg",
                 "taken_at": "2024-01-01 11:00", "match": {
                     "match_id": "d1", "match_name": "d.jpg"},
                 "n_agree": 2},
                {"asset_id": "d1", "role": "located", "file_name": "d.jpg",
                 "taken_at": "2024-01-01 09:00"},
            ],
        }
        with unittest.mock.patch.object(geotag, "load_env"):
            with unittest.mock.patch.dict(
                    os.environ, {"IMMICH_URL": "http://x"}):
                added = similar_fill.sync_ledger([cluster])
        self.assertEqual(added, 1)  # a1 already known, a2 added
        rows = geotag.read_csv(self.ledger)
        by_id = {r["asset_id"]: r for r in rows}
        self.assertEqual(len(rows), 2)
        self.assertEqual(by_id["a2"]["status"], "review")
        self.assertEqual(by_id["a2"]["neighbor_id"], "d1")
        self.assertEqual(by_id["a2"]["latitude"], "45.000000")


if __name__ == "__main__":
    unittest.main()

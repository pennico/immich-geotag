"""Tests for monthly_geotag.py argument handling.

The live job must never launch from an unexpected command line: only
no-args, --dry, and --limit N are accepted. Everything else (typos,
--help, --dry-run) must stop before any environment loading or API
call. These tests mock nothing that touches the network because they
never get past argument parsing.
"""
import contextlib
import io
import unittest
from unittest import mock

import monthly_geotag


class ParseArgsTest(unittest.TestCase):
    def test_no_args_runs_live_job(self):
        args = monthly_geotag.parse_args([])
        self.assertFalse(args.dry)
        self.assertIsNone(args.limit)

    def test_dry_flag(self):
        self.assertTrue(monthly_geotag.parse_args(["--dry"]).dry)

    def test_limit_flag(self):
        self.assertEqual(monthly_geotag.parse_args(["--limit", "5"]).limit, 5)

    def test_dry_and_limit_together(self):
        args = monthly_geotag.parse_args(["--dry", "--limit", "3"])
        self.assertTrue(args.dry)
        self.assertEqual(args.limit, 3)

    def test_limit_requires_a_value(self):
        with self.assertRaises(SystemExit) as cm:
            monthly_geotag.parse_args(["--limit"])
        self.assertEqual(cm.exception.code, 2)

    def test_unknown_flag_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            monthly_geotag.parse_args(["--bogus"])
        self.assertEqual(cm.exception.code, 2)

    def test_help_prints_usage_and_exits_zero(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, \
                contextlib.redirect_stdout(out):
            monthly_geotag.parse_args(["--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--dry", out.getvalue())
        self.assertIn("--limit", out.getvalue())


class MainGuardTest(unittest.TestCase):
    """main() must stop at parse errors before any environment work."""

    def test_unknown_flag_stops_before_load_env(self):
        with mock.patch.object(monthly_geotag.geotag, "load_env") as le:
            with self.assertRaises(SystemExit) as cm:
                monthly_geotag.main(["--bogus"])
            le.assert_not_called()
        self.assertEqual(cm.exception.code, 2)

    def test_help_stops_before_load_env(self):
        out = io.StringIO()
        with mock.patch.object(monthly_geotag.geotag, "load_env") as le:
            with self.assertRaises(SystemExit) as cm, \
                    contextlib.redirect_stdout(out):
                monthly_geotag.main(["--help"])
            le.assert_not_called()
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("--dry", out.getvalue())


if __name__ == "__main__":
    unittest.main()

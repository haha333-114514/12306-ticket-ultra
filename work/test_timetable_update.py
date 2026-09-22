"""Regression tests for the once-per-day timetable refresh."""

import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import ticket_server as server


class FakeDate(date):
    current = date(2026, 9, 22)

    @classmethod
    def today(cls):
        return cls.current


class TimetableUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        cache = Path(self.temp.name)
        self.patches = [
            patch.object(server, "GTFS_CACHE", cache),
            patch.object(server, "GTFS_META", cache / "metadata.json"),
            patch.object(server, "GTFS_ZIP", cache / "rail_gtfs.zip"),
            patch.object(server, "date", FakeDate),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        server.TIMETABLE = None
        FakeDate.current = date(2026, 9, 22)

    def test_same_release_is_checked_once_per_day(self):
        release = {"tag_name": server.GTFS_BUNDLED_TAG, "assets": []}
        calls = []

        def fetch(request, timeout):
            calls.append(request.full_url)
            return io.BytesIO(json.dumps(release).encode())

        with patch.object(server, "urlopen", side_effect=fetch):
            first, info = server.rail_timetable()
            second, again = server.rail_timetable()
            self.assertIs(first, second)
            self.assertEqual(len(calls), 1)
            self.assertEqual(info["checkedOn"], "2026-09-22")
            self.assertEqual(again["warning"], "")
            FakeDate.current = date(2026, 9, 23)
            server.rail_timetable()
            self.assertEqual(len(calls), 2)

    def test_new_release_is_cached_after_validation(self):
        latest = "gtfs-20260921-120000"
        url = f"https://github.com/wensimehrp/chinese-railway-gtfs/releases/download/{latest}/output_gtfs.zip"
        release = {"tag_name": latest, "assets": [{"name": "output_gtfs.zip", "browser_download_url": url}]}
        bundled = (server.ROOT / "work" / "data" / "rail_gtfs.zip").read_bytes()
        calls = []

        def fetch(request, timeout):
            calls.append(request.full_url)
            return io.BytesIO(json.dumps(release).encode() if request.full_url == server.GTFS_RELEASE_API else bundled)

        with patch.object(server, "urlopen", side_effect=fetch):
            timetable, info = server.rail_timetable()
            self.assertEqual(timetable.source, server.GTFS_ZIP)
            self.assertEqual(info["version"], "20260921-120000")
            self.assertTrue(server.GTFS_ZIP.is_file())
            server.rail_timetable()
            self.assertEqual(len(calls), 2)

    def test_network_failure_keeps_bundled_data(self):
        with patch.object(server, "urlopen", side_effect=OSError("offline")):
            timetable, info = server.rail_timetable()
        self.assertGreater(len(timetable.trips), 1000)
        self.assertIn("offline", info["warning"])
        self.assertEqual(info["checkedOn"], "")

    def test_invalid_download_does_not_replace_existing_data(self):
        latest = "gtfs-20260921-120000"
        url = f"https://github.com/wensimehrp/chinese-railway-gtfs/releases/download/{latest}/output_gtfs.zip"
        release = {"tag_name": latest, "assets": [{"name": "output_gtfs.zip", "browser_download_url": url}]}

        def fetch(request, timeout):
            return io.BytesIO(json.dumps(release).encode() if request.full_url == server.GTFS_RELEASE_API else b"bad zip")

        with patch.object(server, "urlopen", side_effect=fetch):
            timetable, info = server.rail_timetable()
        self.assertGreater(len(timetable.trips), 1000)
        self.assertFalse(server.GTFS_ZIP.exists())
        self.assertTrue(info["warning"])


if __name__ == "__main__":
    unittest.main()

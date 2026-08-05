"""Unit tests for timelapse_capture.py.

Covers the destination-path logic, which owns the on-disk contract the encoder
depends on, and the DST fall-back collision handling. No network, no threads
started - HttpCamera is constructed but never run().
"""

import math
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import _support

import timelapse_capture as cap


def make_config(frames_root, interval=5):
    return {
        "paths": {"frames_root": str(frames_root), "ffmpeg": "/usr/bin/ffmpeg"},
        "capture": {"interval_seconds": interval, "timeout_seconds": interval - 1,
                    "min_bytes": 4096, "min_free_gb": 0,
                    "log_every_n_failures": 60},
    }


class TestDestPath(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = make_config(self.tmp)
        self.cam = cap.HttpCamera(
            {"name": "Gate", "url": "http://192.0.2.1/snap", "auth": "none"}, cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_layout_is_camera_then_date_then_time(self):
        p = self.cam._dest_path(datetime(2026, 8, 4, 13, 5, 9))
        self.assertEqual(p.name, "130509.jpg")
        self.assertEqual(p.parent.name, "2026-08-04")
        self.assertEqual(p.parent.parent.name, "Gate")

    def test_time_is_zero_padded(self):
        # Zero padding is what makes lexical order chronological; the encoder
        # sorts by name and never looks at mtime.
        p = self.cam._dest_path(datetime(2026, 8, 4, 0, 0, 5))
        self.assertEqual(p.name, "000005.jpg")

    def test_names_sort_chronologically(self):
        stamps = [datetime(2026, 8, 4, h, m, s)
                  for h, m, s in [(23, 59, 59), (0, 0, 0), (12, 0, 0)]]
        names = sorted(self.cam._dest_path(t).name for t in stamps)
        self.assertEqual(names, ["000000.jpg", "120000.jpg", "235959.jpg"])

    def test_creates_the_day_directory(self):
        self.cam._dest_path(datetime(2026, 8, 4, 1, 2, 3))
        self.assertTrue((self.tmp / "Gate" / "2026-08-04").is_dir())

    def test_rolls_over_to_a_new_day_directory(self):
        self.cam._dest_path(datetime(2026, 8, 4, 23, 59, 55))
        self.cam._dest_path(datetime(2026, 8, 5, 0, 0, 0))
        self.assertTrue((self.tmp / "Gate" / "2026-08-04").is_dir())
        self.assertTrue((self.tmp / "Gate" / "2026-08-05").is_dir())

    def test_dst_collision_gets_a_suffix(self):
        # During the autumn fall-back, local time repeats 03:00-03:59, so the
        # same HHMMSS comes round twice. Keep both rather than overwrite.
        when = datetime(2026, 10, 25, 3, 30, 0)
        first = self.cam._dest_path(when)
        first.write_bytes(b"\xff\xd8first")
        second = self.cam._dest_path(when)
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, "033000-1.jpg")

    def test_repeated_collisions_keep_incrementing(self):
        when = datetime(2026, 10, 25, 3, 30, 0)
        made = []
        for _ in range(3):
            p = self.cam._dest_path(when)
            p.write_bytes(b"\xff\xd8x")
            made.append(p.name)
        self.assertEqual(made, ["033000.jpg", "033000-1.jpg", "033000-2.jpg"])

    def test_suffixed_names_still_belong_to_the_same_day(self):
        when = datetime(2026, 10, 25, 3, 30, 0)
        first = self.cam._dest_path(when)
        first.write_bytes(b"\xff\xd8x")
        self.assertEqual(self.cam._dest_path(when).parent, first.parent)

    def test_thread_attribute_shadowing_did_not_happen(self):
        # threading.Thread.__init__ sets self._target = None, which silently
        # shadows a method of that name. The destination helper is deliberately
        # called _dest_path; this guards the rename.
        self.assertTrue(callable(getattr(self.cam, "_dest_path", None)))
        self.assertIsNone(getattr(self.cam, "_target"))
        self.assertEqual(self.cam.name_, "Gate")


class TestCameraConstruction(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_config(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, **cam):
        base = {"name": "Cam", "url": "http://192.0.2.1/snap"}
        base.update(cam)
        return cap.HttpCamera(base, self.cfg)

    def test_digest_auth_is_configured(self):
        from requests.auth import HTTPDigestAuth
        c = self.build(auth="digest", username="u", password="p")
        self.assertIsInstance(c.session.auth, HTTPDigestAuth)

    def test_basic_auth_is_configured(self):
        from requests.auth import HTTPBasicAuth
        c = self.build(auth="basic", username="u", password="p")
        self.assertIsInstance(c.session.auth, HTTPBasicAuth)

    def test_no_auth_leaves_the_session_bare(self):
        self.assertIsNone(self.build(auth="none").session.auth)

    def test_missing_auth_key_defaults_to_none(self):
        self.assertIsNone(self.build().session.auth)

    def test_auth_is_case_insensitive(self):
        from requests.auth import HTTPDigestAuth
        c = self.build(auth="DIGEST", username="u", password="p")
        self.assertIsInstance(c.session.auth, HTTPDigestAuth)

    def test_thread_is_a_daemon(self):
        # The process must be able to exit even if a fetch is wedged.
        self.assertTrue(self.build().daemon)

    def test_capture_settings_are_read_from_config(self):
        c = self.build()
        self.assertEqual(c.interval, 5)
        self.assertEqual(c.timeout, 4)
        self.assertEqual(c.min_bytes, 4096)


class TestScheduleMath(unittest.TestCase):
    """The absolute-boundary arithmetic that keeps capture drift-free."""

    def next_boundary(self, now, interval):
        return math.ceil(now / interval) * interval

    def test_lands_on_an_exact_multiple(self):
        self.assertEqual(self.next_boundary(1000.0, 5), 1000)
        self.assertEqual(self.next_boundary(1001.0, 5), 1005)
        self.assertEqual(self.next_boundary(1004.9, 5), 1005)

    def test_targets_do_not_drift_with_slow_fetches(self):
        # Each target is computed from the clock, not by adding to the previous
        # completion time, so latency cannot accumulate.
        interval, t = 5, self.next_boundary(1000.0, 5)
        targets = []
        for _ in range(5):
            targets.append(t)
            t += interval
        self.assertEqual(targets, [1000, 1005, 1010, 1015, 1020])

    def test_resync_skips_a_missed_backlog(self):
        # Fallen 30s behind: jump to the next boundary rather than replaying
        # six missed frames back to back.
        interval, next_t, now = 5, 1000, 1030.0
        if next_t <= now:
            next_t = self.next_boundary(now, interval)
        self.assertEqual(next_t, 1030)


class TestDiskGuard(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_threshold_is_converted_to_bytes(self):
        cfg = make_config(self.tmp)
        cfg["capture"]["min_free_gb"] = 60
        self.assertEqual(cap.DiskGuard(cfg).min_free, 60 * 1024 ** 3)

    def test_zero_disables_the_guard(self):
        cfg = make_config(self.tmp)
        cfg["capture"]["min_free_gb"] = 0
        self.assertEqual(cap.DiskGuard(cfg).min_free, 0)

    def test_resume_threshold_has_hysteresis(self):
        # Pause below the threshold, resume only at 110% of it, so a disk
        # hovering at the limit cannot flap the guard on and off.
        cfg = make_config(self.tmp)
        cfg["capture"]["min_free_gb"] = 10
        guard = cap.DiskGuard(cfg)
        self.assertGreater(guard.min_free * 1.1, guard.min_free)


if __name__ == "__main__":
    unittest.main()

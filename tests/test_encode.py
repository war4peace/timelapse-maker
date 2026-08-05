"""Unit tests for timelapse_encode.py — the pure logic, no ffmpeg involved.

The end-to-end encode is covered separately by tests/smoke_test.py.
"""

import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import _support
from _support import make_frame

import timelapse_encode as enc


class TestHumanFormatting(unittest.TestCase):

    def test_duration_seconds_only(self):
        self.assertEqual(enc.human_duration(0), "0s")
        self.assertEqual(enc.human_duration(45), "45s")

    def test_duration_minutes(self):
        self.assertEqual(enc.human_duration(60), "1m 00s")
        self.assertEqual(enc.human_duration(125), "2m 05s")

    def test_duration_hours(self):
        self.assertEqual(enc.human_duration(3600), "1h 00m 00s")
        self.assertEqual(enc.human_duration(3723), "1h 02m 03s")

    def test_duration_truncates_fractions(self):
        self.assertEqual(enc.human_duration(45.9), "45s")

    def test_size_bytes_have_no_decimal(self):
        self.assertEqual(enc.human_size(0), "0 B")
        self.assertEqual(enc.human_size(512), "512 B")

    def test_size_scales_up(self):
        self.assertEqual(enc.human_size(2048), "2.0 KB")
        self.assertEqual(enc.human_size(1024 ** 2), "1.0 MB")
        self.assertEqual(enc.human_size(1024 ** 3), "1.0 GB")

    def test_size_caps_at_terabytes(self):
        # Nothing above TB, so a petabyte reports as 1024 TB rather than
        # falling off the end of the unit list and returning None.
        self.assertEqual(enc.human_size(1024 ** 5), "1024.0 TB")


class TestDateDirPattern(unittest.TestCase):

    def test_accepts_iso_dates(self):
        self.assertTrue(enc.DATE_DIR.match("2026-08-05"))

    def test_rejects_everything_else(self):
        for name in ("2026-8-5", "2026-08-05-1", "frames", "20260805",
                     "_staging", ".hidden"):
            with self.subTest(name=name):
                self.assertFalse(enc.DATE_DIR.match(name))


class TestValidFrames(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.day = self.tmp / "Cam" / "2026-08-04"
        self.day.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_accepts_well_formed_frames(self):
        for n in ("000000", "000005", "000010"):
            make_frame(self.day / f"{n}.jpg")
        good, bad = enc.valid_frames(self.day, 4096)
        self.assertEqual(len(good), 3)
        self.assertEqual(bad, 0)

    def test_rejects_undersized_files(self):
        make_frame(self.day / "000000.jpg")
        make_frame(self.day / "000005.jpg", size=100)
        good, bad = enc.valid_frames(self.day, 4096)
        self.assertEqual([p.name for p in good], ["000000.jpg"])
        self.assertEqual(bad, 1)

    def test_rejects_non_jpeg_headers(self):
        make_frame(self.day / "000000.jpg")
        make_frame(self.day / "000005.jpg", header=b"NOT")
        good, bad = enc.valid_frames(self.day, 4096)
        self.assertEqual([p.name for p in good], ["000000.jpg"])
        self.assertEqual(bad, 1)

    def test_result_is_chronological_by_name(self):
        # Written out of order on purpose: ordering must come from the
        # filename, never from mtime or creation order.
        for n in ("235959", "000000", "120000"):
            make_frame(self.day / f"{n}.jpg")
        good, _ = enc.valid_frames(self.day, 4096)
        self.assertEqual([p.stem for p in good],
                         ["000000", "120000", "235959"])

    def test_ignores_temp_files_and_non_jpegs(self):
        make_frame(self.day / "000000.jpg")
        make_frame(self.day / ".000005.tmp")       # in-flight capture
        make_frame(self.day / "notes.txt")
        good, bad = enc.valid_frames(self.day, 4096)
        self.assertEqual(len(good), 1)
        self.assertEqual(bad, 0, "non-.jpg files must not count as bad frames")

    def test_empty_directory(self):
        good, bad = enc.valid_frames(self.day, 4096)
        self.assertEqual((good, bad), ([], 0))


class TestWriteConcatList(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_basic_format(self):
        target = self.tmp / "list.txt"
        enc.write_concat_list([Path("/frames/a.jpg"), Path("/frames/b.jpg")],
                              target)
        lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertTrue(line.startswith("file '"))
            self.assertTrue(line.endswith("'"))

    def test_no_utf8_bom(self):
        # A BOM makes ffmpeg fail with: unknown keyword '﻿file'
        target = self.tmp / "list.txt"
        enc.write_concat_list([Path("/frames/a.jpg")], target)
        self.assertFalse(target.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_escapes_single_quotes(self):
        target = self.tmp / "list.txt"
        enc.write_concat_list([Path("/frames/it's here.jpg")], target)
        self.assertIn("'\\''", target.read_text(encoding="utf-8"))


class TestFindPending(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "frames"
        self.today = date.today()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def day(self, camera, offset):
        d = (self.today + timedelta(days=offset)).isoformat()
        p = self.root / camera / d
        p.mkdir(parents=True, exist_ok=True)
        return d

    def test_todays_directory_is_left_alone(self):
        # Capture is still writing to it.
        self.day("Cam", 0)
        self.assertEqual(enc.find_pending(self.root, ["Cam"], None, 7), [])

    def test_finds_completed_days(self):
        self.day("Cam", -1)
        self.day("Cam", -2)
        jobs = enc.find_pending(self.root, ["Cam"], None, 7)
        self.assertEqual(len(jobs), 2)

    def test_returns_oldest_first(self):
        self.day("Cam", -1)
        self.day("Cam", -3)
        self.day("Cam", -2)
        names = [d.name for _, d in enc.find_pending(self.root, ["Cam"], None, 7)]
        self.assertEqual(names, sorted(names))

    def test_backlog_cap_keeps_the_newest_dates(self):
        for off in range(-10, 0):
            self.day("Cam", off)
        jobs = enc.find_pending(self.root, ["Cam"], None, 3)
        names = [d.name for _, d in jobs]
        expected = [(self.today + timedelta(days=o)).isoformat()
                    for o in (-3, -2, -1)]
        self.assertEqual(names, expected)

    def test_backlog_cap_counts_dates_not_jobs(self):
        # Two cameras over two days is four jobs but only two dates; a cap of
        # 2 must keep all four, not truncate to two.
        for cam in ("A", "B"):
            self.day(cam, -1)
            self.day(cam, -2)
        self.assertEqual(len(enc.find_pending(self.root, ["A", "B"], None, 2)), 4)

    def test_only_date_selects_exactly_one(self):
        target = self.day("Cam", -2)
        self.day("Cam", -1)
        jobs = enc.find_pending(self.root, ["Cam"], target, 7)
        self.assertEqual([d.name for _, d in jobs], [target])

    def test_only_date_ignores_the_backlog_cap(self):
        target = self.day("Cam", -9)
        for off in range(-8, 0):
            self.day("Cam", off)
        jobs = enc.find_pending(self.root, ["Cam"], target, 3)
        self.assertEqual([d.name for _, d in jobs], [target])

    def test_only_date_may_be_today(self):
        # --date is an explicit override, so it can encode a day still in use.
        target = self.day("Cam", 0)
        jobs = enc.find_pending(self.root, ["Cam"], target, 7)
        self.assertEqual([d.name for _, d in jobs], [target])

    def test_non_date_directories_are_skipped(self):
        (self.root / "Cam").mkdir(parents=True)
        (self.root / "Cam" / "scratch").mkdir()
        (self.root / "Cam" / "2026-8-4").mkdir()
        self.assertEqual(enc.find_pending(self.root, ["Cam"], None, 7), [])

    def test_unconfigured_cameras_are_not_touched(self):
        self.day("Ghost", -1)
        self.assertEqual(enc.find_pending(self.root, ["Cam"], None, 7), [])

    def test_missing_camera_directory_is_not_an_error(self):
        self.root.mkdir(parents=True)
        self.assertEqual(enc.find_pending(self.root, ["Cam"], None, 7), [])


class TestEncoderCandidates(unittest.TestCase):

    def test_preference_order_is_av1_then_hevc_then_x264(self):
        codecs = [c["codec"] for c in enc.build_candidates({})]
        self.assertEqual(codecs, ["av1_nvenc", "hevc_nvenc", "libx264"])

    def test_config_values_reach_the_arguments(self):
        args = enc.build_candidates({"av1_cq": 31, "gop": 90,
                                     "av1_preset": "p4"})[0]["args"]
        self.assertIn("31", args)
        self.assertIn("90", args)
        self.assertIn("p4", args)

    def test_defaults_apply_when_config_is_empty(self):
        args = enc.build_candidates({})[0]["args"]
        self.assertIn("p6", args)
        self.assertIn("26", args)


class TestBuildSummary(unittest.TestCase):

    def row(self, **kw):
        base = {"camera": "Cam", "date": "2026-08-04", "status": "OK",
                "frames": 17280, "bad": 0, "size": 1024 ** 3,
                "seconds": 90, "note": ""}
        base.update(kw)
        return base

    def test_full_coverage_reads_100_percent(self):
        out = enc.build_summary([self.row()], 5)
        self.assertIn("100", out)

    def test_half_coverage(self):
        out = enc.build_summary([self.row(frames=8640)], 5)
        self.assertIn("50", out)

    def test_zero_frames_does_not_divide_by_zero(self):
        out = enc.build_summary([self.row(frames=0, size=0)], 5)
        self.assertIn("Cam", out)

    def test_is_a_code_block(self):
        out = enc.build_summary([self.row()], 5)
        self.assertTrue(out.startswith("```"))
        self.assertTrue(out.rstrip().endswith("```"))


if __name__ == "__main__":
    unittest.main()

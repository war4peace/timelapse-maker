"""Unit tests for timelapse_encode.py: the pure logic, no ffmpeg involved.

The end-to-end encode is covered separately by tests/smoke_test.py.
"""

import inspect
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

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

    def mark(self, camera, offset, video="Cam.mkv"):
        """Mark a day encoded, the way a successful run leaves it."""
        d = (self.today + timedelta(days=offset)).isoformat()
        (self.root / camera / d / enc.ENCODED_FILE).write_text(
            json.dumps({"video": video}), encoding="utf-8")
        return d

    def pending(self, *a, **kw):
        """The job list only; the skipped count is asserted where it matters."""
        return enc.find_pending(*a, **kw)[0]

    def test_todays_directory_is_left_alone(self):
        # Capture is still writing to it.
        self.day("Cam", 0)
        self.assertEqual(self.pending(self.root, ["Cam"], None, 7), [])

    def test_finds_completed_days(self):
        self.day("Cam", -1)
        self.day("Cam", -2)
        jobs = self.pending(self.root, ["Cam"], None, 7)
        self.assertEqual(len(jobs), 2)

    def test_returns_oldest_first(self):
        self.day("Cam", -1)
        self.day("Cam", -3)
        self.day("Cam", -2)
        names = [d.name for _, d in self.pending(self.root, ["Cam"], None, 7)]
        self.assertEqual(names, sorted(names))

    def test_backlog_cap_keeps_the_newest_dates(self):
        for off in range(-10, 0):
            self.day("Cam", off)
        jobs = self.pending(self.root, ["Cam"], None, 3)
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
        self.assertEqual(len(self.pending(self.root, ["A", "B"], None, 2)), 4)

    def test_only_date_selects_exactly_one(self):
        target = self.day("Cam", -2)
        self.day("Cam", -1)
        jobs = self.pending(self.root, ["Cam"], target, 7)
        self.assertEqual([d.name for _, d in jobs], [target])

    def test_only_date_ignores_the_backlog_cap(self):
        target = self.day("Cam", -9)
        for off in range(-8, 0):
            self.day("Cam", off)
        jobs = self.pending(self.root, ["Cam"], target, 3)
        self.assertEqual([d.name for _, d in jobs], [target])

    def test_only_date_may_be_today(self):
        # --date is an explicit override, so it can encode a day still in use.
        target = self.day("Cam", 0)
        jobs = self.pending(self.root, ["Cam"], target, 7)
        self.assertEqual([d.name for _, d in jobs], [target])

    def test_non_date_directories_are_skipped(self):
        (self.root / "Cam").mkdir(parents=True)
        (self.root / "Cam" / "scratch").mkdir()
        (self.root / "Cam" / "2026-8-4").mkdir()
        self.assertEqual(self.pending(self.root, ["Cam"], None, 7), [])

    def test_unconfigured_cameras_are_not_touched(self):
        self.day("Ghost", -1)
        self.assertEqual(self.pending(self.root, ["Cam"], None, 7), [])

    def test_missing_camera_directory_is_not_an_error(self):
        self.root.mkdir(parents=True)
        self.assertEqual(self.pending(self.root, ["Cam"], None, 7), [])


class TestFindPendingSkipsEncoded(unittest.TestCase):
    """The whole point of the marker: kept frames are not encoded twice.

    Before 0.1.6 nothing recorded that a day had been encoded, so with
    `delete_frames_on_success` off the encoder re-encoded the newest N days
    from scratch every single night, and re-transferred the results.
    """

    setUp = TestFindPending.setUp
    tearDown = TestFindPending.tearDown
    day = TestFindPending.day
    mark = TestFindPending.mark
    pending = TestFindPending.pending

    def test_a_marked_day_is_not_offered_again(self):
        self.day("Cam", -1)
        self.mark("Cam", -1)
        jobs, done = enc.find_pending(self.root, ["Cam"], None, 7)
        self.assertEqual(jobs, [])
        self.assertEqual(done, 1)

    def test_an_unmarked_day_beside_a_marked_one_still_runs(self):
        self.day("Cam", -1)
        self.day("Cam", -2)
        self.mark("Cam", -2)
        jobs, done = enc.find_pending(self.root, ["Cam"], None, 7)
        self.assertEqual([d.name for _, d in jobs],
                         [(self.today + timedelta(days=-1)).isoformat()])
        self.assertEqual(done, 1)

    def test_the_marker_is_per_camera_not_per_date(self):
        # Two cameras share a date; marking one must not excuse the other.
        for cam in ("A", "B"):
            self.day(cam, -1)
        self.mark("A", -1)
        jobs, done = enc.find_pending(self.root, ["A", "B"], None, 7)
        self.assertEqual([c for c, _ in jobs], ["B"])
        self.assertEqual(done, 1)

    def test_force_re_encodes_a_marked_day(self):
        self.day("Cam", -1)
        self.mark("Cam", -1)
        jobs, done = enc.find_pending(self.root, ["Cam"], None, 7, force=True)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(done, 0)

    def test_only_date_overrides_the_marker(self):
        # Re-encoding one day by hand has to stay possible without --force.
        target = self.day("Cam", -2)
        self.mark("Cam", -2)
        jobs, _ = enc.find_pending(self.root, ["Cam"], target, 7)
        self.assertEqual([d.name for _, d in jobs], [target])

    def test_marked_days_do_not_consume_the_backlog_window(self):
        # Dropped before the cap, or a week of finished days would push the
        # one day that still needs encoding straight out of the window.
        for off in range(-10, 0):
            self.day("Cam", off)
            if off != -10:
                self.mark("Cam", off)
        jobs, done = enc.find_pending(self.root, ["Cam"], None, 3)
        self.assertEqual([d.name for _, d in jobs],
                         [(self.today + timedelta(days=-10)).isoformat()])
        self.assertEqual(done, 9)

    def test_a_damaged_marker_means_encode_it_again(self):
        self.day("Cam", -1)
        (self.root / "Cam" / (self.today + timedelta(days=-1)).isoformat()
         / enc.ENCODED_FILE).write_text("{ truncated", encoding="utf-8")
        jobs, done = enc.find_pending(self.root, ["Cam"], None, 7)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(done, 0)

    def test_a_cadence_marker_is_not_an_encoded_marker(self):
        # Two dotfiles in the same directory, and only one of them means done.
        self.day("Cam", -1)
        (self.root / "Cam" / (self.today + timedelta(days=-1)).isoformat()
         / enc.CADENCE_FILE).write_text(
            json.dumps({"interval_seconds": 5, "framerate": 60}),
            encoding="utf-8")
        jobs, done = enc.find_pending(self.root, ["Cam"], None, 7)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(done, 0)


class TestExplainIdle(unittest.TestCase):
    """"Nothing to process" is the same sentence for four different states.

    An operator ran the Windows scheduled task by hand at 21:58, got that
    line and an empty videos folder, and had no way to tell the correct
    answer (today is still being captured) from the one worth acting on
    (the frames are stranded behind a renamed camera).
    """

    setUp = TestFindPending.setUp
    tearDown = TestFindPending.tearDown
    day = TestFindPending.day

    def why(self, cameras):
        return " ".join(enc.explain_idle(self.root, cameras))

    def test_only_today_says_so_and_says_when_it_goes_out(self):
        self.day("Cam", 0)
        why = self.why(["Cam"])
        self.assertIn(self.today.isoformat(), why)
        self.assertIn("still", why)
        self.assertIn("tonight", why)

    def test_only_today_across_several_cameras_is_still_the_today_answer(self):
        for cam in ("A", "B", "C"):
            self.day(cam, 0)
        self.assertIn("still", self.why(["A", "B", "C"]))

    def test_a_missing_directory_is_named(self):
        # Enabled in the config, nothing on disk: harmless before the first
        # capture, and the entire fault after a rename.
        self.day("Roof", 0)
        why = self.why(["Roof", "Garage"])
        self.assertIn("Garage", why)
        self.assertNotIn("Roof,", why)
        self.assertIn("renamed", why)

    def test_no_camera_has_a_directory_at_all(self):
        why = self.why(["Roof"])
        self.assertIn("Roof", why)
        self.assertIn(str(self.root), why)

    def test_older_days_all_encoded_points_at_force(self):
        self.day("Cam", -1)
        self.assertIn("--force", self.why(["Cam"]))

    def test_a_camera_directory_with_no_day_folders(self):
        (self.root / "Cam").mkdir(parents=True)
        why = self.why(["Cam"])
        self.assertIn("No day folders", why)
        self.assertNotIn("--force", why)

    def test_no_enabled_cameras_is_its_own_answer(self):
        self.assertIn("No cameras are enabled", self.why([]))

    def test_a_stray_file_is_not_mistaken_for_a_day(self):
        (self.root / "Cam").mkdir(parents=True)
        (self.root / "Cam" / "2026-08-11").write_text("x", encoding="utf-8")
        self.assertIn("No day folders", self.why(["Cam"]))

    def test_every_branch_returns_at_least_one_line(self):
        # A silent explanation is the defect this function exists to fix.
        self.day("Has", -1)
        for cameras in ([], ["Has"], ["Has", "Missing"], ["Missing"]):
            self.assertTrue(enc.explain_idle(self.root, cameras), cameras)


class TestEncodedMarker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.day = self.tmp / "2026-08-11"
        self.day.mkdir()
        self.result = {"frames": 10896, "size": 612345678}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_it_is_hidden_like_the_cadence_marker(self):
        # Both live in a directory otherwise full of frames; neither should
        # show up in a casual listing, and neither is a *.jpg.
        self.assertTrue(enc.ENCODED_FILE.startswith("."))
        self.assertNotEqual(enc.ENCODED_FILE, enc.CADENCE_FILE)

    def test_round_trip(self):
        enc.mark_encoded(self.day, Path("/out/Cam.20260811.mkv"),
                         self.result, "av1_nvenc (NVIDIA AV1)")
        got = enc.day_encoded(self.day)
        self.assertEqual(got["video"], "Cam.20260811.mkv")
        self.assertEqual(got["frames"], 10896)
        self.assertEqual(got["size"], 612345678)
        self.assertEqual(got["encoder"], "av1_nvenc (NVIDIA AV1)")
        self.assertEqual(got["version"], enc.__version__)

    def test_it_records_when(self):
        enc.mark_encoded(self.day, Path("Cam.mkv"), self.result, "libx264")
        stamp = enc.day_encoded(self.day)["encoded_at"]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_no_marker_reads_as_none(self):
        self.assertIsNone(enc.day_encoded(self.day))

    def test_a_missing_directory_reads_as_none(self):
        self.assertIsNone(enc.day_encoded(self.tmp / "nope"))

    def test_malformed_json_reads_as_none(self):
        (self.day / enc.ENCODED_FILE).write_text("{ nope", encoding="utf-8")
        self.assertIsNone(enc.day_encoded(self.day))

    def test_json_that_is_not_an_object_reads_as_none(self):
        (self.day / enc.ENCODED_FILE).write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(enc.day_encoded(self.day))

    def test_a_marker_naming_no_video_reads_as_none(self):
        # An empty {} is what a half-written or hand-made file looks like, and
        # it is not evidence that anything was produced.
        (self.day / enc.ENCODED_FILE).write_text("{}", encoding="utf-8")
        self.assertIsNone(enc.day_encoded(self.day))

    def test_it_overwrites_an_earlier_marker(self):
        # --force re-encodes; the marker must then describe the new video, not
        # keep pointing at the one that was just replaced.
        enc.mark_encoded(self.day, Path("old.mkv"), self.result, "libx264")
        enc.mark_encoded(self.day, Path("new.mkv"), self.result, "av1_nvenc")
        self.assertEqual(enc.day_encoded(self.day)["video"], "new.mkv")

    def test_it_leaves_no_temporary_file_behind(self):
        enc.mark_encoded(self.day, Path("Cam.mkv"), self.result, "libx264")
        self.assertEqual([p.name for p in self.day.iterdir()],
                         [enc.ENCODED_FILE])

    def test_an_unwritable_directory_is_not_an_error(self):
        # Failing to annotate a day costs a re-encode next run, which is what
        # the project did for five releases. It is not worth failing over.
        missing = self.tmp / "gone" / "2026-08-11"
        with self.assertLogs("encode", level="WARNING") as cm:
            self.assertFalse(
                enc.mark_encoded(missing, Path("Cam.mkv"), self.result, "x"))
        self.assertIn("encoded again", "\n".join(cm.output))


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


class TestEncoderDiagnostics(unittest.TestCase):
    """A failed probe must explain itself.

    The exit code alone cannot distinguish "this ffmpeg has no av1_nvenc" from
    "this GPU cannot do AV1", and the two need opposite fixes. Guessing from
    the codec name produced a wizard that told an RTX 4060 owner their GPU was
    too old.
    """

    def test_probe_frame_is_large_enough_for_nvenc(self):
        # hevc_nvenc rejects 128x128 with "invalid param (8): Frame dimensions".
        w, h = (int(v) for v in enc.PROBE_SIZE.split("x"))
        self.assertGreaterEqual(min(w, h), 256)

    def test_probe_pins_the_pixel_format(self):
        """Regression: the probe must not let ffmpeg negotiate a format.

        testsrc emits rgb24; left alone ffmpeg picks the closest format the
        encoder advertises, which for av1_nvenc is yuv444p. NVENC on Ada
        cannot do AV1 in 4:4:4, so the probe failed with "No capable devices
        found" and declared an RTX 4060 incapable of AV1 that it can do
        perfectly well in 4:2:0.
        """
        captured = {}

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return Result()

        original = enc.subprocess.run
        enc.subprocess.run = fake_run
        try:
            enc.probe_encoder_detail("ffmpeg", {"codec": "av1_nvenc",
                                                "args": ["-c:v", "av1_nvenc"]})
        finally:
            enc.subprocess.run = original

        cmd = captured["cmd"]
        self.assertIn("-pix_fmt", cmd)
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], enc.PIX_FMT)

    def test_probe_format_matches_what_the_pipeline_produces(self):
        """The probe is only meaningful if it encodes what encode_day() will.

        Both now read the same constant; this guards the invariant rather than
        the spelling.
        """
        import inspect
        source = inspect.getsource(enc.encode_day)
        self.assertIn("format={PIX_FMT}", source,
                      "encode_day must build its filter chain from PIX_FMT")
        self.assertEqual(enc.PIX_FMT, "yuv420p")

    def test_pixel_format_rejection_is_explained(self):
        hint = enc.encoder_hint("av1_nvenc", "YUV444P not supported",
                                built_in=True)
        self.assertIn("pixel format", hint)

    def test_missing_encoder_in_build_blames_the_build(self):
        hint = enc.encoder_hint("av1_nvenc", "Unknown encoder 'av1_nvenc'")
        self.assertIn("build", hint)
        self.assertNotIn("GPU", hint)

    def test_encoder_list_absence_blames_the_build(self):
        hint = enc.encoder_hint("av1_nvenc", "some other error", built_in=False)
        self.assertIn("build", hint)

    def test_no_capable_devices_does_not_assert_a_single_cause(self):
        # "Codec not supported" / "No capable devices found" is genuinely
        # ambiguous: an incapable GPU and an ffmpeg too old to ask for the
        # codec produce the same line. Asserting either one was the original
        # bug, so the hint must name both possibilities.
        hint = enc.encoder_hint("av1_nvenc", "No capable devices found",
                                built_in=True)
        self.assertIn("GPU", hint)
        self.assertIn("build", hint)

    def test_codec_not_supported_is_treated_the_same_way(self):
        hint = enc.encoder_hint("av1_nvenc", "Codec not supported",
                                built_in=True)
        self.assertIn("did not advertise", hint)

    def test_driver_too_old_is_named_precisely(self):
        hint = enc.encoder_hint(
            "av1_nvenc",
            "The minimum required Nvidia driver for nvenc is 520.56.06 or newer",
            built_in=True)
        self.assertIn("driver", hint)
        self.assertNotIn("GPU", hint)


class TestVerboseProbeFiltering(unittest.TestCase):
    """The verbose probe must surface the line that explains the failure."""

    def run_filter(self, stderr_text):
        captured = {}

        class Result:
            returncode = 1
            stderr = stderr_text
            stdout = ""

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return Result()

        original = enc.subprocess.run
        enc.subprocess.run = fake_run
        try:
            return enc.probe_encoder_verbose(
                "ffmpeg", {"codec": "av1_nvenc", "args": ["-c:v", "av1_nvenc"]})
        finally:
            enc.subprocess.run = original

    REAL_OUTPUT = """[av1_nvenc @ 0x1] Loaded Nvenc version 13.1
[av1_nvenc @ 0x1] Nvenc initialized successfully
[av1_nvenc @ 0x1] 1 CUDA capable devices found
[av1_nvenc @ 0x1] [ GPU #0 - < NVIDIA GeForce RTX 3090 > has Compute SM 8.6 ]
[av1_nvenc @ 0x1] Codec not supported
[av1_nvenc @ 0x1] No capable devices found
[vost#0:0] Starting thread...
[vost#0:0] Task finished with error code: -22 (Invalid argument)
"""

    def test_keeps_the_explanatory_line(self):
        lines = self.run_filter(self.REAL_OUTPUT)
        self.assertIn("Codec not supported", lines)

    def test_keeps_the_nvenc_version_and_gpu_identity(self):
        lines = " | ".join(self.run_filter(self.REAL_OUTPUT))
        self.assertIn("Loaded Nvenc version 13.1", lines)
        self.assertIn("RTX 3090", lines)

    def test_drops_unrelated_noise(self):
        lines = " | ".join(self.run_filter(self.REAL_OUTPUT))
        self.assertNotIn("Starting thread", lines)

    def test_strips_the_ffmpeg_context_prefix(self):
        # "[av1_nvenc @ 0x1] Codec not supported" -> "Codec not supported".
        # The GPU line keeps its own brackets, which are part of the message.
        for line in self.run_filter(self.REAL_OUTPUT):
            self.assertNotIn("@ 0x", line, line)

    def test_deduplicates_repeated_lines(self):
        lines = self.run_filter(self.REAL_OUTPUT + self.REAL_OUTPUT)
        self.assertEqual(len(lines), len(set(lines)))

    def test_uses_the_standard_probe_size(self):
        captured = {}

        class Result:
            returncode = 1
            stderr = ""
            stdout = ""

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return Result()

        original = enc.subprocess.run
        enc.subprocess.run = fake_run
        try:
            enc.probe_encoder_verbose("ffmpeg", {"codec": "x", "args": []})
        finally:
            enc.subprocess.run = original
        joined = " ".join(captured["cmd"])
        self.assertIn(enc.PROBE_SIZE, joined)
        self.assertIn("verbose", joined)

    def test_a_crashing_probe_does_not_raise(self):
        def boom(cmd, **kw):
            raise OSError("nope")
        original = enc.subprocess.run
        enc.subprocess.run = boom
        try:
            lines = enc.probe_encoder_verbose("ffmpeg", {"codec": "x", "args": []})
        finally:
            enc.subprocess.run = original
        self.assertEqual(len(lines), 1)
        self.assertIn("could not run", lines[0])

    def test_dimension_error_is_flagged_as_our_bug(self):
        hint = enc.encoder_hint(
            "hevc_nvenc",
            "InitializeEncoder failed: invalid param (8): Frame dimensions",
            built_in=True)
        self.assertIn("report", hint)

    def test_session_exhaustion_is_recognised(self):
        hint = enc.encoder_hint("hevc_nvenc", "OpenEncodeSessionEx failed: out of memory",
                                built_in=True)
        self.assertIn("session", hint.lower())

    def test_unrecognised_error_yields_no_invented_cause(self):
        self.assertEqual(enc.encoder_hint("av1_nvenc", "something odd",
                                          built_in=True), "")

    def test_probe_detail_reports_a_missing_binary(self):
        ok, msg = enc.probe_encoder_detail(
            "/nonexistent/ffmpeg", {"codec": "x", "args": []})
        self.assertFalse(ok)
        self.assertIn("not found", msg)

    def test_probe_bool_wrapper_agrees_with_detail(self):
        cand = {"codec": "x", "args": []}
        self.assertEqual(enc.probe_encoder("/nonexistent/ffmpeg", cand),
                         enc.probe_encoder_detail("/nonexistent/ffmpeg", cand)[0])

    def test_list_encoders_handles_a_missing_binary(self):
        self.assertIsNone(enc.list_encoders("/nonexistent/ffmpeg"))


class TestWebhookRequest(unittest.TestCase):
    """Discord sits behind Cloudflare, which 403s urllib's default UA."""

    def _capture(self, payload=None):
        captured = {}

        class FakeResponse:
            status = 204

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return FakeResponse()

        original = enc.urlrequest.urlopen
        enc.urlrequest.urlopen = fake_urlopen
        try:
            enc.post_webhook("https://discord.com/api/webhooks/x/y",
                             payload or {"content": "hi"})
        finally:
            enc.urlrequest.urlopen = original
        return captured["req"]

    def test_sends_an_explicit_user_agent(self):
        ua = self._capture().get_header("User-agent")
        self.assertTrue(ua)
        self.assertNotIn("Python-urllib", ua)

    def test_user_agent_matches_discords_documented_format(self):
        # "DiscordBot ($url, $version)" - the form Discord asks API clients for.
        ua = self._capture().get_header("User-agent")
        self.assertTrue(ua.startswith("DiscordBot ("), ua)
        self.assertIn("github.com", ua)

    def test_user_agent_carries_the_version(self):
        self.assertIn(enc.__version__, self._capture().get_header("User-agent"))

    def test_posts_json_with_the_right_content_type(self):
        req = self._capture({"content": "hello"})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data.decode()), {"content": "hello"})

    def test_unicode_payload_is_utf8_encoded(self):
        req = self._capture({"content": "café ✅"})
        self.assertEqual(json.loads(req.data.decode("utf-8"))["content"],
                         "café ✅")


class TestTransferWithoutRsync(unittest.TestCase):
    """What the nightly job says when it cannot move the videos.

    The same missing binary means two different things. On Linux rsync is a
    package and installing it fixes this. On Windows it is not the mechanism at
    all: transfer is not built yet (item 11f step 4), so telling an operator
    there to run apt names a thing they do not have, and the videos are not
    lost, they are simply still in the videos folder.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        (self.tmp / "Roof.20260816.mkv").write_bytes(b"video")
        self.cfg = {"paths": {"video_output": str(self.tmp)},
                    "transfer": {"enabled": True, "destination": "/dest",
                                 "require_mountpoint": False}}

    def run_transfer(self, windows):
        with mock.patch.object(enc, "IS_WINDOWS", windows), \
             mock.patch.object(enc, "mount_problem", return_value=None), \
             mock.patch.object(enc.subprocess, "run",
                               side_effect=FileNotFoundError(2, "no rsync")):
            return enc.transfer(self.cfg, dry_run=False)

    def test_linux_says_install_rsync(self):
        result = self.run_transfer(windows=False)
        self.assertFalse(result["ok"])
        self.assertIn("rsync", result["detail"])

    def test_windows_says_it_is_not_built_yet(self):
        result = self.run_transfer(windows=True)
        self.assertFalse(result["ok"])
        self.assertNotIn("rsync", result["detail"])
        self.assertIn("not implemented", result["detail"])

    def test_neither_raises(self):
        """Failure isolation, which is the older rule: a failed transfer must

        not turn a successful encode into a failure. The videos are safe where
        they are and the next run ships them.
        """
        for windows in (True, False):
            self.assertIsInstance(self.run_transfer(windows), dict)

    def test_nothing_is_deleted_either_way(self):
        for windows in (True, False):
            self.run_transfer(windows)
            self.assertTrue((self.tmp / "Roof.20260816.mkv").exists())


class TestMountGuard(unittest.TestCase):
    """transfer.require_mountpoint - the CIFS/NFS dropped-mount protection.

    Without it, an unmounted share is an ordinary empty directory and rsync
    fills the local disk, then --remove-source-files deletes the originals.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # transfer() logs the refusal at ERROR; keep it out of test output.
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_disabled_by_default(self):
        self.assertIsNone(enc.mount_problem({}, "/mnt/nas/tl/"))

    def test_explicitly_disabled(self):
        self.assertIsNone(
            enc.mount_problem({"require_mountpoint": False}, "/mnt/nas/tl/"))

    def test_never_applies_to_a_remote_destination(self):
        # rsync over SSH writes nothing locally, so there is no mount to check.
        self.assertIsNone(enc.mount_problem({"require_mountpoint": True},
                                            "user@nas:/mnt/user/tl/"))

    def test_string_form_rejects_a_path_that_is_not_a_mount(self):
        # POSIX-style strings throughout: the guard only engages for
        # destinations starting with "/", which is how a local path is told
        # apart from an rsync remote spec.
        problem = enc.mount_problem(
            {"require_mountpoint": "/mnt/unraid-cctv"},
            "/mnt/unraid-cctv/TL/")
        self.assertIsNotNone(problem)
        self.assertIn("not a mounted filesystem", problem)

    def test_string_form_accepts_a_real_mount(self):
        # "/" is a mount point on every POSIX system, and on Windows
        # ismount() accepts a drive root, so this works either way.
        root = os.path.abspath(os.sep)
        self.assertIsNone(
            enc.mount_problem({"require_mountpoint": root}, "/mnt/nas/tl/"))

    def test_boolean_form_rejects_an_unmounted_destination(self):
        # Nothing is mounted under the temp dir, so the nearest mount point
        # walking up is the filesystem root - meaning the share is absent.
        problem = enc.mount_problem({"require_mountpoint": True},
                                    "/definitely/not/mounted/tl/")
        self.assertIsNotNone(problem)
        self.assertIn("not mounted", problem)

    def test_nearest_mountpoint_terminates_at_the_root(self):
        mp = enc.nearest_mountpoint("/no/such/path/anywhere")
        self.assertEqual(str(mp), os.path.abspath(os.sep))

    def test_transfer_refuses_and_reports_a_failure(self):
        videos = self.tmp / "videos"
        videos.mkdir()
        (videos / "Cam.20260804.mkv").write_bytes(b"x" * 2048)
        cfg = {"paths": {"video_output": str(videos)},
               "transfer": {"enabled": True,
                            "destination": "/definitely/not/mounted/tl/",
                            "require_mountpoint": True}}
        result = enc.transfer(cfg, dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["moved"], 0)

    def test_refusing_leaves_the_videos_alone(self):
        # The encode succeeded; the videos must survive to ship next run.
        videos = self.tmp / "videos"
        videos.mkdir()
        keep = videos / "Cam.20260804.mkv"
        keep.write_bytes(b"x" * 2048)
        cfg = {"paths": {"video_output": str(videos)},
               "transfer": {"enabled": True,
                            "destination": "/definitely/not/mounted/tl/",
                            "require_mountpoint": True}}
        enc.transfer(cfg, dry_run=False)
        self.assertTrue(keep.exists(), "videos must not be deleted or moved")

    def test_nothing_to_transfer_short_circuits_before_the_guard(self):
        videos = self.tmp / "videos"
        videos.mkdir()
        cfg = {"paths": {"video_output": str(videos)},
               "transfer": {"enabled": True,
                            "destination": "/definitely/not/mounted/tl/",
                            "require_mountpoint": True}}
        self.assertTrue(enc.transfer(cfg, dry_run=False)["ok"])

    def test_disabled_transfer_returns_none(self):
        cfg = {"paths": {"video_output": str(self.tmp)},
               "transfer": {"enabled": False}}
        self.assertIsNone(enc.transfer(cfg, dry_run=False))


class TestRsyncProbe(unittest.TestCase):
    """Measured, not guessed.

    The pre-flight used to warn that CIFS plus `-a` meant rsync would exit 23
    every night. `-a` does imply --owner --group, and a share often cannot set
    them, but whether it actually fails depends on the server and the mount
    options. On the author's own share it does not, so a working config was
    reported as broken. A guess dressed as a finding is worse than no check.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # This suite runs on machines without rsync, where the probe correctly
        # declines to answer. Pretend it is installed; runuser is not, so the
        # command stays un-wrapped unless a test says otherwise.
        p = mock.patch.object(
            enc.shutil, "which",
            lambda n: "/usr/bin/rsync" if n == "rsync" else None)
        p.start()
        self.addCleanup(p.stop)

    def fake_run(self, rc, stderr=""):
        return mock.MagicMock(return_value=mock.Mock(returncode=rc,
                                                     stderr=stderr))

    def test_success_is_reported_as_working(self):
        with mock.patch.object(enc.subprocess, "run", self.fake_run(0)):
            self.assertEqual(enc.try_rsync_args(self.tmp, ["-a"]), (True, ""))

    def test_a_failure_carries_the_exit_code_and_the_message(self):
        # Exit 23 with no explanation is the thing an operator has to search
        # for; keep rsync's own words.
        with mock.patch.object(enc.subprocess, "run",
                               self.fake_run(23, "rsync: chgrp failed")):
            ok, detail = enc.try_rsync_args(self.tmp, ["-a"])
        self.assertFalse(ok)
        self.assertIn("exit 23", detail)
        self.assertIn("chgrp failed", detail)

    def test_the_configured_flags_are_the_ones_run(self):
        args = ["-a", "--no-owner", "--partial", "--remove-source-files"]
        with mock.patch.object(enc.subprocess, "run", self.fake_run(0)) as run:
            enc.try_rsync_args(self.tmp, args)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "rsync")
        for a in args:
            self.assertIn(a, cmd)

    def test_it_runs_as_the_service_account_when_there_is_one(self):
        # A share can accept root and refuse the account that runs nightly.
        #
        # Says who it is, rather than inheriting it. Wrapping in runuser is now
        # conditional on being root, and `getattr(os, "geteuid", lambda: 0)`
        # reports 0 on Windows, which has no such call: this passed here and
        # failed on every CI leg, because a runner is not root either.
        me, euid = self.as_user("root", root=True)
        with me, euid, \
                mock.patch.object(enc.shutil, "which", lambda n: f"/usr/bin/{n}"), \
                mock.patch.object(enc.subprocess, "run", self.fake_run(0)) as run:
            enc.try_rsync_args(self.tmp, ["-a"], svcuser="timelapse")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:4], ["runuser", "-u", "timelapse", "--"])

    def test_no_rsync_installed_is_untestable_not_a_failure(self):
        with mock.patch.object(enc.shutil, "which", lambda n: None):
            ok, detail = enc.try_rsync_args(self.tmp, ["-a"])
        self.assertIsNone(ok)
        self.assertIn("rsync is not installed", detail)

    # -- who the probe runs as --------------------------------------------
    # Reported from a real 0.1.3 install, during `sudo timelapse update`:
    #
    #   FAIL  rsync -a --partial --remove-source-files fails against
    #         /mnt/cctv/TL/: exit 1: runuser: may not be used by non-root users
    #   ....  no flag combination worked; check the share permissions for
    #   ....  timelapse.
    #
    # Nothing was wrong with the share. install.sh runs the pre-flight through
    # `as_service_user`, so it was already `runuser -u timelapse`; the probe
    # then ran runuser a second time from inside that unprivileged process,
    # and the nested refusal was read as rsync's answer. Fourth instance of
    # one shape here: "could not check" collapsed into "checked, and broken".
    #
    # test_already_being_the_service_account_needs_no_runuser is the one that
    # covers the reported case; the rest cover the neighbours.

    def as_user(self, name, root):
        """Pretend to be `name`, with or without root."""
        return (mock.patch.object(enc, "whoami", lambda: name),
                mock.patch.object(enc.os, "geteuid", lambda: 0 if root else 1000,
                                  create=True))

    def test_without_root_it_declines_instead_of_blaming_the_share(self):
        me, euid = self.as_user("eduard", root=False)
        with me, euid, mock.patch.object(enc.subprocess, "run") as run:
            ok, detail = enc.try_rsync_args(self.tmp, ["-a"], svcuser="timelapse")
        self.assertIsNone(ok, "not a verdict on the share")
        self.assertIsNot(ok, False)
        self.assertIn("root", detail)
        self.assertIn("sudo timelapse test", detail)
        run.assert_not_called()

    def test_already_being_the_service_account_needs_no_runuser(self):
        # The reported case, and the important one: install.sh runs the
        # pre-flight as `runuser -u timelapse`, so this probe is already the
        # account it wants to test as. Wrapping it in a second runuser from an
        # unprivileged process is what produced the false FAIL, and the answer
        # is not to decline but to measure, since running as that account is
        # exactly what makes the result authoritative.
        me, euid = self.as_user("timelapse", root=False)
        with me, euid, mock.patch.object(enc.shutil, "which",
                                         lambda n: "/usr/bin/rsync"
                                         if n == "rsync" else None), \
                mock.patch.object(enc.subprocess, "run", self.fake_run(0)) as run:
            ok, _ = enc.try_rsync_args(self.tmp, ["-a"], svcuser="timelapse")
        self.assertTrue(ok)
        self.assertEqual(run.call_args[0][0][0], "rsync")

    def test_no_runuser_on_the_host_is_untestable_too(self):
        me, euid = self.as_user("root", root=True)
        with me, euid, mock.patch.object(
                enc.shutil, "which",
                lambda n: "/usr/bin/rsync" if n == "rsync" else None), \
                mock.patch.object(enc.subprocess, "run") as run:
            ok, detail = enc.try_rsync_args(self.tmp, ["-a"], svcuser="timelapse")
        self.assertIsNone(ok)
        self.assertIn("runuser", detail)
        run.assert_not_called()

    def test_the_flag_search_does_not_report_untestable_as_nothing_works(self):
        # [] means "tried them all, none worked" and sends the reader to the
        # share's permissions. None means "never got to try", which must not
        # send them anywhere.
        me, euid = self.as_user("eduard", root=False)
        with me, euid:
            self.assertIsNone(enc.probe_rsync_flags(self.tmp, "timelapse"))

    def test_the_probe_file_is_cleaned_up_either_way(self):
        for rc in (0, 23):
            with mock.patch.object(enc.subprocess, "run", self.fake_run(rc)):
                enc.try_rsync_args(self.tmp, ["-a"])
            self.assertEqual(os.listdir(self.tmp), [], f"rc={rc}")

    def test_probe_returns_the_first_flag_set_that_works(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            # Refuse -a without --no-owner, the CIFS shape.
            bad = "-a" in cmd and "--no-owner" not in cmd
            return mock.Mock(returncode=23 if bad else 0, stderr="chgrp")

        with mock.patch.object(enc.subprocess, "run", run):
            self.assertEqual(enc.probe_rsync_flags(self.tmp), ["-rt", "--partial"])

    def test_probe_returns_empty_when_nothing_works(self):
        with mock.patch.object(enc.subprocess, "run", self.fake_run(23)):
            self.assertEqual(enc.probe_rsync_flags(self.tmp), [])

    def test_probe_returns_none_when_it_cannot_be_tested(self):
        # None and [] mean different things: "unknown" and "nothing works".
        with mock.patch.object(enc.shutil, "which", lambda n: None):
            self.assertIsNone(enc.probe_rsync_flags(self.tmp))

    def test_dash_a_is_accepted_when_the_share_accepts_it(self):
        # The false positive that started this: -a on CIFS is not a fault.
        with mock.patch.object(enc.subprocess, "run", self.fake_run(0)):
            self.assertEqual(enc.probe_rsync_flags(self.tmp), ["-a", "--partial"])


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

    def test_coverage_uses_the_cameras_own_interval(self):
        # A full day at one frame a minute is 1,440 frames. Measured against
        # the global 5s interval that reads as 8% coverage: a complete day
        # reported as a near-total outage, every single night.
        out = enc.build_summary([self.row(frames=1440, interval=60)], 5)
        self.assertIn("100", out)
        self.assertNotIn(" 8 ", out)

    def test_a_row_without_an_interval_falls_back_to_the_global(self):
        # Rows built before this existed, and any caller that does not set it.
        row = self.row()
        row.pop("interval", None)
        self.assertIn("100", enc.build_summary([row], 5))

    # -- width ------------------------------------------------------------
    # Reported from the real deployment: Discord renders an embed's
    # description in a column narrower than an ordinary message, and the
    # fixed 62-column table wrapped its last field onto a second line
    # underneath the first. About 50 columns survive; 48 leaves a margin for
    # a narrower client.
    BUDGET = 48

    def widest(self, out):
        return max(len(line) for line in out.splitlines())

    def test_a_full_house_fits_the_discord_embed(self):
        rows = [self.row(camera=n) for n in
                ("Court180", "Doorbell", "Garage", "Gate", "Roof",
                 "Street4K", "Workshop")]
        self.assertLessEqual(self.widest(enc.build_summary(rows, 5)),
                             self.BUDGET)

    def test_even_the_widest_plausible_row_fits(self):
        # Every column at its maximum at once: a name at the 12-char cap, the
        # longest status, a one-second cadence, three-digit megabytes and an
        # encode over an hour, which is the form human_duration writes widest.
        row = self.row(camera="BackCourtyardWest", status="FAIL", frames=86400,
                       size=int(1023.9 * 1024 ** 2), seconds=3723, interval=1)
        self.assertLessEqual(self.widest(enc.build_summary([row], 5)), 52)

    def test_the_date_is_a_heading_not_a_column(self):
        rows = [self.row(camera="Roof"), self.row(camera="Gate")]
        out = enc.build_summary(rows, 5)
        self.assertEqual(out.count("2026-08-04"), 1)

    def test_each_date_gets_its_own_block_in_one_code_fence(self):
        # A catch-up run after an outage encodes several days at once.
        rows = [self.row(date="2026-08-04"), self.row(date="2026-08-03")]
        out = enc.build_summary(rows, 5)
        self.assertEqual(out.count("2026-08-03"), 1)
        self.assertEqual(out.count("2026-08-04"), 1)
        self.assertEqual(out.count("```"), 2)
        # Oldest first, the order the days were captured in.
        self.assertLess(out.index("2026-08-03"), out.index("2026-08-04"))

    def test_columns_size_to_their_content(self):
        # The old Time column was 8 wide and "1h 02m 03s" is 10, so a slow
        # encode pushed every column after it out of line.
        out = enc.build_summary([self.row(camera="Roof", seconds=3723)], 5)
        self.assertIn("1h 02m 03s", out)
        lines = out.splitlines()
        head = next(ln for ln in lines if ln.startswith("Camera"))
        body = next(ln for ln in lines if ln.startswith("Roof"))
        self.assertEqual(len(head), len(body))

    def test_no_results_is_not_an_empty_code_block(self):
        self.assertEqual(enc.build_summary([], 5), "Nothing to report.")


class TestPerCameraEncodeSettings(unittest.TestCase):

    CFG = {"capture": {"interval_seconds": 5},
           "encode": {"framerate": 60, "gop": 120},
           "cameras": [{"name": "Roof", "interval_seconds": 60,
                        "framerate": 30},
                       {"name": "Drive"}]}

    def test_a_camera_without_overrides_uses_the_globals(self):
        cam = enc.camera_entry(self.CFG, "Drive")
        self.assertEqual(enc.camera_framerate(self.CFG, cam), 60)
        self.assertEqual(enc.camera_interval(self.CFG, cam), 5)
        self.assertEqual(enc.camera_gop(self.CFG, cam), 120)

    def test_a_camera_with_overrides_uses_them(self):
        cam = enc.camera_entry(self.CFG, "Roof")
        self.assertEqual(enc.camera_framerate(self.CFG, cam), 30)
        self.assertEqual(enc.camera_interval(self.CFG, cam), 60)

    def test_gop_follows_a_per_camera_frame_rate(self):
        # 120 frames is two seconds at 60fps and four at 30. Left global, a
        # camera at 30fps silently gets half the keyframes it should.
        cam = enc.camera_entry(self.CFG, "Roof")
        self.assertEqual(enc.camera_gop(self.CFG, cam), 30 * enc.GOP_SECONDS)

    def test_an_explicit_per_camera_gop_wins(self):
        self.assertEqual(
            enc.camera_gop(self.CFG, {"framerate": 30, "gop": 250}), 250)

    def test_a_hand_tuned_global_gop_survives_for_cameras_that_follow(self):
        # Only a camera setting its own frame rate gets a derived gop; one
        # following the globals keeps whatever was configured by hand.
        cfg = {"capture": {"interval_seconds": 5},
               "encode": {"framerate": 60, "gop": 250}}
        self.assertEqual(enc.camera_gop(cfg, {}), 250)

    def test_an_unknown_camera_gets_the_defaults_not_an_error(self):
        self.assertEqual(enc.camera_entry(self.CFG, "Ghost"), {})
        self.assertEqual(enc.camera_framerate(self.CFG, {}), 60)

    def test_a_days_recorded_cadence_beats_the_config(self):
        # A cadence edit takes effect at midnight, so tonight's encode is of a
        # day that ran on the *old* settings. Reading the config here would
        # measure yesterday against a cadence yesterday never ran at.
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path
        tmp = _Path(_tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / enc.CADENCE_FILE).write_text(
            _json.dumps({"interval_seconds": 5, "framerate": 60}),
            encoding="utf-8")
        self.assertEqual(enc.day_cadence(tmp), (5, 60))

    def test_an_unmarked_day_falls_back_to_the_config(self):
        import tempfile as _tempfile
        from pathlib import Path as _Path
        tmp = _Path(_tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertIsNone(enc.day_cadence(tmp))

    def test_a_corrupt_marker_falls_back_rather_than_failing_the_day(self):
        import tempfile as _tempfile
        from pathlib import Path as _Path
        tmp = _Path(_tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / enc.CADENCE_FILE).write_text("{ nope", encoding="utf-8")
        self.assertIsNone(enc.day_cadence(tmp))

    def test_encode_day_actually_prefers_the_recorded_cadence(self):
        """The one that matters, and the one the isolated helper tests miss.

        A cadence edit takes effect at midnight, so tonight's run encodes a day
        that ran on the *old* settings. `day_cadence()` returning the right
        answer is worth nothing if `encode_day()` does not ask it.
        """
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path
        from unittest import mock as _mock
        from _support import make_frame

        tmp = _Path(_tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        day = tmp / "frames" / "Roof" / "2026-08-10"
        for i in range(5):
            make_frame(day / ("%06d.jpg" % (i * 5)))
        (day / enc.CADENCE_FILE).write_text(
            _json.dumps({"interval_seconds": 5, "framerate": 60}),
            encoding="utf-8")

        # The config has since been changed to the slower cadence.
        cfg = {"capture": {"interval_seconds": 5, "min_bytes": 100},
               "encode": {"framerate": 60, "gop": 120, "min_frames": 1,
                          "container": "mkv"},
               "paths": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
               "cameras": [{"name": "Roof", "interval_seconds": 60,
                            "framerate": 30}]}
        out = tmp / "videos"
        out.mkdir()
        with _mock.patch.object(enc, "probe_dimensions", return_value=(512, 512)):
            r = enc.encode_day(cfg, {"codec": "libx264", "args": []},
                               "Roof", day, out, dry_run=True)
        self.assertEqual(r["fps"], 60)          # not the config's 30
        self.assertEqual(r["interval"], 5)      # not the config's 60

    def test_encode_day_falls_back_to_the_config_for_an_unmarked_day(self):
        import tempfile as _tempfile
        from pathlib import Path as _Path
        from unittest import mock as _mock
        from _support import make_frame

        tmp = _Path(_tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        day = tmp / "frames" / "Roof" / "2026-08-10"
        for i in range(5):
            make_frame(day / ("%06d.jpg" % (i * 5)))
        cfg = {"capture": {"interval_seconds": 5, "min_bytes": 100},
               "encode": {"framerate": 60, "gop": 120, "min_frames": 1,
                          "container": "mkv"},
               "paths": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
               "cameras": [{"name": "Roof", "interval_seconds": 60,
                            "framerate": 30}]}
        out = tmp / "videos"
        out.mkdir()
        with _mock.patch.object(enc, "probe_dimensions", return_value=(512, 512)):
            r = enc.encode_day(cfg, {"codec": "libx264", "args": []},
                               "Roof", day, out, dry_run=True)
        self.assertEqual((r["fps"], r["interval"]), (30, 60))

    def test_the_arguments_are_rebuilt_for_that_gop(self):
        # The codec is probed once per run; the arguments must still carry
        # this camera's keyframe interval rather than the run's.
        args = enc.build_candidates({"gop": 120}, gop=60)[0]["args"]
        self.assertIn("60", args)
        self.assertNotIn("120", args)


class TestCameraSmoothing(unittest.TestCase):
    """Per-camera tmix. Absence means off, and nothing else may turn it on."""

    def test_a_camera_that_says_nothing_gets_no_smoothing(self):
        # The whole opt-in claim rests on this: every existing config on every
        # existing install is a camera that says nothing.
        self.assertEqual(enc.camera_smoothing({}), 0)

    def test_a_configured_value_is_used(self):
        self.assertEqual(enc.camera_smoothing({"smooth_frames": 15}), 15)

    def test_there_is_no_global_to_inherit(self):
        # interval_seconds and framerate fall back to encode/capture defaults.
        # This one deliberately does not, so putting it there does nothing.
        self.assertEqual(enc.camera_smoothing({}), 0)

    def test_a_value_under_the_floor_reads_as_off(self):
        # A leftover 0, or a 1 or 2 from someone guessing at the units. None
        # can average anything, and each must mean off rather than emit a
        # filter that does nothing or fails.
        for value in (0, 1, 2, -5):
            self.assertEqual(enc.camera_smoothing({"smooth_frames": value}), 0,
                             f"{value} should read as off")

    def test_an_absurd_value_is_clamped_rather_than_obeyed(self):
        # This is read off a file an operator may have edited by hand, and the
        # cost of believing it is buffering that many frames of 4K per camera.
        self.assertEqual(enc.camera_smoothing({"smooth_frames": 500}),
                         enc.SMOOTH_MAX)

    def test_junk_is_off_rather_than_a_traceback(self):
        # A oneshot that dies on a malformed key takes the whole night's
        # encode with it, for every camera, not just the mistyped one.
        for value in ("lots", None, [], {}, "15x"):
            self.assertEqual(enc.camera_smoothing({"smooth_frames": value}), 0,
                             f"{value!r} should read as off")

    def test_a_string_number_is_accepted(self):
        # JSON written by hand puts quotes in odd places.
        self.assertEqual(enc.camera_smoothing({"smooth_frames": "15"}), 15)

    def test_the_offered_default_sits_inside_the_bounds(self):
        self.assertLessEqual(enc.SMOOTH_MIN, enc.SMOOTH_DEFAULT)
        self.assertLessEqual(enc.SMOOTH_DEFAULT, enc.SMOOTH_MAX)


class TestSmoothingReachesFfmpeg(unittest.TestCase):
    """camera_smoothing() returning 15 is worth nothing if -vf never says so.

    Same reasoning as the cadence and marker tests: the accessor is the easy
    half, and the wiring is the half that has actually broken here before.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.day = self.tmp / "frames" / "Roof" / "2026-08-10"
        for i in range(5):
            make_frame(self.day / ("%06d.jpg" % (i * 5)))
        self.out = self.tmp / "videos"
        self.out.mkdir()

    def vf_for(self, cam):
        """The -vf argument encode_day() builds for this camera entry."""
        cfg = {"capture": {"interval_seconds": 5, "min_bytes": 100},
               "encode": {"framerate": 60, "gop": 120, "min_frames": 1,
                          "container": "mkv"},
               "paths": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
               "cameras": [dict(cam, name="Roof")]}
        seen = {}

        def fake_run(cmd, **kw):
            seen["vf"] = cmd[cmd.index("-vf") + 1]
            Path(cmd[-1]).write_bytes(b"\0" * 4096)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(enc, "probe_dimensions", return_value=(512, 512)), \
                mock.patch.object(enc.subprocess, "run", side_effect=fake_run):
            enc.encode_day(cfg, {"codec": "libx264", "args": [],
                                 "name": "libx264 (CPU)"},
                           "Roof", self.day, self.out, False)
        return seen["vf"]

    def test_an_unsmoothed_camera_gets_no_tmix_at_all(self):
        self.assertNotIn("tmix", self.vf_for({}))

    def test_a_smoothed_camera_gets_its_own_frame_count(self):
        self.assertIn("tmix=frames=15", self.vf_for({"smooth_frames": 15}))

    def test_tmix_runs_last_so_it_averages_converted_pixels(self):
        # Ahead of the range conversion it would average full-range JPEG and
        # hand the scaler something it was not told about.
        vf = self.vf_for({"smooth_frames": 15})
        self.assertLess(vf.index("out_range=limited"), vf.index("tmix"))
        self.assertLess(vf.index(f"format={enc.PIX_FMT}"), vf.index("tmix"))

    def test_a_clamped_value_reaches_ffmpeg_clamped(self):
        self.assertIn(f"tmix=frames={enc.SMOOTH_MAX}",
                      self.vf_for({"smooth_frames": 900}))

    def test_an_edit_today_smooths_the_day_it_was_made_on(self):
        """Turning smoothing on at 14:00 smooths that same day's video.

        The nightly run encodes the day that has just finished and reads the
        config as it stands then, so there is nothing to wait for. This is
        deliberately unlike a cadence change, which is pinned to what the day
        was *captured* at precisely so an edit cannot land mid-day. Smoothing
        is not a property of the capture, so it has no such anchor, and a day
        already under way is the normal thing for it to apply to.
        """
        (self.day / enc.CADENCE_FILE).write_text(
            json.dumps({"interval_seconds": 5, "framerate": 60}),
            encoding="utf-8")
        vf = self.vf_for({"smooth_frames": 15})
        self.assertIn("tmix=frames=15", vf)

    def test_the_cadence_marker_cannot_pin_smoothing_off(self):
        # A day recorded at a cadence that predates the setting must still be
        # smoothed. If smoothing ever moved into .cadence.json, turning it on
        # would silently skip every day already under way, which is every day
        # anyone would think to turn it on for.
        (self.day / enc.CADENCE_FILE).write_text(
            json.dumps({"interval_seconds": 60, "framerate": 30}),
            encoding="utf-8")
        self.assertIn("tmix=frames=8", self.vf_for({"smooth_frames": 8}))

    def test_the_marker_carries_cadence_only(self):
        # The read side of the same claim: day_cadence() has nowhere to put a
        # smoothing value even if something wrote one.
        (self.day / enc.CADENCE_FILE).write_text(
            json.dumps({"interval_seconds": 5, "framerate": 60,
                        "smooth_frames": 30}), encoding="utf-8")
        self.assertEqual(enc.day_cadence(self.day), (5, 60))

    def test_smoothing_does_not_disturb_the_colour_tagging(self):
        # The filter sits in the same chain as the range conversion, and
        # getting this wrong is invisible until someone plays the file.
        vf = self.vf_for({"smooth_frames": 15})
        self.assertIn("in_range=full:out_range=limited", vf)


class TestEncodeDayMarksTheDay(unittest.TestCase):
    """`day_encoded()` answering correctly is worth nothing if nothing writes.

    Same reasoning as the cadence test above: the helpers are cheap to test in
    isolation and that is exactly why the wiring is the part that breaks.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.day = self.tmp / "frames" / "Roof" / "2026-08-10"
        for i in range(5):
            make_frame(self.day / ("%06d.jpg" % (i * 5)))
        self.out = self.tmp / "videos"
        self.out.mkdir()
        self.cfg = {"capture": {"interval_seconds": 5, "min_bytes": 100},
                    "encode": {"framerate": 60, "gop": 120, "min_frames": 1,
                               "container": "mkv"},
                    "paths": {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
                    "cameras": [{"name": "Roof"}]}

    def run_day(self, rc=0, produce=True, dry_run=False):
        """encode_day() with ffmpeg replaced by something that writes a file."""
        def fake_run(cmd, **kw):
            if produce:
                Path(cmd[-1]).write_bytes(b"\0" * 4096)
            return mock.Mock(returncode=rc, stdout="", stderr="boom")

        with mock.patch.object(enc, "probe_dimensions", return_value=(512, 512)), \
                mock.patch.object(enc.subprocess, "run", side_effect=fake_run):
            return enc.encode_day(self.cfg, {"codec": "libx264", "args": [],
                                             "name": "libx264 (CPU)"},
                                  "Roof", self.day, self.out, dry_run)

    def test_a_successful_day_is_marked(self):
        r = self.run_day()
        self.assertEqual(r["status"], "OK")
        marker = enc.day_encoded(self.day)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["video"], "Roof.20260810.mkv")
        self.assertEqual(marker["frames"], 5)
        self.assertEqual(marker["encoder"], "libx264 (CPU)")

    def test_a_marked_day_is_the_one_find_pending_then_skips(self):
        # The round trip through both halves, because each half passing its
        # own test proves nothing about the pair.
        self.run_day()
        jobs, done = enc.find_pending(self.tmp / "frames", ["Roof"],
                                      None, 7, force=False)
        self.assertEqual(jobs, [])
        self.assertEqual(done, 1)

    def test_a_failed_encode_is_not_marked(self):
        r = self.run_day(rc=1, produce=False)
        self.assertEqual(r["status"], "FAIL")
        self.assertIsNone(enc.day_encoded(self.day))

    def test_an_output_that_never_appeared_is_not_marked(self):
        # ffmpeg exiting 0 having written nothing usable is a failure too, and
        # marking it would strand the day permanently.
        r = self.run_day(rc=0, produce=False)
        self.assertEqual(r["status"], "FAIL")
        self.assertIsNone(enc.day_encoded(self.day))

    def test_a_skipped_day_is_not_marked(self):
        self.cfg["encode"]["min_frames"] = 100
        r = self.run_day()
        self.assertEqual(r["status"], "SKIP")
        self.assertIsNone(enc.day_encoded(self.day))

    def test_a_dry_run_is_not_marked(self):
        # --dry-run must be able to answer "what would run tonight" twice.
        r = self.run_day(dry_run=True)
        self.assertEqual(r["status"], "DRY")
        self.assertIsNone(enc.day_encoded(self.day))

    def test_the_marker_is_not_mistaken_for_a_frame(self):
        self.run_day()
        good, bad = enc.valid_frames(self.day, 100)
        self.assertEqual((len(good), bad), (5, 0))


class SinkHarness(unittest.TestCase):
    """Captures every request a notification attempt would make.

    No network, ever. urlopen is replaced, and a test that reached the wire
    would be a test that fails on a build machine with no internet and posts
    to somebody's real channel on the machine that does.
    """

    def setUp(self):
        self.sent = []
        self.fail_with = None

        class FakeResponse:
            status = 200

            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            self.sent.append(req)
            if self.fail_with:
                raise self.fail_with
            return FakeResponse()

        original = enc.urlrequest.urlopen
        enc.urlrequest.urlopen = fake_urlopen
        self.addCleanup(setattr, enc.urlrequest, "urlopen", original)

    def body(self, index=0):
        return json.loads(self.sent[index].data.decode("utf-8"))

    def url(self, index=0):
        return self.sent[index].full_url


class TestNotifySinkSelection(SinkHarness):

    def test_no_configuration_sends_nothing(self):
        sent, failed = enc.notify({}, "t", "d", "ok", [])
        self.assertEqual((sent, failed, self.sent), (0, 0, []))

    def test_the_legacy_discord_block_still_works(self):
        """Every config written before 0.1.6 has this shape, and upgrades keep
        the existing config, so it has to go on working untouched."""
        cfg = {"discord": {"enabled": True, "webhook_url": "https://d/x",
                           "username": "Bot"}}
        sent, failed = enc.notify(cfg, "Title", "Desc", "ok", [])
        self.assertEqual((sent, failed), (1, 0))
        self.assertEqual(self.url(), "https://d/x")

    def test_a_disabled_legacy_block_sends_nothing(self):
        cfg = {"discord": {"enabled": False, "webhook_url": "https://d/x"}}
        self.assertEqual(enc.notify(cfg, "t", "d", "ok", []), (0, 0))

    def test_a_legacy_block_with_no_url_sends_nothing(self):
        cfg = {"discord": {"enabled": True, "webhook_url": ""}}
        self.assertEqual(enc.notify(cfg, "t", "d", "ok", []), (0, 0))

    def test_the_notify_list_is_authoritative_when_present(self):
        # Both configured; the legacy block must not produce a second message.
        cfg = {"discord": {"enabled": True, "webhook_url": "https://old/x"},
               "notify": [{"type": "discord", "enabled": True,
                           "webhook_url": "https://new/x"}]}
        enc.notify(cfg, "t", "d", "ok", [])
        self.assertEqual([self.url(i) for i in range(len(self.sent))],
                         ["https://new/x"])

    def test_disabled_sinks_are_skipped(self):
        cfg = {"notify": [
            {"type": "discord", "enabled": False, "webhook_url": "https://a/x"},
            {"type": "discord", "enabled": True, "webhook_url": "https://b/x"}]}
        enc.notify(cfg, "t", "d", "ok", [])
        self.assertEqual([self.url(i) for i in range(len(self.sent))],
                         ["https://b/x"])

    def test_several_sinks_all_get_it(self):
        cfg = {"notify": [
            {"type": "discord", "enabled": True, "webhook_url": "https://d/x"},
            {"type": "ntfy", "enabled": True, "topic": "tl"},
            {"type": "telegram", "enabled": True, "token": "T", "chat_id": "1"}]}
        self.assertEqual(enc.notify(cfg, "t", "d", "ok", []), (3, 0))
        self.assertEqual(len(self.sent), 3)

    def test_one_sink_failing_does_not_stop_the_others(self):
        """The hard requirement. A failed notification must never take down
        the run it is reporting on, nor the sink after it."""
        cfg = {"notify": [
            {"type": "discord", "enabled": True, "webhook_url": ""},
            {"type": "ntfy", "enabled": True, "topic": "tl"}]}
        with self.assertLogs("encode", level="WARNING"):
            sent, failed = enc.notify(cfg, "t", "d", "ok", [])
        self.assertEqual((sent, failed), (1, 1))

    def test_a_transport_error_is_caught_not_raised(self):
        self.fail_with = OSError("connection reset")
        cfg = {"notify": [{"type": "ntfy", "enabled": True, "topic": "tl"}]}
        with self.assertLogs("encode", level="WARNING") as cm:
            self.assertEqual(enc.notify(cfg, "t", "d", "ok", []), (0, 1))
        self.assertIn("connection reset", "\n".join(cm.output))

    def test_an_unknown_type_says_what_it_knows(self):
        cfg = {"notify": [{"type": "carrier-pigeon", "enabled": True}]}
        with self.assertLogs("encode", level="WARNING") as cm:
            enc.notify(cfg, "t", "d", "ok", [])
        out = "\n".join(cm.output)
        self.assertIn("carrier-pigeon", out)
        self.assertIn("discord", out)
        self.assertIn("ntfy", out)
        self.assertIn("telegram", out)

    def test_a_sink_with_no_type_is_a_discord_webhook(self):
        # What the legacy block looks like once it is moved into the list.
        cfg = {"notify": [{"enabled": True, "webhook_url": "https://d/x"}]}
        self.assertEqual(enc.notify(cfg, "t", "d", "ok", []), (1, 0))

    def test_the_old_entry_point_still_works(self):
        # send_discord() was the only one for six releases; a fork may use it.
        cfg = {"discord": {"enabled": True, "webhook_url": "https://d/x"}}
        enc.send_discord(cfg, "t", "d", 0x2ECC71, [])
        self.assertEqual(self.body()["embeds"][0]["color"], 0x2ECC71)


class TestDiscordSink(SinkHarness):

    def send(self, level="ok", description="body", fields=()):
        cfg = {"notify": [{"type": "discord", "enabled": True,
                           "webhook_url": "https://d/x", "username": "Bot"}]}
        enc.notify(cfg, "Title", description, level, list(fields))
        return self.body()

    def test_the_embed_shape_is_unchanged(self):
        got = self.send(fields=[("Encoder", "AV1")])
        embed = got["embeds"][0]
        self.assertEqual(got["username"], "Bot")
        self.assertEqual(embed["title"], "Title")
        self.assertEqual(embed["description"], "body")
        self.assertEqual(embed["fields"][0], {"name": "Encoder",
                                              "value": "AV1", "inline": False})
        self.assertIn("timestamp", embed)

    def test_levels_map_to_the_colours_that_were_hardcoded_before(self):
        for level, colour in (("ok", 0x2ECC71), ("info", 0x95A5A6),
                              ("warn", 0xF1C40F), ("error", 0xE74C3C)):
            self.sent.clear()
            self.assertEqual(self.send(level)["embeds"][0]["color"], colour)

    def test_an_empty_field_value_becomes_a_dash(self):
        # Discord rejects an empty field value with a 400.
        self.assertEqual(self.send(fields=[("Note", "")])["embeds"][0]
                         ["fields"][0]["value"], "-")

    def test_long_text_is_trimmed_and_says_so(self):
        embed = self.send(description="x" * 5000)["embeds"][0]
        self.assertLessEqual(len(embed["description"]), enc.DISCORD_DESC_LIMIT)
        self.assertIn("truncated", embed["description"])

    def test_at_most_25_fields(self):
        embed = self.send(fields=[(f"f{i}", "v") for i in range(40)])["embeds"][0]
        self.assertEqual(len(embed["fields"]), 25)

    def test_it_keeps_the_discord_user_agent(self):
        self.send()
        self.assertTrue(self.sent[0].get_header("User-agent")
                        .startswith("DiscordBot ("))


class TestNtfySink(SinkHarness):

    def send(self, sink=None, title="Title", description="body", level="ok",
             fields=()):
        base = {"type": "ntfy", "enabled": True, "topic": "timelapse"}
        base.update(sink or {})
        enc.notify({"notify": [base]}, title, description, level, list(fields))

    def test_it_posts_json_to_the_server_root(self):
        """Not text to /topic, deliberately: that form carries the title in an
        HTTP header, headers are ASCII, and every title here starts with an
        emoji."""
        self.send(title="✅ Timelapse")
        self.assertEqual(self.url(), "https://ntfy.sh")
        self.assertEqual(self.body()["topic"], "timelapse")
        self.assertEqual(self.body()["title"], "✅ Timelapse")

    def test_a_self_hosted_server_is_honoured(self):
        self.send({"server": "https://ntfy.example.lan/"})
        self.assertEqual(self.url(), "https://ntfy.example.lan")

    def test_the_message_carries_the_summary_and_the_fields(self):
        self.send(description="TABLE HERE", fields=[("Encoder", "AV1")])
        msg = self.body()["message"]
        self.assertIn("TABLE HERE", msg)
        self.assertIn("Encoder: AV1", msg)

    def test_severity_becomes_priority(self):
        for level, priority in (("ok", 3), ("info", 2), ("warn", 4),
                                ("error", 5)):
            self.sent.clear()
            self.send(level=level)
            self.assertEqual(self.body()["priority"], priority, level)

    def test_a_configured_priority_wins(self):
        self.send({"priority": 1}, level="error")
        self.assertEqual(self.body()["priority"], 1)

    def test_a_token_becomes_a_bearer_header(self):
        self.send({"token": "tk_secret"})
        self.assertEqual(self.sent[0].get_header("Authorization"),
                         "Bearer tk_secret")

    def test_no_token_means_no_authorization_header(self):
        self.send()
        self.assertIsNone(self.sent[0].get_header("Authorization"))

    def test_tags_are_split_on_commas(self):
        self.send({"tags": "camera, night"})
        self.assertEqual(self.body()["tags"], ["camera", "night"])

    def test_no_topic_is_a_configuration_error_not_a_crash(self):
        with self.assertLogs("encode", level="WARNING") as cm:
            self.send({"topic": ""})
        self.assertIn("topic", "\n".join(cm.output))
        self.assertEqual(self.sent, [])

    def test_it_does_not_claim_to_be_a_discord_bot(self):
        self.send()
        agent = self.sent[0].get_header("User-agent")
        self.assertIn("timelapse-maker", agent)
        self.assertNotIn("DiscordBot", agent)

    def test_a_long_message_is_trimmed(self):
        self.send(description="x" * 6000)
        self.assertLessEqual(len(self.body()["message"]), enc.NTFY_LIMIT)


class TestTelegramSink(SinkHarness):

    def send(self, sink=None, title="Title", description="body", fields=()):
        base = {"type": "telegram", "enabled": True,
                "token": "123:ABC", "chat_id": "-100200"}
        base.update(sink or {})
        enc.notify({"notify": [base]}, title, description, "ok", list(fields))

    def test_it_calls_sendmessage_with_the_bot_token_in_the_path(self):
        self.send()
        self.assertEqual(self.url(),
                         "https://api.telegram.org/bot123:ABC/sendMessage")
        self.assertEqual(self.body()["chat_id"], "-100200")

    def test_the_table_is_wrapped_in_pre_so_it_stays_monospace(self):
        """A proportional font turns the summary table into rubble, which is
        the whole reason this uses HTML rather than plain text."""
        self.send(description="Camera  St  Frames")
        text = self.body()["text"]
        self.assertIn("<pre>", text)
        self.assertIn("Camera  St  Frames", text)
        self.assertEqual(self.body()["parse_mode"], "HTML")

    def test_html_special_characters_are_escaped(self):
        # An unescaped < is a 400 from the API, not a wrong-looking message.
        self.send(title="A & B", description="1 < 2 > 0 & <b>x</b>")
        text = self.body()["text"]
        self.assertIn("A &amp; B", text)
        self.assertIn("1 &lt; 2 &gt; 0 &amp; &lt;b&gt;x&lt;/b&gt;", text)

    def test_the_title_is_bold_and_outside_the_pre_block(self):
        self.send(title="Timelapse")
        self.assertTrue(self.body()["text"].startswith("<b>Timelapse</b>"))

    def test_fields_are_included(self):
        self.send(fields=[("Transfer", "OK")])
        self.assertIn("Transfer: OK", self.body()["text"])

    def test_it_is_trimmed_to_telegrams_limit(self):
        self.send(description="x" * 9000)
        self.assertLessEqual(len(self.body()["text"]), enc.TELEGRAM_LIMIT)

    def test_link_previews_are_off(self):
        self.send()
        self.assertTrue(self.body()["disable_web_page_preview"])

    def test_missing_credentials_are_a_configuration_error(self):
        for sink in ({"token": ""}, {"chat_id": ""}):
            self.sent.clear()
            with self.assertLogs("encode", level="WARNING") as cm:
                self.send(sink)
            self.assertIn("chat_id", "\n".join(cm.output))
            self.assertEqual(self.sent, [])

    def test_it_does_not_claim_to_be_a_discord_bot(self):
        self.send()
        self.assertNotIn("DiscordBot", self.sent[0].get_header("User-agent"))


class TestSinkCredentialsAreRedacted(unittest.TestCase):
    """Three new places a credential can now sit in the config.

    `timelapse config --redacted` exists to be pasted into a bug report, and
    the existing key rules happen to cover all of these. "Happen to" is why
    this is pinned: the next sink type may not be so lucky.
    """

    def redact(self, sink):
        return enc.redact_config({"notify": [sink]})["notify"][0]

    def test_a_telegram_bot_token_is_masked(self):
        # Whoever holds it controls the bot.
        got = self.redact({"type": "telegram", "token": "123456:SECRET",
                           "chat_id": "-100"})
        self.assertEqual(got["token"], enc.MASK)
        self.assertEqual(got["chat_id"], "-100")   # not a secret, and useful

    def test_an_ntfy_token_is_masked(self):
        got = self.redact({"type": "ntfy", "topic": "t", "token": "tk_SECRET"})
        self.assertEqual(got["token"], enc.MASK)
        self.assertEqual(got["topic"], "t")

    def test_a_discord_webhook_keeps_its_id_and_loses_its_secret(self):
        got = self.redact({"type": "discord",
                           "webhook_url": "https://discord.com/api/webhooks/17/SECRET"})
        self.assertIn("17", got["webhook_url"])
        self.assertNotIn("SECRET", got["webhook_url"])

    def test_an_empty_token_stays_empty(self):
        # "" answers "did you set one?", which is what a bug report needs.
        self.assertEqual(self.redact({"type": "ntfy", "token": ""})["token"], "")


class TestStateDir(unittest.TestCase):

    def test_the_configured_directory_wins(self):
        self.assertEqual(
            enc.state_dir({"paths": {"state_dir": "/srv/tl/state"}}).as_posix(),
            "/srv/tl/state")

    def test_absent_falls_back_to_the_default(self):
        # Every config written before 0.1.6 lacks the key, and upgrades keep
        # the existing config, so this is the normal case for a while.
        # str(), not as_posix(): STATE_DIR_DEFAULT is the platform's own
        # spelling, and on Windows as_posix() would turn it into a path no
        # caller ever sees.
        self.assertEqual(str(enc.state_dir({"paths": {}})),
                         enc.STATE_DIR_DEFAULT)

    def test_an_empty_or_blank_value_falls_back_too(self):
        for value in ("", "   ", None):
            self.assertEqual(
                str(enc.state_dir({"paths": {"state_dir": value}})),
                enc.STATE_DIR_DEFAULT)

    def test_a_config_with_no_paths_block_at_all(self):
        self.assertEqual(str(enc.state_dir({})), enc.STATE_DIR_DEFAULT)

    def test_it_is_not_the_web_index_directory(self):
        # The web UI's state_dir lives under web, not paths, and means
        # something else entirely: the one directory that service may write.
        cfg = {"paths": {}, "web": {"state_dir": "/var/lib/timelapse/web"}}
        self.assertEqual(str(enc.state_dir(cfg)), enc.STATE_DIR_DEFAULT)

    def test_the_state_files_are_named_once(self):
        self.assertEqual((enc.CAPTURE_STATE, enc.ENCODE_STATE),
                         ("capture.json", "encode.json"))
        self.assertEqual(enc.STATE_VERSION, 1)


class TestRunRecord(unittest.TestCase):
    """What the nightly job leaves behind for anything that wants to know.

    The numbers all existed already: they were formatted into a Discord table
    and thrown away, so a status page could say nothing about last night.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = {"paths": {"state_dir": str(self.tmp)}}
        self.enc = {"name": "H.265 (hevc_nvenc)", "codec": "hevc_nvenc"}

    def result(self, **kw):
        base = {"camera": "Roof", "date": "2026-08-11", "status": "OK",
                "frames": 17280, "bad": 0, "size": 1024, "seconds": 12.34,
                "note": "", "interval": 5, "fps": 60}
        base.update(kw)
        return base

    def read(self):
        return json.loads((self.tmp / enc.ENCODE_STATE)
                          .read_text(encoding="utf-8"))

    def test_a_run_is_recorded_with_its_days(self):
        enc.write_run_state(self.cfg, enc.run_record(
            time.time() - 90, self.enc, [self.result()], None, 5))
        got = self.read()
        self.assertEqual(got["version"], enc.STATE_VERSION)
        self.assertEqual(got["kind"], "encode")
        run = got["runs"][0]
        self.assertEqual(run["ok"], 1)
        self.assertEqual(run["encoder"], "H.265 (hevc_nvenc)")
        self.assertEqual(run["days"][0]["camera"], "Roof")
        self.assertGreaterEqual(run["seconds"], 90)

    def test_coverage_is_computed_against_the_cameras_own_cadence(self):
        # A camera at one frame a minute is not at 8% because the global
        # interval is 5s. This is the bug the Discord table already had.
        run = enc.run_record(time.time(), self.enc,
                             [self.result(frames=1440, interval=60)], None, 5)
        self.assertEqual(run["days"][0]["coverage"], 100.0)

    def test_coverage_matches_what_the_discord_table_says(self):
        """One formula, two consumers. They used to be one formula in one
        consumer, and the file would have been the copy that drifted."""
        r = self.result(frames=8640, interval=5)
        run = enc.run_record(time.time(), self.enc, [r], None, 5)
        self.assertEqual(run["days"][0]["coverage"], 50.0)
        self.assertIn("50", enc.build_summary([r], 5))

    def test_a_day_with_no_frames_has_no_coverage_rather_than_zero(self):
        run = enc.run_record(time.time(), self.enc,
                             [self.result(status="SKIP", frames=0)], None, 5)
        self.assertIsNone(run["days"][0]["coverage"])
        self.assertEqual(run["skipped"], 1)

    def test_the_transfer_outcome_is_carried(self):
        run = enc.run_record(time.time(), self.enc, [self.result()],
                             {"ok": False, "moved": 0, "detail": "exit 23"}, 5)
        self.assertFalse(run["transfer"]["ok"])
        self.assertEqual(run["transfer"]["detail"], "exit 23")

    def test_no_transfer_is_null_not_a_failed_one(self):
        # --no-transfer and "the transfer failed" must not look alike.
        run = enc.run_record(time.time(), self.enc, [self.result()], None, 5)
        self.assertIsNone(run["transfer"])

    def test_a_run_that_found_nothing_is_still_a_run(self):
        # Otherwise "the timer fired and there was nothing to do" is
        # indistinguishable from "the timer never fired".
        run = enc.run_record(time.time(), self.enc, [], None, 5)
        self.assertEqual((run["ok"], run["failed"], run["days"]), (0, 0, []))
        self.assertEqual(run["error"], "")

    def test_an_aborted_run_carries_its_error(self):
        run = enc.run_record(time.time(), None, [], None, 5,
                             error="No usable encoder found")
        self.assertIn("No usable encoder", run["error"])
        self.assertEqual(run["encoder"], "")

    def test_runs_accumulate_newest_first(self):
        for n in range(3):
            enc.write_run_state(self.cfg, enc.run_record(
                time.time(), self.enc, [self.result(camera=f"C{n}")], None, 5))
        cams = [r["days"][0]["camera"] for r in self.read()["runs"]]
        self.assertEqual(cams, ["C2", "C1", "C0"])

    def test_history_is_bounded(self):
        for n in range(enc.MAX_RUNS + 5):
            enc.write_run_state(self.cfg, enc.run_record(
                time.time(), self.enc, [], None, 5))
        self.assertEqual(len(self.read()["runs"]), enc.MAX_RUNS)

    def test_a_damaged_history_starts_a_new_one_rather_than_losing_tonight(self):
        (self.tmp / enc.ENCODE_STATE).write_text("{ truncated",
                                                 encoding="utf-8")
        self.assertTrue(enc.write_run_state(self.cfg, enc.run_record(
            time.time(), self.enc, [self.result()], None, 5)))
        self.assertEqual(len(self.read()["runs"]), 1)

    def test_a_history_of_the_wrong_shape_is_replaced_not_appended_to(self):
        (self.tmp / enc.ENCODE_STATE).write_text('{"runs": "nope"}',
                                                 encoding="utf-8")
        enc.write_run_state(self.cfg, enc.run_record(
            time.time(), self.enc, [], None, 5))
        self.assertEqual(len(self.read()["runs"]), 1)

    def test_an_unwritable_state_directory_does_not_fail_the_run(self):
        cfg = {"paths": {"state_dir": str(self.tmp / "gone")}}
        with self.assertLogs("encode", level="WARNING"):
            self.assertFalse(enc.write_run_state(
                cfg, enc.run_record(time.time(), self.enc, [], None, 5)))

    def test_it_leaves_no_temporary_file_behind(self):
        enc.write_run_state(self.cfg, enc.run_record(
            time.time(), self.enc, [], None, 5))
        self.assertEqual([p.name for p in self.tmp.iterdir()],
                         [enc.ENCODE_STATE])

    def test_main_records_before_it_notifies(self):
        """A notification sink is the part that can be disabled, unreachable
        or rate-limited; the local record must not depend on it."""
        source = inspect.getsource(enc.main)
        self.assertLess(source.index("write_run_state"),
                        source.index("notify("))

    def test_main_records_on_every_exit_path(self):
        # Nothing to do, ordinary completion, and the critical-failure handler.
        self.assertEqual(inspect.getsource(enc.main).count("write_run_state"), 3)


class TestForceIsWiredUp(unittest.TestCase):
    """main() is not unit-testable here, and this is the part that rots."""

    def test_main_passes_force_through_to_find_pending(self):
        source = inspect.getsource(enc.main)
        self.assertIn("--force", source)
        self.assertIn("args.force", source)

    def test_main_reports_what_it_skipped(self):
        # A run that quietly does nothing because every day is marked looks
        # identical to a broken one from the log.
        self.assertIn("already encoded", inspect.getsource(enc.main))


class TestRedact(unittest.TestCase):
    """The canonical rule. Every camera password in this project reaches a log
    or a page through one of these four shapes."""

    S = "Sup3rS3cret!"

    def test_a_query_string_password(self):
        out = enc.redact(f"http://cam/api.cgi?cmd=Snap&user=admin&password={self.S}")
        self.assertNotIn(self.S, out)
        self.assertIn("password=***", out)

    def test_the_rest_of_the_url_survives(self):
        # A redacted line still has to be worth keeping: the host and the
        # endpoint are what makes a capture failure diagnosable.
        out = enc.redact(f"http://192.0.2.7/cgi-bin/api.cgi?cmd=Snap&channel=0"
                         f"&user=admin&password={self.S}")
        for keep in ("192.0.2.7", "cgi-bin/api.cgi", "cmd=Snap", "channel=0",
                     "user=admin"):
            self.assertIn(keep, out)

    def test_every_spelling_of_the_key(self):
        for key in ("password", "passwd", "pwd", "pass", "secret", "token",
                    "auth", "apikey", "api_key", "api-key", "PASSWORD",
                    "Password"):
            with self.subTest(key=key):
                out = enc.redact(f"http://h/a?{key}={self.S}&x=1")
                self.assertNotIn(self.S, out)
                self.assertIn("x=1", out)

    def test_a_key_that_merely_ends_in_pass_is_left_alone(self):
        # \b in the pattern. Over-redaction is the safe direction, but not so
        # far that ordinary parameters vanish.
        self.assertEqual(enc.redact("http://h/a?bypass=no&compass=n"),
                         "http://h/a?bypass=no&compass=n")

    def test_url_userinfo_the_rtsp_shape(self):
        out = enc.redact(f"rtsp://admin:{self.S}@192.0.2.7:554/Preview_01_main")
        self.assertNotIn(self.S, out)
        self.assertIn("rtsp://admin:***@192.0.2.7:554/Preview_01_main", out)

    def test_a_discord_webhook_token(self):
        # Not a locator: whoever holds it can post to the channel.
        out = enc.redact("https://discord.com/api/webhooks/123456/abcDEF-ghi_JKL")
        self.assertNotIn("abcDEF", out)
        self.assertIn("/api/webhooks/123456/***", out)

    def test_a_url_with_no_credential_is_untouched(self):
        for clean in ("https://github.com/war4peace/timelapse-maker",
                      "http://192.0.2.7/cgi-bin/api.cgi?cmd=Snap&channel=0",
                      "rsync://nas/videos"):
            self.assertEqual(enc.redact(clean), clean)

    def test_several_credentials_in_one_line(self):
        out = enc.redact(f"tried rtsp://u:{self.S}@a/ then http://b?pwd={self.S}")
        self.assertNotIn(self.S, out)
        self.assertEqual(out.count("***"), 2)

    def test_the_value_ends_where_the_url_does(self):
        # A log line is prose with a URL in it, not a URL. Stopping only at &
        # swallowed the rest of the sentence, which hid the error message.
        out = enc.redact(f"http://h/a?password={self.S} (attempt 2)")
        self.assertIn("(attempt 2)", out)
        self.assertNotIn(self.S, out)

    def test_redacting_twice_changes_nothing(self):
        once = enc.redact(f"http://h/a?password={self.S}")
        self.assertEqual(enc.redact(once), once)

    def test_it_takes_anything_printable_not_just_strings(self):
        # Log arguments are frequently exception objects.
        exc = RuntimeError(f"failed: http://h/a?password={self.S}")
        self.assertNotIn(self.S, enc.redact(exc))


class TestRedactConfig(unittest.TestCase):
    """The same rule applied to a parsed config rather than to a log line.

    Two shapes of secret live in this file and each pass misses the other:
    `"password": "x"` has no `=` for the text rule, and a Reolink `url` hides
    the credential inside a query string where a field rule never looks.
    """

    S = "Sup3rS3cret!"

    def config(self):
        return {
            "paths": {"frames_root": "/srv/tl/frames"},
            "discord": {
                "enabled": True,
                "webhook_url": "https://discord.com/api/webhooks/123456/abcDEF",
            },
            "transfer": {"destination": "nasuser@nas:/mnt/user/tl/",
                         "rsync_args": ["-rt", "--partial"]},
            "cameras": [
                {"name": "Driveway", "auth": "digest", "username": "admin",
                 "password": self.S,
                 "url": "http://192.0.2.10/cgi-bin/snapshot.cgi?channel=1"},
                {"name": "Doorbell", "auth": "none",
                 "url": f"http://192.0.2.11/cgi-bin/api.cgi?cmd=Snap"
                        f"&user=admin&password={self.S}"},
                {"name": "Hallway", "method": "rtsp",
                 "url": f"rtsp://admin:{self.S}@192.0.2.14:554/stream1"},
            ],
        }

    def dumped(self):
        return json.dumps(enc.redact_config(self.config()))

    def test_the_secret_appears_nowhere_at_all(self):
        # The test that actually matters: one assertion over the whole tree,
        # so a credential in a shape nobody thought of still fails this.
        self.assertNotIn(self.S, self.dumped())

    def test_a_password_field(self):
        out = enc.redact_config(self.config())
        self.assertEqual(out["cameras"][0]["password"], enc.MASK)

    def test_a_password_inside_a_url(self):
        out = enc.redact_config(self.config())
        self.assertIn("password=***", out["cameras"][1]["url"])

    def test_a_password_inside_an_rtsp_url(self):
        out = enc.redact_config(self.config())
        self.assertEqual(out["cameras"][2]["url"],
                         "rtsp://admin:***@192.0.2.14:554/stream1")

    def test_the_discord_token_goes_and_the_id_stays(self):
        out = enc.redact_config(self.config())
        self.assertEqual(out["discord"]["webhook_url"],
                         "https://discord.com/api/webhooks/123456/***")

    def test_what_a_fault_report_needs_survives(self):
        # A dump that masked the addresses and the paths would be safe and
        # useless. These are the fields somebody answering the report reads.
        dump = self.dumped()
        for keep in ("192.0.2.10", "192.0.2.11", "Driveway", "Hallway",
                     "admin", "digest", "/srv/tl/frames",
                     "nasuser@nas:/mnt/user/tl/", "--partial"):
            with self.subTest(keep=keep):
                self.assertIn(keep, dump)

    def test_the_structure_is_preserved_exactly(self):
        cfg = self.config()
        out = enc.redact_config(cfg)
        self.assertEqual(sorted(out), sorted(cfg))
        self.assertEqual(len(out["cameras"]), 3)
        self.assertEqual([c["name"] for c in out["cameras"]],
                         ["Driveway", "Doorbell", "Hallway"])
        self.assertEqual(out["transfer"]["rsync_args"], ["-rt", "--partial"])

    def test_the_original_is_not_touched(self):
        # It is handed a live config in-process. Masking in place would edit
        # the thing the caller is about to write back to disk.
        cfg = self.config()
        enc.redact_config(cfg)
        self.assertEqual(cfg["cameras"][0]["password"], self.S)

    def test_an_empty_password_stays_empty(self):
        # "" is not a secret, it is the answer to "did you set one?", which is
        # exactly what a report about a 401 needs to show.
        out = enc.redact_config({"cameras": [{"password": "", "username": ""}]})
        self.assertEqual(out["cameras"][0]["password"], "")

    # -- a stored hash is a secret for this purpose -------------------------
    # The web UI's login keeps a PBKDF2 hash in the config. It is not a
    # password, and it is not replayed to anything, but it is offline-
    # crackable and this dump exists to be pasted into a public issue. The
    # original rule anchored on `pass(word|wd)?$`, so `password_hash` went
    # through untouched.

    def test_a_stored_password_hash_is_masked(self):
        out = enc.redact_config({"web": {"auth": {
            "username": "ed",
            "password_hash": f"pbkdf2_sha256$600000$c2FsdA$ZGVyaXZlZA{self.S}",
        }}})
        self.assertEqual(out["web"]["auth"]["password_hash"], enc.MASK)

    def test_the_hash_appears_nowhere_in_the_dump(self):
        # The same whole-tree assertion the passwords get: a hash in a shape
        # nobody thought of still fails this.
        dump = json.dumps(enc.redact_config({"web": {"auth": {
            "password_hash": f"pbkdf2_sha256$600000$c2FsdA${self.S}"}}}))
        self.assertNotIn(self.S, dump)

    def test_the_login_username_survives(self):
        # Same reasoning as the camera usernames: "who is it configured for"
        # is a question a fault report has to be able to answer.
        out = enc.redact_config({"web": {"auth": {"username": "ed",
                                                  "password_hash": "x"}}})
        self.assertEqual(out["web"]["auth"]["username"], "ed")

    def test_an_unset_login_still_reads_as_unset(self):
        # "" is the answer to "have you configured a login at all?", which is
        # the first thing to ask about a UI that is letting anyone in.
        out = enc.redact_config({"web": {"auth": {"username": "",
                                                  "password_hash": ""}}})
        self.assertEqual(out["web"]["auth"], {"username": "",
                                              "password_hash": ""})

    def test_the_camera_auth_scheme_is_not_swept_up_with_it(self):
        # `auth` on a camera is "digest" or "basic", not a credential, and
        # masking the block it names would hide the username with it.
        out = enc.redact_config({"cameras": [{"auth": "digest",
                                              "username": "admin"}]})
        self.assertEqual(out["cameras"][0], {"auth": "digest",
                                             "username": "admin"})

    def test_an_ordinary_hash_field_is_left_alone(self):
        # The rule is "a password key, however suffixed", not "anything with
        # hash in the name". A dump where half the settings read *** is one
        # nobody can act on.
        out = enc.redact_config({"encode": {"frame_hash": "abc123",
                                            "hash_algorithm": "sha256"}})
        self.assertEqual(out["encode"], {"frame_hash": "abc123",
                                         "hash_algorithm": "sha256"})

    def test_a_key_nobody_planned_for(self):
        # The schema may grow one, and a hand-edited config may already have.
        out = enc.redact_config({"x": {"api_key": "k", "AuthToken": "t",
                                       "db_credentials": "c", "passwd": "p"}})
        self.assertEqual(set(out["x"].values()), {enc.MASK})

    def test_ordinary_keys_are_not_masked_by_a_loose_match(self):
        # Over-redaction is the safe direction, but a dump where half the
        # settings read *** is one nobody can act on.
        out = enc.redact_config({"encode": {"framerate": 60, "container": "mkv",
                                            "av1_preset": "p6"},
                                 "capture": {"retry_within_tick": True}})
        self.assertEqual(out, {"encode": {"framerate": 60, "container": "mkv",
                                          "av1_preset": "p6"},
                               "capture": {"retry_within_tick": True}})

    def test_non_strings_come_through_as_themselves(self):
        # It must still be JSON-serialisable afterwards, with the types intact.
        out = enc.redact_config({"a": 1, "b": True, "c": None, "d": 1.5})
        self.assertEqual(out, {"a": 1, "b": True, "c": None, "d": 1.5})


class TestCredentialWatch(unittest.TestCase):
    """Notify once per incident, once when it ends, and never from stale facts.

    The daemon publishes; this decides. Everything here drives that decision
    through real capture.json files, because the file is the contract between
    two processes and a mocked dict would not exercise it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = {"paths": {"state_dir": str(self.tmp)},
                    "notify": [{"type": "discord", "enabled": True,
                                "webhook_url": "https://example.invalid/w"}]}
        self.sent = []
        patcher = mock.patch.object(
            enc, "notify",
            side_effect=lambda cfg, title, desc, level, fields:
                self.sent.append((title, level)) or (1, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_capture(self, cameras, running=True, age=0):
        state = {"version": 1, "kind": "capture", "running": running,
                 "updated_epoch": int(time.time()) - age,
                 "cameras": cameras}
        (self.tmp / enc.CAPTURE_STATE).write_text(json.dumps(state),
                                                  encoding="utf-8")

    def camera(self, name="Roof", cls="auth", confirmed=True,
               since="2026-08-14T09:00:00"):
        err = None
        if cls:
            err = {"class": cls, "since": since, "ticks": 3,
                   "detail": "401 Client Error", "confirmed": confirmed,
                   "quiet_until": "2026-08-14T09:41:00"}
        return {"name": name, "supervised": False, "error": err}

    def titles(self):
        return [t for t, _ in self.sent]

    # -- the happy path -----------------------------------------------------

    def test_a_confirmed_refusal_is_reported(self):
        self.write_capture([self.camera()])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("refused our credentials", self.titles()[0])
        self.assertEqual(self.sent[0][1], "error")

    def test_the_same_incident_is_not_reported_twice(self):
        self.write_capture([self.camera()])
        enc.watch_credentials(self.cfg)
        for _ in range(5):
            self.assertEqual(enc.watch_credentials(self.cfg), 0)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_sends_one_all_clear(self):
        self.write_capture([self.camera()])
        enc.watch_credentials(self.cfg)
        self.write_capture([self.camera(cls=None)])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)
        self.assertIn("accepted again", self.titles()[1])
        self.assertEqual(self.sent[1][1], "ok")

    def test_the_all_clear_is_not_repeated_either(self):
        self.write_capture([self.camera()])
        enc.watch_credentials(self.cfg)
        self.write_capture([self.camera(cls=None)])
        enc.watch_credentials(self.cfg)
        self.assertEqual(enc.watch_credentials(self.cfg), 0)
        self.assertEqual(len(self.sent), 2)

    def test_a_second_incident_is_reported_again(self):
        # Same camera, a new refusal after a recovery. The incident's identity
        # is its start time, so this must not be mistaken for the old one.
        self.write_capture([self.camera()])
        enc.watch_credentials(self.cfg)
        self.write_capture([self.camera(cls=None)])
        enc.watch_credentials(self.cfg)
        self.write_capture([self.camera(since="2026-08-14T18:30:00")])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)
        self.assertEqual(len(self.sent), 3)

    def test_a_refusal_that_restarts_without_recovering_is_still_new(self):
        # A daemon restart re-enters the ladder, so `since` moves even though
        # nobody saw an all-clear. That is a genuinely new incident.
        self.write_capture([self.camera()])
        enc.watch_credentials(self.cfg)
        self.write_capture([self.camera(since="2026-08-14T11:00:00")])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)

    # -- what must stay quiet ------------------------------------------------

    def test_an_unconfirmed_refusal_says_nothing(self):
        # Two refusals in ten seconds is a camera that might be rebooting.
        self.write_capture([self.camera(confirmed=False)])
        self.assertEqual(enc.watch_credentials(self.cfg), 0)
        self.assertEqual(self.sent, [])

    def test_an_unreachable_camera_says_nothing(self):
        # That is what an uptime monitor is for, and it sees it sooner.
        self.write_capture([self.camera(cls="unreachable")])
        self.assertEqual(enc.watch_credentials(self.cfg), 0)

    def test_some_other_failure_says_nothing(self):
        self.write_capture([self.camera(cls="other")])
        self.assertEqual(enc.watch_credentials(self.cfg), 0)

    def test_a_stale_heartbeat_is_not_acted_on(self):
        # A file from an hour ago describes an hour ago. Sending an alarm from
        # it would be guessing, and so would sending an all-clear.
        self.write_capture([self.camera()], age=enc.CAPTURE_STALE + 60)
        self.assertEqual(enc.watch_credentials(self.cfg), 0)

    def test_a_stopped_daemon_produces_no_all_clear(self):
        # The trap this guards: capture stops, the error disappears from the
        # file with it, and a naive reader announces that the camera recovered.
        self.write_capture([self.camera()])
        enc.watch_credentials(self.cfg)
        self.write_capture([self.camera(cls=None)], running=False)
        self.assertEqual(enc.watch_credentials(self.cfg), 0)
        self.assertEqual(len(self.sent), 1)

    def test_no_capture_state_at_all_is_harmless(self):
        self.assertEqual(enc.watch_credentials(self.cfg), 0)

    def test_a_corrupt_capture_state_is_harmless(self):
        (self.tmp / enc.CAPTURE_STATE).write_text("{not json",
                                                  encoding="utf-8")
        self.assertEqual(enc.watch_credentials(self.cfg), 0)

    def test_it_can_be_switched_off(self):
        self.cfg["capture"] = {"notify_auth_failures": False}
        self.write_capture([self.camera()])
        self.assertEqual(enc.watch_credentials(self.cfg), 0)

    def test_no_sinks_means_no_record_either(self):
        # The trap: recording the incident here would mean that configuring a
        # sink tomorrow leaves today's refusal permanently unannounced.
        self.cfg["notify"] = []
        self.write_capture([self.camera()])
        self.assertEqual(enc.watch_credentials(self.cfg), 0)
        self.assertFalse((self.tmp / enc.WATCH_STATE).exists())
        self.cfg["notify"] = [{"type": "discord", "enabled": True,
                               "webhook_url": "https://example.invalid/w"}]
        self.assertEqual(enc.watch_credentials(self.cfg), 1)

    def test_it_is_on_when_the_key_is_absent(self):
        # Every config written before this release lacks the key, and the
        # feature is worth having by default on all of them.
        self.assertNotIn("capture", self.cfg)
        self.write_capture([self.camera()])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)

    def test_a_failed_delivery_is_retried_next_tick(self):
        # A sink that was briefly unreachable must not swallow the message.
        self.write_capture([self.camera()])
        with mock.patch.object(enc, "notify", return_value=(0, 1)):
            self.assertEqual(enc.watch_credentials(self.cfg), 0)
        self.assertFalse((self.tmp / enc.WATCH_STATE).exists())
        self.assertEqual(enc.watch_credentials(self.cfg), 1)
        self.assertEqual(len(self.sent), 1)

    def test_a_failed_all_clear_is_retried_too(self):
        self.write_capture([self.camera()])
        enc.watch_credentials(self.cfg)
        self.write_capture([self.camera(cls=None)])
        with mock.patch.object(enc, "notify", return_value=(0, 1)):
            self.assertEqual(enc.watch_credentials(self.cfg), 0)
        self.assertEqual(enc.load_notified(self.cfg),
                         {"Roof": "2026-08-14T09:00:00"})
        self.assertEqual(enc.watch_credentials(self.cfg), 1)

    def test_one_sink_succeeding_is_enough(self):
        self.write_capture([self.camera()])
        with mock.patch.object(enc, "notify", return_value=(1, 2)):
            self.assertEqual(enc.watch_credentials(self.cfg), 1)
        self.assertEqual(enc.watch_credentials(self.cfg), 0)

    # -- several cameras -----------------------------------------------------

    def test_cameras_are_tracked_independently(self):
        self.write_capture([self.camera("Roof"),
                            self.camera("Gate", cls=None),
                            self.camera("Yard", cls="unreachable")])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)
        self.write_capture([self.camera("Roof"), self.camera("Gate"),
                            self.camera("Yard", cls="unreachable")])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)
        self.assertEqual(len(self.sent), 2)

    def test_one_camera_recovering_leaves_the_others_alone(self):
        self.write_capture([self.camera("Roof"), self.camera("Gate")])
        enc.watch_credentials(self.cfg)
        self.write_capture([self.camera("Roof"), self.camera("Gate", cls=None)])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)
        record = json.loads((self.tmp / enc.WATCH_STATE)
                            .read_text(encoding="utf-8"))
        self.assertIn("Roof", record["incidents"])
        self.assertNotIn("Gate", record["incidents"])

    # -- the record itself ---------------------------------------------------

    def test_the_record_survives_a_restart_of_this_checker(self):
        # Each run is a fresh process, so "already told them" has to be on
        # disk. Without this the timer would send the same alarm every five
        # minutes for as long as the camera stayed broken.
        self.write_capture([self.camera()])
        enc.watch_credentials(self.cfg)
        self.assertTrue((self.tmp / enc.WATCH_STATE).exists())
        self.assertEqual(enc.load_notified(self.cfg),
                         {"Roof": "2026-08-14T09:00:00"})

    def test_an_unwritable_record_still_notifies(self):
        # Losing the record risks a repeat; failing to warn risks silence.
        self.write_capture([self.camera()])
        with mock.patch.object(enc, "save_notified", return_value=False):
            self.assertEqual(enc.watch_credentials(self.cfg), 1)
        self.assertEqual(len(self.sent), 1)

    def test_a_corrupt_record_is_treated_as_empty(self):
        (self.tmp / enc.WATCH_STATE).write_text("nonsense", encoding="utf-8")
        self.write_capture([self.camera()])
        self.assertEqual(enc.watch_credentials(self.cfg), 1)

    def test_the_message_names_the_camera_and_carries_no_credential(self):
        cam = self.camera()
        cam["error"]["detail"] = "401 for url: http://c/s?password=***"
        self.write_capture([cam])
        with mock.patch.object(enc, "notify") as spy:
            spy.return_value = (1, 0)
            enc.watch_credentials(self.cfg)
        _cfg, title, desc, level, fields = spy.call_args[0]
        self.assertIn("Roof", desc)
        self.assertEqual(level, "error")
        self.assertIn(("Camera", "Roof"), fields)
        self.assertNotIn("hunter2", json.dumps(fields))


class TestWatchUnitWiring(unittest.TestCase):
    """The shipped unit and the installer that rewrites it must agree.

    None of this is reachable from Python at runtime, and all of it was found
    on real systemd rather than here, which is exactly why it is pinned: the
    failure is silent and expensive in both directions.
    """

    def setUp(self):
        self.repo = _support.REPO
        self.unit = (self.repo / "service" / "timelapse-watch.service"
                     ).read_text(encoding="utf-8")
        self.installer = (self.repo / "install.sh").read_text(encoding="utf-8")

    def test_the_unit_asks_for_watch_mode(self):
        self.assertIn("timelapse_encode.py --watch", self.unit)

    def test_the_installer_rewrites_it_with_the_flag_intact(self):
        # The loop that templates capture and encode matches any ExecStart
        # mentioning timelapse_encode.py. If the watch unit went through it,
        # --watch would be stripped and a five-minute timer would become a
        # five-minute *encode run*, silently, on every install.
        self.assertIn("timelapse_encode.py --watch $CONFIG", self.installer)

    def test_the_shared_loop_does_not_touch_the_watch_unit(self):
        loop = self.installer.split("for unit in timelapse-capture.service "
                                    "timelapse-encode.service; do")[1]
        loop = loop.split("done")[0]
        self.assertNotIn("watch", loop)

    def test_it_is_installed_and_removed_with_the_others(self):
        for phase in ("install -m 0644", "rm -f"):
            self.assertIn("timelapse-watch.service", self.installer)
            self.assertIn("timelapse-watch.timer", self.installer)
        self.assertIn("disable --now timelapse-watch.timer", self.installer)

    def test_its_writable_set_is_the_state_directory_alone(self):
        # It reads the capture heartbeat and writes one small record. Giving it
        # $rw would hand a five-minutely job write access to every frame.
        self.assertIn("ReadWritePaths=$staterw", self.installer)

    def test_new_timers_are_adopted_on_upgrade(self):
        # A unit that did not exist at the previous install is enabled by
        # nobody, because an upgrade skips the wizard and offer_enable returns
        # early. restore_services() adopts it by noticing it was not present
        # before, and only for timers: a *service* that was never installed
        # must not be switched on by an upgrade, which is what keeps the
        # opt-in web UI opt-in.
        self.assertIn("UNITS_PRESENT_BEFORE", self.installer)
        self.assertIn("*.timer)", self.installer)

    def test_an_upgrade_asks_nothing(self):
        # Verified live in WSL, where an upgrade of a fully enabled install
        # printed zero prompts. This pins the three that were removed, since
        # each one would silently take its default under `curl | bash` and so
        # would not fail loudly if it came back.
        for gone in ("Reconfigure it?",
                     "Restart so this version takes effect?"):
            self.assertNotIn(gone, self.installer)
        # These two survive, for a *fresh* install only, behind the early
        # return in offer_enable.
        self.assertIn('if [ "$IS_UPGRADE" = "1" ]; then', self.installer)
        self.assertIn("Run the pre-flight check now?", self.installer)

    def test_service_state_is_captured_before_anything_is_written(self):
        # install_units() and sync_units() both rewrite what the snapshot
        # reads, so ordering is the whole correctness argument here.
        body = self.installer.split("main() {")[1]
        snap = body.index("snapshot_services")
        self.assertLess(snap, body.index("install_units"))
        self.assertLess(snap, body.index("run_wizard"))
        self.assertLess(body.index("restore_services"), body.index("offer_enable"))

    def test_the_timer_does_not_catch_up_missed_runs(self):
        timer = (self.repo / "service" / "timelapse-watch.timer"
                 ).read_text(encoding="utf-8")
        # Persistent=true would fire a burst at boot to make up for downtime,
        # and every one of those would read the same live heartbeat.
        self.assertNotIn("Persistent=true", timer)
        self.assertIn("OnUnitActiveSec=", timer)

    def test_watch_mode_does_not_open_the_encode_log(self):
        # Its unit may write one directory, the state directory, so a file
        # handler under ProtectSystem=strict kills the process at startup.
        # Verified on systemd 255; the symptom is a read-only filesystem error
        # naming encode.log, from a service that never encodes anything.
        src = (self.repo / "scripts" / "timelapse_encode.py").read_text(
            encoding="utf-8")
        self.assertIn("None if args.watch else", src)


class TestAddressFormatting(unittest.TestCase):
    """url_host/is_ipv6/hostport, the shared rule for emitting an address.

    It lives in timelapse_encode because the wizard, the installer and the web
    UI all need it, and two copies is two chances to emit a URL nothing can
    open.
    """

    def test_a_hostname_is_not_ipv6(self):
        self.assertFalse(enc.is_ipv6("nas.local"))

    def test_an_ipv4_address_is_not_ipv6(self):
        self.assertFalse(enc.is_ipv6("192.168.2.16"))

    def test_a_literal_is_ipv6_bracketed_or_not(self):
        self.assertTrue(enc.is_ipv6("::1"))
        self.assertTrue(enc.is_ipv6("[::1]"))
        self.assertTrue(enc.is_ipv6("fdd2:49bd::1"))

    def test_the_wildcard_is_ipv6(self):
        # "::" is the address most likely to be typed at the wizard's prompt,
        # and the one where getting the family wrong fails at startup.
        self.assertTrue(enc.is_ipv6("::"))

    def test_a_zone_id_does_not_defeat_detection(self):
        self.assertTrue(enc.is_ipv6("fe80::1%eth0"))

    def test_hostport_leaves_ipv4_alone(self):
        self.assertEqual(enc.hostport("127.0.0.1", 8787), "127.0.0.1:8787")

    def test_hostport_brackets_ipv6(self):
        self.assertEqual(enc.hostport("::1", 8787), "[::1]:8787")

    def test_hostport_survives_a_port_given_as_a_string(self):
        # The config is JSON and nothing coerces it, so both arrive.
        self.assertEqual(enc.hostport("::", "8787"), "[::]:8787")

    def test_a_url_built_from_hostport_parses(self):
        url = "http://%s/video/3" % enc.hostport("fdd2::1", 8787)
        parsed = urlparse(url)
        self.assertEqual(parsed.hostname, "fdd2::1")
        self.assertEqual(parsed.port, 8787)
        self.assertEqual(parsed.path, "/video/3")

    def test_the_installer_uses_the_shared_rule(self):
        # install.sh prints the web URL in the prompt that offers to enable
        # the service, and used to build it with a bare format string.
        src = (self.repo() / "install.sh").read_text(encoding="utf-8")
        self.assertIn("from timelapse_encode import hostport", src)

    def repo(self):
        return Path(__file__).resolve().parent.parent


class TestReplaceAtomic(unittest.TestCase):
    """The rename half of the atomic write.

    On Linux every one of these passes on the first attempt, so the retry is
    dead code here and would rot unobserved. Hence the fake: the failure mode
    is Windows-only but the *handling* of it is not, and it can be tested
    anywhere.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "state.json.tmp"
        self.dst = self.tmp / "state.json"
        self.src.write_text("new", encoding="utf-8")
        self.dst.write_text("old", encoding="utf-8")

    def test_it_replaces_the_destination(self):
        enc.replace_atomic(self.src, self.dst)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "new")
        self.assertFalse(self.src.exists())

    def test_it_does_not_sleep_when_the_first_attempt_wins(self):
        # The common path on every platform, and it must cost nothing: this
        # runs once per captured frame, per camera, for ever.
        with mock.patch.object(enc.time, "sleep") as slept:
            enc.replace_atomic(self.src, self.dst)
        slept.assert_not_called()

    def test_a_reader_holding_the_destination_is_waited_out(self):
        """Windows refuses the rename while another handle holds the target.

        Simulated rather than reproduced, because on Linux the real call
        simply succeeds. Two refusals then success is the shape a web UI page
        load produces: it is gone in milliseconds.
        """
        real, calls = enc.os.replace, []

        def flaky(a, b):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "Access is denied")
            return real(a, b)

        with mock.patch.object(enc.os, "replace", flaky), \
                mock.patch.object(enc.time, "sleep") as slept:
            enc.replace_atomic(self.src, self.dst)
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept.call_count, 2)
        self.assertEqual(self.dst.read_text(encoding="utf-8"), "new")

    def test_a_real_permission_problem_still_raises(self):
        """A reader goes away; a wrong owner does not.

        The caller's own OSError handling is what turns this into a logged
        warning rather than a crash, so the error has to arrive rather than be
        swallowed into a silent no-op that loses the write.
        """
        def denied(a, b):
            raise PermissionError(13, "Access is denied")

        with mock.patch.object(enc.os, "replace", denied), \
                mock.patch.object(enc.time, "sleep"):
            with self.assertRaises(PermissionError):
                enc.replace_atomic(self.src, self.dst)

    def test_other_errors_are_not_retried(self):
        # A missing source or a cross-device rename will not fix itself, and
        # retrying it 20 times just delays the log line.
        def gone(a, b):
            raise FileNotFoundError(2, "No such file")

        with mock.patch.object(enc.os, "replace", gone), \
                mock.patch.object(enc.time, "sleep") as slept:
            with self.assertRaises(FileNotFoundError):
                enc.replace_atomic(self.src, self.dst)
        slept.assert_not_called()

    def test_every_atomic_write_in_the_project_goes_through_it(self):
        """A new call site that used os.replace directly would be a fresh
        instance of the same bug, on the platform least likely to be the one
        it was written on. Cheaper to pin the count at zero than to find it."""
        import re
        # Anchored to the start of a line, so the mention in replace_atomic's
        # own docstring does not count as a call. The first draft of this test
        # counted the substring and failed on its own documentation.
        call = re.compile(r"^\s*os\.replace\(", re.M)
        scripts = Path(__file__).resolve().parent.parent / "scripts"
        for path in sorted(scripts.glob("timelapse_*.py")):
            src = path.read_text(encoding="utf-8")
            # The definition itself is the one legitimate use, and there is
            # one copy of it per daemon-independent module.
            self.assertEqual(
                len(call.findall(src)), src.count("def replace_atomic("),
                f"{path.name} calls os.replace() outside replace_atomic()")


if __name__ == "__main__":
    unittest.main()

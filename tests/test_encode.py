"""Unit tests for timelapse_encode.py: the pure logic, no ffmpeg involved.

The end-to-end encode is covered separately by tests/smoke_test.py.
"""

import json
import logging
import os
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


if __name__ == "__main__":
    unittest.main()

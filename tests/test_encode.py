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
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()

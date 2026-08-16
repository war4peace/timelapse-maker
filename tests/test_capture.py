"""Unit tests for timelapse_capture.py.

Covers the destination-path logic, which owns the on-disk contract the encoder
depends on, and the DST fall-back collision handling. No network, no threads
started - HttpCamera is constructed but never run().
"""

import io
import json
import logging
import math
import shutil
import sys
import tempfile
import threading
import time
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


class TestPerCameraCadence(unittest.TestCase):
    """A camera may carry its own interval; absent means follow the global.

    Absent, rather than a copy of the global value, is the whole design: a
    camera nobody has pinned still moves when the global interval changes.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_config(self.tmp)          # 5s interval, 4s timeout

    def build(self, **cam):
        base = {"name": "Cam", "url": "http://192.0.2.1/snap"}
        base.update(cam)
        return cap.HttpCamera(base, self.cfg)

    def test_no_override_follows_the_global(self):
        self.assertEqual(cap.camera_interval({}, self.cfg), 5)

    def test_an_override_wins(self):
        self.assertEqual(
            cap.camera_interval({"interval_seconds": 60}, self.cfg), 60)

    def test_the_thread_runs_on_its_own_interval(self):
        self.assertEqual(self.build(interval_seconds=60).interval, 60)

    def test_a_longer_interval_keeps_the_global_timeout(self):
        # 4s is already comfortably under 60s; there is nothing to clamp.
        self.assertEqual(self.build(interval_seconds=60).timeout, 4)

    def test_a_shorter_interval_clamps_the_timeout_under_it(self):
        # The global 4s timeout would otherwise still be in flight when the
        # next 3s tick fires, so every request overruns its own slot.
        self.assertEqual(self.build(interval_seconds=3).timeout, 2)

    def test_the_clamp_never_reaches_zero(self):
        # A 1s interval has no room under it; 1s beats a timeout of 0, which
        # requests treats as "no timeout at all".
        self.assertEqual(self.build(interval_seconds=1).timeout, 1)

    def test_a_zero_or_missing_override_is_not_an_override(self):
        # 0 is not a cadence, and reading it as one would busy-loop.
        for value in (0, None):
            self.assertEqual(
                cap.camera_interval({"interval_seconds": value}, self.cfg), 5)

    def test_a_zero_framerate_override_falls_back_to_the_global(self):
        self.assertEqual(cap.camera_framerate({"framerate": 0},
                                              {"encode": {"framerate": 30}}), 30)

    def test_a_config_with_no_encode_section_still_works(self):
        # The daemon has never needed that section, and must not start
        # requiring one just to record a day's cadence.
        self.assertEqual(cap.camera_framerate({}, {"capture": {}}), 60)

    def test_the_rtsp_thread_honours_it_too(self):
        # ffmpeg gets fps=1/interval, so this is the same setting by another
        # route; leaving it global would make the two paths disagree.
        c = cap.RtspCamera({"name": "R", "url": "rtsp://192.0.2.1/s",
                            "interval_seconds": 120}, self.cfg)
        self.assertEqual(c.interval, 120)
        self.assertIn("fps=1/120", c._cmd("2026-08-14"))


class TestOneDayOneCadence(unittest.TestCase):
    """A cadence change lands at midnight and nowhere else.

    The day directory records what it was captured at, and that record beats
    the config for as long as the day lasts. Without it, editing a camera at
    14:00 and restarting would leave the day half at one rate and half at
    another, and the encoder would then measure it against the wrong one.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_config(self.tmp)          # 5s interval, 4s timeout
        self.cfg["encode"] = {"framerate": 60}
        self.path = self.tmp / "config.json"
        self.write_config(5, 60)

    def write_config(self, interval, framerate):
        """The config as it would be on disk, with Roof on its own cadence."""
        cfg = dict(self.cfg)
        cfg["cameras"] = [{"name": "Roof", "url": "http://192.0.2.1/s",
                           "interval_seconds": interval,
                           "framerate": framerate}]
        self.path.write_text(json.dumps(cfg), encoding="utf-8")

    def camera(self):
        return cap.HttpCamera({"name": "Roof", "url": "http://192.0.2.1/s",
                               "interval_seconds": 5, "framerate": 60},
                              self.cfg, str(self.path))

    def day(self, name, interval=None, framerate=None):
        d = self.tmp / "Roof" / name
        d.mkdir(parents=True, exist_ok=True)
        if interval:
            cap.write_cadence(d, interval, framerate)
        return d

    # -- the marker ---------------------------------------------------------

    def test_a_recorded_cadence_round_trips(self):
        d = self.day("2026-08-11", 60, 30)
        self.assertEqual(cap.read_cadence(d), (60, 30))

    def test_an_unmarked_day_reads_as_nothing(self):
        self.assertIsNone(cap.read_cadence(self.day("2026-08-11")))

    def test_a_corrupt_marker_reads_as_nothing_rather_than_raising(self):
        d = self.day("2026-08-11")
        (d / cap.CADENCE_FILE).write_text("{ not json", encoding="utf-8")
        self.assertIsNone(cap.read_cadence(d))

    def test_the_marker_is_never_overwritten(self):
        # The first writer is the one that knows what the day began at.
        d = self.day("2026-08-11", 60, 30)
        self.assertFalse(cap.write_cadence(d, 5, 60))
        self.assertEqual(cap.read_cadence(d), (60, 30))

    def test_an_unwritable_directory_is_not_fatal(self):
        # Failing to annotate a day is not a reason to stop capturing it.
        self.assertFalse(cap.write_cadence(self.tmp / "nope", 5, 60))

    def test_the_marker_is_invisible_to_the_frame_glob(self):
        # valid_frames() globs *.jpg and the usage report tests .jpg; a
        # dotfile called .cadence.json is neither.
        d = self.day("2026-08-11", 5, 60)
        self.assertEqual(list(d.glob("*.jpg")), [])
        self.assertTrue(cap.CADENCE_FILE.startswith("."))

    # -- adoption -----------------------------------------------------------

    def test_a_day_already_under_way_keeps_its_cadence(self):
        # The config now says 60s, but today started at 5s. A daemon
        # restarted at 14:00 must finish the day the way it began it.
        self.day("2026-08-11", 5, 60)
        self.write_config(60, 30)
        cam = self.camera()
        cam.begin_day("2026-08-11")
        self.assertEqual((cam.interval, cam.framerate), (5, 60))

    def test_a_day_that_has_not_begun_reads_the_config(self):
        self.write_config(60, 30)
        cam = self.camera()
        self.assertTrue(cam.begin_day("2026-08-12"))
        self.assertEqual((cam.interval, cam.framerate), (60, 30))

    def test_the_timeout_is_reclamped_when_a_day_pins_a_short_interval(self):
        self.day("2026-08-11", 3, 60)
        cam = self.camera()
        cam.begin_day("2026-08-11")
        self.assertEqual(cam.timeout, 2)

    def test_an_unreadable_config_leaves_the_cadence_alone(self):
        # Somebody is mid-edit. Capture keeps running on what it has.
        self.path.write_text("{ broken", encoding="utf-8")
        cam = self.camera()
        self.assertFalse(cam.begin_day("2026-08-12"))
        self.assertEqual(cam.interval, 5)

    def test_a_camera_removed_from_the_config_keeps_running(self):
        self.path.write_text(json.dumps({"capture": self.cfg["capture"],
                                         "cameras": []}), encoding="utf-8")
        cam = self.camera()
        self.assertFalse(cam.begin_day("2026-08-12"))
        self.assertEqual(cam.interval, 5)

    def test_no_config_path_at_all_is_survivable(self):
        cam = cap.HttpCamera({"name": "Roof", "url": "http://192.0.2.1/s"},
                             self.cfg)
        self.assertFalse(cam.begin_day("2026-08-12"))

    # -- writing it as the day is created -----------------------------------

    def test_creating_a_day_directory_records_the_cadence(self):
        cam = self.camera()
        cam._dest_path(datetime(2026, 8, 11, 0, 0, 5))
        self.assertEqual(cap.read_cadence(self.tmp / "Roof" / "2026-08-11"),
                         (5, 60))

    def test_housekeeping_annotates_a_directory_ffmpeg_made(self):
        # The RTSP path never creates day directories itself: ffmpeg does,
        # through -strftime_mkdir, so the marker has to come from elsewhere.
        cam = self.camera()
        today = cap.day_string(time.time())
        (self.tmp / "Roof" / today).mkdir(parents=True)
        cap.record_cadences([cam])
        self.assertEqual(cap.read_cadence(self.tmp / "Roof" / today), (5, 60))

    def test_housekeeping_never_creates_a_directory(self):
        # A camera offline all day would otherwise leave an empty one behind
        # every night, which the encoder finds, reports as a SKIP, and never
        # cleans up.
        cap.record_cadences([self.camera()])
        self.assertFalse((self.tmp / "Roof").exists())

    # -- the boundary itself ------------------------------------------------

    def test_seconds_to_midnight_is_the_time_left_in_the_day(self):
        self.assertEqual(
            cap.seconds_to_midnight(datetime(2026, 8, 11, 23, 59, 30)), 30)
        self.assertEqual(
            cap.seconds_to_midnight(datetime(2026, 8, 11, 0, 0, 0)), 86400)

    def test_seconds_to_midnight_never_returns_zero(self):
        # It becomes ffmpeg's -t; zero would exit immediately and spin.
        self.assertGreaterEqual(
            cap.seconds_to_midnight(datetime(2026, 8, 11, 23, 59, 59, 999999)),
            1.0)

    def test_the_rtsp_command_stops_at_the_boundary(self):
        # ffmpeg carries fps=1/interval on its command line, so adopting a
        # new cadence means launching a new process, and midnight is the only
        # moment that may happen.
        cam = cap.RtspCamera({"name": "R", "url": "rtsp://192.0.2.1/s"},
                             self.cfg, str(self.path))
        cmd = cam._cmd("2026-08-14")
        self.assertIn("-t", cmd)
        self.assertGreater(int(cmd[cmd.index("-t") + 1]), 0)


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


class TestIntraTickRetry(unittest.TestCase):
    """One retry inside the tick, for cameras that refuse a snapshot instantly.

    The whole design rests on the budget arithmetic being unable to overrun the
    next tick, so that is tested as a property rather than at one sample point.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cam = cap.HttpCamera(
            {"name": "Court180", "url": "http://192.0.2.1/snap"},
            make_config(self.tmp))          # interval 5, timeout 4

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        cap.STOP.clear()

    # -- budget arithmetic --------------------------------------------------

    def test_fast_failure_leaves_room_to_retry(self):
        # 500 came back 50ms into a 5s tick.
        self.assertGreater(self.cam._retry_timeout(1005.0, 1000.05), 0)

    def test_a_timed_out_attempt_is_not_retried(self):
        # The core safety property: a 4s timeout has already spent the tick.
        # No fast/slow heuristic decides this - the subtraction does.
        self.assertEqual(self.cam._retry_timeout(1005.0, 1004.0), 0.0)

    def test_retry_can_never_run_into_the_next_tick(self):
        deadline = 1005.0
        for ms in range(0, 5000, 25):
            now = 1000.0 + ms / 1000.0
            t = self.cam._retry_timeout(deadline, now)
            if t:
                finish = now + cap.RETRY_DELAY + t
                self.assertLessEqual(finish, deadline - cap.RETRY_GUARD + 1e-9,
                                     f"overruns when failing at +{ms}ms")

    def test_timeout_is_capped_at_the_configured_timeout(self):
        # A long resync-widened window must not make one fetch hang for it.
        self.assertEqual(self.cam._retry_timeout(1100.0, 1000.0), self.cam.timeout)

    def test_no_retry_when_the_interval_is_too_short_to_fit_one(self):
        cfg = make_config(self.tmp, interval=1)
        cam = cap.HttpCamera({"name": "Fast", "url": "http://192.0.2.1/s"}, cfg)
        self.assertEqual(cam._retry_timeout(1001.0, 1000.0), 0.0)

    # -- behaviour ----------------------------------------------------------

    def call_retry(self, grab, deadline_in=5.0):
        self.cam._grab = grab
        return self.cam._retry_grab(datetime(2026, 8, 6, 9, 4, 45),
                                    time.time() + deadline_in,
                                    RuntimeError("500 Server Error"))

    def test_disabled_by_config_reports_the_first_failure(self):
        self.cam.retry = False
        called = []
        err = self.call_retry(lambda dt, timeout=None: called.append(dt))
        self.assertEqual(called, [])
        self.assertEqual(str(err), "500 Server Error")

    def test_no_retry_when_the_previous_tick_also_failed(self):
        # An outage spanning more than one interval cannot be beaten from
        # inside a tick - the next tick already is the retry. Measured 0%
        # recovery there, so this guard exists to stop paying for it.
        self.cam.consec_fail = 1
        called = []
        err = self.call_retry(lambda dt, timeout=None: called.append(dt))
        self.assertEqual(called, [])
        self.assertEqual(str(err), "500 Server Error")

    def test_the_first_tick_of_a_burst_is_still_retried(self):
        self.cam.consec_fail = 0
        self.assertIsNone(self.call_retry(lambda dt, timeout=None: None))

    def test_a_rescued_tick_reports_no_error(self):
        self.assertIsNone(self.call_retry(lambda dt, timeout=None: None))
        self.assertEqual(self.cam.retried, 1)

    def test_the_retry_gets_the_budgeted_timeout_not_the_configured_one(self):
        seen = []

        def grab(dt, timeout=None):
            seen.append(timeout)

        # Raise the configured timeout rather than shrinking the deadline, so
        # the budget clears RETRY_MIN_BUDGET by seconds. Squeezing the deadline
        # instead leaves only ~0.25s of headroom and the test flakes under load.
        self.cam.timeout = 10
        self.call_retry(grab, deadline_in=5.0)
        self.assertTrue(seen, "retry did not fire")
        self.assertLess(seen[0], self.cam.timeout)
        self.assertGreater(seen[0], cap.RETRY_MIN_BUDGET)

    def test_a_second_failure_reports_the_second_exception(self):
        # The retry's error is the current state of the camera; the first one
        # is stale by then.
        def grab(dt, timeout=None):
            raise RuntimeError("connection refused")

        self.assertEqual(str(self.call_retry(grab)), "connection refused")

    def test_shutdown_during_the_delay_skips_the_second_fetch(self):
        called = []
        cap.STOP.set()
        err = self.call_retry(lambda dt, timeout=None: called.append(dt))
        self.assertEqual(called, [])
        self.assertEqual(str(err), "500 Server Error")

    def test_a_failed_attempt_leaves_no_file_for_the_retry_to_collide_with(self):
        # _grab validates before it writes, so the retry must land on the plain
        # HHMMSS name - not a DST-style "-1" suffix.
        when = datetime(2026, 8, 6, 9, 4, 45)
        self.assertEqual(self.cam._dest_path(when).name, "090445.jpg")
        self.assertEqual(self.cam._dest_path(when).name, "090445.jpg")

    def test_retry_is_on_by_default_and_switchable(self):
        self.assertTrue(self.cam.retry)
        cfg = make_config(self.tmp)
        cfg["capture"]["retry_within_tick"] = False
        cam = cap.HttpCamera({"name": "C", "url": "http://192.0.2.1/s"}, cfg)
        self.assertFalse(cam.retry)


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


class TestCredentialsNeverReachTheLog(unittest.TestCase):
    """Reported from the real deployment 2026-08-11, from the web UI's log
    page:

        WARNING [Doorbell] grab failed (#1): 502 Server Error: Bad Gateway
        for url: http://.../api.cgi?cmd=Snap&...&user=admin&password=hunter2

    The call site is `log.warning("grab failed (#%d): %s", n, err)`. It never
    mentions a URL: requests puts it in the exception text. That is why the
    guarantee is a formatter and not a rule about how to write log calls.
    """

    SECRET = "Sup3rS3cret!"

    def logged(self, call):
        """Run `call` against a logger wired exactly as the daemon wires its
        own, and return what would have been written."""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(cap.RedactingFormatter("%(message)s"))
        logger = logging.getLogger("redaction-test")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        call(logger)
        return stream.getvalue()

    def test_the_reported_line(self):
        err = ("502 Server Error: Bad Gateway for url: "
               "http://192.168.2.208/cgi-bin/api.cgi?cmd=Snap&channel=0"
               f"&rs=tl&user=admin&password={self.SECRET}")
        out = self.logged(lambda log: log.warning("grab failed (#%d): %s",
                                                  1, err))
        self.assertNotIn(self.SECRET, out)
        self.assertIn("password=***", out)
        # Still a usable log line: the camera, the status and the endpoint
        # are what makes it worth keeping.
        self.assertIn("502 Server Error", out)
        self.assertIn("192.168.2.208", out)
        self.assertIn("user=admin", out)

    def test_ffmpeg_stderr_from_the_rtsp_path(self):
        # The other half of the same bug. ffmpeg quotes the URL it was handed,
        # and an RTSP URL carries the password in its userinfo.
        msg = (f"rtsp://admin:{self.SECRET}@192.168.2.208:554/h264Preview_01 "
               f"Server returned 401 Unauthorized")
        out = self.logged(lambda log: log.warning(
            "ffmpeg exited (rc=%s, restart #%d): %s", 1, 2, msg))
        self.assertNotIn(self.SECRET, out)
        self.assertIn("rtsp://admin:***@192.168.2.208", out)

    def test_a_traceback_is_redacted_too(self):
        # log.exception() formats exc_info in the formatter, so a filter on
        # the record's message would let this one straight through.
        def call(log):
            try:
                raise RuntimeError(f"connecting to http://h/a?password="
                                   f"{self.SECRET}")
            except RuntimeError:
                log.exception("grab failed")
        out = self.logged(call)
        self.assertIn("Traceback", out)
        self.assertNotIn(self.SECRET, out)

    def test_a_message_with_no_credential_is_left_alone(self):
        out = self.logged(lambda log: log.info(
            "capture started (5s interval, 4s timeout)"))
        self.assertIn("capture started (5s interval, 4s timeout)", out)

    def wired_by(self, setup_logging):
        """The formatters `setup_logging` actually installs on the root logger.

        Found by reverting: with every test above using RedactingFormatter
        directly, putting a plain logging.Formatter back into setup_logging
        broke nothing at all. The class being correct is worth nothing if the
        daemon does not use it.
        """
        root = logging.getLogger()
        saved, level = root.handlers[:], root.level
        root.handlers = []
        try:
            with tempfile.TemporaryDirectory() as td:
                setup_logging(td)
                self.assertTrue(root.handlers)
                kinds = [type(h.formatter) for h in root.handlers]
                # Before leaving the directory: the rotating file handler
                # holds capture.log open, and Windows will not delete it.
                for h in root.handlers:
                    h.close()
                root.handlers = []
                return kinds
        finally:
            for h in root.handlers:
                h.close()
            root.handlers, root.level = saved, level

    def test_the_daemon_installs_the_redacting_formatter(self):
        # Both handlers: journald sees stdout, and capture.log is a file on
        # disk that outlives the journal's retention.
        kinds = self.wired_by(cap.setup_logging)
        self.assertEqual(len(kinds), 2)
        for kind in kinds:
            self.assertIs(kind, cap.RedactingFormatter)

    def test_the_encoder_installs_it_too(self):
        import timelapse_encode as enc
        kinds = self.wired_by(enc.setup_logging)
        self.assertEqual(len(kinds), 2)
        for kind in kinds:
            self.assertIs(kind, enc.RedactingFormatter)

    def test_a_dying_thread_does_not_print_around_the_formatter(self):
        """Found on real systemd, not in a test: threading's own excepthook
        writes a dead thread's traceback straight to stderr, meeting no
        formatter on the way. An exception carrying a URL leaked in full."""
        saved_thread, saved_sys = threading.excepthook, sys.excepthook
        self.addCleanup(setattr, threading, "excepthook", saved_thread)
        self.addCleanup(setattr, sys, "excepthook", saved_sys)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(cap.RedactingFormatter("%(message)s"))
        saved_handlers = cap.log.handlers[:]
        cap.log.handlers = [handler]
        cap.log.propagate = False
        self.addCleanup(setattr, cap.log, "handlers", saved_handlers)

        cap.route_exceptions_through_logging()

        def die():
            raise RuntimeError(f"http://cam/a?password={self.SECRET}")

        thread = threading.Thread(target=die, name="cap-Doorbell")
        thread.start()
        thread.join()

        out = stream.getvalue()
        self.assertIn("cap-Doorbell", out)
        self.assertIn("Traceback", out)
        self.assertNotIn(self.SECRET, out)

    def test_the_daemons_copy_of_the_state_location_has_not_drifted(self):
        """Same reasoning as the redaction rule below, different constant.

        The encoder and the web UI look for capture.json where timelapse_encode
        says it is; the daemon writes it where its own copy says. Two answers
        means a heartbeat nobody reads and a status page that says capture is
        not running while it is.
        """
        import timelapse_encode as enc
        self.assertEqual(cap.STATE_DIR_DEFAULT, enc.STATE_DIR_DEFAULT)
        self.assertEqual(cap.CAPTURE_STATE, enc.CAPTURE_STATE)
        self.assertEqual(cap.STATE_VERSION, enc.STATE_VERSION)
        self.assertEqual(cap.state_dir({"paths": {"state_dir": "/srv/s"}}),
                         enc.state_dir({"paths": {"state_dir": "/srv/s"}}))
        self.assertEqual(cap.state_dir({}), enc.state_dir({}))

    def test_the_daemons_copy_of_the_path_derivation_has_not_drifted(self):
        """Fourth duplicated thing, and the first one that is platform code.

        The daemon may not import timelapse_platform for the same reason it
        imports nothing else: a syntax error in a script it does not need must
        not be able to stop the capture. So it carries the derivation, and this
        holds the two copies character-identical, which is the only assertion
        that would notice the *Windows* branch drifting while both platforms'
        tests still pass on the Linux one.

        This is also the exception named in test_platform's rule that no file
        outside timelapse_platform tests os.name.
        """
        import inspect
        import timelapse_platform as plat
        self.assertEqual(inspect.getsource(cap.program_data),
                         inspect.getsource(plat.program_data))
        self.assertEqual(inspect.getsource(cap.locations),
                         inspect.getsource(plat.locations))
        for name in ("LINUX_CONFIG_DIR", "LINUX_DATA_ROOT", "LINUX_STATE_DIR",
                     "LINUX_WEB_STATE_DIR"):
            self.assertEqual(getattr(cap, name), getattr(plat, name), name)
        # And the values it actually derives, which is what the rest of the
        # project reads. STATE_DIR_DEFAULT is pinned to the encoder above; this
        # pins the pair of them to the module that owns the answer.
        self.assertEqual(cap.STATE_DIR_DEFAULT, plat.STATE_DIR_DEFAULT)
        self.assertEqual(cap.CONFIG_PATH, plat.CONFIG_PATH)

    def test_the_daemons_copy_of_the_log_handler_has_not_drifted(self):
        """Fifth duplicated thing, and the largest.

        Worth stating plainly: the independence rule now costs this daemon
        five copies (load_config, the redaction rule, replace_atomic, the path
        derivation and this). It keeps paying while every one of them is
        pinned; the day a pin cannot be written is the day to revisit it.

        Character-identical rather than behavioural, because what matters is
        the ordering inside _switch_to - open the new file before closing the
        old one - and no assertion about the handler's output on Linux would
        notice that being reversed, since Linux never takes this path.
        """
        import inspect
        import timelapse_platform as plat
        self.assertEqual(inspect.getsource(cap.DailyFileHandler),
                         inspect.getsource(plat.DailyFileHandler))
        self.assertEqual(inspect.getsource(cap.log_handler),
                         inspect.getsource(plat.log_handler))

    def test_the_daemon_still_writes_capture_log_on_linux(self):
        """The Windows fix must not have moved the file anybody greps.

        install.md tells operators to grep <log_dir>/capture.log, and an
        upgrade that silently renamed it would break that with no message.
        """
        from unittest import mock
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(cap, "IS_WINDOWS", False):
            handler = cap.log_handler(tmp, "capture", backups=3)
        self.addCleanup(handler.close)
        self.assertIsInstance(handler,
                              logging.handlers.RotatingFileHandler)
        self.assertEqual(Path(handler.baseFilename).name, "capture.log")

    def test_the_daemons_copy_of_the_atomic_write_has_not_drifted(self):
        """Third duplicated thing, pinned for the same reason as the other two.

        This one is character-identical rather than value-identical because it
        is a function, and the part that matters is the retry, which no
        assertion about its output would notice going missing on Linux.
        """
        import inspect
        import timelapse_encode as enc
        self.assertEqual(inspect.getsource(cap.replace_atomic),
                         inspect.getsource(enc.replace_atomic))
        self.assertEqual(cap.REPLACE_TRIES, enc.REPLACE_TRIES)
        self.assertEqual(cap.REPLACE_WAIT, enc.REPLACE_WAIT)

    def test_the_daemons_copy_of_the_rule_has_not_drifted(self):
        """The rule exists twice: this daemon imports nothing from its
        siblings, for the same reason load_config() is duplicated. A security
        rule that exists twice will drift, and the copy that drifts is the one
        nobody is looking at, so pin them together here."""
        import timelapse_encode as enc
        self.assertEqual(cap.MASK, enc.MASK)
        self.assertEqual(
            [(p.pattern, p.flags, r) for p, r in cap.CRED_PATTERNS],
            [(p.pattern, p.flags, r) for p, r in enc.CRED_PATTERNS])


class TestCaptureState(unittest.TestCase):
    """The heartbeat: what systemd cannot say about a running daemon."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / "state"
        self.state.mkdir()
        self.cfg = make_config(self.tmp / "frames")
        self.cfg["paths"]["state_dir"] = str(self.state)
        self.addCleanup(cap.PAUSED.clear)

    def camera(self, name="Roof", **kw):
        cam = cap.HttpCamera({"name": name, "url": "http://192.0.2.1/s"},
                             self.cfg)
        for key, value in kw.items():
            setattr(cam, key, value)
        return cam

    def write(self, cams, running=True):
        cap.write_state(self.cfg, cams, time.time() - 60, running)
        return json.loads((self.state / cap.CAPTURE_STATE)
                          .read_text(encoding="utf-8"))

    # -- the file itself ----------------------------------------------------

    def test_it_lands_in_the_configured_state_directory(self):
        self.write([self.camera()])
        self.assertTrue((self.state / "capture.json").is_file())

    def test_it_is_versioned_from_the_first_release(self):
        """A second on-disk contract outlives whatever reads it first."""
        got = self.write([self.camera()])
        self.assertEqual(got["version"], cap.STATE_VERSION)
        self.assertEqual(got["kind"], "capture")

    def test_it_leaves_no_temporary_file_behind(self):
        self.write([self.camera()])
        self.assertEqual([p.name for p in self.state.iterdir()],
                         [cap.CAPTURE_STATE])

    def test_a_second_write_replaces_the_first(self):
        self.write([self.camera(ok=1)])
        got = self.write([self.camera(ok=9)])
        self.assertEqual(got["cameras"][0]["ok"], 9)

    def test_an_unwritable_directory_is_a_warning_not_a_crash(self):
        self.cfg["paths"]["state_dir"] = str(self.tmp / "nope")
        with self.assertLogs("capture", level="WARNING") as cm:
            self.assertFalse(cap.write_state(self.cfg, [self.camera()], 0))
        self.assertIn("capture continues", "\n".join(cm.output))

    def test_it_complains_once_not_every_minute(self):
        # A line a minute for as long as the condition lasts is how a journal
        # becomes unreadable.
        self.cfg["paths"]["state_dir"] = str(self.tmp / "nope")
        cap._state_warned = False
        self.addCleanup(setattr, cap, "_state_warned", False)
        with self.assertLogs("capture", level="WARNING") as cm:
            for _ in range(5):
                cap.write_state(self.cfg, [self.camera()], 0)
        self.assertEqual(len(cm.output), 1)

    # -- what it says -------------------------------------------------------

    def test_an_http_camera_publishes_its_counters(self):
        got = self.write([self.camera(ok=1200, fail=3, retried=2,
                                      consec_fail=0)])
        cam = got["cameras"][0]
        self.assertEqual((cam["ok"], cam["fail"], cam["retried"],
                          cam["consec_fail"]), (1200, 3, 2, 0))
        self.assertFalse(cam["supervised"])

    def test_never_captured_is_null_not_zero(self):
        # None means "not yet" and has to stay distinguishable from "a long
        # time ago", which is what an epoch of 0 would read as.
        cam = self.write([self.camera()])["cameras"][0]
        self.assertIsNone(cam["last_success"])
        self.assertIsNone(cam["last_attempt"])

    def test_timestamps_are_local_iso_seconds(self):
        cam = self.write([self.camera(last_success=1786000000.4)])["cameras"][0]
        self.assertRegex(cam["last_success"],
                         r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")

    def test_it_carries_an_epoch_for_the_reader_to_do_maths_with(self):
        got = self.write([self.camera()])
        self.assertAlmostEqual(got["updated_epoch"], time.time(), delta=5)

    def test_a_paused_daemon_says_so(self):
        """The one thing systemd actively misrepresents: a disk-guard pause
        leaves the unit active (running) and capturing nothing."""
        cap.PAUSED.set()
        self.assertTrue(self.write([self.camera()])["paused"])

    def test_a_running_daemon_is_not_paused_by_default(self):
        self.assertFalse(self.write([self.camera()])["paused"])

    def test_a_clean_exit_is_distinguishable_from_a_wedge(self):
        # Staleness alone would call a stopped daemon and a hung one the same
        # thing, and only one of them is a fault.
        self.assertFalse(self.write([self.camera()], running=False)["running"])

    def test_every_camera_appears(self):
        got = self.write([self.camera("Roof"), self.camera("Gate")])
        self.assertEqual([c["name"] for c in got["cameras"]],
                         ["Roof", "Gate"])

    def test_it_publishes_facts_not_verdicts(self):
        """Deliberate: no "healthy"/"failing" field anywhere.

        Whether 42 seconds of silence is a fault depends on the camera's
        interval and on whether capture is paused. A reader can work that out
        from these numbers; a writer that guessed could not be overruled.
        """
        cam = self.write([self.camera(consec_fail=99)])["cameras"][0]
        self.assertNotIn("state", cam)
        self.assertNotIn("healthy", cam)
        self.assertIn("interval", cam)

    def test_a_camera_carries_the_cadence_its_numbers_mean_something_against(self):
        self.cfg["capture"]["interval_seconds"] = 5
        cam = self.write([self.camera()])["cameras"][0]
        self.assertEqual(cam["interval"], 5)


class TestRtspStateIsHonest(unittest.TestCase):
    """An RTSP camera cannot report per-frame success, and must not pretend.

    ffmpeg writes the frames; this thread only supervises the process. What it
    knows is when that process started and how often it has been restarted.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_config(self.tmp / "frames")
        self.cam = cap.RtspCamera({"name": "Gate", "url": "rtsp://192.0.2.1/s",
                                   "method": "rtsp"}, self.cfg)

    def entry(self):
        return cap.capture_state([self.cam], time.time())["cameras"][0]

    def test_it_is_marked_supervised(self):
        self.assertTrue(self.entry()["supervised"])
        self.assertEqual(self.entry()["method"], "rtsp")

    def test_last_success_stays_null_rather_than_being_invented(self):
        self.cam.last_started = time.time()
        self.assertIsNone(self.entry()["last_success"])

    def test_it_publishes_restarts_and_liveness(self):
        self.cam.restarts = 4
        self.assertEqual(self.entry()["restarts"], 4)
        self.assertFalse(self.entry()["alive"])

    def test_no_frame_counters_are_offered(self):
        # ok/fail here would be a number nobody could account for.
        self.assertNotIn("ok", self.entry())
        self.assertNotIn("consec_fail", self.entry())


class FakeResponse:
    """Enough of a requests.Response for _grab() and raise_for_status()."""

    def __init__(self, content=b"", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise cap.requests.HTTPError(
                "%d Client Error: for url: http://192.0.2.1/snap?password=hunt"
                % self.status_code, response=self)


# The three bodies below are the real ones, copied from a Reolink on
# 2026-08-14. -9 is the important one: it is the same shape as a refusal and
# is not a refusal, which is why the classifier reads the code and not the
# presence of an error object.
REOLINK_WRONG_PW = (b'[{"cmd":"Snap","code":1,'
                    b'"error":{"detail":"login failed","rspCode":-7}}]')
REOLINK_NO_CREDS = (b'[{"cmd":"Snap","code":1,'
                    b'"error":{"detail":"please login first","rspCode":-6}}]')
REOLINK_BAD_CMD = (b'[{"cmd":"Unknown","code":1,'
                   b'"error":{"detail":"not support","rspCode":-9}}]')


class TestFailureClassification(unittest.TestCase):
    """Which failures are a refusal, and which only look like one."""

    def http_error(self, status):
        return cap.requests.HTTPError("boom", response=FakeResponse(b"", status))

    def test_401_is_a_refusal(self):
        self.assertEqual(cap.classify(self.http_error(401)), "auth")

    def test_403_is_a_refusal_too(self):
        # Some firmware separates "who are you" from "not you". Both mean the
        # same thing to a program holding one credential.
        self.assertEqual(cap.classify(self.http_error(403)), "auth")

    def test_500_is_not(self):
        self.assertEqual(cap.classify(self.http_error(500)), "other")

    def test_404_is_not(self):
        # A wrong URL is a config error, but not one more attempts can worsen.
        self.assertEqual(cap.classify(self.http_error(404)), "other")

    def test_reolink_wrong_password_is_a_refusal(self):
        exc = cap.NotAJpeg("too small", REOLINK_WRONG_PW)
        self.assertEqual(cap.classify(exc), "auth")

    def test_reolink_missing_credentials_is_a_refusal(self):
        exc = cap.NotAJpeg("too small", REOLINK_NO_CREDS)
        self.assertEqual(cap.classify(exc), "auth")

    def test_reolink_unknown_command_is_not_a_refusal(self):
        # Measured: -9 arrives as HTTP 200 with an error object, exactly like
        # -7 does. Treating "the camera said something" as "the camera refused
        # us" would back off from a malformed request for ever.
        exc = cap.NotAJpeg("too small", REOLINK_BAD_CMD)
        self.assertEqual(cap.classify(exc), "other")

    def test_an_html_page_is_not_a_refusal(self):
        # A URL typo that drops the query string lands on the camera's own web
        # page: 200, not a JPEG, no error object.
        exc = cap.NotAJpeg("bad SOI", b"<html><head><title>Login</title>")
        self.assertEqual(cap.classify(exc), "other")

    def test_an_empty_body_is_not_a_refusal(self):
        self.assertEqual(cap.classify(cap.NotAJpeg("empty", b"")), "other")

    def test_connection_refused_is_unreachable(self):
        self.assertEqual(cap.classify(cap.requests.ConnectionError("no route")),
                         "unreachable")

    def test_timeout_is_unreachable(self):
        self.assertEqual(cap.classify(cap.requests.Timeout("timed out")),
                         "unreachable")

    def test_anything_else_is_other(self):
        self.assertEqual(cap.classify(ValueError("who knows")), "other")


class TestRefusalReachesTheClassifier(unittest.TestCase):
    """The refusal body has to survive _grab() to be classifiable at all."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cam = cap.HttpCamera(
            {"name": "Gate", "url": "http://192.0.2.1/snap"},
            make_config(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def grab(self, response):
        self.cam.session.get = lambda url, timeout=None: response
        try:
            self.cam._grab(datetime(2026, 8, 14, 9, 0, 0))
        except Exception as exc:            # noqa: BLE001
            return exc
        return None

    def test_a_short_refusal_is_caught_by_the_size_check_not_the_soi_check(self):
        # The trap: min_bytes is 4096 and a Reolink refusal is ~140 bytes, so
        # the branch that fires for the shape we most need to recognise is the
        # size one. If only the SOI branch carried the payload, every Reolink
        # refusal would classify as 'other'.
        exc = self.grab(FakeResponse(REOLINK_WRONG_PW))
        self.assertIsInstance(exc, cap.NotAJpeg)
        self.assertIn("too small", str(exc))
        self.assertEqual(cap.classify(exc), "auth")

    def test_a_long_non_jpeg_keeps_its_body_too(self):
        exc = self.grab(FakeResponse(b"<html>" + b"x" * 9000))
        self.assertIsInstance(exc, cap.NotAJpeg)
        self.assertIn("SOI", str(exc))
        self.assertEqual(cap.classify(exc), "other")

    def test_a_401_never_reaches_the_body_checks(self):
        exc = self.grab(FakeResponse(b"Invalid Authority!", 401))
        self.assertIsInstance(exc, cap.requests.HTTPError)
        self.assertEqual(cap.classify(exc), "auth")

    def test_a_real_jpeg_still_works(self):
        jpeg = b"\xff\xd8" + b"j" * 9000
        self.assertIsNone(self.grab(FakeResponse(jpeg)))


class TestAuthBackOff(unittest.TestCase):
    """Two strikes, ten minutes, one more attempt, then every 31 minutes.

    Driven by calling _note_failure with an explicit clock: the ladder is
    arithmetic on wall-clock times, and sleeping through it in a test would
    prove the same thing 40 minutes more slowly.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cam = cap.HttpCamera(
            {"name": "Roof", "url": "http://192.0.2.1/snap"},
            make_config(self.tmp))
        # The ladder logs on purpose; this suite is not the place to read it.
        self.cam.log.setLevel(logging.CRITICAL)
        self.t = 1_000_000.0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def refuse(self, at=None):
        exc = cap.requests.HTTPError("401", response=FakeResponse(b"", 401))
        self.cam._note_failure(exc, self.t if at is None else at)

    def unreachable(self, at=None):
        self.cam._note_failure(cap.requests.ConnectionError("down"),
                               self.t if at is None else at)

    def test_one_refusal_does_not_stop_anything(self):
        self.refuse()
        self.assertEqual(self.cam.err_class, "auth")
        self.assertEqual(self.cam.err_ticks, 1)
        self.assertFalse(self.cam.auth_backed_off)
        self.assertEqual(self.cam.auth_quiet_until, 0.0)

    def test_two_refusals_buy_ten_minutes_of_silence(self):
        self.refuse()
        self.refuse(self.t + 5)
        self.assertTrue(self.cam.auth_backed_off)
        self.assertFalse(self.cam.auth_confirmed)
        self.assertEqual(self.cam.auth_quiet_until, self.t + 5 + cap.AUTH_PAUSE)

    def test_the_attempt_after_the_pause_confirms_it(self):
        self.refuse()
        self.refuse(self.t + 5)
        later = self.t + 5 + cap.AUTH_PAUSE
        self.refuse(later)
        self.assertTrue(self.cam.auth_confirmed)
        self.assertEqual(self.cam.auth_quiet_until, later + cap.AUTH_RETRY)

    def test_a_confirmed_refusal_re_arms_at_31_minutes_for_ever(self):
        self.refuse()
        self.refuse(self.t + 5)
        self.refuse(self.t + 5 + cap.AUTH_PAUSE)
        for n in range(1, 6):
            at = self.t + 5 + cap.AUTH_PAUSE + n * cap.AUTH_RETRY
            self.refuse(at)
            self.assertEqual(self.cam.auth_quiet_until, at + cap.AUTH_RETRY)
        self.assertTrue(self.cam.auth_confirmed)

    def test_31_minutes_clears_the_measured_30_minute_lockout(self):
        # The number is not arbitrary: the observed window is 30 minutes, and
        # a probe inside it can renew the lock rather than escape it.
        self.assertGreater(cap.AUTH_RETRY, 30 * 60)

    def test_a_success_ends_the_incident(self):
        self.refuse()
        self.refuse(self.t + 5)
        self.cam._end_incident()
        self.assertIsNone(self.cam.err_class)
        self.assertEqual(self.cam.err_ticks, 0)
        self.assertFalse(self.cam.auth_backed_off)
        self.assertFalse(self.cam.auth_confirmed)
        self.assertEqual(self.cam.auth_quiet_until, 0.0)

    def test_an_unreachable_tick_between_refusals_resets_the_ladder(self):
        # A camera that is sometimes refusing and sometimes unreachable is not
        # a diagnosis, and must never climb to a verdict.
        self.refuse()
        self.unreachable(self.t + 5)
        self.refuse(self.t + 10)
        self.assertEqual(self.cam.err_ticks, 1)
        self.assertFalse(self.cam.auth_backed_off)

    def test_an_unreachable_camera_never_backs_off(self):
        # Backing off here would turn a 30-second reboot into a 10-minute hole.
        for n in range(20):
            self.unreachable(self.t + n * 5)
        self.assertEqual(self.cam.err_class, "unreachable")
        self.assertEqual(self.cam.err_ticks, 20)
        self.assertEqual(self.cam.auth_quiet_until, 0.0)

    def test_the_incident_start_is_stable_while_it_lasts(self):
        # It is the incident's identity downstream, so it must not move.
        self.refuse()
        began = self.cam.err_since
        self.refuse(self.t + 5)
        self.refuse(self.t + 5 + cap.AUTH_PAUSE)
        self.assertEqual(self.cam.err_since, began)

    def test_the_detail_is_redacted(self):
        exc = cap.requests.HTTPError(
            "401 Client Error for url: http://cam/snap?password=hunter2",
            response=FakeResponse(b"", 401))
        self.cam._note_failure(exc, self.t)
        self.assertNotIn("hunter2", self.cam.err_detail)
        self.assertIn("401", self.cam.err_detail)


class TestRefusalIsNotRetriedInsideTheTick(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cam = cap.HttpCamera(
            {"name": "Gate", "url": "http://192.0.2.1/snap"},
            make_config(self.tmp))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        cap.STOP.clear()

    def test_a_401_is_not_retried(self):
        # The second attempt cannot succeed and does count towards the
        # camera's lockout, so it is the one failure worth not repeating.
        called = []
        self.cam._grab = lambda dt, timeout=None: called.append(dt)
        first = cap.requests.HTTPError("401", response=FakeResponse(b"", 401))
        err = self.cam._retry_grab(datetime(2026, 8, 14, 9, 0, 0),
                                   time.time() + 5.0, first)
        self.assertEqual(called, [])
        self.assertIs(err, first)

    def test_a_500_still_is_retried(self):
        called = []
        self.cam._grab = lambda dt, timeout=None: called.append(dt)
        first = cap.requests.HTTPError("500", response=FakeResponse(b"", 500))
        err = self.cam._retry_grab(datetime(2026, 8, 14, 9, 0, 0),
                                   time.time() + 5.0, first)
        self.assertEqual(len(called), 1)
        self.assertIsNone(err)


class TestErrorReachesTheStateFile(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cam = cap.HttpCamera(
            {"name": "Roof", "url": "http://192.0.2.1/snap"},
            make_config(self.tmp))
        self.cam.log.setLevel(logging.CRITICAL)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entry(self):
        return cap.capture_state([self.cam], time.time())["cameras"][0]

    def test_a_healthy_camera_reports_no_error(self):
        self.assertIsNone(self.entry()["error"])

    def test_a_refusal_is_published_with_its_class_and_start(self):
        exc = cap.requests.HTTPError("401", response=FakeResponse(b"", 401))
        self.cam._note_failure(exc, 1_000_000.0)
        err = self.entry()["error"]
        self.assertEqual(err["class"], "auth")
        self.assertEqual(err["ticks"], 1)
        self.assertFalse(err["confirmed"])
        self.assertIsNotNone(err["since"])
        self.assertIsNone(err["quiet_until"])

    def test_a_confirmed_refusal_says_so_and_names_the_next_attempt(self):
        exc = cap.requests.HTTPError("401", response=FakeResponse(b"", 401))
        for at in (0, 5, 5 + cap.AUTH_PAUSE):
            self.cam._note_failure(exc, 1_000_000.0 + at)
        err = self.entry()["error"]
        self.assertTrue(err["confirmed"])
        self.assertIsNotNone(err["quiet_until"])

    def test_the_published_detail_carries_no_credential(self):
        exc = cap.requests.HTTPError(
            "401 for url: http://cam/s?user=admin&password=hunter2",
            response=FakeResponse(b"", 401))
        self.cam._note_failure(exc, 1_000_000.0)
        self.assertNotIn("hunter2", json.dumps(self.entry()))

    def test_rtsp_cameras_carry_no_error_field(self):
        # ffmpeg holds the credential and fetches the frames there, so nothing
        # in this process ever sees the camera's answer. Publishing a null
        # would read as "fine", which is a claim this daemon cannot make.
        rtsp = cap.RtspCamera({"name": "Hallway", "url": "rtsp://192.0.2.9/s1"},
                              make_config(self.tmp))
        entry = cap.capture_state([rtsp], time.time())["cameras"][0]
        self.assertNotIn("error", entry)


class TestRtspWritesItsFrames(unittest.TestCase):
    """The RTSP path never worked, from the first release to 0.1.6.

    The command put %Y-%m-%d in the output pattern and passed
    `-strftime_mkdir 1` to have the muxer create it. That option belongs to
    the **hls** muxer; image2 has never had it, and ffmpeg does not complain
    about an unknown private option even at -loglevel warning. So every write
    failed with "Could not open file" and the daemon restart-looped. Reported
    from a real 0.1.6 install, reproduced against ffmpeg 6.1.1.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = make_config(self.tmp / "frames")
        self.cam = cap.RtspCamera({"name": "Hallway", "method": "rtsp",
                                   "url": "rtsp://192.0.2.9/s1"}, self.cfg)
        self.day = "2026-08-14"

    def test_the_muxer_is_never_asked_to_make_a_directory(self):
        self.assertNotIn("-strftime_mkdir", self.cam._cmd(self.day))

    def test_the_output_pattern_holds_no_directory_strftime(self):
        # %H%M%S in the filename still needs -strftime; what must not be
        # there is a %-directory, because nothing would create it.
        pattern = self.cam._cmd(self.day)[-1]
        self.assertNotIn("%Y", pattern)
        self.assertTrue(pattern.endswith("%H%M%S.jpg"), pattern)
        self.assertIn(self.day, pattern)
        self.assertIn("-strftime", self.cam._cmd(self.day))

    def test_the_pattern_is_inside_the_day_directory_that_gets_created(self):
        made = self.cam._prepare_day(self.day)
        self.assertTrue(made.is_dir())
        self.assertEqual(Path(self.cam._cmd(self.day)[-1]).parent, made)

    def test_preparing_the_day_records_its_cadence(self):
        made = self.cam._prepare_day(self.day)
        self.assertEqual(cap.read_cadence(made),
                         (self.cam.interval, self.cam.framerate))

    def test_preparing_twice_is_harmless(self):
        first = self.cam._prepare_day(self.day)
        (first / "120000.jpg").write_bytes(b"\xff\xd8frame")
        self.cam._prepare_day(self.day)
        self.assertTrue((first / "120000.jpg").exists())

    def test_a_day_with_no_frames_is_discarded(self):
        # A camera unreachable all day would otherwise leave one empty
        # directory per day for ever: the encoder SKIPs and never cleans up.
        made = self.cam._prepare_day(self.day)
        self.cam._discard_empty_day(made)
        self.assertFalse(made.exists())

    def test_a_day_with_frames_is_kept(self):
        made = self.cam._prepare_day(self.day)
        (made / "120000.jpg").write_bytes(b"\xff\xd8frame")
        self.cam._discard_empty_day(made)
        self.assertTrue(made.exists())
        self.assertTrue((made / "120000.jpg").exists())

    def test_anything_unexpected_in_the_day_is_left_alone(self):
        made = self.cam._prepare_day(self.day)
        (made / "notes.txt").write_text("do not delete me", encoding="utf-8")
        self.cam._discard_empty_day(made)
        self.assertTrue(made.exists())

    def test_discarding_a_day_that_is_gone_does_not_raise(self):
        self.cam._discard_empty_day(self.tmp / "nope" / "2026-08-14")


if __name__ == "__main__":
    unittest.main()

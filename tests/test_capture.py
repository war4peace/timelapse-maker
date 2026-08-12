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
        self.assertIn("fps=1/120", c._cmd())


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
        cmd = cam._cmd()
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


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the `timelapse usage` disk report.

The report's real job is not arithmetic; it is telling you when frames on disk
have nothing that will ever encode them. A camera removed from the config, or
merely *disabled*, keeps its directory forever and the nightly encode skips it.
Someone runs this precisely because disk is filling up, so those two cases have
to be called out rather than silently folded into a total.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import _support                                            # noqa: F401

import timelapse_platform as plat
import timelapse_test as tt


def make_frames(day_dir, count, size=1024):
    day_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (day_dir / f"{i:06d}.jpg").write_bytes(b"\xff\xd8" + b"\0" * (size - 2))


class TestHumanBytes(unittest.TestCase):

    def test_scales_through_the_units(self):
        self.assertEqual(tt.human_bytes(512), "512 B")
        self.assertEqual(tt.human_bytes(2048), "2 KB")
        self.assertEqual(tt.human_bytes(5 * 1024 ** 2), "5.0 MB")
        self.assertEqual(tt.human_bytes(3 * 1024 ** 3), "3.0 GB")

    def test_does_not_run_past_terabytes(self):
        self.assertIn("TB", tt.human_bytes(9 * 1024 ** 5))


class TestScanCameraFrames(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_counts_frames_and_bytes_across_days(self):
        make_frames(self.tmp / "2026-08-01", 3, size=1000)
        make_frames(self.tmp / "2026-08-02", 2, size=1000)
        st = tt.scan_camera_frames(self.tmp)
        self.assertEqual(st["frames"], 5)
        self.assertEqual(st["size"], 5000)
        self.assertEqual(st["days"], ["2026-08-01", "2026-08-02"])

    def test_ignores_directories_that_are_not_dates(self):
        make_frames(self.tmp / "2026-08-01", 2)
        make_frames(self.tmp / "scratch", 9)
        st = tt.scan_camera_frames(self.tmp)
        self.assertEqual(st["frames"], 2)
        self.assertEqual(st["days"], ["2026-08-01"])

    def test_counts_leftover_tmp_files_separately(self):
        # A capture that died between write() and os.replace(). They are not
        # frames and must not inflate the byte total.
        d = self.tmp / "2026-08-01"
        make_frames(d, 2, size=1000)
        (d / ".999999.tmp").write_bytes(b"partial")
        st = tt.scan_camera_frames(self.tmp)
        self.assertEqual(st["frames"], 2)
        self.assertEqual(st["size"], 2000)
        self.assertEqual(st["stray"], 1)

    def test_days_are_sorted_so_the_range_reads_correctly(self):
        for day in ("2026-08-09", "2026-08-01", "2026-08-10"):
            make_frames(self.tmp / day, 1)
        st = tt.scan_camera_frames(self.tmp)
        self.assertEqual(st["days"][0], "2026-08-01")
        self.assertEqual(st["days"][-1], "2026-08-10")

    def test_a_missing_directory_is_not_an_error(self):
        st = tt.scan_camera_frames(self.tmp / "nope")
        self.assertEqual(st, {"days": [], "frames": 0, "size": 0, "stray": 0})


class TestUsageReport(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.frames = self.tmp / "frames"
        self.frames.mkdir()
        (self.tmp / "video").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg(self, cameras):
        return {"paths": {"frames_root": str(self.frames),
                          "video_output": str(self.tmp / "video")},
                "cameras": cameras}

    def run_report(self, cameras):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tt.report_usage(self.cfg(cameras))
        return buf.getvalue()

    def cam(self, name, enabled=True):
        return {"name": name, "enabled": enabled}

    def test_reports_counts_and_a_total(self):
        make_frames(self.frames / "Gate" / "2026-08-01", 4, size=1000)
        make_frames(self.frames / "Roof" / "2026-08-01", 6, size=1000)
        out = self.run_report([self.cam("Gate"), self.cam("Roof")])
        self.assertIn("Gate", out)
        self.assertIn("10", out)            # total frames
        self.assertIn("total", out)

    def test_frames_with_no_config_entry_are_flagged_as_orphans(self):
        make_frames(self.frames / "Garage" / "2026-08-01", 2)
        out = self.run_report([self.cam("Gate")])
        self.assertIn("ORPHAN", out)
        self.assertIn("not in the config at all", out)

    def test_a_disabled_camera_with_frames_is_flagged_too(self):
        # The trap: `enabled: false` hides the camera from the encoder as
        # surely as deleting it, so its frames accumulate untouched.
        make_frames(self.frames / "Roof" / "2026-08-01", 2)
        out = self.run_report([self.cam("Roof", enabled=False)])
        self.assertIn("disabled in the config", out)
        self.assertIn("stay forever", out)

    def test_an_enabled_camera_with_frames_raises_nothing(self):
        make_frames(self.frames / "Gate" / "2026-08-01", 2)
        out = self.run_report([self.cam("Gate")])
        self.assertNotIn("ORPHAN", out)
        self.assertNotIn("stay forever", out)

    def test_a_configured_camera_with_no_frames_yet_is_listed(self):
        out = self.run_report([self.cam("Driveway")])
        self.assertIn("Driveway", out)
        self.assertIn("not captured yet", out)
        self.assertNotIn("ORPHAN", out)

    def test_stray_tmp_files_are_reported_but_not_counted(self):
        d = self.frames / "Gate" / "2026-08-01"
        make_frames(d, 2, size=1000)
        (d / ".x.tmp").write_bytes(b"junk")
        out = self.run_report([self.cam("Gate")])
        self.assertIn("leftover .tmp", out)

    def test_a_missing_frames_root_says_so_instead_of_crashing(self):
        shutil.rmtree(self.frames)
        out = self.run_report([self.cam("Gate")])
        self.assertIn("does not exist", out)

    def test_no_trailing_whitespace_on_any_line(self):
        make_frames(self.frames / "Gate" / "2026-08-01", 1)
        out = self.run_report([self.cam("Gate")])
        for ln in out.splitlines():
            self.assertEqual(ln, ln.rstrip(), f"trailing space: {ln!r}")


class TestRsyncFlagCheckOutput(unittest.TestCase):
    """What the operator actually reads. The unit tests below this cover the
    probe's return values; this covers the four lines it prints, which is
    where the false alarm was seen."""

    CFG = {"transfer": {"rsync_args": ["-a", "--partial",
                                       "--remove-source-files"]}}

    def run_check(self, result, detail, working=None):
        import timelapse_encode as enc
        buf = io.StringIO()
        with mock.patch.object(enc, "try_rsync_args",
                               return_value=(result, detail)), \
                mock.patch.object(enc, "probe_rsync_flags",
                                  return_value=working), \
                mock.patch.object(tt, "service_account",
                                  return_value="timelapse"), \
                contextlib.redirect_stdout(buf):
            tt.check_rsync_args(self.CFG, "/mnt/cctv/TL/")
        return buf.getvalue()

    def test_untestable_is_not_reported_as_a_failure(self):
        # The reported output, verbatim:
        #   FAIL  rsync ... fails against /mnt/cctv/TL/: exit 1: runuser: may
        #         not be used by non-root users
        #   ....  no flag combination worked; check the share permissions
        out = self.run_check(None, "only root can run the probe as timelapse; "
                                   "try: sudo timelapse test")
        self.assertNotIn("FAIL", out)
        self.assertNotIn("fails against", out)
        self.assertNotIn("share permissions", out)
        self.assertIn("not checked", out)
        self.assertIn("sudo timelapse test", out)

    def test_a_real_failure_still_reads_as_one(self):
        # The check has to keep working when it genuinely can run.
        out = self.run_check(False, "exit 23: rsync: chgrp failed",
                             working=["-rt", "--partial"])
        self.assertIn("FAIL", out)
        self.assertIn("exit 23", out)
        self.assertIn("-rt --partial", out)
        self.assertIn("sudo timelapse transfer", out)

    def test_success_names_the_account_it_proved_it_for(self):
        out = self.run_check(True, "")
        self.assertIn("PASS", out)
        self.assertIn("as timelapse", out)


class TestCameraNameCheck(unittest.TestCase):
    """The config that already holds a bad name, which the wizard cannot see.

    Two ways in: hand-edited through `timelapse config`, or written on Linux
    and carried to a Windows box, where two directories become one.
    """

    def run_check(self, names):
        cfg = {"cameras": [{"name": n} for n in names]}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tt.check_camera_names(cfg)
        return buf.getvalue()

    def test_ordinary_names_pass(self):
        out = self.run_check(["Driveway", "Court180", "Roof"])
        self.assertIn("PASS", out)
        self.assertNotIn("FAIL", out)

    def test_an_exact_duplicate_is_caught_on_every_platform(self):
        # A hand-edited config can hold this on Linux, where it is just as
        # destructive: one directory, two cameras, one video.
        out = self.run_check(["Workshop", "Workshop"])
        self.assertIn("FAIL", out)
        self.assertIn("one folder here", out)

    def test_case_variants_are_judged_by_this_filesystem(self):
        """Workshop and workshop are a real pair from this project's library.

        On NTFS they are one directory and the check must fail; on Linux they
        are two and it must not, because reporting a working install as broken
        is the error `try_rsync_args` exists to avoid.
        """
        out = self.run_check(["Workshop", "workshop"])
        collides = os.path.normcase("Workshop") == os.path.normcase("workshop")
        self.assertEqual("FAIL" in out, collides)

    def test_a_reserved_name_is_reported_with_the_command_to_fix_it(self):
        out = self.run_check(["Driveway", "NUL"])
        self.assertIn("FAIL", out)
        self.assertIn("reserved device name", out)
        self.assertIn("timelapse cameras -e NUL", out)

    def test_a_disabled_camera_is_checked_too(self):
        """Its frames are still in that directory, and re-enabling it is the
        moment the collision starts."""
        cfg = {"cameras": [{"name": "Shed"},
                           {"name": "Shed", "enabled": False}]}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tt.check_camera_names(cfg)
        self.assertIn("FAIL", buf.getvalue())

    def test_no_cameras_is_not_a_failure(self):
        out = self.run_check([])
        self.assertNotIn("FAIL", out)


class TestStateDirCheck(unittest.TestCase):
    """A missing state directory stops both daemons from starting at all.

    systemd's error names a mount namespace and nothing else, so the pre-flight
    is the only place an operator can be told what is actually wrong.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_check(self, cfg):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tt.test_state_dir(cfg)
        return buf.getvalue()

    def test_a_present_writable_directory_passes(self):
        d = self.tmp / "state"
        d.mkdir()
        out = self.run_check({"paths": {"state_dir": str(d)}})
        self.assertIn("PASS", out)
        self.assertNotIn("FAIL", out)

    def test_it_leaves_nothing_behind(self):
        d = self.tmp / "state"
        d.mkdir()
        self.run_check({"paths": {"state_dir": str(d)}})
        self.assertEqual(list(d.iterdir()), [])

    def test_a_missing_directory_fails_and_says_why(self):
        out = self.run_check({"paths": {"state_dir": str(self.tmp / "gone")}})
        self.assertIn("FAIL", out)
        self.assertIn("ReadWritePaths", out)
        self.assertIn("timelapse setup", out)

    def test_an_unwritable_directory_names_the_account_it_tried_as(self):
        d = self.tmp / "state"
        d.mkdir()
        with mock.patch.object(Path, "write_text",
                               side_effect=PermissionError(13, "denied")):
            out = self.run_check({"paths": {"state_dir": str(d)}})
        self.assertIn("FAIL", out)
        self.assertIn("not writable by", out)

    def test_a_config_without_the_key_checks_the_default(self):
        # The platform's default rather than the literal: what the pre-flight
        # must not do is check some other directory than the one the daemons
        # will actually write to on this machine.
        #
        # is_dir() is stubbed rather than left to hit the real path, and that
        # is not tidiness: on Windows the default IS %ProgramData%\timelapse\
        # state, which install.ps1 restricts to SYSTEM and Administrators, so
        # this test began raising PermissionError on the developer's box the
        # moment a real install had been run there. A test whose result depends
        # on whether the product happens to be installed is a test that will
        # eventually be wrong on somebody's machine.
        with mock.patch.object(Path, "is_dir", return_value=False):
            out = self.run_check({"paths": {}})
        self.assertIn(plat.STATE_DIR_DEFAULT, out)

    def test_a_directory_it_may_not_even_look_at_is_a_separate_finding(self):
        """"Missing" and "not allowed to look" want different remedies, and

        Path.is_dir() raises for the second on Windows rather than returning
        False. Found for real: after an install, an unelevated pre-flight
        stat()ed the restricted %ProgramData%\\timelapse\\state and tracebacked,
        which reads as the tool being broken rather than as the answer.
        """
        with mock.patch.object(Path, "is_dir",
                               side_effect=PermissionError(5, "Access denied")):
            out = self.run_check({"paths": {"state_dir": str(self.tmp)}})
        self.assertIn("FAIL", out)
        self.assertIn("Cannot tell whether", out)
        self.assertIn("permission problem", out)
        self.assertNotIn("does not exist", out)


if __name__ == "__main__":
    unittest.main()

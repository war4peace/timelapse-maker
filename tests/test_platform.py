"""Unit tests for timelapse_platform.py: the one file allowed a platform branch.

Two things are being pinned here, and they are different in kind.

The first is the storage scan, which simply moved from timelapse_setup at
0.2.0 and is tested exactly as it was: against a synthetic /proc/mounts, so
the dozen kinds of thing that look like a disk and are not are always present
regardless of the machine this runs on.

The second is new and is the reason this module exists. Every platform branch
is code that one CI leg cannot reach, which is the standing cost item 11e
admits to. The path derivation is therefore a pure function taking the
platform as an argument, so the Windows answers are asserted on Linux and the
Linux answers on Windows, and neither leg is trusting the other to have looked.
"""

import ast
import contextlib
import ctypes
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

import _support                                            # noqa: F401
from _support import FakeStatVFS, write_mounts

import timelapse_platform as plat


def fake_statvfs(_target):
    return FakeStatVFS(free_gb=100, total_gb=200)


def no_rotational(_source):
    return None


class TestScanMounts(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, lines, statvfs=fake_statvfs):
        return plat.scan_mounts(write_mounts(self.tmp, lines),
                                statvfs=statvfs, rotational=no_rotational)

    def test_accepts_a_plain_disk(self):
        disks = self.scan(["/dev/sda1 /mnt/data ext4 rw,relatime 0 0"])
        self.assertEqual([d["mount"] for d in disks], ["/mnt/data"])

    def test_rejects_pseudo_filesystems(self):
        lines = [
            "proc /proc proc rw 0 0",
            "sysfs /sys sysfs rw 0 0",
            "none /run tmpfs rw 0 0",
            "cgroup2 /sys/fs/cgroup cgroup2 rw 0 0",
            "none /snap/core squashfs ro 0 0",
            "overlay /var/lib/docker/overlay2/x overlay rw 0 0",
            "devtmpfs /dev devtmpfs rw 0 0",
            "/dev/sda1 /mnt/data ext4 rw 0 0",
        ]
        self.assertEqual([d["mount"] for d in self.scan(lines)], ["/mnt/data"])

    def test_rejects_network_fstypes(self):
        # Fine as a transfer target, wrong for frames: os.replace() gives no
        # atomicity guarantee across the wire.
        #
        # The source is deliberately /dev/-prefixed and the mountpoint outside
        # SKIP_PREFIXES, so the ONLY thing that can reject these is the fstype
        # rule. Realistic network sources ("nas:/vol") are also caught by the
        # source filter, which would make this pass for the wrong reason.
        for fstype in ("nfs", "nfs4", "cifs", "smb3", "9p", "fuse.sshfs",
                       "ceph", "glusterfs", "lustre"):
            with self.subTest(fstype=fstype):
                self.assertEqual(
                    self.scan([f"/dev/sda1 /mnt/data {fstype} rw 0 0"]), [],
                    f"{fstype} must not be offered as frame storage")

    def test_rejects_realistic_network_mount_lines(self):
        lines = [
            "nas:/vol /mnt/nfs nfs4 rw 0 0",
            "//nas/share /mnt/cifs cifs rw 0 0",
            "D:\\134 /mnt/d 9p rw 0 0",
            "/dev/sda1 /mnt/data ext4 rw 0 0",
        ]
        self.assertEqual([d["mount"] for d in self.scan(lines)], ["/mnt/data"])

    def test_rejects_read_only_mounts(self):
        lines = ["/dev/sr0 /media/cdrom ext4 ro,relatime 0 0",
                 "/dev/sda1 /mnt/data ext4 rw 0 0"]
        self.assertEqual([d["mount"] for d in self.scan(lines)], ["/mnt/data"])

    def test_ro_substring_in_another_option_is_not_read_only(self):
        # "errors=remount-ro" contains "ro" but the mount is writable.
        lines = ["/dev/sda1 / ext4 rw,relatime,errors=remount-ro 0 0"]
        self.assertEqual([d["mount"] for d in self.scan(lines)], ["/"])

    def test_rejects_skipped_prefixes(self):
        lines = ["/dev/sda1 /boot/efi vfat rw 0 0",
                 "/dev/sdb1 /var/lib/docker/x ext4 rw 0 0",
                 "/dev/sdc1 /mnt/data ext4 rw 0 0"]
        self.assertEqual([d["mount"] for d in self.scan(lines)], ["/mnt/data"])

    def test_root_survives_the_prefix_filter(self):
        # "/" is a prefix of everything; it must be special-cased.
        self.assertEqual([d["mount"] for d in
                          self.scan(["/dev/sda1 / ext4 rw 0 0"])], ["/"])

    def test_rejects_sources_that_are_not_devices(self):
        self.assertEqual(self.scan(["tmpfs /mnt/ram ext4 rw 0 0"]), [])

    def test_deduplicates_a_device_mounted_twice(self):
        # Keeps the primary (shortest) mountpoint. Both mountpoints must be
        # ones nothing else would reject, or this passes for the wrong reason.
        lines = ["/dev/sdd /mnt/data ext4 rw 0 0",
                 "/dev/sdd /mnt/data/bind-mount ext4 rw 0 0"]
        disks = self.scan(lines)
        self.assertEqual([d["mount"] for d in disks], ["/mnt/data"])

    def test_deduplicates_regardless_of_line_order(self):
        lines = ["/dev/sdd /mnt/data/bind-mount ext4 rw 0 0",
                 "/dev/sdd /mnt/data ext4 rw 0 0"]
        self.assertEqual([d["mount"] for d in self.scan(lines)], ["/mnt/data"])

    def test_different_devices_are_both_kept(self):
        lines = ["/dev/sda1 /mnt/one ext4 rw 0 0",
                 "/dev/sdb1 /mnt/two ext4 rw 0 0"]
        self.assertEqual(len(self.scan(lines)), 2)

    def test_sorted_by_free_space_descending(self):
        sizes = {"/mnt/small": 10, "/mnt/big": 900, "/mnt/mid": 400}

        def sizing(target):
            return FakeStatVFS(free_gb=sizes[target], total_gb=1000)

        lines = [f"/dev/sd{c}1 {m} ext4 rw 0 0"
                 for c, m in zip("abc", sizes)]
        disks = self.scan(lines, statvfs=sizing)
        self.assertEqual([d["mount"] for d in disks],
                         ["/mnt/big", "/mnt/mid", "/mnt/small"])

    def test_skips_filesystems_that_cannot_be_stat_ed(self):
        def boom(_target):
            raise OSError("gone")
        self.assertEqual(self.scan(["/dev/sda1 /mnt/data ext4 rw 0 0"],
                                   statvfs=boom), [])

    def test_skips_zero_block_filesystems(self):
        def empty(_target):
            return FakeStatVFS(free_gb=0, total_gb=0)
        self.assertEqual(self.scan(["/dev/sda1 /mnt/data ext4 rw 0 0"],
                                   statvfs=empty), [])

    def test_decodes_escaped_mountpoints(self):
        disks = self.scan([r"/dev/sda1 /mnt/my\040disk ext4 rw 0 0"])
        self.assertEqual(disks[0]["mount"], "/mnt/my disk")

    def test_tolerates_short_and_blank_lines(self):
        disks = self.scan(["", "garbage", "/dev/sda1 /mnt/data ext4 rw 0 0"])
        self.assertEqual([d["mount"] for d in disks], ["/mnt/data"])

    def test_missing_mounts_file_returns_empty(self):
        self.assertEqual(plat.scan_mounts("/nonexistent/mounts"), [])

    def test_reports_free_and_total_in_bytes(self):
        disks = self.scan(["/dev/sda1 /mnt/data ext4 rw 0 0"])
        self.assertAlmostEqual(disks[0]["free"] / 1024 ** 3, 100, delta=0.1)
        self.assertAlmostEqual(disks[0]["total"] / 1024 ** 3, 200, delta=0.1)


class TestBaseDevice(unittest.TestCase):
    """Partition-name stripping, against a fake /sys/block."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for dev in ("sda", "nvme0n1", "mmcblk0"):
            (self.tmp / dev / "queue").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def base(self, source):
        return plat._base_device(source, sys_block=str(self.tmp))

    def test_whole_disk(self):
        self.assertEqual(self.base("/dev/sda"), "sda")

    def test_sata_partition(self):
        self.assertEqual(self.base("/dev/sda1"), "sda")

    def test_nvme_partition(self):
        self.assertEqual(self.base("/dev/nvme0n1p2"), "nvme0n1")

    def test_nvme_whole_disk(self):
        self.assertEqual(self.base("/dev/nvme0n1"), "nvme0n1")

    def test_mmc_partition(self):
        self.assertEqual(self.base("/dev/mmcblk0p1"), "mmcblk0")

    def test_unknown_device(self):
        self.assertIsNone(self.base("/dev/sdz9"))

    def test_non_device_source(self):
        self.assertIsNone(self.base("tmpfs"))

    def test_rotational_reads_the_flag(self):
        (self.tmp / "sda" / "queue" / "rotational").write_text("1\n")
        (self.tmp / "nvme0n1" / "queue" / "rotational").write_text("0\n")
        self.assertIs(plat._is_rotational("/dev/sda1", str(self.tmp)), True)
        self.assertIs(plat._is_rotational("/dev/nvme0n1p2", str(self.tmp)),
                      False)

    def test_rotational_is_unknown_when_absent(self):
        self.assertIsNone(plat._is_rotational("/dev/sda1", str(self.tmp)))


class TestLocations(unittest.TestCase):
    """The path derivation, both branches, on whichever platform is running.

    locations() takes the platform rather than reading it precisely so that
    this class is not half-skipped on each CI leg. A branch only one runner
    can reach is a branch that regresses on the other one quietly.
    """

    WIN_ENV = {"ProgramData": "C:\\ProgramData"}

    def test_linux_puts_config_and_state_where_the_fhs_does(self):
        loc = plat.locations(False)
        self.assertEqual(loc["config_dir"], "/etc/timelapse")
        self.assertEqual(loc["config"], "/etc/timelapse/config.json")
        self.assertEqual(loc["data_root"], "/var/lib/timelapse")
        self.assertEqual(loc["state"], "/var/lib/timelapse/state")
        self.assertEqual(loc["web_state"], "/var/lib/timelapse/web")

    def test_windows_puts_all_of_it_under_program_data(self):
        loc = plat.locations(True, self.WIN_ENV)
        self.assertEqual(loc["config"],
                         "C:\\ProgramData\\timelapse\\config.json")
        self.assertEqual(loc["state"], "C:\\ProgramData\\timelapse\\state")
        self.assertEqual(loc["web_state"], "C:\\ProgramData\\timelapse\\web")

    def test_the_windows_branch_produces_windows_separators_anywhere(self):
        """ntpath.join, not os.path.join, and this is what that buys.

        Derived on Linux it must still be a Windows path, or the Linux legs
        would be asserting against something no Windows box will ever see.
        """
        for value in plat.locations(True, self.WIN_ENV).values():
            self.assertNotIn("/", value)
            self.assertTrue(value.startswith("C:\\ProgramData\\timelapse"))

    def test_program_data_prefers_the_environment(self):
        self.assertEqual(plat.program_data({"ProgramData": "D:\\PD"}), "D:\\PD")

    def test_all_users_profile_is_the_second_answer(self):
        # A service or a scheduled task runs with a stripped environment, and
        # this is the variable that tends to survive it.
        self.assertEqual(plat.program_data({"ALLUSERSPROFILE": "D:\\PD"}),
                         "D:\\PD")

    def test_an_empty_environment_still_answers(self):
        # A machine with neither set is not one where guessing differently
        # would help, so the documented default is the right last resort.
        self.assertEqual(plat.program_data({}), "C:\\ProgramData")

    def test_state_and_web_state_are_never_the_same_directory(self):
        """The web UI's index is the one directory that service may write.

        Collapsing it into the daemons' state directory would hand a
        network-facing service write access to the heartbeat it is meant only
        to be reading, on either platform.
        """
        for windows in (False, True):
            loc = plat.locations(windows, self.WIN_ENV)
            self.assertNotEqual(loc["state"], loc["web_state"])

    def test_the_module_constants_come_from_the_derivation(self):
        # The constants are what every caller imports; the function is what
        # the tests above pin. This is the join between the two.
        loc = plat.locations(plat.IS_WINDOWS)
        self.assertEqual(plat.CONFIG_DIR, loc["config_dir"])
        self.assertEqual(plat.CONFIG_PATH, loc["config"])
        self.assertEqual(plat.DATA_ROOT_DEFAULT, loc["data_root"])
        self.assertEqual(plat.STATE_DIR_DEFAULT, loc["state"])
        self.assertEqual(plat.WEB_STATE_DIR_DEFAULT, loc["web_state"])

    def test_the_linux_constants_stay_linux_whatever_this_platform_is(self):
        """Named separately because a systemd unit is a POSIX artefact.

        writable_paths() emits ReadWritePaths= lines, and a ProgramData path
        in one of those would be nonsense; so would refusing to generate the
        unit on the platform the tests happen to run on.
        """
        self.assertEqual(plat.LINUX_STATE_DIR, "/var/lib/timelapse/state")
        self.assertEqual(plat.LINUX_WEB_STATE_DIR, "/var/lib/timelapse/web")
        self.assertEqual(plat.LINUX_CONFIG_DIR, "/etc/timelapse")


class TestServiceIsActive(unittest.TestCase):
    """True, False, or None, and None is not "stopped"."""

    def _ask(self, rc=0, exc=None):
        result = mock.Mock(returncode=rc)
        which = mock.patch.object(plat.shutil, "which",
                                  return_value="/bin/systemctl")
        run = mock.patch.object(plat.subprocess, "run", side_effect=exc,
                                return_value=result)
        with mock.patch.object(plat, "IS_WINDOWS", False), which, run:
            return plat.service_is_active("timelapse-capture.service")

    def test_no_systemctl_means_the_question_could_not_be_put(self):
        with mock.patch.object(plat, "IS_WINDOWS", False), \
             mock.patch.object(plat.shutil, "which", return_value=None):
            self.assertIsNone(
                plat.service_is_active("timelapse-capture.service"))

    def _windows(self, state=None, task=None):
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "service_state", return_value=state), \
             mock.patch.object(plat, "task_exists", return_value=task):
            return (plat.service_is_active(plat.CAPTURE_UNIT),
                    plat.service_is_active(plat.ENCODE_UNIT))

    def test_a_running_windows_service_is_running(self):
        self.assertIs(self._windows(state=plat.SERVICE_RUNNING)[0], True)

    def test_a_stopped_windows_service_is_not(self):
        self.assertIs(self._windows(state=plat.SERVICE_STOPPED)[0], False)

    def test_a_service_the_scm_has_never_heard_of_is_not_running(self):
        """Absent and stopped are different facts and the same answer here.

        service_state() keeps them apart, because installing and starting are
        different remedies; service_is_active() is asked a yes/no question and
        must not answer None to it, which would read as "could not ask".
        """
        self.assertIs(self._windows(state=plat.SERVICE_ABSENT)[0], False)

    def test_a_windows_service_that_could_not_be_asked_about_says_so(self):
        """Not False. A service manager that cannot be queried must not report

        a healthy system as stopped: that is the error the three-way
        daemon/timer/oneshot split in the status page exists to avoid, met
        here in a smaller place.
        """
        self.assertIsNone(self._windows(state=None)[0])

    def test_a_scheduled_task_is_asked_about_as_a_task(self):
        """The batch jobs are not services there, so nothing may ask the SCM.

        `systemctl is-active` on a .timer answers "is it armed", and this is
        the same question: a task registered and enabled is a timer armed.
        """
        self.assertIs(self._windows(state=plat.SERVICE_RUNNING, task=True)[1],
                      True)
        self.assertIs(self._windows(state=plat.SERVICE_RUNNING, task=False)[1],
                      False)
        self.assertIsNone(self._windows(state=plat.SERVICE_RUNNING)[1])

    def test_exit_zero_is_running(self):
        self.assertIs(self._ask(rc=0), True)

    def test_a_non_zero_exit_is_not_running(self):
        self.assertIs(self._ask(rc=3), False)

    def test_an_unrunnable_systemctl_is_unanswerable_not_stopped(self):
        self.assertIsNone(self._ask(exc=OSError("boom")))

    def test_a_timeout_is_unanswerable_too(self):
        self.assertIsNone(
            self._ask(exc=subprocess.TimeoutExpired("systemctl", 15)))


class TestRestartService(unittest.TestCase):

    def _restart(self, rc=0, exc=None):
        result = mock.Mock(returncode=rc)
        run = mock.patch.object(plat.subprocess, "run", side_effect=exc,
                                return_value=result)
        with mock.patch.object(plat, "IS_WINDOWS", False), run:
            return plat.restart_service("timelapse-capture.service")

    def test_success_carries_no_detail(self):
        self.assertEqual(self._restart(rc=0), (True, ""))

    def test_a_non_zero_exit_carries_no_detail_either(self):
        # Deliberate: the reason is nearly always "not root", and the caller
        # words that better than an exit code does.
        self.assertEqual(self._restart(rc=1), (False, ""))

    def test_a_restart_that_could_not_be_attempted_says_why(self):
        ok, detail = self._restart(exc=OSError("no such file"))
        self.assertFalse(ok)
        self.assertIn("no such file", detail)

    def test_windows_stops_before_it_starts(self):
        """There is no `sc restart`. Two calls, in that order, and the start

        does not happen if the stop failed: starting a service that is still
        stopping is refused by the SCM with an error naming neither.
        """
        order = []
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "stop_service",
                               side_effect=lambda u: (order.append("stop"),
                                                      (True, ""))[1]), \
             mock.patch.object(plat, "start_service",
                               side_effect=lambda u: (order.append("start"),
                                                      (True, ""))[1]):
            self.assertEqual(plat.restart_service(plat.CAPTURE_UNIT),
                             (True, ""))
        self.assertEqual(order, ["stop", "start"])

    def test_windows_does_not_start_what_it_could_not_stop(self):
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "stop_service",
                               return_value=(False, "denied")), \
             mock.patch.object(plat, "start_service") as start:
            self.assertEqual(plat.restart_service(plat.CAPTURE_UNIT),
                             (False, "denied"))
        start.assert_not_called()

    def test_a_scheduled_task_has_nothing_to_restart(self):
        """It is not running, so restarting it is not a thing that can be done.

        Saying so beats the alternatives: silently succeeding would tell the
        operator a change had taken effect that had not, and running the job
        now would turn a settings edit into a nightly encode at lunchtime.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True):
            ok, detail = plat.restart_service(plat.ENCODE_UNIT)
        self.assertFalse(ok)
        self.assertIn("scheduled task", detail)

    def test_it_prints_nothing_on_any_path(self):
        """A platform module a Windows service cannot call is not one.

        Under the SCM there is no console and sys.stdout may be a dead handle,
        so a stray print in the service path kills service_main and presents
        as "the approach does not work" (item 11c.2).
        """
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._restart(rc=0)
            self._restart(rc=1)
            self._restart(exc=OSError("boom"))
        self.assertEqual(buf.getvalue(), "")


class TestHints(unittest.TestCase):
    """What to tell an operator to type, for the cases where nothing ran.

    Both platforms are forced, on both CI legs. A hint is the one thing here
    that is read by a person rather than by a caller, so a hint naming the
    wrong platform's command is not a wrong value, it is wrong advice.
    """

    UNIT = plat.WEB_UNIT

    @contextlib.contextmanager
    def platform(self, windows):
        with mock.patch.object(plat, "IS_WINDOWS", windows):
            yield

    def test_the_three_linux_hints_name_the_unit(self):
        with self.platform(False):
            for hint in (plat.start_hint, plat.stop_hint, plat.restart_hint):
                self.assertIn(self.UNIT, hint(self.UNIT))

    def test_the_three_windows_hints_name_the_service(self):
        with self.platform(True):
            for hint in (plat.start_hint, plat.stop_hint, plat.restart_hint):
                self.assertIn("TimelapseWeb", hint(self.UNIT))
                self.assertNotIn(".service", hint(self.UNIT))

    def test_windows_drives_a_task_with_schtasks_not_sc(self):
        """sc knows nothing about a scheduled task. The commands do not

        overlap at all, and offering the wrong one gives an error about a
        service that does not exist, which reads as this tool having lost it.
        """
        with self.platform(True):
            self.assertIn("schtasks", plat.start_hint(plat.ENCODE_UNIT))
            self.assertIn("schtasks", plat.stop_hint(plat.ENCODE_UNIT))

    def test_the_log_hint_drops_the_unit_suffix(self):
        # journalctl -u takes the unit name without .service. Pasting the
        # suffix in works, but reads as though it were required.
        with self.platform(False):
            self.assertEqual(plat.log_hint(self.UNIT),
                             "journalctl -u timelapse-web -n 40")

    def test_the_log_hint_takes_a_line_count(self):
        with self.platform(False):
            self.assertIn("-n 5", plat.log_hint(self.UNIT, 5))

    def test_the_windows_log_hint_reads_the_file_the_daemon_writes(self):
        """Not journalctl, and not capture.log either: DailyFileHandler names

        the file after the day, so a hint naming a fixed filename would point
        at something that does not exist on any Windows install.
        """
        with self.platform(True):
            hint = plat.log_hint(plat.CAPTURE_UNIT, 10,
                                 log_dir=r"D:\timelapse\logs")
        self.assertIn(r"D:\timelapse\logs\capture-*.log", hint)
        self.assertIn("-Tail 10", hint)

    def test_the_watch_reads_the_encoders_log_because_it_is_the_encoder(self):
        with self.platform(True):
            self.assertIn("encode-*.log", plat.log_hint(plat.WATCH_UNIT))

    def test_the_windows_log_hint_falls_back_to_the_default_directory(self):
        with self.platform(True):
            self.assertIn("logs", plat.log_hint(plat.CAPTURE_UNIT))

    def test_the_elevation_hint_offers_no_command_on_windows(self):
        """There is nothing to type: privilege is a property of how the shell

        was launched. `runas` looks like the answer and is not, so the hint is
        a sentence rather than something to paste.
        """
        with self.platform(True):
            self.assertIn("administrator", plat.elevation_hint().lower())
        with self.platform(False):
            self.assertIn("sudo", plat.elevation_hint())


class TestComponentNames(unittest.TestCase):
    """One identifier, two spellings, and only this file knows the second."""

    def test_every_unit_has_a_windows_name(self):
        for unit in (plat.CAPTURE_UNIT, plat.WEB_UNIT, plat.ENCODE_UNIT,
                     plat.WATCH_UNIT):
            self.assertIn(unit, plat.WINDOWS_NAMES)
            self.assertIn(unit, plat.LOG_STEMS)

    def test_linux_calls_a_component_by_its_unit(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertEqual(plat.native_name(plat.CAPTURE_UNIT),
                             plat.CAPTURE_UNIT)

    def test_windows_calls_it_something_a_windows_admin_would_recognise(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.native_name(plat.CAPTURE_UNIT),
                             "TimelapseCapture")

    def test_the_display_name_is_not_the_description(self):
        """services.msc is sorted by display name, so it is the only string an

        administrator can find the service by. The first version passed the
        description as the display name, which filed the capture service under
        "Camera snapshot capture for timelapse": reported from a real install,
        where `sc query` said RUNNING and the Services window appeared not to
        list it at all. That is half of 11c.2's argument for using real
        services rather than scheduled tasks, lost to one misplaced argument.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.display_name(plat.CAPTURE_UNIT),
                             "Timelapse Capture")
            self.assertEqual(plat.display_name(plat.WEB_UNIT),
                             "Timelapse Web UI")

    def test_every_display_name_sorts_with_the_scheduled_tasks(self):
        """One block under T wherever any of this is listed, rather than the

        services under one letter and the tasks under another.
        """
        names = [plat.DISPLAY_NAMES[plat.CAPTURE_UNIT],
                 plat.DISPLAY_NAMES[plat.WEB_UNIT],
                 plat.WINDOWS_NAMES[plat.ENCODE_UNIT],
                 plat.WINDOWS_NAMES[plat.WATCH_UNIT]]
        for name in names:
            self.assertTrue(name.startswith("Timelapse"), name)

    def test_a_component_with_no_display_name_falls_back_to_its_own(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.display_name(plat.ENCODE_UNIT),
                             "Timelapse Encode")

    def test_an_unknown_identifier_passes_through_unchanged(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.native_name("something.service"),
                             "something.service")

    def test_the_batch_jobs_are_the_scheduled_ones(self):
        self.assertTrue(plat.is_scheduled(plat.ENCODE_UNIT))
        self.assertTrue(plat.is_scheduled(plat.WATCH_UNIT))
        self.assertFalse(plat.is_scheduled(plat.CAPTURE_UNIT))
        self.assertFalse(plat.is_scheduled(plat.WEB_UNIT))

    def test_the_watch_logs_where_the_encoder_does(self):
        # Because it *is* the encoder: timelapse_encode.py --watch.
        self.assertEqual(plat.LOG_STEMS[plat.WATCH_UNIT],
                         plat.LOG_STEMS[plat.ENCODE_UNIT])


class TestStructureLayout(unittest.TestCase):
    """The measurement that made these structures fixed-width.

    ctypes.wintypes.DWORD is c_ulong, which is four bytes on Windows and
    **eight** on 64-bit Linux. Every layout here would be silently wrong on the
    Linux CI legs, and every test asserting one would agree with it, which is
    worse than not testing them at all. c_uint32 is four bytes everywhere,
    which is what makes the numbers below assertable from either leg.
    """

    def test_service_status_is_seven_dwords(self):
        self.assertEqual(ctypes.sizeof(plat.SERVICE_STATUS), 28)

    def test_the_extended_status_adds_two_more(self):
        self.assertEqual(ctypes.sizeof(plat.SERVICE_STATUS_PROCESS), 36)

    def test_a_failure_action_is_a_type_and_a_delay(self):
        self.assertEqual(ctypes.sizeof(plat.SC_ACTION), 8)

    def test_the_dword_this_project_uses_is_four_bytes_on_both_platforms(self):
        self.assertEqual(ctypes.sizeof(plat._DWORD), 4)

    def test_a_netresource_is_four_dwords_and_four_pointers(self):
        # 16 bytes of counters, then pointers on their own alignment, so the
        # answer differs by word size and not by platform: 48 on a 64-bit
        # build of either, 32 on a 32-bit one.
        expected = 16 + 4 * ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(plat.NETRESOURCE), expected)

    def test_the_remote_name_is_where_the_share_goes(self):
        # Cheap, and it is the field a typo would silently move: setting
        # lpLocalName instead asks for a drive letter, which is the entire
        # mechanism this code exists to avoid.
        nr = plat.NETRESOURCE()
        nr.lpRemoteName = r"\\tower\cctv"
        self.assertEqual(nr.lpRemoteName, r"\\tower\cctv")
        self.assertIsNone(nr.lpLocalName)


class TestShareRoot(unittest.TestCase):
    """\\\\server\\share from a path inside it, on both platforms.

    Pure string work on purpose: WNetAddConnection2W wants the share and
    refuses a folder inside it, and every caller here holds the folder.
    """

    def test_a_folder_inside_a_share(self):
        self.assertEqual(plat.share_root(r"\\tower\cctv\TL\_temp_"),
                         r"\\tower\cctv")

    def test_the_share_itself(self):
        self.assertEqual(plat.share_root(r"\\tower\cctv"), r"\\tower\cctv")

    def test_forward_slashes_are_accepted(self):
        # Windows accepts them, so an operator who types them should not be
        # told their path is not a path.
        self.assertEqual(plat.share_root("//tower/cctv/TL"), r"\\tower\cctv")

    def test_a_bare_server_has_no_share_to_connect_to(self):
        # Measured, not assumed: the first implementation used
        # ntpath.splitdrive, which answers "\\\\tower" here and so calls a
        # server with no share on it a complete drive.
        self.assertIsNone(plat.share_root(r"\\tower"))
        self.assertIsNone(plat.share_root("\\\\tower\\"))

    def test_a_local_path_is_not_a_share(self):
        self.assertIsNone(plat.share_root(r"D:\timelapse\videos"))
        self.assertIsNone(plat.share_root("/mnt/nas/tl"))

    def test_nothing_at_all(self):
        self.assertIsNone(plat.share_root(""))
        self.assertIsNone(plat.share_root(None))

    def test_is_unc_agrees_with_it(self):
        for path in (r"\\tower\cctv\TL", "//tower/cctv", r"\\tower"):
            self.assertTrue(plat.is_unc(path), path)
        for path in (r"D:\out", "/mnt/nas", "", None, r"C:\\"):
            self.assertFalse(plat.is_unc(path), path)


class TestNetErrors(unittest.TestCase):
    """A network error in words, keeping the number.

    Both halves earn their place: the words say what to go and do, the number
    is what survives being pasted into a search engine.
    """

    def test_a_known_code_is_explained(self):
        text = plat.net_error(plat.ERROR_LOGON_FAILURE)
        self.assertIn("rejected", text)
        self.assertIn("1326", text)

    def test_an_unknown_code_still_reports_the_number(self):
        self.assertEqual(plat.net_error(4242), "error 4242")

    def test_the_three_that_need_different_actions_read_differently(self):
        # A wrong password, a share that is not there and a server that is not
        # answering are three separate jobs for whoever reads the log.
        wrong = plat.net_error(plat.ERROR_LOGON_FAILURE)
        absent = plat.net_error(plat.ERROR_BAD_NET_NAME)
        unreachable = plat.net_error(plat.ERROR_NO_NET_OR_BAD_PATH)
        self.assertEqual(len({wrong, absent, unreachable}), 3)

    def test_a_credential_conflict_explains_itself(self):
        # The one an operator will otherwise read as a permission problem.
        text = plat.net_error(plat.ERROR_SESSION_CREDENTIAL_CONFLICT)
        self.assertIn("different user", text)


class TestShareConnections(unittest.TestCase):
    """connect_share(), with the platform declared rather than inherited.

    Every test here runs on both CI legs, which is the point: patching
    plat.IS_WINDOWS and plat._win means the real marshalling code is exercised
    on Linux too, and a skip on one runner is a check that cannot fail there.
    Patched on plat, the module that owns both names, because a `from ...
    import IS_WINDOWS` elsewhere makes a second binding that does not move.
    """

    def windows(self, err=0):
        """Pretend to be Windows with an SCM that answers `err`."""
        api = mock.Mock()
        api.mpr.WNetAddConnection2W.return_value = err
        api.mpr.WNetCancelConnection2W.return_value = err
        return api

    def test_it_declines_off_windows(self):
        # None, never False: a share refusing us and a platform that has no
        # such mechanism need opposite responses from the caller.
        with mock.patch.object(plat, "IS_WINDOWS", False):
            ok, why = plat.connect_share(r"\\tower\cctv\TL", "u", "p")
            gone, _ = plat.disconnect_share(r"\\tower\cctv")
        self.assertIsNone(ok)
        self.assertIsNone(gone)
        self.assertIn("Windows", why)

    def test_a_local_path_is_not_a_share(self):
        # The other None, and only the detail separates the two.
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win",
                               side_effect=AssertionError("bound the API")):
            ok, why = plat.connect_share("D:\\out")
        self.assertIsNone(ok)
        self.assertIn("not a network path", why)

    def test_it_connects_to_the_share_not_the_folder(self):
        api = self.windows()
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win", return_value=api):
            ok, _ = plat.connect_share(r"\\tower\cctv\TL\_temp_", "war", "pw")
        self.assertTrue(ok)
        ref, password, user, flags = api.mpr.WNetAddConnection2W.call_args[0]
        self.assertEqual(ref._obj.lpRemoteName, r"\\tower\cctv")
        self.assertEqual((password, user), ("pw", "war"))
        # Nothing written to the operator's profile: a persistent connection
        # would be a change to their machine nobody asked this tool to make.
        self.assertEqual(flags, 0)

    def test_it_asks_for_no_drive_letter(self):
        """The single most important field, and it is the one left empty.

        A local name would request a drive letter, which belongs to one logon
        session, which is the entire failure this mechanism routes around.
        """
        api = self.windows()
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win", return_value=api):
            plat.connect_share(r"\\tower\cctv", "u", "p")
        nr = api.mpr.WNetAddConnection2W.call_args[0][0]._obj
        self.assertIsNone(nr.lpLocalName)
        self.assertEqual(nr.dwType, plat.RESOURCETYPE_DISK)

    def test_no_credentials_means_the_current_token(self):
        # Passing "" would offer an empty password rather than saying "use
        # whoever I already am", which are different requests.
        api = self.windows()
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win", return_value=api):
            plat.connect_share(r"\\tower\cctv", "", "")
        _ref, password, user, _flags = api.mpr.WNetAddConnection2W.call_args[0]
        self.assertIsNone(password)
        self.assertIsNone(user)

    def test_a_rejected_password_is_a_refusal(self):
        api = self.windows(plat.ERROR_LOGON_FAILURE)
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win", return_value=api):
            ok, why = plat.connect_share(r"\\tower\cctv", "war", "wrong")
        self.assertFalse(ok)
        self.assertIn("rejected", why)
        self.assertIn("1326", why)

    def test_a_credential_conflict_is_not_a_refusal(self):
        """Already connected to that server as somebody else.

        Their connection may be the one that works, and tearing down a
        connection this program did not make is not ours to do, so this has to
        come back as "not attempted" and let the caller settle it by writing.
        """
        api = self.windows(plat.ERROR_SESSION_CREDENTIAL_CONFLICT)
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win", return_value=api):
            ok, why = plat.connect_share(r"\\tower\cctv", "war", "pw")
        self.assertIsNone(ok)
        self.assertIn("different user", why)

    def test_disconnecting_names_the_share(self):
        api = self.windows()
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win", return_value=api):
            ok, _ = plat.disconnect_share(r"\\tower\cctv\TL\_temp_")
        self.assertTrue(ok)
        name, _flags, force = api.mpr.WNetCancelConnection2W.call_args[0]
        self.assertEqual(name, r"\\tower\cctv")
        # Never forced: something else on this machine may be using the share,
        # and dropping it under them is not this program's call.
        self.assertFalse(force)


class TestServiceBinpath(unittest.TestCase):
    """A Python installed per user lives under a path with a space in it."""

    def test_each_element_is_quoted_separately(self):
        self.assertEqual(
            plat.service_binpath([r"C:\Program Files\Python\python.exe",
                                  r"C:\Program Files\timelapse\x.py",
                                  "--service"]),
            '"C:\\Program Files\\Python\\python.exe" '
            '"C:\\Program Files\\timelapse\\x.py" --service')

    def test_a_path_with_no_space_is_left_alone(self):
        self.assertEqual(plat.service_binpath([r"C:\py\python.exe", "x.py"]),
                         r"C:\py\python.exe x.py")

    def test_an_empty_argument_survives_as_an_empty_argument(self):
        self.assertEqual(plat.service_binpath(["a", ""]), 'a ""')


class TestTaskDefinitions(unittest.TestCase):
    """The scheduled half. Every element here has a line in service/*.timer.

    Well-formedness is asserted by parsing rather than by matching text: a
    definition Task Scheduler will not read is a job that never runs, and the
    error it gives back is about a value being out of range, which reads as a
    bug in the schedule rather than in the XML.
    """

    def parse(self, xml):
        return ElementTree.fromstring(xml.split("?>", 1)[1])

    def text(self, xml, *names):
        node = self.parse(xml)
        for name in names:
            node = node.find("{%s}%s" % (plat.TASK_NS, name))
            if node is None:
                return None
        return node.text

    def test_a_generated_definition_is_well_formed_xml(self):
        xml = plat.task_xml("Nightly encode", ["py.exe", "e.py", "cfg"],
                            plat.daily_trigger(0, 5))
        self.assertIsNotNone(self.parse(xml))

    def test_the_command_and_its_arguments_are_separate_elements(self):
        """Task Scheduler splits them; a single string in Command is treated

        as one executable name, spaces and all, and fails to start with a file
        not found naming the whole command line.
        """
        xml = plat.task_xml("x", [r"C:\py\python.exe",
                                  r"C:\Program Files\t\e.py", "cfg.json"],
                            plat.daily_trigger(0, 5))
        self.assertEqual(self.text(xml, "Actions", "Exec", "Command"),
                         r"C:\py\python.exe")
        self.assertEqual(self.text(xml, "Actions", "Exec", "Arguments"),
                         r'"C:\Program Files\t\e.py" cfg.json')

    def test_it_runs_as_the_system_sid_rather_than_by_name(self):
        # "SYSTEM" is localised; S-1-5-18 is not.
        xml = plat.task_xml("x", ["a"], plat.daily_trigger(0, 5))
        self.assertEqual(self.text(xml, "Principals", "Principal", "UserId"),
                         "S-1-5-18")

    def test_a_named_account_replaces_it(self):
        xml = plat.task_xml("x", ["a"], plat.daily_trigger(0, 5),
                            user_id="TOWER\\svc")
        self.assertEqual(self.text(xml, "Principals", "Principal", "UserId"),
                         "TOWER\\svc")

    def test_the_nightly_trigger_is_the_calendar_one(self):
        xml = plat.task_xml("x", ["a"], plat.daily_trigger(0, 5, 5))
        trigger = self.parse(xml).find("{%s}Triggers/{%s}CalendarTrigger"
                                       % (plat.TASK_NS, plat.TASK_NS))
        self.assertIsNotNone(trigger)
        self.assertIn("T00:05:00", plat.daily_trigger(0, 5))

    def test_the_jitter_maps_onto_randomizeddelaysec(self):
        self.assertIn("<RandomDelay>PT5M</RandomDelay>",
                      plat.daily_trigger(0, 5, jitter_minutes=5))
        self.assertNotIn("RandomDelay", plat.daily_trigger(0, 5))

    def test_the_repeating_trigger_names_no_duration(self):
        """Which is how the schema spells "for ever". Naming a Duration would

        give the credential watch a stop date some weeks out that nobody would
        notice passing, and the symptom would be a check that simply stopped.
        """
        trigger = plat.repeating_trigger(5)
        self.assertIn("<Interval>PT5M</Interval>", trigger)
        self.assertNotIn("Duration>", trigger.replace("StopAtDurationEnd", ""))

    def test_catch_up_is_the_timers_persistent_true(self):
        on = plat.task_xml("x", ["a"], plat.daily_trigger(0, 5), catch_up=True)
        off = plat.task_xml("x", ["a"], plat.repeating_trigger(5),
                            catch_up=False)
        self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", on)
        self.assertIn("<StartWhenAvailable>false</StartWhenAvailable>", off)

    def test_the_default_time_limit_is_none_at_all(self):
        """TimeoutStartSec=infinity, because a full backlog catch-up

        legitimately takes hours and must not be killed part way through an
        encode. Task Scheduler's own default is 72 hours, which would.
        """
        xml = plat.task_xml("x", ["a"], plat.daily_trigger(0, 5))
        self.assertIn("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", xml)

    def test_a_description_with_an_ampersand_does_not_break_the_document(self):
        xml = plat.task_xml("Encode & notify", ["a"], plat.daily_trigger(0, 5))
        self.assertIsNotNone(self.parse(xml))
        self.assertIn("&amp;", xml)

    def test_it_declares_utf16_because_schtasks_rejects_utf8(self):
        # On some builds, and the error names a value rather than an encoding.
        self.assertIn('encoding="UTF-16"',
                      plat.task_xml("x", ["a"], plat.daily_trigger(0, 5)))


class TestTaskResult(unittest.TestCase):
    """LastTaskResult in words, because the number invents a fault.

    Found in the log of the first passing Windows install: two freshly
    registered tasks reported "last result 267011", which is 0x00041303,
    SCHED_S_TASK_HAS_NOT_RUN, and is the correct and healthy state for a task
    that has never fired. Printed as a five-figure number it reads as an error
    code, on a system with nothing wrong with it. Same defect as calling the
    nightly oneshot "Stopped" for the 23 hours a day it is not running.
    """

    def test_a_task_that_has_never_run_says_so(self):
        self.assertEqual(plat.task_result(267011), "has not run yet")

    def test_a_successful_run_says_so(self):
        self.assertEqual(plat.task_result(0), "last run succeeded")

    def test_the_healthy_states_never_render_as_a_number(self):
        for code in plat.SCHED_RESULTS:
            self.assertNotIn("0x", plat.task_result(code), hex(code))

    def test_a_real_failure_is_not_softened(self):
        """The other half of the same rule. A task whose program exited 1 has

        failed, and dressing that up would be the opposite error: reporting a
        fault as health.
        """
        self.assertIn("failed", plat.task_result(1))
        self.assertIn("0x00000001", plat.task_result(1))

    def test_a_signed_hresult_is_recognised_anyway(self):
        # PowerShell hands back anything above 0x7FFFFFFF as a negative int,
        # so the same state arrives with two different spellings.
        self.assertEqual(plat.task_result(267011 - (1 << 32)),
                         "has not run yet")

    def test_an_unknown_failure_keeps_its_code(self):
        self.assertIn("0x80070005", plat.task_result(0x80070005))

    def test_nothing_to_report_is_unknown_rather_than_a_guess(self):
        for given in (None, "", "not a number"):
            self.assertEqual(plat.task_result(given), "unknown", repr(given))


class TestWindowsCallsDeclineElsewhere(unittest.TestCase):
    """Every Windows-only call answers on Linux rather than raising.

    These run on the Linux CI legs and are the only assertion there that the
    guards exist at all: without them a wizard on Linux touching any of this
    would traceback out of an OSError from a DLL that is not there.
    """

    CALLS = {
        "install_service": (plat.CAPTURE_UNIT, "x", ["py.exe"]),
        "remove_service": (plat.CAPTURE_UNIT,),
        "start_service": (plat.CAPTURE_UNIT,),
        "stop_service": (plat.CAPTURE_UNIT,),
        "install_task": (plat.ENCODE_UNIT, "<Task/>"),
        "remove_task": (plat.ENCODE_UNIT,),
    }

    def test_they_decline_with_a_reason(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            for name, args in self.CALLS.items():
                ok, detail = getattr(plat, name)(*args)
                self.assertFalse(ok, name)
                self.assertIn("Windows", detail, name)

    def test_the_queries_answer_unasked_rather_than_no(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertIsNone(plat.service_state(plat.CAPTURE_UNIT))
            self.assertIsNone(plat.task_exists(plat.ENCODE_UNIT))
            self.assertIsNone(plat.task_info(plat.ENCODE_UNIT))

    def test_hosting_a_service_elsewhere_fails_rather_than_hangs(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertEqual(
                plat.run_as_service(plat.CAPTURE_UNIT, lambda ready: None,
                                    lambda: None), 1)

    def test_is_elevated_asks_the_kernel_rather_than_assuming_zero(self):
        """The 0.1.4 bug in one line: getattr(os, "geteuid", lambda: 0)()

        answers 0 on a platform with no such call, so a Windows box looks like
        root to every check that asks, which is why that test passed here and
        failed on all three CI legs.
        """
        with mock.patch.object(plat, "IS_WINDOWS", False), \
             mock.patch.object(plat.os, "geteuid", create=True,
                               return_value=1000):
            self.assertFalse(plat.is_elevated())
        with mock.patch.object(plat, "IS_WINDOWS", False), \
             mock.patch.object(plat.os, "geteuid", create=True,
                               return_value=0):
            self.assertTrue(plat.is_elevated())


class TestSecureSecretFile(unittest.TestCase):
    """The file holds camera passwords, so what this does is a claim."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "config.json"
        self.path.write_text("{}", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_linux_sets_the_mode_and_the_group(self):
        # create=True: shutil.chown does not exist on Windows, and this branch
        # is asserted there too.
        with mock.patch.object(plat, "IS_WINDOWS", False), \
             mock.patch.object(plat.os, "chmod") as chmod, \
             mock.patch.object(plat.shutil, "chown", create=True) as chown:
            plat.secure_secret_file(self.path, "timelapse")
        chmod.assert_called_once_with(self.path, 0o640)
        chown.assert_called_once_with(self.path, group="timelapse")

    def test_no_group_means_no_chown(self):
        with mock.patch.object(plat, "IS_WINDOWS", False), \
             mock.patch.object(plat.os, "chmod"), \
             mock.patch.object(plat.shutil, "chown", create=True) as chown:
            plat.secure_secret_file(self.path)
        chown.assert_not_called()

    def test_it_never_raises_over_a_file_already_written(self):
        # Refusing to have written a config because its group could not be set
        # would be a worse outcome than one the pre-flight complains about.
        with mock.patch.object(plat, "IS_WINDOWS", False), \
             mock.patch.object(plat.os, "chmod", side_effect=OSError(1, "no")), \
             mock.patch.object(plat.shutil, "chown", create=True,
                               side_effect=LookupError("no such group")):
            plat.secure_secret_file(self.path, "timelapse")

    def test_windows_does_not_chmod_and_that_is_the_point(self):
        """chmod on Windows sets one bit, read-only, and 0640 clears it.

        Calling it there would report success for a file every account on the
        box can still read, which is a security claim that is false. Doing
        nothing until the installer can set a real ACL is the honest answer.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat.os, "chmod") as chmod, \
             mock.patch.object(plat.shutil, "chown", create=True) as chown:
            plat.secure_secret_file(self.path, "timelapse")
        chmod.assert_not_called()
        chown.assert_not_called()


class TestTakeLock(unittest.TestCase):
    """One encode run at a time, on both platforms, with one implementation.

    An OS lock and not a pid file, and the case that decides it is the encode
    that was *killed*: a pid file would outlive it and refuse every run
    afterwards until somebody found and deleted it. Measured on both platforms
    in temp/c4_lock_probe.py, which is where the cross-process half lives;
    these are the answers and the edges.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "state" / ".encode.lock"
        self.held = []
        self.addCleanup(lambda: [plat.release_lock(h) for h in self.held])

    def take(self, path=None):
        status, handle, detail = plat.take_lock(path or self.path)
        if handle is not None:
            self.held.append(handle)
        return status, handle, detail

    def test_a_free_lock_is_taken(self):
        status, handle, detail = self.take()
        self.assertEqual(status, "taken")
        self.assertIsNotNone(handle)
        self.assertEqual(detail, "")

    def test_it_creates_the_directory_it_needs(self):
        # state_dir exists on every real install, because ReadWritePaths names
        # it and a unit whose ReadWritePaths points at nothing does not start.
        # A manual run as root on a box that never ran the wizard is the case.
        self.assertFalse(self.path.parent.exists())
        self.take()
        self.assertTrue(self.path.parent.exists())

    def test_a_second_take_in_the_same_process_is_held(self):
        self.take()
        status, handle, detail = self.take()
        self.assertEqual(status, "held")
        self.assertIsNone(handle)
        self.assertIn(str(os.getpid()), detail)

    def test_the_holder_is_named_as_a_pid(self):
        self.take()
        self.assertEqual(plat.lock_holder(self.path), "pid %d" % os.getpid())

    def test_releasing_lets_the_next_caller_have_it(self):
        _status, handle, _detail = self.take()
        self.held.remove(handle)
        plat.release_lock(handle)
        self.assertEqual(self.take()[0], "taken")

    def test_releasing_twice_is_harmless(self):
        _status, handle, _detail = self.take()
        self.held.remove(handle)
        plat.release_lock(handle)
        plat.release_lock(handle)

    def test_releasing_nothing_is_harmless(self):
        plat.release_lock(None)

    def test_a_leftover_file_naming_a_dead_pid_is_not_a_lock(self):
        """The whole argument for an OS lock over a pid file.

        A killed encode leaves this file behind with its pid in it, and the
        next run has to be able to take it. Measured for real against a killed
        child in temp/c4_lock_probe.py; here it is the file alone.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"\x00999999\n")
        self.assertEqual(self.take()[0], "taken")

    def test_an_unusable_path_is_unavailable_and_not_held(self):
        """Three answers, not two, and this is the one that must not stop a

        run. A lock file that cannot be created is an environment problem;
        refusing to encode over it would be the check causing the outage it
        exists to prevent. "Held" must mean a real other process and nothing
        else.
        """
        with mock.patch.object(plat.os, "open",
                               side_effect=OSError(13, "read-only")):
            status, handle, detail = plat.take_lock(self.path)
        self.assertEqual(status, "unavailable")
        self.assertIsNone(handle)
        self.assertIn("read-only", detail)

    def test_the_holder_reader_never_raises(self):
        # It decorates a refusal; a failure here is worth strictly less than
        # the refusal itself, and on Windows the locked byte cannot be read at
        # all, which is why it reads from past it.
        self.assertEqual(plat.lock_holder(self.tmp / "nothing" / "here.lock"),
                         "another process")

    def test_the_pid_is_written_past_the_locked_byte(self):
        """Not tidiness. msvcrt's lock is mandatory, so a contender reading

        from byte 0 is refused outright and the pid would never reach the
        message it exists for. Byte 0 is the lock byte and is never written.

        Written this way because the first version read the whole file and
        raised PermissionError on Windows, which is the behaviour being
        described happening to the test describing it.
        """
        self.take()
        self.assertEqual(plat.LOCK_BYTE, 1)
        with open(str(self.path), "rb") as fh:
            fh.seek(plat.LOCK_BYTE)
            self.assertEqual(fh.read(32).strip(),
                             str(os.getpid()).encode("ascii"))

    def test_a_failure_to_write_the_pid_still_leaves_the_lock_held(self):
        # The courtesy of naming ourselves is worth less than the lock.
        with mock.patch.object(plat.os, "write",
                               side_effect=OSError(28, "no space")):
            status, handle, _detail = plat.take_lock(self.path)
        if handle is not None:
            self.held.append(handle)
        self.assertEqual(status, "taken")


class TestAllowUsersRead(unittest.TestCase):
    """The other direction: opening one directory back up, on purpose.

    The installer closes the whole data tree so that config.json beside it is
    not readable by every account, and by default the finished videos are
    inside it, so a timelapse the operator cannot double-click is the cost.
    This grants Users read and execute on that one directory.

    What the ACL then does was measured unelevated rather than reasoned about
    (temp/c3b_users_grant_probe.ps1 and temp/c3b_check.py, 21 checks): a video
    written after the grant opens, the folder lists, the data root above it
    does not, config.json and the logs and the frames stay closed, and an
    ordinary account can neither add nor delete a file there.
    """

    def run_it(self, windows=True, rc=0, out=b"", raises=None):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            if raises:
                raise raises
            return mock.Mock(returncode=rc, stdout=out)

        with mock.patch.object(plat, "IS_WINDOWS", windows), \
             mock.patch.object(plat.subprocess, "run", fake_run):
            result = plat.allow_users_read("C:\\ProgramData\\timelapse\\videos")
        return result, calls

    def test_linux_answers_none_rather_than_false(self):
        """Not the same answer as a failure.

        There is nothing to undo: the restriction this reverses is an ACL only
        the Windows installer writes. A caller reading False would report a
        failure on a platform where none happened, which is the falsy-return
        shape this project has paid for five times.
        """
        (ok, detail), calls = self.run_it(windows=False)
        self.assertIsNone(ok)
        self.assertTrue(detail)
        self.assertEqual(calls, [])

    def test_it_grants_the_users_sid_read_and_execute(self):
        (ok, _detail), calls = self.run_it()
        self.assertIs(ok, True)
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[0], "icacls")
        self.assertIn("/grant", argv)
        self.assertIn(plat.SID_USERS + ":(OI)(CI)RX", argv)

    def test_it_never_asks_for_write(self):
        """A surveillance archive any account can quietly alter is worth less

        than one it cannot open. RX, and the harness confirms an ordinary
        account can neither create nor delete there.
        """
        argv = self.run_it()[1][0]
        joined = " ".join(argv)
        for permission in (":(OI)(CI)F", ":(OI)(CI)M", ":(OI)(CI)W"):
            self.assertNotIn(permission, joined)

    def test_the_sids_are_numbers_and_not_names(self):
        """icacls resolves a name through the local SAM, and the group is

        Administratoren on a German Windows and Administrateurs on a French
        one. A name would fail on exactly the installs least able to debug it.
        """
        for sid in (plat.SID_SYSTEM, plat.SID_ADMINISTRATORS, plat.SID_USERS):
            self.assertTrue(sid.startswith("*S-1-"), sid)

    def test_a_refusal_comes_back_with_what_icacls_said(self):
        (ok, detail), _calls = self.run_it(rc=1, out=b"videos: Access is denied.")
        self.assertIs(ok, False)
        self.assertIn("Access is denied", detail)

    def test_icacls_missing_is_reported_rather_than_raised(self):
        # This runs after the videos already exist and are correct. Raising
        # here would turn "your videos need administrator rights to open" into
        # a failed install.
        (ok, detail), _calls = self.run_it(raises=OSError("no icacls"))
        self.assertIs(ok, False)
        self.assertIn("icacls", detail)


class TestReservedNames(unittest.TestCase):
    """Refused on both platforms, which is the deliberate part."""

    def test_the_documented_four_plus_the_numbered_ports(self):
        for name in ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1",
                     "LPT9"):
            self.assertTrue(plat.is_reserved_name(name), name)

    def test_it_is_case_insensitive_because_the_filesystem_is(self):
        for name in ("nul", "Nul", "nUl"):
            self.assertTrue(plat.is_reserved_name(name), name)

    def test_surrounding_space_does_not_smuggle_one_through(self):
        self.assertTrue(plat.is_reserved_name("  NUL "))

    def test_ordinary_names_are_left_alone(self):
        for name in ("Driveway", "Court180", "Workshop", "CONSERVATORY",
                     "COM", "LPT", "COM10", "NULL", "Camera1"):
            self.assertFalse(plat.is_reserved_name(name), name)

    def test_the_rule_does_not_depend_on_the_platform(self):
        """A config.json is portable, so a name only one platform accepts is a
        trap set for whoever moves the file."""
        for windows in (False, True):
            with mock.patch.object(plat, "IS_WINDOWS", windows):
                self.assertTrue(plat.is_reserved_name("NUL"))
                self.assertFalse(plat.is_reserved_name("Driveway"))


class TestSameFileName(unittest.TestCase):
    """os.path.normcase, so the filesystem answers rather than a branch."""

    def test_identical_names_always_collide(self):
        # The case both CI legs exercise, and the one a hand-edited config on
        # Linux can actually produce.
        self.assertTrue(plat.same_file_name("Workshop", "Workshop"))

    def test_different_names_never_collide(self):
        self.assertFalse(plat.same_file_name("Workshop", "Garage"))

    def test_case_variants_follow_the_platform(self):
        # True on Windows, False on Linux, and both are correct: on Linux they
        # genuinely are two directories. Asserting the platform's own answer
        # rather than a fixed one is the point of using normcase at all.
        expected = os.path.normcase("Workshop") == os.path.normcase("workshop")
        self.assertIs(plat.same_file_name("Workshop", "workshop"), expected)


class TestLogHandler(unittest.TestCase):
    """RotatingFileHandler renames; Windows will not rename an open file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        # Not logging.shutdown(): it closes every handler in the *process*,
        # including ones other test modules are still using, and this class
        # only ever opens its own. close() on what it opened is the whole
        # obligation.
        shutil.rmtree(self.tmp, ignore_errors=True)

    def close(self, handler):
        handler.close()
        return handler

    def test_linux_keeps_the_handler_it_always_had(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            handler = self.close(plat.log_handler(self.tmp, "capture",
                                                  backups=3))
        self.assertIsInstance(handler, logging.handlers.RotatingFileHandler)
        self.assertEqual(Path(handler.baseFilename).name, "capture.log")
        self.assertEqual(handler.backupCount, 3)

    def test_windows_gets_a_handler_that_never_renames(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            handler = self.close(plat.log_handler(self.tmp, "capture",
                                                  backups=3))
        self.assertIsInstance(handler, plat.DailyFileHandler)
        self.assertRegex(Path(handler.baseFilename).name,
                         r"^capture-\d{8}\.log$")

    def test_the_history_setting_means_the_same_thing_on_both(self):
        # backups=3 is "three lots of history beside the current one" either
        # way: three rotated files, or three days beside today.
        with mock.patch.object(plat, "IS_WINDOWS", True):
            handler = self.close(plat.log_handler(self.tmp, "capture",
                                                  backups=3))
        self.assertEqual(handler.keep_days, 4)

    def test_it_creates_the_log_directory(self):
        target = self.tmp / "a" / "b"
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.close(plat.log_handler(target, "capture"))
        self.assertTrue(target.is_dir())


class FixedClock(object):
    """`datetime` with now() pinned. Everything else passes through.

    The product calls datetime.now() on the name it imported, so that name is
    what has to change; subclassing datetime would leave the call untouched.
    """

    def __init__(self, when):
        self._when = when

    def now(self):
        return self._when

    def __getattr__(self, name):
        return getattr(datetime, name)


class TestDailyFileHandler(unittest.TestCase):
    """The Windows handler, driven on whichever platform is running."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = logging.getLogger("daily-probe")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False

    def tearDown(self):
        for handler in list(self.log.handlers):
            handler.close()
            self.log.removeHandler(handler)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def attach(self, keep_days=3):
        handler = plat.DailyFileHandler(self.tmp, "capture", keep_days)
        self.log.addHandler(handler)
        return handler

    def files(self):
        return sorted(p.name for p in self.tmp.glob("capture-*.log"))

    def test_it_writes_todays_file(self):
        # The handler stamps the name from the clock when the record arrives,
        # and this reads it again afterwards: one run in a hundred thousand
        # starts on one day and asserts on the next.
        fixed = datetime(2026, 8, 11, 12, 0, 0)
        with mock.patch.object(plat, "datetime",
                               FixedClock(fixed)):
            self.attach()
            self.log.info("hello")
        today = fixed.strftime("%Y%m%d")
        self.assertEqual(self.files(), [f"capture-{today}.log"])
        self.assertIn("hello", (self.tmp / f"capture-{today}.log")
                      .read_text(encoding="utf-8"))

    def test_crossing_midnight_opens_a_new_file_without_renaming(self):
        """The whole point. Nothing is renamed, so nothing can fail to rename.

        The day is forced rather than waited for; what is being asserted is
        that yesterday's file is still there, under its own name, with its own
        content, which is what a rename would have destroyed.
        """
        handler = self.attach()
        self.log.info("yesterday")
        first = Path(handler.baseFilename)

        handler.day = "20000101"          # pretend the process started then
        self.log.info("today")

        today = datetime.now().strftime("%Y%m%d")
        self.assertEqual(Path(handler.baseFilename).name,
                         f"capture-{today}.log")
        self.assertEqual(first.name, f"capture-{today}.log")
        body = first.read_text(encoding="utf-8")
        self.assertIn("yesterday", body)
        self.assertIn("today", body)

    def test_a_reader_holding_the_file_open_does_not_break_it(self):
        """The measured failure, reproduced against the replacement.

        RotatingFileHandler raises PermissionError WinError 32 here and logging
        swallows it, so the daemon prints a traceback per record and the file
        never rotates. This handler renames nothing, so there is nothing to
        refuse.
        """
        handler = self.attach()
        self.log.info("first")
        reader = open(handler.baseFilename, "r", encoding="utf-8")
        try:
            handler.day = "20000101"      # force the day change while it is open
            self.log.info("second")
        finally:
            reader.close()
        body = Path(handler.baseFilename).read_text(encoding="utf-8")
        self.assertIn("second", body)

    def test_old_files_are_pruned_by_age(self):
        old = datetime.now() - timedelta(days=10)
        stale = self.tmp / f"capture-{old.strftime('%Y%m%d')}.log"
        stale.write_text("ancient", encoding="utf-8")
        self.attach(keep_days=3)
        self.assertFalse(stale.exists())

    def test_recent_files_are_kept(self):
        recent = datetime.now() - timedelta(days=1)
        keep = self.tmp / f"capture-{recent.strftime('%Y%m%d')}.log"
        keep.write_text("recent", encoding="utf-8")
        self.attach(keep_days=3)
        self.assertTrue(keep.exists())

    def test_a_stranger_in_the_directory_is_left_alone(self):
        """Pruning parses a date out of a filename, and anything that is not

        one of ours will not parse. Deleting it because it sat in the log
        directory would be this program throwing away somebody else's file.
        """
        stranger = self.tmp / "capture-notadate.log"
        stranger.write_text("theirs", encoding="utf-8")
        self.attach(keep_days=1)
        self.assertTrue(stranger.exists())

    def test_a_failed_roll_keeps_logging_rather_than_losing_the_record(self):
        # A log call must never be the thing that stops the recording.
        handler = self.attach()
        self.log.info("before")
        handler.day = "20000101"
        with mock.patch.object(handler, "_open", side_effect=OSError("denied")):
            self.log.info("during")
        body = Path(handler.baseFilename).read_text(encoding="utf-8")
        self.assertIn("during", body)


class TestColour(unittest.TestCase):
    """Whether to emit escapes at all, which is not just "is this a tty".

    Reported from the first real Windows install: the wizard printed
    `<-[32mOK<-[0m    Registered TimelapseCapture.` A Windows console
    understands escapes only once a mode bit is set, and conhost leaves it off,
    so `isatty()` being true is necessary and not sufficient.

    Invisible in two places at once, which is why it reached an operator:
    Windows Terminal sets the bit itself, and no test or CI runner is a
    terminal, so `_COLOR` is false everywhere it could have been caught.
    """

    class FakeStream:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    def test_not_a_terminal_means_no_colour(self):
        """The half that was wrong on every platform: timelapse test wrote

        escape codes into a redirected file, because it never asked.
        """
        with mock.patch.dict(plat.os.environ, {}, clear=False):
            plat.os.environ.pop("NO_COLOR", None)
            self.assertFalse(plat.use_colour(self.FakeStream(tty=False)))

    def test_no_color_is_honoured_before_anything_else_is_asked(self):
        with mock.patch.dict(plat.os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(plat.use_colour(self.FakeStream(tty=True)))

    def test_a_terminal_that_understands_escapes_gets_them(self):
        with mock.patch.dict(plat.os.environ, {}, clear=False), \
             mock.patch.object(plat, "enable_ansi", return_value=True):
            plat.os.environ.pop("NO_COLOR", None)
            self.assertTrue(plat.use_colour(self.FakeStream(tty=True)))

    def test_a_terminal_that_does_not_gets_none(self):
        with mock.patch.dict(plat.os.environ, {}, clear=False), \
             mock.patch.object(plat, "enable_ansi", return_value=False):
            plat.os.environ.pop("NO_COLOR", None)
            self.assertFalse(plat.use_colour(self.FakeStream(tty=True)))

    def test_a_stream_with_no_isatty_is_not_a_terminal(self):
        self.assertFalse(plat.use_colour(object()))

    def test_enabling_is_a_no_op_off_windows(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertTrue(plat.enable_ansi())

    def test_it_sets_the_bit_rather_than_only_asking(self):
        """A query would answer "no" for ever: nothing else in the process is

        going to turn it on.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win") as api:
            kernel = api.return_value.kernel32
            kernel.GetStdHandle.return_value = 7

            def get_mode(_handle, mode):
                mode._obj.value = 0x0001
                return 1

            kernel.GetConsoleMode.side_effect = get_mode
            kernel.SetConsoleMode.return_value = 1
            self.assertTrue(plat.enable_ansi())
        wanted = kernel.SetConsoleMode.call_args[0][1]
        self.assertTrue(wanted & plat.ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        self.assertTrue(wanted & 0x0001, "it clobbered the existing mode")

    def test_a_console_that_refuses_is_false_rather_than_fatal(self):
        # An old conhost on Server 2016 does exactly this.
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win") as api:
            api.return_value.kernel32.GetStdHandle.return_value = 7
            api.return_value.kernel32.GetConsoleMode.return_value = 0
            self.assertFalse(plat.enable_ansi())


class TestScanDrives(unittest.TestCase):
    """The Windows half of the storage scan, asserted from either leg.

    Every input is injected, for the same reason the Linux half's are: a CD
    drive, an empty card reader, a mapped share and a disk that refuses to
    report its size are exactly the cases worth testing and exactly the ones no
    CI runner will happen to have.
    """

    def scan(self, drives, sizes=None, kinds=None, names=None):
        mask = 0
        for letter in drives:
            mask |= 1 << (ord(letter) - ord("A"))
        sizes = sizes or {}
        kinds = kinds or {}
        names = names or {}

        def usage(root):
            if root not in sizes:
                raise OSError("not ready")
            total, free = sizes[root]
            return (total, total - free, free)

        return plat.scan_drives(
            mask=mask,
            kind=lambda root: kinds.get(root, plat.DRIVE_FIXED),
            usage=usage,
            volume=lambda root: names.get(root, "NTFS"))

    def test_a_plain_fixed_disk_is_offered(self):
        disks = self.scan("C", {"C:\\": (100, 40)})
        self.assertEqual([d["mount"] for d in disks], ["C:\\"])
        self.assertEqual(disks[0]["free"], 40)
        self.assertEqual(disks[0]["total"], 100)
        self.assertEqual(disks[0]["source"], "C:")
        self.assertEqual(disks[0]["fstype"], "NTFS")

    def test_removable_network_and_optical_drives_are_not(self):
        """The same exclusions the Linux half makes, for the same reasons: 17k

        small writes per camera per day over the wire is painful, and a disk
        whose media can be ejected is worse.
        """
        sizes = {r"%s:\\" % c: (100, 50) for c in "CDEF"}
        sizes = {"C:\\": (100, 50), "D:\\": (100, 50), "E:\\": (100, 50),
                 "F:\\": (100, 50)}
        kinds = {"D:\\": plat.DRIVE_REMOVABLE, "E:\\": plat.DRIVE_REMOTE,
                 "F:\\": plat.DRIVE_CDROM}
        disks = self.scan("CDEF", sizes, kinds)
        self.assertEqual([d["mount"] for d in disks], ["C:\\"])

    def test_a_drive_with_no_media_is_skipped_rather_than_fatal(self):
        # An empty card reader reports DRIVE_REMOVABLE, but a letter that is
        # merely not ready raises from disk_usage, and one bad drive must not
        # cost the operator the listing.
        disks = self.scan("CD", {"C:\\": (100, 40)})
        self.assertEqual([d["mount"] for d in disks], ["C:\\"])

    def test_a_zero_sized_drive_is_skipped(self):
        self.assertEqual(self.scan("C", {"C:\\": (0, 0)}), [])

    def test_sorted_by_free_space_descending(self):
        sizes = {"C:\\": (500, 10), "D:\\": (500, 400), "E:\\": (500, 100)}
        disks = self.scan("CDE", sizes)
        self.assertEqual([d["mount"] for d in disks], ["D:\\", "E:\\", "C:\\"])

    def test_a_drive_that_will_not_name_its_filesystem_is_still_offered(self):
        disks = self.scan("C", {"C:\\": (100, 40)}, names={"C:\\": ""})
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["fstype"], "")

    def test_rotational_is_none_rather_than_guessed(self):
        """Dropped deliberately (item 11c.8): there is no cheap equivalent of

        /sys/block/*/queue/rotational, and "HDD" is a note beside a number, so
        a wrong note is worth less than a blank one.
        """
        disks = self.scan("C", {"C:\\": (100, 40)})
        self.assertIsNone(disks[0]["rotational"])

    def test_the_letters_come_from_the_bitmask(self):
        self.assertEqual(plat.drive_letters(0b101), ["A", "C"])
        self.assertEqual(plat.drive_letters(1 << 25), ["Z"])
        self.assertEqual(plat.drive_letters(0), [])

    def test_both_halves_return_the_same_shape(self):
        """The wizard reads one dict from either platform, so a key missing on

        one side is a KeyError that only happens there.
        """
        windows = self.scan("C", {"C:\\": (100, 40)})[0]
        self.assertEqual(set(windows),
                         {"mount", "source", "fstype", "free", "total",
                          "rotational"})


class TestOsDiskMount(unittest.TestCase):
    """Which disk not to fill."""

    def test_linux_is_root(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertEqual(plat.os_disk_mount(), "/")

    def test_windows_reads_the_environment(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.os_disk_mount({"SystemDrive": "D:"}), "D:\\")

    def test_it_tolerates_the_shapes_an_environment_variable_can_take(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            for given in ("C:", "c:", "C:\\", " C: ", "C"):
                self.assertEqual(plat.os_disk_mount({"SystemDrive": given}),
                                 "C:\\", given)

    def test_it_falls_back_when_nothing_says(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.os_disk_mount({}), "C:\\")


class TestNetworkPath(unittest.TestCase):
    """A mapped drive letter does not exist for a service.

    Item 11d calls this the single most likely way a Windows install fails, and
    the reason it is nasty is that it fails on a path the operator can open in
    Explorer: mappings belong to a logon session, and a service gets its own.
    """

    def rewrite(self, path, mapping):
        return plat.network_path(path, lookup=mapping.get)

    def test_a_mapped_drive_becomes_its_unc_target(self):
        self.assertEqual(self.rewrite(r"U:\TL", {"U:": r"\\tower\cctv"}),
                         r"\\tower\cctv\TL")

    def test_a_trailing_separator_on_the_target_is_not_doubled(self):
        self.assertEqual(self.rewrite(r"U:\TL", {"U:": "\\\\tower\\cctv\\"}),
                         r"\\tower\cctv\TL")

    def test_the_drive_root_alone_works(self):
        self.assertEqual(self.rewrite("U:", {"U:": r"\\tower\cctv"}),
                         r"\\tower\cctv")

    def test_a_local_drive_is_left_alone(self):
        self.assertIsNone(self.rewrite(r"C:\videos", {}))

    def test_a_unc_path_is_already_right(self):
        self.assertIsNone(self.rewrite(r"\\tower\cctv\TL", {}))

    def test_a_posix_path_is_not_mistaken_for_a_drive(self):
        self.assertIsNone(self.rewrite("/mnt/nas/timelapse", {}))

    def test_it_returns_none_rather_than_the_input_when_unchanged(self):
        """Deliberate: the caller has to tell the operator that a substitution

        happened, and a function that hands back either the same path or a
        different one makes that impossible to say.
        """
        self.assertIsNone(self.rewrite(r"C:\x", {}))

    def test_the_registry_answers_when_the_api_cannot(self):
        """The fix for the defect this whole feature failed to catch.

        WNetGetConnectionW answers about the *calling logon session*, and UAC
        gives an elevated process a different one from the desktop that
        launched it. The wizard always runs elevated, so the API sees none of
        the operator's drive mappings, and the check written to warn about a
        service having no mappings was itself running somewhere with none.
        Measured on the first real Windows install: U:\\TL was stored verbatim.

        HKCU survives the split, because the elevated token is still the same
        user and so still the same hive.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win") as api, \
             mock.patch.object(plat, "_mapped_in_registry",
                               return_value=r"\\tower\cctv") as registry:
            api.return_value.mpr.WNetGetConnectionW.return_value = 1
            self.assertEqual(plat.unc_for_drive("U:"), r"\\tower\cctv")
        registry.assert_called_once_with("U")

    def test_the_api_is_preferred_when_it_does_answer(self):
        # It is authoritative for the session actually asking, and it covers a
        # mapping made without /persistent, which the registry does not.
        def wnet(_local, buf, _size):
            buf.value = r"\\live\share"
            return 0

        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win") as api, \
             mock.patch.object(plat, "_mapped_in_registry") as registry:
            api.return_value.mpr.WNetGetConnectionW.side_effect = wnet
            self.assertEqual(plat.unc_for_drive("U:"), r"\\live\share")
        registry.assert_not_called()

    def test_a_letter_neither_source_knows_is_still_none(self):
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win") as api, \
             mock.patch.object(plat, "_mapped_in_registry", return_value=None):
            api.return_value.mpr.WNetGetConnectionW.return_value = 1
            self.assertIsNone(plat.unc_for_drive("Q:"))

    def test_a_fixed_disk_is_local_and_a_mapping_is_not(self):
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "drive_kind",
                               side_effect=lambda r: plat.DRIVE_FIXED
                               if r.startswith("C") else 1):
            self.assertIs(plat.drive_is_local("C:"), True)
            self.assertIs(plat.drive_is_local("U:"), False)

    def test_it_takes_a_path_and_not_only_a_letter(self):
        """Every caller has a path in its hand. The first version wanted `U:`

        exactly and answered None for `U:\\TL`, so the wizard's refusal branch
        could never fire; the wizard test mocked this function with a fixed
        return value and therefore never passed it an argument at all. A mock
        that stands in for the thing being integrated tests the mock.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "drive_kind",
                               side_effect=lambda r: plat.DRIVE_FIXED
                               if r.startswith("D") else 1):
            self.assertIs(plat.drive_is_local(r"D:\timelapse\out"), True)
            self.assertIs(plat.drive_is_local(r"U:\TL\_temp_"), False)
            self.assertIs(plat.drive_is_local("Q:/videos"), False)

    def test_a_path_with_no_drive_letter_has_no_answer(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            for given in (r"\\tower\cctv\TL", "/mnt/nas", "", "x"):
                self.assertIsNone(plat.drive_is_local(given), given)

    def test_the_lookup_declines_off_windows(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertIsNone(plat.unc_for_drive("U:"))
            self.assertIsNone(plat.drive_is_local("U:"))

    def test_the_lookup_refuses_anything_that_is_not_a_drive(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            for given in ("U", r"\\tower\cctv", "", "C:\\videos"):
                self.assertIsNone(plat.unc_for_drive(given), given)

    def test_the_lookup_accepts_a_drive_written_either_way(self):
        # WNetGetConnectionW wants "U:", but "U:\" is what a path starts with
        # and what an operator types, so the separator is trimmed rather than
        # being grounds for refusal.
        with mock.patch.object(plat, "IS_WINDOWS", True), \
             mock.patch.object(plat, "_win") as api:
            api.return_value.mpr.WNetGetConnectionW.return_value = 1
            for given in ("U:", "U:\\", "U:/"):
                plat.unc_for_drive(given)
                called = api.return_value.mpr.WNetGetConnectionW.call_args[0]
                self.assertEqual(called[0], "U:", given)


class TestNoConsole(unittest.TestCase):
    """Nothing this project starts may put a window on the screen.

    Reported from a real run: testing ffmpeg in the graphical wizard, and then
    pressing Next, flashed up a run of command prompt windows. pythonw.exe has
    no console, so Windows gives a new one to every console child, and a new
    console is a window.
    """

    # These three are exempt, each for its own reason, and the reasons are the
    # point: an exemption without one is how a rule stops meaning anything.
    #
    # capture: the daemon imports no sibling at module scope (§4.1), and it
    #   cannot flash anything in any case, since under the SCM there is no
    #   desktop and in the foreground it already has a console for its child
    #   to inherit.
    # update, web: Linux only in practice, and pulling in a platform import
    #   for a call that is a no-op there is a coupling bought for nothing.
    EXEMPT = {"timelapse_capture.py", "timelapse_update.py",
              "timelapse_web.py"}

    def scripts(self):
        root = Path(__file__).resolve().parent.parent / "scripts"
        return sorted(p for p in root.glob("timelapse_*.py")
                      if p.name not in self.EXEMPT)

    def test_it_is_a_windows_only_flag(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.no_console(),
                             {"creationflags": plat.CREATE_NO_WINDOW})
        with mock.patch.object(plat, "IS_WINDOWS", False):
            # Not creationflags=0: subprocess rejects the argument outright on
            # POSIX, so the keyword has to be absent rather than empty.
            self.assertEqual(plat.no_console(), {})

    def test_every_captured_child_asks_for_no_window(self):
        """Captured, and only captured: that is the whole rule.

        A child that inherits the console in order to print to it would have
        that console taken away and its output would go nowhere, which is why
        the `timelapse` dispatcher and the editor are not covered.
        """
        checked = 0
        for path in self.scripts():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr in ("run", "Popen", "call",
                                               "check_output")):
                    continue
                names = [k.arg for k in node.keywords]
                if not ({"capture_output", "stdout", "stderr"} & set(names)):
                    continue
                asked = any(k.arg is None
                            and isinstance(k.value, ast.Call)
                            and getattr(k.value.func, "id", "") == "no_console"
                            for k in node.keywords)
                self.assertTrue(
                    asked,
                    "%s line %d captures a child without **no_console(), so "
                    "it will flash a window when started from pythonw"
                    % (path.name, node.lineno))
                checked += 1
        self.assertGreater(checked, 12, "no call sites were found at all")


class TestMappedDrives(unittest.TestCase):
    """Enumerating what the operator has mapped, for the wizard's picker.

    From the registry, not from WNetGetConnectionW, for the reason
    unc_for_drive() sets out: the wizard runs elevated and an elevated process
    has its own logon session with no mappings in it.
    """

    def test_it_declines_off_windows(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertEqual(plat.mapped_drives(), [])

    def drives(self, registry):
        """mapped_drives() over an injected registry, on either platform.

        The test this replaces read the developer's real HKCU\\Network and
        asserted the *shape* of whatever came back, so it passed with seven
        mappings here and passed with none on a runner: it reported the
        machine rather than the code, and every assertion in it was vacuous
        on CI.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True), \
                mock.patch.object(plat, "_mapped_letters",
                                  lambda: list(registry)), \
                mock.patch.object(plat, "_mapped_in_registry",
                                  lambda letter: registry[letter]):
            return plat.mapped_drives()

    def test_each_letter_is_paired_with_its_target(self):
        self.assertEqual(
            self.drives({"u": "\\\\tower\\cctv"}),
            [("U:", "\\\\tower\\cctv")])

    def test_the_letter_is_upper_cased_and_colonised(self):
        # The registry holds them lower case; the wizard shows them as drives.
        rows = self.drives({"z": "\\\\nas\\media"})
        self.assertEqual(rows[0][0], "Z:")

    def test_a_letter_with_no_target_is_not_a_mapping(self):
        rows = self.drives({"u": "\\\\tower\\cctv", "x": None})
        self.assertEqual([r[0] for r in rows], ["U:"])

    def test_they_come_back_sorted(self):
        rows = self.drives({"z": "\\\\nas\\z", "u": "\\\\nas\\u",
                            "m": "\\\\nas\\m"})
        self.assertEqual([r[0] for r in rows], ["M:", "U:", "Z:"])

    def test_no_mappings_is_an_empty_list_not_an_error(self):
        self.assertEqual(self.drives({}), [])

    def test_it_is_sorted_by_letter(self):
        rows = plat.mapped_drives()
        self.assertEqual(rows, sorted(rows))


class TestFindingTools(unittest.TestCase):
    """Where ffmpeg is, and what an operator is allowed to type."""

    def test_path_wins(self):
        found = plat.find_tool("ffmpeg", roots=["/nowhere"],
                               which=lambda n: "/usr/bin/ffmpeg",
                               exists=lambda p: True)
        self.assertEqual(found, "/usr/bin/ffmpeg")

    def test_the_roots_are_tried_when_it_is_not_on_path(self):
        with mock.patch.object(plat, "IS_WINDOWS", False):
            found = plat.find_tool("ffmpeg", roots=["/opt/a", "/opt/b"],
                                   which=lambda n: None,
                                   exists=lambda p: p == "/opt/b/ffmpeg")
        self.assertEqual(found, "/opt/b/ffmpeg")

    def test_nothing_found_is_an_empty_string_not_a_guess(self):
        """Empty means "no default", and the wizard then insists on an answer.

        A made-up path would be offered as though it existed, the operator
        would accept it, and the first encode would be where they found out.
        """
        self.assertEqual(plat.find_tool("ffmpeg", roots=[],
                                        which=lambda n: None,
                                        exists=lambda p: False), "")

    def test_the_windows_roots_are_the_places_builds_actually_land(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            roots = plat.ffmpeg_roots({"ProgramFiles": r"C:\Program Files",
                                       "LOCALAPPDATA": r"C:\Users\x\AppData\Local"})
        self.assertIn(r"C:\Program Files\ffmpeg\bin", roots)
        self.assertIn(r"C:\ffmpeg\bin", roots)
        self.assertTrue(any("WinGet" in r for r in roots))

    def test_a_missing_environment_variable_drops_that_root(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            roots = plat.ffmpeg_roots({})
        self.assertEqual(roots, ["C:\\ffmpeg\\bin"])

    def test_the_installers_own_ffmpeg_is_looked_for_first(self):
        """The one root here this project ever writes to.

        installer/prepare.ps1 unpacks a pinned build to exactly this path when
        the machine had none, so if it exists then something has already run it
        once and watched it work. Ahead of the others because we know that
        about it and about none of them; behind PATH, which find_tool checks
        before any root, because an ffmpeg the operator put on PATH is a
        decision and item 11c.6a says we do not overrule those.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True):
            roots = plat.ffmpeg_roots({"ProgramFiles": r"C:\Program Files"})
        self.assertEqual(roots[0],
                         r"C:\Program Files\timelapse-maker\ffmpeg\bin")
        self.assertLess(roots.index(r"C:\Program Files\timelapse-maker\ffmpeg\bin"),
                        roots.index(r"C:\Program Files\ffmpeg\bin"))

    def test_a_file_is_taken_as_given(self):
        self.assertEqual(
            plat.resolve_tool(r"C:\ffmpeg\bin\ffmpeg.exe", "ffmpeg",
                              isdir=lambda p: False),
            r"C:\ffmpeg\bin\ffmpeg.exe")

    def test_a_directory_is_accepted_and_resolved(self):
        """The Windows-shaped part: the zip unpacks a bin folder holding

        ffmpeg.exe, ffprobe.exe and ffplay.exe together, and "the ffmpeg
        binaries path" is how operators there describe it.
        """
        with mock.patch.object(plat, "IS_WINDOWS", True):
            found = plat.resolve_tool(r"C:\ffmpeg\bin", "ffmpeg",
                                      isdir=lambda p: p == r"C:\ffmpeg\bin",
                                      exists=lambda p: p.endswith("ffmpeg.exe"))
        self.assertEqual(found, r"C:\ffmpeg\bin\ffmpeg.exe")

    def test_the_unpacked_folder_above_bin_works_too(self):
        # What you get by unzipping the download and pointing at the result.
        with mock.patch.object(plat, "IS_WINDOWS", True):
            found = plat.resolve_tool(
                r"C:\ffmpeg", "ffprobe",
                isdir=lambda p: p == r"C:\ffmpeg",
                exists=lambda p: p == r"C:\ffmpeg\bin\ffprobe.exe")
        self.assertEqual(found, r"C:\ffmpeg\bin\ffprobe.exe")

    def test_quotes_are_stripped(self):
        # Explorer's "Copy as path" wraps the result in double quotes, and
        # pasting that is the most likely way this answer arrives.
        self.assertEqual(plat.resolve_tool('"C:\\ff\\ffmpeg.exe"', "ffmpeg",
                                           isdir=lambda p: False),
                         r"C:\ff\ffmpeg.exe")

    def test_an_empty_answer_stays_empty(self):
        self.assertEqual(plat.resolve_tool("  ", "ffmpeg"), "")


class TestNoPlatformBranchesElsewhere(unittest.TestCase):
    """Item 11e rule 1: no `if os.name == "nt"` outside this module.

    The two-forks outcome arrives by increments, and this is the increment.
    Written as a scan rather than left as a convention for the same reason the
    RedactingFormatter duplication is a pinned duplication: a rule nobody
    measures is a rule already broken somewhere nobody has looked.
    """

    # timelapse_capture.py is the one exception, and a deliberate one: the
    # daemon imports nothing from its siblings, so it carries a pinned copy of
    # the derivation instead. The pin itself is in test_capture.py.
    ALLOWED = {"timelapse_platform.py", "timelapse_capture.py"}

    PATTERN = re.compile(r"os\.name\s*[=!]=\s*['\"]nt['\"]"
                         r"|sys\.platform\s*[=!]=\s*['\"]win32['\"]"
                         r"|platform\.system\(\)\s*[=!]=")

    def test_no_script_tests_the_platform_by_hand(self):
        offenders = []
        for path in sorted(_support.SCRIPTS.glob("timelapse_*.py")):
            if path.name in self.ALLOWED:
                continue
            if self.PATTERN.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [],
                         "platform branch outside timelapse_platform.py")


class FakeTable(ctypes.Structure):
    """A real structure, because the product builds a (TABLE * 2) array of it.

    `lpServiceProc` is a py_object rather than a function pointer so a plain
    Python callable survives the round trip: ctypes.WINFUNCTYPE does not exist
    off Windows at all, which is why the whole SCM half of this module had no
    test on either leg.
    """
    _fields_ = [("lpServiceName", ctypes.c_wchar_p),
                ("lpServiceProc", ctypes.py_object)]


def deref(arg):
    """The structure behind a byref() or a pointer."""
    obj = getattr(arg, "_obj", None)
    return obj if obj is not None else arg.contents


class FakeAdvapi(object):
    """Enough advapi32 to run the service paths on a runner that has none.

    Every call is recorded with the arguments that matter, and the structures
    handed to SetServiceStatus are copied out rather than referenced, because
    the product reuses one SERVICE_STATUS per report and the interesting thing
    is the sequence.
    """

    def __init__(self, refuse=()):
        self.calls = []
        self.reported = []
        self.config2 = []               # (info level, structure)
        self.hints = []                 # dwWaitHint per report, in order
        self.refuse = set(refuse)       # calls that answer NULL / FALSE
        self.handler = None
        self.service_main = None

    def _ok(self, name):
        self.calls.append(name)
        return 0 if name in self.refuse else 1

    # Registration ---------------------------------------------------------
    def OpenSCManagerW(self, machine, database, access):
        return 11 * self._ok("OpenSCManagerW")

    def OpenServiceW(self, scm, name, access):
        return 12 * self._ok("OpenServiceW")

    def CreateServiceW(self, *args):
        self.created = args
        return 13 * self._ok("CreateServiceW")

    def ChangeServiceConfigW(self, *args):
        return self._ok("ChangeServiceConfigW")

    def ChangeServiceConfig2W(self, svc, level, info):
        ok = self._ok("ChangeServiceConfig2W")
        self.config2.append((level, deref(info)))
        return ok

    def CloseServiceHandle(self, handle):
        return self._ok("CloseServiceHandle")

    def ControlService(self, svc, control, status):
        return self._ok("ControlService")

    # Hosting --------------------------------------------------------------
    def RegisterServiceCtrlHandlerExW(self, name, handler, context):
        self.handler = handler
        return 14 * self._ok("RegisterServiceCtrlHandlerExW")

    def SetServiceStatus(self, handle, status):
        ok = self._ok("SetServiceStatus")
        s = deref(status)
        self.reported.append((s.dwCurrentState, s.dwWin32ExitCode,
                              s.dwServiceSpecificExitCode))
        self.hints.append(s.dwWaitHint)
        return ok

    def StartServiceCtrlDispatcherW(self, table):
        self._ok("StartServiceCtrlDispatcherW")
        self.service_main = table[0].lpServiceProc
        self.service_main(0, None)
        return 1


class FakeWinApi(object):
    """What _win() returns. MAIN and HANDLER hand the callable straight back."""

    def __init__(self, advapi=None, kernel32=None):
        self.advapi32 = advapi or FakeAdvapi()
        self.shell32 = None
        self.kernel32 = kernel32
        self.mpr = None
        # Plain lambdas, not staticmethod(): a staticmethod object is
        # not callable on 3.9, and 3.9 is this project's floor.
        self.MAIN = lambda fn=None: fn
        self.HANDLER = lambda fn=None: fn
        self.TABLE = FakeTable


class FakeKernel32(object):
    """Enough kernel32 for the console-mode path.

    `handle` is what GetStdHandle answers: a c_void_p restype comes back as an
    unsigned integer, so INVALID_HANDLE_VALUE arrives as 18446744073709551615
    and not as -1. That is the whole of L22's fourth part.
    """

    def __init__(self, handle=7, mode=0, set_ok=True):
        self.handle = handle
        self.mode = mode
        self.set_ok = set_ok
        self.set_to = None

    def GetStdHandle(self, which):
        return self.handle

    def GetConsoleMode(self, handle, out):
        out._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_to = mode
        return 1 if self.set_ok else 0


@contextlib.contextmanager
def fake_windows(advapi=None, kernel32=None):
    """Run a Windows-only path on whichever platform this is.

    IS_WINDOWS is patched on the module that reads it, which is this one:
    `from timelapse_platform import IS_WINDOWS` elsewhere makes a second
    binding, and patching one does not move the other.
    """
    api = FakeWinApi(advapi, kernel32)
    with mock.patch.object(plat, "IS_WINDOWS", True), \
            mock.patch.object(plat, "_win", lambda: api):
        yield api.advapi32


class TestAFailedServiceIsReportedAsOne(unittest.TestCase):
    """A stop the SCM should act on has to look different from one it should
    not.

    Runs on both CI legs: nothing here touches a DLL. The SCM runs the
    failure actions only for a service that either died without reporting
    SERVICE_STOPPED or reported one with a non-zero exit code, so the daemon
    catching its own exception and reporting a tidy STOPPED meant the three
    SC_ACTION_RESTART entries, documented as the equivalent of Restart=always,
    never ran at all. One exception out of write_state() left capture down
    until the next reboot; on Linux the same exception restarts in 15 seconds.
    """

    def host(self, body):
        advapi = FakeAdvapi()
        with fake_windows(advapi):
            rc = plat.run_as_service(plat.CAPTURE_UNIT, body, lambda: None)
        return rc, advapi

    def test_a_clean_return_reports_a_clean_stop(self):
        """The control, and it is the half that must not change: stopping a
        service on purpose must not queue a restart."""
        _, advapi = self.host(lambda ready: ready())
        self.assertEqual(advapi.reported[-1][0], plat.SERVICE_STOPPED)
        self.assertEqual(advapi.reported[-1][1:], (plat.NO_ERROR, 0))

    def test_an_exception_reports_a_failure(self):
        def explode(ready):
            ready()
            raise OSError(13, "capture.json is held open by something")

        _, advapi = self.host(explode)
        state, win32, specific = advapi.reported[-1]
        self.assertEqual(state, plat.SERVICE_STOPPED)
        self.assertEqual(win32, plat.ERROR_SERVICE_SPECIFIC_ERROR)
        self.assertNotEqual(specific, 0)

    def test_the_running_report_is_still_a_clean_one(self):
        """Only the last report carries the verdict. A RUNNING with an exit
        code in it would be a service reporting its own death while alive."""
        _, advapi = self.host(lambda ready: ready())
        running = [r for r in advapi.reported
                   if r[0] == plat.SERVICE_RUNNING]
        self.assertEqual(running, [(plat.SERVICE_RUNNING, plat.NO_ERROR, 0)])

    def test_the_failure_is_witnessed_as_well_as_reported(self):
        """The exit code tells Windows to restart; it tells the operator
        nothing. service-error.log is the only witness a failure has before
        logging is up."""
        seen = []

        def explode(ready):
            raise ValueError("config.json: Expecting value: line 1 column 1")

        advapi = FakeAdvapi()
        with fake_windows(advapi):
            plat.run_as_service(plat.CAPTURE_UNIT, explode,
                                lambda: None, on_error=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertIn("ValueError", seen[0])


class TestTheRestartActionsAreArmed(unittest.TestCase):
    """Registering the service has to opt in to acting on a reported failure.

    fFailureActionsOnNonCrashFailures is FALSE by default, and with it FALSE
    the exit code above changes nothing: the SCM runs failure actions only for
    a process that died without reporting SERVICE_STOPPED. The two halves are
    one fix and neither works alone.
    """

    def install(self, refuse=()):
        advapi = FakeAdvapi(refuse)
        with fake_windows(advapi):
            ok, detail = plat.install_service(
                plat.CAPTURE_UNIT, "Camera snapshot capture",
                ["C:\\Python\\python.exe", "C:\\tl\\timelapse_capture.py"])
        return ok, detail, advapi

    def levels(self, advapi):
        return [level for level, _info in advapi.config2]

    def test_the_flag_is_set_alongside_the_actions(self):
        ok, detail, advapi = self.install()
        self.assertTrue(ok, detail)
        self.assertIn(plat.SERVICE_CONFIG_FAILURE_ACTIONS,
                      self.levels(advapi))
        self.assertIn(plat.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG,
                      self.levels(advapi))

    def test_the_flag_is_true(self):
        """A structure of the right shape carrying zero would register, report
        success and do nothing, which is the state this fix found."""
        _, _, advapi = self.install()
        flag = [info for level, info in advapi.config2
                if level == plat.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG]
        self.assertEqual(len(flag), 1)
        self.assertEqual(flag[0].fFailureActionsOnNonCrashFailures, 1)

    def test_the_actions_are_still_three_restarts(self):
        _, _, advapi = self.install()
        actions = [info for level, info in advapi.config2
                   if level == plat.SERVICE_CONFIG_FAILURE_ACTIONS]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].cActions, 3)
        for i in range(3):
            self.assertEqual(actions[0].lpsaActions[i].Type,
                             plat.SC_ACTION_RESTART)

    def test_a_refused_flag_is_reported_and_does_not_undo_the_install(self):
        """A service that runs without automatic restart is worth more than no
        service and a rollback, which is what the other two already do."""
        ok, detail, _ = self.install(refuse=("ChangeServiceConfig2W",))
        self.assertTrue(ok)
        self.assertIn("restart-on-reported-failure", detail)

if __name__ == "__main__":
    unittest.main()




class TestTheOsProbesLiveHere(unittest.TestCase):
    """Three probes moved out of the wizard at 0.2.1 (L13).

    Item 11e's rule: the platform branch lives in one file. Each of these runs
    a command or reads a file that exists on one platform and not the other,
    and the wizard runs on both. Every test declares the platform rather than
    inheriting it, so both halves are exercised on both CI legs.
    """

    def test_the_address_list_does_not_shell_out_on_windows(self):
        # It ran `ip -j addr` and then `hostname -I`, on a platform that has
        # neither: two process launches per page, for nothing.
        def refuse(*a, **kw):
            raise AssertionError("shelled out on Windows: %r" % (a,))

        with mock.patch.object(plat, "IS_WINDOWS", True), \
                mock.patch.object(plat.subprocess, "run", refuse), \
                mock.patch.object(plat.socket, "getaddrinfo",
                                  return_value=[(2, 1, 6, "", ("192.0.2.5", 0))]):
            self.assertEqual(plat.host_addresses(), ["192.0.2.5"])

    def test_the_address_list_still_reads_ip_addr_on_linux(self):
        payload = json.dumps([
            {"addr_info": [{"local": "192.0.2.5"}, {"local": "fe80::1"}]},
            {"addr_info": [{"local": "127.0.0.1"}]},
        ]).encode("utf-8")

        with mock.patch.object(plat, "IS_WINDOWS", False), \
                mock.patch.object(plat.subprocess, "run",
                                  return_value=mock.Mock(returncode=0,
                                                         stdout=payload)):
            found = plat.host_addresses()
        # Link-local dropped (it cannot be bound without a scope id), and
        # loopback last, because it is a valid answer and never the
        # interesting one.
        self.assertEqual(found, ["192.0.2.5", "127.0.0.1"])

    def test_a_windows_path_has_no_mount_type(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.mount_fstype("C:\\videos"), ("", None))

    def test_a_linux_mount_type_is_read_from_proc(self):
        mounts = ("proc /proc proc rw 0 0\n"
                  "//tower/cctv /mnt/nas cifs rw 0 0\n"
                  "/dev/sda1 / ext4 rw 0 0\n")
        with mock.patch.object(plat, "IS_WINDOWS", False), \
                mock.patch("builtins.open",
                           mock.mock_open(read_data=mounts)):
            # The longest matching mount point wins, or every path under a
            # share reports the root filesystem.
            self.assertEqual(plat.mount_fstype("/mnt/nas/tl")[0], "cifs")

    def test_installing_cifs_utils_says_nothing(self):
        """The wizard owns the talking.

        This half knows about package managers and the caller knows what the
        operator is in the middle of. It is also imported by the daemon, which
        has no console under the SCM.
        """
        with mock.patch.object(plat, "IS_WINDOWS", False), \
                mock.patch.object(plat.shutil, "which", lambda n: None), \
                mock.patch.object(plat.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ok = plat.install_cifs_utils()
        self.assertFalse(ok)
        self.assertEqual(out.getvalue(), "")

    def test_nothing_is_installed_on_windows(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertFalse(plat.install_cifs_utils())

    def test_an_already_installed_cifs_needs_no_package_manager(self):
        def refuse(*a, **kw):
            raise AssertionError("ran a package manager for nothing")

        with mock.patch.object(plat, "IS_WINDOWS", False), \
                mock.patch.object(plat.shutil, "which",
                                  lambda n: "/sbin/mount.cifs"), \
                mock.patch.object(plat.subprocess, "run", refuse):
            self.assertTrue(plat.install_cifs_utils())


class TestTheStopErrorIsTheOneThatFailed(unittest.TestCase):
    """GetLastError is per thread and the next API call overwrites it.

    `stop_service()` called `service_state()` in between, which makes several
    calls of its own and finishes with CloseServiceHandle, so the number shown
    to the operator was whatever that left. 1061 (still starting) and 1053
    (the stop is hung) are the two that mean something here, and neither could
    survive the trip.
    """

    def clobbering_state(self, unit):
        # What the real one does: several calls, each setting the error.
        #
        # create=True on the patches below, because ctypes.get_last_error is
        # **Windows-only**: these two passed here and errored on all three
        # Linux legs, which is the fourth time this project has met "a local
        # branch is a branch CI has never seen".
        plat.ctypes.get_last_error()
        return plat.SERVICE_RUNNING

    def test_the_detail_names_control_services_own_error(self):
        with fake_windows() as advapi:
            advapi.refuse.add("ControlService")
            with mock.patch.object(plat, "service_state",
                                   self.clobbering_state), \
                    mock.patch.object(plat.ctypes, "get_last_error",
                                      create=True,
                                      side_effect=[1053, 6, 6, 6]):
                ok, detail = plat.stop_service("timelapse-capture", timeout=0)
        self.assertFalse(ok)
        self.assertIn("1053", detail)
        self.assertNotIn("(Windows error 6)", detail)

    def test_a_service_already_stopped_is_still_a_success(self):
        # The control: ControlService refusing because the service is already
        # stopped is the ordinary case and must not become an error.
        with fake_windows() as advapi:
            advapi.refuse.add("ControlService")
            with mock.patch.object(plat, "service_state",
                                   lambda u: plat.SERVICE_STOPPED), \
                    mock.patch.object(plat.ctypes, "get_last_error",
                                      create=True, return_value=1062):
                ok, detail = plat.stop_service("timelapse-capture", timeout=0)
        self.assertTrue(ok)
        self.assertEqual(detail, "")


class TestWaitingStopsWhenTheAnswerIsIn(unittest.TestCase):
    """A service that exits at once answered in a second and was waited on
    for thirty.

    `run_daemon()` raises SystemExit with no enabled cameras, so the service
    goes START_PENDING then STOPPED, and the GUI sat with a busy cursor for
    the rest of the timeout before saying "started, but reported stopped".
    """

    def setUp(self):
        """A fake clock, because the point of this item is elapsed time.

        With the real one, a test that only reads the returned state passes
        whether or not the wait gave up early: it simply takes thirty seconds
        to say so. Both of these tests were written that way first and both
        were NOT CAUGHT by the revert check. So sleep() advances the clock,
        nothing waits, and the assertion is on how many times the service was
        asked.
        """
        self.now = 1000.0
        self.polls = 0

        def clock():
            return self.now

        def sleep(seconds):
            self.now += seconds

        for name, fn in (("time", clock), ("sleep", sleep)):
            p = mock.patch.object(plat.time, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def states(self, sequence):
        """A service_state() that walks a list and then repeats the last."""
        seq = list(sequence)

        def answer(_unit):
            self.polls += 1
            return seq.pop(0) if len(seq) > 1 else seq[0]

        return answer

    def test_it_gives_up_when_the_service_has_stopped(self):
        answer = self.states([plat.SERVICE_START_PENDING, plat.SERVICE_STOPPED])
        with mock.patch.object(plat, "service_state", answer), \
                mock.patch.object(plat.time, "sleep", lambda s: None):
            state = plat._await_state("x", plat.SERVICE_RUNNING, 30, settle=0)
        self.assertEqual(state, plat.SERVICE_STOPPED)
        # Two polls: pending, then stopped. Without the early exit it polls
        # for the whole thirty seconds to reach the same answer.
        self.assertLessEqual(self.polls, 3, "it kept waiting anyway")

    def test_it_gives_up_when_the_service_is_not_there(self):
        with mock.patch.object(plat, "service_state",
                               lambda u: plat.SERVICE_ABSENT), \
                mock.patch.object(plat.time, "sleep", lambda s: None):
            state = plat._await_state("x", plat.SERVICE_STOPPED, 30)
        self.assertEqual(state, plat.SERVICE_ABSENT)
        self.assertLessEqual(self.polls, 3, "it waited for a service that is "
                                            "not installed")

    def test_a_stopped_reading_before_the_grace_is_not_believed(self):
        """StartServiceW can return before the SCM has moved it off STOPPED.

        So the first reading is not evidence of anything, and giving up on it
        would turn a service that started perfectly into a reported failure.
        """
        answer = self.states([plat.SERVICE_STOPPED, plat.SERVICE_STOPPED,
                              plat.SERVICE_RUNNING])
        with mock.patch.object(plat, "service_state", answer), \
                mock.patch.object(plat.time, "sleep", lambda s: None):
            state = plat._await_state("x", plat.SERVICE_RUNNING, 30, settle=30)
        self.assertEqual(state, plat.SERVICE_RUNNING)

    def test_it_still_waits_through_a_pending_start(self):
        # The control: pending is exactly what the wait is for.
        answer = self.states([plat.SERVICE_START_PENDING,
                              plat.SERVICE_START_PENDING,
                              plat.SERVICE_RUNNING])
        with mock.patch.object(plat, "service_state", answer), \
                mock.patch.object(plat.time, "sleep", lambda s: None):
            state = plat._await_state("x", plat.SERVICE_RUNNING, 30, settle=0)
        self.assertEqual(state, plat.SERVICE_RUNNING)

    def test_running_on_the_way_down_is_not_a_contradiction(self):
        answer = self.states([plat.SERVICE_RUNNING, plat.SERVICE_STOP_PENDING,
                              plat.SERVICE_STOPPED])
        with mock.patch.object(plat, "service_state", answer), \
                mock.patch.object(plat.time, "sleep", lambda s: None):
            state = plat._await_state("x", plat.SERVICE_STOPPED, 30)
        self.assertEqual(state, plat.SERVICE_STOPPED)


class TestTheControlHandlerAnswersProperly(unittest.TestCase):
    """It returned NO_ERROR for every control there is.

    Which says "handled" to the SCM, so a pause request looked accepted and
    did nothing at all.
    """

    def handler(self, advapi):
        self.assertIsNotNone(advapi.handler, "the handler was never registered")
        return advapi.handler

    def run_service(self, advapi):
        stopped = []
        plat.run_as_service("timelapse-capture",
                            lambda ready: (ready(), stopped.append(True)),
                            lambda: None)
        return stopped

    def test_an_unknown_control_is_refused_rather_than_swallowed(self):
        with fake_windows() as advapi:
            self.run_service(advapi)
            answer = self.handler(advapi)(32, 0, None, None)
        self.assertEqual(answer, plat.ERROR_CALL_NOT_IMPLEMENTED)

    def test_a_stop_is_still_accepted(self):
        # The control: refusing the unknown ones must not refuse the known.
        with fake_windows() as advapi:
            self.run_service(advapi)
            answer = self.handler(advapi)(plat.SERVICE_CONTROL_STOP, 0, None,
                                          None)
        self.assertEqual(answer, plat.NO_ERROR)

    def test_an_interrogate_during_a_stop_carries_the_wait_hint(self):
        """Answering with no hint would tell the SCM the wait it granted is
        over, which is how a slow stop comes to be called hung."""
        with fake_windows() as advapi:
            self.run_service(advapi)
            handler = self.handler(advapi)
            handler(plat.SERVICE_CONTROL_STOP, 0, None, None)
            before = len(advapi.hints)
            handler(plat.SERVICE_CONTROL_INTERROGATE, 0, None, None)
        self.assertGreater(advapi.hints[before], 0,
                           "the interrogate answered with no wait hint")

    def test_an_interrogate_while_running_carries_none(self):
        with fake_windows() as advapi:
            self.run_service(advapi)
            handler = self.handler(advapi)
            before = len(advapi.hints)
            handler(plat.SERVICE_CONTROL_INTERROGATE, 0, None, None)
        self.assertEqual(advapi.hints[before], 0)


class TestTheConsoleHandleIsCompared(unittest.TestCase):
    """`handle == -1` is never true.

    GetStdHandle's restype is a void pointer, so ctypes hands back an unsigned
    integer: INVALID_HANDLE_VALUE arrives as 18446744073709551615. The guard
    was doing nothing, and it never showed because GetConsoleMode fails on
    that handle anyway.
    """

    def test_an_invalid_handle_is_recognised(self):
        bad = FakeKernel32(handle=ctypes.c_void_p(-1).value)
        with fake_windows(kernel32=bad):
            self.assertFalse(plat.enable_ansi())
        self.assertIsNone(bad.set_to, "it tried to set the mode anyway")

    def test_a_real_handle_gets_the_bit_set(self):
        good = FakeKernel32(handle=7, mode=0)
        with fake_windows(kernel32=good):
            self.assertTrue(plat.enable_ansi())
        self.assertEqual(good.set_to, plat.ENABLE_VIRTUAL_TERMINAL_PROCESSING)

    def test_a_console_that_already_has_it_is_left_alone(self):
        already = FakeKernel32(
            handle=7, mode=plat.ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        with fake_windows(kernel32=already):
            self.assertTrue(plat.enable_ansi())
        self.assertIsNone(already.set_to)


class TestSchtasksOutputIsDecodedAsTheConsoleWrote(unittest.TestCase):
    """schtasks writes the console codepage, not UTF-8.

    Its output is never parsed, only shown, so the cost of getting this wrong
    is mojibake in front of an operator rather than a wrong decision. The
    `oem` codec is Windows-only, which is why the product names the platform
    here and this test declares it.
    """

    def run_it(self, stdout, codec=None):
        patches = [mock.patch.object(plat.subprocess, "run",
                                     return_value=mock.Mock(returncode=0,
                                                            stdout=stdout))]
        if codec:
            patches.append(mock.patch.object(plat, "console_codec",
                                             lambda: codec))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return plat._schtasks(["/query"])

    def test_the_codec_is_the_console_one_on_windows(self):
        with mock.patch.object(plat, "IS_WINDOWS", True):
            self.assertEqual(plat.console_codec(), "oem")

    def test_it_is_utf_8_where_there_is_no_such_thing(self):
        # The codec itself is Windows-only, so naming it on Linux would be an
        # import-time LookupError rather than mojibake.
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertEqual(plat.console_codec(), "utf-8")

    def test_the_output_is_decoded_with_it(self):
        # cp850 stands in for the real console codepage: it is a stdlib codec
        # on every platform, so this runs on both legs and still measures the
        # thing that matters, which is that the bytes are decoded as the
        # console wrote them rather than as UTF-8. Under UTF-8 these two bytes
        # are a replacement character apiece.
        ok, text = self.run_it(b"Dur\xe9e\n", codec="cp850")
        self.assertTrue(ok)
        self.assertEqual(text, b"Dur\xe9e".decode("cp850"))
        # The control, and the whole finding: under UTF-8 that byte is a
        # replacement character. Asserting against the codec rather than
        # against a character I typed, because cp850 is a console codepage and
        # 0xE9 is not what latin-1 would make of it.
        self.assertNotEqual(text, b"Dur\xe9e".decode("utf-8", "replace"))

    def test_high_bytes_never_raise(self):
        ok, text = self.run_it(b"SUCCESS: \xe9\xe8\n")
        self.assertTrue(ok)
        self.assertIn("SUCCESS", text)

    def test_plain_output_is_unchanged(self):
        ok, text = self.run_it(b"SUCCESS: the task was created\n")
        self.assertTrue(ok)
        self.assertEqual(text, "SUCCESS: the task was created")

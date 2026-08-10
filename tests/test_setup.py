"""Unit tests for timelapse_setup.py: storage discovery and config shaping.

The storage scan is the part with real bug surface: it has to reject a dozen
kinds of thing that look like disks but aren't. These drive it with a synthetic
/proc/mounts so the awkward cases are always present, regardless of the machine.
"""

import contextlib
import io
import json
import shutil
import socket
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import _support
from _support import FakeStatVFS, write_mounts

import timelapse_setup as setup


def fake_statvfs(_target):
    return FakeStatVFS(free_gb=100, total_gb=200)


def no_rotational(_source):
    return None


class TestScanFilesystems(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self, lines, statvfs=fake_statvfs):
        return setup.scan_filesystems(write_mounts(self.tmp, lines),
                                      statvfs=statvfs,
                                      rotational=no_rotational)

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
        self.assertEqual(setup.scan_filesystems("/nonexistent/mounts"), [])

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
        return setup._base_device(source, sys_block=str(self.tmp))

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
        self.assertIs(setup._is_rotational("/dev/sda1", str(self.tmp)), True)
        self.assertIs(setup._is_rotational("/dev/nvme0n1p2", str(self.tmp)),
                      False)

    def test_rotational_is_unknown_when_absent(self):
        self.assertIsNone(setup._is_rotational("/dev/sda1", str(self.tmp)))


class TestRecommend(unittest.TestCase):

    def disk(self, mount, free_gb):
        return {"mount": mount, "free": int(free_gb * 1024 ** 3),
                "total": 1024 ** 4, "fstype": "ext4", "source": "/dev/sda1",
                "rotational": None}

    def test_no_disks(self):
        self.assertIsNone(setup.recommend([]))

    def test_prefers_a_roomy_non_root_disk_over_a_bigger_root(self):
        # Don't fill the OS disk, even when it has more space.
        disks = [self.disk("/", 900), self.disk("/mnt/data", 100)]
        self.assertEqual(setup.recommend(disks)["mount"], "/mnt/data")

    def test_falls_back_to_root_when_others_are_too_small(self):
        disks = [self.disk("/", 900), self.disk("/boot-ish", 5)]
        self.assertEqual(setup.recommend(disks)["mount"], "/")

    def test_picks_the_largest_non_root(self):
        disks = [self.disk("/mnt/big", 800), self.disk("/mnt/small", 50),
                 self.disk("/", 900)]
        self.assertEqual(setup.recommend(disks)["mount"], "/mnt/big")

    def test_root_only(self):
        self.assertEqual(setup.recommend([self.disk("/", 500)])["mount"], "/")


class TestWritablePaths(unittest.TestCase):

    def cfg(self, transfer=None):
        return {"paths": {"frames_root": "/srv/tl/frames",
                          "video_output": "/srv/tl/videos",
                          "log_dir": "/srv/tl/logs"},
                "transfer": transfer or {"enabled": False}}

    def test_lists_the_three_data_directories(self):
        self.assertEqual(setup.writable_paths(self.cfg()),
                         ["/srv/tl/frames", "/srv/tl/logs", "/srv/tl/videos"])

    def test_includes_a_local_transfer_destination(self):
        # Trailing slash on purpose: the shipped example config writes the
        # destination that way, and systemd wants it normalised.
        paths = setup.writable_paths(
            self.cfg({"enabled": True, "destination": "/mnt/nas/tl/"}))
        self.assertIn("/mnt/nas/tl", paths)

    def test_normalises_trailing_and_duplicate_separators(self):
        cfg = {"paths": {"frames_root": "/srv/tl/frames/",
                         "video_output": "/srv/tl//videos",
                         "log_dir": "/srv/tl/logs/"},
               "transfer": {"enabled": False}}
        self.assertEqual(setup.writable_paths(cfg),
                         ["/srv/tl/frames", "/srv/tl/logs", "/srv/tl/videos"])

    def test_a_trailing_slash_does_not_defeat_deduplication(self):
        cfg = {"paths": {"frames_root": "/srv/tl", "video_output": "/srv/tl/",
                         "log_dir": "/srv/tl"},
               "transfer": {"enabled": False}}
        self.assertEqual(setup.writable_paths(cfg), ["/srv/tl"])

    def test_ignores_a_remote_transfer_destination(self):
        # rsync over SSH writes nothing locally, so systemd needs no permission.
        paths = setup.writable_paths(
            self.cfg({"enabled": True, "destination": "user@nas:/mnt/tl"}))
        self.assertEqual(len(paths), 3)

    def test_ignores_a_destination_when_transfer_is_disabled(self):
        paths = setup.writable_paths(
            self.cfg({"enabled": False, "destination": "/mnt/nas/tl"}))
        self.assertNotIn("/mnt/nas/tl", paths)

    def test_drops_paths_covered_by_a_parent(self):
        cfg = self.cfg()
        cfg["paths"]["log_dir"] = "/srv/tl/frames/logs"
        self.assertNotIn("/srv/tl/frames/logs", setup.writable_paths(cfg))

    def test_deduplicates_identical_paths(self):
        cfg = self.cfg()
        cfg["paths"]["video_output"] = cfg["paths"]["frames_root"]
        self.assertEqual(len(setup.writable_paths(cfg)), 2)

    def test_a_similar_prefix_is_not_treated_as_a_parent(self):
        # /srv/tl-old must not be swallowed by /srv/tl.
        cfg = {"paths": {"frames_root": "/srv/tl",
                         "video_output": "/srv/tl-old",
                         "log_dir": "/srv/tl"},
               "transfer": {"enabled": False}}
        self.assertEqual(setup.writable_paths(cfg), ["/srv/tl", "/srv/tl-old"])


class TestCredentialQuoting(unittest.TestCase):
    """A password in a query string must survive intact.

    Encoding is deliberately minimal: some camera firmware does not
    percent-decode query values, so anything encoded unnecessarily becomes an
    authentication failure that looks like a bad URL.
    """

    def test_query_breaking_characters_are_escaped(self):
        self.assertEqual(setup.quote("p@ss&w=rd#1"), "p@ss%26w%3Drd%231")

    def test_percent_and_plus_are_escaped(self):
        self.assertEqual(setup.quote("50%+more"), "50%25%2Bmore")

    def test_space_is_escaped(self):
        self.assertEqual(setup.quote("two words"), "two%20words")

    def test_characters_that_need_no_escaping_are_left_alone(self):
        for raw in ("simple123", "a/b", "p@ssword", "a:b", "wh?at", "a!b*c",
                    "under_score-dot.til~de"):
            with self.subTest(raw=raw):
                self.assertEqual(setup.quote(raw), raw)

    def test_non_ascii_becomes_utf8_percent_bytes(self):
        self.assertEqual(setup.quote("é"), "%C3%A9")

    def test_every_escaped_form_still_round_trips(self):
        for secret in ("p@ss&w=rd#1", "50%+more", "two words", "simple123",
                       "a/b", "é", "!$'()*,;:@"):
            with self.subTest(secret=secret):
                url = f"http://h/x?user=admin&password={setup.quote(secret)}&cmd=Snap"
                q = parse_qs(urlparse(url).query)
                self.assertEqual(q["password"], [secret])
                self.assertEqual(q["cmd"], ["Snap"])


class FakeTTY(io.StringIO):
    """A terminal that supplies a fixed script of keystrokes."""

    def __init__(self, text, tty=True):
        super().__init__(text)
        self.reads = 0
        self._tty = tty

    def isatty(self):
        return self._tty

    def readline(self):
        self.reads += 1
        return super().readline()


class TestEnterAcceptsTheDefault(unittest.TestCase):
    """The wizard's one promise: Enter accepts what is in brackets.

    Regression: ask() used to return early only for a non-empty default and
    otherwise loop, so pressing Enter at any yes/no prompt (which passes an
    empty default) re-prompted forever.
    """

    def drive(self, keystrokes, fn):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY(keystrokes)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                result = fn()
        finally:
            reads = setup._TTY.reads
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        return result, reads, buf.getvalue()

    def test_enter_accepts_a_yes_default_with_one_read(self):
        result, reads, _ = self.drive("\n", lambda: setup.ask_yes("Go?", True))
        self.assertTrue(result)
        self.assertEqual(reads, 1, "Enter must not re-prompt")

    def test_enter_accepts_a_no_default_with_one_read(self):
        result, reads, _ = self.drive("\n", lambda: setup.ask_yes("Go?", False))
        self.assertFalse(result)
        self.assertEqual(reads, 1)

    def test_prompt_is_shown_exactly_once(self):
        _, _, out = self.drive("\n", lambda: setup.ask_yes("Unique?", True))
        self.assertEqual(out.count("Unique?"), 1)

    def test_enter_accepts_an_empty_string_default(self):
        result, reads, _ = self.drive("\n", lambda: setup.ask("Webhook URL", ""))
        self.assertEqual(result, "")
        self.assertEqual(reads, 1)

    def test_enter_accepts_a_text_default(self):
        result, reads, _ = self.drive("\n", lambda: setup.ask("Name", "Camera1"))
        self.assertEqual(result, "Camera1")
        self.assertEqual(reads, 1)

    def test_enter_accepts_a_numeric_default(self):
        result, reads, _ = self.drive("\n", lambda: setup.ask_int("How many?", 4))
        self.assertEqual(result, 4)
        self.assertEqual(reads, 1)

    def test_typed_input_still_wins(self):
        self.assertEqual(self.drive("Doorbell\n",
                                    lambda: setup.ask("Name", "Camera1"))[0],
                         "Doorbell")
        self.assertTrue(self.drive("y\n", lambda: setup.ask_yes("Go?", False))[0])
        self.assertFalse(self.drive("n\n", lambda: setup.ask_yes("Go?", True))[0])

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(self.drive("  Gate  \n",
                                    lambda: setup.ask("Name", "x"))[0], "Gate")

    def test_whitespace_only_counts_as_blank(self):
        self.assertEqual(self.drive("   \n",
                                    lambda: setup.ask("Name", "Camera1"))[0],
                         "Camera1")

    def test_eof_falls_back_to_the_default(self):
        result, _, _ = self.drive("", lambda: setup.ask("Name", "Camera1"))
        self.assertEqual(result, "Camera1")

    def test_a_bad_number_reprompts_but_enter_then_ends_it(self):
        # Rejecting non-numeric input is correct; it must still terminate.
        result, reads, _ = self.drive("abc\n\n", lambda: setup.ask_int("N?", 7))
        self.assertEqual(result, 7)
        self.assertEqual(reads, 2)

    def test_an_unrecognised_yes_no_reprompts_then_terminates(self):
        result, reads, _ = self.drive("maybe\n\n",
                                      lambda: setup.ask_yes("Go?", True))
        self.assertTrue(result)
        self.assertEqual(reads, 2)


class TestCameraCounter(unittest.TestCase):
    """The prompt asks about the *next* camera, so it must name that one.

    Regression: after adding the third camera it read "Add another camera?
    (3 of ~9)", which looks like the count went backwards.
    """

    def drive(self, keystrokes, expected):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        prev_test = setup.test_camera
        setup.AUTO = False
        setup._TTY = FakeTTY(keystrokes, tty=False)
        setup.test_camera = lambda cam, cfg: True     # no network in tests
        cfg = setup.default_config()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                setup.choose_cameras(cfg, expected)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
            setup.test_camera = prev_test
        return cfg["cameras"], buf.getvalue()

    # configure? / type / name / ip / user / pass / test? / add another?
    ONE = "y\n1\nGate\n10.0.0.1\nadmin\npw\nn\n"
    TWO = ONE + "y\n1\nYard\n10.0.0.2\nadmin\npw\nn\n"

    def test_prompt_names_the_next_camera_not_the_count(self):
        _, out = self.drive(self.ONE + "n\n", 9)
        self.assertIn("Add camera 2 of ~9?", out)
        self.assertNotIn("(1 of ~9)", out)

    def test_counter_advances_with_each_camera(self):
        cams, out = self.drive(self.TWO + "n\n", 9)
        self.assertEqual([c["name"] for c in cams], ["Gate", "Yard"])
        self.assertIn("Add camera 2 of ~9?", out)
        self.assertIn("Add camera 3 of ~9?", out)

    def test_added_message_still_reports_the_running_total(self):
        _, out = self.drive(self.TWO + "n\n", 9)
        self.assertIn("(1 configured)", out)
        self.assertIn("(2 configured)", out)

    def test_no_nonsense_index_once_the_estimate_is_met(self):
        # With an estimate of 1, the second prompt must not read "2 of ~1".
        _, out = self.drive(self.ONE + "n\n", 1)
        self.assertNotIn("of ~1?", out)
        self.assertIn("(1 configured)", out)

    def test_default_flips_to_no_once_the_estimate_is_met(self):
        # Answering with a bare Enter after meeting the estimate should stop.
        cams, _ = self.drive(self.ONE + "\n", 1)
        self.assertEqual(len(cams), 1)


class TestCameraManagement(unittest.TestCase):
    """`timelapse cameras`: add/edit/remove against a live config.

    The bias in these tests is toward the ways this can silently lose data.
    The encoder builds its work list from the cameras *enabled* in the config
    and looks for <frames_root>/<name>/, so removing, disabling or renaming a
    camera can strand everything it has already captured.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = {"paths": {"frames_root": str(self.tmp)},
                    "capture": {"timeout_seconds": 4},
                    "cameras": [
                        {"name": "Gate", "enabled": True, "method": "http",
                         "url": "http://192.0.2.1/snap", "auth": "none"},
                        {"name": "Roof", "enabled": True, "method": "http",
                         "url": "http://192.0.2.2/snap", "auth": "none"}]}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        setup._TTY, setup.AUTO = None, False

    def drive(self, keystrokes, fn):
        setup.AUTO = False
        setup._TTY = FakeTTY(keystrokes, tty=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fn()
        return result, buf.getvalue()

    def day(self, camera, name):
        d = self.tmp / camera / name
        d.mkdir(parents=True)
        return d

    # -- stranded frames ----------------------------------------------------

    def test_removing_a_camera_with_unencoded_frames_warns(self):
        self.day("Gate", "2020-01-01")
        _, out = self.drive("n\n", lambda: setup.warn_stranded(
            self.cfg, "Gate", "remove"))
        self.assertIn("1 un-encoded day", out)
        self.assertIn("timelapse encode --date 2020-01-01", out)

    def test_declining_the_warning_stops_the_removal(self):
        self.day("Gate", "2020-01-01")
        keep, _ = self.drive("n\n", lambda: setup.warn_stranded(
            self.cfg, "Gate", "remove"))
        self.assertFalse(keep)

    def test_a_camera_with_no_pending_frames_just_confirms(self):
        ok, out = self.drive("y\n", lambda: setup.warn_stranded(
            self.cfg, "Gate", "remove"))
        self.assertTrue(ok)
        self.assertNotIn("un-encoded", out)

    def test_todays_frames_are_not_counted_as_pending(self):
        # Today is still being captured; the encoder deliberately skips it, so
        # warning about it would cry wolf every single time.
        self.day("Gate", date.today().isoformat())
        self.assertEqual(setup.pending_days(self.cfg, "Gate"), [])

    def test_non_date_directories_are_ignored(self):
        self.day("Gate", "notadate")
        self.assertEqual(setup.pending_days(self.cfg, "Gate"), [])

    def test_disabling_warns_too_because_encode_skips_disabled_cameras(self):
        self.day("Roof", "2020-01-01")
        _, out = self.drive("n\n", lambda: setup.warn_stranded(
            self.cfg, "Roof", "disable"))
        self.assertIn("un-encoded", out)

    # -- renaming -----------------------------------------------------------

    def test_renaming_moves_the_frames_directory(self):
        self.day("Gate", "2020-01-01")
        self.drive("y\n", lambda: setup.rename_camera_frames(
            self.cfg, "Gate", "FrontGate"))
        self.assertTrue((self.tmp / "FrontGate" / "2020-01-01").is_dir())
        self.assertFalse((self.tmp / "Gate").exists())

    def test_declining_the_move_leaves_the_frames_and_says_so(self):
        self.day("Gate", "2020-01-01")
        _, out = self.drive("n\n", lambda: setup.rename_camera_frames(
            self.cfg, "Gate", "FrontGate"))
        self.assertTrue((self.tmp / "Gate" / "2020-01-01").is_dir())
        self.assertIn("no longer be encoded", out)

    def test_rename_into_an_existing_directory_refuses_to_merge(self):
        self.day("Gate", "2020-01-01")
        self.day("Roof", "2020-01-02")
        _, out = self.drive("y\n", lambda: setup.rename_camera_frames(
            self.cfg, "Gate", "Roof"))
        self.assertTrue((self.tmp / "Gate" / "2020-01-01").is_dir())
        self.assertTrue((self.tmp / "Roof" / "2020-01-02").is_dir())
        self.assertIn("already exists", out)

    # -- names --------------------------------------------------------------

    def test_duplicate_names_are_rejected_case_insensitively(self):
        cams = self.cfg["cameras"]
        self.assertTrue(setup.name_taken(cams, "gate"))
        self.assertTrue(setup.name_taken(cams, "GATE"))
        self.assertFalse(setup.name_taken(cams, "Garage"))

    def test_a_camera_does_not_collide_with_itself_when_edited(self):
        cams = self.cfg["cameras"]
        self.assertFalse(setup.name_taken(cams, "Gate", skip=cams[0]))

    def test_names_are_reduced_to_safe_directory_characters(self):
        self.assertEqual(setup.sanitise_name("Front Gate!", "x"), "FrontGate")
        self.assertEqual(setup.sanitise_name("../etc", "x"), "etc")
        self.assertEqual(setup.sanitise_name("!!!", "fallback"), "fallback")

    # -- credentials --------------------------------------------------------

    def test_the_camera_list_does_not_print_passwords(self):
        # ask_secret() keeps them out of scroll-back; the listing must not
        # hand them straight back.
        self.cfg["cameras"][0]["url"] = (
            "http://192.0.2.1/cgi-bin/api.cgi?cmd=Snap&user=admin"
            "&password=hunter2&channel=0")
        _, out = self.drive("", lambda: setup.list_cameras(self.cfg))
        self.assertNotIn("hunter2", out)
        self.assertIn("***", out)

    def test_redaction_keeps_the_rest_of_the_url_intact(self):
        red = setup.redact_url("http://h/a?user=admin&password=p%40ss&channel=0")
        self.assertIn("user=admin", red)
        self.assertIn("channel=0", red)
        self.assertNotIn("p%40ss", red)


class TestResolveCamera(unittest.TestCase):
    """`timelapse cameras -e:2` and `-e:Doorbell` reach the same camera.

    The number is the position 'timelapse cameras -l' prints. Nothing in the
    config is a stable id, so the number is an artefact of the order cameras
    were added, which is why a name that matches beats a position that does.
    """

    CAMS = [{"name": "Gate"}, {"name": "Doorbell"}, {"name": "Roof"}]

    def resolve(self, token, cams=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = setup.resolve_camera(self.CAMS if cams is None else cams,
                                       token)
        return got, buf.getvalue()

    def test_a_number_is_the_position_in_the_listing(self):
        self.assertEqual(self.resolve("2")[0], 1)
        self.assertEqual(self.resolve("1")[0], 0)

    def test_a_name_matches_whatever_its_case(self):
        self.assertEqual(self.resolve("doorbell")[0], 1)
        self.assertEqual(self.resolve("DOORBELL")[0], 1)

    def test_a_number_out_of_range_says_how_many_there_are(self):
        got, out = self.resolve("9")
        self.assertIsNone(got)
        self.assertIn("there are 3", out)

    def test_an_unknown_name_is_refused_rather_than_guessed(self):
        # Never fuzzy-match: the actions behind this include "remove".
        got, out = self.resolve("Gat")
        self.assertIsNone(got)
        self.assertIn("No camera is called 'Gat'", out)

    def test_a_camera_actually_called_2_wins_over_position_2(self):
        cams = [{"name": "Gate"}, {"name": "Roof"}, {"name": "2"}]
        got, out = self.resolve("2", cams)
        self.assertEqual(got, 2)
        self.assertIn("#2", out)          # and it says which one it took

    def test_a_hash_forces_the_position_for_that_config(self):
        cams = [{"name": "Gate"}, {"name": "Roof"}, {"name": "2"}]
        self.assertEqual(self.resolve("#2", cams)[0], 1)

    def test_an_empty_target_asks_for_one(self):
        got, out = self.resolve("")
        self.assertIsNone(got)
        self.assertIn("-e:Doorbell", out)

    def test_no_cameras_at_all_is_said_plainly(self):
        got, out = self.resolve("1", [])
        self.assertIsNone(got)
        self.assertIn("No cameras are configured", out)


class TestStripColon(unittest.TestCase):

    def test_the_documented_form_survives_argparse(self):
        # argparse hands a short option everything attached to it, so
        # -e:Doorbell arrives as ":Doorbell".
        self.assertEqual(setup.strip_colon(":Doorbell"), "Doorbell")

    def test_the_ordinary_form_is_untouched(self):
        self.assertEqual(setup.strip_colon("Doorbell"), "Doorbell")
        self.assertEqual(setup.strip_colon("2"), "2")


class TestCameraActions(unittest.TestCase):
    """The menu's actions, reached directly.

    Same bias as TestCameraManagement: these are the paths that can silently
    strand captured frames, and now they can be reached in one command with
    no list on screen first.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = {"paths": {"frames_root": str(self.tmp)},
                    "capture": {"timeout_seconds": 4},
                    "cameras": [
                        {"name": "Gate", "enabled": True, "method": "http",
                         "url": "http://192.0.2.1/snap", "auth": "none"},
                        {"name": "Roof", "enabled": False, "method": "http",
                         "url": "http://192.0.2.2/snap", "auth": "none"}]}

    def tearDown(self):
        setup._TTY, setup.AUTO = None, False

    def act(self, action, target=None, keys=""):
        setup.AUTO = False
        setup._TTY = FakeTTY(keys, tty=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            changed, okay = setup.camera_action(self.cfg, action, target)
        return changed, okay, buf.getvalue()

    def names(self):
        return [(c["name"], c.get("enabled", True))
                for c in self.cfg["cameras"]]

    def test_toggle_disables_an_enabled_camera(self):
        changed, okay, _ = self.act("toggle", "Gate", "y\n")
        self.assertEqual((changed, okay), (True, True))
        self.assertEqual(self.names(), [("Gate", False), ("Roof", False)])

    def test_toggle_enables_a_disabled_camera_without_asking(self):
        # Enabling strands nothing, so there is nothing to warn about.
        changed, okay, out = self.act("toggle", "2")
        self.assertEqual((changed, okay), (True, True))
        self.assertEqual(self.names(), [("Gate", True), ("Roof", True)])
        self.assertNotIn("un-encoded", out)

    def test_disabling_still_warns_about_stranded_frames(self):
        (self.tmp / "Gate" / "2020-01-01").mkdir(parents=True)
        changed, okay, out = self.act("toggle", "Gate", "n\n")
        self.assertIn("1 un-encoded day", out)
        # Declined is not a failure, and nothing was written.
        self.assertEqual((changed, okay), (False, True))
        self.assertEqual(self.names()[0], ("Gate", True))

    def test_remove_takes_one_confirmation_and_takes_it(self):
        changed, okay, _ = self.act("remove", "Gate", "y\n")
        self.assertEqual((changed, okay), (True, True))
        self.assertEqual(self.names(), [("Roof", False)])

    def test_a_declined_removal_is_not_a_failure(self):
        changed, okay, out = self.act("remove", "Roof", "n\n")
        self.assertEqual((changed, okay), (False, True))
        self.assertIn("Nothing removed", out)
        self.assertEqual(len(self.cfg["cameras"]), 2)

    def test_an_unknown_target_fails_without_touching_the_config(self):
        before = self.names()
        changed, okay, _ = self.act("remove", "Nope", "y\n")
        self.assertEqual((changed, okay), (False, False))
        self.assertEqual(self.names(), before)

    def test_test_changes_nothing_even_when_the_camera_answers(self):
        # Read-only: it must not report "changed" and so must not trigger a
        # config write or a capture restart.
        with mock.patch.object(setup, "test_camera", return_value=True):
            changed, okay, _ = self.act("test", "Gate")
        self.assertEqual((changed, okay), (False, True))

    def test_a_failing_test_is_a_non_zero_result(self):
        with mock.patch.object(setup, "test_camera", return_value=False):
            changed, okay, _ = self.act("test", "Gate")
        self.assertEqual((changed, okay), (False, False))

    def test_list_prints_the_numbers_the_other_flags_take(self):
        changed, okay, out = self.act("list")
        self.assertEqual((changed, okay), (False, True))
        self.assertIn("Gate", out)
        self.assertIn("Roof", out)

    def test_add_refuses_a_name_that_is_already_taken(self):
        with mock.patch.object(setup, "add_one_camera",
                               return_value={"name": "Gate"}):
            changed, okay, out = self.act("add")
        self.assertEqual((changed, okay), (False, False))
        self.assertIn("already exists", out)
        self.assertEqual(len(self.cfg["cameras"]), 2)

    def test_add_appends_and_reports_the_new_total(self):
        with mock.patch.object(setup, "add_one_camera",
                               return_value={"name": "Shed", "enabled": True}):
            changed, okay, out = self.act("add")
        self.assertEqual((changed, okay), (True, True))
        self.assertIn("3 configured", out)

    def test_a_cancelled_add_is_not_a_failure(self):
        with mock.patch.object(setup, "add_one_camera", return_value=None):
            changed, okay, _ = self.act("add")
        self.assertEqual((changed, okay), (False, True))


class TestCameraActionDispatch(unittest.TestCase):
    """Which flag means which action, and what refuses to run headless."""

    def args(self, **kw):
        base = dict(list_only=False, cam_add=False, edit=None, toggle=None,
                    test=None, remove=None)
        base.update(kw)
        return mock.Mock(**base)

    def test_each_flag_maps_to_its_action(self):
        self.assertEqual(setup.chosen_camera_action(self.args(cam_add=True)),
                         ("add", None))
        self.assertEqual(setup.chosen_camera_action(self.args(edit="2")),
                         ("edit", "2"))
        self.assertEqual(setup.chosen_camera_action(self.args(toggle="Gate")),
                         ("toggle", "Gate"))
        self.assertEqual(setup.chosen_camera_action(self.args(test="Gate")),
                         ("test", "Gate"))
        self.assertEqual(setup.chosen_camera_action(self.args(remove="1")),
                         ("remove", "1"))
        self.assertEqual(setup.chosen_camera_action(self.args(list_only=True)),
                         ("list", None))

    def test_no_flag_means_the_menu(self):
        self.assertEqual(setup.chosen_camera_action(self.args()), (None, None))

    def test_every_action_the_menu_offers_is_reachable_by_flag(self):
        # An action added to the menu with no flag behind it is the drift this
        # catches: the two lists have to stay the same length.
        flags = {"list": {"list_only": True}, "add": {"cam_add": True},
                 "edit": {"edit": "1"}, "toggle": {"toggle": "1"},
                 "test": {"test": "1"}, "remove": {"remove": "1"}}
        self.assertEqual(set(flags), set(setup.CAMERA_ACTIONS))
        for action, kw in flags.items():
            got, _ = setup.chosen_camera_action(self.args(**kw))
            self.assertEqual(got, action)

    def test_the_writing_actions_refuse_to_run_without_a_terminal(self):
        # Accepting defaults would write a camera entry pointing at nothing.
        setup.AUTO, setup._TTY = True, None
        self.addCleanup(lambda: setattr(setup, "AUTO", False))
        for action, kw in (("add", {"cam_add": True}), ("edit", {"edit": "1"}),
                           ("toggle", {"toggle": "1"}),
                           ("remove", {"remove": "1"})):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    contextlib.redirect_stderr(buf):
                rc = setup.run_camera_action({"cameras": []},
                                             self.args(output="x", **kw))
            self.assertEqual(rc, 1, action)
            self.assertIn("needs a terminal", buf.getvalue())


class TestWebhookVerificationMarker(unittest.TestCase):
    """One test message, not two.

    install.sh runs the wizard and then the pre-flight check, so a webhook the
    wizard just verified was being tested again seconds later and two identical
    messages arrived in the channel.
    """

    URL = "https://discord.com/api/webhooks/123/abc"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg_path = self.tmp / "config.json"
        sys.path.insert(0, str(_support.SCRIPTS))
        import timelapse_test
        self.checker = timelapse_test

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_marker_is_written_on_success(self):
        setup.record_webhook_verified(self.cfg_path, self.URL)
        self.assertTrue((self.tmp / setup.WEBHOOK_MARKER).exists())

    def test_marker_does_not_contain_the_url(self):
        # The URL is a posting credential; a digest is enough to match on.
        setup.record_webhook_verified(self.cfg_path, self.URL)
        body = (self.tmp / setup.WEBHOOK_MARKER).read_text(encoding="utf-8")
        self.assertNotIn("abc", body)
        self.assertNotIn("discord.com", body)

    def test_checker_recognises_a_fresh_marker(self):
        setup.record_webhook_verified(self.cfg_path, self.URL)
        age = self.checker.webhook_verified_age(self.cfg_path, self.URL)
        self.assertGreaterEqual(age, 0)
        self.assertLess(age, 5)

    def test_a_different_webhook_is_still_tested(self):
        setup.record_webhook_verified(self.cfg_path, self.URL)
        other = "https://discord.com/api/webhooks/999/zzz"
        self.assertEqual(
            self.checker.webhook_verified_age(self.cfg_path, other), 0)

    def test_a_stale_marker_is_ignored(self):
        import time
        marker = self.tmp / setup.WEBHOOK_MARKER
        old = int(time.time()) - self.checker.WEBHOOK_MARKER_TTL - 60
        marker.write_text(f"{setup.webhook_fingerprint(self.URL)} {old}\n",
                          encoding="utf-8")
        self.assertEqual(
            self.checker.webhook_verified_age(self.cfg_path, self.URL), 0)

    def test_a_future_timestamp_is_ignored(self):
        # Clock skew must not suppress the check indefinitely.
        import time
        marker = self.tmp / setup.WEBHOOK_MARKER
        marker.write_text(
            f"{setup.webhook_fingerprint(self.URL)} {int(time.time()) + 9999}\n",
            encoding="utf-8")
        self.assertEqual(
            self.checker.webhook_verified_age(self.cfg_path, self.URL), 0)

    def test_missing_marker_means_test_it(self):
        self.assertEqual(
            self.checker.webhook_verified_age(self.cfg_path, self.URL), 0)

    def test_corrupt_marker_means_test_it(self):
        (self.tmp / setup.WEBHOOK_MARKER).write_text("garbage\n",
                                                     encoding="utf-8")
        self.assertEqual(
            self.checker.webhook_verified_age(self.cfg_path, self.URL), 0)

    def test_recording_without_a_config_path_is_a_no_op(self):
        setup.record_webhook_verified(None, self.URL)      # must not raise

    def test_fingerprint_is_stable_and_url_specific(self):
        self.assertEqual(setup.webhook_fingerprint(self.URL),
                         setup.webhook_fingerprint(self.URL))
        self.assertNotEqual(setup.webhook_fingerprint(self.URL),
                            setup.webhook_fingerprint(self.URL + "x"))


class TestNarrowStdout(unittest.TestCase):
    """Formatting must never abort the wizard."""

    def test_helper_tolerates_a_stream_without_reconfigure(self):
        # StringIO has no reconfigure(); the helper must not raise.
        prev = sys.stdout
        sys.stdout = io.StringIO()
        try:
            setup._survive_narrow_stdout()
        finally:
            sys.stdout = prev

    def test_headings_survive_an_ascii_only_stream(self):
        class AsciiOnly(io.TextIOBase):
            encoding = "ascii"

            def __init__(self):
                self.text = ""

            def write(self, s):
                s.encode("ascii")       # raises on box-drawing characters
                self.text += s
                return len(s)

        prev = sys.stdout
        sys.stdout = AsciiOnly()
        try:
            setup._survive_narrow_stdout()      # no reconfigure available
            raised = None
            try:
                setup.heading("Cameras")
            except UnicodeEncodeError as exc:
                raised = exc
        finally:
            sys.stdout = prev
        # Documents the residual risk: without reconfigure() support the
        # characters still cannot be written. Real streams have it.
        self.assertIsNotNone(raised)


class TestTransferDestinationKind(unittest.TestCase):
    """SSH guidance must appear only for the SSH option."""

    def drive(self, keystrokes):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY(keystrokes, tty=False)
        cfg = setup.default_config()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                setup.choose_transfer(cfg)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        return cfg["transfer"], buf.getvalue()

    def test_declining_leaves_transfer_disabled(self):
        transfer, _ = self.drive("n\n")
        self.assertFalse(transfer["enabled"])

    def test_ssh_option_mentions_keys(self):
        transfer, out = self.drive("y\n3\nuser@nas:/vol/tl/\n")
        self.assertEqual(transfer["destination"], "user@nas:/vol/tl/")
        self.assertIn("SSH key", out)

    def test_ssh_option_never_requires_a_mountpoint(self):
        # There is no local mount involved in an rsync-over-SSH transfer.
        transfer, _ = self.drive("y\n3\nuser@nas:/vol/tl/\n")
        self.assertFalse(transfer["require_mountpoint"])

    def test_the_wizard_offers_to_mount_a_share_itself(self):
        # The point of a wizard: it must not tell you to go mount the share
        # first and come back.
        _, out = self.drive("y\n2\n/tmp/nonexistent-dest/\nn\n")
        menu = out.split("How is the destination reached?", 1)[0]
        self.assertIn("set it up for me", menu)
        self.assertIn("SMB/CIFS", menu)

    def test_cifs_option_refuses_politely_without_root(self):
        # Mounting needs root; without it, say so and offer the alternative
        # rather than failing obscurely.
        if setup.is_root():
            self.skipTest("running as root")
        transfer, out = self.drive("y\n1\n")
        self.assertIn("root", out)
        self.assertFalse(transfer["enabled"],
                         "an unconfigured share must not leave transfer on")

    @staticmethod
    def _after_the_choice(out):
        """Text shown once a transport has been picked.

        The menu legitimately describes both options, SSH included. What must
        not happen is SSH guidance after choosing a local path.
        """
        marker = "How is the destination reached?"
        return out.split(marker, 1)[-1]

    def test_local_option_does_not_mention_ssh_keys(self):
        # The bug this class exists for: the old prompt talked about SSH keys
        # whichever transport you picked.
        _, out = self.drive("y\n1\n/tmp/nonexistent-dest/\nn\n")
        self.assertNotIn("SSH key", self._after_the_choice(out))

    def test_the_menu_describes_every_transport(self):
        _, out = self.drive("y\n2\n/tmp/nonexistent-dest/\nn\n")
        menu = out.split("How is the destination reached?", 1)[0]
        self.assertIn("SMB/CIFS", menu)
        self.assertIn("mounted yourself", menu)
        self.assertIn("SSH", menu)


class TestExplainPayload(unittest.TestCase):
    """Cameras answer 200 OK with an error body; surface what they said."""

    def test_reolink_error_shape(self):
        body = (b'[{"cmd":"Snap","code":1,'
                b'"error":{"detail":"login failed","rspCode":-7}}]')
        _, reason = setup.explain_payload(body)
        self.assertEqual(reason, "login failed")

    def test_reolink_please_login_first(self):
        body = (b'[{"cmd":"Snap","code":1,'
                b'"error":{"detail":"please login first","rspCode":-6}}]')
        self.assertEqual(setup.explain_payload(body)[1], "please login first")

    def test_object_rather_than_list(self):
        self.assertEqual(
            setup.explain_payload(b'{"error":{"detail":"no permission"}}')[1],
            "no permission")

    def test_string_error_field(self):
        self.assertEqual(
            setup.explain_payload(b'{"error":"unauthorized"}')[1],
            "unauthorized")

    def test_html_login_page_has_no_structured_reason(self):
        head, reason = setup.explain_payload(b"<html><body>Login</body></html>")
        self.assertIsNone(reason)
        self.assertIn("html", head)

    def test_binary_junk_does_not_raise(self):
        head, reason = setup.explain_payload(bytes(range(256)))
        self.assertIsNone(reason)
        self.assertIsInstance(head, str)

    def test_head_is_bounded_and_single_line(self):
        head, _ = setup.explain_payload(b"x\n" * 500)
        self.assertLessEqual(len(head), 160)
        self.assertNotIn("\n", head)

    def test_round_trips_through_a_reolink_style_url(self):
        secret = "p@ss&w=rd#1"
        template = ("http://{ip}/cgi-bin/api.cgi?cmd=Snap&channel=0"
                    "&user={user}&password={password}")
        url = template.format(ip="192.0.2.1", user=setup.quote("admin"),
                              password=setup.quote(secret))
        parsed = parse_qs(urlparse(url).query)
        self.assertEqual(parsed["password"], [secret])
        self.assertEqual(parsed["cmd"], ["Snap"])


class TestHumanReadable(unittest.TestCase):

    def test_bytes_and_kilobytes_have_no_decimal(self):
        self.assertEqual(setup.human(512), "512 B")
        self.assertEqual(setup.human(2048), "2 KB")

    def test_larger_units_get_one_decimal(self):
        self.assertEqual(setup.human(1024 ** 3), "1.0 GB")
        self.assertEqual(setup.human(int(1.5 * 1024 ** 4)), "1.5 TB")

    def test_petabytes(self):
        self.assertEqual(setup.human(1024 ** 5), "1.0 PB")

    def test_caps_at_petabytes(self):
        # Nothing above PB, so an exabyte reports as 1024 PB rather than
        # running off the end of the unit list and returning None.
        self.assertEqual(setup.human(1024 ** 6), "1024.0 PB")


class TestWebLibraryPreview(unittest.TestCase):
    """The wizard shows where videos will be read from, because that is the
    surprising part: transfer moves them away with --remove-source-files, so
    the answer is usually the destination and not video_output."""

    def cfg(self, **kw):
        c = setup.default_config()
        c["paths"]["video_output"] = "/var/lib/timelapse/videos"
        c.setdefault("web", {})
        c["transfer"].update(kw.pop("transfer", {}))
        c["web"].update(kw.pop("web", {}))
        return c

    def test_transfer_disabled_shows_video_output(self):
        where, why = setup.web_library_preview(
            self.cfg(transfer={"enabled": False}))
        self.assertEqual(where, "/var/lib/timelapse/videos")
        self.assertIn("video_output", why)

    def test_enabled_transfer_shows_the_destination(self):
        where, why = setup.web_library_preview(self.cfg(
            transfer={"enabled": True, "destination": "/mnt/nas/tl/"}))
        self.assertEqual(where, "/mnt/nas/tl/")
        self.assertIn("destination", why)

    def test_remote_destination_is_called_out(self):
        _, why = setup.web_library_preview(self.cfg(
            transfer={"enabled": True, "destination": "user@nas:/vol/tl/"}))
        self.assertIn("REMOTE", why)

    def test_rsync_url_is_remote_too(self):
        _, why = setup.web_library_preview(self.cfg(
            transfer={"enabled": True, "destination": "rsync://nas/tl"}))
        self.assertIn("REMOTE", why)

    def test_a_path_with_a_colon_is_not_remote(self):
        _, why = setup.web_library_preview(self.cfg(
            transfer={"enabled": True, "destination": "/mnt/odd:name/tl/"}))
        self.assertNotIn("REMOTE", why)

    def test_override_wins(self):
        where, why = setup.web_library_preview(self.cfg(
            transfer={"enabled": True, "destination": "user@nas:/vol/tl/"},
            web={"library_root": "/mnt/nas/tl/"}))
        self.assertEqual(where, "/mnt/nas/tl/")
        self.assertIn("library_root", why)


class TestChooseWeb(unittest.TestCase):

    # The wizard now asks the kernel about the address it was given. Stubbed
    # here so these tests describe the wizard rather than the machine running
    # them: a real lan_address() would make the expected default whatever the
    # CI runner happens to be numbered. The probes themselves are covered by
    # TestBindProbe against the two addresses whose behaviour is universal.
    LAN = "192.168.1.50"

    def drive(self, keystrokes, cfg=None, checks=None, lan=LAN):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY(keystrokes, tty=False)
        cfg = cfg or setup.default_config()
        buf = io.StringIO()
        # Keyed by address, not a call sequence: suggest_bind() probes too, so
        # a positional list would make these tests depend on how many times
        # the wizard happens to ask.
        def fake_check(addr, port):
            return (checks or {}).get(addr, ("ok", ""))

        try:
            with contextlib.redirect_stdout(buf), \
                    mock.patch.object(setup, "check_bind", fake_check), \
                    mock.patch.object(setup, "lan_address", lambda: lan), \
                    mock.patch.object(setup, "host_addresses",
                                      lambda: [lan, "127.0.0.1"]):
                setup.choose_web(cfg)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        return cfg.get("web", {}), buf.getvalue()

    def test_declining_leaves_it_disabled(self):
        web, _ = self.drive("n\n")
        self.assertFalse(web["enabled"])

    def test_it_is_off_by_default(self):
        # A network-facing service must be opt-in, so Enter means no.
        web, _ = self.drive("\n")
        self.assertFalse(web["enabled"])

    def test_accepting_defaults_binds_to_the_lan_address(self):
        # A status page reachable only from the machine it describes is close
        # to useless on a headless recorder, so the LAN address is offered.
        web, _ = self.drive("y\n\n\n\n\n")
        self.assertTrue(web["enabled"])
        self.assertEqual(web["bind"], self.LAN)
        self.assertEqual(web["port"], 8787)
        self.assertEqual(web["state_dir"], "/var/lib/timelapse/web")

    def test_wildcard_is_the_fallback_when_there_is_no_lan_address(self):
        web, _ = self.drive("y\n\n\n\n\n", lan="")
        self.assertEqual(web["bind"], "0.0.0.0")

    def test_loopback_can_still_be_chosen(self):
        web, _ = self.drive("y\n127.0.0.1\n\n\n\n")
        self.assertEqual(web["bind"], "127.0.0.1")

    def test_the_lack_of_auth_is_stated_before_the_bind_prompt(self):
        _, out = self.drive("y\n\n\n\n\n")
        before = out.split("Listen on", 1)[0]
        self.assertIn("no login", before)

    def test_a_non_loopback_bind_is_warned_about(self):
        web, out = self.drive("y\n0.0.0.0\n\n\n\n")
        self.assertEqual(web["bind"], "0.0.0.0")
        self.assertIn("reverse proxy", out)

    def test_loopback_gets_no_scare_warning(self):
        _, out = self.drive("y\n127.0.0.1\n\n\n\n")
        self.assertNotIn("reverse proxy", out)

    def test_moving_an_existing_loopback_bind_is_called_out(self):
        # The default moved from 127.0.0.1 to the LAN address, so pressing
        # Enter through the wizard would otherwise quietly expose an install
        # that had deliberately been kept local.
        cfg = setup.default_config()
        cfg["web"] = {"enabled": True, "bind": "127.0.0.1", "port": 8787}
        _, out = self.drive("y\n\n\n\n\n", cfg)
        before = out.split("Listen on", 1)[0]
        self.assertIn("reachable only from this host", before)
        self.assertIn("opens it to your network", before)

    def test_an_address_this_host_lacks_is_refused_and_reasked(self):
        # The bug this exists for: the service starts, logs the address it is
        # serving, and is unreachable. Nothing says the address was wrong.
        web, out = self.drive(
            "y\n10.9.9.9\n192.168.1.50\n\n\n\n",
            checks={"10.9.9.9": ("unavailable", "no interface has 10.9.9.9")})
        self.assertEqual(web["bind"], "192.168.1.50")
        self.assertIn("no interface has 10.9.9.9", out)
        self.assertIn("unreachable", out)

    def test_a_port_in_use_is_accepted_with_a_note(self):
        # Almost always the web UI itself, about to be restarted. Refusing to
        # let someone change the port of a running server would be absurd.
        web, out = self.drive(
            "y\n\n\n\n\n",
            checks={self.LAN: ("in-use", "something is already listening")})
        self.assertEqual(web["bind"], self.LAN)
        self.assertIn("already listening", out)

    def test_a_privileged_port_is_refused(self):
        # The probe would pass as root and prove nothing: the service runs
        # unprivileged.
        web, out = self.drive("y\n\n80\n8787\n\n\n")
        self.assertEqual(web["port"], 8787)
        self.assertIn("privileged", out)

    def test_it_says_where_videos_will_come_from(self):
        cfg = setup.default_config()
        cfg["transfer"].update({"enabled": True, "destination": "/mnt/nas/tl/"})
        _, out = self.drive("y\n\n\n\n\n", cfg)
        self.assertIn("/mnt/nas/tl/", out)

    def test_a_remote_destination_is_flagged_during_setup(self):
        cfg = setup.default_config()
        cfg["transfer"].update({"enabled": True,
                                "destination": "user@nas:/vol/tl/"})
        _, out = self.drive("y\n\n\n\n\n", cfg)
        self.assertIn("not a path this host can read", out)

    def test_blank_library_root_means_work_it_out(self):
        web, _ = self.drive("y\n\n\n\n\n")
        self.assertEqual(web["library_root"], "")

    def test_the_update_check_defaults_on(self):
        web, out = self.drive("y\n\n\n\n\n\n")
        self.assertTrue(web["update_check"])
        # Consent means saying what it does before asking.
        before = out.split("Check GitHub for updates?", 1)[0]
        self.assertIn("api.github.com", before)
        self.assertIn("only outbound connection", before)

    def test_the_update_check_can_be_declined(self):
        web, _ = self.drive("y\n\n\n\n\nn\n")
        self.assertFalse(web["update_check"])

    def test_an_existing_choice_to_decline_is_kept(self):
        # Re-running the wizard must not quietly switch outbound traffic back
        # on for someone who turned it off.
        cfg = setup.default_config()
        cfg["web"] = {"enabled": True, "bind": "10.0.0.5", "port": 9000,
                      "update_check": False}
        web, _ = self.drive("y\n\n\n\n\n\n", cfg)
        self.assertFalse(web["update_check"])

    def test_reconfiguring_offers_the_existing_values_as_defaults(self):
        # A working bind address is kept: someone re-running the wizard to
        # change the library path must not have their address moved under them.
        cfg = setup.default_config()
        cfg["web"] = {"enabled": True, "bind": "10.0.0.5", "port": 9000,
                      "library_root": "/srv/tl", "state_dir": "/srv/idx"}
        web, _ = self.drive("y\n\n\n\n\n", cfg)
        self.assertEqual(web["bind"], "10.0.0.5")
        self.assertEqual(web["port"], 9000)
        self.assertEqual(web["library_root"], "/srv/tl")
        self.assertEqual(web["state_dir"], "/srv/idx")


class TestBindProbe(unittest.TestCase):
    """The real probes, unmocked, against the two addresses whose behaviour is
    the same on every host: loopback always binds, and TEST-NET-3 is reserved
    for documentation so nothing should ever hold it. No DNS is involved in
    any of these, so they cannot hang on a runner with a slow resolver."""

    def test_loopback_is_bindable(self):
        self.assertEqual(setup.check_bind("127.0.0.1", 0)[0], "ok")

    def test_wildcard_is_bindable(self):
        self.assertEqual(setup.check_bind("0.0.0.0", 0)[0], "ok")

    def test_an_address_this_host_lacks_is_unavailable(self):
        kind, detail = setup.check_bind("203.0.113.99", 0)
        self.assertEqual(kind, "unavailable")
        self.assertIn("203.0.113.99", detail)

    def test_junk_is_rejected_without_a_traceback(self):
        self.assertEqual(setup.check_bind("!!!", 0)[0], "bad")

    def test_empty_is_rejected(self):
        self.assertEqual(setup.check_bind("", 0)[0], "bad")

    @unittest.skipIf(sys.platform == "win32",
                     "Windows SO_REUSEADDR permits binding an active "
                     "listener, where Linux refuses; the service is Linux-only")
    def test_a_taken_port_reads_as_in_use(self):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        kind, _ = setup.check_bind("127.0.0.1", s.getsockname()[1])
        self.assertEqual(kind, "in-use")

    def test_the_probe_leaves_the_port_free(self):
        # It binds without listening and closes immediately; if it leaked the
        # socket, the service it just approved could not start.
        setup.check_bind("127.0.0.1", 8787)
        again = socket.socket()
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.addCleanup(again.close)
        again.bind(("127.0.0.1", 8787))     # raises if the probe held on

    def test_lan_address_is_never_loopback(self):
        # It may legitimately be "" on a host with no default route; what it
        # must never be is the 127.0.1.1 that gethostname() would have given.
        addr = setup.lan_address()
        self.assertFalse(addr.startswith("127."))

    def test_suggest_keeps_a_working_address(self):
        with mock.patch.object(setup, "lan_address", lambda: "192.168.1.50"), \
                mock.patch.object(setup, "check_bind", lambda a, p: ("ok", "")):
            self.assertEqual(setup.suggest_bind("10.0.0.5"), "10.0.0.5")

    def test_suggest_keeps_the_wildcard(self):
        # Unmocked: 0.0.0.0 binds on any host, so this pins the same rule
        # without asserting anything about the machine running the test.
        with mock.patch.object(setup, "lan_address", lambda: "192.168.1.50"):
            self.assertEqual(setup.suggest_bind("0.0.0.0"), "0.0.0.0")

    def test_suggest_replaces_junk(self):
        with mock.patch.object(setup, "lan_address", lambda: "192.168.1.50"):
            self.assertEqual(setup.suggest_bind("!!!"), "192.168.1.50")

    def test_suggest_replaces_loopback(self):
        with mock.patch.object(setup, "lan_address", lambda: "192.168.1.50"):
            self.assertEqual(setup.suggest_bind("127.0.0.1"), "192.168.1.50")

    def test_suggest_replaces_an_address_the_host_lost(self):
        # A NIC renumbered since the last run must not be offered again.
        with mock.patch.object(setup, "lan_address", lambda: "192.168.1.50"):
            self.assertEqual(setup.suggest_bind("203.0.113.99"), "192.168.1.50")

    def test_suggest_falls_back_to_the_wildcard(self):
        with mock.patch.object(setup, "lan_address", lambda: ""):
            self.assertEqual(setup.suggest_bind(""), "0.0.0.0")


class TestRestartWeb(unittest.TestCase):
    """`systemctl enable --now` is a no-op on an already-active unit, so the
    wizard used to print a new bind address while the running process kept
    serving the old one. That shipped, and looked like the UI refusing
    connections on an address the wizard had just called correct."""

    def drive(self, keystrokes, enabled=True, active=True):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY(keystrokes, tty=False)
        cfg = {"web": {"enabled": enabled}}
        buf = io.StringIO()
        restarted = []
        try:
            with contextlib.redirect_stdout(buf), \
                    mock.patch.object(setup, "unit_is_active",
                                      lambda unit: active), \
                    mock.patch.object(setup, "restart_unit",
                                      lambda unit, msg: restarted.append(unit)):
                setup.restart_web_if_running(cfg)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        return restarted, buf.getvalue()

    def test_a_running_service_is_restarted(self):
        restarted, _ = self.drive("y\n")
        self.assertEqual(restarted, ["timelapse-web.service"])

    def test_enter_accepts_the_restart(self):
        # The whole point is that the common path applies the change.
        restarted, _ = self.drive("\n")
        self.assertEqual(restarted, ["timelapse-web.service"])

    def test_declining_says_the_settings_are_not_live(self):
        restarted, out = self.drive("n\n")
        self.assertEqual(restarted, [])
        self.assertIn("previous settings", out)
        self.assertIn("systemctl restart", out)

    def test_a_stopped_service_is_told_how_to_start(self):
        restarted, out = self.drive("", active=False)
        self.assertEqual(restarted, [])
        self.assertIn("enable --now", out)

    def test_disabling_offers_to_stop_a_running_service(self):
        # It exits 0 when disabled, so a restart is what stops it. Left alone
        # it would keep serving a UI the operator just turned off.
        restarted, out = self.drive("y\n", enabled=False)
        self.assertEqual(restarted, ["timelapse-web.service"])
        self.assertIn("still running", out)

    def test_a_stopped_and_disabled_service_says_nothing(self):
        restarted, out = self.drive("", enabled=False, active=False)
        self.assertEqual(restarted, [])
        self.assertNotIn("enable --now", out)

    def test_no_systemd_means_no_prompt(self):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY("", tty=False)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                    mock.patch.object(setup, "unit_is_active", lambda u: None):
                setup.restart_web_if_running({"web": {"enabled": True}})
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        self.assertEqual(buf.getvalue(), "")


class TestLoadExistingConfig(unittest.TestCase):
    """`timelapse cameras` without sudo said "No existing config" about a file
    that exists, and told the operator to run the full wizard, which would
    have offered to overwrite the config they could not read."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = Path(self.tmp) / "config.json"

    def run_load(self, path=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            got = setup.load_existing_config(str(path or self.path))
        return got, buf.getvalue()

    def test_a_readable_config_is_returned(self):
        self.path.write_text('{"cameras": []}', encoding="utf-8")
        got, _ = self.run_load()
        self.assertEqual(got, {"cameras": []})

    def test_a_missing_file_says_to_run_setup(self):
        got, out = self.run_load()
        self.assertIsNone(got)
        self.assertIn("No config at", out)
        self.assertIn("timelapse setup", out)

    def test_permission_denied_says_to_use_sudo(self):
        # The reported case. It must not be reported as absence, and must not
        # send the operator at the full wizard.
        self.path.write_text('{"cameras": []}', encoding="utf-8")
        with mock.patch("builtins.open", side_effect=PermissionError(13, "no")):
            got, out = self.run_load()
        self.assertIsNone(got)
        self.assertIn("permission denied", out.lower())
        self.assertIn("sudo", out)
        self.assertNotIn("No config at", out)
        self.assertNotIn("run the full wizard", out)

    def test_malformed_json_says_so(self):
        # Previously an uncaught ValueError, so a stray comma was a traceback.
        self.path.write_text("{ not json", encoding="utf-8")
        got, out = self.run_load()
        self.assertIsNone(got)
        self.assertIn("not valid JSON", out)

    def test_other_os_errors_are_reported_as_themselves(self):
        with mock.patch("builtins.open", side_effect=IsADirectoryError(21, "dir")):
            got, out = self.run_load()
        self.assertIsNone(got)
        self.assertIn("Cannot read", out)


class TestWebStateDir(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_created_when_enabled(self):
        target = Path(self.tmp) / "idx"
        cfg = setup.default_config()
        cfg["web"] = {"enabled": True, "state_dir": str(target)}
        setup.create_web_state_dir(cfg)
        self.assertTrue(target.is_dir())

    def test_created_even_when_disabled(self):
        """Deliberate. Someone who flips web.enabled by hand and starts the
        unit would otherwise meet a mount-namespace error naming neither this
        directory nor the setting that wants it."""
        target = Path(self.tmp) / "idx"
        cfg = setup.default_config()
        cfg["web"] = {"enabled": False, "state_dir": str(target)}
        setup.create_web_state_dir(cfg)
        self.assertTrue(target.is_dir())

    def test_the_installer_makes_it_because_the_service_cannot(self):
        """ReadWritePaths naming a missing directory stops the unit dead, and
        inside the sandbox the parent is read-only, so the service could not
        create it even if it started."""
        target = Path(self.tmp) / "a" / "b" / "idx"
        cfg = setup.default_config()
        cfg["web"] = {"enabled": True, "state_dir": str(target)}
        setup.create_web_state_dir(cfg)
        self.assertTrue(target.is_dir())

    def test_web_writable_paths_is_only_the_state_dir(self):
        """Never the frames root. Reusing writable_paths() here would hand the
        one network-facing unit write access to every captured frame."""
        cfg = setup.default_config()
        cfg["paths"]["frames_root"] = "/mnt/big/frames"
        cfg["web"] = {"enabled": True, "state_dir": "/var/lib/timelapse/web"}
        self.assertEqual(setup.web_writable_paths(cfg),
                         ["/var/lib/timelapse/web"])

    def test_web_writable_paths_falls_back_when_unset(self):
        self.assertEqual(setup.web_writable_paths({}),
                         ["/var/lib/timelapse/web"])


class TestDefaultConfig(unittest.TestCase):

    def test_builtin_default_has_every_section(self):
        cfg = setup.default_config()
        for key in ("paths", "capture", "encode", "transfer", "discord",
                    "cameras"):
            self.assertIn(key, cfg)

    def test_timeout_stays_below_the_interval(self):
        cfg = setup.default_config()
        self.assertLess(cfg["capture"]["timeout_seconds"],
                        cfg["capture"]["interval_seconds"])

    def test_shipped_example_loads_and_is_stripped_of_comments(self):
        example = _support.REPO / "config" / "config.example.json"
        cfg = setup.default_config(str(example))
        self.assertNotIn("_comment", cfg)
        self.assertNotIn("_cameras_comment", cfg)
        self.assertNotIn("_comment", cfg["paths"])
        self.assertEqual(cfg["cameras"], [],
                         "example cameras must not leak into a real config")

    def test_no_section_leaks_documentation_keys(self):
        """Every dict section, not just the ones someone remembered.

        The stripper used to walk a hardcoded list of section names, so adding
        a "web" section to the template shipped its three _comment keys
        straight into live configs. Checking one named section - which is what
        the test above did - could not catch that. This asserts the invariant
        instead of a sample of it.
        """
        example = _support.REPO / "config" / "config.example.json"
        cfg = setup.default_config(str(example))
        for name, block in cfg.items():
            self.assertFalse(name.startswith("_"), f"top-level {name} leaked")
            if isinstance(block, dict):
                leaked = [k for k in block if k.startswith("_")]
                self.assertEqual(leaked, [], f"{name} leaked {leaked}")

    def test_missing_template_falls_back_to_the_builtin(self):
        self.assertIn("paths", setup.default_config("/nonexistent.json"))


class TestFramerate(unittest.TestCase):
    """`encode.framerate` was in the schema from the beginning and read with a
    default of 60, but the wizard never asked, so the only way to change it was
    to edit the JSON by hand."""

    def ask(self, answer, cfg=None):
        cfg = cfg if cfg is not None else {"encode": {}}
        with mock.patch.object(setup, "ask_int", return_value=answer):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                setup.choose_framerate(cfg, 17280)
        return cfg, out.getvalue()

    def test_the_answer_lands_in_the_config(self):
        cfg, _ = self.ask(30)
        self.assertEqual(cfg["encode"]["framerate"], 30)

    def test_the_default_offered_is_sixty(self):
        with mock.patch.object(setup, "ask_int", return_value=60) as ask:
            with contextlib.redirect_stdout(io.StringIO()):
                setup.choose_framerate({"encode": {}}, 17280)
        self.assertEqual(ask.call_args[0][1], 60)

    def test_an_existing_value_becomes_the_default(self):
        # Re-running the wizard must offer what you already chose, not 60.
        with mock.patch.object(setup, "ask_int", return_value=24) as ask:
            with contextlib.redirect_stdout(io.StringIO()):
                setup.choose_framerate({"encode": {"framerate": 24}}, 17280)
        self.assertEqual(ask.call_args[0][1], 24)

    def test_gop_follows_the_frame_rate(self):
        # Shipped as gop 120 against framerate 60, which is two seconds. Left
        # alone, 30fps would put a keyframe every four seconds instead.
        for fps in (24, 30, 60):
            cfg, _ = self.ask(fps)
            self.assertEqual(cfg["encode"]["gop"], fps * setup.GOP_SECONDS)

    def test_the_bounds_refuse_a_zero_frame_rate(self):
        with mock.patch.object(setup, "ask_int", return_value=60) as ask:
            with contextlib.redirect_stdout(io.StringIO()):
                setup.choose_framerate({"encode": {}}, 17280)
        self.assertEqual(ask.call_args[0][2], 1)      # lo

    def test_it_says_how_long_a_day_becomes(self):
        # The reason to ask at all: the same frames are 4:48 at 60fps and
        # 9:36 at 30, and that is not obvious from a number in a JSON file.
        _, out = self.ask(30)
        self.assertIn("9:36", out)

    def test_video_length_is_minutes_and_seconds(self):
        self.assertEqual(setup.video_length(17280, 60), "4:48")
        self.assertEqual(setup.video_length(17280, 30), "9:36")
        self.assertEqual(setup.video_length(0, 60), "0:00")

    def test_video_length_survives_a_zero_rate(self):
        # Never let the wizard divide by zero while describing a bad answer.
        self.assertEqual(setup.video_length(60, 0), "1:00")


class TestConfigBackups(unittest.TestCase):
    """Every write goes through write_config(), so the rotation lives there."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "config.json"

    def write(self, marker):
        with contextlib.redirect_stdout(io.StringIO()):
            setup.write_config({"marker": marker}, self.out)

    def backup(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return setup.backup_config(self.out)

    def markers(self):
        return [json.loads(p.read_text(encoding="utf-8"))["marker"]
                for p in setup.backup_paths(self.out)]

    def test_nothing_to_back_up_is_not_an_error(self):
        self.assertIsNone(self.backup())

    def test_it_keeps_five_and_drops_the_oldest(self):
        for i in range(8):
            self.write(i)
        self.assertEqual(len(setup.backup_paths(self.out)), setup.BACKUP_KEEP)
        # The five most recent, oldest first. Write n backs up marker n-1.
        self.assertEqual(self.markers(), [2, 3, 4, 5, 6])

    def test_two_writes_in_the_same_second_are_both_kept(self):
        # The stamp has one-second resolution and two camera commands in a row
        # are well under that; the second must not replace the first.
        with mock.patch.object(setup.time, "strftime",
                               return_value="20260810-120000"):
            self.write("first")
            self.write("second")
            self.write("third")
        self.assertEqual(self.markers(), ["first", "second"])

    def test_a_backup_carries_the_config_mode(self):
        # It holds the same camera passwords the config does.
        self.write("a")
        self.write("b")
        made = setup.backup_paths(self.out)[0]
        self.assertEqual(made.stat().st_mode & 0o777,
                         self.out.stat().st_mode & 0o777)

    def test_a_failed_backup_does_not_block_the_write(self):
        # The backup exists to protect the write; it must never prevent it.
        self.write("first")
        with mock.patch.object(setup.shutil, "copy2",
                               side_effect=OSError("read-only")):
            self.write("second")
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8")),
                         {"marker": "second"})

    def test_the_bare_bak_from_older_versions_is_still_found(self):
        # 0.1.1 and earlier wrote one unstamped file. It is somebody's only
        # backup; it must appear in the list rather than being ignored.
        self.out.write_text('{"marker": "legacy"}', encoding="utf-8")
        (self.tmp / "config.json.bak").write_text('{"marker": "legacy"}',
                                                  encoding="utf-8")
        self.write("new")
        self.assertEqual(self.markers()[0], "legacy")   # oldest, sorts first

    def test_the_temp_file_is_never_mistaken_for_a_backup(self):
        self.write("a")
        (self.tmp / "config.json.tmp").write_text("{}", encoding="utf-8")
        self.assertEqual(setup.backup_paths(self.out), [])


class TestRestore(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "config.json"
        setup.AUTO, setup._TTY = False, None

    def tearDown(self):
        setup.AUTO, setup._TTY = False, None

    def write(self, cams=1, fps=60):
        cfg = {"cameras": [{"name": f"C{i}", "enabled": True}
                           for i in range(cams)],
               "capture": {"interval_seconds": 5},
               "encode": {"framerate": fps}}
        with contextlib.redirect_stdout(io.StringIO()):
            setup.write_config(cfg, self.out)

    def restore(self, keys):
        setup.AUTO = False
        setup._TTY = FakeTTY(keys, tty=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = setup.restore_config(self.out)
        return rc, buf.getvalue()

    def cams(self):
        return len(json.loads(self.out.read_text(encoding="utf-8"))["cameras"])

    def test_no_backups_is_reported_rather_than_a_traceback(self):
        rc, out = self.restore("")
        self.assertEqual(rc, 1)
        self.assertIn("No backups", out)

    def test_it_lists_newest_first(self):
        for n in (1, 2, 3):
            self.write(cams=n)
        _, out = self.restore("0\n")
        rows = [l for l in out.splitlines() if l.strip().startswith(("1 ", "2 "))]
        self.assertIn("2 camera", rows[0])      # newest backup: the 2-cam one
        self.assertIn("1 camera", rows[1])

    def test_zero_cancels_and_changes_nothing(self):
        self.write(cams=1)
        self.write(cams=2)
        rc, out = self.restore("0\n")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing was restored", out)
        self.assertEqual(self.cams(), 2)

    def test_a_declined_confirmation_changes_nothing(self):
        self.write(cams=1)
        self.write(cams=2)
        rc, _ = self.restore("1\nn\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cams(), 2)

    def test_restoring_puts_the_older_config_back(self):
        self.write(cams=1)
        self.write(cams=2)
        self.write(cams=3)
        rc, _ = self.restore("2\ny\n")      # the 1-camera one
        self.assertEqual(rc, 0)
        self.assertEqual(self.cams(), 1)

    def test_restoring_is_itself_reversible(self):
        # The point of backing up the current config first: pick the wrong one
        # and what you replaced is number 1 in the same list.
        self.write(cams=1)
        self.write(cams=5)
        self.restore("1\ny\n")
        self.assertEqual(self.cams(), 1)
        rc, _ = self.restore("1\ny\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cams(), 5)

    def test_it_works_when_the_config_is_gone_entirely(self):
        # "I deleted it" is one of the two reasons anybody runs this, so it
        # must not require a readable config first.
        self.write(cams=4)
        self.write(cams=4)
        self.out.unlink()
        rc, _ = self.restore("1\ny\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.cams(), 4)

    def test_a_corrupt_backup_is_listed_but_refused(self):
        self.write(cams=1)
        self.write(cams=2)
        (self.tmp / "config.json.bak.20260101-000000").write_text(
            "{ not json", encoding="utf-8")
        rc, out = self.restore("2\n")       # the corrupt one, by date
        self.assertIn("unreadable", out)
        self.assertEqual(rc, 1)
        self.assertEqual(self.cams(), 2)    # and nothing was touched

    def test_the_backup_matching_the_running_config_is_marked(self):
        self.write(cams=1)
        self.write(cams=1)
        _, out = self.restore("0\n")
        self.assertIn("= current", out)

    def test_choosing_needs_a_terminal(self):
        self.write(cams=1)
        self.write(cams=2)
        setup.AUTO, setup._TTY = True, None
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = setup.restore_config(self.out)
        self.assertEqual(rc, 1)
        self.assertIn("restore -l", buf.getvalue())
        self.assertEqual(self.cams(), 2)

    def test_describe_reads_a_backup_without_restoring_it(self):
        self.write(cams=2, fps=24)
        self.write(cams=2, fps=24)
        _, summary = setup.describe_backup(setup.backup_paths(self.out)[0])
        self.assertIn("2 camera", summary)
        self.assertIn("24fps", summary)
        self.assertIn("5s", summary)


class TestWriteConfig(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def write(cfg, out):
        """write_config() narrates to stdout; keep it out of the test output."""
        with contextlib.redirect_stdout(io.StringIO()):
            setup.write_config(cfg, out)

    def test_writes_valid_json(self):
        import json
        out = self.tmp / "sub" / "config.json"
        self.write({"cameras": [], "paths": {}}, out)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["cameras"],
                         [])

    def test_backs_up_an_existing_config(self):
        out = self.tmp / "config.json"
        self.write({"marker": "first"}, out)
        self.write({"marker": "second"}, out)
        backups = setup.backup_paths(out)
        self.assertEqual(len(backups), 1)
        self.assertIn("first", backups[0].read_text(encoding="utf-8"))

    def test_leaves_no_temporary_file_behind(self):
        out = self.tmp / "config.json"
        self.write({"a": 1}, out)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["config.json"])


if __name__ == "__main__":
    unittest.main()

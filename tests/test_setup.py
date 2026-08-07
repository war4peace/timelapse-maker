"""Unit tests for timelapse_setup.py: storage discovery and config shaping.

The storage scan is the part with real bug surface: it has to reject a dozen
kinds of thing that look like disks but aren't. These drive it with a synthetic
/proc/mounts so the awkward cases are always present, regardless of the machine.
"""

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
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

    def drive(self, keystrokes, cfg=None):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY(keystrokes, tty=False)
        cfg = cfg or setup.default_config()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
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

    def test_accepting_defaults_binds_to_loopback(self):
        web, _ = self.drive("y\n\n\n\n\n")
        self.assertTrue(web["enabled"])
        self.assertEqual(web["bind"], "127.0.0.1")
        self.assertEqual(web["port"], 8787)
        self.assertEqual(web["state_dir"], "/var/lib/timelapse/web")

    def test_the_lack_of_auth_is_stated_before_the_bind_prompt(self):
        _, out = self.drive("y\n\n\n\n\n")
        before = out.split("Listen on", 1)[0]
        self.assertIn("no login", before)

    def test_a_non_loopback_bind_is_warned_about(self):
        web, out = self.drive("y\n0.0.0.0\n\n\n\n")
        self.assertEqual(web["bind"], "0.0.0.0")
        self.assertIn("reverse proxy", out)

    def test_loopback_gets_no_scare_warning(self):
        _, out = self.drive("y\n\n\n\n\n")
        self.assertNotIn("reverse proxy", out)

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

    def test_reconfiguring_offers_the_existing_values_as_defaults(self):
        cfg = setup.default_config()
        cfg["web"] = {"enabled": True, "bind": "10.0.0.5", "port": 9000,
                      "library_root": "/srv/tl", "state_dir": "/srv/idx"}
        web, _ = self.drive("y\n\n\n\n\n", cfg)
        self.assertEqual(web["bind"], "10.0.0.5")
        self.assertEqual(web["port"], 9000)
        self.assertEqual(web["library_root"], "/srv/tl")
        self.assertEqual(web["state_dir"], "/srv/idx")


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
        self.assertTrue((self.tmp / "config.json.bak").exists())
        self.assertIn("first",
                      (self.tmp / "config.json.bak").read_text(encoding="utf-8"))

    def test_leaves_no_temporary_file_behind(self):
        out = self.tmp / "config.json"
        self.write({"a": 1}, out)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["config.json"])


if __name__ == "__main__":
    unittest.main()

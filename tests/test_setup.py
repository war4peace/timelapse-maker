"""Unit tests for timelapse_setup.py — storage discovery and config shaping.

The storage scan is the part with real bug surface: it has to reject a dozen
kinds of thing that look like disks but aren't. These drive it with a synthetic
/proc/mounts so the awkward cases are always present, regardless of the machine.
"""

import contextlib
import io
import shutil
import tempfile
import unittest
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

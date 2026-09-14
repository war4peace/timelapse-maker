"""Unit tests for timelapse_setup.py: the wizard's questions and config shaping.

The storage *scan* moved to timelapse_platform at 0.2.0, and its tests went
with it: /proc/mounts is the Linux answer to "which disks could hold frames"
and the platform module is where a platform answer belongs. What stays here is
what the wizard does with the answer, which is the same on either platform.
"""

import contextlib
import inspect
import io
import os
import collections
import json
import ntpath
import re
import shutil
import socket
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import _support
from _support import FakeStatVFS, write_mounts

import timelapse_encode as enc
import timelapse_platform as plat
import timelapse_setup as setup


class TestRecommend(unittest.TestCase):
    """Which disk to offer, and the OS disk is the one not to fill.

    The mountpoints below are Linux ones, so "which is the OS disk" is forced
    rather than inherited: on the Windows CI leg the real answer is C:\\ and
    every case here would silently stop testing the rule it was written for.
    """

    def setUp(self):
        patch = mock.patch.object(setup, "os_disk_mount", return_value="/")
        patch.start()
        self.addCleanup(patch.stop)

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
                          "log_dir": "/srv/tl/logs",
                          "state_dir": "/srv/tl/state"},
                "transfer": transfer or {"enabled": False}}

    def test_lists_the_four_data_directories(self):
        self.assertEqual(setup.writable_paths(self.cfg()),
                         ["/srv/tl/frames", "/srv/tl/logs", "/srv/tl/state",
                          "/srv/tl/videos"])

    def test_the_state_directory_is_included(self):
        """The daemons cannot publish state into a read-only directory.

        ProtectSystem=strict means an unlisted path is read-only, and the
        failure is a mount namespace error rather than anything naming the
        setting. Note this is a *sibling* of the others on a default install
        (/var/lib/timelapse/state), so the parent-collapse below never absorbs
        it and it genuinely has to be named.
        """
        self.assertIn("/srv/tl/state", setup.writable_paths(self.cfg()))

    def test_the_state_directory_defaults_when_the_key_is_absent(self):
        # Every config written before 0.1.6 has no such key.
        cfg = self.cfg()
        del cfg["paths"]["state_dir"]
        self.assertIn("/var/lib/timelapse/state", setup.writable_paths(cfg))

    def test_the_state_directory_is_not_the_web_index_directory(self):
        # Two different directories with confusable names. The web UI's is the
        # one directory that service may write; this one the daemons write and
        # the UI only reads.
        cfg = self.cfg()
        cfg["web"] = {"state_dir": "/var/lib/timelapse/web"}
        self.assertNotIn("/var/lib/timelapse/web", setup.writable_paths(cfg))
        self.assertNotIn("/srv/tl/state", setup.web_writable_paths(cfg))

    def test_includes_a_local_transfer_destination(self):
        # Trailing slash on purpose: the shipped example config writes the
        # destination that way, and systemd wants it normalised.
        paths = setup.writable_paths(
            self.cfg({"enabled": True, "destination": "/mnt/nas/tl/"}))
        self.assertIn("/mnt/nas/tl", paths)

    def test_normalises_trailing_and_duplicate_separators(self):
        cfg = {"paths": {"frames_root": "/srv/tl/frames/",
                         "video_output": "/srv/tl//videos",
                         "log_dir": "/srv/tl/logs/",
                         "state_dir": "/srv/tl/state/"},
               "transfer": {"enabled": False}}
        self.assertEqual(setup.writable_paths(cfg),
                         ["/srv/tl/frames", "/srv/tl/logs", "/srv/tl/state",
                          "/srv/tl/videos"])

    def test_a_trailing_slash_does_not_defeat_deduplication(self):
        cfg = {"paths": {"frames_root": "/srv/tl", "video_output": "/srv/tl/",
                         "log_dir": "/srv/tl", "state_dir": "/srv/tl/"},
               "transfer": {"enabled": False}}
        self.assertEqual(setup.writable_paths(cfg), ["/srv/tl"])

    def test_ignores_a_remote_transfer_destination(self):
        # rsync over SSH writes nothing locally, so systemd needs no permission.
        paths = setup.writable_paths(
            self.cfg({"enabled": True, "destination": "user@nas:/mnt/tl"}))
        self.assertEqual(len(paths), 4)

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
        self.assertEqual(len(setup.writable_paths(cfg)), 3)

    def test_a_similar_prefix_is_not_treated_as_a_parent(self):
        # /srv/tl-old must not be swallowed by /srv/tl.
        cfg = {"paths": {"frames_root": "/srv/tl",
                         "video_output": "/srv/tl-old",
                         "log_dir": "/srv/tl", "state_dir": "/srv/tl"},
               "transfer": {"enabled": False}}
        self.assertEqual(setup.writable_paths(cfg), ["/srv/tl", "/srv/tl-old"])


class TestUnitPathQuoting(unittest.TestCase):
    """A path with a space in it must reach systemd as one entry.

    `/media/<user>/<Label>` automounts carry spaces routinely and the wizard
    proposes mountpoints from its own disk scan, so this is reachable by
    pressing Enter. Unquoted, systemd split `/media/war/My Passport/frames`
    into `/media/war/My` (does not exist) and `Passport/frames` (not absolute,
    silently dropped), and an entry that does not exist stops the unit with a
    mount namespace error: both daemons, on a fresh install, with a message
    naming neither the path nor the space.

    Every expected spelling below was measured against systemd 255 rather than
    read out of the manual; see temp/c2_quoting_readback.sh and the docstring
    on quote_unit_path().
    """

    def test_a_space_is_kept_in_one_entry(self):
        self.assertEqual(setup.quote_unit_path("/media/war/My Passport/frames"),
                         '"/media/war/My Passport/frames"')

    def test_a_plain_path_is_quoted_too(self):
        # Quoting one that needs none is free: systemd prints those back
        # unquoted. A rule with an exception is a rule somebody has to
        # remember, and this one is applied by two front doors.
        self.assertEqual(setup.quote_unit_path("/srv/tl"), '"/srv/tl"')

    def test_a_backslash_is_doubled(self):
        # Measured: inside double quotes systemd eats a lone backslash, so
        # "/srv/back\\slash" arrived as /srv/backslash.
        self.assertEqual(setup.quote_unit_path("/srv/back\\slash"),
                         '"/srv/back\\\\slash"')

    def test_a_double_quote_is_escaped(self):
        self.assertEqual(setup.quote_unit_path('/srv/say"q"'),
                         '"/srv/say\\"q\\""')

    def test_a_percent_is_doubled(self):
        # Measured, and the surprise of the set: % is a specifier prefix and is
        # expanded *inside* the quotes, so "/srv/fifty%full" came back as
        # /srv/fifty/c2readull, %f being the unit's own file name.
        self.assertEqual(setup.quote_unit_path("/srv/fifty%full"),
                         '"/srv/fifty%%full"')

    def test_the_escapes_do_not_escape_each_other(self):
        # Backslash first, or the backslash added by the quote escape would
        # itself be doubled and the quote would end the string after all.
        self.assertEqual(setup.quote_unit_path('/srv/a\\b"c%d'),
                         '"/srv/a\\\\b\\"c%%d"')

    def test_the_value_is_the_quoted_entries_joined(self):
        self.assertEqual(
            setup.unit_path_value(["/media/x/My Disk/f", "/srv/tl/state"]),
            '"/media/x/My Disk/f" "/srv/tl/state"')

    def test_no_paths_is_the_empty_string(self):
        # sync_unit_readwritepaths() reads falsy as "the config names no
        # writable paths", which is one of its six answers.
        self.assertEqual(setup.unit_path_value([]), "")

    def test_the_config_path_reaches_the_value(self):
        """The join is not the seam that matters; the emitter is.

        writable_paths() is still a list of paths, deliberately, because its
        own logic collapses children into parents by prefix and quotes would
        break that comparison. The rendering happens after.
        """
        cfg = {"paths": {"frames_root": "/media/war/My Passport/frames",
                         "video_output": "/media/war/My Passport/videos",
                         "log_dir": "/var/log/tl", "state_dir": "/srv/state"},
               "transfer": {"enabled": False}}
        value = setup.unit_path_value(setup.writable_paths(cfg))
        self.assertIn('"/media/war/My Passport/frames"', value)
        self.assertIn('"/media/war/My Passport/videos"', value)
        # Four entries, not six: the count is what the bug destroyed.
        self.assertEqual(value.count('"'), 8)


class TestTheInstallerActsOnThePreflight(unittest.TestCase):
    """It ran the checker and ignored what it said, twice over.

    `... || warn "Pre-flight reported problems"` could only fire on a
    traceback, because the checker returned 0 whatever it found; and the next
    question, "Enable capture and the nightly encode now?", defaulted to yes
    regardless. So an install where every camera was refusing could report the
    problem and enable the services in the same breath.

    A source scan is what a unit test can do here. The end to end half is
    `temp/h8_install_check.sh`, on real systemd.
    """

    def setUp(self):
        self.source = (_support.REPO / "install.sh").read_text(
            encoding="utf-8")

    def body(self):
        """The Next steps section, which is where both questions live."""
        return self.source.split('step "Next steps"', 1)[1]

    def test_the_enable_question_takes_a_variable_default(self):
        """Not a literal y. The default is the whole mechanism: the question
        is still asked, because the operator may know the camera is unplugged
        today and want the services on anyway."""
        self.assertIn(
            'ask_yn "Enable capture and the nightly encode now?" '
            '"$enable_default"', self.body())

    def test_a_failing_pre_flight_flips_that_default(self):
        self.assertIn('enable_default="n"', self.body())

    def test_it_still_defaults_to_yes_otherwise(self):
        self.assertIn('local enable_default="y"', self.body())

    def test_the_warning_is_still_printed(self):
        self.assertIn("Pre-flight reported problems", self.body())


class TestTheInstallerUsesTheEmitter(unittest.TestCase):
    """install.sh splices what Python hands it, so both halves have to agree.

    The rule lives in one place on purpose, which is the same argument that
    deleted tools/ and that keeps registration out of install.ps1: two
    implementations of an escaping rule is one implementation and one
    liability. What a source scan can check is that the shell half asks.
    """

    def setUp(self):
        self.source = (_support.REPO / "install.sh").read_text(encoding="utf-8")

    def test_the_installer_does_not_quote_anything_itself(self):
        # A printf that wraps a path in quotes here would be the second
        # implementation. The one place allowed to know the spelling is the
        # fallback for python3 being unrunnable, and it deliberately produces
        # the bare path rather than a guess at the escaping.
        body = self.source.split("quote_unit_path() {")[1].split("}")[0]
        self.assertIn("--quote-unit-path", body)
        self.assertNotIn('\\"', body)

    def test_the_shell_no_longer_writes_a_unit_at_all(self):
        """L46: there is one implementation, and it is not this one.

        install.sh carried four sed blocks doing what timelapse_setup.py also
        did, so the two disagreed the moment either grew a rule, and sed
        cannot quote a path in any case: `&` becomes the matched text, a
        backslash is an escape and `|` ends the expression, which is exactly
        what C2 needed to fix.
        """
        self.assertNotIn("ReadWritePaths=$rw", self.source)
        self.assertNotIn("s|^ExecStart=", self.source)
        self.assertIn("--sync-units", self.source)

    def test_it_still_makes_the_directories_a_unit_will_name(self):
        # What only the installer can do, and what it kept: systemd refuses to
        # start a unit whose ReadWritePaths points at a directory that is not
        # there, and the service cannot create its own, because by then its
        # parent is read-only to it.
        self.assertIn("--print-state-path", self.source)
        self.assertIn("--print-web-paths", self.source)

    def test_the_directories_themselves_are_made_from_the_bare_paths(self):
        # The other half of the same split: a directory whose name contains
        # quote marks is not what anybody asked for.
        for bare in ("staterw", "webrw"):
            self.assertIn('install -d -m 0750 "$%s"' % bare, self.source)

    def test_the_list_never_passes_through_a_shell_variable(self):
        """The defect C2 was, and the reason L46 finished it.

        A list of paths cannot survive a shell variable once one entry has a
        space in it: there is no spelling of `ReadWritePaths=$rw` that splits
        on the separators and not on the spaces. install.sh spliced that value
        until 0.2.1; now it does not build one at all, and the only strings it
        still asks Python for are single directories it is about to create.
        """
        self.assertNotIn("$rw", self.source)
        self.assertNotIn("--print-paths", self.source)
        import timelapse_setup as setup
        cfg = {"paths": {"frames_root": "/media/My Disk/frames",
                         "video_output": "/media/My Disk/videos",
                         "log_dir": "/var/log/tl",
                         "state_dir": "/srv/state"},
               "transfer": {"enabled": False}}
        value = setup.unit_writable_value(cfg, "data")
        self.assertIn('"/media/My Disk/frames"', value)
        self.assertEqual(value.count('"'), 8)

    def test_the_flag_answers_and_prompts_for_nothing(self):
        # Through main(), because the filter has to return before init_tty():
        # install.sh calls it non-interactively and an installer that can block
        # on a question nobody can see is an installer that hangs. init_tty is
        # patched to raise for exactly that reason.
        out = io.StringIO()
        argv = ["timelapse_setup.py", "--quote-unit-path", "/media/x/My Disk/f"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(setup, "init_tty", mock.Mock(
                    side_effect=AssertionError("asked for a terminal"))), \
                contextlib.redirect_stdout(out):
            rc = setup.main()
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), '"/media/x/My Disk/f"')


class TestOpeningTheVideosBackUp(unittest.TestCase):
    """Only undo the restriction this program applied, and only for videos.

    install.ps1 closes the whole data tree so that `config.json` beside it is
    not readable by every account on the machine. By default the finished
    videos are inside that tree, so the operator cannot double-click their own
    timelapse: the product's output, not its administration, and an elevated
    prompt is no answer to it.

    Two conditions and both are the rule. Inside the config directory, because
    a video_output on another drive was chosen by the operator, is already
    readable, and is not ours to change permissions on. And videos only:
    frames are transient and nobody browses a hundred thousand of them, while
    a log is the one place in this project a camera password has ever actually
    turned up, so opening the logs would widen the blast radius of the next
    leak.
    """

    def cfg(self, dest):
        return {"paths": {"frames_root": "/f", "video_output": dest,
                          "log_dir": "/l"}}

    def granted(self, dest, windows=True):
        """What open_video_dir() asks the platform to do, if anything."""
        asked = []
        with mock.patch.object(setup, "IS_WINDOWS", windows), \
             mock.patch.object(setup, "allow_users_read",
                               lambda p: (asked.append(p), (True, ""))[1]), \
             contextlib.redirect_stdout(io.StringIO()):
            setup.open_video_dir(self.cfg(dest))
        return asked

    def test_a_child_of_the_config_directory_is_opened(self):
        dest = str(Path(setup.CONFIG_DIR) / "videos")
        self.assertEqual(self.granted(dest), [dest])

    def test_a_path_elsewhere_is_left_alone(self):
        # Chosen by the operator, already readable, and not ours to touch.
        for dest in ("/srv/tl/videos", "D:\\timelapse\\videos"):
            self.assertEqual(self.granted(dest), [], dest)

    def test_the_config_directory_itself_is_refused(self):
        """The trap. video_output set to the data root would have this lift

        the restriction off config.json and every camera password in it, by a
        function whose whole job is to be safe. Strictly inside, never equal.
        """
        self.assertEqual(self.granted(str(setup.CONFIG_DIR)), [])
        self.assertEqual(self.granted(str(setup.CONFIG_DIR) + os.sep), [])

    def test_a_sibling_with_a_longer_name_is_not_inside(self):
        # The `/srv/tl` versus `/srv/tl-old` shape, which writable_paths()
        # already had to get right. A prefix is not a parent.
        self.assertEqual(self.granted(str(setup.CONFIG_DIR) + "-other"), [])

    def test_an_empty_destination_asks_for_nothing(self):
        self.assertEqual(self.granted(""), [])

    def test_case_variants_follow_the_platform(self):
        """Keyed on os.path.normcase, so the filesystem answers rather than a

        branch of ours: on Windows the two spellings are one directory, on
        Linux they genuinely are two and calling them one would be the error.
        Asserting the platform's own answer rather than a fixed one is the
        point of using normcase at all, and the same shape as
        TestSameFileName.test_case_variants_follow_the_platform.

        The first version of this test asserted the Windows answer and
        inherited its platform, so it passed here and failed on all three
        Linux legs: the rule this project already had for euid and privilege,
        met once more.
        """
        dest = str(Path(setup.CONFIG_DIR) / "videos")
        self.assertTrue(setup.inside_config_dir(dest))
        expected = os.path.normcase("A") == os.path.normcase("a")
        self.assertIs(setup.inside_config_dir(dest.upper()), expected)

    def test_create_directories_opens_the_videos_and_nothing_else(self):
        """The wiring, which is the part a pure test of the rule would miss.

        It has to be create_directories() and not some other place, because
        that is what follows the videos wherever the wizard puts them.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = {"paths": {"frames_root": str(tmp / "frames"),
                         "video_output": str(tmp / "videos"),
                         "log_dir": str(tmp / "logs"),
                         "state_dir": str(tmp / "state")},
               "web": {"state_dir": str(tmp / "web")}}
        asked = []
        with mock.patch.object(setup, "inside_config_dir", lambda p: True), \
             mock.patch.object(setup, "allow_users_read",
                               lambda p: (asked.append(p), (True, ""))[1]), \
             contextlib.redirect_stdout(io.StringIO()):
            setup.create_directories(cfg)
        self.assertEqual(asked, [str(tmp / "videos")])

    def test_a_failure_warns_and_does_not_stop_the_install(self):
        # The videos exist and are correct; an administrator can still open
        # them. Failing the install over a permission nicety would be worse,
        # and saying nothing would be worse still.
        out = io.StringIO()
        with mock.patch.object(setup, "inside_config_dir", lambda p: True), \
             mock.patch.object(setup, "allow_users_read",
                               lambda p: (False, "Access is denied.")), \
             contextlib.redirect_stdout(out):
            result = setup.open_video_dir(self.cfg("/anything"))
        self.assertIs(result, False)
        self.assertIn("administrator", out.getvalue().lower())

    def test_the_installer_asks_for_it_on_an_upgrade(self):
        """An upgrade never runs the wizard, so create_directories() is never

        reached and the videos of an install made before this release would
        stay closed for ever. One implementation, two doors.
        """
        source = (_support.REPO / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("--grant-video-access", source)
        body = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertIn("--grant-video-access", body)

    def test_the_flag_is_silent_when_there_is_no_config_yet(self):
        # install.ps1 calls it on a first install too, before the wizard has
        # written anything. An error there would be a failed install for a
        # file that is about to exist.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        argv = ["timelapse_setup.py", "--grant-video-access",
                str(tmp / "nothing.json")]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(setup, "init_tty", mock.Mock(
                    side_effect=AssertionError("asked for a terminal"))), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main(), 0)


class TestFstabEscaping(unittest.TestCase):
    """The same defect in the other file this program writes a path into.

    persist_cifs_mount() appends a line to /etc/fstab, which is
    whitespace-separated and has no quoting at all: the escape is octal.
    Measured with findmnt reading a file handed to it
    (temp/c2_fstab_probe.sh): a bare space in either field is "parse error at
    line 1 -- ignored", so the entry does nothing whatever while the wizard
    reports that the share will remount at boot. `//tower/My Share` is an
    ordinary Windows share name.
    """

    def test_a_space_becomes_the_octal_escape(self):
        self.assertEqual(setup.fstab_escape("//tower/My Share"),
                         "//tower/My\\040Share")

    def test_a_tab_and_a_newline_too(self):
        self.assertEqual(setup.fstab_escape("/mnt/a\tb\nc"),
                         "/mnt/a\\011b\\012c")

    def test_a_backslash_is_escaped_first(self):
        # Otherwise it would escape the backslashes the other rules add, and
        # \040 would arrive as a literal backslash followed by 040.
        self.assertEqual(setup.fstab_escape("/mnt/back\\slash here"),
                         "/mnt/back\\134slash\\040here")

    def test_an_ordinary_path_is_untouched(self):
        self.assertEqual(setup.fstab_escape("//tower/cctv"), "//tower/cctv")

    def test_the_written_line_is_escaped_in_both_fields(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        fstab = tmp / "fstab"
        fstab.write_text("# existing\n", encoding="utf-8")
        with mock.patch.object(setup, "Path", lambda p: fstab
                               if str(p) == "/etc/fstab" else Path(p)), \
                mock.patch.object(setup.shutil, "copy2", lambda *a: None), \
                mock.patch.object(setup.subprocess, "run", lambda *a, **k: None), \
                contextlib.redirect_stdout(io.StringIO()):
            setup.persist_cifs_mount("//tower/My Share", "/mnt/My Disk",
                                     "credentials=/etc/tl.cred", "3.0")
        line = [ln for ln in fstab.read_text(encoding="utf-8").splitlines()
                if ln.startswith("//tower")][0]
        self.assertIn("//tower/My\\040Share", line)
        self.assertIn("/mnt/My\\040Disk", line)
        # Six fields, which is what a parser counts. Unescaped it was eight.
        self.assertEqual(len(line.split()), 6)

    def test_an_existing_entry_is_recognised_by_its_escaped_spelling(self):
        """Otherwise a spaced mountpoint appends a duplicate on every run."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        fstab = tmp / "fstab"
        fstab.write_text("//tower/My\\040Share  /mnt/My\\040Disk  cifs  x  0  0\n",
                         encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(setup, "Path", lambda p: fstab
                               if str(p) == "/etc/fstab" else Path(p)), \
                contextlib.redirect_stdout(buf):
            setup.persist_cifs_mount("//tower/My Share", "/mnt/My Disk",
                                     "credentials=/etc/tl.cred", "3.0")
        self.assertIn("already has an entry", buf.getvalue())
        self.assertEqual(fstab.read_text(encoding="utf-8").count("cifs"), 1)


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


# One definition, in _support, where test_gui.py and test_usage.py can reach
# it too (0.2.1, L47). The name stays bound here because twenty call sites use
# it, and moving a fake is not a reason to touch twenty tests.
#
# Note the default: _support.FakeTTY is tty=False, and the copy that lived
# here was tty=True. Every site that wants a terminal says so, and the ones
# that do not now cannot reach the real getpass by accident.
FakeTTY = _support.FakeTTY


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
        # wizard_tty(), which also stands in for offer_discovery: this reaches
        # choose_cameras(), and the real one puts a WS-Discovery multicast on
        # the network of whoever is running the suite and waits three seconds
        # for answers.
        cfg = setup.default_config()
        buf = io.StringIO()
        with _support.wizard_tty(keystrokes), \
                mock.patch.object(setup, "test_camera", return_value=True), \
                contextlib.redirect_stdout(buf):
            setup.choose_cameras(cfg, expected)
        return cfg["cameras"], buf.getvalue()

    # configure? / scan? / type / name / ip / user / pass / smooth? / test? /
    # add another?
    # The scan is offered once for the whole section, not once per camera,
    # which is why TWO does not answer it a second time.
    #
    # Smoothing is answered with a bare Enter rather than an explicit "n", so
    # these also pin "off unless asked for" through the real wizard path. If
    # that default ever flips, Enter starts answering the frames question
    # instead and every keystroke after it shifts, which fails loudly here
    # rather than quietly enabling smoothing on every camera anyone adds.
    ONE = "y\nn\n1\nGate\n10.0.0.1\nadmin\npw\n\nn\n"
    TWO = ONE + "y\n1\nYard\n10.0.0.2\nadmin\npw\n\nn\n"

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


class TestTheFirstRunWizardRefusesADuplicateName(unittest.TestCase):
    """A camera's name is a directory, so two of them is one directory.

    Platform-independent and unprivileged; it declares its own terminal.
    choose_cameras() appended whatever add_one_camera() handed back with no
    check at all: the check lived in manage_cameras() and camera_action(), so
    it covered `timelapse cameras` and not the wizard everybody runs first.
    On NTFS `Gate` and `gate` are one frames directory with two cameras
    interleaving frames into one video; on Linux they are two entries writing
    into one directory.

    test_add_refuses_a_name_that_is_already_taken mocks add_one_camera
    itself, so it tests the backstop and cannot see any of this.
    """

    def drive(self, keystrokes, expected=9):
        cfg = setup.default_config()
        buf = io.StringIO()
        with _support.wizard_tty(keystrokes), \
                mock.patch.object(setup, "test_camera", return_value=True), \
                contextlib.redirect_stdout(buf):
            setup.choose_cameras(cfg, expected)
        return cfg["cameras"], buf.getvalue()

    # configure? / scan? / type / name / ip / user / pass / smooth? / test?
    FIRST = "y\nn\n1\nGate\n10.0.0.1\nadmin\npw\n\nn\n"
    # add another? / type / name ... , with the name answered twice because
    # the first answer is refused.
    SECOND = "y\n1\n%s\nYard\n10.0.0.2\nadmin\npw\n\nn\n"

    def test_a_second_camera_cannot_take_the_first_ones_name(self):
        cams, out = self.drive(self.FIRST + self.SECOND % "Gate" + "n\n")
        self.assertEqual([c["name"] for c in cams], ["Gate", "Yard"])
        self.assertIn("already exists", out)

    def test_the_case_variant_is_refused_too(self):
        """`Gate` and `gate` are one directory on NTFS, which is where two
        cameras would interleave their frames into one video. name_taken()
        has always been case-insensitive; nothing was calling it here."""
        cams, out = self.drive(self.FIRST + self.SECOND % "gate" + "n\n")
        self.assertEqual([c["name"] for c in cams], ["Gate", "Yard"])
        self.assertIn("already exists", out)

    def test_the_refusal_costs_the_name_and_nothing_else(self):
        """Refused at the prompt that asked for it, rather than after the
        address, the credentials, the smoothing and a live camera test. Two
        cameras were added, so the address was asked for exactly twice."""
        _, out = self.drive(self.FIRST + self.SECOND % "Gate" + "n\n")
        self.assertEqual(out.count("IP address or hostname"), 2)

    def test_a_distinct_name_is_still_accepted(self):
        """The control: refusing everything would pass the three above."""
        cams, out = self.drive(self.FIRST + self.SECOND % "Yard" + "n\n")
        self.assertEqual([c["name"] for c in cams], ["Gate", "Yard"])
        self.assertNotIn("already exists", out)

    def test_timelapse_cameras_add_re_asks_rather_than_giving_up(self):
        """The management path checked, but only after the camera was built,
        so a duplicate name cost every answer and ended the action. It
        re-prompts now, and the backstop it used to rely on stays."""
        cfg = {"paths": {"frames_root": "/nonexistent"},
               "capture": {"interval_seconds": 5}, "encode": {"framerate": 60},
               "cameras": [{"name": "Gate", "enabled": True, "method": "http",
                            "url": "http://192.0.2.1/s", "auth": "none"}]}
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY("1\nGate\nShed\n10.0.0.2\nadmin\npw\n\nn\n",
                             tty=False)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "test_camera", return_value=True), \
                 mock.patch.object(setup, "offer_discovery", return_value=[]):
                changed, okay = setup.camera_action(cfg, "add", None)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        self.assertEqual((changed, okay), (True, True))
        self.assertEqual([c["name"] for c in cfg["cameras"]], ["Gate", "Shed"])
        self.assertIn("already exists", buf.getvalue())
        self.assertEqual(buf.getvalue().count("IP address or hostname"), 1)

    def test_the_interactive_menu_re_asks_too(self):
        """Third call site. The other two are covered above; this one had no
        test at all, so a revert of its argument alone went unnoticed."""
        cfg = {"paths": {"frames_root": "/nonexistent"},
               "capture": {"interval_seconds": 5}, "encode": {"framerate": 60},
               "cameras": [{"name": "Gate", "enabled": True, "method": "http",
                            "url": "http://192.0.2.1/s", "auth": "none"}]}
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY("a\n1\nGate\nShed\n10.0.0.2\nadmin\npw\n\nn\nq\n",
                             tty=False)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "test_camera", return_value=True), \
                 mock.patch.object(setup, "offer_discovery", return_value=[]):
                changed = setup.manage_cameras(cfg)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        self.assertTrue(changed)
        self.assertEqual([c["name"] for c in cfg["cameras"]], ["Gate", "Shed"])
        self.assertIn("already exists", buf.getvalue())


class TestTheAuthPromptChecksItsAnswer(unittest.TestCase):
    """It listed three answers and accepted anything typed at it.

    Platform-independent and unprivileged; it declares its own terminal. The
    stakes are not the typo. The branch for "neither digest nor basic"
    *deletes* the stored username and password, and the daemon reads an
    unknown mode as none, so `digets` left a camera with nothing to present,
    401ing every tick from the next restart, and nothing to put back.

    edit_one_camera() had no test of its own before this.
    """

    def cam(self, **over):
        cam = {"name": "Gate", "enabled": True, "method": "http",
               "url": "http://192.0.2.1/snap", "auth": "digest",
               "username": "admin", "password": "hunter2"}
        cam.update(over)
        return cam

    def edit(self, keystrokes, cam=None):
        """Keystrokes run out, and every prompt after that takes its default,
        which is how these stay about the Auth question alone."""
        cam = self.cam() if cam is None else cam
        cfg = {"paths": {"frames_root": "/nonexistent"},
               "capture": {"interval_seconds": 5},
               "encode": {"framerate": 60}, "cameras": [cam]}
        prev = (setup._TTY, setup.AUTO)
        setup._TTY, setup.AUTO = FakeTTY(keystrokes, tty=False), False
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "test_camera", return_value=True), \
                 mock.patch.object(setup, "rename_camera_frames"):
                setup.edit_one_camera(cfg, cfg["cameras"], cam)
        finally:
            setup._TTY, setup.AUTO = prev
        return cam, buf.getvalue()

    def test_a_typo_is_refused_rather_than_stored(self):
        cam, out = self.edit("\n\ndigets\ndigest\n")
        self.assertEqual(cam["auth"], "digest")
        self.assertEqual(cam["password"], "hunter2")
        self.assertEqual(cam["username"], "admin")
        self.assertIn("Answer one of", out)

    def test_enter_still_keeps_the_mode_that_is_there(self):
        cam, _ = self.edit("\n\n\n")
        self.assertEqual(cam["auth"], "digest")
        self.assertEqual(cam["password"], "hunter2")

    def test_none_still_clears_the_credentials(self):
        """The control. Without it, "the password survives a typo" would be
        satisfied by a version that had stopped clearing at all, and a camera
        deliberately set to no auth would keep presenting one."""
        cam, _ = self.edit("\n\nnone\n")
        self.assertEqual(cam["auth"], "none")
        self.assertNotIn("username", cam)
        self.assertNotIn("password", cam)

    def test_a_stored_value_the_prompt_would_refuse_does_not_loop(self):
        """With no terminal ask() hands the default straight back, so a
        default outside the list would re-prompt for ever: the fix causing a
        worse outage than the defect. A config edited by hand, or written
        before this prompt checked anything, can hold one."""
        prev = (setup._TTY, setup.AUTO)
        setup._TTY, setup.AUTO = None, True
        real, asked = setup.ask, []

        def counted(question, default=""):
            # A loop would hang the run rather than fail it, and a hanging
            # test tells nobody anything.
            asked.append(question)
            if len(asked) > 4:
                raise AssertionError("%d prompts and no terminal to answer "
                                     "them" % len(asked))
            return real(question, default)

        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "ask", counted):
                answer = setup.ask_choice("Auth", setup.AUTH_MODES, "digets")
        finally:
            setup._TTY, setup.AUTO = prev
        self.assertIn(answer, setup.AUTH_MODES)
        self.assertEqual(len(asked), 1)
        self.assertIn("digets", buf.getvalue())     # named, not silently fixed

    def test_the_add_path_checks_it_too(self):
        """Custom URL is the other site, and it is the one where the answer
        decides whether a password is asked for at all."""
        cfg = {"paths": {"frames_root": "/nonexistent"},
               "capture": {"interval_seconds": 5}, "encode": {"framerate": 60},
               "cameras": []}
        custom = len(setup.CAMERA_PRESETS)          # "Custom URL" is the last
        prev = (setup._TTY, setup.AUTO)
        setup._TTY, setup.AUTO = FakeTTY(
            "%d\nGate\nn\nhttp://192.0.2.1/s\nbasick\nbasic\n" % custom,
            tty=False), False
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "test_camera", return_value=True):
                cam = setup.add_one_camera(cfg, 1)
        finally:
            setup._TTY, setup.AUTO = prev
        self.assertEqual(cam["auth"], "basic")
        self.assertIn("Answer one of", buf.getvalue())


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

    def test_sanitise_name_leaves_a_reserved_name_alone(self):
        """Deliberate, and the opposite of what it looks like.

        Quietly handing back 'Camera3' to somebody who typed 'NUL' teaches
        them nothing, and this function has no way to say why. The prompts
        refuse it with an explanation instead; this only strips characters.
        """
        self.assertEqual(setup.sanitise_name("NUL", "x"), "NUL")

    def test_a_reserved_name_is_refused_with_a_reason(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            refused = setup.reject_reserved("NUL")
        self.assertTrue(refused)
        out = buf.getvalue()
        self.assertIn("reserved device name", out)
        # It must say why a Linux operator is being refused as well, or the
        # refusal reads as arbitrary on the platform where it costs them
        # something and buys them nothing locally.
        self.assertIn("portable", out)

    def test_an_ordinary_name_is_not_refused_and_says_nothing(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertFalse(setup.reject_reserved("Driveway"))
        self.assertEqual(buf.getvalue(), "")

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

    def test_redaction_covers_the_rtsp_shape_too(self):
        # The wizard's own RTSP path produces exactly this form, and the local
        # rule only knew about `password=`, so every RTSP camera it added was
        # listed with its password in full.
        red = setup.redact_url("rtsp://admin:hunter2@192.0.2.7:554/Preview_01")
        self.assertNotIn("hunter2", red)
        self.assertIn("192.0.2.7:554/Preview_01", red)


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
        """One camera action, with the terminal and discovery both stood in.

        Four tests reach the real WS-Discovery multicast through this helper
        (measured with temp/l47_who_multicasts.py), which puts packets on the
        network of whoever is running the suite and waits three seconds for
        answers. wizard_tty() patches offer_discovery, so it is fixed here
        once rather than in each of them.
        """
        buf = io.StringIO()
        with _support.wizard_tty(keys), contextlib.redirect_stdout(buf):
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
    """SSH guidance must appear only for the SSH option.

    Linux is forced, on both legs. This whole menu is Linux-only: mounting a
    CIFS share and an rsync remote spec are mechanisms Windows does not have,
    and the Windows branch is a different set of questions tested below.

    **Privilege is declared here, not inherited**, and that is not tidiness.
    Option 1 is the SMB/CIFS branch, and as root it runs the first package
    manager it finds, writes a 0600 credentials file into /etc/timelapse,
    mkdirs a mountpoint and attempts a mount, all from keystrokes meant for a
    different question. Nobody noticed because unprivileged it refuses
    politely, and the project's own note says the Linux test bed is
    `wsl -d Ubuntu -u root`. The two stubs below are tripwires rather than
    conveniences: with is_root() False neither can be reached, so either one
    firing means a keystroke change has walked into the branch again.
    """

    def setUp(self):
        # A real directory, so the local branch runs past its "does not exist"
        # early return and the rsync probe is genuinely reached and stubbed.
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def drive(self, keystrokes):
        cfg = setup.default_config()
        buf = io.StringIO()

        def never(*_args, **_kw):
            raise AssertionError(
                "a test reached the CIFS mount path; it installs packages and "
                "writes to /etc")

        with _support.wizard_tty(keystrokes):
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "IS_WINDOWS", False), \
                 mock.patch.object(setup, "is_elevated", return_value=False), \
                 mock.patch.object(setup, "ensure_cifs_utils",
                                   side_effect=never), \
                 mock.patch.object(setup, "probe_rsync_flags",
                                   return_value=["-rt"]):
                setup.choose_transfer(cfg)
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
        """Mounting needs root; without it, say so and offer the alternative
        rather than failing obscurely.

        This used to skip itself when the suite happened to be running as
        root, which is inheriting privilege rather than declaring it: the one
        run where the branch does something is the one where nothing checked
        it. drive() forces unprivileged now and the skip is gone.
        """
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
        """The bug this class exists for: the old prompt talked about SSH keys
        whichever transport you picked.

        It answered `1`, which is the SMB/CIFS option, so what it was reading
        was a root refusal with no SSH in it: the assertion passed and the
        regression it exists for was unguarded. Answering `2` is the local
        path, and the Destination prompt is asserted so that a future menu
        reordering fails here rather than silently testing the wrong branch
        again.
        """
        transfer, out = self.drive("y\n2\n%s\n" % self.tmp)
        after = self._after_the_choice(out)
        self.assertIn("Destination path", after)
        self.assertNotIn("SSH key", after)
        self.assertEqual(transfer["destination"], str(self.tmp))

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


class TestEditingACameraDoesNotPrintItsLogin(unittest.TestCase):
    """The stored URL was offered as the default, and a default is printed.

    Measured before the fix: `Snapshot URL [rtsp://admin:hunter2@...]:`, for
    RTSP and for a Reolink `&password=` alike, from the one prompt about a
    camera that did not go through ask_secret(). M10.
    """

    RTSP = "rtsp://admin:hunter2@192.0.2.9:554/stream1"
    REOLINK = ("http://192.0.2.8/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=tl"
               "&user=admin&password=hunter2")

    def edit(self, cam, keys):
        cfg = {"paths": {"frames_root": "/nonexistent"},
               "capture": {"interval_seconds": 5},
               "encode": {"framerate": 60}, "cameras": [cam]}
        buf = io.StringIO()
        with _support.wizard_tty(keys), contextlib.redirect_stdout(buf), \
                mock.patch.object(setup, "test_camera", return_value=True), \
                mock.patch.object(setup, "rename_camera_frames"):
            setup.edit_one_camera(cfg, cfg["cameras"], cam)
        return cam, buf.getvalue()

    def rtsp(self, url=RTSP):
        return {"name": "Hallway", "enabled": True, "method": "rtsp",
                "url": url, "quality": 2}

    def test_enter_through_an_rtsp_camera_prints_no_password(self):
        # Name, address, username, password; the rest take their defaults.
        cam, out = self.edit(self.rtsp(), "\n\n\n\n")
        self.assertNotIn("hunter2", out)
        self.assertEqual(cam["url"], self.RTSP)
        self.assertIn("Stream URL", out)

    def test_a_new_address_keeps_the_login(self):
        cam, out = self.edit(self.rtsp(), "\nrtsp://192.0.2.10:554/stream1\n\n\n")
        self.assertEqual(cam["url"], "rtsp://admin:hunter2@192.0.2.10:554/stream1")
        self.assertNotIn("hunter2", out)

    def test_a_new_password_goes_into_the_address(self):
        cam, _ = self.edit(self.rtsp(), "\n\n\nnewpass\n")
        self.assertEqual(cam["url"], "rtsp://admin:newpass@192.0.2.9:554/stream1")

    def test_an_address_encoded_by_hand_survives_enter_byte_for_byte(self):
        """Rebuilt only when something changed. Rebuilding anyway would turn
        %40 into a bare @, which ends the userinfo one character early."""
        url = "rtsp://admin:p%40ss@192.0.2.9:554/stream1"
        cam, out = self.edit(self.rtsp(url), "\n\n\n\n")
        self.assertEqual(cam["url"], url)
        self.assertNotIn("p%40ss", out)

    def test_a_reolink_login_in_the_query_is_hidden_too(self):
        cam = {"name": "Drive", "enabled": True, "method": "http",
               "auth": "none", "url": self.REOLINK}
        cam, out = self.edit(cam, "\n\n\n\n\n")
        self.assertNotIn("hunter2", out)
        self.assertEqual(cam["url"], self.REOLINK)

    def test_an_address_without_a_login_is_offered_as_before(self):
        cam = {"name": "Gate", "enabled": True, "method": "http",
               "auth": "digest", "username": "admin", "password": "hunter2",
               "url": "http://192.0.2.1/snap"}
        _, out = self.edit(cam, "\n\n")
        self.assertIn("[http://192.0.2.1/snap]", out)

    def test_lifting_and_putting_back_is_a_round_trip(self):
        for url in (self.RTSP, self.REOLINK):
            bare, shape, user, password = setup.lift_url_credentials(url)
            self.assertNotIn("hunter2", bare)
            self.assertEqual((user, password), ("admin", "hunter2"))
            self.assertEqual(
                setup.put_url_credentials(bare, shape, user, password), url)

    def test_an_address_with_no_login_is_left_alone(self):
        self.assertEqual(setup.lift_url_credentials("http://192.0.2.1/snap"),
                         ("http://192.0.2.1/snap", None, None, None))


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

    def test_it_agrees_with_the_web_ui_about_every_shape(self):
        """One rule for one question (M13). The wizard kept its own, and on
        Windows it called D:\\timelapse\\out an rsync remote spec."""
        import timelapse_web as web
        for dest in ("user@nas:/vol/tl/", "rsync://nas/tl",
                     "/mnt/odd:name/tl/", "nas:tl", "D:\\timelapse\\out",
                     "\\\\tower\\cctv\\TL"):
            _, why = setup.web_library_preview(self.cfg(
                transfer={"enabled": True, "destination": dest}))
            self.assertEqual("REMOTE" in why, web.is_remote_spec(dest), dest)

    def test_a_drive_path_is_not_remote_where_it_is_absolute(self):
        # Declared rather than inherited: on the Linux legs D:\ is not an
        # absolute path, and there the web UI's rule rightly calls it remote.
        import ntpath
        import timelapse_web as web
        cfg = self.cfg(transfer={"enabled": True,
                                 "destination": "D:\\timelapse\\out"})
        with mock.patch.object(web.os.path, "isabs", ntpath.isabs):
            _, why = setup.web_library_preview(cfg)
        self.assertNotIn("REMOTE", why)


class TestChooseWeb(unittest.TestCase):

    # The wizard now asks the kernel about the address it was given. Stubbed
    # here so these tests describe the wizard rather than the machine running
    # them: a real lan_address() would make the expected default whatever the
    # CI runner happens to be numbered. The probes themselves are covered by
    # TestBindProbe against the two addresses whose behaviour is universal.
    LAN = "192.168.1.50"

    def drive(self, keystrokes, cfg=None, checks=None, lan=LAN):
        cfg = cfg or setup.default_config()
        buf = io.StringIO()
        # Keyed by address, not a call sequence: suggest_bind() probes too, so
        # a positional list would make these tests depend on how many times
        # the wizard happens to ask.
        def fake_check(addr, port):
            return (checks or {}).get(addr, ("ok", ""))

        with _support.wizard_tty(keystrokes):
            with contextlib.redirect_stdout(buf), \
                    mock.patch.object(setup, "check_bind", fake_check), \
                    mock.patch.object(setup, "lan_address", lambda: lan), \
                    mock.patch.object(setup, "host_addresses",
                                      lambda: [lan, "127.0.0.1"]):
                setup.choose_web(cfg)
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
        # The platform's default, not the literal: this one goes into a
        # config.json, which is a per-machine file rather than a systemd unit.
        self.assertEqual(web["state_dir"], plat.WEB_STATE_DIR_DEFAULT)

    def test_wildcard_is_the_fallback_when_there_is_no_lan_address(self):
        web, _ = self.drive("y\n\n\n\n\n", lan="")
        self.assertEqual(web["bind"], "0.0.0.0")

    def test_loopback_can_still_be_chosen(self):
        web, _ = self.drive("y\n127.0.0.1\n\n\n\n")
        self.assertEqual(web["bind"], "127.0.0.1")

    def test_the_exposure_is_stated_before_the_bind_prompt(self):
        # Choosing an address is the moment this matters, and it is asked
        # before the login question, so the note has to be true either way:
        # there is no HTTPS, and the video files are open regardless.
        _, out = self.drive("y\n\n\n\n\n")
        before = out.split("Listen on", 1)[0]
        self.assertIn("no HTTPS", before)
        self.assertIn("video files stay reachable", before)

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
        # enable, bind, port, login, library, state_dir, then the answer.
        web, _ = self.drive("y\n\n\n\n\n\nn\n")
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

    def test_a_stored_library_and_index_can_be_reset(self):
        """Enter keeps, which is the test above; '-' is the way back to
        working the library out and to the default index (M16)."""
        cfg = setup.default_config()
        cfg["web"] = {"enabled": True, "bind": "10.0.0.5", "port": 9000,
                      "library_root": "/srv/tl", "state_dir": "/srv/idx"}
        web, out = self.drive("y\n\n\n\n-\n-\n", cfg)
        self.assertEqual(web["library_root"], "")
        self.assertEqual(web["state_dir"], setup.WEB_STATE_DIR_DEFAULT)
        self.assertNotIn("Leave blank to use the path above", out)


class TestClearingAnOptionalAnswer(unittest.TestCase):
    """A stored ntfy token could never be removed, and the prompt said
    "blank for none" while Enter kept it (M16)."""

    def ntfy(self, keys, token):
        sink = {"type": "ntfy", "enabled": True, "server": "https://ntfy.sh",
                "topic": "t-9f3"}
        if token is not None:
            sink["token"] = token
        cfg = {"notify": [sink]}
        buf = io.StringIO()
        with _support.wizard_tty(keys), contextlib.redirect_stdout(buf):
            setup.choose_ntfy_sink(cfg)
        return setup.existing_sink(cfg, "ntfy"), buf.getvalue()

    def test_a_hyphen_removes_the_token(self):
        # Send to ntfy, server, topic, token, test message.
        sink, out = self.ntfy("y\n\n\n-\nn\n", "tk_old")
        self.assertEqual(sink["token"], "")
        self.assertIn("- clears", out)
        self.assertNotIn("blank for none", out)

    def test_enter_still_keeps_it(self):
        sink, _ = self.ntfy("y\n\n\n\nn\n", "tk_old")
        self.assertEqual(sink["token"], "tk_old")

    def test_with_nothing_stored_blank_really_is_none(self):
        sink, out = self.ntfy("y\n\n\n\nn\n", None)
        self.assertEqual(sink["token"], "")
        self.assertIn("blank for none", out)
        self.assertNotIn("- clears", out)


class TestChooseWebLogin(unittest.TestCase):
    """The optional single login.

    Driven through choose_web_login() directly rather than the whole wizard,
    so a change to the questions either side of it does not renumber every
    keystroke here.
    """

    def drive(self, keystrokes, web=None, tty=False):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY(keystrokes, tty=tty)
        web = {} if web is None else web
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                setup.choose_web_login(web)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        return web, buf.getvalue()

    def configured(self, user="ed", password="hunter2"):
        import timelapse_web as web_mod
        return {"auth": {"username": user,
                         "password_hash": web_mod.hash_password(password,
                                                                iters=1000)}}

    def verify(self, web, password):
        import timelapse_web as web_mod
        return web_mod.verify_password(web["auth"]["password_hash"], password)

    # -- off is the default and the status quo ------------------------------

    def test_it_is_off_by_default(self):
        # Every install that predates this. Enter must not switch a login on.
        web, _ = self.drive("\n")
        self.assertNotIn("auth", web)

    def test_declining_writes_no_auth_key_at_all(self):
        # Absent, not blank: two spellings of "off" is one too many, and the
        # server reads a missing key and an empty one the same way anyway.
        web, _ = self.drive("n\n")
        self.assertNotIn("auth", web)

    def test_it_says_what_it_is_for_and_what_it_is_not(self):
        _, out = self.drive("n\n")
        self.assertIn("lock on a door", out)
        self.assertIn("crosses your", out)
        self.assertIn("video files themselves stay reachable", out)

    # -- setting one --------------------------------------------------------

    def test_a_login_is_stored_as_a_hash_that_verifies(self):
        web, _ = self.drive("y\ned\nhunter2\nhunter2\n")
        self.assertEqual(web["auth"]["username"], "ed")
        self.assertTrue(self.verify(web, "hunter2"))
        self.assertFalse(self.verify(web, "hunter3"))

    def test_the_password_itself_is_not_written_anywhere(self):
        web, _ = self.drive("y\ned\nSup3rS3cret\nSup3rS3cret\n")
        self.assertNotIn("Sup3rS3cret", json.dumps(web))

    def test_the_hash_is_one_the_server_understands(self):
        # The wizard and the server have to agree about the format, and the
        # failure if they do not is an operator locked out of their own page.
        import timelapse_web as web_mod
        web, _ = self.drive("y\ned\nhunter2\nhunter2\n")
        auth = web_mod.Auth.from_config({"web": web})
        self.assertTrue(auth.enabled)
        self.assertTrue(auth.check("ed", "hunter2"))

    def test_the_username_defaults_to_admin(self):
        web, _ = self.drive("y\n\nhunter2\nhunter2\n")
        self.assertEqual(web["auth"]["username"], "admin")

    def test_mistyping_the_confirmation_asks_again(self):
        web, out = self.drive("y\ned\nhunter2\ntypo\nhunter2\nhunter2\n")
        self.assertIn("did not match", out)
        self.assertTrue(self.verify(web, "hunter2"))

    def test_a_blank_password_changes_nothing(self):
        # An empty password is not a login, and quietly accepting one would
        # produce a page that asks for a credential and takes any.
        web, out = self.drive("y\ned\n\n")
        self.assertNotIn("auth", web)
        self.assertIn("blank password", out)

    def test_a_short_password_is_allowed_but_called_out(self):
        # This is a household lock; refusing "cats" would be pretending to be
        # something it is not. Saying so is still worth it.
        web, out = self.drive("y\ned\ncats\ncats\n")
        self.assertTrue(self.verify(web, "cats"))
        self.assertIn("short one", out)

    def test_it_says_the_password_cannot_be_read_back(self):
        # So "I forgot it" has an answer, and names the command, before
        # somebody goes looking in the config for it.
        _, out = self.drive("y\ned\nhunter2\nhunter2\n")
        self.assertIn("cannot be read back", out)
        self.assertIn("sudo timelapse password", out)

    # -- changing and removing an existing one ------------------------------

    def test_an_existing_login_is_named_and_kept_by_default(self):
        web = self.configured()
        before = web["auth"]["password_hash"]
        web, out = self.drive("\n\n", web)
        self.assertIn("'ed'", out)
        self.assertEqual(web["auth"]["password_hash"], before)

    def test_an_existing_login_can_be_replaced(self):
        web = self.configured()
        before = web["auth"]["password_hash"]
        web, _ = self.drive("y\ny\nsomeone\nnewpass\nnewpass\n", web)
        self.assertEqual(web["auth"]["username"], "someone")
        self.assertNotEqual(web["auth"]["password_hash"], before)
        self.assertTrue(self.verify(web, "newpass"))

    def test_removing_it_says_what_that_means(self):
        web, out = self.drive("n\n", self.configured())
        self.assertNotIn("auth", web)
        self.assertIn("open to anyone", out)

    def test_the_existing_username_is_the_default_when_replacing(self):
        web, _ = self.drive("y\ny\n\nnewpass\nnewpass\n", self.configured())
        self.assertEqual(web["auth"]["username"], "ed")

    # -- no terminal --------------------------------------------------------

    def test_unattended_leaves_an_existing_login_alone(self):
        # A password cannot come from a default, and reading one from a pipe
        # is what init_tty() exists to prevent: under `curl | bash` that pipe
        # is the installer script.
        prev_auto = setup.AUTO
        setup.AUTO = True
        try:
            web = self.configured()
            before = web["auth"]["password_hash"]
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                setup.choose_web_login(web)
            self.assertEqual(web["auth"]["password_hash"], before)
            # And it never asked: "Set a new one?" defaults to no, so an
            # unattended re-install cannot reach the password prompt at all.
            self.assertNotIn("Password", buf.getvalue())
        finally:
            setup.AUTO = prev_auto

    def test_unattended_never_reaches_a_prompt_it_cannot_answer(self):
        # The backstop, forced: the two questions above default to "leave it"
        # under AUTO, so this branch is normally unreachable. If either
        # default ever changes, an unattended run must say so rather than
        # stopping at a password prompt with no terminal behind it.
        prev_auto = setup.AUTO
        setup.AUTO = True
        try:
            web = {}
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                    mock.patch.object(setup, "ask_yes", lambda *a, **k: True):
                setup.choose_web_login(web)
            self.assertNotIn("auth", web)
            self.assertIn("nothing to type a password into", buf.getvalue())
        finally:
            setup.AUTO = prev_auto

    def test_unattended_does_not_invent_one(self):
        prev_auto = setup.AUTO
        setup.AUTO = True
        try:
            web = {}
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                setup.choose_web_login(web)
            self.assertNotIn("auth", web)
        finally:
            setup.AUTO = prev_auto


class TestPasswordCommand(unittest.TestCase):
    """`sudo timelapse password`: set or change the web UI's login, and
    nothing else.

    No "old password" question anywhere in here, deliberately. It needs root
    to write the config at all, and root can already read every camera
    password in that same file, so asking would prove nothing while locking
    out exactly the person entitled to fix a forgotten one.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "config.json"

    def write(self, web=None):
        cfg = setup.default_config()
        if web is not None:
            cfg["web"] = web
        self.path.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg

    def run_it(self, keystrokes, extra=()):
        argv = ["timelapse_setup.py", "--password-only",
                "--output", str(self.path)] + list(extra)
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        out = io.StringIO()
        try:
            # stderr as well as stdout: argparse writes its usage block
            # there, and one of these drives an invalid combination on
            # purpose, so the suite printed a full usage message every run.
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(setup, "init_tty",
                                      lambda **kw: None), \
                    mock.patch.object(setup, "restart_web_if_running",
                                      lambda cfg: None), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    contextlib.redirect_stdout(out):
                setup.AUTO = False
                setup._TTY = FakeTTY(keystrokes, tty=False)
                rc = setup.main()
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        return rc, saved, out.getvalue()

    def verify(self, saved, password):
        import timelapse_web as web_mod
        return web_mod.verify_password(
            saved["web"]["auth"]["password_hash"], password)

    def test_it_sets_a_login_on_a_config_that_had_none(self):
        self.write({"enabled": True, "bind": "127.0.0.1", "port": 8787})
        rc, saved, _ = self.run_it("ed\nhunter2\nhunter2\n")
        self.assertEqual(rc, 0)
        self.assertEqual(saved["web"]["auth"]["username"], "ed")
        self.assertTrue(self.verify(saved, "hunter2"))

    def test_it_does_not_ask_whether_you_want_one(self):
        # Running the command is the answer to that question.
        self.write({"enabled": True})
        _, _, out = self.run_it("ed\nhunter2\nhunter2\n")
        self.assertNotIn("Require a login?", out)

    def test_it_never_asks_for_the_old_password(self):
        self.write({"enabled": True, "auth": {
            "username": "ed", "password_hash": "irrelevant"}})
        _, _, out = self.run_it("ed\nnewpass\nnewpass\n")
        self.assertNotIn("Current password", out)
        self.assertNotIn("Old password", out)

    def test_it_replaces_an_existing_login(self):
        import timelapse_web as web_mod
        self.write({"enabled": True, "auth": {
            "username": "ed",
            "password_hash": web_mod.hash_password("old", iters=1000)}})
        _, saved, out = self.run_it("someone\nnewpass\nnewpass\n")
        self.assertIn("Currently set for 'ed'", out)
        self.assertEqual(saved["web"]["auth"]["username"], "someone")
        self.assertTrue(self.verify(saved, "newpass"))
        self.assertFalse(self.verify(saved, "old"))

    def test_the_existing_username_is_the_default(self):
        self.write({"enabled": True, "auth": {
            "username": "ed", "password_hash": "x"}})
        _, saved, _ = self.run_it("\nnewpass\nnewpass\n")
        self.assertEqual(saved["web"]["auth"]["username"], "ed")

    def test_a_blank_password_leaves_the_config_untouched(self):
        self.write({"enabled": True})
        before = self.path.read_bytes()
        rc, _, out = self.run_it("ed\n\n")
        self.assertEqual(rc, 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn("No changes made", out)

    def test_a_mismatch_is_asked_again_not_saved(self):
        self.write({"enabled": True})
        _, saved, out = self.run_it("ed\nhunter2\ntypo\nhunter2\nhunter2\n")
        self.assertIn("did not match", out)
        self.assertTrue(self.verify(saved, "hunter2"))

    def test_the_password_is_never_written_in_the_clear(self):
        self.write({"enabled": True})
        _, _, _ = self.run_it("ed\nSup3rS3cret\nSup3rS3cret\n")
        self.assertNotIn("Sup3rS3cret",
                         self.path.read_text(encoding="utf-8"))

    def test_it_works_before_the_web_ui_is_switched_on(self):
        # Somebody may set the password first and enable the UI after. Saving
        # it and saying so beats refusing.
        self.write({"enabled": False})
        rc, saved, out = self.run_it("ed\nhunter2\nhunter2\n")
        self.assertEqual(rc, 0)
        self.assertTrue(self.verify(saved, "hunter2"))
        self.assertIn("switched off", out)

    def test_it_says_how_to_remove_the_login(self):
        self.write({"enabled": True})
        _, _, out = self.run_it("ed\nhunter2\nhunter2\n")
        self.assertIn("sudo timelapse password --disable", out)

    # -- turning it off -----------------------------------------------------

    def test_disable_removes_the_login(self):
        import timelapse_web as web_mod
        self.write({"enabled": True, "auth": {
            "username": "ed",
            "password_hash": web_mod.hash_password("hunter2", iters=1000)}})
        rc, saved, out = self.run_it("", extra=["--disable"])
        self.assertEqual(rc, 0)
        # Removed, not blanked: absent is what a config without a login looks
        # like everywhere else.
        self.assertNotIn("auth", saved["web"])
        self.assertFalse(web_mod.Auth.from_config(saved).enabled)
        self.assertIn("Removed the login for 'ed'", out)

    def test_disable_says_what_it_just_did(self):
        # Somebody who typed this in a hurry should not have to infer that
        # the pages are now open.
        self.write({"enabled": True, "auth": {"username": "ed",
                                              "password_hash": "x"}})
        _, _, out = self.run_it("", extra=["--disable"])
        self.assertIn("open to anyone", out)
        self.assertIn("sets one again", out)

    def test_disable_asks_nothing_at_all(self):
        # It needs no password to carry out and it is undone by running the
        # command again, so a prompt would only stop it being scriptable.
        self.write({"enabled": True, "auth": {"username": "ed",
                                              "password_hash": "x"}})
        _, saved, out = self.run_it("", extra=["--disable"])
        self.assertNotIn("Password", out)
        self.assertNotIn("(y/N)", out)
        self.assertNotIn("auth", saved["web"])

    def test_disable_is_idempotent(self):
        self.write({"enabled": True})
        rc, saved, out = self.run_it("", extra=["--disable"])
        self.assertEqual(rc, 0)
        self.assertIn("nothing to remove", out)
        self.assertNotIn("auth", saved["web"])

    def test_disable_works_with_no_terminal_at_all(self):
        # The point of having a flag: a definite instruction that needs no
        # answers should run unattended.
        self.write({"enabled": True, "auth": {"username": "ed",
                                              "password_hash": "x"}})
        prev_auto = setup.AUTO
        argv = ["timelapse_setup.py", "--password-only", "--disable",
                "--output", str(self.path)]
        try:
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(setup, "init_tty", lambda **kw: None), \
                    mock.patch.object(setup, "restart_web_if_running",
                                      lambda cfg: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                setup.AUTO = True
                setup._TTY = None
                rc = setup.main()
        finally:
            setup.AUTO = prev_auto
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(rc, 0)
        self.assertNotIn("auth", saved["web"])

    def test_disable_restarts_the_web_ui(self):
        # Otherwise the running server keeps asking for a login that is no
        # longer configured.
        self.write({"enabled": True, "auth": {"username": "ed",
                                              "password_hash": "x"}})
        argv = ["timelapse_setup.py", "--password-only", "--disable",
                "--output", str(self.path)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(setup, "init_tty", lambda **kw: None), \
                mock.patch.object(setup, "restart_web_if_running") as res, \
                contextlib.redirect_stdout(io.StringIO()):
            setup.main()
        res.assert_called_once()

    def test_enable_is_the_ordinary_behaviour_spelled_out(self):
        # Nobody should have to discover that the bare command is the enable
        # path, or meet "unrecognized arguments: --enable".
        self.write({"enabled": True})
        rc, saved, _ = self.run_it("ed\nhunter2\nhunter2\n",
                                   extra=["--enable"])
        self.assertEqual(rc, 0)
        self.assertTrue(self.verify(saved, "hunter2"))

    def test_the_two_flags_are_refused_together(self):
        self.write({"enabled": True})
        with self.assertRaises(SystemExit) as caught:
            self.run_it("", extra=["--enable", "--disable"])
        self.assertEqual(caught.exception.code, 2)

    def test_the_flags_are_refused_outside_this_command(self):
        # `timelapse setup --disable` reads as a definite instruction about
        # something; walking the whole wizard instead would be the wrong kind
        # of surprise.
        self.write({"enabled": True})
        argv = ["timelapse_setup.py", "--disable", "--output", str(self.path)]
        with mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                setup.main()
        self.assertEqual(caught.exception.code, 2)

    def test_a_missing_config_is_an_error_not_a_new_one(self):
        # There is no config here at all: nothing was written by setUp.
        argv = ["timelapse_setup.py", "--password-only",
                "--output", str(self.path)]
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        try:
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(setup, "init_tty", lambda **kw: None), \
                    contextlib.redirect_stdout(io.StringIO()):
                setup.AUTO = False
                setup._TTY = FakeTTY("ed\nhunter2\nhunter2\n", tty=False)
                rc = setup.main()
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        self.assertEqual(rc, 1)
        self.assertFalse(self.path.exists())

    def test_it_restarts_the_web_ui_so_the_change_takes_effect(self):
        # And logs everybody out with it, which is what somebody changing a
        # password expects. Sessions are in memory, so the restart is the
        # revocation.
        self.write({"enabled": True})
        argv = ["timelapse_setup.py", "--password-only",
                "--output", str(self.path)]
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        try:
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(setup, "init_tty", lambda **kw: None), \
                    mock.patch.object(setup, "restart_web_if_running") as res, \
                    contextlib.redirect_stdout(io.StringIO()):
                setup.AUTO = False
                setup._TTY = FakeTTY("ed\nhunter2\nhunter2\n", tty=False)
                setup.main()
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        res.assert_called_once()

    def test_the_saved_config_is_one_the_server_accepts(self):
        # The two ends of this have to agree, and the failure if they do not
        # is an operator locked out of their own page.
        import timelapse_web as web_mod
        self.write({"enabled": True})
        _, saved, _ = self.run_it("ed\nhunter2\nhunter2\n")
        auth = web_mod.Auth.from_config(saved)
        self.assertTrue(auth.enabled)
        self.assertTrue(auth.check("ed", "hunter2"))
        self.assertFalse(auth.check("ed", "wrong"))


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

    def test_a_taken_port_reads_as_in_use(self):
        """This used to be skipped on Windows, which was the wrong call.

        The skip said "the service is Linux-only", but the *wizard* is not:
        it runs wherever someone runs it, and there it reported a taken port
        as free, which is the one answer this check exists to prevent. That
        is a defect in check_bind(), not a platform difference to route
        around, so the probe now asks for exclusive use where the constant
        exists and the test runs everywhere.
        """
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
        #
        # The port is borrowed from the kernel rather than hardcoded. A fixed
        # 8787 failed here with WinError 10013 the day Hyper-V's dynamic
        # exclusion range happened to cover it, which is a fault in the test,
        # not in the probe.
        borrow = socket.socket()
        borrow.bind(("127.0.0.1", 0))
        port = borrow.getsockname()[1]
        borrow.close()

        setup.check_bind("127.0.0.1", port)
        again = socket.socket()
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.addCleanup(again.close)
        again.bind(("127.0.0.1", port))     # raises if the probe held on

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
        cfg = {"web": {"enabled": enabled}}
        buf = io.StringIO()
        restarted = []
        with _support.wizard_tty(keystrokes):
            with contextlib.redirect_stdout(buf), \
                    mock.patch.object(setup, "unit_is_active",
                                      lambda unit: active), \
                    mock.patch.object(setup, "restart_unit",
                                      lambda unit, msg: restarted.append(unit)):
                setup.restart_web_if_running(cfg)
        return restarted, buf.getvalue()

    def test_a_running_service_is_restarted(self):
        restarted, _ = self.drive("y\n")
        self.assertEqual(restarted, ["timelapse-web.service"])

    def test_enter_accepts_the_restart(self):
        # The whole point is that the common path applies the change.
        restarted, _ = self.drive("\n")
        self.assertEqual(restarted, ["timelapse-web.service"])

    def test_declining_says_the_settings_are_not_live(self):
        # Against the platform's own hint rather than against `systemctl`: this
        # asserts that the operator is told how to finish the job, and what
        # that command is depends on the box they are standing at.
        restarted, out = self.drive("n\n")
        self.assertEqual(restarted, [])
        self.assertIn("previous settings", out)
        self.assertIn(plat.restart_hint("timelapse-web.service"), out)

    def test_a_stopped_service_is_told_how_to_start(self):
        restarted, out = self.drive("", active=False)
        self.assertEqual(restarted, [])
        self.assertIn(plat.start_hint("timelapse-web.service"), out)

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


class TestRuntimeStateDir(unittest.TestCase):
    """The directory the daemons publish runtime state into.

    Its whole hazard is that it is named in ReadWritePaths: if it does not
    exist when the units start, systemd refuses to start them, and the error
    talks about a mount namespace rather than about a directory or a release.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_created_from_the_config(self):
        target = Path(self.tmp) / "state"
        cfg = setup.default_config()
        cfg["paths"]["state_dir"] = str(target)
        setup.create_state_dir(cfg)
        self.assertTrue(target.is_dir())

    def test_parents_are_created_too(self):
        target = Path(self.tmp) / "a" / "b" / "state"
        cfg = setup.default_config()
        cfg["paths"]["state_dir"] = str(target)
        setup.create_state_dir(cfg)
        self.assertTrue(target.is_dir())

    def test_create_directories_makes_it_as_well(self):
        # The wizard's own path. A state directory made only by install.sh
        # would be missing on any install that reconfigured without upgrading.
        cfg = setup.default_config()
        for key, sub in (("frames_root", "f"), ("video_output", "v"),
                         ("log_dir", "l"), ("state_dir", "s")):
            cfg["paths"][key] = str(Path(self.tmp) / sub)
        cfg["web"] = {"state_dir": str(Path(self.tmp) / "w")}
        setup.create_directories(cfg)
        self.assertTrue((Path(self.tmp) / "s").is_dir())

    def test_an_unwritable_location_is_reported_not_raised(self):
        cfg = setup.default_config()
        cfg["paths"]["state_dir"] = str(Path(self.tmp) / "nope")
        buf = io.StringIO()
        with mock.patch.object(Path, "mkdir",
                               side_effect=PermissionError(13, "denied")):
            with contextlib.redirect_stdout(buf):
                setup.create_state_dir(cfg)
        self.assertIn("Could not create", buf.getvalue())

    def test_print_state_path_is_what_install_sh_reads(self):
        """install.sh derives the directory it creates from this flag.

        A flag that prints the wrong thing, or prints nothing, sends the
        installer to its /var/lib/timelapse/state fallback while the units are
        templated from the config: the directory made and the directory
        required would then be different ones.
        """
        cfg = setup.default_config()
        cfg["paths"]["state_dir"] = "/srv/tl/state"
        path = Path(self.tmp) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(sys, "argv",
                               ["timelapse_setup.py", "--print-state-path",
                                str(path)]), \
                contextlib.redirect_stdout(buf):
            rc = setup.main()
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "/srv/tl/state")

    def test_print_state_path_answers_for_a_config_without_the_key(self):
        cfg = setup.default_config()
        del cfg["paths"]["state_dir"]
        path = Path(self.tmp) / "old.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        buf = io.StringIO()
        with mock.patch.object(sys, "argv",
                               ["timelapse_setup.py", "--print-state-path",
                                str(path)]), \
                contextlib.redirect_stdout(buf):
            setup.main()
        # Not the platform's default: this flag exists for install.sh, which
        # is the Linux installer, and what it prints becomes a directory named
        # in a systemd unit. See writable_paths().
        self.assertEqual(buf.getvalue().strip(), plat.LINUX_STATE_DIR)

    def test_the_default_is_named_in_exactly_one_place(self):
        """Two copies of this string is how the unit and the directory drift.

        The wizard's template, the config example and install.sh's fallback all
        say /var/lib/timelapse/state, but only one of them is consulted at run
        time - and since 0.2.0 that one is timelapse_platform, because
        /var/lib means nothing on Windows.
        """
        import timelapse_encode as enc

        cfg = setup.default_config()
        del cfg["paths"]["state_dir"]
        self.assertEqual(str(enc.state_dir(cfg)), enc.STATE_DIR_DEFAULT)
        self.assertEqual(enc.STATE_DIR_DEFAULT, plat.STATE_DIR_DEFAULT)


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


class TestPerCameraSmoothing(unittest.TestCase):
    """Optional tmix, per camera, off unless asked for.

    The mirror image of the cadence rule next door, and the difference is the
    point: cadence keys are absent to mean "follow the global", this one is
    absent to mean "no". There is no global to follow, because averaging suits
    a roof of trees and spoils a doorway.
    """

    def setUp(self):
        self.cfg = {"capture": {"interval_seconds": 5},
                    "encode": {"framerate": 60, "min_frames": 100}}
        setup.AUTO, setup._TTY = False, None

    def tearDown(self):
        setup.AUTO, setup._TTY = False, None

    def edit(self, cam, keys):
        setup._TTY = FakeTTY(keys, tty=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            setup.edit_camera_smoothing(self.cfg, cam)
        return cam, buf.getvalue()

    def test_the_default_answer_is_no_for_a_new_camera(self):
        # Pressing Enter through a new camera must never enable it. This is
        # the "off by default for new installations" claim, in one test.
        cam, _ = self.edit({"name": "C"}, "\n")
        self.assertEqual(cam, {"name": "C"})

    def test_saying_no_stores_nothing_rather_than_a_zero(self):
        # A config that has never wanted smoothing keeps the shape it has
        # always had, which is what makes the upgrade a no-op.
        cam, _ = self.edit({"name": "C"}, "n\n")
        self.assertNotIn("smooth_frames", cam)

    def test_saying_yes_offers_the_documented_default(self):
        cam, out = self.edit({"name": "C"}, "y\n\n")
        self.assertEqual(cam["smooth_frames"], enc.SMOOTH_DEFAULT)
        self.assertIn(f"[{enc.SMOOTH_DEFAULT}]", out)

    def test_a_chosen_value_is_stored(self):
        cam, _ = self.edit({"name": "C"}, "y\n8\n")
        self.assertEqual(cam["smooth_frames"], 8)

    def test_turning_it_off_removes_the_key(self):
        cam, _ = self.edit({"name": "C", "smooth_frames": 15}, "n\n")
        self.assertNotIn("smooth_frames", cam)

    def test_an_enabled_camera_defaults_to_staying_enabled(self):
        cam, out = self.edit({"name": "C", "smooth_frames": 20}, "\n\n")
        self.assertEqual(cam["smooth_frames"], 20)
        self.assertIn("[20]", out)

    def test_a_value_outside_the_range_is_refused_and_reasked(self):
        # ask_int loops rather than accepting, so the stored value is the
        # second answer, not the first.
        cam, out = self.edit({"name": "C"}, "y\n99\n30\n")
        self.assertEqual(cam["smooth_frames"], 30)
        cam, _ = self.edit({"name": "D"}, "y\n1\n3\n")
        self.assertEqual(cam["smooth_frames"], 3)

    def test_the_span_is_reported_in_real_time_not_frames(self):
        # 15 frames means something different at 5s and at 60s, and the
        # seconds are what the operator is actually choosing.
        _, out = self.edit({"name": "C"}, "y\n15\n")
        self.assertIn("75s", out)

    def test_the_span_follows_this_cameras_own_interval(self):
        _, out = self.edit({"name": "C", "interval_seconds": 60}, "y\n15\n")
        self.assertIn("900s", out)

    def test_what_the_wizard_writes_is_what_the_encoder_reads(self):
        # The two halves live in different modules and only agree by
        # convention. Pin them together rather than trusting the key name.
        cam, _ = self.edit({"name": "C"}, "y\n12\n")
        self.assertEqual(enc.camera_smoothing(cam), 12)
        cam, _ = self.edit(cam, "n\n")
        self.assertEqual(enc.camera_smoothing(cam), 0)

    def test_the_wizard_cannot_offer_a_value_the_encoder_would_clamp(self):
        # The prompt's bounds and the encoder's clamp are two statements of
        # one rule, so a drift between them would silently ignore an answer
        # the wizard had just accepted.
        for value in (enc.SMOOTH_MIN, enc.SMOOTH_MAX):
            cam, _ = self.edit({"name": "C"}, f"y\n{value}\n")
            self.assertEqual(enc.camera_smoothing(cam), value)


class TestSmoothingSurvivesCameraToggling(unittest.TestCase):
    """Disabling a camera must not cost it its settings.

    A camera gets disabled for a season, not forever, and re-enabling it
    should not mean setting it up again. The enable/disable path touches one
    key, and this is what says so.
    """

    def test_disabling_and_re_enabling_keeps_smoothing(self):
        cam = {"name": "Roof", "enabled": True, "smooth_frames": 15,
               "interval_seconds": 60}
        cam["enabled"] = False
        self.assertEqual(cam["smooth_frames"], 15)
        self.assertEqual(enc.camera_smoothing(cam), 15)
        cam["enabled"] = True
        self.assertEqual(cam["smooth_frames"], 15)
        self.assertEqual(enc.camera_smoothing(cam), 15)

    def test_a_disabled_camera_still_reports_its_smoothing(self):
        # It has to survive being read while off, or the listing would show
        # the setting vanishing the moment someone disabled the camera.
        self.assertEqual(
            enc.camera_smoothing({"enabled": False, "smooth_frames": 15}), 15)

    def test_the_toggle_in_manage_cameras_writes_only_enabled(self):
        # Read the source rather than drive the loop: the claim is about what
        # that branch does NOT touch, and a test that drives it would pass
        # just as happily if a future edit reset the camera's other keys.
        src = inspect.getsource(setup.manage_cameras)
        toggle = src[src.index('elif action == "x"'):src.index('elif action == "t"')]
        self.assertIn('cam["enabled"] = False', toggle)
        self.assertIn('cam["enabled"] = True', toggle)
        self.assertNotIn("smooth_frames", toggle)
        self.assertNotIn("pop(", toggle)


class TestPerCameraCadence(unittest.TestCase):
    """Editing one camera's interval and frame rate.

    The rule that makes the whole thing work: answering with the global value
    *removes* the key. Storing a copy would silently pin the camera, so a
    later change to the global would move every camera except the ones
    somebody had merely looked at.
    """

    def setUp(self):
        self.cfg = {"capture": {"interval_seconds": 5},
                    "encode": {"framerate": 60, "min_frames": 100}}
        setup.AUTO, setup._TTY = False, None

    def tearDown(self):
        setup.AUTO, setup._TTY = False, None

    def edit(self, cam, keys):
        setup._TTY = FakeTTY(keys, tty=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            setup.edit_camera_cadence(self.cfg, cam)
        return cam, buf.getvalue()

    def test_answering_with_the_globals_stores_nothing(self):
        cam, _ = self.edit({"name": "C"}, "5\n60\n")
        self.assertNotIn("interval_seconds", cam)
        self.assertNotIn("framerate", cam)

    def test_pressing_enter_twice_stores_nothing(self):
        # The defaults offered are the effective values, so Enter means
        # "leave it alone", which for an unpinned camera means unpinned.
        cam, _ = self.edit({"name": "C"}, "\n\n")
        self.assertEqual(cam, {"name": "C"})

    def test_different_values_are_stored_on_the_camera(self):
        cam, _ = self.edit({"name": "C"}, "60\n30\n")
        self.assertEqual(cam["interval_seconds"], 60)
        self.assertEqual(cam["framerate"], 30)

    def test_going_back_to_the_global_clears_the_override(self):
        cam, _ = self.edit({"name": "C", "interval_seconds": 60,
                            "framerate": 30}, "5\n60\n")
        self.assertNotIn("interval_seconds", cam)
        self.assertNotIn("framerate", cam)

    def test_the_current_override_is_the_default_offered(self):
        cam, out = self.edit({"name": "C", "interval_seconds": 60,
                              "framerate": 30}, "\n\n")
        self.assertEqual(cam["interval_seconds"], 60)
        self.assertEqual(cam["framerate"], 30)
        self.assertIn("[60]", out)

    def test_one_override_alone_is_allowed(self):
        cam, _ = self.edit({"name": "C"}, "5\n30\n")
        self.assertNotIn("interval_seconds", cam)
        self.assertEqual(cam["framerate"], 30)

    def test_it_says_what_the_result_will_be(self):
        # 86400/60 = 1440 frames, at 30fps = 48 seconds.
        _, out = self.edit({"name": "C"}, "60\n30\n")
        self.assertIn("1,440", out)
        self.assertIn("0:48", out)

    def test_a_cadence_below_min_frames_is_refused(self):
        # 86400/900 = 96 frames, under min_frames 100, so the nightly encode
        # would SKIP this camera every night without ever failing. It used to
        # warn and store it; the window refused it, and so does this now (M11).
        cam, out = self.edit({"name": "C"}, "900\n60\n30\n")
        self.assertEqual(cam["interval_seconds"], 60)
        self.assertIn("min_frames", out)
        self.assertIn("skip", out.lower())

    def test_a_normal_cadence_does_not_warn(self):
        _, out = self.edit({"name": "C"}, "60\n30\n")
        self.assertNotIn("min_frames", out)


class TestTheGlobalIntervalIsCheckedToo(unittest.TestCase):
    """choose_capture() accepted 3600 with nothing said, and had no test (M11)."""

    def drive(self, keys, cfg=None, auto=False):
        cfg = cfg or setup.default_config()
        buf = io.StringIO()
        with _support.wizard_tty(keys, auto=auto), \
                contextlib.redirect_stdout(buf), \
                mock.patch.object(setup, "choose_framerate"), \
                mock.patch.object(setup, "estimate_budget"):
            setup.choose_capture(cfg, None)
        return cfg, buf.getvalue()

    def test_an_interval_that_never_makes_a_video_is_refused(self):
        cfg, out = self.drive("3600\n60\n")
        self.assertEqual(cfg["capture"]["interval_seconds"], 60)
        self.assertIn("min_frames", out)

    def test_the_longest_interval_that_works_is_accepted(self):
        # 86400 / 864 = 100 frames, exactly min_frames.
        cfg, out = self.drive("864\n")
        self.assertEqual(cfg["capture"]["interval_seconds"], 864)
        self.assertNotIn("min_frames", out)

    def test_the_advice_names_the_longest_interval_that_works(self):
        _, out = self.drive("900\n60\n")
        self.assertIn("864s or less", out)

    def test_with_no_terminal_it_is_kept_and_said_rather_than_asked_for_ever(self):
        cfg = setup.default_config()
        cfg["encode"]["min_frames"] = 20000        # 5s gives 17,280
        cfg, out = self.drive("", cfg, auto=True)
        self.assertEqual(cfg["capture"]["interval_seconds"], 5)
        self.assertIn("min_frames", out)


class TestCadenceListing(unittest.TestCase):

    def show(self, cams):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            setup.list_cameras({"capture": {"interval_seconds": 5},
                                "encode": {"framerate": 60},
                                "cameras": cams})
        return buf.getvalue()

    def test_the_effective_cadence_is_shown(self):
        out = self.show([{"name": "A", "url": "http://h/s"}])
        self.assertIn("5s/60", out)

    def test_an_override_is_marked_and_explained(self):
        # Which cameras a change to the global will actually move is not
        # something you can see from names and URLs.
        out = self.show([{"name": "A", "url": "http://h/s"},
                         {"name": "B", "url": "http://h/s",
                          "interval_seconds": 60, "framerate": 30}])
        self.assertIn("60s/30*", out)
        self.assertIn("own interval or frame rate", out)

    def test_no_overrides_means_no_footnote(self):
        out = self.show([{"name": "A", "url": "http://h/s"}])
        self.assertNotIn("own interval or frame rate", out)

    def test_columns_line_up_whether_or_not_a_camera_is_disabled(self):
        """Reported from a real terminal, and invisible here until forced.

        `dim()` is a no-op unless stdout is a tty, which no test run and no
        CI job ever is, so the padding looked correct everywhere it was
        checked and collided only in front of the operator: a format width
        counts the escape codes, so `{state:<4}` on an already-coloured "no"
        adds nothing and the row reads "no5s/60". Pad first, colour second.

        Asserting on column positions rather than on the absence of one
        string, because the defect is alignment and any coloured column can
        reintroduce it.
        """
        with mock.patch.object(setup, "_COLOR", True):
            out = self.show([{"name": "On", "url": "http://h/s"},
                             {"name": "Off", "url": "http://h/s",
                              "enabled": False}])
        rows = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in out.splitlines()
                if "http://h/s" in ln]
        self.assertEqual(len(rows), 2, out)
        self.assertEqual(rows[0].index("5s/60"), rows[1].index("5s/60"),
                         f"cadence column moved:\n{rows[0]}\n{rows[1]}")

    def test_smoothing_is_shown_against_the_camera_that_has_it(self):
        out = self.show([{"name": "A", "url": "http://h/s"},
                         {"name": "B", "url": "http://h/s",
                          "smooth_frames": 15}])
        self.assertIn("5s/60+15", out)
        self.assertIn("averages N frames", out)

    def test_no_smoothing_means_no_footnote(self):
        out = self.show([{"name": "A", "url": "http://h/s"}])
        self.assertNotIn("averages N frames", out)

    def test_both_markers_can_appear_on_one_camera(self):
        out = self.show([{"name": "A", "url": "http://h/s",
                          "interval_seconds": 60, "framerate": 30,
                          "smooth_frames": 8}])
        self.assertIn("60s/30*+8", out)

    def test_a_smoothed_listing_still_fits_eighty_columns(self):
        # The row had no slack left when smoothing was added, which is why it
        # shares the cadence column rather than getting one of its own. The
        # URL elision is what would otherwise have paid, and it cannot: see
        # the mask test below.
        out = self.show([{"name": "DoorbellFront", "interval_seconds": 3,
                          "smooth_frames": 30,
                          "url": "http://192.0.2.11/cgi-bin/api.cgi?cmd=Snap"
                                 "&channel=0&user=admin&password=hunter2"}])
        for line in out.splitlines():
            self.assertLessEqual(len(re.sub(r"\x1b\[[0-9;]*m", "", line)), 80,
                                 line)

    def test_a_smoothed_row_still_masks_the_password(self):
        # The 13-character URL tail is what keeps *** in view on a Reolink
        # URL, and a listing that elides the mask reads as though nothing had
        # been redacted. Adding a column must never be paid for from there.
        out = self.show([{"name": "A", "smooth_frames": 15,
                          "url": "http://h/api?user=admin&password=hunter2"}])
        self.assertNotIn("hunter2", out)
        self.assertIn("***", out)

    def test_an_absurd_cadence_does_not_collide_with_the_next_column(self):
        # 3600s at 240fps with smoothing overflows the column. It may push
        # the row wider, but it must not run into the type.
        out = self.show([{"name": "A", "url": "http://h/s", "method": "rtsp",
                          "interval_seconds": 3600, "framerate": 240,
                          "smooth_frames": 30}])
        self.assertIn("3600s/240*+30 rtsp", out)

    def test_the_listing_still_fits_eighty_columns(self):
        # It gained a Cadence column, and the URL elision was shortened from
        # 44 to 36 characters to pay for it. A name longer than its 14-wide
        # column pushes the row wider, which it always did, so the contract
        # here is a name that fits.
        out = self.show([{"name": "DoorbellFront", "interval_seconds": 3,
                          "url": "http://192.0.2.11/cgi-bin/api.cgi?cmd=Snap"
                                 "&channel=0&user=admin&password=hunter2"}])
        for line in out.splitlines():
            self.assertLessEqual(len(re.sub(r"\x1b\[[0-9;]*m", "", line)), 80,
                                 line)

    def test_passwords_are_still_masked_after_the_column_change(self):
        out = self.show([{"name": "A", "interval_seconds": 3,
                          "url": "http://h/api?user=admin&password=hunter2"}])
        self.assertNotIn("hunter2", out)
        self.assertIn("***", out)


class TestSyncReadWritePaths(unittest.TestCase):
    """"Nothing to do" and "could not do it" must not share a return value.

    This returned a bare bool counting the units it rewrote, and the caller
    read anything falsy as failure. So running it as root against units that
    were already correct - the ordinary case, since the installer writes them
    on every upgrade - told the operator to edit the unit by hand as root,
    while they were root. Reported from a real 0.1.2 install.
    """

    CFG = {"paths": {"frames_root": "/var/lib/timelapse/frames",
                     "video_output": "/var/lib/timelapse/videos",
                     "log_dir": "/var/lib/timelapse/logs"},
           "transfer": {"enabled": True, "destination": "/mnt/cctv/TL/"}}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # The rendered value, not the bare list: what goes into the unit is
        # quoted, so that a path with a space in it is one entry and not two.
        self.want = setup.unit_path_value(setup.writable_paths(self.CFG))

    def unit(self, name="timelapse-encode.service", rwp=None):
        p = self.tmp / name
        body = "[Service]\n"
        if rwp is not None:
            body += f"ReadWritePaths={rwp}\n"
        p.write_text(body + "ProtectSystem=strict\n", encoding="utf-8")
        return p

    def sync(self, root=True):
        with mock.patch.object(setup, "is_elevated", lambda: root), \
                contextlib.redirect_stdout(io.StringIO()):
            return setup.sync_unit_readwritepaths(self.CFG, unitdir=str(self.tmp))

    def report(self, status, detail=""):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            setup.report_readwritepaths(status, detail)
        return buf.getvalue()

    def test_a_stale_unit_is_rewritten(self):
        u = self.unit(rwp="/var/lib/timelapse/frames")
        status, detail = self.sync()
        self.assertEqual(status, "changed")
        self.assertIn(f"ReadWritePaths={self.want}", u.read_text(encoding="utf-8"))

    def test_a_correct_unit_reports_current_not_failure(self):
        self.unit(rwp=self.want)
        self.assertEqual(self.sync()[0], "current")

    def test_a_correct_unit_is_not_rewritten(self):
        u = self.unit(rwp=self.want)
        before = u.read_text(encoding="utf-8")
        self.sync()
        self.assertEqual(u.read_text(encoding="utf-8"), before)

    def test_no_units_installed_is_absent_not_failure(self):
        self.assertEqual(self.sync()[0], "absent")

    def test_not_root_is_denied(self):
        self.unit(rwp="/wrong")
        self.assertEqual(self.sync(root=False)[0], "denied")

    def test_an_unwritable_unit_is_a_real_failure(self):
        self.unit(rwp="/wrong")
        with mock.patch.object(Path, "write_text",
                               side_effect=OSError("read-only")):
            status, detail = self.sync()
        self.assertEqual(status, "failed")
        self.assertIn("read-only", detail)

    def test_the_transfer_destination_is_included(self):
        self.assertIn("/mnt/cctv/TL", self.want)

    # -- what the operator is told ------------------------------------------

    def test_current_says_nothing_is_wrong(self):
        out = self.report("current", self.want)
        self.assertNotIn("by hand", out)
        self.assertIn("nothing to do", out.lower())

    def test_changed_is_silent_because_it_already_narrated(self):
        self.assertEqual(self.report("changed", self.want), "")

    def test_denied_is_the_only_one_that_mentions_sudo(self):
        self.assertIn("sudo", self.report("denied", ""))
        for status in ("current", "changed", "absent", "failed"):
            self.assertNotIn("sudo", self.report(status, "x"), status)

    def test_failed_still_tells_you_to_fix_it_by_hand(self):
        out = self.report("failed", "encode.service: read-only")
        self.assertIn("by hand", out)
        self.assertIn("read-only", out)

    def test_absent_does_not_sound_like_a_problem(self):
        out = self.report("absent", "")
        self.assertNotIn("by hand", out)
        self.assertIn("nothing to", out.lower())


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


class TestRedactedDump(unittest.TestCase):
    """`timelapse config --redacted`: the config, minus the credentials.

    It exists because "here is my config, why won't camera 3 connect" is a
    thing people do, and doing it by hand puts the guarantee in the least
    reliable place there is.
    """

    S = "Sup3rS3cret!"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "config.json"

    def write(self, cfg):
        self.path.write_text(json.dumps(cfg), encoding="utf-8")

    def dump(self):
        """(exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = setup.print_redacted_config(str(self.path))
        return rc, out.getvalue(), err.getvalue()

    def test_stdout_is_json_and_holds_no_password(self):
        # The contract is both halves at once: parseable, and safe.
        self.write({"cameras": [{"name": "Gate", "password": self.S}]})
        rc, out, _ = self.dump()
        self.assertEqual(rc, 0)
        self.assertNotIn(self.S, out)
        self.assertEqual(json.loads(out)["cameras"][0]["password"], "***")

    def test_prose_goes_to_stderr_so_a_redirect_stays_parseable(self):
        # `timelapse config --redacted > report.json` must produce a file that
        # still parses; the person at the terminal must still be warned.
        self.write({"cameras": []})
        _, out, err = self.dump()
        json.loads(out)                        # would raise if prose leaked in
        self.assertTrue(err.strip())

    def test_the_dump_carries_its_own_notice(self):
        # Inside the JSON, not beside it: what gets pasted into an issue is
        # the blob, never the warning that was printed next to it.
        self.write({"cameras": []})
        _, out, _ = self.dump()
        notice = json.loads(out)["_redacted"]
        self.assertIn("NOT masked", notice)

    def test_the_notice_is_first_so_it_is_read(self):
        self.write({"cameras": [], "paths": {}})
        _, out, _ = self.dump()
        self.assertEqual(next(iter(json.loads(out))), "_redacted")

    def test_it_changes_nothing_on_disk(self):
        # A read-only command run by somebody already having a bad day.
        cfg = {"cameras": [{"name": "Gate", "password": self.S}]}
        self.write(cfg)
        before = self.path.read_bytes()
        self.dump()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in Path(self.dir.name).iterdir()],
                         ["config.json"])

    def test_a_missing_config_fails_without_printing_a_dump(self):
        rc, out, _ = self.dump()
        self.assertEqual(rc, 1)
        self.assertEqual(out.strip().count("{"), 0)

    def test_an_unreadable_config_does_not_print_half_of_one(self):
        self.path.write_text("{ not json", encoding="utf-8")
        rc, out, _ = self.dump()
        self.assertEqual(rc, 1)
        self.assertNotIn("_redacted", out)

    def test_the_flag_reaches_it_without_ever_touching_a_terminal(self):
        # Handled before init_tty() deliberately. This is a filter, and a
        # filter that can prompt is one that hangs inside a pipe.
        self.write({"cameras": [{"name": "Gate", "password": self.S}]})
        argv = ["timelapse_setup.py", "--redacted", "--output", str(self.path)]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(setup, "init_tty",
                                  side_effect=AssertionError("it prompted")), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = setup.main()
        self.assertEqual(rc, 0)
        self.assertNotIn(self.S, out.getvalue())
        json.loads(out.getvalue())


class TestChooseNotify(unittest.TestCase):
    """The notification sinks, driven one section at a time.

    Whole-wizard driving would renumber every keystroke here whenever a
    question moves, which is why the login tests do the same thing.
    """

    def drive(self, func, keystrokes, cfg=None):
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        # A list of answers is easier to read and to comment than one long
        # string, and the prompts here are numerous enough to need it.
        setup._TTY = FakeTTY("\n".join(list(keystrokes) + [""]), tty=False)
        cfg = {"discord": {"enabled": False, "webhook_url": "",
                           "username": "Timelapse Bot"}} if cfg is None else cfg
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                func(cfg)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        return cfg, buf.getvalue()

    def sink(self, cfg, kind):
        return setup.existing_sink(cfg, kind)

    # -- discord ------------------------------------------------------------

    def test_declining_leaves_nothing_enabled(self):
        cfg, _ = self.drive(setup.choose_discord_sink, ["n"])
        self.assertFalse(self.sink(cfg, "discord").get("enabled"))

    def test_a_webhook_becomes_a_sink(self):
        cfg, _ = self.drive(setup.choose_discord_sink,
                            ["y", "https://discord.com/api/webhooks/1/x", "n"])
        sink = self.sink(cfg, "discord")
        self.assertTrue(sink["enabled"])
        self.assertEqual(sink["webhook_url"],
                         "https://discord.com/api/webhooks/1/x")
        self.assertEqual(sink["type"], "discord")

    def test_an_existing_legacy_webhook_is_offered_as_the_default(self):
        """Re-running this against a pre-0.1.6 config must not present a blank
        prompt for a webhook that is already configured."""
        cfg = {"discord": {"enabled": True, "webhook_url": "https://old/x",
                           "username": "Bot"}}
        cfg, _ = self.drive(setup.choose_discord_sink, ["", "", "n"], cfg)
        self.assertEqual(self.sink(cfg, "discord")["webhook_url"],
                         "https://old/x")

    # -- ntfy ---------------------------------------------------------------

    def test_ntfy_defaults_to_the_public_server(self):
        cfg, _ = self.drive(setup.choose_ntfy_sink,
                            ["y", "", "mytopic", "", "n"])
        sink = self.sink(cfg, "ntfy")
        self.assertEqual(sink["server"], "https://ntfy.sh")
        self.assertEqual(sink["topic"], "mytopic")
        self.assertTrue(sink["enabled"])

    def test_ntfy_says_a_public_topic_is_the_only_secret(self):
        _, out = self.drive(setup.choose_ntfy_sink,
                            ["y", "", "mytopic", "", "n"])
        self.assertIn("only secret", out)

    def test_a_self_hosted_ntfy_is_not_lectured_about_public_topics(self):
        _, out = self.drive(setup.choose_ntfy_sink,
                            ["y", "https://ntfy.lan", "t", "", "n"])
        self.assertNotIn("only secret", out)

    def test_an_ntfy_token_is_kept(self):
        cfg, _ = self.drive(setup.choose_ntfy_sink,
                            ["y", "", "t", "tk_abc", "n"])
        self.assertEqual(self.sink(cfg, "ntfy")["token"], "tk_abc")

    # -- telegram -----------------------------------------------------------

    def test_telegram_needs_both_halves_to_be_enabled(self):
        cfg, _ = self.drive(setup.choose_telegram_sink,
                            ["y", "123:ABC", "", "n"])
        self.assertFalse(self.sink(cfg, "telegram")["enabled"])

    def test_telegram_with_both_halves(self):
        cfg, _ = self.drive(setup.choose_telegram_sink,
                            ["y", "123:ABC", "-100200", "n"])
        sink = self.sink(cfg, "telegram")
        self.assertEqual((sink["token"], sink["chat_id"]),
                         ("123:ABC", "-100200"))
        self.assertTrue(sink["enabled"])

    def test_telegram_explains_where_the_token_comes_from(self):
        _, out = self.drive(setup.choose_telegram_sink, ["n"])
        self.assertIn("BotFather", out)

    # -- the section as a whole ---------------------------------------------

    def test_several_sinks_can_be_on_at_once(self):
        cfg, _ = self.drive(
            setup.choose_notify,
            ["y", "https://d/x", "n",          # discord, no test message
             "y", "", "topic", "", "n",        # ntfy
             "y", "123:ABC", "-100", "n"])     # telegram
        kinds = [(s.get("type"), s.get("enabled")) for s in cfg["notify"]]
        self.assertEqual(kinds, [("discord", True), ("ntfy", True),
                                 ("telegram", True)])

    def test_the_legacy_block_is_turned_off_once_notify_exists(self):
        """It is ignored while `notify` is present, so leaving it enabled
        would be a webhook that looks configured and never fires."""
        cfg = {"discord": {"enabled": True, "webhook_url": "https://old/x",
                           "username": "Bot"}}
        cfg, _ = self.drive(setup.choose_notify,
                            ["y", "https://new/x", "n", "n", "n"], cfg)
        self.assertFalse(cfg["discord"]["enabled"])
        self.assertEqual(cfg["discord"]["webhook_url"], "")
        self.assertEqual(self.sink(cfg, "discord")["webhook_url"],
                         "https://new/x")

    def test_declining_everything_leaves_nothing_to_send_to(self):
        import timelapse_encode as enc_mod
        cfg, _ = self.drive(setup.choose_notify, ["n", "n", "n"])
        self.assertEqual(enc_mod.notify_sinks(cfg), [])

    def test_the_order_of_sinks_is_stable_across_runs(self):
        # A list that reshuffles itself makes a config diff unreadable.
        cfg, _ = self.drive(setup.choose_notify,
                            ["y", "https://d/x", "n", "y", "", "t", "", "n",
                             "n"])
        first = [s.get("type") for s in cfg["notify"]]
        cfg, _ = self.drive(setup.choose_notify,
                            ["y", "", "n", "y", "", "", "", "n", "n"], cfg)
        self.assertEqual([s.get("type") for s in cfg["notify"]], first)


class TestSinkIdentity(unittest.TestCase):
    """What the "already verified" marker hashes.

    Every one of these strings is or contains a credential, which is why only
    a digest of it is ever written to disk.
    """

    def test_each_type_has_its_own_identity(self):
        seen = {
            setup.sink_identity({"type": "discord",
                                 "webhook_url": "https://d/x"}),
            setup.sink_identity({"type": "ntfy", "server": "https://ntfy.sh",
                                 "topic": "t"}),
            setup.sink_identity({"type": "telegram", "token": "T",
                                 "chat_id": "1"}),
        }
        self.assertEqual(len(seen), 3)

    def test_changing_the_topic_changes_the_identity(self):
        a = setup.sink_identity({"type": "ntfy", "topic": "one"})
        b = setup.sink_identity({"type": "ntfy", "topic": "two"})
        self.assertNotEqual(a, b)

    def test_a_sink_with_no_type_is_a_discord_webhook(self):
        self.assertEqual(setup.sink_identity({"webhook_url": "https://d/x"}),
                         "https://d/x")

    def test_only_a_digest_reaches_the_disk(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg_path = tmp / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        setup.record_notify_verified(str(cfg_path),
                                     {"type": "telegram", "token": "SECRET",
                                      "chat_id": "1"})
        written = (tmp / setup.WEBHOOK_MARKER).read_text(encoding="utf-8")
        self.assertNotIn("SECRET", written)


def probe_match(uuid_str, xaddrs, types, scopes):
    """A ProbeMatch in the shape real cameras send.

    Vendors differ in prefix and in namespace declaration style, which is the
    whole reason the parser matches on local names. These bodies are modelled
    on the four makes measured on 2026-08-14.
    """
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<SOAP-ENV:Envelope'
        ' xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
        '<SOAP-ENV:Header>'
        '<wsa:MessageID>uuid:abc</wsa:MessageID>'
        '</SOAP-ENV:Header><SOAP-ENV:Body><d:ProbeMatches><d:ProbeMatch>'
        f'<wsa:EndpointReference><wsa:Address>{uuid_str}</wsa:Address>'
        '</wsa:EndpointReference>'
        f'<d:Types>{types}</d:Types>'
        f'<d:Scopes>{scopes}</d:Scopes>'
        f'<d:XAddrs>{xaddrs}</d:XAddrs>'
        '<d:MetadataVersion>1</d:MetadataVersion>'
        '</d:ProbeMatch></d:ProbeMatches></SOAP-ENV:Body></SOAP-ENV:Envelope>'
    ).encode("utf-8")


DAHUA = probe_match(
    "uuid:0e3cec9c-8515-54c3-c170-79b445c58515",
    "http://192.168.2.202/onvif/device_service",
    "dn:NetworkVideoTransmitter tds:Device",
    "onvif://www.onvif.org/type/Network_Video_Transmitter "
    "onvif://www.onvif.org/name/Dahua "
    "onvif://www.onvif.org/hardware/IPC-HFW5442E-SE")

HIKVISION = probe_match(
    "urn:uuid:a2ba0000-ff84-11b3-8265-bc9b5e7022ac",
    "http://192.168.2.204/onvif/device_service "
    "http://[fdd2:49bd:6a04:a0b:be9b:5eff:fe70:22ac]/onvif/device_service",
    "dn:NetworkVideoTransmitter tds:Device",
    "onvif://www.onvif.org/type/video_encoder "
    "onvif://www.onvif.org/name/HIKVISION%20DS-2DE2A404IW-DE3 "
    "onvif://www.onvif.org/hardware/DS-2DE2A404IW-DE3")

REOLINK = probe_match(
    "urn:uuid:05140407-0104-0002-0400-ec71db196f62",
    "http://192.168.2.208:8000/onvif/device_service",
    "tds:Device tdn:NetworkVideoTransmitter",
    "onvif://www.onvif.org/type/video_encoder "
    "onvif://www.onvif.org/name/IPC-BO "
    "onvif://www.onvif.org/hardware/D340P")

# A Windows PC. Answers the same protocol on the same group and port.
WINDOWS_PC = probe_match(
    "urn:uuid:2c5ccd51-b091-4ff5-9a2c-8d36d7676716",
    "http://192.168.2.86:5357/2c5ccd51/",
    "wsdp:Device pub:Computer", "")


class TestDiscoveryParsing(unittest.TestCase):
    """Everything here is pinned to shapes measured from real cameras."""

    def test_a_probe_match_is_parsed(self):
        got = setup.wsd_parse(DAHUA)
        self.assertEqual(got["uuid"], "uuid:0e3cec9c-8515-54c3-c170-79b445c58515")
        self.assertEqual(got["xaddrs"],
                         ["http://192.168.2.202/onvif/device_service"])

    def test_junk_is_not_a_probe_match(self):
        self.assertIsNone(setup.wsd_parse(b"not xml at all <<<"))
        self.assertIsNone(setup.wsd_parse(b"<html><body>hello</body></html>"))

    def test_scopes_are_unquoted(self):
        got = setup.wsd_parse(HIKVISION)
        self.assertEqual(setup.wsd_scope(got_dev(got), "name"),
                         "HIKVISION DS-2DE2A404IW-DE3")

    def test_the_types_field_classifies_not_the_type_scope(self):
        # The measured trap: Dahua's type scope says
        # Network_Video_Transmitter, Hikvision's and Reolink's say
        # video_encoder. Classifying on the scope called six of eight real
        # cameras not-cameras.
        for body in (DAHUA, HIKVISION, REOLINK):
            self.assertTrue(setup.wsd_is_camera(setup.wsd_parse(body)["types"]))

    def test_a_windows_pc_is_not_a_camera(self):
        self.assertFalse(
            setup.wsd_is_camera(setup.wsd_parse(WINDOWS_PC)["types"]))

    def test_the_types_prefix_may_be_dn_or_tdn(self):
        self.assertTrue(setup.wsd_is_camera("dn:NetworkVideoTransmitter"))
        self.assertTrue(setup.wsd_is_camera("tds:Device tdn:NetworkVideoTransmitter"))
        self.assertTrue(setup.wsd_is_camera("Network_Video_Transmitter"))
        self.assertFalse(setup.wsd_is_camera("wsdp:Device pub:Computer"))

    def test_an_xaddr_that_is_not_a_url_does_not_raise(self):
        # A real Dahua advertises exactly this, and urlparse raises
        # ValueError on it under Python 3.12+.
        self.assertEqual(setup.wsd_host("http://[]/onvif/device_service"), "")

    def test_the_address_preferred_is_one_it_answered_from(self):
        dev = {"xaddrs": ["http://[]/onvif/device_service",
                          "http://10.0.0.9/onvif/device_service"],
               "sources": ["192.168.2.205"]}
        # Neither advertised host was the source, so the parseable one wins
        # over the unusable one.
        self.assertEqual(setup.wsd_address(dev), "10.0.0.9")

    def test_a_confirmed_address_beats_an_advertised_one(self):
        dev = {"xaddrs": ["http://10.0.0.9/onvif/device_service",
                          "http://192.168.2.204/onvif/device_service"],
               "sources": ["192.168.2.204"]}
        self.assertEqual(setup.wsd_address(dev), "192.168.2.204")

    def test_ipv4_is_preferred_over_ipv6(self):
        dev = {"xaddrs": ["http://[fdd2::1]/onvif/device_service",
                          "http://192.168.2.204/onvif/device_service"],
               "sources": ["fdd2::1", "192.168.2.204"]}
        self.assertEqual(setup.wsd_address(dev), "192.168.2.204")

    def test_a_device_with_no_usable_xaddr_falls_back_to_its_source(self):
        dev = {"xaddrs": ["http://[]/x"], "sources": ["192.168.2.205"]}
        self.assertEqual(setup.wsd_address(dev), "192.168.2.205")

    def test_the_probe_is_typed_and_well_formed(self):
        packet = setup.wsd_probe()
        root = ET.fromstring(packet)
        types = [e for e in root.iter()
                 if setup.wsd_localname(e.tag) == "Types"]
        self.assertEqual(len(types), 1)
        self.assertEqual(types[0].text, "dn:NetworkVideoTransmitter")

    def test_each_probe_carries_a_fresh_message_id(self):
        # Some firmware suppresses a MessageID it has already answered.
        self.assertNotEqual(setup.wsd_probe(), setup.wsd_probe())

    def test_addresses_sort_numerically(self):
        got = sorted(["192.168.2.10", "192.168.2.9", "192.168.2.100"],
                     key=setup.sort_key_for_address)
        self.assertEqual(got, ["192.168.2.9", "192.168.2.10", "192.168.2.100"])


def collect(*bodies_and_sources):
    """Feed replies through the same merge discover_cameras() uses."""
    devices = {}
    for body, source in bodies_and_sources:
        reply = setup.wsd_parse(body)
        key = reply["uuid"] or f"addr:{source}"
        dev = devices.setdefault(key, {"uuid": reply["uuid"], "xaddrs": [],
                                       "sources": [], "scopes": [],
                                       "types": ""})
        dev["types"] = dev["types"] or reply["types"]
        for x in reply["xaddrs"]:
            if x not in dev["xaddrs"]:
                dev["xaddrs"].append(x)
        if source not in dev["sources"]:
            dev["sources"].append(source)
        for pair in reply["scopes"]:
            if pair not in dev["scopes"]:
                dev["scopes"].append(pair)
    return setup.wsd_finalise(devices)


class TestDiscoveryResults(unittest.TestCase):
    """The whole path from response bodies to what the wizard offers."""

    def test_a_camera_whose_scope_says_encoder_is_still_a_camera(self):
        # Hikvision's type scope reads "video_encoder". This is the wiring
        # test: classifying on the scope would drop it from the list.
        found = collect((HIKVISION, "192.168.2.204"))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["camera"])
        self.assertEqual(found[0]["address"], "192.168.2.204")
        self.assertEqual(found[0]["hardware"], "DS-2DE2A404IW-DE3")

    def test_one_device_answering_twice_is_one_entry(self):
        # The same camera on both stacks, which is what an operator sees as
        # duplicates in other tools.
        found = collect((HIKVISION, "192.168.2.204"),
                        (HIKVISION, "fe80::be9b:5eff:fe70:22ac"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["address"], "192.168.2.204")

    def test_cameras_sort_before_other_devices(self):
        found = collect((WINDOWS_PC, "192.168.2.86"),
                        (REOLINK, "192.168.2.208"))
        self.assertEqual([d["camera"] for d in found], [True, False])

    def test_the_reolink_port_is_not_carried_into_the_address(self):
        # Its ONVIF service is on :8000 and its snapshot is on :80. Taking
        # the port would build a URL that cannot work.
        found = collect((REOLINK, "192.168.2.208"))
        self.assertEqual(found[0]["address"], "192.168.2.208")

    def test_addresses_come_out_in_numeric_order(self):
        d1 = probe_match("uuid:1", "http://192.168.2.9/x", "dn:NetworkVideoTransmitter", "")
        d2 = probe_match("uuid:2", "http://192.168.2.10/x", "dn:NetworkVideoTransmitter", "")
        found = collect((d2, "192.168.2.10"), (d1, "192.168.2.9"))
        self.assertEqual([d["address"] for d in found],
                         ["192.168.2.9", "192.168.2.10"])


class TestVendorPreselection(unittest.TestCase):

    def test_a_vendor_in_the_name_selects_its_preset(self):
        self.assertEqual(
            setup.wsd_preset({"name": "Dahua", "hardware": "IPC-HFW5442E-SE"}),
            "Dahua / Amcrest")
        self.assertEqual(
            setup.wsd_preset({"name": "HIKVISION DS-2CD1723G0-IZ",
                              "hardware": "DS-2CD1723G0-IZ"}),
            "Hikvision (ISAPI)")

    def test_an_unrecognised_device_selects_nothing(self):
        # Measured: a Reolink calls itself IPC-BO and a TP-Link Tapo calls
        # itself TC40. Neither names its maker, and a wrong preselection is
        # worse than none because it is a wrong URL that looks deliberate.
        self.assertEqual(setup.wsd_preset({"name": "IPC-BO",
                                           "hardware": "D340P"}), "")
        self.assertEqual(setup.wsd_preset({"name": "TC40",
                                           "hardware": "TC40"}), "")

    def test_every_hint_names_a_preset_that_exists(self):
        labels = [p[0] for p in setup.CAMERA_PRESETS]
        for _needle, label in setup.VENDOR_HINTS:
            self.assertIn(label, labels)


class TestFillTemplate(unittest.TestCase):
    """The one place a preset becomes a URL.

    Both wizards and the window's identify_camera() come here, so what a
    preset produces cannot be two different strings depending on who asked.
    """

    def preset(self, label):
        for entry in setup.CAMERA_PRESETS:
            if entry[0] == label:
                return entry
        raise AssertionError("no preset called %s" % label)

    def test_credentials_are_filled_in(self):
        url = setup.fill_template(self.preset("Reolink")[3], "192.0.2.5",
                                  "admin", "s3cret")
        self.assertIn("user=admin", url)
        self.assertIn("password=s3cret", url)

    def test_an_open_stream_carries_no_userinfo(self):
        """An RTSP stream with no login is a real thing, and rtsp://:@host is
        a legal but strange way to spell one: it reads as a mistake, and it
        invites a client to attempt an authentication nobody asked for."""
        url = setup.fill_template(self.preset("RTSP only (no snapshot URL)")[3],
                                  "192.0.2.5", "", "")
        self.assertEqual(url, "rtsp://192.0.2.5:554/stream1")
        self.assertNotIn(":@", url)

    def test_a_username_with_no_password_is_left_alone(self):
        # A different and deliberate answer, not the same as having neither.
        url = setup.fill_template(self.preset("RTSP only (no snapshot URL)")[3],
                                  "192.0.2.5", "admin", "")
        self.assertEqual(url, "rtsp://admin:@192.0.2.5:554/stream1")

    def test_a_query_string_carrying_credentials_is_never_stripped(self):
        """A Reolink's &user=&password= is part of the address of the
        resource, not how it is authenticated, so removing it would change
        what is being asked for."""
        url = setup.fill_template(self.preset("Reolink")[3], "192.0.2.5",
                                  "", "")
        self.assertIn("user=", url)
        self.assertIn("password=", url)
        self.assertTrue(url.startswith("http://192.0.2.5/"))

    def test_an_ipv6_literal_keeps_its_brackets(self):
        url = setup.fill_template(self.preset("Axis")[3], "[2001:db8::5]",
                                  "", "")
        self.assertTrue(url.startswith("http://[2001:db8::5]/"))

    def test_the_console_wizard_builds_through_it(self):
        """Or the two front doors produce different URLs for one camera,
        which is the drift tools/ was deleted for.

        Counted rather than forbidden, because the one legitimate occurrence
        is fill_template()'s own body: a scan for the thing a rule bans will
        always find the code implementing it, which has bitten this project
        three times now.
        """
        source = Path(_support.SCRIPTS).joinpath(
            "timelapse_setup.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("template.format("), 1,
                         "a preset is being expanded outside fill_template()")


def jpeg(width, height, marker=0xC0, before=b""):
    """The smallest JPEG header carrying a size, with `before` in front of it.

    Built rather than fetched so a test can say what it is measuring. Only the
    header is real; nothing decodes this.
    """
    frame = (bytes(bytearray([0xFF, marker, 0x00, 0x11, 0x08,
                              height >> 8, height & 0xFF,
                              width >> 8, width & 0xFF, 0x03]))
             + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01")
    return b"\xff\xd8" + before + frame + b"\xff\xd9"


def app0(payload=b"JFIF\x00"):
    """An APP0 segment, which every real JPEG carries before its SOF."""
    return (b"\xff\xe0" + bytes(bytearray([(len(payload) + 2) >> 8,
                                           (len(payload) + 2) & 0xFF]))
            + payload)


class TestJpegSize(unittest.TestCase):
    """Reading a frame's size out of its own header.

    Worth having in Python rather than as an ffprobe call because picking the
    largest stream measures three images per test, and three subprocesses to
    read nine bytes is a poor trade.
    """

    def test_a_baseline_frame_is_measured(self):
        self.assertEqual(setup.jpeg_size(jpeg(2560, 1440)), (2560, 1440))

    def test_width_and_height_are_not_swapped(self):
        # The header stores height first, which is the easy one to get wrong
        # and the hard one to notice on a square test image.
        self.assertEqual(setup.jpeg_size(jpeg(640, 360)), (640, 360))

    def test_segments_before_the_frame_header_are_stepped_over(self):
        self.assertEqual(setup.jpeg_size(jpeg(1920, 1080, before=app0())),
                         (1920, 1080))
        self.assertEqual(
            setup.jpeg_size(jpeg(1920, 1080,
                                 before=app0() + app0(b"Exif\x00" + b"x" * 40))),
            (1920, 1080))

    def test_a_progressive_frame_is_measured_the_same_way(self):
        # SOF2 rather than SOF0, and the size sits in the same four bytes.
        self.assertEqual(setup.jpeg_size(jpeg(800, 600, marker=0xC2)),
                         (800, 600))

    def test_a_fill_byte_before_a_marker_is_legal(self):
        self.assertEqual(setup.jpeg_size(jpeg(320, 240, before=b"\xff")),
                         (320, 240))

    def test_nothing_is_claimed_for_what_is_not_a_jpeg(self):
        for data in (b"", None, b"not an image at all", b"\x89PNG\r\n\x1a\n",
                     b"\xff\xd8", b"\xff\xd8\xff"):
            self.assertIsNone(setup.jpeg_size(data), repr(data))

    def test_a_frame_with_no_size_in_it_is_not_guessed_at(self):
        """A scan with no SOF before it is a JPEG this cannot measure, and
        the answer to that is None rather than a plausible number."""
        self.assertIsNone(setup.jpeg_size(b"\xff\xd8" + app0() + b"\xff\xda"
                                          + b"\x00\x08" + b"\x01" * 6))

    def test_the_shape_three_real_dahua_cameras_send(self):
        """Measured 2026-08-18 against 192.168.2.202, .203 and .205, all
        three identical: a COM segment declaring 4094 bytes, then **608 zero
        bytes belonging to no segment at all**, then a second COM, then the
        frame header. Stepping by the declared length lands in the padding,
        which is why every Dahua reported no dimensions while its picture
        drew perfectly: ffmpeg resynchronises and says nothing about it.
        """
        comment = b"\xff\xfe\x0f\xfe" + b"D" * 4092
        padding = b"\x00" * 608
        second = b"\xff\xfe\x00\xfe" + b"D" * 252
        self.assertEqual(
            setup.jpeg_size(jpeg(2560, 1440,
                                 before=app0() + comment + padding + second)),
            (2560, 1440))

    def test_padding_that_is_not_zero_is_stepped_over_too(self):
        """Only one vendor's padding has been measured, and keying the fix on
        its byte value would be fitting the rule to the one sample."""
        self.assertEqual(
            setup.jpeg_size(jpeg(800, 600,
                                 before=app0() + b"\x11\x22\x33" * 40)),
            (800, 600))

    def test_the_hunt_is_bounded(self):
        """Or a corrupt file turns a nine-byte header read into a scan of the
        whole picture, three times per test."""
        junk = bytes(bytearray([0x7F])) * (setup.RESYNC_BUDGET + 4096)
        self.assertIsNone(setup.jpeg_size(b"\xff\xd8" + junk + jpeg(64, 48)[2:]))

    def test_something_that_is_not_a_frame_header_is_not_believed(self):
        """After a resync, the alternative to checking is a plausible number
        read out of the wrong place, and a wrong size picks the wrong
        stream."""
        # FFC0 with a sample precision of 3, which no JPEG has.
        bogus = b"\xff\xc0\x00\x11\x03\x01\x02\x03\x04" + b"\x00" * 8
        self.assertIsNone(setup.jpeg_size(b"\xff\xd8" + bogus + b"\xff\xda"
                                          + b"\x00\x08" + b"\x01" * 6))
        # And a frame header claiming a zero dimension is not a measurement.
        self.assertIsNone(setup.jpeg_size(jpeg(0, 1080)))

    def test_a_lost_segment_chain_stops_rather_than_hunting(self):
        self.assertIsNone(setup.jpeg_size(b"\xff\xd8" + b"garbage" * 20))


class TestStreamKnobs(unittest.TestCase):
    """Which stream a URL selects, and what else the camera may offer.

    Three presets carry a selector. The rest test once, because guessing a
    URL a camera never advertised is a request that can only fail.
    """

    def preset(self, label):
        for entry in setup.CAMERA_PRESETS:
            if entry[0] == label:
                return entry
        raise AssertionError("no preset called %s" % label)

    def test_each_knob_is_recognised_in_its_own_templates_url(self):
        for label, token in (("Dahua / Amcrest", "subtype=0"),
                             ("Hikvision (ONVIF)", "profile=1"),
                             ("Hikvision (ISAPI)", "stream=1"),
                             ("Generic ONVIF snapshot", "profile=1")):
            url = setup.fill_template(self.preset(label)[3], "10.0.0.5", "", "")
            self.assertEqual(setup.stream_token(url), token, label)

    def test_a_preset_with_no_selector_offers_nothing(self):
        for label in ("Reolink", "Axis", "RTSP only (no snapshot URL)"):
            url = setup.fill_template(self.preset(label)[3], "10.0.0.5",
                                      "u", "p")
            self.assertEqual(setup.stream_token(url), "", label)
            self.assertEqual(setup.stream_candidates(url), [], label)

    def test_the_configured_stream_is_never_among_the_candidates(self):
        url = setup.fill_template(self.preset("Dahua / Amcrest")[3],
                                  "10.0.0.5", "", "")
        tokens = [token for token, _u in setup.stream_candidates(url)]
        self.assertEqual(tokens, ["subtype=1", "subtype=2"])

    def test_a_candidate_differs_from_the_configured_url_in_one_place(self):
        url = setup.fill_template(self.preset("Generic ONVIF snapshot")[3],
                                  "10.0.0.5", "", "")
        got = dict(setup.stream_candidates(url))
        self.assertEqual(got["profile=2"],
                         "http://10.0.0.5/onvif-http/snapshot?Profile_2")
        self.assertEqual(sorted(got), ["profile=2", "profile=3"])

    def test_only_the_stream_digit_of_an_isapi_channel_moves(self):
        """101 is camera 1 stream 1 and 201 is camera 2 stream 1, so the
        camera number has to survive being switched to the sub-stream."""
        url = "http://10.0.0.5/ISAPI/Streaming/channels/201/picture"
        self.assertEqual(setup.stream_token(url), "stream=1")
        self.assertEqual(setup.stream_candidates(url),
                         [("stream=2",
                           "http://10.0.0.5/ISAPI/Streaming/channels/202/"
                           "picture")])

    def test_setting_a_stream_is_reversible(self):
        url = setup.fill_template(self.preset("Dahua / Amcrest")[3],
                                  "10.0.0.5", "", "")
        moved = setup.with_stream(url, "subtype=2")
        self.assertNotEqual(moved, url)
        self.assertEqual(setup.stream_token(moved), "subtype=2")
        self.assertEqual(setup.with_stream(moved, "subtype=0"), url)

    def test_a_url_with_no_knob_is_returned_untouched(self):
        for token in ("subtype=1", "profile=2", "stream=2", "", None):
            self.assertEqual(setup.with_stream("http://10.0.0.5/snap.jpg",
                                               token),
                             "http://10.0.0.5/snap.jpg")

    def test_a_knob_is_not_read_out_of_another_makes_url(self):
        """Reolink's channel=0 and an RTSP /stream1 both look enough like a
        selector to be mistaken for one, and neither selects anything this
        can change."""
        for url in ("http://10.0.0.5/cgi-bin/api.cgi?cmd=Snap&channel=0",
                    "rtsp://10.0.0.5:554/stream1"):
            self.assertEqual(setup.stream_token(url), "", url)


class FakeResponse(object):
    def __init__(self, status, content):
        self.status_code = status
        self.content = content


class FakeSession(object):
    """Enough of requests.Session for grab_snapshot, and a record of the asks.

    The record is the point in two of the tests below: how many times a camera
    is contacted is a property worth pinning, not an implementation detail.
    """

    def __init__(self, reply):
        self.auth = None
        self.reply = reply
        self.asked = []

    def get(self, url, timeout=None):
        self.asked.append((url, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply(url) if callable(self.reply) else self.reply


@contextlib.contextmanager
def fake_requests(reply):
    """Stand a fake `requests` up in sys.modules for the duration.

    grab_snapshot imports it inside the function, so this reaches it. Patching
    an attribute would not: there is no module-level name to patch, which is
    deliberate, since the daemon must run where requests is absent.
    """
    import types
    session = FakeSession(reply)
    module = types.ModuleType("requests")
    module.Session = lambda: session
    auth = types.ModuleType("requests.auth")
    auth.HTTPBasicAuth = lambda user, password: ("basic", user, password)
    auth.HTTPDigestAuth = lambda user, password: ("digest", user, password)
    module.auth = auth
    was = {name: sys.modules.get(name)
           for name in ("requests", "requests.auth")}
    sys.modules["requests"] = module
    sys.modules["requests.auth"] = auth
    try:
        yield session
    finally:
        for name, before in was.items():
            if before is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = before


class TestGrabSnapshot(unittest.TestCase):
    """One fetch, reported rather than printed.

    The graphical wizard shows the frame and measures it, and neither is
    reachable through a report that goes to a console pythonw does not have.
    """

    def setUp(self):
        self.cfg = {"capture": {"timeout_seconds": 6, "interval_seconds": 5}}
        self.cam = {"method": "http", "url": "http://10.0.0.5/snap.jpg",
                    "auth": "digest", "username": "u", "password": "p"}

    def test_a_frame_comes_back_measured(self):
        with fake_requests(FakeResponse(200, jpeg(2560, 1440))):
            data, detail = setup.grab_snapshot(self.cam, self.cfg)
        self.assertTrue(data)
        self.assertEqual(detail["size"], (2560, 1440))
        self.assertEqual(detail["status"], 200)
        self.assertEqual(detail["bytes"], len(data))
        self.assertEqual(detail["reason"], "")

    def test_every_key_exists_however_the_fetch_went(self):
        """A caller reading detail['said'] must not have to know how far the
        request got. Four kinds of failure, one shape of answer."""
        replies = [FakeResponse(401, b""), FakeResponse(503, b""),
                   FakeResponse(200, b"<html>no</html>"), OSError("down")]
        for reply in replies:
            with fake_requests(reply):
                data, detail = setup.grab_snapshot(self.cam, self.cfg)
            self.assertIsNone(data, repr(reply))
            self.assertTrue(detail["reason"], repr(reply))
            for key in ("said", "notes", "status", "seconds", "bytes", "size",
                        "skipped"):
                self.assertIn(key, detail, repr(reply))

    def test_a_401_is_reported_as_itself(self):
        with fake_requests(FakeResponse(401, b"")):
            _data, detail = setup.grab_snapshot(self.cam, self.cfg)
        self.assertEqual(detail["status"], 401)
        self.assertIn("401", detail["reason"])

    def test_a_camera_that_answers_200_with_a_complaint_is_quoted(self):
        """Reolink refuses a bad password with 200 and a JSON error, so the
        body is where the real cause is. Reporting only 'not a JPEG' hides
        it."""
        body = (b'[{"cmd":"Snap","code":1,"error":{"detail":"login failed",'
                b'"rspCode":-7}}]')
        with fake_requests(FakeResponse(200, body)):
            _data, detail = setup.grab_snapshot(self.cam, self.cfg)
        self.assertEqual(detail["said"], "login failed")
        self.assertTrue(detail["notes"])

    def test_the_camera_is_contacted_exactly_once(self):
        with fake_requests(FakeResponse(200, jpeg(640, 480))) as session:
            setup.grab_snapshot(self.cam, self.cfg)
        self.assertEqual(len(session.asked), 1)
        self.assertEqual(session.asked[0][0], self.cam["url"])

    def test_the_timeout_leaves_room_beyond_the_configured_one(self):
        with fake_requests(FakeResponse(200, jpeg(640, 480))) as session:
            setup.grab_snapshot(self.cam, self.cfg)
        self.assertEqual(session.asked[0][1], 10)

    def test_nothing_fetched_at_all_is_not_the_cameras_fault(self):
        """Third answer, not a second. requests being absent and the camera
        refusing need opposite remedies, and a bare False says neither."""
        was = {name: sys.modules.get(name)
               for name in ("requests", "requests.auth")}
        sys.modules["requests"] = None          # import raises ImportError
        try:
            data, detail = setup.grab_snapshot(self.cam, self.cfg)
        finally:
            for name, before in was.items():
                if before is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = before
        self.assertIsNone(data)
        self.assertTrue(detail["skipped"])
        self.assertIsNone(detail["status"])

    def test_the_console_wizard_still_prints_what_it_always_did(self):
        out = io.StringIO()
        with fake_requests(FakeResponse(200, jpeg(2560, 1440))), \
             contextlib.redirect_stdout(out):
            self.assertTrue(setup.test_camera(dict(self.cam), self.cfg))
        printed = out.getvalue()
        self.assertIn("2560x1440", printed)
        self.assertIn("KB", printed)

    def test_the_console_wizard_still_offers_the_other_auth_scheme(self):
        """A 401 under digest is often basic, and the console has a terminal
        to ask on. Answering no must not then retry anyway.

        The terminal is declared rather than inherited: without that this
        test passes on the headless path, which never asks at all, so it
        would agree with a version that had stopped offering the retry.
        """
        out = io.StringIO()
        prev = (setup._TTY, setup.AUTO)
        setup._TTY, setup.AUTO = FakeTTY(""), False
        try:
            with fake_requests(FakeResponse(401, b"")) as session, \
                 contextlib.redirect_stdout(out), \
                 mock.patch.object(setup, "ask_yes", return_value=False):
                self.assertFalse(setup.test_camera(dict(self.cam), self.cfg))
        finally:
            setup._TTY, setup.AUTO = prev
        self.assertEqual(len(session.asked), 1)
        self.assertIn("401", out.getvalue())


class TestTheAuthRetryIsBounded(unittest.TestCase):
    """A 401 offers the other auth scheme once, and only where it can be
    answered.

    Platform-independent, unprivileged, and it declares its own terminal
    rather than inheriting the runner's, since whether there is one is the
    whole subject.
    """

    def setUp(self):
        self.cfg = {"capture": {"timeout_seconds": 6, "interval_seconds": 5}}
        self.cam = {"method": "http", "url": "http://10.0.0.5/snap.jpg",
                    "auth": "digest", "username": "u", "password": "p"}
        prev = (setup._TTY, setup.AUTO)
        self.addCleanup(lambda: setattr(setup, "_TTY", prev[0]))
        self.addCleanup(lambda: setattr(setup, "AUTO", prev[1]))

    def drive(self, reply, tty=None, auto=False):
        setup._TTY, setup.AUTO = tty, auto
        out = io.StringIO()
        with fake_requests(reply) as session, contextlib.redirect_stdout(out):
            result = setup.test_camera(self.cam, self.cfg)
        return result, session, out.getvalue()

    def test_under_defaults_the_camera_is_asked_exactly_once(self):
        """AUTO answers every question with its default and this default is
        yes, so the unguarded version asked itself to flip digest to basic to
        digest until the stack ran out. run_camera_action() refuses the
        writing actions headless and lets "test" through, so
        `timelapse cameras -t:Gate` from cron reaches it.
        """
        result, session, _ = self.drive(FakeResponse(401, b""), auto=True)
        self.assertFalse(result)
        self.assertEqual(len(session.asked), 1)

    def test_with_no_terminal_the_camera_is_asked_exactly_once(self):
        """The other half of the same condition. A scheduled task has no
        terminal even when nobody passed --defaults."""
        result, session, _ = self.drive(FakeResponse(401, b""), tty=None)
        self.assertFalse(result)
        self.assertEqual(len(session.asked), 1)

    def test_it_names_the_other_scheme_instead_of_asking_about_it(self):
        """The question carried the one useful fact in it. Printing it keeps
        that where nobody can answer."""
        _, _, printed = self.drive(FakeResponse(401, b""), auto=True)
        self.assertIn("basic", printed)
        self.assertNotIn("Retry with auth", printed)

    def test_a_camera_that_refuses_both_schemes_asks_once_not_for_ever(self):
        """Interactively, with somebody answering yes every time. Offering
        the flip is worth doing; offering it again after it has been made is
        the same loop with a person inside it."""
        asked = []

        def yes(question, default=True):
            asked.append(question)
            return True

        with mock.patch.object(setup, "ask_yes", side_effect=yes):
            result, session, _ = self.drive(FakeResponse(401, b""),
                                            tty=FakeTTY(""))
        self.assertFalse(result)
        self.assertEqual(len(session.asked), 2)
        self.assertEqual(len(asked), 1)

    def test_the_flip_still_happens_and_still_works(self):
        """The control. Without it, "asked once" would be satisfied by a
        version that had simply stopped retrying."""
        replies = [FakeResponse(401, b""), FakeResponse(200, jpeg(1920, 1080))]
        with mock.patch.object(setup, "ask_yes", return_value=True):
            result, session, _ = self.drive(lambda url: replies.pop(0),
                                            tty=FakeTTY(""))
        self.assertTrue(result)
        self.assertEqual(self.cam["auth"], "basic")
        self.assertEqual(len(session.asked), 2)


class TestRenderPng(unittest.TestCase):
    """Converting a fetched frame into something tkinter can draw.

    tkinter reads PNG, GIF and PPM and no JPEG at all, so this is what stands
    between a camera's answer and the operator seeing it.
    """

    def cfg(self):
        return {"paths": {"ffmpeg": "ffmpeg"}}

    def test_the_frame_is_piped_rather_than_written_to_disk(self):
        """A snapshot on its way to a preview has no business becoming a file:
        it would land in a temp directory this project does not control, and
        the camera's own view of a house is not something to leave lying
        about."""
        with mock.patch.object(setup.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0,
                                         stdout=b"\x89PNG\r\n\x1a\n" + b"x")
            setup.render_png(self.cfg(), b"\xff\xd8jpeg", 240, 135)
        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(kwargs["input"], b"\xff\xd8jpeg")
        self.assertIn("-", argv)
        self.assertEqual(argv[-1], "-")
        self.assertIn("scale=240:135:force_original_aspect_ratio=decrease",
                      argv)

    def test_an_unscaled_conversion_asks_for_no_filter(self):
        with mock.patch.object(setup.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0,
                                         stdout=b"\x89PNG\r\n\x1a\n")
            setup.render_png(self.cfg(), b"\xff\xd8jpeg")
        self.assertNotIn("-vf", run.call_args[0][0])

    def test_anything_that_is_not_a_png_is_refused(self):
        """ffmpeg exiting 0 having written a warning is not a picture, and
        handing that to PhotoImage raises inside the draw."""
        for returncode, stdout in ((0, b"not a png"), (1, b""),
                                   (0, b""), (0, None)):
            with mock.patch.object(setup.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=returncode,
                                             stdout=stdout)
                self.assertIsNone(
                    setup.render_png(self.cfg(), b"\xff\xd8jpeg", 8, 8))

    def test_a_machine_with_no_ffmpeg_loses_the_picture_and_nothing_else(self):
        with mock.patch.object(setup.subprocess, "run",
                               side_effect=FileNotFoundError()):
            self.assertIsNone(setup.render_png(self.cfg(), b"\xff\xd8jpeg"))


class TestDiscoveryIsNeverFatal(unittest.TestCase):
    """Discovery is an offer. Nothing it does may stop a camera being added."""

    def test_no_socket_means_no_devices_not_an_exception(self):
        with mock.patch.object(setup, "wsd_open_sockets", return_value=[]):
            self.assertEqual(setup.discover_cameras(), [])

    def test_a_failing_scan_still_lets_the_wizard_continue(self):
        with mock.patch.object(setup, "ask_yes", return_value=True), \
             mock.patch.object(setup, "discover_cameras",
                               side_effect=OSError("no route to host")):
            self.assertEqual(setup.offer_discovery(), [])

    def test_declining_the_scan_probes_nothing(self):
        with mock.patch.object(setup, "ask_yes", return_value=False), \
             mock.patch.object(setup, "discover_cameras") as scan:
            self.assertEqual(setup.offer_discovery(), [])
        scan.assert_not_called()


def got_dev(parsed):
    """Adapt a parsed reply to the shape wsd_scope() reads."""
    return {"scopes": parsed["scopes"]}


class TestIPv6CameraAddresses(unittest.TestCase):
    """A camera reachable only over IPv6 must be configurable.

    The daemon itself needs no change: requests and ffmpeg both fetch a
    bracketed IPv6 URL, measured against a real camera. The wizard was the
    gap, because it formats whatever was typed straight into a template.
    """

    def test_a_hostname_is_untouched(self):
        self.assertEqual(setup.url_host("cam.example.net"), "cam.example.net")

    def test_an_ipv4_address_is_untouched(self):
        self.assertEqual(setup.url_host("192.168.2.204"), "192.168.2.204")

    def test_an_ipv6_literal_is_bracketed(self):
        self.assertEqual(setup.url_host("fdd2:49bd:6a04:a0b::1"),
                         "[fdd2:49bd:6a04:a0b::1]")

    def test_an_already_bracketed_address_is_not_double_bracketed(self):
        self.assertEqual(setup.url_host("[fdd2::1]"), "[fdd2::1]")

    def test_a_zone_id_survives(self):
        # The zone is part of the address, not a separator, so it belongs
        # inside the brackets.
        self.assertEqual(setup.url_host("fe80::1%eth0"), "[fe80::1%eth0]")

    def test_surrounding_whitespace_does_not_defeat_detection(self):
        self.assertEqual(setup.url_host("  fdd2::1  "), "[fdd2::1]")

    def test_every_preset_builds_a_parseable_url(self):
        # The failure this pins is silent: an unbracketed literal produces a
        # URL whose colons read as a port, and the error surfaces later as
        # "failed to resolve hostname fdd2".
        host = setup.url_host("fdd2:49bd:6a04:a0b:be9b:5eff:fe70:22ac")
        for label, _method, _auth, template in setup.CAMERA_PRESETS:
            if not template:
                continue                      # the custom-URL entry
            url = template.format(ip=host, user="admin", password="x")
            parsed = urlparse(url)
            self.assertEqual(parsed.hostname,
                             "fdd2:49bd:6a04:a0b:be9b:5eff:fe70:22ac",
                             "%s built an unusable URL: %s" % (label, url))

    def test_the_rtsp_preset_keeps_its_port(self):
        # rtsp://user:pass@[addr]:554/... is the shape that goes wrong first,
        # because it has userinfo and a port either side of the address.
        template = dict((p[0], p[3]) for p in setup.CAMERA_PRESETS)[
            "RTSP only (no snapshot URL)"]
        url = template.format(ip=setup.url_host("fdd2::1"), user="admin",
                              password="x")
        parsed = urlparse(url)
        self.assertEqual(parsed.hostname, "fdd2::1")
        self.assertEqual(parsed.port, 554)


class TestStartCapture(unittest.TestCase):
    """Starting a service that has never run, which is not restarting one.

    install.sh ends an install with `systemctl enable --now`; the Windows port
    had no counterpart, so a fresh install registered a delayed-auto-start
    service and left it stopped until the next reboot. Reported from a real
    install, where the graphical wizard said capture was already running.
    """

    def test_a_service_already_running_is_left_alone(self):
        with mock.patch.object(setup, "service_is_active", return_value=True),              mock.patch.object(setup, "start_service") as started:
            self.assertEqual(setup.start_capture(), (True, ""))
        started.assert_not_called()

    def test_a_stopped_service_is_started(self):
        with mock.patch.object(setup, "service_is_active", return_value=False),              mock.patch.object(setup, "start_service",
                               return_value=(True, "")) as started:
            self.assertEqual(setup.start_capture(), (True, ""))
        started.assert_called_once_with(setup.CAPTURE_UNIT)

    def test_an_unanswerable_state_is_tried_rather_than_assumed(self):
        """service_is_active() answers three ways and None means the question

        could not be put. That is not "stopped", but it is no reason to refuse
        either: start_service() waits for RUNNING, so attempting it gives an
        authoritative answer where treating None as either state would be a
        guess.
        """
        with mock.patch.object(setup, "service_is_active", return_value=None),              mock.patch.object(setup, "start_service",
                               return_value=(False, "access denied")) as started:
            self.assertEqual(setup.start_capture(), (False, "access denied"))
        started.assert_called_once_with(setup.CAPTURE_UNIT)

    def test_it_is_not_folded_into_restart_units(self):
        """They answer opposite questions and merging them would lose that.

        restart_units() must keep skipping a service that is not running,
        because its job is an upgrade: restarting one the operator had stopped
        would overrule a decision they made deliberately.
        """
        with mock.patch.object(setup, "service_is_active", return_value=False),              mock.patch.object(setup, "restart_service") as restarted:
            setup.restart_units()
        restarted.assert_not_called()


class TestLocaliseLocations(unittest.TestCase):
    """A template is a source of settings, not of locations.

    `config/config.example.json` is a Linux document and the Windows installer
    passes it to the wizard, so a real install wrote a config whose daemons
    publish their heartbeat to `/var/lib/timelapse/state`. Nothing failed
    loudly: it resolved against whatever drive was current, and `timelapse
    test` reported the directory missing with advice to re-run the wizard,
    which would have written the same thing again.

    The trap was already recorded for CI, where the Windows job runs the wizard
    *without* a template for exactly this reason. Then the installer was given
    one.
    """

    def linux_template(self):
        return {"paths": {"state_dir": "/var/lib/timelapse/state"},
                "web": {"state_dir": "/var/lib/timelapse/web"}}

    def windows_template(self):
        return {"paths": {"state_dir": r"C:\ProgramData\timelapse\state"},
                "web": {"state_dir": r"C:\ProgramData\timelapse\web"}}

    def test_a_foreign_state_dir_is_replaced_with_this_platforms(self):
        foreign = (self.linux_template() if plat.IS_WINDOWS
                   else self.windows_template())
        cfg = setup.localise_locations(foreign)
        self.assertEqual(cfg["paths"]["state_dir"], plat.STATE_DIR_DEFAULT)
        self.assertEqual(cfg["web"]["state_dir"], plat.WEB_STATE_DIR_DEFAULT)

    def test_a_foreign_ffmpeg_path_does_not_reach_the_wizard(self):
        """Reported from a real Windows install 2026-08-18: the graphical

        wizard's ffmpeg box was pre-filled with `/usr/bin/ffmpeg`.

        This function used to cover the two state directories alone, on the
        stated reasoning that ffmpeg is *asked* about so a wrong value could
        never survive. Being asked is not the same as being typed: every one of
        those questions offers the config's value as its default, so it
        survives by being accepted, and in a window a filled field does not
        read as a default at all. It reads as a discovered answer.
        """
        foreign = ({"paths": {"ffmpeg": "/usr/bin/ffmpeg",
                              "ffprobe": "/usr/bin/ffprobe"}}
                   if plat.IS_WINDOWS else
                   {"paths": {"ffmpeg": r"C:\ffmpeg\bin\ffmpeg.exe",
                              "ffprobe": r"C:\ffmpeg\bin\ffprobe.exe"}})
        paths = setup.localise_locations(foreign)["paths"]
        wanted = setup.ffmpeg_defaults()
        self.assertEqual((paths["ffmpeg"], paths["ffprobe"]), wanted)

    def test_the_windows_answer_is_empty_rather_than_a_guess(self):
        """find_binary()'s rule, and the reason it is not cosmetic.

        On Linux the installer has put ffmpeg at /usr/bin/ffmpeg by the time
        any of this runs, so a wrong default is merely unhelpful. On Windows
        nothing installed it, so a made-up path would be offered as though it
        existed, accepted, and discovered at the first encode. Empty means "no
        default", and the wizard then detects or insists.
        """
        with mock.patch.object(setup, "IS_WINDOWS", True):
            self.assertEqual(setup.ffmpeg_defaults(), ("", ""))
        with mock.patch.object(setup, "IS_WINDOWS", False):
            self.assertEqual(setup.ffmpeg_defaults(),
                             ("/usr/bin/ffmpeg", "/usr/bin/ffprobe"))

    def test_the_no_template_default_config_is_localised_too(self):
        """Where the reported defect actually came from.

        Not a template at all: default_config()'s no-template branch hard-coded
        the Linux tool paths, three lines below a state_dir that was carefully
        platform-aware, and the graphical wizard calls exactly that branch
        because it starts from default_config() with no template when there is
        no config yet.
        """
        paths = setup.default_config()["paths"]
        self.assertEqual((paths["ffmpeg"], paths["ffprobe"]),
                         setup.ffmpeg_defaults())
        for key in ("ffmpeg", "ffprobe", "state_dir"):
            self.assertFalse(setup.foreign_path(paths[key]), key)

    def test_a_foreign_data_directory_is_replaced_as_well(self):
        # Same class as ffmpeg and found by looking rather than by being
        # reported: the graphical wizard fills its Data folder box from
        # frames_root, so a template's /var/lib/timelapse arrives there too.
        foreign = ({"paths": {"frames_root": "/var/lib/timelapse/frames"}}
                   if plat.IS_WINDOWS else
                   {"paths": {"frames_root": r"C:\timelapse\frames"}})
        cfg = setup.localise_locations(foreign)
        self.assertFalse(setup.foreign_path(cfg["paths"]["frames_root"]))

    def test_the_transfer_destination_is_deliberately_not_touched(self):
        """Clearing it would silently disarm a configured transfer, which is

        worse than a wrong path the wizard asks about and the pre-flight
        probes by writing to it.
        """
        foreign = ({"transfer": {"destination": "/mnt/nas/tl"}}
                   if plat.IS_WINDOWS else
                   {"transfer": {"destination": r"U:\TL"}})
        before = json.dumps(foreign)
        self.assertEqual(json.dumps(setup.localise_locations(foreign)), before)

    def test_this_platforms_own_paths_are_left_exactly_alone(self):
        native = (self.windows_template() if plat.IS_WINDOWS
                  else self.linux_template())
        before = json.dumps(native)
        self.assertEqual(json.dumps(setup.localise_locations(native)), before)

    def test_a_deliberate_choice_elsewhere_on_this_platform_survives(self):
        """Only the *other* platform's shapes are corrected. An operator who

        put the state directory on another disk chose that, and a wizard that
        moved it back would be overriding them.
        """
        chosen = r"E:\timelapse\state" if plat.IS_WINDOWS else "/srv/tl/state"
        cfg = setup.localise_locations({"paths": {"state_dir": chosen}})
        self.assertEqual(cfg["paths"]["state_dir"], chosen)

    def test_it_corrects_what_is_there_and_invents_nothing(self):
        """It runs on the single write path, so it sees every config anything

        writes. The first version used setdefault() and added empty "paths"
        and "web" sections to configs that had neither; its own test caught
        that by comparing the written file byte for byte.
        """
        cfg = {"marker": "x"}
        self.assertEqual(setup.localise_locations(cfg), {"marker": "x"})

    def test_an_empty_or_relative_value_is_not_evidence_of_a_platform(self):
        for given in ("", "   ", "state", "./state", None):
            self.assertFalse(setup.foreign_path(given), repr(given))

    def test_a_unc_path_belongs_to_neither(self):
        self.assertFalse(setup.foreign_path(r"\\tower\cctv\state"))

    def test_the_template_load_localises(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp), True)
        template = tmp / "config.example.json"
        template.write_text(json.dumps(self.linux_template()),
                            encoding="utf-8")
        cfg = setup.default_config(str(template))
        self.assertEqual(cfg["paths"]["state_dir"], plat.STATE_DIR_DEFAULT)

    def test_writing_localises_too(self):
        """So a config that arrived by any other route is corrected the next

        time anything touches it, rather than only when the whole wizard runs.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(tmp), True)
        out = tmp / "config.json"
        setup.write_config(self.linux_template()
                           if plat.IS_WINDOWS else self.windows_template(),
                           str(out))
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["paths"]["state_dir"], plat.STATE_DIR_DEFAULT)


class TestSummarySinks(unittest.TestCase):
    """The summary must describe what the questions just configured.

    It read cfg['discord'] directly until 0.2.0, which is the legacy block
    alone, so an operator who had just set up ntfy or Telegram was shown
    "Discord  disabled" and nothing about what they had done. Wrong on Linux
    since 0.1.6; noticed only when a Windows transcript was read line by line.
    """

    def test_the_new_sinks_are_named(self):
        cfg = {"notify": [{"type": "ntfy", "enabled": True, "topic": "x"},
                          {"type": "telegram", "enabled": True, "token": "t"}]}
        # As each service spells itself. "ntfy" keeps its lower case, which is
        # the product's name rather than a missing capital.
        self.assertEqual(setup.summarise_sinks(cfg), "ntfy, Telegram")

    def test_a_disabled_sink_is_not_named(self):
        cfg = {"notify": [{"type": "ntfy", "enabled": False},
                          {"type": "discord", "enabled": True,
                           "webhook_url": "u"}]}
        self.assertEqual(setup.summarise_sinks(cfg), "Discord")

    def test_nothing_configured_says_disabled(self):
        self.assertEqual(setup.summarise_sinks({"notify": []}), "disabled")
        self.assertEqual(setup.summarise_sinks({}), "disabled")

    def test_the_legacy_block_still_shows(self):
        # Every config written before 0.1.6 has one, and upgrades keep it.
        cfg = {"discord": {"enabled": True, "webhook_url": "https://x/y"}}
        self.assertEqual(setup.summarise_sinks(cfg), "Discord")

    def test_it_prints_no_credential(self):
        """A webhook URL is the authority to post, exactly as a bot token is,

        and this line gets pasted into bug reports.
        """
        cfg = {"notify": [
            {"type": "discord", "enabled": True,
             "webhook_url": "https://discord.com/api/webhooks/1/SECRET"},
            {"type": "telegram", "enabled": True, "token": "12345:SECRET",
             "chat_id": "99"}]}
        out = setup.summarise_sinks(cfg)
        for secret in ("SECRET", "discord.com", "12345", "99"):
            self.assertNotIn(secret, out, secret)

    def test_the_summary_line_uses_it(self):
        cfg = setup.default_config()
        cfg["notify"] = [{"type": "ntfy", "enabled": True, "topic": "x"}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            setup.summarise(cfg, "cfg.json")
        self.assertIn("ntfy", buf.getvalue())


class TestWindowsWebUI(unittest.TestCase):
    """It is not ported, and the question must not pretend otherwise."""

    def drive(self, keystrokes, windows):
        cfg = setup.default_config()
        buf = io.StringIO()
        with _support.wizard_tty(keystrokes):
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "IS_WINDOWS", windows):
                setup.choose_web(cfg)
        return cfg["web"], buf.getvalue()

    def test_windows_says_nothing_will_run_it(self):
        """Before the question, not after. A question that looks like every

        other question gets answered like every other question.
        """
        _web, out = self.drive("n\n", windows=True)
        self.assertIn("not installed as a service on windows", out.lower())
        self.assertIn("web-serve", out)
        marker = out.lower().index("not installed as a service")
        self.assertLess(marker, out.lower().index("?"))

    def test_windows_asks_a_different_question(self):
        _web, out = self.drive("n\n", windows=True)
        self.assertIn("web-serve", out.split("?")[0])

    def test_linux_is_unchanged(self):
        _web, out = self.drive("n\n", windows=False)
        self.assertIn("Enable the web UI?", out)
        self.assertNotIn("web-serve", out)

    def test_declining_leaves_it_off_on_both(self):
        for windows in (True, False):
            web, _out = self.drive("n\n", windows=windows)
            self.assertFalse(web["enabled"], windows)


class TestSshSpecDetection(unittest.TestCase):
    """user@host:/path is rsync's shape and only rsync's."""

    def test_it_recognises_an_rsync_remote_spec(self):
        for dest in ("user@nas:/vol/tl/", "root@192.0.2.9:/srv",
                     "backup@tower:timelapse"):
            self.assertTrue(setup.looks_like_ssh_spec(dest), dest)

    def test_a_drive_letter_is_not_a_host(self):
        """The case that makes a naive "contains a colon" test wrong, and it

        would refuse the most ordinary Windows answer there is.
        """
        for dest in (r"C:\videos", r"D:\timelapse\out", "C:/videos"):
            self.assertFalse(setup.looks_like_ssh_spec(dest), dest)

    def test_a_unc_path_is_not_a_host(self):
        self.assertFalse(setup.looks_like_ssh_spec(r"\\tower\cctv\TL"))

    def test_a_plain_path_is_not(self):
        for dest in ("/mnt/nas/timelapse", "videos", ""):
            self.assertFalse(setup.looks_like_ssh_spec(dest), dest)

    def test_an_email_shaped_folder_deeper_in_the_path_is_not(self):
        # The @ has to be before the colon, in the host part. A folder called
        # "a@b" further along is just a folder.
        self.assertFalse(setup.looks_like_ssh_spec("/srv/a@b:c"))


class TestWindowsTransfer(unittest.TestCase):
    """One question on Windows: a path this machine can write.

    Mounting a CIFS share and an rsync remote spec are both mechanisms Windows
    does not have, so the Linux menu would be offering two of three options
    that cannot work.
    """

    def drive(self, keystrokes, mapping=None, fixed="D",
              probe_ok=True, probe_why=""):
        """`fixed` names the drive letters that are real local disks here.

        Patched at plat.drive_kind rather than at setup.drive_is_local, and
        that is the point rather than a detail: mocking the wizard's own
        collaborator with a fixed return value meant the argument never
        reached it, which is exactly how drive_is_local came to want `U:` while
        every caller handed it `U:\\TL`. Patch below the seam you are testing.

        The write probe is stubbed, and that one is not a preference. It is
        the real thing: it mkdirs the destination and writes a file into it,
        so an unstubbed run of this class created D:\\timelapse\\out on the
        developer's disk and wrote to \\\\tower\\cctv\\TL on the author's NAS,
        from `python -m unittest`. **A unit test must not touch anything
        outside its own temp directory**, and the two functions are patched on
        timelapse_encode because that is the module that defines them, which
        works here only because the wizard imports them inside the function.
        """
        cfg = setup.default_config()
        buf = io.StringIO()
        mapping = mapping or {}
        with _support.wizard_tty(keystrokes):
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "IS_WINDOWS", True), \
                 mock.patch.object(plat, "IS_WINDOWS", True), \
                 mock.patch.object(plat, "drive_kind",
                                   side_effect=lambda root:
                                   plat.DRIVE_FIXED if root[0] in fixed else 1), \
                 mock.patch.object(enc, "try_destination",
                                   return_value=(probe_ok, probe_why)), \
                 mock.patch.object(enc, "verify_share_credentials",
                                   return_value=(probe_ok, probe_why)), \
                 mock.patch.object(setup, "network_path",
                                   side_effect=lambda p: mapping.get(str(p))):
                setup.choose_transfer(cfg)
        return cfg["transfer"], buf.getvalue()

    def test_declining_leaves_it_disabled(self):
        transfer, _ = self.drive("n\n")
        self.assertFalse(transfer["enabled"])

    def test_a_local_folder_is_stored_as_typed(self):
        transfer, _ = self.drive("y\nD:\\timelapse\\out\n")
        self.assertTrue(transfer["enabled"])
        self.assertEqual(transfer["destination"], "D:\\timelapse\\out")

    def test_a_unc_path_is_stored_as_typed(self):
        transfer, _ = self.drive("y\n\\\\tower\\cctv\\TL\nn\n")
        self.assertEqual(transfer["destination"], "\\\\tower\\cctv\\TL")

    def test_a_mapped_drive_is_stored_as_its_unc_target(self):
        """The single most likely way a Windows install fails, and it fails on

        a path the operator can open in Explorer: drive mappings belong to a
        logon session, and a service gets its own with none in it.
        """
        transfer, out = self.drive("y\nU:\\TL\nn\n",
                                   mapping={"U:\\TL": "\\\\tower\\cctv\\TL"})
        self.assertEqual(transfer["destination"], "\\\\tower\\cctv\\TL")
        self.assertIn("mapped drive", out)
        self.assertIn("\\\\tower\\cctv\\TL", out)

    def test_it_says_why_rather_than_silently_substituting(self):
        _transfer, out = self.drive("y\nU:\\TL\nn\n",
                                    mapping={"U:\\TL": "\\\\t\\c\\TL"})
        self.assertIn("logon session", out)

    def test_a_share_is_told_that_the_encode_runs_as_another_account(self):
        """The whole difficulty on this platform, said before the question.

        The wizard runs as the operator; the nightly encode runs as the system
        account, which presents the machine name on the network. A probe that
        passed here proves nothing about the job that matters, and an operator
        who is not told that will read a green line as a guarantee.
        """
        _transfer, out = self.drive("y\n\\\\tower\\cctv\\TL\nn\n")
        self.assertIn("system account", out)
        self.assertIn("\\\\tower\\cctv", out)

    def test_a_local_folder_is_not_asked_about_credentials(self):
        # There is no share to authenticate to, and LocalSystem is the most
        # privileged account on this machine, so the probe carries over.
        _transfer, out = self.drive("y\nD:\\timelapse\\out\n")
        self.assertNotIn("system account", out)
        self.assertNotIn("Store credentials", out)

    def test_credentials_are_stored_when_offered(self):
        transfer, out = self.drive(
            "y\n\\\\tower\\cctv\\TL\ny\nTOWER\\war\nhunter2\n")
        self.assertEqual(transfer["username"], "TOWER\\war")
        self.assertEqual(transfer["password"], "hunter2")
        self.assertNotIn("hunter2", out, "the password must not be echoed")

    def test_declining_credentials_stores_neither_key(self):
        # Absent rather than empty: "" is a real answer to "did you set one?"
        # and an empty username would make reach_destination() think it had
        # something to try.
        transfer, out = self.drive("y\n\\\\tower\\cctv\\TL\nn\n")
        self.assertNotIn("username", transfer)
        self.assertNotIn("password", transfer)
        self.assertIn("untested", out)

    def test_a_failed_probe_keeps_the_credentials_for_correction(self):
        transfer, out = self.drive(
            "y\n\\\\tower\\cctv\\TL\ny\nwar\nwrong\n",
            probe_ok=False, probe_why="the password was rejected (error 1326)")
        self.assertEqual(transfer["username"], "war")
        self.assertIn("1326", out)
        self.assertIn("will fail until this passes", out)

    def test_the_credentials_are_what_gets_tested_not_the_current_token(self):
        """reach_destination() tries the current token first, deliberately,
        because a share that already accepts this machine needs no password
        stored anywhere. On the machine a wizard runs on that token is the
        operator's own, so the probe file gets written by *them* and a
        password that was never presented to anything is reported as good.

        So the wizard calls verify_share_credentials(), which presents them
        and nothing else. The ordering inside it (drop whatever this session
        already holds, or the check cannot fail) is pinned one layer down, in
        test_encode.py, where that rule lives.
        """
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = False
        setup._TTY = FakeTTY("y\n\\\\tower\\cctv\\TL\ny\nwar\npw\n", tty=False)
        cfg = setup.default_config()
        called = []
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 mock.patch.object(setup, "IS_WINDOWS", True), \
                 mock.patch.object(plat, "IS_WINDOWS", True), \
                 mock.patch.object(enc, "try_destination",
                                   return_value=(False, "denied")), \
                 mock.patch.object(
                     enc, "reach_destination",
                     side_effect=lambda *a: (called.append("token"),
                                             (True, ""))[1]), \
                 mock.patch.object(
                     enc, "verify_share_credentials",
                     side_effect=lambda *a: (called.append("credentials"),
                                             (True, ""))[1]), \
                 mock.patch.object(setup, "network_path", return_value=None):
                setup.choose_transfer(cfg)
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto
        self.assertEqual(called, ["credentials"])

    def test_a_drive_it_cannot_resolve_is_refused_not_stored(self):
        """The backstop for a mapping made without /persistent, and for a typo.

        From an elevated session both report DRIVE_NO_ROOT_DIR and both mean
        the same thing to the operator: the nightly encode runs without their
        sign-in session and will not find it. Storing it would reproduce the
        exact failure this whole question exists to prevent.
        """
        transfer, out = self.drive("y\nQ:\\videos\n")
        self.assertFalse(transfer["enabled"])
        self.assertIn("unattended", out)
        self.assertIn("net use", out)

    def test_a_real_local_drive_is_not_refused(self):
        transfer, _out = self.drive("y\nD:\\timelapse\\out\n")
        self.assertTrue(transfer["enabled"])
        self.assertEqual(transfer["destination"], "D:\\timelapse\\out")

    def test_a_unc_path_is_never_asked_about_as_a_drive(self):
        # It has no drive letter, so drive_is_local() answers None and the
        # refusal must not fire on it.
        transfer, _out = self.drive("y\n\\\\tower\\cctv\\TL\n")
        self.assertTrue(transfer["enabled"])

    def test_an_rsync_spec_is_refused_with_its_reason(self):
        """A config written on Linux and carried over. Treated as a relative

        path it would silently create a folder called `user@nas:` beside the
        videos, which is a fault nobody would ever look for.
        """
        transfer, out = self.drive("y\nuser@nas:/vol/tl/\n")
        self.assertFalse(transfer["enabled"])
        self.assertIn("Linux only", out)

    def test_an_empty_answer_disables_rather_than_storing_nothing(self):
        transfer, out = self.drive("y\n\n")
        self.assertFalse(transfer["enabled"])
        self.assertIn("left disabled", out)

    def test_it_never_offers_to_mount_anything(self):
        _transfer, out = self.drive("y\nD:\\out\n")
        for absent in ("SMB/CIFS", "set it up for me", "SSH key", "rsync"):
            self.assertNotIn(absent, out, absent)

    def test_there_is_no_mountpoint_to_require(self):
        # The Linux flag guards against a dropped mount turning a share back
        # into an empty local directory. A UNC path that is unreachable simply
        # fails, so there is nothing to guard.
        transfer, _ = self.drive("y\n\\\\tower\\cctv\\TL\n")
        self.assertFalse(transfer["require_mountpoint"])


class TestWindowsFfmpeg(unittest.TestCase):
    """The wizard asks; the installer does not provide (item 11c.6a)."""

    def drive(self, keystrokes, found="", resolve=None,
              encoders=("libx264", [], "")):
        cfg = setup.default_config()
        buf = io.StringIO()
        with _support.wizard_tty(keystrokes):
            with contextlib.redirect_stdout(buf), \
                 mock.patch.object(setup, "IS_WINDOWS", True), \
                 mock.patch.object(setup, "find_tool", return_value=found), \
                 mock.patch.object(setup, "resolve_tool",
                                   side_effect=resolve or (lambda a, n: a)), \
                 mock.patch.object(setup, "sibling_tool", return_value=""), \
                 mock.patch.object(setup, "detect_encoders",
                                   return_value=encoders):
                setup.choose_tools(cfg)
        return cfg["paths"], buf.getvalue()

    def test_it_offers_no_default_when_nothing_is_installed(self):
        """Not /usr/bin/ffmpeg and not a guess. A made-up default would be

        offered as though it existed, accepted, and found out about at the
        first encode: on Linux the installer has already put one there, and
        here nothing has.
        """
        _paths, out = self.drive("C:\\ff\\ffmpeg.exe\nC:\\ff\\ffprobe.exe\n")
        self.assertNotIn("/usr/bin/ffmpeg", out)

    def test_it_says_where_to_get_one_when_there_is_none(self):
        _paths, out = self.drive("C:\\ff\\ffmpeg.exe\nC:\\ff\\ffprobe.exe\n")
        self.assertIn("ffmpeg.org", out)
        self.assertIn("NVENC", out)

    def test_it_says_a_folder_is_acceptable(self):
        _paths, out = self.drive("C:\\ff\\bin\nC:\\ff\\bin\n")
        self.assertIn("folder", out.lower())

    def test_an_existing_ffmpeg_becomes_the_default(self):
        paths, _out = self.drive("\n\n", found="C:\\ffmpeg\\bin\\ffmpeg.exe")
        self.assertEqual(paths["ffmpeg"], "C:\\ffmpeg\\bin\\ffmpeg.exe")

    def test_no_usable_encoder_names_the_consequence(self):
        _paths, out = self.drive("C:\\ff\\ffmpeg.exe\nC:\\ff\\ffprobe.exe\n",
                                 encoders=(None, [], ""))
        self.assertIn("no product", out)
        self.assertIn("ffmpeg.org", out)

    def test_an_ffmpeg_that_will_not_run_says_so_separately(self):
        """"Could not run it" and "it ran and nothing worked" want different

        things doing about them, and until 0.2.0 both came back as (None, [])
        with the difference printed from inside the probe, where a GUI could
        not see it. Fifth instance of a falsy return meaning several things.
        """
        _paths, out = self.drive(
            "C:\\ff\\ffmpeg.exe\nC:\\ff\\ffprobe.exe\n",
            encoders=(None, [], "could not run C:\\ff\\ffmpeg.exe - is the "
                                "path right?"))
        self.assertIn("is the path right", out)


class TestEncoderDetection(unittest.TestCase):
    """detect_encoders() decides and does not print (11c.6b's rule).

    Anything a GUI has to reuse must return its findings rather than write
    them to a terminal it does not own.
    """

    def test_it_prints_nothing_on_any_path(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), \
             mock.patch.object(enc, "list_encoders", return_value=None):
            chosen, failures, problem = setup.detect_encoders("nosuchffmpeg")
        self.assertEqual(buf.getvalue(), "")
        self.assertIsNone(chosen)
        self.assertEqual(failures, [])
        self.assertIn("could not run", problem)

    def test_a_working_encoder_reports_no_problem(self):
        with mock.patch.object(enc, "list_encoders", return_value=["libx264"]), \
             mock.patch.object(enc, "probe_encoder_detail",
                               return_value=(True, "")):
            chosen, failures, problem = setup.detect_encoders("ffmpeg")
        self.assertEqual(chosen, "av1_nvenc")
        self.assertEqual((failures, problem), ([], ""))

    def test_ran_but_nothing_worked_is_not_the_same_as_could_not_run(self):
        with mock.patch.object(enc, "list_encoders", return_value=[]), \
             mock.patch.object(enc, "probe_encoder_detail",
                               return_value=(False, "Unknown encoder")):
            chosen, failures, problem = setup.detect_encoders("ffmpeg")
        self.assertIsNone(chosen)
        self.assertEqual(len(failures), 3)
        self.assertEqual(problem, "", "ffmpeg ran; that is not a problem "
                                      "with reaching it")


class TestFedoraFfmpeg(unittest.TestCase):
    """Fedora's default ffmpeg cannot encode without a GPU, and says where to go.

    Measured on a Fedora Server VM 2026-09-14: `dnf install ffmpeg` gave
    ffmpeg-free, libx264 was an unknown encoder, and the only advice named
    jellyfin-ffmpeg and BtbN.
    """

    X264_MISSING = [("av1_nvenc", "Cannot load libcuda.so.1", "hint"),
                    ("libx264", "[vost#0:0 @ 0x1] Unknown encoder 'libx264'",
                     "hint")]

    def test_dnf_gets_rpm_fusion(self):
        lines = setup.encoder_remedy(
            self.X264_MISSING, which=lambda name: name == "dnf" or None)
        text = "\n".join(lines)
        self.assertIn("rpmfusion-free-release", text)
        self.assertIn("dnf swap ffmpeg-free ffmpeg --allowerasing", text)

    def test_without_dnf_or_yum_there_is_nothing_to_add(self):
        self.assertEqual(setup.encoder_remedy(self.X264_MISSING,
                                              which=lambda name: None), [])

    def test_an_ffmpeg_with_libx264_needs_no_remedy(self):
        failures = [("av1_nvenc", "Cannot load libcuda.so.1", "hint")]
        self.assertEqual(setup.encoder_remedy(
            failures, which=lambda name: "/usr/bin/dnf"), [])


class TestLinuxFfmpegThatCannotEncode(unittest.TestCase):
    """The ffmpeg section as the Fedora VM saw it, end to end."""

    FAILURES = [
        ("av1_nvenc", "[av1_nvenc @ 0x5568004033c0] Cannot load libcuda.so.1 "
                      "[vost#0:0/av1_nvenc @ 0x556800402dc0] [enc:av1_nvenc @ "
                      "0x5568003fe700] Error while opening encoder - maybe "
                      "incorrect parameters such as bit_rate, rate, width or "
                      "height.", "no NVIDIA driver is loaded on this machine"),
        ("libx264", "[vost#0:0 @ 0x5627764b7dc0] Unknown encoder 'libx264' "
                    "[vost#0:0 @ 0x5627764b7dc0] Error selecting an encoder",
         "this ffmpeg build does not include libx264"),
    ]

    def drive(self, which):
        cfg = setup.default_config()
        buf = io.StringIO()
        with _support.wizard_tty("\n"), \
                contextlib.redirect_stdout(buf), \
                mock.patch.object(setup, "IS_WINDOWS", False), \
                mock.patch.object(setup, "find_tool",
                                  return_value="/usr/sbin/ffmpeg"), \
                mock.patch.object(setup, "resolve_tool",
                                  side_effect=lambda a, n: a), \
                mock.patch.object(setup, "sibling_tool",
                                  return_value="/usr/sbin/ffprobe"), \
                mock.patch.object(setup, "detect_encoders",
                                  return_value=(None, self.FAILURES, "")), \
                mock.patch.object(setup.shutil, "which", which):
            setup.choose_tools(cfg)
        return buf.getvalue()

    def test_ffmpeg_is_quoted_whole_and_without_addresses(self):
        out = self.drive(lambda name: None)
        self.assertIn("ffmpeg said: Cannot load libcuda.so.1\n", out)
        self.assertIn("ffmpeg said: Unknown encoder 'libx264'\n", out)
        self.assertNotIn("0x5568004033c0", out)
        self.assertNotIn("opening encod\n", out)

    def test_the_consequence_is_said_on_linux_too(self):
        out = self.drive(lambda name: None)
        self.assertIn("no product", out)
        self.assertIn("timelapse test --encoders", out)
        self.assertNotIn("ffmpeg.org", out)

    def test_fedora_is_pointed_at_rpm_fusion(self):
        out = self.drive(lambda name: "/usr/bin/dnf" if name == "dnf" else None)
        self.assertIn("rpmfusion-free-release", out)

    def test_the_libx264_hint_does_not_point_the_other_way(self):
        # Fedora retest, 2026-09-14: "try jellyfin-ffmpeg or a BtbN static
        # build" sat directly under the RPM Fusion instructions.
        out = self.drive(lambda name: "/usr/bin/dnf" if name == "dnf" else None)
        self.assertIn("RPM Fusion's does, as above", out)
        self.assertNotIn("jellyfin", out)

    def test_without_a_remedy_the_libx264_hint_is_unchanged(self):
        out = self.drive(lambda name: None)
        self.assertIn("this ffmpeg build does not include libx264", out)
        self.assertNotIn("as above", out)


class TestTurningTheGuardOff(unittest.TestCase):
    """0 stays a valid answer, and is no longer a silent one where it is risky."""

    GB = 1024 ** 3

    def test_the_os_disk_is_named(self):
        warning, advice = setup.guard_off_warning("/", True, "ok", 10)
        self.assertIn("OS disk", warning)
        self.assertIn("10 GB", advice)

    def test_a_tight_estimate_is_named(self):
        warning, _advice = setup.guard_off_warning("/srv", False, "tight", 10)
        self.assertIn("tight", warning)

    def test_a_roomy_data_disk_is_left_alone(self):
        self.assertIsNone(setup.guard_off_warning("/srv", False, "ok", 10))

    def drive(self, keys, verdict="tight", os_disk=True):
        cfg = setup.default_config()
        disk = {"mount": "/", "free": 25 * self.GB, "total": 30 * self.GB,
                "fstype": "ext4", "rotational": True}
        buf = io.StringIO()
        with _support.wizard_tty(keys), \
                contextlib.redirect_stdout(buf), \
                mock.patch.object(setup, "choose_framerate"), \
                mock.patch.object(setup, "estimate_budget",
                                  return_value=verdict), \
                mock.patch.object(setup, "is_os_disk", return_value=os_disk):
            setup.choose_capture(cfg, disk)
        return cfg, buf.getvalue()

    def test_the_arch_vm_answer_is_kept_and_warned_about(self):
        # Interval 5, one camera, then 0: exactly what the transcript typed.
        cfg, out = self.drive("5\n1\n0\n")
        self.assertEqual(cfg["capture"]["min_free_gb"], 0)
        self.assertIn("OS disk", out)

    def test_a_threshold_is_not_warned_about(self):
        cfg, out = self.drive("5\n1\n10\n")
        self.assertEqual(cfg["capture"]["min_free_gb"], 10)
        self.assertNotIn("pausing off", out)

    def test_the_estimate_returns_what_it_printed(self):
        disk = {"mount": "/", "free": 25 * self.GB}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.estimate_budget({}, disk, 1, 17280),
                             "tight")
            self.assertEqual(setup.estimate_budget({}, None, 1, 17280), None)


class TestInstallerFlags(unittest.TestCase):
    """--check-encoder and a --sync-units that is quiet when it did its job."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({"paths": {"ffmpeg": "ffmpeg"}}),
                               encoding="utf-8")

    def main(self, *flags):
        out = io.StringIO()
        argv = ["timelapse_setup.py"] + list(flags) + ["--output",
                                                       str(self.config)]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(setup, "init_tty", mock.Mock(
                    side_effect=AssertionError("asked for a terminal"))), \
                contextlib.redirect_stdout(out):
            rc = setup.main()
        return rc, out.getvalue()

    def test_an_encoder_that_works_exits_zero(self):
        with mock.patch.object(setup, "detect_encoders",
                               return_value=("libx264", [], "")):
            rc, out = self.main("--check-encoder")
        self.assertEqual((rc, out.strip()), (0, "libx264"))

    def test_no_encoder_exits_one_and_says_where_to_go(self):
        failures = TestFedoraFfmpeg.X264_MISSING
        with mock.patch.object(setup, "detect_encoders",
                               return_value=(None, failures, "")), \
                mock.patch.object(setup.shutil, "which",
                                  lambda name: "/usr/bin/dnf"
                                  if name == "dnf" else None):
            rc, out = self.main("--check-encoder")
        self.assertEqual(rc, 1)
        self.assertIn("no encoder works", out)
        self.assertIn("rpmfusion", out)

    def test_an_ffmpeg_that_will_not_run_exits_one(self):
        with mock.patch.object(setup, "detect_encoders",
                               return_value=(None, [], "could not run x")):
            rc, out = self.main("--check-encoder")
        self.assertEqual(rc, 1)
        self.assertIn("could not run", out)

    def test_units_rewritten_says_nothing(self):
        # Both VMs printed `units changed: "/var/lib/timelapse/frames" ...`.
        for status in ("changed", "current"):
            with mock.patch.object(setup, "sync_units",
                                   return_value=(status, '"/x"')):
                rc, out = self.main("--sync-units", str(self.tmp))
            self.assertEqual((rc, out), (0, ""), status)

    def test_a_failure_is_still_reported(self):
        with mock.patch.object(setup, "sync_units",
                               return_value=("failed", "unit: read-only")):
            rc, out = self.main("--sync-units", str(self.tmp))
        self.assertEqual(rc, 1)
        self.assertIn("units failed: unit: read-only", out)


class TestFfprobeFollowsFfmpeg(unittest.TestCase):
    """One answer, two binaries."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def test_the_neighbour_is_preferred_over_path(self):
        """A machine can easily have two ffmpeg builds, and taking one binary

        from each is a mismatch that shows up as an error about a stream, much
        later, with nothing pointing back here.
        """
        (self.tmp / "ffmpeg").write_text("x", encoding="utf-8")
        (self.tmp / "ffprobe").write_text("x", encoding="utf-8")
        # plat, not setup. `from timelapse_platform import IS_WINDOWS` makes
        # two bindings to one value, and resolve_tool resolves names in the
        # module that defines it: patching the wizard's copy would leave the
        # platform module still answering "Windows" and this looking for
        # ffprobe.exe. Same trap the update checker's four tests fell into.
        with mock.patch.object(plat, "IS_WINDOWS", False):
            found = setup.sibling_tool(str(self.tmp / "ffmpeg"), "ffprobe")
        self.assertEqual(Path(found), self.tmp / "ffprobe")

    def test_a_missing_neighbour_is_reported_as_absent(self):
        (self.tmp / "ffmpeg").write_text("x", encoding="utf-8")
        with mock.patch.object(plat, "IS_WINDOWS", False):
            self.assertEqual(setup.sibling_tool(str(self.tmp / "ffmpeg"),
                                                "ffprobe"), "")

    def test_an_empty_answer_has_no_neighbour(self):
        self.assertEqual(setup.sibling_tool("", "ffprobe"), "")

    def test_a_bare_name_with_no_folder_has_no_neighbour(self):
        self.assertEqual(setup.sibling_tool("ffmpeg", "ffprobe"), "")


class TestServiceDefinitions(unittest.TestCase):
    """What gets registered on Windows, held against what the units say.

    This is the table install.ps1 drives, and it is pure on purpose: the argv,
    the schedule and the mapping onto service/*.service and service/*.timer are
    exactly what goes wrong here, and none of it needs a service manager to
    check. So both CI legs assert the same thing.
    """

    SCRIPTS = r"C:\Program Files\timelapse"
    CONFIG = r"C:\ProgramData\timelapse\config.json"

    def defs(self):
        return dict((unit, (desc, argv, extras)) for unit, desc, argv, extras
                    in setup.service_definitions(self.SCRIPTS, self.CONFIG,
                                                 python=r"C:\py\python.exe"))

    def test_it_registers_the_capture_service_and_the_two_batch_jobs(self):
        self.assertEqual(set(self.defs()),
                         {plat.CAPTURE_UNIT, plat.ENCODE_UNIT,
                          plat.WATCH_UNIT})

    def test_the_web_ui_is_not_among_them(self):
        """Deliberate, per item 11f: the first release is capture and encode.

        The web UI carries the status source, the log source and an account
        question of its own, and registering a service for a component that is
        not ported yet would be a service that fails to start on every boot.
        """
        self.assertNotIn(plat.WEB_UNIT, self.defs())

    def test_capture_is_told_to_talk_to_the_service_manager(self):
        """Without --service it is the ordinary foreground daemon, which is

        what error 1053 looks like: the SCM waits about thirty seconds for a
        dispatcher that never connects and kills it.
        """
        argv = self.defs()[plat.CAPTURE_UNIT][1]
        self.assertIn("--service", argv)
        self.assertTrue(argv[1].endswith("timelapse_capture.py"))
        self.assertEqual(argv[0], r"C:\py\python.exe")

    def test_the_config_path_is_always_last(self):
        # Every script here takes the config as a positional argument, so a
        # flag appended after it would be read as a second config.
        for _unit, (_desc, argv, _extras) in self.defs().items():
            self.assertEqual(argv[-1], self.CONFIG)

    def test_the_watch_is_the_encoder_wearing_a_flag(self):
        argv = self.defs()[plat.WATCH_UNIT][1]
        self.assertTrue(argv[1].endswith("timelapse_encode.py"))
        self.assertIn("--watch", argv)
        self.assertNotIn("--watch", self.defs()[plat.ENCODE_UNIT][1])

    def test_the_nightly_job_runs_at_five_past_midnight(self):
        # OnCalendar=*-*-* 00:05:00, so the previous day's directory is
        # complete before anything reads it.
        extras = self.defs()[plat.ENCODE_UNIT][2]
        self.assertIn("T00:05:00", extras["triggers"])

    def test_the_nightly_job_catches_up_and_the_watch_does_not(self):
        """Persistent=true against a timer that deliberately is not.

        A missed encode strands a whole day of frames; a missed credential
        check is answered by the next one five minutes later.
        """
        self.assertTrue(self.defs()[plat.ENCODE_UNIT][2]["catch_up"])
        self.assertFalse(self.defs()[plat.WATCH_UNIT][2]["catch_up"])

    def test_the_nightly_job_has_no_time_limit_and_the_watch_has_one(self):
        # TimeoutStartSec=infinity against TimeoutStartSec=60.
        self.assertEqual(self.defs()[plat.ENCODE_UNIT][2]["time_limit"],
                         "PT0S")
        self.assertEqual(self.defs()[plat.WATCH_UNIT][2]["time_limit"], "PT1M")

    def test_the_watch_repeats_every_five_minutes(self):
        self.assertIn("<Interval>PT5M</Interval>",
                      self.defs()[plat.WATCH_UNIT][2]["triggers"])

    def test_the_scripts_directory_defaults_to_where_this_one_lives(self):
        """An installer that guesses the wrong directory registers a service

        whose binPath names a file that is not there, and the SCM reports that
        as 1053 too, which is the same symptom as three other faults.
        """
        pairs = setup.service_definitions(
            Path(setup.__file__).resolve().parent, self.CONFIG)
        expected = Path(setup.__file__).resolve().parent / "timelapse_capture.py"
        self.assertEqual(Path(pairs[0][2][1]), expected)

    def test_every_definition_makes_a_document_task_scheduler_will_read(self):
        for unit, (desc, argv, extras) in self.defs().items():
            if not plat.is_scheduled(unit):
                continue
            xml = plat.task_xml(desc, argv, **extras)
            ET.fromstring(xml.split("?>", 1)[1])


class TestUnitRegistrationCommands(unittest.TestCase):
    """install_units / remove_units: failure isolation and privilege."""

    def run_units(self, install=(True, ""), task=(True, "")):
        out = io.StringIO()
        with mock.patch.object(setup, "install_service",
                               return_value=install) as svc, \
             mock.patch.object(setup, "install_task",
                               return_value=task) as tsk, \
             contextlib.redirect_stdout(out):
            ok = setup.install_units("scripts", "cfg.json")
        return ok, out.getvalue(), svc, tsk

    def test_it_registers_one_service_and_two_tasks(self):
        _ok, _text, svc, tsk = self.run_units()
        self.assertEqual(svc.call_count, 1)
        self.assertEqual(tsk.call_count, 2)

    def test_one_failure_does_not_stop_the_others(self):
        """The encoder's failure isolation, met in the installer. A watch task

        that will not register must not cost the operator their capture
        service, which is the component that cannot be caught up later.
        """
        ok, text, svc, _tsk = self.run_units(task=(False, "denied"))
        self.assertFalse(ok)
        self.assertEqual(svc.call_count, 1)
        # By whatever this platform calls it: the report is read by whoever ran
        # the installer, so it has to use the name their machine uses.
        self.assertIn(plat.native_name(plat.CAPTURE_UNIT), text)

    def test_registering_does_not_restart_anything(self):
        """The restart is a separate step run AFTER the wizard, and this pins

        the ordering rather than trusting install.ps1 to keep it. Registering
        replaces the files on disk and leaves the running process alone; the
        wizard then rewrites the config underneath it. Restarting during
        registration picks up the new build with the *old* settings, which is
        the worst of the three orderings because nothing about it looks wrong.
        """
        with mock.patch.object(setup, "install_service",
                               return_value=(True, "")), \
             mock.patch.object(setup, "install_task", return_value=(True, "")), \
             mock.patch.object(setup, "service_is_active", return_value=True), \
             mock.patch.object(setup, "restart_service") as restart, \
             contextlib.redirect_stdout(io.StringIO()):
            setup.install_units("scripts", "cfg.json")
        restart.assert_not_called()

    def test_a_running_service_is_restarted_onto_the_new_build(self):
        out = io.StringIO()
        with mock.patch.object(setup, "service_is_active", return_value=True), \
             mock.patch.object(setup, "restart_service",
                               return_value=(True, "")) as restart, \
             contextlib.redirect_stdout(out):
            self.assertTrue(setup.restart_units())
        restart.assert_called_once_with(plat.CAPTURE_UNIT)
        self.assertIn("onto the new build", out.getvalue())

    def test_a_service_that_was_not_running_is_left_stopped(self):
        # A first install, or one where the operator had stopped it. Starting
        # it here would be deciding something they did not ask for.
        with mock.patch.object(setup, "service_is_active", return_value=False), \
             mock.patch.object(setup, "restart_service") as restart, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(setup.restart_units())
        restart.assert_not_called()

    def test_a_restart_that_fails_says_what_is_still_running(self):
        """Not just that it failed: an operator who does nothing is left with

        the previous build serving while every version number says otherwise,
        so the message has to say so and give the command.
        """
        out = io.StringIO()
        with mock.patch.object(setup, "service_is_active", return_value=True), \
             mock.patch.object(setup, "restart_service",
                               return_value=(False, "access denied")), \
             contextlib.redirect_stdout(out):
            self.assertFalse(setup.restart_units())
        self.assertIn("access denied", out.getvalue())
        self.assertIn("previous build", out.getvalue())

    def test_a_partial_success_is_reported_without_being_called_a_failure(self):
        ok, text, _svc, _tsk = self.run_units(
            install=(True, "registered, but could not set: description"))
        self.assertTrue(ok)
        self.assertIn("could not set", text)

    def test_removal_stops_a_service_before_deleting_it(self):
        """DeleteService on a running service only marks it for deletion, and

        it then lingers in that state until the process exits, which blocks the
        next install with an error naming neither cause.
        """
        order = []
        with mock.patch.object(setup, "stop_service",
                               side_effect=lambda u: order.append("stop")), \
             mock.patch.object(setup, "remove_service",
                               side_effect=lambda u: (order.append("remove"),
                                                      (True, ""))[1]), \
             mock.patch.object(setup, "remove_task",
                               return_value=(True, "")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(setup.remove_units())
        self.assertEqual(order, ["stop", "remove"])

    def test_registering_without_privilege_refuses_and_says_how(self):
        out = io.StringIO()
        with mock.patch.object(setup, "is_elevated", return_value=False), \
             mock.patch.object(setup, "install_units") as install, \
             mock.patch.object(setup.sys, "argv",
                               ["timelapse_setup.py", "--install-units"]), \
             contextlib.redirect_stdout(out):
            self.assertEqual(setup.main(), 1)
        install.assert_not_called()
        self.assertTrue(out.getvalue().strip())

    def test_the_status_command_never_guesses(self):
        """A component the machine could not be asked about says "unknown".

        Reporting it as stopped would invent a fault on a healthy system, which
        is the same error the three-way status split exists to avoid.
        """
        out = io.StringIO()
        with mock.patch.object(setup, "service_state", return_value=None), \
             mock.patch.object(setup, "task_info", return_value=None), \
             mock.patch.object(setup, "task_exists", return_value=None), \
             contextlib.redirect_stdout(out):
            self.assertEqual(setup.print_unit_status(), 0)
        self.assertEqual(out.getvalue().count("unknown"), 3)

    def test_the_status_command_prints_one_line_per_component(self):
        out = io.StringIO()
        with mock.patch.object(setup, "service_state",
                               return_value=plat.SERVICE_RUNNING), \
             mock.patch.object(setup, "task_info",
                               return_value={"State": "Ready",
                                             "LastResult": 0}), \
             contextlib.redirect_stdout(out):
            setup.print_unit_status()
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("running", lines[0])
        self.assertIn("Ready", lines[1])

    def test_a_task_that_has_never_run_does_not_look_like_an_error(self):
        """Straight from the log of the first passing Windows install: two

        freshly registered tasks reported "last result 267011", which is the
        healthy "has not run yet" and reads as a five-figure error code.
        """
        out = io.StringIO()
        with mock.patch.object(setup, "service_state",
                               return_value=plat.SERVICE_STOPPED), \
             mock.patch.object(setup, "task_info",
                               return_value={"State": "Ready",
                                             "LastResult": 267011}), \
             contextlib.redirect_stdout(out):
            setup.print_unit_status()
        text = out.getvalue()
        self.assertIn("has not run yet", text)
        self.assertNotIn("267011", text)

    def test_a_task_that_really_failed_still_says_so(self):
        out = io.StringIO()
        with mock.patch.object(setup, "service_state",
                               return_value=plat.SERVICE_STOPPED), \
             mock.patch.object(setup, "task_info",
                               return_value={"State": "Ready",
                                             "LastResult": 1}), \
             contextlib.redirect_stdout(out):
            setup.print_unit_status()
        self.assertIn("failed", out.getvalue())

    def status_with_unreadable_tasks(self, elevated):
        out = io.StringIO()
        with mock.patch.object(setup, "service_state",
                               return_value=plat.SERVICE_RUNNING), \
             mock.patch.object(setup, "task_info", return_value=None), \
             mock.patch.object(setup, "task_exists", return_value=None), \
             mock.patch.object(setup, "is_elevated", return_value=elevated), \
             contextlib.redirect_stdout(out):
            setup.print_unit_status()
        return out.getvalue()

    def test_an_unelevated_prompt_is_told_what_would_answer(self):
        text = self.status_with_unreadable_tasks(elevated=False)
        self.assertEqual(text.count("needs an elevated prompt"), 2)
        self.assertNotIn("not installed", text)

    def test_an_elevated_unknown_stays_a_plain_unknown(self):
        # Elevation is not the missing piece there, so it is not named.
        text = self.status_with_unreadable_tasks(elevated=True)
        self.assertNotIn("elevated", text)
        self.assertEqual(text.count("unknown"), 2)


class TestDefaultMinFreeGb(unittest.TestCase):
    """A flat 60 is wrong on any disk with less than about 66 GB free.

    Not "tight": DiskGuard pauses below the threshold and resumes at 110% of
    it, so the guard has to fit inside the disk twice over before capture can
    ever run. Reported from a 29 GB VM 2026-08-18.
    """

    GB = 1024 ** 3

    def test_an_unknown_disk_keeps_the_shipped_default(self):
        for unknown in (None, 0):
            self.assertEqual(setup.default_min_free_gb(unknown), 60)

    def test_a_small_disk_gets_the_floor_not_the_shipped_default(self):
        # The VM. 60 would pause capture for ever; 10 leaves 19 GB to work in.
        self.assertEqual(setup.default_min_free_gb(29 * self.GB), 10)

    def test_a_large_disk_is_capped(self):
        self.assertEqual(setup.default_min_free_gb(4000 * self.GB), 60)

    def test_in_between_it_is_a_tenth(self):
        self.assertEqual(setup.default_min_free_gb(200 * self.GB), 20)

    def test_the_result_always_fits_the_disk_it_was_measured_from(self):
        """The property that matters, over the whole plausible range.

        A default this function returns must leave capture able to run, or it
        has reproduced the reported bug at a different size.
        """
        for gb in (12, 20, 29, 50, 100, 250, 500, 1000, 4000):
            guard = setup.default_min_free_gb(gb * self.GB)
            self.assertLess(guard, gb,
                            "%d GB disk got a %d GB guard" % (gb, guard))

    def test_the_console_wizard_has_no_second_copy(self):
        source = (Path(_support.SCRIPTS) / "timelapse_setup.py").read_text(
            encoding="utf-8")
        body = "\n".join(line for line in source.splitlines()
                          if not line.lstrip().startswith("#"))
        self.assertEqual(body.count("min(60,"), 1)


class TestPreflightSeesTheGuard(unittest.TestCase):
    """The pre-flight said "Headroom fine" on the VM as well.

    It projected a day of frames against free space and never compared free
    space to the threshold that decides whether a frame is written at all.
    Three surfaces agreed the machine was healthy; the daemon wrote nothing.
    """

    GB = 1024 ** 3

    def setUp(self):
        import timelapse_test
        self.checker = timelapse_test

    def run_check(self, free_gb, guard_gb):
        Usage = collections.namedtuple("Usage", "total used free")
        # Its own directory, not the shared one. A check that reads the
        # process's temp directory is a check whose subject is whatever else
        # is in there, and this one goes on to stat it.
        own = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, own, True)
        cfg = {"paths": {"frames_root": str(own)},
               "capture": {"min_free_gb": guard_gb, "interval_seconds": 5},
               "cameras": [{"name": "a", "enabled": True}]}
        said = {"bad": [], "warn": [], "ok": [], "info": []}
        import shutil as _shutil
        with mock.patch.object(_shutil, "disk_usage",
                               return_value=Usage(600 * self.GB, 0,
                                                  int(free_gb * self.GB))), \
             mock.patch.object(self.checker, "bad",
                               lambda m: said["bad"].append(m)), \
             mock.patch.object(self.checker, "warn",
                               lambda m: said["warn"].append(m)), \
             mock.patch.object(self.checker, "ok",
                               lambda m: said["ok"].append(m)), \
             mock.patch.object(self.checker, "info",
                               lambda m: said["info"].append(m)):
            self.checker.test_disk(cfg, 0, 1)
        return said

    def test_a_guard_above_the_free_space_is_reported_bad(self):
        said = self.run_check(29, 60)
        self.assertTrue(said["bad"], "the pre-flight said nothing")
        self.assertIn("never resume", " ".join(said["bad"]))

    def test_a_workable_guard_is_not_reported(self):
        said = self.run_check(29, 10)
        self.assertFalse(said["bad"])
        self.assertFalse(said["warn"])

    def test_a_guard_just_under_the_free_space_warns(self):
        """Above the threshold but below the resume point, so capture would
        pause on the first quiet day and not come back."""
        said = self.run_check(63, 60)
        self.assertTrue(said["warn"])

    def test_a_disabled_guard_says_nothing_about_it(self):
        said = self.run_check(29, 0)
        self.assertFalse(said["bad"])
        self.assertFalse(said["warn"])


if __name__ == "__main__":
    unittest.main()


class SpinGuardTTY(FakeTTY):
    """A terminal that refuses to feed a runaway loop for ever.

    These tests are about loops that did not terminate, so without a bound the
    reverted build hangs the suite rather than failing it, and a revert check
    that hangs proves nothing at all. Worse, it leaves the source file
    reverted, because the harness's restore never runs.

    `tty=False` for the second half of that: with isatty() true, ask_secret()
    hands the question to the real getpass, which reads the **console** rather
    than this object, and a suite that blocks on a password prompt is the same
    hang by another road. Not hypothetical; that is how it presented.
    """

    LIMIT = 40

    def __init__(self, text, tty=False):
        super().__init__(text, tty=tty)

    def readline(self):
        if self.reads >= self.LIMIT:
            raise RuntimeError("the prompt loop did not terminate")
        return super().readline()


class TestEndOfInputStopsAsking(unittest.TestCase):
    """EOF returned the default and left the wizard believing it had a tty.

    Several prompts sit inside a loop that re-asks until the answer is usable,
    and their escape hatch is `AUTO or _TTY is None`. EOF set neither, so the
    loop asked a terminal that was not there, got the same unusable default
    back, and asked again. Measured with --stdin exhausted and `web.port`
    already 80: the Port prompt was read more than thirty times in a second
    before the probe's own guard tripped.

    Platform-independent and unprivileged: the terminal is a StringIO and
    nothing binds a socket, because check_bind is patched below the seam.
    """

    def setUp(self):
        self.prev_tty, self.prev_auto = setup._TTY, setup.AUTO
        self.addCleanup(self.restore)
        setup.AUTO = False

    def restore(self):
        setup._TTY, setup.AUTO = self.prev_tty, self.prev_auto

    def drive(self, keystrokes, fn, tty=SpinGuardTTY):
        setup._TTY = tty(keystrokes)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fn()
        return result, setup._TTY.reads, buf.getvalue()

    def test_the_first_eof_switches_to_defaults(self):
        self.drive("", lambda: setup.ask("Anything", "x"))
        self.assertTrue(setup.AUTO, "EOF must stop the wizard asking")

    def test_the_answer_at_eof_is_still_the_default(self):
        answer, _, _ = self.drive("", lambda: setup.ask("Anything", "x"))
        self.assertEqual(answer, "x")

    def test_a_real_answer_does_not_switch_to_defaults(self):
        # The control. Without it, "AUTO gets set" would be satisfied by a
        # wizard that never asks anybody anything.
        answer, _, _ = self.drive("yes\n", lambda: setup.ask("Anything", "x"))
        self.assertEqual(answer, "yes")
        self.assertFalse(setup.AUTO)

    def test_the_port_loop_terminates(self):
        """The loop the finding was measured on.

        A port under 1024 is refused without probing, because the wizard runs
        as root and the service does not, so it is a rejected answer that can
        never become acceptable by being asked for again.
        """
        cfg = setup.default_config()
        cfg.setdefault("web", {})["port"] = 80
        setup._TTY = SpinGuardTTY("y\n")
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(setup, "check_bind",
                                  lambda addr, port: ("ok", "")), \
                mock.patch.object(setup, "lan_address", lambda: "192.0.2.5"), \
                mock.patch.object(setup, "host_addresses",
                                  lambda: ["192.0.2.5"]):
            setup.choose_web(cfg)
        self.assertEqual(cfg["web"]["port"], 80)

    def test_the_camera_name_loop_terminates_too(self):
        """And skips the camera, which is the only honest answer here.

        The port and bind loops have an acceptable value to keep; a name that
        is already taken has none, and re-asking a terminal that is not there
        is handed the same refused name for ever.
        """
        cfg = {"cameras": [], "capture": {"interval_seconds": 5}}
        setup._TTY = SpinGuardTTY("")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cam = setup.add_one_camera(cfg, 1, taken=[{"name": "Camera1"}])
        self.assertIsNone(cam)
        self.assertIn("skipping this camera", out.getvalue())

    def test_a_free_name_is_still_accepted_without_a_terminal(self):
        # The control for the one above: the guard must not refuse every
        # unattended camera, only the one it cannot name.
        cfg = {"cameras": [], "capture": {"interval_seconds": 5}}
        setup._TTY = SpinGuardTTY("")
        with contextlib.redirect_stdout(io.StringIO()):
            cam = setup.add_one_camera(cfg, 1, taken=[{"name": "Gate"}])
        self.assertIsNotNone(cam)
        self.assertEqual(cam["name"], "Camera1")


class TestCtrlCAtAPasswordIsNotAnAnswer(unittest.TestCase):
    """ask() exits 1 on Ctrl-C; ask_secret() returned "".

    So interrupting at "Password" did not leave the wizard, it added the
    camera with an empty password and carried on, and every fetch from then
    on is refused by the camera.
    """

    def setUp(self):
        self.prev_tty, self.prev_auto = setup._TTY, setup.AUTO
        self.addCleanup(self.restore)
        setup.AUTO = False
        # tty=True said out loud: ask_secret() only reaches getpass when it
        # believes there is a terminal, and that is the branch under test. The
        # shared FakeTTY defaults to False so that no test reaches the real
        # console by accident.
        setup._TTY = FakeTTY("", tty=True)

    def restore(self):
        setup._TTY, setup.AUTO = self.prev_tty, self.prev_auto

    def secret(self, raising):
        import getpass
        with mock.patch.object(getpass, "getpass", side_effect=raising), \
                contextlib.redirect_stdout(io.StringIO()):
            return setup.ask_secret("Password")

    def test_ctrl_c_leaves_the_wizard(self):
        with self.assertRaises(SystemExit) as caught:
            self.secret(KeyboardInterrupt)
        self.assertEqual(caught.exception.code, 1)

    def test_end_of_input_gives_nothing_and_stops_asking(self):
        # A different cause wants a different answer: the terminal went away
        # rather than the operator changing their mind, so it matches ask().
        self.assertEqual(self.secret(EOFError), "")
        self.assertTrue(setup.AUTO)

    def test_an_ordinary_password_still_comes_back(self):
        import getpass
        with mock.patch.object(getpass, "getpass", return_value="hunter2"):
            self.assertEqual(setup.ask_secret("Password"), "hunter2")


class TestTheCameraTestReadsTheCameraItWasGiven(unittest.TestCase):
    """A hand-edited config has no "method" key, and the daemon defaults it.

    The wizard did not, so `timelapse cameras -t` on such a config raised
    KeyError rather than testing anything.
    """

    def setUp(self):
        self.prev_tty, self.prev_auto = setup._TTY, setup.AUTO
        self.addCleanup(self.restore)
        setup.AUTO = True

    def restore(self):
        setup._TTY, setup.AUTO = self.prev_tty, self.prev_auto

    def run_test(self, cam, cfg, seconds=0.1):
        detail = {"skipped": False, "reason": "", "notes": [], "status": 200,
                  "bytes": 1024, "seconds": seconds, "size": (1920, 1080)}
        with mock.patch.object(setup, "grab_snapshot",
                               return_value=(b"\xff\xd8" + b"x" * 1024,
                                             detail)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            ok = setup.test_camera(cam, cfg)
        return ok, out.getvalue()

    def test_a_camera_without_a_method_is_tested_as_http(self):
        ok, _ = self.run_test({"name": "Gate", "url": "http://192.0.2.1/s"},
                              {"capture": {"interval_seconds": 5}})
        self.assertTrue(ok)

    def test_a_config_without_a_timeout_still_fetches(self):
        """grab_snapshot() builds its own timeout from capture.timeout_seconds.

        Patched at requests.Session, which is below the seam under test: the
        argument has to reach the real code to prove anything.
        """
        import requests
        seen = {}

        class FakeSession:
            auth = None

            def get(self, url, timeout=None):
                seen["timeout"] = timeout
                raise RuntimeError("far enough")

        with mock.patch.object(requests, "Session", FakeSession):
            data, detail = setup.grab_snapshot(
                {"name": "Gate", "url": "http://192.0.2.1/s"}, {"capture": {}})
        self.assertIsNone(data)
        self.assertEqual(seen["timeout"], 8)

    def test_slowness_is_judged_against_this_cameras_interval(self):
        # A camera on a 60s override was called slow at 3.1s, against an
        # interval it does not run at.
        cam = {"name": "Roof", "url": "http://192.0.2.1/s", "method": "http",
               "interval_seconds": 60}
        _, said = self.run_test(cam, {"capture": {"interval_seconds": 5}},
                                seconds=3.1)
        self.assertNotIn("slow", said)

    def test_a_camera_on_the_global_interval_is_still_warned(self):
        # The control: the warning has to survive for the cameras it is about.
        cam = {"name": "Gate", "url": "http://192.0.2.1/s", "method": "http"}
        _, said = self.run_test(cam, {"capture": {"interval_seconds": 5}},
                                seconds=3.1)
        self.assertIn("slow", said)


class TestTheStrandedWarningCountsWhatIsLeft(unittest.TestCase):
    """With delete_frames_on_success off, every encoded day is still on disk.

    So counting day directories told the operator that disabling a camera
    would strand N days, and sent them to `timelapse encode --date` for days
    that carry a marker and are skipped. The encoder decides with
    find_pending(); this agrees with it rather than restating it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = {"paths": {"frames_root": str(self.tmp)},
                    "cameras": [{"name": "Gate", "enabled": True}]}

    def day(self, name, encoded=False):
        d = self.tmp / "Gate" / name
        d.mkdir(parents=True)
        if encoded:
            (d / ".encoded.json").write_text(
                '{"video": "Gate.%s.mkv"}' % name.replace("-", ""),
                encoding="utf-8")
        return d

    def test_an_encoded_day_is_not_pending(self):
        self.day("2020-01-01", encoded=True)
        self.assertEqual(setup.pending_days(self.cfg, "Gate"), [])

    def test_an_unencoded_day_still_is(self):
        # The control: the warning has to survive for what it is about.
        self.day("2020-01-02")
        self.assertEqual([p.name for p in setup.pending_days(self.cfg, "Gate")],
                         ["2020-01-02"])

    def test_a_marker_that_will_not_parse_counts_as_unencoded(self):
        # day_encoded()'s rule, inherited rather than re-decided: the wasteful
        # answer costs one re-encode and is self-correcting.
        d = self.day("2020-01-03")
        (d / ".encoded.json").write_text("{ this is not json", encoding="utf-8")
        self.assertEqual([p.name for p in setup.pending_days(self.cfg, "Gate")],
                         ["2020-01-03"])

    def test_the_encoder_and_the_wizard_agree(self):
        # Both keyed on the same function, so a day the encoder would skip is
        # a day the wizard does not warn about.
        import timelapse_encode as enc
        self.day("2020-01-04", encoded=True)
        self.day("2020-01-05")
        jobs, done = enc.find_pending(self.tmp, ["Gate"], None, 0)
        self.assertEqual([d.name for _, d in jobs], ["2020-01-05"])
        self.assertEqual(done, 1)
        self.assertEqual([p.name for p in setup.pending_days(self.cfg, "Gate")],
                         ["2020-01-05"])


class TestAWindowsDestinationIsStoredAbsolute(unittest.TestCase):
    """A relative path is resolved at 00:05 against the task's directory.

    Which for a LocalSystem task is System32. The wizard's own write probe
    resolved it against this process's directory instead, so the check agreed
    with a setting that cannot work.

    Declares its platform: the wizard branch is Windows-only, and os.path is
    the right module here because the running machine is the subject.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prev_tty, self.prev_auto = setup._TTY, setup.AUTO
        self.addCleanup(self.restore)

    def restore(self):
        setup._TTY, setup.AUTO = self.prev_tty, self.prev_auto

    def choose(self, typed):
        cfg = {"transfer": {}, "paths": {}}
        setup.AUTO = False
        setup._TTY = FakeTTY(typed, tty=False)
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(setup, "network_path", lambda d: None), \
                mock.patch.object(setup, "drive_is_local", lambda d: True), \
                mock.patch.object(setup, "check_windows_destination",
                                  lambda cfg, dest: None):
            setup.choose_windows_destination(cfg)
        return cfg["transfer"], out.getvalue()

    def test_a_bare_folder_name_is_made_absolute(self):
        transfer, said = self.choose("videos\n")
        self.assertTrue(os.path.isabs(transfer["destination"]))
        self.assertIn("Storing the full path", said)

    def test_a_drive_relative_path_is_made_absolute_too(self):
        """`D:timelapse` means "the current directory on D:", which is not a
        place a service can find.

        No skip: `ntpath.isabs()` calls it relative on either platform, which
        is the decision under test, and what gets stored is absolute by the
        running platform's rules, which is what the caller needs. A skip here
        would have been a test that runs on one leg and reports nothing on the
        other three.
        """
        self.assertFalse(ntpath.isabs("D:timelapse"))
        transfer, _ = self.choose("D:timelapse\n")
        self.assertTrue(os.path.isabs(transfer["destination"]))

    def test_an_absolute_path_is_left_exactly_as_typed(self):
        want = str(self.tmp / "library")
        transfer, said = self.choose(want + "\n")
        self.assertEqual(transfer["destination"], want)
        self.assertNotIn("Storing the full path", said)

    def test_a_unc_path_is_left_alone(self):
        transfer, said = self.choose("\\\\tower\\cctv\\TL\n")
        self.assertEqual(transfer["destination"], "\\\\tower\\cctv\\TL")
        self.assertNotIn("Storing the full path", said)


class TestTheWizardDoesNotGrowSysPath(unittest.TestCase):
    """Six copies of `sys.path.insert(0, ...)` ran on every call.

    The module-level `from timelapse_platform import ...` at the top of the
    file already proves `scripts/` is importable from here, and two functions
    (`redact_url`, `url_host`) imported the same sibling without one, so the
    inserts were never what made it work. Measured before removing them: 66
    new entries on sys.path after test_setup alone.
    """

    def test_the_wizard_inserts_nothing(self):
        # Anchored at the start of a line so the sentence above, and this
        # comment, are not read as a breach of the rule they describe.
        src = Path(setup.__file__).read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^\s*sys\.path\.insert\(", src, re.M), [])

    def test_the_sibling_imports_still_work(self):
        # The control. Deleting the inserts must not have deleted the reach:
        # this is the one that runs in every install.
        from timelapse_encode import day_encoded
        self.assertTrue(callable(day_encoded))

    def test_the_os_probes_come_from_the_platform_module(self):
        # Item 11e: one file carries the platform branch. Identity, not
        # equality, so a re-added local copy fails here.
        import timelapse_platform as plat
        for name in ("host_addresses", "mount_fstype", "install_cifs_utils",
                     "is_elevated"):
            self.assertIs(getattr(setup, name), getattr(plat, name), name)

    def test_there_is_one_way_to_test_a_notification(self):
        # send_test_webhook() was a second path that bypassed notify(), which
        # send_test_notification()'s own docstring calls the wrong way to test
        # a sink. Nothing called it.
        self.assertFalse(hasattr(setup, "send_test_webhook"))
        self.assertTrue(hasattr(setup, "send_test_notification"))


class TestTheExampleAndTheDefaultsAgree(unittest.TestCase):
    """Two documents describing one config, written in two places.

    `config/config.example.json` is the only place an operator ever sees a new
    key, because an upgrade keeps the existing config and refreshes the
    example beside it. `default_config()` is the only place a fresh install
    gets one. Nothing breaks when they disagree, which is exactly why they
    drifted: 0.2.0's Windows share credentials reached `reach_destination()`
    without ever appearing in either.
    """

    # Deliberate, each with its reason. Anything not listed here is drift.
    ONLY_IN_EXAMPLE = {
        # An example of a configured camera is worth more than an empty list,
        # and the wizard clears it: a fresh config must not start with a
        # camera nobody owns.
        ("cameras",),
        # The login is keyed on absence, which is the same rule the per-camera
        # overrides are under: no `auth` means the pages are open, and
        # choose_web_login() *removes* the key rather than emptying it. The
        # example shows the shape it takes when set; a fresh config writing an
        # empty one would be storing "no login" twice over.
        ("web", "auth"),
    }
    ONLY_IN_DEFAULTS = set()

    def setUp(self):
        self.example = json.loads(
            (_support.REPO / "config" / "config.example.json").read_text(
                encoding="utf-8"))
        prev_tty, prev_auto = setup._TTY, setup.AUTO
        setup.AUTO = True
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.defaults = setup.default_config()
        finally:
            setup._TTY, setup.AUTO = prev_tty, prev_auto

    def keys(self, cfg):
        """Every (section,) and (section, key) in a config, comments aside."""
        out = set()
        for name, block in cfg.items():
            if name.startswith("_"):
                continue
            out.add((name,))
            if isinstance(block, dict):
                out.update((name, k) for k in block if not k.startswith("_"))
        return out

    def test_the_example_carries_nothing_new_installs_lack(self):
        missing = self.keys(self.example) - self.keys(self.defaults) \
            - self.ONLY_IN_EXAMPLE
        self.assertEqual(missing, set(),
                         "in the example and not in default_config()")

    def test_new_installs_carry_nothing_the_example_hides(self):
        extra = self.keys(self.defaults) - self.keys(self.example) \
            - self.ONLY_IN_DEFAULTS
        self.assertEqual(extra, set(),
                         "written by default_config() and undocumented")

    def test_the_windows_share_credentials_are_in_both(self):
        # The pair that went missing, named rather than left to the sets
        # above: they are read by the nightly encode and were invisible.
        for cfg in (self.example, self.defaults):
            self.assertIn("username", cfg["transfer"])
            self.assertIn("password", cfg["transfer"])

    def test_the_comparison_is_looking_at_something(self):
        # Without this, a keys() that returned nothing would make both
        # assertions above pass for ever.
        self.assertGreater(len(self.keys(self.defaults)), 30)


class TestTheShellInstallerAgreesWithItself(unittest.TestCase):
    """Four small claims install.sh made and did not keep (L33 to L37, L40).

    A source scan is what a unit test can do here; the end to end half is a
    real install in WSL, which the batch ends with.
    """

    def setUp(self):
        self.source = (_support.REPO / "install.sh").read_text(
            encoding="utf-8")
        # Prose that describes a constraint is not a breach of it. The comment
        # explaining why `pacman -Sy` is gone contains the words `pacman -Sy`,
        # and the first version of this class failed on exactly that: the
        # fifth time this project has caught a source scan reading its own
        # explanation. Lines that are wholly a comment are dropped for the
        # assertions that say something is absent.
        self.code = "\n".join(
            line for line in self.source.splitlines()
            if not line.lstrip().startswith("#"))

    def test_the_python_floor_is_the_projects_floor(self):
        # 3.8 here and 3.9 in README, install.ps1 and prepare.ps1. RHEL 9 and
        # Debian 11 ship 3.9, and this project states 3.9 as a property it
        # holds rather than as momentum.
        self.assertIn("sys.version_info >= (3, 9)", self.source)
        self.assertNotIn("(3, 8)", self.code)

    def test_every_front_door_names_the_same_floor(self):
        for name in ("install.ps1", "README.md"):
            text = (_support.REPO / name).read_text(encoding="utf-8")
            self.assertIn("3.9", text, name)

    def test_the_terminal_is_probed_by_opening_it(self):
        # `test -r /dev/tty` is true under setsid, with no controlling
        # terminal, and then every read fails: two bash errors per question
        # and every default taken.
        self.assertIn("! { : < /dev/tty; } 2>/dev/null", self.source)
        self.assertNotIn("[ ! -r /dev/tty ]", self.code)

    def pkg_lists(self):
        return [line.split('pyreq="', 1)[1].split('"', 1)[0].split()
                for line in self.code.splitlines() if 'pyreq="' in line]

    def test_curl_is_not_in_any_base_transaction(self):
        # Fedora's curl-minimal conflicts with the curl package, and a package
        # manager installs a list as one transaction, so naming it took
        # python3-requests and rsync down with it.
        lists = self.pkg_lists()
        self.assertEqual(len(lists), 5, lists)
        for names in lists:
            self.assertNotIn("curl", names)
            self.assertIn("rsync", names)

    def test_curl_is_installed_only_when_the_command_is_missing(self):
        self.assertIn("if ! command -v curl >/dev/null 2>&1; then\n"
                      "        pkg_install curl", self.code)

    def test_a_refused_install_shows_what_the_package_manager_said(self):
        body = self.source.split("\npkg_install() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotIn(">/dev/null 2>&1 ;;", body)
        self.assertIn('tail -n 8 "$log"', body)

    def test_pacman_does_not_refresh_without_upgrading(self):
        # -Sy then -S is the partial-upgrade pattern Arch does not support.
        self.assertNotIn("pacman -Sy", self.code)
        self.assertIn("pacman -S --noconfirm --needed", self.source)

    def test_an_ignored_ref_is_said_out_loud(self):
        self.assertIn("REF_GIVEN=1", self.source)
        self.assertIn("ignored: this checkout is what gets installed",
                      self.source)

    def test_the_status_command_covers_every_managed_unit(self):
        """The help said "all four units" and the command listed four, while
        MANAGED_UNITS has five and the docs describe the watch timer as one
        of the things `timelapse status` shows."""
        block = self.source.split("MANAGED_UNITS=(", 1)[1].split(")", 1)[0]
        managed = block.split()
        self.assertIn("timelapse-watch.timer", managed)
        status = self.source.split("status)    exec systemctl status", 1)[1]
        status = status.split(";;", 1)[0]
        for unit in managed:
            self.assertIn(unit, status, "%s is managed and not shown" % unit)

    def test_the_help_no_longer_counts_them_wrong(self):
        self.assertNotIn("status for all four units", self.code)

    def test_adding_a_camera_names_the_command_that_adds_one(self):
        # `timelapse config` opens the file in an editor; the command that
        # adds a camera is `timelapse cameras -a`.
        self.assertNotIn('Add them with:  timelapse config', self.code)
        self.assertIn("timelapse cameras -a", self.source)

    def test_uninstall_says_what_it_left_behind(self):
        tail = self.source.split("do_uninstall()", 1)[1]
        self.assertIn("Kept $CONFDIR (the example config", tail)
        self.assertIn("Kept /var/lib/timelapse", tail)


class TestThePowerShellInstallerDeclaresItsArguments(unittest.TestCase):
    """Test-PythonVersion had no param block.

    It resolved `$Python` from the caller's scope by dynamic scoping, which
    happened to hold one. Its two neighbours, Test-Requests and
    Install-Requests, both declare it.
    """

    def test_every_function_that_takes_a_python_declares_it(self):
        text = (_support.REPO / "install.ps1").read_text(encoding="utf-8")
        for name in ("Test-PythonVersion", "Test-Requests",
                     "Install-Requests"):
            body = text.split("function %s {" % name, 1)[1]
            body = body.split("\nfunction ", 1)[0]
            self.assertIn("param(", body,
                          "%s reads its argument by dynamic scope" % name)


class TestTheWizardsRtspProbeKeepsTheDiagnosis(unittest.TestCase):
    """At -loglevel error every RTSP failure is "Operation not permitted", so
    the wizard told an operator with correct credentials to check them."""

    BUSY = """[in#0 @ 1] Failed reading RTSP data: End of file
[in#0 @ 2] Error opening input: Operation not permitted
Error opening input file rtsp://war4peace:hunter2@h:554/stream1.
"""

    def probe(self):
        cfg = {"paths": {"ffmpeg": "ffmpeg"}}
        cam = {"name": "Hallway", "url": "rtsp://war4peace:hunter2@h/stream1"}
        result = mock.Mock(returncode=1, stderr=self.BUSY)
        with mock.patch.object(setup.subprocess, "run",
                               return_value=result) as run:
            data, detail = setup.grab_snapshot_rtsp(cam, cfg)
        return run.call_args[0][0], data, detail

    def test_it_asks_ffmpeg_for_warnings_not_only_errors(self):
        argv, _data, _detail = self.probe()
        self.assertIn("warning", argv)
        self.assertNotIn("error", argv)

    def test_the_reason_names_the_cause_rather_than_ffmpegs_word_for_it(self):
        _argv, data, detail = self.probe()
        self.assertIsNone(data)
        self.assertIn("free client slot", detail["reason"])
        self.assertNotIn("Operation not permitted", detail["reason"])

    def test_ffmpegs_own_line_is_kept_as_a_note(self):
        _argv, _data, detail = self.probe()
        self.assertTrue(any("End of file" in n for n in detail["notes"]))

    def test_the_password_reaches_neither(self):
        _argv, _data, detail = self.probe()
        self.assertNotIn("hunter2", detail["reason"])
        self.assertNotIn("hunter2", " ".join(detail["notes"]))

    def test_no_ffmpeg_configured_is_a_reason_not_a_traceback(self):
        """paths.ffmpeg is "" on a Windows install where none was found and
        Enter was pressed; handed to subprocess that raised OSError (M12)."""
        with mock.patch.object(setup.subprocess, "run",
                               side_effect=AssertionError("ran nothing")):
            data, detail = setup.grab_snapshot_rtsp(
                {"url": "rtsp://x"}, {"paths": {"ffmpeg": ""}})
        self.assertIsNone(data)
        self.assertIn("paths.ffmpeg", detail["reason"])

    def test_an_ffmpeg_that_will_not_start_is_a_reason(self):
        with mock.patch.object(setup.subprocess, "run",
                               side_effect=FileNotFoundError(2, "No such file")):
            data, detail = setup.grab_snapshot_rtsp(
                {"url": "rtsp://war4peace:hunter2@h/stream1"},
                {"paths": {"ffmpeg": "/nowhere/ffmpeg"}})
        self.assertIsNone(data)
        self.assertIn("Could not run ffmpeg", detail["reason"])
        self.assertIn("/nowhere/ffmpeg", detail["reason"])
        self.assertNotIn("hunter2", detail["reason"])


class TestAnUnwritableConfigIsSaidFirst(unittest.TestCase):
    """The full wizard asked every question, typed passwords included, and
    then tracebacked out of write_config() (M14)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.blocker = self.tmp / "a-file"
        self.blocker.write_text("not a directory", encoding="utf-8")

    def run_main(self, output, extra=(), tty=None):
        argv = ["timelapse_setup.py", "--output", str(output)] + list(extra)
        tty = tty or mock.Mock(side_effect=AssertionError("asked a question"))
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(setup, "init_tty", tty), \
                mock.patch.object(setup, "is_elevated", return_value=False), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = setup.main()
        return rc, out.getvalue()

    def test_the_full_wizard_refuses_before_its_first_question(self):
        rc, out = self.run_main(self.blocker / "config.json")
        self.assertEqual(rc, 1)
        self.assertIn("Cannot write", out)
        self.assertIn("not a directory", out)

    def test_every_section_that_writes_refuses_first_too(self):
        for flag in ("--notify-only", "--transfer-only", "--web-only",
                     "--cameras-only", "--password-only", "--restore-only"):
            rc, out = self.run_main(self.blocker / "config.json", [flag])
            self.assertEqual(rc, 1, flag)
            self.assertIn("Cannot write", out, flag)

    def test_a_camera_test_and_a_listing_are_not_writes(self):
        # The control: these write nothing and must not be refused for it.
        for extra in (["--cameras-only", "-t:Gate"], ["--cameras-only", "-l"]):
            _rc, out = self.run_main(self.blocker / "config.json", extra,
                                     tty=mock.Mock())
            self.assertNotIn("Cannot write", out, extra)

    def test_a_place_that_can_be_written_has_no_problem(self):
        self.assertIsNone(setup.output_problem(self.tmp / "new" / "config.json"))

    def test_a_failed_final_write_reports_the_answers_masked(self):
        cfg = setup.default_config()
        cfg["cameras"] = [{"name": "Gate", "method": "http", "auth": "digest",
                           "username": "admin", "password": "hunter2",
                           "url": "http://192.0.2.1/snap"}]
        argv = ["timelapse_setup.py", "--output",
                str(self.tmp / "config.json")]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(setup, "init_tty", lambda **kw: None), \
                mock.patch.object(setup, "default_config", lambda t=None: cfg), \
                mock.patch.object(setup, "choose_storage", lambda c: None), \
                mock.patch.object(setup, "choose_tools", lambda c: None), \
                mock.patch.object(setup, "choose_capture", lambda c, d: 1), \
                mock.patch.object(setup, "choose_cameras", lambda c, n: None), \
                mock.patch.object(setup, "choose_transfer", lambda c, o: None), \
                mock.patch.object(setup, "choose_notify", lambda c, p: None), \
                mock.patch.object(setup, "choose_web", lambda c: None), \
                mock.patch.object(setup, "create_directories", lambda c, o: None), \
                mock.patch.object(setup, "write_config",
                                  side_effect=PermissionError(13, "denied")), \
                contextlib.redirect_stdout(out):
            rc = setup.main()
        self.assertEqual(rc, 1)
        self.assertIn("Nothing was saved", out.getvalue())
        self.assertIn("Gate", out.getvalue())
        self.assertNotIn("hunter2", out.getvalue())


class TestTheAdviceNamesTheCommandThatKeepsEverything(unittest.TestCase):
    """`timelapse setup` starts from an empty camera list, so following the
    advice meant retyping every camera password (M15)."""

    DEST = "\\\\tower\\cctv\\TL"

    def check(self, keys, verified=(True, "")):
        import timelapse_encode as enc
        cfg = {"transfer": {"enabled": True, "destination": self.DEST}}
        buf = io.StringIO()
        with _support.wizard_tty(keys), contextlib.redirect_stdout(buf), \
                mock.patch.object(enc, "try_destination",
                                  return_value=(True, "")), \
                mock.patch.object(enc, "verify_share_credentials",
                                  return_value=verified):
            setup.check_windows_destination(cfg, self.DEST)
        return buf.getvalue()

    def test_declining_credentials_names_timelapse_transfer(self):
        out = self.check("n\n")
        self.assertIn("timelapse transfer", out)
        self.assertNotIn("timelapse setup", out)

    def test_credentials_that_fail_name_it_too(self):
        out = self.check("y\nuser\npass\n", verified=(False, "refused"))
        self.assertIn("timelapse transfer", out)
        self.assertNotIn("timelapse setup", out)

    def test_no_cameras_headless_names_the_command_that_adds_one(self):
        cfg = setup.default_config()
        buf = io.StringIO()
        with _support.wizard_tty("", auto=True), contextlib.redirect_stdout(buf):
            setup.choose_cameras(cfg, 1)
        self.assertIn("timelapse cameras -a", buf.getvalue())
        self.assertNotIn("timelapse setup", buf.getvalue())

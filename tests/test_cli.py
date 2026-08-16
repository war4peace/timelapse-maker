"""Unit tests for timelapse_cli.py: the `timelapse` command on Windows.

The interesting one is TestNoDrift. This file and the bash wrapper that
install.sh generates are two front doors onto the same set of commands, written
in two languages, and the failure mode is not that one breaks: it is that
somebody adds a command to one and the other silently keeps working without it.
Nothing about either would fail, and the gap would only be found by a Windows
operator typing a command they read about in the docs.

Everything else here is the dispatcher's own logic, which is deliberately
small: it builds an argv and runs a sibling. A test that had to mock a lot
would be a sign this file had started deciding things.
"""

import contextlib
import io
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _support                                            # noqa: F401

import timelapse_cli as cli

ROOT = Path(__file__).resolve().parent.parent


class TestBuildArgv(unittest.TestCase):
    """Each command's argv, which is the whole of what this file decides."""

    def argv(self, command, extra=()):
        return cli.build_argv(command, extra, python="py.exe",
                              config="C:\\cfg.json")

    def test_the_wizard_takes_the_config_as_an_option(self):
        # --output, because timelapse_setup.py's positional argument is not the
        # config: it has none, and passing one would be silently ignored.
        argv = self.argv("setup")
        self.assertIn("--output", argv)
        self.assertEqual(argv[argv.index("--output") + 1], "C:\\cfg.json")

    def test_everything_else_takes_it_positionally(self):
        for command in ("test", "usage", "encode", "web-serve"):
            argv = self.argv(command)
            self.assertNotIn("--output", argv)
            self.assertEqual(argv[-1], "C:\\cfg.json")

    def test_discover_is_given_no_config_at_all(self):
        """It presents no credentials to anything, so it cannot lock a camera

        account, and it neither reads nor writes the config. Handing it one
        would imply otherwise.
        """
        self.assertNotIn("C:\\cfg.json", self.argv("discover"))

    def test_the_flags_come_before_the_config(self):
        argv = self.argv("cameras")
        self.assertLess(argv.index("--cameras-only"), argv.index("--output"))

    def test_extra_arguments_are_passed_through_last(self):
        """This file must not know that -x:Doorbell exists. Everything after

        the command goes to the script unread, which is what makes
        `timelapse cameras --help` show that command's options and not these.
        """
        argv = self.argv("cameras", ["-x:Doorbell"])
        self.assertEqual(argv[-1], "-x:Doorbell")

    def test_every_command_names_a_script_that_exists(self):
        for command in cli.COMMANDS:
            argv = self.argv(command)
            self.assertTrue(Path(argv[1]).name.startswith("timelapse_"),
                            command)
            self.assertTrue((ROOT / "scripts" / Path(argv[1]).name).exists(),
                            "%s points at a script that is not there" % command)

    def test_the_scripts_are_found_beside_this_one(self):
        """Derived, not baked. The installer copies them together, so the

        wrapper needs only the interpreter and this file's path, and moving the
        install directory cannot leave it pointing at the old layout.
        """
        self.assertEqual(Path(cli.script("setup")).parent, cli.HERE)


class TestNoDrift(unittest.TestCase):
    """The two front doors, held to the same set of commands.

    Read out of install.sh rather than restated here, because a list written
    twice is the thing being guarded against.
    """

    # Linux-only, each for a stated reason rather than by oversight. If one of
    # these ever arrives on Windows, delete it from here and the pin resumes
    # covering it.
    ABSENT_ON_WINDOWS = {
        # Upgrading in place is Linux-only so far (item 11f). The dispatcher
        # answers this command with what to do instead, so it is not silence.
        "update",
    }

    @staticmethod
    def bash_commands():
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        marker = 'case "\\${1:-}" in'
        body = text.split(marker, 1)[1].split("\nesac", 1)[0]
        return set(re.findall(r"^    ([a-z][a-z0-9-]*)\)", body, re.M))

    def windows_commands(self):
        return set(cli.COMMANDS) | set(cli.SPECIAL)

    def test_the_extraction_actually_found_something(self):
        # Without this, a change to install.sh's formatting would empty the set
        # and every assertion below would pass by finding nothing.
        found = self.bash_commands()
        self.assertGreater(len(found), 10)
        self.assertIn("cameras", found)

    def test_windows_has_every_command_linux_does(self):
        missing = self.bash_commands() - self.windows_commands() \
            - self.ABSENT_ON_WINDOWS
        self.assertEqual(missing, set(),
                         "these exist on Linux and not on Windows")

    def test_windows_has_invented_nothing_linux_lacks(self):
        extra = self.windows_commands() - self.bash_commands()
        self.assertEqual(extra, set(),
                         "these exist on Windows and not on Linux")

    def test_the_absent_ones_are_answered_rather_than_unknown(self):
        """A command that exists on the other platform must not come back as

        "unknown command": that reads as a typo, and the operator retypes it.
        """
        for command in self.ABSENT_ON_WINDOWS:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main([command]), 1)
            self.assertNotIn("unknown command", out.getvalue())
            self.assertIn("install.ps1", out.getvalue())

    def test_the_help_describes_every_command(self):
        for command in sorted(self.windows_commands()):
            self.assertIn(command, cli.HELP,
                          "%s is dispatchable and undocumented" % command)


class TestDispatch(unittest.TestCase):

    def drive(self, argv, elevated=True):
        out = io.StringIO()
        with mock.patch.object(cli, "is_elevated", return_value=elevated), \
             mock.patch.object(cli.subprocess, "run") as ran, \
             contextlib.redirect_stdout(out):
            ran.return_value = mock.Mock(returncode=0)
            code = cli.main(argv)
        return code, out.getvalue(), ran

    def test_a_privileged_command_refuses_without_privilege(self):
        code, out, ran = self.drive(["cameras"], elevated=False)
        self.assertEqual(code, 1)
        ran.assert_not_called()
        self.assertIn("camera passwords", out)
        # Against the platform's own hint rather than the word "administrator":
        # this dispatcher is only ever installed on Windows, but the test runs
        # on three Linux legs too, and what matters is that the refusal tells
        # the operator how to become able to do it on the box they are at.
        self.assertIn(cli.elevation_hint(), out)

    def test_discover_needs_none(self):
        code, _out, ran = self.drive(["discover"], elevated=False)
        self.assertEqual(code, 0)
        ran.assert_called_once()

    def test_an_unknown_command_is_a_usage_error(self):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["camrus"]), 1)
        self.assertIn("camrus", err.getvalue())
        self.assertIn("USAGE", out.getvalue())

    def test_no_command_prints_help_and_fails(self):
        """Same text as --help, but a bare invocation is a usage error, so the

        exit status has to say so for anything scripting around this.
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main([]), 1)
        self.assertIn("USAGE", out.getvalue())

    def test_help_succeeds(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--help"]), 0)
        self.assertIn("USAGE", out.getvalue())

    def test_it_survives_an_interpreter_that_will_not_run(self):
        out = io.StringIO()
        with mock.patch.object(cli, "is_elevated", return_value=True), \
             mock.patch.object(cli.subprocess, "run",
                               side_effect=OSError("no such file")), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["test"]), 1)
        self.assertIn("no such file", out.getvalue())


class TestLogSelection(unittest.TestCase):
    """Which file `timelapse logs` follows."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name, text="x\n"):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_it_picks_the_newest_by_name(self):
        """By name, not by mtime, which is this project's standing rule and is

        right here for a second reason: the daily handler names files by day,
        so a reader touching an old one cannot promote it.
        """
        self.write("capture-20260814.log")
        newest = self.write("capture-20260816.log")
        self.write("capture-20260815.log")
        self.assertEqual(cli.newest_log("capture", self.tmp), newest)

    def test_it_does_not_confuse_the_two_logs(self):
        self.write("encode-20260816.log")
        self.assertIsNone(cli.newest_log("capture", self.tmp))

    def test_nothing_there_is_not_an_error(self):
        self.assertIsNone(cli.newest_log("capture", self.tmp))

    def test_a_missing_directory_is_not_an_error(self):
        self.assertIsNone(cli.newest_log("capture", self.tmp / "nope"))

    def test_it_says_where_it_looked_when_there_is_nothing(self):
        out = io.StringIO()
        with mock.patch.object(cli, "newest_log", return_value=None), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.do_logs([]), 1)
        self.assertIn("log_dir", out.getvalue())


class TestInstallerText(unittest.TestCase):
    """install.ps1's encoding and its file list, which no unit test can run.

    PowerShell cannot be executed on the Linux legs and elevation cannot be had
    on the Windows one, so the installer is verified by hand
    (temp/step3b_check.ps1). These two properties are the ones that are cheap
    to check here and expensive to notice there.
    """

    SOURCE = (ROOT / "install.ps1").read_bytes()

    def test_it_is_pure_ascii(self):
        """Windows PowerShell 5.1 reads a .ps1 with no byte order mark as ANSI.

        Measured: install.sh's box-drawing banner comes out as a wall of
        mojibake, so the installer's very first line looks like a corrupted
        download, at the worst possible moment to look broken. A BOM is the
        usual fix; ASCII is the one that does not make this the single file in
        the repo with its own encoding rule.
        """
        offenders = [(self.SOURCE[:i].count(b"\n") + 1, byte)
                     for i, byte in enumerate(self.SOURCE) if byte > 127]
        self.assertEqual(offenders, [],
                         "non-ASCII in install.ps1 renders as mojibake on 5.1")

    def test_no_python_one_liner_contains_a_quote(self):
        """PowerShell strips embedded double quotes on the way to a native exe.

        Measured on the first real elevated install: `-c 'import sys;
        print("%d.%d" % sys.version_info[:2])'` arrived at Python as
        `print(%d.%d % sys.version_info[:2])` and died with a SyntaxError
        pointing at a percent sign. The version then came back empty, the
        check refused a perfectly good 3.12, and the message read "this is ."
        with nothing after it. Eleven of eighteen checks failed downstream of
        that one line.

        A quote inside `-c '...'` is therefore banned outright rather than
        reasoned about per call site: the formatting belongs on the PowerShell
        side, where the quoting rules are known.
        """
        text = self.SOURCE.decode("ascii")
        for snippet in re.findall(r"-c '([^']*)'", text):
            self.assertNotIn('"', snippet,
                             "a double quote here is silently eaten: " + snippet)

    def test_it_installs_exactly_the_scripts_that_exist(self):
        """A third list of the same files, in a third language. The other two

        are pinned in test_usage.py (install.sh) and above (the dispatcher).
        """
        text = self.SOURCE.decode("ascii")
        block = text.split("$SCRIPTS = @(", 1)[1].split(")", 1)[0]
        listed = set(re.findall(r"'(timelapse_\w+\.py)'", block))
        on_disk = {p.name for p in (ROOT / "scripts").glob("timelapse_*.py")}
        self.assertEqual(listed, on_disk)

    def test_it_calls_the_wizard_rather_than_registering_anything_itself(self):
        """The rule tools/ was deleted over. Two installers that both know how

        to register a service disagree within one release, so this one must
        contain no sc.exe, no schtasks and no New-Service.
        """
        text = self.SOURCE.decode("ascii")
        body = text.split("# --- main", 1)[0] + text.split("# --- main", 1)[1]
        self.assertIn("--install-units", body)
        for forbidden in ("New-Service", "Register-ScheduledTask"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_the_closing_advice_says_new_and_says_administrator(self):
        """It said "Open a NEW terminal" and stopped there, so the operator's

        next action was a command that refuses. Both halves need saying: new,
        because PATH only reaches windows opened afterwards, and administrator,
        because the two commands named read the file holding camera passwords.
        """
        text = self.SOURCE.decode("ascii")
        steps = text.split("Step 'Next steps'", 1)[1]
        self.assertIn("NEW", steps)
        self.assertIn("ADMINISTRATOR", steps)

    def test_the_commands_it_names_are_ones_the_cli_has(self):
        text = self.SOURCE.decode("ascii")
        steps = text.split("Step 'Next steps'", 1)[1]
        named = set(re.findall(r"^\s*Say '  (timelapse \w[\w-]*)", steps,
                               re.M))
        self.assertTrue(named, "it names no commands at all")
        for line in named:
            command = line.split()[1]
            self.assertIn(command, set(cli.COMMANDS) | set(cli.SPECIAL),
                          "install.ps1 recommends a command that does not exist")

    def test_uninstalling_leaves_the_recordings_alone(self):
        text = self.SOURCE.decode("ascii")
        uninstall = text.split("function Invoke-Uninstall", 1)[1] \
                        .split("\n# --- main", 1)[0]
        self.assertNotIn("Remove-Item -Recurse -Force $CONFDIR", uninstall)
        self.assertIn("Left alone", uninstall)


class TestVersions(unittest.TestCase):

    def test_it_reports_every_installed_script(self):
        found = dict(cli.installed_versions())
        self.assertEqual(set(found), set(cli.SCRIPTS))
        for name, version in found.items():
            self.assertRegex(version, r"^\d+\.\d+\.\d+", name)

    def test_the_eight_scripts_match_what_the_installers_place(self):
        """Three lists of files, in three languages, and this pins two of them

        to the source tree. install.sh's is checked by the same reasoning in
        test_usage.py.
        """
        on_disk = {p.stem.replace("timelapse_", "")
                   for p in (ROOT / "scripts").glob("timelapse_*.py")}
        self.assertEqual(set(cli.SCRIPTS), on_disk)


if __name__ == "__main__":
    unittest.main()

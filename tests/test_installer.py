"""Unit tests for the Windows .exe installer: the .iss, its prerequisite
stage, and the pinned downloads they share.

None of this can be *run* on either CI leg. Inno Setup is not installed on the
Linux runners and compiling on the Windows one would prove only that the script
compiles, not that it installs; prepare.ps1 downloads a Python and unpacks an
ffmpeg, which is not a thing a unit test does. So what is checked here is the
same class of property the rest of the installer is held to: the encoding, the
version agreement, the file list, the pins, and above all the seam.

The seam is the point. install.ps1 is the only program in this project that
knows how to install anything, and the .exe and its prerequisite stage are
front doors onto it. This project deleted a whole directory over two programs
that both knew how to do the same job and drifted, so the rule is asserted
rather than promised: no sc.exe, no schtasks, no icacls, no New-Service, in
either new file, and install.ps1 named on both the install and the uninstall
path.
"""

import json
import re
import unittest
from pathlib import Path

import _support                                            # noqa: F401

import timelapse_platform as plat                          # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"


def code_only(text):
    """The source with comment lines dropped.

    Because every one of these properties is about what a file *does*, and a
    scan over raw text finds the comment explaining the rule it enforces. That
    has bitten this project three times now: a check that tkinter appears
    nowhere in the wizard's decide half failed on the docstring saying tkinter
    is imported lazily, and a check that the launcher contains no sc.exe failed
    on a comment saying it contains no sc.exe. Prose that describes a
    constraint is not a breach of it.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith(";"):
            continue
        kept.append(line)
    return "\n".join(kept)


class TestPins(unittest.TestCase):
    """prerequisites.json: one table, read by three things.

    prepare.ps1 downloads from it, the release workflow HEADs both URLs so a
    stale pin is found before a release rather than by the first person to run
    the installer, and this file checks the shape. A pin is only as good as the
    fields around it, so all of them are asserted rather than the hash alone.
    """

    PINS = json.loads((INSTALLER / "prerequisites.json").read_text("utf-8"))

    def entries(self):
        return [(name, self.PINS[name]) for name in ("python", "ffmpeg")]

    def test_both_prerequisites_are_pinned(self):
        for name in ("python", "ffmpeg"):
            self.assertIn(name, self.PINS)

    def test_every_download_is_https(self):
        # The hash is what proves the bytes; https is what stops a downgrade
        # being offered in the first place.
        for name, pin in self.entries():
            self.assertTrue(pin["url"].startswith("https://"), name)

    def test_every_hash_is_a_real_sha256(self):
        for name, pin in self.entries():
            self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$", name)

    def test_every_pin_carries_a_size_as_well_as_a_hash(self):
        """Not redundant, and the reason is measured elsewhere in this project.

        A 404 page, a captive portal and a proxy error all arrive as a
        successful HTTP response. Reporting one of those as a hash mismatch
        reads as a corrupted download; reporting it as four kilobytes where a
        hundred megabytes were expected reads as the network, which is what it
        is. The size is also the only field the release workflow can check
        without downloading the file.
        """
        for name, pin in self.entries():
            self.assertIsInstance(pin["bytes"], int, name)
            self.assertGreater(pin["bytes"], 1000000, name)

    def test_the_url_and_the_version_agree(self):
        # A pin whose URL says one version and whose version field says another
        # cannot be refreshed by anybody but its author.
        for name, pin in self.entries():
            self.assertIn(pin["version"], pin["url"], name)

    def test_each_pin_says_how_to_refresh_it(self):
        # Because the next person to bump these will not be the one who chose
        # them, and a hash with no provenance is a hash nobody dares to touch.
        for name, pin in self.entries():
            self.assertGreater(len(pin.get("refresh", "")), 80, name)

    def test_the_ffmpeg_pin_names_the_folder_inside_the_archive(self):
        """The layout in the zip is pinned too, so that a vendor changing it

        reports the path that was expected rather than a missing ffmpeg.exe
        with no explanation.
        """
        self.assertTrue(self.PINS["ffmpeg"]["binaries"].endswith("bin"))

    def test_the_ffmpeg_pin_is_a_versioned_url_not_a_moving_one(self):
        """gyan.dev publishes both. ffmpeg-release-essentials.zip is whatever

        is current, so a hash pinned against it is wrong within weeks and the
        installer then refuses a download that is perfectly good. The packages/
        path is immutable.
        """
        self.assertIn("/packages/", self.PINS["ffmpeg"]["url"])
        self.assertNotIn("release-essentials", self.PINS["ffmpeg"]["url"])


class TestPrepareStage(unittest.TestCase):
    """installer/prepare.ps1: prerequisites, and nothing else.

    Its whole justification is that install.ps1's refusal to install Python is
    right for a checkout and wrong for a .exe. That makes the boundary between
    them the thing worth pinning: this stage may fetch tools, and may not place
    a file, register a service or write a config.
    """

    SOURCE = (INSTALLER / "prepare.ps1").read_text(encoding="ascii")

    def setUp(self):
        self.code = code_only(self.SOURCE)

    def test_it_installs_nothing_itself(self):
        """The rule tools/ was deleted over, met on a third file.

        install.ps1 places the scripts, sets the ACL, writes the wrapper and
        calls --install-units. A prerequisite stage that learned any of that
        would be a second installer, and the second installer is always the one
        that drifts, because it is the one nobody runs from a terminal.
        """
        self.assertIn("install.ps1", self.code, "the scan found no code")
        for forbidden in ("sc.exe", "New-Service", "schtasks",
                          "Register-ScheduledTask", "icacls", "--install-units",
                          "SetEnvironmentVariable"):
            self.assertNotIn(forbidden, self.code, forbidden)

    def test_it_hands_over_to_install_ps1(self):
        # Front-ending it is the entire design. A stage that only checked
        # prerequisites and left the operator to run something else would be a
        # download-and-double-click that does not install anything.
        self.assertIn("install.ps1", self.code)
        self.assertIn("-NoWizard", self.code)

    def test_it_does_not_walk_the_console_wizard(self):
        """-NoWizard, because the finish page offers the graphical one.

        A .exe that ends by opening a console and asking for camera passwords
        one at a time is precisely what the graphical wizard was built to
        replace, and an upgrade must not walk the wizard at all.
        """
        self.assertNotIn("--defaults", self.code)
        self.assertIn("-NoWizard", self.code)

    def test_the_pins_are_read_rather_than_repeated(self):
        """One table, and no second copy of a URL to fall out of step with it.

        A hash in prerequisites.json and a URL in the script is how a pin comes
        to verify one file while downloading another.
        """
        self.assertIn("prerequisites.json", self.code)
        self.assertNotIn("python.org/ftp", self.code)
        self.assertNotIn("gyan.dev/ffmpeg/builds/packages", self.code)

    def test_nothing_is_run_before_it_is_verified(self):
        # Both checks, in the one function everything downloads through, and
        # both before the caller ever sees a path.
        block = self.SOURCE.split("function Get-Verified", 1)[1]
        block = block.split("\nfunction ", 1)[0]
        self.assertIn("Get-FileHash", block)
        self.assertIn("-Algorithm SHA256", block)
        self.assertIn(".Length", block)

    def test_a_download_needs_both_permission_and_an_absence(self):
        """Two conditions, never one.

        The switch alone would replace an ffmpeg the operator chose, which item
        11c.6a refused. The absence alone would download without being asked.
        Both branches are elseif clauses hanging off a successful find, which is
        what makes a ticked checkbox safe to leave ticked.
        """
        for tool, switch in (("Python", "AllowPython"), ("ffmpeg", "AllowFfmpeg")):
            self.assertIn("elseif ($%s)" % switch, self.code, tool)

    def test_it_asks_the_product_where_ffmpeg_is(self):
        """--find-tool, not a second search written in PowerShell.

        The question is "will the wizard find one", and the only answer that
        cannot be wrong is the wizard's own. A PowerShell reimplementation
        would be a second opinion that disagrees on the machines nobody tests.
        """
        self.assertIn("--find-tool ffmpeg", self.code)
        self.assertIn("timelapse_platform.py", self.code)

    def test_it_negotiates_tls_12(self):
        """5.1 offers SSL3 and TLS 1.0 by default on some builds, and both

        vendors refuse those, so without this every download fails with a
        connection error that says nothing about TLS.
        """
        self.assertIn("Tls12", self.code)

    def test_it_captures_what_install_ps1_says(self):
        """It runs hidden behind a progress bar, so anything install.ps1 prints

        goes nowhere unless it is captured: the same trap as a service writing
        to a stderr the SCM discards, and answered the same way. install.ps1
        warns about a per-user Python and about the PATH, and both are worth
        more than the exit code they arrive with.
        """
        self.assertIn("RedirectStandardOutput", self.code)
        self.assertIn("RedirectStandardError", self.code)

    def test_the_log_is_somewhere_a_support_request_can_find(self):
        self.assertIn("install.log", self.code)


class TestNativeCallsUnderStop(unittest.TestCase):
    """The defect a clean VM found, and it had shipped.

    Every .ps1 here sets $ErrorActionPreference to Stop, which is right for the
    cmdlets: a Copy-Item that fails must not be walked past. But PowerShell 5.1
    wraps every stderr line from a NATIVE command in a NativeCommandError
    record, and under Stop that throws. So `python -c import requests` on a
    machine without requests, which is a probe whose whole purpose is to fail,
    killed install.ps1 with a traceback instead of returning 1.

    It survived from the first Windows release because every machine it had run
    on already had requests: the probe succeeded, wrote nothing to stderr, and
    the trap never fired. Reproduced here 2026-08-18 and then fixed.

    These scan for the shape rather than for the one call site, because the
    next one will be somewhere else.
    """

    FILES = ("install.ps1", "installer/prepare.ps1")

    def sources(self):
        return [(name, (ROOT / name).read_text(encoding="ascii"))
                for name in self.FILES]

    def test_a_probe_that_is_meant_to_fail_goes_through_the_helper(self):
        source = (ROOT / "install.ps1").read_text(encoding="ascii")
        block = source.split("function Test-Requests", 1)[1]
        block = block.split("\nfunction Protect-", 1)[0]
        self.assertIn("Invoke-Tool", block)

    def test_the_helper_relaxes_the_preference_and_puts_it_back(self):
        source = (ROOT / "install.ps1").read_text(encoding="ascii")
        block = source.split("function Invoke-Tool", 1)[1]
        block = block.split("\nfunction ", 1)[0]
        self.assertIn("$ErrorActionPreference = 'Continue'", block)
        self.assertIn("finally", block)
        self.assertIn("$ErrorActionPreference = $previous", block)

    def test_the_helper_does_not_trust_a_stale_exit_code(self):
        """$LASTEXITCODE is only written by a process that actually started.

        One that could not leaves the previous call's code sitting there, which
        is then read as this one's answer. The harness for this installer had
        the same bug on the same day, in the other direction: it reported PASS
        for a command that did not exist.
        """
        source = (ROOT / "install.ps1").read_text(encoding="ascii")
        block = source.split("function Invoke-Tool", 1)[1]
        block = block.split("\nfunction ", 1)[0]
        self.assertIn("$global:LASTEXITCODE = -1", block)

    def test_no_bare_native_call_redirects_stderr_to_null(self):
        """`& $thing ... 2>$null` outside a try or the helper is the trap.

        2>$null looks like "ignore the errors" and is the opposite: it routes
        them into PowerShell's error stream, where Stop makes them fatal. The
        allowed forms are inside a try/catch, or through Invoke-Tool.
        """
        for name, source in self.sources():
            for number, line in enumerate(source.splitlines(), 1):
                if "2>$null" not in line or line.lstrip().startswith("#"):
                    continue
                # The guarded ones assign, so the surrounding try/catch or the
                # relaxed preference is visible within a few lines above.
                above = "\n".join(source.splitlines()[max(0, number - 12):number])
                guarded = ("try {" in above
                           or "$ErrorActionPreference = 'Continue'" in above)
                self.assertTrue(guarded,
                                "%s:%d redirects a native stderr with nothing "
                                "catching it: %s" % (name, number, line.strip()))

    def test_no_native_command_feeds_select_object_first(self):
        """Measured 2026-08-18: 39 failures in 40 runs.

        Select-Object -First stops the pipeline the moment it has enough, and
        PowerShell then terminates the upstream native process, so
        $LASTEXITCODE comes back -1 for a command that did exactly what was
        asked. `& ffmpeg -version | Select-Object -First 1` is therefore a
        reliable way to conclude that ffmpeg will not run, and it is how a
        clean VM came to be told its perfectly good download was broken.

        Worth recording how it was nearly missed: the first measurement took
        ONE sample, hit the one run in forty that finishes before the pipeline
        stops it, and concluded the pattern was innocent. A single sample of a
        race is not evidence, which this project already knew from measuring
        retry recovery against an unanchored failure phase.

        The fix is always the same shape: capture the whole stream with
        Out-String, then take the line you want out of the string.
        """
        pattern = re.compile(r"&\s*\$\w+.*\|\s*Select-Object\s+-First")
        for name, source in self.sources():
            for number, line in enumerate(source.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                self.assertIsNone(
                    pattern.search(line),
                    "%s:%d terminates the process it is measuring: %s"
                    % (name, number, line.strip()))

    def test_the_ffmpeg_probe_keeps_what_it_was_told(self):
        """The rule this project already had, broken here and restored.

        Never discard a probe's stderr: "Unknown encoder" and "No capable
        devices found" need opposite fixes and share an exit code. The same
        applies to a binary that will not start at all, where the message is
        the only thing separating a blocked download from a missing runtime.
        The first version of this said "will not run" and nothing else, which
        is a conclusion with no evidence under it.
        """
        source = (ROOT / "installer" / "prepare.ps1").read_text(encoding="ascii")
        block = source.split("ffmpeg unpacked but will not run", 1)[1]
        block = block.split("return $null", 1)[0]
        self.assertIn("exit code", block)
        self.assertIn("Note", block)


class TestInnoScript(unittest.TestCase):
    """installer/timelapse-maker.iss: what the .exe is made of."""

    PATH = INSTALLER / "timelapse-maker.iss"
    RAW = PATH.read_bytes()
    SOURCE = RAW.decode("ascii")

    def setUp(self):
        self.code = code_only(self.SOURCE)

    def test_it_is_pure_ascii(self):
        """Same rule as every .ps1 here, and for the same reason: no file in

        this project gets its own encoding note, and mojibake in an installer's
        first screen is the worst possible moment to look like a bad download.
        """
        offenders = [(self.RAW[:i].count(b"\n") + 1, byte)
                     for i, byte in enumerate(self.RAW) if byte > 127]
        self.assertEqual(offenders, [])

    def test_the_default_version_matches_install_ps1(self):
        """The release workflow passes /DAppVersion from the tag, so this

        default only reaches a local build. It is still held against the tree,
        because a version that lives in ten places drifts in one of them and
        this is the tenth.
        """
        source = (ROOT / "install.ps1").read_text(encoding="ascii")
        expected = re.search(r"\$VERSION\s*=\s*'([0-9.]+)'", source).group(1)
        found = re.search(r'#define AppVersion "([0-9.]+)"', self.SOURCE)
        self.assertIsNotNone(found, "the .iss defines no fallback version")
        self.assertEqual(found.group(1), expected)

    def test_the_version_can_be_overridden_from_the_command_line(self):
        # #ifndef, or /DAppVersion would be a redefinition error rather than a
        # value, and the release build would fail on the tag it was given.
        self.assertIn("#ifndef AppVersion", self.SOURCE)

    def sources(self):
        found = re.findall(r'^Source:\s*"([^"]+)"', self.SOURCE, re.M)
        assert found, "the scan found no [Files] entries"
        return found

    @staticmethod
    def resolve(entry):
        """An Inno path is Windows-shaped wherever this test happens to run.

        Split on the separator rather than handing the string to Path, which
        reads `..\\install.ps1` as a traversal on one platform and as a single
        filename containing a backslash on the other. The first version did
        exactly that and passed here while failing on all three Linux legs,
        which is the same shape as `os.path` being `ntpath` on the Windows
        runner: a path that belongs to a platform must be built for that
        platform, never for the one doing the building.
        """
        return INSTALLER.joinpath(*entry.split("\\"))

    def test_every_file_it_ships_exists(self):
        """Paths are relative to the .iss, which is one directory down. A typo

        here is an installer that compiles and then delivers a release tree
        with a hole in it.
        """
        for entry in self.sources():
            target = self.resolve(entry)
            if "*" in target.name:
                # The wildcard goes to glob and the "\..\" stays in the
                # directory, because pathlib will walk a parent it is given and
                # will not match one inside a pattern.
                matches = list(target.parent.glob(target.name))
                self.assertTrue(matches, "nothing matches " + entry)
                continue
            self.assertTrue(target.exists(), entry)

    def test_it_ships_every_script_install_ps1_will_look_for(self):
        """The payload has to contain what install.ps1 installs, or the .exe

        dies on a missing file after the operator has already agreed to
        everything. install.ps1 lists them explicitly and this globs, which is
        safe in one direction only: a glob that is too wide reaches nothing,
        because install.ps1's list is what decides.
        """
        self.assertTrue(any(entry.endswith("scripts\\*.py")
                            for entry in self.sources()))

    def test_it_does_not_ship_the_real_config(self):
        """config.json holds every camera password. It is gitignored so that a

        build machine cannot pick one up, and named here so that a later edit
        cannot quietly add it back.
        """
        for entry in self.sources():
            self.assertNotIn("config.json", entry.replace("config.example.json", ""))

    def test_it_registers_nothing_itself(self):
        """The rule tools/ was deleted over, met on a fourth file, and this is

        the one where breaking it would be easiest: Inno has sections for
        services, registry keys and permissions, and using any of them here
        would put a second opinion about installation inside the installer.
        """
        for forbidden in ("sc.exe", "schtasks", "New-Service", "icacls",
                          "[Registry]", "ServiceInstall"):
            self.assertNotIn(forbidden, self.code, forbidden)

    def test_installing_goes_through_prepare_which_goes_through_install_ps1(self):
        self.assertIn("prepare.ps1", self.code)

    def test_uninstalling_goes_through_install_ps1(self):
        """It deregisters the service and both tasks. An [UninstallDelete] that

        merely removed the files would leave a registered service pointing at a
        directory that no longer exists, which fails at the next boot rather
        than at the uninstall.
        """
        block = self.SOURCE.split("[UninstallRun]", 1)[1].split("\n[", 1)[0]
        self.assertIn("install.ps1", block)
        self.assertIn("-Uninstall", block)

    def test_a_downloaded_ffmpeg_is_removed_again(self):
        """It is unpacked after the file list was recorded, so nothing else

        would take it away: 300 MB left behind by an uninstall that reported
        success. An ffmpeg the operator installed themselves is somewhere else
        entirely and is untouched.
        """
        block = self.SOURCE.split("[UninstallDelete]", 1)[1].split("\n[", 1)[0]
        self.assertIn("ffmpeg", block)

    def test_it_asks_for_administrator_up_front(self):
        """Everything this installs is machine-wide: a service, two scheduled

        tasks, the system PATH, a directory under Program Files. Asking at the
        start beats failing at the first write, thirty seconds in.
        """
        self.assertIn("PrivilegesRequired=admin", self.SOURCE)

    def test_the_app_id_is_fixed(self):
        """It is what makes the next release upgrade this one rather than sit

        beside it in Add or remove programs. Changing it strands the old entry
        with an uninstaller for files that are gone.
        """
        found = re.search(r"^AppId=\{\{([0-9A-F-]{36})", self.SOURCE, re.M)
        self.assertIsNotNone(found, "AppId is not a fixed GUID")

    def test_the_filename_carries_the_version(self):
        # Two installers in a downloads folder, and nothing else tells them
        # apart: the release page name is the only version an operator sees
        # before running it.
        self.assertIn("OutputBaseFilename=timelapse-maker-setup-{#AppVersion}",
                      self.SOURCE)

    def test_the_prerequisite_checkboxes_say_they_only_act_when_missing(self):
        """Both are ticked by default, which is only safe because neither

        replaces anything. The wording has to carry that, or a ticked box reads
        as an offer to install a second ffmpeg over the operator's own.
        """
        block = self.SOURCE.split("[Tasks]", 1)[1].split("\n[", 1)[0]
        self.assertEqual(block.lower().count("if this machine has none"), 2)

    def test_a_failed_stage_is_not_reported_as_a_success(self):
        """The finish page says "Setup has finished installing" whatever

        happened, so without this the last thing an operator reads about a
        failed install is a success. Same shape as the nightly encode calling
        an idle run a fault: the page has to say which of the two it was.
        """
        self.assertIn("wpFinished", self.code)
        self.assertIn("StageFailed", self.code)


class TestReleaseWorkflow(unittest.TestCase):
    """.github/workflows/installer.yml: the build nobody has to run by hand."""

    SOURCE = (ROOT / ".github" / "workflows" / "installer.yml").read_text("utf-8")

    def test_it_builds_on_a_tag(self):
        self.assertIn("tags: ['v*']", self.SOURCE)

    def test_it_passes_the_version_in_rather_than_trusting_the_default(self):
        self.assertIn("/DAppVersion=", self.SOURCE)

    def test_it_refuses_a_tag_that_disagrees_with_the_tree(self):
        """A tag cannot be moved once anybody has fetched it, so a release

        built from a tree still carrying the previous version reports the wrong
        one from `timelapse version` for ever. Nine files carry that string.
        """
        self.assertIn("install.ps1 says", self.SOURCE)

    def test_it_checks_the_pins_are_still_live(self):
        """They are used at install time, not build time, so nothing in the

        build would notice one going stale. The first person to run the
        installer would.
        """
        self.assertIn("prerequisites.json", self.SOURCE)
        self.assertIn("Method Head", self.SOURCE)

    def test_it_publishes_a_checksum(self):
        """The installer is not code-signed, so SmartScreen will warn about it

        and a published hash is the only way anybody can tell the real download
        from something that looks like it.
        """
        self.assertIn("sha256", self.SOURCE.lower())

    def test_it_installs_inno_setup_rather_than_assuming_it(self):
        """It was preinstalled on the Server 2022 image and is not on Server

        2025, which windows-latest now is. Installing it explicitly means this
        does not start failing the next time the image moves.
        """
        self.assertIn("choco install innosetup", self.SOURCE)


class TestOnePythonFinder(unittest.TestCase):
    """Three PowerShell files look for Python, and they must agree.

    install.ps1 bakes the path into a service command line, setup-gui.ps1 needs
    one to start the wizard, and prepare.ps1 has to decide whether to install
    one. They are three copies because the third runs before either of the
    others exists on the machine, which is the same argument the capture daemon
    makes for its five pinned duplicates: the copy that drifts is the one
    nobody watches, so the properties are pinned rather than the prose.
    """

    FILES = ("install.ps1", "setup-gui.ps1", "installer/prepare.ps1")

    def sources(self):
        return [(name, (ROOT / name).read_text(encoding="ascii"))
                for name in self.FILES]

    def test_they_all_skip_the_microsoft_store_stub(self):
        """It is on PATH by default, is not an interpreter, reports a version

        quite happily, and opens the Store when run. As a service command line
        that is a service which starts and does nothing.
        """
        for name, source in self.sources():
            self.assertIn("WindowsApps", code_only(source), name)

    def test_they_all_ask_the_launcher_first(self):
        """py -3 knows about every interpreter on the machine, including the

        ones not on PATH. Starting with PATH instead would miss an all-users
        install on a box where somebody had put a per-user one first.
        """
        for name, source in self.sources():
            self.assertIn("py -3", code_only(source), name)

    def test_the_two_that_install_enforce_the_floor(self):
        """3.9, because RHEL 9 and Debian 11 ship it as the system python3.

        setup-gui.ps1 is exempt: it starts a wizard on a machine that is
        already installed, so the floor was settled before it ran.
        """
        for name in ("install.ps1", "installer/prepare.ps1"):
            self.assertIn("3.9", (ROOT / name).read_text(encoding="ascii"), name)


if __name__ == "__main__":
    unittest.main()

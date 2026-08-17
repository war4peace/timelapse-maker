"""Unit tests for timelapse_gui.py: the decide layer, no widgets involved.

The split this file relies on is the point of 11c.6b, not an implementation
detail. Everything above the SHOW LAYER banner in that module takes strings and
returns (level, message), so it can be checked on a CI runner with no window
station at all, and the widget half stays thin enough to verify by hand.

Two structural tests here matter more than any single check: that tkinter is
not imported at module scope, and that the module decides nothing the console
wizard does not already decide.
"""

import ast
import unittest
from pathlib import Path
from unittest import mock

import _support

import timelapse_gui as gui
import timelapse_setup as setup


SOURCE = Path(_support.SCRIPTS) / "timelapse_gui.py"


class TestNoDisplayNeeded(unittest.TestCase):
    """Importing this must not need a window station.

    The suite runs on CI runners and in WSL. A module-level `import tkinter`
    would take every one of them down at collection time, which is the same
    reasoning that keeps the Win32 bindings behind a lazy binder.
    """

    def test_tkinter_is_not_imported_at_module_scope(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            self.assertFalse(any(n.split(".")[0] == "tkinter" for n in names),
                             "tkinter must be imported inside a function")

    def test_importing_it_does_not_pull_tkinter_in(self):
        """Measured in a fresh interpreter, not by inspecting sys.modules here.

        The first version asserted against this process's sys.modules and so
        depended on test *order*: any earlier test that reached the message-box
        path imported tkinter, and this then failed for a reason that had
        nothing to do with the property being claimed.
        """
        import subprocess
        import sys

        code = ("import sys; sys.path.insert(0, %r); import timelapse_gui; "
                "print('tkinter' in sys.modules)" % str(_support.SCRIPTS))
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "False", out.stdout)


class TestDecideLayerIsSeparate(unittest.TestCase):
    """No widget code above the banner, no decisions below it."""

    def names_above_the_banner(self):
        """Every identifier in the decide half, from tokens rather than text.

        Tokenising rather than scanning the source string, because the module
        docstring *explains* the lazy tkinter import and a text scan reads that
        prose as a violation. Comments and docstrings are not code.
        """
        import io
        import tokenize

        text = SOURCE.read_text(encoding="utf-8")
        lines = text.splitlines()
        banner = [n for n, line in enumerate(lines, 1)
                  if "# THE SHOW LAYER" in line]
        self.assertEqual(len(banner), 1,
                         "the banner is what the split is named by")

        names = set()
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.start[0] >= banner[0]:
                break
            if token.type == tokenize.NAME:
                names.add(token.string)
        return names

    def test_the_decide_half_uses_no_widgets(self):
        names = self.names_above_the_banner()
        self.assertGreater(len(names), 50, "the tokeniser found nothing")
        for word in ("tkinter", "tk", "ttk", "messagebox", "filedialog"):
            self.assertNotIn(word, names,
                             "%s belongs below the banner" % word)

    def test_every_check_returns_a_level_the_window_can_colour(self):
        # A check that invents a fourth level would render with no colour and
        # look like a bug in the window rather than in the check.
        levels = {gui.OK, gui.WARN, gui.FAIL}
        self.assertEqual(set(gui.COLOURS), levels)


class TestStorage(unittest.TestCase):

    def test_a_missing_answer_is_refused(self):
        level, message = gui.check_storage("")
        self.assertEqual(level, gui.FAIL)
        self.assertIn("folder", message)

    def test_a_relative_path_is_refused(self):
        level, _message = gui.check_storage("timelapse")
        self.assertEqual(level, gui.FAIL)

    def test_the_three_directories_match_the_console_wizard(self):
        # Same base, same layout, or a config written here differs from one
        # written there and the two wizards have drifted.
        paths = gui.storage_paths("D:\\data")
        self.assertTrue(paths["frames_root"].endswith("frames"))
        self.assertTrue(paths["video_output"].endswith("videos"))
        self.assertTrue(paths["log_dir"].endswith("logs"))
        for value in paths.values():
            self.assertTrue(value.startswith("D:"), value)

    def test_quotes_pasted_from_explorer_are_stripped(self):
        paths = gui.storage_paths('"D:\\data"')
        self.assertNotIn('"', paths["frames_root"])


class TestCadence(unittest.TestCase):

    def test_a_sane_interval(self):
        level, message, value = gui.check_interval("5")
        self.assertEqual((level, value), (gui.OK, 5))
        self.assertIn("17280", message)

    def test_text_is_refused_rather_than_crashing(self):
        self.assertEqual(gui.check_interval("often")[0], gui.FAIL)
        self.assertEqual(gui.check_interval("")[0], gui.FAIL)
        self.assertEqual(gui.check_interval(None)[0], gui.FAIL)

    def test_out_of_range(self):
        self.assertEqual(gui.check_interval("0")[0], gui.FAIL)
        self.assertEqual(gui.check_interval("4000")[0], gui.FAIL)

    def test_a_cadence_that_would_produce_nothing_is_refused(self):
        """The failure this check exists for: below encode.min_frames the

        encoder skips the day, so a 15 minute interval yields no video and no
        error either, for ever.
        """
        level, message, _value = gui.check_interval("900")
        self.assertEqual(level, gui.FAIL)
        self.assertIn("skipped", message)

    def test_a_slow_but_workable_cadence_only_warns(self):
        self.assertEqual(gui.check_interval("301")[0], gui.WARN)

    def test_the_frame_rate_reports_the_video_length(self):
        level, message, value = gui.check_framerate("60", 5)
        self.assertEqual((level, value), (gui.OK, 60))
        self.assertIn("of video", message)

    def test_the_frame_rate_alone_is_still_checked(self):
        self.assertEqual(gui.check_framerate("0")[0], gui.FAIL)
        self.assertEqual(gui.check_framerate("241")[0], gui.FAIL)
        self.assertEqual(gui.check_framerate("30")[0], gui.OK)


class TestCameraNames(unittest.TestCase):
    """The same rules the console wizard applies, reached the same way."""

    def test_a_clean_name_passes(self):
        level, _message, cleaned = gui.check_camera_name("Roof", [])
        self.assertEqual((level, cleaned), (gui.OK, "Roof"))

    def test_an_empty_name(self):
        self.assertEqual(gui.check_camera_name("  ", [])[0], gui.FAIL)

    def test_a_name_that_sanitises_to_nothing(self):
        level, message, _c = gui.check_camera_name("!!!", [])
        self.assertEqual(level, gui.FAIL)
        self.assertIn("letters", message)

    def test_a_stripped_name_warns_and_says_what_was_stored(self):
        level, message, cleaned = gui.check_camera_name("Roof Top!", [])
        self.assertEqual((level, cleaned), (gui.WARN, "RoofTop"))
        self.assertIn("RoofTop", message)

    def test_a_reserved_device_name_is_refused(self):
        # Measured at step 2: frames/NUL/<date> fails WinError 3 per frame, so
        # nothing is ever written and the day is silently skipped.
        level, message, _c = gui.check_camera_name("NUL", [])
        self.assertEqual(level, gui.FAIL)
        self.assertIn("reserves", message)

    def test_a_duplicate_is_refused_case_insensitively(self):
        cams = [{"name": "Roof"}]
        self.assertEqual(gui.check_camera_name("roof", cams)[0], gui.FAIL)

    def test_editing_a_camera_does_not_collide_with_itself(self):
        cam = {"name": "Roof"}
        level, _m, cleaned = gui.check_camera_name("Roof", [cam], skip=cam)
        self.assertEqual((level, cleaned), (gui.OK, "Roof"))

    def test_it_uses_the_wizard_rather_than_its_own_rules(self):
        # Patch below the seam: if the GUI had its own copy of the rule, the
        # patched one would not be reached and this would still pass.
        with mock.patch.object(setup, "name_taken", return_value=True):
            self.assertEqual(gui.check_camera_name("Anything", [])[0], gui.FAIL)


class TestCameraAddresses(unittest.TestCase):

    def test_http_wants_http(self):
        self.assertEqual(gui.check_camera_url("http", "rtsp://x/y")[0], gui.FAIL)
        self.assertEqual(gui.check_camera_url("http", "http://x/y")[0], gui.OK)
        self.assertEqual(gui.check_camera_url("http", "https://x/y")[0], gui.OK)

    def test_rtsp_wants_rtsp(self):
        self.assertEqual(gui.check_camera_url("rtsp", "http://x/y")[0], gui.FAIL)
        self.assertEqual(gui.check_camera_url("rtsp", "rtsp://x/y")[0], gui.OK)

    def test_nothing_typed(self):
        self.assertEqual(gui.check_camera_url("http", "")[0], gui.FAIL)


class TestDestination(unittest.TestCase):

    def test_a_mapped_drive_is_rewritten_and_says_so(self):
        """The single most likely way a Windows install fails, and the GUI has

        to make the same substitution the console wizard does, not merely warn.
        """
        with mock.patch.object(setup, "network_path",
                               return_value="\\\\tower\\cctv\\TL"):
            level, message, stored = gui.check_destination("U:\\TL")
        self.assertEqual((level, stored), (gui.WARN, "\\\\tower\\cctv\\TL"))
        self.assertIn("mapped drive", message)

    def test_an_ssh_spec_is_refused_as_linux_only(self):
        level, message, _s = gui.check_destination("user@nas:/vol/tl")
        self.assertEqual(level, gui.FAIL)
        self.assertIn("Linux", message)

    def test_an_unusable_drive_letter_is_refused_not_stored(self):
        with mock.patch.object(setup, "network_path", return_value=None), \
             mock.patch.object(setup, "drive_is_local", return_value=False):
            level, _m, stored = gui.check_destination("Q:\\videos")
        self.assertEqual((level, stored), (gui.FAIL, ""))

    def test_a_plain_local_folder_is_kept_as_typed(self):
        with mock.patch.object(setup, "network_path", return_value=None), \
             mock.patch.object(setup, "drive_is_local", return_value=True):
            level, _m, stored = gui.check_destination("D:\\videos")
        self.assertEqual((level, stored), (gui.OK, "D:\\videos"))

    def test_only_a_share_raises_the_account_question(self):
        self.assertTrue(gui.destination_needs_credentials("\\\\tower\\cctv"))
        self.assertFalse(gui.destination_needs_credentials("D:\\videos"))

    def test_the_advice_names_the_server_being_talked_about(self):
        text = gui.credentials_advice("\\\\tower\\cctv\\TL")
        self.assertIn("\\\\tower\\cctv", text)
        self.assertIn("system account", text)


class TestNotifyTargets(unittest.TestCase):

    def test_a_discord_webhook(self):
        url = "https://discord.com/api/webhooks/1/abc"
        self.assertEqual(gui.check_notify_target("discord", url)[0], gui.OK)
        self.assertEqual(gui.check_notify_target("discord", "abc")[0], gui.FAIL)

    def test_something_https_that_is_not_a_webhook_only_warns(self):
        # Refusing outright would block a self-hosted or proxied endpoint.
        level, _m, _v = gui.check_notify_target("discord",
                                                "https://example.invalid/hook")
        self.assertEqual(level, gui.WARN)

    def test_ntfy_wants_a_full_url(self):
        self.assertEqual(gui.check_notify_target("ntfy", "mytopic")[0], gui.FAIL)
        self.assertEqual(
            gui.check_notify_target("ntfy", "https://ntfy.sh/t")[0], gui.OK)

    def test_a_telegram_token_has_a_colon_in_it(self):
        self.assertEqual(gui.check_notify_target("telegram", "abc")[0], gui.FAIL)
        self.assertEqual(gui.check_notify_target("telegram", "1:ab")[0], gui.OK)


class TestReview(unittest.TestCase):

    def config(self, **over):
        cfg = {"paths": {"frames_root": "D:\\f", "video_output": "D:\\v",
                         "log_dir": "D:\\l", "ffmpeg": "C:\\ff\\ffmpeg.exe"},
               "capture": {"interval_seconds": 5},
               "encode": {"framerate": 60, "container": "mkv"},
               "transfer": {"enabled": False},
               "cameras": [{"name": "Roof", "enabled": True}]}
        cfg.update(over)
        return cfg

    def test_a_complete_config_has_nothing_left_to_fix(self):
        self.assertEqual(gui.preflight(self.config()), [])

    def test_no_ffmpeg_names_the_consequence(self):
        cfg = self.config()
        cfg["paths"]["ffmpeg"] = ""
        self.assertIn("encoded", " ".join(gui.preflight(cfg)))

    def test_no_enabled_camera_is_caught(self):
        cfg = self.config(cameras=[{"name": "Roof", "enabled": False}])
        self.assertIn("captured", " ".join(gui.preflight(cfg)))

    def test_transfer_on_with_no_destination_is_caught(self):
        cfg = self.config(transfer={"enabled": True})
        self.assertIn("destination", " ".join(gui.preflight(cfg)))

    def test_the_summary_never_shows_a_secret(self):
        """This panel is what gets screenshotted into a bug report.

        Names, never values: a webhook URL is the authority to post exactly as
        a password is, which is the rule summarise() had to learn.
        """
        cfg = self.config(transfer={"enabled": True, "destination": "\\\\t\\s",
                                    "username": "war", "password": "hunter2"})
        cfg["notify"] = [{"type": "discord", "enabled": True,
                          "url": "https://discord.com/api/webhooks/1/SEKRIT"}]
        text = " ".join("%s %s" % row for row in gui.summary_lines(cfg))
        self.assertNotIn("hunter2", text)
        self.assertNotIn("SEKRIT", text)
        self.assertIn("war", text, "the account name is the useful half")
        self.assertIn("discord", text.lower())

    def test_the_summary_says_what_off_means_rather_than_just_off(self):
        text = dict(gui.summary_lines(self.config()))["Transfer"]
        self.assertIn("videos folder", text)

    def test_the_next_steps_mention_the_destination_when_there_is_one(self):
        cfg = self.config(transfer={"enabled": True, "destination": "\\\\t\\s"})
        self.assertIn("\\\\t\\s", " ".join(gui.next_steps(cfg)))


class TestFfmpegCheck(unittest.TestCase):
    """Reuses detect_encoders(), which is why that stopped printing."""

    def test_nothing_typed_names_the_consequence(self):
        level, message, _f, _p, codec = gui.check_ffmpeg("")
        self.assertEqual((level, codec), (gui.FAIL, None))
        self.assertIn("ffmpeg.org", message)

    def test_an_ffmpeg_that_cannot_be_run_reports_the_reason(self):
        with mock.patch.object(setup, "detect_encoders",
                               return_value=(None, [], "could not run it")):
            level, message, _f, _p, _c = gui.check_ffmpeg("C:\\ff\\ffmpeg.exe")
        self.assertEqual(level, gui.FAIL)
        self.assertIn("could not run it", message.lower())

    def test_hardware_encoding_is_the_happy_answer(self):
        with mock.patch.object(setup, "detect_encoders",
                               return_value=("hevc_nvenc", [], "")), \
             mock.patch.object(setup, "sibling_tool",
                               return_value="C:\\ff\\ffprobe.exe"):
            level, message, ffmpeg, ffprobe, codec = \
                gui.check_ffmpeg("C:\\ff\\ffmpeg.exe")
        self.assertEqual((level, codec), (gui.OK, "hevc_nvenc"))
        self.assertEqual(ffprobe, "C:\\ff\\ffprobe.exe")
        self.assertIn("GPU", message)

    def test_the_cpu_fallback_warns_rather_than_failing(self):
        with mock.patch.object(setup, "detect_encoders",
                               return_value=("libx264", [], "")), \
             mock.patch.object(setup, "sibling_tool", return_value="p"):
            level, message, _f, _p, _c = gui.check_ffmpeg("C:\\ff\\ffmpeg.exe")
        self.assertEqual(level, gui.WARN)
        self.assertIn("slower", message)

    def test_a_missing_ffprobe_warns_and_asks_for_the_folder(self):
        with mock.patch.object(setup, "detect_encoders",
                               return_value=("hevc_nvenc", [], "")), \
             mock.patch.object(setup, "sibling_tool", return_value=""):
            level, message, _f, ffprobe, _c = \
                gui.check_ffmpeg("C:\\ff\\ffmpeg.exe")
        self.assertEqual((level, ffprobe), (gui.WARN, ""))
        self.assertIn("folder", message)

    def test_the_encoder_notes_explain_each_skip(self):
        failures = [("av1_nvenc", "No capable devices found", "the GPU is "
                                                              "too old")]
        with mock.patch.object(setup, "detect_encoders",
                               return_value=("hevc_nvenc", failures, "")):
            lines = gui.encoder_notes("C:\\ff\\ffmpeg.exe")
        self.assertEqual(len(lines), 1)
        self.assertIn("av1_nvenc", lines[0])
        self.assertIn("too old", lines[0])


class TestEntryPoint(unittest.TestCase):
    """main() itself, which is the part that had no coverage and broke.

    The decide/show split made every *rule* testable and left the glue between
    them untested, so an editing slip removed the line that sets config_path
    and every test still passed: the off-Windows test returns before reaching
    it, and nothing else called main() on the Windows path. It failed with a
    NameError at the entry point, and under pythonw.exe, where stderr goes
    nowhere, it failed **silently**.

    The lesson is not "add a test for this line". It is that a wiring function
    with no branches worth testing still has to be *executed* by something.
    """

    def drive(self, argv, elevated=True, exists=False):
        seen = {}

        def fake_run(config_path=None, existing=None):
            seen["config"] = config_path
            seen["existing"] = existing
            return 0

        with mock.patch.object(gui, "IS_WINDOWS", True), \
             mock.patch.object(gui, "is_elevated", return_value=elevated), \
             mock.patch.object(gui.os.path, "exists", return_value=exists), \
             mock.patch.object(setup, "load_existing_config",
                               return_value={"cameras": []}), \
             mock.patch.object(gui, "run", side_effect=fake_run):
            code = gui.main(argv)
        return code, seen

    def test_it_reaches_the_window_with_the_default_config_path(self):
        code, seen = self.drive([])
        self.assertEqual(code, 0)
        self.assertEqual(seen["config"], gui.CONFIG_PATH)

    def test_a_config_path_can_be_passed_positionally(self):
        # What the dispatcher hands every script that is not timelapse_setup.
        _code, seen = self.drive(["C:\\somewhere\\config.json"])
        self.assertEqual(seen["config"], "C:\\somewhere\\config.json")

    def test_flags_are_not_mistaken_for_the_config_path(self):
        _code, seen = self.drive(["--whatever"])
        self.assertEqual(seen["config"], gui.CONFIG_PATH)

    def test_an_existing_config_is_loaded_rather_than_defaulted(self):
        """Reconfiguring must open on the saved answers.

        Opening on defaults would look like it worked and quietly propose
        replacing every setting the operator already has.
        """
        _code, seen = self.drive([], exists=True)
        self.assertEqual(seen["existing"], {"cameras": []})

    def test_a_fresh_install_starts_from_defaults(self):
        _code, seen = self.drive([], exists=False)
        self.assertIsNone(seen["existing"])

    def test_it_refuses_before_asking_anything_when_not_elevated(self):
        """Thirty answers and then "you cannot write that" is the worst moment

        to find out, so the check comes before the window. warn_not_elevated is
        patched rather than allowed to run: unpatched it opens a real modal
        dialog and waits for a click, which hangs the suite rather than
        failing it.
        """
        told = []
        with mock.patch.object(gui, "IS_WINDOWS", True), \
             mock.patch.object(gui, "is_elevated", return_value=False), \
             mock.patch.object(gui.os.path, "exists", return_value=False), \
             mock.patch.object(gui, "warn_not_elevated", told.append), \
             mock.patch.object(gui, "run",
                               side_effect=AssertionError("opened anyway")):
            code = gui.main([])
        self.assertEqual(code, 1)
        self.assertEqual(told, [gui.CONFIG_PATH],
                         "the message must name the file it cannot write")

    def test_the_version_flag_answers_without_a_display(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(gui.main(["--version"]), 0)
        self.assertIn(gui.__version__, buf.getvalue())


class TestItRefusesOffWindows(unittest.TestCase):

    def test_it_names_the_command_to_use_instead(self):
        import io
        import contextlib
        buf = io.StringIO()
        with mock.patch.object(gui, "IS_WINDOWS", False), \
             contextlib.redirect_stdout(buf):
            code = gui.main([])
        self.assertEqual(code, 2)
        self.assertIn("timelapse setup", buf.getvalue())


class TestNoEmDashes(unittest.TestCase):
    """A standing order, and user-facing strings are where it shows."""

    def test_the_source_has_none(self):
        text = SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("\u2014", text)
        self.assertNotIn("&mdash;", text)


if __name__ == "__main__":
    unittest.main()

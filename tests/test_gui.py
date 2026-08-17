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


class TestNotifySinks(unittest.TestCase):
    """The field names are the schema's, and getting them wrong is silent.

    The first cut stored a single "url" for all three services, which the
    encoder reads as an empty sink: it would have written a config that looked
    configured and notified nobody.
    """

    def test_each_service_writes_the_keys_the_encoder_reads(self):
        expected = {"discord": {"webhook_url"},
                    "ntfy": {"server", "topic", "token"},
                    "telegram": {"token", "chat_id"}}
        for kind, keys in expected.items():
            _level, _m, sink = gui.build_sink(
                kind, {k: "x" for k in keys}, enabled=False)
            self.assertTrue(keys <= set(sink), "%s: %s" % (kind, sorted(sink)))
            self.assertEqual(sink["type"], kind)

    def test_a_discord_webhook(self):
        url = "https://discord.com/api/webhooks/1/abc"
        level, _m, sink = gui.build_sink("discord", {"webhook_url": url})
        self.assertEqual(level, gui.OK)
        self.assertEqual(sink["webhook_url"], url)
        # The encoder posts under a name; absent, Discord shows the app's.
        self.assertTrue(sink.get("username"))

    def test_discord_without_https(self):
        self.assertEqual(
            gui.build_sink("discord", {"webhook_url": "abc"})[0], gui.FAIL)

    def test_ntfy_defaults_to_the_public_server_and_warns_about_it(self):
        level, message, sink = gui.build_sink("ntfy", {"topic": "mytopic"})
        self.assertEqual(sink["server"], "https://ntfy.sh")
        self.assertEqual(level, gui.WARN)
        self.assertIn("only secret", message)

    def test_ntfy_needs_a_topic(self):
        level, message, _s = gui.build_sink("ntfy", {"server": "https://n.sh"})
        self.assertEqual(level, gui.FAIL)
        self.assertIn("Topic", message)

    def test_telegram_needs_both_halves(self):
        """A token with no chat id is the one that used to send people to the

        command line, from a window built so they would not need one.
        """
        level, message, _s = gui.build_sink("telegram", {"token": "1:ab"})
        self.assertEqual(level, gui.FAIL)
        self.assertIn("Chat id", message)
        self.assertEqual(
            gui.build_sink("telegram", {"token": "1:ab", "chat_id": "7"})[0],
            gui.OK)

    def test_a_bot_token_has_a_colon_in_it(self):
        self.assertEqual(
            gui.build_sink("telegram", {"token": "abc", "chat_id": "7"})[0],
            gui.FAIL)

    def test_turning_one_off_is_recorded_rather_than_refused(self):
        # Written even when empty, so "off" is a state in the config rather
        # than a sink that merely looks configured.
        level, _m, sink = gui.build_sink("discord", {"webhook_url": ""},
                                         enabled=False)
        self.assertEqual(level, gui.OK)
        self.assertFalse(sink["enabled"])

    def test_the_starting_values_come_from_the_existing_config(self):
        cfg = {"notify": [{"type": "ntfy", "enabled": True,
                           "server": "https://n.example", "topic": "t"}]}
        values, on = gui.sink_values(cfg, "ntfy")
        self.assertTrue(on)
        self.assertEqual(values["server"], "https://n.example")
        self.assertEqual(values["topic"], "t")

    def test_an_absent_sink_starts_from_its_default(self):
        values, on = gui.sink_values({}, "ntfy")
        self.assertFalse(on)
        self.assertEqual(values["server"], "https://ntfy.sh")

    def test_every_declared_field_has_a_label_and_a_secret_flag(self):
        for kind, (title, blurb, fields) in gui.NOTIFY_FIELDS.items():
            self.assertTrue(title and blurb, kind)
            for key, label, secret, default in fields:
                self.assertTrue(key and label, kind)
                self.assertIn(secret, (True, False), kind)
                self.assertIsInstance(default, str)

    def test_secrets_are_marked_so_the_window_masks_them(self):
        marked = {(kind, key)
                  for kind, (_t, _b, fields) in gui.NOTIFY_FIELDS.items()
                  for key, _l, secret, _d in fields if secret}
        self.assertIn(("telegram", "token"), marked)
        self.assertIn(("ntfy", "token"), marked)


class TestBrowseStart(unittest.TestCase):
    """Where Browse opens, which the first version got wrong.

    It ignored what was already in the box, so the operator had to navigate
    back to the folder the field was showing them.
    """

    def test_an_existing_folder_is_used_as_is(self):
        self.assertEqual(gui.browse_start("D:\\data", isdir=lambda p: True),
                         "D:\\data")

    def test_a_file_opens_its_folder(self):
        """Built with os.path.join rather than written as a Windows literal.

        browse_start asks a question about the *running* machine's filesystem,
        so os.path is the right module for it to use, which means os.path is
        also what the test has to build with: a "C:\\ff\\bin" literal has no
        directory separator at all under posixpath, and this failed on the
        three Linux legs while passing here.
        """
        import os
        folder = os.path.join("ff", "bin")
        target = os.path.join(folder, "ffmpeg" + (".exe" if os.name == "nt"
                                                  else ""))

        def isdir(path):
            return path == folder

        self.assertEqual(gui.browse_start(target, isdir), folder)

    def test_nothing_usable_falls_back_to_the_dialog_default(self):
        self.assertEqual(gui.browse_start("", isdir=lambda p: False), "")
        self.assertEqual(gui.browse_start(None, isdir=lambda p: False), "")
        self.assertEqual(gui.browse_start("Q:\\gone", isdir=lambda p: False),
                         "")

    def test_quotes_pasted_from_explorer_are_stripped(self):
        self.assertEqual(gui.browse_start('"D:\\data"', isdir=lambda p: True),
                         "D:\\data")

    def test_a_directory_it_may_not_read_does_not_raise(self):
        # is_dir() raises on Windows for an unreadable directory, where on
        # Linux it answers False. A Browse button must not throw.
        def boom(_path):
            raise PermissionError(5, "Access is denied")
        self.assertEqual(gui.browse_start("D:\\locked", isdir=boom), "")


class TestCameraBuilding(unittest.TestCase):
    """The dialog's real output, credentials included.

    The first cut offered a name, a radio button and a URL, so a camera
    needing a password could not be configured in the window at all.
    """

    def preset(self, label):
        for entry in gui.camera_types():
            if entry[0] == label:
                return entry
        raise AssertionError("no preset called %s" % label)

    def test_the_preset_list_is_the_wizards_own(self):
        # Read, not restated: it is the answer to "what is a Reolink URL", and
        # a second copy would become a second answer.
        self.assertEqual(gui.camera_types(), list(setup.CAMERA_PRESETS))

    def test_a_preset_builds_its_url_from_an_address(self):
        level, _m, cam = gui.build_camera(
            {"name": "Yard", "preset": self.preset("Dahua / Amcrest"),
             "address": "192.168.1.9", "username": "admin",
             "password": "secret"}, [])
        self.assertEqual(level, gui.OK)
        self.assertIn("192.168.1.9", cam["url"])
        self.assertEqual(cam["method"], "http")
        # Digest, so the credentials are their own fields rather than the URL.
        self.assertEqual(cam["auth"], "digest")
        self.assertEqual(cam["username"], "admin")
        self.assertEqual(cam["password"], "secret")
        self.assertNotIn("secret", cam["url"])

    def test_a_reolink_carries_the_password_in_the_url(self):
        """Which is a property of that make, not a choice made here.

        Worth a test because it is the case where an empty password field does
        not mean no password is stored.
        """
        preset = self.preset("Reolink")
        self.assertTrue(gui.credentials_go_in_the_url(preset))
        _level, _m, cam = gui.build_camera(
            {"name": "Drive", "preset": preset, "address": "10.0.0.4",
             "username": "admin", "password": "hunter2"}, [])
        self.assertIn("hunter2", cam["url"])
        self.assertNotIn("password", set(cam) - {"url"})

    def test_an_ipv6_address_is_bracketed(self):
        _level, _m, cam = gui.build_camera(
            {"name": "V6", "preset": self.preset("Axis"),
             "address": "2001:db8::1"}, [])
        self.assertIn("[2001:db8::1]", cam["url"])

    def test_rtsp_credentials_go_into_the_stream_url(self):
        preset = self.preset("RTSP only (no snapshot URL)")
        _level, _m, cam = gui.build_camera(
            {"name": "Tapo", "preset": preset, "address": "10.0.0.7",
             "username": "u", "password": "p"}, [])
        self.assertEqual(cam["method"], "rtsp")
        self.assertTrue(cam["url"].startswith("rtsp://"))
        self.assertNotIn("auth", cam)
        self.assertEqual(cam.get("quality"), 2)

    def test_a_custom_camera_carries_the_url_it_was_given(self):
        level, _m, cam = gui.build_camera(
            {"name": "Odd", "preset": self.preset("Custom URL"),
             "url": "http://host/snap.jpg", "auth": "basic",
             "username": "u", "password": "p", "method": "http"}, [])
        self.assertEqual(level, gui.OK)
        self.assertEqual(cam["url"], "http://host/snap.jpg")
        self.assertEqual(cam["auth"], "basic")

    def test_auth_none_does_not_leave_a_stale_username_behind(self):
        """A username stored under auth "none" reads as a credential in use.

        It is not, and leaving it is how a config comes to describe something
        that is not happening.
        """
        existing = {"name": "Old", "auth": "digest", "username": "admin",
                    "password": "p", "url": "http://x/y", "method": "http"}
        _level, _m, cam = gui.build_camera(
            {"name": "Old", "preset": self.preset("Custom URL"),
             "url": "http://x/y", "auth": "none", "method": "http"},
            [existing], cam=existing)
        self.assertNotIn("username", cam)
        self.assertNotIn("password", cam)

    def test_a_preset_with_no_address_is_refused(self):
        level, message, cam = gui.build_camera(
            {"name": "Nope", "preset": self.preset("Axis"), "address": ""}, [])
        self.assertEqual(level, gui.FAIL)
        self.assertIsNone(cam)
        self.assertIn("IP address", message)

    def test_a_bad_name_is_refused_before_anything_else(self):
        level, message, cam = gui.build_camera(
            {"name": "NUL", "preset": self.preset("Axis"),
             "address": "10.0.0.1"}, [])
        self.assertEqual(level, gui.FAIL)
        self.assertIsNone(cam)
        self.assertIn("reserves", message)

    def test_editing_keeps_the_keys_it_was_not_asked_about(self):
        existing = {"name": "Roof", "url": "http://x", "method": "http",
                    "interval_seconds": 10, "enabled": True}
        _level, _m, cam = gui.build_camera(
            {"name": "Roof", "preset": self.preset("Custom URL"),
             "url": "http://y", "auth": "none", "method": "http",
             "enabled": True}, [existing], cam=existing)
        self.assertEqual(cam["interval_seconds"], 10,
                         "a per-camera cadence must survive an edit here")

    def test_disabling_is_carried_through(self):
        _level, _m, cam = gui.build_camera(
            {"name": "Off", "preset": self.preset("Axis"),
             "address": "10.0.0.1", "enabled": False}, [])
        self.assertFalse(cam["enabled"])

    def test_which_types_ask_for_credentials(self):
        wants = {p[0] for p in gui.camera_types()
                 if gui.preset_wants_credentials(p)}
        self.assertIn("Reolink", wants)
        self.assertIn("Dahua / Amcrest", wants)
        self.assertIn("RTSP only (no snapshot URL)", wants)

    def test_smoothing_is_optional_and_bounded(self):
        from timelapse_encode import SMOOTH_MAX, SMOOTH_MIN
        self.assertEqual(gui.check_smoothing("")[2], None)
        self.assertEqual(gui.check_smoothing(str(SMOOTH_MIN))[2], SMOOTH_MIN)
        self.assertEqual(gui.check_smoothing(str(SMOOTH_MAX - 1))[0], gui.OK)
        self.assertEqual(gui.check_smoothing("2")[0], gui.FAIL)
        self.assertEqual(gui.check_smoothing("999")[0], gui.FAIL)
        self.assertEqual(gui.check_smoothing("lots")[0], gui.FAIL)

    def test_smoothing_is_removed_when_cleared(self):
        existing = {"name": "R", "url": "http://x", "method": "http",
                    "smooth_frames": 15}
        _level, _m, cam = gui.build_camera(
            {"name": "R", "preset": self.preset("Custom URL"),
             "url": "http://x", "auth": "none", "method": "http",
             "smoothing": None}, [existing], cam=existing)
        self.assertNotIn("smooth_frames", cam)


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

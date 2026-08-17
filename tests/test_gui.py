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

    def test_no_field_label_is_wider_than_the_column_it_sits_in(self):
        """Truncation is invisible to every other test in this file.

        A ttk.Label with a fixed width in characters simply cuts what does not
        fit, silently, so "Discord webhook URL" arrived as "Discord webhook
        UR..." and "Seconds between frames" as "Seconds between fram". Both
        were reported by the operator from a real run, which is an expensive
        way to measure a string length. The labels are literals in the source,
        so read them from the source.
        """
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        checked = 0
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "field"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "self"):
                continue
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                continue
            label = node.args[1].value
            width = gui.LABELS
            for word in node.keywords:
                if word.arg == "label_width" and \
                        isinstance(word.value, ast.Constant):
                    width = word.value.value
                elif word.arg == "label_width" and \
                        isinstance(word.value, ast.Name):
                    width = getattr(gui, word.value.id, width)
            self.assertLessEqual(
                len(label), width,
                "%r is %d characters in a %d character label, so it will be "
                "cut off on screen" % (label, len(label), width))
            checked += 1
        self.assertGreater(checked, 5, "the labels were not found at all")

        # The notification page passes its labels from a table rather than as
        # literals, so the source scan above cannot see them, and the table is
        # where the defect that started this actually was.
        for kind, (_title, _blurb, fields) in gui.NOTIFY_FIELDS.items():
            for _key, label, _secret, _default in fields:
                self.assertLessEqual(len(label), gui.LABELS,
                                     "%s: %r will be cut off" % (kind, label))

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

    def test_the_network_picker_offers_the_letter_and_the_target(self):
        """The letter is what the operator recognises; the UNC is what is kept.

        The picker exists because the folder browser is shown by an elevated
        process, which has its own logon session and therefore none of the
        operator's drive mappings in it: they browse for the share they use
        daily and it is not in the list.
        """
        rows = gui.network_choices([("U:", "\\\\tower\\cctv"),
                                    ("Z:", "\\\\tower\\media")])
        self.assertEqual([unc for _label, unc in rows],
                         ["\\\\tower\\cctv", "\\\\tower\\media"])
        self.assertIn("U:", rows[0][0])
        self.assertIn("\\\\tower\\cctv", rows[0][0])

    def test_nothing_mapped_offers_nothing(self):
        self.assertEqual(gui.network_choices([]), [])
        # And says what to do instead, rather than an empty box with no reason.
        self.assertIn("\\\\server\\share", gui.no_network_advice())

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
        # timeout_seconds is not on the page and never was, so an edit here
        # has no business touching it.
        existing = {"name": "Roof", "url": "http://x", "method": "http",
                    "timeout_seconds": 9, "enabled": True}
        _level, _m, cam = gui.build_camera(
            {"name": "Roof", "preset": self.preset("Custom URL"),
             "url": "http://y", "auth": "none", "method": "http",
             "enabled": True}, [existing], cam=existing)
        self.assertEqual(cam["timeout_seconds"], 9)

    def test_a_per_camera_cadence_is_carried_by_the_page(self):
        """It is asked about now, so the page carries it rather than luck.

        Until the pane offered cadence and frame rate, build_camera() left
        both alone and a per-camera value survived an edit because nothing
        here knew it was there. That is not a property to rely on once the
        page has a box for it: what it saves has to be what the box holds.
        """
        existing = {"name": "Roof", "url": "http://x", "method": "http",
                    "interval_seconds": 10, "framerate": 30}
        _level, _m, cam = gui.build_camera(
            {"name": "Roof", "preset": self.preset("Custom URL"),
             "url": "http://x", "auth": "none", "method": "http",
             "interval_seconds": 10, "framerate": 30},
            [existing], cam=existing)
        self.assertEqual(cam["interval_seconds"], 10)
        self.assertEqual(cam["framerate"], 30)

    def test_an_override_is_removed_rather_than_stored_as_a_copy(self):
        """The whole reason per-camera settings are keyed on ABSENCE.

        A camera carrying no key follows the global, which is what lets a
        later change to the global still move it. Writing a value that merely
        equals today's global would pin every camera anybody had opened.
        """
        existing = {"name": "Roof", "url": "http://x", "method": "http",
                    "interval_seconds": 10, "framerate": 30}
        _level, _m, cam = gui.build_camera(
            {"name": "Roof", "preset": self.preset("Custom URL"),
             "url": "http://x", "auth": "none", "method": "http",
             "interval_seconds": None, "framerate": None},
            [existing], cam=existing)
        self.assertNotIn("interval_seconds", cam)
        self.assertNotIn("framerate", cam)

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


class TestCameraPane(unittest.TestCase):
    """The list on the left and the detail pane on the right.

    The pane replaced a modal dialog, which is a change of shape rather than
    of rules: what these check is the three things the pane has to get right
    on its own, which are what it shows in the list, what it fills the fields
    with, and whether it has been edited since.

    `preset()` is the same helper TestCameraBuilding uses; a camera has to be
    built before it can be identified again, and building it here rather than
    hand-writing a URL is the point of most of these.
    """

    def preset(self, label):
        for entry in gui.camera_types():
            if entry[0] == label:
                return entry
        raise AssertionError("no preset called %s" % label)

    def test_the_list_shows_the_name(self):
        self.assertEqual(gui.camera_label({"name": "Roof", "enabled": True}),
                         "Roof")

    def test_a_disabled_camera_says_so(self):
        """Disabling is as destructive as removing, so it cannot look the same.

        The encoder builds its work list from cameras enabled in the config,
        so a disabled one's frames are stranded on disk exactly as a renamed
        one's are.
        """
        self.assertIn("disabled",
                      gui.camera_label({"name": "Yard", "enabled": False}))

    def test_an_entry_add_made_and_nobody_filled_in(self):
        self.assertEqual(gui.camera_label({}), "(new camera)")
        self.assertEqual(gui.camera_label(None), "(new camera)")

    def test_a_url_nothing_built_opens_on_custom(self):
        """Which is the honest answer, not a fallback.

        Nothing in the list produces this URL, so no make can be claimed for
        it, and Custom carries it through untouched.
        """
        values = gui.camera_form_values({"name": "Roof", "method": "http",
                                         "url": "http://10.0.0.1/snap"})
        self.assertEqual(values["type"], gui.camera_types()[-1][0])
        self.assertEqual(values["url"], "http://10.0.0.1/snap")

    def test_a_camera_opens_on_the_make_it_was_added_as(self):
        """Reported as an inconsistency: a Dahua came back as Custom URL.

        The config records the URL, not the make, so the make has to be worked
        out again. Accepted only when rebuilding reproduces the URL exactly,
        which makes claiming a make a claim that Save changes nothing.
        """
        _l, _m, built = gui.build_camera(
            {"name": "Street4K", "preset": self.preset("Dahua / Amcrest"),
             "address": "192.168.1.9", "username": "admin",
             "password": "secret"}, [])
        values = gui.camera_form_values(built)
        self.assertEqual(values["type"], "Dahua / Amcrest")
        self.assertEqual(values["address"], "192.168.1.9")
        # Digest, so these live in their own fields and not in the URL. Taking
        # the extracted ones blindly would blank both.
        self.assertEqual(values["username"], "admin")
        self.assertEqual(values["password"], "secret")

    def test_a_make_that_keeps_its_credentials_in_the_url_round_trips(self):
        _l, _m, built = gui.build_camera(
            {"name": "Drive", "preset": self.preset("Reolink"),
             "address": "10.0.0.4", "username": "admin",
             "password": "hunt&r 2"}, [])
        values = gui.camera_form_values(built)
        self.assertEqual(values["type"], "Reolink")
        self.assertEqual(values["address"], "10.0.0.4")
        # Through quote() on the way in and back out again, ampersand and
        # space included, which is the case that would silently corrupt.
        self.assertEqual(values["password"], "hunt&r 2")

    def test_every_make_survives_the_round_trip(self):
        """Each preset in the list, built and then identified again.

        A new preset whose template this cannot reverse would quietly fall
        back to Custom for every camera using it.
        """
        for label, _method, _auth, template in gui.camera_types():
            if template is None:
                continue
            _l, _m, built = gui.build_camera(
                {"name": "C", "preset": self.preset(label),
                 "address": "10.0.0.8", "username": "u", "password": "p"}, [])
            found = gui.identify_camera(built)
            self.assertIsNotNone(found, label)
            self.assertEqual(found[1], "10.0.0.8", label)

    def test_an_ipv6_camera_comes_back_as_it_was_typed(self):
        _l, _m, built = gui.build_camera(
            {"name": "V6", "preset": self.preset("Axis"),
             "address": "2001:db8::1"}, [])
        values = gui.camera_form_values(built)
        # Bracketed inside the URL, bare in the box that produced it.
        self.assertEqual(values["address"], "2001:db8::1")

    def test_nothing_is_claimed_for_a_url_with_no_make(self):
        self.assertIsNone(gui.identify_camera({"url": "http://x/y.jpg"}))
        self.assertIsNone(gui.identify_camera({}))
        self.assertIsNone(gui.identify_camera(None))

    def test_a_hand_edited_template_url_is_not_claimed(self):
        """The check is the round trip, not the shape.

        A URL that looks like a Dahua but is not what the template produces
        must not be claimed, because saving would rewrite it.
        """
        self.assertIsNone(gui.identify_camera(
            {"url": "http://10.0.0.1/cgi-bin/snapshot.cgi?channel=2&subtype=0"}))

    def test_a_new_camera_opens_on_a_make_where_an_address_is_enough(self):
        values = gui.camera_form_values({})
        self.assertEqual(values["type"], gui.camera_types()[0][0])
        self.assertEqual(values["url"], "")
        self.assertTrue(values["enabled"])

    def test_the_credentials_are_there_to_be_changed(self):
        # The whole reason the pane exists: the first cut showed a name, a
        # type and an address, so a stored password could not be corrected.
        values = gui.camera_form_values({"name": "R", "url": "http://x",
                                         "auth": "basic", "username": "admin",
                                         "password": "hunter2"})
        self.assertEqual(values["username"], "admin")
        self.assertEqual(values["password"], "hunter2")
        self.assertEqual(values["auth"], "basic")

    def test_smoothing_arrives_as_a_switch_and_a_count(self):
        from timelapse_encode import SMOOTH_DEFAULT

        on = gui.camera_form_values({"name": "R", "url": "http://x",
                                     "smooth_frames": 9})
        self.assertTrue(on["smoothing_on"])
        self.assertEqual(on["smoothing"], "9")

        off = gui.camera_form_values({"name": "R", "url": "http://x"})
        self.assertFalse(off["smoothing_on"])
        # The box still offers a number, so switching it on needs one click
        # rather than a click and a guess at what a sensible value is.
        self.assertEqual(off["smoothing"], str(SMOOTH_DEFAULT))

    def test_a_camera_with_no_override_offers_empty_boxes(self):
        """Empty is the only rendering of "follows the global" that is safe.

        A box prefilled with the global would be saved as a copy, pinning
        every camera anybody had merely opened to today's setting.
        """
        values = gui.camera_form_values({"name": "R", "url": "http://x"})
        self.assertEqual(values["interval"], "")
        self.assertEqual(values["framerate"], "")

    def test_a_camera_with_overrides_shows_them(self):
        values = gui.camera_form_values({"name": "R", "url": "http://x",
                                         "interval_seconds": 10,
                                         "framerate": 30})
        self.assertEqual(values["interval"], "10")
        self.assertEqual(values["framerate"], "30")

    def test_blank_means_follow_the_global(self):
        for level, message, value in (gui.check_camera_interval("", 5),
                                      gui.check_camera_framerate("", 60)):
            self.assertEqual(level, gui.OK)
            self.assertIsNone(value)
            self.assertIn("global", message)

    def test_the_same_as_the_global_is_not_stored(self):
        # Answering with the global value has to mean "follow it", or a later
        # change to the global stops reaching this camera.
        self.assertIsNone(gui.check_camera_interval("5", 5)[2])
        self.assertIsNone(gui.check_camera_framerate("60", 60)[2])

    def test_a_real_override_is_kept(self):
        self.assertEqual(gui.check_camera_interval("10", 5)[2], 10)
        self.assertEqual(gui.check_camera_framerate("30", 60)[2], 30)

    def test_an_override_is_checked_as_hard_as_the_global(self):
        # 900s a frame is under min_frames for a whole day, which produces no
        # video at all, silently and for ever.
        level, message, value = gui.check_camera_interval("900", 5)
        self.assertEqual(level, gui.FAIL)
        self.assertIsNone(value)
        self.assertIn("skipped", message)
        self.assertEqual(gui.check_camera_framerate("0", 60)[0], gui.FAIL)
        self.assertEqual(gui.check_camera_interval("often", 5)[0], gui.FAIL)

    def test_an_untouched_pane_is_not_dirty(self):
        values = gui.camera_form_values({"name": "R", "url": "http://x"})
        self.assertFalse(gui.form_is_dirty(values, dict(values)))

    def test_a_typed_password_makes_it_dirty(self):
        values = gui.camera_form_values({"name": "R", "url": "http://x"})
        current = dict(values, password="new")
        self.assertTrue(gui.form_is_dirty(values, current))

    def test_a_key_the_pane_does_not_carry_is_not_a_change(self):
        values = gui.camera_form_values({"name": "R", "url": "http://x"})
        self.assertFalse(gui.form_is_dirty(values, dict(values, extra="x")))

    def test_the_enable_switch_counts_as_a_change(self):
        values = gui.camera_form_values({"name": "R", "url": "http://x"})
        self.assertTrue(gui.form_is_dirty(values, dict(values, enabled=False)))

    def test_a_type_is_looked_up_by_its_label(self):
        for preset in gui.camera_types():
            self.assertIs(gui.preset_named(preset[0]), preset)

    def test_an_unknown_type_falls_back_to_custom(self):
        # Which is the safe direction: Custom carries the URL through
        # untouched, where a wrong make would rebuild it from a template.
        self.assertTrue(gui.preset_is_custom(gui.preset_named("Nonesuch")))


class TestCameraDiscovery(unittest.TestCase):
    """The Scan network button: what is offered, and what a tick becomes.

    The probe itself is timelapse_setup's and is tested there against real
    response bodies. Everything here is the decision layer the window sits on:
    which answers are offered at all, which are already configured, what a
    ticked row is called, and what config entry it turns into.
    """

    def device(self, address, **over):
        dev = {"address": address, "camera": True, "name": "", "hardware": "",
               "xaddrs": [], "sources": [address], "scopes": [], "types": ""}
        dev.update(over)
        return dev

    # -- what gets offered ---------------------------------------------

    def test_only_video_transmitters_are_offered(self):
        """An NVR, a printer and every Windows PC answer this same probe.

        Offering one as a camera would be offering a snapshot URL that cannot
        work, which is the console wizard's rule read from the same field.
        """
        rows = gui.scan_rows([self.device("192.0.2.5"),
                              self.device("192.0.2.6", camera=False)], [])
        self.assertEqual([r["address"] for r in rows], ["192.0.2.5"])

    def test_the_others_are_counted_rather_than_hidden(self):
        summary = gui.scan_summary([self.device("192.0.2.5"),
                                    self.device("192.0.2.6", camera=False)],
                                   [{"address": "192.0.2.5"}])
        self.assertIn("1 camera answered", summary)
        self.assertIn("1 other device", summary)

    def test_one_camera_alone_is_not_pluralised(self):
        rows = [{"address": "192.0.2.5"}]
        self.assertNotIn("cameras", gui.scan_summary([], rows))

    def test_a_device_with_no_address_is_not_a_row(self):
        # wsd_address() answers "" when a device advertised nothing usable and
        # replied from nowhere we recorded; a row with no address offers a URL
        # with a hole in it.
        self.assertEqual(gui.scan_rows([self.device("")], []), [])

    def test_the_model_identifies_the_row_not_the_onvif_name(self):
        """Three Dahuas here all call themselves Dahua; the models differ."""
        row = gui.scan_rows([self.device("192.0.2.5", name="Dahua",
                                         hardware="IPC-HDW2431T")], [])[0]
        self.assertEqual(row["model"], "IPC-HDW2431T")

    def test_a_device_naming_neither_still_shows_something(self):
        self.assertEqual(gui.scan_rows([self.device("192.0.2.5")],
                                       [])[0]["model"], "(unnamed)")

    # -- already added --------------------------------------------------

    def test_a_configured_camera_is_marked(self):
        cams = [{"name": "Roof",
                 "url": "http://192.0.2.5/cgi-bin/snapshot.cgi"}]
        rows = gui.scan_rows([self.device("192.0.2.5"),
                              self.device("192.0.2.9")], cams)
        self.assertEqual([r["added"] for r in rows], [True, False])

    def test_the_address_comes_out_of_the_url(self):
        """Because there is nowhere else it could come from.

        A config stores a URL; an address is only ever what the wizard asked
        for on the way to building one.
        """
        self.assertEqual(
            gui.camera_address({"url": "rtsp://u:p@192.0.2.7:554/stream1"}),
            "192.0.2.7")

    def test_a_custom_url_camera_is_matched_too(self):
        # identify_camera() cannot name this one's make, and it does not have
        # to: the address is what says it is already configured.
        rows = gui.scan_rows(
            [self.device("10.1.2.3")],
            [{"name": "Odd", "url": "http://10.1.2.3/some/vendor/path?x=1"}])
        self.assertTrue(rows[0]["added"])

    def test_an_ipv6_camera_is_matched_without_its_brackets(self):
        rows = gui.scan_rows(
            [self.device("2001:db8::5")],
            [{"name": "Six", "url": "http://[2001:db8::5]/axis-cgi/jpg/"
                                    "image.cgi"}])
        self.assertTrue(rows[0]["added"])

    def test_a_url_that_does_not_parse_is_not_an_address(self):
        """One real Dahua advertises http://[]/onvif/device_service.

        urlparse raises ValueError on that from 3.12, and a traceback out of
        the scan would read as the scan being broken.
        """
        self.assertEqual(gui.camera_address({"url": "http://[]/onvif"}), "")
        self.assertEqual(gui.configured_addresses([{"url": ""}, {}]), set())

    def test_case_does_not_decide_whether_it_is_already_added(self):
        rows = gui.scan_rows([self.device("CAM-ROOF")],
                             [{"url": "http://cam-roof/axis-cgi/jpg/"
                                      "image.cgi"}])
        self.assertTrue(rows[0]["added"])

    # -- the make -------------------------------------------------------

    def test_a_named_vendor_preselects_its_make(self):
        row = gui.scan_rows([self.device("192.0.2.5",
                                         hardware="Amcrest IP2M-841")], [])[0]
        self.assertEqual(row["type"], "Dahua / Amcrest")

    def test_a_device_naming_no_vendor_preselects_nothing(self):
        """The Reolink here calls itself IPC-BO and the Tapo calls itself TC40.

        Empty is what makes the window ask. A preselection that is wrong is a
        wrong URL that looks deliberate, which is worse than no answer.
        """
        row = gui.scan_rows([self.device("192.0.2.5", hardware="TC40")],
                            [])[0]
        self.assertEqual(row["type"], "")

    def test_custom_url_is_not_a_choice_for_a_scanned_camera(self):
        """It builds no URL from an address, so it could only produce an empty
        one. The detail pane is where a camera becomes a custom one."""
        choices = gui.scan_type_choices()
        self.assertNotIn("Custom URL", choices)
        for label in choices:
            self.assertIsNotNone(gui.preset_named(label)[3],
                                 "%s builds no URL from an address" % label)

    def test_every_vendor_hint_names_a_type_the_window_offers(self):
        # VENDOR_HINTS is keyed on preset labels precisely so this can be
        # asserted rather than assumed; a hint naming a label the picker does
        # not list would preselect a value the Combobox cannot show.
        for _needle, label in setup.VENDOR_HINTS:
            self.assertIn(label, gui.scan_type_choices())

    # -- naming ---------------------------------------------------------

    def test_new_cameras_are_numbered(self):
        self.assertEqual(gui.next_camera_names([], 3),
                         ["Camera1", "Camera2", "Camera3"])

    def test_a_number_already_in_the_config_is_skipped(self):
        self.assertEqual(gui.next_camera_names([{"name": "Camera1"},
                                                {"name": "Camera3"}], 2),
                         ["Camera2", "Camera4"])

    def test_a_batch_does_not_collide_with_itself(self):
        names = gui.next_camera_names([], 5)
        self.assertEqual(len(set(names)), 5)

    def test_naming_uses_the_wizards_own_rule(self):
        """Patching setup.name_taken has to move the answer.

        Which it can only do if there is no second opinion about what a taken
        name is here. Case-insensitivity is that rule, and it lives in one
        place.
        """
        with mock.patch.object(setup, "name_taken",
                               side_effect=lambda cams, name, skip=None:
                               name == "Camera1"):
            self.assertEqual(gui.next_camera_names([], 1), ["Camera2"])

    def test_a_camera_differing_only_in_case_is_taken(self):
        self.assertEqual(gui.next_camera_names([{"name": "camera1"}], 1),
                         ["Camera2"])

    def test_asking_for_none_gets_none(self):
        self.assertEqual(gui.next_camera_names([{"name": "Camera1"}], 0), [])

    # -- what a tick builds ---------------------------------------------

    def test_a_ticked_row_becomes_the_same_shape_as_a_typed_one(self):
        row = {"address": "192.0.2.5", "model": "IP2M-841",
               "type": "Dahua / Amcrest", "added": False}
        level, _message, cam = gui.build_scanned(row, "Camera1", [])
        self.assertEqual(level, gui.OK)
        self.assertEqual(cam["name"], "Camera1")
        self.assertEqual(cam["method"], "http")
        self.assertEqual(cam["auth"], "digest")
        self.assertIn("192.0.2.5", cam["url"])
        self.assertTrue(cam["enabled"])

    def test_it_carries_no_credentials(self):
        """The operator chose per-camera over one shared pair for the scan.

        So a scanned camera arrives with an address and a make, and the
        password is typed in the detail pane. Empty rather than absent for a
        digest camera, which is what build_camera() writes either way.
        """
        _l, _m, cam = gui.build_scanned({"address": "192.0.2.5",
                                         "type": "Dahua / Amcrest"},
                                        "Camera1", [])
        self.assertEqual(cam.get("username"), "")
        self.assertEqual(cam.get("password"), "")

    def test_it_follows_the_global_cadence(self):
        # Keyed on absence, as everywhere else: a scan that wrote today's
        # global into every camera it added would pin the whole fleet.
        _l, _m, cam = gui.build_scanned({"address": "192.0.2.5",
                                         "type": "Reolink"}, "Camera1", [])
        self.assertNotIn("interval_seconds", cam)
        self.assertNotIn("framerate", cam)

    def test_a_scanned_camera_reads_back_as_the_make_it_was_added_as(self):
        """The round trip, for every make the scan can offer.

        Which is what stops the scan being a special case: a camera it added
        opens on its own make the next time the page does, exactly as one
        typed in by hand does.

        Compared on the template rather than on the label, because two of the
        makes offered carry the *same* template (Hikvision ONVIF and Generic
        ONVIF snapshot), so no amount of looking at a URL can tell them apart.
        That is exactly why it does not matter: either answer saves the same
        config, and it is the config this is about.
        """
        for label in gui.scan_type_choices():
            _l, _m, cam = gui.build_scanned({"address": "192.0.2.5",
                                             "type": label}, "Camera1", [])
            values = gui.camera_form_values(cam)
            self.assertEqual(gui.preset_named(values["type"])[3],
                             gui.preset_named(label)[3],
                             "%s did not survive being added" % label)
            self.assertEqual(values["address"], "192.0.2.5")
            # And re-saving it untouched has to be a no-op, which is the whole
            # claim identify_camera() makes.
            _l2, _m2, again = gui.build_camera(
                {"name": cam["name"],
                 "preset": gui.preset_named(values["type"]),
                 "address": values["address"], "username": values["username"],
                 "password": values["password"], "enabled": True}, [], cam)
            self.assertEqual(again["url"], cam["url"])

    def test_a_scanned_camera_is_found_again_by_its_own_address(self):
        """So ticking the same camera twice is not possible.

        Ties the two halves together: build_scanned() writes a URL, and
        scan_rows() has to be able to read the address back out of it.
        """
        cams = []
        for label in gui.scan_type_choices():
            _l, _m, cam = gui.build_scanned({"address": "192.0.2.5",
                                             "type": label},
                                            "Camera%d" % (len(cams) + 1), [])
            self.assertTrue(
                gui.scan_rows([self.device("192.0.2.5")], [cam])[0]["added"],
                "%s was not recognised as already added" % label)

    def test_an_unnamed_row_is_refused_rather_than_written(self):
        level, message, cam = gui.build_scanned(
            {"address": "", "type": "Axis"}, "Camera1", [])
        self.assertEqual(level, gui.FAIL)
        self.assertIsNone(cam)
        self.assertTrue(message)

    def test_nothing_answering_is_not_the_same_as_no_cameras(self):
        # Multicast does not cross subnets or VLANs, and a camera VLAN is
        # common in exactly the deployments with the most cameras.
        advice = gui.nothing_found_advice()
        self.assertIn("VLAN", advice)
        self.assertIn("Add", advice)
        self.assertEqual(gui.scan_summary([], []), "")


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

    def test_the_video_row_names_the_encoder_and_its_quality(self):
        """Which is the difference between a 300 MB day and a 900 MB one.

        The encoder is a property of the machine rather than of the config,
        found by the ffmpeg check on the first page, so it is passed in.
        """
        rows = dict(gui.summary_lines(self.config(), codec="hevc_nvenc"))
        self.assertIn("60 fps", rows["Video"])
        self.assertIn("hevc_nvenc", rows["Video"])
        self.assertIn("cq 24", rows["Video"])

    def test_the_video_row_still_reads_without_one(self):
        # Nothing has probed yet, and a row saying "None" would be worse than
        # a row that simply does not mention it.
        rows = dict(gui.summary_lines(self.config()))
        self.assertIn("60 fps", rows["Video"])
        self.assertNotIn("nvenc", rows["Video"])

    def test_the_quality_comes_from_the_arguments_that_will_run(self):
        """Not restated here, or this panel would describe an older setting.

        Same rule as try_rsync_args(): the description lives beside the code
        it describes, so the two cannot disagree.
        """
        cfg = self.config()
        cfg["encode"].update({"av1_cq": 31, "av1_preset": "p4"})
        detail = gui.encoder_details(cfg, "av1_nvenc")
        self.assertIn("cq 31", detail)
        self.assertIn("preset p4", detail)

    def test_the_cpu_fallback_reports_a_crf(self):
        detail = gui.encoder_details(self.config(), "libx264")
        self.assertIn("libx264", detail)
        self.assertIn("crf 20", detail)

    def test_an_unrecognised_encoder_is_still_named(self):
        # Whatever it is, saying its name beats saying nothing.
        self.assertEqual(gui.encoder_details(self.config(), "vp9_qsv"),
                         "vp9_qsv")
        self.assertEqual(gui.encoder_details(self.config(), None), "")

    def test_the_notification_row_spells_each_service_properly(self):
        cfg = self.config()
        cfg["notify"] = [{"type": "discord", "enabled": True,
                          "webhook_url": "https://x/y"}]
        rows = dict(gui.summary_lines(cfg))
        self.assertEqual(rows["Notifications"], "Discord")

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

"""Unit tests for timelapse_web.py: library resolution and request routing.

No sockets are opened. The handler is exercised through a fake request rather
than a live server: the routing and the escaping are the parts worth pinning,
and binding a port in a unit test invites flakiness on a CI runner.
"""

import contextlib
import io
import json
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import _support  # noqa: F401  (puts scripts/ on sys.path)

import timelapse_web as web


def cfg(tmp, transfer=None, web_section=None, video_output=None):
    """A config shaped like the real one, with only the keys under test."""
    out = {
        "paths": {
            "frames_root": str(Path(tmp) / "frames"),
            "video_output": video_output or str(Path(tmp) / "videos"),
            "log_dir": str(Path(tmp) / "logs"),
        },
        "cameras": [],
    }
    if transfer is not None:
        out["transfer"] = transfer
    if web_section is not None:
        out["web"] = web_section
    return out


class TestRemoteSpec(unittest.TestCase):
    """A colon before the first slash is what separates an rsync remote from a
    path. Getting this wrong either hides a usable library or tries to list a
    hostname as a directory."""

    def test_plain_paths_are_local(self):
        for dest in ("/mnt/nas/timelapse/", "/var/lib/timelapse/videos",
                     "relative/path", "/"):
            self.assertFalse(web.is_remote_spec(dest), dest)

    def test_user_at_host_is_remote(self):
        self.assertTrue(web.is_remote_spec("user@nas:/mnt/user/timelapse/"))

    def test_bare_host_is_remote(self):
        self.assertTrue(web.is_remote_spec("nas:/mnt/user/timelapse/"))
        self.assertTrue(web.is_remote_spec("nas:videos"))

    def test_rsync_url_is_remote(self):
        self.assertTrue(web.is_remote_spec("rsync://nas/timelapse"))

    def test_colon_after_a_slash_is_still_a_path(self):
        # A directory may legitimately contain a colon. Only a colon in the
        # first segment means "host:".
        self.assertFalse(web.is_remote_spec("/mnt/odd:name/timelapse"))


class TestResolveLibrary(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()

    def test_transfer_disabled_falls_back_to_video_output(self):
        got = web.resolve_library(cfg(self.tmp, transfer={"enabled": False}))
        self.assertEqual(got["path"], Path(self.tmp) / "videos")
        self.assertTrue(got["usable"])
        self.assertIn("video_output", got["source"])

    def test_no_transfer_section_at_all(self):
        # An install predating the transfer feature, or a hand-trimmed config.
        got = web.resolve_library(cfg(self.tmp))
        self.assertTrue(got["usable"])

    def test_enabled_transfer_wins_over_video_output(self):
        dest = Path(self.tmp) / "nas"
        dest.mkdir()
        got = web.resolve_library(cfg(self.tmp, transfer={
            "enabled": True, "destination": str(dest)}))
        self.assertEqual(got["path"], dest)
        self.assertTrue(got["usable"])

    def test_this_is_the_whole_point(self):
        """video_output is EMPTY after a successful transfer, because rsync runs
        with --remove-source-files. Resolving to it would show an empty library
        on every correctly configured install."""
        dest = Path(self.tmp) / "nas"
        dest.mkdir()
        got = web.resolve_library(cfg(self.tmp, transfer={
            "enabled": True, "destination": str(dest),
            "delete_local_after_transfer": True}))
        self.assertNotEqual(got["path"], Path(self.tmp) / "videos")

    def test_remote_destination_is_unusable_and_explains_itself(self):
        got = web.resolve_library(cfg(self.tmp, transfer={
            "enabled": True, "destination": "user@nas:/mnt/user/timelapse/"}))
        self.assertFalse(got["usable"])
        self.assertIsNone(got["path"])
        self.assertIn("remote", got["source"])
        self.assertIn("library_root", got["note"])

    def test_missing_directory_is_unusable_and_mentions_mounting(self):
        got = web.resolve_library(cfg(self.tmp, transfer={
            "enabled": True, "destination": str(Path(self.tmp) / "absent")}))
        self.assertFalse(got["usable"])
        self.assertIn("mounted", got["note"])

    def test_override_beats_a_remote_destination(self):
        local = Path(self.tmp) / "local-nas"
        local.mkdir()
        got = web.resolve_library(cfg(
            self.tmp,
            transfer={"enabled": True, "destination": "user@nas:/x/"},
            web_section={"library_root": str(local)}))
        self.assertEqual(got["path"], local)
        self.assertTrue(got["usable"])

    def test_blank_override_is_ignored(self):
        # The wizard writes "" for "work it out yourself", not a missing key.
        got = web.resolve_library(cfg(self.tmp, transfer={"enabled": False},
                                      web_section={"library_root": "   "}))
        self.assertEqual(got["path"], Path(self.tmp) / "videos")

    def test_enabled_transfer_with_blank_destination_falls_back(self):
        got = web.resolve_library(cfg(self.tmp, transfer={
            "enabled": True, "destination": ""}))
        self.assertEqual(got["path"], Path(self.tmp) / "videos")


class FakeRequest:
    """Enough of a socket for BaseHTTPRequestHandler to serve one request.

    sendall, not a writable makefile: StreamRequestHandler sets wbufsize = 0,
    so wfile is a _SocketWriter wrapping the socket directly and makefile() is
    only ever called for the read side.
    """

    def __init__(self, raw):
        self._raw = raw
        self.sent = bytearray()

    def makefile(self, mode="rb", *args, **kwargs):
        return io.BytesIO(self._raw)

    def sendall(self, data):
        self.sent.extend(data)

    def settimeout(self, _timeout):
        pass            # Handler.timeout makes setup() call this.

    def close(self):
        pass


class FakeServer:
    def __init__(self, config, index=None, updates=None, auth=None):
        self.cfg = config
        self.index = index
        # Disabled by default, and that is not incidental: an enabled checker
        # would have the overview page reaching api.github.com from the test
        # suite. Tests that want the panel populated set the state directly.
        self.updates = updates or web.UpdateChecker(None, enabled=False)
        # Also disabled by default, so every test written before the login
        # existed still describes a server with no login.
        self.auth = auth or web.Auth()


def request_bytes(path, config, index=None, method="GET", headers="",
                  updates=None, auth=None, body=None, client="127.0.0.1"):
    """Drive one request through the real handler; body stays bytes.

    Extra headers go first, and Host is only defaulted when they do not supply
    one: a duplicate Host would be silently ignored by the header parser, which
    quietly made an early version of the forged-Host test pass for no reason.
    """
    extra = headers or ""
    host = "" if "host:" in extra.lower() else "Host: nas.local\r\n"
    payload = (body or "").encode("utf-8")
    ctype = ("Content-Type: application/x-www-form-urlencoded\r\n"
             if payload else "")
    raw = (f"{method} {path} HTTP/1.1\r\n{extra}{host}{ctype}"
           f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n")
    req = FakeRequest(raw.encode() + payload)
    handler = web.Handler.__new__(web.Handler)
    handler.log_message = lambda *a, **k: None
    handler.rfile = None
    # BaseHTTPRequestHandler does all its work from __init__.
    web.Handler.__init__(handler, req, (client, 5555),
                         FakeServer(config, index, updates, auth))
    out = bytes(req.sent)
    head, _, body = out.partition(b"\r\n\r\n")
    head = head.decode("latin-1")
    return int(head.split()[1]), head, body


def request(path, config, index=None, method="GET", headers="", updates=None,
            auth=None, body=None, client="127.0.0.1"):
    """As request_bytes, with the body decoded for HTML assertions."""
    status, head, raw = request_bytes(path, config, index, method, headers,
                                      updates, auth, body, client)
    return status, head, raw.decode("utf-8", "replace")


class TestRouting(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})

    def test_root_serves_the_page(self):
        status, _, body = request("/", self.config)
        self.assertEqual(status, 200)
        self.assertIn("timelapse-maker", body)

    def test_healthz(self):
        status, _, body = request("/healthz", self.config)
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), "ok")

    def test_unknown_route_is_404(self):
        status, _, _ = request("/nope", self.config)
        self.assertEqual(status, 404)

    def test_query_string_is_ignored(self):
        status, _, _ = request("/healthz?x=1", self.config)
        self.assertEqual(status, 200)

    def test_trailing_slash_is_ignored(self):
        status, _, _ = request("/healthz/", self.config)
        self.assertEqual(status, 200)

    def test_every_response_has_a_content_length(self):
        # protocol_version is HTTP/1.1, so a missing Content-Length would hang
        # a keep-alive client rather than fail loudly.
        for path in ("/", "/healthz", "/nope"):
            _, head, _ = request(path, self.config)
            self.assertIn("Content-Length:", head, path)

    def test_interpreter_version_is_not_advertised(self):
        _, head, _ = request("/healthz", self.config)
        self.assertIn("timelapse-web", head)
        self.assertNotIn("Python/", head)

    def test_remote_destination_renders_the_explanation(self):
        config = cfg(self.tmp, transfer={
            "enabled": True, "destination": "user@nas:/mnt/user/timelapse/"})
        status, _, body = request("/", config)
        self.assertEqual(status, 200)
        self.assertIn("Browsing is not supported", body)

    def test_path_from_config_is_escaped(self):
        config = cfg(self.tmp, transfer={"enabled": False},
                     video_output="/mnt/<script>alert(1)</script>")
        _, _, body = request("/", config)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)


class TestRunCommand(unittest.TestCase):
    """The status pane shells out. What matters is which outcomes count as
    failures - getting that wrong replaces the answer with an error page."""

    def test_output_is_captured(self):
        out, problem = web.run_command(
            [sys.executable, "-c", "print('hello')"])
        self.assertEqual(problem, "")
        self.assertEqual(out, "hello")

    def test_nonzero_exit_is_not_a_problem(self):
        """`systemctl status` exits 3 for an inactive unit and 4 for a missing
        one. That output is exactly what the page exists to show."""
        out, problem = web.run_command(
            [sys.executable, "-c", "print('inactive (dead)'); raise SystemExit(3)"])
        self.assertEqual(problem, "")
        self.assertIn("inactive", out)

    def test_stderr_is_kept(self):
        # journalctl explains itself on stderr.
        out, _ = web.run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('why')"])
        self.assertIn("why", out)

    def test_missing_binary_is_a_problem(self):
        out, problem = web.run_command(["definitely-not-a-real-binary-xyz"])
        self.assertEqual(out, "")
        self.assertIn("not installed", problem)

    def test_timeout_is_a_problem_not_a_hang(self):
        with mock.patch.object(web, "COMMAND_TIMEOUT", 1):
            out, problem = web.run_command(
                [sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertEqual(out, "")
        self.assertIn("did not answer", problem)


class TestCommandOutputIsRedacted(unittest.TestCase):
    """The leak was found *on this page*. Every journal this service can read
    was written by some version of the daemon, and on any host that ran one
    before 0.1.3 it holds camera passwords in full until it ages out. Fixing
    the daemon does nothing for those entries; this is what covers them."""

    SECRET = "Sup3rS3cret!"

    LEAKED = ("Aug 09 01:59:55 host python3[1414708]: 2026-08-09 01:59:55 "
              "WARNING [Doorbell] grab failed (#1): 502 Server Error: Bad "
              "Gateway for url: http://192.168.2.208/cgi-bin/api.cgi?"
              "cmd=Snap&channel=0&rs=tl&user=admin&password=" + SECRET)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})

    def test_run_command_redacts_before_anything_can_render_it(self):
        # At the source rather than in each renderer, so that adding a page
        # which shows command output cannot reintroduce this by omission.
        with mock.patch.object(web.subprocess, "run") as run:
            run.return_value = mock.Mock(stdout=self.LEAKED, stderr="")
            out, problem = web.run_command(["journalctl"])
        self.assertEqual(problem, "")
        self.assertNotIn(self.SECRET, out)
        self.assertIn("password=***", out)
        self.assertIn("[Doorbell] grab failed", out)

    def leaky_journal(self):
        """Patch at the subprocess boundary, not at run_command.

        Mocking run_command would step over the very code being tested and
        pass against a page that served the password in full, which is what
        the first draft of these two did.
        """
        return mock.patch.object(
            web.subprocess, "run",
            return_value=mock.Mock(stdout=self.LEAKED, stderr=""))

    def test_the_log_page_does_not_serve_the_password(self):
        with self.leaky_journal():
            _, _, body = request("/logs", self.config)
        self.assertNotIn(self.SECRET, body)
        self.assertIn("password=***", body)

    def test_the_status_page_does_not_serve_the_password(self):
        with self.leaky_journal():
            _, _, body = request("/status", self.config)
        self.assertNotIn(self.SECRET, body)


class TestReports(unittest.TestCase):

    def test_status_suppresses_the_journal_excerpt(self):
        # Without --lines=0 systemctl appends log lines that need journal
        # access, so the output looks truncated for no visible reason.
        with mock.patch.object(web, "run_command",
                               return_value=("", "")) as run:
            web.status_report()
        self.assertIn("--lines=0", run.call_args[0][0])
        self.assertIn("--no-pager", run.call_args[0][0])

    def test_journal_never_follows(self):
        """`timelapse logs` is journalctl -f, which never returns. A request
        handler running that would hang until the client gave up."""
        with mock.patch.object(web, "run_command",
                               return_value=("x", "")) as run:
            web.journal_report("capture", "200")
        argv = run.call_args[0][0]
        self.assertNotIn("-f", argv)
        self.assertNotIn("--follow", argv)
        self.assertIn("--no-pager", argv)
        self.assertEqual(argv[argv.index("-n") + 1], "200")

    def test_request_values_never_reach_the_command_line(self):
        with mock.patch.object(web, "run_command",
                               return_value=("x", "")) as run:
            web.journal_report("; rm -rf /", "9999; whoami")
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("-u") + 1], "timelapse-capture")
        self.assertEqual(argv[argv.index("-n") + 1], "200")
        for token in argv:
            self.assertNotIn("rm -rf", token)
            self.assertNotIn("whoami", token)

    def test_every_log_unit_maps_to_a_real_unit_name(self):
        for key, unit in web.LOG_UNITS.items():
            self.assertTrue(unit.startswith("timelapse-"), key)

    def test_empty_journal_explains_the_group_problem(self):
        """An unprivileged reader gets "-- No entries --", not a permission
        error, which reads as a bug in the UI."""
        for output in ("-- No entries --", "", "-- no entries --"):
            with mock.patch.object(web, "run_command",
                                   return_value=(output, "")):
                rep = web.journal_report("capture", "200")
            self.assertIn("systemd-journal", rep["hint"], repr(output))

    def test_real_output_gets_no_hint(self):
        with mock.patch.object(web, "run_command",
                               return_value=("Aug 07 00:31 started", "")):
            rep = web.journal_report("capture", "200")
        self.assertEqual(rep["hint"], "")

    def test_a_failed_command_gets_no_misleading_hint(self):
        with mock.patch.object(web, "run_command",
                               return_value=("", "journalctl is not installed here.")):
            rep = web.journal_report("capture", "200")
        self.assertEqual(rep["hint"], "")
        self.assertIn("not installed", rep["problem"])


RAW_STATUS = """\
● timelapse-capture.service - Timelapse capture daemon
     Loaded: loaded (/etc/systemd/system/timelapse-capture.service; enabled)
     Active: active (running) since Mon 2026-08-10 22:00:01 EEST; 14h ago
       Docs: https://github.com/war4peace/timelapse-maker
 Invocation: 2b1f4c9e7a3d4f0b8c6e1a2d3f4b5c6d
   Main PID: 1234 (python3)
      Tasks: 9 (limit: 38000)
     CGroup: /system.slice/timelapse-capture.service
"""


class TestUnitStates(unittest.TestCase):
    """`systemctl status` spends a page per unit answering a question that
    fits in one word. These cover the translation into that word."""

    def show(self, *units):
        """systemctl show's real shape: one block per unit, blank-line
        separated, properties in whatever order systemd feels like."""
        return "\n\n".join(
            "\n".join(f"{k}={v}" for k, v in u.items()) for u in units)

    def daemon(self, unit="timelapse-capture.service", **over):
        props = {"Id": unit, "LoadState": "loaded", "ActiveState": "active",
                 "UnitFileState": "enabled",
                 "ActiveEnterTimestamp": "Mon 2026-08-10 22:00:01 EEST"}
        props.update(over)
        return props

    def rows(self, text):
        with mock.patch.object(web, "run_command", return_value=(text, "")):
            rows, problem = web.unit_states()
        self.assertEqual(problem, "")
        return {r[0]: r for r in rows}

    def test_it_asks_show_for_named_properties_not_status(self):
        # `systemctl status` is a human report and not a contract; `show`
        # names its fields and keeps them.
        with mock.patch.object(web, "run_command",
                               return_value=("", "")) as run:
            web.unit_states()
        argv = run.call_args[0][0]
        self.assertIn("show", argv)
        self.assertNotIn("status", argv)
        for prop in web.STATUS_PROPS:
            self.assertIn(f"--property={prop}", argv)

    def test_a_running_daemon_says_running_and_since_when(self):
        row = self.rows(self.show(self.daemon()))["Capture"]
        self.assertEqual(row[1:3], ("ok", "Running"))
        self.assertIn("since Mon 2026-08-10", row[3])

    def test_a_finished_oneshot_reads_as_a_success(self):
        # The nightly encode is inactive for 23 hours 22 minutes of every day,
        # so "Stopped" (the daemon rule) would invent a fault on a healthy
        # system. "Idle" fixed that but answered the wrong question: this row
        # is here to say whether last night's encode worked.
        row = self.rows(self.show(self.daemon(
            "timelapse-encode.service", ActiveState="inactive",
            InactiveEnterTimestamp="Mon 2026-08-11 00:42:11 EEST",
            Result="success")))["Last encode run"]
        self.assertEqual(row[1:3], ("ok", "Successful"))
        self.assertIn("last finished Mon 2026-08-11", row[3])

    def test_a_oneshot_that_has_never_run_says_that_instead(self):
        # No timestamp, so there is no run to call successful. Saying so beats
        # a green "Successful" about something that has not happened.
        row = self.rows(self.show(self.daemon(
            "timelapse-encode.service", ActiveState="inactive",
            Result="success")))["Last encode run"]
        self.assertEqual(row[1:3], ("", "Not yet run"))
        self.assertIn("timer has not fired", row[3])

    def test_a_bad_result_is_not_dressed_up_as_success(self):
        # systemd normally leaves a failed oneshot ActiveState=failed, which
        # is caught earlier. Belt and braces: the green line must depend on
        # the result, not merely on there being a timestamp.
        row = self.rows(self.show(self.daemon(
            "timelapse-encode.service", ActiveState="inactive",
            InactiveEnterTimestamp="Mon 2026-08-11 00:42:11 EEST",
            Result="exit-code")))["Last encode run"]
        self.assertEqual(row[1:3], ("bad", "Failed"))
        self.assertIn("exit-code", row[3])

    def test_a_stopped_daemon_is_a_fault_and_says_how_to_fix_it(self):
        row = self.rows(self.show(self.daemon(
            ActiveState="inactive")))["Capture"]
        self.assertEqual(row[1:3], ("bad", "Stopped"))
        self.assertIn("systemctl start timelapse-capture.service", row[3])

    def test_an_active_timer_shows_its_next_run(self):
        row = self.rows(self.show(self.daemon(
            "timelapse-encode.timer",
            NextElapseUSecRealtime="Wed 2026-08-12 00:05:00 EEST",
        )))["Nightly encode"]
        self.assertEqual(row[1:3], ("ok", "Scheduled"))
        self.assertIn("next run Wed 2026-08-12 00:05:00", row[3])

    def test_a_monotonic_timer_shows_its_next_run_too(self):
        """The credential watch fires on OnBootSec/OnUnitActiveSec, so
        systemd leaves NextElapseUSecRealtime empty and answers in a timespan
        since boot instead. Reading only the realtime property left that row
        with an empty Detail, which reads as though the unit were broken.
        These are the exact strings systemd 255 produced, measured
        2026-08-14."""
        with mock.patch.object(web.time, "monotonic", return_value=15.463836):
            row = self.rows(self.show(self.daemon(
                "timelapse-watch.timer",
                NextElapseUSecRealtime="",
                NextElapseUSecMonotonic="5min 1.016502s",
            )))["Credential watch"]
        self.assertEqual(row[1:3], ("ok", "Scheduled"))
        self.assertIn("next run in 4m", row[3])

    def test_a_timer_that_has_run_says_when(self):
        row = self.rows(self.show(self.daemon(
            "timelapse-watch.timer",
            NextElapseUSecMonotonic="infinity",
            LastTriggerUSec="Fri 2026-08-14 22:50:12 EEST",
        )))["Credential watch"]
        self.assertIn("last ran Fri 2026-08-14 22:50:12", row[3])

    def test_a_timer_that_will_never_fire_again_says_that(self):
        row = self.rows(self.show(self.daemon(
            "timelapse-watch.timer",
            NextElapseUSecMonotonic="infinity")))["Credential watch"]
        self.assertEqual(row[3], "no further runs scheduled")

    def test_a_timer_about_to_fire_says_due_now(self):
        with mock.patch.object(web.time, "monotonic", return_value=301.0):
            row = self.rows(self.show(self.daemon(
                "timelapse-watch.timer",
                NextElapseUSecMonotonic="5min 1.016502s")))["Credential watch"]
        self.assertEqual(row[3], "due now")

    def test_no_timer_row_is_ever_left_with_an_empty_detail(self):
        # The defect this fixes, stated as the invariant rather than as one
        # case: whatever systemd says, a scheduled timer explains itself.
        for props in ({"NextElapseUSecRealtime": "Wed 2026-08-12 00:05 EEST"},
                      {"NextElapseUSecMonotonic": "5min 1.016502s"},
                      {"NextElapseUSecMonotonic": "infinity"},
                      {"LastTriggerUSec": "Fri 2026-08-14 22:50:12 EEST"}):
            row = self.rows(self.show(
                self.daemon("timelapse-watch.timer", **props)
            ))["Credential watch"]
            self.assertTrue(row[3], f"empty Detail for {props}")


    def test_a_unit_that_is_not_installed_says_so(self):
        row = self.rows(self.show(self.daemon(
            LoadState="not-found", ActiveState="inactive")))["Capture"]
        self.assertEqual(row[1], "bad")
        self.assertIn("installer", row[3])

    def test_a_failed_unit_carries_the_reason_and_points_at_the_log(self):
        row = self.rows(self.show(self.daemon(
            ActiveState="failed", Result="exit-code")))["Capture"]
        self.assertEqual(row[1:3], ("bad", "Failed"))
        self.assertIn("exit-code", row[3])
        self.assertIn("log", row[3].lower())

    def test_running_but_disabled_says_it_will_not_survive_a_reboot(self):
        # The one state that looks entirely healthy and is not.
        row = self.rows(self.show(self.daemon(
            UnitFileState="disabled")))["Capture"]
        self.assertEqual(row[1:3], ("ok", "Running"))
        self.assertIn("reboot", row[3])

    def test_a_crash_loop_is_a_fault_not_a_start(self):
        # Restart=always plus something that will not stay up sits in
        # "activating/auto-restart" indefinitely. Reported as a calm
        # "Starting" it looks like a service caught mid-boot, forever.
        row = self.rows(self.show(self.daemon(
            ActiveState="activating", SubState="auto-restart")))["Capture"]
        self.assertEqual(row[1:3], ("bad", "Restarting"))
        self.assertIn("log", row[3].lower())

    def test_a_finished_oneshot_is_not_called_running(self):
        # Verified against real systemd: a RemainAfterExit oneshot reports
        # active/exited long after it stopped doing anything. Our encode unit
        # does not set it, but the row must not lie if one ever does.
        row = self.rows(self.show(self.daemon(
            "timelapse-encode.service", SubState="exited",
            ActiveEnterTimestamp="Tue 2026-08-11 00:05:12 EEST",
        )))["Last encode run"]
        self.assertEqual(row[2], "Finished")
        self.assertIn("ran at Tue 2026-08-11", row[3])

    def test_a_running_oneshot_is_called_running(self):
        row = self.rows(self.show(self.daemon(
            "timelapse-encode.service", SubState="running")))["Last encode run"]
        self.assertEqual(row[1:3], ("ok", "Running"))

    def test_an_encode_in_progress_does_not_read_as_stuck(self):
        # Reported from the deployment. A Type=oneshot sits in
        # activating/start for the whole of its ExecStart, which for the
        # nightly encode is twenty minutes and more; the daemon word for that
        # state is "Starting", and watching it say so for a quarter of an hour
        # reads as a job that never got going. Verified on systemd 255: the
        # start time is in InactiveExitTimestamp and the other two timestamps
        # are empty until it finishes.
        row = self.rows(self.show(self.daemon(
            "timelapse-encode.service", ActiveState="activating",
            SubState="start", ActiveEnterTimestamp="",
            InactiveExitTimestamp="Wed 2026-08-12 00:05:00 EEST",
        )))["Last encode run"]
        self.assertEqual(row[1:3], ("ok", "Running"))
        self.assertIn("started Wed 2026-08-12 00:05:00", row[3])

    def test_a_daemon_mid_start_is_still_starting(self):
        # The oneshot rule must not swallow the state it was named for.
        row = self.rows(self.show(self.daemon(
            ActiveState="activating", SubState="start")))["Capture"]
        self.assertEqual(row[1:3], ("", "Starting"))

    def test_a_oneshot_crash_loop_is_still_a_fault(self):
        row = self.rows(self.show(self.daemon(
            "timelapse-encode.service", ActiveState="activating",
            SubState="auto-restart")))["Last encode run"]
        self.assertEqual(row[1:3], ("bad", "Restarting"))

    def test_rows_are_matched_by_id_not_by_position(self):
        # A block that does not come back must not shift every later unit onto
        # the wrong row, which is what reading them in order would do.
        rows = self.rows(self.show(
            self.daemon("timelapse-web.service"),
            self.daemon("timelapse-encode.timer",
                        NextElapseUSecRealtime="Wed 2026-08-12 00:05 EEST")))
        self.assertEqual(rows["Web interface"][2], "Running")
        self.assertEqual(rows["Nightly encode"][2], "Scheduled")
        self.assertEqual(rows["Capture"][2], "Unknown")

    def test_a_diagnostic_line_is_not_mistaken_for_a_property(self):
        # run_command folds stderr in with stdout, and systemctl's warnings
        # contain "=" often enough.
        text = (self.show(self.daemon())
                + "\nWarning: unit file changed, run daemon-reload=maybe")
        self.assertEqual(self.rows(text)["Capture"][2], "Running")

    def test_a_missing_systemctl_is_a_problem_not_an_empty_table(self):
        with mock.patch.object(web, "run_command",
                               return_value=("", "systemctl is not installed.")):
            rows, problem = web.unit_states()
        self.assertEqual(rows, [])
        self.assertIn("not installed", problem)


class TestTimespanParsing(unittest.TestCase):
    """systemd's timespan format, from systemd.time(7). Parsed rather than
    guessed at because it is documented, which `systemctl status` output is
    not."""

    def test_the_measured_shape(self):
        self.assertAlmostEqual(web.parse_timespan("5min 1.016502s"),
                               301.016502, places=5)

    def test_compound_units(self):
        self.assertEqual(web.parse_timespan("1h 2min 3s"), 3723)
        self.assertEqual(web.parse_timespan("2d 4h"), 187200)

    def test_a_bare_number_of_seconds(self):
        self.assertEqual(web.parse_timespan("90s"), 90)

    def test_sub_second_units(self):
        self.assertAlmostEqual(web.parse_timespan("250ms"), 0.25)

    def test_infinity_and_junk_are_no_answer(self):
        self.assertIsNone(web.parse_timespan("infinity"))
        self.assertIsNone(web.parse_timespan(""))
        self.assertIsNone(web.parse_timespan(None))
        self.assertIsNone(web.parse_timespan("0"))
        self.assertIsNone(web.parse_timespan("whenever"))


class TestStatusRoutes(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})

    def test_status_page(self):
        with mock.patch.object(web, "run_command",
                               return_value=("Active: active (running)", "")):
            status, _, body = request("/status", self.config)
        self.assertEqual(status, 200)
        self.assertIn("Active: active (running)", body)

    SHOW = "\n".join(["Id=timelapse-capture.service", "LoadState=loaded",
                      "ActiveState=active", "UnitFileState=enabled"])

    def overview(self, show_text=None):
        with mock.patch.object(web, "run_command",
                               return_value=(show_text or self.SHOW, "")):
            return request("/", self.config)[2]

    def test_the_services_table_lives_on_the_overview(self):
        # Four rows did not justify a quarter of the navigation, and "is it
        # running" belongs beside "where are my videos".
        body = self.overview()
        self.assertIn("Services", body)
        self.assertIn("Capture", body)
        self.assertIn("Running", body)

    def test_the_overview_says_nothing_a_reader_did_not_ask_for(self):
        body = self.overview()
        for noise in ("Invocation:", "Main PID:", "CGroup:", "Docs:"):
            self.assertNotIn(noise, body, noise)

    def test_the_overview_shells_out_exactly_once(self):
        # It is the landing page. Rendering the full `systemctl status` into a
        # collapsed <details> here would cost a second subprocess and a screen
        # of markup on every view, to serve something rarely opened.
        with mock.patch.object(web, "run_command",
                               return_value=(self.SHOW, "")) as run:
            request("/", self.config)
        self.assertEqual(run.call_count, 1)
        self.assertIn("show", run.call_args[0][0])

    def test_the_raw_output_is_still_there_one_click_away(self):
        # When something is wrong this is what a bug report needs, and
        # re-running systemctl over ssh to get it is worse than a link.
        body = self.overview()
        self.assertIn('href="/status"', body)
        with mock.patch.object(web, "run_command",
                               return_value=(RAW_STATUS, "")):
            _, _, detail = request("/status", self.config)
        self.assertIn("Invocation:", detail)
        self.assertIn("Main PID:", detail)
        self.assertIn("systemctl status", detail)

    def test_the_detail_page_is_reachable_but_not_a_tab(self):
        # An old bookmark has to land somewhere useful; the navigation is
        # three tabs, and this is a page under the overview, not a fourth.
        with mock.patch.object(web, "run_command",
                               return_value=(RAW_STATUS, "")):
            status, _, body = request("/status", self.config)
        self.assertEqual(status, 200)
        nav = body.split("<nav>", 1)[1].split("</nav>", 1)[0]
        self.assertEqual(nav.count("<a "), 3)
        self.assertNotIn("Service status", nav)
        self.assertIn('href="/"', body)          # a way back

    def test_a_systemctl_that_will_not_run_is_reported_not_hidden(self):
        with mock.patch.object(
                web, "run_command",
                return_value=("", "systemctl is not installed.")) as run:
            _, _, body = request("/", self.config)
        self.assertEqual(run.call_count, 1)
        self.assertIn("not installed", body)

    def test_logs_page_defaults_to_capture(self):
        with mock.patch.object(web, "run_command",
                               return_value=("a log line", "")) as run:
            status, _, body = request("/logs", self.config)
        self.assertEqual(status, 200)
        self.assertIn("a log line", body)
        self.assertIn("timelapse-capture", run.call_args[0][0])

    def test_logs_page_honours_the_unit_picker(self):
        with mock.patch.object(web, "run_command",
                               return_value=("x", "")) as run:
            request("/logs?unit=encode&n=1000", self.config)
        argv = run.call_args[0][0]
        self.assertIn("timelapse-encode", argv)
        self.assertEqual(argv[argv.index("-n") + 1], "1000")

    def test_a_stale_bookmark_falls_back_rather_than_erroring(self):
        with mock.patch.object(web, "run_command",
                               return_value=("x", "")) as run:
            status, _, _ = request("/logs?unit=nonsense&n=7", self.config)
        self.assertEqual(status, 200)
        argv = run.call_args[0][0]
        self.assertIn("timelapse-capture", argv)
        self.assertEqual(argv[argv.index("-n") + 1], "200")

    def test_command_output_is_escaped(self):
        with mock.patch.object(web, "run_command",
                               return_value=("<script>alert(1)</script>", "")):
            _, _, body = request("/status", self.config)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_a_problem_is_shown_instead_of_an_empty_pre(self):
        with mock.patch.object(web, "run_command",
                               return_value=("", "systemctl is not installed here.")):
            _, _, body = request("/status", self.config)
        self.assertIn("not installed here", body)

    def test_the_cheap_endpoints_still_shell_out_for_nothing(self):
        """The overview did not used to run anything; folding the services
        table into it means one `systemctl show` per view, which is the
        deliberate cost of losing a tab. Everything a machine might hit in a
        loop must stay free of it: /healthz is a liveness probe, and /scan and
        /update are polled once a second by the page's own scripts."""
        with mock.patch.object(web, "run_command") as run:
            for path in ("/healthz", "/update"):
                request(path, self.config)
        run.assert_not_called()
        # /scan is the third of those and needs an index to answer at all, so
        # it is exercised in TestScanProgress; it reads an in-memory dict.

    def test_output_pages_ask_for_the_whole_window(self):
        # A journal line is as wide as journald decided, so the 54rem reading
        # column that suits prose and tables is the wrong frame for it. The
        # stylesheet keys off these two classes; without them the pane went
        # back to a fixed width with its scrollbar far below the fold.
        with mock.patch.object(web, "run_command",
                               return_value=("a line", "")):
            _, _, body = request("/logs", self.config)
        self.assertIn('<body class="pane-page">', body)
        self.assertIn('<section class="pane">', body)

    def test_the_status_page_is_prose_width_not_output_width(self):
        # It stopped being raw output, so it stopped wanting the raw-output
        # layout. Left in PANE_PAGES it would have stretched a four-row table
        # across the window and pinned it to the viewport height.
        with mock.patch.object(web, "run_command", return_value=("", "")):
            _, _, body = request("/status", self.config)
        self.assertIn('<body class="">', body)
        self.assertNotIn('<section class="pane">', body)

    def test_the_tabs_are_centred_independently_of_the_page_width(self):
        """Reported: the tabs jumped ~240px between the overview and the log
        page, because the wide pages drop the 54rem column and the nav was
        left-aligned inside it. Centring on the viewport makes their position
        independent of the content. This pins the rules; the positions
        themselves were measured in Chrome, see architecture.md section 9."""
        _, _, body = request("/", self.config)
        css = body.split("</style>", 1)[0]
        self.assertIn("scrollbar-gutter: stable", css)
        nav = css.split("nav {", 1)[1].split("}", 1)[0]
        self.assertIn("justify-content: center", nav)
        head = css.split("header {", 1)[1].split("}", 1)[0]
        self.assertIn("justify-content: center", head)

    def test_the_header_names_the_project_and_its_version_only(self):
        _, _, body = request("/", self.config)
        header = body.split("<header>", 1)[1].split("</header>", 1)[0]
        self.assertIn("timelapse-maker", header)
        self.assertIn(web.__version__, header)
        # It read "web 0.1.2", which said nothing: there is no way to be
        # looking at this page other than through the web interface.
        self.assertNotIn("web", header)

    def test_every_page_carries_the_same_nav(self):
        # Whatever the layout does, the controls themselves must not differ
        # between pages.
        navs = set()
        for path in ("/", "/status", "/logs"):
            with mock.patch.object(web, "run_command", return_value=("x", "")):
                _, _, body = request(path, self.config)
            # Strip the whole class attribute, not just "on": the inactive
            # links carry class="", so removing only the active marker leaves
            # a different residue on each page.
            navs.add(re.sub(r'\s*class="[^"]*"', "",
                            body.split("<nav>", 1)[1].split("</nav>", 1)[0]))
        self.assertEqual(len(navs), 1)

    def test_the_reading_column_is_left_alone_elsewhere(self):
        # Assert on the body tag, not the document: the stylesheet defining
        # .pane-page is inline, so the name appears on every page either way.
        _, _, body = request("/", self.config)
        self.assertIn('<body class="">', body)
        self.assertNotIn('<section class="pane">', body)

    def test_a_problem_message_is_not_stretched_to_the_window(self):
        # The pane fills the viewport height. Applying that to a one-line
        # error would render an almost empty box the height of the screen.
        with mock.patch.object(web, "run_command",
                               return_value=("", "journalctl is not installed.")):
            _, _, body = request("/logs", self.config)
        self.assertIn("not installed", body)
        self.assertNotIn('<section class="pane">', body)
        # The page itself still gets the full width; only the box is normal.
        self.assertIn('<body class="pane-page">', body)


class TestParseName(unittest.TestCase):
    """Six conventions, all present in a real five-year library. The native
    format is 64% of it - handling only that drops a third of the files."""

    def test_native(self):
        self.assertEqual(web.parse_name("Gate.20260707.mkv"),
                         ("Gate", "2026-07-07", "native"))

    def test_date_first(self):
        self.assertEqual(web.parse_name("2024-01-01_Workshop.mp4"),
                         ("Workshop", "2024-01-01", "date-first"))

    def test_date_last(self):
        self.assertEqual(web.parse_name("Courtyard_4K_2021-11-01.mp4"),
                         ("Courtyard_4K", "2021-11-01", "date-last"))

    def test_date_only_has_no_camera(self):
        # 449 files in the surveyed library. Not an error; a real bucket.
        self.assertEqual(web.parse_name("2021-11-01.mp4"),
                         ("", "2021-11-01", "date-only"))

    def test_double_stamp(self):
        self.assertEqual(
            web.parse_name("Court18020240428_20240428233819.mkv"),
            ("Court180", "2024-04-28", "double-stamp"))

    def test_timestamped_with_and_without_camera(self):
        self.assertEqual(web.parse_name("2023-05-12T22-00-01_roof.mp4"),
                         ("roof", "2023-05-12", "timestamped"))
        self.assertEqual(web.parse_name("2023-05-12T22-00-01.mp4"),
                         ("", "2023-05-12", "timestamped"))

    def test_an_impossible_date_does_not_win(self):
        # A pattern that matches but yields a non-date must fall through
        # rather than claiming the file.
        cam, day, kind = web.parse_name("something.99999999.mkv")
        self.assertIsNone(day)
        self.assertEqual(kind, "unrecognised")

    def test_february_30_is_not_a_date(self):
        self.assertIsNone(web.parse_name("Gate.20250230.mkv")[1])

    def test_dots_in_a_camera_name_survive(self):
        self.assertEqual(web.parse_name("Cam.One.20260707.mkv")[0], "Cam.One")

    def test_unrecognised(self):
        self.assertEqual(web.parse_name("MakeTLALL_backup.ps1"),
                         ("", None, "unrecognised"))


def build_library(root):
    """A miniature of the surveyed library: every pattern, both eras."""
    files = {
        "Gate.20260707.mkv": 500_000_000,
        "Workshop.20260707.mkv": 400_000_000,
        "Gate.20260706.mkv": 17_298,             # a failed encode
        "2024-01/2024-01-01_Workshop.mp4": 600_000_000,
        # The other spelling, on a different day: two names differing only in
        # case cannot coexist as the SAME filename on NTFS or an SMB share,
        # which is exactly how the real library carries both.
        "2024-01/2024-01-02_workshop.mp4": 600_000_000,
        "2021-11/2021-11-01.mp4": 520_000_000,   # no camera in the name
        "2021-11/Courtyard_4K_2021-11-02.mp4": 545_000_000,
        "2023-05-10 - Renovari/2023-05-10_Roof.mp4": 788_000_000,
        "MakeTLALL_backup.ps1": 18_514,          # not a video
        "notes.txt": 12,
    }
    for rel, size in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"\0")
            if size > 1:
                fh.seek(size - 1)
                fh.write(b"\0")
    return files


class IndexCase(unittest.TestCase):
    """Shared fixture: a real sqlite index over a real temp tree."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = Path(self.tmp) / "library"
        self.root.mkdir()
        build_library(self.root)
        self.state = Path(self.tmp) / "state"
        self.index = web.Index(self.state / "index.db", self.root)
        self.addCleanup(self.index_close)

    def index_close(self):
        pass

    def scan(self):
        """Run the scan synchronously - a thread would race the assertions."""
        self.index._scan_worker()


class TestIndexScan(IndexCase):

    def test_only_video_files_are_indexed(self):
        self.scan()
        names = {r["name"] for r in self.index._query("SELECT name FROM files")}
        self.assertIn("Gate.20260707.mkv", names)
        # "not a directory" is not a test for "is a video".
        self.assertNotIn("MakeTLALL_backup.ps1", names)
        self.assertNotIn("notes.txt", names)

    def test_counts_and_totals(self):
        self.scan()
        tot = self.index.totals()
        self.assertEqual(tot["n"], 8)
        self.assertEqual(tot["a"], "2021-11-01")
        self.assertEqual(tot["z"], "2026-07-07")

    def test_nested_and_root_files_both_land(self):
        self.scan()
        folders = {r["folder"] for r in self.index._query("SELECT folder FROM files")}
        self.assertIn("", folders)
        self.assertIn("2024-01", folders)
        self.assertIn("2023-05-10 - Renovari", folders)

    def test_small_files_are_flagged(self):
        self.scan()
        sus = self.index.suspects()
        self.assertEqual([r["name"] for r in sus], ["Gate.20260706.mkv"])

    def test_camera_spellings_are_not_merged(self):
        """A name is a place and places get recycled between cameras, so the
        index never decides that two names mean one thing."""
        self.scan()
        cams = [c["camera"] for c in self.index.cameras()]
        self.assertIn("Workshop", cams)
        self.assertIn("workshop", cams)

    def test_cameras_sort_case_insensitively_so_variants_are_adjacent(self):
        self.scan()
        cams = [c["camera"] for c in self.index.cameras()]
        i, j = cams.index("Workshop"), cams.index("workshop")
        self.assertEqual(abs(i - j), 1)

    def test_the_unnamed_bucket_exists(self):
        self.scan()
        cams = {c["camera"]: c["n"] for c in self.index.cameras()}
        self.assertEqual(cams.get(""), 1)

    def test_scan_progress_is_reported(self):
        self.scan()
        self.assertFalse(self.index.scan["running"])
        self.assertEqual(self.index.scan["files"], 8)
        self.assertEqual(self.index.scan["error"], "")

    def test_rescanning_does_not_duplicate(self):
        self.scan()
        self.scan()
        self.assertEqual(self.index.totals()["n"], 8)

    def test_a_deleted_file_is_dropped_by_a_full_scan(self):
        self.scan()
        (self.root / "Gate.20260707.mkv").unlink()
        self.scan()
        self.assertIsNone(self.index.get("Gate.20260707.mkv"))
        self.assertEqual(self.index.totals()["n"], 7)


class TestScanProgress(IndexCase):

    def test_the_count_advances_on_every_file(self):
        """It used to advance once per 500-file write batch, which is the
        wrong unit for a progress report: a library smaller than a batch
        finished still reporting 0, and a slow share stalled the number for
        500 files at a time. Both read as a stuck scan."""
        real_walk = self.index._walk
        seen = []

        def watched():
            # Records the counter as it stood before each row was counted,
            # so a per-batch update would show 0 for all eight.
            for row in real_walk():
                seen.append(self.index.scan["files"])
                yield row

        with mock.patch.object(self.index, "_walk", watched):
            self.scan()
        self.assertEqual(seen, [0, 1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(self.index.scan["files"], 8)


class TestIndexSurvivesAMissingLibrary(IndexCase):
    """A NAS that is not mounted yet must not cost you the index.

    Measured before this guard existed: restarting the service while the
    library was away reported "Indexed 0 files" and deleted every row, and it
    did not come back when the mount returned. On a real share that is a full
    rescan of thousands of files over CIFS, at every reboot that loses the
    race with the mount.
    """

    def setUp(self):
        super().setUp()
        # Retrying for ten minutes would be a slow unit test.
        patch = mock.patch.multiple(web, SCAN_RETRY_DELAY=0, SCAN_RETRY_LIMIT=2)
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_unreadable_root_keeps_the_index(self):
        self.scan()
        self.assertEqual(self.index.totals()["n"], 8)
        shutil.rmtree(self.root)
        self.scan()
        self.assertEqual(self.index.totals()["n"], 8, "index was wiped")

    def test_and_says_why(self):
        self.scan()
        shutil.rmtree(self.root)
        self.scan()
        self.assertIn("kept the existing index", self.index.scan["error"])
        self.assertFalse(self.index.scan["running"])

    def test_an_empty_but_readable_root_also_keeps_the_index(self):
        """The real NAS case: an unmounted CIFS mountpoint is a readable,
        EMPTY directory, so a readability check alone would not catch it."""
        self.scan()
        for child in list(self.root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        self.assertTrue(self.root.is_dir(), "root must still be readable")
        self.scan()
        self.assertEqual(self.index.totals()["n"], 8, "index was wiped")
        self.assertIn("kept", self.index.scan["error"])

    def test_the_library_returning_repairs_it(self):
        self.scan()
        moved = Path(self.tmp) / "away"
        self.root.rename(moved)
        self.scan()
        moved.rename(self.root)
        self.scan()
        self.assertEqual(self.index.totals()["n"], 8)
        self.assertEqual(self.index.scan["error"], "")

    def test_a_first_scan_of_an_empty_library_is_not_an_error(self):
        # Nothing indexed yet, nothing on disk: that is simply an empty
        # library, not a mount problem.
        for child in list(self.root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        self.scan()
        self.assertEqual(self.index.totals()["n"], 0)
        self.assertEqual(self.index.scan["error"], "")

    def test_a_real_deletion_still_shrinks_the_index(self):
        # Guard against the fix being too eager: removing SOME files must
        # still prune them.
        self.scan()
        (self.root / "Gate.20260707.mkv").unlink()
        self.scan()
        self.assertEqual(self.index.totals()["n"], 7)
        self.assertIsNone(self.index.get("Gate.20260707.mkv"))

    def test_stale_rows_are_repaired_by_browsing(self):
        """The cost of keeping the index: if the library really was emptied,
        the rows linger until something looks. Opening a folder removes them,
        which is the reconcile-on-access behaviour that already existed.

        Note the guard is narrow: it only holds when a scan finds NOTHING.
        A partial deletion still prunes normally, which the test above pins.
        """
        self.scan()
        for child in list(self.root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        (self.root / "2024-01").mkdir()
        self.scan()
        self.assertIsNotNone(self.index.get("2024-01/2024-01-01_Workshop.mp4"),
                             "rows should have been kept")
        self.index.reconcile_dir("2024-01")
        self.assertIsNone(self.index.get("2024-01/2024-01-01_Workshop.mp4"))

    def test_it_waits_rather_than_giving_up_at_once(self):
        with mock.patch.object(web.time, "sleep") as slept:
            shutil.rmtree(self.root)
            self.scan()
        self.assertEqual(slept.call_count, web.SCAN_RETRY_LIMIT - 1)

    def test_a_late_mount_is_picked_up_without_a_manual_rescan(self):
        """The boot race: the library appears while the scan is still waiting."""
        moved = Path(self.tmp) / "late"
        self.root.rename(moved)
        state = {"n": 0}

        def appear(_delay):
            state["n"] += 1
            if state["n"] == 1:
                moved.rename(self.root)

        with mock.patch.object(web.time, "sleep", side_effect=appear):
            self.scan()
        self.assertEqual(self.index.totals()["n"], 8)
        self.assertEqual(self.index.scan["error"], "")


class TestIndexReconcile(IndexCase):

    def test_unchanged_directory_reports_no_change(self):
        self.scan()
        self.assertFalse(self.index.reconcile_dir("2024-01"))

    def test_a_change_within_the_same_second_is_still_caught(self):
        """The old mtime gate stored seconds, so anything added in the same
        second as the scan stayed invisible until something else changed."""
        self.scan()
        new = self.root / "2024-01" / "2024-01-03_Workshop.mp4"
        new.write_bytes(b"\0" * 2_000_000)
        self.assertTrue(self.index.reconcile_dir("2024-01"))

    def test_added_file_appears(self):
        self.scan()
        new = self.root / "2024-01" / "2024-01-09_Workshop.mp4"
        new.write_bytes(b"\0" * 2_000_000)
        self.assertTrue(self.index.reconcile_dir("2024-01"))
        self.assertIsNotNone(self.index.get("2024-01/2024-01-09_Workshop.mp4"))

    def test_removed_file_disappears(self):
        self.scan()
        (self.root / "2024-01" / "2024-01-01_Workshop.mp4").unlink()
        self.assertTrue(self.index.reconcile_dir("2024-01"))
        self.assertIsNone(self.index.get("2024-01/2024-01-01_Workshop.mp4"))

    def test_reconciling_one_folder_leaves_the_others_alone(self):
        self.scan()
        (self.root / "2024-01" / "2024-01-01_Workshop.mp4").unlink()
        self.index.reconcile_dir("2024-01")
        self.assertIsNotNone(self.index.get("2021-11/2021-11-01.mp4"))

    def test_a_vanished_directory_takes_its_rows_with_it(self):
        self.scan()
        shutil.rmtree(self.root / "2021-11")
        self.assertTrue(self.index.reconcile_dir("2021-11"))
        self.assertIsNone(self.index.get("2021-11/2021-11-01.mp4"))

    def test_reconcile_file_notices_a_size_change(self):
        """A file overwritten in place does not move its directory's mtime, so
        this is the only thing that catches it."""
        self.scan()
        rel = "Gate.20260707.mkv"
        before = self.index.get(rel)["size"]
        with open(self.root / rel, "wb") as fh:
            fh.write(b"\0" * 1000)
        after = self.index.reconcile_file(rel)
        self.assertNotEqual(after["size"], before)
        self.assertEqual(after["size"], 1000)

    def test_reconcile_file_reflags_a_shrunken_file(self):
        self.scan()
        rel = "Gate.20260707.mkv"
        self.assertEqual(self.index.get(rel)["suspect"], 0)
        with open(self.root / rel, "wb") as fh:
            fh.write(b"\0" * 100)
        self.assertEqual(self.index.reconcile_file(rel)["suspect"], 1)

    def test_reconcile_file_drops_a_missing_file(self):
        self.scan()
        (self.root / "Gate.20260707.mkv").unlink()
        self.assertIsNone(self.index.reconcile_file("Gate.20260707.mkv"))
        self.assertIsNone(self.index.get("Gate.20260707.mkv"))


class TestIndexSafety(IndexCase):

    def test_traversal_is_refused(self):
        for rel in ("../etc/passwd", "../../etc", "2024-01/../../outside"):
            self.assertIsNone(self.index.abs_path(rel), rel)

    def test_a_sibling_directory_with_a_shared_prefix_is_refused(self):
        # startswith() would accept /library-old for a root of /library.
        sibling = Path(self.tmp) / "library-old"
        sibling.mkdir()
        self.assertIsNone(self.index.abs_path("../library-old"))

    def test_ordinary_paths_resolve(self):
        self.assertIsNotNone(self.index.abs_path("2024-01"))
        self.assertIsNotNone(self.index.abs_path("Gate.20260707.mkv"))

    def test_reconcile_file_enforces_the_extension_allow_list(self):
        """/video/<path> resolves through reconcile_file, so the allow-list has
        to hold here and not only in the scan. Without it a request could name
        any file the user keeps beside their videos, and this would stat it,
        index it and serve it back."""
        self.scan()
        self.assertIsNone(self.index.reconcile_file("MakeTLALL_backup.ps1"))
        self.assertIsNone(self.index.reconcile_file("notes.txt"))
        self.assertIsNone(self.index.get("MakeTLALL_backup.ps1"))
        # And a real video still works.
        self.assertIsNotNone(self.index.reconcile_file("Gate.20260707.mkv"))

    def test_changing_the_root_wipes_the_index(self):
        """An index built from a different directory is worse than no index."""
        self.scan()
        self.assertEqual(self.index.totals()["n"], 8)
        other = Path(self.tmp) / "elsewhere"
        other.mkdir()
        second = web.Index(self.state / "index.db", other)
        self.assertEqual(second.totals()["n"], 0)

    def test_an_unwritable_state_dir_degrades_rather_than_crashes(self):
        # A regular file where the state directory should be: mkdir fails, and
        # the service must still serve status and logs rather than refuse to
        # start. This is what a missing ReadWritePaths looks like in practice.
        Path(self.tmp, "afile").write_text("not a directory")
        broken = web.Index(Path(self.tmp) / "afile" / "sub" / "index.db",
                           self.root)
        self.assertFalse(broken.usable)
        self.assertIn("ReadWritePaths", broken.error)
        self.assertEqual(broken.cameras(), [])


class TestLibraryRoutes(IndexCase):

    def setUp(self):
        super().setUp()
        self.config = cfg(self.tmp, transfer={"enabled": True,
                                              "destination": str(self.root)})
        self.scan()

    def get(self, path):
        return request(path, self.config, self.index)

    def test_library_lists_cameras_and_folders(self):
        status, _, body = self.get("/library")
        self.assertEqual(status, 200)
        self.assertIn("Gate", body)
        self.assertIn("2024-01", body)

    def test_both_spellings_are_listed(self):
        _, _, body = self.get("/library")
        self.assertIn(">Workshop<", body)
        self.assertIn(">workshop<", body)

    def test_camera_view(self):
        _, _, body = self.get("/library?camera=Gate")
        self.assertIn("Gate.20260707.mkv", body)
        self.assertNotIn("Courtyard_4K_2021-11-02.mp4", body)

    def test_folder_view_reconciles(self):
        _, _, body = self.get("/library?folder=2024-01")
        self.assertIn("Re-checked against disk", body)
        self.assertIn("2024-01-01_Workshop.mp4", body)

    def test_flagged_view_shows_the_full_path(self):
        _, _, body = self.get("/library?flagged=1")
        self.assertIn("Gate.20260706.mkv", body)
        self.assertIn(str(self.root), body)
        self.assertIn("never deletes", body)

    def test_unnamed_camera_gets_a_readable_label(self):
        _, _, body = self.get("/library")
        self.assertIn("no name in filename", body)

    def test_the_unnamed_camera_group_opens(self):
        # `?camera=` is a real filter, not an absent one: files with no camera
        # in the name group under "". parse_qs drops blank values by default,
        # so this fell through to the home page and the group the index links
        # to looked empty. 450 files were unreachable on the real library.
        _, _, body = self.get("/library?camera=")
        self.assertIn("no name in filename", body)
        self.assertIn("2021-11-01.mp4", body)
        # The home page, which is what used to be served instead.
        self.assertNotIn("<h2>Folders</h2>", body)

    def test_the_root_folder_opens(self):
        # Same bug, other route: the library root's folder value is "".
        _, _, body = self.get("/library?folder=")
        self.assertIn("(root)", body)
        self.assertIn("Gate.20260707.mkv", body)
        self.assertIn("Re-checked against disk", body)
        self.assertNotIn("<h2>Folders</h2>", body)

    def test_the_links_the_index_offers_are_the_ones_that_work(self):
        # The two views above are only reachable by the links on the home
        # page, so pin that they are still generated in the form just tested.
        _, _, home = self.get("/library")
        self.assertIn('href="/library?camera="', home)
        self.assertIn('href="/library?folder="', home)

    def test_the_day_view_drops_the_day_column(self):
        # Every row carried a link back to the page being read, under a
        # heading that already states the day.
        _, _, body = self.get("/library?day=2024-01-01")
        self.assertNotIn("<th>Day</th>", body)
        self.assertNotIn("/library?day=2024-01-01\"", body)
        self.assertIn("2024-01-01_Workshop.mp4", body)

    def test_other_views_keep_the_day_column(self):
        # There it discriminates between rows and links somewhere new.
        for path in ("/library?camera=Gate", "/library?folder=",
                     "/library?flagged=1"):
            with self.subTest(path=path):
                _, _, body = self.get(path)
                self.assertIn("<th>Day</th>", body)

    def sub_row_width(self, body):
        """Cells in the first path sub-row, counting colspan."""
        m = re.search(r'<tr class="sub-row">(.*?)</tr>', body, re.S)
        self.assertIsNotNone(m, "no path sub-row rendered")
        width = 0
        for td in re.findall(r"<td[^>]*>", m.group(1)):
            span = re.search(r'colspan="(\d+)"', td)
            width += int(span.group(1)) if span else 1
        return width

    def test_tables_stay_rectangular(self):
        # Guards a future column being added without adjusting the colspan.
        # Note it does NOT catch the alignment bug below: dropping the Day
        # column while keeping the leading cell is still rectangular, just
        # wrong, which is why both tests exist.
        for path in ("/library?day=2024-01-01", "/library?camera=Gate",
                     "/library?folder=", "/library?flagged=1"):
            with self.subTest(path=path):
                _, _, body = self.get(path)
                self.assertEqual(self.sub_row_width(body), body.count("<th>"))

    def running(self):
        """Put the index into the running-scan state the banner reports."""
        self.index.scan.update(running=True, files=1234, started=time.time())

    def test_a_running_scan_marks_itself_and_ships_the_poller(self):
        # Without this the line sat at its server-rendered value forever and
        # read as a stuck process.
        self.running()
        _, _, body = self.get("/library")
        self.assertIn('data-running="1"', body)
        self.assertIn("setInterval", body)
        self.assertIn("1,234 files so far", body)

    def test_an_idle_page_ships_no_poller(self):
        _, _, body = self.get("/library")
        self.assertIn('data-running="0"', body)
        self.assertNotIn("setInterval", body)

    def test_the_running_banner_does_not_tell_you_to_reload(self):
        # It updates itself now; the old text told you to reload. Scoped to
        # the banner, since the poller itself calls location.reload().
        self.running()
        _, _, body = self.get("/scan")
        self.assertIn("Indexing", body)
        self.assertNotIn("reload", body.lower())

    def test_scan_endpoint_returns_the_banner_alone(self):
        self.running()
        status, _, body = self.get("/scan")
        self.assertEqual(status, 200)
        self.assertIn('id="scan"', body)
        self.assertIn("1,234 files so far", body)
        # No script: a script assigned through outerHTML would not run, and
        # shipping one that cannot work invites someone to debug it.
        self.assertNotIn("setInterval", body)
        # No page furniture either, so polling stays cheap.
        self.assertNotIn("<h2>Folders</h2>", body)
        self.assertNotIn("<nav>", body)

    def test_scan_endpoint_reports_completion(self):
        # This transition is what stops the poller and triggers the reload.
        _, _, body = self.get("/scan")
        self.assertIn('data-running="0"', body)

    def test_scan_endpoint_touches_neither_disk_nor_index(self):
        # Polled once a second during a scan, so it must not compete with the
        # scan for the share it is reporting on.
        with mock.patch.object(self.index, "reconcile_dir") as rec, \
                mock.patch.object(self.index, "_query") as q:
            self.get("/scan")
        rec.assert_not_called()
        q.assert_not_called()

    def test_the_library_keeps_the_reading_column(self):
        # Tables and prose want the 54rem column; only raw command output
        # takes the whole window.
        _, _, body = self.get("/library")
        self.assertIn('<body class="">', body)

    def test_the_path_lines_up_under_the_name(self):
        # The sub-row's empty leading cell exists only to skip the Day column.
        # Keeping it when there is no Day column indents the share path and
        # URL under Folder instead of under the file they belong to.
        _, _, day = self.get("/library?day=2024-01-01")
        _, _, camera = self.get("/library?camera=Gate")
        self.assertNotIn('<tr class="sub-row"><td></td>', day)
        self.assertIn('<tr class="sub-row"><td></td>', camera)

    def test_a_blank_day_is_still_refused(self):
        # keep_blank_values makes `?day=` reach the day view, where valid_day
        # must reject it rather than querying for a day of "".
        _, _, body = self.get("/library?day=")
        self.assertIn("Not a date", body)

    def test_index_error_is_explained_not_hidden(self):
        broken = web.Index(Path(self.tmp) / "nope" / "x", self.root)
        broken.error = "boom, needs ReadWritePaths"
        _, _, body = request("/library", self.config, broken)
        self.assertIn("boom", body)

    def test_rescan_needs_a_post(self):
        # A GET link would let a prefetch or a refresh start a full scan.
        status, _, _ = self.get("/rescan")
        self.assertEqual(status, 404)

    def test_rescan_post_redirects(self):
        with mock.patch.object(self.index, "start_scan") as start:
            status, head, _ = request("/rescan", self.config, self.index,
                                      method="POST")
        self.assertEqual(status, 303)
        self.assertIn("Location: /library", head)
        start.assert_called_once()


class TestFilenameHelpers(unittest.TestCase):

    def test_media_types(self):
        self.assertEqual(web.media_type("Gate.20260707.mkv"),
                         "video/x-matroska")
        self.assertEqual(web.media_type("x.mp4"), "video/mp4")

    def test_unknown_extension_is_generic(self):
        self.assertEqual(web.media_type("x.qqq"), "application/octet-stream")

    def test_ascii_filename_strips_non_ascii(self):
        # The real library has Romanian folder names; a raw non-ASCII value in
        # Content-Disposition risks a mangled header.
        got = web.ascii_filename("Renovări.mkv", ".m3u")
        self.assertTrue(all(ord(c) < 128 for c in got), got)
        self.assertTrue(got.endswith(".m3u"))

    def test_ascii_filename_removes_quoting_hazards(self):
        got = web.ascii_filename('we"ird\\name.mkv', ".m3u")
        self.assertNotIn('"', got)
        self.assertNotIn("\\", got)

    def test_ascii_filename_never_returns_only_a_suffix(self):
        # Everything in the stem is stripped, so there is nothing left to name
        # the file after.
        self.assertEqual(web.ascii_filename("___.mkv", ".m3u"), "timelapse.m3u")
        self.assertEqual(web.ascii_filename("字.mkv", ".m3u"), "timelapse.m3u")


class TestServing(IndexCase):

    def setUp(self):
        super().setUp()
        self.config = cfg(self.tmp, transfer={"enabled": True,
                                              "destination": str(self.root)})
        self.scan()

    def get(self, path, **kw):
        return request(path, self.config, self.index, **kw)

    def test_video_is_served_with_its_media_type(self):
        status, head, _ = self.get("/video/Gate.20260707.mkv")
        self.assertEqual(status, 200)
        self.assertIn("Content-Type: video/x-matroska", head)

    def test_content_length_matches_the_file(self):
        size = (self.root / "Gate.20260707.mkv").stat().st_size
        _, head, body = self.get("/video/Gate.20260707.mkv")
        self.assertIn(f"Content-Length: {size}", head)
        self.assertEqual(len(body.encode("utf-8", "surrogateescape")), size)

    def test_range_support_is_advertised(self):
        _, head, _ = self.get("/video/Gate.20260707.mkv")
        self.assertIn("Accept-Ranges: bytes", head)

    def test_head_sends_headers_but_no_body(self):
        _, head, body = self.get("/video/Gate.20260707.mkv", method="HEAD")
        self.assertIn("Content-Length:", head)
        self.assertEqual(body, "")

    def test_download_sets_an_attachment_disposition(self):
        _, head, _ = self.get("/video/Gate.20260707.mkv?download=1")
        self.assertIn("Content-Disposition: attachment", head)
        self.assertIn("Gate.20260707.mkv", head)

    def test_a_nested_path_with_spaces_resolves(self):
        status, _, _ = self.get(
            "/video/2023-05-10%20-%20Renovari/2023-05-10_Roof.mp4")
        self.assertEqual(status, 200)

    def test_missing_file_is_404(self):
        status, _, _ = self.get("/video/nope.mkv")
        self.assertEqual(status, 404)

    def test_a_file_deleted_behind_our_back_is_404_and_leaves_the_index(self):
        (self.root / "Gate.20260707.mkv").unlink()
        status, _, _ = self.get("/video/Gate.20260707.mkv")
        self.assertEqual(status, 404)
        self.assertIsNone(self.index.get("Gate.20260707.mkv"))

    def test_serving_reconciles_a_file_changed_in_place(self):
        """A file overwritten at the same name does not move its directory's
        mtime, so serving it is the only thing that notices."""
        with open(self.root / "Gate.20260707.mkv", "wb") as fh:
            fh.write(b"\0" * 4096)
        _, head, _ = self.get("/video/Gate.20260707.mkv")
        self.assertIn("Content-Length: 4096", head)
        self.assertEqual(self.index.get("Gate.20260707.mkv")["size"], 4096)

    def test_traversal_is_refused(self):
        for attack in ("/video/../../etc/passwd",
                       "/video/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                       "/video/2024-01/../../../etc/passwd"):
            status, _, _ = self.get(attack)
            self.assertEqual(status, 404, attack)

    def test_a_non_video_in_the_library_is_not_servable(self):
        # It is not in the index, so there is nothing to serve.
        status, _, _ = self.get("/video/MakeTLALL_backup.ps1")
        self.assertEqual(status, 404)


class TestParseRange(unittest.TestCase):
    """RFC 7233 in the small. Getting the edges wrong makes a scrubber jump to
    the wrong place, which looks like a corrupt file rather than a bad header."""

    def test_no_header_means_whole_file(self):
        self.assertIsNone(web.parse_range(None, 1000))
        self.assertIsNone(web.parse_range("", 1000))

    def test_closed_range(self):
        self.assertEqual(web.parse_range("bytes=0-499", 1000), (0, 499))
        self.assertEqual(web.parse_range("bytes=500-999", 1000), (500, 999))

    def test_open_ended_range(self):
        self.assertEqual(web.parse_range("bytes=500-", 1000), (500, 999))

    def test_suffix_range(self):
        # Players read a trailer this way; Matroska keeps its cues at the end.
        self.assertEqual(web.parse_range("bytes=-500", 1000), (500, 999))

    def test_suffix_longer_than_the_file_is_the_whole_file(self):
        self.assertEqual(web.parse_range("bytes=-5000", 1000), (0, 999))

    def test_end_past_the_file_is_clamped_not_rejected(self):
        self.assertEqual(web.parse_range("bytes=900-99999", 1000), (900, 999))

    def test_start_past_the_file_is_unsatisfiable(self):
        self.assertIs(web.parse_range("bytes=1000-", 1000), web.UNSATISFIABLE)
        self.assertIs(web.parse_range("bytes=5000-6000", 1000),
                      web.UNSATISFIABLE)

    def test_zero_length_suffix_is_unsatisfiable(self):
        self.assertIs(web.parse_range("bytes=-0", 1000), web.UNSATISFIABLE)

    def test_backwards_range_is_unsatisfiable(self):
        self.assertIs(web.parse_range("bytes=500-100", 1000), web.UNSATISFIABLE)

    def test_an_empty_file_satisfies_nothing(self):
        self.assertIs(web.parse_range("bytes=0-0", 0), web.UNSATISFIABLE)

    def test_multi_range_is_ignored_rather_than_refused(self):
        # Would need multipart/byteranges. RFC 7233 permits ignoring Range, and
        # nothing seeking a video asks for more than one.
        self.assertIsNone(web.parse_range("bytes=0-99,200-299", 1000))

    def test_junk_is_ignored_rather_than_refused(self):
        for junk in ("bytes=abc", "items=0-1", "bytes=", "bytes=-", "0-99"):
            self.assertIsNone(web.parse_range(junk, 1000), junk)

    def test_absurdly_long_digits_do_not_reach_int(self):
        # \d{0,19} rather than \d*: a megabyte of digits is not a request worth
        # doing arbitrary-precision arithmetic for.
        self.assertIsNone(web.parse_range("bytes=" + "9" * 5000 + "-", 1000))


class TestRangeRequests(IndexCase):

    def setUp(self):
        super().setUp()
        # Distinctive content: the shared fixture is sparse zeros, so every
        # slice of it would compare equal and prove nothing.
        self.data = bytes(range(256)) * 400        # 102,400 bytes
        (self.root / "Range.20260101.mkv").write_bytes(self.data)
        self.config = cfg(self.tmp, transfer={"enabled": True,
                                              "destination": str(self.root)})
        self.scan()

    def get(self, rng=None, **kw):
        headers = f"Range: {rng}\r\n" if rng else ""
        return request_bytes("/video/Range.20260101.mkv", self.config,
                             self.index, headers=headers, **kw)

    def test_a_range_returns_206_with_the_right_bytes(self):
        status, head, body = self.get("bytes=1000-1999")
        self.assertEqual(status, 206)
        self.assertIn("Content-Range: bytes 1000-1999/102400", head)
        self.assertIn("Content-Length: 1000", head)
        self.assertEqual(body, self.data[1000:2000])

    def test_open_ended_range_runs_to_the_end(self):
        status, head, body = self.get("bytes=102000-")
        self.assertEqual(status, 206)
        self.assertIn("Content-Range: bytes 102000-102399/102400", head)
        self.assertEqual(body, self.data[102000:])

    def test_suffix_range_returns_the_tail(self):
        status, head, body = self.get("bytes=-256")
        self.assertEqual(status, 206)
        self.assertIn("Content-Range: bytes 102144-102399/102400", head)
        self.assertEqual(body, self.data[-256:])

    def test_a_clamped_range_reports_what_it_actually_sent(self):
        status, head, body = self.get("bytes=102300-999999")
        self.assertEqual(status, 206)
        self.assertIn("Content-Range: bytes 102300-102399/102400", head)
        self.assertEqual(len(body), 100)

    def test_first_byte_range(self):
        status, _, body = self.get("bytes=0-0")
        self.assertEqual(status, 206)
        self.assertEqual(body, self.data[:1])

    def test_no_range_still_serves_the_whole_file(self):
        status, head, body = self.get()
        self.assertEqual(status, 200)
        self.assertIn("Content-Length: 102400", head)
        self.assertEqual(body, self.data)

    def test_unsatisfiable_is_416_and_says_how_big_the_file_is(self):
        status, head, body = self.get("bytes=999999-")
        self.assertEqual(status, 416)
        self.assertIn("Content-Range: bytes */102400", head)
        self.assertEqual(body, b"")

    def test_multi_range_falls_back_to_the_whole_file(self):
        status, _, body = self.get("bytes=0-99,200-299")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data)

    def test_junk_range_falls_back_to_the_whole_file(self):
        status, _, body = self.get("bytes=nonsense")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data)

    def test_head_with_a_range_has_headers_and_no_body(self):
        status, head, body = self.get("bytes=10-19", method="HEAD")
        self.assertEqual(status, 206)
        self.assertIn("Content-Range: bytes 10-19/102400", head)
        self.assertEqual(body, b"")

    def test_etag_and_last_modified_are_sent(self):
        _, head, _ = self.get()
        self.assertIn("ETag:", head)
        self.assertIn("Last-Modified:", head)

    def test_etag_changes_when_the_file_does(self):
        _, head1, _ = self.get()
        etag1 = [l for l in head1.splitlines() if l.startswith("ETag:")][0]
        (self.root / "Range.20260101.mkv").write_bytes(b"x" * 50)
        _, head2, _ = self.get()
        etag2 = [l for l in head2.splitlines() if l.startswith("ETag:")][0]
        self.assertNotEqual(etag1, etag2)

    def _etag(self):
        _, head, _ = self.get()
        return [l.split(": ", 1)[1] for l in head.splitlines()
                if l.startswith("ETag:")][0]

    def test_if_range_matching_honours_the_range(self):
        etag = self._etag()
        status, _, body = request_bytes(
            "/video/Range.20260101.mkv", self.config, self.index,
            headers=f"Range: bytes=0-99\r\nIf-Range: {etag}\r\n")
        self.assertEqual(status, 206)
        self.assertEqual(body, self.data[:100])

    def test_if_range_mismatch_sends_the_whole_current_file(self):
        """The client is resuming against a version we no longer have, so its
        offsets are meaningless - splicing two encodes together would hand back
        a file that is corrupt in a way nothing would report."""
        status, head, body = request_bytes(
            "/video/Range.20260101.mkv", self.config, self.index,
            headers='Range: bytes=0-99\r\nIf-Range: "stale-etag"\r\n')
        self.assertEqual(status, 200)
        self.assertEqual(body, self.data)
        self.assertNotIn("Content-Range:", head)

    def test_a_range_after_the_file_shrank_is_recomputed_not_stale(self):
        # reconcile_file runs first, so the size in Content-Range is current.
        (self.root / "Range.20260101.mkv").write_bytes(b"y" * 500)
        status, head, body = self.get("bytes=100-399")
        self.assertEqual(status, 206)
        self.assertIn("/500", head)
        self.assertEqual(body, b"y" * 300)


class TestPlaylist(IndexCase):

    def setUp(self):
        super().setUp()
        self.config = cfg(self.tmp, transfer={"enabled": True,
                                              "destination": str(self.root)})
        self.scan()

    def get(self, path, **kw):
        return request(path, self.config, self.index, **kw)

    def test_playlist_shape(self):
        status, head, body = self.get("/play/Gate.20260707.mkv")
        self.assertEqual(status, 200)
        self.assertIn("audio/x-mpegurl", head)
        lines = body.strip().splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertTrue(lines[1].startswith("#EXTINF:-1,"))
        self.assertTrue(lines[2].startswith("http://"))

    def test_the_url_comes_from_the_request_host(self):
        """An .m3u containing 127.0.0.1 is useless the moment it opens on a
        phone. The only address known to work is the one the client used."""
        _, _, body = self.get("/play/Gate.20260707.mkv")
        self.assertIn("http://nas.local/video/Gate.20260707.mkv", body)

    def test_a_forged_host_falls_back_to_the_configured_bind(self):
        config = dict(self.config)
        config["web"] = {"bind": "127.0.0.1", "port": 8787}
        _, _, body = request("/play/Gate.20260707.mkv", config, self.index,
                             headers="Host: bad host\r\n")
        self.assertIn("http://127.0.0.1:8787/video/", body)

    def test_forwarded_proto_is_honoured_but_only_for_http_or_https(self):
        _, _, body = self.get("/play/Gate.20260707.mkv",
                              headers="X-Forwarded-Proto: https\r\n")
        self.assertIn("https://nas.local/", body)
        _, _, body = self.get("/play/Gate.20260707.mkv",
                              headers="X-Forwarded-Proto: gopher\r\n")
        self.assertIn("http://nas.local/", body)

    def test_disposition_makes_the_desktop_open_a_player(self):
        # The .m3u filename here is what picks the app, not the URL.
        _, head, _ = self.get("/play/Gate.20260707.mkv")
        self.assertIn("Content-Disposition: attachment", head)
        self.assertIn('filename="Gate.20260707.m3u"', head)

    def test_title_names_the_place_and_the_day(self):
        _, _, body = self.get("/play/Gate.20260707.mkv")
        self.assertIn("#EXTINF:-1,Gate 2026-07-07", body)

    def test_a_file_with_no_camera_still_gets_a_title(self):
        _, _, body = self.get("/play/2021-11/2021-11-01.mp4")
        self.assertIn("#EXTINF:-1,2021-11-01", body)

    def test_path_is_percent_encoded_in_the_url(self):
        _, _, body = self.get(
            "/play/2023-05-10%20-%20Renovari/2023-05-10_Roof.mp4")
        self.assertIn("2023-05-10%20-%20Renovari/2023-05-10_Roof.mp4", body)
        self.assertNotIn("Renovari/2023-05-10_Roof.mp4\n", body.split("://")[0])

    def test_missing_file_is_404(self):
        status, _, _ = self.get("/play/nope.mkv")
        self.assertEqual(status, 404)


class TestDayHelpers(unittest.TestCase):

    def test_valid_days(self):
        self.assertEqual(web.valid_day("2026-01-23"), "2026-01-23")

    def test_rejects_nonsense(self):
        for bad in ("", None, "2026-1-23", "20260123", "yesterday",
                    "2026-01-23; rm -rf /", "../../etc"):
            self.assertIsNone(web.valid_day(bad), repr(bad))

    def test_rejects_impossible_dates(self):
        self.assertIsNone(web.valid_day("2025-02-30"))
        self.assertIsNone(web.valid_day("2026-13-01"))

    def test_m3u_title_collapses_newlines(self):
        """A filename may legally contain a newline on Linux, and an #EXTINF
        carrying one would split into a bogus second playlist entry."""
        self.assertEqual(web.m3u_title("Gate\n2026-01-23"), "Gate 2026-01-23")
        self.assertEqual(web.m3u_title("a\r\nb\tc"), "a b c")

    def test_m3u_title_never_empty(self):
        self.assertEqual(web.m3u_title("   "), "timelapse")
        self.assertEqual(web.m3u_title(None), "timelapse")


class TestDayPlaylist(IndexCase):
    """One file to review a whole day, instead of one per place."""

    def setUp(self):
        super().setUp()
        # A day with three places, deliberately in mixed case so the ordering
        # rule is visible.
        for name, size in (("Gate.20260123.mkv", 2_000_000),
                           ("workshop.20260123.mkv", 2_000_000),
                           ("Court180.20260123.mkv", 2_000_000),
                           ("Gate.20260124.mkv", 2_000_000)):
            (self.root / name).write_bytes(b"\0" * 4096)
        self.config = cfg(self.tmp, transfer={"enabled": True,
                                              "destination": str(self.root)})
        self.scan()

    def get(self, path, **kw):
        return request(path, self.config, self.index, **kw)

    def test_a_day_lists_every_place_from_that_day(self):
        status, _, body = self.get("/day/2026-01-23")
        self.assertEqual(status, 200)
        for name in ("Gate.20260123.mkv", "workshop.20260123.mkv",
                     "Court180.20260123.mkv"):
            self.assertIn(name, body)

    def test_it_does_not_leak_the_neighbouring_day(self):
        _, _, body = self.get("/day/2026-01-23")
        self.assertNotIn("Gate.20260124.mkv", body)

    def test_playlist_shape(self):
        _, head, body = self.get("/day/2026-01-23")
        self.assertIn("audio/x-mpegurl", head)
        lines = body.strip().splitlines()
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertEqual(lines[1], "#PLAYLIST:Timelapses 2026-01-23")
        # Then alternating #EXTINF / URL, three of each.
        self.assertEqual(len(lines), 2 + 6)
        for i in range(2, len(lines), 2):
            self.assertTrue(lines[i].startswith("#EXTINF:-1,"), lines[i])
            self.assertTrue(lines[i + 1].startswith("http://"), lines[i + 1])

    def test_ordered_case_insensitively_by_place(self):
        _, _, body = self.get("/day/2026-01-23")
        order = [l.split(",", 1)[1].split(" ")[0]
                 for l in body.splitlines() if l.startswith("#EXTINF")]
        self.assertEqual(order, ["Court180", "Gate", "workshop"])

    def test_filename_names_the_day(self):
        _, head, _ = self.get("/day/2026-01-23")
        self.assertIn('filename="timelapse-2026-01-23.m3u"', head)

    def test_urls_are_absolute_and_use_the_request_host(self):
        _, _, body = self.get("/day/2026-01-23")
        for line in body.splitlines():
            if line.startswith("http"):
                self.assertTrue(line.startswith("http://nas.local/video/"), line)

    def test_a_day_with_nothing_is_404(self):
        status, _, _ = self.get("/day/2019-01-01")
        self.assertEqual(status, 404)

    def test_a_bad_date_is_404_and_never_reaches_the_index(self):
        with mock.patch.object(self.index, "by_day") as by_day:
            for bad in ("2026-13-01", "notadate", "../../etc/passwd"):
                status, _, _ = self.get(f"/day/{bad}")
                self.assertEqual(status, 404, bad)
            by_day.assert_not_called()

    def test_a_file_deleted_since_the_scan_is_left_out(self):
        """A playlist is handed to a player that will not come back and ask
        again, so a dead URL in it is worse than a shorter list."""
        (self.root / "Gate.20260123.mkv").unlink()
        status, _, body = self.get("/day/2026-01-23")
        self.assertEqual(status, 200)
        self.assertNotIn("Gate.20260123.mkv", body)
        self.assertIn("workshop.20260123.mkv", body)

    def test_a_day_whose_files_have_all_gone_is_404(self):
        for name in ("Gate.20260123.mkv", "workshop.20260123.mkv",
                     "Court180.20260123.mkv"):
            (self.root / name).unlink()
        status, _, _ = self.get("/day/2026-01-23")
        self.assertEqual(status, 404)

    def test_day_view_offers_the_whole_day(self):
        _, _, body = request("/library?day=2026-01-23", self.config, self.index)
        self.assertIn('href="/day/2026-01-23"', body)
        self.assertIn("Gate.20260123.mkv", body)

    def test_day_view_rejects_nonsense(self):
        _, _, body = request("/library?day=notadate", self.config, self.index)
        self.assertIn("Not a date", body)

    def test_recent_days_are_listed_and_playable(self):
        _, _, body = request("/library", self.config, self.index)
        self.assertIn('href="/day/2026-01-23"', body)
        self.assertIn("Play the day", body)

    def test_day_cells_link_to_the_day(self):
        _, _, body = request("/library?camera=Gate", self.config, self.index)
        self.assertIn("/library?day=2026-01-23", body)


class TestLibraryLinks(IndexCase):

    def setUp(self):
        super().setUp()
        self.config = cfg(self.tmp, transfer={"enabled": True,
                                              "destination": str(self.root)})
        self.scan()

    def test_folder_view_offers_play_and_download(self):
        _, _, body = request("/library?folder=2024-01", self.config, self.index)
        self.assertIn("/play/2024-01/2024-01-01_Workshop.mp4", body)
        self.assertIn("?download=1", body)

    def test_both_reachable_addresses_are_shown(self):
        _, _, body = request("/library?folder=2024-01", self.config, self.index)
        self.assertIn(str(self.root), body)          # share path, for a mount
        self.assertIn("http://nas.local/video/", body)  # URL, for any player


class TestUpdateChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, enabled=True, current="0.0.9"):
        return web.UpdateChecker(self.tmp, enabled=enabled, current=current)

    def test_disabled_never_calls_out(self):
        # The one outbound connection this service makes, so "off" has to
        # mean off rather than "checked and discarded".
        c = self.make(enabled=False)
        with mock.patch.object(web, "latest_release") as call:
            c.refresh_if_stale()
            time.sleep(0.1)
        call.assert_not_called()

    def test_a_check_records_the_newer_version(self):
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "notes")):
            c._check()
        s = c.snapshot()
        self.assertTrue(s["available"])
        self.assertEqual(s["latest"], "0.1.0")
        self.assertEqual(s["tag"], "v0.1.0")
        self.assertEqual(s["url"], "u")

    def test_the_same_version_is_not_an_update(self):
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 0, 9), "v0.0.9", "u", "")):
            c._check()
        s = c.snapshot()
        self.assertFalse(s["available"])
        self.assertTrue(s["known"])

    def test_an_older_tag_is_not_an_update(self):
        # A local build ahead of the tags must not be told to downgrade.
        c = self.make(current="0.2.0")
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "")):
            c._check()
        self.assertFalse(c.snapshot()["available"])

    # -- a cache the installed version has overtaken ------------------------
    # Reported from the deployment: upgraded 0.1.3 to 0.1.4 from the terminal,
    # and the panel then read "Installed 0.1.4 / Latest 0.1.3" until the daily
    # check came round. Upstream cannot be behind what is installed here, so
    # the cached answer is out of date rather than a finding about GitHub.

    def test_an_overtaken_cache_reports_the_installed_version(self):
        c = self.make(current="0.1.3")
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 3), "v0.1.3", "u", "")):
            c._check()
        c.current, c.current_text = web.parse_version("0.1.4"), "0.1.4"
        s = c.snapshot()
        self.assertEqual(s["latest"], "0.1.4")
        self.assertFalse(s["available"])
        self.assertTrue(s["known"])

    def test_an_overtaken_cache_drops_the_older_release_link(self):
        # The tag and the URL name 0.1.3's release; kept, they would label a
        # link "0.1.4" and land the reader on the previous release's page.
        c = self.make(current="0.1.3")
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 3), "v0.1.3", "u", "")):
            c._check()
        c.current, c.current_text = web.parse_version("0.1.4"), "0.1.4"
        s = c.snapshot()
        self.assertEqual(s["tag"], "")
        self.assertEqual(s["url"], "")

    def test_the_cache_itself_is_not_rewritten(self):
        # Only the rendering is clamped. The file stays a record of what
        # GitHub actually said, so the next real check has something to
        # correct and "Last successful check" keeps meaning what it says.
        c = self.make(current="0.1.3")
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 3), "v0.1.3", "u", "")):
            c._check()
        c.current, c.current_text = web.parse_version("0.1.4"), "0.1.4"
        c.snapshot()
        self.assertEqual(c.state["latest"], "0.1.3")
        self.assertEqual(c.state["tag"], "v0.1.3")

    def test_a_newer_upstream_is_left_alone(self):
        c = self.make(current="0.1.4")
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 5), "v0.1.5", "u", "")):
            c._check()
        s = c.snapshot()
        self.assertEqual(s["latest"], "0.1.5")
        self.assertEqual(s["url"], "u")
        self.assertTrue(s["available"])

    def test_a_failure_does_not_count_as_a_check(self):
        """The reported bug. A momentary DNS failure during an upgrade set
        `checked`, so the daily cache gated the retry and the operator was
        told to wait a day over a blip that lasted seconds."""
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               side_effect=OSError("temporary failure")):
            c._check()
        s = c.snapshot()
        self.assertEqual(s["checked"], 0.0)      # nothing was checked
        self.assertGreater(s["attempted"], 0.0)  # but it was tried
        self.assertEqual(s["failures"], 1)

    def test_a_failure_retries_in_minutes_not_a_day(self):
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               side_effect=OSError("dns")):
            c._check()
        wait = c.snapshot()["retry_at"] - time.time()
        self.assertLess(wait, web.UPDATE_INTERVAL / 10)
        self.assertLessEqual(wait, web.UPDATE_RETRY + 1)

    def test_repeated_failures_back_off(self):
        # A host with no internet at all should settle at the daily rate
        # rather than asking every quarter of an hour forever.
        c = self.make()
        waits = []
        with mock.patch.object(web, "latest_release", side_effect=OSError("x")):
            for _ in range(4):
                c._check()
                waits.append(c.snapshot()["retry_at"] - c.state["attempted"])
        self.assertEqual(waits, sorted(waits))
        self.assertGreater(waits[-1], waits[0])

    def test_the_backoff_is_capped(self):
        c = self.make()
        c.state.update(failures=40, attempted=time.time())
        wait = c.snapshot()["retry_at"] - c.state["attempted"]
        self.assertLessEqual(wait, web.UPDATE_RETRY_MAX)

    def test_a_success_clears_the_failure_count(self):
        c = self.make()
        with mock.patch.object(web, "latest_release", side_effect=OSError("x")):
            c._check()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "n")):
            c._check()
        s = c.snapshot()
        self.assertEqual(s["failures"], 0)
        self.assertEqual(s["error"], "")
        self.assertGreater(s["checked"], 0.0)

    def test_a_failure_is_retried_on_a_later_view(self):
        c = self.make()
        with mock.patch.object(web, "latest_release", side_effect=OSError("x")):
            c._check()
        # Pretend the backoff has elapsed; the next page view must try again.
        c.state["attempted"] = time.time() - web.UPDATE_RETRY - 1
        done = threading.Event()
        with mock.patch.object(web, "latest_release",
                               lambda: (done.set(),
                                        ((0, 1, 0), "v0.1.0", "u", ""))[1]):
            c.refresh_if_stale()
            self.assertTrue(done.wait(5))

    def test_force_skips_the_backoff(self):
        # What the retry button is for: the operator has just fixed their DNS
        # and should not have to wait out a backoff they can see is stale.
        c = self.make()
        with mock.patch.object(web, "latest_release", side_effect=OSError("x")):
            c._check()
        done = threading.Event()
        with mock.patch.object(web, "latest_release",
                               lambda: (done.set(),
                                        ((0, 1, 0), "v0.1.0", "u", ""))[1]):
            self.assertFalse(c.refresh(force=False))   # still backed off
            self.assertTrue(c.refresh(force=True))
            self.assertTrue(done.wait(5))

    def test_force_does_nothing_when_disabled(self):
        # The button must not become a way around web.update_check: false.
        c = self.make(enabled=False)
        with mock.patch.object(web, "latest_release") as call:
            self.assertFalse(c.refresh(force=True))
            time.sleep(0.1)
        call.assert_not_called()

    def test_a_failure_is_recorded_not_raised(self):
        # Somebody else's outage must not reach the page as a 500.
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               side_effect=OSError("no route to host")):
            c._check()
        s = c.snapshot()
        self.assertIn("no route to host", s["error"])
        self.assertFalse(s["available"])

    def test_a_failure_keeps_the_last_good_answer(self):
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "n")):
            c._check()
        with mock.patch.object(web, "latest_release",
                               side_effect=OSError("dns")):
            c._check()
        s = c.snapshot()
        self.assertTrue(s["available"])       # still known to be behind
        self.assertIn("dns", s["error"])

    def test_the_result_survives_a_restart(self):
        # Without this, a service that restarts often would ask on every boot
        # and spend the 60-per-hour anonymous rate limit.
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "n")):
            c._check()
        again = self.make()
        self.assertEqual(again.snapshot()["latest"], "0.1.0")
        self.assertTrue(again.snapshot()["available"])

    def test_a_fresh_answer_is_not_rechecked(self):
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "n")):
            c._check()
        with mock.patch.object(web, "latest_release") as call:
            c.refresh_if_stale()
            time.sleep(0.1)
        call.assert_not_called()

    def test_a_stale_answer_is_rechecked(self):
        c = self.make()
        c.state["checked"] = time.time() - web.UPDATE_INTERVAL - 1
        done = threading.Event()

        def slow():
            done.set()
            return (0, 1, 0), "v0.1.0", "u", "n"

        with mock.patch.object(web, "latest_release", slow):
            c.refresh_if_stale()
            self.assertTrue(done.wait(5))

    def test_a_010_cache_written_by_a_failure_is_repaired(self):
        """0.1.0 set `checked` on failure too. Without repairing that on load,
        the fix never reaches the people who actually hit the bug: their
        cached file still claims a successful check a minute ago."""
        failed_at = time.time() - 60
        Path(self.tmp, "update.json").write_text(json.dumps({
            "checked": failed_at, "tag": "", "latest": "", "url": "",
            "notes": "", "error": "URLError: name resolution"}),
            encoding="utf-8")
        s = self.make().snapshot()
        self.assertEqual(s["checked"], 0.0)
        self.assertEqual(s["failures"], 1)
        # Minutes away, not a day.
        self.assertLess(s["retry_at"] - time.time(), web.UPDATE_RETRY + 1)

    def test_a_010_cache_from_a_success_is_left_alone(self):
        ok_at = time.time() - 60
        Path(self.tmp, "update.json").write_text(json.dumps({
            "checked": ok_at, "tag": "v0.1.0", "latest": "0.1.0",
            "url": "u", "notes": "n", "error": ""}), encoding="utf-8")
        s = self.make().snapshot()
        self.assertAlmostEqual(s["checked"], ok_at, places=3)
        self.assertEqual(s["failures"], 0)
        self.assertTrue(s["known"])

    def test_a_corrupt_cache_is_ignored_not_fatal(self):
        Path(self.tmp, "update.json").write_text("{ this is not json",
                                                 encoding="utf-8")
        self.assertEqual(self.make().snapshot()["latest"], "")

    def test_the_release_body_is_neither_stored_nor_fetched_twice(self):
        """The panel links to the release instead of reproducing it, so there
        is nothing here to store and nothing to make a second request for.

        The changelog fetch existed only to fill "what is new" when a tag had
        no Release behind it. Keeping it would mean this service reaching out
        twice to render a link it already has."""
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "")):
            c._check()
        s = c.snapshot()
        self.assertTrue(s["available"])
        self.assertNotIn("notes", s)
        self.assertNotIn("clipped", s)

    def test_the_next_successful_check_rewrites_the_file_without_them(self):
        # An old cache keeps its `notes` key until something rewrites the
        # file. Nothing reads it, so it is harmless, but the state file should
        # not carry a dead field for ever either.
        path = Path(self.tmp, "update.json")
        path.write_text(json.dumps({
            "checked": time.time(), "tag": "v0.1.3", "latest": "0.1.3",
            "url": "u", "notes": "## Fixed", "clipped": True, "error": ""}),
            encoding="utf-8")
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 4), "v0.1.4", "u2", "")):
            c._check()
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("notes", saved)
        self.assertNotIn("clipped", saved)
        self.assertEqual(saved["tag"], "v0.1.4")

    def test_a_cache_written_by_an_older_build_still_loads(self):
        # 0.1.0 to 0.1.3 stored `notes` and `clipped`. Those keys are simply
        # not copied out now; everything else in the file still is.
        ok_at = time.time() - 60
        Path(self.tmp, "update.json").write_text(json.dumps({
            "checked": ok_at, "attempted": ok_at, "failures": 0,
            "tag": "v0.1.3", "latest": "0.1.3", "url": "u",
            "notes": "- a change", "clipped": True, "error": ""}),
            encoding="utf-8")
        s = self.make().snapshot()
        self.assertEqual(s["tag"], "v0.1.3")
        self.assertTrue(s["known"])
        self.assertNotIn("notes", s)


class TestUpdatePanel(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})

    def checker(self, enabled=True, current="0.0.9", **state):
        # Pinned, not web.__version__: these describe what the panel renders
        # for a given pair of versions. Letting the installed version leak in
        # made three of them fail the moment 0.1.0 was cut, which would have
        # repeated at every release.
        c = web.UpdateChecker(None, enabled=enabled, current=current)
        c.state.update(state)
        return c

    def get(self, checker):
        return request("/", self.config, updates=checker)[2]

    def version_list(self, body):
        return body.split("<h2>Version</h2>", 1)[1].split("</dl>", 1)[0]

    def test_installed_and_latest_are_rendered_the_same_way(self):
        # Reported: one was a <code> and the other a bare string, so a
        # two-row list showed a version number in two different fonts and
        # read as a rendering fault. Colour is the only difference that
        # carries meaning here.
        dl = self.version_list(self.get(self.checker(
            checked=time.time(), tag="v0.0.9", latest="0.0.9")))
        self.assertNotIn("<code>", dl)
        self.assertIn("<dd>0.0.9</dd>", dl)

    def test_an_update_is_still_marked_out_by_colour(self):
        dl = self.version_list(self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            url="https://example/rel")))
        self.assertNotIn("<code>", dl)
        self.assertIn('class="ok"', dl)

    def test_an_available_update_shows_the_version_and_the_command(self):
        body = self.get(self.checker(checked=time.time(), tag="v0.1.0",
                                     latest="0.1.0", url="https://example/rel"))
        self.assertIn("An update is available", body)
        self.assertIn("0.1.0", body)
        self.assertIn("sudo timelapse update", body)
        self.assertIn("https://example/rel", body)

    # -- one shape for both version numbers ---------------------------------
    # Reported from the deployment: "Installed 0.1.4" beside "Latest v0.1.4".
    # The tag is what the repo wrote and the installed version is what
    # __version__ says; side by side in one two-row list the v reads as a
    # difference between the two values rather than as punctuation in one.

    def test_the_latest_version_is_printed_without_the_tag_v(self):
        dl = self.version_list(self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            url="https://example/rel")))
        self.assertIn("0.1.0", dl)
        self.assertNotIn("v0.1.0", dl)

    def test_the_two_versions_agree_on_shape_when_they_agree(self):
        dl = self.version_list(self.get(self.checker(
            checked=time.time(), tag="v0.0.9", latest="0.0.9")))
        self.assertEqual(dl.count("<dd>0.0.9</dd>"), 2)

    def test_the_update_sentence_uses_the_same_shape(self):
        # "You have 0.1.4; v0.1.5 is out" puts both spellings in one sentence.
        body = self.get(self.checker(checked=time.time(), tag="v0.1.0",
                                     latest="0.1.0", url="https://example/rel"))
        self.assertIn("You have 0.0.9; ", body)
        self.assertNotIn("v0.1.0", body)

    def test_a_cache_with_only_a_tag_falls_back_to_it(self):
        # Belt and braces: _check writes the two together, so this is only a
        # fallback for a cache that somehow has one without the other. Better
        # a v than a blank where a version should be.
        self.assertEqual(web.latest_label({"latest": "", "tag": "v0.1.0"}),
                         "v0.1.0")

    # -- the release notes are GitHub's job ---------------------------------
    # This panel used to reproduce the release body. It is markdown, and this
    # is not a markdown renderer, so `## Camera passwords` and backticked
    # commands appeared as-is and read as a formatting failure by this
    # program. Reported from the real deployment 2026-08-11. GitHub renders it
    # properly one click away, which makes the link the honest version.

    def test_the_panel_does_not_reproduce_the_release_body(self):
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            url="https://example/rel"))
        self.assertNotIn("What is new", body)
        self.assertNotIn("shortened", body)

    def test_it_links_to_the_release_instead(self):
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            url="https://example/rel"))
        self.assertIn("https://example/rel", body)
        self.assertIn("Read what changed", body)

    def test_a_link_that_leaves_the_ui_opens_in_its_own_tab(self):
        # Replacing the page somebody is reading with GitHub is the wrong
        # move; every other link here navigates inside this server.
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            url="https://example/rel"))
        for link in re.findall(r'<a [^>]*href="https://[^"]*"[^>]*>', body):
            self.assertIn('target="_blank"', link)
            # Without noopener the opened page can reach back through
            # window.opener; it costs nothing and is not optional.
            self.assertIn("noopener", link)

    def test_internal_links_stay_in_the_tab(self):
        body = self.get(self.checker(checked=time.time(), tag="v0.0.9",
                                     latest="0.0.9"))
        for link in re.findall(r'<a [^>]*href="/[^"]*"[^>]*>', body):
            self.assertNotIn("target=", link)

    def test_a_release_with_no_url_still_offers_somewhere_to_go(self):
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0", url=""))
        self.assertIn(web.RELEASES_URL, body)

    def test_up_to_date_says_so_without_commands(self):
        body = self.get(self.checker(checked=time.time(), tag="v0.0.9",
                                     latest="0.0.9"))
        self.assertIn("Up to date", body)
        self.assertNotIn("install_timelapse.sh", body)

    def test_a_terminal_upgrade_does_not_leave_the_panel_contradicting_itself(self):
        # The reported sequence: `sudo timelapse update` moves the installed
        # version, the cache still holds yesterday's answer, and the panel
        # reads "Installed 0.1.4 / Latest 0.1.3" for up to a day.
        body = self.get(self.checker(current="0.1.4", checked=time.time(),
                                     tag="v0.1.3", latest="0.1.3",
                                     url="https://example/rel"))
        dl = self.version_list(body)
        self.assertEqual(dl.count("<dd>0.1.4</dd>"), 2)
        self.assertNotIn("0.1.3", dl)
        self.assertIn("Up to date", body)
        # And it still says how old the answer is, which is the honest part.
        self.assertIn("Last successful check", body)

    def test_disabled_says_how_to_turn_it_on(self):
        body = self.get(self.checker(enabled=False))
        self.assertIn("timelapse web", body)
        self.assertNotIn("Checking GitHub", body)

    def test_a_failed_check_is_quiet_not_alarming(self):
        body = self.get(self.checker(checked=time.time(), attempted=time.time(),
                                     failures=1, error="Could not reach GitHub"))
        self.assertIn("Could not reach GitHub", body)
        self.assertIn("Nothing here is broken by this", body)

    def test_a_failure_offers_a_retry_and_says_when_it_would_try_anyway(self):
        # The reported bug: a momentary DNS failure left the panel sitting on
        # the error with no way to ask again short of waiting a day.
        body = self.get(self.checker(attempted=time.time(), failures=1,
                                     error="DNS lookup failed"))
        self.assertIn("/check-update", body)
        self.assertIn("Check now", body)
        self.assertIn("try again by itself in about", body)

    def test_a_failure_does_not_claim_to_have_checked(self):
        # It said "Checked 09:01" for an attempt that resolved nothing.
        body = self.get(self.checker(attempted=time.time(), failures=1,
                                     error="DNS lookup failed"))
        self.assertNotIn("Last successful check", body)

    def test_a_good_check_still_reports_when_it_happened(self):
        body = self.get(self.checker(checked=time.time(), attempted=time.time(),
                                     tag="v0.0.9", latest="0.0.9"))
        self.assertIn("Last successful check", body)
        self.assertIn("Check now", body)

    def test_a_check_in_flight_offers_no_retry_button(self):
        c = self.checker(attempted=time.time(), failures=1, error="boom")
        c._busy = True
        body = self.get(c)
        self.assertIn("Checking GitHub", body)
        self.assertNotIn("Check now", body)

    def test_the_page_says_where_it_connects_and_how_to_stop_it(self):
        body = self.get(self.checker(checked=time.time(), latest="0.0.9",
                                     tag="v0.0.9"))
        self.assertIn("only request this service makes to the internet", body)

    def test_a_check_in_flight_ships_the_poller(self):
        c = self.checker()
        c._busy = True
        body = self.get(c)
        self.assertIn('data-busy="1"', body)
        self.assertIn("Checking GitHub", body)
        self.assertIn("setInterval", body)

    def test_a_settled_panel_ships_no_poller(self):
        body = self.get(self.checker(checked=time.time(), latest="0.0.9"))
        self.assertIn('data-busy="0"', body)
        self.assertNotIn("setInterval", body)

    def test_the_retry_needs_a_post(self):
        # A GET link would let a prefetch or a refresh reach out to GitHub,
        # which is exactly what /rescan is a POST to avoid.
        status, _, _ = request("/check-update", self.config,
                               updates=self.checker())
        self.assertEqual(status, 404)

    def test_the_retry_post_forces_a_check_and_redirects(self):
        c = self.checker(attempted=time.time(), failures=1, error="dns")
        with mock.patch.object(c, "refresh") as refresh:
            status, head, _ = request("/check-update", self.config,
                                      method="POST", updates=c)
        self.assertEqual(status, 303)
        self.assertIn("Location: /", head)
        refresh.assert_called_once_with(force=True)

    def test_the_update_endpoint_returns_the_panel_alone(self):
        c = self.checker(checked=time.time(), tag="v0.1.0", latest="0.1.0")
        status, _, body = request("/update", self.config, updates=c)
        self.assertEqual(status, 200)
        self.assertIn('id="update"', body)
        self.assertIn("0.1.0", body)
        self.assertNotIn("setInterval", body)
        self.assertNotIn("<nav>", body)

    def test_the_overview_never_waits_on_the_network(self):
        # refresh_if_stale must hand off to a thread. If it ever blocks, this
        # page render blocks with it.
        c = self.checker()
        c.state["checked"] = 0.0
        started = threading.Event()
        release = threading.Event()

        def slow():
            started.set()
            release.wait(10)
            return (0, 1, 0), "v0.1.0", "u", ""

        with mock.patch.object(web, "latest_release", slow):
            t0 = time.time()
            body = self.get(c)
            elapsed = time.time() - t0
            self.assertTrue(started.wait(5), "the check never started")
            release.set()
        self.assertLess(elapsed, 2.0, "the overview blocked on the check")
        self.assertIn("Checking GitHub", body)


class TestEscape(unittest.TestCase):

    def test_escapes_markup(self):
        self.assertEqual(web.escape('<a href="x">&'),
                         "&lt;a href=&quot;x&quot;&gt;&amp;")

    def test_ampersand_first(self):
        # Escaping & after < would double-escape into &amp;lt;.
        self.assertEqual(web.escape("<"), "&lt;")

    def test_accepts_non_strings(self):
        self.assertEqual(web.escape(Path("/tmp/x")), str(Path("/tmp/x")))


class TestHandleError(unittest.TestCase):
    """A client disconnect is noise; a bug is not. socketserver's default
    prints a bare traceback to stderr for both, and journald tags stderr as an
    error, so closing VLC mid-playback looked exactly like a crash."""

    def setUp(self):
        # __new__ because handle_error touches nothing __init__ sets up, and a
        # real Server would bind a port.
        self.server = web.Server.__new__(web.Server)

    def _handle(self, exc):
        """Call handle_error with exc live, the way process_request_thread
        does: it reads sys.exc_info() rather than taking an argument."""
        with mock.patch.object(web, "log") as logger:
            try:
                raise exc
            except BaseException:
                self.server.handle_error(None, ("192.168.2.90", 14539))
        return logger

    def test_disconnects_are_debug_only(self):
        for exc in (ConnectionResetError(104, "Connection reset by peer"),
                    BrokenPipeError(32, "Broken pipe"),
                    ConnectionAbortedError(103, "Connection aborted"),
                    socket.timeout("timed out")):
            with self.subTest(exc=type(exc).__name__):
                logger = self._handle(exc)
                self.assertTrue(logger.debug.called)
                self.assertFalse(logger.exception.called)
                self.assertFalse(logger.warning.called)

    def test_real_errors_are_still_reported(self):
        logger = self._handle(ValueError("a genuine bug"))
        self.assertTrue(logger.exception.called)
        self.assertFalse(logger.debug.called)

    def test_disconnect_writes_nothing_to_stderr(self):
        # The reported symptom was a traceback in the journal, so assert on
        # the channel that produced it rather than only on the logger.
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            try:
                raise ConnectionResetError(104, "Connection reset by peer")
            except BaseException:
                self.server.handle_error(None, ("192.168.2.90", 14539))
        self.assertEqual(buf.getvalue(), "")


def with_login(user="ed", password="hunter2", fail_delay=0):
    """An Auth with a real credential, hashed cheaply and failing instantly.

    fail_delay defaults to 0 here: a wrong password costs three real seconds
    in the shipped code, and a dozen tests of failed logins would otherwise
    spend half a minute proving that sleep() sleeps. The delay itself is
    tested where it belongs, in TestFailedLoginDelay.
    """
    return web.Auth(user, web.hash_password(password, iters=1000),
                    fail_delay=fail_delay)


def cookie_header(token):
    return f"Cookie: {web.SESSION_COOKIE}={token}\r\n"


def set_cookie(head):
    """The Set-Cookie value from a response, or ""."""
    for line in head.split("\r\n"):
        if line.lower().startswith("set-cookie:"):
            return line.split(":", 1)[1].strip()
    return ""


def location(head):
    for line in head.split("\r\n"):
        if line.lower().startswith("location:"):
            return line.split(":", 1)[1].strip()
    return ""


class TestSafeNext(unittest.TestCase):
    """Where the login page is allowed to send you afterwards."""

    def test_a_local_path_survives(self):
        self.assertEqual(web.safe_next("/library?camera=Garage"),
                         "/library?camera=Garage")

    def test_nothing_becomes_the_home_page(self):
        for value in ("", None, "   "):
            with self.subTest(value=value):
                self.assertEqual(web.safe_next(value), "/")

    def test_another_site_is_refused(self):
        # An open redirect on a login page is the classic way to make a
        # phishing link look like it belongs to the host it names.
        for value in ("//evil.example/", "http://evil.example/",
                      "https://evil.example/", "javascript:alert(1)",
                      "\\\\evil.example\\share", "/\\evil.example"):
            with self.subTest(value=value):
                self.assertEqual(web.safe_next(value), "/")

    def test_a_header_injection_attempt_is_refused(self):
        # This value goes into a Location header.
        self.assertEqual(web.safe_next("/x\r\nSet-Cookie: a=b"), "/")
        self.assertEqual(web.safe_next("/x\nX: y"), "/")


class TestGate(unittest.TestCase):
    """Which routes need the session, and which cannot."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})
        self.auth = with_login()

    def get(self, path, headers="", method="GET"):
        return request(path, self.config, method=method, headers=headers,
                       auth=self.auth)

    def test_without_a_login_configured_nothing_changes(self):
        # Every existing install. The gate must be invisible until somebody
        # asks for it.
        status, _, body = request("/", self.config)
        self.assertEqual(status, 200)
        self.assertIn("Video library", body)

    def test_the_pages_need_a_session(self):
        for path, where in (("/", "/login"),
                            ("/library", "/login?next=%2Flibrary"),
                            ("/logs", "/login?next=%2Flogs"),
                            ("/status", "/login?next=%2Fstatus")):
            with self.subTest(path=path):
                status, head, _ = self.get(path)
                self.assertEqual(status, 303)
                self.assertEqual(location(head), where)

    def test_the_page_you_wanted_is_remembered(self):
        _, head, _ = self.get("/library?camera=Garage")
        self.assertEqual(location(head),
                         "/login?next=%2Flibrary%3Fcamera%3DGarage")

    def test_a_session_gets_you_in(self):
        status, _, body = self.get("/", cookie_header(
            self.auth.open_session()))
        self.assertEqual(status, 200)
        self.assertIn("Video library", body)

    def test_a_stale_or_forged_cookie_does_not(self):
        for value in ("nonsense", "", "a=b; c=d"):
            with self.subTest(cookie=value):
                status, _, _ = self.get("/", f"Cookie: {value}\r\n")
                self.assertEqual(status, 303)

    def test_a_malformed_cookie_header_is_not_an_error(self):
        # Some other application's cookie on the same host, badly formed.
        status, _, _ = self.get("/", "Cookie: =broken;;;\r\n")
        self.assertEqual(status, 303)

    def test_a_cookie_split_over_several_headers_still_works(self):
        # A proxy may do this, and taking only the first header would log
        # everybody behind it out.
        token = self.auth.open_session()
        headers = f"Cookie: other=1\r\nCookie: {web.SESSION_COOKIE}={token}\r\n"
        status, _, _ = self.get("/", headers)
        self.assertEqual(status, 200)

    def test_an_expired_session_does_not(self):
        auth = web.Auth("ed", web.hash_password("hunter2", iters=1000),
                        idle=0.05)
        token = auth.open_session()
        time.sleep(0.1)
        status, _, _ = request("/", self.config, headers=cookie_header(token),
                               auth=auth)
        self.assertEqual(status, 303)

    def test_a_logged_out_token_does_not(self):
        token = self.auth.open_session()
        self.auth.close_session(token)
        status, _, _ = self.get("/", cookie_header(token))
        self.assertEqual(status, 303)

    def test_head_is_gated_too(self):
        # do_HEAD is do_GET, so this is really a check that the gate sits
        # before the routing rather than inside one branch of it.
        status, head, _ = self.get("/", method="HEAD")
        self.assertEqual(status, 303)
        self.assertTrue(location(head).startswith("/login"))

    def test_the_pollers_are_refused_not_redirected(self):
        # A 303 here would have the poller fetch the login page and splice it
        # into the panel it is refreshing.
        for path in ("/scan", "/update"):
            with self.subTest(path=path):
                status, _, _ = self.get(path)
                self.assertEqual(status, 401)

    def test_the_actions_need_a_session(self):
        for path in ("/rescan", "/check-update"):
            with self.subTest(path=path):
                status, head, _ = request(path, self.config, method="POST",
                                          auth=self.auth)
                self.assertEqual(status, 303)
                self.assertEqual(location(head), "/login")

    def test_healthz_stays_open(self):
        # A monitor should not need a credential to ask whether the process
        # is alive, and the answer says nothing about anybody's videos.
        status, _, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body.strip(), "ok")

    def test_the_login_page_itself_is_reachable(self):
        status, _, body = self.get("/login")
        self.assertEqual(status, 200)
        self.assertIn('name="password"', body)

    def test_an_unknown_route_still_404s_once_you_are_in(self):
        status, _, _ = self.get("/nope", cookie_header(
            self.auth.open_session()))
        self.assertEqual(status, 404)

    def test_an_unknown_route_is_gated_before_it_404s(self):
        # Otherwise the 404 itself confirms which paths exist.
        status, _, _ = self.get("/nope")
        self.assertEqual(status, 303)


class TestVideoStaysOpen(IndexCase):
    """The deliberate hole, and the reason for it.

    VLC has no cookie jar, and the .m3u handoff is what the library page is
    for. A saved playlist that stopped working at the next logout would be a
    worse outcome than a video file that answers anyone who knows its exact
    path, which is the trade the operator chose knowingly.
    """

    def setUp(self):
        super().setUp()
        self.config = cfg(self.tmp, transfer={"enabled": True,
                                              "destination": str(self.root)})
        self.scan()

    def get(self, path, **kw):
        return request(path, self.config, self.index, auth=with_login(), **kw)

    def test_a_video_is_served_without_a_session(self):
        status, head, _ = self.get("/video/Gate.20260707.mkv")
        self.assertEqual(status, 200)
        self.assertIn("Content-Type: video/x-matroska", head)

    def test_a_range_request_works_without_one_too(self):
        # Seeking in VLC, which is the whole point of leaving this open.
        status, _, _ = self.get("/video/Gate.20260707.mkv",
                                headers="Range: bytes=0-99\r\n")
        self.assertEqual(status, 206)

    def test_a_playlist_still_needs_a_session(self):
        # The browser fetches this one, so it has the cookie. Keeping it
        # gated is free, and it is what holds the camera names and the day
        # groupings behind the login.
        status, head, _ = self.get("/play/Gate.20260707.mkv")
        self.assertEqual(status, 303)
        self.assertTrue(location(head).startswith("/login"))

    def test_a_day_playlist_needs_one_too(self):
        status, _, _ = self.get("/day/2026-07-07")
        self.assertEqual(status, 303)

    def test_the_playlist_url_is_one_vlc_can_actually_fetch(self):
        # The two halves of this decision have to agree, so this follows the
        # playlist the way VLC would: take the URL out of it, ask for that
        # path with no cookie, and expect the video rather than a login page.
        auth = with_login()
        _, _, playlist = request("/play/Gate.20260707.mkv", self.config,
                                 self.index, auth=auth,
                                 headers=cookie_header(auth.open_session()))
        urls = [ln for ln in playlist.splitlines() if ln.startswith("http")]
        self.assertEqual(len(urls), 1)
        path = "/" + urls[0].split("/", 3)[3]
        status, _, _ = request(path, self.config, self.index, auth=auth)
        self.assertEqual(status, 200)


class TestLoginForm(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})
        self.auth = with_login()

    def post(self, body, headers="", auth=None, client="127.0.0.1"):
        return request("/login", self.config, method="POST", body=body,
                       headers=headers, auth=auth or self.auth, client=client)

    def test_the_right_credentials_start_a_session(self):
        status, head, _ = self.post("username=ed&password=hunter2")
        self.assertEqual(status, 303)
        self.assertEqual(location(head), "/")
        self.assertIn(f"{web.SESSION_COOKIE}=", set_cookie(head))
        self.assertEqual(self.auth.session_count, 1)

    def test_the_cookie_it_sets_actually_works(self):
        # End to end rather than by inspection: the value it hands out must be
        # the value the gate accepts.
        _, head, _ = self.post("username=ed&password=hunter2")
        token = set_cookie(head).split(";")[0].split("=", 1)[1]
        status, _, _ = request("/", self.config, headers=cookie_header(token),
                               auth=self.auth)
        self.assertEqual(status, 200)

    def test_the_cookie_is_httponly_and_samesite(self):
        _, head, _ = self.post("username=ed&password=hunter2")
        cookie = set_cookie(head)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn(f"Max-Age={int(web.SESSION_IDLE)}", cookie)

    def test_the_cookie_is_not_marked_secure_over_plain_http(self):
        # Marked Secure, the browser would drop it on exactly the connection
        # this service actually serves, and the login would silently never
        # take.
        self.assertNotIn("Secure", set_cookie(
            self.post("username=ed&password=hunter2")[1]))

    def test_it_is_marked_secure_behind_a_tls_proxy(self):
        _, head, _ = self.post("username=ed&password=hunter2",
                               headers="X-Forwarded-Proto: https\r\n")
        self.assertIn("Secure", set_cookie(head))

    def test_a_wrong_password_gets_the_form_back(self):
        status, head, body = self.post("username=ed&password=wrong")
        self.assertEqual(status, 401)
        self.assertEqual(set_cookie(head), "")
        self.assertIn('name="password"', body)
        self.assertEqual(self.auth.session_count, 0)

    def test_the_error_does_not_say_which_half_was_wrong(self):
        # Telling a guest that the username is right narrows their next
        # attempt, and tells the household member nothing.
        _, _, wrong_pw = self.post("username=ed&password=wrong")
        _, _, wrong_user = self.post("username=nobody&password=hunter2")
        self.assertIn("did not match", wrong_pw)
        self.assertEqual(
            re.findall(r'<p class="bad">[^<]*</p>', wrong_pw),
            re.findall(r'<p class="bad">[^<]*</p>', wrong_user))

    def test_the_attempt_is_not_echoed_back_into_the_page(self):
        # A form that helpfully refills what you typed would put the password
        # in the HTML, and from there into a proxy log or a screenshot.
        _, _, body = self.post("username=ed&password=Sup3rS3cret")
        self.assertNotIn("Sup3rS3cret", body)

    def test_where_you_were_going_is_honoured(self):
        _, head, _ = self.post(
            "username=ed&password=hunter2&next=%2Flibrary%3Fcamera%3DGarage")
        self.assertEqual(location(head), "/library?camera=Garage")

    def test_a_hostile_next_is_not(self):
        _, head, _ = self.post(
            "username=ed&password=hunter2&next=https%3A%2F%2Fevil.example%2F")
        self.assertEqual(location(head), "/")

    def test_the_next_field_is_carried_through_a_failed_attempt(self):
        _, _, body = self.post("username=ed&password=wrong&next=%2Flogs")
        self.assertIn('value="/logs"', body)

    def test_a_missing_field_is_a_failed_login_not_a_crash(self):
        for body in ("", "username=ed", "password=hunter2", "x=y"):
            with self.subTest(body=body):
                status, _, _ = self.post(body)
                self.assertIn(status, (401, 429))

    def test_a_wrong_password_costs_three_seconds(self):
        # The one place the real delay is exercised through the handler; the
        # rest of this class uses an Auth that fails instantly.
        real = with_login(fail_delay=web.LOGIN_DELAY)
        with mock.patch.object(web.time, "sleep") as slept:
            status, _, _ = self.post("username=ed&password=wrong", auth=real)
        self.assertEqual(status, 401)
        slept.assert_called_once_with(3.0)

    def test_the_right_password_costs_nothing(self):
        real = with_login(fail_delay=web.LOGIN_DELAY)
        with mock.patch.object(web.time, "sleep") as slept:
            self.post("username=ed&password=hunter2", auth=real)
        slept.assert_not_called()

    def test_guesses_are_never_capped(self):
        # The point of the change: twenty wrong answers, and the twenty-first
        # attempt with the right one still works. Nobody gets locked out of
        # their own video index.
        for _ in range(20):
            status, _, _ = self.post("username=ed&password=wrong")
            self.assertEqual(status, 401)
        status, head, _ = self.post("username=ed&password=hunter2")
        self.assertEqual(status, 303)
        self.assertIn(f"{web.SESSION_COOKIE}=", set_cookie(head))

    def test_one_client_guessing_does_not_affect_another(self):
        for _ in range(6):
            self.post("username=ed&password=wrong", client="192.0.2.5")
        status, _, _ = self.post("username=ed&password=hunter2",
                                 client="192.0.2.9")
        self.assertEqual(status, 303)

    def test_the_form_says_what_it_does_and_does_not_protect(self):
        # This is a household lock, not a security control, and the page it
        # is on should not imply otherwise.
        _, _, body = request("/login", self.config, auth=self.auth)
        text = " ".join(body.split())
        self.assertIn("not encrypted", text)
        self.assertIn("video files themselves stay reachable", text)

    def test_the_login_page_has_no_navigation(self):
        # Every tab on it would bounce straight back here, which reads as a
        # broken page rather than a locked one.
        _, _, body = request("/login", self.config, auth=self.auth)
        self.assertNotIn("<nav>", body)

    def test_with_no_login_configured_the_form_is_not_offered(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                status, head, _ = request("/login", self.config,
                                          method=method,
                                          body="username=x&password=y")
                self.assertEqual(status, 303)
                self.assertEqual(location(head), "/")


class TestLogout(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})
        self.auth = with_login()

    def test_it_clears_the_cookie(self):
        token = self.auth.open_session()
        _, head, _ = request("/logout", self.config,
                             headers=cookie_header(token), auth=self.auth)
        self.assertIn("Max-Age=0", set_cookie(head))
        self.assertEqual(location(head), "/")

    def test_it_revokes_the_token_as_well(self):
        # A cookie copied out of the browser must stop working, which a
        # cleared cookie alone would not achieve.
        token = self.auth.open_session()
        request("/logout", self.config, headers=cookie_header(token),
                auth=self.auth)
        self.assertFalse(self.auth.valid(token))

    def test_it_leaves_the_other_browser_alone(self):
        phone, laptop = self.auth.open_session(), self.auth.open_session()
        request("/logout", self.config, headers=cookie_header(phone),
                auth=self.auth)
        self.assertTrue(self.auth.valid(laptop))

    def test_logging_out_without_a_session_is_harmless(self):
        status, _, _ = request("/logout", self.config, auth=self.auth)
        self.assertEqual(status, 303)

    def test_the_link_is_offered_while_logged_in(self):
        _, _, body = request("/", self.config, auth=self.auth,
                             headers=cookie_header(self.auth.open_session()))
        self.assertIn('href="/logout"', body)

    def test_no_link_when_there_is_no_login(self):
        # A control that does nothing is worse than no control.
        _, _, body = request("/", self.config)
        self.assertNotIn("/logout", body)

    def test_log_out_is_not_dressed_as_one_more_tab(self):
        """It sat beside "Recent log", sharing the word "log", and the
        operator hit it repeatedly while trying to open the log. It is the
        only control in that bar that is not a destination and the only one
        that is expensive to hit by accident, so it is spaced away from the
        tabs and coloured as an action."""
        _, _, body = request("/", self.config, auth=self.auth,
                             headers=cookie_header(self.auth.open_session()))
        self.assertIn('href="/logout" class="signout"', body)
        self.assertIn("nav a.signout", body)
        # Set apart from the tabs, and visibly not one of them.
        self.assertIn("margin-left: 3rem", body)
        self.assertIn("#8c1d18", body)

    def test_the_tabs_themselves_are_not_recoloured(self):
        # Only the action changes. A red tab bar would be worse than the
        # problem it fixes.
        _, _, body = request("/", self.config, auth=self.auth,
                             headers=cookie_header(self.auth.open_session()))
        nav = re.search(r"<nav>(.*?)</nav>", body, re.S).group(1)
        self.assertEqual(nav.count("signout"), 1)
        for tab in ("Overview", "Library", "Recent log"):
            self.assertIn(tab, nav)


class TestPostBody(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})

    def test_an_oversized_body_is_refused(self):
        # The only body this server reads. A length the client chose is not a
        # length to allocate on trust.
        status, _, _ = request("/login", self.config, method="POST",
                               body="x" * (web.FORM_LIMIT + 1),
                               auth=with_login())
        self.assertEqual(status, 413)

    def test_a_body_at_the_limit_is_still_read(self):
        pad = "x" * (web.FORM_LIMIT - len("username=ed&password=hunter2&p="))
        status, _, _ = request("/login", self.config, method="POST",
                               body=f"username=ed&password=hunter2&p={pad}",
                               auth=with_login())
        self.assertEqual(status, 303)

    def test_the_existing_actions_still_work_with_no_body(self):
        with mock.patch.object(web, "run_command", return_value=("", "")):
            status, head, _ = request("/check-update", self.config,
                                      method="POST")
        self.assertEqual(status, 303)
        self.assertEqual(location(head), "/")


class TestPasswordHash(unittest.TestCase):
    """The credential is verified here and never presented to anything, which
    is what makes hashing correct. The camera passwords are the opposite case
    and must stay in the clear; see redact_config."""

    # Every test here uses a low iteration count deliberately. The shipped
    # 600k is a defence against offline guessing, not a property under test,
    # and paying 0.1s per call would make this class the slowest in the suite.
    FAST = 1000

    def test_a_password_verifies_against_its_own_hash(self):
        h = web.hash_password("hunter2", iters=self.FAST)
        self.assertTrue(web.verify_password(h, "hunter2"))

    def test_a_wrong_password_does_not(self):
        h = web.hash_password("hunter2", iters=self.FAST)
        self.assertFalse(web.verify_password(h, "hunter3"))
        self.assertFalse(web.verify_password(h, ""))
        self.assertFalse(web.verify_password(h, "HUNTER2"))

    def test_the_hash_does_not_contain_the_password(self):
        self.assertNotIn("hunter2", web.hash_password("hunter2",
                                                      iters=self.FAST))

    def test_two_hashes_of_one_password_differ(self):
        # Per-hash salt: two installs with the same weak password must not
        # produce the same string, and a repeated one must not be a tell.
        a = web.hash_password("hunter2", iters=self.FAST)
        b = web.hash_password("hunter2", iters=self.FAST)
        self.assertNotEqual(a, b)
        self.assertTrue(web.verify_password(a, "hunter2"))
        self.assertTrue(web.verify_password(b, "hunter2"))

    def test_the_hash_carries_its_own_parameters(self):
        # What lets the iteration count rise later without locking anybody
        # out of a config written today.
        h = web.hash_password("hunter2", iters=4242)
        self.assertTrue(h.startswith("pbkdf2_sha256$4242$"))
        self.assertTrue(web.verify_password(h, "hunter2"))

    def test_a_unicode_password_survives_the_round_trip(self):
        h = web.hash_password("paßwort☃", iters=self.FAST)
        self.assertTrue(web.verify_password(h, "paßwort☃"))

    def test_a_malformed_hash_is_a_no_not_a_traceback(self):
        # This runs inside a request handler. A hand-edited config must give
        # a failed login, not a 500 on the page you log in from.
        for bad in ("", "x", "$$$", "pbkdf2_sha256$$$", "pbkdf2_sha256$a$b$c",
                    "pbkdf2_sha256$1000$!!!$!!!", "bcrypt$12$abc$def",
                    "pbkdf2_sha256$0$c2FsdA==$a2V5", None, 17, [],
                    "pbkdf2_sha256$1000$c2FsdA==$a2V5$extra"):
            with self.subTest(stored=bad):
                self.assertFalse(web.verify_password(bad, "hunter2"))
                self.assertIsNone(web.parse_password_hash(bad))

    def test_a_good_hash_parses(self):
        parsed = web.parse_password_hash(web.hash_password("x",
                                                           iters=self.FAST))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], self.FAST)


class TestAuthConfig(unittest.TestCase):

    def test_no_web_section_at_all_means_no_login(self):
        # Every install that predates this feature. It must behave exactly as
        # it did, which is the .get() rule doing its job.
        self.assertFalse(web.Auth.from_config({}).enabled)
        self.assertFalse(web.Auth.from_config({"web": {}}).enabled)
        self.assertFalse(web.Auth.from_config({"web": None}).enabled)

    def test_half_a_credential_is_not_a_login(self):
        # A username with no hash cannot be checked against anything, so
        # treating it as "on" would lock the page with no way in.
        for auth in ({"username": "ed"}, {"password_hash": "x"},
                     {"username": "", "password_hash": ""},
                     {"username": "  ", "password_hash": "  "}):
            with self.subTest(auth=auth):
                self.assertFalse(
                    web.Auth.from_config({"web": {"auth": auth}}).enabled)

    def test_a_configured_pair_is_a_login(self):
        auth = web.Auth.from_config({"web": {"auth": {
            "username": "ed",
            "password_hash": web.hash_password("hunter2", iters=1000)}}})
        self.assertTrue(auth.enabled)
        self.assertTrue(auth.check("ed", "hunter2"))

    def test_an_uncheckable_hash_refuses_to_start(self):
        # Fail closed, and loudly. Carrying on without the login would serve
        # the pages to everyone precisely because somebody asked for the
        # opposite, and doing it silently is how that goes unnoticed.
        with self.assertRaises(ValueError) as caught:
            web.Auth.from_config({"web": {"auth": {
                "username": "ed", "password_hash": "not-a-hash"}}})
        self.assertIn("timelapse web", str(caught.exception))

    def test_a_disabled_auth_never_accepts_anything(self):
        auth = web.Auth()
        self.assertFalse(auth.check("", ""))
        self.assertFalse(auth.check("ed", "hunter2"))


class TestAuthCheck(unittest.TestCase):

    def auth(self, user="ed", password="hunter2"):
        return web.Auth(user, web.hash_password(password, iters=1000))

    def test_the_right_pair_passes(self):
        self.assertTrue(self.auth().check("ed", "hunter2"))

    def test_either_half_wrong_fails(self):
        auth = self.auth()
        self.assertFalse(auth.check("ed", "wrong"))
        self.assertFalse(auth.check("nobody", "hunter2"))
        self.assertFalse(auth.check("nobody", "wrong"))

    def test_the_username_is_case_sensitive_and_exact(self):
        auth = self.auth()
        self.assertFalse(auth.check("ED", "hunter2"))
        self.assertFalse(auth.check("ed ", "hunter2"))
        self.assertFalse(auth.check("", "hunter2"))

    def test_none_is_treated_as_missing_not_as_a_crash(self):
        # A form can post a field with no value, and a client can omit it.
        auth = self.auth()
        self.assertFalse(auth.check(None, None))


class TestSessions(unittest.TestCase):

    def auth(self, idle=web.SESSION_IDLE):
        return web.Auth("ed", web.hash_password("hunter2", iters=1000),
                        idle=idle)

    def test_a_new_session_is_valid(self):
        auth = self.auth()
        self.assertTrue(auth.valid(auth.open_session()))

    def test_an_unknown_token_is_not(self):
        auth = self.auth()
        auth.open_session()
        self.assertFalse(auth.valid("something-else"))
        self.assertFalse(auth.valid(""))
        self.assertFalse(auth.valid(None))

    def test_tokens_are_unguessable_and_unique(self):
        auth = self.auth()
        tokens = {auth.open_session() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    def test_logout_revokes_the_token_not_just_the_cookie(self):
        # A token copied out of the browser must stop working too, which is
        # what makes this revocation rather than a cleared cookie.
        auth = self.auth()
        token = auth.open_session()
        auth.close_session(token)
        self.assertFalse(auth.valid(token))

    def test_logging_out_twice_is_not_an_error(self):
        auth = self.auth()
        token = auth.open_session()
        auth.close_session(token)
        auth.close_session(token)

    def test_one_logout_leaves_the_other_browser_alone(self):
        auth = self.auth()
        phone, laptop = auth.open_session(), auth.open_session()
        auth.close_session(phone)
        self.assertTrue(auth.valid(laptop))

    def test_an_idle_session_expires(self):
        auth = self.auth(idle=0.05)
        token = auth.open_session()
        time.sleep(0.1)
        self.assertFalse(auth.valid(token))

    def test_use_restarts_the_idle_clock(self):
        auth = self.auth(idle=0.3)
        token = auth.open_session()
        for _ in range(4):
            time.sleep(0.1)
            self.assertTrue(auth.valid(token))

    def test_expiry_is_measured_on_the_monotonic_clock(self):
        # A recorder without a battery-backed clock jumps at boot when NTP
        # lands, and an hours-long correction must not log everybody out.
        auth = self.auth()
        token = auth.open_session()
        with mock.patch.object(web.time, "time",
                               return_value=time.time() + 90 * 24 * 3600):
            self.assertTrue(auth.valid(token))

    def test_sessions_are_capped(self):
        # They only leave on logout or expiry, so something has to bound them.
        auth = self.auth()
        tokens = [auth.open_session() for _ in range(web.MAX_SESSIONS + 5)]
        self.assertLessEqual(auth.session_count, web.MAX_SESSIONS)
        # The newest survive; the oldest are the ones dropped.
        self.assertTrue(auth.valid(tokens[-1]))
        self.assertFalse(auth.valid(tokens[0]))

    def test_sessions_survive_concurrent_use(self):
        # ThreadingHTTPServer: every request runs on its own thread, and the
        # dict is shared.
        auth = self.auth()
        errors = []

        def hammer():
            try:
                for _ in range(50):
                    token = auth.open_session()
                    auth.valid(token)
                    auth.close_session(token)
            except Exception as exc:            # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])


class TestFailedLoginDelay(unittest.TestCase):
    """A wrong password costs three seconds and nothing else.

    No counter, no lockout, unlimited attempts: this gates a status page and a
    list of video files, and an account somebody has locked themselves out of
    is infuriating without being much of a defence.
    """

    def test_the_shipped_delay_is_three_seconds(self):
        self.assertEqual(web.LOGIN_DELAY, 3.0)
        self.assertEqual(web.Auth().fail_delay, 3.0)

    def test_a_failure_waits_before_answering(self):
        auth = web.Auth("ed", web.hash_password("x", iters=1000))
        with mock.patch.object(web.time, "sleep") as slept:
            auth.pause_after_failure()
        slept.assert_called_once_with(3.0)

    def test_the_wait_can_be_turned_off_for_tests(self):
        with mock.patch.object(web.time, "sleep") as slept:
            web.Auth(fail_delay=0).pause_after_failure()
        slept.assert_not_called()

    def test_there_is_no_lockout_state_to_get_stuck_in(self):
        # Deliberately pinned: the first implementation counted strikes per
        # address and locked out after five, which is exactly the behaviour
        # this replaced.
        auth = web.Auth("ed", web.hash_password("x", iters=1000))
        for name in ("locked_for", "record_failure", "record_success"):
            self.assertFalse(hasattr(auth, name), name)


class StateMixin:
    """A config whose state directory is a real, empty temp directory."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        Path(self.tmp, "videos").mkdir()
        self.state = Path(self.tmp) / "state"
        self.state.mkdir()
        self.config = cfg(self.tmp, transfer={"enabled": False})
        self.config["paths"]["state_dir"] = str(self.state)

    def publish(self, name, payload):
        (self.state / name).write_text(json.dumps(payload), encoding="utf-8")

    def capture(self, cameras=None, **kw):
        now = time.time()
        payload = {"version": 1, "kind": "capture", "running": True,
                   "paused": False, "pid": 1,
                   "started": web.datetime.datetime.fromtimestamp(
                       now - 3600).replace(microsecond=0).isoformat(),
                   "updated": web.datetime.datetime.fromtimestamp(
                       now).replace(microsecond=0).isoformat(),
                   "updated_epoch": int(now),
                   "cameras": cameras if cameras is not None else [
                       self.cam("Roof")]}
        payload.update(kw)
        self.publish("capture.json", payload)
        return payload

    def cam(self, name, quiet=0, **kw):
        """One HTTP camera entry, last seen `quiet` seconds before the snap."""
        seen = time.time() - quiet
        entry = {"name": name, "method": "http", "supervised": False,
                 "interval": 5, "framerate": 60, "ok": 1200, "fail": 0,
                 "retried": 0, "consec_fail": 0,
                 "last_attempt": web.datetime.datetime.fromtimestamp(
                     seen).replace(microsecond=0).isoformat(),
                 "last_success": web.datetime.datetime.fromtimestamp(
                     seen).replace(microsecond=0).isoformat()}
        entry.update(kw)
        return entry


class TestReadState(StateMixin, unittest.TestCase):

    def test_a_good_file_is_returned(self):
        self.publish("capture.json", {"version": 1, "cameras": []})
        data, problem = web.read_state(self.config, "capture.json")
        self.assertEqual(problem, "")
        self.assertEqual(data["cameras"], [])

    def test_a_missing_file_is_a_sentence_not_a_fault(self):
        # What every install shows between upgrading and restarting.
        data, problem = web.read_state(self.config, "capture.json")
        self.assertIsNone(data)
        self.assertIn("nothing has been published", problem)

    def test_malformed_json_says_so(self):
        (self.state / "capture.json").write_text("{ nope", encoding="utf-8")
        data, problem = web.read_state(self.config, "capture.json")
        self.assertIsNone(data)
        self.assertIn("not valid JSON", problem)

    def test_a_list_is_not_the_shape_this_expects(self):
        self.publish("capture.json", [1, 2, 3])
        _, problem = web.read_state(self.config, "capture.json")
        self.assertIn("shape", problem)

    def test_a_newer_format_is_refused_rather_than_guessed_at(self):
        """The units restart on upgrade and this service may not have.

        Reading a format this build has never seen would put invented numbers
        on a page whose entire value is being true.
        """
        self.publish("capture.json", {"version": web.STATE_VERSION + 1,
                                      "cameras": []})
        data, problem = web.read_state(self.config, "capture.json")
        self.assertIsNone(data)
        self.assertIn("newer version", problem)
        self.assertIn("timelapse-web", problem)

    def test_the_same_version_is_fine(self):
        self.publish("capture.json", {"version": web.STATE_VERSION,
                                      "cameras": []})
        _, problem = web.read_state(self.config, "capture.json")
        self.assertEqual(problem, "")

    def test_it_reads_the_daemons_directory_not_the_web_index(self):
        # Two different directories whose config keys are both "state_dir".
        self.config["web"] = {"state_dir": str(Path(self.tmp) / "webidx")}
        self.publish("capture.json", {"version": 1, "cameras": []})
        _, problem = web.read_state(self.config, "capture.json")
        self.assertEqual(problem, "")


class TestCameraVerdict(unittest.TestCase):
    """Judging happens here, not in the daemon: what counts as quiet depends
    on the camera's interval, and a file that had already decided could not be
    overruled by a reader that knows better."""

    def verdict(self, quiet, **kw):
        snap = 1_800_000_000
        cam = {"name": "Roof", "interval": 5, "supervised": False,
               "last_success": web.datetime.datetime.fromtimestamp(
                   snap - quiet).isoformat()}
        cam.update(kw)
        return web.camera_verdict(cam, snap)

    def test_a_recent_frame_is_fine(self):
        cls, phrase = self.verdict(3)
        self.assertEqual(cls, "ok")
        self.assertIn("ago", phrase)

    def test_a_long_silence_is_flagged(self):
        cls, _ = self.verdict(600)
        self.assertEqual(cls, "bad")

    def test_a_slow_camera_is_not_judged_by_a_fast_cameras_clock(self):
        # At one frame a minute, 90 seconds of silence is one missed tick.
        self.assertEqual(self.verdict(90, interval=60)[0], "ok")
        # The same 90 seconds at a five-second cadence is eighteen.
        self.assertEqual(self.verdict(90, interval=5)[0], "bad")

    def test_a_single_slow_fetch_does_not_paint_the_row_red(self):
        # A 5s camera seen 12s ago has missed at most two ticks, and the
        # heartbeat itself only lands once a minute.
        self.assertEqual(self.verdict(12, interval=5)[0], "ok")

    def test_a_camera_that_has_never_answered_says_so(self):
        cls, phrase = self.verdict(0, last_success=None)
        self.assertEqual(cls, "warn")
        self.assertIn("no frame yet", phrase)

    def test_an_rtsp_camera_is_judged_on_its_process(self):
        # Judged on the grabber process, because there is no last-frame time
        # to judge, but said in the reader's terms rather than by naming the
        # program that does the work.
        cls, phrase = self.verdict(0, supervised=True, alive=True)
        self.assertEqual(cls, "ok")
        self.assertEqual(phrase, "recording")
        cls, phrase = self.verdict(0, supervised=True, alive=False)
        self.assertEqual(cls, "bad")
        self.assertEqual(phrase, "not recording")

    def test_silence_is_measured_against_the_snapshot_not_now(self):
        """The heartbeat is a minute old by the time anyone reads it.

        Measuring against now would add the file's own age to every camera and
        make a healthy 5-second camera look a minute quiet.
        """
        snap = time.time() - 59
        cam = {"interval": 5, "supervised": False,
               "last_success": web.datetime.datetime.fromtimestamp(
                   snap - 2).isoformat()}
        self.assertEqual(web.camera_verdict(cam, snap)[0], "ok")
        self.assertAlmostEqual(web.silence_seconds(cam, snap), 2, delta=1)


class TestHumanAge(unittest.TestCase):

    def test_scales(self):
        self.assertEqual(web.human_age(0), "0s")
        self.assertEqual(web.human_age(45), "45s")
        self.assertEqual(web.human_age(90), "1m")
        self.assertEqual(web.human_age(3600), "1h 0m")
        self.assertEqual(web.human_age(90000), "1d 1h")


class TestCameraPanel(StateMixin, unittest.TestCase):

    def body(self):
        status, _, body = request("/", self.config)
        self.assertEqual(status, 200)
        return " ".join(body.split())

    def test_the_panel_appears_with_a_row_per_camera(self):
        self.capture([self.cam("Roof"), self.cam("Gate")])
        body = self.body()
        self.assertIn("Cameras", body)
        self.assertIn("Roof", body)
        self.assertIn("Gate", body)

    def test_missing_state_explains_itself_and_does_not_break_the_page(self):
        body = self.body()
        self.assertIn("nothing has been published", body)
        self.assertIn("0.1.6", body)

    def test_a_paused_daemon_is_shouted_about(self):
        """systemd calls a paused daemon active (running). It is capturing
        nothing, and that is the one case the unit table gets actively wrong."""
        self.capture(paused=True)
        body = self.body()
        self.assertIn("PAUSED", body)
        self.assertIn("min_free_gb", body)

    def test_a_stopped_daemon_says_so_before_listing_quiet_cameras(self):
        self.capture(running=False)
        body = self.body()
        self.assertIn("stopped cleanly", body)

    def test_a_stale_heartbeat_is_reported_as_stale(self):
        state = self.capture()
        state["updated_epoch"] = int(time.time() - 3600)
        self.publish("capture.json", state)
        body = self.body()
        self.assertIn("Last heartbeat", body)
        self.assertIn("wedged", body)

    def test_a_fresh_heartbeat_says_nothing_about_staleness(self):
        self.capture()
        self.assertNotIn("Last heartbeat", self.body())

    def test_failures_are_shown(self):
        self.capture([self.cam("Roof", ok=17280, fail=3, consec_fail=2)])
        body = self.body()
        self.assertIn("3 failed fetch(es)", body)
        self.assertIn("2 in a row", body)

    def test_the_restart_counter_is_not_shown_as_a_frame_count(self):
        # The daemon's own "ok" counter resets whenever capture restarts, so
        # it answered a question nobody asked. It is gone from the table.
        self.capture([self.cam("Roof", ok=17280)])
        self.assertNotIn("17,280", self.body())

    def test_an_rtsp_camera_shows_restarts_and_plain_language(self):
        self.capture([self.cam("Gate", supervised=True, alive=True,
                               restarts=4, last_success=None)])
        body = self.body()
        self.assertIn("4 restart(s)", body)
        self.assertIn("recording", body)
        # Naming the program doing the work is an implementation detail, and
        # the reader of a status page has not asked what ffmpeg is.
        self.assertNotIn("ffmpeg", body)

    def test_an_rtsp_camera_that_is_not_running_says_so_plainly(self):
        self.capture([self.cam("Gate", supervised=True, alive=False,
                               restarts=9, last_success=None)])
        self.assertIn("not recording", self.body())

    def test_no_enabled_cameras_is_a_sentence(self):
        self.capture([])
        self.assertIn("No cameras are enabled", self.body())

    def test_the_page_says_where_the_numbers_come_from(self):
        self.capture()
        body = self.body()
        self.assertIn("counted from the files on disk", body)
        self.assertIn("since midnight", body)


class TestTodaysCoverage(StateMixin, unittest.TestCase):
    """Frames and coverage are counted on disk, not taken from the daemon.

    The daemon's counter resets on restart, so "48 frames" after a restart at
    17:55 says nothing about whether today has been captured. It also does not
    exist at all for RTSP cameras, where a separate process writes the frames.
    The directory is the record for both.
    """

    def setUp(self):
        super().setUp()
        web._counts.clear()
        self.addCleanup(web._counts.clear)
        self.frames = Path(self.config["paths"]["frames_root"])

    def day_dir(self, camera, when=None):
        when = when or web.datetime.datetime.now()
        d = self.frames / camera / when.strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_frames(self, camera, n, cadence=None):
        d = self.day_dir(camera)
        for i in range(n):
            (d / f"{i:06d}.jpg").write_bytes(b"\xff\xd8x")
        if cadence:
            (d / ".cadence.json").write_text(
                json.dumps({"interval_seconds": cadence, "framerate": 60}),
                encoding="utf-8")
        return d

    def body(self):
        _, _, body = request("/", self.config)
        return body

    def test_frames_on_disk_are_counted(self):
        self.write_frames("Roof", 42)
        self.capture([self.cam("Roof")])
        self.assertIn("42", self.body())

    def test_a_camera_with_no_directory_today_counts_zero(self):
        # "No directory" and "an empty directory" are the same thing to an
        # operator, and both mean nothing has been captured today.
        self.capture([self.cam("Roof")])
        frames, cov = web.today_progress(self.config, {"name": "Roof",
                                                       "interval": 5})
        self.assertEqual(frames, 0)
        self.assertEqual(cov, 0.0)

    def test_nothing_captured_reads_as_zero_percent_not_a_dash(self):
        # The case an operator most needs stated plainly. A dash invites
        # being read as "not measured".
        self.capture([self.cam("Roof")])
        body = self.body()
        self.assertIn("0%", body)

    def test_the_cadence_reads_as_a_rate_not_as_a_fraction(self):
        """"1 / 5s" was read as one fifth of a second as readily as as one
        frame every five seconds, and those are two very different cameras.
        Reported by the operator 2026-08-14. Naming the unit fixes it: a rate
        cannot be read backwards."""
        self.capture([self.cam("Roof", interval=5)])
        body = self.body()
        self.assertIn("5s / frame", body)
        self.assertNotIn("1 / 5s", body)

    def test_a_slow_camera_reads_the_same_way(self):
        self.capture([self.cam("Roof", interval=60)])
        self.assertIn("60s / frame", self.body())

    def test_a_healthy_row_has_an_empty_problems_column(self):
        # A column that says "0 failed" on every healthy row trains the eye
        # to skip it, which is the opposite of what it is for.
        self.write_frames("Roof", 10)
        self.capture([self.cam("Roof", fail=0, consec_fail=0)])
        self.assertNotIn("failed fetch(es)", self.body())

    def test_an_rtsp_camera_is_counted_the_same_way(self):
        # The whole point: this column used to read "-" for RTSP, because the
        # daemon cannot see frames another process writes.
        self.write_frames("Gate", 17)
        self.capture([self.cam("Gate", supervised=True, alive=True,
                               last_success=None)])
        body = self.body()
        self.assertIn("17", body)
        self.assertNotIn("<td class=\"num dim\">-</td>", body)

    def test_coverage_is_measured_against_elapsed_time_not_the_whole_day(self):
        now = web.datetime.datetime.now().replace(hour=6, minute=0, second=0,
                                                  microsecond=0)
        # Six hours at one frame every 5s is 4,320 frames for a full house.
        frames, cov = web.today_progress(
            self.config, {"name": "Roof", "interval": 5}, now=now)
        self.assertEqual(frames, 0)
        self.write_frames("Roof", 0)
        d = self.day_dir("Roof")
        for i in range(4320):
            (d / f"{i:06d}.jpg").write_bytes(b"\xff\xd8x")
        web._counts.clear()
        _, cov = web.today_progress(self.config, {"name": "Roof",
                                                  "interval": 5}, now=now)
        self.assertEqual(cov, 100.0)

    def test_half_the_expected_frames_reads_as_half_coverage(self):
        now = web.datetime.datetime.now().replace(hour=1, minute=0, second=0,
                                                  microsecond=0)
        self.write_frames("Roof", 360)          # 720 expected in one hour
        _, cov = web.today_progress(self.config, {"name": "Roof",
                                                  "interval": 5}, now=now)
        self.assertEqual(cov, 50.0)

    def test_the_days_recorded_cadence_beats_the_running_config(self):
        # A cadence edit takes effect at midnight, so today may still be
        # running on yesterday's answer. Measuring against the new one would
        # report a perfect day as a near-outage, which is the same bug the
        # nightly summary already had.
        now = web.datetime.datetime.now().replace(hour=1, minute=0, second=0,
                                                  microsecond=0)
        self.write_frames("Roof", 60, cadence=60)   # 60 expected in one hour
        _, cov = web.today_progress(self.config,
                                    {"name": "Roof", "interval": 5}, now=now)
        self.assertEqual(cov, 100.0)

    def test_an_unreadable_directory_is_a_question_mark_not_a_zero(self):
        with mock.patch.object(web.os, "scandir",
                               side_effect=PermissionError("nope")):
            web._counts.clear()
            frames, cov = web.today_progress(self.config, {"name": "Roof",
                                                           "interval": 5})
        self.assertIsNone(frames)
        self.assertIsNone(cov)

    def test_the_count_is_cached_so_refreshing_does_not_walk_the_disk(self):
        d = self.write_frames("Roof", 3)
        self.assertEqual(web.count_frames(d), 3)
        (d / "999999.jpg").write_bytes(b"\xff\xd8x")
        self.assertEqual(web.count_frames(d), 3)    # served from the cache
        web._counts.clear()
        self.assertEqual(web.count_frames(d), 4)

    def test_only_jpegs_are_counted(self):
        d = self.write_frames("Roof", 2)
        (d / ".cadence.json").write_text("{}", encoding="utf-8")
        (d / ".encoded.json").write_text("{}", encoding="utf-8")
        web._counts.clear()
        self.assertEqual(web.count_frames(d), 2)


class TestLastEncodePanel(StateMixin, unittest.TestCase):

    def run_payload(self, **kw):
        run = {"started": "2026-08-12T00:05:00",
               "finished": "2026-08-12T00:27:56", "seconds": 1376.0,
               "encoder": "AV1 (av1_nvenc)", "error": "",
               "ok": 1, "skipped": 0, "failed": 0, "bytes": 4200000000,
               "transfer": {"ok": True, "moved": 1, "detail": ""},
               "days": [{"camera": "Roof", "date": "2026-08-11",
                         "status": "OK", "frames": 17280, "bad": 0,
                         "size": 4200000000, "seconds": 1376.0,
                         "interval": 5, "coverage": 100.0, "note": ""}]}
        run.update(kw)
        self.publish("encode.json", {"version": 1, "kind": "encode",
                                     "updated": "2026-08-12T00:27:56",
                                     "updated_epoch": 1786000000,
                                     "runs": [run]})
        return run

    def body(self):
        status, _, body = request("/", self.config)
        self.assertEqual(status, 200)
        return " ".join(body.split())

    def test_the_last_run_is_summarised(self):
        self.run_payload()
        body = self.body()
        self.assertIn("Last encode", body)
        self.assertIn("2026-08-12T00:27:56", body)
        self.assertIn("AV1 (av1_nvenc)", body)
        self.assertIn("1 video(s)", body)

    def test_the_days_table_carries_coverage(self):
        self.run_payload()
        body = self.body()
        self.assertIn("Roof", body)
        self.assertIn("2026-08-11", body)
        self.assertIn("100%", body)
        self.assertIn("17,280", body)

    def test_a_failed_transfer_is_visible(self):
        self.run_payload(transfer={"ok": False, "moved": 0,
                                   "detail": "exit 23: chgrp failed"})
        self.assertIn("exit 23", self.body())

    def test_a_run_with_no_transfer_does_not_look_like_a_failed_one(self):
        self.run_payload(transfer=None)
        body = self.body()
        self.assertIn("not attempted", body)
        self.assertNotIn("failed:", body)

    def test_an_aborted_run_is_not_shown_as_a_quiet_night(self):
        # Zero videos and zero failures is what a crash before the first
        # encode looks like from the counters alone.
        self.run_payload(error="No usable encoder found", ok=0, days=[],
                         encoder="")
        body = self.body()
        self.assertIn("Aborted", body)
        self.assertIn("No usable encoder", body)

    def test_a_run_that_found_nothing_says_that_plainly(self):
        self.run_payload(ok=0, days=[], bytes=0)
        self.assertIn("found nothing to do", self.body())

    def test_missing_state_explains_itself(self):
        self.assertIn("nothing has been published", self.body())

    def test_only_the_newest_run_is_rendered(self):
        # The file keeps a fortnight; the panel is about last night.
        self.publish("encode.json", {
            "version": 1, "kind": "encode", "runs": [
                {"finished": "2026-08-12T00:27:56", "seconds": 1,
                 "encoder": "", "error": "", "ok": 0, "skipped": 0,
                 "failed": 0, "bytes": 0, "transfer": None,
                 "days": [{"camera": "Newest", "date": "2026-08-11",
                           "status": "OK", "frames": 1, "bad": 0, "size": 1,
                           "seconds": 1, "interval": 5, "coverage": 1.0,
                           "note": ""}]},
                {"finished": "2026-08-11T00:27:56", "seconds": 1,
                 "encoder": "", "error": "", "ok": 0, "skipped": 0,
                 "failed": 0, "bytes": 0, "transfer": None,
                 "days": [{"camera": "Older", "date": "2026-08-10",
                           "status": "OK", "frames": 1, "bad": 0, "size": 1,
                           "seconds": 1, "interval": 5, "coverage": 1.0,
                           "note": ""}]}]})
        body = self.body()
        self.assertIn("Newest", body)
        self.assertNotIn("Older", body)


class TestIPv6Bind(unittest.TestCase):
    """The server must open the family its bind address names.

    ThreadingHTTPServer's address_family is AF_INET, so before this the
    service exited on an IPv6 bind while check_bind() in the wizard, which
    walks every family getaddrinfo() returns, had already called it usable.
    A check that passes what the service refuses is worse than no check.
    """

    def serve(self, bind):
        """A real listening socket, closed on teardown. Port 0 lets the
        kernel choose, so this cannot collide with anything."""
        cfg = {"web": {"enabled": True, "bind": bind, "port": 0},
               "paths": {}, "cameras": []}
        srv = web.Server((bind, 0), web.Handler, cfg, None)
        self.addCleanup(srv.server_close)
        return srv

    def test_an_ipv4_bind_opens_an_ipv4_socket(self):
        self.assertEqual(self.serve("127.0.0.1").address_family,
                         socket.AF_INET)

    def test_an_ipv6_bind_opens_an_ipv6_socket(self):
        srv = self.serve("::1")
        self.assertEqual(srv.address_family, socket.AF_INET6)
        self.assertEqual(srv.socket.family, socket.AF_INET6)

    def test_an_ipv6_server_actually_listens(self):
        # The point of the fix: it binds, rather than raising at startup.
        srv = self.serve("::1")
        host, port = srv.socket.getsockname()[:2]
        self.assertEqual(port, srv.server_address[1])
        conn = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        self.addCleanup(conn.close)
        conn.settimeout(5)
        conn.connect((host, port))          # raises if nothing is listening

    def test_the_ipv6_wildcard_accepts_ipv4_too(self):
        # IPV6_V6ONLY is set explicitly rather than inherited from
        # net.ipv6.bindv6only, so "listen on ::" means the same on any host.
        srv = self.serve("::")
        self.assertEqual(
            srv.socket.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY), 0)


class TestIPv6InEmittedURLs(IndexCase):
    """The bind address reaches a playlist, and must be bracketed there.

    _base_url() prefers the request's own Host header, which HOST_RE already
    accepts in bracketed form. The fallback is the config, and that is the
    path that produced http://::1:8787/video/... which no player can open.
    A .m3u that does not play arrives as a bug report about video.
    """

    def setUp(self):
        super().setUp()
        self.config = cfg(self.tmp, transfer={"enabled": True,
                                              "destination": str(self.root)})
        self.scan()

    def test_an_ipv6_bind_is_bracketed_in_the_playlist_fallback(self):
        config = dict(self.config)
        config["web"] = {"bind": "::1", "port": 8787}
        _, _, body = request("/play/Gate.20260707.mkv", config, self.index,
                             headers="Host: bad host\r\n")
        self.assertIn("http://[::1]:8787/video/", body)
        self.assertNotIn("http://::1:8787/", body)

    def test_a_bracketed_host_header_is_still_preferred(self):
        _, _, body = request("/play/Gate.20260707.mkv", self.config,
                             self.index, headers="Host: [fdd2::1]:8787\r\n")
        self.assertIn("http://[fdd2::1]:8787/video/", body)


if __name__ == "__main__":
    unittest.main()

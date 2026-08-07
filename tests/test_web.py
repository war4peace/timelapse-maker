"""Unit tests for timelapse_web.py — library resolution and request routing.

No sockets are opened. The handler is exercised through a fake request rather
than a live server: the routing and the escaping are the parts worth pinning,
and binding a port in a unit test invites flakiness on a CI runner.
"""

import io
import shutil
import sys
import tempfile
import unittest
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
    def __init__(self, config):
        self.cfg = config


def request(path, config):
    """Drive one GET through the real handler and return (status, body)."""
    raw = f"GET {path} HTTP/1.1\r\nHost: nas.local\r\nConnection: close\r\n\r\n"
    req = FakeRequest(raw.encode())
    handler = web.Handler.__new__(web.Handler)
    handler.log_message = lambda *a, **k: None
    handler.rfile = None
    # BaseHTTPRequestHandler does all its work from __init__.
    web.Handler.__init__(handler, req, ("127.0.0.1", 5555), FakeServer(config))
    out = bytes(req.sent).decode("utf-8", "replace")
    head, _, body = out.partition("\r\n\r\n")
    status = int(head.split()[1])
    return status, head, body


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

    def test_nothing_runs_unless_that_page_is_asked_for(self):
        # Status is on request only: the overview must not shell out.
        with mock.patch.object(web, "run_command") as run:
            request("/", self.config)
            request("/healthz", self.config)
        run.assert_not_called()


class TestEscape(unittest.TestCase):

    def test_escapes_markup(self):
        self.assertEqual(web.escape('<a href="x">&'),
                         "&lt;a href=&quot;x&quot;&gt;&amp;")

    def test_ampersand_first(self):
        # Escaping & after < would double-escape into &amp;lt;.
        self.assertEqual(web.escape("<"), "&lt;")

    def test_accepts_non_strings(self):
        self.assertEqual(web.escape(Path("/tmp/x")), str(Path("/tmp/x")))


if __name__ == "__main__":
    unittest.main()

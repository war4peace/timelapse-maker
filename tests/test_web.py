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
    def __init__(self, config, index=None, updates=None):
        self.cfg = config
        self.index = index
        # Disabled by default, and that is not incidental: an enabled checker
        # would have the overview page reaching api.github.com from the test
        # suite. Tests that want the panel populated set the state directly.
        self.updates = updates or web.UpdateChecker(None, enabled=False)


def request_bytes(path, config, index=None, method="GET", headers="",
                  updates=None):
    """Drive one request through the real handler; body stays bytes.

    Extra headers go first, and Host is only defaulted when they do not supply
    one: a duplicate Host would be silently ignored by the header parser, which
    quietly made an early version of the forged-Host test pass for no reason.
    """
    extra = headers or ""
    host = "" if "host:" in extra.lower() else "Host: nas.local\r\n"
    raw = (f"{method} {path} HTTP/1.1\r\n{extra}{host}"
           f"Content-Length: 0\r\nConnection: close\r\n\r\n")
    req = FakeRequest(raw.encode())
    handler = web.Handler.__new__(web.Handler)
    handler.log_message = lambda *a, **k: None
    handler.rfile = None
    # BaseHTTPRequestHandler does all its work from __init__.
    web.Handler.__init__(handler, req, ("127.0.0.1", 5555),
                         FakeServer(config, index, updates))
    out = bytes(req.sent)
    head, _, body = out.partition(b"\r\n\r\n")
    head = head.decode("latin-1")
    return int(head.split()[1]), head, body


def request(path, config, index=None, method="GET", headers="", updates=None):
    """As request_bytes, with the body decoded for HTML assertions."""
    status, head, body = request_bytes(path, config, index, method, headers,
                                       updates)
    return status, head, body.decode("utf-8", "replace")


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

    def test_output_pages_ask_for_the_whole_window(self):
        # A journal line is as wide as journald decided, so the 54rem reading
        # column that suits prose and tables is the wrong frame for it. The
        # stylesheet keys off these two classes; without them the pane went
        # back to a fixed width with its scrollbar far below the fold.
        for path in ("/status", "/logs"):
            with self.subTest(path=path):
                with mock.patch.object(web, "run_command",
                                       return_value=("a line", "")):
                    _, _, body = request(path, self.config)
                self.assertIn('<body class="pane-page">', body)
                self.assertIn('<section class="pane">', body)

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
                               return_value=("", "systemctl is not installed.")):
            _, _, body = request("/status", self.config)
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
        self.assertEqual(s["notes"], "notes")

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

    def test_long_notes_are_clipped_and_the_clip_is_recorded(self):
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u",
                                             "- a line\n" * 2000)):
            c._check()
        s = c.snapshot()
        self.assertTrue(s["clipped"])
        self.assertLessEqual(len(s["notes"]), web.NOTES_LIMIT)
        self.assertTrue(s["notes"].endswith("- a line"))   # whole lines only

    def test_short_notes_are_not_flagged_as_clipped(self):
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "- one")):
            c._check()
        s = c.snapshot()
        self.assertFalse(s["clipped"])
        self.assertEqual(s["notes"], "- one")

    def test_a_cache_from_before_the_flag_infers_it_from_the_length(self):
        """0.1.0 and 0.1.1 sliced at exactly NOTES_LIMIT and recorded nothing.
        Without inferring it, an install carrying such a cache shows the
        half-sentence with no way to reach the rest until it next checks."""
        Path(self.tmp, "update.json").write_text(json.dumps({
            "checked": time.time(), "tag": "v0.1.0", "latest": "0.1.0",
            "url": "u", "notes": "x" * web.NOTES_LIMIT, "error": ""}),
            encoding="utf-8")
        self.assertTrue(self.make().snapshot()["clipped"])

    def test_a_short_cache_from_before_the_flag_is_not_flagged(self):
        Path(self.tmp, "update.json").write_text(json.dumps({
            "checked": time.time(), "tag": "v0.1.0", "latest": "0.1.0",
            "url": "u", "notes": "- short", "error": ""}), encoding="utf-8")
        self.assertFalse(self.make().snapshot()["clipped"])

    def test_a_corrupt_cache_is_ignored_not_fatal(self):
        Path(self.tmp, "update.json").write_text("{ this is not json",
                                                 encoding="utf-8")
        self.assertEqual(self.make().snapshot()["latest"], "")

    def test_notes_come_from_the_changelog_when_there_is_no_release(self):
        # The tags fallback yields no body, and that is the normal case for
        # this repo, so "what's new" has to come from somewhere.
        c = self.make()
        text = "## [0.1.0] - 2026-08-09\n- a real change\n\n## [0.0.9]\n- old\n"
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "")), \
                mock.patch.object(web, "fetch_text", return_value=text):
            c._check()
        self.assertIn("a real change", c.snapshot()["notes"])
        self.assertNotIn("old", c.snapshot()["notes"])

    def test_a_changelog_failure_still_leaves_the_update_known(self):
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 1, 0), "v0.1.0", "u", "")), \
                mock.patch.object(web, "fetch_text", side_effect=OSError("x")):
            c._check()
        s = c.snapshot()
        self.assertTrue(s["available"])
        self.assertEqual(s["notes"], "")
        self.assertEqual(s["error"], "")

    def test_no_changelog_fetch_when_up_to_date(self):
        # One request in the common case, which is what keeps this cheap.
        c = self.make()
        with mock.patch.object(web, "latest_release",
                               return_value=((0, 0, 9), "v0.0.9", "u", "")), \
                mock.patch.object(web, "fetch_text") as text:
            c._check()
        text.assert_not_called()


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

    def test_an_available_update_shows_the_tag_and_the_command(self):
        body = self.get(self.checker(checked=time.time(), tag="v0.1.0",
                                     latest="0.1.0", url="https://example/rel"))
        self.assertIn("An update is available", body)
        self.assertIn("v0.1.0", body)
        self.assertIn("sudo timelapse update", body)
        self.assertIn("https://example/rel", body)

    def test_clipped_notes_say_so_and_link_to_the_rest(self):
        # A cap that silently eats the end of somebody's release notes reads
        # as this program losing them, and leaves no way to go and read them.
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            url="https://example/rel", notes="- a change", clipped=True))
        self.assertIn("shortened", body)
        self.assertIn("https://example/rel", body)

    def test_unclipped_notes_do_not_claim_anything_is_missing(self):
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            notes="- a change", clipped=False))
        self.assertIn("What is new", body)
        self.assertNotIn("shortened", body)

    def test_a_clip_with_no_release_url_still_offers_somewhere_to_go(self):
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0", url="",
            notes="- a change", clipped=True))
        self.assertIn(web.RELEASES_URL, body)

    def test_release_notes_are_shown_and_escaped(self):
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            notes="- fixed <script>alert(1)</script>"))
        self.assertIn("What is new", body)
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;", body)

    def test_changelog_heading_markers_are_stripped(self):
        # The notes go into a <div>, not a markdown renderer, so a literal
        # "### Fixed" reads as a mistake rather than as formatting.
        body = self.get(self.checker(
            checked=time.time(), tag="v0.1.0", latest="0.1.0",
            notes="### Fixed\n- a thing"))
        self.assertIn("Fixed", body)
        self.assertNotIn("### Fixed", body)
        self.assertIn("- a thing", body)

    def test_up_to_date_says_so_without_commands(self):
        body = self.get(self.checker(checked=time.time(), tag="v0.0.9",
                                     latest="0.0.9"))
        self.assertIn("Up to date", body)
        self.assertNotIn("install_timelapse.sh", body)

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
        self.assertIn("v0.1.0", body)
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


if __name__ == "__main__":
    unittest.main()

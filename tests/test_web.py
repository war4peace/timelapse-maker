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
    def __init__(self, config, index=None):
        self.cfg = config
        self.index = index


def request(path, config, index=None, method="GET"):
    """Drive one request through the real handler and return (status, body)."""
    raw = (f"{method} {path} HTTP/1.1\r\nHost: nas.local\r\n"
           f"Content-Length: 0\r\nConnection: close\r\n\r\n")
    req = FakeRequest(raw.encode())
    handler = web.Handler.__new__(web.Handler)
    handler.log_message = lambda *a, **k: None
    handler.rfile = None
    # BaseHTTPRequestHandler does all its work from __init__.
    web.Handler.__init__(handler, req, ("127.0.0.1", 5555),
                         FakeServer(config, index))
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
            self.assertIsNone(self.index._abs(rel), rel)

    def test_a_sibling_directory_with_a_shared_prefix_is_refused(self):
        # startswith() would accept /library-old for a root of /library.
        sibling = Path(self.tmp) / "library-old"
        sibling.mkdir()
        self.assertIsNone(self.index._abs("../library-old"))

    def test_ordinary_paths_resolve(self):
        self.assertIsNotNone(self.index._abs("2024-01"))
        self.assertIsNotNone(self.index._abs("Gate.20260707.mkv"))

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

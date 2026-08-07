"""Unit tests for timelapse_web.py — library resolution and request routing.

No sockets are opened. The handler is exercised through a fake request rather
than a live server: the routing and the escaping are the parts worth pinning,
and binding a port in a unit test invites flakiness on a CI runner.
"""

import io
import shutil
import tempfile
import unittest
from pathlib import Path

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

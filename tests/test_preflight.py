"""Unit tests for the pre-flight checker, `timelapse test`.

The checker's own tests have been scattered across the other files by subject
(the transfer half sits in test_encode.py, the storage guard and the webhook
marker in test_setup.py, the usage report in test_usage.py). This file is for
the camera checks, starting with the one that leaked.

`install.sh` runs the checker on every install and every upgrade, under sudo,
and the documented remedy for a leak here is the expensive one: rotate every
camera password and purge the journal. So the guarantee the daemon gets from
`RedactingFormatter` has to hold in the checker too, and it prints rather than
logs, which is exactly how it came to be missed.
"""

import ast
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import _support                                            # noqa: F401

import timelapse_encode as enc
import timelapse_test as tt


SECRET = "Sup3rS3cret!"

# The Reolink shape: the credential is a query parameter, so the URL *is* the
# password. Measured against a loopback listener on 2026-09-13
# (temp/c1_leak_probe.py): this is what requests puts in the exception text
# when the camera is off or the address is wrong, and the checker printed it
# verbatim.
REFUSED = (
    "HTTPConnectionPool(host='192.168.2.208', port=80): Max retries exceeded "
    "with url: /cgi-bin/api.cgi?cmd=Snap&channel=0&rs=tl&user=admin"
    "&password=%s (Caused by NewConnectionError(\"HTTPConnection("
    "host='192.168.2.208', port=80): Failed to establish a new connection: "
    "[Errno 111] Connection refused\"))" % SECRET
)

# The other shape an operator meets: a camera that answers and then wedges.
# No credential in it, so it must come through untouched.
TIMED_OUT = ("HTTPConnectionPool(host='192.168.2.208', port=80): "
             "Read timed out. (read timeout=4)")


class RaisingSession(object):
    """Enough of requests.Session for test_http, and it never opens a socket."""

    def __init__(self, exc):
        self.auth = None
        self.exc = exc
        self.asked = []

    def get(self, url, timeout=None):
        self.asked.append((url, timeout))
        raise self.exc


class TestHttpFailureIsRedacted(unittest.TestCase):
    """The fourth place this project has put a camera password in front of
    somebody, after the daemon's log, the web UI's log page and the wizard.

    The RTSP branch three functions down has redacted since it was written,
    with a comment about scroll-back and bug reports; the HTTP branch beside
    it did not, and HTTP is the deployment's only method.
    """

    CFG = {"capture": {"interval_seconds": 5, "timeout_seconds": 4},
           "paths": {}}
    CAM = {"name": "Doorbell", "auth": "digest",
           "username": "admin", "password": SECRET,
           "url": ("http://192.168.2.208/cgi-bin/api.cgi?cmd=Snap&channel=0"
                   "&rs=tl&user=admin&password=" + SECRET)}

    def run_check(self, exc):
        """What the operator sees, with the network replaced.

        Patched on `tt.requests` rather than in sys.modules: the checker
        imports requests at module level, so that is the name it resolves.
        """
        session = RaisingSession(exc)
        fake = types.ModuleType("requests")
        fake.Session = lambda: session
        buf = io.StringIO()
        with mock.patch.object(tt, "requests", fake), \
             contextlib.redirect_stdout(buf):
            result = tt.test_http(dict(self.CAM), self.CFG)
        self.assertIsNone(result)
        self.assertEqual(len(session.asked), 1)
        return buf.getvalue()

    def test_the_password_is_not_printed(self):
        out = self.run_check(Exception(REFUSED))
        self.assertNotIn(SECRET, out)
        self.assertIn("password=***", out)

    def test_the_reason_survives(self):
        # A redacted line still has to be worth keeping: without the address
        # and the cause this says only that something went wrong.
        out = self.run_check(Exception(REFUSED))
        for keep in ("Doorbell", "192.168.2.208", "cgi-bin/api.cgi",
                     "user=admin", "Connection refused"):
            self.assertIn(keep, out)
        self.assertIn("FAIL", out)

    def test_a_failure_with_no_credential_is_printed_whole(self):
        out = self.run_check(Exception(TIMED_OUT))
        self.assertIn("Read timed out. (read timeout=4)", out)
        self.assertNotIn("***", out)

    def test_it_is_the_shared_rule_rather_than_a_local_copy(self):
        # Every spelling of the key, the RTSP userinfo shape and the webhook
        # token all come free from `redact`; a second implementation here
        # would be a second thing to keep in step. Reverse-check: with the
        # checker calling str(exc), patching redact changes nothing.
        import timelapse_encode as enc
        with mock.patch.object(enc, "redact", lambda t: "REDACTED-BY-THE-RULE"):
            out = self.run_check(Exception(REFUSED))
        self.assertIn("REDACTED-BY-THE-RULE", out)
        self.assertNotIn(SECRET, out)


if __name__ == "__main__":
    unittest.main()


class TestTheExitCodeSaysWhatItFound(unittest.TestCase):
    """The checker exited 0 whatever it printed, and install.sh was written as
    though it did not.

    `as_service_user python3 timelapse_test.py "$CONFIG" || warn "Pre-flight
    reported problems"` could only fire on a traceback, and the installer then
    offered to enable the services with the same default whatever the check
    had just said. Platform-independent and unprivileged: every check is
    stubbed, so this is about the wiring and the verdict, not about any
    machine.

    main() itself had never been executed by a test, which is the GUI entry
    point lesson: a wiring function with no branches worth testing still has
    to be run by something.
    """

    CHECKS = ("check_camera_names", "test_cadence", "test_encoders",
              "test_disk", "test_state_dir", "test_transfer", "test_notify",
              "test_http", "test_rtsp", "report_usage", "diagnose_encoders")

    def setUp(self):
        self.addCleanup(setattr, tt, "_FAILS", tt._FAILS)

    def run_main(self, argv=(), cameras=(), during=None, **replace):
        """Drive the real main() with every check stubbed.

        `during` is called in place of the first camera check, which is where
        a real failure comes from.
        """
        cfg = {"cameras": [dict(c) for c in cameras],
               "capture": {"interval_seconds": 5}, "paths": {}}
        stack = contextlib.ExitStack()
        out = io.StringIO()
        with stack:
            stack.enter_context(mock.patch.object(
                sys, "argv", ["timelapse_test.py", "cfg.json"] + list(argv)))
            stack.enter_context(mock.patch.object(
                enc, "load_config", return_value=cfg))
            for name in self.CHECKS:
                stack.enter_context(mock.patch.object(
                    tt, name, replace.get(name, lambda *a, **k: None)))
            if during is not None:
                stack.enter_context(mock.patch.object(
                    tt, "test_http", side_effect=during))
            stack.enter_context(contextlib.redirect_stdout(out))
            rc = tt.main()
        return rc, out.getvalue()

    def test_a_clean_run_exits_zero(self):
        """The control, and it comes first: returning 1 always would satisfy
        every other test here and break every install."""
        rc, _ = self.run_main(cameras=[{"name": "Gate", "method": "http"}])
        self.assertEqual(rc, 0)

    def test_one_failure_makes_it_non_zero(self):
        def refuse(cam, cfg):
            tt.bad("%s: HTTP 401 Unauthorized" % cam["name"])
            return None

        rc, out = self.run_main(cameras=[{"name": "Gate", "method": "http"}],
                                during=refuse)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)

    def test_a_warning_alone_is_still_zero(self):
        """Deliberate. A warning is something to know about, not something
        that stops the product working, and a checker that exits non-zero for
        every one of them says nothing by the end of a release."""
        def grumble(cam, cfg):
            tt.warn("%s: 0.9s fetch is slow for a 1s interval" % cam["name"])
            return 100

        rc, out = self.run_main(cameras=[{"name": "Gate", "method": "http"}],
                                during=grumble)
        self.assertEqual(rc, 0)
        self.assertIn("WARN", out)

    def test_the_count_starts_clean_on_each_run(self):
        """Two runs in one process is what a test does, and soon enough what
        a caller does. A counter that carried over would report yesterday's
        camera fault against today's config."""
        def refuse(cam, cfg):
            tt.bad("%s: refused" % cam["name"])
            return None

        first, _ = self.run_main(cameras=[{"name": "Gate", "method": "http"}],
                                 during=refuse)
        second, _ = self.run_main(cameras=[{"name": "Gate", "method": "http"}])
        self.assertEqual((first, second), (1, 0))

    def test_usage_is_a_report_and_exits_zero_regardless(self):
        """`timelapse usage` answers a question rather than passing a verdict,
        and it belongs in a pipeline."""
        def noisy(cfg):
            tt.bad("Gate: something the usage report noticed")

        rc, _ = self.run_main(argv=["--usage"], report_usage=noisy)
        self.assertEqual(rc, 0)

    def test_the_encoder_diagnosis_is_a_report_too(self):
        """It exists to be run when something is already wrong; exiting
        non-zero for saying so would make it useless in a script."""
        def noisy(cfg):
            tt.bad("No usable encoder at all")

        rc, _ = self.run_main(argv=["--encoders"], diagnose_encoders=noisy)
        self.assertEqual(rc, 0)

    def test_the_entry_point_hands_the_status_to_the_shell(self):
        """Half a fix is a function that returns a number nobody reads. Found
        through the AST rather than by scanning the text, because prose
        describing a rule is not a breach of it and this file has a comment
        about exit codes in it.
        """
        source = Path(_support.SCRIPTS, "timelapse_test.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        guards = [n for n in tree.body
                  if isinstance(n, ast.If)
                  and ast.dump(n.test).find("__name__") != -1]
        self.assertEqual(len(guards), 1)
        calls = [n for n in ast.walk(guards[0]) if isinstance(n, ast.Call)]
        spelled = set()
        for call in calls:
            if isinstance(call.func, ast.Attribute):
                spelled.add(call.func.attr)
            elif isinstance(call.func, ast.Name):
                spelled.add(call.func.id)
        self.assertIn("exit", spelled)
        self.assertIn("main", spelled)


class TestAnEmptyFfmpegPathIsNamed(unittest.TestCase):
    """`Path("").exists()` is True, because it is ".".

    So `ffmpeg: ""` slipped past the "not found" check and produced four "not
    available" lines, one per candidate encoder, instead of one sentence
    naming the setting. The wizard can write it: ffmpeg_defaults() answers ""
    on Windows under --defaults or with no terminal.
    """

    def run_check(self, ffmpeg):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            tt.test_encoders({"paths": {"ffmpeg": ffmpeg}, "encode": {}})
        return out.getvalue()

    def test_an_empty_path_is_one_finding_naming_the_setting(self):
        said = self.run_check("")
        self.assertIn("paths.ffmpeg is empty", said)
        self.assertNotIn("not available", said)

    def test_a_wrong_path_still_says_where_it_looked(self):
        # The control: the empty case must not have swallowed the ordinary one.
        said = self.run_check("/no/such/ffmpeg")
        self.assertIn("/no/such/ffmpeg", said)


class TestTheAccountIsNamedOnEveryPlatform(unittest.TestCase):
    """"... is not writable by : [WinError 5]" was the whole message.

    pwd is Linux-only, and the empty string landed on the one platform where
    the account is the likeliest cause of the failure being reported.
    """

    def test_it_answers_something_without_pwd(self):
        import builtins
        real = builtins.__import__

        def refuse(name, *a, **kw):
            if name == "pwd":
                raise ImportError("no pwd here")
            return real(name, *a, **kw)

        with mock.patch.object(builtins, "__import__", refuse):
            self.assertTrue(enc.whoami())

    def test_it_still_prefers_the_real_account_where_there_is_one(self):
        # The control: getpass.getuser() reads the environment, which is good
        # enough for a message and not as good as the password database.
        if not hasattr(os, "geteuid"):
            self.assertTrue(enc.whoami())
            return
        import pwd
        self.assertEqual(enc.whoami(), pwd.getpwuid(os.geteuid()).pw_name)


class TestAnUnknownCameraNameIsRefused(unittest.TestCase):
    """`--camera Typo` silently reported "Cameras (0 enabled)".

    Which reads as a configuration problem, so the operator goes looking for
    one. A name this config has never heard of is a typo, and the fix is the
    list of names it does know.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.tmp / "config.json"
        self.config.write_text(json.dumps({
            "paths": {"frames_root": str(self.tmp / "frames"),
                      "video_output": str(self.tmp / "videos"),
                      "log_dir": str(self.tmp / "logs"),
                      "state_dir": str(self.tmp / "state"),
                      "ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
            "capture": {"interval_seconds": 5, "timeout_seconds": 4,
                        "min_bytes": 4096},
            "encode": {"framerate": 60, "min_frames": 100},
            "transfer": {"enabled": False},
            "cameras": [{"name": "Gate", "enabled": True,
                         "url": "http://192.0.2.1/s", "method": "http"}],
        }), encoding="utf-8")

    def run_main(self, camera):
        argv = ["timelapse_test.py", str(self.config), "--camera", camera,
                "--no-notify"]
        # test_http is stubbed: a unit test must not open a socket, and
        # 192.0.2.1 is TEST-NET, so the real one sits out its whole timeout
        # before failing. Four seconds of that was the first version of this
        # class.
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(tt, "test_http", lambda cam, cfg: None), \
                mock.patch.object(tt, "test_rtsp", lambda cam, cfg: None), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = tt.main()
        return code, out.getvalue()

    def test_a_name_that_is_not_there_is_a_failure_naming_itself(self):
        code, said = self.run_main("Typo")
        self.assertEqual(code, 1)
        self.assertIn("Typo", said)

    def test_it_lists_the_names_it_does_know(self):
        _, said = self.run_main("Typo")
        self.assertIn("Gate", said)

    def test_a_name_it_knows_is_not_refused(self):
        # The control, and case-insensitively, as the option always was.
        _, said = self.run_main("gate")
        self.assertNotIn("No enabled camera", said)


class TestAStatCanBeRefusedRatherThanMissing(unittest.TestCase):
    """Path.is_dir() raises on Windows for a directory this account may not
    read, where on Linux it answers False.

    test_state_dir() learned that at 0.2.0, when install.ps1 started
    restricting %ProgramData%\\timelapse for real; three of its neighbours
    were left doing it the old way, and a traceback out of a pre-flight reads
    as the tool being broken rather than as the answer.

    The refusal is injected, not arranged: arranging one needs a real ACL
    change, and the test would then run on one platform and be a skip on the
    other.
    """

    def test_the_helper_tells_a_refusal_from_an_absence(self):
        missing = tt.can_read(Path("/no/such/place/at/all"))
        self.assertEqual(missing, (False, None))

    def test_a_refusal_comes_back_with_its_reason(self):
        class Refusing(object):
            def is_dir(self):
                raise PermissionError(13, "Permission denied")

        answer, why = tt.can_read(Refusing())
        self.assertFalse(answer)
        self.assertIsInstance(why, OSError)

    def test_the_frames_check_says_which_it_was(self):
        cfg = {"paths": {"frames_root": "X:\\frames",
                         "video_output": "X:\\videos"},
               "cameras": []}

        def refuse(_self):
            raise PermissionError(13, "Permission denied")

        with mock.patch.object(tt.Path, "is_dir", refuse), \
                mock.patch.object(tt.Path, "exists", refuse), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            tt.report_usage(cfg)
        said = out.getvalue()
        self.assertIn("Cannot tell whether", said)
        self.assertNotIn("does not exist yet", said)


class TestThePreflightsRtspProbeKeepsTheDiagnosis(unittest.TestCase):
    """The same fix as the wizard's, in the copy next door. Both hand-roll the
    argv the daemon builds in RtspCamera._cmd(), which is its own finding."""

    BUSY = """[in#0 @ 1] Failed reading RTSP data: End of file
[in#0 @ 2] Error opening input: Operation not permitted
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # OUT defaults to the system temp directory, and a test writes inside
        # its own or nowhere.
        p = mock.patch.object(tt, "OUT", self.tmp / "samples")
        p.start()
        self.addCleanup(p.stop)

    def probe(self):
        cfg = {"paths": {"ffmpeg": "ffmpeg"}}
        cam = {"name": "Hallway", "url": "rtsp://war4peace:hunter2@h/stream1"}
        result = mock.Mock(returncode=1, stderr=self.BUSY)
        with mock.patch.object(tt.subprocess, "run", return_value=result) as run,                 contextlib.redirect_stdout(io.StringIO()) as out:
            size = tt.test_rtsp(cam, cfg)
        return run.call_args[0][0], size, out.getvalue()

    def test_it_asks_ffmpeg_for_warnings_not_only_errors(self):
        argv, _size, _said = self.probe()
        self.assertIn("warning", argv)
        self.assertNotIn("error", argv)

    def test_it_says_what_the_failure_was(self):
        _argv, size, said = self.probe()
        self.assertIsNone(size)
        self.assertIn("free client slot", said)
        self.assertIn("End of file", said)
        self.assertNotIn("hunter2", said)

    def rtsp_with(self, ffmpeg, run):
        cam = {"name": "Hallway", "url": "rtsp://war4peace:hunter2@h/stream1"}
        before = tt._FAILS
        self.addCleanup(setattr, tt, "_FAILS", before)
        with mock.patch.object(tt.subprocess, "run", run), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            size = tt.test_rtsp(cam, {"paths": {"ffmpeg": ffmpeg}})
        return size, out.getvalue(), tt._FAILS - before

    def test_no_ffmpeg_configured_is_a_fail_line_not_a_traceback(self):
        """A traceback here ended the run at the first RTSP camera, before
        the Encoders section could name the actual problem (M12)."""
        size, said, fails = self.rtsp_with(
            "", mock.Mock(side_effect=AssertionError("ran nothing")))
        self.assertIsNone(size)
        self.assertIn("paths.ffmpeg is empty", said)
        self.assertEqual(fails, 1)

    def test_an_ffmpeg_that_will_not_start_is_a_fail_line(self):
        size, said, fails = self.rtsp_with(
            "/nowhere/ffmpeg",
            mock.Mock(side_effect=FileNotFoundError(2, "No such file")))
        self.assertIsNone(size)
        self.assertIn("could not run ffmpeg", said)
        self.assertNotIn("hunter2", said)
        self.assertEqual(fails, 1)


class TestTheSampleDirectoryIsPrivatePerRun(unittest.TestCase):
    """/tmp/timelapse-test was one directory for every account that runs this,
    so the second account's write raised PermissionError out of main() (M20)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.addCleanup(setattr, tt, "_FAILS", tt._FAILS)

    def test_each_run_makes_its_own(self):
        made = []
        for _ in range(2):
            with mock.patch.object(tt, "OUT", None):
                folder = tt.sample_dir()
                self.addCleanup(shutil.rmtree, str(folder), True)
                self.assertEqual(tt.sample_dir(), folder, "not stable in a run")
                made.append(folder)
        self.assertNotEqual(made[0], made[1])
        for folder in made:
            self.assertTrue(folder.name.startswith("timelapse-test-"), folder)

    def test_nothing_is_made_until_a_sample_is_saved(self):
        with mock.patch.object(tt, "OUT", None):
            self.assertIsNone(tt.OUT)

    def test_a_sample_that_cannot_be_kept_is_a_warning_not_a_crash(self):
        """The camera answered with a JPEG, which is the verdict; failing to
        keep a copy is this checker's own problem."""
        blocker = self.tmp / "a-file"
        blocker.write_text("not a directory", encoding="utf-8")
        body = b"\xff\xd8\xff" + b"\0" * 5000
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=200, content=body)
        tt._FAILS = 0
        with mock.patch.object(tt, "OUT", blocker / "samples"), \
                mock.patch.object(tt.requests, "Session", return_value=session), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            size = tt.test_http(
                {"name": "Gate", "url": "http://192.0.2.1/snap"},
                {"capture": {"interval_seconds": 5, "timeout_seconds": 4},
                 "paths": {}})
        self.assertEqual(size, len(body))
        self.assertIn("WARN", out.getvalue())
        self.assertIn("could not save the sample", out.getvalue())
        self.assertEqual(tt._FAILS, 0)


class TestThePreflightRunsEndToEnd(unittest.TestCase):
    """main() against a real config, a real HTTP camera on loopback and a port
    nothing listens on (M33).

    Only the ffmpeg seams are stubbed (ffprobe's dimensions and the encoder
    probe), because those need a binary this suite does not assume. Every
    check the operator sees runs for real, which is the half nothing had ever
    executed: C1, M12 and M20 were all found in it.
    """

    def setUp(self):
        import http.server
        import socket
        import threading

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        for name in ("frames", "videos", "logs", "state", "samples"):
            (self.tmp / name).mkdir()
        frame = self.tmp / "frame.jpg"
        _support.make_frame(frame, size=8192)
        body = frame.read_bytes()

        class Snapshot(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Snapshot)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        # A port that was free a moment ago and is closed now: refused at once.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.closed_port = probe.getsockname()[1]
        probe.close()
        self.addCleanup(setattr, tt, "_FAILS", tt._FAILS)

    def camera(self, name, port):
        return {"name": name, "enabled": True, "method": "http", "auth": "none",
                "url": "http://127.0.0.1:%d/snapshot.jpg" % port}

    def config(self, cameras):
        cfg = {
            "paths": {"frames_root": str(self.tmp / "frames"),
                      "video_output": str(self.tmp / "videos"),
                      "log_dir": str(self.tmp / "logs"),
                      "state_dir": str(self.tmp / "state"),
                      "ffmpeg": sys.executable, "ffprobe": sys.executable},
            "capture": {"interval_seconds": 5, "timeout_seconds": 4,
                        "min_bytes": 4096, "min_free_gb": 0},
            "encode": {"framerate": 60, "container": "mkv", "gop": 120,
                       "av1_cq": 30, "hevc_cq": 28, "x264_crf": 23,
                       "min_frames": 100, "delete_frames_on_success": True,
                       "max_backlog_days": 7},
            "transfer": {"enabled": False},
            "cameras": cameras,
        }
        path = self.tmp / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def run_main(self, cameras):
        path = self.config(cameras)
        loopback = {"NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost"}
        with mock.patch.object(sys, "argv", ["timelapse_test.py", str(path)]), \
                mock.patch.object(tt, "OUT", self.tmp / "samples"), \
                mock.patch.object(tt, "dimensions",
                                  lambda ffprobe, sample: "64,64,yuvj420p"), \
                mock.patch.object(enc, "list_encoders",
                                  lambda ffmpeg: {"libx264"}), \
                mock.patch.object(enc, "probe_encoder_detail",
                                  lambda ffmpeg, cand:
                                  (cand["codec"] == "libx264", "")), \
                mock.patch.dict(os.environ, loopback), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = tt.main()
        return rc, out.getvalue()

    def test_a_working_install_passes_and_exits_zero(self):
        """The control: every section is green for this config, so a non-zero
        exit below is the camera and nothing else."""
        rc, out = self.run_main([self.camera("Gate", self.server.server_port)])
        self.assertIn("PASS  Gate:", out)
        self.assertNotIn("FAIL", out)
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / "samples" / "Gate.jpg").is_file())

    def test_a_camera_nobody_answers_for_fails_the_run(self):
        rc, out = self.run_main([self.camera("Gate", self.server.server_port),
                                 self.camera("Shed", self.closed_port)])
        self.assertIn("PASS  Gate:", out)
        self.assertIn("FAIL  Shed: request failed", out)
        self.assertEqual(rc, 1)

    def test_every_section_ran(self):
        _, out = self.run_main([self.camera("Gate", self.server.server_port)])
        for heading in ("=== Camera names ===", "=== Cameras (1 enabled) ===",
                        "=== Cadence ===", "=== Encoders ===", "=== Disk ===",
                        "=== Transfer destination ===", "=== Notifications ==="):
            self.assertIn(heading, out)
        self.assertIn("libx264", out)


class TestTheCadenceAndNotifySections(unittest.TestCase):
    """The two section checks M33 names that had no test of their own."""

    def setUp(self):
        self.addCleanup(setattr, tt, "_FAILS", tt._FAILS)
        tt._FAILS = 0

    def test_a_camera_that_would_never_make_a_video_fails(self):
        cfg = {"capture": {"interval_seconds": 5, "timeout_seconds": 4},
               "encode": {"framerate": 60, "min_frames": 100}}
        cams = [{"name": "Slow", "interval_seconds": 900}]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            tt.test_cadence(cfg, cams)
        self.assertIn("FAIL", out.getvalue())
        self.assertEqual(tt._FAILS, 1)

    def test_an_ordinary_cadence_passes_quietly(self):
        cfg = {"capture": {"interval_seconds": 5, "timeout_seconds": 4},
               "encode": {"framerate": 60, "min_frames": 100}}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            tt.test_cadence(cfg, [{"name": "Gate"}])
        self.assertNotIn("FAIL", out.getvalue())
        self.assertEqual(tt._FAILS, 0)

    def test_no_sinks_sends_nothing_and_says_so(self):
        with mock.patch.object(enc, "notify",
                               side_effect=AssertionError("sent something")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            tt.test_notify({"cameras": []}, None)
        self.assertIn("No notification sinks configured", out.getvalue())
        self.assertEqual(tt._FAILS, 0)


class TestNoSshClientIsAFailNotACrash(unittest.TestCase):
    """The remote-destination check ran ssh bare, so a machine without an
    ssh client ended the pre-flight with FileNotFoundError (M12)."""

    def test_it_is_reported(self):
        before = tt._FAILS
        self.addCleanup(setattr, tt, "_FAILS", before)
        cfg = {"transfer": {"enabled": True, "destination": "nas:/vol/tl"}}
        with mock.patch.object(tt, "IS_WINDOWS", False), \
                mock.patch.object(tt.subprocess, "run",
                                  side_effect=FileNotFoundError(2, "ssh")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            tt.test_transfer(cfg)
        self.assertIn("could not run ssh", out.getvalue())
        self.assertEqual(tt._FAILS - before, 1)

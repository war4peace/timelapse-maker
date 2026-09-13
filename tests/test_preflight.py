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
import sys
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

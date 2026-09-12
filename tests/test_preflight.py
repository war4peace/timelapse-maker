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

import contextlib
import io
import types
import unittest
from unittest import mock

import _support                                            # noqa: F401

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

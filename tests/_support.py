"""Shared test helpers.

Puts scripts/ on sys.path so the modules can be imported by name, and provides
the small fakes the tests need. Stdlib only - no pytest, no fixtures library.
"""

import contextlib
import io
import logging
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# One handler, so logging's last resort never fires (0.2.1, L42). The product's
# loggers have no handler under the suite, and logging then writes any WARNING
# or worse straight to stderr: the run printed "rsync is not installed",
# "Another encode run is in progress", "ffmpeg exited (rc=1 ...)" and a
# handful of others, all of them the behaviour under test, all of them reading
# as something having gone wrong in the run.
#
# A NullHandler rather than logging.disable(), which was the first attempt and
# is wrong: disable() is checked before the record reaches any handler, so it
# blinds assertLogs too, and three tests that assert a line IS logged went red.
# This suppresses no record, it only gives them somewhere to go.
logging.getLogger().addHandler(logging.NullHandler())


class FakeStatVFS:
    """Enough of os.statvfs_result for scan_filesystems()."""

    def __init__(self, free_gb, total_gb, frsize=4096):
        self.f_frsize = frsize
        self.f_bavail = int(free_gb * 1024 ** 3) // frsize
        self.f_blocks = int(total_gb * 1024 ** 3) // frsize


def write_mounts(tmpdir, lines):
    """Write a synthetic /proc/mounts and return its path."""
    p = Path(tmpdir) / "mounts"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


JPEG_HEADER = b"\xff\xd8\xff"


def make_frame(path, size=8192, header=JPEG_HEADER):
    """A file that passes (or, with a different header, fails) validation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"\x00" * max(0, size - len(header)))
    return path


class FakeTTY(io.StringIO):
    """A terminal that supplies a fixed script of keystrokes.

    Here rather than in test_setup.py (0.2.1, L47), which is where it lived
    while test_gui.py and test_usage.py drove the same prompts and could not
    reach it. `tty=False` is the common case: with isatty() true, ask_secret()
    hands the question to the real getpass, which reads the **console** rather
    than this object, and a suite that blocks on a password prompt is a suite
    nobody can run unattended.
    """

    def __init__(self, text, tty=False):
        super().__init__(text)
        self.reads = 0
        self._tty = tty

    def isatty(self):
        return self._tty

    def readline(self):
        self.reads += 1
        return super().readline()


@contextlib.contextmanager
def wizard_tty(keystrokes, tty=False, auto=False, discovery=()):
    """Drive the wizard's prompts, and put everything back afterwards.

    Nineteen copies of this save/restore block existed, which is nineteen
    chances to forget the restore and leave `setup.AUTO` set for whatever runs
    next. The module is imported here rather than at the top because
    _support.py is imported by test modules that have nothing to do with the
    wizard.

    `offer_discovery` is patched by default, and that is not tidiness: the
    real one sends a WS-Discovery multicast, so a test that reaches it puts
    packets on the network of whoever is running the suite and waits three
    seconds for answers. Pass `discovery=` to hand back fake devices.
    """
    import timelapse_setup as setup

    prev_tty, prev_auto = setup._TTY, setup.AUTO
    prev_discovery = setup.offer_discovery
    setup._TTY = FakeTTY(keystrokes, tty=tty)
    setup.AUTO = auto
    setup.offer_discovery = lambda: list(discovery)
    try:
        yield setup._TTY
    finally:
        setup._TTY, setup.AUTO = prev_tty, prev_auto
        setup.offer_discovery = prev_discovery


class TempDirCase(unittest.TestCase):
    """A test case with its own temp directory, removed afterwards.

    `tempfile.mkdtemp()` plus `addCleanup(rmtree)` appeared 71 times. The
    directory matters more than the line count: a test that writes outside one
    has twice created files on the developer's own disk, and once on the
    author's NAS.
    """

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

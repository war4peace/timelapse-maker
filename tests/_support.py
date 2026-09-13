"""Shared test helpers.

Puts scripts/ on sys.path so the modules can be imported by name, and provides
the small fakes the tests need. Stdlib only - no pytest, no fixtures library.
"""

import logging
import sys
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

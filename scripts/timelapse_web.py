#!/usr/bin/env python3
"""
timelapse_web.py: read-only web UI.

Serves a status page and an index of the finished videos at the transfer
destination, browsable by camera and by date.

Read-only towards everything of yours: it never triggers an encode, restarts a
camera, edits the config or deletes a file. The one thing it writes is its own
sqlite index, and the unit's ReadWritePaths names that directory and nothing
else - so under ProtectSystem=strict the library, the frames and the config are
all read-only to this process. That is enforced by the sandbox, not promised by
this comment.

Playback is deliberately not done in the browser. The default output is AV1 in
Matroska, which browsers handle poorly and VLC, mpv and friends handle
natively. So this serves the bytes over HTTP and hands off with a one-line .m3u
playlist that the desktop opens in whatever plays .m3u.

Binds to 127.0.0.1 by default. http.server is not a hardened internet-facing
server and there is no TLS here - anything beyond loopback is an explicit
opt-in, and anything beyond the LAN belongs behind a reverse proxy.

Run under systemd. Logs to stdout (journald).
"""

import argparse
import base64
import binascii
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from email.utils import formatdate
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

# The only *module-level* import between this project's scripts, and
# deliberately one-way. Several others exist inside functions, all reaching
# into timelapse_encode; this one is at the top because the web server needs
# it to define a class, not to answer one call.
# The release query is the same knowledge whether a page or a command asks it,
# and two copies of "compare versions as tuples, not strings" is one copy too
# many. Installed side by side in the same directory, which is sys.path[0] for
# either entry point; the tests put scripts/ on the path for the same reason.
from timelapse_update import (                            # noqa: E402
    RELEASES_URL, fetch_json, friendly_error, latest_release, parse_version,
    version_text,
)

# Also at module level, and for a harder reason: this page renders the
# journal, which on any host that ran a version before 0.1.3 contains camera
# passwords in full. A lazy import inside the renderer could fail at request
# time and quietly leave the page unredacted; failing at startup is the
# behaviour a security filter should have.
from timelapse_encode import hostport, is_ipv6, redact    # noqa: E402

# The runtime-state contract, from the module that defines it. Aliased on
# purpose: this file already uses "state_dir" for web.state_dir, the UI's own
# index directory, which is a different directory holding different things and
# is the only one this service may write to. Two meanings of one name in one
# file is how the wrong directory ends up in a hardening claim.
from timelapse_encode import (                            # noqa: E402
    CAPTURE_STATE, ENCODE_STATE, STATE_VERSION,
    coverage_of, day_cadence,
    state_dir as runtime_state_dir,
)

__version__ = "0.1.7"

log = logging.getLogger("web")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8787

# The only path this service writes to. The unit's ReadWritePaths is scoped to
# exactly this, so everything else - library, frames, config - stays read-only.
DEFAULT_STATE_DIR = "/var/lib/timelapse/web"

# A request handler must never outlive the client that abandoned it. This
# belongs on the handler, not the server: ThreadingHTTPServer.timeout is only
# consulted by handle_request(), which serve_forever() never calls, so setting
# it there looks like a timeout and is not one. On the handler it reaches
# socket.settimeout() via StreamRequestHandler.setup().
SOCKET_TIMEOUT = 30

# Exceptions that mean "the client went away", which is not a fault. Python 3.10
# made socket.timeout an alias of TimeoutError; on 3.9 it is a distinct OSError
# subclass that TimeoutError does not cover, so both are named. getattr rather
# than the attribute, because the docs call the alias deprecated and an
# AttributeError at import would take the whole service down over a log message.
DISCONNECTED = (ConnectionError, TimeoutError,
                getattr(socket, "timeout", TimeoutError))


def load_config(path):
    """Duplicated from timelapse_capture rather than imported, for the same
    reason it is duplicated there: no daemon should be able to fail because a
    sibling changed. The distinct messages matter more than the duplication -
    journald showing "run timelapse setup" beats a traceback."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        sys.exit(f"No config at {path}. Run: sudo timelapse setup")
    except PermissionError:
        sys.exit(f"Cannot read {path} - it is 0640 root:timelapse and this "
                 f"process is not in that group.")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    except OSError as exc:
        sys.exit(f"Cannot read {path}: {exc}")


def setup_logging():
    """journald only. The other tools also write a rotating file; this one
    would need a second writable path for it, and the unit is deliberately
    scoped to one."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


# ----------------------------------------------------------------------------
# Library root
# ----------------------------------------------------------------------------

def is_remote_spec(dest):
    """True for an rsync remote such as 'user@nas:/path' or 'rsync://host/mod'.

    These are not filesystem paths and cannot be listed without SSH/SFTP.

    An absolute path is settled first, before the colon test. That is what
    distinguishes '/mnt/odd:name/videos' from 'nas:videos' - and on Windows it
    is what stops a drive letter being read as a hostname, which is only a
    developer-machine concern (the tools run on Linux) but made the unit tests
    disagree with CI.
    """
    if dest.startswith("rsync://"):
        return True
    if os.path.isabs(dest):
        return False
    return ":" in dest.split("/", 1)[0]


def resolve_library(cfg):
    """Work out where finished videos actually live.

    The trap this exists for: transfer runs rsync with --remove-source-files
    and transfer.delete_local_after_transfer defaults to true, so after a
    successful night paths.video_output is EMPTY. Reading it would show an
    empty library on every correctly configured install.

    Returns a dict rather than a path because "why is it empty" is the question
    the page has to answer, and only this function knows.
    """
    web = cfg.get("web", {})
    trans = cfg.get("transfer", {})
    out = {"path": None, "source": "", "usable": False, "note": ""}

    override = (web.get("library_root") or "").strip()
    if override:
        out["path"], out["source"] = Path(override), "web.library_root"
    elif trans.get("enabled", False) and (trans.get("destination") or "").strip():
        dest = trans["destination"].strip()
        if is_remote_spec(dest):
            out["source"] = "transfer.destination (remote)"
            out["note"] = (f"Videos are transferred to {dest}, which is a remote "
                           f"rsync target, not a path this host can read. "
                           f"Browsing is not supported. Set web.library_root if "
                           f"the same files are reachable locally.")
            return out
        out["path"], out["source"] = Path(dest), "transfer.destination"
    else:
        out["path"] = Path(cfg["paths"]["video_output"])
        out["source"] = "paths.video_output (transfer disabled)"

    if not out["path"].is_dir():
        out["note"] = (f"{out['path']} does not exist or is not readable. "
                       f"If it is a NAS mount, it may simply not be mounted.")
        return out

    out["usable"] = True
    return out


# ----------------------------------------------------------------------------
# Filename parsing
# ----------------------------------------------------------------------------

# Six conventions, measured against a real five-year library (6,848 files;
# docs/architecture.md §9a records the survey). The native format is 64% of it -
# a parser that handles only that silently drops a third of the library and all
# history before 2024-04. Order matters: most specific first.
#
# Every camera group is a *place*, not a device. Cameras get repurposed, so two
# similar names are not evidence of the same thing and are never merged here.

_PATTERNS = (
    # Camera.20260707
    ("native", re.compile(
        r"^(?P<cam>.+)\.(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})$")),
    # 2024-01-01_Workshop
    ("date-first", re.compile(
        r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[ _-]+(?P<cam>.+)$")),
    # Courtyard_4K_2021-11-01
    ("date-last", re.compile(
        r"^(?P<cam>.+?)[ _-]+(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})$")),
    # 2021-11-01  - no camera in the name at all
    ("date-only", re.compile(
        r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})$")),
    # 2023-05-12T22-00-01_roof
    ("timestamped", re.compile(
        r"^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})T[\d.:-]+"
        r"(?:[ _-]+(?P<cam>.+))?$")),
    # Court18020240428_20240428233819
    ("double-stamp", re.compile(
        r"^(?P<cam>.*?)(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})_\d{14}$")),
)

NO_CAMERA = ""      # 449 files in the surveyed library carry no name at all


def parse_name(name):
    """(camera, day, pattern) for a video filename. day is ISO or None.

    A pattern that matches but yields an impossible date falls through to the
    next rather than winning - `something.99999999.mkv` is not a dated file.
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for label, rx in _PATTERNS:
        m = rx.match(stem)
        if not m:
            continue
        try:
            day = datetime.date(int(m.group("y")), int(m.group("mo")),
                                int(m.group("d"))).isoformat()
        except ValueError:
            continue
        cam = ""
        if "cam" in rx.groupindex:
            cam = (m.group("cam") or "").strip(" _-.")
        return cam, day, label
    return "", None, "unrecognised"


# ----------------------------------------------------------------------------
# Library index
# ----------------------------------------------------------------------------

# "not a directory" is not a test for "is a video": the surveyed library has a
# leftover MakeTLALL_backup.ps1 sitting in its root.
VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".avi", ".webm", ".m4v", ".mpg", ".mpeg"}

# A day's timelapse is hundreds of megabytes. Anything this small is a failed
# encode; 11 such files exist in the surveyed library. They are listed with
# their full path so they can be dealt with by other means - this UI never
# deletes anything.
SUSPECT_BYTES = 1024 * 1024

SCHEMA_VERSION = "1"

# A library on a NAS is not always there when the service starts at boot. The
# scan waits for it rather than concluding the library is empty.
SCAN_RETRY_DELAY = 60
SCAN_RETRY_LIMIT = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path       TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    folder     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      INTEGER NOT NULL,
    camera     TEXT NOT NULL,
    day        TEXT,
    pattern    TEXT NOT NULL,
    suspect    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_camera ON files(camera, day);
CREATE INDEX IF NOT EXISTS files_day    ON files(day);
CREATE INDEX IF NOT EXISTS files_folder ON files(folder, name);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class Index:
    """The sqlite-backed file index.

    This is the one thing the web UI writes, and the unit's ReadWritePaths is
    scoped to exactly its directory - the library, the frames and the config
    all stay read-only to this process.

    A connection per operation rather than one shared handle: sqlite objects
    are not safely shared across threads, and the scan worker writes while
    request threads read. WAL is what makes that concurrency free.
    """

    def __init__(self, db_path, root):
        self.db_path = Path(db_path)
        self.root = Path(root) if root else None
        self.error = ""
        self.scan = {"running": False, "files": 0, "started": 0.0,
                     "finished": 0.0, "error": ""}
        self._lock = threading.Lock()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.executescript(SCHEMA)
                self._check_generation(db)
        except (OSError, sqlite3.Error) as exc:
            # Serving status and logs without an index beats refusing to start.
            self.error = (f"Cannot use the index at {self.db_path}: {exc}. "
                          f"The unit needs ReadWritePaths for that directory.")
            log.error("%s", self.error)

    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=15)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        db.row_factory = sqlite3.Row
        return db

    def _check_generation(self, db):
        """Wipe the index when the schema or the library root changes.

        Rebuilding costs one scan; serving an index built from a different
        directory costs the user's trust.
        """
        want = {"schema": SCHEMA_VERSION, "root": str(self.root or "")}
        have = {r["key"]: r["value"] for r in db.execute("SELECT * FROM meta")}
        if any(have.get(k) != v for k, v in want.items()):
            if have:
                log.info("Index generation changed (%s -> %s); rebuilding.",
                         have, want)
            db.execute("DELETE FROM files")
            for k, v in want.items():
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
            db.commit()

    @property
    def usable(self):
        return not self.error and self.root is not None

    # -- scanning ------------------------------------------------------------

    def start_scan(self, reason="first run"):
        """Kick off a full scan in the background.

        Deliberately not timed against a budget. The only measurement taken -
        1.7 s for 6,848 files - came from a 10G workstation, while deployments
        read CIFS over 1G; the work is round-trips rather than megabytes, so it
        moves for reasons that are hard to predict. Nothing here blocks a
        request, so the duration does not matter.
        """
        if not self.usable:
            return False
        with self._lock:
            if self.scan["running"]:
                return False
            self.scan = {"running": True, "files": 0, "started": time.time(),
                         "finished": 0.0, "error": ""}
        log.info("Library scan started (%s): %s", reason, self.root)
        threading.Thread(target=self._scan_worker, name="index-scan",
                         daemon=True).start()
        return True

    def _root_state(self):
        """(readable, reason). An unreadable root is not evidence of deletion."""
        if self.root is None:
            return False, "no library root is configured"
        try:
            if not self.root.is_dir():
                return False, f"{self.root} is not a readable directory"
            with os.scandir(self.root) as it:
                next(it, None)
        except OSError as exc:
            return False, f"{self.root} cannot be read: {exc}"
        return True, ""

    def _wait_for_library(self):
        """Wait for the library to appear, up to a bounded number of tries.

        A NAS mount is not always up when the service starts at boot, and a
        scan that cannot read the root has not discovered that every file is
        gone - it has discovered nothing. Waiting is what turns a late mount
        into a non-event instead of an index wiped at every reboot.
        """
        for attempt in range(1, SCAN_RETRY_LIMIT + 1):
            ok, why = self._root_state()
            if ok:
                return True
            with self._lock:
                self.scan["error"] = (f"waiting for the library ({attempt}/"
                                      f"{SCAN_RETRY_LIMIT}): {why}")
            log.warning("Library not readable (attempt %d/%d): %s",
                        attempt, SCAN_RETRY_LIMIT, why)
            if attempt == SCAN_RETRY_LIMIT:
                return False
            time.sleep(SCAN_RETRY_DELAY)
        return False

    def _scan_worker(self):
        seen, batch, count = set(), [], 0
        try:
            if not self._wait_for_library():
                _, why = self._root_state()
                with self._lock:
                    self.scan.update(
                        running=False, finished=time.time(),
                        error=f"library unreadable, kept the existing index: {why}")
                log.error("Giving up on the scan; the existing index is kept.")
                return

            with self._connect() as db:
                before = db.execute(
                    "SELECT COUNT(*) c FROM files").fetchone()["c"]
                for row in self._walk():
                    seen.add(row[0])
                    batch.append(row)
                    count += 1
                    # Per file, not per batch. The batch exists to keep the
                    # database writes efficient and is a poor unit for a
                    # progress report: on a slow share 500 files is a long
                    # stall showing a stale number, and on a fast one the
                    # whole scan can finish before the first batch, leaving
                    # the page reporting 0. An uncontended lock costs nothing
                    # against the I/O this loop is already doing.
                    with self._lock:
                        self.scan["files"] = count
                    if len(batch) >= 500:
                        self._write(db, batch)
                        batch = []
                if batch:
                    self._write(db, batch)

                # Anything not seen this pass is gone from disk - unless the
                # pass saw nothing at all while the index holds rows. An
                # unmounted CIFS mountpoint is a *readable, empty* directory,
                # so "found nothing" is far more often a mount that is not
                # there than a library someone deleted. Keeping stale rows
                # costs a 404 and is repaired by opening the folder; wiping
                # the index costs a full rescan of the whole share.
                if count == 0 and before > 0:
                    db.commit()
                    with self._lock:
                        self.scan.update(
                            running=False, files=0, finished=time.time(),
                            error=(f"found no videos in {self.root}, so the "
                                   f"existing index of {before} was kept. If "
                                   f"the library really is empty it will "
                                   f"correct itself as you browse."))
                    log.warning("Scan found nothing but the index holds %d; "
                                "keeping it.", before)
                    return

                keep = list(seen)
                db.execute("CREATE TEMP TABLE seen(path TEXT PRIMARY KEY)")
                db.executemany("INSERT OR IGNORE INTO seen VALUES (?)",
                               ((p,) for p in keep))
                db.execute("DELETE FROM files WHERE path NOT IN "
                           "(SELECT path FROM seen)")
                db.commit()
            with self._lock:
                self.scan.update(running=False, files=count,
                                 finished=time.time(), error="")
            log.info("Library scan finished: %d files", count)
        except Exception as exc:            # a scan must never kill the server
            log.error("Library scan failed: %s", exc)
            with self._lock:
                self.scan.update(running=False, finished=time.time(),
                                 error=str(exc)[:300])

    def _walk(self):
        """Every video file under the root, as an index row.

        os.scandir, and size/mtime taken from the entry: on a network share the
        cost is round-trips, and asking the directory once for what it already
        knows is the whole difference.
        """
        stack = [("", str(self.root))]
        while stack:
            rel, path = stack.pop()
            try:
                entries = list(os.scandir(path))
            except OSError as exc:
                log.warning("Cannot read %s: %s", path, exc)
                continue
            for entry in entries:
                child = f"{rel}/{entry.name}" if rel else entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((child, entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        row = self._row(child, entry.name, rel, entry)
                        if row:
                            yield row
                except OSError:
                    continue        # vanished mid-scan; the next pass sorts it

    @staticmethod
    def _row(rel, name, folder, entry):
        if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
            return None
        st = entry.stat()
        cam, day, pattern = parse_name(name)
        return (rel, name, folder, st.st_size, int(st.st_mtime),
                cam, day, pattern, 1 if st.st_size < SUSPECT_BYTES else 0)

    @staticmethod
    def _write(db, batch):
        db.executemany(
            "INSERT OR REPLACE INTO files "
            "(path,name,folder,size,mtime,camera,day,pattern,suspect) "
            "VALUES (?,?,?,?,?,?,?,?,?)", batch)
        db.commit()

    # -- reconciliation ------------------------------------------------------

    def reconcile_dir(self, rel):
        """Re-read one directory and bring its rows into line. Returns whether
        anything actually differed.

        This always does the scandir. An earlier version gated on the
        directory's mtime and skipped the read when it matched, which was both
        a correctness hole and a false economy: mtime is stored at second
        granularity, so anything added within the same second as the last scan
        stayed invisible until something else changed. Reading one directory is
        a single round trip. The expensive thing is walking the whole tree, and
        that is what this exists to avoid.
        """
        if not self.usable:
            return False
        path = self.abs_path(rel)
        if path is None:
            return False

        rows, paths = [], set()
        try:
            for entry in os.scandir(path):
                if not entry.is_file(follow_symlinks=False):
                    continue
                child = f"{rel}/{entry.name}" if rel else entry.name
                built = self._row(child, entry.name, rel, entry)
                if built:
                    rows.append(built)
                    paths.add(child)
        except OSError:
            # The directory is gone: drop everything filed under it.
            with self._connect() as db:
                gone = db.execute("SELECT COUNT(*) c FROM files WHERE folder=?",
                                  (rel,)).fetchone()["c"]
                db.execute("DELETE FROM files WHERE folder = ?", (rel,))
                db.commit()
            return bool(gone)

        with self._connect() as db:
            before = {r["path"]: (r["size"], r["mtime"]) for r in db.execute(
                "SELECT path, size, mtime FROM files WHERE folder = ?", (rel,))}
            now = {r[0]: (r[3], r[4]) for r in rows}
            if rows:
                self._write(db, rows)
            stale = [p for p in before if p not in paths]
            if stale:
                db.executemany("DELETE FROM files WHERE path = ?",
                               ((p,) for p in stale))
            db.commit()
        return before != now

    def reconcile_file(self, rel):
        """Re-stat one file. Returns its row, or None when it is gone.

        The extension allow-list is applied HERE too, not only in the scan.
        This is what /video/<path> resolves through, and without the check a
        request could name any file inside the library root - a script, a
        config, whatever the user keeps alongside their videos - and this
        would stat it, index it and serve it. Path containment stops the
        request escaping the library; this stops it reading everything within.
        """
        if not self.usable:
            return None
        name = os.path.basename(rel)
        if os.path.splitext(name)[1].lower() not in VIDEO_EXTS:
            return None
        path = self.abs_path(rel)
        if path is None:
            return None
        try:
            st = os.stat(path)
        except OSError:
            with self._connect() as db:
                db.execute("DELETE FROM files WHERE path = ?", (rel,))
                db.commit()
            return None
        folder = os.path.dirname(rel)
        cam, day, pattern = parse_name(name)
        row = (rel, name, folder, st.st_size, int(st.st_mtime), cam, day,
               pattern, 1 if st.st_size < SUSPECT_BYTES else 0)
        with self._connect() as db:
            self._write(db, [row])
        return self.get(rel)

    def abs_path(self, rel):
        """Resolve a relative index path inside the root, or None.

        Nothing from a request should reach the filesystem unchecked, even
        while this phase only stats. commonpath rather than startswith: the
        latter accepts /library-old for a root of /library.
        """
        if self.root is None:
            return None
        candidate = (self.root / rel).resolve() if rel else self.root.resolve()
        try:
            root = self.root.resolve()
            if os.path.commonpath([str(root), str(candidate)]) != str(root):
                return None
        except (OSError, ValueError):
            return None
        return candidate

    # -- queries -------------------------------------------------------------

    def _query(self, sql, args=()):
        if not self.usable:
            return []
        try:
            with self._connect() as db:
                return [dict(r) for r in db.execute(sql, args)]
        except sqlite3.Error as exc:
            log.warning("Index query failed: %s", exc)
            return []

    def totals(self):
        rows = self._query(
            "SELECT COUNT(*) n, COALESCE(SUM(size),0) b, MIN(day) a, MAX(day) z,"
            " SUM(suspect) s FROM files")
        return rows[0] if rows else {"n": 0, "b": 0, "a": None, "z": None,
                                     "s": 0}

    def cameras(self):
        # ORDER BY lower(camera): two spellings of a place sit next to each
        # other so the reader can judge them. They are never merged - see
        # architecture.md §9a; a name is a place, and places get recycled.
        return self._query(
            "SELECT camera, COUNT(*) n, SUM(size) b, MIN(day) a, MAX(day) z "
            "FROM files GROUP BY camera ORDER BY lower(camera), camera")

    def folders(self):
        return self._query(
            "SELECT folder, COUNT(*) n, SUM(size) b FROM files "
            "GROUP BY folder ORDER BY folder DESC")

    def by_day(self, day):
        # lower(camera) for the same reason as cameras(): two spellings of a
        # place sit together, and neither is folded into the other.
        return self._query(
            "SELECT * FROM files WHERE day = ? "
            "ORDER BY lower(camera), camera, name", (day,))

    def recent_days(self, limit=14):
        return self._query(
            "SELECT day, COUNT(*) n, SUM(size) b FROM files "
            "WHERE day IS NOT NULL GROUP BY day ORDER BY day DESC LIMIT ?",
            (limit,))

    def by_camera(self, camera):
        return self._query(
            "SELECT * FROM files WHERE camera = ? ORDER BY day DESC, name",
            (camera,))

    def in_folder(self, folder):
        return self._query(
            "SELECT * FROM files WHERE folder = ? ORDER BY name", (folder,))

    def suspects(self):
        return self._query(
            "SELECT * FROM files WHERE suspect = 1 ORDER BY size, path")

    def unrecognised(self):
        return self._query(
            "SELECT * FROM files WHERE day IS NULL ORDER BY path")

    def get(self, rel):
        rows = self._query("SELECT * FROM files WHERE path = ?", (rel,))
        return rows[0] if rows else None


# ----------------------------------------------------------------------------
# Serving video, and handing off to a real player
# ----------------------------------------------------------------------------

MEDIA_TYPES = {
    ".mkv": "video/x-matroska", ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".webm": "video/webm", ".m4v": "video/x-m4v",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
}

# audio/x-mpegurl is the type desktops actually have registered for .m3u.
M3U_TYPE = "audio/x-mpegurl"

SEND_CHUNK = 256 * 1024

# A Host header goes straight into the playlist URL, so it is validated rather
# than trusted. Hostnames, IPv4, bracketed IPv6, optional port - nothing else.
HOST_RE = re.compile(r"^[A-Za-z0-9._\-]+(:\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](:\d{1,5})?$")


# Digits are bounded: an unbounded \d* invites a megabyte of them, and int()
# on that is real work for a request that was never going to be satisfiable.
RANGE_RE = re.compile(r"^bytes=(\d{0,19})-(\d{0,19})$")

UNSATISFIABLE = object()


def parse_range(header, size):
    """(start, end) inclusive, UNSATISFIABLE, or None to send the whole file.

    RFC 7233 allows a server to ignore a Range header it does not care for, and
    that is what None means here: an unparseable header, or a multi-range
    request, is answered with a normal 200 rather than an error. Multi-range
    would need multipart/byteranges, and nothing seeking a video asks for it.
    """
    if not header:
        return None
    header = header.strip()
    if "," in header:
        return None
    m = RANGE_RE.match(header)
    if not m:
        return None
    first, last = m.group(1), m.group(2)
    if not first and not last:
        return None

    if not first:
        # bytes=-N - the final N bytes. Players use this to read a trailer.
        want = int(last)
        if want == 0:
            return UNSATISFIABLE
        start, end = max(0, size - want), size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1
        if end >= size:
            end = size - 1          # clamping is required, not an error

    if size == 0 or start >= size or start > end:
        return UNSATISFIABLE
    return start, end


DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_day(text):
    """An ISO date, or None. Guards every day-keyed route."""
    if not DAY_RE.match(text or ""):
        return None
    try:
        return datetime.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def m3u_title(text):
    """A title that cannot span two lines.

    A filename may legally contain a newline on Linux, and an #EXTINF carrying
    one would split into a bogus second entry - so all whitespace collapses.
    """
    return " ".join(str(text or "").split()) or "timelapse"


def media_type(name):
    return MEDIA_TYPES.get(os.path.splitext(name)[1].lower(),
                           "application/octet-stream")


def ascii_filename(name, suffix):
    """A Content-Disposition filename that cannot break the header.

    The library has Romanian folder names, so non-ASCII is normal here. Rather
    than risk a mangled header, unrepresentable characters become '_' and the
    stem falls back to something sane if nothing survives.
    """
    stem = os.path.splitext(os.path.basename(name))[0]
    safe = "".join(c if 32 < ord(c) < 127 and c not in '"\\/:*?<>|' else "_"
                   for c in stem).strip("_. ")
    return (safe or "timelapse") + suffix


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


# ----------------------------------------------------------------------------
# Service status and logs
# ----------------------------------------------------------------------------

# Run only when asked - a page load or a click. Nothing polls, nothing is
# collected in the background.

COMMAND_TIMEOUT = 10

# Unit, the words a reader wants, and what "not running" means for it. That
# last field is the point of the whole table: a oneshot sitting inactive is
# what healthy looks like between nightly runs, while a daemon sitting
# inactive is the fault somebody opened this page to find.
STATUS_UNITS = (
    ("timelapse-capture.service", "Capture", "daemon"),
    ("timelapse-encode.timer", "Nightly encode", "timer"),
    ("timelapse-encode.service", "Last encode run", "oneshot"),
    ("timelapse-watch.timer", "Credential watch", "timer"),
    ("timelapse-web.service", "Web interface", "daemon"),
)

# Everything the table needs and nothing else. `systemctl status` prints the
# invocation ID, the cgroup, the PID and the Docs= line once per unit, which
# is four repetitions of a URL and a page of detail nobody reads to find out
# whether capture is running.
STATUS_PROPS = ("Id", "LoadState", "ActiveState", "SubState", "UnitFileState",
                "ActiveEnterTimestamp", "InactiveEnterTimestamp",
                "InactiveExitTimestamp", "Result", "NextElapseUSecRealtime",
                # A monotonic timer (OnBootSec/OnUnitActiveSec, which is what
                # the credential watch uses) leaves NextElapseUSecRealtime
                # empty and reports here instead. Reading only the realtime
                # one left that row with a blank Detail, which reads as though
                # something were wrong with it.
                "NextElapseUSecMonotonic", "LastTriggerUSec")

# Request values pick a key; the *value* is what reaches the command line. No
# string from a request is ever interpolated into an argv, so there is no
# injection surface even in principle. Keep it that way.
LOG_UNITS = {
    "capture": "timelapse-capture",
    "encode": "timelapse-encode",
    "web": "timelapse-web",
}
LOG_LINES = {"200": "200", "1000": "1000"}
DEFAULT_LOG_UNIT = "capture"
DEFAULT_LOG_LINES = "200"

# ----------------------------------------------------------------------------
# Update check
#
# The one outbound connection this service makes, and the only one it ever
# should. It is opt-out (web.update_check), never blocks a page, and sends
# nothing but an HTTPS GET: no config, no camera names, no library contents.
# What GitHub learns is the deployment's IP and the version in the User-Agent.
# ----------------------------------------------------------------------------

# Unauthenticated GitHub allows 60 requests an hour per IP. One a day leaves
# that entirely to the operator, and a release is not a thing that happens
# hourly.
UPDATE_INTERVAL = 24 * 3600
# A failed check must not be cached like a successful one. A DNS blip lasting
# seconds would otherwise cost a day of not asking again, which is what
# happened to the first operator to hit it: an overloaded local resolver
# during an upgrade, and the panel then sat on the error until tomorrow.
# Retry soon, then back off, so a genuinely offline host settles at the normal
# daily rate instead of asking every quarter of an hour forever.
UPDATE_RETRY = 15 * 60
UPDATE_RETRY_MAX = UPDATE_INTERVAL

# One command now, because timelapse_update.py exists to be that command. The
# three-line curl form it replaced is still what the installer's own
# documentation says, and still works; this is the same thing with the version
# check and the confirmation attached.
UPDATE_COMMANDS = "sudo timelapse update"


def external(url, label):
    """A link that leaves this UI, and says so by opening in its own tab.

    Every other link here navigates within the server; this one hands the
    reader to GitHub. Replacing the page they were reading with it is the
    wrong move, and `noopener` is not optional on a target=_blank link: it
    stops the opened page reaching back through window.opener.
    """
    return (f'<a href="{escape(url)}" target="_blank" '
            f'rel="noopener noreferrer">{escape(label)}</a>')


def latest_label(u):
    """The version to print for "Latest", in the shape "Installed" uses.

    Two names for one thing: `latest` is normalised ("0.1.5") and `tag` is
    whatever the repo wrote ("v0.1.5"). Printing the tag put a v on one of two
    adjacent values in the same two-row list, which reads as a difference
    between them rather than as punctuation in one. The tag is kept only as
    the fallback for a cache that somehow has one without the other.
    """
    return u["latest"] or u["tag"]


class UpdateChecker:
    """Asks GitHub, at most once a day, whether there is a newer version.

    Checked lazily: a request for the overview kicks off a refresh only when
    the cached answer is stale, and renders whatever is already known. So a
    service nobody looks at never calls out at all, and a page render never
    waits on the network.

    The result is cached in state_dir, which is the one directory this service
    may write. Surviving a restart is the point: without it, a service that
    restarts often would ask on every boot and burn the rate limit.
    """

    def __init__(self, state_dir, enabled=True, current=__version__):
        self.enabled = bool(enabled)
        self.current = parse_version(current)
        self.current_text = current
        self.path = Path(state_dir) / "update.json" if state_dir else None
        self._lock = threading.Lock()
        self._busy = False
        # "checked" is the last SUCCESSFUL check and "attempted" the last try,
        # which are different things and used to be conflated. Recording a
        # failure as a check both gated the retry for a day and made the page
        # claim it had checked when it had not.
        # No `notes` here any more. The panel links to the release rather than
        # reproducing it, so caching the body would be storing something
        # nothing reads. A cache written by an older build simply has a key
        # this no longer copies out of it.
        self.state = {"checked": 0.0, "attempted": 0.0, "failures": 0,
                      "tag": "", "latest": "", "url": "", "error": ""}
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                self.state.update({k: saved[k] for k in self.state
                                   if k in saved})
                self._migrate(saved)
        except (OSError, ValueError):
            pass            # No cache yet, or a corrupt one. Ask again.

    def _migrate(self, saved):
        """Repair a cache written by 0.1.0, which set `checked` on failure.

        Without this the fix does not reach the people who hit the bug: their
        cached file records a failed attempt as a successful check, so the
        daily interval would still gate the retry and they would still wait a
        day. If the stored error is non-empty then the last write was a
        failure, `checked` is that failure's timestamp, and the time of the
        last real success is not recoverable. Treat it as one failure, which
        puts the next attempt a few minutes out.
        """
        if saved.get("error") and not saved.get("failures"):
            self.state["failures"] = 1
            self.state["attempted"] = saved.get("checked", 0.0)
            self.state["checked"] = 0.0

    def _save(self):
        if not self.path:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.state, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            # An unwritable state dir already has its own visible symptom.
            log.debug("Could not cache the update check: %s", exc)

    def snapshot(self):
        with self._lock:
            s = dict(self.state)
            s["busy"] = self._busy
        s["enabled"] = self.enabled
        s["current"] = self.current_text
        latest = parse_version(s["latest"])
        s["available"] = bool(latest and self.current and latest > self.current)
        # An upgrade run from the terminal moves the installed version without
        # touching this cache, so for up to a day the panel said "Installed
        # 0.1.4" beside "Latest 0.1.3", which reads as this checker being
        # wrong rather than merely old. Upstream cannot be behind what is
        # installed here: the tag is where the installer got it. So a cached
        # answer older than the running version is simply out of date, and the
        # version we can prove exists is the one running. The tag and the URL
        # go with it, because they name the older release and would otherwise
        # link "0.1.4" to 0.1.3's page. "Last successful check" still says how
        # old the answer is, which is the honest part of this.
        if latest and self.current and self.current > latest:
            s["latest"] = self.current_text
            s["tag"] = ""
            s["url"] = ""
        s["known"] = bool(s["checked"])
        s["retry_at"] = self._retry_at(s)
        return s

    @staticmethod
    def _retry_at(s):
        """When the next automatic attempt is due, as an epoch time."""
        if s["failures"]:
            wait = min(UPDATE_RETRY * (2 ** (s["failures"] - 1)),
                       UPDATE_RETRY_MAX)
            return s["attempted"] + wait
        return s["checked"] + UPDATE_INTERVAL

    def refresh(self, force=False):
        """Start a check in the background unless one is already running.

        force is the retry button: an operator looking at a failure should not
        have to wait out a backoff they can see is stale, because they usually
        know what they just fixed.
        """
        if not self.enabled:
            return False
        with self._lock:
            if self._busy:
                return False
            if not force and time.time() < self._retry_at(self.state):
                return False
            self._busy = True
        threading.Thread(target=self._check, daemon=True).start()
        return True

    def refresh_if_stale(self):
        return self.refresh(force=False)

    def _check(self):
        try:
            # One request, always. This used to fetch the changelog as well
            # when a tag had no Release behind it, to fill the "what is new"
            # panel; with that panel gone the second request has nothing to
            # fill, and the service's single outbound connection stays single.
            ver, tag, url, _ = latest_release()
            now = time.time()
            with self._lock:
                self.state.update(checked=now, attempted=now, failures=0,
                                  tag=tag, latest=version_text(ver),
                                  url=url, error="")
        except Exception as exc:                  # noqa: BLE001
            # Every failure here is somebody else's outage. Record it, keep
            # the last good answer, and never let it reach the page as a 500.
            # "checked" is deliberately not touched: nothing was checked, and
            # writing it here is what used to make a momentary DNS failure
            # cost a full day before the next attempt.
            with self._lock:
                self.state["attempted"] = time.time()
                self.state["failures"] = self.state["failures"] + 1
                self.state["error"] = friendly_error(exc)
            log.info("Update check failed: %s", exc)
        finally:
            with self._lock:
                self._busy = False
            self._save()


JOURNAL_DENIED = (
    "No entries. If the unit has been running, this process cannot read the "
    "journal rather than the journal being empty: journalctl shows nothing to "
    "a user outside the systemd-journal group, and says so no more loudly "
    "than this. Add SupplementaryGroups=systemd-journal to "
    "timelapse-web.service."
)


def run_command(argv):
    """Run a fixed argv. Returns (output, problem).

    `problem` is set only when the command could not be run at all. A non-zero
    exit is not a problem: `systemctl status` exits 3 for an inactive unit and
    4 for one that does not exist, and that output is precisely what the page
    is for. Treating those as failures would replace the answer with an error.

    Never shell=True, and argv is always built from constants.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=COMMAND_TIMEOUT)
    except FileNotFoundError:
        return "", (f"{argv[0]} is not installed here. The status pane needs "
                    f"systemd.")
    except subprocess.TimeoutExpired:
        return "", f"{argv[0]} did not answer within {COMMAND_TIMEOUT}s."
    except OSError as exc:
        return "", f"Could not run {argv[0]}: {exc}"

    # stderr matters as much as stdout: journalctl explains itself there.
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    # Redacted here rather than at each renderer, so that adding a page that
    # shows command output cannot reintroduce the leak by omission. Every
    # journal this service can read predates the fix by some amount, and the
    # entries already in it keep the password until the journal ages out.
    return redact(out), ""


def status_report():
    """`systemctl status` for every unit this project installs.

    --lines=0 suppresses the journal excerpt systemctl normally appends. That
    excerpt needs journal access, so without it the output looks mysteriously
    truncated; the logs page asks for logs explicitly instead.
    """
    argv = (["systemctl", "status", "--no-pager", "--lines=0"]
            + [name for name, _, _ in STATUS_UNITS])
    out, problem = run_command(argv)
    return {"command": " ".join(argv), "output": out, "problem": problem,
            "hint": ""}


def describe_unit(label, kind, props, name):
    """One row of the services table: (label, class, state, detail).

    Everything here is a translation, not a judgement: each branch reports one
    systemd state in the words an operator would use for it.
    """
    if not props:
        return (label, "bad", "Unknown", f"systemd reported nothing for {name}")

    load = props.get("LoadState", "")
    active = props.get("ActiveState", "")
    sub = props.get("SubState", "")

    if load == "not-found":
        return (label, "bad", "Not installed",
                "no unit file here; re-run the installer")
    if load == "masked":
        return (label, "bad", "Masked", f"sudo systemctl unmask {name}")

    if active == "failed":
        return (label, "bad", "Failed",
                f"{props.get('Result') or 'failed'}. The Recent log tab has "
                f"the reason.")
    if active in ("activating", "reloading"):
        if sub == "auto-restart":
            # Restart=always plus something that will not stay up. systemd
            # calls this "activating" forever; reporting it in calm grey as
            # "Starting" would be the wrong answer to "is it working".
            return (label, "bad", "Restarting",
                    "it keeps exiting. The Recent log tab has the reason.")
        if kind == "oneshot":
            # A oneshot is "activating" for the whole time its ExecStart runs,
            # which for the nightly encode is however long seven cameras take:
            # twenty minutes on the deployment, and more on a slow disk. The
            # daemon word for this state is "Starting", and an operator
            # watching it sit there for a quarter of an hour reads that as
            # stuck. It is not starting, it is doing the work. Verified on
            # systemd 255: activating/start, with the start time in
            # InactiveExitTimestamp and both other timestamps empty until it
            # finishes.
            began = props.get("InactiveExitTimestamp", "")
            return (label, "ok", "Running",
                    f"started {began}" if began else "in progress now")
        return (label, "", "Starting", "")
    if active == "deactivating":
        return (label, "", "Stopping", "")

    if active == "active":
        if kind == "oneshot" and sub == "exited":
            # RemainAfterExit leaves a finished job "active". Nothing is
            # running, so saying "Running" would be a plain untruth.
            when = props.get("ActiveEnterTimestamp", "")
            return (label, "", "Finished", f"ran at {when}" if when else "")
        if kind == "timer":
            detail = next_run_detail(props)
            last = (props.get("LastTriggerUSec") or "").strip()
            if last:
                # A repeating timer's last run is as reassuring as its next
                # one, and it is the half that proves it has ever worked.
                detail = f"{detail}; last ran {last}" if detail \
                    else f"last ran {last}"
            return (label, "ok", "Scheduled", detail)
        detail = ""
        since = props.get("ActiveEnterTimestamp", "")
        if since:
            detail = f"since {since}"
        if props.get("UnitFileState") == "disabled":
            # Running now, gone after the next reboot. The one state that
            # looks entirely healthy and is not.
            detail = (detail + "; " if detail else "") + \
                "not enabled, so it will not start again after a reboot"
        return (label, "ok", "Running", detail)

    if kind == "oneshot":
        # A finished oneshot is inactive, exactly as one that has never run
        # is, and the timestamp is what tells them apart. "Idle" was true of
        # both and useful about neither: the question this row answers is
        # whether last night's encode worked, so say so, in green. A run that
        # failed does not reach here; systemd leaves it ActiveState=failed.
        when = props.get("InactiveEnterTimestamp", "")
        if not when:
            return (label, "", "Not yet run",
                    "the timer has not fired since this was installed")
        result = props.get("Result", "success")
        if result and result != "success":
            return (label, "bad", "Failed", f"{result}, at {when}")
        return (label, "ok", "Successful", f"last finished {when}")
    return (label, "bad", "Stopped", f"start it with: sudo systemctl start {name}")


def unit_states():
    """A plain-language row per unit. Returns (rows, problem).

    `systemctl show` rather than `systemctl status`: it is the machine-readable
    half of the pair, it asks for exactly the fields the table needs, and it
    exits 0 even for a unit that is not installed. The human output is not a
    contract; parsing it would mean tracking whatever systemd prints this year.
    """
    argv = (["systemctl", "show", "--no-pager"]
            + [f"--property={p}" for p in STATUS_PROPS]
            + [name for name, _, _ in STATUS_UNITS])
    out, problem = run_command(argv)
    if problem:
        return [], problem

    # One block per unit, blank-line separated, in the order asked for. Keyed
    # by Id rather than by position anyway: a missing block would otherwise
    # shift every later unit onto the wrong row.
    props = {}
    for block in out.split("\n\n"):
        found = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            # run_command folds stderr in with stdout, and a diagnostic line
            # is not a property however much it may contain an "=".
            if sep and key.isidentifier():
                found[key] = value
        if found.get("Id"):
            props[found["Id"]] = found

    return ([describe_unit(label, kind, props.get(name), name)
             for name, label, kind in STATUS_UNITS], "")


# ----------------------------------------------------------------------------
# Runtime state, published by the daemons
#
# Read-only, like everything else this service touches. The files answer the
# one question the unit table cannot: a capture daemon whose every camera is
# refusing connections is `active (running)`, and so is one the disk guard has
# paused with nothing being written at all.
# ----------------------------------------------------------------------------

# Capture rewrites its heartbeat once a minute, so two missed writes is the
# earliest point at which silence means anything.
STATE_STALE_AFTER = 180



def read_state(cfg, filename):
    """(data, problem) for one state file.

    A missing file is not an error worth shouting about: it is what every
    install shows until the daemons have been restarted onto 0.1.6, and what a
    machine that has never run capture shows forever. It is reported as a
    plain sentence, not as a fault.
    """
    path = runtime_state_dir(cfg) / filename
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, "nothing has been published here yet"
    except OSError as exc:
        return None, f"cannot read {path.as_posix()}: {exc}"
    except ValueError:
        return None, f"{path.as_posix()} is not valid JSON"
    if not isinstance(data, dict):
        return None, f"{path.as_posix()} is not the shape this expects"
    # Forward compatibility, from the first release that has any: a newer
    # daemon may publish a format this build has never seen, and guessing at
    # it would put invented numbers on the page.
    # A file claiming a newer format than this build knows was written by a
    # newer daemon than this UI, which happens when the units are restarted on
    # an upgrade and this service is not. Reading it as if it were this format
    # would put invented numbers on a page whose whole value is being true.
    if data.get("version", 0) > STATE_VERSION:
        return None, (f"written by a newer version of timelapse-maker "
                      f"(format {data.get('version')}); restart "
                      f"timelapse-web.service to catch up")
    return data, ""


# How long a frame count is reused before the directory is walked again. The
# page is read by a person, so a count up to this old is indistinguishable
# from a live one, and it bounds the cost of somebody holding down refresh.
COUNT_TTL = 20

_counts = {}                    # (path, day) -> (counted_at, frames)
_counts_lock = threading.Lock()


def count_frames(day_dir):
    """How many frames are on disk for a day, or None if it cannot be read.

    Counted from the directory rather than taken from the daemon's own
    counter, which resets on every restart and so answers a question nobody
    asked: "48 frames" since a restart at 17:55 tells an operator nothing
    about whether today has been captured. The directory is the record.

    It also works for RTSP cameras, where the daemon *cannot* count: ffmpeg
    writes those frames and this program never sees them. Counting on disk is
    method-agnostic, which is what lets that column stop saying "-".

    None, not 0, when the directory is missing: "no directory" and "a
    directory with nothing in it" are the same thing to an operator, but
    "cannot read this" is a different claim and must not be dressed as zero.
    """
    key = (str(day_dir), int(time.time() // COUNT_TTL))
    with _counts_lock:
        hit = _counts.get(key)
    if hit is not None:
        return hit
    try:
        with os.scandir(day_dir) as it:
            # scandir over listdir: the name is enough, so this never stats a
            # single file, which is what keeps a 17,000-frame day cheap.
            frames = sum(1 for e in it if e.name.endswith(".jpg"))
    except FileNotFoundError:
        frames = 0
    except OSError:
        return None
    with _counts_lock:
        # Bounded by construction: keys carry a TTL bucket, so old ones can
        # never be looked up again. Clearing wholesale beats an LRU here.
        if len(_counts) > 512:
            _counts.clear()
        _counts[key] = frames
    return frames


def seconds_elapsed_today(now=None):
    """Seconds since local midnight. The denominator for today's coverage."""
    now = datetime.datetime.now() if now is None else now
    return (now - now.replace(hour=0, minute=0, second=0,
                              microsecond=0)).total_seconds()


def today_progress(cfg, cam, now=None):
    """(frames, coverage) for one camera so far today.

    The cadence comes from the day's own `.cadence.json` first, because a
    cadence edit lands at midnight and today may still be running on
    yesterday's answer; the heartbeat's value is the fallback. This is the
    same precedence the encoder uses, and for the same reason.
    """
    root = cfg.get("paths", {}).get("frames_root")
    if not root or not cam.get("name"):
        return None, None
    now = datetime.datetime.now() if now is None else now
    day_dir = Path(root) / str(cam["name"]) / now.strftime("%Y-%m-%d")
    frames = count_frames(day_dir)
    if frames is None:
        return None, None
    recorded = day_cadence(day_dir)
    interval = recorded[0] if recorded else cam.get("interval")
    return frames, coverage_of(frames, interval, seconds_elapsed_today(now))


def parse_stamp(text):
    """One of our own ISO stamps back to epoch seconds, or None."""
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def silence_seconds(cam, snapshot_epoch):
    """How long this camera had been quiet when the snapshot was taken.

    Measured against the snapshot rather than against now, deliberately: the
    heartbeat is written once a minute, so measuring against now would add up
    to a minute of the file's own age to every camera and make a perfectly
    healthy 5-second camera look 65 seconds quiet.
    """
    last = parse_stamp(cam.get("last_success"))
    if last is None or not snapshot_epoch:
        return None
    return max(0.0, snapshot_epoch - last)


def camera_verdict(cam, snapshot_epoch):
    """(css class, phrase) for one camera row.

    The judgement lives here rather than in the daemon: what counts as quiet
    depends on that camera's interval, and a file that had already decided
    could not be overruled by a reader that knows better.
    """
    if cam.get("supervised"):
        # An RTSP camera's frames are written by a separate process, so this
        # daemon has no last-frame time to judge and the honest answer is
        # whether that process is alive. Say that in the reader's terms: the
        # old wording named the program doing the work, which is an
        # implementation detail nobody reading a status page has asked about.
        # The Frames and Coverage columns now answer "is it working", counted
        # from disk, which is what that question was really standing in for.
        if cam.get("alive"):
            return "ok", "recording"
        return "bad", "not recording"

    # A camera that is refusing our credentials is silent for a reason this
    # page can state, and "3h ago" would send somebody looking at the network.
    # Only a confirmed refusal is shown: before that the daemon is still
    # deciding, and a camera part way through a reboot would be libelled.
    err = cam.get("error") or {}
    if err.get("class") == "auth" and err.get("confirmed"):
        return "bad", "refusing our credentials"

    quiet = silence_seconds(cam, snapshot_epoch)
    if quiet is None:
        return "warn", "no frame yet"
    interval = cam.get("interval") or 0
    # Two intervals of grace, and never less than 15 seconds: at a 5-second
    # cadence a single slow fetch would otherwise paint the row red.
    limit = max(15.0, (interval or 5) * 2)
    if quiet <= limit:
        return "ok", f"{human_age(quiet)} ago"
    return "bad", f"{human_age(quiet)} ago"


def human_age(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


# systemd prints a monotonic timestamp as a timespan since boot, e.g.
# "5min 1.016502s" or "1h 2min 3s". Documented in systemd.time(7), which is
# what makes this safe to parse where `systemctl status` output is not.
TIMESPAN_UNITS = {
    "us": 1e-6, "usec": 1e-6, "ms": 1e-3, "msec": 1e-3,
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}
TIMESPAN_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")


def parse_timespan(text):
    """A systemd timespan to seconds, or None if it says nothing useful.

    None for "infinity" as well as for junk: a timer whose next elapse is
    infinity will not fire again, which is a real answer but not a duration,
    and the caller says so in words.
    """
    text = (text or "").strip().lower()
    if not text or text == "infinity":
        return None
    total = 0.0
    matched = 0
    for value, unit in TIMESPAN_TOKEN.findall(text):
        if unit in TIMESPAN_UNITS:
            total += float(value) * TIMESPAN_UNITS[unit]
            matched += 1
    return total if matched else None


def next_run_detail(props, now_monotonic=None):
    """When a timer fires next, in whichever form systemd offers it.

    A calendar timer (`OnCalendar`, the nightly encode) answers with a real
    timestamp. A monotonic one (`OnBootSec`/`OnUnitActiveSec`, the credential
    watch) answers with a span since boot and leaves the realtime property
    empty, so a reader of only that saw an empty cell and reasonably wondered
    whether the unit was broken. Reported by the operator 2026-08-14.
    """
    stamp = (props.get("NextElapseUSecRealtime") or "").strip()
    if stamp:
        return f"next run {stamp}"

    raw = (props.get("NextElapseUSecMonotonic") or "").strip()
    if raw.lower() == "infinity":
        return "no further runs scheduled"
    since_boot = parse_timespan(raw)
    if since_boot is None:
        return ""
    now = time.monotonic() if now_monotonic is None else now_monotonic
    left = since_boot - now
    # Both properties are CLOCK_MONOTONIC on Linux, so this subtraction is
    # exact. A tiny negative is a timer about to fire, not an error.
    if left <= 1:
        return "due now"
    return f"next run in {human_age(left)}"


def journal_report(unit_key, lines_key):
    unit = LOG_UNITS.get(unit_key, LOG_UNITS[DEFAULT_LOG_UNIT])
    lines = LOG_LINES.get(lines_key, LOG_LINES[DEFAULT_LOG_LINES])
    argv = ["journalctl", "-u", unit, "-n", lines, "--no-pager"]
    out, problem = run_command(argv)

    # -f would never return and would hang the request until the client gave
    # up; the `timelapse logs` wrapper follows, and this deliberately does not.
    hint = ""
    if not problem and out.strip().lower().strip("- ") in ("no entries", ""):
        hint = JOURNAL_DENIED
    return {"command": " ".join(argv), "output": out, "problem": problem,
            "hint": hint}


# ----------------------------------------------------------------------------
# Login
#
# One optional user, and deliberately modest about what it is for: it keeps a
# household out of the pages, not an attacker off the host. There is no TLS
# here, so the password crosses the LAN in clear; the videos themselves stay
# reachable by path (see the gate in do_GET); and this is stated in the UI and
# the docs rather than left for somebody to discover.
#
# Configured means `web.auth.username` and `web.auth.password_hash` are both
# set. Absent or blank, every route behaves exactly as it did before this
# existed, which is what an upgrade must keep doing.
# ----------------------------------------------------------------------------

# The credential is *verified* here, never replayed to anything, so it is
# hashed - the exact inverse of the camera passwords, which have to be
# presented to the camera and therefore have to be kept. PBKDF2 rather than
# scrypt: it is always present, needs no memory tuning, and works on the 3.9
# floor. Measured on Linux, 600k iterations is 0.09s on a workstation, so a
# login on a recorder that is also running an NVR stays well under a second
# while an offline guessing run pays that per attempt.
PBKDF2_NAME = "pbkdf2_sha256"
PBKDF2_ITERS = 600_000
SALT_BYTES = 16

SESSION_COOKIE = "tl_session"
# The session ends at logout, which is what the operator asked for. This is
# the backstop for the browser that is never coming back: a tab left open on a
# machine that is then repurposed should not stay logged in forever.
SESSION_IDLE = 30 * 24 * 3600
# One entry per browser that has ever logged in, and they only leave on logout
# or expiry, so a script with the right password could accumulate them. A cap
# with the oldest dropped first keeps that bounded; nobody has 64 browsers.
MAX_SESSIONS = 64

# A wrong password costs three seconds and nothing else. Attempts are never
# capped and nothing is ever locked: what is behind this is a status page and
# a list of video files, and a lockout would mostly succeed at infuriating the
# household member who mistyped, which helps nobody. Three seconds is plenty
# against somebody guessing at a keyboard, and this does not pretend to be
# more than that: a script guessing in parallel is barely slowed, because the
# delay is per request rather than global.
LOGIN_DELAY = 3.0

# The only request body this server reads. A username and a password do not
# reach four kilobytes, and a length the client chose is not a length to
# allocate on trust.
FORM_LIMIT = 4096


def safe_next(where):
    """A request-supplied path to return to after logging in, or "/".

    Only a path on this server: a value starting "//" or carrying a scheme is
    somebody else's site, and a login page that forwards to one is an open
    redirect. Anything that is not plainly a local path becomes the home page,
    which is a harmless place to be sent.
    """
    where = (where or "").strip()
    if (not where.startswith("/") or where.startswith("//")
            or "\\" in where or any(c < " " for c in where)):
        return "/"
    return where


def hash_password(password, iters=PBKDF2_ITERS, salt=None):
    """`pbkdf2_sha256$<iters>$<salt>$<key>`, all base64.

    Self-describing on purpose: the iteration count and the salt travel with
    the hash, so raising the count later leaves every existing config working
    instead of locking its owner out.
    """
    salt = os.urandom(SALT_BYTES) if salt is None else salt
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return (f"{PBKDF2_NAME}${iters}${base64.b64encode(salt).decode()}"
            f"${base64.b64encode(key).decode()}")


def verify_password(stored, password):
    """True if `password` produced `stored`. Never raises.

    A malformed or hand-edited hash is a no, not a traceback: this runs inside
    a request handler, and the alternative is a 500 on the login page.
    """
    parsed = parse_password_hash(stored)
    if parsed is None:
        return False
    iters, salt, key = parsed
    got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters)
    return hmac.compare_digest(got, key)


def parse_password_hash(stored):
    """(iterations, salt, key) for a hash this build can check, else None.

    Asked at startup as well as at each login: a config carrying a hash
    nobody can verify locks its owner out of their own page, and the useful
    moment to say so is while somebody is watching the service start, not at
    the first attempt to log in.
    """
    parts = str(stored).split("$")
    if len(parts) != 4 or parts[0] != PBKDF2_NAME:
        return None
    try:
        iters = int(parts[1])
        salt = base64.b64decode(parts[2], validate=True)
        key = base64.b64decode(parts[3], validate=True)
    except (ValueError, binascii.Error):
        return None
    if iters < 1 or not salt or not key:
        return None
    return iters, salt, key


class Auth:
    """The optional single user, their sessions, and the guessing throttle.

    Sessions live in memory and nowhere else. That keeps `state_dir` holding
    exactly what it held before (the index and the update cache), so the one
    writable directory this service has stays scoped to things it can rebuild.
    The cost is that a restart logs everybody out, which for a service that
    restarts on upgrades and wizard runs is a fair trade and easy to explain.
    """

    def __init__(self, username="", password_hash="", idle=SESSION_IDLE,
                 fail_delay=LOGIN_DELAY):
        self.username = username or ""
        self.password_hash = password_hash or ""
        self.idle = idle
        # An attribute rather than the constant, so the tests can turn it off.
        # Nine tests that each waited three real seconds would be half a minute
        # of suite time spent proving that sleep() sleeps.
        self.fail_delay = fail_delay
        self._lock = threading.Lock()
        self._sessions = {}         # token -> last seen (monotonic)

    @classmethod
    def from_config(cls, cfg):
        """Build from `web.auth`, or a disabled instance when it is not set.

        Raises ValueError for a username with a hash this build cannot check.
        Refusing to start is the fail-closed answer: carrying on without the
        login would serve the pages to everyone precisely because the operator
        asked for the opposite.
        """
        # .get throughout: an upgrade keeps the existing config.json, and every
        # install that predates this has no `auth` key at all.
        auth = (cfg.get("web", {}) or {}).get("auth", {}) or {}
        user = (auth.get("username") or "").strip()
        stored = (auth.get("password_hash") or "").strip()
        if not user or not stored:
            return cls()
        if parse_password_hash(stored) is None:
            raise ValueError(
                "web.auth.password_hash is not a hash this version can check. "
                "Run `sudo timelapse password` to set the login again, or "
                "`sudo timelapse web` to turn it off.")
        return cls(user, stored)

    @property
    def enabled(self):
        return bool(self.username and self.password_hash)

    # -- the credential ------------------------------------------------------

    def check(self, username, password):
        """Whether these are the configured credentials.

        The hash is computed even when the username is wrong. It costs a
        tenth of a second on a request that is already failing, and it keeps
        a wrong *username* from answering faster than a wrong password.
        """
        if not self.enabled:
            return False
        ok_pw = verify_password(self.password_hash, password or "")
        ok_user = hmac.compare_digest((username or "").encode("utf-8"),
                                      self.username.encode("utf-8"))
        return ok_user and ok_pw

    # -- sessions ------------------------------------------------------------

    def open_session(self):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._prune()
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.get)
                del self._sessions[oldest]
            self._sessions[token] = time.monotonic()
        return token

    def valid(self, token):
        """True for a live session, and touches it so the idle clock restarts.

        monotonic() rather than time(): an NTP correction or a manual clock
        change must not expire everybody, and this machine boots without a
        battery-backed clock more often than not.
        """
        if not token:
            return False
        with self._lock:
            self._prune()
            if token not in self._sessions:
                return False
            self._sessions[token] = time.monotonic()
            return True

    def close_session(self, token):
        """Forget a session. Logout is revocation here, not just a cleared
        cookie: a token copied out of the browser stops working too."""
        with self._lock:
            self._sessions.pop(token, None)

    def _prune(self):
        cutoff = time.monotonic() - self.idle
        for token in [t for t, seen in self._sessions.items() if seen < cutoff]:
            del self._sessions[token]

    @property
    def session_count(self):
        with self._lock:
            self._prune()
            return len(self._sessions)

    # -- what a wrong password costs -----------------------------------------

    def pause_after_failure(self):
        """Wait out the delay a failed attempt earns.

        No counter, no lockout, no state per client at all. Attempts are
        unlimited: a locked account would be infuriating for the person who
        mistyped and would still not keep out anybody serious, and what is
        behind this is a status page and a list of video files.

        The wait happens before the answer goes back, so it costs the caller
        the three seconds rather than merely delaying our own bookkeeping. It
        holds this request's thread while it waits, which is affordable
        precisely because nothing here is defending against a flood.
        """
        if self.fail_delay > 0:
            time.sleep(self.fail_delay)


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------

LAYOUT = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>timelapse-maker</title>
<style>
  :root {{ color-scheme: light dark; }}
  /* Reserve the scrollbar's width whether or not the page needs one. Without
     it, anything centred sits ~8px further left on a page long enough to
     scroll (the library) than on one that is not (the overview), and the tabs
     shift as you move between them. */
  html {{ scrollbar-gutter: stable; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 2rem 1.25rem;
         max-width: 54rem; margin-inline: auto; }}
  h1 {{ font-size: 1.3rem; margin: 0; }}
  h2 {{ font-size: .95rem; text-transform: uppercase; letter-spacing: .06em;
        opacity: .6; margin: 0 0 .6rem; }}
  /* Centred, not left-aligned in the content column. The title and the tabs
     are the fixed furniture of every page, and the pages are not all the same
     width: status and logs drop the 54rem column for the whole window, which
     moved both to the far left. Centring on the viewport makes their position
     independent of whatever the page below them is doing. */
  header {{ display: flex; align-items: baseline; gap: .75rem;
            justify-content: center;
            border-bottom: 1px solid rgba(128,128,128,.3);
            padding-bottom: .9rem; margin-bottom: 1.5rem; }}
  .ver {{ opacity: .55; font-size: .85rem; }}
  section {{ border: 1px solid rgba(128,128,128,.3); border-radius: 8px;
             padding: 1rem 1.1rem; margin-bottom: 1rem; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr;
        gap: .35rem .9rem; margin: 0; }}
  dt {{ opacity: .6; }}
  dd {{ margin: 0; overflow-wrap: anywhere; }}
  code {{ font-family: ui-monospace, monospace; font-size: .9em; }}
  .note {{ margin: .8rem 0 0; padding: .6rem .75rem; border-radius: 6px;
           background: rgba(200,140,0,.14); font-size: .9rem; }}
  .ok {{ color: #1a7f37; }} .bad {{ color: #b3261e; }}
  @media (prefers-color-scheme: dark) {{
    .ok {{ color: #4ac26b; }} .bad {{ color: #ff7b72; }}
  }}
  ul.todo {{ margin: 0; padding-left: 1.1rem; opacity: .65; font-size: .9rem; }}
  nav {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1.25rem;
         justify-content: center; }}
  nav a {{ text-decoration: none; border: 1px solid rgba(128,128,128,.35);
           border-radius: 999px; padding: .25rem .8rem; font-size: .9rem;
           color: inherit; }}
  nav a.on {{ background: rgba(128,128,128,.18); font-weight: 600; }}
  /* Log out is the only thing in this bar that is not a destination, and the
     only one that is expensive to hit by mistake. It sat beside "Recent log",
     the two of them sharing the word "log", and the operator reported hitting
     it repeatedly while trying to open the log (2026-08-14). So it is spaced
     away from the tabs and coloured as an action rather than dressed as one
     more of them. */
  nav a.signout {{ margin-left: 3rem; background: #8c1d18; color: #fff;
                   border-color: #8c1d18; }}
  nav a.signout:hover {{ background: #6f1713; border-color: #6f1713; }}
  @media (max-width: 30rem) {{
    /* Narrow enough that the bar wraps: the button is then on its own line
       and already separated, so the gap would only push it off-centre. */
    nav a.signout {{ margin-left: .5rem; }}
  }}
  pre {{ overflow-x: auto; background: rgba(128,128,128,.12); border-radius: 6px;
         padding: .8rem .9rem; font-size: .82rem; line-height: 1.45;
         margin: 0; }}
  /* Command output pages (status, logs). The 54rem column above is right for
     prose and tables and wrong for raw output: a journal line is as wide as
     journald decided, so it is the window that should be the limit. Height is
     capped at the viewport too, because a <pre> that grows to its content puts
     its horizontal scrollbar hundreds of lines below the text it scrolls.
     dvh after vh, so a browser without dvh still gets a bounded pane. */
  body.pane-page {{ max-width: none; box-sizing: border-box;
                    height: 100vh; height: 100dvh;
                    display: flex; flex-direction: column; }}
  body.pane-page > :not(section.pane) {{ flex: none; }}
  /* min-height: 0 twice, and both are load-bearing: a flex item's default
     min-height is its content, so without it the pre keeps its full height
     and nothing ever scrolls. */
  body.pane-page section.pane {{ display: flex; flex-direction: column;
                                 min-height: 0; }}
  body.pane-page section.pane > :not(pre) {{ flex: none; }}
  body.pane-page section.pane pre {{ min-height: 0; overflow: auto; }}
  .cmd {{ font-size: .8rem; opacity: .55; margin: 0 0 .5rem; }}
  .sub {{ display: flex; gap: .5rem; flex-wrap: wrap; margin: 0 0 .8rem;
          font-size: .85rem; }}
  .sub a {{ color: inherit; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
  th {{ text-align: left; font-weight: 600; opacity: .6; font-size: .8rem;
        text-transform: uppercase; letter-spacing: .04em;
        border-bottom: 1px solid rgba(128,128,128,.3); padding: .3rem .5rem; }}
  td {{ padding: .3rem .5rem; border-bottom: 1px solid rgba(128,128,128,.14);
        vertical-align: top; }}
  td.num {{ text-align: right; font-variant-numeric: tabular-nums;
            white-space: nowrap; }}
  td.dim {{ opacity: .6; font-size: .85em; }}
  summary {{ cursor: pointer; font-size: .85rem; opacity: .6; }}
  .wrap {{ overflow-x: auto; }}
  .path {{ font-family: ui-monospace, monospace; font-size: .8rem;
           user-select: all; overflow-wrap: anywhere; opacity: .8; }}
  .flag {{ color: #b3261e; font-weight: 600; }}
  td.acts {{ white-space: nowrap; }}
  td.acts a {{ color: inherit; margin-right: .5rem; }}
  tr.sub-row td {{ border-bottom: 1px solid rgba(128,128,128,.14);
                   padding-top: 0; }}
  .scan {{ font-size: .85rem; opacity: .7; margin: 0 0 1rem; }}
  .new {{ background: rgba(26,127,55,.14); }}
  .quiet {{ font-size: .8rem; opacity: .55; margin: .6rem 0 0; }}
  form.inline {{ display: inline; }}
  button {{ font: inherit; font-size: .85rem; padding: .25rem .8rem;
            border-radius: 999px; border: 1px solid rgba(128,128,128,.35);
            background: transparent; color: inherit; cursor: pointer; }}
  /* The login form. One column, so it reads the same on a phone as on a
     desktop, and the section keeps its own width rather than the 54rem the
     content column would give two fields. */
  .signin {{ max-width: 22rem; margin-inline: auto; }}
  .signin label {{ display: block; font-size: .85rem; opacity: .6;
                   margin: .8rem 0 .2rem; }}
  .signin input {{ font: inherit; width: 100%; box-sizing: border-box;
                   padding: .4rem .6rem; border-radius: 6px;
                   border: 1px solid rgba(128,128,128,.45);
                   background: transparent; color: inherit; }}
  .signin button {{ margin-top: 1rem; }}
  .bad {{ color: #b3261e; }}
  @media (prefers-color-scheme: dark) {{
    .flag, .bad {{ color: #ff7b72; }} }}
</style>
<body class="{body_class}">
<header>
  <h1>timelapse-maker</h1>
  <span class="ver">{version}</span>
</header>
{nav}
{content}
"""

# Split out of LAYOUT so the login page can render with the same stylesheet
# and no navigation: every tab on it would bounce straight back here, which
# reads as a broken page rather than as a locked one.
NAV = """<nav>
  <a href="/" class="{on_home}">Overview</a>
  <a href="/library" class="{on_library}">Library</a>
  <a href="/logs" class="{on_logs}">Recent log</a>
  {logout}
</nav>"""

# autocomplete on both fields, so a password manager fills this the way it
# fills any other login. The note is not decoration: somebody typing a
# password into a page has a right to know it is not going over TLS, and that
# the videos are reachable without it. Saying so here costs nothing and stops
# this looking like more protection than it is.
LOGIN_FORM = """<section class="signin">
  <h2>Log in</h2>
  {error}
  <form method="post" action="/login">
    <input type="hidden" name="next" value="{next}">
    <label for="u">Username</label>
    <input id="u" name="username" autocomplete="username" autofocus>
    <label for="p">Password</label>
    <input id="p" name="password" type="password"
           autocomplete="current-password">
    <button type="submit">Log in</button>
  </form>
  <p class="quiet">This keeps the pages to whoever knows the password. It is
  not encrypted, so use it on a network you trust, and note that the video
  files themselves stay reachable to anyone who knows their exact address:
  that is what lets a saved playlist keep working in VLC.</p>
</section>"""

# Emitted only while a scan is running, and only into a full page. A rendered
# fragment cannot poll for itself, so this replaces the banner in place until
# the scan ends and then reloads once so the tables below it catch up.
#
# It polls /scan, which reads an in-memory dict and touches no filesystem.
# The obvious no-JS alternative, <meta http-equiv="refresh">, was rejected: it
# would re-request whichever library view is open, and on a folder view that
# means reconcile_dir() hitting a CIFS share once a second during the very
# scan it is competing with. Without JS the banner behaves as it always has,
# a server-rendered snapshot, which is what the Rescan button gives you.
# The update check runs on a thread, so the first view of the overview would
# otherwise show "checking" and sit there. Same shape as the scan poller, minus
# the reload: only this one section changes, so replacing it is enough.
UPDATE_POLL_JS = """<script>
(function () {
  var box = document.getElementById('update');
  if (!box || box.dataset.busy !== '1' || !window.fetch) { return; }
  var tries = 0;
  var timer = setInterval(function () {
    if (++tries > 30) { clearInterval(timer); return; }
    fetch('/update', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) { throw new Error(r.status); }
      return r.text();
    }).then(function (html) {
      box.outerHTML = html;
      box = document.getElementById('update');
      if (!box || box.dataset.busy !== '1') { clearInterval(timer); }
    }).catch(function () { clearInterval(timer); });
  }, 1000);
})();
</script>"""

SCAN_POLL_JS = """<script>
(function () {
  var box = document.getElementById('scan');
  if (!box || box.dataset.running !== '1') { return; }
  // No fetch means a browser old enough that throwing once a second into its
  // console is the only thing this would achieve. The static banner stands.
  if (!window.fetch) { return; }
  var timer = setInterval(function () {
    fetch('/scan', {cache: 'no-store'}).then(function (r) {
      if (!r.ok) { throw new Error(r.status); }
      return r.text();
    }).then(function (html) {
      box.outerHTML = html;
      // outerHTML replaces the node, so the old reference is now detached.
      box = document.getElementById('scan');
      if (!box || box.dataset.running !== '1') {
        clearInterval(timer);
        location.reload();
      }
    }).catch(function () {
      // A restart or a dropped connection: stop rather than hammer it.
      clearInterval(timer);
    });
  }, 1000);
})();
</script>"""

OVERVIEW = """<section>
  <h2>Video library</h2>
  <dl>
    <dt>Location</dt><dd><code>{lib_path}</code></dd>
    <dt>Resolved from</dt><dd>{lib_source}</dd>
    <dt>Readable</dt><dd class="{lib_class}">{lib_state}</dd>
  </dl>
  {lib_note}
</section>

{update}

{cameras}

{encode}

{services}
"""

# Said once, here, rather than in three branches: an operator upgrading from
# 0.1.5 sees this until the daemons are restarted, and "nothing is published
# yet" on its own reads like a fault rather than like a version skew.
STATE_MISSING = ('This needs the capture and encode services from 0.1.6 or '
                 'later. If you have just upgraded, they publish it once they '
                 'have been restarted.')


class Handler(BaseHTTPRequestHandler):

    # Keep-alive needs an accurate Content-Length on every response, which the
    # helpers below always send.
    protocol_version = "HTTP/1.1"

    timeout = SOCKET_TIMEOUT

    # Default is "BaseHTTP/x.y Python/3.z", which advertises the interpreter
    # version to anything that connects. Nothing needs to know that.
    server_version = "timelapse-web"
    sys_version = ""

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # Nothing here is cacheable: the whole point is what is true right now.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        route = path.rstrip("/") or "/"
        # keep_blank_values, because an empty value is a real answer here and
        # not an absent one. The library root's folder is "" and files with no
        # camera in the name group under camera "", so `?folder=` and
        # `?camera=` are the only way to reach two groups the index itself
        # offers links to. Dropping them silently fell through to the home
        # page, which looked like the group being empty.
        args = parse_qs(query, keep_blank_values=True)

        if not self._authorised(route):
            self._deny(route, self.path)
            return

        if route == "/login":
            # With no login configured this page is a form that can log
            # nobody in, so the honest answer is the page they wanted.
            if self.server.auth.enabled:
                self._send(200, self._login_page(args))
            else:
                self._redirect("/")
            return
        if route == "/logout":
            self._logout()
            return

        # Prefix routes first: the rest of the path is a library-relative file
        # path, which may contain slashes, spaces and non-ASCII.
        if route.startswith("/video/"):
            self._serve_file(unquote(route[len("/video/"):]), "download" in args)
            return
        if route.startswith("/play/"):
            self._serve_m3u(unquote(route[len("/play/"):]))
            return
        if route.startswith("/day/"):
            self._serve_day_m3u(unquote(route[len("/day/"):]))
            return

        if route == "/":
            self._send(200, self._render("home", self._overview()))
        elif route == "/library":
            self._send(200, self._render("library", self._library(args)))
        elif route == "/status":
            # No longer a tab: the four-row summary lives at the foot of the
            # overview. This is the full `systemctl status` behind it, kept as
            # a page of its own so the overview shells out once rather than
            # twice and carries one table rather than a screen of output that
            # is almost never opened. Old bookmarks still land somewhere
            # useful, which is the other reason not to fold it away entirely.
            self._send(200, self._render("home", self._status_detail()))
        elif route == "/logs":
            self._send(200, self._render("logs", self._logs(args)))
        elif route == "/scan":
            # The banner alone, for the poller. Deliberately cheap: it reads
            # an in-memory dict and touches neither the database nor the
            # library, so polling it once a second during a scan costs
            # nothing and never competes with the scan for the share.
            self._send(200, self._scan_banner(with_script=False))
        elif route == "/update":
            # The version panel alone, for its poller. Reads cached state; the
            # network call it is waiting on is already on its own thread.
            self._send(200, self._update_panel(with_script=False))
        elif route == "/healthz":
            self._send(200, "ok\n", "text/plain; charset=utf-8")
        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self):
        """The three actions a read-only UI has, all about its own state:
        rescan the index, retry the update check, and log in.

        POST rather than links so a crawler, a prefetch or a refresh cannot
        set any of them going. None changes anything outside our own state
        directory, and the update retry is the only one that leaves the host
        at all.
        """
        route = self.path.split("?", 1)[0].rstrip("/") or "/"

        # Read the body here, once, whatever the route. Only /login has one,
        # but under keep-alive an unread body is the *next* request as far as
        # the parser is concerned, so leaving it in the buffer would turn a
        # stray POST into a corrupted session rather than an ignored one.
        form = self._read_form()
        if form is None:
            # Refused rather than parsed, so the stream is no longer at a
            # request boundary and this connection cannot be reused.
            self.close_connection = True
            self._send(413, "request too large\n", "text/plain; charset=utf-8")
            return

        if not self._authorised(route):
            # No `next` for a POST: the target is an action, not a page, and
            # sending somebody to a GET of /rescan after logging in would land
            # them on a 404. The home page has the button.
            self._deny(route, "/")
            return

        if route == "/login":
            self._login(form)
            return
        if route == "/logout":
            self._logout()
            return

        if route == "/rescan":
            self.server.index.start_scan("requested")
            self._redirect("/library")
        elif route == "/check-update":
            # force: the point of the button is to skip a backoff the operator
            # can see is stale, usually because they just fixed the thing.
            self.server.updates.refresh(force=True)
            self._redirect("/")
        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    def _redirect(self, where, cookie=None):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    # -- the login gate ------------------------------------------------------
    #
    # What this is for, stated once: keeping a household out of the pages. It
    # is not a defence against an attacker, and it is not sold as one. There
    # is no TLS, so the password crosses the LAN in clear.
    #
    # /video/ is deliberately outside the gate. VLC is a separate process with
    # no cookie jar, and the .m3u handoff is what the library page exists for;
    # gating the bytes would break every Play link, and a saved playlist would
    # stop working the moment the session ended. So the pages, the index, the
    # status and the logs need the login; a video file still answers anyone
    # who knows its exact path.

    OPEN_PREFIXES = ("/video/",)
    # /healthz so a monitor needs no credential; /login and /logout because
    # they are how you get in and out.
    OPEN_ROUTES = ("/login", "/logout", "/healthz")
    # Fragments fetched by the pollers rather than navigated to. They must be
    # refused, not redirected: a 303 to /login would have the poller splice a
    # login page into the panel it is refreshing.
    FRAGMENT_ROUTES = ("/scan", "/update")

    def _authorised(self, route):
        auth = self.server.auth
        if not auth.enabled:
            return True
        if route in self.OPEN_ROUTES or route.startswith(self.OPEN_PREFIXES):
            return True
        return auth.valid(self._cookie_token())

    def _deny(self, route, wanted):
        if route in self.FRAGMENT_ROUTES:
            self._send(401, "log in\n", "text/plain; charset=utf-8")
            return
        if not wanted or wanted == "/":
            # Where the login sends you by default anyway. A `?next=%2F` on
            # the address bar is noise about nothing.
            self._redirect("/login")
            return
        self._redirect(f"/login?next={quote(wanted, safe='')}")

    def _cookie_token(self):
        """The session token this request carries, or "".

        get_all rather than get: a proxy is allowed to split the cookies over
        several headers, and taking only the first would drop the session for
        anybody running behind one.
        """
        for raw in (self.headers.get_all("Cookie") or []):
            try:
                jar = SimpleCookie()
                jar.load(raw)
            except CookieError:
                continue        # Junk from somebody else's cookie, not ours.
            morsel = jar.get(SESSION_COOKIE)
            if morsel and morsel.value:
                return morsel.value
        return ""

    def _https(self):
        """Whether the client's leg of this connection is TLS.

        Only a reverse proxy can answer that, since this server never speaks
        it. Same header, and the same "anything else is http", as _base_url().
        """
        return (self.headers.get("X-Forwarded-Proto") or "").strip().lower() \
            == "https"

    def _session_cookie(self, token, clear=False):
        """The Set-Cookie value for starting or ending a session.

        Secure is conditional and must stay so: set unconditionally, a browser
        would drop the cookie on the plain HTTP this service actually serves,
        and the login would silently never take. SameSite=Lax is what keeps
        another site from posting to /rescan with this cookie attached.
        """
        parts = [f"{SESSION_COOKIE}={'' if clear else token}", "Path=/",
                 "HttpOnly", "SameSite=Lax",
                 f"Max-Age={0 if clear else int(SESSION_IDLE)}"]
        if self._https():
            parts.append("Secure")
        return "; ".join(parts)

    def _read_form(self):
        """A parsed urlencoded body, {} when there is none, None when refused.

        Bounded: this is the only body this server has ever read, and reading
        a length somebody else chose is how a small service becomes a way to
        exhaust its own memory.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > FORM_LIMIT:
            return None
        if not length:
            return {}
        raw = self.rfile.read(length)
        return parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)

    @staticmethod
    def _first(form, key):
        values = (form or {}).get(key) or [""]
        return values[0]

    def _login(self, form):
        auth = self.server.auth
        if not auth.enabled:
            # Nothing to log in to. Sending them to the page they wanted beats
            # a form that cannot do anything.
            self._redirect("/")
            return

        if not auth.check(self._first(form, "username"),
                          self._first(form, "password")):
            # Three seconds, every time, and then the form back. Never a
            # lockout: see pause_after_failure().
            auth.pause_after_failure()
            # One message for both halves. Saying which was wrong tells an
            # unwelcome guest which half to keep working on, and tells the
            # legitimate user nothing they cannot work out by retrying.
            #
            # Nothing about the attempt is logged: the password would be the
            # interesting part and it must never reach the journal, and the
            # rest is a line per wrong guess in a log somebody reads for
            # encode failures.
            self._send(401, self._login_page(
                {}, error="That username and password did not match.",
                nxt=self._first(form, "next")))
            return

        token = auth.open_session()
        self._redirect(safe_next(self._first(form, "next")),
                       cookie=self._session_cookie(token))

    def _logout(self):
        """End the session, both halves: the cookie goes and so does the
        token, so a copy of it taken from the browser stops working too."""
        self.server.auth.close_session(self._cookie_token())
        self._redirect("/", cookie=self._session_cookie("", clear=True))

    def _login_page(self, args, error="", nxt=None):
        nxt = safe_next(nxt if nxt is not None else self._first(args, "next"))
        problem = f'<p class="bad">{escape(error)}</p>' if error else ""
        return self._render("login", LOGIN_FORM.format(
            error=problem, next=escape(nxt)), nav=False)

    # Pages whose body is one pane of raw command output, which wants the whole
    # window rather than the reading column the rest of the UI uses. The status
    # page left this list when it stopped being raw output.
    PANE_PAGES = ("logs",)

    def _render(self, page, content, nav=True):
        # The logout link appears only when there is a session to end. With no
        # login configured it would be a control that does nothing, and on the
        # login page itself it would offer to leave somewhere nobody is.
        logout = ('<a href="/logout" class="signout">Log out</a>'
                  if self.server.auth.enabled else "")
        bar = NAV.format(
            on_home="on" if page == "home" else "",
            on_library="on" if page == "library" else "",
            on_logs="on" if page == "logs" else "",
            logout=logout,
        ) if nav else ""
        return LAYOUT.format(
            version=__version__,
            body_class="pane-page" if page in self.PANE_PAGES else "",
            nav=bar,
            content=content,
        )

    def _overview(self):
        # Re-resolved per request rather than cached at startup: a NAS mount
        # comes and goes, and a page that reports a stale answer is worse than
        # no page. It is two stat() calls.
        lib = resolve_library(self.server.cfg)
        note = f'<p class="note">{escape(lib["note"])}</p>' if lib["note"] else ""
        # Kicked off here rather than at startup: a service nobody looks at
        # never calls out, and this returns immediately either way.
        self.server.updates.refresh_if_stale()
        return OVERVIEW.format(
            lib_path=escape(str(lib["path"]) if lib["path"] else "-"),
            lib_source=escape(lib["source"] or "-"),
            lib_class="ok" if lib["usable"] else "bad",
            lib_state="yes" if lib["usable"] else "no",
            lib_note=note,
            update=self._update_panel(),
            cameras=self._cameras(),
            encode=self._last_encode(),
            services=self._services(),
        )

    def _update_panel(self, with_script=True):
        """The version section, wrapped so the poller can replace it."""
        u = self.server.updates.snapshot()
        # Both values plain, like every other <dd> on the page. They were a
        # <code> and a bare string, which put two fonts side by side in one
        # two-row list and read as a rendering fault. Colour carries the only
        # difference that means anything here.
        body = [f'<h2>Version</h2><dl><dt>Installed</dt>'
                f'<dd>{escape(u["current"])}</dd>']

        if not u["enabled"]:
            body.append('<dt>Update check</dt><dd>off</dd></dl>'
                        '<p class="quiet">Turn it on with <code>timelapse web'
                        '</code> to be told when a new version is tagged.</p>')
            return self._wrap_update("".join(body), busy=False)

        if u["available"]:
            body.append(f'<dt>Latest</dt><dd class="ok">'
                        f'<strong>{escape(latest_label(u))}</strong>'
                        f'</dd></dl>')
        elif u["known"] and u["latest"]:
            body.append(f'<dt>Latest</dt><dd>{escape(latest_label(u))}'
                        f'</dd></dl>')
        else:
            body.append("</dl>")

        if u["busy"]:
            body.append('<p class="scan">Checking GitHub&hellip;</p>')
        elif u["available"]:
            # The release notes themselves are not rendered here, deliberately.
            # They are markdown, this is not a markdown renderer, and showing
            # the source with its `##` and backticks intact read as this
            # program having failed to format something. GitHub renders them
            # properly, one click away, so the honest version of this panel is
            # the link.
            body.append(
                f'<p class="note new"><strong>An update is available.</strong> '
                f'You have {escape(u["current"])}; '
                f'{external(u["url"] or RELEASES_URL, latest_label(u))}'
                f' is out.</p>'
                f'<p class="cmd">Upgrading re-runs the installer, which keeps '
                f'your config, your frames and your videos.'
                f'</p><pre>{escape(UPDATE_COMMANDS)}</pre>'
                f'<p class="quiet">'
                f'{external(u["url"] or RELEASES_URL, "Read what changed on GitHub")}'
                f'</p>')
        elif u["known"] and not u["error"]:
            body.append('<p class="scan">Up to date.</p>')

        retry = ('<form class="inline" method="post" action="/check-update">'
                 '<button type="submit">Check now</button></form>')

        if u["error"] and not u["busy"]:
            # Upstream being down is not a fault here, and any earlier answer
            # above still stands. Say so, offer the retry, and say when it
            # would otherwise happen: a operator who has just fixed their
            # resolver should not have to guess whether waiting will help.
            body.append(f'<p class="note">{escape(u["error"])}</p>'
                        f'<p class="quiet">Nothing here is broken by this; the '
                        f'page and the library work regardless. '
                        f'{self._next_try(u)} {retry}</p>')

        if u["checked"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(u["checked"]))
            body.append(
                f'<p class="quiet">Last successful check {when}, and at most '
                f'once a day. This is the only request this service makes to '
                f'the internet; turn it off with <code>timelapse web</code>. '
                f'{"" if u["error"] or u["busy"] else retry}</p>')
        elif not u["error"] and not u["busy"]:
            body.append(f'<p class="quiet">Not checked yet. {retry}</p>')
        return self._wrap_update("".join(body), busy=u["busy"],
                                 with_script=with_script)

    @staticmethod
    def _next_try(u):
        """When the automatic retry is due, in words rather than a timestamp."""
        left = u["retry_at"] - time.time()
        if left <= 0:
            return "It will try again on the next page load."
        mins = int(left // 60) + 1
        if mins < 90:
            return f"It will try again by itself in about {mins} minutes."
        return f"It will try again by itself in about {int(mins // 60)} hours."

    @staticmethod
    def _wrap_update(body, busy, with_script=True):
        out = (f'<section id="update" data-busy="{"1" if busy else "0"}">'
               f'{body}</section>')
        if with_script and busy:
            out += UPDATE_POLL_JS
        return out

    # -- files and playlists -------------------------------------------------

    def _locate(self, rel):
        """(row, path) for a request-supplied relative path, or (None, None).

        Every access re-stats the file - this is the "reconcile on access" half
        that a folder view cannot do, because a file overwritten in place does
        not change its directory. abs_path() is what keeps a crafted path
        inside the library.
        """
        index = self.server.index
        if not index.usable:
            return None, None
        row = index.reconcile_file(rel)
        if row is None:
            return None, None
        return row, index.abs_path(rel)

    def _serve_file(self, rel, download):
        row, path = self._locate(rel)
        if row is None or path is None:
            self._send(404, "no such video\n", "text/plain; charset=utf-8")
            return
        try:
            fh = open(path, "rb")
        except OSError as exc:
            log.warning("Cannot open %s: %s", path, exc)
            self._send(404, "no such video\n", "text/plain; charset=utf-8")
            return

        size = row["size"]
        # Both derived from the fresh stat, so they change whenever the file
        # does. That is what makes If-Range below meaningful.
        etag = f'"{size:x}-{row["mtime"]:x}"'
        last_mod = formatdate(row["mtime"], usegmt=True)

        rng = parse_range(self.headers.get("Range"), size)
        if_range = (self.headers.get("If-Range") or "").strip()
        if rng is not None and if_range and if_range not in (etag, last_mod):
            # The client is resuming against a version we no longer have. Its
            # offsets mean nothing now, so send the whole current file rather
            # than a slice that would splice two encodes together.
            rng = None

        with fh:
            if rng is UNSATISFIABLE:
                # 416 must still say how big the file actually is, or the
                # client has no way to correct itself.
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return

            if rng is None:
                start, length, status = 0, size, 200
            else:
                start, end = rng
                length, status = end - start + 1, 206

            self.send_response(status)
            self.send_header("Content-Type", media_type(row["name"]))
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_mod)
            if status == 206:
                self.send_header("Content-Range",
                                 f"bytes {start}-{start + length - 1}/{size}")
            if download:
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{ascii_filename(row["name"], "")}'
                    f'{os.path.splitext(row["name"])[1]}"')
            self.end_headers()
            if self.command == "HEAD":
                return
            if start:
                fh.seek(start)
            self._pump(fh, length)

    def _pump(self, fh, size):
        """Send exactly `size` bytes from wherever the handle is, then stop.

        Bounded by the length already promised in the header rather than by
        EOF: if the file grew since the stat, sending the extra would corrupt
        the response. If it shrank, the connection is closed instead, because a
        short body under HTTP/1.1 keep-alive hangs the client rather than
        failing it.
        """
        left = size
        try:
            while left > 0:
                chunk = fh.read(min(SEND_CHUNK, left))
                if not chunk:
                    log.warning("File shrank mid-send; closing the connection.")
                    self.close_connection = True
                    return
                self.wfile.write(chunk)
                left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            # Entirely normal: the viewer closed VLC, or stopped the download.
            self.close_connection = True

    def _base_url(self):
        """The origin to put inside a playlist.

        Built from the request rather than from config, because that is the
        only address known to reach this server: an .m3u containing
        127.0.0.1 is useless the moment it is opened on a phone.
        """
        host = (self.headers.get("Host") or "").strip()
        if not HOST_RE.match(host):
            web = self.server.cfg.get("web", {})
            # hostport(), not an f-string: an IPv6 bind would otherwise put
            # http://::1:8787/video/3 into a playlist, and a .m3u that does
            # not play arrives as a bug report about video, not about binding.
            host = hostport(web.get("bind", DEFAULT_BIND),
                            web.get("port", DEFAULT_PORT))
        # Set by a reverse proxy terminating TLS; anything else is ignored.
        proto = (self.headers.get("X-Forwarded-Proto") or "http").strip().lower()
        if proto not in ("http", "https"):
            proto = "http"
        return f"{proto}://{host}"

    @staticmethod
    def _entry_title(row):
        return m3u_title(" ".join(x for x in (row["camera"], row["day"]) if x)
                         or row["name"])

    def _send_playlist(self, lines, filename):
        raw = ("\n".join(lines) + "\n").encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", M3U_TYPE)
        self.send_header("Content-Length", str(len(raw)))
        # The filename is what makes the desktop hand this to a player, so the
        # .m3u extension here matters more than anything in the URL.
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _serve_m3u(self, rel):
        row, path = self._locate(rel)
        if row is None or path is None:
            self._send(404, "no such video\n", "text/plain; charset=utf-8")
            return
        self._send_playlist(
            ["#EXTM3U",
             f"#EXTINF:-1,{self._entry_title(row)}",
             f"{self._base_url()}/video/{quote(rel)}"],
            ascii_filename(row["name"], ".m3u"))

    def _serve_day_m3u(self, text):
        """Every video from one day, as a single playlist.

        The point of the feature: to review a day you open one file, and the
        player queues every place in turn instead of you opening seven.
        """
        day = valid_day(text)
        index = self.server.index
        if day is None or not index.usable:
            self._send(404, "no such day\n", "text/plain; charset=utf-8")
            return

        # Re-stat each entry rather than trusting the index. A playlist is
        # handed to a player that will not come back and ask again, so a dead
        # URL in it is worse than a shorter list. A day is a handful of files.
        live = []
        for row in index.by_day(day):
            fresh = index.reconcile_file(row["path"])
            if fresh is not None:
                live.append(fresh)
        if not live:
            self._send(404, "no videos for that day\n",
                       "text/plain; charset=utf-8")
            return

        base = self._base_url()
        lines = ["#EXTM3U", f"#PLAYLIST:Timelapses {day}"]
        for row in live:
            lines.append(f"#EXTINF:-1,{self._entry_title(row)}")
            lines.append(f"{base}/video/{quote(row['path'])}")
        self._send_playlist(lines, f"timelapse-{day}.m3u")

    # -- library ------------------------------------------------------------

    def _library(self, args):
        index = self.server.index
        if index.error:
            return f'<section><p class="note">{escape(index.error)}</p></section>'
        lib = resolve_library(self.server.cfg)
        if not lib["usable"]:
            # The scan banner belongs here too: if a scan is waiting for the
            # library to appear, saying so is the difference between "this is
            # broken" and "this will fix itself when the mount comes back".
            return (self._scan_banner() +
                    '<section><h2>Video library</h2>'
                    f'<p class="note">{escape(lib["note"] or "No readable library.")}'
                    '</p></section>')

        if "day" in args:
            return self._scan_banner() + self._day_view(args["day"][0])
        if "folder" in args:
            return self._scan_banner() + self._folder_view(args["folder"][0])
        if "camera" in args:
            return self._scan_banner() + self._camera_view(args["camera"][0])
        if "flagged" in args:
            return self._scan_banner() + self._flagged_view()
        return self._scan_banner() + self._library_home()

    def _scan_banner(self, with_script=True):
        """The indexing status line, wrapped so it can be replaced in place.

        with_script adds the poller. Off for the /scan fragment: a script
        assigned through outerHTML does not execute anyway, and relying on
        that would be a subtle thing to leave for the next reader.
        """
        s = dict(self.server.index.scan)
        rescan = ('<form class="inline" method="post" action="/rescan">'
                  '<button type="submit">Rescan</button></form>')
        if s["running"] and s["error"]:
            # Waiting on the library rather than reading it. Say which.
            body = (f'<p class="note">{escape(s["error"])}</p>'
                    f'<p class="scan">{rescan}</p>')
        elif s["running"]:
            # No "reload for more": the poller below does that, and telling
            # someone to reload a line that updates itself is worse than
            # saying nothing.
            body = (f'<p class="scan">Indexing&hellip; {s["files"]:,} files '
                    f'so far. This page works while it runs.</p>')
        elif s["error"]:
            body = (f'<p class="note">{escape(s["error"])}</p>'
                    f'<p class="scan">{rescan}</p>')
        elif s["finished"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["finished"]))
            # Only claim a duration when the start was actually recorded;
            # otherwise the subtraction reports the age of the epoch.
            took = (f' in {s["finished"] - s["started"]:.1f}s'
                    if s["started"] else "")
            body = (f'<p class="scan">Indexed {s["files"]:,} files at {when}'
                    f'{took}. {rescan}</p>')
        else:
            body = f'<p class="scan">Not indexed yet. {rescan}</p>'

        running = "1" if s["running"] else "0"
        out = f'<div id="scan" data-running="{running}">{body}</div>'
        if with_script and s["running"]:
            out += SCAN_POLL_JS
        return out

    def _library_home(self):
        index = self.server.index
        tot = index.totals()
        parts = [
            '<section><h2>Library</h2><dl>',
            f'<dt>Files</dt><dd>{tot["n"]:,}</dd>',
            f'<dt>Size</dt><dd>{human_size(tot["b"])}</dd>',
            f'<dt>Span</dt><dd>{escape(tot["a"] or "-")} to '
            f'{escape(tot["z"] or "-")}</dd>',
        ]
        if tot["s"]:
            parts.append(f'<dt>Flagged</dt><dd><a href="/library?flagged=1" '
                         f'class="flag">{tot["s"]} suspiciously small</a></dd>')
        parts.append("</dl></section>")

        cams = index.cameras()
        if cams:
            parts.append('<section><h2>Cameras</h2><div class="wrap"><table>'
                         '<tr><th>Name</th><th>Files</th><th>Size</th>'
                         '<th>First</th><th>Last</th></tr>')
            for c in cams:
                label = c["camera"] or "(no name in filename)"
                link = f'/library?camera={quote(c["camera"])}'
                parts.append(
                    f'<tr><td><a href="{link}">{escape(label)}</a></td>'
                    f'<td class="num">{c["n"]:,}</td>'
                    f'<td class="num">{human_size(c["b"])}</td>'
                    f'<td class="num">{escape(c["a"] or "-")}</td>'
                    f'<td class="num">{escape(c["z"] or "-")}</td></tr>')
            parts.append("</table></div>"
                         '<p class="scan">Names are shown exactly as they are '
                         'on disk and never merged; a name is a place, '
                         'and places get recycled between cameras. Sorted '
                         'case-insensitively so variants sit together.</p>'
                         "</section>")

        days = index.recent_days()
        if days:
            parts.append('<section><h2>Recent days</h2><div class="wrap"><table>'
                         '<tr><th>Day</th><th>Videos</th><th>Size</th>'
                         '<th>Open</th></tr>')
            for d in days:
                parts.append(
                    f'<tr><td class="num">'
                    f'<a href="/library?day={escape(d["day"])}">'
                    f'{escape(d["day"])}</a></td>'
                    f'<td class="num">{d["n"]:,}</td>'
                    f'<td class="num">{human_size(d["b"])}</td>'
                    f'<td class="acts"><a href="/day/{escape(d["day"])}">'
                    f'Play the day</a></td></tr>')
            parts.append('</table></div><p class="scan">One playlist per day, '
                         'so reviewing a day means opening a single file rather '
                         'than one per place.</p></section>')

        folders = index.folders()
        if folders:
            parts.append('<section><h2>Folders</h2><div class="wrap"><table>'
                         '<tr><th>Folder</th><th>Files</th><th>Size</th></tr>')
            for f in folders:
                label = f["folder"] or "(root)"
                link = f'/library?folder={quote(f["folder"])}'
                parts.append(
                    f'<tr><td><a href="{link}">{escape(label)}</a></td>'
                    f'<td class="num">{f["n"]:,}</td>'
                    f'<td class="num">{human_size(f["b"])}</td></tr>')
            parts.append("</table></div></section>")
        return "".join(parts)

    def _day_view(self, text):
        day = valid_day(text)
        if day is None:
            return ('<section><p class="note">Not a date: '
                    f'{escape(text)}</p></section>')
        rows = self.server.index.by_day(day)
        if not rows:
            return (f'<section><h2>{escape(day)}</h2>'
                    '<p class="scan">Nothing indexed for that day.</p></section>')
        head = (f'<section><h2>{escape(day)}</h2>'
                f'<p class="sub"><a href="/day/{escape(day)}"><strong>'
                f'Play the whole day</strong></a>: one playlist, every '
                f'place in turn ({len(rows)} videos, '
                f'{human_size(sum(r["size"] for r in rows))}).</p>')
        # No Day column: the heading is the day, and every link in that column
        # pointed back at this same page.
        return head + self._file_table(rows, show_folder=True, full_path=True,
                                       show_day=False) + "</section>"

    def _camera_view(self, camera):
        rows = self.server.index.by_camera(camera)
        label = camera or "(no name in filename)"
        head = (f'<section><h2>{escape(label)}</h2>'
                f'<p class="scan">{len(rows):,} files, from the index. '
                f'Open a folder to re-check it against disk.</p>')
        return head + self._file_table(rows, show_folder=True,
                                       full_path=True) + "</section>"

    def _folder_view(self, folder):
        index = self.server.index
        changed = index.reconcile_dir(folder)
        rows = index.in_folder(folder)
        label = folder or "(root)"
        note = ("Re-checked against disk; the index was out of date."
                if changed else "Re-checked against disk; nothing had changed.")
        head = (f'<section><h2>{escape(label)}</h2>'
                f'<p class="scan">{len(rows):,} files. {note}</p>')
        return head + self._file_table(rows, show_folder=False,
                                       full_path=True) + "</section>"

    def _flagged_view(self):
        index = self.server.index
        rows = index.suspects()
        unknown = index.unrecognised()
        parts = [
            '<section><h2>Flagged files</h2>',
            '<p class="scan">Smaller than ',
            human_size(SUSPECT_BYTES),
            '; a day of timelapse is hundreds of megabytes, so these '
            'are almost certainly failed encodes. Full paths are given so you '
            'can check and remove them yourself; this UI never deletes '
            'anything.</p>',
            self._file_table(rows, show_folder=True, full_path=True),
            "</section>",
        ]
        if unknown:
            parts += [
                '<section><h2>No date in the filename</h2>',
                f'<p class="scan">{len(unknown):,} files whose names match none '
                'of the known conventions. They are indexed and listed, just '
                'not filed under a date.</p>',
                self._file_table(unknown, show_folder=True, full_path=True),
                "</section>",
            ]
        return "".join(parts)

    def _file_table(self, rows, show_folder, full_path=False, show_day=True):
        """One table of videos.

        show_folder and show_day drop a column whose value the heading above
        the table already states and every row repeats. In the day view each
        of those links pointed back at the page being read.
        """
        if not rows:
            return '<p class="scan">Nothing here.</p>'
        root = self.server.index.root
        base = self._base_url()
        cols = ["Day"] if show_day else []
        cols.append("Name")
        if show_folder:
            cols.append("Folder")
        cols += ["Size", "Open"]
        out = ['<div class="wrap"><table><tr>']
        out += [f"<th>{c}</th>" for c in cols]
        out.append("</tr>")
        for r in rows:
            enc = quote(r["path"])
            flag = ' class="flag"' if r["suspect"] else ""
            out.append("<tr>")
            if show_day:
                day = (f'<a href="/library?day={escape(r["day"])}">'
                       f'{escape(r["day"])}</a>') if r["day"] else "-"
                out.append(f'<td class="num">{day}</td>')
            out.append(f'<td{flag}>{escape(r["name"])}</td>')
            if show_folder:
                out.append(f'<td>{escape(r["folder"] or "(root)")}</td>')
            out.append(f'<td class="num">{human_size(r["size"])}</td>')
            out.append(f'<td class="acts"><a href="/play/{enc}">Play</a> '
                       f'<a href="/video/{enc}?download=1">Download</a></td></tr>')
            if full_path and root is not None:
                # Two ways to reach the same file without this UI: the share
                # path for a machine that has it mounted, and the HTTP URL for
                # VLC's "Open Network Stream" anywhere else. Selectable in one
                # click, because that is the whole point of showing them.
                whole = os.path.join(str(root), r["path"].replace("/", os.sep))
                # The empty leading cell exists only to skip the Day column,
                # so that the path lines up under the name it belongs to. With
                # no Day column there is nothing to skip.
                skip = '<td></td>' if show_day else ""
                span = len(cols) - 1 if show_day else len(cols)
                out.append(f'<tr class="sub-row">{skip}'
                           f'<td colspan="{span}" class="path">'
                           f'{escape(whole)}<br>{escape(base)}/video/{enc}'
                           f'</td></tr>')
        out.append("</table></div>")
        return "".join(out)

    def _cameras(self):
        """Whether the cameras are actually answering, which is the question
        the Services table below cannot be asked.

        A daemon with every camera refusing connections is `active (running)`,
        and so is one the disk guard has paused. Both look perfect down there
        and neither is capturing anything.
        """
        state, problem = read_state(self.server.cfg, CAPTURE_STATE)
        parts = ["<section><h2>Cameras</h2>"]
        if problem:
            parts.append(f'<p class="note">{escape(problem)}</p>'
                         f'<p class="quiet">{STATE_MISSING}</p></section>')
            return "".join(parts)

        snap = state.get("updated_epoch") or 0
        age = time.time() - snap if snap else None

        # Order matters: a stopped daemon explains every quiet camera below it,
        # so say that first rather than leaving the reader to infer it from
        # eight red rows.
        if not state.get("running", True):
            parts.append('<p class="note">Capture stopped cleanly at '
                         f'{escape(state.get("updated") or "?")}. '
                         'Nothing is being captured.</p>')
        elif age is not None and age > STATE_STALE_AFTER:
            parts.append(
                f'<p class="note bad">Last heartbeat {escape(human_age(age))} '
                f'ago. This is written once a minute while capture runs, so '
                f'the daemon is stopped or wedged, and the rows below are '
                f'from {escape(state.get("updated") or "?")}.</p>')
        if state.get("paused"):
            parts.append('<p class="note bad">Capture is PAUSED by the disk '
                         'guard: free space fell below '
                         '<code>capture.min_free_gb</code>. No frames are '
                         'being written by any camera.</p>')

        cams = state.get("cameras") or []
        if not cams:
            parts.append('<p class="note">No cameras are enabled.</p></section>')
            return "".join(parts)

        parts.append("<table><tr><th>Camera</th><th>Last frame</th>"
                     "<th>Cadence</th><th>Frames today</th>"
                     "<th>Coverage</th><th>Problems</th></tr>")
        unreadable = False
        for cam in cams:
            cls, phrase = camera_verdict(cam, snap)
            interval = cam.get("interval")
            # "5s / frame", not "1 / 5s". The old form was read as the
            # fraction one fifth of a second as readily as one frame every
            # five seconds, which are two very different cameras; reported by
            # the operator 2026-08-14. A rate with its unit named cannot be
            # read backwards.
            cadence = f"{interval}s / frame" if interval else "-"
            # Empty when there is nothing wrong. A column that says "0 failed"
            # on every healthy row trains the eye to skip it, which is the
            # opposite of what a problems column is for.
            if cam.get("supervised"):
                # This camera's frames are written by a separate process, so
                # the daemon has no fetch count. Restarts are what it knows.
                restarts = cam.get("restarts", 0)
                fails = f'{restarts} restart(s)' if restarts else ""
            else:
                consec = cam.get("consec_fail", 0)
                failed = cam.get("fail", 0)
                fails = f'{failed:,} failed fetch(es)' if failed else ""
                if consec:
                    fails += f' ({consec} in a row)'

            # Counted on disk, so this is the same number for an RTSP camera
            # as for an HTTP one, and survives a restart of the daemon.
            frames, cov = today_progress(self.server.cfg, cam)
            if frames is None:
                unreadable = True
                shown, cov_txt, cov_cls = "?", "?", "dim"
            else:
                shown = f"{frames:,}"
                if cov is None:
                    cov_txt, cov_cls = "-", "dim"
                else:
                    cov_txt = f"{cov:.0f}%"
                    # 98 rather than 100: a frame is written at the start of
                    # each tick, so the current tick is always outstanding
                    # and a perfect camera reads a shade under 100.
                    cov_cls = "dim" if cov >= 98 else "warn"
            parts.append(
                f'<tr><td>{escape(str(cam.get("name", "?")))}</td>'
                f'<td class="{cls}">{escape(phrase)}</td>'
                f'<td class="dim">{escape(cadence)}</td>'
                f'<td class="num dim">{escape(shown)}</td>'
                f'<td class="num {cov_cls}">{escape(cov_txt)}</td>'
                f'<td class="dim">{escape(fails)}</td></tr>')
        parts.append("</table>")
        parts.append(
            '<p class="quiet">Frames and coverage are counted from the files '
            'on disk since midnight, against the cadence each camera is '
            'running at today; under 100% means frames are missing, including '
            'any part of today before capture started. The rest of each row '
            'comes from the capture service, updated once a minute'
            + (f', last at {escape(state.get("updated"))}.'
               if state.get("updated") else ".")
            + '</p>')
        if unreadable:
            parts.append('<p class="note">A "?" means this service could not '
                         'read that camera\'s frames directory. It is not a '
                         'statement about the camera.</p>')
        parts.append("</section>")
        return "".join(parts)

    def _last_encode(self):
        """What last night actually produced, rather than what systemd made of
        it. The unit row can say the timer ran; only this can say the run
        encoded seven days and shipped them."""
        state, problem = read_state(self.server.cfg, ENCODE_STATE)
        parts = ["<section><h2>Last encode</h2>"]
        if problem:
            parts.append(f'<p class="note">{escape(problem)}</p>'
                         f'<p class="quiet">{STATE_MISSING}</p></section>')
            return "".join(parts)

        runs = state.get("runs") or []
        if not runs:
            parts.append('<p class="note">No run has been recorded yet.</p>'
                         '</section>')
            return "".join(parts)

        run = runs[0]
        when = run.get("finished") or run.get("started") or "?"
        rows = [("Finished", when),
                ("Took", human_age(run.get("seconds") or 0))]
        if run.get("encoder"):
            rows.append(("Encoder", run["encoder"]))

        if run.get("error"):
            # An aborted run has counts of zero, which would otherwise read as
            # a quiet night rather than as a crash.
            rows.append(("Aborted", run["error"]))
        else:
            made = (f'{run.get("ok", 0)} video(s), '
                    f'{human_size(run.get("bytes") or 0)}')
            if run.get("failed"):
                made += f', {run["failed"]} failed'
            if run.get("skipped"):
                made += f', {run["skipped"]} skipped'
            rows.append(("Produced", made))

        xfer = run.get("transfer")
        if xfer is None:
            rows.append(("Transfer", "not attempted"))
        elif xfer.get("ok"):
            rows.append(("Transfer", f'{xfer.get("moved", 0)} file(s) moved'))
        else:
            rows.append(("Transfer", f'failed: {xfer.get("detail") or "?"}'))

        parts.append("<dl>")
        for label, value in rows:
            cls = ' class="bad"' if label in ("Aborted",) or (
                label == "Transfer" and xfer is not None
                and not xfer.get("ok")) else ""
            parts.append(f"<dt>{escape(label)}</dt>"
                         f"<dd{cls}>{escape(str(value))}</dd>")
        parts.append("</dl>")

        days = run.get("days") or []
        if days:
            parts.append("<table><tr><th>Camera</th><th>Day</th>"
                         "<th>Result</th><th>Frames</th><th>Coverage</th>"
                         "<th>Size</th></tr>")
            for d in days:
                # All three built outside the f-string below. Nesting a quote
                # or a backslash inside an f-string expression is PEP 701
                # grammar, which is Python 3.12, and this project's floor is
                # 3.9 (RHEL 9, Debian 11). It is a SyntaxError at *import*
                # there, so it would not break this panel, it would stop the
                # web service from starting at all. See the same note in
                # timelapse_setup.py's summarise_web().
                cov = d.get("coverage")
                mark = ' class="bad"' if d.get("status") == "FAIL" else ""
                shown = "-" if cov is None else f"{cov:g}%"
                parts.append(
                    f'<tr><td>{escape(str(d.get("camera", "?")))}</td>'
                    f'<td class="dim">{escape(str(d.get("date", "?")))}</td>'
                    f'<td{mark}>{escape(str(d.get("status", "?")))}</td>'
                    f'<td class="dim">{d.get("frames", 0):,}</td>'
                    f'<td class="dim">{escape(shown)}</td>'
                    f'<td class="dim">{escape(human_size(d.get("size") or 0))}'
                    f'</td></tr>')
            parts.append("</table>")
        else:
            parts.append('<p class="quiet">That run found nothing to do, '
                         'which is what an ordinary night looks like when '
                         'yesterday has already been encoded.</p>')
        parts.append("</section>")
        return "".join(parts)

    def _services(self):
        """Four rows saying whether it works, for the foot of the overview.

        It was a tab of its own, and before that a tab holding `systemctl
        status` verbatim. Once it became four rows there was not enough of it
        to justify a quarter of the navigation, and "is it running" belongs
        next to "where are my videos" anyway.

        One `systemctl show` per page load, and no more: the full output is a
        click away at /status rather than inline in a <details>, because
        rendering it here would cost a second subprocess and a screen of
        markup on every view of the landing page to serve something that is
        almost never opened.
        """
        rows, problem = unit_states()
        parts = ["<section><h2>Services</h2>"]
        if problem:
            parts.append(f'<p class="note">{escape(problem)}</p></section>')
            return "".join(parts)

        parts.append("<table><tr><th>Service</th><th>State</th>"
                     "<th>Detail</th></tr>")
        for label, cls, state, detail in rows:
            mark = f' class="{cls}"' if cls else ""
            parts.append(f'<tr><td>{escape(label)}</td>'
                         f'<td{mark}>{escape(state)}</td>'
                         f'<td class="dim">{escape(detail)}</td></tr>')
        parts.append('</table><p class="quiet">'
                     '<a href="/status">Technical data</a></p>'
                     '</section>')
        return "".join(parts)

    def _status_detail(self):
        """`systemctl status` in full, for when something is actually wrong.

        Not a tab any more, and not inline on the overview either, but still a
        page: this is what a bug report wants pasted into it, and re-running
        systemctl over ssh to get it is worse than a link.
        """
        rep = status_report()
        return ('<p class="sub"><a href="/">&larr; Overview</a></p>'
                + self._report(rep))

    def _logs(self, args):
        unit = (args.get("unit") or [DEFAULT_LOG_UNIT])[0]
        lines = (args.get("n") or [DEFAULT_LOG_LINES])[0]
        # Unknown values fall back rather than 400: these come from links, and
        # a stale bookmark should show the default log, not an error.
        if unit not in LOG_UNITS:
            unit = DEFAULT_LOG_UNIT
        if lines not in LOG_LINES:
            lines = DEFAULT_LOG_LINES

        picker = ['<p class="sub">']
        for key in LOG_UNITS:
            mark = "<strong>%s</strong>" % key if key == unit else key
            picker.append(f'<a href="/logs?unit={key}&amp;n={lines}">{mark}</a>')
        picker.append("&nbsp;|&nbsp;")
        for key in LOG_LINES:
            mark = "<strong>%s</strong>" % key if key == lines else key
            picker.append(f'<a href="/logs?unit={unit}&amp;n={key}">{mark}</a>')
        picker.append("</p>")

        return "".join(picker) + self._report(journal_report(unit, lines),
                                              pane=True)

    @staticmethod
    def _report(rep, pane=False):
        """One command's output in a <pre>, with whatever went wrong instead.

        pane marks this as the page's main output pane, which the stylesheet
        then bounds to the viewport so the <pre> scrolls inside its own frame.
        Only when there is output to scroll: stretching an error message to
        the full window would be an empty box with one line in it.
        """
        cls = ' class="pane"' if pane and not rep["problem"] else ""
        parts = [f'<section{cls}>'
                 f'<p class="cmd"><code>{escape(rep["command"])}</code></p>']
        if rep["problem"]:
            parts.append(f'<p class="note">{escape(rep["problem"])}</p>')
        else:
            parts.append(f'<pre>{escape(rep["output"]) or "(no output)"}</pre>')
        if rep["hint"]:
            parts.append(f'<p class="note">{escape(rep["hint"])}</p>')
        parts.append("</section>")
        return "".join(parts)

    def log_message(self, fmt, *args):
        # Default writes to stderr, which journald tags as an error. These are
        # ordinary access lines.
        log.info("%s %s", self.address_string(), fmt % args)


def escape(text):
    """Minimal HTML escaping. Everything rendered so far comes from the config
    or the filesystem, not from the request - but a camera name or a path is
    still user-supplied text reaching a browser."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ----------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, cfg, index, updates=None, auth=None):
        # Set before super().__init__, which creates the socket from it.
        # ThreadingHTTPServer's default is AF_INET, so without this an IPv6
        # bind fails at startup while check_bind() in the wizard, which walks
        # every family getaddrinfo() returns, had already reported it usable.
        self.address_family = (socket.AF_INET6 if is_ipv6(addr[0])
                               else socket.AF_INET)
        super().__init__(addr, handler)
        self.cfg = cfg
        self.index = index
        # Default to a disabled checker rather than None: every page that
        # renders the panel would otherwise need to know it might be absent.
        self.updates = updates or UpdateChecker(None, enabled=False)
        # Same reasoning: a disabled Auth answers every question the handler
        # asks, so no route has to know whether a login exists.
        self.auth = auth or Auth()

    def server_bind(self):
        """Decide dual-stack explicitly rather than inheriting a sysctl.

        A socket bound to `::` accepts IPv4 as v4-mapped addresses when
        IPV6_V6ONLY is 0, which is Linux's default (`net.ipv6.bindv6only`) and
        not Windows'. Inheriting that would make "listen on ::" mean different
        things on two hosts with identical configs, and a hardened host may
        well have changed it. 0 is the choice here: someone who asks for the
        IPv6 wildcard wants every interface, which is what 0.0.0.0 means to
        them on the other stack.

        It is set for any AF_INET6 socket, not only the wildcard, because on a
        specific address the option has no effect and a conditional would only
        be one more thing to get wrong.
        """
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6,
                                       socket.IPV6_V6ONLY, 0)
            except OSError as exc:
                # Some kernels refuse to change it. Serving IPv6 only is a far
                # better outcome than refusing to start.
                log.warning("Could not enable dual-stack on this socket "
                            "(%s); IPv4 clients will not be accepted.", exc)
        super().server_bind()

    def handle_error(self, request, client_address):
        """A client that walks away is not an error.

        Video playback makes this routine. A player opens a connection, takes
        the byte range it wanted, and abandons it; the handler is then blocked
        in readline() waiting for a request that never comes, and the socket
        resets under it. socketserver's default prints a full traceback to
        stderr for that, which journald tags as an error, so an ordinary seek
        in VLC reads as a crash in the log.

        _pump() already swallows a disconnect *during* the body, which is why
        that case never showed up. This catches the same thing happening
        between requests, where no code of ours is on the stack to catch it.

        Real faults still get reported, but through the logger instead of a
        bare stderr traceback, so they carry a timestamp and a level.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, DISCONNECTED):
            log.debug("%s disconnected: %s", client_address[0], exc)
            return
        log.exception("Unhandled error serving %s", client_address[0])


def main():
    ap = argparse.ArgumentParser(
        prog="timelapse_web.py",
        description="Read-only web UI for timelapse-maker.")
    ap.add_argument("config", nargs="?", default="/etc/timelapse/config.json")
    ap.add_argument("--bind", help=f"address to listen on (default {DEFAULT_BIND})")
    ap.add_argument("--port", type=int, help=f"port (default {DEFAULT_PORT})")
    ap.add_argument("--force", action="store_true",
                    help="run even when web.enabled is false")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging()

    web = cfg.get("web", {})
    # .get() throughout: an upgrade keeps the existing config.json, so a key
    # read with [] would break every install that predates this feature.
    if not web.get("enabled", False) and not args.force:
        log.info("web.enabled is false in %s; nothing to serve.", args.config)
        return 0

    bind = args.bind or web.get("bind", DEFAULT_BIND)
    port = args.port or int(web.get("port", DEFAULT_PORT))

    # Before the bind warning, because what that warning should say depends on
    # the answer, and because refusing to start is the point: an unusable hash
    # means the login cannot be checked, and carrying on regardless would open
    # the pages to everyone exactly because somebody asked for the opposite.
    try:
        auth = Auth.from_config(cfg)
    except ValueError as exc:
        sys.exit(f"Cannot start: {exc}")

    if bind not in ("127.0.0.1", "::1", "localhost"):
        if auth.enabled:
            log.warning("Listening on %s - the login keeps the pages to "
                        "whoever knows the password, but there is no TLS, so "
                        "it crosses the network in clear, and the video files "
                        "answer anyone who knows their exact address.", bind)
        else:
            log.warning("Listening on %s - this server has no authentication "
                        "and no TLS. Put a reverse proxy in front of it for "
                        "anything beyond a trusted LAN.", bind)
    if auth.enabled:
        log.info("Login required for the pages (user %s).", auth.username)

    lib = resolve_library(cfg)
    log.info("Library: %s (from %s)%s",
             lib["path"] or "-", lib["source"], "" if lib["usable"] else " [UNUSABLE]")
    if lib["note"]:
        log.warning("%s", lib["note"])

    state_dir = web.get("state_dir", DEFAULT_STATE_DIR)
    index = Index(Path(state_dir) / "index.db", lib["path"])
    # .get with a default: an upgrade keeps the existing config.json, so a key
    # read with [] would break every install that predates this feature.
    updates = UpdateChecker(state_dir, web.get("update_check", True))
    if not updates.enabled:
        log.info("Update check is off; this service makes no outbound "
                 "connections.")

    try:
        httpd = Server((bind, port), Handler, cfg, index, updates, auth)
    except OSError as exc:
        # Almost always "address already in use" or a bind address that does
        # not exist on this host. Both are config errors, not crashes.
        sys.exit(f"Cannot listen on {hostport(bind, port)}: {exc}")

    def on_signal(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        # shutdown() blocks until serve_forever returns, so it cannot be called
        # from the handler thread itself.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log.info("Serving on http://%s/ (pid %d)", hostport(bind, port),
             os.getpid())

    # After the socket is listening, never before: the first scan of a large
    # share on a slow link must not delay the page that reports its progress.
    if index.usable:
        index.start_scan()

    httpd.serve_forever()
    httpd.server_close()
    log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

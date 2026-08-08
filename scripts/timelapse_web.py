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
import datetime
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

__version__ = "0.0.9"

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
# docs/future-features.md records the survey). The native format is 64% of it -
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
                    if len(batch) >= 500:
                        self._write(db, batch)
                        batch = []
                        with self._lock:
                            self.scan["files"] = count
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
        # future-features.md; a name is a place, and places get recycled.
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

STATUS_UNITS = ("timelapse-capture.service", "timelapse-encode.timer",
                "timelapse-encode.service", "timelapse-web.service")

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
    return out, ""


def status_report():
    """`systemctl status` for every unit this project installs.

    --lines=0 suppresses the journal excerpt systemctl normally appends. That
    excerpt needs journal access, so without it the output looks mysteriously
    truncated; the logs page asks for logs explicitly instead.
    """
    argv = ["systemctl", "status", "--no-pager", "--lines=0"] + list(STATUS_UNITS)
    out, problem = run_command(argv)
    return {"command": " ".join(argv), "output": out, "problem": problem,
            "hint": ""}


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
# Page
# ----------------------------------------------------------------------------

LAYOUT = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>timelapse-maker</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 2rem 1.25rem;
         max-width: 54rem; margin-inline: auto; }}
  h1 {{ font-size: 1.3rem; margin: 0; }}
  h2 {{ font-size: .95rem; text-transform: uppercase; letter-spacing: .06em;
        opacity: .6; margin: 0 0 .6rem; }}
  header {{ display: flex; align-items: baseline; gap: .75rem;
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
  nav {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1.25rem; }}
  nav a {{ text-decoration: none; border: 1px solid rgba(128,128,128,.35);
           border-radius: 999px; padding: .25rem .8rem; font-size: .9rem;
           color: inherit; }}
  nav a.on {{ background: rgba(128,128,128,.18); font-weight: 600; }}
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
  .wrap {{ overflow-x: auto; }}
  .path {{ font-family: ui-monospace, monospace; font-size: .8rem;
           user-select: all; overflow-wrap: anywhere; opacity: .8; }}
  .flag {{ color: #b3261e; font-weight: 600; }}
  td.acts {{ white-space: nowrap; }}
  td.acts a {{ color: inherit; margin-right: .5rem; }}
  tr.sub-row td {{ border-bottom: 1px solid rgba(128,128,128,.14);
                   padding-top: 0; }}
  .scan {{ font-size: .85rem; opacity: .7; margin: 0 0 1rem; }}
  form.inline {{ display: inline; }}
  button {{ font: inherit; font-size: .85rem; padding: .25rem .8rem;
            border-radius: 999px; border: 1px solid rgba(128,128,128,.35);
            background: transparent; color: inherit; cursor: pointer; }}
  @media (prefers-color-scheme: dark) {{ .flag {{ color: #ff7b72; }} }}
</style>
<body class="{body_class}">
<header>
  <h1>timelapse-maker</h1>
  <span class="ver">web {version}</span>
</header>
<nav>
  <a href="/" class="{on_home}">Overview</a>
  <a href="/library" class="{on_library}">Library</a>
  <a href="/status" class="{on_status}">Service status</a>
  <a href="/logs" class="{on_logs}">Recent log</a>
</nav>
{content}
"""

OVERVIEW = """<section>
  <h2>Video library</h2>
  <dl>
    <dt>Location</dt><dd><code>{lib_path}</code></dd>
    <dt>Resolved from</dt><dd>{lib_source}</dd>
    <dt>Readable</dt><dd class="{lib_class}">{lib_state}</dd>
  </dl>
  {lib_note}
</section>

<section>
  <h2>Not built yet</h2>
  <ul class="todo">
    <li>Playback handoff: <code>.m3u</code> to VLC, plus a download link</li>
  </ul>
</section>
"""


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
            self._send(200, self._render(
                "status", self._report(status_report(), pane=True)))
        elif route == "/logs":
            self._send(200, self._render("logs", self._logs(args)))
        elif route == "/healthz":
            self._send(200, "ok\n", "text/plain; charset=utf-8")
        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self):
        """The one action a read-only UI has: rescan its own index.

        POST rather than a link so a crawler, a prefetch or a refresh cannot
        set a full scan going. It still changes nothing outside our own
        database.
        """
        route = self.path.split("?", 1)[0].rstrip("/") or "/"
        if route == "/rescan":
            self.server.index.start_scan("requested")
            self.send_response(303)
            self.send_header("Location", "/library")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    # Pages whose body is one pane of raw command output, which wants the whole
    # window rather than the reading column the rest of the UI uses.
    PANE_PAGES = ("status", "logs")

    def _render(self, page, content):
        return LAYOUT.format(
            version=__version__,
            body_class="pane-page" if page in self.PANE_PAGES else "",
            on_home="on" if page == "home" else "",
            on_library="on" if page == "library" else "",
            on_status="on" if page == "status" else "",
            on_logs="on" if page == "logs" else "",
            content=content,
        )

    def _overview(self):
        # Re-resolved per request rather than cached at startup: a NAS mount
        # comes and goes, and a page that reports a stale answer is worse than
        # no page. It is two stat() calls.
        lib = resolve_library(self.server.cfg)
        note = f'<p class="note">{escape(lib["note"])}</p>' if lib["note"] else ""
        return OVERVIEW.format(
            lib_path=escape(str(lib["path"]) if lib["path"] else "-"),
            lib_source=escape(lib["source"] or "-"),
            lib_class="ok" if lib["usable"] else "bad",
            lib_state="yes" if lib["usable"] else "no",
            lib_note=note,
        )

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
            host = f"{web.get('bind', DEFAULT_BIND)}:{web.get('port', DEFAULT_PORT)}"
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

    def _scan_banner(self):
        s = dict(self.server.index.scan)
        rescan = ('<form class="inline" method="post" action="/rescan">'
                  '<button type="submit">Rescan</button></form>')
        if s["running"] and s["error"]:
            # Waiting on the library rather than reading it. Say which.
            return (f'<p class="note">{escape(s["error"])}</p>'
                    f'<p class="scan">{rescan}</p>')
        if s["running"]:
            return (f'<p class="scan">Indexing&hellip; {s["files"]} files so far. '
                    f'This page works while it runs; reload for more.</p>')
        if s["error"]:
            return (f'<p class="note">{escape(s["error"])}</p>'
                    f'<p class="scan">{rescan}</p>')
        if s["finished"]:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["finished"]))
            # Only claim a duration when the start was actually recorded;
            # otherwise the subtraction reports the age of the epoch.
            took = (f' in {s["finished"] - s["started"]:.1f}s'
                    if s["started"] else "")
            return (f'<p class="scan">Indexed {s["files"]} files at {when}'
                    f'{took}. {rescan}</p>')
        return f'<p class="scan">Not indexed yet. {rescan}</p>'

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

    def __init__(self, addr, handler, cfg, index):
        super().__init__(addr, handler)
        self.cfg = cfg
        self.index = index

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

    if bind not in ("127.0.0.1", "::1", "localhost"):
        log.warning("Listening on %s - this server has no authentication and "
                    "no TLS. Put a reverse proxy in front of it for anything "
                    "beyond a trusted LAN.", bind)

    lib = resolve_library(cfg)
    log.info("Library: %s (from %s)%s",
             lib["path"] or "-", lib["source"], "" if lib["usable"] else " [UNUSABLE]")
    if lib["note"]:
        log.warning("%s", lib["note"])

    state_dir = web.get("state_dir", DEFAULT_STATE_DIR)
    index = Index(Path(state_dir) / "index.db", lib["path"])

    try:
        httpd = Server((bind, port), Handler, cfg, index)
    except OSError as exc:
        # Almost always "address already in use" or a bind address that does
        # not exist on this host. Both are config errors, not crashes.
        sys.exit(f"Cannot listen on {bind}:{port}: {exc}")

    def on_signal(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        # shutdown() blocks until serve_forever returns, so it cannot be called
        # from the handler thread itself.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log.info("Serving on http://%s:%d/ (pid %d)", bind, port, os.getpid())

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

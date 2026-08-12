#!/usr/bin/env python3
"""
timelapse_capture.py: long-running snapshot grabber.

One thread per camera. Each thread wakes on absolute wall-clock boundaries
(00, 05, 10, ... seconds past the minute) so it cannot drift, fetches a
full-quality JPEG, and writes it atomically to:

    <frames_root>/<Camera>/<YYYY-MM-DD>/<HHMMSS>.jpg

Cameras with no HTTP snapshot endpoint (method "rtsp") are handled by a
supervised persistent ffmpeg process using the image2 muxer's strftime
naming, which produces the identical layout.

A disk guard pauses all capture if free space on the frames filesystem
drops below capture.min_free_gb, rather than filling the disk.

Run under systemd. Logs to stdout (journald) and to a rotating file.
"""

import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
    from requests.auth import HTTPBasicAuth, HTTPDigestAuth
except ImportError:
    sys.exit("Missing dependency: pip install requests "
             "(or: sudo apt install python3-requests)")

__version__ = "0.1.5"


# ----------------------------------------------------------------------------
# Shared state
# ----------------------------------------------------------------------------

STOP = threading.Event()
PAUSED = threading.Event()      # set by the disk guard when space runs low
log = logging.getLogger("capture")

# Intra-tick retry, for snapshot endpoints that refuse instantly (ONVIF answers
# 500 rather than queueing) and leave the tick's budget unspent.
#
# Scope, measured - do not widen this without re-measuring: it recovers ~58% of
# failures that are *per-request* blips, and 0% of failures that are a busy
# window longer than one interval. The latter is not a tuning problem. If the
# camera is out for longer than the interval, the next tick already is the
# retry, so nothing inside this tick can win.
RETRY_DELAY = 0.25          # let a camera that just said "busy" breathe
RETRY_GUARD = 0.5           # a retry must never run into the next tick
RETRY_MIN_BUDGET = 1.0      # below this a retry would only time out again


def load_config(path):
    """Deliberately duplicated rather than imported from timelapse_encode: the
    daemon must not depend on the batch job, so an encoder change can never
    stop capture. A clean message here also means journald shows the reason
    instead of a traceback when the unit refuses to start."""
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


# ----------------------------------------------------------------------------
# Credential redaction
#
# Duplicated from timelapse_encode.py for the same reason load_config() is: no
# daemon should be able to fail to start because a sibling changed. A test
# asserts the two copies are character-identical, because a security rule that
# exists twice will otherwise drift, and the copy that drifts is the one nobody
# is looking at.
#
# This daemon is where the leak was found. Both of its failure paths carry a
# URL it never chose to print: requests puts the URL in the exception text, and
# ffmpeg prints the RTSP URL it was handed in its own stderr, which the RTSP
# grabber logs verbatim.
# ----------------------------------------------------------------------------

MASK = "***"

CRED_PATTERNS = (
    # Query-string credentials: the Reolink shape, ?user=admin&password=hunter2.
    # \b so "bypass=" is not read as "pass=". The value ends at the next
    # parameter or at whitespace, since a URL in a log line is followed by
    # prose more often than not.
    (re.compile(r"\b((?:password|passwd|pwd|pass|secret|token|auth|apikey|"
                r"api[-_]?key)=)[^&\s\"'<>]*", re.I), r"\1" + MASK),
    # URL userinfo: the RTSP shape, rtsp://user:hunter2@host. ffmpeg prints the
    # URL it was given in its own error output, so this arrives second-hand.
    (re.compile(r"(//[^/\s:@]{1,64}:)[^/\s@]*(@)"), r"\1" + MASK + r"\2"),
    # A Discord webhook URL is not a locator, it is the authority to post. It
    # reaches the log through urllib's exception text on a failed notification.
    (re.compile(r"(/api/webhooks/\d+/)[\w-]+", re.I), r"\1" + MASK),
)


def redact(text):
    """Mask anything in `text` that would be a credential in a log or on a page.

    Deliberately over-eager: masking a value that turns out not to be secret
    costs somebody a debugging session, and the other way round costs them a
    password.
    """
    text = str(text)
    for pattern, replacement in CRED_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """A formatter, not a filter, and not a call at each log site.

    The leak this exists for came from `log.warning("grab failed: %s", err)`,
    a call that never mentions a URL: the credential was inside an exception
    raised three libraries away. Nothing at the call site can be trusted to
    know what it is about to print, so the guarantee has to sit at the last
    point every record passes through. Formatting last also covers tracebacks
    and `log.exception()`, which a filter on the record's message would not.
    """

    def format(self, record):
        return redact(super().format(record))


def setup_logging(log_dir):
    fmt = RedactingFormatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            Path(log_dir) / "capture.log", maxBytes=8 * 1024 * 1024, backupCount=3)
        fileh.setFormatter(fmt)
        root.addHandler(fileh)

    route_exceptions_through_logging()


def route_exceptions_through_logging():
    """Send uncaught exceptions to the log rather than to bare stderr.

    Found while verifying the redaction on real systemd: a camera thread that
    dies prints its traceback through `threading.excepthook`, which writes
    straight to stderr and never passes the formatter above. The exception
    text is exactly the thing that carries a URL, so that path was still a
    leak. It is also the path that logs a thread's death at no priority at
    all, which journald then shows as an error anyway, unlabelled.
    """
    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        log.error("thread %s died", name,
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    inherited = sys.excepthook

    def main_hook(exc_type, exc, tb):
        # Ctrl-C and a clean exit are not faults, and the default hook prints
        # them the way anyone running this by hand expects.
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            inherited(exc_type, exc, tb)
            return
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))

    threading.excepthook = thread_hook
    sys.excepthook = main_hook


# ----------------------------------------------------------------------------
# Per-camera settings
#
# `capture.interval_seconds` and `encode.framerate` are the defaults; a camera
# may carry either key itself and is then on its own cadence. A wide courtyard
# view is fine at one frame a minute, and a workbench is not. Absent means
# "follow the default", so raising the global interval still moves every camera
# that has not opted out.
# ----------------------------------------------------------------------------

def camera_interval(cam, cfg):
    """Seconds between snapshots for this camera."""
    return int(cam.get("interval_seconds") or cfg["capture"]["interval_seconds"])


def camera_timeout(cam, cfg):
    """Fetch timeout, never allowed to reach this camera's own interval.

    The global timeout is chosen against the global interval, so a camera that
    opts into a shorter one inherits a timeout that can outlast its own tick.
    That is not a slow camera, it is a camera whose every request is still in
    flight when the next one is due. Clamped rather than made a third knob:
    there is no useful answer other than "under the interval".
    """
    interval = camera_interval(cam, cfg)
    return max(1, min(int(cfg["capture"]["timeout_seconds"]), interval - 1))


def camera_framerate(cam, cfg):
    # .get on both levels: the daemon has never needed the `encode` section,
    # and it must not start requiring one just to annotate a day directory.
    return int(cam.get("framerate")
               or cfg.get("encode", {}).get("framerate", 60))


# ----------------------------------------------------------------------------
# One day, one cadence
#
# A day directory records the interval and frame rate it was captured at, and
# that record is what both this daemon and the encoder obey. It buys one rule
# with no exceptions: **a change to a camera's cadence takes effect at the next
# midnight**, whatever happens in between.
#
# Without it the rule leaks. Editing a camera at 14:00 and restarting capture
# would leave the day half at one cadence and half at another, and the video is
# then uneven with no way to tell after the fact. Worse, the encoder would
# measure that day's coverage against the *new* interval, so a complete day at
# one frame a minute would report 8% coverage.
#
# The marker is a dotfile, so valid_frames()'s "*.jpg" glob and the usage
# report's ".jpg" test both ignore it, and it goes when the day's frames go.
# ----------------------------------------------------------------------------

CADENCE_FILE = ".cadence.json"


def day_string(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def seconds_to_midnight(now=None):
    """Seconds until the next local midnight, floored at 1."""
    now = datetime.now() if now is None else now
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (midnight - now).total_seconds())


def read_cadence(day_dir):
    """(interval, framerate) this day was started at, or None."""
    try:
        with open(Path(day_dir) / CADENCE_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return int(d["interval_seconds"]), int(d["framerate"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_cadence(day_dir, interval, framerate):
    """Record a day's cadence, once. Never overwrites and never raises.

    Never overwrites, because the first writer is the one that knows what the
    day actually began at. Never raises, because failing to annotate a day is
    not a reason to stop capturing it; the encoder falls back to the config.
    """
    path = Path(day_dir) / CADENCE_FILE
    if path.exists():
        return False
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"interval_seconds": int(interval),
                       "framerate": int(framerate)}, fh)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.debug("could not record the cadence in %s: %s", day_dir, exc)
        return False


def config_cadence(cfg_path, name):
    """(interval, timeout, framerate) for one camera, freshly from disk.

    None when the file cannot be read or the camera is no longer in it.
    Capture must never stop because somebody is halfway through an edit, so
    every failure here means "keep running on what we have".
    """
    if not cfg_path:
        return None                 # constructed without one; keep what we have
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    for cam in cfg.get("cameras", []):
        if cam.get("name") == name:
            try:
                return (camera_interval(cam, cfg), camera_timeout(cam, cfg),
                        camera_framerate(cam, cfg))
            except (KeyError, TypeError, ValueError):
                return None
    return None


# ----------------------------------------------------------------------------
# HTTP snapshot cameras
# ----------------------------------------------------------------------------

class DayCadenceMixin:
    """Settles a camera's cadence for one day, and keeps it there.

    Both camera classes need the same rule, and it is the rule rather than the
    fetching that is easy to get subtly wrong.
    """

    def begin_day(self, day):
        """Adopt the cadence for `day`. True if it changed.

        A day already under way wins, always: its marker says what its frames
        were captured at, so a daemon restarted at 14:00 finishes the day the
        way it started it. Only a day with no marker yet, which means a day
        that has not begun, reads the config, and that is what makes an edit
        take effect at midnight and not before.
        """
        before = self.interval
        pinned = read_cadence(self.root / day)
        if pinned:
            self.interval, self.framerate = pinned
            self.timeout = max(1, min(self.base_timeout, self.interval - 1))
            source = "the day's recorded cadence"
        else:
            fresh = config_cadence(self.cfg_path, self.name_)
            if fresh:
                self.interval, self.timeout, self.framerate = fresh
            source = "the config"
        self.day = day
        if before != self.interval:
            self.log.info("%s: now one frame every %ss at %sfps, from %s",
                          day, self.interval, self.framerate, source)
        return before != self.interval


class HttpCamera(DayCadenceMixin, threading.Thread):

    def __init__(self, cam, cfg, cfg_path=None):
        super().__init__(name=f"cap-{cam['name']}", daemon=True)
        self.cam = cam
        self.name_ = cam["name"]
        self.url = cam["url"]
        self.cfg_path = cfg_path
        self.day = None
        self.interval = camera_interval(cam, cfg)
        self.framerate = camera_framerate(cam, cfg)
        self.base_timeout = int(cfg["capture"]["timeout_seconds"])
        self.timeout = camera_timeout(cam, cfg)
        self.min_bytes = cfg["capture"]["min_bytes"]
        self.log_every = cfg["capture"].get("log_every_n_failures", 60)
        self.retry = cfg["capture"].get("retry_within_tick", True)
        self.root = Path(cfg["paths"]["frames_root"]) / self.name_
        self.log = logging.getLogger(self.name_)

        self.session = requests.Session()
        auth_mode = (cam.get("auth") or "none").lower()
        user, pw = cam.get("username"), cam.get("password")
        if auth_mode == "digest":
            self.session.auth = HTTPDigestAuth(user, pw)
        elif auth_mode == "basic":
            self.session.auth = HTTPBasicAuth(user, pw)

        self._last_dir = None
        self.ok = 0
        self.fail = 0
        self.consec_fail = 0
        self.retried = 0        # ticks a second attempt rescued
        # Published in the heartbeat. None means "not yet", which is a
        # different thing from "a long time ago" and has to stay tellable.
        self.last_attempt = None
        self.last_success = None

    # -- helpers ------------------------------------------------------------

    def _dest_path(self, dt):
        """Path for this capture, creating the day dir when the date rolls over.

        NB: not named _target - threading.Thread.__init__ sets self._target,
        which would silently shadow the method."""
        day_dir = self.root / dt.strftime("%Y-%m-%d")
        if day_dir != self._last_dir:
            day_dir.mkdir(parents=True, exist_ok=True)
            # The day now exists, so record what it is being captured at. This
            # is the only place that knows the directory is new, which is
            # exactly when the answer is not in doubt.
            write_cadence(day_dir, self.interval, self.framerate)
            self._last_dir = day_dir
        final = day_dir / (dt.strftime("%H%M%S") + ".jpg")
        # Only collides during the DST fall-back hour, when local time repeats.
        if final.exists():
            n = 1
            while final.exists() and n < 100:
                final = day_dir / f"{dt.strftime('%H%M%S')}-{n}.jpg"
                n += 1
        return final

    def _retry_timeout(self, deadline, now):
        """Timeout for a second attempt this tick, or 0.0 to not bother.

        Purely budget-driven, which is what makes it safe: an attempt that
        *timed out* has already spent the tick, so the arithmetic declines on
        its own and no special case for slow-vs-fast failures is needed. The
        worst-case finish is deadline - RETRY_GUARD, so a retry can never cost
        the next frame as well.
        """
        budget = (deadline - RETRY_GUARD) - (now + RETRY_DELAY)
        if budget < RETRY_MIN_BUDGET:
            return 0.0
        return min(self.timeout, budget)

    def _retry_grab(self, dt, deadline, first_exc):
        """One more attempt inside the same tick. Returns None if it worked,
        otherwise the exception to report."""
        if not self.retry:
            return first_exc
        # The previous tick failed too, so this is an outage lasting longer than
        # one interval - and the next tick already *is* a retry. Measured: a
        # camera busy for a fixed 2.6s window (2 ticks) recovers 0% from a retry
        # 250ms later, so retrying here is pure extra load on a device that just
        # said it was busy. Only the first tick of a burst is worth a second try.
        if self.consec_fail:
            return first_exc
        timeout = self._retry_timeout(deadline, time.time())
        if not timeout:
            return first_exc
        if STOP.wait(RETRY_DELAY):      # shutting down; don't start a fetch
            return first_exc
        try:
            self._grab(dt, timeout=timeout)
        except Exception as exc:
            return exc
        self.retried += 1
        return None

    def _grab(self, dt, timeout=None):
        resp = self.session.get(self.url, timeout=timeout or self.timeout)
        resp.raise_for_status()
        data = resp.content

        if len(data) < self.min_bytes:
            raise ValueError(f"response too small ({len(data)} bytes)")
        if data[:2] != b"\xff\xd8":
            raise ValueError("response is not a JPEG (bad SOI marker)")

        final = self._dest_path(dt)
        tmp = final.parent / f".{final.stem}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, final)          # atomic: no partial file ever visible

    # -- main loop ----------------------------------------------------------

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.begin_day(day_string(time.time()))
        next_t = math.ceil(time.time() / self.interval) * self.interval
        # The timeout is logged because it can differ from the configured one:
        # a camera on a shorter interval than the global has it clamped, and
        # the journal is where that becomes visible.
        self.log.info("capture started (%ss interval, %ss timeout%s)",
                      self.interval, self.timeout,
                      ", per-camera" if self.cam.get("interval_seconds") else "")

        while not STOP.is_set():
            wait = next_t - time.time()
            if wait > 0:
                STOP.wait(wait)
            if STOP.is_set():
                break

            fire_t, next_t = next_t, next_t + self.interval

            # The day boundary is the only moment a cadence change is allowed
            # to land, so it is the only moment the config is re-read.
            day = day_string(fire_t)
            if day != self.day and self.begin_day(day):
                next_t = fire_t + self.interval

            # If we fell behind (slow camera, suspended host), resync forward
            # rather than trying to catch up on a backlog of missed frames.
            if next_t <= time.time():
                next_t = math.ceil(time.time() / self.interval) * self.interval

            if PAUSED.is_set():
                continue

            dt = datetime.fromtimestamp(fire_t)
            self.last_attempt = fire_t
            try:
                self._grab(dt)
                err = None
            except Exception as exc:
                # A rescued tick counts as a plain success: ok/fail stay a count
                # of frames on disk, which is what the encoder's Cov% reports.
                err = self._retry_grab(dt, next_t, exc)

            if err is None:
                if self.consec_fail:
                    self.log.info("recovered after %d consecutive failures",
                                  self.consec_fail)
                self.ok += 1
                self.consec_fail = 0
                # Wall clock, not fire_t: this is "when did a frame last land",
                # which a reader compares against now to decide whether a
                # camera has gone quiet. fire_t is when the tick was due.
                self.last_success = time.time()
            else:
                self.fail += 1
                self.consec_fail += 1
                # Log the first failure, then throttle to avoid flooding journald
                # when a camera is offline for hours.
                if self.consec_fail == 1 or self.consec_fail % self.log_every == 0:
                    self.log.warning("grab failed (#%d): %s", self.consec_fail, err)

        self.log.info("capture stopped (ok=%d fail=%d retried=%d)",
                      self.ok, self.fail, self.retried)


# ----------------------------------------------------------------------------
# RTSP cameras (no HTTP snapshot endpoint)
# ----------------------------------------------------------------------------

class RtspCamera(DayCadenceMixin, threading.Thread):
    """Supervises a persistent ffmpeg that writes one frame per interval.

    -strftime_mkdir 1 makes ffmpeg create the YYYY-MM-DD directory itself, so
    the on-disk layout matches the HTTP path exactly.
    """

    def __init__(self, cam, cfg, cfg_path=None):
        super().__init__(name=f"cap-{cam['name']}", daemon=True)
        self.cam = cam
        self.name_ = cam["name"]
        self.url = cam["url"]
        self.cfg_path = cfg_path
        self.day = None
        self.interval = camera_interval(cam, cfg)
        self.framerate = camera_framerate(cam, cfg)
        self.base_timeout = int(cfg["capture"]["timeout_seconds"])
        self.timeout = camera_timeout(cam, cfg)
        self.ffmpeg = cfg["paths"]["ffmpeg"]
        self.root = Path(cfg["paths"]["frames_root"]) / self.name_
        self.quality = str(cam.get("quality", 2))
        self.log = logging.getLogger(self.name_)
        self.proc = None
        self.restarts = 0
        # The RTSP path cannot report per-frame success: ffmpeg writes the
        # frames and this thread only supervises the process. What it does know
        # is when that process last started and how often it has had to be
        # restarted, so those are what it publishes, and last_success stays
        # None rather than being invented from something that is not one.
        self.last_attempt = None
        self.last_success = None
        self.last_started = None

    def _cmd(self):
        pattern = str(self.root / "%Y-%m-%d" / "%H%M%S.jpg")
        return [
            self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-use_wallclock_as_timestamps", "1",
            "-i", self.url,
            "-an",
            "-vf", f"fps=1/{self.interval}",
            "-q:v", self.quality,
            "-f", "image2",
            "-strftime", "1",
            "-strftime_mkdir", "1",
            # Stop at midnight so the supervisor gets a turn: this process
            # carries fps=1/interval on its command line, so adopting a new
            # cadence means launching a new one, and the day boundary is the
            # only moment that is allowed to happen. ffmpeg counting stream
            # time rather than wall clock means this can land a little either
            # side; that costs at most one relaunch, and the day's recorded
            # cadence is what actually pins the result.
            "-t", str(int(seconds_to_midnight())),
            "-y", pattern,
        ]

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.begin_day(day_string(time.time()))
        self.log.info("rtsp grabber started (%ss interval)", self.interval)

        while not STOP.is_set():
            if PAUSED.is_set():
                STOP.wait(30)
                continue
            # Re-read at the boundary and nowhere else, the same rule the HTTP
            # path follows. A mid-day relaunch after a dropped connection
            # therefore reconnects on the cadence the day started with.
            self.begin_day(day_string(time.time()))
            rolled = False
            try:
                self.proc = subprocess.Popen(
                    self._cmd(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                self.last_started = self.last_attempt = time.time()
                _, err = self.proc.communicate()
                if not STOP.is_set():
                    # rc=0 means it reached the -t deadline, which is the
                    # planned midnight handover rather than a fault. Logging
                    # that as a warning every night would train people to
                    # ignore the line that matters.
                    rolled = self.proc.returncode == 0
                    if rolled:
                        self.log.info("day rolled over; restarting the grabber")
                    else:
                        self.restarts += 1
                        msg = (err or b"").decode(errors="replace").strip()[:400]
                        self.log.warning("ffmpeg exited (rc=%s, restart #%d): %s",
                                         self.proc.returncode, self.restarts, msg)
            except Exception as exc:
                self.log.error("failed to start ffmpeg: %s", exc)
            if not STOP.is_set() and not rolled:
                STOP.wait(10)           # backoff before reconnecting

        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.info("rtsp grabber stopped")


# ----------------------------------------------------------------------------
# Disk guard
# ----------------------------------------------------------------------------

class DiskGuard(threading.Thread):

    def __init__(self, cfg):
        super().__init__(name="diskguard", daemon=True)
        self.path = Path(cfg["paths"]["frames_root"])
        self.min_free = cfg["capture"].get("min_free_gb", 0) * (1024 ** 3)
        self.log = logging.getLogger("diskguard")

    def run(self):
        if self.min_free <= 0:
            return
        while not STOP.is_set():
            try:
                free = shutil.disk_usage(self.path).free
                if free < self.min_free and not PAUSED.is_set():
                    PAUSED.set()
                    self.log.error(
                        "PAUSING capture: only %.1f GB free on %s (threshold %.1f GB)",
                        free / 1024 ** 3, self.path, self.min_free / 1024 ** 3)
                elif free >= self.min_free * 1.1 and PAUSED.is_set():
                    PAUSED.clear()
                    self.log.warning("RESUMING capture: %.1f GB free",
                                     free / 1024 ** 3)
            except Exception as exc:
                self.log.warning("disk check failed: %s", exc)
            STOP.wait(300)


# ----------------------------------------------------------------------------

def record_cadences(cams):
    """Annotate today's day directory for any camera that has one yet.

    The HTTP path records the cadence as it creates the directory, but the
    RTSP path never creates one: ffmpeg does that itself, through
    -strftime_mkdir, at whatever moment its first frame lands. So the marker
    is also written from here, once a minute, for a directory that already
    exists.

    It never creates a directory. A camera that is offline all day would
    otherwise leave an empty one behind every night, which the encoder would
    find, report as a SKIP, and never clean up.
    """
    today = day_string(time.time())
    for cam in cams:
        day_dir = cam.root / today
        if day_dir.is_dir():
            write_cadence(day_dir, cam.interval, cam.framerate)


# ----------------------------------------------------------------------------
# Runtime state
#
# systemd can say this process is alive. It cannot say the cameras are
# answering: a daemon whose every camera is refusing connections is
# `active (running)` and looks perfect on a status page. This is where the
# program that knows better says so.
#
# Duplicated from timelapse_encode.py, deliberately and for the same reason
# load_config() and the redaction rule are: this daemon imports nothing from
# its siblings, so that a syntax error in a script it does not need cannot
# stop the capture. A test pins the copies together.
#
# Facts only, never conclusions. Whether "42 seconds since the last frame"
# means a fault depends on the interval, on whether capture is paused and on
# who is asking; a reader can work that out and a writer cannot take it back.
# ----------------------------------------------------------------------------

STATE_DIR_DEFAULT = "/var/lib/timelapse/state"
CAPTURE_STATE = "capture.json"
STATE_VERSION = 1
_state_warned = False


def state_dir(cfg):
    return Path((cfg.get("paths", {}).get("state_dir") or "").strip()
                or STATE_DIR_DEFAULT)


def stamp(epoch):
    """Epoch to a local ISO string, or None. None means "never", not "now"."""
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch).replace(microsecond=0).isoformat()


def capture_state(cams, started, running=True):
    """The snapshot published to capture.json.

    Read from the camera threads without a lock. Every value taken here is a
    single int or float that one thread writes and this one reads, so the worst
    outcome is a counter that is one tick old, which for a heartbeat written
    once a minute is not worth a lock in the capture path.
    """
    now = time.time()
    cameras = []
    for c in cams:
        entry = {
            "name": c.name_,
            "method": "rtsp" if isinstance(c, RtspCamera) else "http",
            "interval": c.interval,
            "framerate": c.framerate,
            "last_attempt": stamp(c.last_attempt),
            "last_success": stamp(c.last_success),
        }
        if isinstance(c, RtspCamera):
            # No per-frame answer exists here; see the comment in __init__.
            entry.update(supervised=True, restarts=c.restarts,
                         last_started=stamp(c.last_started),
                         alive=bool(c.proc and c.proc.poll() is None))
        else:
            entry.update(supervised=False, ok=c.ok, fail=c.fail,
                         retried=c.retried, consec_fail=c.consec_fail)
        cameras.append(entry)
    return {
        "version": STATE_VERSION,
        "kind": "capture",
        "pid": os.getpid(),
        "started": stamp(started),
        "updated": stamp(now),
        "updated_epoch": int(now),
        "running": running,
        # The disk guard's verdict, which is the one thing here that systemd
        # actively misrepresents: a paused daemon is still `active (running)`
        # and still capturing nothing.
        "paused": PAUSED.is_set(),
        "cameras": cameras,
    }


def write_state(cfg, cams, started, running=True):
    """Publish the heartbeat. Never raises.

    A daemon that cannot write its status file must keep capturing; the file
    is how you find out about a problem, not a part of the job. It complains
    once and then stays quiet, because the alternative is a line a minute in
    the journal for as long as the condition lasts.
    """
    global _state_warned
    path = state_dir(cfg) / CAPTURE_STATE
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(capture_state(cams, started, running), fh)
        os.replace(tmp, path)
        _state_warned = False
        return True
    except OSError as exc:
        if not _state_warned:
            log.warning("cannot write %s (%s); capture continues, but the web "
                        "UI and 'timelapse test' will report capture state as "
                        "unavailable", path, exc)
            _state_warned = True
        return False


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"timelapse_capture.py {__version__}")
        return
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(f"usage: timelapse_capture.py [config.json]\n{__doc__}")
        return

    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "/etc/timelapse/config.json"
    cfg = load_config(cfg_path)
    setup_logging(cfg["paths"].get("log_dir"))

    Path(cfg["paths"]["frames_root"]).mkdir(parents=True, exist_ok=True)

    def on_signal(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        STOP.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    threads = [DiskGuard(cfg)]
    for cam in cfg["cameras"]:
        if not cam.get("enabled", True):
            log.info("skipping disabled camera: %s", cam["name"])
            continue
        method = (cam.get("method") or "http").lower()
        if method == "http":
            threads.append(HttpCamera(cam, cfg, cfg_path))
        elif method == "rtsp":
            threads.append(RtspCamera(cam, cfg, cfg_path))
        else:
            log.error("unknown method %r for camera %s", method, cam["name"])

    if len(threads) == 1:
        sys.exit("No enabled cameras in config.")

    for t in threads:
        t.start()

    cams = [t for t in threads if isinstance(t, DayCadenceMixin)]
    log.info("running with %d camera thread(s)", len(cams))
    started = time.time()
    # Immediately, not at the first minute mark. A restart otherwise leaves the
    # previous run's file in place for a minute, which reads as a live daemon
    # that has stopped taking pictures.
    write_state(cfg, cams, started)
    ticks = 0
    while not STOP.is_set():
        STOP.wait(1)
        ticks += 1
        if ticks % 60 == 0:
            record_cadences(cams)
            write_state(cfg, cams, started)

    for t in threads:
        t.join(timeout=20)
    # A final write with running=false, so a stopped daemon is distinguishable
    # from a wedged one. A reader that only had staleness to go on would call
    # both of them the same thing after a minute, and only one of them is a
    # fault.
    write_state(cfg, cams, started, running=False)
    log.info("exited cleanly")


if __name__ == "__main__":
    main()

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
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from requests.auth import HTTPBasicAuth, HTTPDigestAuth
except ImportError:
    sys.exit("Missing dependency: pip install requests "
             "(or: sudo apt install python3-requests)")

__version__ = "0.0.8"


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


def setup_logging(log_dir):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
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


# ----------------------------------------------------------------------------
# HTTP snapshot cameras
# ----------------------------------------------------------------------------

class HttpCamera(threading.Thread):

    def __init__(self, cam, cfg):
        super().__init__(name=f"cap-{cam['name']}", daemon=True)
        self.cam = cam
        self.name_ = cam["name"]
        self.url = cam["url"]
        self.interval = cfg["capture"]["interval_seconds"]
        self.timeout = cfg["capture"]["timeout_seconds"]
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

    # -- helpers ------------------------------------------------------------

    def _dest_path(self, dt):
        """Path for this capture, creating the day dir when the date rolls over.

        NB: not named _target - threading.Thread.__init__ sets self._target,
        which would silently shadow the method."""
        day_dir = self.root / dt.strftime("%Y-%m-%d")
        if day_dir != self._last_dir:
            day_dir.mkdir(parents=True, exist_ok=True)
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
        next_t = math.ceil(time.time() / self.interval) * self.interval
        self.log.info("capture started (%ss interval)", self.interval)

        while not STOP.is_set():
            wait = next_t - time.time()
            if wait > 0:
                STOP.wait(wait)
            if STOP.is_set():
                break

            fire_t, next_t = next_t, next_t + self.interval
            # If we fell behind (slow camera, suspended host), resync forward
            # rather than trying to catch up on a backlog of missed frames.
            if next_t <= time.time():
                next_t = math.ceil(time.time() / self.interval) * self.interval

            if PAUSED.is_set():
                continue

            dt = datetime.fromtimestamp(fire_t)
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

class RtspCamera(threading.Thread):
    """Supervises a persistent ffmpeg that writes one frame per interval.

    -strftime_mkdir 1 makes ffmpeg create the YYYY-MM-DD directory itself, so
    the on-disk layout matches the HTTP path exactly.
    """

    def __init__(self, cam, cfg):
        super().__init__(name=f"cap-{cam['name']}", daemon=True)
        self.cam = cam
        self.name_ = cam["name"]
        self.url = cam["url"]
        self.interval = cfg["capture"]["interval_seconds"]
        self.ffmpeg = cfg["paths"]["ffmpeg"]
        self.root = Path(cfg["paths"]["frames_root"]) / self.name_
        self.quality = str(cam.get("quality", 2))
        self.log = logging.getLogger(self.name_)
        self.proc = None
        self.restarts = 0

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
            "-y", pattern,
        ]

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.log.info("rtsp grabber started")

        while not STOP.is_set():
            if PAUSED.is_set():
                STOP.wait(30)
                continue
            try:
                self.proc = subprocess.Popen(
                    self._cmd(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                _, err = self.proc.communicate()
                if not STOP.is_set():
                    self.restarts += 1
                    msg = (err or b"").decode(errors="replace").strip()[:400]
                    self.log.warning("ffmpeg exited (rc=%s, restart #%d): %s",
                                     self.proc.returncode, self.restarts, msg)
            except Exception as exc:
                self.log.error("failed to start ffmpeg: %s", exc)
            if not STOP.is_set():
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
            threads.append(HttpCamera(cam, cfg))
        elif method == "rtsp":
            threads.append(RtspCamera(cam, cfg))
        else:
            log.error("unknown method %r for camera %s", method, cam["name"])

    if len(threads) == 1:
        sys.exit("No enabled cameras in config.")

    for t in threads:
        t.start()

    log.info("running with %d camera thread(s)", len(threads) - 1)
    while not STOP.is_set():
        STOP.wait(1)

    for t in threads:
        t.join(timeout=20)
    log.info("exited cleanly")


if __name__ == "__main__":
    main()

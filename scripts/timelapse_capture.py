#!/usr/bin/env python3
"""
timelapse_capture.py — long-running snapshot grabber.

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

__version__ = "0.0.3"


# ----------------------------------------------------------------------------
# Shared state
# ----------------------------------------------------------------------------

STOP = threading.Event()
PAUSED = threading.Event()      # set by the disk guard when space runs low
log = logging.getLogger("capture")


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


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

    def _grab(self, dt):
        resp = self.session.get(self.url, timeout=self.timeout)
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

            try:
                self._grab(datetime.fromtimestamp(fire_t))
                if self.consec_fail:
                    self.log.info("recovered after %d consecutive failures",
                                  self.consec_fail)
                self.ok += 1
                self.consec_fail = 0
            except Exception as exc:
                self.fail += 1
                self.consec_fail += 1
                # Log the first failure, then throttle to avoid flooding journald
                # when a camera is offline for hours.
                if self.consec_fail == 1 or self.consec_fail % self.log_every == 0:
                    self.log.warning("grab failed (#%d): %s", self.consec_fail, exc)

        self.log.info("capture stopped (ok=%d fail=%d)", self.ok, self.fail)


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

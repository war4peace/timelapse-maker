#!/usr/bin/env python3
"""
timelapse_encode.py: nightly encode + notify + ship.

Finds every completed day directory under <frames_root>/<Camera>/, encodes
each to a 60 fps AV1 video, deletes the frames on success, sends a Discord
summary, then rsyncs the videos to the configured destination (moving them,
not copying) - a NAS share, another host, or any local path.

"Completed" means any date directory strictly older than today, so a missed
run (host down, crash) is picked up automatically on the next pass rather
than silently leaving frames behind.

Usage:
    timelapse_encode.py [config.json] [--date YYYY-MM-DD] [--dry-run]
                        [--keep-frames] [--no-transfer]
"""

import argparse
import json
import logging
import logging.handlers
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import request as urlrequest

__version__ = "0.1.1"

log = logging.getLogger("encode")
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------------

def setup_logging(log_dir):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            Path(log_dir) / "encode.log", maxBytes=8 * 1024 * 1024, backupCount=5)
        fh.setFormatter(fmt)
        root.addHandler(fh)


def human_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


# ----------------------------------------------------------------------------
# Encoder selection
# ----------------------------------------------------------------------------

def build_candidates(enc, gop=None):
    """Ordered encoder candidates: AV1 -> HEVC -> x264.

    `gop` is a parameter because a camera on its own frame rate needs its own
    keyframe interval: 120 frames is two seconds at 60fps and four at 30. The
    codec is chosen once per run by probing; the arguments are rebuilt per
    camera from the same function, so the two cannot drift.
    """
    gop = str(enc.get("gop", 120) if gop is None else gop)
    return [
        {
            "name": "AV1 (av1_nvenc)",
            "codec": "av1_nvenc",
            "args": ["-c:v", "av1_nvenc",
                     "-preset", str(enc.get("av1_preset", "p6")),
                     "-tune", "hq",
                     "-rc", "vbr", "-cq", str(enc.get("av1_cq", 26)), "-b:v", "0",
                     "-g", gop],
        },
        {
            "name": "H.265 (hevc_nvenc)",
            "codec": "hevc_nvenc",
            "args": ["-c:v", "hevc_nvenc",
                     "-preset", "p6", "-tune", "hq",
                     "-rc", "vbr", "-cq", str(enc.get("hevc_cq", 24)), "-b:v", "0",
                     "-g", gop],
        },
        {
            "name": "H.264 (libx264)",
            "codec": "libx264",
            "args": ["-c:v", "libx264", "-preset", "slow",
                     "-crf", str(enc.get("x264_crf", 20)), "-g", gop],
        },
    ]


# NVENC rejects small frames: hevc_nvenc fails 128x128 outright with
# "InitializeEncoder failed: invalid param (8): Frame dimensions". 512 is
# comfortably clear of every documented minimum and costs nothing to encode.
PROBE_SIZE = "512x512"

# The probe MUST encode in the same pixel format the real pipeline produces,
# or it tests something that will never run.
#
# testsrc emits rgb24. Left to negotiate, ffmpeg picks the closest format the
# encoder advertises - which for av1_nvenc is yuv444p. NVENC on Ada does not
# support AV1 in 4:4:4, so the capability check fails and ffmpeg reports the
# unhelpful "No capable devices found". That made an RTX 4060 look incapable of
# AV1 when it is not: encode_day()'s filter chain ends in format=yuv420p, so
# real encodes would always have succeeded. Forcing it here keeps probe and
# pipeline honest with each other.
PIX_FMT = "yuv420p"


def list_encoders(ffmpeg):
    """Encoder names this ffmpeg binary was built with, or None if unknown.

    Distinguishes "the build has no av1_nvenc" from "the build has it but the
    GPU or driver cannot use it" - which need completely different fixes.
    """
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    names = set()
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0][:1] in "VAS":
            names.add(parts[1])
    return names or None


def probe_encoder_detail(ffmpeg, candidate):
    """(available, message). On failure, message is ffmpeg's own error.

    Never swallow this. The two failure modes look identical from the exit
    code but need opposite responses: "No capable devices found" is the GPU or
    driver, "Unknown encoder" is the ffmpeg build.
    """
    cmd = ([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats",
            "-f", "lavfi", "-i", f"testsrc=size={PROBE_SIZE}:rate=1",
            "-frames:v", "1", "-pix_fmt", PIX_FMT]
           + candidate["args"] + ["-f", "null", "-"])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return False, f"{ffmpeg} not found"
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    except Exception as exc:
        return False, str(exc)[:200]
    if p.returncode == 0:
        return True, ""
    return False, " ".join((p.stderr or "").split())[:300] or f"exit {p.returncode}"


def probe_encoder(ffmpeg, candidate):
    return probe_encoder_detail(ffmpeg, candidate)[0]


# Verbose lines worth keeping from an NVENC probe. ffmpeg logs the real reason
# a device was rejected at AV_LOG_VERBOSE and prints only the useless summary
# ("No capable devices found") at error level.
_VERBOSE_KEEP = (
    "loaded nvenc version", "cuda capable devices", "gpu #",
    "codec not supported", "does not support nvenc", "no capable devices",
    "required nvenc features", "minimum required nvidia driver",
    "opencodesessionex", "openencodesessionex", "cannot load", "sessions",
    "not supported", "invalid param", "out of memory",
)


def probe_encoder_verbose(ffmpeg, candidate, size=None):
    """The informative lines from a verbose probe run.

    Only worth calling after a failure: it costs a second ffmpeg invocation and
    exists purely to recover the reason that -loglevel error discards.
    """
    cmd = ([ffmpeg, "-v", "verbose", "-y", "-hide_banner", "-nostats",
            "-f", "lavfi", "-i", f"testsrc=size={size or PROBE_SIZE}:rate=1",
            "-frames:v", "1", "-pix_fmt", PIX_FMT]
           + candidate["args"] + ["-f", "null", "-"])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception as exc:
        return [f"could not run verbose probe: {exc}"]
    lines, seen = [], set()
    for raw in (p.stderr or "").splitlines():
        low = raw.lower()
        if not any(k in low for k in _VERBOSE_KEEP):
            continue
        text = raw.split("] ", 1)[-1].strip()
        if text and text not in seen:
            seen.add(text)
            lines.append(text)
    return lines


def encoder_hint(codec, message, built_in=None):
    """Plain-language cause for a failed probe, or '' if there is nothing useful.

    Derived from ffmpeg's message, never guessed from the codec name. An
    earlier version guessed and told an RTX 4060 owner their GPU was too old
    for AV1, which was both wrong and a dead end.
    """
    m = (message or "").lower()
    if built_in is False or "unknown encoder" in m:
        return (f"this ffmpeg build does not include {codec}; "
                f"try jellyfin-ffmpeg or a BtbN static build")
    if "minimum required nvidia driver" in m:
        return "the NVIDIA driver is older than this ffmpeg build requires"
    if "cannot load" in m or "libnvidia-encode" in m:
        return "the NVIDIA encode library is missing; install the driver's NVENC support"
    if "out of memory" in m or "session" in m:
        return ("no free NVENC session - another process (an NVR, a "
                "transcoder) may be holding them all")
    if "invalid param" in m and "dimension" in m:
        return "the probe frame was rejected as too small - please report this"
    if "not supported" in m and any(
            fmt in m for fmt in ("yuv44", "yuv42", "p010", "nv12", "rgb")):
        return (f"{codec} rejected the pixel format offered to it. The probe "
                f"forces {PIX_FMT}, so seeing this means the GPU cannot "
                f"encode {codec} even in 4:2:0")
    if "codec not supported" in m or "no capable devices" in m:
        # Ambiguous on purpose: the driver did not advertise this codec for
        # this GPU, which is either a genuinely incapable GPU or an ffmpeg
        # whose NVENC headers predate the codec. Do not assert which.
        return (f"the driver did not advertise {codec} support for this GPU. "
                f"If the GPU does support it, the ffmpeg build is the likely "
                f"culprit - run 'timelapse test --encoders' for the details")
    return ""


def select_encoder(ffmpeg, enc):
    built = list_encoders(ffmpeg)
    for cand in build_candidates(enc):
        ok, message = probe_encoder_detail(ffmpeg, cand)
        log.info("  probe %-22s %s", cand["name"],
                 "available" if ok else "not available")
        if ok:
            return cand
        in_build = None if built is None else (cand["codec"] in built)
        hint = encoder_hint(cand["codec"], message, in_build)
        if hint:
            log.info("      %s", hint)
        if message:
            log.debug("      ffmpeg: %s", message)
    return None


# ----------------------------------------------------------------------------
# Frame handling
# ----------------------------------------------------------------------------

def valid_frames(day_dir, min_bytes):
    """Sorted list of frames that pass a cheap size + JPEG-header check.

    Filenames are zero-padded HHMMSS, so lexical order is chronological and we
    never depend on mtime.
    """
    good, bad = [], 0
    for p in sorted(day_dir.glob("*.jpg")):
        try:
            if p.stat().st_size < min_bytes:
                bad += 1
                continue
            with open(p, "rb") as fh:
                if fh.read(3) != b"\xff\xd8\xff":
                    bad += 1
                    continue
        except OSError:
            bad += 1
            continue
        good.append(p)
    return good, bad


def probe_dimensions(ffprobe, path):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
        capture_output=True, text=True, timeout=30)
    m = re.search(r"(\d+)x(\d+)", out.stdout)
    if not m:
        raise RuntimeError(f"could not probe dimensions of {path.name}")
    w, h = int(m.group(1)), int(m.group(2))
    return w - (w % 2), h - (h % 2)      # nvenc requires even dimensions


def write_concat_list(frames, target):
    """ffmpeg concat demuxer escaping: a literal ' becomes '\\''."""
    with open(target, "w", encoding="utf-8") as fh:      # no BOM
        for p in frames:
            fh.write("file '%s'\n" % str(p).replace("'", "'\\''"))


GOP_SECONDS = 2

# Written by the capture daemon into each day directory: the interval and
# frame rate that day was actually captured at. It beats the config, because
# the config says what is in force *now* and this day may predate a change.
# Without it, editing a camera in the afternoon would make tonight's encode
# measure yesterday against a cadence yesterday never ran at.
CADENCE_FILE = ".cadence.json"


def day_cadence(day_dir):
    """(interval, framerate) recorded for a day, or None.

    None for every day captured before this existed, and for any day the
    daemon could not annotate, so the caller falls back to the config.
    """
    try:
        with open(Path(day_dir) / CADENCE_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return int(d["interval_seconds"]), int(d["framerate"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def camera_entry(cfg, name):
    """The config entry for a camera name, or {}.

    {} rather than an error: the encoder builds its work list from the config,
    so a name it is asked about is normally there, and a caller that has one
    from somewhere else should get the defaults rather than a traceback.
    """
    for cam in cfg.get("cameras", []):
        if cam.get("name") == name:
            return cam
    return {}


def camera_interval(cfg, cam):
    return int(cam.get("interval_seconds") or cfg["capture"]["interval_seconds"])


def camera_framerate(cfg, cam):
    return int(cam.get("framerate") or cfg["encode"].get("framerate", 60))


def camera_gop(cfg, cam):
    """Keyframe interval in frames, following this camera's frame rate.

    An explicit per-camera `gop` wins. Otherwise a camera that sets its own
    frame rate gets two seconds' worth at that rate, and one that does not
    keeps whatever the global config says, which may have been hand-tuned.
    """
    if cam.get("gop"):
        return int(cam["gop"])
    if cam.get("framerate"):
        return camera_framerate(cfg, cam) * GOP_SECONDS
    return int(cfg["encode"].get("gop", 120))


def encode_day(cfg, encoder, camera, day_dir, out_dir, dry_run):
    enc = cfg["encode"]
    ffmpeg = cfg["paths"]["ffmpeg"]
    ffprobe = cfg["paths"].get("ffprobe", "ffprobe")
    cam = camera_entry(cfg, camera)
    # What this day was captured at wins over what the config says now. A
    # cadence edit takes effect at midnight, so a day that began before one is
    # still that day's cadence, and this is where that stays true.
    recorded = day_cadence(day_dir)
    if recorded:
        interval, framerate = recorded
        gop = int(cam.get("gop") or framerate * GOP_SECONDS)
    else:
        interval = camera_interval(cfg, cam)
        framerate = camera_framerate(cfg, cam)
        gop = camera_gop(cfg, cam)
    fps = str(framerate)
    day = day_dir.name
    started = time.time()

    # Carried in the result so the summary can work out coverage against the
    # cadence this camera actually ran at, rather than the global one.
    result = {"camera": camera, "date": day, "status": "FAIL", "frames": 0,
              "bad": 0, "size": 0, "seconds": 0, "note": "",
              "interval": interval, "fps": framerate}

    frames, bad = valid_frames(day_dir, cfg["capture"]["min_bytes"])
    result["frames"], result["bad"] = len(frames), bad

    if len(frames) < enc.get("min_frames", 100):
        result["status"] = "SKIP"
        result["note"] = f"only {len(frames)} usable frames"
        result["seconds"] = time.time() - started
        log.warning("  %s %s: skipping - %s", camera, day, result["note"])
        return result

    out_file = out_dir / f"{camera}.{day.replace('-', '')}.{enc.get('container','mkv')}"

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
        concat_path = Path(tf.name)
    try:
        # Inside the try: a camera whose first frame will not probe must fail
        # only itself, not abort the whole run.
        w, h = probe_dimensions(ffprobe, frames[0])

        # JPEG decodes as full-range (yuvj420p). Convert to limited-range BT.709
        # and tag it, so the result looks correct in any player rather than
        # washed out. Pinning the scaler to the first frame's size also makes the
        # run immune to a stray odd-sized snapshot mid-day.
        vf = (f"scale={w}:{h}:in_range=full:out_range=limited,"
              f"format={PIX_FMT}")

        write_concat_list(frames, concat_path)

        # Rebuilt for this camera's keyframe interval, from the codec the run
        # already probed. Appending a second -g instead would leave the
        # command carrying two values for one option, which is exactly the
        # confusion the duplicated -r below has to be commented for.
        args = next((c["args"] for c in build_candidates(enc, gop)
                     if c["codec"] == encoder["codec"]), encoder["args"])

        cmd = ([ffmpeg, "-y", "-hide_banner", "-loglevel", "warning", "-nostats",
                "-f", "concat", "-safe", "0", "-r", fps, "-i", str(concat_path),
                "-vf", vf]
               + args
               + ["-color_range", "tv", "-colorspace", "bt709",
                  "-color_primaries", "bt709", "-color_trc", "bt709",
                  "-r", fps, str(out_file)])

        log.info("  %s %s: encoding %d frames (%dx%d, %d bad) at %sfps -> %s",
                 camera, day, len(frames), w, h, bad, fps, out_file.name)

        if dry_run:
            result["status"] = "DRY"
            result["note"] = "dry run"
            result["seconds"] = time.time() - started
            return result

        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise RuntimeError(f"ffmpeg rc={proc.returncode}: {' | '.join(tail)}")

        if not out_file.exists() or out_file.stat().st_size < 1024:
            raise RuntimeError("output missing or implausibly small")

        result["size"] = out_file.stat().st_size
        result["status"] = "OK"
        if bad:
            result["note"] = f"{bad} bad frame(s) skipped"

    except Exception as exc:
        result["note"] = str(exc)[:300]
        log.error("  %s %s: FAILED - %s", camera, day, result["note"])
        if out_file.exists():
            out_file.unlink(missing_ok=True)
    finally:
        concat_path.unlink(missing_ok=True)
        result["seconds"] = time.time() - started

    return result


# ----------------------------------------------------------------------------
# Transfer
# ----------------------------------------------------------------------------

def nearest_mountpoint(path):
    """The deepest ancestor of path (or path itself) that is a mount point."""
    p = Path(path).resolve()
    while p != p.parent and not os.path.ismount(p):
        p = p.parent
    return p


def mount_problem(t, dest):
    """Why dest must not be written to, or None if it is fine.

    An unmounted CIFS/NFS destination is an ordinary empty local directory, so
    rsync would cheerfully fill the local disk instead of the NAS - and with
    --remove-source-files, delete the originals afterwards. Setting
    transfer.require_mountpoint refuses to transfer in that case.

    true    - the destination must sit on something mounted below /
    "/path" - that exact path must be a mount point (precise; prefer it when
              an intermediate directory like /mnt is its own filesystem)
    """
    req = t.get("require_mountpoint")
    if not req or not dest.startswith("/"):
        return None
    if isinstance(req, str):
        if os.path.ismount(req):
            return None
        return f"{req} is not a mounted filesystem"
    mp = nearest_mountpoint(dest)
    if mp == mp.parent:
        # Walked all the way to the filesystem root without finding a mount,
        # so nothing is mounted under the destination. Comparing against the
        # root this way rather than to os.sep keeps it correct wherever the
        # root is not literally "/".
        return (f"{dest} is not on a mounted filesystem - the share is "
                f"probably not mounted")
    return None


def transfer(cfg, dry_run):
    """rsync the video folder to the destination. Works for both a local mount
    path and a remote user@host:/path spec."""
    t = cfg.get("transfer", {})
    if not t.get("enabled", False):
        return None
    src = Path(cfg["paths"]["video_output"])
    files = sorted(src.glob("*.*"))
    if not files:
        return {"ok": True, "moved": 0, "detail": "nothing to transfer"}

    dest = t["destination"]

    problem = mount_problem(t, dest)
    if problem:
        # Deliberately not an exception: the encode succeeded, the videos are
        # safe in video_output, and the next run ships them once the mount is
        # back. Filling the frames disk instead would be the real disaster.
        log.error("Refusing to transfer - %s", problem)
        return {"ok": False, "moved": 0, "detail": problem}

    args = list(t.get("rsync_args", ["-a", "--partial", "--remove-source-files"]))
    cmd = ["rsync"] + args + [str(f) for f in files] + [dest]

    log.info("Transferring %d file(s) to %s", len(files), dest)
    if dry_run:
        return {"ok": True, "moved": 0, "detail": "dry run"}

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        log.error("rsync is not installed (sudo apt install rsync)")
        return {"ok": False, "moved": 0, "detail": "rsync not installed"}
    except Exception as exc:
        log.error("rsync could not be started: %s", exc)
        return {"ok": False, "moved": 0, "detail": str(exc)[:300]}

    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()[-3:]
        log.error("rsync failed: %s", " | ".join(detail))
        return {"ok": False, "moved": 0, "detail": " | ".join(detail)[:400]}

    remaining = [f for f in files if f.exists()]
    if t.get("delete_local_after_transfer", True):
        for f in remaining:
            f.unlink(missing_ok=True)
    return {"ok": True, "moved": len(files), "detail": ""}


# ----------------------------------------------------------------------------
# Discord
# ----------------------------------------------------------------------------

# Discord sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/3.x" User-Agent with HTTP 403 before the request ever reaches
# the webhook. This is the format Discord documents for API clients.
USER_AGENT = ("DiscordBot (https://github.com/war4peace/timelapse-maker, "
              f"{__version__})")


def post_webhook(url, payload, timeout=20):
    """POST a JSON payload to a webhook. Raises on transport or HTTP error."""
    req = urlrequest.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": USER_AGENT})
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def send_discord(cfg, title, description, color, fields):
    d = cfg.get("discord", {})
    if not d.get("enabled") or not d.get("webhook_url"):
        log.info("Discord disabled or no webhook set; skipping notification.")
        return
    payload = {
        "username": d.get("username", "Timelapse Bot"),
        "embeds": [{
            "title": title,
            "description": description[:4000],
            "color": color,
            "fields": [{"name": n, "value": (v or "-")[:1024], "inline": False}
                       for n, v in fields][:25],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    try:
        post_webhook(d["webhook_url"], payload)
    except Exception as exc:
        # Deliberately broad: a socket timeout is not a URLError, and a failed
        # notification must never take down the run that it is reporting on.
        log.warning("Discord notification failed: %s", exc)


def build_summary(results, interval):
    fmt = "{:<12} {:<10} {:<5} {:>7} {:>5} {:>9} {:>8}"
    rows = [fmt.format("Camera", "Date", "St", "Frames", "Cov%", "Size", "Time"),
            "-" * 62]
    for r in results:
        # Against the cadence this camera ran at. Using the global interval
        # made a camera at one frame a minute read as 8% coverage every night,
        # which is a full day of frames reported as a near-total outage.
        expected = int(86400 / (r.get("interval") or interval))
        cov = f"{100.0 * r['frames'] / expected:.0f}" if r["frames"] else "-"
        rows.append(fmt.format(
            r["camera"][:12], r["date"], r["status"],
            r["frames"] or "-", cov,
            human_size(r["size"]) if r["size"] else "-",
            human_duration(r["seconds"])))
    return "```\n" + "\n".join(rows) + "\n```"


# ----------------------------------------------------------------------------

def load_config(path):
    """Read the config, failing with a sentence instead of a traceback.

    Shared with timelapse_test.py. The three most likely states - not
    configured yet, invalid JSON after a hand-edit, and unreadable because
    config.json is 0640 root:timelapse - each need a different action, and a
    stack trace tells the operator none of them.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        sys.exit(f"No config at {path}. Run: sudo timelapse setup")
    except PermissionError:
        sys.exit(f"Cannot read {path} (it is 0640 root:timelapse, since it "
                 f"holds camera credentials).\nRun with sudo, or add yourself "
                 f"to the 'timelapse' group.")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}\n"
                 f"Fix it with 'sudo timelapse config', or put back the last "
                 f"good one with 'sudo timelapse restore'.")
    except OSError as exc:
        sys.exit(f"Cannot read {path}: {exc}")


def find_pending(frames_root, cameras, only_date, max_backlog):
    """Every date dir older than today, oldest first, capped at max_backlog."""
    today = date.today().isoformat()
    jobs = []
    for cam in cameras:
        cam_dir = frames_root / cam
        if not cam_dir.is_dir():
            continue
        for d in sorted(cam_dir.iterdir()):
            if not d.is_dir() or not DATE_DIR.match(d.name):
                continue
            if only_date:
                if d.name == only_date:
                    jobs.append((cam, d))
            elif d.name < today:
                jobs.append((cam, d))
    if not only_date and max_backlog:
        keep = sorted({d.name for _, d in jobs})[-max_backlog:]
        jobs = [(c, d) for c, d in jobs if d.name in keep]
    return sorted(jobs, key=lambda j: (j[1].name, j[0]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("config", nargs="?", default="/etc/timelapse/config.json")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--date", help="process only this YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--no-transfer", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg["paths"].get("log_dir"))

    run_start = time.time()
    frames_root = Path(cfg["paths"]["frames_root"])
    out_dir = Path(cfg["paths"]["video_output"])
    out_dir.mkdir(parents=True, exist_ok=True)
    interval = cfg["capture"]["interval_seconds"]
    cameras = [c["name"] for c in cfg["cameras"] if c.get("enabled", True)]

    log.info("=" * 62)
    log.info(" Timelapse encode run - %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 62)

    try:
        log.info("Encoder detection:")
        encoder = select_encoder(cfg["paths"]["ffmpeg"], cfg["encode"])
        if encoder is None:
            raise RuntimeError("No usable encoder found "
                               "(tried av1_nvenc, hevc_nvenc, libx264)")
        log.info("Selected: %s", encoder["name"])

        jobs = find_pending(frames_root, cameras, args.date,
                            cfg["encode"].get("max_backlog_days", 7))
        if not jobs:
            log.info("Nothing to process.")
            # Still ship the backlog. A transfer that failed last night leaves
            # videos in video_output, and returning here without trying again
            # stranded them until some later night happened to produce a new
            # video. That made fixing the share by hand useless: the obvious
            # move, re-running the encode, was the one path that never
            # retried. transfer() returns "nothing to transfer" harmlessly
            # when video_output is empty, which is the usual case here.
            xfer = None if args.no_transfer else transfer(cfg, args.dry_run)
            fields = [("Encoder", encoder["name"])]
            if xfer is not None and (xfer["moved"] or not xfer["ok"]):
                fields.append(("Transfer",
                               ("OK - %d file(s) moved" % xfer["moved"])
                               if xfer["ok"] else "FAILED - " + xfer["detail"]))
            good = xfer is None or xfer["ok"]
            send_discord(cfg,
                         "Timelapse - nothing to do" if good
                         else "⚠️ Timelapse - transfer failed",
                         "No completed day folders were found.",
                         0x95A5A6 if good else 0xF1C40F, fields)
            return 0 if good else 1

        log.info("Found %d job(s) across %d camera(s).", len(jobs), len(cameras))

        results = []
        for cam, day_dir in jobs:
            r = encode_day(cfg, encoder, cam, day_dir, out_dir, args.dry_run)
            results.append(r)
            if (r["status"] == "OK"
                    and cfg["encode"].get("delete_frames_on_success", True)
                    and not args.keep_frames and not args.dry_run):
                shutil.rmtree(day_dir, ignore_errors=True)
                log.info("  %s %s: frames deleted", cam, day_dir.name)

        xfer = None if args.no_transfer else transfer(cfg, args.dry_run)

        # -- report ---------------------------------------------------------
        ok = [r for r in results if r["status"] == "OK"]
        failed = [r for r in results if r["status"] == "FAIL"]
        skipped = [r for r in results if r["status"] == "SKIP"]
        total_bytes = sum(r["size"] for r in results)
        elapsed = time.time() - run_start

        log.info("-" * 62)
        for r in results:
            log.info(" %-4s %-12s %-10s frames=%-6s %s",
                     r["status"], r["camera"], r["date"], r["frames"], r["note"])
        log.info("Done in %s | %d ok, %d skipped, %d failed",
                 human_duration(elapsed), len(ok), len(skipped), len(failed))

        try:
            free = shutil.disk_usage(frames_root).free
            free_txt = f"{human_size(free)} free on {frames_root}"
        except Exception:
            free_txt = "unknown"

        fields = [
            ("Videos", f"{len(ok)} created, {human_size(total_bytes)} total"),
            ("Encoder", encoder["name"]),
            ("Run time", human_duration(elapsed)),
            ("Disk", free_txt),
        ]
        if xfer is not None:
            fields.append(("Transfer",
                           ("OK - %d file(s) moved" % xfer["moved"]) if xfer["ok"]
                           else "FAILED - " + xfer["detail"]))
        if failed:
            fields.append(("\u26a0\ufe0f Failed", "\n".join(
                f"{r['camera']} {r['date']}: {r['note']}" for r in failed)))
        if skipped:
            fields.append(("Skipped", "\n".join(
                f"{r['camera']} {r['date']}: {r['note']}" for r in skipped)))

        all_good = not failed and (xfer is None or xfer["ok"])
        send_discord(
            cfg,
            ("\u2705 " if all_good else "\u26a0\ufe0f ") + "Timelapse Generation",
            build_summary(results, interval),
            0x2ECC71 if all_good else 0xF1C40F,
            fields)
        return 0 if all_good else 1

    except Exception as exc:
        log.exception("Critical failure")
        send_discord(cfg, "\u274c Timelapse - Critical Failure",
                     f"The run aborted before completing.\n```\n{exc}\n```",
                     0xE74C3C,
                     [("Host", platform.node()),
                      ("Run time", human_duration(time.time() - run_start))])
        return 2


if __name__ == "__main__":
    sys.exit(main())

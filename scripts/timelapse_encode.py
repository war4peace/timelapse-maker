#!/usr/bin/env python3
"""
timelapse_encode.py: nightly encode + notify + ship.

Finds every completed day directory under <frames_root>/<Camera>/, encodes
each to a 60 fps AV1 video, deletes the frames on success, sends a Discord
summary, then rsyncs the videos to the configured destination (moving them,
not copying) - a NAS share, another host, or any local path.

"Completed" means any date directory strictly older than today that does not
already carry an .encoded.json marker, so a missed run (host down, crash) is
picked up automatically on the next pass rather than silently leaving frames
behind, and a day whose frames were kept is not encoded a second time.

Usage:
    timelapse_encode.py [config.json] [--date YYYY-MM-DD] [--dry-run]
                        [--keep-frames] [--no-transfer] [--force]
"""

import argparse
import ipaddress
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

# Names, not the module: `platform` above is the stdlib one, and two things
# called platform in one file is a trap waiting for a reader in a hurry.
from timelapse_platform import CONFIG_PATH, STATE_DIR_DEFAULT

__version__ = "0.1.9"

log = logging.getLogger("encode")
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------------
# Credential redaction
#
# The canonical copy, here because this is the module the wizard and the
# pre-flight already reach into, and because two versions of a rule like this
# would eventually disagree. timelapse_capture.py carries a duplicate: it is a
# daemon and deliberately imports nothing from its siblings, so a test asserts
# the two patterns are identical rather than trusting anyone to keep them so.
#
# Reported from the real deployment 2026-08-11: a camera returned 502, requests
# raised, and the exception text carries the URL it was fetching. That URL is
# the Reolink shape, credentials in the query string, so the password went to
# journald and then onto the web UI's log page in full. The call site said
# nothing about a URL, which is the point below.
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


def url_host(raw):
    """An address as it must appear inside a URL.

    An IPv6 literal has to be bracketed, or the colons are read as the port
    separator. Both failure modes are misleading: requests raises
    `InvalidURL: Failed to parse`, and ffmpeg reports "Failed to resolve
    hostname fdd2", which sends an operator to look at DNS for a camera whose
    address they typed correctly. Measured against a real Hikvision over its
    ULA address, 2026-08-14: bracketed works on both, bare fails on both.

    Hostnames and IPv4 addresses pass through untouched, so this is safe to
    apply to whatever was typed. It lives here rather than in the wizard
    because the web UI needs the same rule for the addresses it prints, and
    two copies is two chances to emit a URL nothing can open.
    """
    host = raw.strip()
    if host.startswith("[") and host.endswith("]"):
        return host                        # already bracketed by the operator
    try:
        # Split the zone id off before validating: 3.9 is the floor, and a
        # scoped address is only accepted by ipaddress from 3.9 onwards.
        ipaddress.IPv6Address(host.split("%")[0])
    except ValueError:
        return host
    return "[%s]" % host


def is_ipv6(addr):
    """Is this an IPv6 literal? Brackets and a zone id are both tolerated.

    A hostname returns False even if it resolves to IPv6, which is correct for
    every caller here: they are deciding how to *format* an address, or which
    socket family to open for a literal the operator typed.
    """
    host = str(addr).strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.IPv6Address(host.split("%")[0])
    except ValueError:
        return False
    return True


def hostport(addr, port):
    """`host:port` for a URL or for display, with IPv6 bracketed.

    Every unbracketed `f"{bind}:{port}"` in this project was a latent bug: the
    startup log, two wizard summaries and the playlist origin all produced
    `http://::1:8787/`, which no client can open.
    """
    return "%s:%s" % (url_host(str(addr)), port)


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


# A config holds its secrets in two shapes, and each pass below misses the
# other one. `{"password": "hunter2"}` has no `=` in it, so the text rule walks
# straight past it; a Reolink `url` carries the credential inside a query
# string, so a rule that only knew field names walks past that. Hence both,
# over the whole tree.
#
# Matched against key names. Deliberately loose: an unrecognised key gets its
# value printed, so the cost of a missing name is a leak while the cost of a
# spurious match is a question on a bug report.
#
# `_hash` is part of the same rule and not an exception to it. A stored
# password hash is not a password, but it is offline-crackable, and the entire
# use for this dump is pasting it somewhere public. The `$` anchor on the
# original meant `password_hash` sailed straight through: measured, not
# assumed, when the web UI's login was designed. What survives is the key
# itself, so "did you set a password?" is still answerable from the dump.
SECRET_KEY_RE = re.compile(
    r"pass(word|wd)?(_?hash)?$|^pwd$|secret|token|credential|api[-_]?key",
    re.I)


def redact_config(node):
    """A copy of a parsed config with the credentials taken out.

    Structure is preserved exactly, because the whole use for this is handing
    it to somebody else to read: a dump that dropped keys would have people
    diagnosing a config that is not the one on the disk.

    Note what this does *not* remove. Camera hostnames, the transfer
    destination and the Discord webhook's numeric id all survive, because they
    are what a fault report is about, and `usernames` survive because the text
    rule has always kept `user=` while masking `password=`. Whoever runs this
    is told as much, rather than being left to assume it covered more.
    """
    if isinstance(node, dict):
        return {
            # An empty value stays empty: "" for a password is not a secret,
            # it is the answer to "did you actually set one?".
            k: (MASK if (SECRET_KEY_RE.search(str(k)) and v)
                else redact_config(v))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [redact_config(v) for v in node]
    if isinstance(node, str):
        return redact(node)
    return node


# ----------------------------------------------------------------------------
# The atomic write
#
# Write to a temporary name, then rename over the destination. Every state file
# and every captured frame in this project lands that way, so a reader never
# sees a half-written file and a process killed mid-write leaves a stray .tmp
# rather than a corrupt frame.
#
# The rename is the part that is not portable, and the difference is not a
# detail: POSIX rename() ignores open handles entirely, while Windows refuses
# while anything holds the destination. Duplicated in timelapse_capture.py, for
# the same reason load_config() and the redaction rule are, with a test pinning
# the copies together.
# ----------------------------------------------------------------------------

REPLACE_TRIES = 20
REPLACE_WAIT = 0.05


def replace_atomic(tmp, final):
    """os.replace(), retried briefly, for files a reader may hold open.

    POSIX rename() does not care who has the destination open, so this wins on
    the first pass on Linux and costs nothing. Windows raises PermissionError
    instead, because CPython's open() does not ask for FILE_SHARE_DELETE, and
    every file written this way here is written by a daemon and read by the
    web UI. A page load arriving in the same millisecond would otherwise lose
    a frame, a heartbeat or a cadence marker.

    Retried rather than branched on the platform, because a reader is gone in
    milliseconds and there is nothing to decide. A genuine permission problem
    still raises, a second later, carrying its own error, so every caller's
    existing OSError handling applies unchanged.
    """
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, final)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(REPLACE_WAIT)


# ----------------------------------------------------------------------------
# Setup
# ----------------------------------------------------------------------------

def setup_logging(log_dir):
    fmt = RedactingFormatter("%(asctime)s %(levelname)-7s %(message)s",
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

# Optional motion smoothing: how many neighbouring frames tmix averages into
# each output frame. Off unless a camera asks for it, so SMOOTH_DEFAULT is only
# what the wizard offers once you say yes, never what an unanswered config gets.
#
# The bounds are not arbitrary. Below 3 there is nothing to average. Above ~30
# a camera is averaging several minutes of a day and anything that crosses the
# frame is diluted past seeing, which is the opposite of what a timelapse is
# for. Measured on real footage at a 5s interval: at 3 frames a bin lorry stays
# legible; at 7 it is a wash. 15 suits a scene that is mostly foliage, where
# the judder comes from leaves moving and there is no small detail to lose.
SMOOTH_MIN = 3
SMOOTH_MAX = 30
SMOOTH_DEFAULT = 15

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


# Written into a day directory once its video exists. Until 0.1.6 nothing
# recorded that a day had been encoded: deleting the frames *was* the record,
# and it still is whenever `delete_frames_on_success` is left at its default,
# because the directory goes away and the next run cannot find it.
#
# Turn that off and the record goes with it. The directory stays, so the same
# day is found again the next night, and every night after, and re-encoded
# from scratch over a video that already exists.
#
# It cannot be inferred from the output file instead: transfer() *moves* the
# video to the NAS, so by morning video_output is normally empty and every day
# would look unencoded again.
ENCODED_FILE = ".encoded.json"


def day_encoded(day_dir):
    """What a day's marker says was produced, or None if it has none.

    None for a marker that is unreadable or malformed as well as for one that
    is absent. Unreadable has to mean "encode it again": the wasteful answer
    costs one night of GPU time and is self-correcting, where trusting a
    damaged marker loses the day silently and permanently.
    """
    try:
        with open(Path(day_dir) / ENCODED_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) and d.get("video") else None
    except (OSError, ValueError):
        return None


def mark_encoded(day_dir, out_file, result, encoder_name):
    """Record that this day has been encoded. Never raises.

    Never raises for the same reason write_cadence() does not: a marker that
    could not be written costs a re-encode next night, which is exactly the
    behaviour this project had before markers existed, and that is not worth
    turning a successful run into a failed one over.

    This says the day was *encoded*, not that it was delivered. A transfer
    that failed leaves the video in video_output and the next run ships it;
    re-encoding it would not have helped that.
    """
    day_dir = Path(day_dir)
    path = day_dir / ENCODED_FILE
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"encoded_at": datetime.now().replace(
                           microsecond=0).isoformat(),
                       "video": out_file.name,
                       "frames": result["frames"],
                       "size": result["size"],
                       "encoder": encoder_name,
                       "version": __version__}, fh)
        replace_atomic(tmp, path)
        return True
    except OSError as exc:
        log.warning("  %s: could not mark the day encoded (%s); it will be "
                    "encoded again next run", day_dir.name, exc)
        return False


# ----------------------------------------------------------------------------
# Runtime state
#
# systemd knows whether a process is alive. It cannot know whether the cameras
# are answering: a capture daemon whose every camera is refusing connections is
# `active (running)`, and looks perfect on the status page. These files are how
# the programs that *do* know say so.
#
# This is a second on-disk contract and it will outlive whatever reads it
# first, so it carries a version and every reader uses .get(key, default).
#
# A fixed location rather than anywhere derived from the config's other paths.
# The base directory the wizard asks about exists because frames are enormous
# and may want their own disk; a few KB of JSON does not, and /var/lib is where
# FHS puts exactly this. Deriving it from log_dir's parent was considered and
# is a trap: log_dir may be /var/log/timelapse, whose parent is /var/log.
# STATE_DIR_DEFAULT comes from timelapse_platform because /var/lib has no
# meaning on Windows, where Path("/var/lib/...") is not an error but a path on
# whichever drive happens to be current.
#
# NOT `web.state_dir`. That belongs to the web UI's index, which is disposable
# and is the one directory that service may write; this is written by the
# daemons and only read by the UI.
# ----------------------------------------------------------------------------

CAPTURE_STATE = "capture.json"
ENCODE_STATE = "encode.json"
STATE_VERSION = 1

# Runs kept in encode.json. Two weeks is enough to see a pattern and small
# enough that the file stays a few KB, which matters because the web UI reads
# the whole thing on every page view.
MAX_RUNS = 14


def state_dir(cfg):
    """Where the daemons publish runtime state.

    Absent from every config written before 0.1.6, so it is read with a
    default like every other added key. The directory must exist before the
    units start: it is named in ReadWritePaths, and systemd refuses to start a
    unit whose ReadWritePaths points at nothing. install.sh creates it.
    """
    return Path((cfg.get("paths", {}).get("state_dir") or "").strip()
                or STATE_DIR_DEFAULT)


def stamp(epoch):
    """Epoch to a local ISO string, or None. None means "never", not "now"."""
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch).replace(microsecond=0).isoformat()


def coverage_of(frames, interval, seconds):
    """Frames as a percentage of `seconds` captured at one every `interval`.

    The one place this arithmetic lives. The nightly summary measures a whole
    day (86400s) and the web UI measures the part of today that has happened
    so far, and they must not be able to drift apart, because an operator
    comparing "94% at 18:00" with "94% overnight" is entitled to assume the
    two words mean the same thing.
    """
    if not interval or seconds <= 0 or frames is None:
        return None
    expected = int(seconds / interval)
    if not expected:
        return None
    # Zero frames is 0%, not "unknown". A camera that captured nothing today
    # is the case an operator most needs stated, and "-" invites reading it as
    # "not measured".
    return round(100.0 * frames / expected, 1)


def coverage_pct(result, interval):
    """A finished day's coverage, against the cadence that day ran at.

    Using the *global* interval made a camera at one frame a minute read as 8%
    every night, which is a full day of frames reported as a near-outage.

    Keeps its own "no frames means no answer" rule rather than inheriting the
    live panel's 0%: a nightly row exists because a day was processed, so zero
    frames there means the day is not measurable, not that it scored nothing.
    """
    if not result.get("frames"):
        return None
    return coverage_of(result["frames"],
                       result.get("interval") or interval, 86400)


def run_record(started, encoder, results, xfer, interval, error=""):
    """One night's work, as facts. No verdict beyond what the run itself
    already decided by its exit code.

    `error` is set only by the critical-failure path, where the run aborted
    before it could finish. An empty string there and no days at all is a run
    that found nothing to do, which is an entirely different night.
    """
    finished = time.time()
    return {
        "started": stamp(started),
        "finished": stamp(finished),
        "seconds": round(finished - started, 1),
        "encoder": (encoder or {}).get("name", ""),
        "error": error,
        "ok": sum(1 for r in results if r["status"] == "OK"),
        "skipped": sum(1 for r in results if r["status"] == "SKIP"),
        "failed": sum(1 for r in results if r["status"] == "FAIL"),
        "bytes": sum(r["size"] for r in results),
        "transfer": None if xfer is None else {
            "ok": bool(xfer.get("ok")),
            "moved": xfer.get("moved", 0),
            "detail": xfer.get("detail", ""),
        },
        "days": [{
            "camera": r["camera"],
            "date": r["date"],
            "status": r["status"],
            "frames": r["frames"],
            "bad": r["bad"],
            "size": r["size"],
            "seconds": round(r["seconds"], 1),
            "interval": r.get("interval"),
            "coverage": coverage_pct(r, interval),
            "note": r["note"],
        } for r in results],
    }


def write_run_state(cfg, record):
    """Prepend one run to encode.json, keeping the newest MAX_RUNS. Never
    raises: a run that encoded seven days successfully has not failed because
    its history file could not be updated.

    An unreadable or malformed history starts a new one rather than aborting.
    The alternative loses tonight's record to protect a file that is already
    damaged, which is the wrong way round.
    """
    path = state_dir(cfg) / ENCODE_STATE
    runs = []
    try:
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        if isinstance(old, dict) and isinstance(old.get("runs"), list):
            runs = old["runs"]
    except (OSError, ValueError):
        runs = []

    now = time.time()
    payload = {
        "version": STATE_VERSION,
        "kind": "encode",
        "updated": stamp(now),
        "updated_epoch": int(now),
        "runs": ([record] + runs)[:MAX_RUNS],
    }
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        replace_atomic(tmp, path)
        return True
    except OSError as exc:
        log.warning("could not update %s: %s", path, exc)
        return False


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


def camera_smoothing(cam):
    """Frames to average for this camera, or 0 for none.

    Absence means OFF, which is deliberately not how `interval_seconds` and
    `framerate` read: those fall back to a global, so a camera left alone still
    moves when the global changes. There is no global here. Averaging calms
    wind in foliage, which is most of what makes a timelapse look like it is
    jumping, but it also thins out whatever crosses the frame in one or two
    frames. That trade is worth making on a roof and not at a gate, so it is
    answered per camera and the answer is no until someone says otherwise.

    Clamped rather than trusted: this is read straight off a file an operator
    may have edited, and a hand-typed 500 would have the encoder buffering 500
    frames of 4K per camera. A value under SMOOTH_MIN reads as off, which is
    what a leftover 0 or 1 means.
    """
    try:
        n = int(cam.get("smooth_frames", 0) or 0)
    except (TypeError, ValueError):
        # A string, a list, None: all mean the same thing here, which is that
        # nobody asked for smoothing in a way this can act on.
        return 0
    return 0 if n < SMOOTH_MIN else min(n, SMOOTH_MAX)


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

        # Last in the chain, so it averages the finished pixels rather than
        # racing the range conversion. tmix is nearly free (measured under 10%
        # of decode, against an encode that dominates either way) and does not
        # change the frame count, so the video's length and the coverage
        # arithmetic are untouched.
        smooth = camera_smoothing(cam)
        if smooth:
            vf += f",tmix=frames={smooth}"

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

        log.info("  %s %s: encoding %d frames (%dx%d, %d bad) at %sfps%s -> %s",
                 camera, day, len(frames), w, h, bad, fps,
                 f", smoothing {smooth}" if smooth else "", out_file.name)

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

        # After the file is closed and checked, and only on OK, so a day that
        # failed can never look finished. Written even when the frames are
        # about to be deleted (the usual case, where it is redundant and gone
        # a second later), because the caller's rmtree runs with
        # ignore_errors=True: a deletion that quietly failed would otherwise
        # leave an unmarked directory to be encoded all over again.
        mark_encoded(day_dir, out_file, result, encoder["name"])

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


# ----------------------------------------------------------------------------
# Does rsync actually work against this destination?
#
# Lives here because this is the program that runs rsync every night, and both
# the wizard and the pre-flight want the same answer. They ask by importing it,
# the way they already import the encoder probe and post_webhook().
#
# It is a measurement, deliberately. `-a` implies --owner --group, which a CIFS
# share often cannot set; rsync then exits 23 and the run reports a failure
# even though the files arrived. Whether that happens depends on the server and
# the mount options, and the pre-flight used to guess from the filesystem type
# alone. It guessed wrong on the author's own share, warning that a working
# configuration would fail every night.
# ----------------------------------------------------------------------------

RSYNC_CANDIDATES = (
    ["-a", "--partial"],
    ["-rt", "--partial"],
    ["-a", "--no-perms", "--no-owner", "--no-group", "--partial"],
)


def whoami():
    """The current account's name, or "" where that cannot be told."""
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:                           # noqa: BLE001
        return ""


def probe_as(svcuser):
    """How to run the probe as `svcuser`: (argv_prefix, why_not).

    A prefix of [] with no reason means "run it directly, that is already the
    right account". A reason means the probe cannot be run as that account at
    all, which is a fact about *this checker* and must never be reported as a
    fact about the share.

    Reported from a real 0.1.3 install, during `sudo timelapse update`. The
    installer deliberately runs the pre-flight as the service account
    (`as_service_user`, which is `runuser -u timelapse --`) so that permission
    problems surface then rather than at 00:05 tonight. This probe then called
    `runuser` *again* from inside that unprivileged process: a nested runuser,
    which answers "may not be used by non-root users". That was read as rsync's
    verdict on the share, and the operator was told to go and fix permissions
    that were already correct.

    The first branch is what that case needs. Being the service account
    already, there is nothing to switch to and the probe is authoritative.
    """
    if not svcuser or whoami() == svcuser:
        return [], ""
    if getattr(os, "geteuid", lambda: 0)() != 0:
        return None, (f"only root can run the probe as {svcuser}; "
                      f"try: sudo timelapse test")
    if not shutil.which("runuser"):
        return None, f"runuser is not installed, so it cannot test as {svcuser}"
    return ["runuser", "-u", svcuser, "--"], ""


def try_rsync_args(dest, args, svcuser=None):
    """Copy one small file to dest with `args`, as the account that runs it.

    Returns (ok, detail). `ok` is None for "could not be tested", with the
    reason in detail, and that is deliberately not False: a share that refuses
    the copy and a checker that could not attempt it need opposite responses
    from whoever is reading.
    """
    prefix, why_not = probe_as(svcuser)
    if prefix is None:
        return None, why_not
    if not shutil.which("rsync"):
        return None, "rsync is not installed here"
    try:
        tmpdir = tempfile.mkdtemp(prefix="tl-xfer-")
        os.chmod(tmpdir, 0o755)
        probe = Path(tmpdir) / ".tl-transfer-probe"
        probe.write_bytes(b"\0" * 4096)
        if svcuser:
            try:
                shutil.chown(tmpdir, user=svcuser)
                shutil.chown(probe, user=svcuser)
            except (OSError, LookupError):
                pass
    except OSError as exc:
        return None, f"no usable temp space for the probe file: {exc}"

    landed = Path(dest) / probe.name
    try:
        cmd = list(prefix) + ["rsync"] + list(args) + [
            str(probe), str(dest).rstrip("/") + "/"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except Exception as exc:                # noqa: BLE001
            return None, f"could not run rsync: {exc}"
        if r.returncode == 0:
            return True, ""
        detail = " ".join((r.stderr or "").split())[:200]
        return False, f"exit {r.returncode}{': ' + detail if detail else ''}"
    finally:
        # The configured args may include --remove-source-files, which takes
        # the probe with it; missing_ok covers both outcomes.
        landed.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def probe_rsync_flags(dest, svcuser=None):
    """First flag set that works, [] if none do, None if it could not be tested."""
    for args in RSYNC_CANDIDATES:
        ok, _ = try_rsync_args(dest, args, svcuser)
        if ok is None:
            return None
        if ok:
            return args
    return []


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
# Notifications
#
# One nightly summary, delivered to any number of sinks. The transport is the
# same POST for all of them; what differs is the payload shape and the limits.
#
# Every sink obeys two rules that are not negotiable. **A failed notification
# must never fail the run it is reporting on**, so each is wrapped
# individually and one sink being down cannot stop the next. And **an explicit
# User-Agent, always**: Discord sits behind Cloudflare, which answers urllib's
# default "Python-urllib/3.x" with a 403 before the request ever reaches the
# webhook. GitHub refuses one outright in `timelapse_update.py`. Assume any
# service will.
# ----------------------------------------------------------------------------

# The format Discord documents for API clients. Discord only; sending this to
# ntfy or Telegram would be claiming to be something we are not.
USER_AGENT = ("DiscordBot (https://github.com/war4peace/timelapse-maker, "
              f"{__version__})")

PROJECT_AGENT = f"timelapse-maker/{__version__} (+https://github.com/war4peace/timelapse-maker)"

# Severity, chosen at the call site, translated per sink. The call sites used
# to pass a Discord colour, which meant a Discord concept travelled through
# code that had no business knowing about Discord.
LEVEL_COLOR = {"ok": 0x2ECC71, "info": 0x95A5A6,
               "warn": 0xF1C40F, "error": 0xE74C3C}
# ntfy's 1-5 scale. A summary nobody needs to act on should not buzz a phone
# the same way a failed run does.
LEVEL_PRIORITY = {"ok": 3, "info": 2, "warn": 4, "error": 5}

DISCORD_DESC_LIMIT = 4000
DISCORD_FIELD_LIMIT = 1024
TELEGRAM_LIMIT = 4096
NTFY_LIMIT = 4000


def post_webhook(url, payload, timeout=20, headers=None, agent=USER_AGENT):
    """POST a JSON payload. Raises on transport or HTTP error.

    Kept exactly this callable because the wizard and the pre-flight both
    import it to send their test messages.
    """
    head = {"Content-Type": "application/json", "User-Agent": agent}
    head.update(headers or {})
    req = urlrequest.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers=head)
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def clip(text, limit):
    """Trim to a limit and say so, rather than slicing mid-sentence.

    Same lesson as the release notes at 0.1.0: text that stops without saying
    it stopped reads as this program having lost the rest.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit - 20].rstrip() + "\n... (truncated)"


def notify_sinks(cfg):
    """Every configured sink, newest config shape first.

    `notify` is a list of sinks and is authoritative when present. The legacy
    `discord` block is what every config written before 0.1.6 has, and it goes
    on working untouched: upgrades keep the existing config, so a key read any
    other way would break every install that has one.
    """
    sinks = cfg.get("notify")
    if isinstance(sinks, list) and sinks:
        if cfg.get("discord", {}).get("enabled"):
            # Quietly, and once: an operator who hand-edits the old block
            # after the wizard has written the new one deserves to find out
            # here rather than by wondering why nothing arrived.
            log.info("Using the 'notify' sinks; the legacy 'discord' block is "
                     "ignored while it is present.")
        return [s for s in sinks if isinstance(s, dict) and s.get("enabled")]

    legacy = cfg.get("discord", {})
    if legacy.get("enabled") and legacy.get("webhook_url"):
        return [dict(legacy, type="discord")]
    return []


def discord_payload(sink, title, description, level, fields):
    return {
        "username": sink.get("username", "Timelapse Bot"),
        "embeds": [{
            "title": title,
            "description": clip(description, DISCORD_DESC_LIMIT),
            "color": LEVEL_COLOR.get(level, LEVEL_COLOR["info"]),
            "fields": [{"name": n,
                        "value": clip(v or "-", DISCORD_FIELD_LIMIT),
                        "inline": False}
                       for n, v in fields][:25],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }


def plain_text(title, description, fields):
    """The summary as one block of text, for sinks with no embed concept."""
    parts = [title, ""]
    if description:
        parts.append(description)
    for name, value in fields:
        parts.append(f"\n{name}: {value or '-'}")
    return "\n".join(parts).strip()


def send_discord_sink(sink, title, description, level, fields):
    url = sink.get("webhook_url", "")
    if not url:
        raise ValueError("no webhook_url configured")
    post_webhook(url, discord_payload(sink, title, description, level, fields))


def send_ntfy(sink, title, description, level, fields):
    """ntfy.sh or a self-hosted ntfy server.

    JSON to the server root rather than text to /topic, deliberately: the
    other form carries the title in an HTTP *header*, headers are ASCII, and
    every title this program sends starts with an emoji.
    """
    server = (sink.get("server") or "https://ntfy.sh").rstrip("/")
    topic = (sink.get("topic") or "").strip().lstrip("/")
    if not topic:
        raise ValueError("no topic configured")
    body = {
        "topic": topic,
        "title": clip(title, 250),
        "message": clip(plain_text("", description, fields), NTFY_LIMIT),
        "priority": int(sink.get("priority")
                        or LEVEL_PRIORITY.get(level, 3)),
    }
    if sink.get("tags"):
        body["tags"] = [t.strip() for t in str(sink["tags"]).split(",")
                        if t.strip()]
    headers = {}
    token = (sink.get("token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    post_webhook(server, body, headers=headers, agent=PROJECT_AGENT)


def telegram_escape(text):
    """Telegram HTML mode understands exactly these three."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;") \
                       .replace(">", "&gt;")


def send_telegram(sink, title, description, level, fields):
    """Telegram Bot API sendMessage.

    HTML rather than MarkdownV2: the summary is a monospace table, and a
    proportional font turns it into rubble, so it has to be wrapped in
    something. MarkdownV2 would mean escaping fourteen characters correctly
    everywhere, including inside camera names, where getting it wrong is a 400
    rather than a wrong-looking message.
    """
    token = (sink.get("token") or "").strip()
    chat = str(sink.get("chat_id") or "").strip()
    if not token or not chat:
        raise ValueError("token and chat_id are both required")
    body = telegram_escape(plain_text("", description, fields))
    text = f"<b>{telegram_escape(title)}</b>\n<pre>{body}</pre>"
    post_webhook(
        f"https://api.telegram.org/bot{token}/sendMessage",
        {"chat_id": chat, "text": clip(text, TELEGRAM_LIMIT),
         "parse_mode": "HTML", "disable_web_page_preview": True},
        agent=PROJECT_AGENT)


SINKS = {
    "discord": send_discord_sink,
    "ntfy": send_ntfy,
    "telegram": send_telegram,
}


def notify(cfg, title, description, level, fields):
    """Deliver one summary to every configured sink.

    Returns (sent, failed) counts, for the caller that wants to log them; the
    run's own exit status never depends on either.
    """
    sinks = notify_sinks(cfg)
    if not sinks:
        log.info("No notification sinks configured; skipping.")
        return 0, 0

    sent = failed = 0
    for sink in sinks:
        kind = (sink.get("type") or "discord").lower()
        handler = SINKS.get(kind)
        if handler is None:
            log.warning("Unknown notification type %r; skipping it. "
                        "Known types: %s", kind, ", ".join(sorted(SINKS)))
            failed += 1
            continue
        try:
            handler(sink, title, description, level, fields)
            sent += 1
            log.info("Notified %s.", kind)
        except Exception as exc:
            # Deliberately broad, and deliberately per sink: a socket timeout
            # is not a URLError, one sink being down must not stop the next,
            # and none of it may take down the run being reported on.
            failed += 1
            log.warning("%s notification failed: %s", kind, exc)
    return sent, failed


# ----------------------------------------------------------------------------
# Credential watch
#
# The capture daemon publishes what it knows and never opens a socket. This
# reads that file and does the talking, which keeps the thing that knows and
# the thing that sends in separate processes: exactly what the state file
# introduced in 0.1.6 was for. Run from a timer, every few minutes.
#
# It notifies once per incident and once when the incident ends. The incident's
# identity is the `since` timestamp the daemon publishes, so "the same failure"
# needs no definition of its own here.
# ----------------------------------------------------------------------------

WATCH_STATE = "notified.json"

# A heartbeat older than this describes a moment that has passed. Neither an
# alarm nor an all-clear may be sent from it: a stopped daemon is not a camera
# that recovered, and a file from this morning is not evidence about now.
CAPTURE_STALE = 300


def read_capture_state(cfg):
    """capture.json if it is fresh and the daemon is running, else None."""
    path = state_dir(cfg) / CAPTURE_STATE
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError) as exc:
        log.debug("no usable capture state at %s (%s)", path, exc)
        return None
    if not isinstance(state, dict) or not state.get("running"):
        return None
    age = time.time() - (state.get("updated_epoch") or 0)
    if age > CAPTURE_STALE:
        log.debug("capture state is %.0fs old; too old to act on", age)
        return None
    return state


def load_notified(cfg):
    """Which camera incidents have already been reported. {name: since}."""
    try:
        with open(state_dir(cfg) / WATCH_STATE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("incidents", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_notified(cfg, incidents):
    path = state_dir(cfg) / WATCH_STATE
    try:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": STATE_VERSION, "incidents": incidents}, fh)
        replace_atomic(tmp, path)
        return True
    except OSError as exc:
        # Losing this file means the next run repeats a notification, which is
        # a great deal better than the run failing.
        log.warning("cannot record what was notified (%s); a repeat is "
                    "possible on the next check", exc)
        return False


def refusal_fields(cam, err):
    return [
        ("Camera", cam.get("name", "?")),
        ("Since", err.get("since") or "?"),
        ("Camera said", err.get("detail") or "-"),
        ("Next attempt", err.get("quiet_until") or "-"),
    ]


def watch_credentials(cfg):
    """Notify about cameras that are refusing our credentials. Returns a count.

    Only `confirmed` refusals: the daemon reaches that verdict after two
    refusals, ten minutes of silence and one more attempt, so anything short of
    it is still a camera that might merely have been rebooting.
    """
    if not cfg.get("capture", {}).get("notify_auth_failures", True):
        return 0
    # Before anything is recorded, not after. Marking an incident as reported
    # when there was nowhere to report it would mean that configuring a sink
    # tomorrow leaves today's refusal permanently unannounced.
    if not notify_sinks(cfg):
        return 0
    state = read_capture_state(cfg)
    if state is None:
        return 0

    known = load_notified(cfg)
    changed = 0
    for cam in state.get("cameras", []):
        name = cam.get("name")
        if not name:
            continue
        err = cam.get("error") or {}
        refusing = err.get("class") == "auth" and err.get("confirmed")
        since = err.get("since") if refusing else None

        if refusing and known.get(name) != since:
            # The observation, never the diagnosis. A camera that has locked
            # the account refuses a correct password too, so "your password is
            # wrong" would be false exactly when it is least welcome.
            sent, _failed = notify(
                cfg, "⚠️ Timelapse - camera refused our credentials",
                f"{name} has been rejecting our credentials since "
                f"{err.get('since')}. Capture from it is paused apart from "
                f"an occasional retry, so this program is not holding the "
                f"camera's account locked. Other cameras are unaffected.",
                "error", refusal_fields(cam, err))
            # Nothing delivered is not "already told them". The next tick is
            # minutes away, and a sink that was briefly unreachable must not
            # swallow the single message this whole feature exists to send.
            if not sent:
                continue
            known[name] = since
            changed += 1
        elif not refusing and known.get(name):
            sent, _failed = notify(
                cfg, "✅ Timelapse - camera credentials accepted again",
                f"{name} is answering again; normal capture has resumed.",
                "ok", [("Camera", name)])
            if not sent:
                continue
            known.pop(name, None)
            changed += 1

    if changed:
        save_notified(cfg, known)
    return changed


def send_discord(cfg, title, description, color, fields):
    """Backwards-compatible shim: colour in, level out.

    Nothing in this project calls it any more. It stays because it was the
    only notification entry point for six releases, and a fork or a local
    patch may still use it.
    """
    level = next((k for k, v in LEVEL_COLOR.items() if v == color), "info")
    return notify(cfg, title, description, level, fields)


NAME_COL = 12
SUMMARY_HEADS = ("Camera", "St", "Frames", "Cov%", "Size", "Time")
SUMMARY_ALIGN = ("<", "<", ">", ">", ">", ">")


def pad_row(values, widths):
    """One line of the table. Trailing blanks trimmed; they only cost width."""
    return " ".join(
        v.ljust(w) if a == "<" else v.rjust(w)
        for v, a, w in zip(values, SUMMARY_ALIGN, widths)).rstrip()


def build_summary(results, interval):
    """The nightly table, as a Discord code block.

    Discord renders an embed's description in a column narrower than an
    ordinary message and wraps whatever overflows, so a fixed 62-column table
    put its last field on a second line underneath the first. Two things keep
    it narrow: the widths come from the content, and the date is a heading
    rather than a column repeating one value on every row. A run normally
    encodes a single day; catch-up runs after an outage get a block each.
    """
    if not results:
        return "Nothing to report."
    blocks = []
    for date in sorted({r["date"] for r in results}):
        rows = []
        for r in results:
            if r["date"] != date:
                continue
            # Against the cadence this camera ran at, via the same helper the
            # run record uses, so the table and the file cannot disagree.
            cov = coverage_pct(r, interval)
            rows.append((
                str(r["camera"])[:NAME_COL],
                r["status"],
                str(r["frames"] or "-"),
                f"{cov:.0f}" if cov is not None else "-",
                human_size(r["size"]) if r["size"] else "-",
                human_duration(r["seconds"])))
        widths = [max([len(h)] + [len(row[i]) for row in rows])
                  for i, h in enumerate(SUMMARY_HEADS)]
        # Not len(header): the row builder strips trailing blanks, so a short
        # value in the last column would shorten the rule under it.
        rule = "-" * (sum(widths) + len(widths) - 1)
        blocks.append("\n".join(
            [date, pad_row(SUMMARY_HEADS, widths), rule]
            + [pad_row(row, widths) for row in rows]))
    return "```\n" + "\n\n".join(blocks) + "\n```"


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


def find_pending(frames_root, cameras, only_date, max_backlog, force=False):
    """(jobs, done): date dirs older than today that still need encoding.

    Oldest first, capped at max_backlog, which is a count of the newest
    distinct dates still pending rather than an age cutoff.

    `done` counts the camera-days left out because they already carry a marker.
    They are dropped *before* the cap, so a pile of finished days cannot push a
    pending one out of the window; that is the whole point of applying the two
    in this order.

    An explicit --date overrides a marker, because re-encoding one day by hand
    has to stay possible; --force overrides it for the whole backlog.
    """
    today = date.today().isoformat()
    jobs, done = [], 0
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
                if not force and day_encoded(d):
                    done += 1
                else:
                    jobs.append((cam, d))
    if not only_date and max_backlog:
        keep = sorted({d.name for _, d in jobs})[-max_backlog:]
        jobs = [(c, d) for c, d in jobs if d.name in keep]
    return sorted(jobs, key=lambda j: (j[1].name, j[0])), done


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("config", nargs="?", default=CONFIG_PATH)
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--date", help="process only this YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--no-transfer", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="re-encode days that are already marked encoded")
    ap.add_argument("--watch", action="store_true",
                    help="check capture state for cameras refusing our "
                         "credentials, notify, and exit (run from a timer)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    # The watch runs from a timer every few minutes and logs to the journal
    # only. Two reasons, and the first one is fatal rather than aesthetic: its
    # unit may write exactly one directory, the state directory, so opening
    # encode.log under ProtectSystem=strict kills the process at startup with
    # a read-only filesystem error. Verified on real systemd. The second is
    # that 288 heartbeat entries a day do not belong in the encoder's log.
    setup_logging(None if args.watch else cfg["paths"].get("log_dir"))

    if args.watch:
        # Deliberately silent when there is nothing to say: this runs every few
        # minutes, and a line per run would bury the nightly encode in its own
        # log within a week.
        changed = watch_credentials(cfg)
        if changed:
            log.info("Credential watch: %d camera state change(s) reported.",
                     changed)
        return 0

    run_start = time.time()
    frames_root = Path(cfg["paths"]["frames_root"])
    out_dir = Path(cfg["paths"]["video_output"])
    out_dir.mkdir(parents=True, exist_ok=True)
    interval = cfg["capture"]["interval_seconds"]
    cameras = [c["name"] for c in cfg["cameras"] if c.get("enabled", True)]

    log.info("=" * 62)
    log.info(" Timelapse encode run - %s", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 62)

    # Bound before the try so the critical-failure path can still record what
    # it got through. An aborted run is the single most useful thing a status
    # page can show, and it is the one a crash would otherwise take with it.
    encoder, results, xfer = None, [], None

    try:
        log.info("Encoder detection:")
        encoder = select_encoder(cfg["paths"]["ffmpeg"], cfg["encode"])
        if encoder is None:
            raise RuntimeError("No usable encoder found "
                               "(tried av1_nvenc, hevc_nvenc, libx264)")
        log.info("Selected: %s", encoder["name"])

        jobs, done = find_pending(frames_root, cameras, args.date,
                                  cfg["encode"].get("max_backlog_days", 7),
                                  args.force)
        if done:
            log.info("Skipping %d day(s) already encoded; --force re-does them.",
                     done)
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
            # Recorded even though nothing was encoded. "The timer fired at
            # 00:05 and there was nothing to do" is an answer; a status page
            # that cannot tell that from "the timer never fired" is not.
            write_run_state(cfg, run_record(run_start, encoder, [], xfer,
                                            interval))
            # "None found" and "all of them are already done" look identical
            # from here and are very different things to read at breakfast,
            # so say which one it was.
            notify(cfg,
                   "Timelapse - nothing to do" if good
                   else "⚠️ Timelapse - transfer failed",
                   (f"{done} completed day folder(s) were already "
                    f"encoded; nothing new." if done else
                    "No completed day folders were found."),
                   "info" if good else "warn", fields)
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
        # Before the notification, deliberately: Discord is the part that can
        # be disabled, unreachable or rate-limited, and the local record of
        # what happened should not depend on any of that.
        write_run_state(cfg, run_record(run_start, encoder, results, xfer,
                                        interval))
        notify(
            cfg,
            ("\u2705 " if all_good else "\u26a0\ufe0f ") + "Timelapse Generation",
            build_summary(results, interval),
            "ok" if all_good else "warn",
            fields)
        return 0 if all_good else 1

    except Exception as exc:
        log.exception("Critical failure")
        write_run_state(cfg, run_record(run_start, encoder, results, xfer,
                                        interval, error=str(exc)[:300]))
        notify(cfg, "\u274c Timelapse - Critical Failure",
                     f"The run aborted before completing.\n```\n{exc}\n```",
                     "error",
                     [("Host", platform.node()),
                      ("Run time", human_duration(time.time() - run_start))])
        return 2


if __name__ == "__main__":
    sys.exit(main())

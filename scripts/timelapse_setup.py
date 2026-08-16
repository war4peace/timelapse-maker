#!/usr/bin/env python3
"""
timelapse_setup.py: interactive configuration wizard.

Scans the machine's filesystems, proposes where frames and videos should live,
then walks through capture settings, cameras, transfer and notifications, and
writes a complete config.json.

Every question has a default in [brackets]; pressing Enter accepts it.

    timelapse_setup.py [--output /etc/timelapse/config.json]
                       [--defaults]        answer everything with the default
                       [--print-paths CFG] list writable paths for ReadWritePaths
"""

import argparse
import errno
import json
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

# The wizard is the script that touches the machine most, so it is the one that
# imports the most of this. Names rather than the module: several of them read
# as ordinary constants at their call sites, which is the point.
from timelapse_platform import (
    CAPTURE_UNIT, CONFIG_DIR, CONFIG_PATH, DATA_ROOT_DEFAULT, ENCODE_UNIT,
    FFMPEG_URL, IS_WINDOWS, LINUX_STATE_DIR, LINUX_WEB_STATE_DIR,
    SERVICE_STATES, STATE_DIR_DEFAULT, WATCH_UNIT, WEB_STATE_DIR_DEFAULT,
    daily_trigger, disconnect_share, drive_is_local, elevation_hint, find_tool,
    install_service, install_task, is_elevated, is_reserved_name, is_scheduled,
    is_unc, log_hint, native_name, network_path, os_disk_mount, remove_service,
    remove_task, repeating_trigger, resolve_tool, restart_hint,
    restart_service, same_file_name, scan_filesystems, secure_secret_file,
    service_is_active, service_state, share_root, start_hint, stop_hint,
    stop_service, task_exists, task_info, task_result, task_xml, use_colour,
)

__version__ = "0.1.9"

# ----------------------------------------------------------------------------
# Terminal helpers
#
# The installer may be run as `curl ... | sudo bash`, in which case stdin is the
# pipe, not the keyboard. Read from /dev/tty so prompts still work; fall back to
# defaults-only if there is no terminal at all.
# ----------------------------------------------------------------------------

_TTY = None
AUTO = False


def init_tty(force_defaults=False, use_stdin=False):
    """Pick an input source.

    A terminal is preferred. When stdin is a pipe we must NOT read it: under
    `curl ... | bash` that pipe is the installer script itself, and consuming it
    would both corrupt the install and feed shell source into the prompts.
    /dev/tty reaches the real keyboard in that case. --stdin opts back in for
    scripted runs that genuinely want to pipe answers.
    """
    global _TTY, AUTO
    AUTO = force_defaults
    if AUTO:
        return
    if use_stdin:
        _TTY = sys.stdin
        return
    if sys.stdin.isatty():
        _TTY = sys.stdin
        return
    try:
        _TTY = open("/dev/tty", "r")
    except OSError:
        AUTO = True
        note("No terminal available - accepting all defaults.")


def _survive_narrow_stdout():
    """Never let the wizard die formatting its own output.

    The headings and banner use box-drawing characters. Python gives UTF-8 for
    effectively every locale (PEP 538 coerces even LC_ALL=C), but an explicit
    PYTHONIOENCODING=ascii makes printing one raise UnicodeEncodeError and
    abort the run half-configured. Degrading a character to '?' is a better
    outcome than that.
    """
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    if enc.replace("-", "") == "utf8":
        return
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass


_survive_narrow_stdout()

# Asked of the platform, because "is this a terminal" is not the whole question
# on Windows: a console there understands escapes only once a mode bit is set,
# and conhost leaves it off. See use_colour().
_COLOR = use_colour()


def c(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(t):    return c("1", t)
def dim(t):     return c("2", t)
def green(t):   return c("32", t)
def yellow(t):  return c("33", t)
def red(t):     return c("31", t)
def cyan(t):    return c("36", t)


def heading(text):
    print(f"\n{bold(cyan('── ' + text + ' ' + '─' * max(0, 58 - len(text))))}")


def note(text):  print(f"  {dim(text)}")
def good(text):  print(f"  {green('OK')}    {text}")
def warn(text):  print(f"  {yellow('WARN')}  {text}")
def fail(text):  print(f"  {red('FAIL')}  {text}")


def ask(question, default=""):
    """Prompt once. A blank line always accepts the default.

    Blank input must return the default even when the default is the empty
    string. An earlier version only short-circuited on a non-empty default and
    otherwise looped, so pressing Enter at any yes/no prompt - which passes an
    empty default - re-prompted forever. That contradicted the one promise the
    wizard makes, that Enter accepts what is in brackets.
    """
    suffix = f" [{default}]" if default != "" else ""
    if AUTO or _TTY is None:
        print(f"  {question}{suffix}: {dim('(default)')}")
        return default
    try:
        sys.stdout.write(f"  {question}{suffix}: ")
        sys.stdout.flush()
        line = _TTY.readline()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
    if line == "":                          # EOF
        print()
        return default
    if not _TTY.isatty():
        print()                             # piped input echoes no newline
    return line.strip() or default


def ask_secret(question):
    """Read a password without echoing it, when there is a real terminal.

    Camera passwords end up in scroll-back and in any transcript of the
    install otherwise.
    """
    if AUTO or _TTY is None:
        return ""
    if not _TTY.isatty():
        return ask(question, "")
    import getpass
    try:
        return getpass.getpass(f"  {question}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    except Exception:
        return ask(question, "")


def ask_yes(question, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        a = ask(f"{question} ({d})", "").strip().lower()
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False


def ask_int(question, default, lo=None, hi=None):
    while True:
        raw = ask(question, str(default))
        try:
            v = int(raw)
        except ValueError:
            fail(f"'{raw}' is not a number.")
            continue
        if lo is not None and v < lo:
            fail(f"Must be at least {lo}.")
            continue
        if hi is not None and v > hi:
            fail(f"Must be at most {hi}.")
            continue
        return v


def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


# ----------------------------------------------------------------------------
# Storage discovery
#
# scan_filesystems() itself lives in timelapse_platform: /proc/mounts is the
# Linux answer to "which disks could hold frames" and Windows needs a different
# one. What stays here is what to do with the answer, which is the same
# question on either platform.
# ----------------------------------------------------------------------------

def is_os_disk(disk):
    """The one not to fill. "/" on Linux, %SystemDrive% on Windows.

    Asked of the platform rather than spelled as a literal, because C: is only
    usually right and a wizard that recommends the boot drive on the one
    machine where it is wrong has recommended filling it.
    """
    return same_file_name(disk["mount"], os_disk_mount())


def recommend(disks):
    """Prefer the roomiest filesystem that is not the OS disk."""
    if not disks:
        return None
    others = [d for d in disks if not is_os_disk(d)]
    if others and others[0]["free"] >= 20 * 1024 ** 3:
        return others[0]
    return disks[0]


def show_disks(disks, best):
    print()
    label = "Drive" if os_disk_mount() != "/" else "Mount"
    print("   " + bold(f"{'#':<3}{label:<22}{'Type':<8}{'Free':>10}"
                       f"{'Total':>11}   Notes"))
    for i, d in enumerate(disks, 1):
        notes = []
        if d["rotational"] is True:
            notes.append("HDD")
        elif d["rotational"] is False:
            notes.append("SSD")
        if is_os_disk(d):
            notes.append("OS disk")
        mark = green(" <- recommended") if d is best else ""
        print(f"   {i:<3}{d['mount']:<22}{d['fstype']:<8}"
              f"{human(d['free']):>10}{human(d['total']):>11}   "
              f"{', '.join(notes):<12}{mark}")
    print()


# ----------------------------------------------------------------------------
# Wizard sections
# ----------------------------------------------------------------------------

def choose_storage(cfg):
    heading("Storage")
    note("Scanning filesystems...")
    disks = scan_filesystems()

    if not disks:
        warn("Could not detect any local filesystems automatically.")
        base = ask("Base directory for timelapse data", DATA_ROOT_DEFAULT)
        chosen = None
    else:
        best = recommend(disks)
        show_disks(disks, best)
        default_idx = str(disks.index(best) + 1)
        note("Frames are written continuously and deleted after encoding.")
        note("A spinning disk is fine - the access pattern is sequential.")
        idx = ask_int("Which filesystem should hold the frames?",
                      default_idx, 1, len(disks))
        chosen = disks[idx - 1]
        suggested = (DATA_ROOT_DEFAULT if is_os_disk(chosen)
                     else str(Path(chosen["mount"]) / "timelapse"))
        base = ask("Base directory", suggested)

    base = str(Path(base).expanduser())
    cfg["paths"]["frames_root"] = str(Path(base) / "frames")
    cfg["paths"]["video_output"] = str(Path(base) / "videos")
    cfg["paths"]["log_dir"] = str(Path(base) / "logs")
    print()
    for label in ("frames", "videos", "logs"):
        note(f"{label:<7} -> {Path(base) / label}")
    return chosen


def find_binary(name, fallback):
    """Where this machine already has `name`, or the given fallback.

    The fallback is what makes the two platforms differ, and it is not a
    cosmetic difference: on Linux the installer has already put ffmpeg at
    /usr/bin/ffmpeg by the time this runs, so a default that is wrong is
    merely unhelpful. On Windows nothing installed it (item 11c.6a), so a
    made-up default would be a path that does not exist offered as though it
    did, and the operator would accept it and find out at the first encode.
    Empty means "no default", and ask() then insists on an answer.
    """
    return find_tool(name) or fallback


def choose_tools(cfg):
    heading("ffmpeg")
    ffmpeg_default = find_binary("ffmpeg", "" if IS_WINDOWS
                                 else "/usr/bin/ffmpeg")
    if IS_WINDOWS:
        note("A folder is fine: give the one holding ffmpeg.exe and")
        note("ffprobe.exe and both will be taken from it.")
        if not ffmpeg_default:
            warn("No ffmpeg found on this machine.")
            note(f"Builds for Windows: {FFMPEG_URL}")
            note("Get one with NVENC if this box has an NVIDIA card, or the")
            note("encoder probe below will report the slow CPU fallback and")
            note("nothing will say why.")

    answer = ask("Path to ffmpeg (or its folder)" if IS_WINDOWS
                 else "Path to ffmpeg", ffmpeg_default)
    cfg["paths"]["ffmpeg"] = resolve_tool(answer, "ffmpeg")

    # Derived from the same answer rather than asked again. Given a folder they
    # came from one place by construction; given a file, the sibling beside it
    # is a far better guess than PATH, because a machine can easily have two
    # ffmpeg builds and taking one binary from each is the kind of mismatch
    # that produces an unreadable error much later.
    beside = sibling_tool(cfg["paths"]["ffmpeg"], "ffprobe")
    ffprobe_default = beside or find_binary("ffprobe",
                                            "" if IS_WINDOWS
                                            else "/usr/bin/ffprobe")
    if beside:
        cfg["paths"]["ffprobe"] = beside
        note(f"ffprobe -> {beside}")
    else:
        cfg["paths"]["ffprobe"] = resolve_tool(
            ask("Path to ffprobe", ffprobe_default), "ffprobe")

    chosen, failures, problem = detect_encoders(cfg["paths"]["ffmpeg"])

    if problem:
        fail(problem[0].upper() + problem[1:])
    if chosen is None:
        fail("No usable encoder at all - ffmpeg cannot encode here.")
        if IS_WINDOWS:
            note("Without a working ffmpeg there is no product: capture will")
            note("collect frames and nothing will ever turn them into a video.")
            note(f"Builds for Windows: {FFMPEG_URL}")
            note("Re-run 'timelapse setup' once it is installed.")
    elif chosen == "av1_nvenc":
        good("av1_nvenc available - AV1 hardware encoding will be used.")
    elif chosen == "hevc_nvenc":
        good("hevc_nvenc available - HEVC hardware encoding will be used.")
    else:
        warn("No NVENC encoder found; falling back to libx264 on the CPU.")
        note("A nightly run will be slower but the output is fine.")

    # Report why each better encoder was skipped, using ffmpeg's own words
    # rather than a guess. Claiming "your GPU is too old" when the real cause
    # is the ffmpeg build sends people down entirely the wrong path.
    for codec, message, hint in failures:
        print()
        warn(f"{codec} unavailable")
        if hint:
            note(hint)
        if message:
            note(f"ffmpeg said: {message[:150]}")


def sibling_tool(ffmpeg, name):
    """The companion binary next to the one just chosen, or "" if not there.

    ffprobe ships in the same folder as ffmpeg in every build of either, so the
    neighbour is a better answer than PATH: a machine with two ffmpeg builds
    would otherwise get ffmpeg from one and ffprobe from the other, and the two
    disagreeing shows up as an error about a stream much later.
    """
    path = str(ffmpeg).strip()
    if not path:
        return ""
    folder = os.path.dirname(path)
    if not folder:
        return ""
    candidate = resolve_tool(folder, name)
    return candidate if os.path.exists(candidate) else ""


def detect_encoders(ffmpeg):
    """(chosen codec or None, [(codec, ffmpeg message, hint), ...], problem).

    `problem` is "" when ffmpeg ran, and a reason when it could not be asked at
    all. It exists because the first version printed that reason itself and
    returned `(None, [])`, which collided with two other outcomes: an import
    failure, and ffmpeg running fine while every encoder was refused. Three
    states behind one falsy answer, distinguished only by something the caller
    could not see, so the GUI could not report it and neither could anything
    else that does not own a terminal. Fifth instance of that shape in this
    project, after try_rsync_args, service_state,
    sync_unit_readwritepaths and the update checker's cached failure.

    Deciding, never printing: 11c.6b's rule for anything a GUI has to reuse.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from timelapse_encode import (encoder_hint, list_encoders,
                                      probe_encoder_detail)
    except ImportError as exc:
        return None, [], f"could not load the encoder probe: {exc}"

    built = list_encoders(ffmpeg)
    if built is None:
        return None, [], f"could not run {ffmpeg} - is the path right?"

    failures = []
    for codec in ("av1_nvenc", "hevc_nvenc", "libx264"):
        ok, message = probe_encoder_detail(
            ffmpeg, {"codec": codec, "args": ["-c:v", codec]})
        if ok:
            return codec, failures, ""
        failures.append((codec, message,
                         encoder_hint(codec, message, codec in built)))
    return None, failures, ""


def choose_capture(cfg, disk):
    heading("Capture")
    interval = ask_int("Seconds between snapshots", 5, 1, 3600)
    cfg["capture"]["interval_seconds"] = interval

    timeout = min(cfg["capture"]["timeout_seconds"], max(1, interval - 1))
    if timeout != cfg["capture"]["timeout_seconds"]:
        note(f"Fetch timeout lowered to {timeout}s to stay under the interval.")
    cfg["capture"]["timeout_seconds"] = timeout

    per_day = int(86400 / interval)
    choose_framerate(cfg, per_day)

    n_cams = ask_int("Roughly how many cameras will you run?", 4, 1, 64)
    estimate_budget(cfg, disk, n_cams, per_day)

    default_guard = 60
    if disk:
        default_guard = max(10, min(60, int(disk["free"] / 1024 ** 3 * 0.1)))
    print()
    note("Capture pauses instead of filling the disk when free space is low.")
    cfg["capture"]["min_free_gb"] = ask_int(
        "Pause capture below how many GB free? (0 disables)", default_guard, 0)
    return n_cams


# Two seconds' worth of frames. The default config has shipped gop 120 against
# framerate 60 since the beginning; deriving it keeps that relationship true at
# any frame rate instead of leaving a keyframe every four seconds at 30fps.
GOP_SECONDS = 2


def video_length(per_day, fps):
    """m:ss of finished video for a full day's frames at this rate."""
    secs = per_day / max(1, fps)
    return f"{int(secs // 60)}:{int(secs % 60):02d}"


def choose_framerate(cfg, per_day):
    """The one encode setting worth asking about.

    It is the whole shape of the result: the same day's frames are five
    minutes of video at 60fps and ten at 30. Everything else under `encode`
    (container, gop, crf, min_frames) is a sane default that almost nobody
    needs to move, and moving them is what `timelapse config` is for.
    """
    enc = cfg.setdefault("encode", {})
    current = int(enc.get("framerate", 60))
    note(f"{per_day:,} frames/camera/day. The frame rate decides how long "
         f"that is:")
    for fps in (24, 30, 60):
        note(f"  {fps:>3} fps -> {video_length(per_day, fps)} "
             f"{'(smoother, and what most players expect)' if fps == 60 else ''}"
             .rstrip())
    fps = ask_int("Video frame rate (fps)", current, 1, 240)
    enc["framerate"] = fps
    # Derived, not asked: it is a codec detail, and asking about keyframe
    # intervals is not a question an operator should have to have an opinion
    # about. Anyone who does can set it with `timelapse config`.
    enc["gop"] = fps * GOP_SECONDS
    note(f"A day is {video_length(per_day, fps)} of video at {fps}fps, "
         f"keyframe every {GOP_SECONDS}s.")


AVG_SNAPSHOT_KB = 600          # a 1440p JPEG; refined later by timelapse_test.py


def estimate_budget(cfg, disk, n_cams, per_day):
    daily = AVG_SNAPSHOT_KB * 1024 * per_day * n_cams
    resident = daily * 2.2       # yesterday + today + margin
    print()
    note(f"Estimated at ~{AVG_SNAPSHOT_KB} KB/snapshot "
         f"(a full-resolution 1440p JPEG):")
    note(f"  {human(daily)}/day, about {human(resident)} resident on disk")
    if not disk:
        return
    free = disk["free"]
    if free < resident:
        fail(f"{human(free)} free is not enough for {human(resident)}.")
        note("Use a larger disk, fewer cameras, or a longer interval.")
    elif free < resident * 1.5:
        warn(f"Tight: {human(free)} free vs ~{human(resident)} needed.")
    else:
        good(f"{human(free)} free - comfortable headroom.")
    note("timelapse_test.py will replace this estimate with real measurements.")


# ----------------------------------------------------------------------------
# Camera discovery (WS-Discovery)
#
# A UDP multicast Probe that ONVIF devices answer with their device service
# address and a set of scopes. Stdlib sockets and ElementTree; no SOAP stack,
# no WSDL, no dependency, which is why this survived the decision against an
# ONVIF client library (docs/decided-against.md).
#
# It lives in the wizard on purpose. The wizard runs as root and outside a
# unit; the daemons run under ProtectSystem=strict with RestrictAddressFamilies
# set, where a multicast bind is exactly the kind of thing that works in
# development and fails in production. The web UI's bind probe is here for the
# same reason.
#
# Everything below was measured against eight cameras from four vendors on
# 2026-08-14 with temp/ws_discovery.py; architecture.md §4.4a records what that
# run settled and what it disproved.
# ----------------------------------------------------------------------------

WSD_GROUP = "239.255.255.250"
WSD_PORT = 3702
WSD_WINDOW = 3.0                   # seconds to collect; there is no end marker
WSD_REPEAT = 2                     # UDP is lossy, and a lost probe is a camera
WSD_NS = "http://www.onvif.org/ver10/network/wsdl"

# The typed Probe, and only the typed Probe. An untyped one found no camera
# this one missed and pulled in a printer and two Windows PCs, because Windows
# WSD shares this group and port. It is not symmetric either: two of the eight
# cameras answer the typed probe alone, so sending only an untyped probe would
# have missed them.
WSD_TYPES = "dn:NetworkVideoTransmitter"


def wsd_probe():
    """One Probe datagram. Each carries a fresh MessageID, since some firmware
    suppresses a duplicate it has already answered."""
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope'
            ' xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
            ' xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
            ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
            '<s:Header>'
            f'<a:MessageID>uuid:{uuid.uuid4()}</a:MessageID>'
            '<a:To s:mustUnderstand="1">'
            'urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>'
            '<a:Action s:mustUnderstand="1">'
            'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>'
            '</s:Header><s:Body>'
            f'<d:Probe><d:Types xmlns:dn="{WSD_NS}">{WSD_TYPES}'
            '</d:Types></d:Probe>'
            '</s:Body></s:Envelope>').encode("utf-8")


def wsd_source_addresses():
    """Local IPv4 addresses to probe from.

    Sending from 0.0.0.0 lets the routing table pick one interface, and a
    recorder running Docker, a VM bridge or a VPN has several. Probing from
    each in turn is what finds cameras when the default route points elsewhere.
    Reuses host_addresses(), which is already this file's answer to "what is
    on this machine"; "" is the last resort and means INADDR_ANY.
    """
    addrs = [a for a in host_addresses()
             if ":" not in a and not a.startswith("127.")]
    lan = lan_address()
    if lan and lan not in addrs:
        addrs.append(lan)
    return addrs or [""]


def wsd_open_sockets(addrs):
    socks = []
    for addr in addrs:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((addr, 0))
            if addr:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                             socket.inet_aton(addr))
            # 2, not 1: a camera one hop away behind a switch that decrements
            # is cheap to reach and costs nothing when it is not there.
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            socks.append(s)
        except OSError:
            # An interface that cannot send multicast is ordinary. Discovery
            # is an offer, so one failure must never stop the others.
            s.close()
    return socks


def wsd_localname(tag):
    """Namespace prefixes are not portable between vendors: the same field
    arrives as d:XAddrs, wsdd:XAddrs or plain XAddrs."""
    return tag.rpartition("}")[2]


def wsd_text(root, name):
    for elem in root.iter():
        if wsd_localname(elem.tag) == name and elem.text:
            return elem.text.strip()
    return ""


def wsd_scopes(raw):
    """onvif://www.onvif.org/name/Front%20Door -> ("name", "Front Door")."""
    out = []
    for scope in raw.split():
        try:
            parsed = urlparse(scope)
        except ValueError:
            continue
        if parsed.scheme != "onvif":
            continue
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            out.append((parts[0],
                        unquote("/".join(parts[1:])) if len(parts) > 1 else ""))
    return out


def wsd_parse(data):
    """The fields worth having from one ProbeMatch, or None."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None                 # not XML, or truncated; nothing to do
    if not any(wsd_localname(e.tag) == "ProbeMatch" for e in root.iter()):
        return None
    uid = ""
    for elem in root.iter():
        if wsd_localname(elem.tag) == "EndpointReference":
            uid = wsd_text(elem, "Address")
            break
    return {
        # The device identity, and the only field stable across the several
        # answers one device sends. Two spellings exist in the wild,
        # urn:uuid:<x> and bare uuid:<x>; a device is consistent, so the
        # string works as a key, but do not assume the prefix.
        "uuid": uid,
        "xaddrs": wsd_text(root, "XAddrs").split(),
        "types": wsd_text(root, "Types"),
        "scopes": wsd_scopes(wsd_text(root, "Scopes")),
    }


def wsd_is_camera(types):
    """Does the Types list claim a video transmitter?

    Reads Types, never the `type` scope. The scope is vendor free text: Dahua
    writes Network_Video_Transmitter, Hikvision and Reolink write
    video_encoder, TP-Link writes NetworkVideoTransmitter. Classifying on it
    called six of eight real cameras not-cameras. Even in Types the prefix
    moves (dn: on five, tdn: on two), so compare on the local name with the
    punctuation stripped.
    """
    return "networkvideotransmitter" in re.sub(r"[^a-z]", "", types.lower())


def wsd_host(xaddr):
    """The host of an advertised address, or "" if it does not parse.

    An XAddr is a string a camera chose to send, not necessarily a URL. One
    Dahua here advertises `http://[]/onvif/device_service`, empty brackets and
    no host, which urlparse raises ValueError on under Python 3.12+.
    """
    try:
        host = urlparse(xaddr).hostname or ""
    except ValueError:
        return ""
    return host.strip("[]")


def wsd_address(dev):
    """The address to offer for this device.

    Prefers one it demonstrably answered *from* over one it merely advertised.
    That is a cheaper answer to "the advertisement may be unreachable" than
    fetching it: the reply itself proves the source address works from here,
    and the wizard then tests the real snapshot URL anyway, which is the thing
    that actually has to work. IPv4 before IPv6, because that is the address
    an operator configured the camera with.
    """
    hosts = [h for h in (wsd_host(x) for x in dev["xaddrs"]) if h]
    if not hosts:
        return dev["sources"][0] if dev["sources"] else ""
    return sorted(hosts, key=lambda h: (h not in dev["sources"], ":" in h))[0]


def wsd_scope(dev, key):
    for k, v in dev["scopes"]:
        if k == key and v:
            return v
    return ""


def discover_cameras(window=WSD_WINDOW, repeat=WSD_REPEAT):
    """Probe the local network and return the devices that answered.

    Cameras first, then anything else that answered, each sorted by address.
    Never raises: discovery is an offer, and a network this cannot probe must
    leave the operator typing an address rather than looking at a traceback.
    """
    socks = wsd_open_sockets(wsd_source_addresses())
    if not socks:
        return []
    try:
        for _ in range(repeat):
            for s in socks:
                try:
                    s.sendto(wsd_probe(), (WSD_GROUP, WSD_PORT))
                except OSError:
                    pass
            time.sleep(0.15)

        devices = {}
        deadline = time.time() + window
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select(socks, [], [], remaining)
            except OSError:
                break
            for s in ready:
                try:
                    data, addr = s.recvfrom(65535)
                except OSError:
                    continue
                reply = wsd_parse(data)
                if reply is None:
                    continue
                # Deduplicate on the device id, never on the address: one
                # device answers several times, from several addresses, and an
                # NVR proxies the cameras behind it.
                key = reply["uuid"] or f"addr:{addr[0]}"
                dev = devices.setdefault(key, {
                    "uuid": reply["uuid"], "xaddrs": [], "sources": [],
                    "scopes": [], "types": "",
                })
                dev["types"] = dev["types"] or reply["types"]
                for x in reply["xaddrs"]:
                    if x not in dev["xaddrs"]:
                        dev["xaddrs"].append(x)
                if addr[0] not in dev["sources"]:
                    dev["sources"].append(addr[0])
                for pair in reply["scopes"]:
                    if pair not in dev["scopes"]:
                        dev["scopes"].append(pair)
    finally:
        for s in socks:
            s.close()

    return wsd_finalise(devices)


def wsd_finalise(devices):
    """Turn collected replies into the list the wizard shows.

    Separate from the socket work so the classification can be tested against
    real response bodies without a network. `types`, never the `type` scope:
    that is the trap this whole path exists around.
    """
    found = []
    for dev in devices.values():
        dev["address"] = wsd_address(dev)
        dev["camera"] = wsd_is_camera(dev["types"])
        dev["name"] = wsd_scope(dev, "name")
        dev["hardware"] = wsd_scope(dev, "hardware")
        if dev["address"]:
            found.append(dev)
    # Cameras first, then anything else that answered.
    return sorted(found, key=lambda d: (not d["camera"],
                                        sort_key_for_address(d["address"])))


def sort_key_for_address(addr):
    """Numeric where it can be, so .9 sorts before .10."""
    parts = addr.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return (0, tuple(int(p) for p in parts), "")
    return (1, (), addr)


# Substrings that actually name a vendor, matched against the ONVIF name and
# hardware scopes together. Deliberately short: only the makes this wizard has
# a template for, and only names specific enough that a false match is
# unlikely. The value is the CAMERA_PRESETS label, so that reordering the
# presets cannot silently repoint these.
VENDOR_HINTS = (
    ("dahua", "Dahua / Amcrest"),
    ("amcrest", "Dahua / Amcrest"),
    ("hikvision", "Hikvision (ISAPI)"),
    ("reolink", "Reolink"),
    ("axis", "Axis"),
)


def wsd_preset(dev):
    """The CAMERA_PRESETS label this device looks like, or "".

    Returns "" rather than guessing. Measured: this identifies six of the
    eight cameras here, and the two it misses report no vendor name at all
    (a Reolink calls itself IPC-BO, a TP-Link Tapo calls itself TC40). A
    wrong preselection is worse than none, because it is a wrong URL that
    looks deliberate.
    """
    text = f"{dev.get('name', '')} {dev.get('hardware', '')}".lower()
    for needle, label in VENDOR_HINTS:
        if needle in text:
            return label
    return ""


CAMERA_PRESETS = [
    ("Dahua / Amcrest", "http", "digest",
     "http://{ip}/cgi-bin/snapshot.cgi?channel=1&subtype=0"),
    ("Hikvision (ONVIF)", "http", "digest",
     "http://{ip}/onvif-http/snapshot?Profile_1"),
    ("Hikvision (ISAPI)", "http", "digest",
     "http://{ip}/ISAPI/Streaming/channels/101/picture"),
    ("Reolink", "http", "none",
     "http://{ip}/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=tl"
     "&user={user}&password={password}"),
    ("Axis", "http", "digest", "http://{ip}/axis-cgi/jpg/image.cgi"),
    ("Generic ONVIF snapshot", "http", "digest",
     "http://{ip}/onvif-http/snapshot?Profile_1"),
    ("RTSP only (no snapshot URL)", "rtsp", None,
     "rtsp://{user}:{password}@{ip}:554/stream1"),
    ("Custom URL", None, None, None),
]


def url_host(raw):
    """Bracket an IPv6 literal so it can go inside a URL.

    The rule lives in timelapse_encode, which is where the wizard, the
    pre-flight and the web UI all read shared rules from; this is the name the
    wizard calls it by. Same arrangement as redact_url() above, and for the
    same reason: one rule, not one copy per caller.
    """
    from timelapse_encode import url_host as shared
    return shared(raw)


def choose_cameras(cfg, expected):
    heading("Cameras")
    cfg["cameras"] = []
    if AUTO or _TTY is None:
        # There is nothing sensible to invent without input, and probing four
        # imaginary addresses would just burn a timeout each.
        note("Running non-interactively - no cameras configured.")
        note("Add them later with 'timelapse setup', or edit the config file.")
        return

    note("You can skip this and edit the config by hand later, but adding at")
    note("least one camera now lets the wizard verify it actually works.")
    print()
    if not ask_yes("Configure cameras now?", True):
        return

    found = offer_discovery()

    cams = []
    while True:
        cam = add_one_camera(cfg, len(cams) + 1, found)
        if cam:
            cams.append(cam)
            good(f"Added '{cam['name']}' ({len(cams)} configured)")
        print()
        # Name the camera about to be added, not the count already done - the
        # question is about the next one, so "(3 of ~9)" right after adding the
        # third reads as though it were going backwards.
        if len(cams) < expected:
            question = f"Add camera {len(cams) + 1} of ~{expected}?"
        else:
            question = f"Add another camera? ({len(cams)} configured)"
        if not ask_yes(question, len(cams) < expected):
            break
    cfg["cameras"] = cams


def print_discovered():
    """`timelapse discover`: what is on this network, and nothing else.

    Exists because cameras are usually added long after the install, and
    because "what is my camera's address" is a question worth answering
    without starting a wizard that wants to write a config.
    """
    heading("Cameras on this network")
    try:
        found = discover_cameras()
    except Exception as exc:                        # noqa: BLE001
        fail(f"Discovery failed: {type(exc).__name__}: {exc}")
        return 1

    if not found:
        warn("Nothing answered.")
        note("Multicast does not cross subnets or VLANs, so a camera on a")
        note("separate network will not appear here even though it works")
        note("perfectly. Many cameras also ship with ONVIF turned off.")
        return 0

    print()
    print(f"  {'':<4}{'ADDRESS':<16} {'NAME':<24} {'MODEL':<22} TYPE")
    for dev in found:
        print(f"  {'cam' if dev['camera'] else '':<4}"
              f"{dev['address']:<16} {(dev['name'] or '-')[:24]:<24} "
              f"{(dev['hardware'] or '-')[:22]:<22} "
              f"{'camera' if dev['camera'] else 'other device'}")
    print()
    note("'cam' means the device claims to be a video transmitter. Anything")
    note("else answering is an NVR, a doorbell, a printer or a PC: Windows")
    note("shares this discovery protocol.")
    cams = [d for d in found if d["camera"]]
    if cams:
        print()
        note("Service addresses (ONVIF), not snapshot URLs:")
        for dev in cams:
            for xaddr in dev["xaddrs"]:
                print(f"    {dev['address']:<16} {xaddr}")
    return 0


def offer_discovery():
    """Offer a scan, and return what answered. Never the only path.

    Multicast does not cross subnets and rarely crosses VLANs, and a dedicated
    camera VLAN is common in exactly the deployments that have many cameras.
    So finding nothing is reported as "nothing answered here", never as "there
    are no cameras", and typing an address by hand stays a first-class answer.
    """
    if AUTO or _TTY is None:
        # ask_yes() returns the default without a terminal, so without this
        # an unattended run would spend three seconds probing a network on
        # behalf of nobody, and have no way to offer the result.
        return []

    print()
    note("Cameras that speak ONVIF can usually be found automatically. This")
    note("sends one multicast query and listens for a few seconds; it sends")
    note("no credentials, so it cannot lock a camera account.")
    print()
    if not ask_yes("Scan this network for cameras?", True):
        return []

    print()
    note(f"Listening for {WSD_WINDOW:.0f} seconds ...")
    try:
        found = discover_cameras()
    except Exception as exc:                        # noqa: BLE001
        # Discovery is a convenience. Anything at all going wrong here must
        # leave the operator adding cameras by hand, not looking at a stack.
        warn(f"Discovery failed ({type(exc).__name__}: {exc}).")
        note("Add cameras by hand below; nothing else is affected.")
        return []

    cameras = [d for d in found if d["camera"]]
    if not cameras:
        print()
        warn("Nothing answered on this network segment.")
        note("That does not mean there are no cameras. Multicast does not")
        note("cross subnets or VLANs, and many cameras have ONVIF turned off")
        note("by default. Add them by hand below, which always works.")
        return []

    print()
    good(f"{len(cameras)} camera(s) answered.")
    others = len(found) - len(cameras)
    if others:
        # An NVR, an encoder or a doorbell answers this probe too, and so does
        # every Windows machine on the LAN.
        note(f"{others} other device(s) also answered and are not offered.")
    note("They are listed for each camera you add. What a camera reports is")
    note("its model, not its location, so you still choose what to call it.")
    return cameras


def pick_discovered(found):
    """Let the operator choose a discovered camera, or 0 to type one in.

    Returns the device, or None for "by hand". A device already added is not
    offered again, and the marking happens only once the camera is really
    added, so abandoning one halfway does not lose it from the list.
    """
    free = [d for d in found if not d.get("used")]
    if not free:
        return None
    print()
    print("    Found on this network:")
    print(f"          {'ADDRESS':<16} {'MODEL':<24} TYPE")
    for i, dev in enumerate(free, 1):
        # The model, not the name: three Dahuas here all call themselves
        # "Dahua", so the name does not distinguish them and the model does.
        model = dev["hardware"] or dev["name"] or "(unnamed)"
        print(f"      {i:>2}  {dev['address']:<16} {model[:24]:<24} "
              f"{wsd_preset(dev) or '? choose below'}")
    print("       0  none of these, enter an address by hand")
    choice = ask_int("Pick one", 1, 0, len(free))
    return None if choice == 0 else free[choice - 1]


def add_one_camera(cfg, n, found=None):
    print()
    print(f"  {bold(f'Camera {n}')}")

    device = pick_discovered(found) if found else None

    default_type = 1
    if device is not None:
        hint = wsd_preset(device)
        if hint:
            # +1: the menu is 1-based. Matched by label rather than by index
            # so that reordering CAMERA_PRESETS cannot silently repoint it.
            default_type = [p[0] for p in CAMERA_PRESETS].index(hint) + 1
        else:
            print()
            note(f"This device calls itself "
                 f"'{device['name'] or device['hardware'] or 'nothing'}',")
            note("which does not identify the make. Pick the type below.")

    print()
    for i, (label, _, _, _) in enumerate(CAMERA_PRESETS, 1):
        print(f"    {i}  {label}")
    choice = ask_int("Camera type", default_type, 1, len(CAMERA_PRESETS))
    label, method, auth, template = CAMERA_PRESETS[choice - 1]

    # sanitise_name(), not a second copy of its body: this line had drifted
    # into an inline duplicate of it, which is how the two would have come to
    # disagree about what a camera may be called.
    while True:
        name = sanitise_name(ask("Name (used as the folder name)", f"Camera{n}"),
                             f"Camera{n}")
        if not reject_reserved(name):
            break

    if template is None:                       # custom
        method = "rtsp" if ask_yes("Is this an RTSP stream?", False) else "http"
        url = ask("Full snapshot URL", "")
        if not url:
            fail("No URL given; skipping this camera.")
            return None
        auth = ask("Auth (digest/basic/none)", "digest").lower()
        user = pwd = ""
        if auth in ("digest", "basic"):
            user = ask("Username", "admin")
            pwd = ask_secret("Password")
    else:
        # The discovered address is a default, never a fact: it is offered so
        # it can be corrected. Only the host is taken, never the port the
        # device service answered on. Those are different endpoints, and a
        # Reolink's ONVIF service on :8000 says nothing about where its
        # snapshot lives, which is :80.
        ip = ask("IP address or hostname",
                 device["address"] if device else "192.168.1.100")
        host = url_host(ip)
        if host != ip.strip():
            note(f"IPv6 address bracketed for the URL: {host}")
            if ip.strip().lower().startswith("fe80:") and "%" not in ip:
                warn("A link-local address needs a zone id (fe80::1%eth0),")
                note("and it moves with the interface. Prefer the camera's")
                note("stable address if it has one.")
        user = pwd = ""
        if auth in ("digest", "basic") or method == "rtsp" or "{user}" in template:
            user = ask("Username", "admin")
            pwd = ask_secret("Password")
        url = template.format(ip=host, user=quote(user), password=quote(pwd))

    cam = {"name": name, "enabled": True, "method": method, "url": url}
    if method == "http":
        cam["auth"] = auth or "none"
        if cam["auth"] in ("digest", "basic"):
            cam["username"] = user
            cam["password"] = pwd
    else:
        cam["quality"] = 2

    edit_camera_smoothing(cfg, cam)

    if ask_yes("Test this camera now?", True):
        if not test_camera(cam, cfg) and not ask_yes("Keep it anyway?", True):
            return None
    if device is not None:
        # Only now, so that abandoning a camera halfway leaves its device
        # available to pick again.
        device["used"] = True
    return cam


# ----------------------------------------------------------------------------
# Camera management (--cameras-only)
#
# The nightly encode builds its work list from the cameras named *and enabled*
# in the config, and looks for <frames_root>/<name>/. So a camera's identity in
# the config is what makes its already-captured frames reachable: remove it,
# disable it, or rename it without moving the directory, and everything it has
# captured becomes invisible to the encoder and sits on disk forever. Each of
# those three paths below warns about that rather than silently stranding data.
# ----------------------------------------------------------------------------

DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sanitise_name(raw, fallback):
    """Camera names become directory names, so keep them boring.

    Character stripping only. Reserved device names survive this on purpose:
    they are refused by the prompts with an explanation, because silently
    handing back `Camera3` to somebody who typed `NUL` teaches them nothing,
    and this function has no way to say why. `check_camera_names()` in the
    pre-flight is the backstop for a config that got one by another route.
    """
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_") or fallback


def redact_url(url):
    """Mask credentials in a URL, or in any text that might contain one.

    ask_secret() exists to keep passwords out of scroll-back; printing the
    camera list would hand them straight back otherwise.

    The rule itself lives in timelapse_encode, which is where the wizard, the
    pre-flight and the web UI all read it from. This function is the name the
    wizard has always called it by, kept so that there is one rule rather than
    one per caller: the local copy handled `password=` and not the RTSP
    `rtsp://user:pass@host` shape, and every camera added by the wizard's own
    RTSP path uses that shape.
    """
    from timelapse_encode import redact
    return redact(url)


def camera_frames_dir(cfg, name):
    return Path(cfg["paths"]["frames_root"]) / name


def pending_days(cfg, name):
    """Completed day directories this camera has captured but not yet encoded."""
    d = camera_frames_dir(cfg, name)
    if not d.is_dir():
        return []
    today = date.today().isoformat()
    try:
        return sorted(p for p in d.iterdir()
                      if p.is_dir() and DAY_DIR_RE.match(p.name) and p.name < today)
    except OSError:
        return []


def warn_stranded(cfg, name, verb):
    """True if the user still wants to go ahead."""
    pend = pending_days(cfg, name)
    if not pend:
        return ask_yes(f"{verb.capitalize()} '{name}'?", False)
    warn(f"'{name}' has {len(pend)} un-encoded day(s) in "
         f"{camera_frames_dir(cfg, name)}")
    note("The nightly encode only looks at cameras enabled in the config, so")
    note(f"this would leave those frames on disk with nothing to encode them.")
    note(f"Encode them first with:  timelapse encode --date {pend[0].name}")
    return ask_yes(f"{verb.capitalize()} it anyway?", False)


def reject_reserved(name):
    """True, having said so, if this name cannot be a directory on Windows.

    Refused on Linux too. A `config.json` is portable between platforms by
    design, so a name only one of them accepts is a trap set for whoever moves
    the file; and no one has ever wanted a camera called `AUX`.
    """
    if not is_reserved_name(name):
        return False
    fail(f"'{name}' is a reserved device name and cannot be a folder.")
    note("Windows treats it as hardware rather than as a file. A camera named")
    note("NUL there records nothing at all, and the rest are refused outright,")
    note("so the name is rejected on every platform to keep configs portable.")
    return True


def name_taken(cams, name, skip=None):
    # Case-insensitive: two cameras differing only in case would be two config
    # entries writing into two directories that differ only in case, which is a
    # trap on any case-insensitive destination the videos get copied to.
    low = name.lower()
    return any(c is not skip and str(c.get("name", "")).lower() == low
               for c in cams)


def camera_interval(cfg, cam):
    return int(cam.get("interval_seconds")
               or cfg.get("capture", {}).get("interval_seconds", 5))


def camera_framerate(cfg, cam):
    return int(cam.get("framerate")
               or cfg.get("encode", {}).get("framerate", 60))


def camera_overrides(cam):
    return bool(cam.get("interval_seconds") or cam.get("framerate"))


def list_cameras(cfg):
    from timelapse_encode import camera_smoothing

    cams = cfg.get("cameras", [])
    if not cams:
        note("No cameras configured.")
        return
    print()
    print(f"    {'#':>2}  {'Name':<14} {'On':<4}{'Cadence':<11} {'Type':<4} URL")
    for i, cam in enumerate(cams, 1):
        # Elide the middle, not the tail. Reolink-style URLs are identical for
        # their first 40 characters, so a plain truncation makes every camera
        # look the same - and it would hide the *** that shows the password is
        # masked, which reads as though nothing were redacted at all. The head
        # is kept long enough for the IP, which is the part that differs.
        url = redact_url(str(cam.get("url", "")))
        # 36, and the 13-character tail in particular, is load-bearing: on a
        # Reolink URL that is what keeps the *** in view, and a listing that
        # elides the mask reads as though the password were printed in full.
        # Anything new on this row is paid for from the other columns.
        if len(url) > 36:
            url = url[:20] + "..." + url[-13:]
        # Padded before colouring, never after. dim() wraps the text in escape
        # codes and a format width counts them, so `{dim("no"):<4}` pads to
        # nothing and the row reads "no5s/60". Invisible in every test and on
        # CI, because dim() is a no-op unless stdout is a tty; it only appears
        # in front of the operator. Reported from a real terminal at 0.1.9.
        on = cam.get("enabled", True)
        state = f"{'yes' if on else 'no':<4}"
        if not on:
            state = dim(state)
        # A trailing * means this camera is not following the global settings,
        # which is what decides whether changing them will move it. +N is
        # optional smoothing. All three answer one question, "how is this
        # camera's video made", so they share a column rather than spending
        # width the 80-column budget does not have.
        n = camera_smoothing(cam)
        cad = (f"{camera_interval(cfg, cam)}s/{camera_framerate(cfg, cam)}"
               + ("*" if camera_overrides(cam) else "")
               + (f"+{n}" if n else ""))
        # The separators are literal spaces rather than padding, so a cadence
        # too wide for its column pushes the row out instead of running into
        # the next one. 3600s/240*+30 is absurd but constructible, and an
        # absurd config should still be readable.
        print(f"    {i:>2}  {str(cam.get('name', '')):<14} {state}"
              f"{cad:<11} {str(cam.get('method', 'http')):<4} {dim(url)}")
    if any(camera_overrides(c) for c in cams):
        note("  * has its own interval or frame rate; the rest follow the "
             "global settings.")
    if any(camera_smoothing(c) for c in cams):
        note("  +N averages N frames at encode time to smooth motion; "
             "capture is unchanged.")


def pick_camera(cams, verb):
    if not cams:
        fail(f"No cameras to {verb}.")
        return None
    n = ask_int(f"Which camera to {verb}? (0 cancels)", 0, 0, len(cams))
    return None if n == 0 else n - 1


def resolve_camera(cams, token):
    """Index of the camera `token` names or numbers, or None after saying why.

    The number is the position 'timelapse cameras -l' prints, which is the
    only identifier a camera has: nothing in the config is a stable id, and
    inventing one would be a schema change for the sake of a command line.
    That makes the number an artefact of the order cameras were added, so a
    name that matches wins over a position that matches. '#2' forces the
    position, for the one config where a camera is actually called "2".
    """
    tok = (token or "").strip()
    if not cams:
        fail("No cameras are configured.")
        return None
    if not tok:
        fail("Give a camera name or number, as in -e:2 or -e:Doorbell.")
        return None

    by_position = tok.startswith("#")
    if by_position:
        tok = tok[1:].strip()
    else:
        low = tok.lower()
        for i, cam in enumerate(cams):
            if str(cam.get("name", "")).lower() == low:
                if tok.isdigit() and i != int(tok) - 1:
                    note(f"'{tok}' is the name of camera #{i + 1}, so that is "
                         f"the one meant. Use '#{tok}' for the position.")
                return i

    if tok.isdigit():
        n = int(tok)
        if 1 <= n <= len(cams):
            return n - 1
        fail(f"There is no camera #{n}; there are {len(cams)}.")
    elif by_position:
        fail(f"'#{tok}' is not a number.")
    else:
        fail(f"No camera is called '{tok}'.")
    note("Run 'timelapse cameras -l' for the list.")
    return None


def rename_camera_frames(cfg, old, new):
    """Move the frames directory so a rename does not orphan what it captured."""
    src, dst = camera_frames_dir(cfg, old), camera_frames_dir(cfg, new)
    if not src.is_dir():
        return
    if dst.exists():
        warn(f"{dst} already exists, so the frames under '{old}' cannot move.")
        note("Merge the two directories by hand or they will not be encoded.")
        return
    if not ask_yes(f"Move already-captured frames '{old}/' -> '{new}/'?", True):
        warn(f"Frames under '{old}' will no longer be encoded.")
        return
    try:
        src.rename(dst)
        good(f"Moved {src} -> {dst}")
    except OSError as exc:
        fail(f"Could not move the frames: {exc}")
        warn(f"Frames under '{old}' will no longer be encoded.")


def edit_one_camera(cfg, cams, cam):
    old_name = str(cam.get("name", ""))
    print()
    print(f"  {bold('Editing ' + old_name)}")
    note("Enter keeps the current value.")

    while True:
        new_name = sanitise_name(ask("Name", old_name), old_name)
        if reject_reserved(new_name):
            continue
        if new_name != old_name and name_taken(cams, new_name, skip=cam):
            fail(f"Another camera is already called '{new_name}'.")
            continue
        break

    cam["url"] = ask("Snapshot URL", str(cam.get("url", "")))
    if cam.get("method", "http") == "http":
        cam["auth"] = ask("Auth (digest/basic/none)",
                          str(cam.get("auth", "none"))).lower()
        if cam["auth"] in ("digest", "basic"):
            cam["username"] = ask("Username", str(cam.get("username", "")))
            pw = ask_secret("Password (blank keeps the current one)")
            if pw:
                cam["password"] = pw
        else:
            cam.pop("username", None)
            cam.pop("password", None)

    if new_name != old_name:
        rename_camera_frames(cfg, old_name, new_name)
        cam["name"] = new_name

    edit_camera_cadence(cfg, cam)
    edit_camera_smoothing(cfg, cam)

    if ask_yes("Test it now?", True):
        test_camera(cam, cfg)
    return cam


def edit_camera_cadence(cfg, cam):
    """Per-camera interval and frame rate, or back to following the global.

    Answering with the global value *removes* the key rather than storing the
    same number. That is the whole design: absent means "follow the default",
    so a camera left alone still moves when the global interval changes, and
    only the ones deliberately pinned stay put.
    """
    g_int = int(cfg.get("capture", {}).get("interval_seconds", 5))
    g_fps = int(cfg.get("encode", {}).get("framerate", 60))
    before_interval = camera_interval(cfg, cam)
    before_fps = camera_framerate(cfg, cam)
    print()
    note(f"This camera can run on its own cadence. The defaults are one frame "
         f"every {g_int}s, played at {g_fps}fps.")
    note("Answer with the default to go back to following it.")

    interval = ask_int("Seconds between snapshots for this camera",
                       camera_interval(cfg, cam), 1, 3600)
    fps = ask_int("Frame rate for this camera",
                  camera_framerate(cfg, cam), 1, 240)

    for key, value, default in (("interval_seconds", interval, g_int),
                                ("framerate", fps, g_fps)):
        if value == default:
            cam.pop(key, None)
        else:
            cam[key] = value

    per_day = int(86400 / interval)
    note(f"{per_day:,} frames/day -> {video_length(per_day, fps)} of video "
         f"at {fps}fps"
         + ("" if camera_overrides(cam) else ", following the defaults"))
    if (interval, fps) != (before_interval, before_fps):
        # One day is one video at one cadence. Saying so here is the whole
        # reason it is safe to restart capture straight after this: today
        # keeps the cadence it began with either way.
        note("This takes effect at midnight. Today keeps the cadence it "
             "started with, so a day is never half one rate and half another.")

    min_frames = int(cfg.get("encode", {}).get("min_frames", 100))
    if per_day < min_frames:
        # The encoder SKIPs a day below min_frames, so this would produce
        # nothing at all, every night, without ever failing.
        warn(f"That is under encode.min_frames ({min_frames}), so the nightly "
             f"encode would skip this camera every night.")
        note(f"A {interval}s interval needs min_frames below {per_day} to "
             f"produce anything.")


def edit_camera_smoothing(cfg, cam):
    """Optional motion smoothing for this camera, off unless asked for.

    Deliberately not modelled on the cadence questions above. Those answer to a
    global, so absence means "follow it"; here absence means off, because there
    is no sensible global. Averaging frames calms wind in foliage, which is
    most of what makes a timelapse look like it jumps, at the cost of thinning
    out anything that crosses the frame in a frame or two. A roof of trees
    wants it; a gate people walk through does not.

    Answering no *removes* the key rather than storing a zero, so a config that
    has never wanted smoothing stays the shape it has always been.
    """
    from timelapse_encode import (SMOOTH_DEFAULT, SMOOTH_MAX, SMOOTH_MIN,
                                  camera_smoothing)

    current = camera_smoothing(cam)
    print()
    # One note() per printed line: it does not wrap, and the terminal is the
    # only place these are ever read.
    note("Smoothing averages neighbouring frames when encoding, so wind")
    note("in trees stops shimmering. It also fades out anything crossing")
    note("the frame quickly, so it suits wide views more than doorways.")
    if not ask_yes("Smooth this camera?", bool(current)):
        cam.pop("smooth_frames", None)
        return

    n = ask_int(f"Frames to average ({SMOOTH_MIN}-{SMOOTH_MAX})",
                current or SMOOTH_DEFAULT, SMOOTH_MIN, SMOOTH_MAX)
    cam["smooth_frames"] = n
    # Said in real time, not in frames. Frames are the knob, but the thing
    # being blurred together is a span of the day, and that span moves with
    # this camera's interval: 15 frames is 75s at 5s and 15 minutes at 60s.
    span = n * camera_interval(cfg, cam)
    note(f"Each output frame becomes the average of {n}, spanning {span}s of "
         f"real time.")
    note("This changes encoding only. Tonight's run smooths today too; days")
    note("already encoded keep their video unless you re-encode with --force.")


def load_existing_config(path):
    """An existing config for the --*-only sections, or None after saying why.

    The distinction between "not there" and "not readable by you" is the whole
    point. config.json is 0640 root:timelapse so camera passwords stay
    private, so running any of these without sudo hits PermissionError; the
    old message called that "No existing config" and told the operator to run
    the full wizard, which would have offered to overwrite the config they
    could not read. The daemons' load_config() has always drawn this
    distinction properly; this brings the wizard in line with it.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        fail(f"No config at {path}.")
        note("Run: sudo timelapse setup")
    except PermissionError:
        fail(f"Cannot read {path}: permission denied.")
        note("It is 0640 root:timelapse so your camera passwords stay private,")
        note("which means changing it needs root. Try the same command again")
        note("with sudo.")
    except json.JSONDecodeError as exc:
        fail(f"{path} is not valid JSON: {exc}")
        note("Fix it by hand, or run 'sudo timelapse setup' to write a fresh")
        note("one. The wizard backs up whatever is there first.")
    except OSError as exc:
        fail(f"Cannot read {path}: {exc}")
    return None


def unit_is_active(unit):
    """True, False, or None when the service manager cannot be asked at all.

    A thin name over the platform module's answer, kept because every caller
    here reads better for it and because the tests patch it.
    """
    return service_is_active(unit)


def restart_unit(unit, success):
    """Restart a unit and say what happened. True if it came back.

    The wording lives here rather than in the platform module: that module must
    never print, because a Windows service has no console to print to and a
    stray write there kills the service entry point (item 11c.2).
    """
    ok, detail = restart_service(unit)
    if ok:
        good(success)
        return True
    if detail:
        fail(f"Restart failed: {detail}")
        return False
    fail("Restart failed (are you root?).")
    note(f"See: {log_hint(unit)}")
    return False


def restart_web_if_running(cfg):
    """The web server reads its config once, at startup.

    The same trap restart_capture_if_running() exists for, and it bit a real
    install: `systemctl enable --now` is a no-op on an already-active unit, so
    the wizard printed a new bind address while the running process kept
    serving the old one. It looked like the UI refusing connections on an
    address the wizard had just reported as correct.
    """
    unit = "timelapse-web.service"
    enabled = bool(cfg.get("web", {}).get("enabled"))
    active = unit_is_active(unit)
    if active is None:
        return
    if not active:
        if enabled:
            note(f"Start it with: {start_hint(unit)}")
        return
    print()
    if not enabled:
        # It exits 0 when disabled, so a restart is what stops it. Leaving it
        # alone would keep serving a UI the operator just turned off.
        warn("The web UI is still running with the settings it started on.")
        if ask_yes("Stop it now?", True):
            restart_unit(unit, "Web UI stopped.")
        else:
            note(f"Stop it with: {stop_hint(unit)}")
        return
    note("The web UI is running on the settings it read at startup.")
    if not ask_yes("Restart it so the new settings take effect now?", True):
        warn("The running server still has the previous settings.")
        note(f"Apply them with: {restart_hint(unit)}")
        return
    restart_unit(unit, "Web UI restarted on the new settings.")


def restart_capture_if_running():
    """Capture reads its config once, at startup.

    Same trap the installer had when it replaced scripts under a live service:
    editing the file changes nothing until the daemon restarts.
    """
    unit = "timelapse-capture.service"
    active = unit_is_active(unit)
    if active is None:
        return
    if not active:
        note("Capture is not running; the new list applies when it next starts.")
        return
    print()
    if not ask_yes("Restart capture so the change takes effect now?", True):
        warn("Capture is still using the previous camera list.")
        note(f"Apply it with: {restart_hint(unit)}")
        return
    restart_unit(unit, "Capture restarted on the new camera list.")


# ----------------------------------------------------------------------------
# Registering the Windows service and the two scheduled tasks
#
# The Linux equivalent is install.sh sync_units(): a shell script writing four
# unit files. This lives in Python rather than in install.ps1 for one reason,
# and it is the reason `tools/` was deleted: two installers that both know how
# to register a service disagree within one release. install.ps1 front-ends
# this, exactly as item 11c.6b says the GUI must front-end install.ps1.
#
# The definitions are one table because the mapping from a unit file matters
# more than any single field. Every line here has a line in service/*.service
# or service/*.timer behind it, and the pairs should be read together.
# ----------------------------------------------------------------------------

def service_definitions(scripts_dir, config_path, python=None):
    """What to register, as (unit, description, argv, extras) tuples.

    Pure, so both CI legs assert the same table: the argv, the schedule and the
    mapping onto the unit files are exactly what goes wrong here, and none of it
    needs a service manager to check.
    """
    python = python or sys.executable
    scripts = Path(scripts_dir)
    cfg = str(config_path)

    def argv(script, *flags):
        return [python, str(scripts / script)] + list(flags) + [cfg]

    return [
        # timelapse-capture.service. --service is what makes the process talk
        # to the SCM; without it this is the ordinary foreground daemon.
        (CAPTURE_UNIT, "Camera snapshot capture for timelapse",
         argv("timelapse_capture.py", "--service"), {}),
        # timelapse-encode.timer: OnCalendar=*-*-* 00:05:00, Persistent=true,
        # RandomizedDelaySec=300, TimeoutStartSec=infinity.
        (ENCODE_UNIT, "Encode yesterday's timelapse frames into daily videos",
         argv("timelapse_encode.py"),
         {"triggers": daily_trigger(0, 5, jitter_minutes=5),
          "catch_up": True, "time_limit": "PT0S"}),
        # timelapse-watch.timer: every 5 minutes, TimeoutStartSec=60, and
        # deliberately NOT Persistent, because a missed check is not worth
        # catching up on.
        (WATCH_UNIT, "Report cameras that are refusing our credentials",
         argv("timelapse_encode.py", "--watch"),
         {"triggers": repeating_trigger(5),
          "catch_up": False, "time_limit": "PT1M"}),
    ]


def install_units(scripts_dir, config_path, user_id=None):
    """Register everything. True if all of it landed.

    Failures are reported per unit and do not stop the others, which is the
    same failure isolation the encoder has: a watch task that will not register
    must not cost the operator their capture service.
    """
    ok_all = True
    for unit, description, argv, extras in service_definitions(scripts_dir,
                                                               config_path):
        native = native_name(unit)
        if is_scheduled(unit):
            xml = task_xml(description, argv, user_id=user_id, **extras)
            ok, detail = install_task(unit, xml)
        else:
            ok, detail = install_service(unit, description, argv)
        if ok:
            good(f"Registered {native}.")
            if detail:
                note(detail)
        else:
            fail(f"Could not register {native}: {detail}")
            ok_all = False
    return ok_all


def restart_units():
    """Restart whatever is running, so it executes what was just installed.

    Separate from install_units(), and called **after** the wizard rather than
    during registration, which is the ordering install.sh has: registering
    replaces the files on disk and does not touch the process already running,
    and the wizard then rewrites the config underneath it. Restarting at
    registration time therefore restarted onto the new build and the *old*
    config, and the operator was told to start a service that was already
    running the wrong settings.

    Not offered as a choice, for the reason install.sh gives: the operator
    asked for this version, and declining leaves the previous one running while
    every version number says otherwise. It costs the frames due during the
    restart, a second or two.
    """
    ok_all = True
    for unit in (CAPTURE_UNIT,):
        if not service_is_active(unit):
            continue
        ok, detail = restart_service(unit)
        if ok:
            good(f"Restarted {native_name(unit)} onto the new build.")
        else:
            fail(f"Could not restart {native_name(unit)}: {detail}")
            note(f"It is still running the previous build: {restart_hint(unit)}")
            ok_all = False
    return ok_all


def remove_units():
    """Deregister everything. True if all of it went, absent counting as gone."""
    ok_all = True
    for unit in (CAPTURE_UNIT, ENCODE_UNIT, WATCH_UNIT):
        native = native_name(unit)
        if is_scheduled(unit):
            ok, detail = remove_task(unit)
        else:
            # Stopping first is not optional: DeleteService on a running
            # service only marks it for deletion, and it then lingers as
            # "marked for deletion" until the process exits, which blocks the
            # next install with a error that names neither cause.
            stop_service(unit)
            ok, detail = remove_service(unit)
        if ok:
            good(f"Removed {native}.")
        else:
            fail(f"Could not remove {native}: {detail}")
            ok_all = False
    return ok_all


def print_unit_status():
    """One line per component. Never guesses: unanswerable says so."""
    for unit in (CAPTURE_UNIT, ENCODE_UNIT, WATCH_UNIT):
        native = native_name(unit)
        if is_scheduled(unit):
            info = task_info(unit)
            if info is None:
                state = "not installed" if task_exists(unit) is False \
                    else "unknown"
            else:
                # Translated, never the raw number: a task that has never run
                # reports 267011, and printed as a figure that is indis-
                # tinguishable from a fault on a system that has none.
                state = f"{info.get('State', '?')}, " \
                        f"{task_result(info.get('LastResult'))}"
        else:
            code = service_state(unit)
            state = "unknown" if code is None else SERVICE_STATES.get(code,
                                                                      code)
        print(f"{native:<20} {state}")
    return 0


def manage_cameras(cfg):
    """Interactive add/edit/remove loop. True if anything changed."""
    heading("Cameras")
    if AUTO or _TTY is None:
        fail("Managing cameras needs a terminal.")
        return False

    cams = cfg.setdefault("cameras", [])
    changed = False
    # Offered once per session rather than on every "a": a scan takes seconds
    # and the answer does not change between two adds a minute apart.
    found = None
    while True:
        list_cameras(cfg)
        print()
        note("a add   e edit   r remove   x enable/disable   t test   q save & quit")
        action = ask("Action", "q").strip().lower()[:1]

        if action == "a":
            if found is None:
                found = offer_discovery()
            cam = add_one_camera(cfg, len(cams) + 1, found)
            if cam:
                if name_taken(cams, cam["name"]):
                    fail(f"A camera called '{cam['name']}' already exists; "
                         "two cameras cannot share a frames directory.")
                else:
                    cams.append(cam)
                    changed = True
                    good(f"Added '{cam['name']}' ({len(cams)} configured)")

        elif action == "e":
            i = pick_camera(cams, "edit")
            if i is not None:
                edit_one_camera(cfg, cams, cams[i])
                changed = True

        elif action == "r":
            i = pick_camera(cams, "remove")
            if i is not None and warn_stranded(cfg, str(cams[i].get("name", "")),
                                               "remove"):
                good(f"Removed '{cams.pop(i).get('name', '')}'")
                changed = True

        elif action == "x":
            i = pick_camera(cams, "enable or disable")
            if i is not None:
                cam = cams[i]
                name = str(cam.get("name", ""))
                if cam.get("enabled", True):
                    if not warn_stranded(cfg, name, "disable"):
                        continue
                    cam["enabled"] = False
                    good(f"'{name}' disabled")
                else:
                    cam["enabled"] = True
                    good(f"'{name}' enabled")
                changed = True

        elif action == "t":
            i = pick_camera(cams, "test")
            if i is not None:
                test_camera(cams[i], cfg)

        elif action == "q":
            return changed

        else:
            fail(f"Unknown action '{action}'.")


# The menu's actions, reachable directly. Adding a camera should not mean
# reading a list, choosing 'a', and answering the same questions; and the
# targeted forms are what makes anything scriptable, since the menu needs a
# terminal and a human. Same letters as the menu on purpose.
CAMERA_ACTIONS = ("list", "add", "edit", "toggle", "test", "remove")

# Everything except list and test writes the config, so everything except list
# and test needs the questions a terminal can answer.
CAMERA_ACTIONS_WRITING = ("add", "edit", "toggle", "remove")


def camera_action(cfg, action, token):
    """One targeted camera command. (config changed, succeeded).

    "Changed" and "succeeded" are separate answers because declining a
    confirmation is neither a change nor a failure: 'timelapse cameras
    -r:Doorbell' answered with 'n' has done exactly what was asked of it.
    """
    cams = cfg.setdefault("cameras", [])

    if action == "list":
        list_cameras(cfg)
        return False, True

    if action == "add":
        cam = add_one_camera(cfg, len(cams) + 1, offer_discovery())
        if not cam:
            note("Nothing added.")
            return False, True
        if name_taken(cams, cam["name"]):
            fail(f"A camera called '{cam['name']}' already exists; "
                 "two cameras cannot share a frames directory.")
            return False, False
        cams.append(cam)
        good(f"Added '{cam['name']}' ({len(cams)} configured)")
        return True, True

    i = resolve_camera(cams, token)
    if i is None:
        return False, False
    cam = cams[i]
    name = str(cam.get("name", ""))

    if action == "test":
        # Read-only, so it neither writes the config nor offers a restart.
        return False, bool(test_camera(cam, cfg))

    if action == "edit":
        edit_one_camera(cfg, cams, cam)
        return True, True

    if action == "toggle":
        if not cam.get("enabled", True):
            cam["enabled"] = True
            good(f"'{name}' enabled")
            return True, True
        # Disabling strands frames exactly as removing does: the encoder
        # builds its list from the cameras that are enabled, so whatever this
        # one has already captured stops being encoded by anything.
        if not warn_stranded(cfg, name, "disable"):
            note(f"'{name}' left enabled.")
            return False, True
        cam["enabled"] = False
        good(f"'{name}' disabled")
        return True, True

    if action == "remove":
        if not warn_stranded(cfg, name, "remove"):
            note("Nothing removed.")
            return False, True
        cams.pop(i)
        good(f"Removed '{name}'")
        return True, True

    fail(f"Unknown camera action '{action}'.")
    return False, False


def explain_payload(data):
    """(printable head, camera's own error message or None).

    Recognises the Reolink shape:
        [{"cmd":"Snap","code":1,"error":{"detail":"login failed",...}}]
    """
    text = data[:400].decode("utf-8", errors="replace").strip()
    head = " ".join(text.split())[:160]
    try:
        doc = json.loads(data.decode("utf-8", errors="replace"))
    except Exception:
        return head, None
    if isinstance(doc, list) and doc and isinstance(doc[0], dict):
        doc = doc[0]
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            detail = err.get("detail") or err.get("rspCode")
            if detail:
                return head, str(detail)
        if isinstance(err, str):
            return head, err
    return head, None


# Only what would genuinely break a query string. Percent-encoding more than
# necessary is not harmless: some camera firmware (Reolink notably) does not
# percent-decode query values, so an over-encoded password fails to
# authenticate while the same password typed literally works.
_MUST_ENCODE = set("&=#+%")


def quote(s):
    out = []
    for byte in s.encode("utf-8"):
        ch = chr(byte)
        if byte < 0x21 or byte > 0x7E or ch in _MUST_ENCODE:
            out.append("%%%02X" % byte)
        else:
            out.append(ch)
    return "".join(out)


def test_camera(cam, cfg):
    if cam["method"] == "rtsp":
        return test_camera_rtsp(cam, cfg)
    try:
        import requests
        from requests.auth import HTTPBasicAuth, HTTPDigestAuth
    except ImportError:
        warn("python3-requests is not installed; skipping the live test.")
        return True

    sess = requests.Session()
    mode = (cam.get("auth") or "none").lower()
    if mode == "digest":
        sess.auth = HTTPDigestAuth(cam.get("username"), cam.get("password"))
    elif mode == "basic":
        sess.auth = HTTPBasicAuth(cam.get("username"), cam.get("password"))

    import time
    t0 = time.time()
    try:
        r = sess.get(cam["url"], timeout=cfg["capture"]["timeout_seconds"] + 4)
    except Exception as exc:
        fail(f"Could not reach the camera: {type(exc).__name__}")
        note("Check the IP, that the camera is on this network, and that HTTP")
        note("snapshots are enabled on it.")
        return False
    dt = time.time() - t0

    if r.status_code == 401:
        fail("HTTP 401 Unauthorized - wrong credentials or wrong auth scheme.")
        other = "basic" if mode == "digest" else "digest"
        if ask_yes(f"Retry with auth '{other}'?", True):
            cam["auth"] = other
            return test_camera(cam, cfg)
        return False
    if r.status_code != 200:
        fail(f"HTTP {r.status_code} from the camera.")
        return False
    if r.content[:2] != b"\xff\xd8":
        # The body is where the camera says what is actually wrong. Reolink in
        # particular answers 200 OK with a JSON error when auth fails, so
        # reporting only "not a JPEG" hides the real cause.
        head, reason = explain_payload(r.content)
        fail("Responded, but the payload is not a JPEG.")
        if reason:
            fail(f"The camera said: {reason}")
            note("That is an authentication or permission error, not a URL")
            note("problem. Check the username and password, and that the")
            note("account is allowed to take snapshots.")
        else:
            note(f"First bytes: {head}")
        return False

    dims = probe_sample(cfg, r.content)
    good(f"{len(r.content)/1024:.0f} KB{dims}, {dt:.2f}s")
    if dt > cfg["capture"]["interval_seconds"] * 0.6:
        warn(f"{dt:.2f}s is slow for a "
             f"{cfg['capture']['interval_seconds']}s interval.")
    return True


def test_camera_rtsp(cam, cfg):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "frame.jpg"
        cmd = [cfg["paths"]["ffmpeg"], "-y", "-nostdin", "-hide_banner",
               "-loglevel", "error", "-rtsp_transport", "tcp",
               "-i", cam["url"], "-frames:v", "1", "-q:v", "2", str(out)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            fail("RTSP grab timed out after 45s.")
            return False
        if p.returncode != 0 or not out.exists():
            # ffmpeg quotes the URL it was handed, password and all. The
            # wizard asks for that password with ask_secret() precisely to
            # keep it out of the scroll-back; printing ffmpeg's complaint
            # verbatim would hand it straight back.
            fail(f"RTSP grab failed: {redact_url((p.stderr or '').strip())[:160]}")
            return False
        good(f"RTSP frame captured ({out.stat().st_size/1024:.0f} KB)")
        return True


def probe_sample(cfg, data):
    """Report WxH of a JPEG held in memory, if ffprobe is usable."""
    import tempfile
    ffprobe = cfg["paths"].get("ffprobe", "ffprobe")
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            tf.write(data)
            tmp = Path(tf.name)
        r = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0:s=x", str(tmp)],
            capture_output=True, text=True, timeout=20)
        tmp.unlink(missing_ok=True)
        out = r.stdout.strip()
        return f", {out}" if out else ""
    except Exception:
        return ""


def mount_fstype(path):
    """Filesystem type backing path, and its mount point. ('', None) if none."""
    try:
        entries = []
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                entries.append((parts[1].replace("\\040", " "), parts[2]))
    except OSError:
        return "", None
    target = str(Path(path))
    best = ("", None)
    for mount, fstype in entries:
        if target == mount or target.startswith(mount.rstrip("/") + "/"):
            if best[1] is None or len(mount) > len(best[1]):
                best = (fstype, mount)
    return best


def probe_rsync_flags(dest, svcuser=None):
    """Which rsync flags this destination accepts. See timelapse_encode.

    Imported rather than defined here because the pre-flight needs the same
    answer, and both already reach into the encoder for the things it owns.
    rsync is the encoder's business: it is the program that runs it nightly.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import probe_rsync_flags as probe
    return probe(dest, svcuser)


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def looks_like_ssh_spec(dest):
    """user@host:/path, which is an rsync destination and only rsync's.

    Deliberately not a test for "remote": \\\\tower\\cctv is remote and is
    perfectly usable. This asks whether the string is the *Linux* shape, so
    that a config written there and carried to a Windows box is refused with
    its reason rather than treated as a relative path, which would silently
    create a folder called `user@nas:` next to the videos.

    A bare drive letter is excluded by requiring an @ before the colon, so
    C:\\videos is never mistaken for a host.
    """
    text = str(dest)
    head = text.split(":", 1)[0] if ":" in text else ""
    return "@" in head and "/" not in head and "\\" not in head


def choose_windows_destination(cfg):
    """One question: a path this machine can write. (item 11c.5)

    No SSH and no mounting, because neither exists here. A UNC path needs no
    mount at all, which deletes most of what the Linux branch does, and a
    remote destination is required to be an existing writable network path
    rather than something this tool sets up.
    """
    print()
    note("A folder on this machine, or a network path such as")
    note(r"\\tower\videos\timelapse. A network path must already exist and")
    note("be writable; this does not create shares or map drives.")
    print()
    dest = ask("Destination path", "")
    if not dest.strip():
        warn("Nothing given. Transfer left disabled.")
        cfg["transfer"]["enabled"] = False
        return

    if looks_like_ssh_spec(dest):
        fail("That is an rsync-over-SSH destination, which is Linux only.")
        note("Windows has no equivalent and none is planned. Give a folder")
        note("or a network path instead.")
        cfg["transfer"]["enabled"] = False
        return

    # The single most likely way a Windows install fails, and it fails in a
    # way that reads as this tool being broken: drive mappings are per logon
    # session, so U:\TL is something the operator can open in Explorer and a
    # service cannot see at all.
    #
    # This wizard is itself an instance of that, which is how the first version
    # came to store U:\TL verbatim: it runs elevated, UAC gives an elevated
    # process its own logon session, and so the check written to warn about
    # missing drive mappings was running somewhere that had none.
    unc = network_path(dest)
    if unc:
        print()
        warn(f"{dest[:2]} is a mapped drive, which exists only for you.")
        note("A service or a scheduled task gets its own logon session and no")
        note("mappings at all, so it would fail with 'path not found' on a")
        note("path you can open perfectly well. Storing where it really points:")
        note(f"  {unc}")
        dest = unc
    elif drive_is_local(dest) is False:
        # A letter that is neither resolvable nor a fixed disk. From here that
        # is a mapping made without /persistent, or a typo, and the two are
        # indistinguishable *and want the same answer*: this cannot be stored.
        print()
        fail(f"{dest[:2]} is not a drive this machine can use unattended.")
        note("It is either a drive mapping that will not survive a reboot, or")
        note("a letter that does not exist. Either way the nightly encode runs")
        note("without your sign-in session and would not find it.")
        note("")
        note("Give the network path itself instead, the \\\\server\\share form.")
        note("'net use' in a normal window prints it for each mapped drive.")
        cfg["transfer"]["enabled"] = False
        return

    cfg["transfer"]["destination"] = dest
    # Nothing to mount, so nothing to check for having been unmounted. The
    # Linux flag guards against a dropped mount turning a share back into an
    # empty local directory; a UNC path that is unreachable simply fails.
    cfg["transfer"]["require_mountpoint"] = False
    check_windows_destination(cfg, dest)


def check_windows_destination(cfg, dest):
    """Write a file to the destination, and settle the credentials question.

    Two separate things, and conflating them is the mistake available here.
    The probe answers "can *this* account write there". The nightly encode
    runs as LocalSystem, which presents the machine account on the network and
    is a different principal entirely, so on a UNC path a probe that passes
    proves nothing about the job that matters. That is probe_as() met on a
    platform where the account is not a formality.

    Storing credentials is what closes the gap, because then the wizard and
    the encoder make the same WNetAddConnection2W call with the same secret,
    and the probe becomes authoritative for both.
    """
    from timelapse_encode import reach_destination, try_destination

    print()
    ok, why = try_destination(dest)
    if ok:
        good(f"{dest} is writable")
    else:
        warn(f"Could not write to {dest}: {why}")

    if not is_unc(dest):
        # A local folder. LocalSystem is the most privileged account on this
        # machine, so what the wizard just measured carries over.
        if not ok:
            note("Check the path and the permissions on it; the nightly")
            note("encode will report this the same way until it can write.")
        return

    print()
    note("The nightly encode runs as the system account, which presents this")
    note(f"machine's name to {share_root(dest)}, not yours. That is a")
    note("different account from the one you are signed in as, so it may be")
    note("refused where you are allowed.")
    print()
    note("Storing a username and password for the share removes the doubt:")
    note("the encode connects with them itself, exactly as this wizard is")
    note("about to, so what gets tested here is what runs at 00:05.")
    print()
    # Defaulted to yes even when the probe just passed, because the probe
    # passing is the case most likely to mislead: it says the operator may
    # write there, and the operator is not who runs at 00:05. Declining costs
    # a keystroke; the other error costs a silent nightly failure.
    if not ask_yes("Store credentials for the share?", True):
        cfg["transfer"].pop("username", None)
        cfg["transfer"].pop("password", None)
        print()
        if ok:
            warn("Saved without credentials, and untested for the account")
            warn("that will actually run.")
        note("If the nightly transfer is refused, re-run 'timelapse setup'")
        note("and answer yes here. 'timelapse test' reports it too.")
        return

    user = ask("Share username (DOMAIN\\user, or user)", "")
    password = ask_secret("Share password")
    cfg["transfer"]["username"] = user
    cfg["transfer"]["password"] = password

    # Drop any existing connection to this server first, or the probe below
    # cannot fail. Windows permits one identity per server per session, so a
    # connection made earlier (by Explorer, or by a previous run of this
    # wizard under a different username) makes WNetAddConnection2W answer
    # ERROR_SESSION_CREDENTIAL_CONFLICT, and the write that follows then
    # succeeds over *that* connection and reports the new credentials as good.
    # Explorer re-establishes its own on next use, so the cost is nil.
    disconnect_share(dest)

    print()
    ok, why = reach_destination(cfg["transfer"], dest)
    if ok:
        good(f"Connected to {share_root(dest)} as {user} and wrote a file.")
        note("That is the same connection the nightly encode makes, so this")
        note("result is the one that counts.")
    else:
        fail(f"Could not write to {dest} as {user}: {why}")
        note("The credentials are saved so you can correct them with")
        note("'timelapse setup' rather than retyping everything, but the")
        note("nightly transfer will fail until this passes.")


def choose_transfer(cfg, svcuser=None):
    heading("Transfer (optional)")
    note("After encoding, videos can be moved to a NAS or another host.")
    note("Leave this off to keep them on the local disk.")
    print()
    if not ask_yes("Send finished videos somewhere else?", False):
        cfg["transfer"]["enabled"] = False
        return
    cfg["transfer"]["enabled"] = True

    if IS_WINDOWS:
        # Mounting a CIFS share and an rsync remote spec are both Linux-only
        # mechanisms, so offering them here would be offering two of three
        # options that cannot work.
        choose_windows_destination(cfg)
        return

    print()
    print("    1  A network share (SMB/CIFS) - set it up for me")
    print("       (a NAS share; mounts it, tests it and makes it survive reboots)")
    print("    2  A path already on this machine")
    print("       (a share you have mounted yourself, or any local disk)")
    print("    3  Another host over SSH")
    print("       (rsync remote spec - needs working SSH keys for the service account)")
    kind = ask_int("How is the destination reached?", 1, 1, 3)

    if kind == 3:
        note("SSH key authentication must already work for the account the")
        note("encoder runs as, non-interactively. Test it before enabling.")
        cfg["transfer"]["destination"] = ask("Destination",
                                             "user@nas:/mnt/user/timelapse/")
        cfg["transfer"]["require_mountpoint"] = False
    elif kind == 1:
        if not setup_cifs_share(cfg, svcuser):
            print()
            warn("Share not configured. Transfer left disabled; re-run")
            warn("'timelapse transfer' once the share is reachable.")
            cfg["transfer"]["enabled"] = False
    else:
        dest = ask("Destination path", "/mnt/nas/timelapse/")
        cfg["transfer"]["destination"] = dest
        configure_local_transfer(cfg, dest, svcuser)

    if cfg["transfer"]["enabled"] and not shutil.which("rsync"):
        warn("rsync is not installed - transfers will fail until it is.")


# ----------------------------------------------------------------------------
# SMB/CIFS share setup
# ----------------------------------------------------------------------------

def ensure_cifs_utils():
    if shutil.which("mount.cifs"):
        return True
    note("Installing cifs-utils...")
    for mgr, args in (
        ("apt-get", ["install", "-y", "-qq", "cifs-utils"]),
        ("dnf", ["install", "-y", "-q", "cifs-utils"]),
        ("pacman", ["-S", "--noconfirm", "--needed", "cifs-utils"]),
        ("zypper", ["--non-interactive", "install", "cifs-utils"]),
        ("apk", ["add", "--quiet", "cifs-utils"]),
    ):
        if not shutil.which(mgr):
            continue
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        try:
            subprocess.run([mgr] + args, capture_output=True, timeout=300, env=env)
        except Exception:
            pass
        break
    if shutil.which("mount.cifs"):
        good("Installed cifs-utils")
        return True
    fail("Could not install cifs-utils. Install it and re-run.")
    return False


def try_mount_cifs(unc, mountpoint, opts_base):
    """Mount, negotiating the SMB dialect down. Returns (ok, vers, error)."""
    last = ""
    for vers in ("", "vers=3.1.1", "vers=3.0", "vers=2.1"):
        opts = opts_base + (f",{vers}" if vers else "")
        try:
            r = subprocess.run(["mount", "-t", "cifs", unc, mountpoint,
                                "-o", opts],
                               capture_output=True, text=True, timeout=60)
        except Exception as exc:
            return False, "", str(exc)[:200]
        if r.returncode == 0:
            return True, vers, ""
        last = " ".join((r.stderr or "").split())[:200]
    return False, "", last


def setup_cifs_share(cfg, svcuser=None):
    """Prompt for a share, mount it, verify it, and persist it. True on success."""
    print()
    if not is_root():
        fail("Mounting a share needs root privileges.")
        note("Re-run this as root (the installer does), or choose option 2")
        note("and give a path you have already mounted yourself.")
        return False
    if not ensure_cifs_utils():
        return False

    server = ask("NAS address (IP or hostname)", "")
    if not server:
        fail("No address given.")
        return False
    share = ask("Share name (as it appears on the NAS)", "cctv")
    subdir = ask("Folder inside the share", "timelapse")
    mountpoint = ask("Mount the share at", f"/mnt/{share}")
    username = ask("SMB username", "")
    password = ask_secret("SMB password")

    uid = gid = 0
    if svcuser:
        try:
            import pwd
            entry = pwd.getpwnam(svcuser)
            uid, gid = entry.pw_uid, entry.pw_gid
        except (ImportError, KeyError):
            warn(f"Unknown user '{svcuser}'; mounting as root.")

    # 0600 and root-owned: it holds the share password in plain text.
    cred = Path(CONFIG_DIR) / f"cifs-{share}.cred"
    cred.parent.mkdir(parents=True, exist_ok=True)
    old_umask = os.umask(0o077)
    try:
        cred.write_text(f"username={username}\npassword={password}\n",
                        encoding="utf-8")
    finally:
        os.umask(old_umask)
    os.chmod(cred, 0o600)
    del password
    good(f"Credentials written to {cred} (root only)")

    opts = (f"credentials={cred},uid={uid},gid={gid},"
            f"file_mode=0664,dir_mode=0775,iocharset=utf8")
    unc = f"//{server}/{share}"
    Path(mountpoint).mkdir(parents=True, exist_ok=True)

    if os.path.ismount(mountpoint):
        good(f"{mountpoint} is already mounted")
        vers = ""
    else:
        note(f"Mounting {unc} at {mountpoint}...")
        ok_mount, vers, err = try_mount_cifs(unc, mountpoint, opts)
        if not ok_mount:
            fail(f"Could not mount {unc}")
            if err:
                note(err)
            note("Common causes: wrong username or password, the share name")
            note("is not what the NAS calls it, or the user has no access.")
            return False
        good(f"Mounted{' with ' + vers if vers else ''}")

    dest = str(PurePosixPath(mountpoint) / subdir) if subdir else mountpoint
    try:
        Path(dest).mkdir(parents=True, exist_ok=True)
        if svcuser:
            try:
                shutil.chown(dest, user=svcuser)
            except (OSError, LookupError):
                pass
    except OSError as exc:
        fail(f"Could not create {dest} on the share: {exc}")
        return False
    good(f"Destination {dest} ready")

    cfg["transfer"]["destination"] = dest.rstrip("/") + "/"
    cfg["transfer"]["require_mountpoint"] = True

    print()
    note("Checking which rsync flags this share accepts...")
    flags = probe_rsync_flags(dest, svcuser)
    if flags:
        cfg["transfer"]["rsync_args"] = flags + ["--remove-source-files"]
        good(f"rsync {' '.join(flags)} works here")
        if "-a" not in flags:
            note("'-a' failed - it implies --owner --group, which this share")
            note("cannot set. Using the flags above instead.")
    elif flags == []:
        fail("No rsync flag combination worked against the share.")
        note("The mount succeeded but the service account cannot write.")
        return False
    else:
        note("Could not test (rsync missing?); leaving the defaults.")

    persist_cifs_mount(unc, mountpoint, opts, vers)
    return True


def sync_unit_readwritepaths(cfg, unitdir="/etc/systemd/system"):
    """Update ReadWritePaths= in the installed units from the config.

    install.sh does this after the full wizard, but a later `timelapse
    transfer` also changes where the encoder writes. Without this the unit
    still lists only the old paths and ProtectSystem=strict fails the write
    read-only - which looks nothing like a configuration mistake.

    Returns (status, detail). The status matters more than it looks: this used
    to return a bare bool, counting the units it *rewrote*, and the caller read
    anything falsy as failure. So an operator who ran it as root against units
    that were already correct - the ordinary case, since install.sh writes them
    on every upgrade - was told to go and edit the unit by hand, as root, while
    being root. "Nothing to do" and "could not do it" are different answers and
    must not share a return value.

        changed   the units were rewritten
        current   they already said the right thing
        absent    no units are installed here
        denied    not running as root
        empty     the config yields no writable paths
        failed    a unit exists but could not be written
    """
    if not is_root():
        return "denied", "needs root to edit the unit files"
    paths = " ".join(writable_paths(cfg))
    if not paths:
        return "empty", "the config names no writable paths"

    touched, failed, seen = [], [], 0
    for name in ("timelapse-capture.service", "timelapse-encode.service"):
        unit = Path(unitdir) / name
        if not unit.exists():
            continue
        seen += 1
        try:
            lines = unit.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as exc:
            failed.append(f"{name}: {exc}")
            continue
        out, changed = [], False
        for line in lines:
            if line.startswith("ReadWritePaths="):
                new = f"ReadWritePaths={paths}\n"
                changed = changed or new != line
                out.append(new)
            else:
                out.append(line)
        if not changed:
            continue
        try:
            unit.write_text("".join(out), encoding="utf-8")
            touched.append(name)
        except OSError as exc:
            failed.append(f"{name}: {exc}")

    if failed:
        return "failed", "; ".join(failed)
    if not seen:
        return "absent", f"no timelapse units in {unitdir}"
    if not touched:
        return "current", paths

    good(f"Updated ReadWritePaths in {', '.join(touched)}")
    note(f"  {paths}")
    try:
        subprocess.run(["systemctl", "daemon-reload"],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    note("Restart the encoder timer for it to take effect:")
    note("  systemctl restart timelapse-encode.timer")
    return "changed", paths


def report_readwritepaths(status, detail):
    """Say what happened, and only sound the alarm when something is wrong.

    "changed" has already narrated itself. Every other outcome is either fine
    or actionable, and they need telling apart: the whole point of the split.
    """
    if status == "changed":
        return
    if status == "current":
        good("ReadWritePaths already covers the destination; nothing to do.")
        note(f"  {detail}")
        return
    if status == "absent":
        note("No systemd units are installed here, so there is nothing to")
        note("update. They get these paths when the installer runs.")
        return
    warn("Add the destination to ReadWritePaths= in "
         "timelapse-encode.service by hand,")
    warn("or ProtectSystem=strict will fail the write read-only.")
    if status == "denied":
        note("Re-run this with sudo to do it automatically.")
    elif detail:
        note(f"  {detail}")


def persist_cifs_mount(unc, mountpoint, opts, vers):
    """Add an /etc/fstab entry so the share comes back after a reboot."""
    fstab = Path("/etc/fstab")
    try:
        current = fstab.read_text(encoding="utf-8")
    except OSError:
        warn("Could not read /etc/fstab; the mount will not survive a reboot.")
        return
    if f" {mountpoint} " in current:
        good("/etc/fstab already has an entry for this mount point")
        return
    # nofail + x-systemd.automount: a NAS that is down must not block boot,
    # and the share mounts on first access instead.
    full = (f"{opts}{',' + vers if vers else ''},_netdev,nofail,"
            f"x-systemd.automount,x-systemd.mount-timeout=30")
    line = f"{unc}  {mountpoint}  cifs  {full}  0  0"
    try:
        shutil.copy2(fstab, f"/etc/fstab.bak.{int(__import__('time').time())}")
        with open(fstab, "a", encoding="utf-8") as fh:
            fh.write(f"\n# timelapse-maker transfer destination\n{line}\n")
    except OSError as exc:
        warn(f"Could not update /etc/fstab: {exc}")
        return
    good("Added to /etc/fstab (backup taken) - it will remount at boot")
    try:
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True,
                       timeout=30)
    except Exception:
        pass


NETWORK_MOUNTS = ("cifs", "smb3", "smbfs", "nfs", "nfs4", "fuse.sshfs")


def configure_local_transfer(cfg, dest, svcuser):
    """Check a local destination and set the options it needs."""
    fstype, mount = mount_fstype(dest)
    path = Path(dest)

    if not path.is_dir():
        warn(f"{dest} does not exist yet.")
        if mount and fstype:
            note(f"Its filesystem ({fstype} at {mount}) is mounted, so the")
            note("encoder will create the directory on first transfer.")
        else:
            note("Nothing is mounted there. If this is meant to be a NAS share,")
            note("mount it first or the encoder will write to the local disk.")
    else:
        good(f"{dest} exists")

    if fstype in NETWORK_MOUNTS:
        good(f"backed by a {fstype} mount at {mount}")
        # A dropped mount turns the mountpoint back into an empty local
        # directory; rsync would fill the local disk and --remove-source-files
        # would then delete the originals.
        print()
        note("If the share is ever unmounted, this path becomes an ordinary")
        note("local directory and a transfer would fill the local disk instead.")
        cfg["transfer"]["require_mountpoint"] = ask_yes(
            "Refuse to transfer when the share is not mounted?", True)
    elif mount and mount != "/":
        note(f"backed by a {fstype} mount at {mount}")
        cfg["transfer"]["require_mountpoint"] = False
    else:
        warn("This path is on the root filesystem, not a separate mount.")
        note("That is fine for a local destination. If you expected a NAS")
        note("share here, it is not mounted.")
        cfg["transfer"]["require_mountpoint"] = False

    if not path.is_dir():
        return
    print()
    note("Checking which rsync flags this destination accepts...")
    flags = probe_rsync_flags(dest, svcuser)
    if flags is None:
        note("Could not test (rsync missing or destination unwritable).")
    elif not flags:
        fail("No rsync flag combination worked against this destination.")
        note("Check the share permissions for the service account.")
    else:
        cfg["transfer"]["rsync_args"] = flags + ["--remove-source-files"]
        good(f"rsync {' '.join(flags)} works here")
        if "-a" not in flags:
            note("'-a' failed - it implies --owner --group, which this share")
            note("cannot set. Using the flags above instead.")


def web_library_preview(cfg):
    """Where the web UI would look for videos, and whether it can read it.

    Shown during setup because it is the one thing about this feature that
    surprises people: transfer moves videos away with --remove-source-files,
    so the answer is usually the destination and not video_output.
    """
    web = cfg.get("web", {})
    trans = cfg.get("transfer", {})
    override = (web.get("library_root") or "").strip()
    dest = (trans.get("destination") or "").strip()
    if override:
        return override, "web.library_root"
    if trans.get("enabled") and dest:
        if dest.startswith("rsync://") or (not dest.startswith("/")
                                           and ":" in dest.split("/", 1)[0]):
            return dest, "transfer.destination - REMOTE, not readable here"
        return dest, "transfer.destination"
    return cfg["paths"].get("video_output", ""), "paths.video_output"


# ----------------------------------------------------------------------------
# Bind address
#
# A wrong bind address is the worst kind of error this wizard can write. The
# service starts, reports itself healthy, logs the address it is serving and is
# simply unreachable; nothing in the journal says "this host has no such
# address". So it is settled here, against the kernel, before the file is
# written.
# ----------------------------------------------------------------------------

LOOPBACK = ("127.0.0.1", "::1", "localhost")
WILDCARD = ("0.0.0.0", "::")


def lan_address():
    """This host's primary LAN address, or "" if it cannot be worked out.

    Asks the routing table which source address it would use to reach an
    off-subnet destination. No packets are sent: connect() on a UDP socket
    only fixes the peer locally, which is what makes this safe on a host with
    no internet access. TEST-NET-1 is used as the target so that even a
    misread of this code cannot point it at somebody's real server.

    gethostname() is deliberately not used. On Debian it resolves to 127.0.1.1
    to no useful purpose, which is exactly the answer this exists to avoid.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 1))
        addr = s.getsockname()[0]
    except OSError:
        return ""                   # no default route, or no network at all
    finally:
        s.close()
    return "" if not addr or addr.startswith("127.") else addr


def host_addresses():
    """Every address configured on this host, best effort, loopback last.

    Display only. Validation never depends on this list: check_bind() asks the
    kernel, so a host where neither command exists still gets a correct answer
    to the question that matters.
    """
    found = []
    try:
        r = subprocess.run(["ip", "-j", "addr"], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=10)
        if r.returncode == 0:
            for iface in json.loads(r.stdout.decode("utf-8", "replace") or "[]"):
                for info in iface.get("addr_info", []):
                    if info.get("local"):
                        found.append(info["local"])
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    if not found:
        try:
            r = subprocess.run(["hostname", "-I"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=10)
            if r.returncode == 0:
                found = r.stdout.decode("utf-8", "replace").split()
        except (OSError, subprocess.SubprocessError):
            pass
    # IPv6 link-local is dropped: it cannot be bound without a scope id, so
    # offering it as a choice would be offering something that does not work.
    found = [a for a in found if not a.lower().startswith("fe80:")]
    # Loopback is a valid answer, just never the interesting one.
    return sorted(set(found),
                  key=lambda a: (a.startswith("127.") or a == "::1", a))


def check_bind(addr, port):
    """Can the service actually listen there? Returns (kind, detail).

    Binds it for real rather than comparing against a list of interfaces,
    because the kernel is the authority and the failure modes need telling
    apart. "unavailable" is the silent one this whole section exists for; a
    port already taken is a different problem with a different fix.

    SO_REUSEADDR matches what the server sets, so this probes the same
    conditions the service will meet rather than stricter ones. It binds only,
    never listens, and closes immediately.

    Windows is the exception, and matching the server there would defeat the
    whole check: SO_REUSEADDR on Windows permits binding a port something else
    is actively listening on, so the probe succeeds and the wizard reports a
    taken port as free. Measured three ways: SO_REUSEADDR succeeds,
    SO_EXCLUSIVEADDRUSE refuses with 10048, and no option at all refuses with
    10048. So ask for exclusive use where the constant exists, which is
    Windows only and is therefore its own platform test.
    """
    if not str(addr).strip():
        return "bad", "no address given"
    try:
        infos = socket.getaddrinfo(addr, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return "bad", f"not an address this host can resolve ({exc})"

    detail = "could not bind"
    for family, socktype, proto, _canon, sockaddr in infos:
        s = socket.socket(family, socktype, proto)
        try:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            s.setsockopt(socket.SOL_SOCKET,
                         exclusive if exclusive else socket.SO_REUSEADDR, 1)
            s.bind(sockaddr)
            return "ok", ""
        except OSError as exc:
            if exc.errno == errno.EADDRNOTAVAIL:
                return "unavailable", f"no interface on this host has {addr}"
            if exc.errno == errno.EADDRINUSE:
                return "in-use", f"something is already listening on {addr}:{port}"
            if exc.errno in (errno.EACCES, errno.EPERM):
                return "denied", f"permission denied for port {port}"
            detail = str(exc)
        finally:
            s.close()
    return "error", detail


def confirm_bind(addr, port):
    """Report on a chosen address. True if it is worth writing to the config.

    An address already in use is accepted rather than refused: the usual
    reason is the web UI itself, still running on the settings it is being
    reconfigured away from, and refusing to let someone change the port of a
    running server would be absurd.
    """
    kind, detail = check_bind(addr, port)
    if kind == "ok":
        return True
    if kind == "in-use":
        note(f"{detail}.")
        note("Usually the web UI itself; the restart below settles it.")
        return True
    if kind == "denied":
        fail(f"{detail}.")
        note("Ports below 1024 need privileges the service does not have.")
        return False
    if kind == "unavailable":
        fail(f"{detail}.")
        note("The service would start, report success and be unreachable.")
        found = host_addresses()
        if found:
            note(f"This host has: {', '.join(found)}")
        return False
    fail(f"Cannot use {addr}:{port}: {detail}")
    return False


def suggest_bind(current):
    """The address to offer in the prompt.

    A working current setting wins: someone re-running the wizard to change
    the library path should not silently have their bind address moved. Then
    the LAN address, because a status page reachable only from the machine it
    describes is of little use on a headless recorder. 0.0.0.0 last, which
    always works but binds interfaces the operator may not have thought about.
    """
    current = (current or "").strip()
    # "ok" specifically, not "anything but unavailable": junk in the config
    # should be replaced by a working suggestion, not offered back.
    if current and current not in LOOPBACK and check_bind(current, 0)[0] == "ok":
        return current
    return lan_address() or "0.0.0.0"


MIN_PASSWORD = 6


def hash_web_password(password):
    """The one place the wizard turns a password into what gets stored.

    Imported from the web UI rather than reimplemented: two copies of a
    password format is two chances to write a config the server cannot read,
    and the failure would appear as a locked-out operator rather than as an
    error here.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_web import hash_password
    return hash_password(password)


def choose_web_login(web):
    """The optional single login for the web UI.

    Absent or blank means no login, which is what every install that predates
    this has, so the pages keep behaving exactly as they did.
    """
    auth = web.get("auth") or {}
    user = (auth.get("username") or "").strip()
    have = bool(user and (auth.get("password_hash") or "").strip())

    print()
    note("The pages can ask for a username and password first.")
    note("What that is for: keeping the household, or a guest on your wifi,")
    note("out of the status page and the video index. It is a lock on a door,")
    note("not a safe. There is no HTTPS here, so the password crosses your")
    note("network in clear, and the video files themselves stay reachable to")
    note("anyone who knows their exact address: that last part is deliberate,")
    note("it is what lets a saved .m3u keep playing in VLC afterwards.")
    if have:
        print()
        note(f"A login is configured, for '{user}'.")

    if not ask_yes("Require a login?", have):
        if have:
            warn("Removing the login. The pages will open to anyone who can")
            warn("reach the address above.")
        # Removed rather than blanked: absent is what every config without a
        # login looks like, and two spellings of "off" is one too many.
        web.pop("auth", None)
        return

    if have and not ask_yes("Set a new username and password?", False):
        return

    if AUTO or _TTY is None:
        # A backstop, and normally unreachable: both questions above default
        # to "leave it as it is" under AUTO, so an unattended run returns
        # before it gets here. It exists because a password cannot come from a
        # default, so if either of those defaults ever changes, this must be
        # what happens rather than a prompt nobody can answer.
        warn("No terminal here, so there is nothing to type a password into.")
        if have:
            warn(f"Keeping the existing login for '{user}'.")
        else:
            warn("Leaving the web UI without a login.")
            web.pop("auth", None)
        return

    set_web_login(web)


def set_web_login(web):
    """Ask for a username and password and store the hash. True if it changed.

    Split out of choose_web_login() because `timelapse password` is exactly
    this and none of the rest: somebody running that command has already
    answered "do you want a login?" by typing it.
    """
    if AUTO or _TTY is None:
        warn("No terminal here, so there is nothing to type a password into.")
        return False

    user = ((web.get("auth") or {}).get("username") or "").strip()
    print()
    name = ask("Username", user or "admin").strip()
    while True:
        # ask_secret() rather than ask(): a password typed at an install is a
        # password in the scroll-back and in whatever transcript somebody
        # pastes into an issue.
        first = ask_secret("Password")
        if not first:
            warn("A blank password would let anybody in. Nothing changed.")
            return False
        if len(first) < MIN_PASSWORD:
            warn(f"That is under {MIN_PASSWORD} characters. It will work, but")
            warn("it is a short one.")
        again = ask_secret("Password again")
        if again == first:
            break
        fail("Those did not match.")

    web["auth"] = {"username": name, "password_hash": hash_web_password(first)}
    good(f"Login set for '{name}'.")
    note("Only the hash is stored, so this cannot be read back out of the")
    note("config. If you forget it, `sudo timelapse password` sets a new one;")
    note("there is nothing to recover and nothing to type the old one into.")
    return True


def choose_web(cfg):
    heading("Web UI (optional)")
    note("A small read-only page: service status, and an index of your")
    note("finished videos that hands each one to VLC to play.")
    note("It changes nothing - no encoding, no camera control, no deleting.")
    if IS_WINDOWS:
        # Said before the question, not after it. The installer registers no
        # service for this on Windows yet (item 11f step 5), so answering yes
        # writes a setting nothing acts on, and a question that looks like
        # every other question is a question the operator will answer that way.
        print()
        warn("Not installed as a service on Windows yet.")
        note("Turning it on here writes the setting and starts nothing: there")
        note("is no service to run it. 'timelapse web-serve' runs it in the")
        note("foreground, in a window you keep open, which is worth having to")
        note("look at the video index and no good as a permanent arrangement.")
    print()

    cfg.setdefault("web", {})
    web = cfg["web"]
    prompt = ("Set it up anyway, for 'timelapse web-serve'?" if IS_WINDOWS
              else "Enable the web UI?")
    if not ask_yes(prompt, web.get("enabled", False)):
        web["enabled"] = False
        return
    web["enabled"] = True

    print()
    suggested = suggest_bind(web.get("bind"))
    found = host_addresses()
    if found:
        note(f"This host's addresses: {', '.join(found)}")
    note("There is no HTTPS, and the video files stay reachable to anyone who")
    note("knows their address; you can put a login on the pages further down.")
    note("A LAN address is the useful answer on a recorder you connect to")
    note("from elsewhere; 127.0.0.1 restricts it to this machine, and")
    note("0.0.0.0 accepts every interface.")
    if (web.get("bind") or "") in LOOPBACK and suggested not in LOOPBACK:
        # Never move a deliberate loopback choice without saying so out loud.
        print()
        warn(f"This is currently {web['bind']}, reachable only from this host.")
        warn(f"Accepting {suggested} below opens it to your network.")

    note("An IPv6 address works too; :: is the IPv6 answer to 0.0.0.0 and")
    note("accepts IPv4 as well.")
    while True:
        chosen = ask("Listen on", suggested).strip()
        # Port 0 asks the kernel only whether this host holds the address,
        # which is the question at this prompt. Whether the port is free is a
        # separate problem with a separate fix, so it is asked separately.
        if confirm_bind(chosen, 0):
            web["bind"] = chosen
            break
        # AUTO and a closed tty both return the default forever, so a rejected
        # default would spin here rather than stop.
        if AUTO or _TTY is None:
            warn(f"Keeping {chosen} unverified; there is no terminal to ask.")
            web["bind"] = chosen
            break
        print()

    while True:
        web["port"] = ask_int("Port", int(web.get("port", 8787)), 1, 65535)
        if web["port"] < 1024:
            # The probe below would very likely succeed and prove nothing: the
            # wizard normally runs as root and the service does not. Testing
            # as the wrong user is worse than not testing.
            fail(f"Port {web['port']} is privileged; the service runs "
                 f"unprivileged and could not bind it.")
            note("Use something above 1023, or put a reverse proxy in front.")
            if not (AUTO or _TTY is None):
                continue
        confirm_bind(web["bind"], web["port"])
        break

    if web["bind"] not in LOOPBACK:
        print()
        warn("Anyone who can reach this address can watch your videos.")
        warn("Keep it to a trusted LAN, or put a reverse proxy in front.")

    choose_web_login(web)

    print()
    where, why = web_library_preview(cfg)
    note(f"Videos will be read from: {where or '(not set)'}")
    note(f"  because: {why}")
    if "REMOTE" in why:
        # Mounting is the answer, not a fallback: browsing an SSH-only
        # destination over SFTP is refused (decided-against.md), so this must
        # not read as though the operator could wait for it.
        warn("That is an rsync remote spec, not a path this host can read.")
        warn("Browsing needs a readable path: mount that share and give the")
        warn("mount point below, or turn transfer off to keep videos local.")
    elif where and not Path(where).is_dir():
        warn(f"{where} does not exist yet - if it is a NAS mount, it may")
        warn("simply not be mounted. The page will say so rather than")
        warn("showing an empty library.")
    print()
    note("Leave blank to use the path above.")
    web["library_root"] = ask("Read videos from", web.get("library_root", ""))

    print()
    note("The web UI writes one thing: an index of your library. The service")
    note("is allowed to write this directory and nothing else on the system.")
    web["state_dir"] = ask("Index directory",
                           web.get("state_dir", WEB_STATE_DIR_DEFAULT))

    print()
    note("The Overview page can tell you when a new version is tagged, and")
    note("show what changed and the commands to upgrade.")
    note("It asks api.github.com once a day, and only while someone has the")
    note("page open. It sends no configuration, no camera names and nothing")
    note("about your videos; GitHub sees this host's IP and the version.")
    note("This is the only outbound connection the web UI ever makes.")
    web["update_check"] = ask_yes("Check GitHub for updates?",
                                  web.get("update_check", True))


def existing_sink(cfg, kind):
    """The configured sink of this type, or {}.

    Looks in `notify` first and falls back to the legacy `discord` block, so
    re-running this against a config written before 0.1.6 offers the webhook
    that is already in it rather than a blank prompt.
    """
    for sink in cfg.get("notify") or []:
        if isinstance(sink, dict) and (sink.get("type") or "discord") == kind:
            return sink
    if kind == "discord" and cfg.get("discord", {}).get("webhook_url"):
        return dict(cfg["discord"], type="discord")
    return {}


def put_sink(cfg, kind, sink):
    """Replace or append one sink, keeping the order stable.

    Order matters only for the log, but a list that reshuffles itself every
    time the wizard runs makes a config diff unreadable.
    """
    sinks = [s for s in (cfg.get("notify") or []) if isinstance(s, dict)]
    for i, existing in enumerate(sinks):
        if (existing.get("type") or "discord") == kind:
            sinks[i] = sink
            break
    else:
        sinks.append(sink)
    cfg["notify"] = sinks


def choose_notify(cfg, config_path=None):
    """Configure any number of notification sinks.

    Straight-line questions rather than a menu, one per service, which is how
    the rest of this wizard reads and which handles "I want both" without any
    extra machinery. Nothing here needs a service restart: the encoder is a
    oneshot and reads the config at the start of each run.
    """
    heading("Notifications (optional)")
    note("A nightly summary: what encoded, coverage, size, failures.")
    note("Any number of these can be on at once; all of them are optional.")
    print()

    choose_discord_sink(cfg, config_path)
    choose_ntfy_sink(cfg, config_path)
    choose_telegram_sink(cfg, config_path)

    # The legacy block is ignored once `notify` exists, so leaving it enabled
    # would be a webhook that looks configured and never fires. It is emptied
    # rather than deleted: a key that vanishes from a config is harder to
    # recognise than one that is plainly turned off.
    if cfg.get("notify") and cfg.get("discord", {}).get("enabled"):
        cfg["discord"]["enabled"] = False
        cfg["discord"]["webhook_url"] = ""


def choose_discord_sink(cfg, config_path=None):
    current = existing_sink(cfg, "discord")
    on = ask_yes("Send the summary to a Discord webhook?",
                 bool(current.get("enabled") and current.get("webhook_url")))
    if not on:
        if current:
            put_sink(cfg, "discord", dict(current, type="discord",
                                          enabled=False))
        return
    url = ask("Webhook URL", current.get("webhook_url", ""))
    sink = {"type": "discord", "enabled": bool(url), "webhook_url": url,
            "username": current.get("username", "Timelapse Bot")}
    put_sink(cfg, "discord", sink)
    if url and ask_yes("Send a test message now?", True):
        send_test_notification(sink, config_path)


def choose_ntfy_sink(cfg, config_path=None):
    current = existing_sink(cfg, "ntfy")
    print()
    note("ntfy delivers to a phone or desktop from a topic name, with no")
    note("account: pick a topic nobody else would guess and subscribe to it.")
    on = ask_yes("Send the summary to ntfy?", bool(current.get("enabled")))
    if not on:
        if current:
            put_sink(cfg, "ntfy", dict(current, type="ntfy", enabled=False))
        return
    server = ask("Server", current.get("server") or "https://ntfy.sh")
    topic = ask("Topic", current.get("topic", ""))
    # Anyone who knows a public topic can read it, so this is worth saying
    # once rather than leaving as a surprise.
    if topic and "ntfy.sh" in server:
        note("On the public server a topic is the only secret there is.")
    token = ask("Access token (blank for none)", current.get("token", ""))
    sink = {"type": "ntfy", "enabled": bool(topic), "server": server,
            "topic": topic, "token": token}
    put_sink(cfg, "ntfy", sink)
    if topic and ask_yes("Send a test message now?", True):
        send_test_notification(sink, config_path)


def choose_telegram_sink(cfg, config_path=None):
    current = existing_sink(cfg, "telegram")
    print()
    note("Telegram needs a bot token from @BotFather and a chat id. Message")
    note("your bot once, then read the id from @userinfobot or getUpdates.")
    on = ask_yes("Send the summary to Telegram?", bool(current.get("enabled")))
    if not on:
        if current:
            put_sink(cfg, "telegram", dict(current, type="telegram",
                                           enabled=False))
        return
    token = ask("Bot token", current.get("token", ""))
    chat = ask("Chat id", str(current.get("chat_id", "")))
    sink = {"type": "telegram", "enabled": bool(token and chat),
            "token": token, "chat_id": chat}
    put_sink(cfg, "telegram", sink)
    if token and chat and ask_yes("Send a test message now?", True):
        send_test_notification(sink, config_path)


def send_test_notification(sink, config_path=None):
    """Send one test message through the real sink code.

    Through `notify()` rather than a hand-built payload, so what is tested is
    what the nightly run will actually do. The wizard proving a webhook with a
    request the encoder would never make is how a check comes to pass while
    the thing it checks is broken.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import notify

    kind = (sink.get("type") or "discord").lower()
    sent, _ = notify({"notify": [dict(sink, enabled=True)]},
                     "timelapse-maker",
                     "Setup test: notifications are working.", "info",
                     [("Host", socket.gethostname())])
    if sent:
        good(f"{kind} accepted the test message.")
        record_notify_verified(config_path, sink)
        return True
    fail(f"{kind} did not accept the test message; see the message above.")
    if kind == "discord":
        note("403 usually means the webhook was deleted or regenerated.")
        note("Re-copy it from Channel Settings -> Integrations -> Webhooks.")
    elif kind == "ntfy":
        note("Check the topic name, and the token if the server needs one.")
    else:
        note("Check the bot token, and that you have messaged the bot at")
        note("least once: a bot cannot open a conversation with you.")
    return False


def choose_discord(cfg, config_path=None):
    """Kept as the old name for anything that calls it."""
    return choose_notify(cfg, config_path)


def send_test_webhook(url, username, config_path=None):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import post_webhook
    payload = {"username": username,
               "embeds": [{"title": "timelapse-maker",
                           "description": "Setup test - the webhook works.",
                           "color": 0x3498DB}]}
    try:
        post_webhook(url, payload)
        good("Discord accepted the test message.")
        record_webhook_verified(config_path, url)
    except Exception as exc:
        fail(f"Webhook failed: {exc}")
        if "403" in str(exc):
            note("403 usually means the webhook was deleted or regenerated.")
            note("Re-copy it from Channel Settings -> Integrations -> Webhooks.")
        elif "404" in str(exc):
            note("404 means no webhook exists at that URL any more.")


WEBHOOK_MARKER = ".webhook-verified"


def webhook_fingerprint(url):
    """Short digest of a webhook URL.

    A hash, not the URL: anyone holding the URL can post to the channel, so
    there is no reason to write a second copy of it to disk.
    """
    import hashlib
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def sink_identity(sink):
    """What makes one configured sink distinguishable from another.

    Hashed, never stored: every one of these strings is or contains a
    credential. The webhook URL *is* the authority to post; so is a bot token.
    """
    kind = (sink.get("type") or "discord").lower()
    if kind == "ntfy":
        return f"ntfy {sink.get('server', '')} {sink.get('topic', '')}"
    if kind == "telegram":
        return f"telegram {sink.get('token', '')} {sink.get('chat_id', '')}"
    return sink.get("webhook_url", "")


def record_notify_verified(config_path, sink):
    """Note a successful test so the pre-flight need not repeat it."""
    return record_webhook_verified(config_path, sink_identity(sink))


def record_webhook_verified(config_path, url):
    """Note a successful webhook test so the pre-flight need not repeat it.

    Without this, `install.sh` runs the wizard and then the pre-flight check,
    and two identical test messages land in the channel seconds apart.
    """
    if not config_path:
        return
    import time
    try:
        marker = Path(config_path).parent / WEBHOOK_MARKER
        marker.write_text(f"{webhook_fingerprint(url)} {int(time.time())}\n",
                          encoding="utf-8")
        # 0644, not 0640: the wizard runs as root but the pre-flight check runs
        # as the service account, which is not in root's group and could not
        # otherwise read this. The contents are a digest and a timestamp - no
        # secret - and /etc/timelapse is itself 0750, so only root and the
        # service group can reach the file at all.
        os.chmod(marker, 0o644)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------

def foreign_path(value):
    """True if this path was written for the other platform.

    A crude test on purpose, because only two shapes need telling apart: an
    absolute POSIX path (`/var/lib/...`) and a drive-lettered Windows one
    (`C:\\...`). Anything relative, empty or UNC is left alone, since none of
    those is evidence of the platform that wrote it.
    """
    text = str(value or "").strip()
    if not text:
        return False
    windows_shaped = len(text) > 1 and text[1] == ":"
    posix_shaped = text.startswith("/")
    return windows_shaped if not IS_WINDOWS else posix_shaped


def localise_locations(cfg):
    """Replace locations a template carried over from the other platform.

    A template is a source of **settings**, not of locations, and the two live
    in the same file. `config.example.json` is a Linux document, so on Windows
    it hands over `/var/lib/timelapse/state`, and the wizard never asks about
    state_dir: it is not a choice, it is where this platform keeps things.

    So a real Windows install wrote a config whose daemons publish their
    heartbeat to `\\var\\lib\\timelapse\\state`, which resolves against
    whatever drive happens to be current. Nothing failed, and `timelapse test`
    reported the directory missing with advice to run `sudo timelapse setup`,
    which would have written the same thing again. This project had recorded
    the trap for CI, where the wizard is run *without* a template for exactly
    this reason, and then the Windows installer was given one.

    Only the two state directories, deliberately. Frames, videos, logs, ffmpeg
    and the transfer destination are all asked about, so a wrong value from a
    template never survives the wizard; these two are the only locations
    nobody is offered.
    """
    # get(), never setdefault(). This runs on the single write path, so it sees
    # every config anything writes, and a normaliser there must correct what is
    # present and never invent what is not: the first version added empty
    # "paths" and "web" sections to configs that had neither, which its own
    # test caught by comparing the file byte for byte.
    for section, key, default in (("paths", "state_dir", STATE_DIR_DEFAULT),
                                  ("web", "state_dir", WEB_STATE_DIR_DEFAULT)):
        block = cfg.get(section)
        if isinstance(block, dict) and foreign_path(block.get(key)):
            block[key] = default
    return cfg


def default_config(template_path=None):
    if template_path and Path(template_path).exists():
        with open(template_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        # Strip every documentation key, not just the ones that existed when
        # this was written - a stale "_comment_cifs" once shipped into a live
        # config still describing a tool that had been removed.
        #
        # Every dict section, not a named list of them: the list was itself the
        # drift it was meant to prevent. Adding a "web" section to the template
        # shipped three _comment keys straight into live configs, because the
        # new section simply was not in the list.
        for key in [k for k in cfg if k.startswith("_")]:
            cfg.pop(key)
        for block in cfg.values():
            if isinstance(block, dict):
                for key in [k for k in block if k.startswith("_")]:
                    block.pop(key)
        cfg["cameras"] = []
        localise_locations(cfg)
        return cfg
    return {
        "paths": {"frames_root": "", "video_output": "", "log_dir": "",
                  "state_dir": STATE_DIR_DEFAULT,
                  "ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"},
        "capture": {"interval_seconds": 5, "timeout_seconds": 4,
                    "min_bytes": 4096, "min_free_gb": 60,
                    "log_every_n_failures": 60, "retry_within_tick": True,
                    "notify_auth_failures": True},
        "encode": {"framerate": 60, "container": "mkv", "gop": 120,
                   "av1_preset": "p6", "av1_cq": 26, "hevc_cq": 24,
                   "x264_crf": 20, "min_frames": 100,
                   "delete_frames_on_success": True, "max_backlog_days": 7},
        "transfer": {"enabled": False, "destination": "",
                     "rsync_args": ["-a", "--partial", "--remove-source-files"],
                     "delete_local_after_transfer": True},
        "discord": {"enabled": False, "webhook_url": "",
                    "username": "Timelapse Bot"},
        "cameras": [],
    }


def writable_paths(cfg):
    """Minimal set of directories systemd's ReadWritePaths must allow.

    PurePosixPath, not Path: the output goes into a systemd unit, so it is a
    POSIX path whatever platform normalises it. Same reason LINUX_STATE_DIR is
    named below rather than the platform's own default: a ReadWritePaths= line
    is a Linux artefact even when a Windows box generated it, and this is the
    function whose output install.sh consumes.
    """
    paths = [cfg["paths"][k] for k in ("frames_root", "video_output", "log_dir")
             if cfg["paths"].get(k)]
    # Both daemons publish runtime state here. It is usually /var/lib/timelapse
    # /state, a sibling of the others rather than a child, so the collapse
    # below does not absorb it and it genuinely needs naming.
    #
    # as_posix(), not str(): a configured path may arrive with backslashes,
    # which PurePosixPath below would treat as one long filename rather than a
    # path. Same reason as the docstring above.
    #
    # Deliberately not timelapse_encode.state_dir(): that function answers
    # "where does this daemon write its state on this machine", and the only
    # part of it that differs here is the fallback, which is the part that has
    # to be Linux's.
    configured = (cfg.get("paths", {}).get("state_dir") or "").strip()
    paths.append(Path(configured).as_posix() if configured else LINUX_STATE_DIR)
    t = cfg.get("transfer", {})
    dest = t.get("destination", "")
    if t.get("enabled") and dest.startswith("/"):
        paths.append(dest)
    resolved = sorted({str(PurePosixPath(p)) for p in paths})
    # Drop any path already covered by a parent in the set.
    minimal = []
    for p in resolved:
        if not any(p != q and (p == q or p.startswith(q.rstrip("/") + "/"))
                   for q in resolved):
            minimal.append(p)
    return minimal


def web_writable_paths(cfg):
    """The single directory timelapse-web.service may write to.

    Deliberately separate from writable_paths(): the web UI writes only its
    sqlite index, and handing it the frames root as well - which is what
    reusing that function would do - would give a network-facing service write
    access to every captured frame in exchange for nothing.
    """
    state = (cfg.get("web", {}).get("state_dir") or "").strip()
    return [str(PurePosixPath(state or LINUX_WEB_STATE_DIR))]


def summarise_sinks(cfg):
    """Which notification sinks are on, by name. "disabled" when none is.

    Names only. Every sink holds a credential of some kind, a webhook URL being
    the authority to post as surely as a bot token is, and a summary printed to
    a terminal and pasted into a bug report is not the place for any of them.
    """
    from timelapse_encode import notify_sinks

    kinds = [(s.get("type") or "discord").lower() for s in notify_sinks(cfg)]
    return ", ".join(kinds) if kinds else "disabled"


def summarise(cfg, out_path):
    heading("Summary")
    print(f"  {'Frames':<12}{cfg['paths']['frames_root']}")
    print(f"  {'Videos':<12}{cfg['paths']['video_output']}")
    print(f"  {'Logs':<12}{cfg['paths']['log_dir']}")
    print(f"  {'Interval':<12}{cfg['capture']['interval_seconds']}s")
    print(f"  {'Cameras':<12}{len(cfg['cameras'])}")
    for cam in cfg["cameras"]:
        print(f"                {dim('- ' + cam['name'] + ' (' + cam['method'] + ')')}")
    t = cfg["transfer"]
    print(f"  {'Transfer':<12}{t['destination'] if t.get('enabled') else 'disabled'}")
    # Every configured sink, from the same function the encoder uses to decide
    # where to send. It read cfg['discord'] directly until 0.2.0, which was the
    # legacy block alone: an operator who had just set up ntfy or Telegram was
    # shown "Discord  disabled" and nothing about what they had configured, so
    # the summary contradicted the questions immediately above it. Not a
    # Windows defect; it had been wrong on Linux since 0.1.6.
    print(f"  {'Notify':<12}{summarise_sinks(cfg)}")
    w = cfg.get("web", {})
    # Built outside the f-string: nesting an expression like this inside one
    # needs PEP 701, which is Python 3.12. This project supports 3.9.
    web_line = "disabled"
    if w.get("enabled"):
        from timelapse_encode import hostport
        web_line = "http://{}/".format(hostport(w.get("bind"), w.get("port")))
    print(f"  {'Web UI':<12}{web_line}")
    print(f"  {'Config':<12}{out_path}")


# ----------------------------------------------------------------------------
# Config backups
#
# Every write goes through write_config(), so hooking rotation here covers the
# wizard, all four --*-only sections and every camera shortcut. The one path
# that does not is `timelapse config`, which hands the file to $EDITOR; the
# wrapper calls --backup-now first for exactly that reason.
# ----------------------------------------------------------------------------

BACKUP_KEEP = 5
BACKUP_STAMP = "%Y%m%d-%H%M%S"
BACKUP_RE = re.compile(r"\.bak\.(\d{8}-\d{6})(?:-(\d+))?$")


def backup_key(path):
    """Sort key: (stamp, counter within that second).

    Parsed rather than lexical. `-10` sorts below `-2` as text, and ten config
    writes inside one second is a shell loop, not a hypothetical. The bare
    `config.json.bak` written by 0.1.1 and earlier has no stamp and sorts
    first, which is right: it is certainly older than anything written since.
    """
    m = BACKUP_RE.search(Path(path).name)
    return (m.group(1), int(m.group(2) or 0)) if m else ("", 0)


def backup_paths(out_path):
    """Every backup of this config, oldest first."""
    out = Path(out_path)
    legacy = out.name + ".bak"
    return sorted((p for p in out.parent.glob(out.name + ".bak*")
                   if p.name == legacy or BACKUP_RE.search(p.name)),
                  key=backup_key)


def backup_config(out_path, keep=BACKUP_KEEP):
    """Copy the config aside and prune to `keep`. Path, or None if there was
    nothing to copy."""
    out = Path(out_path)
    if not out.exists():
        return None
    stamp = time.strftime(BACKUP_STAMP)
    # Two writes in the same second are one `timelapse cameras -x:A -x:B`
    # away, and the second must not silently replace the first. The counter is
    # the highest already used this second plus one, never the first free
    # slot: pruning leaves holes, and refilling one hands the newest backup
    # the oldest-sorting name, which then prunes it on the spot. Measured -
    # eight writes in one second kept markers 1..5 and threw away 6.
    used = [c for s, c in map(backup_key, backup_paths(out)) if s == stamp]
    n = max(used) + 1 if used else 0
    dest = out.with_suffix(out.suffix + f".bak.{stamp}" + (f"-{n}" if n else ""))
    while dest.exists():                       # belt and braces
        n += 1
        dest = out.with_suffix(out.suffix + f".bak.{stamp}-{n}")
    try:
        # copy2, so the 0640 comes with it. The group does not: a backup is
        # root's business, and the service has no reason to read one.
        shutil.copy2(out, dest)
    except OSError as exc:
        # Never let a backup failure block the write it was protecting.
        warn(f"Could not back up {out}: {exc}")
        return None

    old = backup_paths(out)[:-keep] if keep > 0 else []
    for p in old:
        try:
            p.unlink()
        except OSError:
            pass
    if old:
        note(f"Backed up to {dest.name} ({len(old)} older one(s) removed)")
    else:
        note(f"Backed up to {dest.name}")
    return dest


def write_config(cfg, out_path, owner=None):
    """Write the config 0640, readable by the service account.

    The group matters: 0640 root:root leaves the daemons unable to read their
    own configuration, which only shows up when a service fails to start.
    install.sh used to fix this afterwards, so a standalone `timelapse setup`
    produced a config the service could not read.
    """
    from timelapse_encode import replace_atomic
    # Here as well as in default_config(), because this is the single write
    # path and that is what makes it the place a correction reaches every
    # route in: a config carried between platforms, or one written by an
    # earlier version from a Linux template, is fixed the next time anything
    # touches it rather than only when the whole wizard is re-run.
    localise_locations(cfg)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    backup_config(out)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    replace_atomic(tmp, out)
    secure_secret_file(out, owner)    # it holds camera credentials


def print_redacted_config(path):
    """The config with its credentials taken out, for pasting into bug reports.

    The whole reason this exists: "here is my config, why won't camera 3
    connect" is a thing people do, and every camera password is in that file.
    Asking them to redact by hand puts the guarantee in the least reliable
    place available, and the shape of the secret is not obvious anyway, since
    a Reolink URL *is* the credential.

    What is masked travels *inside* the dump, as a `_` key, which the schema
    already reserves for documentation. The moment this matters is the moment
    somebody pastes the text into an issue, and a warning printed next to it is
    exactly the part that does not get pasted.

    JSON on stdout, prose on stderr, so `> report.json` gets a file that still
    parses while the person at the terminal is still told to check it.
    """
    from timelapse_encode import MASK, redact_config
    cfg = load_existing_config(path)
    if cfg is None:
        return 1
    safe = {"_redacted": (
        f"Camera passwords and the Discord webhook token are replaced with "
        f"'{MASK}'. Hostnames, usernames, paths and the transfer destination "
        f"are NOT masked: they are usually what a fault report is about.")}
    safe.update(redact_config(cfg))
    print(json.dumps(safe, indent=2))
    print("\n  Read this through before posting it anywhere public. The "
          "addresses,\n  usernames and paths are still in there, because a "
          "fault report needs them.\n", file=sys.stderr)
    return 0


def load_json(path):
    """Parsed JSON, or None. For inspecting files we are not about to trust."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def describe_backup(path):
    """(when, summary). What is actually in a backup, without restoring it."""
    m = BACKUP_RE.search(path.name)
    if m:
        when = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]} " \
               f"{m.group(1)[9:11]}:{m.group(1)[11:13]}:{m.group(1)[13:]}"
    else:
        # The bare .bak from 0.1.1 and earlier carries no stamp in its name.
        try:
            when = time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(path.stat().st_mtime))
        except OSError:
            when = "unknown"
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        # Shown, not hidden: a corrupt backup is exactly what you want to know
        # about before you pick it, and it is still listed so the numbering
        # matches what a second look would show.
        return when, red(f"unreadable ({type(exc).__name__})")
    cams = cfg.get("cameras") or []
    on = sum(1 for c in cams if c.get("enabled", True))
    bits = [f"{len(cams)} camera(s), {on} enabled"]
    interval = cfg.get("capture", {}).get("interval_seconds")
    if interval:
        bits.append(f"{interval}s")
    fps = cfg.get("encode", {}).get("framerate")
    if fps:
        bits.append(f"{fps}fps")
    return when, ", ".join(bits)


def list_backups(out_path):
    """Print the numbered listing the picker uses. Newest first."""
    backups = list(reversed(backup_paths(out_path)))
    if not backups:
        note(f"No backups of {out_path} yet.")
        note("One is taken automatically before every change.")
        return []
    current = load_json(out_path)
    print()
    print(f"    {'#':>2}  {'Taken':<21}{'Size':>8}  Contents")
    for i, p in enumerate(backups, 1):
        when, summary = describe_backup(p)
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        # Parsed, not byte-compared. Two files holding the same settings can
        # differ by a trailing newline or key order, and reporting those as
        # different configs would be reporting on the formatter.
        same = current is not None and load_json(p) == current
        # Marking the identical one matters: after a restore, the newest
        # backup is the config you just replaced, and the one below it is what
        # you are running. Without this the list is six lines of near-identical
        # timestamps.
        mark = dim("  = current") if same else ""
        print(f"    {i:>2}  {when:<21}{size:>8}  {summary}{mark}")
    return backups


def restore_config(out_path, owner=None):
    """Put a backup back. Exit status.

    Deliberately does not require the current config to be readable, or to
    exist at all: "I broke it" and "it is gone" are the two reasons anybody
    runs this.
    """
    heading("Restore configuration")
    backups = list_backups(out_path)
    if not backups:
        return 1
    print()

    if AUTO or _TTY is None:
        fail("Choosing a backup needs a terminal.")
        note("List them with: timelapse restore -l")
        return 1

    n = ask_int("Restore which backup? (0 cancels)", 0, 0, len(backups))
    if n == 0:
        note("Nothing was restored.")
        return 0
    chosen = backups[n - 1]

    try:
        with open(chosen, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        fail(f"{chosen.name} cannot be read as a config: {exc}")
        note("Pick another one; the listing says which are readable.")
        return 1

    when, summary = describe_backup(chosen)
    print()
    note(f"About to restore the config from {when} ({summary}).")
    if Path(out_path).exists():
        # The restore is itself a change, so it takes a backup too. That is
        # what makes it undoable: get the wrong one and the config you just
        # replaced is now number 1 in this same list.
        note("The current config is backed up first, so this is reversible.")
    if not ask_yes(f"Overwrite {out_path}?", False):
        note("Nothing was restored.")
        return 0

    write_config(cfg, out_path, owner)
    good(f"Restored {chosen.name} -> {out_path}")
    cams = cfg.get("cameras") or []
    note(f"{len(cams)} camera(s), "
         f"{sum(1 for c in cams if c.get('enabled', True))} enabled")

    # Both daemons read the config once, at startup, so a restore that does
    # not restart them has changed a file and nothing else.
    restart_capture_if_running()
    restart_web_if_running(cfg)
    return 0


def create_directories(cfg, owner=None):
    for key in ("frames_root", "video_output", "log_dir"):
        p = Path(cfg["paths"][key])
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fail(f"Could not create {p}: {exc}")
            continue
        if owner:
            try:
                shutil.chown(p, user=owner, group=owner)
            except (OSError, LookupError):
                pass
    create_state_dir(cfg, owner)
    create_web_state_dir(cfg, owner)


def create_state_dir(cfg, owner=None):
    """The directory the daemons publish runtime state into.

    Same reasoning as create_web_state_dir() below, and the same failure if it
    is skipped: ReadWritePaths names this directory, so a unit whose
    ReadWritePaths points at something absent does not start at all, and the
    error names a mount namespace rather than a missing directory. The daemons
    cannot create it themselves either, because inside the sandbox its parent
    is read-only to them.
    """
    from timelapse_encode import state_dir

    p = state_dir(cfg)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"Could not create {p}: {exc}")
        return
    if owner:
        try:
            shutil.chown(p, user=owner, group=owner)
        except (OSError, LookupError):
            pass


def create_web_state_dir(cfg, owner=None):
    """The web UI's index directory, made here rather than by the service.

    It cannot make its own: ReadWritePaths names this directory, and a
    ReadWritePaths pointing at something that does not exist stops the unit
    from starting at all - while inside the sandbox the parent is read-only,
    so the service could not create it even if it started.

    Made even when the UI is disabled, deliberately. Someone who turns
    web.enabled on by hand and starts the unit would otherwise meet a mount
    namespace error that names neither this directory nor the setting that
    wants it. One empty directory is a cheap price for never seeing that.
    """
    web = cfg.get("web", {})
    p = Path(web.get("state_dir") or WEB_STATE_DIR_DEFAULT)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"Could not create {p}: {exc}")
        return
    if owner:
        try:
            shutil.chown(p, user=owner, group=owner)
        except (OSError, LookupError):
            pass


def summarise_notify(cfg):
    """One line per sink, with nothing secret on any of them."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import notify_sinks

    sinks = notify_sinks(cfg)
    if not sinks:
        note("No notifications configured; the nightly summary goes nowhere.")
        return
    print()
    for sink in sinks:
        kind = (sink.get("type") or "discord").lower()
        if kind == "ntfy":
            where = f"{sink.get('server', '')}/{sink.get('topic', '')}"
        elif kind == "telegram":
            # The token is the credential; the chat id is not.
            where = f"chat {sink.get('chat_id', '')}"
        else:
            where = "webhook set"
        note(f"{kind:<10}{where}")


def summarise_web(cfg):
    web = cfg.get("web", {})
    if not web.get("enabled"):
        note("Web UI disabled.")
        return
    where, why = web_library_preview(cfg)
    from timelapse_encode import hostport
    print()
    note(f"listen        http://{hostport(web.get('bind'), web.get('port'))}/")
    note(f"library       {where or '(not set)'}  ({why})")
    note(f"index         {web.get('state_dir')}")
    note(f"update check  {'on' if web.get('update_check', True) else 'off'}")


# ----------------------------------------------------------------------------

def strip_colon(value):
    """Accept -e:NAME as well as -e NAME.

    argparse hands a short option everything attached to it, so -e:Doorbell
    arrives as ":Doorbell". Nothing is lost by stripping that leading colon:
    sanitise_name() keeps only alphanumerics, '-' and '_', so no camera can be
    called ':anything'.
    """
    return value[1:] if value.startswith(":") else value


def chosen_camera_action(args):
    """(action, target) from the shortcut flags, or (None, None) for the menu."""
    for action, value in (("add", args.cam_add), ("edit", args.edit),
                          ("toggle", args.toggle), ("test", args.test),
                          ("remove", args.remove)):
        if value:
            return action, (None if value is True else value)
    return (("list", None) if args.list_only else (None, None))


def run_camera_action(cfg, args):
    """--cameras-only with a shortcut flag. Exit status."""
    action, target = chosen_camera_action(args)
    if action in CAMERA_ACTIONS_WRITING and (AUTO or _TTY is None):
        # Not a soft failure: pretending to add a camera by accepting defaults
        # would write a config entry pointing at nothing.
        fail(f"'{action}' asks questions, so it needs a terminal.")
        return 1

    changed, okay = camera_action(cfg, action, target)
    if not changed:
        print()
        return 0 if okay else 1

    heading("Writing configuration")
    write_config(cfg, args.output, args.owner)
    good(f"Updated {args.output}")
    enabled = [c for c in cfg["cameras"] if c.get("enabled", True)]
    note(f"{len(cfg['cameras'])} camera(s), {len(enabled)} enabled")
    restart_capture_if_running()
    print()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--output", default=CONFIG_PATH)
    ap.add_argument("--template", default=None,
                    help="config.example.json to start from")
    ap.add_argument("--owner", default=None,
                    help="chown created directories to this user")
    ap.add_argument("--defaults", action="store_true",
                    help="accept every default without prompting")
    ap.add_argument("--stdin", action="store_true",
                    help="read answers from stdin instead of the terminal")
    ap.add_argument("--notify-only", action="store_true",
                    help="reconfigure notifications only")
    ap.add_argument("--transfer-only", action="store_true",
                    help="reconfigure just the transfer destination")
    ap.add_argument("--cameras-only", action="store_true",
                    help="add, edit or remove cameras in an existing config")
    ap.add_argument("--web-only", action="store_true",
                    help="reconfigure just the web UI")
    ap.add_argument("--password-only", action="store_true",
                    help="set the web UI's login and nothing else")
    ap.add_argument("--disable", action="store_true",
                    help="with --password-only: remove the login entirely")
    ap.add_argument("--enable", action="store_true",
                    help="with --password-only: set one (the default anyway)")
    ap.add_argument("--restore-only", action="store_true",
                    help="restore a previous config from the backups")
    ap.add_argument("--backup-now", action="store_true",
                    help="take a config backup and print its path, then stop")
    ap.add_argument("--discover", action="store_true",
                    help="list ONVIF devices answering on this network "
                         "and exit; sends no credentials")
    ap.add_argument("--redacted", action="store_true",
                    help="print the config with its credentials masked, for "
                         "pasting into a bug report")
    # Camera shortcuts. The documented spelling is -e:2 / -e:Doorbell, which
    # reaches argparse as the value ":2" because a short option swallows
    # whatever is attached to it; strip_colon puts it back. -e 2, -e2 and
    # --edit=2 all work too, and cost nothing to allow.
    cam = ap.add_argument_group(
        "camera actions",
        "with --cameras-only, go straight to one camera instead of the menu")
    ap.add_argument("-l", "--list", action="store_true", dest="list_only",
                    help="with --cameras-only or --restore-only: list and stop")
    picked = cam.add_mutually_exclusive_group()
    picked.add_argument("-a", "--add", action="store_true", dest="cam_add",
                        help="add a camera")
    picked.add_argument("-e", "--edit", metavar="CAM", type=strip_colon,
                        help="edit CAM, by name or by its number in --list")
    picked.add_argument("-x", "--toggle", metavar="CAM", type=strip_colon,
                        help="enable CAM if disabled, disable it if enabled")
    picked.add_argument("-t", "--test", metavar="CAM", type=strip_colon,
                        help="fetch one snapshot from CAM; changes nothing")
    picked.add_argument("-r", "--remove", metavar="CAM", type=strip_colon,
                        help="remove CAM, after one confirmation")
    ap.add_argument("--print-paths", metavar="CONFIG",
                    help="print the paths systemd must be allowed to write")
    ap.add_argument("--print-web-paths", metavar="CONFIG",
                    help="print the one path the web UI must be allowed to write")
    ap.add_argument("--print-state-path", metavar="CONFIG",
                    help="print the directory the daemons publish state into")
    units = ap.add_argument_group(
        "service registration (Windows)",
        "What install.sh does with unit files, for the platform that has none. "
        "install.ps1 calls these; the Linux installer does not.")
    units.add_argument("--install-units", action="store_true",
                       help="register the capture service and the two tasks")
    units.add_argument("--remove-units", action="store_true",
                       help="deregister them again")
    units.add_argument("--restart-units", action="store_true",
                       help="restart what is running onto the new build")
    units.add_argument("--unit-status", action="store_true",
                       help="print one line per component, for scripts")
    units.add_argument("--scripts-dir", default=None,
                       help="where the scripts are installed "
                            "(default: beside this one)")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    # These only mean anything against an existing camera list. Silently
    # ignoring them would be worse than refusing: 'timelapse_setup.py -r:2'
    # reads as a removal and would instead rewrite the whole config.
    if chosen_camera_action(args) != (None, None) and not (args.cameras_only
                                                           or args.restore_only):
        ap.error("the camera actions need --cameras-only "
                 "(the 'timelapse cameras' command passes it for you)")

    # Same reasoning as the camera actions above. `timelapse setup --disable`
    # reads like a definite instruction about something; ignoring it and
    # walking the whole wizard instead would be the wrong kind of surprise.
    if (args.disable or args.enable) and not args.password_only:
        ap.error("--disable and --enable belong to 'timelapse password'")
    if args.disable and args.enable:
        ap.error("--disable and --enable are opposites; pick one")

    # Machine-readable, and the reason `timelapse config` is covered by the
    # backup rotation at all: the wrapper calls this before handing the file
    # to $EDITOR, which is the one write path that does not go through
    # write_config().
    if args.backup_now:
        made = backup_config(args.output)
        return 0 if made else 1

    # Before init_tty(): this is a filter, not a wizard. It must work under a
    # pipe, and it must never prompt.
    if args.redacted:
        return print_redacted_config(args.output)

    # Also never prompts, so it works over a pipe and needs no terminal. It
    # sends no credentials, so it needs no privileges either.
    if args.discover:
        return print_discovered()

    # Machine-readable mode used by install.sh to template the systemd units.
    if args.print_paths:
        with open(args.print_paths, encoding="utf-8") as fh:
            print(" ".join(writable_paths(json.load(fh))))
        return 0

    if args.print_web_paths:
        with open(args.print_web_paths, encoding="utf-8") as fh:
            print(" ".join(web_writable_paths(json.load(fh))))
        return 0

    if args.print_state_path:
        with open(args.print_state_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        # POSIX, and the Linux fallback, for the same reasons writable_paths()
        # uses both: this is read by install.sh to make a directory that a
        # systemd unit then names, so it is a Linux answer even when a Windows
        # box computed it. A Path stringifies to whatever the *running*
        # platform separates with, which is the wrong thing here twice over.
        configured = (cfg.get("paths", {}).get("state_dir") or "").strip()
        print(Path(configured).as_posix() if configured else LINUX_STATE_DIR)
        return 0

    # Registration, and the status that goes with it. Before init_tty() because
    # none of the three prompts: install.ps1 drives them, and an installer that
    # can be blocked on a question it cannot see is an installer that hangs.
    if args.unit_status:
        return print_unit_status()

    if args.install_units or args.remove_units or args.restart_units:
        if not is_elevated():
            fail("This changes how the machine starts, so it needs privilege.")
            note(elevation_hint())
            return 1
        scripts = args.scripts_dir or Path(__file__).resolve().parent
        if args.remove_units:
            return 0 if remove_units() else 1
        if args.restart_units:
            return 0 if restart_units() else 1
        return 0 if install_units(scripts, args.output) else 1

    init_tty(force_defaults=args.defaults, use_stdin=args.stdin)

    # A shortcut is a command, not a wizard. Announcing the setup wizard and
    # explaining how defaults work, above four lines of camera list, is noise
    # in something meant to be run from a shell prompt or a script.
    if chosen_camera_action(args) == (None, None) and not args.restore_only:
        print()
        print(bold("  ╔══════════════════════════════════════════════════════════╗"))
        print(bold("  ║              timelapse-maker  ·  setup                   ║"))
        print(bold("  ╚══════════════════════════════════════════════════════════╝"))
        print()
        note("Press Enter to accept the [default] shown for any question.")

    # Restore reads the backups, not the config, so it deliberately does not
    # load one first: "I broke it" and "it is gone" are the two reasons to run
    # this, and refusing on an unreadable config would refuse exactly then.
    if args.restore_only:
        if args.list_only:
            list_backups(args.output)
            print()
            return 0
        return restore_config(args.output, args.owner)

    # Manage cameras against an existing config. Adding a camera after the
    # initial install must not mean re-running the whole wizard, and must not
    # mean reinstalling: nothing here touches paths, so the units are unchanged.
    if args.cameras_only:
        cfg = load_existing_config(args.output)
        if cfg is None:
            return 1
        if chosen_camera_action(args) != (None, None):
            return run_camera_action(cfg, args)
        if not manage_cameras(cfg):
            print()
            note("No changes made.")
            return 0
        heading("Writing configuration")
        write_config(cfg, args.output, args.owner)
        good(f"Updated {args.output}")
        enabled = [c for c in cfg["cameras"] if c.get("enabled", True)]
        note(f"{len(cfg['cameras'])} camera(s), {len(enabled)} enabled")
        restart_capture_if_running()
        print()
        return 0

    # Re-run just the notifications. Nothing to restart afterwards: the
    # encoder is a oneshot and reads the config at the start of each run, so
    # tonight's summary already uses whatever this writes.
    if args.notify_only:
        cfg = load_existing_config(args.output)
        if cfg is None:
            return 1
        choose_notify(cfg, args.output)
        heading("Writing configuration")
        write_config(cfg, args.output, args.owner)
        good(f"Updated {args.output}")
        summarise_notify(cfg)
        print()
        return 0

    # Re-run just the transfer section against an existing config, so a share
    # can be set up after the fact without walking the whole wizard again.
    if args.transfer_only:
        cfg = load_existing_config(args.output)
        if cfg is None:
            return 1
        cfg.setdefault("transfer", default_config()["transfer"])
        choose_transfer(cfg, args.owner)
        heading("Writing configuration")
        write_config(cfg, args.output, args.owner)
        good(f"Updated {args.output}")
        t = cfg["transfer"]
        if t.get("enabled"):
            print()
            note(f"destination      {t['destination']}")
            note(f"rsync_args       {' '.join(t.get('rsync_args', []))}")
            note(f"require_mountpoint {t.get('require_mountpoint', False)}")
            if str(t.get("destination", "")).startswith("/"):
                print()
                report_readwritepaths(*sync_unit_readwritepaths(cfg))
        print()
        return 0

    # `timelapse password`. Changing a password is a thing people do in a
    # hurry, from a phone, having just been asked for one they cannot
    # remember; making them walk the web section to get there would be
    # unkind. There is deliberately no "old password" question: this needs
    # root to write the config at all, and root can already read every
    # camera password in that same file, so asking would prove nothing and
    # would lock out precisely the person entitled to fix it.
    if args.password_only:
        cfg = load_existing_config(args.output)
        if cfg is None:
            return 1
        heading("Web UI login")
        web = cfg.setdefault("web", {})
        current = (web.get("auth") or {}).get("username")

        if args.disable:
            # No prompt and no confirmation: --disable says exactly what it
            # wants, it needs no password to carry out, and it is undone by
            # running this command again. That also makes it scriptable, which
            # a prompt would not be.
            if not current:
                note("No login was set; nothing to remove.")
                print()
                return 0
            web.pop("auth", None)
            good(f"Removed the login for '{current}'.")
            warn("The pages now open to anyone who can reach the address.")
            note("`sudo timelapse password` sets one again.")
        else:
            if current:
                note(f"Currently set for '{current}'.")
            else:
                note("No login is set, so the pages open to anyone who can")
                note("reach them. This sets one.")
            if not web.get("enabled", False):
                print()
                warn("The web UI itself is switched off. This is still saved,")
                warn("and applies when you turn it on with `timelapse web`.")
            if not set_web_login(web):
                print()
                note("No changes made.")
                return 0
            print()
            note("To remove it again: `sudo timelapse password --disable`.")

        heading("Writing configuration")
        write_config(cfg, args.output, args.owner)
        good(f"Updated {args.output}")
        # Everyone is logged out by this, twice over: the sessions live in the
        # server's memory and the restart empties them, which is the behaviour
        # somebody changing a password is entitled to expect.
        restart_web_if_running(cfg)
        print()
        return 0

    # Re-run just the web section. Same reason as --transfer-only: turning the
    # UI on later must not mean walking the whole wizard, and a feature the
    # wizard never offers is one nobody finds.
    if args.web_only:
        cfg = load_existing_config(args.output)
        if cfg is None:
            return 1
        choose_web(cfg)
        heading("Writing configuration")
        create_web_state_dir(cfg, args.owner)
        write_config(cfg, args.output, args.owner)
        good(f"Updated {args.output}")
        summarise_web(cfg)
        # Before the summary would be tidier, but writing the file first means
        # a declined restart still leaves the new settings on disk for the
        # next start.
        restart_web_if_running(cfg)
        print()
        return 0

    cfg = default_config(args.template)
    disk = choose_storage(cfg)
    choose_tools(cfg)
    n_cams = choose_capture(cfg, disk)
    choose_cameras(cfg, n_cams)
    choose_transfer(cfg, args.owner)
    choose_notify(cfg, args.output)
    choose_web(cfg)

    heading("Writing configuration")
    create_directories(cfg, args.owner)
    write_config(cfg, args.output, args.owner)
    good(f"Wrote {args.output}")
    summarise(cfg, args.output)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

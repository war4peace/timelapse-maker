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
import shutil
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path, PurePosixPath

__version__ = "0.0.9"

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

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


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
# ----------------------------------------------------------------------------

PSEUDO_FS = {
    "tmpfs", "devtmpfs", "sysfs", "proc", "procfs", "cgroup", "cgroup2",
    "overlay", "overlayfs", "squashfs", "devpts", "mqueue", "hugetlbfs",
    "debugfs", "tracefs", "binfmt_misc", "configfs", "securityfs", "pstore",
    "efivarfs", "fusectl", "autofs", "rpc_pipefs", "nsfs", "ramfs", "bpf",
    "rootfs", "selinuxfs", "fuse.snapfuse", "fuse.gvfsd-fuse", "fuse.portal",
    "iso9660", "udf",
}

# Usable, but a bad idea for frames: no atomic rename guarantees across the
# wire, and 17k small writes per camera per day over a network is painful.
NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "9p", "fuse.sshfs", "ceph",
    "glusterfs", "fuse.rclone", "afs", "lustre",
}

SKIP_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/snap", "/var/snap", "/boot",
    "/var/lib/docker", "/var/lib/containers", "/mnt/wsl", "/usr/lib/wsl",
    "/mnt/wslg", "/tmp/.",
)


def _base_device(source, sys_block="/sys/block"):
    """/dev/sda1 -> sda, /dev/nvme0n1p2 -> nvme0n1, /dev/mapper/vg-lv -> dm-N.

    sys_block is injectable so the partition-stripping rules can be tested
    against a fake tree rather than whatever disks this machine happens to have.
    """
    if not source.startswith("/dev/"):
        return None
    name = source[5:]
    if name.startswith("mapper/"):
        try:
            return os.path.basename(os.readlink(source))
        except OSError:
            return None
    if Path(f"{sys_block}/{name}").exists():
        return name
    # Strip a partition suffix: sda1 -> sda, nvme0n1p2 -> nvme0n1, mmcblk0p1.
    for cut in (lambda s: s.rstrip("0123456789"),
                lambda s: s.rsplit("p", 1)[0] if "p" in s else s):
        cand = cut(name)
        if cand and Path(f"{sys_block}/{cand}").exists():
            return cand
    return None


def _is_rotational(source, sys_block="/sys/block"):
    dev = _base_device(source, sys_block)
    if not dev:
        return None
    try:
        p = Path(f"{sys_block}/{dev}/queue/rotational")
        return p.read_text().strip() == "1"
    except OSError:
        return None


def scan_filesystems(mounts_path="/proc/mounts", statvfs=None, rotational=None):
    """Real, writable, local filesystems that could hold frames.

    The three inputs are injectable so the filtering can be tested against a
    synthetic /proc/mounts without needing the machine to have the interesting
    cases (network mounts, read-only duplicates, a device mounted twice) on it.

    Resolved here rather than as default arguments: os.statvfs does not exist on
    Windows, and evaluating it at def time would make the module unimportable
    there - which matters only for running the tests off-target, but there is no
    reason to forbid that.
    """
    if statvfs is None:
        statvfs = getattr(os, "statvfs", None)
        if statvfs is None:
            return []
    if rotational is None:
        rotational = _is_rotational
    try:
        raw = Path(mounts_path).read_text().splitlines()
    except OSError:
        return []

    found = {}
    for line in raw:
        parts = line.split()
        if len(parts) < 4:
            continue
        source, target, fstype, opts = parts[0], parts[1], parts[2], parts[3]
        target = target.replace("\\040", " ").replace("\\011", "\t")

        if fstype in PSEUDO_FS or fstype in NETWORK_FS:
            continue
        if target != "/" and target.startswith(SKIP_PREFIXES):
            continue
        if "ro" in opts.split(","):
            continue
        if not source.startswith("/dev/"):
            continue
        try:
            st = statvfs(target)
        except OSError:
            continue
        if st.f_blocks == 0:
            continue

        entry = {
            "mount": target,
            "source": source,
            "fstype": fstype,
            "free": st.f_bavail * st.f_frsize,
            "total": st.f_blocks * st.f_frsize,
            "rotational": rotational(source),
        }
        # One device can be mounted repeatedly (bind mounts, WSL). Keep the
        # shortest mountpoint, which is the primary one.
        prev = found.get(source)
        if prev is None or len(target) < len(prev["mount"]):
            found[source] = entry

    disks = sorted(found.values(), key=lambda d: -d["free"])
    return disks


def recommend(disks):
    """Prefer the roomiest non-root filesystem; don't fill the OS disk."""
    if not disks:
        return None
    non_root = [d for d in disks if d["mount"] != "/"]
    if non_root and non_root[0]["free"] >= 20 * 1024 ** 3:
        return non_root[0]
    return disks[0]


def show_disks(disks, best):
    print()
    print("   " + bold(f"{'#':<3}{'Mount':<22}{'Type':<8}{'Free':>10}"
                       f"{'Total':>11}   Notes"))
    for i, d in enumerate(disks, 1):
        notes = []
        if d["rotational"] is True:
            notes.append("HDD")
        elif d["rotational"] is False:
            notes.append("SSD")
        if d["mount"] == "/":
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
        base = ask("Base directory for timelapse data", "/var/lib/timelapse")
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
        suggested = ("/var/lib/timelapse" if chosen["mount"] == "/"
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
    found = shutil.which(name)
    return found or fallback


def choose_tools(cfg):
    heading("ffmpeg")
    ffmpeg = find_binary("ffmpeg", "/usr/bin/ffmpeg")
    ffprobe = find_binary("ffprobe", "/usr/bin/ffprobe")
    cfg["paths"]["ffmpeg"] = ask("Path to ffmpeg", ffmpeg)
    cfg["paths"]["ffprobe"] = ask("Path to ffprobe", ffprobe)

    chosen, failures = detect_encoders(cfg["paths"]["ffmpeg"])

    if chosen is None:
        fail("No usable encoder at all - ffmpeg cannot encode here.")
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


def detect_encoders(ffmpeg):
    """(chosen codec or None, [(codec, ffmpeg message, hint), ...])."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from timelapse_encode import (encoder_hint, list_encoders,
                                      probe_encoder_detail)
    except ImportError:
        return None, []

    built = list_encoders(ffmpeg)
    if built is None:
        fail(f"Could not run {ffmpeg} - is the path right?")
        return None, []

    failures = []
    for codec in ("av1_nvenc", "hevc_nvenc", "libx264"):
        ok, message = probe_encoder_detail(
            ffmpeg, {"codec": codec, "args": ["-c:v", codec]})
        if ok:
            return codec, failures
        failures.append((codec, message,
                         encoder_hint(codec, message, codec in built)))
    return None, failures


def choose_capture(cfg, disk):
    heading("Capture")
    interval = ask_int("Seconds between snapshots", 5, 1, 3600)
    cfg["capture"]["interval_seconds"] = interval

    timeout = min(cfg["capture"]["timeout_seconds"], max(1, interval - 1))
    if timeout != cfg["capture"]["timeout_seconds"]:
        note(f"Fetch timeout lowered to {timeout}s to stay under the interval.")
    cfg["capture"]["timeout_seconds"] = timeout

    per_day = int(86400 / interval)
    video_secs = per_day / cfg["encode"]["framerate"]
    note(f"{per_day:,} frames/camera/day -> "
         f"{int(video_secs // 60)}:{int(video_secs % 60):02d} of video at "
         f"{cfg['encode']['framerate']}fps")

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

    cams = []
    while True:
        cam = add_one_camera(cfg, len(cams) + 1)
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


def add_one_camera(cfg, n):
    print()
    print(f"  {bold(f'Camera {n}')}")
    for i, (label, _, _, _) in enumerate(CAMERA_PRESETS, 1):
        print(f"    {i}  {label}")
    choice = ask_int("Camera type", 1, 1, len(CAMERA_PRESETS))
    label, method, auth, template = CAMERA_PRESETS[choice - 1]

    name = ask("Name (used as the folder name)", f"Camera{n}")
    name = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or f"Camera{n}"

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
        ip = ask("IP address or hostname", "192.168.1.100")
        user = pwd = ""
        if auth in ("digest", "basic") or method == "rtsp" or "{user}" in template:
            user = ask("Username", "admin")
            pwd = ask_secret("Password")
        url = template.format(ip=ip, user=quote(user), password=quote(pwd))

    cam = {"name": name, "enabled": True, "method": method, "url": url}
    if method == "http":
        cam["auth"] = auth or "none"
        if cam["auth"] in ("digest", "basic"):
            cam["username"] = user
            cam["password"] = pwd
    else:
        cam["quality"] = 2

    if ask_yes("Test this camera now?", True):
        if not test_camera(cam, cfg) and not ask_yes("Keep it anyway?", True):
            return None
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
CRED_IN_URL_RE = re.compile(r"((?:password|passwd|pwd|pass)=)[^&]*", re.I)


def sanitise_name(raw, fallback):
    """Camera names become directory names, so keep them boring."""
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_") or fallback


def redact_url(url):
    """Mask credentials carried in the query string (the Reolink shape).

    ask_secret() exists to keep passwords out of scroll-back; printing the
    camera list would hand them straight back otherwise.
    """
    return CRED_IN_URL_RE.sub(r"\1***", url)


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


def name_taken(cams, name, skip=None):
    # Case-insensitive: two cameras differing only in case would be two config
    # entries writing into two directories that differ only in case, which is a
    # trap on any case-insensitive destination the videos get copied to.
    low = name.lower()
    return any(c is not skip and str(c.get("name", "")).lower() == low
               for c in cams)


def list_cameras(cfg):
    cams = cfg.get("cameras", [])
    if not cams:
        note("No cameras configured.")
        return
    print()
    print(f"    {'#':>2}  {'Name':<14} {'On':<4}{'Type':<6}URL")
    for i, cam in enumerate(cams, 1):
        # Elide the middle, not the tail. Reolink-style URLs are identical for
        # their first 40 characters, so a plain truncation makes every camera
        # look the same - and it would hide the *** that shows the password is
        # masked, which reads as though nothing were redacted at all.
        url = redact_url(str(cam.get("url", "")))
        if len(url) > 44:
            url = url[:24] + "..." + url[-17:]
        state = "yes" if cam.get("enabled", True) else dim("no")
        print(f"    {i:>2}  {str(cam.get('name', '')):<14} {state:<4}"
              f"{str(cam.get('method', 'http')):<6}{dim(url)}")


def pick_camera(cams, verb):
    if not cams:
        fail(f"No cameras to {verb}.")
        return None
    n = ask_int(f"Which camera to {verb}? (0 cancels)", 0, 0, len(cams))
    return None if n == 0 else n - 1


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
    if ask_yes("Test it now?", True):
        test_camera(cam, cfg)
    return cam


def unit_is_active(unit):
    """True, False, or None when systemd cannot be asked at all."""
    if not shutil.which("systemctl"):
        return None
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                           timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


def restart_unit(unit, success):
    """Restart a unit and say what happened. True if it came back."""
    try:
        r = subprocess.run(["systemctl", "restart", unit], timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"Restart failed: {exc}")
        return False
    if r.returncode == 0:
        good(success)
        return True
    fail("Restart failed (are you root?).")
    note(f"See: journalctl -u {unit.split('.')[0]} -n 40")
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
            note(f"Start it with: systemctl enable --now {unit}")
        return
    print()
    if not enabled:
        # It exits 0 when disabled, so a restart is what stops it. Leaving it
        # alone would keep serving a UI the operator just turned off.
        warn("The web UI is still running with the settings it started on.")
        if ask_yes("Stop it now?", True):
            restart_unit(unit, "Web UI stopped.")
        else:
            note(f"Stop it with: systemctl stop {unit}")
        return
    note("The web UI is running on the settings it read at startup.")
    if not ask_yes("Restart it so the new settings take effect now?", True):
        warn("The running server still has the previous settings.")
        note(f"Apply them with: systemctl restart {unit}")
        return
    restart_unit(unit, "Web UI restarted on the new settings.")


def restart_capture_if_running():
    """Capture reads its config once, at startup.

    Same trap the installer had when it replaced scripts under a live service:
    editing the file changes nothing until the daemon restarts.
    """
    if not shutil.which("systemctl"):
        return
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "timelapse-capture.service"],
            timeout=15)
    except (OSError, subprocess.SubprocessError):
        return
    if active.returncode != 0:
        note("Capture is not running; the new list applies when it next starts.")
        return
    print()
    if not ask_yes("Restart capture so the change takes effect now?", True):
        warn("Capture is still using the previous camera list.")
        note("Apply it with: systemctl restart timelapse-capture.service")
        return
    try:
        r = subprocess.run(["systemctl", "restart", "timelapse-capture.service"],
                           timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"Restart failed: {exc}")
        return
    if r.returncode == 0:
        good("Capture restarted on the new camera list.")
    else:
        fail("Restart failed (are you root?).")
        note("See: journalctl -u timelapse-capture -n 40")


def manage_cameras(cfg):
    """Interactive add/edit/remove loop. True if anything changed."""
    heading("Cameras")
    if AUTO or _TTY is None:
        fail("Managing cameras needs a terminal.")
        return False

    cams = cfg.setdefault("cameras", [])
    changed = False
    while True:
        list_cameras(cfg)
        print()
        note("a add   e edit   r remove   x enable/disable   t test   q save & quit")
        action = ask("Action", "q").strip().lower()[:1]

        if action == "a":
            cam = add_one_camera(cfg, len(cams) + 1)
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
            fail(f"RTSP grab failed: {(p.stderr or '').strip()[:160]}")
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
    """Which rsync flags this destination actually accepts, or None if untested.

    On a CIFS share, `-a` implies --owner --group, which the share often cannot
    set; rsync then exits 23 and every nightly run reports a transfer failure
    even though the files arrived. Whether it happens depends on the server and
    mount options, so measure instead of guessing.
    """
    if not shutil.which("rsync"):
        return None
    import tempfile
    candidates = (["-a", "--partial"],
                  ["-rt", "--partial"],
                  ["-a", "--no-perms", "--no-owner", "--no-group", "--partial"])
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
    except OSError:
        return None

    landed = Path(dest) / probe.name
    try:
        for args in candidates:
            cmd = ["rsync"] + args + [str(probe), str(dest).rstrip("/") + "/"]
            if svcuser and shutil.which("runuser"):
                cmd = ["runuser", "-u", svcuser, "--"] + cmd
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
            except Exception:
                return None
            landed.unlink(missing_ok=True)
            if r.returncode == 0:
                return args
        return []
    finally:
        landed.unlink(missing_ok=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def choose_transfer(cfg, svcuser=None):
    heading("Transfer (optional)")
    note("After encoding, videos can be moved to a NAS or another host.")
    note("Leave this off to keep them on the local disk.")
    print()
    if not ask_yes("Send finished videos somewhere else?", False):
        cfg["transfer"]["enabled"] = False
        return
    cfg["transfer"]["enabled"] = True

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
    cred = Path("/etc/timelapse") / f"cifs-{share}.cred"
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
    """
    if not is_root():
        return False
    paths = " ".join(writable_paths(cfg))
    if not paths:
        return False
    touched = []
    for name in ("timelapse-capture.service", "timelapse-encode.service"):
        unit = Path(unitdir) / name
        if not unit.exists():
            continue
        try:
            lines = unit.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            continue
        out, changed = [], False
        for line in lines:
            if line.startswith("ReadWritePaths="):
                new = f"ReadWritePaths={paths}\n"
                changed = changed or new != line
                out.append(new)
            else:
                out.append(line)
        if changed:
            try:
                unit.write_text("".join(out), encoding="utf-8")
                touched.append(name)
            except OSError:
                pass
    if touched:
        good(f"Updated ReadWritePaths in {', '.join(touched)}")
        note(f"  {paths}")
        try:
            subprocess.run(["systemctl", "daemon-reload"],
                           capture_output=True, timeout=30)
        except Exception:
            pass
        note("Restart the encoder timer for it to take effect:")
        note("  systemctl restart timelapse-encode.timer")
    return bool(touched)


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
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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


def choose_web(cfg):
    heading("Web UI (optional)")
    note("A small read-only page: service status, and an index of your")
    note("finished videos that hands each one to VLC to play.")
    note("It changes nothing - no encoding, no camera control, no deleting.")
    print()

    cfg.setdefault("web", {})
    web = cfg["web"]
    if not ask_yes("Enable the web UI?", web.get("enabled", False)):
        web["enabled"] = False
        return
    web["enabled"] = True

    print()
    suggested = suggest_bind(web.get("bind"))
    found = host_addresses()
    if found:
        note(f"This host's addresses: {', '.join(found)}")
    note("There is no login and no HTTPS, so whoever can reach this address")
    note("can watch your videos. A LAN address is the useful answer on a")
    note("recorder you connect to from elsewhere; 127.0.0.1 restricts it to")
    note("this machine, and 0.0.0.0 accepts every interface.")
    if (web.get("bind") or "") in LOOPBACK and suggested not in LOOPBACK:
        # Never move a deliberate loopback choice without saying so out loud.
        print()
        warn(f"This is currently {web['bind']}, reachable only from this host.")
        warn(f"Accepting {suggested} below opens it to your network.")

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

    print()
    where, why = web_library_preview(cfg)
    note(f"Videos will be read from: {where or '(not set)'}")
    note(f"  because: {why}")
    if "REMOTE" in why:
        warn("That is an rsync remote spec, not a path this host can read.")
        warn("If the same files are also mounted here, give that path below.")
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
                           web.get("state_dir", "/var/lib/timelapse/web"))

    print()
    note("The Overview page can tell you when a new version is tagged, and")
    note("show what changed and the commands to upgrade.")
    note("It asks api.github.com once a day, and only while someone has the")
    note("page open. It sends no configuration, no camera names and nothing")
    note("about your videos; GitHub sees this host's IP and the version.")
    note("This is the only outbound connection the web UI ever makes.")
    web["update_check"] = ask_yes("Check GitHub for updates?",
                                  web.get("update_check", True))


def choose_discord(cfg, config_path=None):
    heading("Notifications (optional)")
    note("A nightly Discord summary: what encoded, coverage, size, failures.")
    print()
    if not ask_yes("Send a nightly summary to a Discord webhook?", False):
        cfg["discord"]["enabled"] = False
        cfg["discord"]["webhook_url"] = ""
        return
    url = ask("Webhook URL", "")
    cfg["discord"]["enabled"] = bool(url)
    cfg["discord"]["webhook_url"] = url
    if url and ask_yes("Send a test message now?", True):
        send_test_webhook(url, cfg["discord"].get("username", "Timelapse Bot"),
                          config_path)


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
        return cfg
    return {
        "paths": {"frames_root": "", "video_output": "", "log_dir": "",
                  "ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"},
        "capture": {"interval_seconds": 5, "timeout_seconds": 4,
                    "min_bytes": 4096, "min_free_gb": 60,
                    "log_every_n_failures": 60, "retry_within_tick": True},
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
    POSIX path whatever platform normalises it.
    """
    paths = [cfg["paths"][k] for k in ("frames_root", "video_output", "log_dir")
             if cfg["paths"].get(k)]
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
    return [str(PurePosixPath(state or "/var/lib/timelapse/web"))]


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
    print(f"  {'Discord':<12}{'enabled' if cfg['discord'].get('enabled') else 'disabled'}")
    w = cfg.get("web", {})
    # Built outside the f-string: nesting an expression like this inside one
    # needs PEP 701, which is Python 3.12. This project supports 3.9.
    web_line = "disabled"
    if w.get("enabled"):
        web_line = "http://{}:{}/".format(w.get("bind"), w.get("port"))
    print(f"  {'Web UI':<12}{web_line}")
    print(f"  {'Config':<12}{out_path}")


def write_config(cfg, out_path, owner=None):
    """Write the config 0640, readable by the service account.

    The group matters: 0640 root:root leaves the daemons unable to read their
    own configuration, which only shows up when a service fails to start.
    install.sh used to fix this afterwards, so a standalone `timelapse setup`
    produced a config the service could not read.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        backup = out.with_suffix(out.suffix + ".bak")
        shutil.copy2(out, backup)
        note(f"Existing config backed up to {backup}")
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, out)
    try:
        os.chmod(out, 0o640)          # it holds camera credentials
    except OSError:
        pass
    if owner:
        try:
            shutil.chown(out, group=owner)
        except (OSError, LookupError):
            pass


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
    create_web_state_dir(cfg, owner)


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
    p = Path(web.get("state_dir") or "/var/lib/timelapse/web")
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


def summarise_web(cfg):
    web = cfg.get("web", {})
    if not web.get("enabled"):
        note("Web UI disabled.")
        return
    where, why = web_library_preview(cfg)
    print()
    note(f"listen        http://{web.get('bind')}:{web.get('port')}/")
    note(f"library       {where or '(not set)'}  ({why})")
    note(f"index         {web.get('state_dir')}")
    note(f"update check  {'on' if web.get('update_check', True) else 'off'}")


# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--output", default="/etc/timelapse/config.json")
    ap.add_argument("--template", default=None,
                    help="config.example.json to start from")
    ap.add_argument("--owner", default=None,
                    help="chown created directories to this user")
    ap.add_argument("--defaults", action="store_true",
                    help="accept every default without prompting")
    ap.add_argument("--stdin", action="store_true",
                    help="read answers from stdin instead of the terminal")
    ap.add_argument("--transfer-only", action="store_true",
                    help="reconfigure just the transfer destination")
    ap.add_argument("--cameras-only", action="store_true",
                    help="add, edit or remove cameras in an existing config")
    ap.add_argument("--web-only", action="store_true",
                    help="reconfigure just the web UI")
    ap.add_argument("--print-paths", metavar="CONFIG",
                    help="print the paths systemd must be allowed to write")
    ap.add_argument("--print-web-paths", metavar="CONFIG",
                    help="print the one path the web UI must be allowed to write")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    # Machine-readable mode used by install.sh to template the systemd units.
    if args.print_paths:
        with open(args.print_paths, encoding="utf-8") as fh:
            print(" ".join(writable_paths(json.load(fh))))
        return 0

    if args.print_web_paths:
        with open(args.print_web_paths, encoding="utf-8") as fh:
            print(" ".join(web_writable_paths(json.load(fh))))
        return 0

    init_tty(force_defaults=args.defaults, use_stdin=args.stdin)

    print()
    print(bold("  ╔══════════════════════════════════════════════════════════╗"))
    print(bold("  ║              timelapse-maker  ·  setup                   ║"))
    print(bold("  ╚══════════════════════════════════════════════════════════╝"))
    print()
    note("Press Enter to accept the [default] shown for any question.")

    # Manage cameras against an existing config. Adding a camera after the
    # initial install must not mean re-running the whole wizard, and must not
    # mean reinstalling: nothing here touches paths, so the units are unchanged.
    if args.cameras_only:
        try:
            with open(args.output, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except OSError:
            fail(f"No existing config at {args.output}; run the full wizard.")
            return 1
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

    # Re-run just the transfer section against an existing config, so a share
    # can be set up after the fact without walking the whole wizard again.
    if args.transfer_only:
        try:
            with open(args.output, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except OSError:
            fail(f"No existing config at {args.output}; run the full wizard.")
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
                if not sync_unit_readwritepaths(cfg):
                    warn("Add the destination to ReadWritePaths= in "
                         "timelapse-encode.service by hand,")
                    warn("or ProtectSystem=strict will fail the write "
                         "read-only. (Run as root to do this automatically.)")
        print()
        return 0

    # Re-run just the web section. Same reason as --transfer-only: turning the
    # UI on later must not mean walking the whole wizard, and a feature the
    # wizard never offers is one nobody finds.
    if args.web_only:
        try:
            with open(args.output, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except OSError:
            fail(f"No existing config at {args.output}; run the full wizard.")
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
    choose_discord(cfg, args.output)
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

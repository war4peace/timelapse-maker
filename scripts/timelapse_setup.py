#!/usr/bin/env python3
"""
timelapse_setup.py — interactive configuration wizard.

Scans the machine's filesystems, proposes where frames and videos should live,
then walks through capture settings, cameras, transfer and notifications, and
writes a complete config.json.

Every question has a default in [brackets]; pressing Enter accepts it.

    timelapse_setup.py [--output /etc/timelapse/config.json]
                       [--defaults]        answer everything with the default
                       [--print-paths CFG] list writable paths for ReadWritePaths
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

__version__ = "0.0.3"

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
    """Prompt with an Enter-accepts default."""
    suffix = f" [{default}]" if default != "" else ""
    if AUTO or _TTY is None:
        print(f"  {question}{suffix}: {dim('(default)')}")
        return default
    while True:
        try:
            sys.stdout.write(f"  {question}{suffix}: ")
            sys.stdout.flush()
            line = _TTY.readline()
        except KeyboardInterrupt:
            print("\nAborted.")
            sys.exit(1)
        if line == "":                      # EOF
            print()
            return default
        if not _TTY.isatty():
            print()                         # piped input echoes no newline
        line = line.strip()
        if line:
            return line
        if default != "":
            return default


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
        if not ask_yes(f"Add another camera? ({len(cams)} of ~{expected})",
                       len(cams) < expected):
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


def choose_transfer(cfg):
    heading("Transfer (optional)")
    note("After encoding, videos can be moved to a NAS or another host.")
    note("Leave this off to keep them on the local disk.")
    print()
    if not ask_yes("Send finished videos somewhere else?", False):
        cfg["transfer"]["enabled"] = False
        return
    cfg["transfer"]["enabled"] = True
    note("Either a local mount path (/mnt/nas/timelapse/) or an rsync remote")
    note("spec (user@nas:/mnt/user/timelapse/). SSH keys must already work.")
    cfg["transfer"]["destination"] = ask("Destination", "/mnt/nas/timelapse/")
    if not shutil.which("rsync"):
        warn("rsync is not installed - transfers will fail until it is.")


def choose_discord(cfg):
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
        send_test_webhook(url, cfg["discord"].get("username", "Timelapse Bot"))


def send_test_webhook(url, username):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import post_webhook
    payload = {"username": username,
               "embeds": [{"title": "timelapse-maker",
                           "description": "Setup test - the webhook works.",
                           "color": 0x3498DB}]}
    try:
        post_webhook(url, payload)
        good("Discord accepted the test message.")
    except Exception as exc:
        fail(f"Webhook failed: {exc}")
        if "403" in str(exc):
            note("403 usually means the webhook was deleted or regenerated.")
            note("Re-copy it from Channel Settings -> Integrations -> Webhooks.")
        elif "404" in str(exc):
            note("404 means no webhook exists at that URL any more.")


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------

def default_config(template_path=None):
    if template_path and Path(template_path).exists():
        with open(template_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg.pop("_comment", None)
        cfg.pop("_cameras_comment", None)
        for section in ("paths", "capture", "encode", "transfer", "discord"):
            cfg.get(section, {}).pop("_comment", None)
        cfg["cameras"] = []
        return cfg
    return {
        "paths": {"frames_root": "", "video_output": "", "log_dir": "",
                  "ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"},
        "capture": {"interval_seconds": 5, "timeout_seconds": 4,
                    "min_bytes": 4096, "min_free_gb": 60,
                    "log_every_n_failures": 60},
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
    print(f"  {'Config':<12}{out_path}")


def write_config(cfg, out_path):
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
    ap.add_argument("--print-paths", metavar="CONFIG",
                    help="print the paths systemd must be allowed to write")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    # Machine-readable mode used by install.sh to template the systemd units.
    if args.print_paths:
        with open(args.print_paths, encoding="utf-8") as fh:
            print(" ".join(writable_paths(json.load(fh))))
        return 0

    init_tty(force_defaults=args.defaults, use_stdin=args.stdin)

    print()
    print(bold("  ╔══════════════════════════════════════════════════════════╗"))
    print(bold("  ║              timelapse-maker  ·  setup                   ║"))
    print(bold("  ╚══════════════════════════════════════════════════════════╝"))
    print()
    note("Press Enter to accept the [default] shown for any question.")

    cfg = default_config(args.template)
    disk = choose_storage(cfg)
    choose_tools(cfg)
    n_cams = choose_capture(cfg, disk)
    choose_cameras(cfg, n_cams)
    choose_transfer(cfg)
    choose_discord(cfg)

    heading("Writing configuration")
    create_directories(cfg, args.owner)
    write_config(cfg, args.output)
    good(f"Wrote {args.output}")
    summarise(cfg, args.output)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

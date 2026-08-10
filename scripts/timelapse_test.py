#!/usr/bin/env python3
"""
timelapse_test.py: pre-flight check. Run this before enabling anything.

Verifies, for each camera in the config:
  * the snapshot URL responds, with the right auth scheme
  * the payload is a real JPEG, its size, resolution and fetch latency
  * fetch time is comfortably under the capture interval

Then checks: available encoders, disk headroom vs. projected daily usage,
rsync destination reachability, and the Discord webhook.

Sample images are written to a temp directory so you can eyeball quality.

    ./timelapse_test.py config.json [--camera Driveway] [--no-discord]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import request as urlrequest

try:
    import requests
    from requests.auth import HTTPBasicAuth, HTTPDigestAuth
except ImportError:
    sys.exit("Missing dependency: pip install requests "
             "(or: sudo apt install python3-requests)")

__version__ = "0.1.1"

OUT = Path(os.environ.get("TIMELAPSE_TEST_DIR") or
           Path(tempfile.gettempdir()) / "timelapse-test")
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def ok(msg):    print(f"  {GREEN}PASS{RESET}  {msg}")
def bad(msg):   print(f"  {RED}FAIL{RESET}  {msg}")
def warn(msg):  print(f"  {YELLOW}WARN{RESET}  {msg}")
def info(msg):  print(f"  {DIM}....{RESET}  {msg}")


# A camera may carry its own cadence and frame rate; absent means follow the
# global. Kept as three one-line lookups rather than an import from the capture
# daemon: a `.get` with a default is not an algorithm worth sharing, and the
# pre-flight must not depend on the daemon importing cleanly to run at all.
def camera_interval(cam, cfg):
    return int(cam.get("interval_seconds") or cfg["capture"]["interval_seconds"])


def camera_timeout(cam, cfg):
    """Matches the clamp the daemon applies, so the test measures what runs."""
    return max(1, min(int(cfg["capture"]["timeout_seconds"]),
                      camera_interval(cam, cfg) - 1))


def camera_framerate(cam, cfg):
    return int(cam.get("framerate") or cfg["encode"].get("framerate", 60))


def dimensions(ffprobe, path):
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height,pix_fmt",
                            "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip()
    except Exception as exc:
        return f"probe failed: {exc}"


def test_http(cam, cfg):
    name = cam["name"]
    sess = requests.Session()
    mode = (cam.get("auth") or "none").lower()
    if mode == "digest":
        sess.auth = HTTPDigestAuth(cam.get("username"), cam.get("password"))
    elif mode == "basic":
        sess.auth = HTTPBasicAuth(cam.get("username"), cam.get("password"))

    interval = camera_interval(cam, cfg)
    timeout = camera_timeout(cam, cfg)

    t0 = time.time()
    try:
        r = sess.get(cam["url"], timeout=timeout)
    except Exception as exc:
        bad(f"{name}: request failed - {exc}")
        return None
    dt = time.time() - t0

    if r.status_code == 401:
        bad(f"{name}: HTTP 401 Unauthorized - wrong credentials, or wrong auth "
            f"scheme (config says '{mode}'; try digest/basic)")
        return None
    if r.status_code != 200:
        bad(f"{name}: HTTP {r.status_code} ({len(r.content)} bytes)")
        return None

    data = r.content
    if data[:2] != b"\xff\xd8":
        head = data[:80].decode(errors="replace").replace("\n", " ")
        bad(f"{name}: 200 OK but not a JPEG. First bytes: {head!r}")
        return None

    OUT.mkdir(parents=True, exist_ok=True)
    sample = OUT / f"{name}.jpg"
    sample.write_bytes(data)
    dims = dimensions(cfg["paths"].get("ffprobe", "ffprobe"), sample)

    ok(f"{name}: {len(data)/1024:.0f} KB, {dims}, {dt:.2f}s  -> {sample}")
    if dt > interval * 0.6:
        # Against this camera's own interval: one on a 3s cadence is in
        # trouble at 2s where one on 60s is not, and the whole point of the
        # per-camera setting is that they differ.
        warn(f"{name}: {dt:.2f}s fetch is slow relative to its {interval}s "
             f"interval; consider raising the interval for this camera")
    return len(data)


def test_rtsp(cam, cfg):
    name = cam["name"]
    OUT.mkdir(parents=True, exist_ok=True)
    sample = OUT / f"{name}.jpg"
    cmd = [cfg["paths"]["ffmpeg"], "-y", "-nostdin", "-hide_banner",
           "-loglevel", "error", "-rtsp_transport", "tcp",
           "-i", cam["url"], "-frames:v", "1", "-q:v", "2", str(sample)]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        bad(f"{name}: RTSP grab timed out after 45s")
        return None
    dt = time.time() - t0
    if p.returncode != 0 or not sample.exists():
        bad(f"{name}: RTSP grab failed - {(p.stderr or '').strip()[:200]}")
        return None
    size = sample.stat().st_size
    dims = dimensions(cfg["paths"].get("ffprobe", "ffprobe"), sample)
    ok(f"{name}: {size/1024:.0f} KB, {dims}, {dt:.1f}s  -> {sample}")
    return size


def probe_profiles(cam, cfg):
    """For ONVIF snapshot URLs, try Profile_1..Profile_4 and report the
    resolution each returns, so you can pick the full-resolution one.

    On Hikvision, Profile_1 is normally the main stream, Profile_2 the sub,
    Profile_3 the third stream, but vendors are not consistent and the URL
    gives no hint. Fetching each one and comparing is the only reliable way.
    """
    name = cam["name"]
    url = cam["url"]
    if "Profile_" not in url:
        return
    base = re.sub(r"Profile_\d+", "Profile_{}", url)

    sess = requests.Session()
    mode = (cam.get("auth") or "none").lower()
    if mode == "digest":
        sess.auth = HTTPDigestAuth(cam.get("username"), cam.get("password"))
    elif mode == "basic":
        sess.auth = HTTPBasicAuth(cam.get("username"), cam.get("password"))

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"  {name}:")
    best = (0, None)
    for n in range(1, 5):
        try:
            r = sess.get(base.format(n), timeout=cfg["capture"]["timeout_seconds"] + 4)
        except Exception as exc:
            print(f"      Profile_{n}: request failed ({type(exc).__name__})")
            continue
        if r.status_code != 200 or r.content[:2] != b"\xff\xd8":
            print(f"      Profile_{n}: HTTP {r.status_code}, not a JPEG")
            continue
        sample = OUT / f"{name}-Profile_{n}.jpg"
        sample.write_bytes(r.content)
        dims = dimensions(cfg["paths"].get("ffprobe", "ffprobe"), sample)
        m = re.match(r"(\d+),(\d+)", dims)
        px = int(m.group(1)) * int(m.group(2)) if m else 0
        if px > best[0]:
            best = (px, n)
        print(f"      Profile_{n}: {dims:<24} {len(r.content)/1024:>6.0f} KB")
    if best[1]:
        cur = re.search(r"Profile_(\d+)", url).group(1)
        verdict = "already selected" if str(best[1]) == cur else \
                  f"CHANGE the config from Profile_{cur} to Profile_{best[1]}"
        print(f"      -> highest resolution is Profile_{best[1]} ({verdict})")


def test_encoders(cfg):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import (build_candidates, encoder_hint,
                                  list_encoders, probe_encoder_detail)
    ffmpeg = cfg["paths"]["ffmpeg"]
    if not shutil.which(ffmpeg) and not Path(ffmpeg).exists():
        bad(f"ffmpeg not found at {ffmpeg}")
        return
    built = list_encoders(ffmpeg)
    found = False
    for cand in build_candidates(cfg["encode"]):
        available, message = probe_encoder_detail(ffmpeg, cand)
        if available:
            (ok if not found else info)(f"{cand['name']} available"
                                        + ("  <- will be used" if not found else ""))
            found = True
            continue
        info(f"{cand['name']} not available")
        # Say *why*. "Unknown encoder" (rebuild ffmpeg) and "No capable
        # devices" (GPU/driver) share an exit code but need opposite fixes.
        in_build = None if built is None else (cand["codec"] in built)
        hint = encoder_hint(cand["codec"], message, in_build)
        if hint:
            info(f"      {hint}")
        if message:
            info(f"      ffmpeg: {message[:140]}")
    if not found:
        bad("No usable encoder at all - see the reasons above.")


def diagnose_encoders(cfg):
    """Everything needed to work out why a hardware encoder is unavailable.

    Exists because the useful information is spread across ffmpeg's verbose
    log, its build flags, and nvidia-smi - and the one line ffmpeg prints by
    default ("No capable devices found") is the least useful of all.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import (PROBE_SIZE, encoder_hint, list_encoders,
                                  probe_encoder_detail, probe_encoder_verbose)
    ffmpeg = cfg["paths"].get("ffmpeg", "ffmpeg")

    print("\n=== ffmpeg ===")
    try:
        v = subprocess.run([ffmpeg, "-version"], capture_output=True,
                           text=True, timeout=30)
    except Exception as exc:
        bad(f"cannot run {ffmpeg}: {exc}")
        return
    banner = (v.stdout or "").splitlines()
    info(banner[0] if banner else "unknown version")
    config = " ".join(banner)
    for flag in ("--enable-nvenc", "--enable-cuda", "--enable-cuvid",
                 "--enable-ffnvcodec", "--enable-nonfree"):
        if flag in config:
            info(f"built with {flag}")
    if "--enable-nvenc" not in config and "ffnvcodec" not in config:
        warn("no NVENC flag in the build configuration; this ffmpeg may have "
             "been built without NVIDIA support")

    print("\n=== NVENC encoders in this build ===")
    built = list_encoders(ffmpeg) or set()
    for codec in ("av1_nvenc", "hevc_nvenc", "h264_nvenc"):
        (ok if codec in built else bad)(
            f"{codec} {'present' if codec in built else 'NOT COMPILED IN'}")
    if "av1_nvenc" not in built:
        info("An ffmpeg built against nv-codec-headers older than 11.1 has no")
        info("av1_nvenc at all. jellyfin-ffmpeg or a BtbN static build will.")

    print("\n=== GPU ===")
    try:
        q = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,encoder.stats.sessionCount",
             "--format=csv,noheader"], capture_output=True, text=True, timeout=30)
        if q.returncode == 0 and q.stdout.strip():
            for line in q.stdout.strip().splitlines():
                info(line.strip())
            info("nvidia-smi does not report which codecs NVENC supports; "
                 "only the probe below can tell you that")
        else:
            warn("nvidia-smi returned nothing useful")
    except FileNotFoundError:
        warn("nvidia-smi not found - no NVIDIA driver installed?")
    except Exception as exc:
        warn(f"nvidia-smi failed: {exc}")

    print(f"\n=== Probe ({PROBE_SIZE}) ===")
    for codec in ("av1_nvenc", "hevc_nvenc", "h264_nvenc", "libx264"):
        if codec not in built:
            continue
        cand = {"codec": codec, "args": ["-c:v", codec]}
        available, message = probe_encoder_detail(ffmpeg, cand)
        if available:
            ok(f"{codec} works")
            continue
        bad(f"{codec} failed")
        hint = encoder_hint(codec, message, True)
        if hint:
            info(f"  {hint}")
        # The reason ffmpeg hides at error level.
        for line in probe_encoder_verbose(ffmpeg, cand):
            info(f"  ffmpeg: {line}")

    print("\n=== If a hardware encoder you expect is missing ===")
    info("'Codec not supported' means the driver did not advertise that codec")
    info("for this GPU. Known causes, roughly in order:")
    info("  - the GPU genuinely lacks it (AV1 encode needs Ada / RTX 40+)")
    info("  - another process is holding every NVENC session; stop your NVR")
    info("    or transcoder and re-run to rule this out")
    info("  - the ffmpeg build's NVENC headers predate the codec - check the")
    info("    'Loaded Nvenc version' line above; AV1 needs 11.1 or newer")
    info("  - a container or VM exposing the GPU without full NVENC access")
    info("")
    info("A second opinion, independent of this ffmpeg:")
    info("  sudo apt install jellyfin-ffmpeg7   # /usr/lib/jellyfin-ffmpeg/ffmpeg")
    info("  or a static build from https://github.com/BtbN/FFmpeg-Builds")
    info("If a different build succeeds, it was the build; if both fail the")
    info("same way, it is the driver, the GPU or session contention.")
    info("")
    info("Falling back to hevc_nvenc is not a problem - it is fast and the")
    info("quality is fine. AV1 mainly buys smaller files.")
    print()


def test_cadence(cfg, cams):
    """What each camera will actually produce, and whether that is sane.

    Worth its own section because the two numbers behind it are the ones a
    per-camera override changes, and their consequence (how long tonight's
    video is) is not something you can read off the config.
    """
    min_frames = cfg.get("encode", {}).get("min_frames", 100)
    g_int = cfg["capture"]["interval_seconds"]
    g_fps = cfg["encode"].get("framerate", 60)
    info(f"Defaults: one frame every {g_int}s, played at {g_fps}fps")
    for cam in cams:
        name = cam["name"]
        interval, fps = camera_interval(cam, cfg), camera_framerate(cam, cfg)
        per_day = int(86400 / interval)
        secs = per_day / fps
        own = "own" if (cam.get("interval_seconds") or cam.get("framerate")) \
            else "default"
        line = (f"{name}: every {interval}s at {fps}fps -> {per_day:,} "
                f"frames/day, {int(secs // 60)}:{int(secs % 60):02d} of video "
                f"({own})")
        if per_day < min_frames:
            # The encoder SKIPs below min_frames, so this camera would produce
            # nothing at all, silently, every night. Cheap to catch here.
            bad(f"{line} - below encode.min_frames ({min_frames}), so it will "
                f"be SKIPped every night")
        elif camera_timeout(cam, cfg) < cfg["capture"]["timeout_seconds"]:
            warn(f"{line} - fetch timeout clamped to "
                 f"{camera_timeout(cam, cfg)}s to stay under the interval")
        else:
            ok(line)


def test_disk(cfg, avg_bytes, n_cameras):
    root = Path(cfg["paths"]["frames_root"])
    probe = root if root.exists() else root.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    free = shutil.disk_usage(probe).free

    # Summed per camera rather than multiplied, because they no longer share a
    # cadence. One camera at 60s and five at 5s is not six times any single
    # figure, and the multiplied version overstated it by 5x for that camera.
    enabled = [c for c in cfg.get("cameras", []) if c.get("enabled", True)]
    frames = [int(86400 / camera_interval(c, cfg)) for c in enabled]
    per_day = sum(frames)
    mixed = len(set(frames)) > 1

    info(f"{free/1024**3:.0f} GB free on {probe}")
    if avg_bytes and n_cameras and per_day:
        daily = avg_bytes * per_day
        shape = (f"{per_day:,} frames/day across {len(enabled)} cameras"
                 if mixed else
                 f"{frames[0]:,} frames/camera/day x {len(enabled)} cameras")
        info(f"Projected: {shape} @ ~{avg_bytes/1024:.0f} KB "
             f"= {daily/1024**3:.0f} GB/day")
        needed = daily * 2.2      # yesterday + today + margin
        if free < needed:
            bad(f"Need roughly {needed/1024**3:.0f} GB resident; only "
                f"{free/1024**3:.0f} GB free")
        elif free < needed * 1.5:
            warn(f"Tight: ~{needed/1024**3:.0f} GB needed vs {free/1024**3:.0f} GB free")
        else:
            ok(f"Headroom fine (~{needed/1024**3:.0f} GB needed)")


# ----------------------------------------------------------------------------
# Disk usage report (--usage)
# ----------------------------------------------------------------------------

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def scan_camera_frames(cam_dir):
    """Frame counts and bytes for one camera, by day directory.

    os.scandir rather than Path.rglob: this walks six figures of small files on
    a busy install, and scandir avoids a stat() per entry just to learn it is a
    file.
    """
    days, frames, size, stray = [], 0, 0, 0
    try:
        entries = sorted(os.scandir(cam_dir), key=lambda e: e.name)
    except OSError:
        return {"days": [], "frames": 0, "size": 0, "stray": 0}
    for d in entries:
        if not d.is_dir() or not DAY_RE.match(d.name):
            continue
        days.append(d.name)
        try:
            with os.scandir(d.path) as it:
                for f in it:
                    if not f.is_file():
                        continue
                    if f.name.lower().endswith(".jpg"):
                        frames += 1
                        try:
                            size += f.stat().st_size
                        except OSError:
                            pass
                    elif f.name.endswith(".tmp"):
                        # A capture that died between write and os.replace.
                        stray += 1
        except OSError:
            continue
    return {"days": days, "frames": frames, "size": size, "stray": stray}


def report_usage(cfg):
    root = Path(cfg["paths"]["frames_root"])
    videos = Path(cfg["paths"]["video_output"])
    enabled = {c["name"] for c in cfg["cameras"] if c.get("enabled", True)}
    known = {c["name"] for c in cfg["cameras"]}

    print(f"\n=== Frames: {root} ===")
    if not root.is_dir():
        bad(f"{root} does not exist yet.")
        return
    try:
        on_disk = sorted(e.name for e in os.scandir(root) if e.is_dir())
    except OSError as exc:
        bad(f"Cannot read {root}: {exc}")
        return

    rows, tot_f, tot_b, tot_d, orphans, stray_total = [], 0, 0, 0, [], 0
    for name in sorted(set(on_disk) | known):
        st = scan_camera_frames(root / name) if name in on_disk else None
        if st is None:
            rows.append((name, "-", "-", "-", "-", "not captured yet"))
            continue
        tag = ""
        if name not in known:
            tag, _ = "ORPHAN", orphans.append(name)
        elif name not in enabled:
            tag, _ = "disabled", orphans.append(name)
        tot_f += st["frames"]
        tot_b += st["size"]
        tot_d += len(st["days"])
        stray_total += st["stray"]
        span = (f"{st['days'][0]}..{st['days'][-1]}" if len(st["days"]) > 1
                else (st["days"][0] if st["days"] else "-"))
        rows.append((name, len(st["days"]), f"{st['frames']:,}",
                     human_bytes(st["size"]), span, tag))

    fmt = "  {:<14} {:>5} {:>10} {:>10}  {:<23} {}"
    line = lambda *a: print(fmt.format(*a).rstrip())
    line("Camera", "Days", "Frames", "Size", "Range", "")
    print("  " + "-" * 74)
    for r in rows:
        line(*r)
    print("  " + "-" * 74)
    line("total", tot_d, f"{tot_f:,}", human_bytes(tot_b), "", "")
    if tot_f:
        info(f"average frame {human_bytes(tot_b / tot_f)}")

    if orphans:
        print()
        for name in orphans:
            why = ("is not in the config at all" if name not in known
                   else "is disabled in the config")
            warn(f"'{name}' has frames on disk but {why}.")
        # This is the failure the camera wizard warns about at edit time; say it
        # here too, because that is where someone counting disk space will look.
        info("The nightly encode skips them, so those frames stay forever.")
        info("Re-enable the camera, or encode a day with: "
             "timelapse encode --date YYYY-MM-DD")
    if stray_total:
        print()
        warn(f"{stray_total} leftover .tmp file(s) from interrupted writes.")
        info("Harmless, and not counted above; they are overwritten in place.")

    print(f"\n=== Videos: {videos} ===")
    if not videos.is_dir():
        info("no video directory yet")
    else:
        vids = [p for p in videos.rglob("*") if p.is_file()]
        vb = sum(p.stat().st_size for p in vids)
        print(f"  {len(vids)} file(s), {human_bytes(vb)}")

    print("\n=== Disk ===")
    seen = {}
    for label, path in (("frames", root), ("videos", videos)):
        probe = path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            du = shutil.disk_usage(probe)
        except OSError:
            continue
        key = (du.total, du.free)
        if key in seen:
            info(f"{label:<7} {probe} (same filesystem as {seen[key]})")
            continue
        seen[key] = label
        pct = 100.0 * (du.total - du.free) / du.total if du.total else 0
        info(f"{label:<7} {probe}: {human_bytes(du.free)} free of "
             f"{human_bytes(du.total)} ({pct:.0f}% used)")
    print()


def test_transfer(cfg):
    t = cfg.get("transfer", {})
    if not t.get("enabled"):
        info("transfer disabled in config")
        return
    dest = t["destination"]
    if ":" in dest and not dest.startswith("/"):
        host = dest.split(":")[0]
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                            host, "true"], capture_output=True, text=True)
        if r.returncode == 0:
            ok(f"SSH to {host} works with key auth")
        else:
            bad(f"SSH to {host} failed: {(r.stderr or '').strip()[:160]}")
        return

    p = Path(dest)
    if not p.is_dir():
        bad(f"{dest} is not a directory (is the share mounted?)")
        return
    try:
        with tempfile.NamedTemporaryFile(dir=p, prefix=".tl-write-test-"):
            pass
        ok(f"{dest} exists and is writable")
    except Exception as exc:
        bad(f"{dest} is not writable: {exc}")
        return

    # A destination that is a plain local directory rather than a mount is the
    # dangerous case: rsync succeeds, fills the local disk, and
    # --remove-source-files then deletes the originals.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import nearest_mountpoint
    mp = nearest_mountpoint(dest)
    if str(mp) == "/":
        if t.get("require_mountpoint"):
            bad(f"{dest} is not on a mounted filesystem, and "
                f"require_mountpoint is set - transfers will be refused")
        else:
            warn(f"{dest} is on the root filesystem, not a mount.")
            info("If this should be a NAS share, it is not mounted right now,")
            info("and a transfer would fill the local disk instead. Consider")
            info('setting "require_mountpoint": true in the transfer block.')
    else:
        ok(f"backed by a mount at {mp}")

    # rsync -a implies -o -g, which CIFS cannot honour; it exits 23 and the
    # nightly run reports a transfer failure even though the files arrived.
    args = t.get("rsync_args", [])
    fstype = _fstype(mp)
    if fstype in ("cifs", "smb3", "nfs", "nfs4") and any(
            a == "-a" or (a.startswith("-") and not a.startswith("--")
                          and "a" in a) for a in args):
        warn(f"destination is {fstype} and rsync_args uses -a")
        info("-a implies --owner --group, which this filesystem cannot set;")
        info("rsync will exit 23 every night. Use -rt instead, or add")
        info("--no-owner --no-group --no-perms.")


def _fstype(mountpoint):
    """Filesystem type of a mount point, or '' if it cannot be determined."""
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] == str(mountpoint):
                return parts[2]
    except OSError:
        pass
    return ""


WEBHOOK_MARKER = ".webhook-verified"
WEBHOOK_MARKER_TTL = 900          # 15 minutes


def webhook_verified_age(config_path, url):
    """Seconds since the wizard verified this exact webhook, else 0.

    install.sh runs the wizard and then this checker, so without it two
    identical test messages land in the channel seconds apart. The marker
    holds a digest rather than the URL - the URL is a posting credential.
    """
    import hashlib
    import time
    try:
        marker = Path(config_path).parent / WEBHOOK_MARKER
        digest, stamp = marker.read_text(encoding="utf-8").split()
    except (OSError, ValueError):
        return 0
    if digest != hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]:
        return 0                  # a different webhook; test it properly
    age = time.time() - int(stamp)
    return age if 0 <= age <= WEBHOOK_MARKER_TTL else 0


def test_discord(cfg, config_path=None, force=False):
    d = cfg.get("discord", {})
    if not d.get("enabled") or not d.get("webhook_url"):
        info("Discord disabled or no webhook configured")
        return

    if not force and config_path:
        age = webhook_verified_age(config_path, d["webhook_url"])
        if age:
            ok(f"Discord webhook verified during setup {int(age)}s ago")
            info("not sending a second test message; --force-discord to re-send")
            return
    payload = {"username": d.get("username", "Timelapse Bot"),
               "embeds": [{"title": "Timelapse pre-flight test",
                           "description": "If you can read this, the webhook works.",
                           "color": 0x3498DB}]}
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from timelapse_encode import post_webhook
    try:
        post_webhook(d["webhook_url"], payload)
        ok("Discord webhook accepted the test message")
    except Exception as exc:
        bad(f"Discord webhook failed: {exc}")
        if "403" in str(exc):
            info("403 usually means the webhook URL was deleted or regenerated;")
            info("check it in Discord under Channel Settings -> Integrations.")
        elif "404" in str(exc):
            info("404 means the webhook no longer exists at that URL.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("config", nargs="?", default="/etc/timelapse/config.json")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--camera", help="test only this camera")
    ap.add_argument("--no-discord", action="store_true",
                    help="skip the webhook check entirely")
    ap.add_argument("--force-discord", action="store_true",
                    help="send a test message even if setup just verified it")
    ap.add_argument("--probe-profiles", action="store_true",
                    help="for ONVIF snapshot URLs, compare Profile_1..4 resolutions")
    ap.add_argument("--encoders", action="store_true",
                    help="diagnose why a hardware encoder is unavailable")
    ap.add_argument("--usage", action="store_true",
                    help="report frames and disk usage per camera")
    args = ap.parse_args()

    from timelapse_encode import load_config
    cfg = load_config(args.config)

    cams = [c for c in cfg["cameras"] if c.get("enabled", True)]
    if args.camera:
        cams = [c for c in cams if c["name"].lower() == args.camera.lower()]

    if args.usage:
        report_usage(cfg)
        return

    if args.encoders:
        diagnose_encoders(cfg)
        return

    if args.probe_profiles:
        onvif = [c for c in cams if "Profile_" in c.get("url", "")]
        print(f"\n=== ONVIF profile comparison ({len(onvif)} camera(s)) ===")
        for cam in onvif:
            probe_profiles(cam, cfg)
        print(f"\nSample images: {OUT}\n")
        return

    print(f"\n=== Cameras ({len(cams)} enabled) ===")
    sizes = []
    for cam in cams:
        method = (cam.get("method") or "http").lower()
        got = test_http(cam, cfg) if method == "http" else test_rtsp(cam, cfg)
        if got:
            sizes.append(got)

    print("\n=== Cadence ===")
    test_cadence(cfg, cams)

    print("\n=== Encoders ===")
    test_encoders(cfg)

    print("\n=== Disk ===")
    avg = sum(sizes) / len(sizes) if sizes else 0
    test_disk(cfg, avg, len(cams))

    print("\n=== Transfer destination ===")
    test_transfer(cfg)

    if not args.no_discord:
        print("\n=== Discord ===")
        test_discord(cfg, args.config, args.force_discord)

    print(f"\nSample images: {OUT}\n")


if __name__ == "__main__":
    main()

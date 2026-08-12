#!/usr/bin/env python3
"""
smoke_test.py: end-to-end check of the encode pipeline against synthetic frames.

Builds a throwaway frames tree that looks exactly like a real capture day,
runs timelapse_encode.py over it, and asserts the things that have actually
broken before: bad frames rejected, correct duration, correct colour tagging,
frames cleaned up afterwards.

Needs ffmpeg and ffprobe on PATH (override with FFMPEG / FFPROBE). No cameras,
no network, no GPU - it falls back to libx264 if NVENC is unavailable.

    python3 tests/smoke_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENCODE = REPO / "scripts" / "timelapse_encode.py"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

GOOD_FRAMES = 150          # must exceed encode.min_frames
INTERVAL = 5
FPS = 30

failures = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}"
          + (f"  ({detail})" if detail else ""))
    if not condition:
        failures.append(label)


def ffprobe_field(path, entry):
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "v:0",
         "-show_entries", entry, "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


def build_frames(day_dir):
    """One JPEG per INTERVAL seconds, named HHMMSS.jpg like a real capture."""
    day_dir.mkdir(parents=True, exist_ok=True)
    staging = day_dir.parent / "_staging"
    staging.mkdir(exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=1",
         "-frames:v", str(GOOD_FRAMES), "-q:v", "5",
         str(staging / "src_%06d.jpg")], check=True, timeout=180)

    for i, src in enumerate(sorted(staging.glob("src_*.jpg"))):
        t = i * INTERVAL
        src.replace(day_dir / f"{t//3600:02d}{(t%3600)//60:02d}{t%60:02d}.jpg")
    staging.rmdir()

    # Two frames the encoder must reject, matching the two real-world modes:
    # a file below min_bytes, and a file with a non-JPEG header.
    (day_dir / "235950.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    (day_dir / "235955.jpg").write_bytes(b"NOT A JPEG" + b"\x00" * 8000)


def main():
    for tool in (FFMPEG, FFPROBE):
        try:
            subprocess.run([tool, "-version"], capture_output=True, timeout=30)
        except Exception:
            sys.exit(f"{tool} not found - install ffmpeg or set FFMPEG/FFPROBE")

    tmp = Path(tempfile.mkdtemp(prefix="timelapse-smoke-"))
    print(f"\nWorkspace: {tmp}\n")

    frames_root = tmp / "frames"
    videos = tmp / "videos"
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    day_dir = frames_root / "TestCam" / yesterday

    print("Building synthetic capture day...")
    build_frames(day_dir)

    cfg = {
        "paths": {"frames_root": str(frames_root), "video_output": str(videos),
                  "log_dir": str(tmp / "logs"), "ffmpeg": FFMPEG, "ffprobe": FFPROBE},
        "capture": {"interval_seconds": INTERVAL, "timeout_seconds": 4,
                    "min_bytes": 4096, "min_free_gb": 0},
        "encode": {"framerate": FPS, "container": "mkv", "gop": 60,
                   "av1_cq": 30, "hevc_cq": 28, "x264_crf": 28,
                   "min_frames": 100, "delete_frames_on_success": True,
                   "max_backlog_days": 7},
        "transfer": {"enabled": False},
        "discord": {"enabled": False},
        "cameras": [{"name": "TestCam", "enabled": True, "method": "http",
                     "url": "http://192.0.2.1/snapshot"}],
    }
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    print("Running encoder...\n")
    proc = subprocess.run([sys.executable, str(ENCODE), str(cfg_path)],
                          capture_output=True, text=True, timeout=900)
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr)

    print("Assertions:")
    check("encoder exits 0", proc.returncode == 0, f"rc={proc.returncode}")

    out = videos / f"TestCam.{yesterday.replace('-', '')}.mkv"
    check("video produced", out.exists(), out.name)
    if not out.exists():
        return finish(tmp)

    check("video is non-trivial", out.stat().st_size > 1024,
          f"{out.stat().st_size} B")

    # 2 bad frames rejected, GOOD_FRAMES kept -> exact expected duration.
    expected = GOOD_FRAMES / FPS
    try:
        dur = float(ffprobe_field(out, "format=duration") or
                    ffprobe_field(out, "stream=duration"))
    except ValueError:
        dur = -1.0
    check("duration matches good frames only", abs(dur - expected) < 0.2,
          f"{dur:.2f}s, expected {expected:.2f}s")

    check("colour range tagged limited",
          ffprobe_field(out, "stream=color_range") == "tv")
    check("colourspace tagged bt709",
          ffprobe_field(out, "stream=color_space") == "bt709")
    check("pixel format is yuv420p",
          ffprobe_field(out, "stream=pix_fmt") == "yuv420p")
    check("frames deleted after success", not day_dir.exists())

    return second_night(tmp, cfg, cfg_path, videos)


def second_night(tmp, cfg, cfg_path, videos):
    """The defect fixed in 0.1.6: with frames kept, does it encode twice?

    Deleting the frames used to be the only thing stopping a second encode, so
    this is the configuration where a regression would hide. Worth the extra
    ffmpeg run: the unit tests can only assert about the marker, not about
    whether a real run leaves the video alone.
    """
    frames_root = Path(cfg["paths"]["frames_root"])
    day = (date.today() - timedelta(days=2)).isoformat()
    day_dir = frames_root / "TestCam" / day

    print("\nSecond night, with delete_frames_on_success off...")
    build_frames(day_dir)
    cfg["encode"]["delete_frames_on_success"] = False
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def run():
        p = subprocess.run([sys.executable, str(ENCODE), str(cfg_path)],
                           capture_output=True, text=True, timeout=900)
        return p

    first = run()
    out = videos / f"TestCam.{day.replace('-', '')}.mkv"
    print("Assertions:")
    check("kept-frames run exits 0", first.returncode == 0,
          f"rc={first.returncode}")
    check("video produced", out.exists(), out.name)
    if not out.exists():
        print(first.stdout, first.stderr)
        return finish(tmp)
    check("frames kept", day_dir.is_dir())
    check("day marked encoded", (day_dir / ".encoded.json").is_file())

    stamp, size = out.stat().st_mtime_ns, out.stat().st_size
    os.utime(out, ns=(stamp - 2_000_000_000, stamp - 2_000_000_000))
    stamp = out.stat().st_mtime_ns

    second = run()
    check("re-run exits 0", second.returncode == 0, f"rc={second.returncode}")
    check("re-run encoded nothing", "Nothing to process" in second.stdout)
    check("re-run said why", "already encoded" in second.stdout)
    check("video untouched", out.stat().st_mtime_ns == stamp
          and out.stat().st_size == size)

    forced = subprocess.run(
        [sys.executable, str(ENCODE), str(cfg_path), "--force"],
        capture_output=True, text=True, timeout=900)
    check("--force encodes it again", forced.returncode == 0
          and out.stat().st_mtime_ns != stamp, f"rc={forced.returncode}")

    return finish(tmp)


def finish(tmp):
    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s) - {', '.join(failures)}")
        print(f"Workspace kept for inspection: {tmp}\n")
        return 1
    print("All checks passed.")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

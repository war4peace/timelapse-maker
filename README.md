# timelapse-maker

Unattended daily timelapses from IP cameras. Pulls a full-resolution snapshot
from each camera on a fixed interval, encodes each finished day into one video
per camera overnight, and optionally ships the results to a NAS and posts a
summary to Discord.

It is a replacement for the timelapse features built into NVR software, which
tend to record from the substream and give you low-resolution video. This talks
to the cameras directly and takes whatever the main stream offers.

**Status: 0.0.1 — first public release.** Working and in daily use, but the
configuration format is not yet frozen. See [CHANGELOG.md](CHANGELOG.md).

---

## What it does

| Piece | Role | Runs as |
|---|---|---|
| `timelapse_capture.py` | One full-quality JPEG per camera every N seconds | systemd service, always on |
| `timelapse_encode.py` | Encodes finished days to AV1, notifies, transfers | systemd timer, 00:05 |
| `timelapse_test.py` | Pre-flight checker — run before enabling anything | manually |

The two daemons never talk to each other. They share only a directory layout,
so either can be stopped, replaced or rewritten without touching the other.

- **NVENC AV1**, falling back to HEVC then x264 if the GPU or ffmpeg build
  can't do it. No silent failure.
- **Drift-free capture.** Threads wake on absolute wall-clock boundaries, so
  fetch latency never accumulates.
- **Atomic frame writes**, so a half-written JPEG never exists on disk.
- **Disk guard** pauses capture before filling the disk, and resumes with
  hysteresis.
- **Backlog recovery.** Machine off for three days? The next run encodes all
  three, oldest first.
- **Failure isolation.** One dead camera never affects the others; a failed
  transfer never turns a good encode into a failed run.
- **Correct colour.** JPEGs are full-range; the pipeline converts to
  limited-range BT.709 and tags it, so output isn't washed out or crushed
  depending on the player.

## Requirements

- Linux with systemd (developed on Ubuntu Server; nothing is distro-specific)
- Python 3.9+ and `requests`
- `ffmpeg` / `ffprobe` — with NVENC if you want AV1 or HEVC hardware encoding
- `rsync`, only if you enable transfer
- Cameras exposing an HTTP snapshot URL (Dahua, Hikvision/ONVIF, Reolink and
  similar) or, failing that, RTSP

Roughly **17,280 frames per camera per day** at the default 5s interval, which
is 4:48 of video at 60fps. Budget disk accordingly — at ~600 KB per 1440p
snapshot that is ~10 GB per camera per day, resident for up to two days.
`timelapse_test.py` computes the real figure from your own cameras.

## Quick start

```bash
git clone https://github.com/war4peace/timelapse-maker.git
cd timelapse-maker

sudo apt install ffmpeg python3-requests rsync

# 1. Configure
cp config/config.example.json config/config.json
$EDITOR config/config.json          # camera URLs, credentials, paths

# 2. Verify before enabling anything
python3 scripts/timelapse_test.py config/config.json --probe-profiles
python3 scripts/timelapse_test.py config/config.json
```

Fix everything the test reports red, then follow
**[docs/install.md](docs/install.md)** for the full system install.

> `config/config.json` is gitignored — it holds camera passwords and your
> webhook URL. Keep it that way, and `chmod 640` the deployed copy.

## Documentation

| Document | For |
|---|---|
| [docs/install.md](docs/install.md) | Installing, configuring, operating, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | How it is built and why — read before modifying |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Reporting issues, sending patches |

## Known limitations

- **DST fall-back** — local time repeats an hour, so `HHMMSS` filenames collide.
  HTTP cameras get a `-1` suffix and keep everything; the RTSP path overwrites
  that hour. Video length varies on DST days.
- **PTZ cameras** jump-cut between presets in the finished video.
- **Cameras are polled independently**, so frames are not synchronised to the
  same instant across cameras.
- **A frozen-but-reachable camera** produces a full frame count and a static
  video. The tell is a suspiciously small output file.
- **No unit test suite.** See §10 of the architecture doc for what was verified
  and how.

## License

[MIT](LICENSE).

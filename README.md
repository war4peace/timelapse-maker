# timelapse-maker

> [!WARNING]
> ## ⚠️ EXPERIMENTAL — IN DEVELOPMENT
>
> **Version 0.0.1.** This is early software that has run on exactly one machine.
> It has not been tested across different distributions, camera makes, GPUs or
> disk layouts, and it almost certainly has rough edges nobody has hit yet.
>
> - The **configuration format may change without warning** between versions.
> - The installer creates a system account, writes to `/opt`, `/etc` and
>   `/etc/systemd/system`, and enables services. **Read
>   [`install.sh`](install.sh) before running it as root.**
> - Capture writes tens of thousands of files a day. Point it at a disk you
>   are willing to fill, and run `timelapse test` first — it projects real
>   usage from your actual cameras.
> - There is no upgrade path between versions yet, and no security review.
>
> Use it if you want to experiment or help develop it. Don't put it anywhere
> that matters yet. **No warranty** — see [LICENSE](LICENSE).

Unattended daily timelapses from IP cameras. Pulls a full-resolution snapshot
from each camera on a fixed interval, encodes each finished day into one video
per camera overnight, and optionally ships the results to a NAS and posts a
summary to Discord.

It is a replacement for the timelapse features built into NVR software, which
tend to record from the substream and give you low-resolution video. This talks
to the cameras directly and takes whatever the main stream offers.

Bug reports and camera compatibility reports are genuinely useful at this stage
— see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## What it does

| Piece | Role | Runs as |
|---|---|---|
| `timelapse_capture.py` | One full-quality JPEG per camera every N seconds | systemd service, always on |
| `timelapse_encode.py` | Encodes finished days to AV1, notifies, transfers | systemd timer, 00:05 |
| `timelapse_test.py` | Pre-flight checker — run before enabling anything | manually |
| `timelapse_setup.py` | Storage-aware configuration wizard | installer, or `timelapse setup` |

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

## Install

```bash
curl -sL https://raw.githubusercontent.com/war4peace/timelapse-maker/main/install.sh -o install_timelapse.sh
sudo bash install_timelapse.sh
rm install_timelapse.sh
```

That installs the dependencies, the scripts and the systemd units, then runs a
setup wizard. The wizard scans your disks and proposes where to put the frames:

```
   #  Mount                 Type          Free      Total   Notes
   1  /mnt/storage          ext4      683.2 GB   916.0 GB   SSD            <- recommended
   2  /mnt/hdd              xfs         1.7 TB     3.6 TB   HDD
   3  /                     ext4      858.0 GB   932.0 GB   SSD, OS disk

  Which filesystem should hold the frames? [1]:
```

Every question has a default in brackets — **press Enter to accept it**. The
wizard then works out the disk budget for your camera count and interval, walks
you through adding cameras (testing each one live, and reporting the resolution
it got back), and writes `/etc/timelapse/config.json`.

It also derives systemd's `ReadWritePaths` from the storage you chose. That is
the single most common way a hand-install fails: the units run with
`ProtectSystem=strict`, so a frames directory that isn't listed produces a
baffling read-only error at 3am.

Prefer to read before running as root? That's the sensible instinct — the
download and the run are separate steps above precisely so you can inspect
[install.sh](install.sh) in between.

<details>
<summary>Other install options</summary>

```bash
sudo bash install_timelapse.sh --unattended   # no questions, sane defaults
sudo bash install_timelapse.sh --no-wizard    # install files only
sudo bash install_timelapse.sh --ref v0.0.1   # pin to a tag
sudo bash install_timelapse.sh --uninstall    # remove; captured data is kept
```

From a clone, which uses your checkout instead of downloading:

```bash
git clone https://github.com/war4peace/timelapse-maker.git
cd timelapse-maker && sudo ./install.sh
```

For a fully manual install, see [docs/install.md](docs/install.md).
</details>

After installing, a `timelapse` command wraps the common operations:

```
timelapse status | logs | test | encode | config | setup
```

Run `timelapse test` before enabling anything — it fetches one snapshot per
camera and reports size, resolution, latency and auth result, probes the
encoders, and projects real disk usage from your actual snapshot sizes.

> `/etc/timelapse/config.json` holds camera passwords and your webhook URL. The
> installer sets it to mode `640`, and `config/config.json` is gitignored.

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
- **Thin test coverage.** There is one end-to-end encode test
  (`python3 tests/smoke_test.py`) and no unit tests. See §9 of the architecture
  doc for what else was verified, and how.

## License

[MIT](LICENSE).

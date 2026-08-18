# timelapse-maker

> [!WARNING]
> ## ⚠️ EXPERIMENTAL: IN DEVELOPMENT
>
> **Version 0.2.0.** This is early software that has run on exactly one machine.
> It has not been tested across different distributions, camera makes, GPUs or
> disk layouts, and it almost certainly has rough edges nobody has hit yet.
>
> - The **configuration format may change without warning** between versions.
> - The installer creates a system account, writes to `/opt`, `/etc` and
>   `/etc/systemd/system`, and enables services. **Read
>   [`install.sh`](install.sh) before running it as root.**
> - Capture writes tens of thousands of files a day. Point it at a disk you
>   are willing to fill, and run `timelapse test` first; it projects real
>   usage from your actual cameras.
> - Upgrading is `sudo timelapse update`, which keeps your configuration.
>   There has been no security review.
>
> Use it if you want to experiment or help develop it. Don't put it anywhere
> that matters yet. **No warranty**; see [LICENSE](LICENSE).

Unattended daily timelapses from IP cameras. Pulls a full-resolution snapshot
from each camera on a fixed interval, encodes each finished day into one video
per camera overnight, and optionally ships the results to a NAS and posts a
nightly summary to Discord, ntfy or Telegram.

It exists because NVR timelapse features are generally built around *clips*,
not around one contiguous file per camera per day. Agent DVR, for instance, is
perfectly capable of pulling full-resolution snapshots and producing good
timelapses, but a camera reboot, or an ONVIF setting change, interrupts the
recording and it resumes into a *new* file. A day ends up as several fragments
rather than one video. There is also no built-in way to ship the results to
another machine.

This handles those specifically: capture is decoupled from the cameras' state,
so a camera that drops out simply contributes fewer frames to the same day's
file instead of splitting it, and finished videos are rsynced wherever you want
them.

Bug reports and camera compatibility reports are genuinely useful at this stage;
see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## What it does

| Piece | Role | Runs as |
|---|---|---|
| `timelapse_capture.py` | One full-quality JPEG per camera every N seconds | systemd service, always on |
| `timelapse_encode.py` | Encodes finished days to AV1, notifies, transfers | systemd timer, 00:05 |
| `timelapse_test.py` | Pre-flight checker, run before enabling anything | manually |
| `timelapse_setup.py` | Storage-aware configuration wizard | installer, or `timelapse setup` |
| `timelapse_web.py` | Optional read-only web UI: status, video index, hands playback to VLC | systemd service, off by default |
| `timelapse_update.py` | Asks GitHub what the latest release is, and installs it | `timelapse update`, and the web UI's version panel |
| `timelapse_platform.py` | Where the config and state live, and how services are asked about | imported by all of the above |

Day to day you drive it through one wrapper; no reinstall, no hand-edited JSON:

| | | |
|---|---|---|
| `timelapse status` | Are the service and timer healthy, when does the next encode fire | |
| `timelapse logs` | Follow the capture journal live | |
| `timelapse version` | What is installed, and whether the daemon is still running an older build | |
| `timelapse usage` | Frames, bytes and date range per camera, and which folders nothing will ever encode | **sudo** |
| `timelapse test` | Pre-flight: every camera, the encoders, disk, transfer, notifications | **sudo** |
| `timelapse cameras` | Add, edit, remove or disable a camera, set its own interval and frame rate, then restart capture. `-l` lists; `-a`, `-e:NAME`, `-x:NAME`, `-t:NAME`, `-r:NAME` skip the menu | **sudo** |
| `timelapse transfer` | Reconfigure the destination, mounting an SMB share if needed | **sudo** |
| `timelapse notify` | Where the nightly summary goes: Discord, ntfy, Telegram, any combination, with a test message for each | **sudo** |
| `timelapse web` | Turn the read-only web UI on or off, set its address, library path and whether it asks for a login | **sudo** |
| `timelapse password` | Set or change the web UI's login; `--disable` removes it. Never asks for the old one: this needs root, and root can read the config anyway | **sudo** |
| `timelapse encode` | Run tonight's encode now | **sudo** |
| `timelapse setup` · `config` | Full wizard · edit the JSON | **sudo** |
| `timelapse restore` | Put back an earlier config; one is kept before every change, five deep | **sudo** |
| `timelapse update` | Check GitHub for a new release and install it, keeping your config | **sudo** (`--check` does not) |

**Anything that reads or writes the config needs `sudo`.**
`/etc/timelapse/config.json` is mode `640`, owned `root:timelapse`, because it
holds your camera passwords. Without it the command tells you so and stops; it
does not half-run.

`status`, `logs`, `version` and `timelapse update --check` need nothing. For `logs` to show more than your
own messages you must be in the `adm` or `systemd-journal` group, which is
usually already true of the account that installed this.

Read-only commands (`test`, `usage`) also work if you add yourself to the
`timelapse` group instead. The ones that *write* the config still need root,
since mode `640` grants the group read but not write.

Full descriptions in [docs/install.md](docs/install.md#8-day-to-day-the-timelapse-command).

The two daemons never talk to each other. They share only a directory layout,
so either can be stopped, replaced or rewritten without touching the other.

- **One file per camera per day, always.** Capture keeps no session state: each
  frame is an independent fetch named after its wall-clock time. A camera that
  reboots, or that you reconfigure over ONVIF mid-afternoon, just contributes
  fewer frames to the same day. It never splits the output into fragments.
- **NVENC AV1**, falling back to HEVC then x264 if the GPU or ffmpeg build
  can't do it. No silent failure.
- **Automated hand-off.** Finished videos rsync to a NAS or another host, with
  a guard that refuses to transfer if the share isn't actually mounted.
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
- **Answers "are my cameras actually working?"** The web UI shows a row per
  camera with the time its last frame landed, its cadence, its frame count and
  its failures, plus what last night's encode produced. `systemctl` cannot
  answer this: a capture daemon whose cameras are all refusing connections is
  "running", and so is one that has paused itself because the disk filled up.
  The daemons publish it as plain versioned JSON, so anything else you want to
  read it with can.
- **Optional read-only web UI.** Service status, an index of your finished
  videos by camera and by day, and a *Play* link that hands each one to VLC,
  including one playlist per day, so reviewing a day means opening a single
  file instead of one per camera. It reads the destination you already keep
  your timelapses in, recognising the naming conventions of whatever tool came
  before. Off by default, binds to localhost, and allowed to write exactly one
  directory: its own index. An **optional login** can be put on the pages;
  read what it is and is not below before relying on it. See
  [docs/install.md §10](docs/install.md#10-the-web-ui).
- **A door lock, not a safe.** The web UI's login keeps the household, or a
  guest on your wifi, out of the status page and the video index. That is all
  it is for, and the design says so out loud rather than implying more: there
  is no HTTPS, so the password crosses your network in clear, and the video
  files themselves stay reachable to anyone who knows a file's exact address.
  That last part is deliberate, and it is what lets a saved `.m3u` keep
  playing in VLC long after you log out. For anything facing the internet, put
  a reverse proxy with TLS in front and do not rely on this.

## Requirements

- Linux with systemd (developed on Ubuntu Server; nothing is distro-specific),
  or **Windows 10/11 and Server, as an early preview**: capture and encode run
  there, as a real service and two scheduled tasks. The web UI does not yet.
  See "Windows" under Install below.
- **Python 3.9 or newer.** The floor is deliberate and machine-checked: RHEL 9
  and its rebuilds ship 3.9 as the system `python3`, and that is the
  interpreter their packaged `requests` is built for, so a newer one would mean
  pip and a venv on one distro family for no gain. It costs this project
  nothing to hold: stdlib only, no type hints, no compatibility shims, no
  version-gated code. CI runs the suite on 3.9, 3.12 and 3.14.
- `ffmpeg` / `ffprobe`, with NVENC if you want AV1 or HEVC hardware encoding
- `rsync`, only if you enable transfer
- Cameras exposing an HTTP snapshot URL (Dahua, Hikvision/ONVIF, Reolink and
  similar) or, failing that, RTSP

Capture interval and frame rate are global defaults that **any camera can
override**. A wide general view is fine at one frame a minute played at 30fps;
a workbench is not. Set it with `sudo timelapse cameras -e:Roof`; leave it
alone and the camera follows the defaults, including when you change them.

Roughly **17,280 frames per camera per day** at the default 5s interval, which
is 4:48 of video at 60fps. Budget disk accordingly: at ~600 KB per 1440p
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

Every question has a default in brackets; **press Enter to accept it**. The
wizard then works out the disk budget for your camera count and interval, walks
you through adding cameras (testing each one live, and reporting the resolution
it got back), and writes `/etc/timelapse/config.json`.

It also derives systemd's `ReadWritePaths` from the storage you chose. That is
the single most common way a hand-install fails: the units run with
`ProtectSystem=strict`, so a frames directory that isn't listed produces a
baffling read-only error at 3am.

Prefer to read before running as root? That's the sensible instinct; the
download and the run are separate steps above precisely so you can inspect
[install.sh](install.sh) in between.

<details>
<summary>Other install options</summary>

```bash
sudo bash install_timelapse.sh --unattended   # no questions, sane defaults
sudo bash install_timelapse.sh --no-wizard    # install files only
sudo bash install_timelapse.sh --ref v0.1.4   # pin to a tag
sudo bash install_timelapse.sh --uninstall    # remove; captured data is kept
```

From a clone, which uses your checkout instead of downloading:

```bash
git clone https://github.com/war4peace/timelapse-maker.git
cd timelapse-maker && sudo ./install.sh
```

For a fully manual install, see [docs/install.md](docs/install.md).
</details>

<details>
<summary>Windows (early preview)</summary>

Download `timelapse-maker-setup-<version>.exe` from the
[releases page](https://github.com/war4peace/timelapse-maker/releases) and run
it. It asks for administrator rights, installs Python and ffmpeg if the machine
has neither, registers the capture daemon as a real Windows service and the
nightly encode and credential check as scheduled tasks, and then opens the
graphical wizard so you can add your cameras. Uninstall from Settings, Apps.

It is not code-signed, so SmartScreen will warn: "More info", then "Run
anyway". A `.sha256` is published beside it.

Neither prerequisite is ever replaced. An **ffmpeg** you installed yourself is
the one that gets used, because a recorder usually already has one you chose
deliberately, and the same goes for an existing **Python** 3.9 or newer. The
downloads only happen on a machine that has none.

Or, from a clone, with **PowerShell as administrator**:

```powershell
.\install.ps1
```

That is what the installer runs, and it is the right one for a scripted
deployment. It supplies neither prerequisite: get
[Python](https://www.python.org/downloads/windows/) 3.9 or newer, preferably
"for all users", and a build of
[ffmpeg](https://ffmpeg.org/download.html) with NVENC if the machine has an
NVIDIA card. `.\install.ps1 -Uninstall` removes everything it registered and
leaves your configuration, frames and videos alone.

What is not there yet: the web UI, and upgrading in place. To upgrade, download
the new release and run `install.ps1` again; it keeps everything.

</details>

After installing, a `timelapse` command wraps the common operations. Run
`timelapse --help` for the full list, what each one does, and which need
`sudo`:

```
timelapse status | logs | test | encode | config | setup | transfer | web | password | update | restore
```

Run `timelapse test` before enabling anything; it fetches one snapshot per
camera and reports size, resolution, latency and auth result, probes the
encoders, and projects real disk usage from your actual snapshot sizes.

> `/etc/timelapse/config.json` holds camera passwords and your webhook URL. The
> installer sets it to mode `640`, and `config/config.json` is gitignored.

## Documentation

| Document | For |
|---|---|
| [docs/install.md](docs/install.md) | Installing, configuring, operating, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | How it is built and why, read before modifying |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Reporting issues, sending patches |

## Known limitations

- **DST fall-back**: local time repeats an hour, so `HHMMSS` filenames collide.
  HTTP cameras get a `-1` suffix and keep everything; the RTSP path overwrites
  that hour. Video length varies on DST days.
- **PTZ cameras** jump-cut between presets in the finished video.
- **Cameras are polled independently**, so frames are not synchronised to the
  same instant across cameras.
- **A frozen-but-reachable camera** produces a full frame count and a static
  video. There is no automatic detection and this is deliberate, not an
  oversight: see [docs/decided-against.md](docs/decided-against.md). The tell
  is a suspiciously small output file.
- **It never touches your cameras.** It reads snapshots and nothing else: no
  reboots, no settings, no PTZ. If a camera hangs, something else on your
  network is better placed to deal with it, and would only end up fighting
  this one over the same device. The one thing it does do is *stop*
  reading: a camera that rejects the configured credentials is left alone
  apart from an occasional retry, because repeating a rejected password is how
  you get an account locked, and camera accounts are usually shared.
- **Test coverage is uneven.** 1,184 unit tests plus one end-to-end encode
  test, covering the pure logic; the RTSP path, transfer and installer
  behaviour on non-apt distros still have no automated coverage. See §9 of the
  architecture doc.

## License

[MIT](LICENSE).

# timelapse-maker

Unattended daily timelapses from IP cameras. It pulls a full-resolution
snapshot from each camera on a fixed interval, encodes each finished day into
one video per camera overnight, and optionally ships the results to a NAS and
posts a nightly summary to Discord, ntfy or Telegram.

Runs on **Linux** and **Windows**. Install it, complete a wizard, and the first
videos appear the following morning.

## Purpose of Timelapse Maker

NVR timelapse features are generally built around *clips*, not around one
contiguous file per camera per day. Agent DVR, for instance, pulls
full-resolution snapshots perfectly well, but a camera reboot or an ONVIF
setting change interrupts the recording and it resumes into a *new* file, so a
day ends up as several fragments. Blue Iris has no timelapse feature at all.
Neither has a built-in way to automatically ship the results to another machine.

This application handles both aspects. Frame capture is decoupled from the 
cameras' state, so a camera that drops out contributes fewer frames 
to the same day's file instead of splitting it, and finished videos 
are moved wherever you want them (e.g. to a remote NAS).
The application can also post-process generated timelapses to smooth out
the video.

> [!IMPORTANT]
> **Version 0.2.0. Early software, under active development.**
>
> It runs daily on the author's own recorder, and has been installed and
> exercised on three machines across Linux and Windows. That means the common
> paths work; it does not mean every combination of distribution, camera and
> GPU has been tried.
>
> The **configuration format may change between `0.x` versions**. Upgrading
> keeps your configuration, your frames and your videos.
>
> **No warranty**; see [LICENSE](LICENSE).

Bug reports and camera compatibility reports are genuinely useful at this
stage; see [CONTRIBUTING.md](CONTRIBUTING.md).

<sub>[&uarr; Contents](#contents)</sub>

## Contents

- [Purpose of Timelapse Maker](#purpose-of-timelapse-maker)
- [Install](#install)
  - [Windows](#windows)
  - [Linux](#linux)
  - [After installing, on both platforms](#after-installing-on-both-platforms)
- [What it does](#what-it-does)
- [The `timelapse` command](#the-timelapse-command)
- [Requirements and sizing](#requirements-and-sizing)
- [Cameras](#cameras)
- [The web UI](#the-web-ui)
- [Documentation](#documentation)
- [Known limitations](#known-limitations)
- [License](#license)

## Install

Both platforms capture and encode. **Linux additionally has the web UI and
in-place upgrades**; on Windows you upgrade by running the new installer.

### Windows

Download `timelapse-maker-setup-<version>.exe` from the
[releases page](https://github.com/war4peace/timelapse-maker/releases) and run it.
Allow the administrator prompt. It copies everything, registers the capture
functionality as a real Windows service and the nightly encode and credential check as
scheduled tasks, then opens a graphical wizard for general and camera configuration.

**It can supply Python and/or ffmpeg if the machine doesn't have them.** 
Two checkboxes, both ticked, and both act only if that component is missing: an ffmpeg 
you installed yourself is the one that gets used, because a recorder usually
already has one chosen deliberately, and the same goes for an existing Python
3.9 or newer. Anything it does fetch is pinned by version and checked against a
SHA-256 before it runs.

The installer executable is not code-signed, so SmartScreen will ask whether
to proceed by stating "Windows protected your PC":
choose **More info**, then **Run anyway**. A `.sha256` is published beside the
installer if you would rather check the download first.

Uninstall from Settings, Add/Remove Programs. Your configuration, frames and videos
are not removed when uninstalling the application.

<details>
<summary>From a clone, or for a scripted deployment</summary>

With **PowerShell as administrator**:

```powershell
.\install.ps1
```

That is what the `.exe` runs. It supplies neither prerequisite, so get
[Python](https://www.python.org/downloads/windows/) 3.9 or newer, preferably
"for all users", and a build of [ffmpeg](https://ffmpeg.org/download.html),
with NVENC if the machine has an NVIDIA card.

`.\install.ps1 -Uninstall` removes everything it registered and leaves your
data alone.
</details>

### Linux

```bash
curl -sL https://raw.githubusercontent.com/war4peace/timelapse-maker/main/install.sh -o install_timelapse.sh
sudo bash install_timelapse.sh
rm install_timelapse.sh
```

The download and the run are separate steps on purpose, so you can read
[install.sh](install.sh) before running it as root. It creates a system
account, writes to `/opt`, `/etc` and `/etc/systemd/system`, and enables
services.

It installs the dependencies, the scripts and the systemd units, then runs a
setup wizard that scans your disks and proposes where to put the frames.
See example below:

```
   #  Mount                 Type          Free      Total   Notes
   1  /mnt/storage          ext4      683.2 GB   916.0 GB   SSD            <- recommended
   2  /mnt/hdd              xfs         1.7 TB     3.6 TB   HDD
   3  /                     ext4      858.0 GB   932.0 GB   SSD, OS disk

  Which filesystem should hold the frames? [1]:
```

Every question has a default in brackets; **press Enter to accept it**. The
wizard works out the disk budget for your camera count and interval, walks you
through adding cameras (testing each one live and reporting the resolution it
got back), and writes `/etc/timelapse/config.json`.

It also derives systemd's `ReadWritePaths` from the storage you chose. That is
the single most common way a hand-install fails: the units run with
`ProtectSystem=strict`, so a frames directory that is not listed produces a
baffling read-only error at 3am, for example.

<details>
<summary>Other install options</summary>

```bash
sudo bash install_timelapse.sh --unattended   # no questions, sane defaults
sudo bash install_timelapse.sh --no-wizard    # install files only
sudo bash install_timelapse.sh --ref v0.2.0   # pin to a tag
sudo bash install_timelapse.sh --uninstall    # remove; captured data is kept
```

From a clone, which uses your checkout instead of downloading:

```bash
git clone https://github.com/war4peace/timelapse-maker.git
cd timelapse-maker && sudo ./install.sh
```

For a fully manual install, see [docs/install.md](docs/install.md).
</details>

Upgrading is achieved via one command, which keeps your configuration:

```bash
sudo timelapse update
```

### After installing, on both platforms

**Say yes when it offers to run the checks.** Both installers end by offering
the same pre-flight: it fetches a snapshot from every camera and reports size,
resolution, latency and authentication result, probes the encoders, checks the
low-space threshold against the disk, and projects real usage from your own
snapshot sizes.

On **Linux** the installer runs it *as the service account*, so a permission
problem surfaces then rather than past midnight, when the encoder runs,
and it goes on to offer to enable capture and the nightly encode. 
On **Windows** the wizard's closing dialog has a **Run the checks now** button,
and capture has already been started by the time you see it.

From that point forward, everything is automated.
The first videos appear after midnight, once a whole day has been captured.

To run the checks again at any point, under *Linux*:

```bash
sudo timelapse test
```

On *Windows*, the same command needs an **Administrator** prompt and no `sudo`.
Open that prompt after installing: `timelapse` is added to the system PATH, and
only new processes inherit it.

<sub>[&uarr; Contents](#contents)</sub>

## What it does

| Piece | Role | Runs as |
|---|---|---|
| `timelapse_capture.py` | One full-quality JPEG per camera every N seconds | service, always on |
| `timelapse_encode.py` | Encodes finished days, notifies, transfers | nightly at 00:05 |
| `timelapse_test.py` | Pre-flight checker | manually |
| `timelapse_setup.py` | Storage-aware configuration wizard | installer, or `timelapse setup` |
| `timelapse_gui.py` | The same wizard in a window | Windows Start menu |
| `timelapse_web.py` | Optional read-only web UI (Linux) | service, off by default |
| `timelapse_update.py` | Asks GitHub for the latest release, and installs it | `timelapse update` |
| `timelapse_platform.py` | Where config and state live, and how services are asked about | imported by the rest |

The two daemons never talk to each other. They share only a directory layout,
so either can be stopped, replaced or rewritten without touching the other.

- **One file per camera per day, always.** Capture keeps no session state: each
  frame is an independent fetch named after its wall-clock time. A camera that
  reboots, or that you reconfigure over ONVIF mid-afternoon, just contributes
  fewer frames to the same day.
- **NVENC AV1**, falling back to HEVC then x264 if the GPU or ffmpeg build
  cannot do it. No silent failure: it says which it chose and why.
- **Automated hand-off.** Finished videos go to a NAS or another host, by rsync
  on Linux and by an explicit share connection on Windows, with a guard that
  refuses to transfer to a destination that is not actually there.
- **Drift-free capture.** Threads wake on absolute wall-clock boundaries, so
  fetch latency never accumulates.
- **Atomic frame writes**, so a half-written JPEG never exists on disk.
- **Disk guard** pauses capture before filling the disk, and resumes with
  hysteresis.
- **Backlog recovery.** Machine off for three days? The next run encodes all
  three, oldest first.
- **Failure isolation.** One dead camera never affects the others; a failed
  transfer or notification never turns a good encode into a failed run.
- **Correct colour.** JPEGs are full-range; the pipeline converts to
  limited-range BT.709 and tags it, so output is not washed out or crushed
  depending on the player.
- **Per-camera settings.** Interval and frame rate are global defaults that any
  camera can override. A wide general view is fine at one frame a minute; a
  workbench is not. A camera with neither key follows the defaults, including
  when you change them later.
- **It answers "are my cameras actually working?"** A row per camera with the
  time its last frame landed, its cadence, its frame count and its failures,
  plus what last night's encode produced. `systemctl` cannot answer this: a
  capture daemon whose cameras are all refusing connections is "running", and
  so is one that has paused itself because the disk filled up. The daemons
  publish it as plain versioned JSON, so anything else you want to read it with
  can.

<sub>[&uarr; Contents](#contents)</sub>

## The `timelapse` command

Day to day you drive it through one wrapper. No reinstall, no hand-edited JSON:

| | | |
|---|---|---|
| `timelapse status` | Are the service and timer healthy, when does the next encode fire | |
| `timelapse logs` | Follow the capture log live | |
| `timelapse version` | What is installed, and whether the daemon is still running an older build | |
| `timelapse usage` | Frames, bytes and date range per camera, and which folders nothing will ever encode | **admin** |
| `timelapse test` | Pre-flight: every camera, the encoders, disk, transfer, notifications | **admin** |
| `timelapse cameras` | Add, edit, remove or disable a camera, set its own interval and frame rate, then restart capture. `-l` lists; `-a`, `-e:NAME`, `-x:NAME`, `-t:NAME`, `-r:NAME` skip the menu | **admin** |
| `timelapse transfer` | Reconfigure the destination, mounting an SMB share if needed | **admin** |
| `timelapse notify` | Where the nightly summary goes: Discord, ntfy, Telegram, any combination, with a test message for each | **admin** |
| `timelapse web` | Turn the read-only web UI on or off, set its address, library path and whether it asks for a login | **admin**, Linux |
| `timelapse password` | Set or change the web UI's login; `--disable` removes it | **admin**, Linux |
| `timelapse encode` | Run tonight's encode now | **admin** |
| `timelapse setup` · `config` | Full wizard · edit the JSON | **admin** |
| `timelapse gui` | The wizard in a window | Windows |
| `timelapse restore` | Put back an earlier config; one is kept before every change, five deep | **admin** |
| `timelapse update` | Check GitHub for a new release and install it, keeping your config | **admin**, Linux (`--check` needs nothing) |

**Anything that reads or writes the config needs administrator rights**,
because it holds your camera passwords. On Linux
`/etc/timelapse/config.json` is mode `640` owned `root:timelapse`; on Windows
`%ProgramData%\timelapse` is restricted to SYSTEM and Administrators. Without
those rights the command tells you so and stops; it does not half-run.

`status`, `logs`, `version` and `timelapse update --check` need nothing. On
Linux, for `logs` to show more than your own messages you must be in the `adm`
or `systemd-journal` group, which is usually already true of the account that
installed this. Read-only commands (`test`, `usage`) also work if you add
yourself to the `timelapse` group; the ones that *write* the config still need
root, since mode `640` grants the group read but not write.

Full descriptions in
[docs/install.md](docs/install.md#8-day-to-day-the-timelapse-command).

<sub>[&uarr; Contents](#contents)</sub>

## Requirements and sizing

- **Linux with systemd** (developed on Ubuntu Server; nothing is
  distro-specific), or **Windows 10/11 and Server**.
- **Python 3.9 or newer.** The floor is deliberate and machine-checked: RHEL 9
  and its rebuilds ship 3.9 as the system `python3`, and that is the
  interpreter their packaged `requests` is built for, so requiring a newer one
  would mean pip and a venv on one distro family for no gain. It costs this
  project nothing to hold: stdlib only, no type hints, no compatibility shims,
  no version-gated code. CI runs the suite on 3.9, 3.12 and 3.14.
- **ffmpeg and ffprobe**, with NVENC if you want AV1 or HEVC hardware encoding.
- **rsync**, on Linux, only if you enable transfer.
- Cameras exposing an HTTP snapshot URL or, failing that, RTSP.

**Sizing.** At the default 5 second interval each camera produces roughly
**17,280 frames a day**, which is 4:48 of video at 60fps. At about 600 KB per
1440p snapshot that is around **10 GB per camera per day**, held on disk for up
to two days before the frames are deleted. So a four-camera setup wants about
80 GB of working room, not counting the finished videos.

Those are illustrative figures. `timelapse test` computes the real ones from
your own cameras' actual snapshot sizes, and the wizard works out the budget
before you commit to a disk. If space does run short, capture pauses rather
than filling the volume.

<sub>[&uarr; Contents](#contents)</sub>

## Cameras

**Set the cameras up first, in their own web interfaces.** This tool only
fetches; it never changes a camera setting, so anything it needs has to be
switched on at the camera beforehand. That usually means two things:

- **An account it can sign in as.** A viewer or operator level account is
  enough, and a dedicated one is worth creating rather than reusing the admin
  login you sign in with. Camera accounts are commonly shared between several
  programs, and a lockout triggered by one of them locks out all of them.
- **The service the snapshot URL belongs to, enabled.** Depending on the make
  that is ONVIF, ISAPI, or the camera's CGI or HTTP API. **Expect ONVIF to be
  off.** Every camera in the author's fleet arrived with it disabled, so treat
  enabling it as a step you will have to take rather than something to check.
  On **Dahua and Hikvision** it is two steps, not one: **ONVIF keeps its own
  user list**, so enabling the service is not enough on its own, and an admin
  account that works perfectly well in the browser is still refused on an ONVIF
  endpoint until you add a user there as well. A TP-Link Tapo needs its
  third-party "camera account" created in the phone app.

If a camera answers on its ONVIF endpoint it will also show up under **Scan
network** below, which is a quick way to confirm you enabled the right thing.

Any camera with an HTTP snapshot URL works, and the wizard knows the URL shape
for the common makes so you give it an address and a password rather than
typing a URL. It offers presets for **Dahua/Amcrest**, **Hikvision** (both
ISAPI and ONVIF paths), **Reolink**, **Axis**, generic **ONVIF snapshot**,
**RTSP only**, and a custom URL.

Tested against real hardware: **Dahua**, **Hikvision**, **Reolink** and
**TP-Link Tapo**. The Tapo is the RTSP case, and is measured rather than
assumed: it authenticates over ONVIF and answers `ActionNotSupported` to
`GetSnapshotUri` on every profile, so RTSP is not a workaround there, it is the
only path. Axis and generic ONVIF presets are built from published URL forms
and have not been exercised on real hardware; reports either way are welcome.

**Scan network** (`timelapse discover`, or the button in the graphical wizard)
sends one WS-Discovery query and lists what answers, with the make already
chosen where the camera said enough about itself. No credentials are sent, so
it cannot lock a camera account. It will not see a camera with ONVIF turned
off, or one on another subnet or VLAN, since multicast stops at the first
router; an empty result means type the address in, not that there are no
cameras.

You do not have to get any of this right first time. Every camera is fetched
from as you add it, and the wizard reports the size, resolution and
authentication result there and then, so a camera that has not been set up yet
says so while you are still looking at it.
[docs/install.md](docs/install.md#common-snags) covers what each failure means,
including the 401 that turns out to be an ONVIF user list.

<sub>[&uarr; Contents](#contents)</sub>

## The web UI

Optional, Linux only, off by default. Service status, an index of your finished
videos by camera and by day, and a *Play* link that hands each one to VLC,
including one playlist per day, so reviewing a day means opening a single file
instead of one per camera. It reads the destination you already keep your
timelapses in, recognising the naming conventions of whatever tool came before.

![The overview page: a row per camera with its last frame, cadence, frame count
and failures; what last night's encode produced; the state of each service; and
the version panel](screenshots/webui-overview.png)

The top table is the question `systemctl` cannot answer. Every camera there is
"running" as far as the service manager is concerned, including the two quietly
accumulating failed fetches. Frames and coverage are counted from the files on
disk rather than from a counter the daemon keeps, so they survive a restart and
work for RTSP cameras, where ffmpeg rather than this program writes the frames.

![The library index: every camera found in the destination, with file counts,
sizes and date ranges, and a list of recent days](screenshots/webui-library-1.png)

The library is read from wherever you already keep your timelapses, which on a
recorder that has been running for years is not a directory this program made.
That one holds 6,922 files over five years in six different naming conventions,
from three tools. `Garaj` and `Garage`, `Roof` and `roof`, `Workshop` and
`workshop` are each **sorted next to each other and never merged**: a camera
name is a place, places get recycled between cameras, and deciding that two
labels mean the same place is a judgement only you can make.

![One day expanded: every video for that date with its size, a Play link, a
Download link and the exact path it came
from](screenshots/webui-library-daily-view.png)

It is the only part of this that listens on a network port, and it is
structurally read-only: it binds localhost unless you change that, and its
systemd unit runs with `ProtectSystem=strict` and exactly one writable
directory, its own index. Everything else, including your configuration, the
frames and the videos, is read-only to it. It cannot start, stop or reconfigure
anything, by design.

**Its login is a door lock, not a safe.** It keeps the household, or a guest on
your wifi, out of the status page and the video index. That is all it is for,
and the design says so out loud rather than implying more: there is no HTTPS,
so the password crosses your network in clear, and the video files themselves
stay reachable to anyone who knows a file's exact address. That last part is
deliberate, and it is what lets a saved `.m3u` keep playing in VLC long after
you log out. For anything facing the internet, put a reverse proxy with TLS in
front and do not rely on this.

Nothing else here accepts an inbound connection. Outbound, the daemons talk to
your cameras and to your notification service if you configured one, and the
web UI asks the GitHub releases API once a day whether a newer version exists,
and only while someone has its overview page open. That is its single outbound
connection, it is named on the page, and `update_check: false` turns it off.

There has been no third-party security audit. What there is instead is a small
and deliberately stated surface, described exactly in
[docs/install.md §11](docs/install.md#11-the-web-ui), which is also where the
setup instructions are.

<sub>[&uarr; Contents](#contents)</sub>

## Documentation

| Document | For |
|---|---|
| [docs/install.md](docs/install.md) | Installing, configuring, operating, troubleshooting |
| [docs/architecture.md](docs/architecture.md) | How it is built and why; read before modifying |
| [docs/decided-against.md](docs/decided-against.md) | Features considered and refused, with the reasoning |
| [docs/future-features.md](docs/future-features.md) | Planned work, in build order |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Reporting issues, sending patches |

<sub>[&uarr; Contents](#contents)</sub>

## Known limitations

- **DST fall-back**: local time repeats an hour, so `HHMMSS` filenames collide.
  HTTP cameras get a `-1` suffix and keep everything; the RTSP path overwrites
  that hour. Video length varies on DST days.
- **PTZ cameras** jump-cut between presets in the finished video.
- **Cameras are polled independently**, so frames are not synchronised to the
  same instant across cameras.
- **A frozen-but-reachable camera** produces a full frame count and a static
  video. There is no automatic detection, and that is deliberate rather than an
  oversight: see [docs/decided-against.md](docs/decided-against.md). The tell is
  a suspiciously small output file.
- **It never touches your cameras.** It reads snapshots and nothing else: no
  reboots, no settings, no PTZ. If a camera hangs, something else on your
  network is better placed to deal with it, and would only end up fighting this
  one over the same device. The one thing it does do is *stop* reading: a
  camera that rejects the configured credentials is left alone apart from an
  occasional retry, because repeating a rejected password is how you get an
  account locked, and camera accounts are usually shared.
- **On Windows**, there is no web UI and no in-place upgrade yet. Download the
  new release and run the installer again; it keeps everything.

<sub>[&uarr; Contents](#contents)</sub>

## License

[MIT](LICENSE).

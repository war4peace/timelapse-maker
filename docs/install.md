# Install and operation

Operator guide. For how the system is built and why, see
[architecture.md](architecture.md).

---

## 1. On-disk layout

```
<frames_root>/<Camera>/<YYYY-MM-DD>/<HHMMSS>.jpg   ← deleted after a successful encode
<video_output>/<Camera>.<YYYYMMDD>.mkv             ← emptied by transfer each night
<log_dir>/{capture,encode}.log
```

Filenames are zero-padded `HHMMSS`, so lexical order **is** chronological.
Nothing depends on file mtime.

## 2. Install

### The short way

```bash
curl -sL https://raw.githubusercontent.com/war4peace/timelapse-maker/main/install.sh -o install_timelapse.sh
sudo bash install_timelapse.sh
rm install_timelapse.sh
```

`install.sh` installs dependencies (apt, dnf, yum, pacman, zypper or apk),
creates a `timelapse` system account, puts the scripts in `/opt/timelapse`, the
units in `/etc/systemd/system`, a `timelapse` wrapper in `/usr/local/bin`, and
then runs the setup wizard described in §3. Re-running it upgrades the scripts
and offers to keep your existing config.

| Flag | Effect |
|---|---|
| `--unattended` | No questions. Defaults everywhere, services not enabled. |
| `--no-wizard` | Install files only; configure later with `timelapse setup`. |
| `--ref REF` | Install a specific branch or tag. |
| `--prefix DIR` | Install somewhere other than `/opt/timelapse`. |
| `--uninstall` | Remove programs and units. Captured data is never deleted. |

The download and the execution are deliberately two steps, so you can read the
script before running it as root.

> **Piping straight to bash** (`curl -sL … | sudo bash`) also works; the
> installer and wizard read prompts from `/dev/tty` rather than stdin, because
> under a pipe stdin *is* the script. If no terminal is reachable at all, both
> fall back to accepting defaults instead of hanging.

### Upgrading

Re-run the same installer. It is the supported upgrade path, and it is safe on
a live install:

```bash
curl -sL https://raw.githubusercontent.com/war4peace/timelapse-maker/main/install.sh -o install_timelapse.sh
sudo bash install_timelapse.sh
```

What it does and does not touch:

| | On upgrade |
|---|---|
| `config.json` | **Kept.** You are asked "Reconfigure it?" and the default is *no*. |
| `config.example.json` | Replaced, so you can diff it for new keys. |
| Scripts and units | Replaced, then `ReadWritePaths` is re-derived from your config. |
| Captured frames and videos | Never touched. |
| A running capture daemon | Restarted, after asking; see below. |
| An encode already in flight | Left alone. It is oneshot, so it finishes on the build it started with and the next nightly trigger uses the new one. |

**Why the restart prompt matters.** A running daemon keeps executing the code it
read at startup, and `systemctl enable --now` does nothing to an already-active
unit. Say no and you keep running the old build with the new one sitting unused
on disk. Apply it later with:

```bash
sudo systemctl restart timelapse-capture.service
```

A restart costs only the frames due while it happens: a second or two.

**New config keys** are read with defaults, so an older `config.json` keeps
working; you get the new behaviour without editing anything. Re-run
`timelapse setup` only if you actually want to change an answer.

Check what is installed, and whether it is what is *running*:

```bash
timelapse version
```

```
  capture  0.1.0
  encode   0.1.0
  test     0.1.0
  setup    0.1.0
  web      0.1.0
```

If the daemon predates the installed files it says so explicitly, which is the
one failure mode a version number by itself cannot show you.

### The manual way

```bash
sudo apt install ffmpeg python3-requests rsync

# a dedicated unprivileged account for the daemons
sudo useradd --system --no-create-home --shell /usr/sbin/nologin timelapse

sudo mkdir -p /opt/timelapse /etc/timelapse
sudo cp scripts/timelapse_*.py /opt/timelapse/
sudo chmod +x /opt/timelapse/*.py

sudo cp config/config.example.json /etc/timelapse/config.json
sudo chown root:timelapse /etc/timelapse/config.json
sudo chmod 640 /etc/timelapse/config.json        # it holds camera passwords

sudo mkdir -p /var/lib/timelapse/{frames,videos,logs}
sudo chown -R timelapse:timelapse /var/lib/timelapse
```

Then edit `/etc/timelapse/config.json`: camera URLs, credentials, and, if you
put frames anywhere other than `/var/lib/timelapse`, the `paths` block.

> **If you change `paths`**, update `ReadWritePaths=` in *both* systemd units to
> match. They run with `ProtectSystem=strict`, so an unlisted path fails with a
> confusing read-only error. The same applies to a local transfer destination
> such as a CIFS or NFS mountpoint.

## 3. The setup wizard

The installer runs it automatically. Run it again any time with:

```bash
timelapse setup
```

It scans `/proc/mounts` for real, writable, local filesystems (skipping pseudo
filesystems, read-only mounts, snap/docker paths and network shares), reads free
space with `statvfs`, and checks `/sys/block/<dev>/queue/rotational` to label
each one SSD or HDD:

```
   #  Mount                 Type          Free      Total   Notes
   1  /mnt/storage          ext4      683.2 GB   916.0 GB   SSD            <- recommended
   2  /mnt/hdd              xfs         1.7 TB     3.6 TB   HDD
   3  /                     ext4      858.0 GB   932.0 GB   SSD, OS disk

  Which filesystem should hold the frames? [1]:
```

It recommends the roomiest filesystem that isn't the OS disk. Every prompt has a
default in brackets; Enter accepts it.

It then covers ffmpeg paths (probing for NVENC and telling you which encoder you
will actually get), the capture interval, a disk budget for your camera count,
the low-space threshold, cameras, transfer, Discord and the optional web UI.

Two things it does that are easy to get wrong by hand:

- **URL-encodes credentials** that belong in a query string. A password
  containing `&`, `#`, `=` or `%` silently breaks a hand-written Reolink URL.
- **Derives `ReadWritePaths`** for the systemd units from the storage you chose.
  The units run `ProtectSystem=strict`; an unlisted frames directory fails with
  a read-only error that looks nothing like a permissions problem.

Network filesystems (NFS, CIFS, 9p, sshfs) are deliberately excluded as frame
storage: `os.replace()` gives no atomicity guarantee across the wire, and 17k
small writes per camera per day over a network is painful. They are still fine
as a *transfer destination*, which is a nightly bulk copy.

## 4. Test before enabling anything

```bash
sudo -u timelapse python3 /opt/timelapse/timelapse_test.py /etc/timelapse/config.json
```

This fetches one snapshot per camera and reports size, resolution, latency and
auth result; writes samples to a temp directory so you can check quality; probes
the encoders; projects daily disk usage against actual free space; verifies the
transfer destination and the Discord webhook.

Fix everything red before going further.

### Check the ONVIF profiles first

If any camera URL contains `Profile_N`, check which profile is actually the main
stream. On Hikvision, **Profile_1 is normally the main stream, Profile_2 the
substream, Profile_3 the third stream**, but the numbering is not consistent
across vendors, and nothing in the URL tells you which you got. Guessing wrong
silently gives you a low-resolution timelapse.

```bash
sudo -u timelapse python3 /opt/timelapse/timelapse_test.py \
     /etc/timelapse/config.json --probe-profiles
```

This fetches every profile from each ONVIF camera, prints the resolution and
file size of each, saves samples for inspection and tells you which to put in
the config:

```
  Workshop:
      Profile_1: 2560,1440,yuvj420p         412 KB
      Profile_2: 704,576,yuvj420p            48 KB
      Profile_3: 640,480,yuvj420p            31 KB
      -> highest resolution is Profile_1 (CHANGE the config from Profile_2 to Profile_1)
```

Worth doing before the main test, because snapshot size drives the whole disk
budget.

### Common snags

- **HTTP 401**: try the other auth scheme (`digest` ↔ `basic`). Some cameras
  reject the admin account on ONVIF endpoints but accept a separate ONVIF user.
  If the URL works with no credentials at all, set `"auth": "none"` and drop
  the username/password.
- **Reolink-style URLs** put credentials in the query string, so they need
  `"auth": "none"`. URL-encode any `&`, `#`, `+` or `%` in the password, or the
  URL silently parses wrong.
- **A camera that passes one test fetch but fails in service**: some cameras
  cope badly with sustained polling. Watch the first hour:
  `grep <Camera> <log_dir>/capture.log`. Raising `interval_seconds` usually
  fixes it.
- **Regular bursts of HTTP 500 from one camera, minutes apart**: check whether
  your NVR is *also* pulling from it. This is the most likely snag on a shared
  host, because that is exactly where you would install this: leaving AgentDVR's
  own timelapse or snapshot schedule enabled points two clients at one camera,
  and most cameras answer the loser with `500` rather than queueing it.

  The signature is a *fixed number* of consecutive failures per burst (a
  duration, not a coin flip), recovering on its own each time. Turn off the
  NVR's timelapse/snapshot schedule for cameras this tool owns; you are
  replacing that feature, which is the point.

  You do not need to watch the journal for this. The nightly Discord summary
  prints `Cov%` per camera; one camera sitting a few points below the others is
  the same story.
- **`av1_nvenc not available`**: distro ffmpeg builds often lack NVENC. Use a
  BtbN static build or `jellyfin-ffmpeg` and point `paths.ffmpeg` at it. The
  script falls back to HEVC then x264 rather than failing, but you lose AV1.

## 5. Enable

```bash
sudo cp service/timelapse-capture.service \
        service/timelapse-encode.service \
        service/timelapse-encode.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now timelapse-capture.service
sudo systemctl enable --now timelapse-encode.timer

journalctl -u timelapse-capture -f
```

Check after an hour: at a 5s interval this should be ≈ 720 × cameras:

```bash
find /var/lib/timelapse/frames -name '*.jpg' | wc -l
```

Dry-run the encoder without waiting for midnight:

```bash
python3 /opt/timelapse/timelapse_encode.py /etc/timelapse/config.json \
        --date 2026-08-04 --dry-run --no-transfer
```

`--dry-run --no-transfer` builds the concat list and reports frame counts
without encoding, transferring or deleting anything. Safe on live data.

## 6. Transfer destination

### If the destination is unavailable

Nothing is lost. A failed transfer leaves the videos in `paths.video_output`
and does not spoil the encode; the run exits `1`, so `timelapse status` shows
`timelapse-encode.service` failed, and the Discord summary says
`Transfer FAILED`. The next run picks up **everything** waiting in
`video_output`, not just that night's video, so a backlog ships itself as soon
as the destination comes back. A run with nothing to encode still ships the
backlog, so fixing the share and running `timelapse encode` by hand works.

The one case that does **not** self-correct is a share that is not mounted
while `require_mountpoint` is `false`. An unmounted mountpoint is an ordinary
empty local directory, so rsync succeeds into it, `--remove-source-files`
deletes the originals, and the run exits `0`. Nothing failed, so nothing is
retried, and the videos sit on the local disk underneath the mountpoint where
they will be hidden the moment the share mounts. Check yours:

```bash
sudo grep require_mountpoint /etc/timelapse/config.json
```

If the destination is a NAS share, this should be `true` (or the exact mount
path, which is more precise). The wizard sets it for you when it recognises a
CIFS/NFS destination or mounts the share itself, but a hand-written config or
a path the wizard saw as an ordinary local directory will not have it.

Note also that the backlog is unbounded: `encode.max_backlog_days` limits
encoding, not transfer, and the disk guard watches `frames_root` rather than
`video_output`. A destination that is down for weeks will accumulate videos.

`transfer.destination` is either a local path or an rsync remote spec; one code
path serves both.

**rsync over SSH (recommended, no stale-mount failure mode):**

```bash
sudo -u timelapse ssh-keygen -t ed25519
sudo -u timelapse ssh-copy-id user@nas
# destination: "user@nas:/mnt/user/timelapse/"
```

**CIFS/SMB share:** the wizard does this for you. Pick *"A network share
(SMB/CIFS) - set it up for me"* at the transfer step and it will install
`cifs-utils`, ask for the server, share, credentials and mount point, mount it
(negotiating the SMB dialect), create the destination folder, work out which
rsync flags the share accepts, and add an `/etc/fstab` entry with
`nofail,x-systemd.automount` so it returns after a reboot without blocking boot
if the NAS is down.

To set this up after the initial install, or to change it later:

```bash
sudo timelapse transfer
```

Two things it exists to catch, both of which are silent by hand:

- **`rsync -a` may or may not work.** `-a` implies `--owner --group`, which a
  CIFS mount often cannot honour; rsync then exits `23` every night even
  though the files arrived. Whether it happens depends on the server and the
  mount options (`forceuid`/`forcegid` can make it a non-issue), so the script
  tries `-a`, `-rt` and `-a --no-perms --no-owner --no-group` and tells you
  which succeeded rather than guessing.
- **A dropped mount is worse than a failed transfer.** An unmounted mountpoint
  is an ordinary empty local directory, so rsync writes your videos to the
  local disk and `--remove-source-files` then deletes the originals. Set
  `"require_mountpoint": true` and the encoder refuses instead, leaving the
  videos in `video_output` to ship on the next run.

Whichever way you mount it, add the mountpoint to `ReadWritePaths=` in
`timelapse-encode.service`; `ProtectSystem=strict` will otherwise fail the
write read-only.

Set `transfer.enabled` to `false` to keep videos on the local disk. Note that
nothing then prunes `video_output`; add your own retention if you do this.

## 7. Sizing

At a 5s interval: **17,280 frames/camera/day** → 4:48 of video at 60fps.

At ~600 KB per 1440p snapshot that is ~10 GB/camera/day, and frames stay
resident for up to two days (yesterday's are still there while the encoder works
through them). Six cameras is therefore ~60 GB/day and ~130 GB resident.

Full-resolution main streams can be several times larger than that. Trust
`timelapse_test.py`'s projection over any estimate; it measures your actual
snapshots. A spinning disk is fine for frames; the access pattern is sequential
write-once/read-once.

Write endurance is a non-issue on SSD: even 110 GB/day is ~40 TB/year against a
typical 600 TBW consumer rating.

## 8. Day to day: the `timelapse` command

The installer puts a single wrapper on `PATH`. Everything below is a subcommand
of it; nothing needs a reinstall.

| Command | What it does |
|---|---|
| `timelapse status` | `systemctl status` for all four units in one shot: capture, the encode timer **and the encode service**, and the web UI. Running or not, how long, recent log lines, and when the next encode fires. The encode *service* is listed separately from its timer on purpose: it is oneshot, so a run that failed (a broken transfer, say) leaves the service failed while the timer still looks healthy. |
| `timelapse logs` | Follows the capture journal live (`journalctl -u timelapse-capture -f`). Ctrl-C to stop. This is where camera failures and recoveries appear. |
| `timelapse usage` | Disk report: frames, bytes and date range per camera, plus totals, videos and free space. See below. |
| `timelapse test` | Pre-flight check. Fetches from every enabled camera and reports resolution and size, verifies the encoders, disk headroom, the transfer destination and Discord. Run it after any change. |
| `timelapse cameras` | Add, edit, remove, enable/disable or test cameras, then restart capture. §9. |
| `timelapse transfer` | Reconfigure just the transfer destination, including mounting an SMB/CIFS share and fixing `ReadWritePaths=`. §6. |
| `timelapse web` | Turn the read-only web UI on or off and set its address, port and library path. §10. |
| `timelapse web-serve` | Run the web UI in the foreground for a look at its log. The service normally does this. |
| `timelapse setup` | The full wizard again: storage, capture settings, cameras, transfer, Discord. Overwrites the whole config, so prefer `cameras` or `transfer` for a single change. |
| `timelapse config` | Opens `config.json` in `$EDITOR` for anything the wizards do not cover. You are then responsible for restarting capture yourself. |
| `timelapse encode` | Runs the nightly encode immediately instead of waiting for 00:05. Useful to clear a backlog. |
| `timelapse version` | The installed version of each script, and a warning if the running daemon predates them. |

**Which need root.** `config.json` is `0640 root:timelapse` because it holds
camera credentials, so anything that touches it needs `sudo`:

| Needs `sudo` | Works unprivileged |
|---|---|
| `setup`, `cameras`, `transfer`, `web`, `config` (they write the config) | `status` |
| `encode`, `web-serve` (they read it, and write frames and videos) | `version` |
| `test`, `usage` (they read it) | `logs` |

`test` and `usage` also work if you add yourself to the `timelapse` group,
since they only read. The ones that write the config still need root: mode
`0640` grants the group read but not write. `encode` needs root regardless,
because group membership does not get it write access to the frame and video
directories either.

`logs` runs unprivileged but shows only your own messages unless you are in
the `adm` or `systemd-journal` group, and says so when you are not.

Getting this wrong is not dangerous: the command says which file it could not
read and stops, rather than half-running.

**A config change only reaches capture when it restarts.** The daemon reads its
camera list once, at startup. `timelapse cameras` handles that for you; after a
hand-edit with `timelapse config`, run `sudo systemctl restart
timelapse-capture`. The encoder re-reads the config on every run, so it never
needs this.

### Checking disk usage

```bash
timelapse usage
```

```
=== Frames: /var/lib/timelapse/frames ===
  Camera          Days     Frames       Size  Range
  --------------------------------------------------------------------------
  Doorbell           2     34,560     14.2 GB  2026-08-05..2026-08-06
  Driveway           -          -          -  -                       not captured yet
  Garage             3     51,840     21.0 GB  2026-08-01..2026-08-03  ORPHAN
  Roof               1     17,280      7.1 GB  2026-08-06
  Workshop           1      9,000      3.7 GB  2026-08-06              disabled
  --------------------------------------------------------------------------
  total              7    112,680     46.0 GB
  ....  average frame 430 KB

  WARN  'Garage' has frames on disk but is not in the config at all.
  WARN  'Workshop' has frames on disk but is disabled in the config.
  ....  The nightly encode skips them, so those frames stay forever.
```

The two flagged rows are the point of the command. The encode job only walks
cameras **enabled** in the config, so a camera you removed, or merely disabled,
keeps everything it ever captured, and nothing will ever encode or delete it.
That is normally what is eating the disk. Re-enable the camera, or encode the
days out with `timelapse encode --date YYYY-MM-DD`.

`ORPHAN` means no config entry at all; `disabled` means the entry exists with
`enabled: false`. Frames are never deleted behind your back, so both are safe
to leave; they just cost space.

It stats every frame file, so on a busy install expect a few seconds.

**Encoder CLI**

```
timelapse_encode.py [config] [--date YYYY-MM-DD] [--dry-run]
                             [--keep-frames] [--no-transfer]
```

Exit codes: `0` all good, `1` partial failure, `2` critical. Pass these through
the wrapper too: `timelapse encode --date 2026-08-01 --keep-frames`.

## 9. Adding, editing and removing cameras

```bash
sudo timelapse cameras
```

No reinstall, and no walking the whole wizard again. It lists what you have and
loops on single-key actions:

```
     #  Name           On  Type  URL
     1  Doorbell       yes http  http://192.168.2.201/cgi-...&password=***
     2  Roof           yes http  http://192.168.2.206/onvif-http/snapshot
     3  Garage         no  http  http://192.168.2.210/ISAPI/Streaming/ch...

  a add   e edit   r remove   x enable/disable   t test   q save & quit
```

Adding uses the same preset list and live test as the installer. Passwords in
the query string are masked in the listing; `ask_secret` keeps them out of
scroll-back when you type them, so printing them back would defeat it.

**It restarts capture for you.** The daemon reads its camera list once, at
startup, so an edit does nothing until it restarts; you are asked, and told the
command if you decline. Nothing here touches paths, so the systemd units are
unaffected.

### It will not let you strand frames silently

The nightly encode builds its work list from the cameras **enabled** in the
config and looks for `<frames_root>/<name>/`. So three things can orphan
already-captured frames, and each one warns first:

| Action | What would be lost |
|---|---|
| Remove a camera | Every un-encoded day it has captured |
| **Disable** a camera | The same: `enabled: false` hides it from the encoder too, which is easy to miss |
| Rename a camera | Everything under the old directory name |

```
  WARN  'Roof' has 1 un-encoded day(s) in /srv/frames/Roof
  The nightly encode only looks at cameras enabled in the config, so
  this would leave those frames on disk with nothing to encode them.
  Encode them first with:  timelapse encode --date 2020-01-02
  Remove it anyway? (y/N):
```

A rename offers to move the directory with it, so that case is normally
handled for you. Frames are never deleted; a removed camera's directory stays
on disk, and re-adding it under the same name picks it straight back up.

Editing `config.json` by hand still works (`timelapse config`), but then the
restart and the stranding checks are yours to remember.

---

## 10. The web UI

Optional, off by default, and read-only: it never triggers an encode, controls
a camera, edits the config or deletes anything. Turn it on with:

```bash
sudo timelapse web
sudo systemctl enable --now timelapse-web.service
```

It suggests this host's LAN address, so the page opens at whatever it reported,
for example `http://192.168.1.50:8787/`. Read the section on securing it below
before accepting that. If you would rather keep it to this machine, answer
`127.0.0.1` at the prompt.

Re-run `sudo timelapse web` any time to change the address, port or library
path; it offers to restart the service so the change takes effect. Editing
`config.json` by hand does not, so follow that with `sudo systemctl restart
timelapse-web`: the server reads its config once, at startup.

It gives you five things:

- **Where your videos actually are**: see the warning below, this is the
  question people get wrong.
- **Service status and recent log**, on request. Same output as `timelapse
  status` and `journalctl`, without an SSH session.
- **An index of finished videos**, browsable by camera, by day and by folder.
- **Playback in your own player.** Each video has a *Play* link that hands VLC
  (or mpv, or whatever opens `.m3u`) a playlist pointing back at the server,
  plus a *Download* link. There is also **one playlist per day**: open it and
  your player queues that day's videos from every camera in turn.
- **Whether there is a new version**, with what changed and the commands to
  upgrade. See below; you can turn it off.

### The update check, and the only packet this UI sends

The Overview page tells you which version you are running and whether a newer
one has been tagged, along with its changelog entry and the upgrade commands.

It asks `api.github.com` **at most once a day**, and only while somebody has
the page open, so a service nobody looks at never contacts anything. The
answer is cached in `web.state_dir` and survives restarts.

**This is the only outbound connection the web UI ever makes.** It sends no
configuration, no camera names, and nothing about your videos or your library.
GitHub sees this host's IP address and a User-Agent naming the project and
version, the same as any browser visiting the repository page would.

`timelapse web` asks whether you want it, and it is on by default, including
after an upgrade. To turn it off:

```bash
sudo timelapse web        # answer "n" to "Check GitHub for updates?"
```

or set `"update_check": false` in the `web` section of `config.json` and run
`sudo systemctl restart timelapse-web`.

### Your videos are probably not in `video_output`

The nightly transfer runs `rsync --remove-source-files`, so once it has run,
`paths.video_output` is **empty**; the videos are at `transfer.destination`.
The UI works this out for you and says which path it chose. Two cases worth
knowing:

- **A remote destination** (`user@nas:/mnt/user/timelapse/`) is not a path this
  host can read at all. The page says so rather than showing an empty list. If
  the same files are also mounted locally, give that path as `library_root`.
- **A NAS that is not mounted** looks identical to an empty library, so the
  page reports the directory as unreadable instead of pretending it is empty.

Do not put the library under `/tmp` or `/var/tmp`. The unit sets
`PrivateTmp=true`, so the service gets a private empty one and reports your
library as unreadable: correct, and thoroughly confusing.

### It is not secured, so bind it accordingly

There is **no login and no HTTPS**. The wizard suggests this host's LAN
address, because a page you can only open on the recorder itself is not much
use, and it says so plainly when you accept it: **anyone on that network can
watch your cameras' footage.**

That is the right trade on a trusted home LAN and the wrong one anywhere else.
Answer `127.0.0.1` to keep it to this machine. Put a reverse proxy with TLS and
authentication in front of it for anything wider, and do not port-forward it.

The wizard also checks that the address you give actually exists on this host.
It used to accept anything, and a wrong address is the worst kind of mistake
here: the service starts, logs the address it is serving and is unreachable,
with nothing in the journal to say why.

### What it is allowed to write

One directory: `web.state_dir`, default `/var/lib/timelapse/web`, which holds
an index of your library. `timelapse-web.service` lists exactly that path in
`ReadWritePaths=`, so the videos, the captured frames and `config.json` are
read-only to it: enforced by systemd, not merely intended.

The installer creates that directory. The service cannot: a `ReadWritePaths=`
naming a directory that does not exist stops the unit from starting, and inside
the sandbox its parent is read-only anyway.

### The index

The first scan runs in the background, so the page is usable immediately and
reports progress. After that, opening a folder re-reads that one directory and
opening a video re-checks that one file, so browsing does not walk your whole
NAS. *Rescan* forces a full pass.

It reads whatever is in the destination, not only what this tool wrote. Six
different filename conventions from predecessor tools are recognised, including
files with no camera name in them at all. **Names are never merged**: if you
have both `Workshop` and `workshop`, or `garaj` and `Garage`, you get both,
sorted next to each other. A camera name is a *place*, places get recycled
between cameras over the years, and deciding whether two labels mean the same
thing is your call, not the index's.

Videos too small to be a real day (a few kilobytes where a day is hundreds of
megabytes) are listed under **Flagged** with their full path, so you can check
and delete them by whatever means you prefer. The UI will not do it for you.

### Snags

| Symptom | Cause |
|---|---|
| Log pane says "no entries" and names `systemd-journal` | The service account cannot read the journal. The unit asks for `SupplementaryGroups=systemd-journal`; the installer drops that line on a distro without the group. Add it back and `systemctl daemon-reload`. |
| Library page says the directory is unreadable | The NAS is not mounted, or the library is under `/tmp`/`/var/tmp` (see above). |
| Library is empty but the directory is right | The scan may still be running; the page says so. Otherwise nothing there matched a video extension. |
| Clicking *Play* downloads a file instead of opening VLC | Your browser has no handler for `.m3u`. Tell it to always open that type, or copy the stream URL shown under each entry into VLC's *Open Network Stream*. |
| Seeking does nothing in a player | Check you are on 0.0.9 or later; earlier builds sent `Accept-Ranges: none`. |

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

> **Piping straight to bash** (`curl -sL … | sudo bash`) also works — the
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
| A running capture daemon | Restarted, after asking — see below. |
| An encode already in flight | Left alone. It is oneshot, so it finishes on the build it started with and the next nightly trigger uses the new one. |

**Why the restart prompt matters.** A running daemon keeps executing the code it
read at startup, and `systemctl enable --now` does nothing to an already-active
unit. Say no and you keep running the old build with the new one sitting unused
on disk. Apply it later with:

```bash
sudo systemctl restart timelapse-capture.service
```

A restart costs only the frames due while it happens — a second or two.

**New config keys** are read with defaults, so an older `config.json` keeps
working; you get the new behaviour without editing anything. Re-run
`timelapse setup` only if you actually want to change an answer.

Check what is installed, and whether it is what is *running*:

```bash
timelapse version
```

```
  capture  0.0.8
  encode   0.0.8
  test     0.0.8
  setup    0.0.8
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

Then edit `/etc/timelapse/config.json`: camera URLs, credentials, and — if you
put frames anywhere other than `/var/lib/timelapse` — the `paths` block.

> **If you change `paths`**, update `ReadWritePaths=` in *both* systemd units to
> match. They run with `ProtectSystem=strict`, so an unlisted path fails with a
> confusing read-only error. The same applies to a local transfer destination
> such as a CIFS or NFS mountpoint.

## 3. The setup wizard

The installer runs it automatically. Run it again any time with:

```bash
timelapse setup
```

It scans `/proc/mounts` for real, writable, local filesystems — skipping pseudo
filesystems, read-only mounts, snap/docker paths and network shares — reads free
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
the low-space threshold, cameras, transfer and Discord.

Two things it does that are easy to get wrong by hand:

- **URL-encodes credentials** that belong in a query string. A password
  containing `&`, `#`, `=` or `%` silently breaks a hand-written Reolink URL.
- **Derives `ReadWritePaths`** for the systemd units from the storage you chose.
  The units run `ProtectSystem=strict`; an unlisted frames directory fails with
  a read-only error that looks nothing like a permissions problem.

Network filesystems (NFS, CIFS, 9p, sshfs) are deliberately excluded as frame
storage — `os.replace()` gives no atomicity guarantee across the wire, and 17k
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
substream, Profile_3 the third stream** — but the numbering is not consistent
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

- **HTTP 401** — try the other auth scheme (`digest` ↔ `basic`). Some cameras
  reject the admin account on ONVIF endpoints but accept a separate ONVIF user.
  If the URL works with no credentials at all, set `"auth": "none"` and drop
  the username/password.
- **Reolink-style URLs** put credentials in the query string, so they need
  `"auth": "none"`. URL-encode any `&`, `#`, `+` or `%` in the password, or the
  URL silently parses wrong.
- **A camera that passes one test fetch but fails in service** — some cameras
  cope badly with sustained polling. Watch the first hour:
  `grep <Camera> <log_dir>/capture.log`. Raising `interval_seconds` usually
  fixes it.
- **Regular bursts of HTTP 500 from one camera, minutes apart** — check whether
  your NVR is *also* pulling from it. This is the most likely snag on a shared
  host, because that is exactly where you would install this: leaving AgentDVR's
  own timelapse or snapshot schedule enabled points two clients at one camera,
  and most cameras answer the loser with `500` rather than queueing it.

  The signature is a *fixed number* of consecutive failures per burst (a
  duration, not a coin flip), recovering on its own each time. Turn off the
  NVR's timelapse/snapshot schedule for cameras this tool owns — you are
  replacing that feature, which is the point.

  You do not need to watch the journal for this. The nightly Discord summary
  prints `Cov%` per camera; one camera sitting a few points below the others is
  the same story.
- **`av1_nvenc not available`** — distro ffmpeg builds often lack NVENC. Use a
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

Check after an hour — at a 5s interval this should be ≈ 720 × cameras:

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

`transfer.destination` is either a local path or an rsync remote spec — one code
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
  CIFS mount often cannot honour — rsync then exits `23` every night even
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
`timelapse-encode.service` — `ProtectSystem=strict` will otherwise fail the
write read-only.

Set `transfer.enabled` to `false` to keep videos on the local disk. Note that
nothing then prunes `video_output` — add your own retention if you do this.

## 7. Sizing

At a 5s interval: **17,280 frames/camera/day** → 4:48 of video at 60fps.

At ~600 KB per 1440p snapshot that is ~10 GB/camera/day, and frames stay
resident for up to two days (yesterday's are still there while the encoder works
through them). Six cameras is therefore ~60 GB/day and ~130 GB resident.

Full-resolution main streams can be several times larger than that. Trust
`timelapse_test.py`'s projection over any estimate — it measures your actual
snapshots. A spinning disk is fine for frames; the access pattern is sequential
write-once/read-once.

Write endurance is a non-issue on SSD: even 110 GB/day is ~40 TB/year against a
typical 600 TBW consumer rating.

## 8. Day to day

```bash
journalctl -u timelapse-capture -f
systemctl list-timers timelapse-encode.timer
find /var/lib/timelapse/frames -name '*.jpg' | wc -l    # ≈720/hour/camera
du -sh /var/lib/timelapse/frames/*
sudo systemctl start timelapse-encode.service           # force a run now
```

**Encoder CLI**

```
timelapse_encode.py [config] [--date YYYY-MM-DD] [--dry-run]
                             [--keep-frames] [--no-transfer]
```

Exit codes: `0` all good, `1` partial failure, `2` critical.

Changing the config requires `systemctl restart timelapse-capture`. The encoder
re-reads it on every run.

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
the query string are masked in the listing — `ask_secret` keeps them out of
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
| **Disable** a camera | The same — `enabled: false` hides it from the encoder too, which is easy to miss |
| Rename a camera | Everything under the old directory name |

```
  WARN  'Roof' has 1 un-encoded day(s) in /srv/frames/Roof
  The nightly encode only looks at cameras enabled in the config, so
  this would leave those frames on disk with nothing to encode them.
  Encode them first with:  timelapse encode --date 2020-01-02
  Remove it anyway? (y/N):
```

A rename offers to move the directory with it, so that case is normally
handled for you. Frames are never deleted — a removed camera's directory stays
on disk, and re-adding it under the same name picks it straight back up.

Editing `config.json` by hand still works (`timelapse config`), but then the
restart and the stranding checks are yours to remember.

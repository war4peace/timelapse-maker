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

## 3. Test before enabling anything

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
substream, Profile_3 the third stream** — picking the wrong one is the usual
reason NVR-generated timelapses come out low-resolution.

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
- **`av1_nvenc not available`** — distro ffmpeg builds often lack NVENC. Use a
  BtbN static build or `jellyfin-ffmpeg` and point `paths.ffmpeg` at it. The
  script falls back to HEVC then x264 rather than failing, but you lose AV1.

## 4. Enable

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

## 5. Transfer destination

`transfer.destination` is either a local path or an rsync remote spec — one code
path serves both.

**rsync over SSH (recommended, no stale-mount failure mode):**

```bash
sudo -u timelapse ssh-keygen -t ed25519
sudo -u timelapse ssh-copy-id user@nas
# destination: "user@nas:/mnt/user/timelapse/"
```

**CIFS/NFS mount:** add it to `/etc/fstab`, set `destination` to the local
mountpoint, and add that mountpoint to `ReadWritePaths=` in
`timelapse-encode.service`.

Set `transfer.enabled` to `false` to keep videos on the local disk. Note that
nothing then prunes `video_output` — add your own retention if you do this.

## 6. Sizing

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

## 7. Day to day

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

## 8. Adding a camera

Add one entry to `cameras[]`, run `timelapse_test.py --camera <Name>`, then
restart capture. No code changes. The frames directory is created on first
capture.

Renaming a camera orphans its existing frames — the encoder walks directories
under configured camera names only, so frames under the old name are stranded.
Let a rename take effect after a successful encode, or move the directory
yourself.

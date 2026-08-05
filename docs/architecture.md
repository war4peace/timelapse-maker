# Architecture

This document is the engineering reference: what the pieces are, why they are
shaped the way they are, and which properties must not be broken by future
edits. [install.md](install.md) is the operator guide; this is the developer
guide.

---

## 1. Purpose and scope

Produce daily timelapse videos from IP cameras, unattended.

The motivating problem is **continuity**, not image quality. NVR timelapse
features are built around recording sessions: Agent DVR and its peers can pull
full-resolution snapshots and produce perfectly good timelapses, but the
recording is tied to the camera connection. A camera reboot, or an ONVIF
setting change, ends the current file and starts a new one. A day then exists
as several fragments instead of one video, and stitching them back together
after the fact is exactly the work this avoids. Nor is there a built-in way to
move finished videos to another machine.

The design consequence is §2's split: capture holds no session state. Each
frame is an independent HTTP fetch named after its wall-clock time and written
into the day's directory. A camera that is unreachable for an hour contributes
no frames for that hour and the day still encodes to one file — shorter, but
whole. Nothing about a camera restarting is visible to the encoder at all.

**In scope:** pulling snapshots, encoding them into daily videos, reporting
results, shipping videos elsewhere.

**Explicitly out of scope:** motion detection, object detection, alerting on
camera content, recording video streams. Your NVR keeps doing all of that. This
system shares *no* code, config, database or runtime dependency with any NVR —
it only shares the cameras themselves, as one more HTTP client.

**Design goal:** unattended operation. It should survive camera outages, host
reboots, missed midnight runs, full disks and network failures without human
intervention, and it should say so on Discord when it can't.

---

## 2. System overview

```
┌──────────────┐   HTTP snapshot every N seconds
│   cameras    │◄──────────────────────────┐
│ Dahua / Hik  │                           │
│ Reolink/Tapo │                           │
└──────────────┘                           │
                                    ┌──────┴───────────────────┐
                                    │ timelapse_capture.py     │
                                    │ systemd service, always  │
                                    │ 1 thread per camera      │
                                    └──────┬───────────────────┘
                                           │ writes JPEGs
                                           ▼
                        ┌──────────────────────────────────────┐
                        │  frames/<Camera>/<YYYY-MM-DD>/*.jpg  │  ← the only
                        └──────────────────┬───────────────────┘    interface
                                           │ reads + deletes
                                    ┌──────┴───────────────────┐
                                    │ timelapse_encode.py      │
                                    │ systemd timer, 00:05     │
                                    │ sequential per camera    │
                                    └──┬────────────┬──────────┘
                                       │            │
                          videos/*.mkv │            │ embed
                                       ▼            ▼
                                 ┌──────────┐  ┌─────────┐
                                 │   NAS    │  │ Discord │
                                 │  (rsync) │  │ webhook │
                                 └──────────┘  └─────────┘
```

The two programs never talk to each other. They never run each other. They share
only the filesystem layout in §3. Either can be stopped, replaced or rewritten
independently.

**Why two processes rather than one:** they have opposite lifecycles. Capture
must run continuously and must not be interrupted; encoding is a batch job that
runs once a day, can take an hour, and can crash without consequence. Coupling
them would mean an encoder bug can stop capture — the one failure that loses
data permanently.

---

## 3. The on-disk contract

This is the integration point. Treat it as an API.

```
${paths.frames_root}/<CameraName>/<YYYY-MM-DD>/<HHMMSS>.jpg
${paths.video_output}/<CameraName>.<YYYYMMDD>.<container>
${paths.log_dir}/{capture,encode}.log
```

Invariants that both sides depend on:

| Invariant | Why |
|---|---|
| Filenames are zero-padded `HHMMSS` | Lexical sort == chronological sort. Nothing reads mtime. A PowerShell predecessor of this tool mixed `CreationTime` and `LastWriteTime` between its processing and deletion passes; making order a property of the *name* removes that class of bug entirely. |
| One directory per camera per local date | Encoding is a directory glob. Cleanup is one `rmtree`. Determining "is this day finished" is a string compare against today. No scanning 100k+ files to filter by date. |
| Files appear atomically | Capture writes `.<stem>.tmp` then `os.replace()`. A reader never sees a partial JPEG. `os.replace` is atomic *within a filesystem* — do not move the temp file to a different mount. |
| Dot-prefixed files are not frames | Temp files start with `.`; `glob("*.jpg")` skips them. Don't add non-frame files to a day directory without a dot prefix. |
| A day directory older than today is complete and owned by the encoder | Capture never writes to a past date. The encoder is free to delete it. |

Adding a camera means adding a directory. Nothing else in either program needs
to know.

---

## 4. Component reference

### 4.1 `timelapse_capture.py`

Long-running daemon. `systemd` `Type=simple`, `Restart=always`.

**Module state**

| Name | Purpose |
|---|---|
| `STOP` | `threading.Event`, set by SIGTERM/SIGINT. Every loop uses `STOP.wait(n)` rather than `time.sleep(n)` so shutdown is immediate. |
| `PAUSED` | `threading.Event`, set by `DiskGuard`. Capture threads check it and skip, but keep their scheduling loop running. |

**`HttpCamera(threading.Thread)`** — one per `method: "http"` camera.

- `run()` — the scheduling loop. Computes `next_t` as an absolute epoch multiple
  of `interval`, sleeps until it, fires, then advances. Because targets are
  absolute, the loop cannot accumulate drift from fetch latency. If it falls a
  full interval behind (slow camera, host suspend) it resyncs forward to the
  next boundary instead of replaying a backlog.
- `_grab(dt)` — one fetch. Validates size ≥ `min_bytes` and JPEG SOI (`FF D8`),
  writes temp, `os.replace`s into place.
- `_dest_path(dt)` — builds the destination, creating the day directory only
  when the date changes (cached in `self._last_dir`, so no `mkdir` syscall per
  frame).
  **Do not rename this method to `_target`.** `threading.Thread.__init__`
  assigns `self._target = None`, which silently shadows any method of that name;
  the symptom is `TypeError: 'NoneType' object is not callable` on every
  capture. This bug was hit during development. `self.name_` has a trailing
  underscore for the same reason — `Thread.name` is taken.
- Failure logging is throttled: the first failure logs, then every
  `log_every_n_failures`-th. An offline camera produces 2 log lines per hour, not
  720. Recovery logs once. External uptime monitoring is the real alerting path
  for camera reachability; this log is for post-hoc diagnosis.

**`RtspCamera(threading.Thread)`** — one per `method: "rtsp"` camera, for devices
with no HTTP snapshot endpoint (e.g. TP-Link Tapo).

Supervises a persistent `ffmpeg` doing `-vf fps=1/N` into the image2 muxer with
`-strftime 1 -strftime_mkdir 1`, so ffmpeg itself creates the `YYYY-MM-DD`
directory and produces the identical layout. On exit, logs stderr and restarts
after 10s.

**`DiskGuard(threading.Thread)`**

Polls `shutil.disk_usage(frames_root).free` every 300s. Below `min_free_gb`, sets
`PAUSED` and logs at ERROR. Clears at 110% of threshold (hysteresis, so it can't
flap). Capture threads keep their timing loops running while paused so they
resume on-boundary. Disabled by setting `min_free_gb: 0`.

### 4.2 `timelapse_encode.py`

Batch job. `systemd` `Type=oneshot` + timer at 00:05, `Persistent=true`.

Pipeline, in `main()`:

1. `select_encoder()` → probes `av1_nvenc`, `hevc_nvenc`, `libx264` in order by
   encoding one synthetic frame at `PROBE_SIZE` (512×512). NVENC rejects small
   frames: measured on an RTX 3090, `hevc_nvenc` fails 128×128 outright with
   `InitializeEncoder failed: invalid param (8): Frame dimensions`. 512 is
   clear of every documented minimum and costs nothing.
   **The probe pins `-pix_fmt` to `PIX_FMT` (yuv420p), the same constant
   `encode_day()` builds its filter chain from.** `testsrc` emits rgb24; left
   to negotiate, ffmpeg picks the closest format the encoder advertises, and
   `av1_nvenc` advertises `yuv444p`. NVENC on Ada cannot do AV1 in 4:4:4, so
   the capability check failed and reported "No capable devices found" — which
   declared an RTX 4060 incapable of AV1 it does perfectly well in 4:2:0. A
   probe that encodes something the pipeline never produces is worse than no
   probe. Both now read one constant so they cannot drift.
   **The probe must never discard ffmpeg's stderr.** Two failure modes are
   indistinguishable by exit code but need opposite fixes:
   `Unknown encoder 'av1_nvenc'` means the ffmpeg build lacks it (install
   jellyfin-ffmpeg or a BtbN build), while `No capable devices found` means the
   GPU or driver cannot do it. `encoder_hint()` maps the message to a cause and
   `list_encoders()` confirms whether the codec is compiled in at all. An
   earlier version guessed from the codec name and told an RTX 4060 owner their
   GPU was too old for AV1.
2. `find_pending()` → every date directory across all enabled cameras whose name
   sorts `< today`, oldest first, capped to the newest `max_backlog_days`
   distinct dates.
3. `encode_day()` per job, sequentially.
4. `rmtree` the day directory on `OK` (unless `--keep-frames` or config says
   otherwise).
5. `transfer()` → rsync.
6. `send_discord()` → embed with a monospace summary table plus fields.
7. Exit `0` all-good, `1` partial failure, `2` critical.

**`encode_day()`** is the core. Notable choices:

- `valid_frames()` filters on file size and a 3-byte SOI read — cheap, ~17k stats
  and 3-byte reads. It should almost never reject anything now that capture
  writes atomically; it exists to defend the encode against the RTSP path and
  against filesystem damage.
- `probe_dimensions()` ffprobes the *first* frame, and the scaler is pinned to
  that size. A stray odd-sized snapshot mid-day would otherwise break the concat
  demuxer. Dimensions are rounded down to even — NVENC requires it. This call
  lives *inside* the try block: a camera whose first frame won't probe must fail
  only itself, not abort the whole run.
- **Colour handling.** JPEG decodes as `yuvj420p`, full range. The filter chain
  is `scale=W:H:in_range=full:out_range=limited,format=yuv420p` and the output is
  tagged `-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc
  bt709`. A predecessor passed `-color_range 2`, which *tagged* full range
  without *converting* — correct only in players that honour the tag. Converting
  and tagging consistently is correct everywhere. If you change one of these,
  change both.
- `write_concat_list()` — ffmpeg concat demuxer format is `file 'path'` with
  literal `'` escaped as `'\''`. Written with plain `open()` in UTF-8, **no BOM**
  — the PowerShell predecessor hit `unknown keyword '﻿file'` because
  `Add-Content -Encoding UTF8` emitted one.
- ffmpeg gets `-r` twice: before `-i` (input rate, i.e. how fast to consume
  stills) and after (output rate). Both must be the same value or frames get
  duplicated/dropped.
- Failure is contained per camera-day. An exception is caught, recorded in the
  result dict, the partial output file is unlinked, and the loop continues.

**`transfer()`** shells out to `rsync` with `--remove-source-files`.

`mount_problem()` runs first when `transfer.require_mountpoint` is set. An
unmounted CIFS/NFS destination is an ordinary empty local directory, so rsync
would fill the local disk and then delete the originals — strictly worse than
not transferring. Refusing returns a transfer failure, which by the rule below
does not spoil the encode; the videos stay in `video_output` and ship next run.
`true` walks up from the destination and fails if it reaches the filesystem
root; a string checks that exact path with `os.path.ismount`, which is more
precise when an intermediate directory is its own filesystem. One code path
serves both a local mount (`/mnt/nas/timelapse/`) and a remote spec
(`user@nas:/mnt/user/timelapse/`) — rsync doesn't care. A missing `rsync` binary
or a non-zero exit returns a failure dict rather than raising: **a transfer
failure must not turn a successful encode into a critical abort**, because the
encode result and the Discord summary are still valid and the videos are still
on disk. This was a real bug found in testing.

**`send_discord()`** uses `urllib` only — no dependency. Posts through
`post_webhook()`, which sets an explicit `User-Agent`. This is not cosmetic:
Discord is behind Cloudflare, which rejects urllib's default
`Python-urllib/3.x` with **HTTP 403, Cloudflare error 1010**, before the
request reaches Discord at all. With the documented
`DiscordBot ($url, $version)` form the same request reaches the API. Truncates
to Discord's limits (4096 description, 1024 per field value, 25 fields). Failure is logged and
swallowed, catching `Exception` deliberately: a socket timeout is not a
`URLError`, and notification is never load-bearing — least of all in the
critical-failure handler, where an exception would mask the original error.

### 4.3 `timelapse_test.py`

Pre-flight checker, run manually. Never run by systemd. Not imported by the
other two (it imports *from* `timelapse_encode`, one-way, so the encoder probe
logic cannot drift between the two).

| Check | What it catches |
|---|---|
| `test_http` / `test_rtsp` | wrong credentials, wrong auth scheme, non-JPEG responses, slow cameras relative to the interval |
| `probe_profiles` | ONVIF `Profile_N` pointing at a substream. Profile numbering is not consistent across vendors, so it is easy to configure a low-resolution stream without noticing |
| `test_encoders` | ffmpeg built without NVENC |
| `test_disk` | projects daily usage from *measured* snapshot sizes against actual free space |
| `test_transfer` | unmounted CIFS path, missing SSH key |
| `test_discord` | bad webhook URL |

Samples land in a temp directory (override with `TIMELAPSE_TEST_DIR`) for visual
inspection. `--probe-profiles` short-circuits everything else.

### 4.4 `timelapse_setup.py`

Configuration wizard. Run by `install.sh`, or standalone as `timelapse setup`.
Writes `config.json` and nothing else — it never touches systemd, never enables
anything, and is safe to re-run.

**Storage discovery** parses `/proc/mounts` rather than shelling out to `lsblk`
or `df`, so it has no dependency beyond the stdlib. Filtering, in order:

| Rejected | Why |
|---|---|
| `PSEUDO_FS` fstypes | tmpfs, overlay, squashfs, cgroup… are not real storage |
| `NETWORK_FS` fstypes | `os.replace()` has no atomicity guarantee across the wire, and 17k small writes/camera/day over a network is painful. Fine as a *transfer* target, not as `frames_root`. |
| `SKIP_PREFIXES` mountpoints | `/snap`, `/boot`, `/var/lib/docker`, WSL internals |
| `ro` mounts | cannot hold frames |
| sources not under `/dev/` | bind mounts and synthetic sources |
| duplicate devices | one device mounted repeatedly; the shortest mountpoint wins |

Free space comes from `os.statvfs` (`f_bavail`, i.e. space available to a
non-root user, not `f_bfree`). Rotational status comes from
`/sys/block/<dev>/queue/rotational`, with `_base_device()` mapping `sda1 → sda`,
`nvme0n1p2 → nvme0n1` and `/dev/mapper/*` through its symlink. Every one of
these is best-effort: a `None` rotational just means the SSD/HDD hint is
omitted.

`recommend()` prefers the roomiest filesystem that is not `/`, provided it has
at least 20 GB — the OS disk is a poor place to write 17k files a day.

**Two things it exists to get right**, both of which are silent failures by hand:

- Credentials destined for a query string are `urllib.parse.quote`d. A password
  containing `&`, `#`, `=` or `%` otherwise breaks the URL in a way that looks
  like an auth failure.
- `--print-paths` emits the minimal set of directories systemd must allow, which
  `install.sh` splices into `ReadWritePaths=`. Paths already covered by a parent
  in the set are dropped.

**Input handling.** `init_tty()` picks a source: stdin if it is a terminal,
otherwise `/dev/tty`, otherwise defaults-only. It must *never* silently read a
piped stdin — under `curl … | bash` that pipe is the installer script itself.
`--stdin` opts in explicitly for scripted runs. Passwords go through
`getpass` when a real terminal is present, so they stay out of scroll-back.

### 4.5 `install.sh`

Bootstrap. Detects the package manager (apt/dnf/yum/pacman/zypper/apk), installs
dependencies, creates the `timelapse` system account, places scripts, units and
a `timelapse` command wrapper, then calls the wizard and offers to enable.

It uses the checkout it is running from when there is one, and otherwise
downloads a tarball from `codeload.github.com` — so the same script serves both
`git clone && sudo ./install.sh` and the piped one-liner.

`sync_units()` is the important part: it rewrites `User=`, `Group=`,
`ExecStart=` and `ReadWritePaths=` in the installed units from the config the
wizard just wrote. Prompts read from `/dev/tty` for the same reason the wizard's
do. `--uninstall` removes programs and units but never captured data.

---

## 5. Configuration reference

Single JSON file, default `/etc/timelapse/config.json`, mode `640`. Both programs
take an optional path as their first positional argument. See
`config/config.example.json`.

### `paths`
| Key | Notes |
|---|---|
| `frames_root` | Must be on a filesystem with room for ~2 days of frames. Temp files are created here, so it must be one mount. |
| `video_output` | Emptied by `transfer()` each night. |
| `log_dir` | Rotating logs, 8 MB × 3 (capture) / × 5 (encode). |
| `ffmpeg`, `ffprobe` | Absolute paths. Point at a BtbN static build if the distro build lacks NVENC. |

### `capture`
| Key | Default | Notes |
|---|---|---|
| `interval_seconds` | 5 | 17,280 frames/day → 4:48 at 60fps. Changing this changes `Cov%` maths and video length. |
| `timeout_seconds` | 4 | Must stay below `interval_seconds`, or a slow camera stalls its own next fetch. |
| `min_bytes` | 4096 | Shared with the encoder's validity check. |
| `min_free_gb` | 60 | `0` disables DiskGuard. |
| `log_every_n_failures` | 60 | At 5s intervals, 60 = one log line per 5 minutes of downtime. |

### `encode`
| Key | Default | Notes |
|---|---|---|
| `framerate` | 60 | Applied to both input and output. |
| `container` | `mkv` | Extension only; ffmpeg infers the muxer. |
| `gop` | 120 | 2s at 60fps. Lower = better scrubbing, larger files. |
| `av1_preset` / `av1_cq` | `p6` / 26 | p1 fastest … p7 slowest. Lower cq = higher quality. |
| `hevc_cq`, `x264_crf` | 24, 20 | Fallback encoders only. |
| `min_frames` | 100 | Below this, `SKIP` rather than produce a 2-second video. |
| `delete_frames_on_success` | true | |
| `max_backlog_days` | 7 | Bounds a catch-up run after long downtime. |

### `transfer`
| Key | Notes |
|---|---|
| `destination` | A local directory or an rsync remote spec; one code path serves both. |
| `rsync_args` | Defaults include `--remove-source-files`; if you drop that, set `delete_local_after_transfer` accordingly or files accumulate. On a CIFS mount `-a` may exit 23 because owner/group cannot be set — `tools/setup-cifs-transfer.sh` measures which flags work on your share. |
| `require_mountpoint` | `false` (default), `true`, or an explicit mount path. Refuses to transfer when the destination is not on a mounted filesystem. Only meaningful for a local destination; ignored for a remote spec. |

### `cameras[]`
| Key | Notes |
|---|---|
| `name` | **Used as the directory name.** Renaming a camera orphans its existing frames — the encoder walks directories under configured camera names, so frames under the old name are stranded. Rename with care. |
| `enabled` | Excluded from both capture and encode when false. |
| `method` | `http` or `rtsp`. |
| `auth` | `digest`, `basic`, or `none`. Cameras that put credentials in the query string → `none`. |
| `_note` | Ignored by code. Keys starting with `_` are documentation. |

---

## 6. Failure handling matrix

| Failure | Behaviour |
|---|---|
| One camera unreachable | That thread logs (throttled) and keeps trying. Others unaffected. Nightly `Cov%` shows the gap. |
| Camera returns non-JPEG / truncated | Frame rejected at capture; nothing written. |
| Disk fills | DiskGuard pauses all capture at threshold, logs ERROR, resumes with hysteresis. |
| Capture process crashes | systemd restarts after 15s. Frames already written are intact. |
| Host down over midnight | `Persistent=true` runs the timer on boot; `find_pending` picks up every missed day. |
| One camera's encode fails | Recorded `FAIL`, partial output deleted, **frames retained**, loop continues, Discord lists it. |
| No encoder available | Critical abort + Discord alert. Frames retained. |
| `rsync` missing or failing | Reported as a transfer failure in the summary; encode still counts as success; videos stay in `video_output` and ship next run. |
| NAS share not mounted | With `require_mountpoint`, the transfer is refused before rsync runs, so nothing is written to the local disk and no originals are deleted. Without it, rsync silently fills the local filesystem. |
| Discord unreachable | Logged, swallowed. |
| Encode never succeeds for a camera | Frames accumulate. `max_backlog_days` bounds the work; DiskGuard bounds the damage; nightly Discord shows repeated failures. |

---

## 7. Extension points

Ordered roughly by how much of the existing design they disturb.

**Add a camera** — one config entry. No code.

**New snapshot protocol** — subclass `threading.Thread` in
`timelapse_capture.py` following `HttpCamera`, honour `STOP` and `PAUSED`, write
via temp + `os.replace`, and dispatch on `method` in `main()`. Nothing in the
encoder changes.

**Per-camera settings** (different interval, resolution, quality) — the camera
dict is already passed whole to the thread constructor; read from `cam` with a
fallback to the global. The encoder's `Cov%` maths assumes the global interval
and would need the per-camera value threading through `build_summary`.

**Different notification sink** — `send_discord()` is the only
outbound-notification function and takes `(title, description, color, fields)`.
Add a sibling and call both from `main()`; keep failures swallowed.

**Camera restart on hang** — natural home is a new module invoked by the capture
daemon's failure path, gated on consecutive-failure count, with a cooldown so it
can't reboot-loop a camera. Detection already exists (`consec_fail`); this is
remediation only.

**Frame retention beyond encode** — set `delete_frames_on_success: false` and add
a separate age-based sweeper. Do not add retention logic to `encode_day()`; keep
"encode" and "delete" separable.

**Parallel encoding** — currently sequential and deliberately so. NVENC session
limits on consumer GeForce cards are low, and the real bottleneck is CPU JPEG
decode, which already uses all cores per job. Parallelism would contend, not
help.

---

## 8. Known limitations

- **DST fall-back**: local time repeats an hour, so `HHMMSS` names collide.
  `HttpCamera._dest_path` appends `-1`, `-2` … and keeps everything, at the cost
  of that hour sorting slightly oddly. The RTSP path (ffmpeg `-strftime`) has no
  such guard and will overwrite. Video length varies on DST days (23h or 25h).
- **PTZ cameras** jump-cut between presets in the finished video.
- **Cameras are polled independently**, so frames across cameras are not
  synchronised to the same instant.
- **A frozen-but-reachable camera** produces a full frame count and a static
  video. No automatic detection; the tell is a suspiciously small output file.
- **Video length varies with capture coverage** — a camera down for 6 hours
  produces a shorter video, not a video with gaps.
- `Cov%` is computed against a nominal `86400/interval` and will read ~104% or
  ~96% on DST days.

---

## 9. Testing notes

```bash
python3 -m unittest discover -s tests -t tests -p 'test_*.py'   # fast, no deps
python3 tests/smoke_test.py                                     # needs ffmpeg
```

**Unit tests** (`tests/test_*.py`, stdlib `unittest`, ~115 cases, under a
second) cover the pure logic: frame validation, concat-list escaping,
`find_pending` backlog selection, `human_*` formatting, the storage scan's
filtering and deduplication, `_base_device` partition stripping, `recommend`,
`writable_paths`, credential quoting, and `_dest_path` including the DST
collision suffixes. Anything needing a camera, a GPU or systemd is out of scope
here by design.

Three seams exist purely for testability, and should be preserved:
`scan_filesystems(mounts_path, statvfs, rotational)` and
`_base_device(source, sys_block)` take injectable inputs, so the awkward cases
(network mounts, read-only duplicates, a device mounted twice, nvme partition
naming) can be exercised on any machine rather than only where they happen to
exist.

**The smoke test** builds a synthetic capture day (150 good frames plus two
corrupt ones, named as real captures), runs `timelapse_encode.py` over it, and
asserts what has actually broken before: bad frames rejected, exact output
duration, `color_range=tv`, `color_space=bt709`, `pix_fmt=yuv420p`, frames
deleted afterwards. Needs ffmpeg but no GPU — it falls back to libx264.

**The suite was mutation-checked** when written: 18 deliberate breakages
introduced one at a time, 16 caught. The two misses were tests passing for the
wrong reason — a network-filesystem case rejected by the source filter before
the fstype rule ran, and a deduplication case filtered by a skip-prefix before
deduplication happened. Both are fixed, and both are worth remembering as the
failure mode to watch for when adding tests here: assert that the rule you
*mean* to test is the one doing the work.

The rest was verified by hand. The methods, in case you want to re-verify after
changes:

- **Capture daemon** — two cameras against a local `http.server`, one valid URL
  and one 404. Confirmed: files land on exact interval boundaries, correct
  directory layout, no leftover `.tmp`, failure throttling works, clean SIGTERM
  shutdown.
- **Installer and wizard** — Ubuntu 24.04 with real systemd. Confirmed: package
  detection and dependency install, service account creation, unit templating
  (`systemd-analyze verify` clean), a live capture run against a local HTTP
  camera writing frames **at a non-default path under `ProtectSystem=strict`**
  — which is what actually exercises the `ReadWritePaths` derivation — a nightly
  encode as the service user, and a clean uninstall. Worth repeating on a distro
  using a different package manager; only apt has been exercised.
- **Query-string credential encoding** — a password containing `@ & = #` fed
  through the wizard and parsed back out of the generated URL unchanged.
- **Profile probe** — mock ONVIF endpoint serving 2560×1440 / 704×576 / 640×480
  for Profile_1/2/3. Confirmed it identifies the largest and recommends the
  config change.
- **Transfer** — stub `rsync` on `PATH`.

Regenerating a synthetic frame set:

```bash
ffmpeg -f lavfi -i testsrc=size=1280x720:rate=1 -frames:v 400 -q:v 3 src_%06d.jpg
python3 -c "
import os,glob
for i,f in enumerate(sorted(glob.glob('src_*.jpg'))):
    t=i*5; os.rename(f,'%02d%02d%02d.jpg'%(t//3600,(t%3600)//60,t%60))"
```

---

## 10. File inventory

```
install.sh                       bootstrap installer, 467 lines
tools/setup-cifs-transfer.sh     CIFS mount setup and verification
scripts/timelapse_capture.py     daemon, 340 lines
scripts/timelapse_encode.py      batch job, 491 lines
scripts/timelapse_test.py        pre-flight checks, 320 lines
scripts/timelapse_setup.py       configuration wizard, 850 lines
tests/_support.py                path setup and fakes
tests/test_capture.py            unit tests
tests/test_encode.py             unit tests
tests/test_setup.py              unit tests
tests/smoke_test.py              end-to-end encode check, needs ffmpeg
config/config.example.json       template; the real config.json is gitignored
service/timelapse-capture.service
service/timelapse-encode.service
service/timelapse-encode.timer
docs/architecture.md             this file
docs/install.md                  operator guide
```

Dependencies: Python 3.9+ stdlib, `requests`, `ffmpeg`/`ffprobe` (NVENC for
AV1/HEVC), `rsync`. No virtualenv required, one pip package, no database.

Both systemd units use `ProtectSystem=strict` with an explicit `ReadWritePaths`.
**Any new write path — a different frames root, a CIFS mountpoint for transfer —
must be added there**, or writes fail with a confusing read-only error.

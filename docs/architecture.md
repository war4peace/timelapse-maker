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
no frames for that hour and the day still encodes to one file: shorter, but
whole. Nothing about a camera restarting is visible to the encoder at all.

**In scope:** pulling snapshots, encoding them into daily videos, reporting
results, shipping videos elsewhere.

**Explicitly out of scope:** motion detection, object detection, alerting on
camera content, recording video streams. Your NVR keeps doing all of that. This
system shares *no* code, config, database or runtime dependency with any NVR;
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
them would mean an encoder bug can stop capture, the one failure that loses
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
| Files appear atomically | Capture writes `.<stem>.tmp` then `os.replace()`. A reader never sees a partial JPEG. `os.replace` is atomic *within a filesystem*; do not move the temp file to a different mount. |
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

**`HttpCamera(threading.Thread)`**: one per `method: "http"` camera.

- `run()`: the scheduling loop. Computes `next_t` as an absolute epoch multiple
  of `interval`, sleeps until it, fires, then advances. Because targets are
  absolute, the loop cannot accumulate drift from fetch latency. If it falls a
  full interval behind (slow camera, host suspend) it resyncs forward to the
  next boundary instead of replaying a backlog.
- `_grab(dt, timeout=None)`: one fetch. Validates size ≥ `min_bytes` and JPEG
  SOI (`FF D8`), writes temp, `os.replace`s into place. Nothing is written until
  both checks pass, so a failed attempt leaves no partial file and no
  `_dest_path` collision for a retry to trip over.
- `_retry_grab(dt, deadline, first_exc)` / `_retry_timeout(deadline, now)`: one
  second attempt inside the same tick, controlled by `retry_within_tick`.
  Rationale: an ONVIF snapshot endpoint on a busy camera answers `500` in
  milliseconds rather than queueing, so the tick was being discarded with ~98%
  of its budget unspent.

  The field report that prompted this turned out **not** to be a case it can
  fix: the camera was being polled by AgentDVR's own timelapse schedule at the
  same time, which is a busy *window*, the 0% row below. Documented as a snag in
  `install.md` §4 instead. The retry stands on the blip row alone; know that
  before extending it.

  **Know what this does and does not fix.** Measured against a local server
  reproducing both failure shapes, with the failure phase anchored to the tick
  grid so both arms meet an identical pattern:

  | Failure shape | Recovery |
  |---|---|
  | Per-request blip (contention, transient reset) | ~58% |
  | Busy *window* longer than one interval | **0%** |

  The zero is structural, not a tuning failure: if the camera is refusing for
  longer than `interval_seconds`, the next tick already *is* a retry, so nothing
  inside this tick can beat it. Do not try to fix it by lengthening the delay:
  that only moves the frame's real timestamp away from its filename. Hence the
  `consec_fail` guard: a tick whose predecessor also failed is part of an outage
  and is not retried, which cut wasted requests in the window case by half
  (18 → 9 over a 72s run) while costing only 67% → 58% on blips.

  The delay/timeout decision is **purely budget arithmetic**,
  `(deadline - RETRY_GUARD) - (now + RETRY_DELAY)`, declined below
  `RETRY_MIN_BUDGET`. This is deliberately not a fast-failure heuristic: an
  attempt that *timed out* has already consumed the tick, so the same
  subtraction rejects it with no special case. Worst-case finish is
  `deadline - RETRY_GUARD`, which is what guarantees a retry can never also cost
  the following frame. `deadline` is the loop's `next_t`, so a resync forward
  legitimately widens the window.

  A rescued tick increments `ok`, not `fail`; those counters mean *frames on
  disk*, which is what the encoder's `Cov%` divides. `retried` is tracked
  separately and reported in the `capture stopped` line.
- `_dest_path(dt)`: builds the destination, creating the day directory only
  when the date changes (cached in `self._last_dir`, so no `mkdir` syscall per
  frame).
  **Do not rename this method to `_target`.** `threading.Thread.__init__`
  assigns `self._target = None`, which silently shadows any method of that name;
  the symptom is `TypeError: 'NoneType' object is not callable` on every
  capture. This bug was hit during development. `self.name_` has a trailing
  underscore for the same reason: `Thread.name` is taken.
- Failure logging is throttled: the first failure logs, then every
  `log_every_n_failures`-th. An offline camera produces 2 log lines per hour, not
  720. Recovery logs once. External uptime monitoring is the real alerting path
  for camera reachability; this log is for post-hoc diagnosis.

**`RtspCamera(threading.Thread)`**: one per `method: "rtsp"` camera, for devices
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
   the capability check failed and reported "No capable devices found", which
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

- `valid_frames()` filters on file size and a 3-byte SOI read: cheap, ~17k stats
  and 3-byte reads. It should almost never reject anything now that capture
  writes atomically; it exists to defend the encode against the RTSP path and
  against filesystem damage.
- `probe_dimensions()` ffprobes the *first* frame, and the scaler is pinned to
  that size. A stray odd-sized snapshot mid-day would otherwise break the concat
  demuxer. Dimensions are rounded down to even; NVENC requires it. This call
  lives *inside* the try block: a camera whose first frame won't probe must fail
  only itself, not abort the whole run.
- **Colour handling.** JPEG decodes as `yuvj420p`, full range. The filter chain
  is `scale=W:H:in_range=full:out_range=limited,format=yuv420p` and the output is
  tagged `-color_range tv -colorspace bt709 -color_primaries bt709 -color_trc
  bt709`. A predecessor passed `-color_range 2`, which *tagged* full range
  without *converting*, correct only in players that honour the tag. Converting
  and tagging consistently is correct everywhere. If you change one of these,
  change both.
- `write_concat_list()`: ffmpeg concat demuxer format is `file 'path'` with
  literal `'` escaped as `'\''`. Written with plain `open()` in UTF-8, **no BOM**;
  the PowerShell predecessor hit `unknown keyword '﻿file'` because
  `Add-Content -Encoding UTF8` emitted one.
- ffmpeg gets `-r` twice: before `-i` (input rate, i.e. how fast to consume
  stills) and after (output rate). Both must be the same value or frames get
  duplicated/dropped.
- Failure is contained per camera-day. An exception is caught, recorded in the
  result dict, the partial output file is unlinked, and the loop continues.

**`transfer()`** shells out to `rsync` with `--remove-source-files`. It globs
the whole of `video_output`, not just this run's output, which is what makes a
failed night ship itself later with no bookkeeping. It is called even when
`find_pending()` returned no jobs: the retry used to be reachable only from a
run that also had something to encode, so the obvious repair after remounting
a share, re-running the encode, was the one path that never retried. A no-op
run whose transfer fails exits `1` rather than `0`, so it surfaces as a failed
unit.

`mount_problem()` runs first when `transfer.require_mountpoint` is set. An
unmounted CIFS/NFS destination is an ordinary empty local directory, so rsync
would fill the local disk and then delete the originals, strictly worse than
not transferring. Refusing returns a transfer failure, which by the rule below
does not spoil the encode; the videos stay in `video_output` and ship next run.
`true` walks up from the destination and fails if it reaches the filesystem
root; a string checks that exact path with `os.path.ismount`, which is more
precise when an intermediate directory is its own filesystem. One code path
serves both a local mount (`/mnt/nas/timelapse/`) and a remote spec
(`user@nas:/mnt/user/timelapse/`); rsync doesn't care. A missing `rsync` binary
or a non-zero exit returns a failure dict rather than raising: **a transfer
failure must not turn a successful encode into a critical abort**, because the
encode result and the Discord summary are still valid and the videos are still
on disk. This was a real bug found in testing.

**`send_discord()`** uses `urllib` only, no dependency. Posts through
`post_webhook()`, which sets an explicit `User-Agent`. This is not cosmetic:
Discord is behind Cloudflare, which rejects urllib's default
`Python-urllib/3.x` with **HTTP 403, Cloudflare error 1010**, before the
request reaches Discord at all. With the documented
`DiscordBot ($url, $version)` form the same request reaches the API. Truncates
to Discord's limits (4096 description, 1024 per field value, 25 fields). Failure is logged and
swallowed, catching `Exception` deliberately: a socket timeout is not a
`URLError`, and notification is never load-bearing, least of all in the
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

It also hosts `--usage` (`timelapse usage`), which is a report rather than a
check but belongs to the same "inspect this installation" job and reuses
`test_disk`'s free-space handling. `report_usage()` walks `frames_root` with
`os.scandir` (a stat per frame, so seconds on a six-figure tree) and prints
frames, bytes and date range per camera.

Its real purpose is the third column. It compares directories on disk against
the config and flags two states that `du` cannot show you:

- **`ORPHAN`**: a directory with no config entry at all.
- **`disabled`**: an entry with `enabled: false`.

`find_pending()` walks only cameras *enabled* in the config, so in both cases
nothing will ever encode or delete those frames. They are the usual answer to
"why is the disk full", and disabling being as final as removing is the part
nobody expects. Leftover `.tmp` files are counted separately: they are captures
that died between `write()` and `os.replace()`, and folding them into the frame
total would misreport both count and size.

Samples land in a temp directory (override with `TIMELAPSE_TEST_DIR`) for visual
inspection. `--probe-profiles` short-circuits everything else.

### 4.4 `timelapse_setup.py`

Configuration wizard. Run by `install.sh`, or standalone as `timelapse setup`.
Writes `config.json` and nothing else: it never touches systemd, never enables
anything, and is safe to re-run.

**Config backups** are taken by `write_config()`, which is the single write
path for the wizard, all four `--*-only` sections and every camera shortcut.
Five are kept, named `config.json.bak.<YYYYmmdd-HHMMSS>`. Two things here are
not obvious:

- **The counter for a repeated second is `max(used) + 1`, never the first free
  slot.** Pruning leaves holes, and refilling one gives the newest backup the
  oldest-sorting name, so it is deleted immediately. Measured before the fix:
  eight writes inside one second kept backups 1 to 5 and threw away 6, the
  newest. The same shape of bug as any "find the first gap" allocator paired
  with deletion.
- **Backups sort on the parsed `(stamp, counter)`, not on the name.** `-10`
  sorts below `-2` as text, and ten config writes inside a second is a shell
  loop rather than a hypothetical. The unstamped `config.json.bak` that 0.1.1
  and earlier wrote parses to `("", 0)` and therefore sorts first, which is
  correct: it is older than anything written since.

`timelapse config` hands the file to `$EDITOR` and so does not pass through
`write_config()` at all; the wrapper calls `--backup-now` first for exactly
that reason. **A new write path means adding a backup call, or that path
silently opts out of the history.**

`restore_config()` deliberately does not load the current config first. "I
broke it" and "it is gone" are the two reasons to run it, and requiring a
readable config would refuse precisely then. It backs the current one up
before overwriting, which is what makes a wrong choice recoverable, and
restarts both daemons afterwards because both read the config only at startup.

**Camera management** (`--cameras-only`) is a menu, and every action in it is
also a flag: `-l`, `-a`, `-e:CAM`, `-x:CAM`, `-t:CAM`, `-r:CAM`. Three things
about that are not obvious:

- **`-e:CAM` reaches argparse as `":CAM"`**, because a short option swallows
  whatever is attached to it. `strip_colon()` puts it back. Nothing is lost:
  `sanitise_name()` keeps only alphanumerics, `-` and `_`, so no camera can be
  called `:anything`.
- **A name beats a number.** The number is the position `-l` prints, and it is
  an artefact of insertion order, because nothing in the config schema is a
  stable id. `#2` forces the position for a config with a camera called `2`.
  Nothing is ever fuzzy-matched: one of these actions is "remove".
- **The writing actions refuse to run without a terminal.** Accepting defaults
  would write a camera entry pointing at nothing. `-l` and `-t` do not write
  and so do not need one, and `-t` neither backs up the config nor offers a
  capture restart.

`CAMERA_ACTIONS` and the flag list are checked against each other by a test:
an action added to the menu with no flag behind it is the drift to expect.

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
at least 20 GB; the OS disk is a poor place to write 17k files a day.

**Two things it exists to get right**, both of which are silent failures by hand:

- Credentials destined for a query string are `urllib.parse.quote`d. A password
  containing `&`, `#`, `=` or `%` otherwise breaks the URL in a way that looks
  like an auth failure.
- `--print-paths` emits the minimal set of directories systemd must allow, which
  `install.sh` splices into `ReadWritePaths=`. Paths already covered by a parent
  in the set are dropped.

**Focused modes.** The wizard is also the maintenance tool, so a change after
install never means reinstalling or re-answering everything:

- `--transfer-only` (`timelapse transfer`): just the destination, including
  re-deriving `ReadWritePaths=` when a share is added after the fact.
- `--cameras-only` (`timelapse cameras`): add/edit/remove/enable/test, looping
  on a listing. Both load the existing config, change one section, and write it
  back through `write_config()`, so ownership and the `.bak` copy are handled
  the same way as a full run.

`--cameras-only` carries two responsibilities that are easy to lose:

1. **It restarts `timelapse-capture.service`.** The daemon reads its camera list
   once at startup, the same trap `install.sh` had when it replaced scripts
   under a live service. `restart_capture_if_running()` asks, and says what to
   run if declined. Cameras never change paths, so the units themselves are
   untouched.
2. **It refuses to strand frames silently.** `find_pending()` in the encoder
   iterates cameras *enabled* in the config and looks for
   `<frames_root>/<name>/`, so removing a camera, **disabling** one, or renaming
   one without moving its directory all orphan whatever it has already
   captured, and permanently, since nothing else will ever encode it.
   `warn_stranded()` counts the un-encoded day directories and names the
   `timelapse encode --date` that would rescue them; `rename_camera_frames()`
   offers to move the directory instead. Disabling being just as destructive as
   removing is the non-obvious half, and is why `x` warns as loudly as `r`.

`redact_url()` masks `password=` in the listing. Not theatre: `ask_secret()`
exists to keep credentials out of scroll-back, and printing the camera table
would hand them straight back. The listing elides the *middle* of a long URL
rather than the tail, because Reolink-style URLs are identical for their first
40 characters and a plain truncation both makes every camera look the same and
cuts off the `***`, reading as though nothing were masked.

**`choose_web()`** is the wizard step for the web UI, and `--web-only` re-runs
just it, same reasoning as `--transfer-only`: turning the UI on later must not
mean walking the whole wizard, and a feature the wizard never offers is one
nobody finds. It previews *where videos will be read from* and why, because
that is the surprising part (see `resolve_library()` in §4.5), states the lack
of authentication before asking for the bind address, and warns when the answer
is not loopback. `create_web_state_dir()` makes the index directory here rather
than in the service, which could not create it: `ReadWritePaths` naming a
missing directory stops the unit dead, and inside the sandbox its parent is
read-only.

**Input handling.** `init_tty()` picks a source: stdin if it is a terminal,
otherwise `/dev/tty`, otherwise defaults-only. It must *never* silently read a
piped stdin: under `curl … | bash` that pipe is the installer script itself.
`--stdin` opts in explicitly for scripted runs. Passwords go through
`getpass` when a real terminal is present, so they stay out of scroll-back.

### 4.5 `timelapse_web.py`

Long-running, `Type=simple`, `Restart=on-failure`. Optional: it exits 0 when
`web.enabled` is false, which is why the unit is `on-failure` rather than
`always`: that exit is a decision to respect, not a crash to restart through.

**Read-only is a structural property, not a promise.** The service writes
exactly one thing, its sqlite index, and `ReadWritePaths` names that
directory and nothing else. The video library, the captured frames and
`config.json` are all read-only to it, enforced by the sandbox rather than by
the source. Verified: a process with this unit's properties can write
`/var/lib/timelapse/web` and cannot write either the library or the frames.

Keep that list to one entry. Reusing the capture/encode `ReadWritePaths` would
hand a network-facing service write access to every captured frame in exchange
for nothing, which is why `sync_units()` templates the web unit separately and
`timelapse_setup.py` has a separate `--print-web-paths`.

It logs only to journald: a rotating log file would be a second writable path
for no benefit.

**`PrivateTmp=true` hides `/tmp` and `/var/tmp` from the unit.** A library
placed under either is invisible to the service and the page reports it as
unreadable, which is correct but baffling. Cost an hour of a test run; worth
knowing before someone points `library_root` at a scratch directory.

**The bind address is settled against the kernel, not against a list.**
`check_bind()` binds the address for real (SO_REUSEADDR set, matching the
server; bind only, never listen, closed at once) because the kernel is the
authority and the failure modes need telling apart. An address this host does
not have is the silent one worth catching: the service starts, logs the
address it is serving and is simply unreachable, with nothing in the journal
naming the cause. A port already in use is accepted with a note, since the
usual holder is the web UI itself being reconfigured. A port below 1024 is
refused without probing at all: the wizard normally runs as root and the
service does not, so that probe would pass and prove nothing.

`lan_address()` asks the routing table for the source address it would use to
reach TEST-NET-1. No packets are sent; a UDP `connect()` only fixes the peer
locally, so it works on a host with no internet access and returns `""` on one
with no default route. `gethostname()` is deliberately not used: on Debian it
resolves to 127.0.1.1, which is the exact useless answer this avoids.

The suggested default is the LAN address, not loopback. A status page reachable
only from the machine it describes is of little use on a headless recorder.
An address already in the config is kept when it still works, and moving an
install that deliberately sat on loopback is called out at the prompt rather
than done quietly. The *config* default stays `127.0.0.1`, so a hand-edited
`config.json` still starts closed; only the wizard, where an operator is
present to read the warning, suggests otherwise.

**Changing any of this requires a restart, and the wizard now does it.**
`systemctl enable --now` is a no-op on an already-active unit, so
`restart_web_if_running()` exists for the same reason
`restart_capture_if_running()` does. This shipped broken in 0.0.9: the wizard
printed a new bind address while the running process kept serving the one it
read at startup, which presented as the UI refusing connections on an address
the wizard had just called correct.

**`resolve_library()`** is the part with actual thinking in it. Videos are not
where a naive reading of the config says they are: `transfer()` runs rsync with
`--remove-source-files` and `transfer.delete_local_after_transfer` defaults to
true, so after a successful night `paths.video_output` is *empty*. Reading it
would show an empty library on every correctly-configured install. Resolution
order is `web.library_root`, then `transfer.destination` when transfer is
enabled, then `video_output`.

A destination is not necessarily a path. `is_remote_spec()` classifies
`user@nas:/path` and `rsync://host/mod` as remote: unreadable without SSH, and
reported as such rather than rendered as an empty list. An absolute path is
settled before the colon test, so `/mnt/odd:name/videos` stays local.

The function returns a dict with `usable` and a `note` rather than a path,
because "why is this empty" is the question the page exists to answer, and a
dropped NAS mount is a *correct* empty library, not a fault.

**The index** (`/library`) is sqlite at `web.state_dir/index.db`.

- **Six filename conventions, not one.** Measured against a real five-year
  library: the native `Camera.YYYYMMDD.ext` is 64% of it, and a parser handling
  only that drops a third of the files and everything before 2024-04.
  `parse_name()` tries patterns most-specific-first and validates the date, so
  a match yielding 30 February falls through instead of winning. All 6,847
  video files in the surveyed library parse.
- **A name is a place, not a camera.** Cameras get repurposed across years, so
  two similar names are not evidence of one thing. The index **never merges**
  them (`Workshop` and `workshop` stay two rows) and only sorts
  case-insensitively so variants sit adjacent for the reader to judge.
- **The path is the primary key.** `(camera, date)` is not unique in the wild.
- **An extension allow-list**, because "not a directory" is not a test for "is
  a video": the surveyed library has a leftover `.ps1` in its root.
- **The first scan runs in a background thread**, started only after the socket
  is listening, so the page reporting its progress is never delayed by it. No
  duration budget is assumed anywhere; the one measurement taken (1.7 s for
  6,848 files) came from a 10G workstation while deployments read CIFS over 1G,
  and the work is round-trips rather than megabytes.
- **Progress is reported per file, and the banner updates itself.** Two
  separate defects made a running scan look like a hung one. The counter
  advanced once per 500-file write batch, which is the right unit for database
  writes and the wrong one for a progress report: a library smaller than a
  batch finished still reporting zero, and a slow share froze the number for
  500 files at a time. And the line itself was a server-rendered snapshot, so
  it never changed until the reader reloaded, having just been told the scan
  had started. `_scan_banner()` now emits `<div id="scan" data-running>`, and
  while a scan runs the page also carries `SCAN_POLL_JS`, which polls `/scan`
  once a second, swaps the fragment in place, and reloads once on completion
  so the tables below catch up. `/scan` returns that fragment and nothing
  else; it reads an in-memory dict and touches neither the database nor the
  library, which is what makes polling it during a scan free. The obvious
  no-JS alternative, `<meta http-equiv="refresh">`, was rejected because it
  re-requests whichever library view is open, and on a folder view that means
  `reconcile_dir()` hitting the share once a second during the scan it is
  competing with. Without JS the banner behaves as it always did. Verified in
  headless Chrome: one navigation, six polls, six distinct banner texts, and
  on completion a second navigation with polling stopped.
- **Reconciliation is on access.** Opening a folder re-reads that one directory
  and diffs it; opening a file re-stats it. An earlier version gated the
  directory read on its mtime and skipped it when unchanged; that was both a
  false economy (reading one directory is a single round trip) and a
  correctness hole, since mtime is stored at second granularity and anything
  added within the same second as the last scan stayed invisible.
- **A changed library root wipes the index.** Serving an index built from a
  different directory is worse than having none.
- **A missing library does NOT wipe the index.** A scan that cannot read the
  root has not discovered that every file is gone, it has discovered nothing,
  and a NAS is often not mounted yet when the service starts at boot. Two
  guards, because one is not enough: `_wait_for_library()` retries for
  `SCAN_RETRY_LIMIT` intervals before giving up, and a completed scan that
  found **zero** files while the index holds rows keeps them instead of
  pruning. The second guard is the one that matters in practice, because an
  unmounted CIFS mountpoint is a *readable, empty* directory, so a readability
  check alone would sail straight past it. The cost is stale rows for a
  library that really was emptied; those 404 on access and are removed by
  `reconcile_dir()` the moment the folder is opened. A partial deletion still
  prunes normally.
- **An unusable state directory degrades rather than crashes.** Status and logs
  still work; the library page explains that the unit needs `ReadWritePaths`.
- **Query parsing keeps blank values.** Two of the groups the index itself
  offers links to are keyed on the empty string: the library root's `folder`,
  and the `camera` of every file whose name carries no camera. `parse_qs`
  discards blank values by default, so `?folder=` and `?camera=` parsed to no
  filter at all and fell through to the home page, which reads as the group
  being empty rather than as a broken link. Both groups are large in a real
  library. `valid_day()` still rejects a blank `?day=`, which now reaches it.

**Serving and handoff** (`/video/<path>`, `/play/<path>`).

- **Playback is delegated, not embedded.** The default output is AV1 in
  Matroska, close to the worst case for a browser `<video>`, and native to
  VLC, mpv and MPC-HC. `/play/<path>` returns a one-line `.m3u` whose
  `Content-Disposition` filename ends in `.m3u`, which is what makes the
  desktop hand it to a player; the URL extension is irrelevant to that.
- **Two playlists.** `/play/<path>` is one video; `/day/<YYYY-MM-DD>` is every
  video from one day, so reviewing a day means opening a single file rather
  than one per place. Entries are ordered `lower(camera)`, which puts two
  spellings of a place adjacent without folding either into the other, the
  same rule the index itself follows.
- **A day playlist re-stats every entry before emitting it.** A playlist is
  handed to a player that will not come back and ask again, so a dead URL in it
  is worse than a shorter list; a file removed since the scan is left out, and
  a day with nothing left is 404 rather than an empty playlist. A day is a
  handful of files, so this costs nothing.
- **`valid_day()` guards every day-keyed route** and runs *before* the index is
  touched: only a real ISO date proceeds, so `2025-02-30` and `../../etc/passwd`
  are both simply 404.
- **`m3u_title()` collapses all whitespace.** A filename may legally contain a
  newline on Linux, and an `#EXTINF` carrying one splits into a bogus second
  entry.
- **The playlist URL is built from the request's `Host`**, never from config.
  An `.m3u` containing `127.0.0.1` is useless the moment it is opened on a
  phone, and the address the client just used is the only one known to work.
  `Host` is validated against `HOST_RE` before it goes into the file, and
  `X-Forwarded-Proto` is honoured only for the literal values `http`/`https`,
  so a reverse proxy terminating TLS produces working links.
- **The extension allow-list is enforced in `reconcile_file()`, not only in the
  scan.** `/video/<path>` resolves through it, and without the check a request
  could name any file the user keeps beside their videos; `abs_path()` stops
  a request leaving the library, this stops it reading everything inside.
  Found by a test, having shipped through a full review as `200 OK` on a
  `.txt`.
- **`_pump()` sends exactly the promised length.** Bounded by the
  `Content-Length` already in the header rather than by EOF: a file that grew
  since the stat would otherwise corrupt the response, and one that shrank
  would leave a short body, which under keep-alive hangs the client instead of
  failing it, so that case closes the connection. `BrokenPipeError` and
  `ConnectionResetError` are caught and ignored, because a viewer quitting VLC
  is the normal way a video request ends.
- **`Server.handle_error()` exists because that `_pump()` guard is not enough.**
  The common disconnect happens *between* requests, not during one: the client
  takes its byte range, the handler loops back into `readline()` waiting for
  the next request on the keep-alive connection, and the socket resets under
  it with no code of ours on the stack. `socketserver` prints a full traceback
  to **stderr** for that, and journald tags stderr as an error, so an ordinary
  seek in VLC reads as a crash in the log. `ConnectionError`, `TimeoutError`
  and `socket.timeout` (a separate class before 3.10) are logged at debug;
  everything else goes through the logger, which also gains it a timestamp and
  a level that the default traceback never had. Reproduced before and after by
  completing one keep-alive request and closing with `SO_LINGER 0`.
- **Range requests** (`parse_range()`) are what make scrubbing work. Single
  ranges only: closed (`bytes=0-499`), open (`bytes=500-`) and suffix
  (`bytes=-500`, which is how a player reads a Matroska trailer). An end past
  the file is **clamped, not refused**: required by RFC 7233, and a common
  place to get a 416 wrong. A start past the file, a backwards range, or
  `bytes=-0` is 416, which still carries `Content-Range: bytes */<size>` so the
  client can correct itself.
- **A header the server does not care for is ignored, not rejected.** RFC 7233
  permits that, so a multi-range request (which would need
  `multipart/byteranges`) and an unparseable one both fall back to a plain 200.
  Nothing seeking a video asks for more than one range.
- **The digit runs are bounded** (`\d{0,19}`): an unbounded `\d*` invites a
  megabyte of digits, and arbitrary-precision arithmetic on that is real work
  for a request that was never going to be satisfiable.
- **`ETag` and `Last-Modified` come from the fresh stat**, so they change
  whenever the file does; that is what makes `If-Range` meaningful. A client
  resuming against a version we no longer have gets the whole current file
  instead of a slice, because splicing two encodes together produces a file
  that is corrupt in a way nothing would report.
- Each listed file also shows two addresses that need no UI at all: the share
  path, for a machine that has the mount, and the HTTP URL for VLC's *Open
  Network Stream*. They sit in a sub-row spanning the table, whose leading
  empty cell exists solely to line the path up under the *name* it belongs to.
- **`_file_table()` drops a column the heading already states.** `show_folder`
  did this from the start; `show_day` was added after the day view shipped
  giving every row a link back to the page being read. Both flags also move
  the sub-row's leading cell, so a new column means checking that too: a table
  can stay perfectly rectangular while the path indents under the wrong
  heading, and a colspan test will not notice.

**The update check** (`UpdateChecker`, on the overview) is the one outbound
connection this service makes, and should stay the only one.

- **Opt-out, and asked for.** `web.update_check` defaults true, `choose_web()`
  asks, and the panel itself names the host it contacts and how to switch it
  off. It sends an HTTPS GET and nothing else: no config, no camera names,
  nothing about the library. What GitHub learns is the IP and the version in
  the User-Agent.
- **Lazy, not scheduled.** A request for the overview starts a check only if
  the cached answer is older than a day, and returns immediately either way,
  so nothing here can delay a page. A service nobody looks at never calls out.
  This follows the same rule as status and logs: on request, never polling.
- **Releases first, tags second.** This is not a detail. The repo publishes
  git tags and **no GitHub Releases**, so `/releases/latest` answers 404; an
  implementation that knew only about Releases would report "up to date"
  forever, on its own project. The fallback reads `/tags` and takes the
  highest parsed version, not the first, because that endpoint's ordering is
  not documented. Publishing real Releases would make the notes richer with no
  code change, since a release body is preferred when one exists.
- **`parse_version` compares tuples, never strings.** `0.0.10` sorts below
  `0.0.9` lexically, and `0.10.0` below `0.9.0`. A two-digit component is
  not a hypothetical for a project on its tenth release.
- **The notes come from the changelog** when the tag has no release body,
  fetched only when there is actually an update to describe, so the ordinary
  case is one request. `plain_notes()` strips heading markers because the text
  lands in a `<div>`, not a markdown renderer.
- **The 4,000-character cap degrades, rather than truncating.** v0.1.0's own
  release body was 4,020 characters, and a plain slice cut it three characters
  into a sentence with nothing recording that it had: the page read as though
  this program had lost the rest. `clip_notes()` cuts on the last line break
  instead (word break, then a blunt cut, for notes written as one paragraph),
  and returns whether anything was dropped, so the panel can say so and link
  to the release. The half-limit floor stops a body whose only newline is near
  the start being trimmed to almost nothing. A cache written before the flag
  existed is repaired on load by inferring it from the length: without that,
  the fix would not reach an install already carrying clipped notes until its
  next check, which is up to a day. **Never cap rendered text without
  reporting the cap and offering the whole thing somewhere.**
- **An explicit User-Agent is mandatory.** GitHub rejects a request without
  one, exactly as Cloudflare does for the Discord webhook. Two vendors, one
  trap; see `post_webhook()`.
- **Every failure is somebody else's outage.** `_check()` catches everything,
  records it, keeps the last good answer and never reaches a page as a 500.
  The cache lives in `state_dir`, the single writable directory, so a service
  that restarts often does not spend the 60-per-hour anonymous rate limit.
- **A failure is not a check, and conflating them shipped as a bug.** 0.1.0
  set `checked` on both paths, so the daily interval gated a *failed* attempt:
  the first operator to hit it had a saturated local resolver for a few
  seconds during an upgrade and the panel then sat on the error until the
  next day. `checked` is now the last success and `attempted` the last try.
  Failures retry after `UPDATE_RETRY`, doubling per consecutive failure and
  capped at the daily interval, so a transient blip recovers in minutes while
  a permanently offline host settles at the normal rate instead of asking
  every quarter hour forever. `POST /check-update` forces one immediately,
  because somebody looking at the error usually knows what they just fixed.
  It is a POST for the same reason `/rescan` is: a prefetch must not be able
  to make this service reach the internet.
- **`_migrate()` repairs a 0.1.0 cache on load.** Without it the fix reaches
  only new installs: an existing `update.json` records the failed attempt as
  a successful check, and the interval would still gate the retry. A non-empty
  stored `error` means the last write was a failure, so `checked` is that
  failure's timestamp and the real last-success time is unrecoverable; it is
  treated as one failure.
- **`friendly_error()` leads with whose fault it is.** The raw
  `URLError: <urlopen error [Errno -3] Temporary failure in name resolution>`
  tells an operator nothing actionable. The original text is kept on the end,
  because that is the part worth searching for.

**Status and logs** (`/status`, `/logs`) shell out, on request only: a page
load or a click. Nothing polls and nothing is collected in the background.

- **The title and tabs are centred on the window, not on the content.** They
  are the fixed furniture of every page and the pages are not all the same
  width, so positioning them by the content column made them jump about 240px
  between the overview and the log page. `justify-content: center` on
  `header` and `nav` makes their position independent of whatever is below.
  `scrollbar-gutter: stable` on `html` closes the remaining 8px: without it a
  page long enough to scroll (the library) is that much narrower than one that
  is not, and anything centred moves with it. Measured in Chrome across all
  four pages at two window sizes: 240px of drift before, 1px after, the
  remainder being sub-pixel rounding when centring inside containers of
  different widths.
- **These two pages drop the 54rem reading column.** That width suits prose and
  tables and is wrong for raw command output, whose line length journald and
  systemctl decide, not us. `_render()` adds `pane-page` to `<body>` for them
  and `_report(pane=True)` marks the output `<section>`; the page is then a
  flex column of viewport height, so the `<pre>` is bounded and scrolls inside
  its own frame on both axes. Before this the pane grew to its content and put
  its horizontal scrollbar hundreds of lines below the text it scrolled.
  `min-height: 0` on the flex items is load-bearing: a flex item's default
  minimum is its content size, so without it the `<pre>` never shrinks and
  nothing scrolls. The marker is withheld when the report carries a *problem*
  rather than output, since stretching a one-line error to the full window
  would render an almost empty box. Verified by measuring the real layout in
  headless Chrome at 1400x900 and 700x600, not by reading the CSS.

- **A non-zero exit is not a failure.** `systemctl status` exits 3 for an
  inactive unit and 4 for one that does not exist, and that output is precisely
  what the page is for. `run_command()` reports a problem only when the command
  could not be run at all: missing binary, timeout, `OSError`. Treating exit 3
  as an error would replace the answer with an error page.
- **`--lines=0`** suppresses the journal excerpt `systemctl status` normally
  appends. That excerpt needs journal access, so without the flag the output
  looks mysteriously truncated for readers who lack it.
- **Never `-f`.** The `timelapse logs` wrapper is `journalctl -f`, which never
  returns; a handler running it would hang until the client gave up. The web
  path is bounded (`-n`, `--no-pager`) with a subprocess timeout on top.
- **The journal needs a group.** `journalctl -u` returns `-- No entries --`,
  not a permission error, to a user outside `systemd-journal`, indistinguishable
  from a quiet service, and it reads as a bug in the UI.
  `SupplementaryGroups=systemd-journal` in the unit grants it without putting
  the account in the group system-wide. `sync_units()` **deletes that line when
  the group does not exist**, because a `SupplementaryGroups` naming a missing
  group stops the unit from starting at all. When it is absent the page detects
  the empty result and explains it rather than leaving a blank pane.
- **No request value reaches a command line.** `unit` and `n` are keys into
  `LOG_UNITS` and `LOG_LINES`; the *values* are what get executed, and an
  unknown key falls back to the default rather than 400: these come from
  links, and a stale bookmark should show the default log, not an error.
  `shell=True` appears nowhere.

**Security posture.** Binds `127.0.0.1` by default and warns when it does not;
`http.server` is not hardened and there is no TLS here. It never serves
`config.json` and never renders a camera URL; those hold credentials. Routing
is an explicit allow-list; anything unrecognised is 404 with no filesystem
lookup. `server_version`/`sys_version` are overridden so the interpreter
version is not advertised.

`Handler.timeout`, not `Server.timeout`: `ThreadingHTTPServer.timeout` is only
consulted by `handle_request()`, which `serve_forever()` never calls, so
setting it there looks like a timeout and is not one.

### 4.6 `timelapse_update.py`

Two things in one file, because they are the same knowledge: the GitHub
release query, and the `timelapse update` command.

**The one import between this project's scripts**, and deliberately one-way:
`timelapse_web.py` imports the query half for its version panel. Everything
else here is standalone, and this exception earns itself. Two callers need to
know which tag is newest, and two copies of that means two places to get the
tuple comparison wrong (`0.0.10` sorts below `0.0.9` as a string), two places
to forget GitHub's mandatory User-Agent, and two places that have to know this
repo has nine tags with no Release behind them. It resolves because both files
are installed into the same directory, which is `sys.path[0]` for either entry
point; the tests put `scripts/` on the path for the same reason.

Upgrading is re-running the installer, so that is what this does:

1. Ask which release is newest, and compare as tuples.
2. Show the notes, clipped on a line boundary (§4.5) rather than mid-sentence.
3. Confirm, unless `--yes`.
4. Download `install.sh` **for that tag**, not for `main`. An installer newer
   than the tree it unpacks can expect files that tree does not contain.
5. Refuse it unless it looks like the installer and passes `bash -n`. This
   runs as root; a 404 page, a captive portal and a proxy error all arrive as
   a perfectly successful HTTP response.
6. Hand over, into a directory holding nothing but `install.sh`, so the
   installer's `obtain_source()` does not mistake it for a checkout.

The privilege check sits between steps 3 and 4, not at the top of `main()`:
`--check` answers the question without root, and only acting on the answer
needs it. `--check` exits **10** when an update is available, so a cron job
can notify without a human reading the output.

### 4.7 `install.sh`

Bootstrap. Detects the package manager (apt/dnf/yum/pacman/zypper/apk), installs
dependencies, creates the `timelapse` system account, places scripts, units and
a `timelapse` command wrapper, then calls the wizard and offers to enable.

It uses the checkout it is running from when there is one, and otherwise
downloads a tarball from `codeload.github.com`, so the same script serves both
`git clone && sudo ./install.sh` and the piped one-liner.

`sync_units()` is the important part: it rewrites `User=`, `Group=`,
`ExecStart=` and `ReadWritePaths=` in the installed units from the config the
wizard just wrote. `timelapse-web.service` is templated **separately**, with
its own narrower `ReadWritePaths` from `--print-web-paths`, and the state
directory is created there too: `ReadWritePaths` naming a directory that does
not exist stops the unit dead, and the service cannot create it because by then
its parent is read-only to it.

`RESTART_UNITS` lists every long-running unit. **A unit missing from it gets
replaced on disk and keeps serving the old build while the installer reports
success**: the bug this function exists to fix. The encoder is deliberately
absent: it is oneshot, so a run in flight finishes on the code it started with.

Prompts read from `/dev/tty` for the same reason the wizard's do. `--uninstall`
removes programs and units but never captured data.

---

### 4.8 Per-camera interval and frame rate

`capture.interval_seconds` and `encode.framerate` are defaults. A camera
carrying `interval_seconds` or `framerate` itself uses that instead. **Absent
is the override mechanism**: it means "follow the default", so raising the
global interval still moves every camera nobody has pinned. The wizard
enforces this by *removing* the key when you answer with the global value
rather than storing an equal copy, which would silently pin a camera somebody
had merely looked at.

Four things follow from it that are not obvious:

- **The fetch timeout is clamped, not configured.** `capture.timeout_seconds`
  is chosen against the *global* interval, so a camera that opts into a
  shorter one inherits a timeout that can outlast its own tick: every request
  still in flight when the next is due. `camera_timeout()` clamps it to
  `interval - 1`, floored at 1 because requests treats 0 as no timeout at all.
  A third knob would have no useful setting other than "under the interval".
- **`gop` follows the frame rate.** 120 frames is two seconds at 60fps and
  four at 30. The codec is probed once per run, but `encode_day()` rebuilds
  the arguments from `build_candidates(enc, gop)` for each camera, rather than
  appending a second `-g` and leaving the command carrying two values for one
  option. An explicit per-camera `gop` still wins, and a camera that does not
  set its own frame rate keeps a hand-tuned global one.
- **`Cov%` is measured against the camera's interval.** Each result row
  carries the interval it ran at. Against the global, a camera at one frame a
  minute reads as 8% coverage: a complete day reported as a near-total outage,
  every night.
- **The disk projection sums, it does not multiply.** One camera at 60s and
  five at 5s is not six times any single figure.

`timelapse test` has a **Cadence** section reporting what each camera will
produce, because the consequence of these two numbers, how long tonight's
video is, cannot be read off the config. It fails a camera whose frames/day
falls below `encode.min_frames`: the encoder `SKIP`s that, so the camera would
produce nothing at all, every night, without ever failing.

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
| `retry_within_tick` | true | One retry per tick when the budget allows. Set false for a camera that degrades under a second request. |

### `encode`
| Key | Default | Notes |
|---|---|---|
| `framerate` | 60 | Applied to both input and output, and asked by the wizard. A day's frames at a 5s interval are 4:48 of video at 60 and 9:36 at 30. |
| `container` | `mkv` | Extension only; ffmpeg infers the muxer. |
| `gop` | 120 | 2s at 60fps. The wizard derives it as `framerate * 2` so that stays true at any rate; set it here to override. Lower = better scrubbing, larger files. |
| `av1_preset` / `av1_cq` | `p6` / 26 | p1 fastest … p7 slowest. Lower cq = higher quality. |
| `hevc_cq`, `x264_crf` | 24, 20 | Fallback encoders only. |
| `min_frames` | 100 | Below this, `SKIP` rather than produce a 2-second video. |
| `delete_frames_on_success` | true | |
| `max_backlog_days` | 7 | Bounds a catch-up run after long downtime. |

### `transfer`
| Key | Notes |
|---|---|
| `destination` | A local directory or an rsync remote spec; one code path serves both. |
| `rsync_args` | Defaults include `--remove-source-files`; if you drop that, set `delete_local_after_transfer` accordingly or files accumulate. On a CIFS mount `-a` may exit 23 because owner/group cannot be set; the wizard measures which flags your share accepts and writes those. |
| `require_mountpoint` | `false` (default), `true`, or an explicit mount path. Refuses to transfer when the destination is not on a mounted filesystem. Only meaningful for a local destination; ignored for a remote spec. |

### `web`
Optional; absent from configs written before the feature existed, so every key
is read with `.get(key, default)`.

| Key | Notes |
|---|---|
| `enabled` | `false` by default. The program exits 0 when false, so the unit may be enabled without the server running. |
| `bind` | `127.0.0.1` by default. There is no authentication and no TLS; any other value exposes the page to the LAN, and anything wider belongs behind a reverse proxy. A non-loopback bind logs a warning at startup. |
| `port` | `8787` by default. |
| `library_root` | Empty means "work it out": the transfer destination when transfer is enabled, otherwise `video_output`. Set it when the videos are readable here under a different path, typically a remote rsync destination that is *also* mounted locally. Not `/tmp` or `/var/tmp`: `PrivateTmp=true` hides those from the unit. |
| `state_dir` | The **only** directory the web UI may write to; holds the sqlite index. The unit's `ReadWritePaths` is scoped to exactly this, so the library, the frames and the config stay read-only to it. `install.sh` creates it: a `ReadWritePaths` naming a missing directory stops the unit dead, and the service cannot create it itself. |

### `cameras[]`
| Key | Notes |
|---|---|
| `name` | **Used as the directory name.** Renaming a camera orphans its existing frames; the encoder walks directories under configured camera names, so frames under the old name are stranded. Rename with care. |
| `enabled` | Excluded from both capture and encode when false. |
| `method` | `http` or `rtsp`. |
| `auth` | `digest`, `basic`, or `none`. Cameras that put credentials in the query string → `none`. |
| `interval_seconds` | Optional. This camera's seconds between snapshots. Absent means `capture.interval_seconds`. |
| `framerate` | Optional. This camera's playback rate. Absent means `encode.framerate`, and `gop` follows it. |
| `quality` | RTSP only: ffmpeg `-q:v`, 2 = high. |
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

**Add a camera**: one config entry. No code.

**New snapshot protocol**: subclass `threading.Thread` in
`timelapse_capture.py` following `HttpCamera`, honour `STOP` and `PAUSED`, write
via temp + `os.replace`, and dispatch on `method` in `main()`. Nothing in the
encoder changes.

**Per-camera settings**: interval and frame rate are done (see §4.8);
resolution and quality would follow the same shape. The camera dict is passed
whole to the thread constructor, so it is `cam.get(key) or <global>` and
nothing structural.

**Different notification sink**: `send_discord()` is the only
outbound-notification function and takes `(title, description, color, fields)`.
Add a sibling and call both from `main()`; keep failures swallowed.

**Camera restart on hang**: natural home is a new module invoked by the capture
daemon's failure path, gated on consecutive-failure count, with a cooldown so it
can't reboot-loop a camera. Detection already exists (`consec_fail`); this is
remediation only.

**Frame retention beyond encode**: set `delete_frames_on_success: false` and add
a separate age-based sweeper. Do not add retention logic to `encode_day()`; keep
"encode" and "delete" separable.

**Parallel encoding**: currently sequential and deliberately so. NVENC session
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
- **Video length varies with capture coverage**: a camera down for 6 hours
  produces a shorter video, not a video with gaps.
- `Cov%` is computed against a nominal `86400/interval` and will read ~104% or
  ~96% on DST days.

---

## 9. Testing notes

```bash
python3 -m unittest discover -s tests -t tests -p 'test_*.py'   # fast, no deps
python3 tests/smoke_test.py                                     # needs ffmpeg
```

**Unit tests** (`tests/test_*.py`, stdlib `unittest`, 668 cases, about a
minute; `test_web.py` builds real sparse files on disk) cover the pure logic: frame validation, concat-list escaping,
`find_pending` backlog selection, `human_*` formatting, the storage scan's
filtering and deduplication, `_base_device` partition stripping, `recommend`,
`writable_paths`, credential quoting, `_dest_path` including the DST
collision suffixes, and the web UI's library resolution and routing. Anything
needing a camera, a GPU or systemd is out of scope here by design.

`test_web.py` drives the real handler through a fake socket rather than binding
a port: a listening socket in a unit test is a flake waiting for a busy CI
runner. The fake implements `sendall`, not a writable `makefile`, because
`StreamRequestHandler` sets `wbufsize = 0` and wraps the socket directly.

`test_update.py` never reaches the network. Every test that would patches
`fetch_json` or `fetch_text`, and one exists specifically to prove the module
makes no request at import, since the web server imports it at startup. When
the release query lived in `timelapse_web.py`, four tests patched
`web.fetch_json` and then called `web.latest_release()`; after the move that
name resolved in the *other* module's namespace and the tests silently started
hitting api.github.com for real. They passed, against the live repo. Patch the
module that owns the function, not the one that re-exports it.

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
deleted afterwards. Needs ffmpeg but no GPU; it falls back to libx264.

**The suite was mutation-checked** when written: 18 deliberate breakages
introduced one at a time, 16 caught. The two misses were tests passing for the
wrong reason: a network-filesystem case rejected by the source filter before
the fstype rule ran, and a deduplication case filtered by a skip-prefix before
deduplication happened. Both are fixed, and both are worth remembering as the
failure mode to watch for when adding tests here: assert that the rule you
*mean* to test is the one doing the work.

The rest was verified by hand. The methods, in case you want to re-verify after
changes:

- **Capture daemon**: two cameras against a local `http.server`, one valid URL
  and one 404. Confirmed: files land on exact interval boundaries, correct
  directory layout, no leftover `.tmp`, failure throttling works, clean SIGTERM
  shutdown.
- **Installer and wizard**: Ubuntu 24.04 with real systemd. Confirmed: package
  detection and dependency install, service account creation, unit templating
  (`systemd-analyze verify` clean), a live capture run against a local HTTP
  camera writing frames **at a non-default path under `ProtectSystem=strict`**
  (which is what actually exercises the `ReadWritePaths` derivation), a nightly
  encode as the service user, and a clean uninstall. Worth repeating on a distro
  using a different package manager; only apt has been exercised.
- **Web UI**: Ubuntu 24.04 under real systemd. Confirmed: `web.enabled: false`
  exits 0 rather than serving; each library-resolution branch reports the right
  source (fallback, destination, remote spec, missing mount, explicit override);
  `/healthz`, 404 on unknown routes, no interpreter version in the `Server`
  header; clean SIGTERM shutdown; `systemd-analyze verify` clean; and the full
  install → re-install → uninstall cycle including `sync_units()` leaving the
  web unit without a `ReadWritePaths` line and `restart_upgraded_services()`
  picking the live unit up.
- **Late mount**: same host, all three shapes. A library that vanishes
  entirely leaves the index intact, says it is waiting, and **picks the
  library up on its own** when it returns, with no user action. An
  empty-but-readable root (what an unmounted CIFS mountpoint actually looks
  like) also keeps the index and says how many rows it kept. A genuine partial
  deletion still prunes. Before this, a restart while the library was away
  reported "Indexed 0 files", deleted every row, and did not recover when the
  mount came back.
- **Library index**: same host. Confirmed: `ReadWritePaths` really is scoped
  (a process with the unit's properties can write the state directory and
  **cannot** write the library or the frames; this is the claim worth
  testing, not asserting); the state directory is created by the installer and
  owned by the service user; `--print-web-paths` agrees with the templated
  unit; a nine-file library indexes to eight with the `.ps1` excluded;
  `Workshop` and `workshop` both appear, unmerged; the unnamed bucket and the
  human-named event folder survive; a folder view picks up a file added and a
  file deleted behind the service's back, and says the index had drifted;
  `/rescan` is 404 on GET and 303 on POST.
- **Live scan banner**: headless Chrome against a real server, since a unit
  test can assert the markup but not that a browser acts on it. Confirmed in
  a single page load: six `/scan` polls and six distinct banner texts, so the
  fragment is genuinely replaced in place rather than the page being
  re-fetched; and on completion, polling stops and the page navigates exactly
  once more. The layout of the status and log panes was measured the same way
  at two window sizes.
- **Wizard and wiring**: same host. Confirmed: an unattended install leaves
  the UI **off**; `timelapse web` runs the wizard (not the server) and writes
  the answers; the state directory is created and owned correctly; a re-install
  templates `ReadWritePaths` to that one directory; enabling the unit serves
  the configured `library_root` on the configured port; the wizard turns it
  back off again; `timelapse web-serve` still runs it in the foreground; and a
  non-loopback bind produces the reverse-proxy warning.
- **Range**: same host, and verified with a real player rather than only with
  curl. Byte-exact slices for closed, open-ended and suffix ranges; clamping;
  416 carrying the true size; multi-range and junk falling back to 200;
  `If-Range` honoured when it matches and ignored when it does not. Then the
  part that actually matters: **ffprobe read the container over HTTP and ffmpeg
  seeked to 5s, 30s and 55s of a 60s video and decoded a frame at each**,
  with the frames compared against each other, because three identical images
  would mean every seek had silently landed at byte zero. Repeated against a
  URL taken straight out of a day playlist.
- **Day playlists**: same host, seven places across two days plus one
  legacy-named file. Confirmed: the playlist carries all eight of that day's
  videos including the legacy name, leaks nothing from the neighbouring day,
  orders `Workshop` and `workshop` adjacent without merging them, is named
  `timelapse-<day>.m3u`, and **every URL inside it fetches**; a file deleted
  after the scan drops out rather than becoming a dead entry; and four
  malformed dates plus an empty day are all 404.
- **Serving**: same host. Confirmed: a 5 MB video arrives with a matching
  sha256 and exact length; `Content-Type`, `Content-Length` and
  `Accept-Ranges: none` are right; `?download=1` sets an attachment; a folder
  name containing spaces round-trips percent-encoded; the `.m3u` carries the
  request's `Host` and an `X-Forwarded-Proto: https` proxy scheme, and the URL
  *inside* the playlist fetches the same bytes; `.txt` and `.ps1` beside the
  videos are 404 and are **not** added to the index by being requested; three
  traversal shapes are refused; a file changed in place is re-stat'd on access
  and a deleted one 404s; and aborting a download mid-stream leaves the service
  running with no traceback in the journal.
- **Status pane**: same host, **both** journal states exercised, which is the
  point: with `SupplementaryGroups=systemd-journal` the log pane returns real
  entries; with the line deleted the unit still starts, `systemctl status` still
  works (it needs no journal), and the log pane explains the empty result
  instead of showing a blank. The `sync_units()` guard was checked by shadowing
  `getent` on `PATH` so the group appeared to be missing: the line is removed
  and the installer says why. Also confirmed `systemctl` reaches PID 1 from
  inside the sandbox, which is what `RestrictAddressFamilies=…AF_UNIX` is for.
- **Query-string credential encoding**: a password containing `@ & = #` fed
  through the wizard and parsed back out of the generated URL unchanged.
- **Profile probe**: mock ONVIF endpoint serving 2560×1440 / 704×576 / 640×480
  for Profile_1/2/3. Confirmed it identifies the largest and recommends the
  config change.
- **Transfer**: stub `rsync` on `PATH`.

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
install.sh                       bootstrap installer, 738 lines
scripts/timelapse_capture.py     daemon, 448 lines
scripts/timelapse_encode.py      batch job, 783 lines
scripts/timelapse_test.py        pre-flight checks + usage report, 724 lines
scripts/timelapse_setup.py       configuration wizard, 2671 lines
scripts/timelapse_update.py      release query + `timelapse update`, 446 lines
scripts/timelapse_web.py         read-only web UI, 2093 lines
tests/_support.py                path setup and fakes
tests/test_capture.py            unit tests
tests/test_encode.py             unit tests
tests/test_setup.py              unit tests
tests/test_update.py             unit tests
tests/test_usage.py              unit tests
tests/test_web.py                unit tests
tests/smoke_test.py              end-to-end encode check, needs ffmpeg
config/config.example.json       template; the real config.json is gitignored
service/timelapse-capture.service
service/timelapse-encode.service
service/timelapse-encode.timer
service/timelapse-web.service
docs/architecture.md             this file
docs/install.md                  operator guide
docs/future-features.md          planned work, in build order
```

Dependencies: Python 3.9+ stdlib, `requests`, `ffmpeg`/`ffprobe` (NVENC for
AV1/HEVC), `rsync`. No virtualenv required, one pip package, no database. The
web UI adds nothing: it is `http.server` and the stdlib.

All three systemd units use `ProtectSystem=strict` with an explicit
`ReadWritePaths`. **Any new write path (a different frames root, a CIFS
mountpoint for transfer) must be added there**, or writes fail with a
confusing read-only error. `timelapse-web.service` is scoped to a single
directory, its index, and must stay that way: it is the only network-facing
unit, and widening it to match the others would give it write access to every
captured frame for nothing.

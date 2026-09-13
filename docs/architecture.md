# Architecture

This document is the engineering reference: what the pieces are, why they are
shaped the way they are, and which properties must not be broken by future
edits. [install.md](install.md) is the operator guide; this is the developer
guide.

---

## Contents

- [1. Purpose and scope](#1-purpose-and-scope)
- [2. System overview](#2-system-overview)
- [3. The on-disk contract](#3-the-on-disk-contract)
  - [3a. Runtime state (0.1.6)](#3a-runtime-state-016)
- [4. Component reference](#4-component-reference)
  - [4.1 `timelapse_capture.py`](#41-timelapse_capturepy)
  - [4.2 `timelapse_encode.py`](#42-timelapse_encodepy)
  - [4.3 `timelapse_test.py`](#43-timelapse_testpy)
  - [4.4 `timelapse_setup.py`](#44-timelapse_setuppy)
  - [4.4b A path with a space in it (0.2.1)](#44b-a-path-with-a-space-in-it-021)
  - [4.4c A question nobody was there to answer (0.2.1)](#44c-a-question-nobody-was-there-to-answer-021)
  - [4.4d A prompt that lists its answers and takes any of them (0.2.1)](#44d-a-prompt-that-lists-its-answers-and-takes-any-of-them-021)
  - [4.4e Three ways to add a camera, two of which checked the name (0.2.1)](#44e-three-ways-to-add-a-camera-two-of-which-checked-the-name-021)
  - [4.5 `timelapse_web.py`](#45-timelapse_webpy)
  - [4.6 `timelapse_update.py`](#46-timelapse_updatepy)
  - [4.6a `timelapse_platform.py` (0.2.0)](#46a-timelapse_platformpy-020)
  - [4.6b `install.ps1` and `timelapse_cli.py` (0.2.0)](#46b-installps1-and-timelapse_clipy-020)
  - [4.6c Moving the videos on Windows (0.2.0)](#46c-moving-the-videos-on-windows-020)
  - [4.6d `timelapse_gui.py` and its launcher (0.2.0)](#46d-timelapse_guipy-and-its-launcher-020)
  - [4.6e The .exe installer (0.2.0)](#46e-the-exe-installer-020)
  - [4.7 `install.sh`](#47-installsh)
  - [4.8 Per-camera interval and frame rate](#48-per-camera-interval-and-frame-rate)
  - [4.8a Per-camera motion smoothing](#48a-per-camera-motion-smoothing)
  - [4.9 Credential redaction](#49-credential-redaction)
- [5. Configuration reference](#5-configuration-reference)
  - [`paths`](#paths)
  - [`capture`](#capture)
  - [`encode`](#encode)
  - [`transfer`](#transfer)
  - [`notify`](#notify)
  - [`web`](#web)
  - [`cameras`](#cameras)
- [6. Failure handling matrix](#6-failure-handling-matrix)
- [7. Extension points](#7-extension-points)
- [8. Known limitations](#8-known-limitations)
- [9. Testing notes](#9-testing-notes)
  - [The low-space guard, and three checks that could not see it](#the-low-space-guard-and-three-checks-that-could-not-see-it)
- [9a. Appendix: the destination library, surveyed 2026-08-07](#9a-appendix-the-destination-library-surveyed-2026-08-07)
- [10. File inventory](#10-file-inventory)

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

<sub>[&uarr; Contents](#contents)</sub>

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

<sub>[&uarr; Contents](#contents)</sub>

## 3. The on-disk contract

This is the integration point. Treat it as an API.

```
${paths.frames_root}/<CameraName>/<YYYY-MM-DD>/<HHMMSS>.jpg
${paths.frames_root}/<CameraName>/<YYYY-MM-DD>/.cadence.json
${paths.frames_root}/<CameraName>/<YYYY-MM-DD>/.encoded.json
${paths.video_output}/<CameraName>.<YYYYMMDD>.<container>
${paths.log_dir}/{capture,encode}.log
${paths.state_dir}/capture.json
${paths.state_dir}/encode.json
```

Invariants that both sides depend on:

| Invariant | Why |
|---|---|
| Filenames are zero-padded `HHMMSS` | Lexical sort == chronological sort. Nothing reads mtime. A PowerShell predecessor of this tool mixed `CreationTime` and `LastWriteTime` between its processing and deletion passes; making order a property of the *name* removes that class of bug entirely. |
| One directory per camera per local date | Encoding is a directory glob. Cleanup is one `rmtree`. Determining "is this day finished" is a string compare against today. No scanning 100k+ files to filter by date. |
| Files appear atomically | Capture writes `.<stem>.tmp` then `os.replace()`. A reader never sees a partial JPEG. `os.replace` is atomic *within a filesystem*; do not move the temp file to a different mount. |
| Dot-prefixed files are not frames | Temp files start with `.`; `glob("*.jpg")` skips them. Don't add non-frame files to a day directory without a dot prefix. |
| A day directory older than today is complete and owned by the encoder | Capture never writes to a past date. The encoder is free to delete it. |
| **A day has one cadence, start to finish** | `.cadence.json` records the interval and frame rate the day began at, and both programs obey it over the config. That is what lets a cadence edit take effect at the next midnight and not before, whatever restarts happen in between. Absent for days captured before 0.1.2, so both sides fall back to the config. |
| **A day is encoded once** | `.encoded.json` records what was produced and when. Until 0.1.6, deleting the frames *was* that record, so `delete_frames_on_success: false` meant the newest `max_backlog_days` were re-encoded from scratch every night. The marker is what makes keeping frames a supportable configuration. It cannot be inferred from the video instead: `transfer()` moves that to the NAS. |

Adding a camera means adding a directory. Nothing else in either program needs
to know.

### 3a. Runtime state (0.1.6)

A **second** on-disk contract, and it will outlive whatever reads it first, so
it is versioned and every reader uses `.get(key, default)`. It exists because
systemd cannot answer the question anybody actually has: a capture daemon whose
every camera is refusing connections is `active (running)`, and so is one the
disk guard has paused with nothing being written at all.

| File | Written by | Contains |
|---|---|---|
| `capture.json` | the capture daemon, once a minute, plus once at startup and once on clean shutdown | pid, start time, `running`, `paused`, and a per-camera block: cadence, last attempt, last success, an `error` object (0.1.6) and either frame counters (HTTP) or restarts and process liveness (RTSP) |
| `encode.json` | the encoder, at the end of every run including one that found nothing to do | the newest `MAX_RUNS` (14) run records: times, encoder, counts, bytes, transfer outcome, and a per-camera-day list with frames and coverage |
| `notified.json` | the credential watch, when it reports a change | `{camera: incident_start}` for refusals already announced, so a timer that fires every five minutes says each thing once |

Rules that hold this together:

| Rule | Why |
|---|---|
| **Facts, never verdicts** | There is deliberately no "healthy" or "state" field. Whether 42 seconds of silence is a fault depends on that camera's interval and on whether capture is paused; a reader can work that out, and a writer that had already decided could not be overruled. The cadence each number should be read against travels beside it for exactly this reason. |
| **`error` is a classification, not a verdict** | It says what the camera answered (`auth`, `unreachable`, `other`), when that started, and whether the daemon's own back-off has confirmed it. `confirmed` is not a health judgement: it means "two refusals, ten minutes, one more attempt", which is a statement about what was tried. A reader still decides what to do about it, and the web UI and the credential watch decide differently. |
| **`error.since` is the incident's identity** | Anything that must act once per incident compares that timestamp rather than inventing its own notion of when one failure stops being the same failure. It is stable while the incident lasts and moves when the class changes or the daemon restarts. |
| **The RTSP path publishes different fields, marked `supervised`** | ffmpeg writes those frames and the thread only supervises the process, so `last_success` stays `null` rather than being filled in from something that is not a frame. Reading the day directory's mtime would have produced a plausible number and taught readers to trust it everywhere. This still holds for the daemon; since 0.1.8 the web UI answers the same question from the newest frame's **filename**, which is a frame rather than a proxy for one (§4.5). |
| **Writing it can never fail the job** | A daemon that cannot write its status file must keep capturing, and a run that encoded seven days has not failed because a history file could not be updated. Capture complains once, not once a minute. |
| **Written atomically** | `os.replace`, like every other file this project writes. A reader gets the previous snapshot or the next one, never half of either. |
| **The daemons write it; the web UI only reads it** | Not `web.state_dir`, which is the UI's own index directory and the single path that service may write. Verified under systemd: a process with the web unit's `ReadWritePaths` gets `Read-only file system` here. |
| **`paths.state_dir` must exist before the units start** | It is named in `ReadWritePaths`, and systemd refuses to start a unit whose `ReadWritePaths` points at nothing, with an error about mount namespaces that names neither the directory nor the release that added it. `install.sh` creates it in `sync_units()`, which is what makes the upgrade path safe for an install that answers "don't reconfigure" and never runs the wizard. |

---

<sub>[&uarr; Contents](#contents)</sub>

## 4. Component reference

### 4.1 `timelapse_capture.py`

Long-running daemon. `systemd` `Type=simple`, `Restart=always`.

**Module state**

| Name | Purpose |
|---|---|
| `STOP` | `threading.Event`, set by SIGTERM/SIGINT. Every loop uses `STOP.wait(n)` rather than `time.sleep(n)` so shutdown is immediate. |
| `PAUSED` | `threading.Event`, set by `DiskGuard`. Capture threads check it and skip, but keep their scheduling loop running. |

**Nothing on the housekeeping tick may raise** (0.2.1). `record_cadences()`
and `write_cadence()` both stat a path, and pathlib swallows only `ENOENT`,
`ENOTDIR`, `EBADF` and `ELOOP`: an `EACCES` from an ACL change or an `EIO`
from a network frames root propagates, and this runs on the main thread, so
the daemon unwinds rather than the camera it was about. The guard is **per
camera**, which is failure isolation met in the housekeeping rather than in a
fetch, and the formatter redacts whatever the path turns out to carry.

The same once-a-minute tick in `main()` that calls `record_cadences()` also
calls `write_state()` (§3a). It is the main thread, so there is one writer and
no lock: every value it reads off a camera thread is a single int or float,
and a counter one tick old is not worth taking a lock in the capture path for.
It also writes **immediately at startup**, or a restart leaves the previous
run's file in place for a minute, which reads as a live daemon that has
stopped taking pictures, and **once more on clean shutdown** with
`running: false`, so a stopped daemon and a wedged one stay tellable apart.

**`HttpCamera(threading.Thread)`**: one per `method: "http"` camera.

- `run()`: the scheduling loop. Computes `next_t` as an absolute epoch multiple
  of `interval`, sleeps until it, fires, then advances. Because targets are
  absolute, the loop cannot accumulate drift from fetch latency. If it falls a
  full interval behind (slow camera, host suspend) it resyncs forward to the
  next boundary instead of replaying a backlog.
- `_grab(dt, timeout=None)`: one fetch. Validates size ≥ `min_bytes` and JPEG
  SOI (`FF D8`), writes temp, `os.replace`s into place. Nothing is written until
  both checks pass, so a failed attempt leaves no partial file and no
  `_dest_path` collision for a retry to trip over. A `FileNotFoundError` from
  that write clears `_last_dir` before re-raising (0.2.1): the day directory is
  created only when the date changes, so an operator clearing today's frames
  to start again, or a dropped mount, otherwise failed every remaining tick of
  the day identically with nothing repairing it until midnight. The tick that
  found it missing is still counted and logged as the failure it was.
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

**Authentication back-off (0.1.6).** A camera that rejects our credentials will
reject them again next tick, and firmware commonly locks the account after a
handful of failures, so fetching every few seconds renews that lock faster than
it expires. Before this, rotating a camera's password without disabling it here
first left the account locked until the daemon stopped, a correct password did
not clear it, and the account is usually shared with an NVR.

`classify(exc)` sorts a failed tick into `auth`, `unreachable` or `other`, in
`run()` rather than in `_grab()`, which stays the fetch. It is deliberately
conservative, because only `auth` withholds fetches and so costs frames:

| Answer | Class | Notes |
|---|---|---|
| HTTP 401 or 403 | `auth` | Measured on Dahua and Hikvision: the status settles it and the body is different on every make, so nothing reads the body |
| HTTP 200 with `rspCode` -6 or -7 | `auth` | The Reolink shape. -6 is "please login first", -7 is "login failed" |
| HTTP 200 with `rspCode` -9 | `other` | "not support": an unknown *command*, in the identical shape. This is why the classifier reads the code and not the presence of an error object, and why unknown codes are logged rather than guessed at |
| `ConnectionError`, `Timeout` | `unreachable` | Powered off, rebooting, unplugged. None of it is our credentials' fault |
| anything else | `other` | Changes nothing |

`_grab()` raises `NotAJpeg` carrying the body, because **`min_bytes` (4096) is
larger than a Reolink refusal (~140 bytes)**, so the size check fires first and
the SOI check is never reached for the one shape that most needs recognising.

The ladder, on `HttpCamera`, in memory: **two consecutive refusals, ten minutes
of withheld fetches, one attempt, then one attempt every 31 minutes for ever.**
Flat rather than escalating, because the observed lockout window is 30 minutes
and one attempt per 31 cannot reach the "N failures inside a window" threshold
that lockout policies are built from. The tick itself keeps running while
fetches are withheld, so the day rollover and the cadence marker are unaffected
and a back-off spanning midnight still starts the new day correctly. The
intra-tick retry is suppressed for `auth`: it cannot succeed and does count
against the camera's lockout. A success or **any change of class** ends the
incident, so a camera alternating between refusing and unreachable never climbs
to a verdict. A restart re-enters at step one, deliberately: a fresh process
knows nothing and two attempts settle it again.

**`RtspCamera(threading.Thread)`**: one per `method: "rtsp"` camera, for devices
with no HTTP snapshot endpoint (e.g. TP-Link Tapo).

Supervises a persistent `ffmpeg` doing `-vf fps=1/N` into the image2 muxer with
`-strftime 1`, writing `HHMMSS.jpg` into a `YYYY-MM-DD` directory **this class
creates**, which produces the identical layout to the HTTP path. On exit, logs
stderr and restarts after 10s.

**This path was broken from the first release until 0.1.7 and nobody noticed**,
because it is the rarest configuration: the deployment's eight cameras are all
HTTP. The pattern used to be `%Y-%m-%d/%H%M%S.jpg` with `-strftime_mkdir 1` to
have the muxer create the directory. **That option belongs to the hls muxer**;
`image2` has never had it, and ffmpeg does not report an unknown private
option, even at `-loglevel warning`. So every write failed with `Could not open
file` and the supervisor restart-looped for ever. Reproduced against ffmpeg
6.1.1 by running the daemon's own argv with `testsrc` as input: `rc=251` and no
frames without the day directory, `rc=0` and a frame with it.

Two things follow, and both are the point rather than the fix:

- **Neither RTSP probe would ever have caught it.** The wizard's
  `test_camera_rtsp()` and `timelapse test`'s `test_rtsp()` both write one
  fixed filename, with `-frames:v 1`, into a directory that already exists. So
  they prove the camera is reachable and decodable and never touch the output
  side, which is where the fault was. Same lesson as `PIX_FMT` in the encoder
  probe: **a probe must produce what the pipeline produces.**
  `smoke_test.py`'s `rtsp_writer_check()` now takes the argv from
  `RtspCamera._cmd()` itself and swaps only the input, so it cannot drift.
- **`_discard_empty_day()` is the other half.** Creating the directory
  ourselves means an unreachable camera would leave one empty directory per
  day for ever, since the encoder `SKIP`s a short day and does not clean up.
  So a day that ends with no `.jpg` in it is removed, along with its cadence
  marker and nothing else.

**Nothing on the stop path could reach ffmpeg until 0.2.1, and the whole of
`run()` was written as though it could.** `communicate()` blocks until the
child exits, and a healthy RTSP grabber exits at midnight, so `STOP` was
noticed by the loop and never by the process; the `terminate()` after the loop
was unreachable for as long as the thing it was terminating was working.
Measured with a fake `Popen`, `STOP` set 0.2s after launch: `terminate()` was
never called at all.

**On Linux this was masked rather than absent**, which is why it survived. The
unit runs under `KillMode=control-group`, so systemd SIGTERMs ffmpeg directly
whatever this code does. Under the Windows SCM nothing does: each RTSP thread
times out on its 20 second join (and two RTSP cameras therefore exceed the 30
second stop hint this daemon gives the SCM), `run_daemon()` returns,
SERVICE_STOPPED is reported, the process exits, and **ffmpeg is orphaned**. It
keeps writing into today's directory until its own `-t` expires at midnight,
and a service restart launches a second one with `-y` on the same pattern, so
two writers race on one directory.

`_supervise()` polls `proc.wait(timeout=SUPERVISE_POLL)` and ends the process
when `STOP` or `PAUSED` is set, keeping the terminate-then-kill ladder. Three
neighbours fall out of the same rewrite, and they were all consequences of the
same blocking call:

- **`PAUSED` now reaches the process.** The disk guard could pause capture
  while a grabber launched at 00:00 went on filling the disk it was pausing
  for, all day, with `capture.json` saying `paused: true` throughout.
- **stderr is drained on a reader thread into a bounded deque**
  (`STDERR_LINES`), rather than accumulated by `communicate()` until the
  process exits. A grabber complaining once a second has all day to produce
  it.
- **The log keeps the tail, not the head.** `err[:400]` on a day of stderr is
  the connection banner; what matters is the last thing it said before it
  died.

And **a process we ended is not a fault**: the returncode after `terminate()`
is our own signal, so a stop or a pause is neither logged as a crash nor
counted in `restarts`, which is published in `capture.json` and shown on the
status page. A pause that incremented it would make a full disk look like a
flapping camera.

Verified on real Linux with a control (`temp/h10_orphan_check.sh`): the same
harness against a copy of the daemon with the blocking call put back leaves an
ffmpeg running after SIGTERM, and against the shipped one leaves none. WSL has
no RTSP server (measured at 0.1.7), so ffmpeg is reached through a stub that
takes the daemon's own output pattern and generates frames from lavfi, which
makes the supervised process a genuine long-running ffmpeg writing real frames.
**The Windows half is what only the operator can check**: `Get-Process ffmpeg`
after `sc stop "TimelapseCapture"` with an RTSP camera configured.

**`DiskGuard(threading.Thread)`**

Polls `shutil.disk_usage(frames_root).free` every 300s. Below `min_free_gb`, sets
`PAUSED` and logs at ERROR. Clears at 110% of threshold (hysteresis, so it can't
flap). Capture threads keep their timing loops running while paused so they
resume on-boundary. Disabled by setting `min_free_gb: 0`.

### 4.2 `timelapse_encode.py`

Batch job. `systemd` `Type=oneshot` + timer at 00:05, `Persistent=true`.

**One run at a time, and the lock comes before everything else** (0.2.1).
`take_lock(state_dir(cfg) / ".encode.lock")` is the first thing `main()` does
after the `--watch` branch returns. Two encodes on the same night are data loss
in both directions, because the output name is fixed per camera per day and
both processes write it at independent offsets: whichever finishes first marks
the day and deletes the frames, and the second then either fails and
**unlinks the file the first just declared finished**, or reads the truncated
concat as an EOF, exits 0 and reports OK over a corrupt video. The frames are
gone either way and both runs report success.

Nothing prevented it. The systemd timer guards only timer against timer and
Task Scheduler's `IgnoreNew` only task against task, while `timelapse encode`
is precisely what an operator types during a multi-hour libx264 backlog after
downtime, which is also when a nightly run is most likely to still be going.

Three answers rather than two, on the rule this project has paid for five
times. **held** refuses, names the holder's pid, exits 1, and sends no
notification and writes no run record, because neither is true of a run that
never started. **unavailable**, meaning the lock file could not be created at
all, logs a warning and proceeds: turning an environment problem into "no
videos tonight" would be the check causing the outage it exists to prevent.
**taken** carries on, and `release_lock()` sits in a `finally` so that the
critical-failure path gives it up too, which matters because an aborted run is
exactly the one somebody re-runs by hand a minute later.

The refusal says the work is not lost, and that is load-bearing rather than
reassurance: `find_pending()` works from the day directories on disk, so a
skipped night is picked up by the next run, up to `max_backlog_days`.

`--watch` does not take it. It runs every five minutes from its own timer,
reads the capture heartbeat and touches no video, and a watch tick able to
refuse the nightly encode would be the opposite of the point.

The primitive is in `timelapse_platform.py` and is the one genuine `os.name`
branch outside `locations()`: `fcntl` is absent on Windows and `msvcrt` is
absent on Linux, so there is no feature detection to do. An **OS lock and not a
pid file**, because the case that decides it is the encode that was *killed*: a
pid file would outlive it and refuse every run afterwards until somebody found
and deleted it. Measured on both platforms (`temp/c4_lock_probe.py`): refused
while the holder lives, free the moment it is killed, free after a clean exit,
takeable again by the same process, and a leftover file naming a dead pid is
not a lock. **Byte 0 is the lock byte and is never written**; the pid goes
after it, because msvcrt's lock is *mandatory* and a contender reading from
byte 0 is refused outright, so the pid would never reach the message it exists
for. The test that asserts this raised `PermissionError` on Windows when it
first read the file whole, which is the behaviour happening to the test
describing it.

The lock lives in `state_dir`, which is the one directory every unit already
has in `ReadWritePaths`; anywhere else means a unit change, and a
`ReadWritePaths` entry that does not exist stops the unit with a mount
namespace error. Verified with two real processes on systemd 255
(`temp/c4_wsl_serialise.sh`, 15 checks): the timer-started unit takes it, the
lock names that unit's exact pid, a manual `timelapse encode` during it is
refused with that pid and deletes nothing, the unit then finishes with one
video, and a run afterwards is not blocked.

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
   **`encoder_hint()` keys on the NVENC reason, never on the failing call.**
   ffmpeg reports every NVENC failure as `<Call> failed: <reason> (<code>)`,
   the reason and code coming from one fixed table in `libavcodec/nvenc.c`, and
   the call is `nvEncOpenEncodeSessionEx` whatever went wrong. Until 0.2.1 the
   classifier matched the bare substring `session`, so all of
   `unsupported device (2)`, `invalid version (15)` and the genuine
   `out of memory (10)` were announced as "no free NVENC session, another
   process may be holding them all". `nvenc_failure()` parses the reason out
   instead; the code is optional in that regex because `probe_encoder_detail()`
   caps stderr at 300 characters and can truncate it away.
   **A driver/library mismatch cannot be read from the probe at all**, which is
   why `driver_mismatch()` exists and asks a different program. An NVIDIA
   package upgraded under a running kernel module leaves NVENC returning
   ordinary status codes, indistinguishable from a GPU that is merely busy;
   NVML refusing to initialise is the witness that settles it, so the check
   runs `nvidia-smi` and looks for `Driver/library version mismatch` (or the
   CUDA spellings, which ffmpeg sometimes prints itself and which then save the
   subprocess). It answers **True, False or None**, the last meaning nothing
   here could tell, because a checker's own limitations are never a finding
   about what it checks. It is asked **once per run**, only after a probe has
   already failed, and never for a codec missing from the build. The result
   outranks every NVENC reading in `encoder_hint()`, since it is established
   rather than inferred, but only for `*_nvenc` codecs: blaming a mismatched
   driver for libx264 failing is the RTX 4060 error in a new coat, and a test
   caught exactly that in the first cut of this.
   **`select_encoder()` logs ffmpeg's own message at INFO**, not `debug`. The
   run sets the logger to INFO, so a `log.debug` there was a discard, and the
   line it discarded is the only part of those two lines that cannot be wrong.
   Reported from the real deployment 2026-09-13, where 595.84 was upgraded to
   595.91 by unattended-upgrades and two nights encoded on libx264 behind a
   hint pointing at an innocent NVR.
2. `find_pending()` → every date directory across all enabled cameras whose name
   sorts `< today` **and carries no `.encoded.json`**, oldest first, capped to
   the newest `max_backlog_days` distinct dates. It returns `(jobs, done)`;
   `done` counts the camera-days already encoded, which the log reports so a
   run that legitimately does nothing cannot be mistaken for a broken one.
   The marker check happens *before* the cap, or a week of finished days would
   push the one day that still needs encoding out of the window. `--date`
   overrides a marker (re-encoding one day by hand has to stay possible) and
   `--force` overrides it for the whole backlog.
3. `encode_day()` per job, sequentially. On `OK`, and only on `OK`, after the
   output file is closed and size-checked, `mark_encoded()` writes
   `.encoded.json`. It never raises: a marker that could not be written costs
   a re-encode next night, which is the behaviour the project had for five
   releases, and that is not worth failing a good run over.
4. `rmtree` the day directory on `OK` (unless `--keep-frames` or config says
   otherwise). The marker is written even when this is about to delete it,
   because `rmtree` runs with `ignore_errors=True`: a deletion that quietly
   failed would otherwise leave an unmarked directory to encode again.
5. `transfer()` → rsync.
6. `write_run_state()` → one record appended to `encode.json` (§3a), **before**
   the notification: Discord is the part that can be disabled, unreachable or
   rate-limited, and the local record of what happened should not depend on
   any of it. Every exit path records, including "nothing to do" and the
   critical-failure handler, because a status page that cannot tell "the timer
   fired and there was nothing to do" from "the timer never fired" is not
   answering the question.
7. `notify()` → one summary to every configured sink.
8. Exit `0` all-good, `1` partial failure, `2` critical.

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

**`notify()`** delivers one summary to any number of sinks: `discord`, `ntfy`
and `telegram` at 0.1.6, from a `notify` list in the config. `urllib` only, no
dependency. Everything goes through `post_webhook()`, which sets an explicit
`User-Agent`. This is not cosmetic: Discord is behind Cloudflare, which rejects
urllib's default `Python-urllib/3.x` with **HTTP 403, Cloudflare error 1010**,
before the request reaches Discord at all. With the documented
`DiscordBot ($url, $version)` form the same request reaches the API. The other
sinks send a project agent instead, because claiming to be a Discord bot to
somebody's ntfy server is a lie that would eventually cost an afternoon.

Each sink is wrapped separately, catching `Exception` deliberately: a socket
timeout is not a `URLError`, one sink being down must not stop the next, and
notification is never load-bearing, least of all in the critical-failure
handler where an exception would mask the original error. `notify()` returns
`(sent, failed)` for callers that want to report it; the run's exit status
never depends on either.

Three things the sinks do not share, and each is a trap in its own right:

- **Limits.** Discord takes 4096 for a description, 1024 per field and 25
  fields; Telegram takes 4096 for the entire message; ntfy's default is around
  4KB. `clip()` trims and *says* it trimmed, the same lesson as the release
  notes at 0.1.0: text that stops without saying so reads as this program
  having lost the rest.
- **ntfy is sent as JSON to the server root**, not as text to `/topic`. The
  other form carries the title in an HTTP *header*, headers are ASCII, and
  every title this program sends begins with an emoji.
- **Telegram is sent as HTML, not MarkdownV2.** The summary is a monospace
  table, and a proportional font turns it into rubble, so it has to be wrapped
  in `<pre>`. MarkdownV2 would mean escaping fourteen characters correctly
  everywhere, including inside camera names, where a miss is a 400 rather than
  a message that merely looks wrong. HTML mode needs three: `&`, `<`, `>`.

**`--watch`: the credential watch (0.1.6).** A second entry point into this
script, run from `timelapse-watch.timer` every five minutes. It reads
`capture.json`, and for every camera whose `error` is a **confirmed** `auth`
refusal it sends one notification, then one all-clear when the refusal ends.
It exists in this file rather than in the capture daemon because that daemon
has never made an outbound connection and the state file was built precisely so
the thing that knows and the thing that sends could be separate processes.

Five rules, four of which are about not lying:

- **Once per incident.** Keyed on `error.since`, recorded in `notified.json`.
  A camera refusing all day produces one message, not 288.
- **Nothing delivered is not "already told them".** `notify()` returns
  `(sent, failed)`; the record is only written when at least one sink accepted,
  so a sink that was briefly unreachable does not swallow the single message
  this whole feature exists to send.
- **No sinks means no record either**, checked before anything is written.
  Marking an incident as reported when there was nowhere to report it would
  leave today's refusal permanently unannounced if a sink were added tomorrow.
- **A stale heartbeat is not acted on**, in either direction. A file older than
  `CAPTURE_STALE` (300s), or one whose `running` is false, describes a moment
  that has passed: a stopped daemon is not a camera that recovered, and an
  all-clear sent from that would be an invention.
- **It logs to the journal only.** Its unit may write exactly one directory,
  the state directory, so opening `encode.log` under `ProtectSystem=strict`
  kills the process at startup with a read-only filesystem error. Found on real
  systemd, not in a test, which is the second time that has been true of a
  `ReadWritePaths` assumption in this release.

**Severity, not colour.** Call sites pass `"ok"`, `"info"`, `"warn"` or
`"error"`; each sink maps that to its own vocabulary (a Discord embed colour,
an ntfy priority, nothing at all for Telegram). They used to pass a hex colour,
which meant a Discord concept travelling through code with no business knowing
what Discord is.

**The legacy `discord` block still works and is still read.** Every config
written before 0.1.6 has one, upgrades keep the existing config, and
`notify_sinks()` falls back to it when no `notify` list is present. When both
exist the list wins and the old block is logged as ignored, because a webhook
that looks configured and never fires is worse than one that is plainly off;
the wizard turns it off when it writes the list, for the same reason.

**`build_summary()`** writes the table inside that embed, and **the embed is
narrower than a message**. Roughly 50 columns survive; past that Discord wraps,
which put the last field on a second line underneath the first row and made the
summary unreadable. Two rules keep it inside that: the column widths come from
the content rather than being fixed (a fixed 8 could not hold `1h 02m 03s`,
so one slow encode knocked every column after it out of line), and **the date
is a heading, not a column**. A run normally encodes a single day, so a Date
column spent ten characters repeating one value on every row; a catch-up run
after an outage emits one block per day, oldest first, in a single code fence.
Seven cameras come out at 39 columns. The worst case that can still be
constructed, a 12-character name with `FAIL`, a one-second cadence and an
encode over an hour, is 50.

### 4.3 `timelapse_test.py`

Pre-flight checker, run manually. Never run by systemd. Not imported by the
other two (it imports *from* `timelapse_encode`, one-way, so the encoder probe
logic cannot drift between the two).

| Check | What it catches |
|---|---|
| `check_camera_names` | two cameras the filesystem cannot keep apart, and names Windows will not accept at all. The wizard refuses both at the prompt, so this is for the config that got one another way: hand-edited, or written on Linux and carried to a Windows box |
| `test_http` / `test_rtsp` | wrong credentials, wrong auth scheme, non-JPEG responses, slow cameras relative to the interval |
| `probe_profiles` | ONVIF `Profile_N` pointing at a substream. Profile numbering is not consistent across vendors, so it is easy to configure a low-resolution stream without noticing |
| `test_encoders` | ffmpeg built without NVENC |
| `test_disk` | projects daily usage from *measured* snapshot sizes against actual free space |
| `test_transfer` | unmounted CIFS path, missing SSH key |
| `test_notify` | a bad webhook URL, a wrong ntfy topic, a Telegram bot nobody has messaged. It sends through the encoder's own `notify()`, so what is proven is the request the nightly run will make; a pre-flight that builds its own payload is how a check passes while the thing it checks is broken |

**The status is part of the answer** (0.2.1). `bad()` only printed, `main()`
returned nothing, and a run with two FAIL lines exited 0 like any other. So
`install.sh`'s
`as_service_user python3 timelapse_test.py "$CONFIG" || warn "Pre-flight
reported problems"` could fire on a traceback and on nothing else, and the
installer then offered to enable the services with the same default whatever
the check had just found. The wrapper `exec`s Python, so a real status reaches
a script that runs `timelapse test` too.

`bad()` counts, `main()` returns `1 if _FAILS else 0`, and the entry point is
`sys.exit(main())`, which is the half that is easy to leave out: a function
returning a number nobody reads is not an exit code. **A WARN deliberately
does not count.** A warning is something to know about rather than something
that stops the product working, and a checker that exits non-zero for every one
of them says nothing by the end of a release. The three report modes (`--usage`,
`--encoders`, `--probe-profiles`) stay at 0 whatever they print: they answer a
question rather than passing a verdict, and `timelapse usage` belongs in a
pipeline. The counter resets at the top of `main()`, so two runs in one process
are independent.

`install.sh` acts on it by flipping the *default* of the next question, not by
skipping it. The operator may know the camera is unplugged today and want
capture on anyway; answering for them when the check has just said something is
broken is the other error. Unattended, where `ask_yn` returns the default
unread, that flip means the services are not enabled.

**The two halves are reachable in two different ways, and the second one is
awkward enough to be worth recording.** `offer_enable()` cannot be reached by
an unattended install at all: it returns early on an upgrade, and again when no
camera is configured, and an unattended wizard configures none, because
`choose_cameras()` empties the list before it looks at anything (so seeding a
camera into the config template does not help; measured, not assumed). So
`temp/h8_install_check.sh` does part A for real, running the checker as the
service account through the installed script against a camera that answers and
one that does not, and for part B lifts `offer_enable()`'s own text out of the
shipped `install.sh` and drives it with a stubbed pre-flight. What runs there is
the shell this project ships; the only thing faked is the exit code being
reacted to.

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

#### 4.4a Camera discovery (WS-Discovery)

`discover_cameras()` sends a UDP multicast Probe to `239.255.255.250:3702` and
collects ProbeMatch replies for a fixed window. Stdlib sockets and
ElementTree: no SOAP stack, no WSDL, no dependency, which is what made this
worth having after the ONVIF client library was refused (decided-against.md).
Offered in the wizard, in `timelapse cameras`, and standalone as
`timelapse discover`, which needs no root and touches no config.

It lives in the wizard rather than in a daemon on purpose. The wizard runs as
root and outside a unit; the daemons run under `RestrictAddressFamilies`,
where a multicast bind is exactly the thing that works in development and
fails in production. The web UI's bind probe is here for the same reason.

Measured against eight cameras from four vendors on 2026-08-14, which settled
more than it confirmed:

- **Only the typed Probe is sent** (`dn:NetworkVideoTransmitter`). An untyped
  one found no camera the typed one missed and pulled in a network printer and
  two Windows PCs, because **Windows WSD shares this group and port**. It is
  not symmetric either: the Reolink and the Tapo answer the typed probe alone,
  so an untyped-only client misses two of eight. Replies were attributed to
  their probe by giving each a distinct MessageID and reading `RelatesTo` back.
- **`Types` classifies, never the `type` scope.** The scope is vendor free
  text: Dahua writes `Network_Video_Transmitter`, Hikvision and Reolink write
  `video_encoder`, TP-Link writes `NetworkVideoTransmitter`. Classifying on it
  called six of the eight real cameras not-cameras. Even in `Types` the prefix
  moves (`dn:` on five, `tdn:` on two), so `wsd_is_camera()` strips
  punctuation and compares on the local name.
- **Deduplication is on the device id, never the address.** One device answers
  several times and an NVR proxies the cameras behind it. Two spellings exist,
  `urn:uuid:<x>` and bare `uuid:<x>`; a device is consistent, so the string
  works as a key, but the prefix is not something to assume.
- **`wsd_address()` prefers an address the device answered *from*** over one it
  merely advertised. That is a cheaper answer to "the advertisement may be
  unreachable" than fetching it: the reply proves the source works from here,
  and the wizard then tests the real snapshot URL, which is the thing that has
  to work. IPv4 before IPv6, because that is what the camera was configured
  with.
- **An XAddr is not necessarily a URL.** One Dahua advertises
  `http://[]/onvif/device_service`, and `urlparse` raises `ValueError` on it
  under Python 3.12+. `wsd_host()` returns `""` instead of raising.
- **Only the host is taken, never the port.** The device service and the
  snapshot endpoint are different things: the Reolink answers ONVIF on `:8000`
  and serves snapshots on `:80`. Discovery's value is the address and the
  model, not a port to paste into a template.
- **The name scope is a model, not a place.** It returns `Dahua` on three
  different cameras, so `wsd_preset()` uses it only to preselect a vendor
  template, and the operator still names the camera. `wsd_preset()` returns
  `""` rather than guessing when nothing matches, which is the honest answer
  for the Reolink (`IPC-BO`) and the Tapo (`TC40`), neither of which names its
  maker. **A wrong preselection is worse than none**, because it is a wrong
  URL that looks deliberate.
- **IPv6 is not probed.** All three plausible destinations were measured;
  every camera answered the IPv4 probe, `ff05::c` (site-local) got no reply at
  all, and `ff02::c` found only this workstation's own loopback WSD service.
  IPv6 would double the replies to deduplicate and add a Windows PC.

Two properties are load-bearing and easy to erode. **Discovery is an offer**:
`offer_discovery()` catches every exception, returns `[]` on any failure, and
is skipped entirely without a terminal, because a network that cannot be
probed must leave the operator typing an address rather than reading a
traceback. And **finding nothing is never reported as "there are no
cameras"**: multicast does not cross subnets or VLANs, and a dedicated camera
VLAN is common in exactly the deployments that have the most cameras.

**`url_host()` brackets an IPv6 address before it reaches a preset template.**
The presets are `http://{ip}/...` strings and the wizard used to format
whatever was typed, so an IPv6-only camera produced
`http://fdd2:49bd:...:22ac/ISAPI/...`, whose colons are read as a port. Both
consumers fail, and neither says so usefully: `requests` raises
`InvalidURL: Failed to parse`, and ffmpeg reports **"Failed to resolve hostname
fdd2"**, which sends an operator to look at DNS for an address they typed
correctly. Measured against a real Hikvision over its ULA address on
2026-08-14: bracketed, both `requests` and ffmpeg reach the camera and get its
401; bare, both fail. Hostnames and IPv4 pass through untouched, an address the
operator already bracketed is not bracketed twice, and a zone id stays inside
the brackets (`[fe80::1%eth0]`) because it is part of the address. The wizard
additionally warns on a bare `fe80:` address, since link-local needs a zone and
moves with the interface. **Nothing in the daemons needed changing**: the URL
reaches `session.get()` and ffmpeg's `-i` verbatim, which is what made this a
wizard-only fix.

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
- Those directories are **quoted**, by `quote_unit_path()`. A unit value is a
  whitespace-separated list, so `/media/war/My Passport/frames` arrived as
  `/media/war/My` plus `Passport/frames`, and an entry that does not exist
  stops the unit: both daemons, on a fresh install, reachable by pressing
  Enter at the first storage question, since the wizard proposes mountpoints
  from its own disk scan and `/media/<user>/<Label>` automounts carry spaces
  routinely. See §4.4b for what has to be escaped and how it was measured.

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

`redact_url()` masks credentials in the listing (§4.9 has the rule and why it
now lives in one place). Not theatre: `ask_secret()`
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

#### 4.4b A path with a space in it (0.2.1)

The wizard writes paths into two files whose fields are separated by
whitespace, and neither one was escaping them. Both are silent: the unit
failure names the namespace rather than the path, and the fstab line is
discarded by the parser while this program says the share will remount at
boot. External drives mount at `/media/<user>/<Label>` and those labels carry
spaces routinely, so the first storage question of a fresh install could
produce either by pressing Enter.

**`quote_unit_path()`** renders one path as a unit file reads it back. Every
rule below was measured against systemd 255 with `systemctl show` as the
oracle, because it prints the parsed list back and so distinguishes "one entry
with a space in it" from "two entries" (`temp/c2_quoting_readback.sh`):

| Written | Parsed back |
|---|---|
| `ReadWritePaths=/srv/My Disk /srv/b` | `/srv/My` `/srv/b`, and the unit fails 226/NAMESPACE |
| `ReadWritePaths="/srv/My Disk" "/srv/b"` | `/srv/My Disk` `/srv/b` |
| `"/srv/back\slash"` | `/srv/backslash`: a lone backslash is an escape and is eaten |
| `"/srv/say\"q\""` | `/srv/say"q"` |
| `"/srv/fifty%full"` | `/srv/fifty/c2readull`: `%` is a **specifier** and is expanded inside the quotes too, `%f` being the unit's own file name |
| `"/srv/fifty%%full"` | `/srv/fifty%full` |

So three characters are escaped, backslash first because the other two add
one. Every entry is quoted rather than only the ones that look dangerous:
quoting a plain path is free (systemd prints those back unquoted, measured as
a control), and a rule with an exception is a rule somebody has to remember in
two languages.

**Where the rendering happens matters.** `writable_paths()` still returns
plain paths, because its own logic collapses a child into its parent by string
prefix and quotes would break that comparison; `unit_path_value()` renders
afterwards. `--print-paths` emits the finished value because **a list cannot
survive a shell variable** once one entry contains a space: there is no
spelling of `ReadWritePaths=$rw` in `install.sh` that splits on the separators
and not on the spaces. Its two siblings stay bare, because `install.sh` both
names those directories in a unit *and* creates them, and a directory with
quote marks in its name is not what anybody asked for; one directory
round-trips through argv where a list cannot, so those go through
`--quote-unit-path` at the point of use. The shell half owns no spelling of
its own, which a test asserts.

**`fstab_escape()`** is the same defect in the file with no quoting at all.
Measured with `findmnt` reading a file handed to it (`temp/c2_fstab_probe.sh`):
a bare space in the device or the mountpoint field is `parse error at line 1
-- ignored`, so `//tower/My Share` produced an entry that did nothing while
`persist_cifs_mount()` reported success. The escapes are octal, `\040` for a
space, `\011` tab, `\012` newline, `\134` backslash, and the duplicate check
matches on the escaped spelling or it would append a fresh entry every run.

**What was checked and needed nothing.** Both rsync call sites build a list
argv with no shell, so a spaced destination was always fine there; the review
that found this expected otherwise. The mkdir and chown calls in `install.sh`
are already quoted, and shellcheck agrees. Nothing refuses a space at the
prompt, deliberately: with these two fixed a spaced path works end to end, and
refusing a working configuration is the mistake `try_rsync_args()` exists
because of.

#### 4.4c A question nobody was there to answer (0.2.1)

`test_camera()` answers a 401 by offering the other authentication scheme,
because a Hikvision configured for digest and answering basic is the single
commonest camera misconfiguration this project meets. It offered it by calling
`ask_yes("Retry with auth 'basic'?", True)` and then **calling itself**.

`ask()` returns its default under `--defaults` and whenever `init_tty()` found
no terminal, which is the whole of its headless contract, and this default is
yes. So with nobody there the function answered its own question: digest to
basic to digest, with no base case. Measured (`temp/h1_probe.py`, the shipped
build and the old one loaded side by side from source): **995 requests to the
camera and then a `RecursionError`**. The traceback is the small half. A
thousand presentations of a rejected credential in a few seconds is exactly
what the item 8 back-off ladder exists to stop the daemon doing, arriving from
the one tool an operator runs *because* a camera is refusing them.

It is reachable by design rather than by accident: `run_camera_action()`
refuses the writing actions with no terminal and lets `test` through, so
`timelapse cameras -t:Gate` from cron or a scheduled task reaches it, as does
any `--defaults` run.

Two guards, because they answer two different questions. The offer is made
only when `not (AUTO or _TTY is None)`, and headless the useful half of the
question is **printed** instead: `Try auth 'basic' if the credentials are
right.` A question with nobody to answer it is not a question, it is an
assignment, and the fact it carried is worth keeping either way. Then
`retried` bounds the interactive path too, where a camera that refuses both
schemes would otherwise ask the same question after every yes; one flip, and
the second failure reports rather than re-offers.

§4.6d closed the graphical half of this in 0.2.0 by structure, and that stays:
the window calls `grab_snapshot()` and a test pins that `test_camera()`,
`ask_yes()` and `ask()` appear nowhere in it. What changes here is where the
rule lives. Avoiding a function that asks questions works only for callers who
know to; the function now knows whether anybody is listening.

#### 4.4d A prompt that lists its answers and takes any of them (0.2.1)

`ask()` hands back whatever was typed and `ask_yes()` loops until it hears
yes or no. The Auth prompt sat between the two: it printed
`Auth (digest/basic/none)` and then stored the answer whatever it was.

That is worse than it sounds, because of what the caller does next. Anything
that is not `digest` or `basic` takes the branch that **pops `username` and
`password`**, and the daemon reads an unknown mode as none
(`HttpCamera.__init__`). Measured on the real edit path
(`temp/h2_probe.py`, the shipped build and the old one loaded side by side
from source):

| answered | stored |
|---|---|
| `digets` | `auth: "digets"`, no username, no password |
| `digest` | `auth: "digest"`, username and password untouched |

So one transposed letter leaves a camera presenting nothing, 401ing every tick
from the next daemon restart, with the credential gone rather than merely
unused: retyping it is the only way back. Nothing reports it either, since a
camera that answers 401 looks exactly like a camera whose password was
changed at the camera.

`ask_choice(question, choices, default)` re-asks, shaped like `ask_yes()`, and
both sites use it. **The default gets checked too**, which is not fussiness:
with no terminal `ask()` returns the default unread, so a default outside the
list would re-prompt for ever, and a fix that hangs the wizard on a config it
merely disapproves of is a worse outage than the defect. An out of range value
is named in a warning and replaced with the first choice, so an operator who
hand-edited `digets` into the file is told what was wrong with it.

**The graphical wizard never had this**, and why is the general point: its
Authentication field is a readonly `Combobox`, and a picker cannot express a
typo. Validation belongs wherever free text is possible, which on the console
is everywhere. The two lists are one list now (`AUTH_MODES`, read by the
window), because two spellings of the same set is the drift `tools/` was
deleted over.

#### 4.4e Three ways to add a camera, two of which checked the name (0.2.1)

A camera's name is a directory name, which is why `name_taken()` has been
case-insensitive since 0.2.0: `Gate` and `gate` are one directory on NTFS, so
two cameras would interleave their frames and encode into one video, and on
Linux they are two config entries writing into one directory.

`manage_cameras()` and `camera_action()` called it. `choose_cameras()`, the
path every first install takes, appended whatever `add_one_camera()` returned
and checked nothing. Measured through the real keystroke path, answering
`Gate` and then `gate`: both stored, `['Gate', 'gate']`, no refusal shown.

The check is inside `add_one_camera()`'s name loop now, beside
`reject_reserved()`, with the cameras to compare against **passed in** rather
than read from `cfg`: the first-run wizard collects into a local list and only
assigns it at the end, so `cfg["cameras"]` is empty for the whole section. The
two outer checks stay as backstops.

**Where the check sits was the second defect**, and it is the one an operator
would have felt more often. Both outer checks ran after the camera was fully
built: the address, the username, the password, the smoothing question and a
live camera test, and after `device["used"] = True` had marked the discovered
device as spent. So a duplicate name threw away every answer, dropped the
device from the discovery list, and ended the action. Refusing at the prompt
that asked for it costs one prompt and keeps everything else.

The general point is about the note this fix had to correct rather than about
camera names. `CLAUDE.md` recorded, correctly reasoned and wrong, that "the
wizard cannot create the pair". The wizard was three code paths and two of
them had been checked. **A claim that a rule holds is a claim about every
caller of it**, so the thing to grep for is the callers, not the rule.

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

The runtime-state files (§3a) are the newest test of that rule and they pass
it: the daemons write them, this service only reads them, and `state_dir` was
added to the *daemons'* `ReadWritePaths` and not to this one. Re-verified under
systemd at 0.1.6 by running a process with this unit's properties against
`/var/lib/timelapse/state`, which gets `Read-only file system`. Note the name
collision that makes this easy to get wrong: `web.state_dir` is the UI's index
directory and `paths.state_dir` is the daemons' runtime state. Two different
directories, two different owners, one word. The module imports the second one
as `runtime_state_dir` for that reason.

It logs only to journald: a rotating log file would be a second writable path
for no benefit.

**The optional login is a door lock, not a safe, and is built to say so.**
`web.auth` sets one username and one password; absent or blank means the
pages behave exactly as they did before it existed, which is what every
upgrade gets. What it is for is keeping a household, or a guest on the wifi,
out of the status page and the video index. The threat model was stated by
the operator and is worth keeping in view, because a design that pretends to
more than this would make worse trades:

- **`/video/` is deliberately outside the gate.** VLC is a separate process
  with no cookie jar, and the `.m3u` handoff is what the library page exists
  for; gating the bytes would break every Play link, and a playlist saved a
  month ago would stop working the moment somebody logged out. `/play/` and
  `/day/` *are* gated, because the browser fetches those with the cookie and
  VLC only ever fetches the `/video/` URL written inside the playlist. So the
  camera names, the day groupings, the logs and the status stay behind the
  login and the file bytes do not. The login page says this in as many words.
- **The password is hashed** (PBKDF2-SHA256, 600k iterations, per-hash salt),
  which is correct precisely because it is *verified* here and never presented
  to anything. That is the inverse of the camera passwords, which must be
  replayed to the camera and therefore must be kept: **you can hash what you
  verify; you must keep what you present.** `SECRET_KEY_RE` was extended for
  it, since the original anchored on `pass(word|wd)?$` and let
  `password_hash` straight through `timelapse config --redacted`.
- **Sessions live in memory only**, so `state_dir` still holds exactly what it
  held before and the one-writable-directory rule is untouched. A restart logs
  everybody out, which for a service that restarts on upgrades and wizard runs
  is a fair trade and easy to explain. Logout revokes the token as well as
  clearing the cookie, so a copy taken out of the browser stops working.
- **The cookie is `HttpOnly`, `SameSite=Lax`, `Path=/`**, with `Secure` set
  only behind a TLS proxy (`X-Forwarded-Proto`). Setting `Secure`
  unconditionally would make the browser drop it on the plain HTTP this
  service actually serves, and the login would silently never take.
  `SameSite=Lax` is also what stops another site posting to `/rescan` with
  this cookie attached.
- **A denied fragment gets 401, never a redirect.** `/scan` and `/update` are
  fetched by the pollers, which check `r.ok`; a 303 would be followed
  transparently and the login page would be spliced into the panel being
  refreshed.
- **An unparseable hash refuses to start.** Fail closed: carrying on would
  serve the pages to everyone precisely because somebody asked for the
  opposite.
- **A wrong password costs three seconds, and nothing is ever locked out.**
  There is no counter and no per-client state at all. The first cut had five
  strikes and an escalating lockout; the operator removed it, and was right
  to: what is behind this is a status page and a list of video files, and a
  locked account mostly infuriates whoever mistyped their own password. Three
  seconds stops somebody guessing at a keyboard, which is the whole threat
  model. It does not stop a script guessing in parallel, because the delay is
  per request rather than global, and the comment on `pause_after_failure()`
  says so rather than implying otherwise.
- **`timelapse password` sets the login and nothing else**, because changing a
  password is a thing people do in a hurry. It never asks for the old one: it
  needs root to write the config, and root can already read every camera
  password in that file, so the question would prove nothing while locking out
  the one person entitled to fix a forgotten login. `set_web_login()` is the
  shared half; `choose_web_login()` is that plus the on/off questions.
  `--disable` removes it with no prompt and no confirmation, which is what
  makes it scriptable; it is idempotent, runs with no terminal, and `--enable`
  exists as a spelled-out synonym for the bare command so that nobody meets
  "unrecognized arguments". Both are refused outside `--password-only`, for
  the same reason the camera shortcuts are: a definite instruction that gets
  silently ignored while the whole wizard runs instead is the wrong surprise.
- **The web UI cannot turn its own login on or off, and must not be able to.**
  That would need write access to `config.json`, and the one structural
  property this service has is that it writes exactly one directory, its own
  index. A page able to edit its own authentication is a page able to edit
  everything, which is the trade `ReadWritePaths` exists to refuse. Asked
  directly by the operator 2026-08-12; the answer belongs in install.md as
  well, because "why is this not in the UI" is a fair question.

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

**`bind` may be an IPv6 address (0.1.7).** `ThreadingHTTPServer`'s
`address_family` is `AF_INET`, so `Server.__init__` sets it from the configured
address before calling `super().__init__()`, which is where the socket is
created. Until then the service exited with `Cannot listen on ::1:8787` while
**`check_bind()` had already called that address usable**: it calls
`getaddrinfo()` and walks every family returned, so `::1` and `::` both probed
`ok`. That was the actual defect, and it is the same shape as any check that
guesses: a probe is only as honest as its agreement with the thing it predicts.

**`IPV6_V6ONLY` is set explicitly, not inherited.** `server_bind()` sets it to
0 for any `AF_INET6` socket, so a `::` bind accepts IPv4 as v4-mapped
addresses. Linux defaults `net.ipv6.bindv6only` to 0 and Windows does not, and
a hardened host may have changed it; inheriting would make one config mean two
things. A kernel that refuses the option is warned about, not fatal, because
serving IPv6 alone beats not starting. Verified under systemd 255: `::1` serves
200 and listens on `[::1]:8787`, and with `::` both an IPv6 and an IPv4 client
get 200 from the same socket.

**Every emitted `host:port` goes through `hostport()`.** There were five, all
latent bugs for an IPv6 bind, and the one that mattered was `_base_url()`'s
fallback: it is what lands in a `.m3u` when the `Host` header is missing or
malformed, so it would have handed VLC `http://::1:8787/video/...` and arrived
as a bug report about video playback. The others are the startup log line, the
bind-failure message, two wizard summaries and the URL `install.sh` offers.
`HOST_RE` already accepted a bracketed authority, so the ordinary path, where
the origin comes from the request, always worked. Verified live over IPv6 that
both the per-file and the day playlist carry `http://[::1]:8787/video/...`.

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

**A readable library path is a prerequisite of the Library tab, not a gap in
it.** Either the destination share is mounted and `library_root` names the
mount point, or transfer is off and the videos stay in `video_output`.
Reaching an SSH-only destination over SFTP was item 6 and is **refused**
(decided-against.md): it would make this, the only network-facing unit, open a
second outbound connection over SSH to the host holding the whole archive,
with a key that can read it, against a standing rule that its one egress point
stays one; and mounting the same share costs a line in `/etc/fstab`. The note
`resolve_library()` returns therefore names both supported fixes rather than
only the problem, because no third answer is coming and the message must not
imply one.

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
- **One file is one row, and containment was not enough to make that true**
  (0.2.1). `abs_path()` proved the *resolved* path was inside the library and
  the caller then stored `rel`, the request's own spelling, as the primary key
  of the files table. `Path.resolve()` accepts `.`, `..`, `//` and components
  that do not exist, so `./Gate.mkv`, `nope/../Gate.mkv` and `2024-01//Roof.mkv`
  all landed on the right file, passed the containment check, and each inserted
  a row that nothing ever prunes. Measured (`temp/h6_probe.py`, the shipped
  build and the old one loaded side by side from source): two files, six
  spellings, **seven rows**. On a live server with a login configured, 200 HEAD
  requests carrying a 58 KB `./` prefix took the index from 0.21 MB to 46.78 MB
  at 21.5 MB/s, and needed no password, because `/video/` sits outside the
  login by design so VLC can follow an `.m3u` without a cookie jar. The index
  normally shares a filesystem with the frames, which makes that free space,
  and free space below `capture.min_free_gb` pauses capture: a LAN client with
  no credentials could stop the recording. The gated side had the same hole
  through `?folder=`, where attacker-chosen text such as `nope/..` then
  persisted in the Folders table until a restart. Escaping held throughout, so
  there was no XSS.
  The rule now is **refuse any spelling this index would not have generated
  itself**: `posixpath.normpath(rel) == rel`, not absolute by either
  platform's reckoning, not `..` or below it, and the resolved path equal *as
  text* to the plain join. That last one earns its place twice over. It refuses
  a symlinked component, which the scan already skips (`follow_symlinks=False`)
  and `reconcile_file()` did not. And it is compared as text rather than as
  `Path`, because `Path` equality on Windows is case-insensitive while
  `resolve()` hands back the spelling on disk, so `gate.mkv` and `Gate.mkv`,
  one file on NTFS, would otherwise be two rows on the platform the Windows
  port runs on. What that does not reach is a case-insensitive share mounted on
  Linux, where `resolve()` normalises no case and the second spelling stats
  fine; the same limit `same_file_name()` states, and the same reason.
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
- **The web panel links to the release; it does not reproduce it.** A release
  body is markdown, and this page is not a markdown renderer, so `##` headings
  and backticked commands appeared verbatim and read as this program having
  failed to format something. Reported 2026-08-11. The alternatives were a
  markdown renderer (a dependency, or a parser to maintain, for a paragraph
  nobody reads twice) or a link to the page that already renders it properly.
  Consequences worth keeping: the panel needs no `notes` in its cache, and the
  changelog fetch that used to fill it when a tag had no Release body is gone,
  so the service's one outbound request is now *always* one.
- **Both version numbers are printed in one shape.** "Installed" comes from
  `__version__` (`0.1.4`) and "Latest" used to come from the git tag
  (`v0.1.4`), so a two-row list carried a `v` on one of two adjacent values
  and the pair read as different kinds of thing rather than as the same
  question answered twice. Reported 2026-08-12. `latest_label()` prefers the
  normalised `latest` the cache already holds and keeps the tag only as a
  fallback; the tag is still what the *terminal* prints, where it names a ref
  the installer will fetch.
- **The cache is clamped to the installed version when it is behind.** An
  upgrade run from the terminal moves `__version__` without touching
  `update.json`, so for up to a day the panel read "Installed 0.1.4 / Latest
  0.1.3", which reads as this checker being wrong rather than merely old.
  Reported 2026-08-12, from a 0.1.3 to 0.1.4 upgrade. Upstream cannot be
  behind what is installed here (the tag is where the installer got it), so a
  cached answer older than the running version is simply out of date. The
  clamp lives in `snapshot()` and takes the stored `tag` and `url` with it,
  since those name the *older* release and would otherwise label a link
  "0.1.4" and land the reader on 0.1.3's page. The **cache itself is not
  rewritten**: it stays the record of what GitHub said, and "Last successful
  check" keeps saying how old that answer is, which is the honest part.
- **A link that leaves the UI opens in a new tab.** Every other link on these
  pages navigates within this server, so replacing the page somebody is
  reading with github.com is the odd one out. `external()` is the only place
  that emits `target="_blank"`, and it always pairs it with
  `rel="noopener noreferrer"`.
- **The 4,000-character cap still exists for the terminal.** `timelapse
  update` prints the notes before asking to install, where plain text is the
  native format. v0.1.0's own release body was 4,020 characters, and a plain
  slice cut it three characters into a sentence with nothing recording that it
  had, so it read as this program losing the rest. `clip_notes()` cuts on the
  last line break instead (word break, then a blunt cut, for notes written as
  one paragraph) and returns whether anything was dropped, so the CLI can say
  so and name where the rest is. The half-limit floor stops a body whose only
  newline is near the start being trimmed to almost nothing. **Never cap
  rendered text without reporting the cap and offering the whole thing
  somewhere.**
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

**Service state and logs** shell out only when a human asks for a page. The
services table is one `systemctl show` on the overview; `/status`
and `/logs` are one command each. Nothing polls and nothing is collected in
the background, and the endpoints a machine might hit in a loop (`/healthz`,
and the `/scan` and `/update` fragments the page's own scripts poll) run no
commands at all. That last property is the one to preserve: the overview
itself stopped being free when the services table moved onto it, which is the
deliberate price of dropping a tab.

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
- **The overview is ordered by how often the question is asked** (0.1.9):
  Cameras, Last encode, Services, Version. It had grown in the order the
  panels were built, which put the version number, the thing nobody opens the
  page for, above the cameras, the thing they do. `OVERVIEW` is the whole of
  it, so the order is one template rather than a call sequence, and a test
  pins the four headings in sequence.
  - **The library's location moved to the Library tab**, into the panel that
    already answered every other question about the library, so the same
    question stopped being answered in halves. `where_rows()` emits it in
    *both* of that panel's states: when the library is unreadable the path is
    not implied by a listing being on the screen, and that is precisely when
    somebody needs to read it.
  - **The fault did not move with the detail.** An unreadable library still
    warns on the landing page, now as a one-line note carrying a link to the
    tab. It is empty when there is nothing wrong, so the healthy page loses
    the section entirely; the alarm is what the landing page is for.
- **Every column starts at the same edge** (0.1.9). The counted columns
  (`td.num`: frames, coverage, sizes, and most of the library table) were
  right-aligned while every other cell and *every header* was left-aligned, so
  each number sat away from the label naming it. They are left-aligned now and
  keep `tabular-nums`, which is what makes a stack of counts scannable without
  moving it. Reported by the operator 2026-08-15.
- **Every timestamp on the page is rendered in one format, at the point of
  display** (0.1.9): `2026-08-15 16:43:21`, by `show_stamp()` for text and
  `stamp_of()` for epoch seconds. Three sources feed this page and each has its
  own idea of a timestamp: the state files carry ISO 8601 with a `T`, systemd
  carries `Sat 2026-08-15 16:42:21 EEST`, and the index carries epoch seconds.
  All three were being printed as they arrived, so one overview showed two
  formats and the same instant looked like two different kinds of fact.
  Reported by the operator 2026-08-15. Two decisions inside it:
  - **The conversion is at the point of display, not at the point of writing.**
    ISO 8601 is correct in a machine-readable state file and systemd's format
    is not ours to change, so nothing that writes a file moves.
  - **The weekday and the zone are dropped.** The weekday is derivable from the
    date, and the zone is the server's own, identical on every row of every
    page, which makes it noise on all of them. An unparseable stamp is passed
    through unchanged rather than blanked: it is still the only answer
    available, and hiding it would turn a format surprise into a missing fact.

  `/status` and `/logs` are exempt, deliberately: they hold command output
  verbatim, which is what makes them worth pasting into a bug report.
- **The services table answers the question in one word, at the foot of the
  overview.** It was a tab holding `systemctl status` verbatim, which is a
  page per unit: the invocation ID, the cgroup, the PID, the task count and
  the same `Documentation=` URL once for each of the four units, to say
  whether capture is running. `unit_states()` asks `systemctl show` for the
  ten properties the table needs and `describe_unit()` turns each into one
  word. Once that was four rows it no longer justified a quarter of the
  navigation, so it moved onto the overview and the UI went to three tabs.
  `/status` survives as a page rather than a tab, holding the full output: a
  bug report wants exactly that, and an old bookmark should land somewhere
  useful. It is deliberately *not* inlined into a `<details>` on the overview,
  which would cost a second subprocess and a screen of markup on every view of
  the landing page to serve something rarely opened. Three things this must
  keep getting right, all verified against real systemd rather than reasoned
  about:
  - **`show`, not `status`.** The human output is not a contract, exits 0 even
    for a unit that is not installed, and names its own fields.
  - **What "not running" means depends on the unit.** The nightly encode is a
    oneshot and is inactive for 23 hours 22 minutes of every day; calling that
    "Stopped", as the daemon rule would, invents a fault on a healthy system
    every time anybody looks. The third field of `STATUS_UNITS` carries this.
    Two neighbours of the same trap: a `RemainAfterExit` oneshot reports
    `active (exited)` long after it finished, and must not read "Running"; and
    `activating/auto-restart` is a crash loop, not a service caught mid-boot,
    so it is red and says so.
  - **A oneshot is `activating` for the whole of its run**, not for a moment
    at the start of it: `activating/start`, with the start time in
    `InactiveExitTimestamp` and both other timestamps empty until it finishes
    (measured on systemd 255). The daemon word for that state is "Starting",
    and the nightly encode holds it for twenty minutes and more, which reads
    as a job that never got going. Reported 2026-08-12. It reads "Running",
    with the time it began.
  - **An inactive oneshot with a timestamp is a success, and says so in
    green.** A finished run and one that has never happened are both
    `inactive`; the timestamp is the only thing that tells them apart. "Idle"
    was true of both and useful about neither, when the question that row
    exists to answer is whether last night's encode worked. A failed run does
    not reach here (systemd leaves it `failed`), but the green line is
    conditioned on `Result` anyway rather than on the timestamp alone.
  - **A timer's next run lives in one of two properties, and which one depends
    on how it is scheduled** (0.1.8). A calendar timer (`OnCalendar`, the
    nightly encode) fills `NextElapseUSecRealtime` with a timestamp. A
    monotonic one (`OnBootSec`/`OnUnitActiveSec`, the credential watch) leaves
    that **empty** and answers in `NextElapseUSecMonotonic` as a timespan
    since boot, e.g. `5min 1.016502s`. Reading only the realtime property left
    the credential watch row saying "Scheduled" with an empty Detail, which an
    operator reasonably read as something being wrong with it; reported
    2026-08-14. `next_run_detail()` reads both and renders "next run in 4m",
    and `LastTriggerUSec` is appended when the timer has ever fired, because
    that is the half that proves it works. Three notes: the timespan format is
    documented in systemd.time(7), which is what makes parsing it safe where
    parsing `systemctl status` is not; `infinity` means the timer will not
    fire again and is said in words rather than treated as a duration; and the
    subtraction against `time.monotonic()` is exact because both are
    `CLOCK_MONOTONIC` on Linux. **No timer row may ever have an empty Detail**,
    and a test pins that as the invariant rather than as one case.
  - **Rows are matched by `Id`, never by position.** A block that does not come
    back would otherwise shift every later unit onto the wrong row.
  The one state that looks healthy and is not gets called out: enabled=no
  while running means it will not come back after a reboot.
- **The Cameras and Last encode panels answer what that table structurally
  cannot** (0.1.6). Every row above comes from systemd, and systemd's opinion
  of a capture daemon whose cameras are all refusing connections is `active
  (running)`. These two read §3a instead, and the judgement lives here rather
  than in the daemon: `camera_verdict()` allows two intervals of silence and
  never less than fifteen seconds, so a camera at one frame a minute is not
  measured by a five-second camera's clock. Four details worth keeping:
  - **"Last frame" is the newest frame on disk, for every camera** (0.1.8).
    `last_frame_epoch()` takes the newest name from the same directory pass
    that counts the frames, so it costs nothing extra, and turns it into a
    time with `frame_epoch()`. Requested by the operator: an RTSP row said
    "recording" where every other row said "12s ago", because the daemon has
    no last-frame time for a stream another process writes.
    - **The time comes from the *name*, never from mtime**, which is the one
      correction to that request. Order and time are properties of the name
      throughout this project (§2), because a PowerShell predecessor mixed
      `CreationTime` and `LastWriteTime` and deleted the wrong files; mtime
      would also be wrong after any restore or `rsync` that did not preserve
      timestamps. `FRAME_NAME` matches a prefix so the DST fall-back suffix
      (`HHMMSS-1.jpg`) still parses.
    - **It gained a fault the page could not previously see.** An RTSP
      grabber can be alive and producing nothing; `alive` said "recording" in
      green for as long as that lasted. Measured on a synthetic day: a camera
      whose newest frame is 66 minutes old now reads red at 93% coverage.
      A *stopped* grabber still overrides the frame reading, because that is
      the more actionable fact and the reason there will be no more frames.
    - **Yesterday is consulted only when today is empty.** That is the minute
      after midnight, when the newest frame belongs to the day that just
      ended, and it costs one listing of a directory that is empty anyway.
  - **Silence is measured against now when it comes from disk, and against
    the snapshot when it falls back to the heartbeat.** The heartbeat lands
    once a minute, so measuring *it* against now would add up to a minute of
    the file's own age to every camera and paint a healthy five-second camera
    as a minute quiet. A file on disk has no such lag, so the same correction
    applied to it would be an error in the other direction.
  - **A stopped or stale daemon is announced above the table**, because it
    explains every quiet row beneath it and leaving the reader to infer that
    from eight red rows is not an explanation. `paused` gets its own line for
    the same reason: it is the one state systemd gets actively wrong.
  - **A missing state file is a sentence, not a fault.** It is what every
    install shows between upgrading and restarting the units, and the panel
    says so in those words rather than reporting an error.
  - **A file claiming a newer format is refused rather than guessed at.** The
    units restart on upgrade and this service may not have; reading an unknown
    format would put invented numbers on a page whose entire value is being
    true.
  - **Frames and coverage are counted from disk, not read from the heartbeat**
    (0.1.8). The daemon's `ok` counter resets on every restart, so "48 frames"
    after a restart at 17:55 answers a question nobody asked; and it does not
    exist at all for RTSP, where a separate process writes the frames, so that
    column used to read `-`. `count_frames()` walks today's directory with
    `os.scandir` and never stats a file, `today_progress()` divides by the
    seconds elapsed since local midnight, and both camera methods get the same
    number by the same route. Requested by the operator 2026-08-14, whose
    argument settles it: an operator wants to know whether today has been
    captured, and only the directory knows that.
    - **The day's own `.cadence.json` beats the running config**, exactly as
      the encoder does it. A cadence edit lands at midnight, so today may
      still be running on yesterday's answer, and measuring against the new
      one would report a perfect day as a near-outage.
    - **`coverage_of()` in `timelapse_encode` is the only place this
      arithmetic lives**, shared with the nightly summary so that "94% at
      18:00" and "94% overnight" cannot come to mean different things.
      `coverage_pct()` keeps its own rule that no frames means no answer,
      because a nightly row exists only for a day that was processed; the live
      panel says **0%**, which is the case an operator most needs stated.
    - **Zero is not the same as unreadable.** A missing directory counts 0,
      because "no directory" and "an empty directory" mean the same thing to
      an operator, but an `OSError` renders `?` with a line saying it is a
      statement about this service's access and not about the camera.
    - **Counts are cached for `COUNT_TTL` (20s)**, keyed on a time bucket so
      old entries can never be read again. Measured on a synthetic worst case
      of 9 cameras at 17,280 frames (155,520 files, a full day at 5s): 37 ms
      on Linux, 80 ms on the Windows dev box, and free on a cached refresh.
      **Do not treat that as a budget**: it was measured on an SSD with a warm
      dentry cache, and the deployment's frames root is an HDD. The bound that
      matters is the TTL and the fact that `frames_root` is local by design.
  - **The Problems column is empty when there is nothing wrong**, and the RTSP
    verdict says "recording" / "not recording" rather than naming ffmpeg. Both
    from the same operator report: a column reading "0 failed" on every healthy
    row trains the eye to skip it, and a reader of a status page has not asked
    which program does the work.
- **The logs page drops the 54rem reading column.** That width suits prose and
  tables and is wrong for raw command output, whose line length journald
  decides, not us. `_render()` adds `pane-page` to `<body>` for it
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
`config.json` and never renders a camera URL; those hold credentials. **It also
never renders command output unredacted**, which is a separate promise and was
learned the hard way: not rendering a URL is worthless when the journal it
displays is full of them (see §4.9). `run_command()` redacts at the source, so
a new page that shows command output cannot reintroduce the leak by omission.
Routing
is an explicit allow-list; anything unrecognised is 404 with no filesystem
lookup. `server_version`/`sys_version` are overridden so the interpreter
version is not advertised.

`Handler.timeout`, not `Server.timeout`: `ThreadingHTTPServer.timeout` is only
consulted by `handle_request()`, which `serve_forever()` never calls, so
setting it there looks like a timeout and is not one.

### 4.6 `timelapse_update.py`

Two things in one file, because they are the same knowledge: the GitHub
release query, and the `timelapse update` command.

Imported at module level by `timelapse_web.py`, one-way, for its version
panel. That used to be described here as the only module-level import between
these scripts, and it has not been true for a while: `timelapse_web.py` also
imports `timelapse_encode` at module level (so that a redaction failure is a
startup failure rather than an unredacted page), and since 0.2.0 every script
imports `timelapse_platform` (§4.6a). The exception still earns itself. Two
callers need to
know which tag is newest, and two copies of that means two places to get the
tuple comparison wrong (`0.0.10` sorts below `0.0.9` as a string), two places
to forget GitHub's mandatory User-Agent, and two places that have to know this
repo has nine tags with no Release behind them. It resolves because both files
are installed into the same directory, which is `sys.path[0]` for either entry
point; the tests put `scripts/` on the path for the same reason.

Upgrading is re-running the installer, so that is what this does:

1. Ask which release is newest, and compare as tuples.
2. Show the notes, clipped on a line boundary (§4.5) rather than mid-sentence.
   This is the one place that renders them: a terminal is where markdown as
   plain text is the native format, and the web panel links to GitHub instead.
3. Confirm, unless `--yes`.
4. Download `install.sh` **for that tag**, not for `main`. An installer newer
   than the tree it unpacks can expect files that tree does not contain.
5. Refuse it unless it looks like the installer and passes `bash -n`. This
   runs as root; a 404 page, a captive portal and a proxy error all arrive as
   a perfectly successful HTTP response.
6. Hand over, into a directory holding nothing but `install.sh`, so the
   installer's `obtain_source()` does not mistake it for a checkout.

**Step 3 fails closed, and until 0.2.1 it failed open three ways** (0.2.1).
`ask_yes()` returned its default when there was no terminal, caught a
`KeyboardInterrupt` and returned the default, and read an EOF the same as
pressing Enter. The default at the only call site is **yes**, and the question
is "shall I download an installer and run it as root". So an unattended
`timelapse update` upgraded without being asked to, which is exactly what
`--yes` exists to say on purpose; and an operator who changed their mind at the
prompt and pressed Ctrl-C watched the installer start.

Every way of not answering means no now. No terminal says so and names `--yes`
as the way to mean yes in advance. An EOF is distinguished from Enter by
checking the raw line before it is stripped, since both are `""` afterwards.
And a **Ctrl-C exits 130** rather than returning False: an interrupt is not an
answer, and a caller reading a bool cannot tell the two apart. The wizard exits
1 for the same gesture and that difference is deliberate rather than
overlooked; the wizard is not writing `/opt/timelapse` when it happens.

This is the opposite rule from `ask()` in the wizard, where a missing answer
takes the default, and both are right for what they do. The question to ask of
a prompt is not "what is the default" but **"what does this do if nobody
answers"**, and the answer has to be safe for the specific thing that happens
next.

The privilege check sits between steps 3 and 4, not at the top of `main()`:
`--check` answers the question without root, and only acting on the answer
needs it. `--check` exits **10** when an update is available, so a cron job
can notify without a human reading the output.

### 4.6a `timelapse_platform.py` (0.2.0)

The one file allowed to know which platform it is running on. Everything else
is platform-neutral and must stay that way; the rule is
**no `if os.name == "nt"` outside this module**, and `test_platform.py` scans
for it rather than leaving it as a convention.

It exists because of the Windows port (future-features.md item 11), but it is
not conditional on that port being finished. What it does today is hold the
Linux answers that were previously spelled as literals in six files, which is
worth having on its own: `/var/lib/timelapse/state` appeared in three scripts
and `/etc/timelapse/config.json` in five.

It answers a **closed** set of questions, and closed is the point. A module
that grows a question per call site is a second copy of the scripts under a
different name:

| Question | Answer |
|---|---|
| where the config lives | `CONFIG_DIR`, `CONFIG_PATH` |
| where runtime state lives | `STATE_DIR_DEFAULT`, `WEB_STATE_DIR_DEFAULT` |
| where data lives by default | `DATA_ROOT_DEFAULT` |
| what a component is called here | `native_name()`, `CAPTURE_UNIT` and friends |
| is a service running | `service_is_active()`, `service_state()` |
| restart one | `restart_service()` |
| register or remove one | `install_service()`, `install_task()`, `remove_*()` |
| be one | `run_as_service()` |
| may this process change the box | `is_elevated()` |
| how an operator drives one | `start_hint()`, `stop_hint()`, `restart_hint()`, `log_hint()`, `elevation_hint()` |
| secure a file holding passwords | `secure_secret_file()` |
| is this name usable as a filename | `is_reserved_name()`, `same_file_name()` |
| how a log file is kept | `log_handler()`, `DailyFileHandler` |
| which disks could hold frames | `scan_filesystems()`, `os_disk_mount()` |
| is this path really on the network | `network_path()`, `unc_for_drive()`, `mapped_drives()` |
| where ffmpeg might be | `find_tool()`, `resolve_tool()` |

Four properties are load-bearing.

**It never prints.** Everything returns a value and the caller words it. A
platform module that writes to stdout is one a Windows service cannot call:
under the SCM there is no console, `sys.stdout` may be a dead handle, and a
stray print kills the service entry point in a way that presents as "this
approach does not work" rather than as a bug.

**"Cannot be asked" is a value, and it is not "no".** `service_is_active()`
returns `None` where there is no systemd, and `None` where the SCM refused the
question. Reporting a healthy system as stopped is the same error the
daemon/timer/oneshot split in §4.5 exists to avoid, met in a smaller place.
`service_state()` splits that answer three ways rather than two, because
*absent*, *stopped* and *unanswerable* need three different responses: install
it, start it, or say you could not tell. It is deliberately falsy for absent, so
a caller reading it as a boolean still gets the right answer.

**The Windows halves exist only where a caller does.** Locations are
implemented for both platforms because everything imports them today. File
permissions and the storage scan are Linux only, and on Windows they degrade to
exactly what they already do on a Linux box without `os.statvfs`: they decline.
There is no stub that lies. In particular `secure_secret_file()` does
**nothing** on Windows rather than calling `chmod`, because `chmod` there sets
one bit (read-only), so `0640` would report success for a file every account on
the machine can still read.

**Both branches are testable from either CI leg.** `locations()` takes the
platform as an argument rather than reading it, and builds the Windows answers
with `ntpath.join`, so the Windows paths are asserted on Linux and the Linux
paths on Windows. This matters more than it looks: a platform branch is
otherwise code that one runner cannot reach, which is the standing cost of
sharing a codebase across platforms. The service and permission functions read
`IS_WINDOWS` at call time for the same reason, so the tests patch it.

**Two of its answers are enforced on both platforms, which looks wrong and is
not.** `is_reserved_name()` refuses `NUL`, `CON`, `AUX`, `PRN`, `COM1-9` and
`LPT1-9` as camera names on Linux too. A `config.json` is portable between
platforms by design, so a name only one of them accepts is a trap set for
whoever moves the file, and refusing a Linux operator a camera called `AUX`
costs nothing anyone will ever notice. The measured hazard is much narrower
than the folklore, and the note in the module says so: only `NUL` touches this
project's path shapes, and it fails loudly rather than silently.
`same_file_name()` is the other way round: it uses `os.path.normcase`, so it
answers for whichever filesystem is running, which is what makes it correct to
report `Workshop` and `workshop` as one directory on NTFS and as two on ext4.

**Logging is the one place Linux and Windows genuinely diverge in behaviour.**
Linux keeps `RotatingFileHandler` and `capture.log`, unchanged, because nothing
is wrong with it there. Windows gets `DailyFileHandler`: one file per day,
named rather than renamed, pruned by age, with no size cap. Renaming is the
whole problem, since Windows refuses to rename a file another process holds
open, which is what the Log tab reading `capture.log` does to the daemon. The
failure is worse than an exception: `logging` catches it, prints the traceback
to stderr and carries on, so the daemon does not crash, it emits a traceback
per record and simply never rotates. Measured both ways: 58 stderr tracebacks
and no rotated files from `RotatingFileHandler`, zero and a correct daily file
from the replacement. The trade is stated rather than hidden: the replacement
is less clever, and in exchange it has no failure mode.

**Two things deliberately did not move.**
`writable_paths()` and `--print-state-path` emit `ReadWritePaths=` lines for a
systemd unit, so they name `LINUX_STATE_DIR` explicitly rather than the running
platform's default: a systemd unit is a POSIX artefact even when a Windows box
generated it. And `timelapse_capture.py` carries a character-identical copy of
the path derivation and of the log handler rather than importing them, pinned by
tests in `test_capture.py` exactly as `replace_atomic()` and the redaction rule
are, because it imports nothing from its siblings so that a syntax error in a
script it does not need cannot stop the recording. It is the sole exception to
the `os.name` rule and the scan names it as such.

#### Hosting a Windows service (0.2.0)

`sc.exe create` pointed at a plain `python.exe` does not produce a service. The
SCM expects `StartServiceCtrlDispatcher` within about thirty seconds and kills
what does not answer, reporting **1053**, which reads as a broken script rather
than as a wrong hosting model. `pywin32` answers it and costs a large native
dependency that the one-third-party-package rule will not pay for, so the
handshake is implemented here in `ctypes`, which is stdlib. It was proven end to
end under a real SCM before any of it was designed (future-features.md 11c.2).

`run_as_service(unit, body, request_stop)` is the whole surface.
`body(ready)` runs on the thread the SCM provides and calls `ready()` once it is
genuinely up; `request_stop()` is called from the SCM's control thread and must
return immediately. Four rules are load-bearing and each was a bug first:

- **Every `ctypes` function returning a handle declares its `restype`.** The
  default is `c_int`, which truncates a 64-bit `SC_HANDLE`, and it works for as
  long as the SCM issues small values: a defect that passes on the machine it
  was written on.
- **Nothing in the service path prints.** There is no console, so the only
  witness a failure has is `on_error`, which the daemon points at its log and at
  `service-error.log` beside the config.
- **The entry point carries its own exception guard.** An exception escaping a
  `ctypes` callback goes to a stderr that is nowhere, and the service then looks
  like it hung rather than like it failed.
- **The control handler reports `STOP_PENDING` with a wait hint before the slow
  work.** Capture has a camera thread per camera to join, each possibly holding
  a fetch open, and an SCM that has not been told to wait calls the stop hung.

**A stop the SCM should act on has to look different from one it should not**
(0.2.1). The four rules above were all about surviving; this is about dying
properly. `report()` set `dwWin32ExitCode = 0` on every status it ever sent,
including the `SERVICE_STOPPED` in `on_main`'s `finally`, which runs whether
`body` returned or raised. To Windows that is a clean stop, and **the SCM runs
a failure action only for a service that either died without reporting
`SERVICE_STOPPED` or reported one with a non-zero exit code**. So the three
`SC_ACTION_RESTART` entries, described above as the equivalent of
`Restart=always, RestartSec=15`, were inert for every failure that is an
exception rather than a process kill, which is nearly all of them: this code
catches what escapes and reports it tidily.

The concrete case: `write_state()` raises once because something held
`capture.json` open past `replace_atomic()`'s retry window. On Linux capture is
back in fifteen seconds. On Windows it stopped, wrote one line to
`service-error.log`, and stayed stopped until somebody rebooted.

Two halves, and neither works alone. `report()` takes an `exit_code`, and a
failure reports `ERROR_SERVICE_SPECIFIC_ERROR` (1066) in `dwWin32ExitCode` with
the meaning in `dwServiceSpecificExitCode`, which is the only way a service can
report a failure of its own. And `install_service()` sets
`SERVICE_CONFIG_FAILURE_ACTIONS_FLAG`, because
`fFailureActionsOnNonCrashFailures` is FALSE by default and with it FALSE the
exit code changes nothing. A deliberate `sc stop` still reports zero and still
queues nothing.

**The parity is exact, including the part nobody enjoys.** A `config.json` that
will never parse now fails, restarts fifteen seconds later, fails again, for
ever, appending a line to `service-error.log` each time: about 5,700 lines a
day. That is precisely what `Restart=always` does on Linux, where `RestartSec=15`
keeps the unit under systemd's default start-limit rate, so it is never given
up on either. The choice was put to the user in those terms and parity was
chosen: a daemon that stays down after one transient fault is the worse
failure, and a broken config is visible in a way a stopped service is not.

**What the unit tests can and cannot say.** `test_platform.py` drives
`run_as_service()` and `install_service()` against a fake `advapi32` on both CI
legs, which is the first test coverage the SCM half of this module has ever
had; `FakeTable` declares `lpServiceProc` as a `py_object` because
`ctypes.WINFUNCTYPE` does not exist off Windows, which is why there was none.
They prove this program tells Windows the right thing. They cannot prove
Windows acts on it, and that is the whole claim, so `temp/h4_check.py`
registers a throwaway `timelapse-h4probe` service with a five second restart
delay, makes it fall over on purpose, and reads back whether the SCM started it
again, whether each start is a new process, and whether a deliberate stop still
stops. It needs an elevated prompt and never touches the installed services.

The batch jobs are **scheduled tasks**, not services, mirroring the
`.service`/`.timer` split exactly: a service that exits within seconds is a
service in a permanent restart loop. Their definitions are generated as XML
rather than driven from the `schtasks` command line, because the two settings
that matter most cannot be expressed there at all: a repetition interval with no
duration, and `StartWhenAvailable`, which is the Windows spelling of the encode
timer's `Persistent=true`. Reading a task back is the opposite problem and
follows the rule `systemctl show` already set: `schtasks /Query` prints
**localised** field names, so existence comes from an exit code and anything
richer comes from PowerShell as JSON, whose property names are English on every
locale.

The structures are declared with `c_uint32` rather than with `ctypes.wintypes`,
and that is not fussiness. On 64-bit Linux `wintypes.DWORD` is `c_ulong`, which
is **eight** bytes, so every layout would be silently wrong there and every test
asserting one would agree with it. Fixed widths are what make `SERVICE_STATUS`
assertable from the Linux CI legs.

This is the one place the daemon's independence rule bends, and the bend has a
shape. `timelapse_capture.py` imports the platform module inside `run_service()`
and nowhere else, so **on Linux it still imports nothing**: that line never
runs. What the rule protects against is a partial upgrade leaving a stale
sibling beside a fresh daemon, and on Windows that is exactly what
`timelapse version` catches, the platform module being one of the seven files it
compares. The alternative was a sixth pinned duplicate of the most intricate
code in the project.

Registration itself lives in `timelapse_setup.py` (`--install-units`,
`--remove-units`, `--unit-status`) rather than in `install.ps1`, for the reason
`tools/` was deleted: two installers that both know how to register a service
disagree within one release. `service_definitions()` is a pure table mapping
each unit file onto its Windows form, so both CI legs assert the same schedule,
argv and account.

### 4.6b `install.ps1` and `timelapse_cli.py` (0.2.0)

The Windows front door: the counterpart to `install.sh` and to the bash wrapper
it generates. Three properties are load-bearing.

**The installer decides nothing.** It checks privilege, finds an interpreter,
places files, restricts an ACL, writes a two-line wrapper, and then calls
`timelapse_setup.py --install-units`. It contains no `sc.exe`, no `schtasks`
and no `New-Service`, and a test asserts that: two installers that both know
how to register a service disagree within one release, which is why `tools/`
was deleted. When the GUI installer of item 11c.6b arrives it front-ends this
file for the same reason, one level up.

**Three things it deliberately does not do**, each decided rather than skipped.
It does not install **ffmpeg** (§11c.6a: Windows has no package manager worth
relying on, and a recorder usually already has an ffmpeg the operator chose, so
installing a second copy would override a decision that was not ours). It does
not install **Python**; it finds one, records its absolute path, and names
python.org when there is none. And it does not carry service state across an
**upgrade** the way `install.sh` does, because registration overwrites and
there is no field of Windows installs whose states need preserving yet.
`timelapse update` is correspondingly Linux-only, and says so rather than
half-working.

**A per-user Python is warned about, not refused.** It demonstrably works, and
refusing a working configuration is the `try_rsync_args` error. But the service
runs as LocalSystem reading an interpreter out of another account's profile, so
removing that profile or reinstalling Python for a different user stops the
service starting, weeks later, with nothing connecting the two events.

`timelapse_cli.py` is the `timelapse` command. It exists because a batch file
has no heredoc and its escaping would turn a page of help text into a wall of
`echo` lines that would rot; `timelapse.cmd` is two lines calling it. Like the
installer it is a **dispatcher**: every command builds an argv and runs a
sibling, so there is no second implementation of any behaviour. Its locations
are derived rather than baked, the scripts being beside it by construction and
the config coming from the platform module, so moving the install directory
cannot leave the wrapper pointing at the old layout.

Two front doors onto one set of commands is a drift risk, and it is answered by
a test rather than by a promise: `test_cli.py` reads the command labels out of
`install.sh`'s generated wrapper and holds them against this file's table, with
an explicit list of what is Linux-only and why. A command that exists on one
platform and not the other must also be *answered* rather than reported as
unknown, because "unknown command" reads as a typo and the operator retypes it.

**The file-permission model does not translate, and the Windows shape is
better.** On Linux `timelapse config` has to restore 0640 root:timelapse after
`$EDITOR`, because an editor that saves by rename leaves root's umask on a brand
new file and the config loses its group. On Windows a new file inherits the
*directory's* ACL, so `install.ps1` breaks inheritance on
`%ProgramData%\timelapse` once and grants only SYSTEM and Administrators; every
file created there afterwards, including an editor's temporary copies, is
covered by where it is rather than by what the editor did.

**What that ACL also covers is `bin\timelapse.cmd`, so every command needs an
elevated prompt** (0.2.1, documentation only). The grant is `(OI)(CI)`, so it
propagates, and under UAC's split token an allow entry for Administrators does
not apply to an administrator who has not elevated. Measured unelevated on a
real box against a scratch directory granted exactly what `Protect-ConfigDir`
writes: reading the wrapper raises `UnauthorizedAccessException` and `cmd /c`
answers `Access is denied`, exit 1. So the refusal happens **before any of this
project's code runs**, which means the dispatcher's own careful refusal message
is never reached and the four words the operator sees are not ours to improve.

The installer's closing advice used to name `status` and `version` as needing
neither a new window nor administrator. That was measured false, and the
scoping call is to **say so rather than to widen the ACL**: the Windows build
exists so that nobody has to use a terminal (Start menu, Timelapse Setup,
which elevates itself), so the terminal being an elevated one costs the
ordinary operator nothing, while a Users grant on the wrapper directory would
be a second permission story to keep straight next to the one that guards the
camera passwords. A test pins the installer text against the old claim.

**The reference install is the one box where the claim was true**, which is how
it survived: `icacls` there shows a third entry for the author's own account
that `install.ps1` never writes, left behind by Explorer's "you don't currently
have permission to access this folder, click Continue to permanently get
access". So the README's "restricted to SYSTEM and Administrators" was already
untrue of the machine it was written on. **Anything in this tree that is
compared against a developer's own box has to be compared against a clean ACL
too**, which is the permission-model cousin of "a local branch is a branch CI
has never seen".

**The videos are the exception, and are opened back up** (0.2.1).
`DATA_ROOT_DEFAULT` is that same directory, so an operator who accepts the
default on a single-disk box would have `frames`, `videos` and `logs` inside
the restricted tree, and a timelapse the operator cannot double-click is not a
delivered product: that is the product's output rather than its administration,
and an elevated prompt is no answer to it. `allow_users_read()` grants
`*S-1-5-32-545` read and execute on `video_output`, and
`open_video_dir()` decides when.

**Two conditions, and each one is the rule.** *Inside the config directory*,
because a `video_output` on another drive was chosen by the operator, is
already readable, and is not ours to change permissions on: **only undo the
restriction this program applied**. And *videos only*: frames are transient and
nobody browses a hundred thousand of them, while a log is the one place in this
project a camera password has ever actually turned up (§4.9), so opening the
logs would widen the blast radius of the next leak.

`inside_config_dir()` asks `Path.parents` rather than `startswith`, and that is
load-bearing twice: a path is never its own parent, so a `video_output` set to
the data root cannot lift the restriction off `config.json`, and a prefix is
not a parent, so `...timelapse-other` is not inside `...timelapse`. An explicit
equality guard was written in front of it and then **removed, because the
revert check showed it could not fail**: it restated what `parents` already
guarantees. Two tests pin the property instead and both fail against the
`startswith` version.

**Read and execute, never write**: a surveillance archive any account can
quietly alter is worth less than one it cannot open. Measured unelevated, first
against a hand-built ACL (`temp/c3b_users_grant_probe.ps1`) and then through
the shipped functions (`temp/c3b_check.py`, 21 checks): a video written *after*
the grant opens, the folder lists, the data root above it does not,
`config.json` and the logs and the frames stay closed, and an ordinary account
can neither add nor delete a file there.

**What to tell an operator**: opening the folder by its path, from a shortcut
or from a saved playlist works, but *browsing* down to it from
`%ProgramData%\timelapse` does not, because browsing means listing every
directory on the way and the one above is closed. The path works only because
"bypass traverse checking" is granted to Everyone by default and skips exactly
that check.

**Two doors, one implementation.** `create_directories()` calls it, so it
follows the videos wherever the wizard puts them and covers any later change of
`video_output`; `--grant-video-access` is the same function for `install.ps1`,
because an upgrade never runs the wizard and an install made before this
release would otherwise stay closed for ever. On a first install the flag runs
before there is a config and exits 0; the wizard does it minutes later.

**The grants are SIDs, not names** (M22). `icacls` resolves a name through the
local SAM, and the group is `Administratoren` on a German Windows and
`Administrateurs` on a French one; the English spelling comes back as "No
mapping between account names and security IDs was done". `Protect-ConfigDir`
used to warn and carry on, which would leave the directory holding every camera
password with the permissive ACL it inherited from `%ProgramData%`: readable by
every account on the machine, on exactly the installs least able to notice.
`*S-1-5-18`, `*S-1-5-32-544` and `*S-1-5-32-545` are identical in every
language, and a test scans the installer's grant arguments for anything else.

**The name an administrator finds a service by is its display name.**
`services.msc` sorts by it, and `CreateServiceW`'s third argument is
`lpDisplayName` rather than the description. Passing the description there
filed the capture service under "Camera snapshot capture for timelapse", so
`sc query` reported RUNNING while the Services window appeared not to list it,
which costs half of §11c.2's argument for using real services at all.
`DISPLAY_NAMES` spells them so that the two services and the two scheduled
tasks sort together in one block. The description is set separately by
`ChangeServiceConfig2W`.

**Re-running the installer reconfigures rather than failing.**
`CreateServiceW` refuses a service that already exists, so `install_service()`
falls back to `ChangeServiceConfigW`, and `install_units()` restarts a service
that was running before it was re-registered. Both halves matter: without the
first, the second install of any version fails on every component; without the
second, the installer reports success while the old build keeps running, which
is the trap `restart_upgraded_services()` exists for in `install.sh`.

**A drive letter cannot be resolved from the process that needs to resolve it.**
Drive mappings belong to a logon session, and UAC's split token gives an
elevated process a different session from the desktop that launched it. The
wizard always runs elevated, so `WNetGetConnectionW` sees none of the
operator's mappings: the check written to warn that a *service* cannot see
drive mappings turned out to be running somewhere that could not see them
either, and stored `U:\TL` verbatim on the first real run.
`HKCU\Network\<letter>\RemotePath` is the source that survives, because the
split token is still the same user and so the same hive. It covers persistent
mappings; for a letter neither source resolves, the wizard refuses and asks for
the UNC, since from there a non-persistent mapping and a typo are
indistinguishable and want the same answer.

**Colour is a platform question, not just an `isatty()` one.** A Windows
console understands ANSI escapes only once `ENABLE_VIRTUAL_TERMINAL_PROCESSING`
is set, and conhost leaves it off, so the wizard printed its escapes literally
on the first real install. `use_colour()` in the platform module owns the
decision for both `timelapse_setup.py` and `timelapse_test.py`, and setting the
bit is part of answering rather than a separate step, because nothing else in
the process will do it. It fixed an older bug in passing: the pre-flight emitted
escapes unconditionally, so `timelapse test > report.txt` wrote them into the
file on every platform. Note where this was invisible, because it is the same
shape as the padded-then-coloured column in §4.4: Windows Terminal sets the bit
itself, and no test or CI runner is a terminal, so every environment that could
have caught it had colour switched off.

**No Python one-liner in `install.ps1` may contain a quote.** PowerShell strips
embedded double quotes when it hands arguments to a native executable, so
`-c 'print("%d.%d" % sys.version_info[:2])'` arrives at Python as
`print(%d.%d % ...)` and dies with a SyntaxError. Found on the first real
elevated install: the version came back empty, the floor check refused a
working 3.12, and eleven of the harness's eighteen checks failed downstream of
that one line. A test bans the quote rather than trusting each call site; the
formatting belongs on the PowerShell side, where the rules are known.

**`install.ps1` is pure ASCII, and that is measured.** Windows PowerShell 5.1
reads a `.ps1` with no byte order mark as ANSI, so `install.sh`'s box-drawing
banner comes out as mojibake and the installer's first line looks like a
corrupted download. A BOM is the usual fix; ASCII is the one that does not make
this the single file in the repo with its own encoding rule.

### 4.6c Moving the videos on Windows (0.2.0)

There is no rsync here and nothing to mount, so `transfer()` branches once, at
the top, into `transfer_windows()`. What follows is a copy, a rename and a
delete, in that order.

**The order is the substance.** `move_video()` copies to `<name>.part`, checks
the length, renames into place with `replace_atomic()`, and only then deletes
the original. A copy interrupted part way (the NAS rebooting, wifi dropping,
the destination filling) otherwise leaves a file with the right name and the
wrong length, and nothing downstream can tell it from a finished one: the
library index counts it, the web UI offers it, and it plays as far as it got
and then stops. Deleting the source last means the failure mode is always a
duplicate, never a hole. The rename uses the retrying `replace_atomic()`
rather than `os.replace` because the destination is a library the operator
watches from, so the file being renamed over can genuinely be open in a media
player, which is exactly the case that raises `PermissionError` on Windows and
nowhere else.

**The account is the hard part, and it is not a detail.** A connection to a
share belongs to a *logon session*, and the nightly encode is a scheduled task
running as LocalSystem, which presents this **machine's** name on the network
rather than the operator's. So a destination the operator can open in Explorer
is not necessarily one the job can write, and the symptom is an access denied
on a path that demonstrably works. This is the drive-letter trap in its second
disguise, and unlike that one it cannot be fixed by rewriting the path.

Four ways out were considered; the chosen one is **explicit authentication at
transfer time** (`connect_share()`, `WNetAddConnection2W`) with an optional
`username` and `password` in the transfer block. It matches what Linux already
does, where `install.sh` writes CIFS credentials to a 0600 file and the tool
owns the secret rather than the OS; the config already holds camera passwords
and `redact_config()` already covers this key by name. The alternative,
registering the task under the operator's own account, is proven in production
(their predecessor script ran that way for years) but needs a Windows password
stored in Task Scheduler and breaks silently at the next password change. S4U
does not help: an S4U token carries no network credentials at all.

**The current token is tried first.** `reach_destination()` writes a probe file
as whoever it already is, and only reaches for a stored credential when that is
refused. A share that already accepts the machine account needs no password in
any file, and asking for one to solve a problem the operator does not have is
how a tool teaches people to store secrets for no reason.

**Everything probes by writing.** `try_destination()` mkdirs the destination,
writes `.timelapse-write-probe` and deletes it. Existence is not the question
and neither is reachability: a share can be listable by an account and still
refuse it a write, and `Path.is_dir()` *raises* on Windows for a directory the
caller may not read. The wizard, the pre-flight and the encoder all call this
one function, for the same reason `try_rsync_args()` lives beside the code that
runs rsync: three opinions about one share is two too many.

**What the wizard can and cannot prove is stated to the operator.** Run without
credentials, its probe tests the operator's own account and says so; with
credentials it puts those credentials, and only those, to the share.

**That second half was not true until 0.2.1, and the way it was false is the
lesson.** Both wizards called `reach_destination()`, which is the function
above: it tries the current token first *on purpose*. That order is right for
the nightly run and exactly wrong for a check, because of where a wizard runs.
On the operator's own machine the current token is the operator, who can write
to their own NAS, so the probe file went in, "Test successful" appeared, and
the password they had just typed was never presented to anything. The
disconnect the console path already did was not enough: Windows re-establishes
a connection to a server from Credential Manager, which holds the operator's
own credentials in their own hive. The graphical wizard warned only when the
username box was *empty*, which is precisely the case that needed no warning.

`verify_share_credentials()` is the check that can fail. It drops whatever this
session holds to that server, signs in with the stored username and password,
writes the probe, and disconnects again so nothing it made can be passed over
later. It is **stricter about one answer than its neighbour**:
`ERROR_SESSION_CREDENTIAL_CONFLICT` is "not attempted" for the nightly run and
a *failure* here, because succeeding over somebody else's connection would be
measuring that connection rather than these credentials. It answers `None` for
"the question does not arise" (not Windows, not a UNC path, no username), which
is the three-way shape again. `reach_destination()` is untouched: the nightly
run still tries the token first, and a test pins that it does.

The graphical wizard's Next warns, and does not refuse, when credentials are
stored that nothing verified, and it compares against **what the last passing
Test actually sent** rather than against whether Test was pressed: editing the
password after a green tick leaves the tick on screen describing the previous
one. Warning rather than refusing was the user's call and the consistent one,
since the share may simply be switched off while somebody configures this, and
refusing a configuration that would work is the mistake `try_rsync_args()`
exists because of.

`ERROR_SESSION_CREDENTIAL_CONFLICT` is also why `connect_share()` returns
`None` rather than `False` for it. Somebody else's connection may be the one
that works, and tearing down a connection this program did not make is not its
call, so the write settles it. That is the `try_rsync_args()` three-way answer
again: refused, and could-not-attempt, are different facts.

### 4.6d `timelapse_gui.py` and its launcher (0.2.0)

The same wizard in a window, for the operator who chose Windows and would
rather click than type. Same questions, same checks, same config file, same
backup, and `timelapse setup` is untouched: 11c.6b's rule that the GUI must
never become the only way in.

**It decides nothing.** Every check calls a function in `timelapse_setup.py` or
its neighbours: `sanitise_name`, `name_taken`, `is_reserved_name`,
`network_path`, `drive_is_local`, `looks_like_ssh_spec`, `detect_encoders`,
`sibling_tool`, `test_camera`, `reach_destination`, `write_config`,
`create_directories`, `restart_units`. The project has been bitten by a second
implementation before (`tools/` duplicated the wizard, drifted, and was
deleted), and two wizards that both know what a valid camera name is will
disagree inside one release.

**Deciding is separated from showing**, which is what makes any of it testable.
Everything above the `# THE SHOW LAYER` banner takes strings and returns
`(level, message)`; below it there are widgets and calls upward. See §9 for how
that line is held, and for the click-through checklist that covers the half no
test reaches.

**tkinter is imported inside `run()`**, not at module scope, for the reason the
Win32 bindings are lazy: the suite is collected on three Linux legs and in WSL,
none of which has a window station, and an import at the top would take all of
them down at collection time.

**Five pages, not seven.** Storage, ffmpeg and cadence began as a page each,
and each held one question: three Next clicks to answer three things that are
all "how does this machine run", where cameras and destinations are about
everything else. They are three `LabelFrame`s on one page now, which also puts
the cadence in view while the disk is being chosen, the one dependency between
them worth seeing. The remaining four are cameras, transfer, notifications and
review.

**The camera page is a list beside the camera it names**, not a modal dialog
per camera. With the dialog, the only way to see what a camera was set to was
to open it, and the only way to compare two was to remember the first; an
operator with eight cameras is doing that constantly. The pane carries the
name, the make, the address or URL, the authentication, the credentials, the
enable switch, smoothing, Test and Save. Four things about it are deliberate:

- **A camera opens on the make it was added as**, worked out by
  `identify_camera()` rather than stored. Until 0.2.0 every existing camera
  opened on Custom URL, and the reasoning for that was sound: guessing which
  template produced a URL, and guessing wrong, would silently rewrite a working
  camera. What was wrong was treating it as a guess. A candidate is accepted
  only when the **whole round trip reproduces the stored URL exactly**,
  credentials put back through `quote()` the way saving will, so claiming a
  make is a claim that pressing Save changes nothing. Anything unaccounted for
  still opens on Custom, which is the honest answer rather than a fallback: a
  hand-edited Dahua URL with `channel=2` is not what that template produces and
  is not claimed. Two presets share one template (Hikvision ONVIF and Generic
  ONVIF snapshot) and cannot be told apart; the first wins, and it does not
  matter, since they build the same URL.
- **The pane holds a camera by identity, never by position.** Saving can rename
  it and removing shifts everything after it, so an index captured when the row
  was clicked is a stale answer by the time it is used.
- **Rows are hidden with `grid_remove()`**, not `pack_forget()`. A removed cell
  remembers where it was, so a row that comes back cannot land at the bottom of
  the pane, which is a defect this project has shipped twice with `pack()`.
- **`exportselection=False` on the Listbox.** Without it, clicking into any
  Entry on the right hands over the X selection and the list silently
  unhighlights the camera being edited.
- **The cadence and frame rate boxes are empty when there is no override**,
  and empty is the only rendering of "follows the global" that is safe here.
  Per-camera settings are keyed on **absence**: a camera carrying neither key
  follows the global, which is what lets a later change to the global still
  move it. A box prefilled with the current global would be saved as a copy and
  pin every camera anybody had merely opened. `check_camera_interval()` and
  `check_camera_framerate()` therefore return `None` both for blank and for a
  value equal to the global, and `build_camera()` pops the key rather than
  writing it. They validate as hard as the global page does, so a per-camera
  900s cadence is refused here too rather than producing nothing for ever.
- **Each verdict sits beside the button that produced it**, Test on the left
  and Save on the right, and switching camera clears both. One shared line at
  the bottom made "Saved." and "Test successful." interchangeable, and a
  verdict left over from the previous row would be read as being about this
  one. The transfer page's Test does the same.
- **Leaving an edited camera asks first.** `form_is_dirty()` compares the pane
  against what was loaded into it, because a typed password is the one thing
  here that cannot be recovered by looking at the config. Discarding an entry
  that Add created and nobody named removes it, rather than leaving the page
  blocked by an error about a row the operator has already decided against.

**Scan network** puts `timelapse discover`'s capability on the camera page: one
multicast WS-Discovery probe (`setup.discover_cameras()`, unchanged and
unwrapped), a modal list of what answered, and a tick beside each. Everything it
has to decide is above the banner (`scan_rows`, `scan_summary`,
`configured_addresses`, `next_camera_names`, `build_scanned`), which is what
lets the rules be checked without a display. Six things are deliberate:

- **Only devices claiming to be video transmitters are offered**, read from
  `Types` rather than the `type` scope, which is the trap §4.4a exists around.
  An NVR, a doorbell, a printer and every Windows machine on the LAN answer this
  same probe; the others are counted in the heading rather than hidden, so
  "4 cameras answered, 1 other device also answered and is not offered".
- **Already configured is decided on the address**, `camera_address()` reading
  the host back out of the stored URL, because there is nowhere else it could
  come from: a config keeps a URL and an address is only ever what the wizard
  asked for on the way to building one. `wsd_host()` is reused rather than
  restated, including the `ValueError` a malformed literal raises on 3.12+.
  Such a row is greyed, is not tickable, **says "already added" on the row**
  and carries that as a tooltip too: greying alone says "not available" without
  saying why, and a hover message has to be found before it can be read.
- **A make that was not identified is asked for, never guessed.**
  `wsd_preset()` names six of the eight cameras here; the other two report no
  vendor at all (a Reolink calling itself IPC-BO, a Tapo calling itself TC40).
  Every row carries a type picker, prefilled where the make is known and empty
  where it is not, and **a row with no make cannot be ticked**. A preselection
  that is wrong is a wrong URL that looks deliberate, which is worse than no
  answer. **Custom URL is not among the choices**: it builds no URL from an
  address, so picking it there could only produce a camera with no URL at all.
- **Credentials are not asked for here.** The alternative considered was one
  username and password in the scan window applied to every camera it adds,
  which a fleet sharing one admin account would like; per-camera was chosen. So
  a scan contributes an address and a make, and a password is typed in the
  detail pane, where it belongs to one camera.
- **`build_camera()` builds the camera**, not an assembly of its own, so one
  that arrives by a scan is the same shape as one typed in, down to the keys it
  leaves absent. A scanned camera therefore carries no `interval_seconds` and no
  `framerate` and follows the global, and reads back as its own make next time
  the page opens, which is `identify_camera()` closing the loop.
- **Names are `CameraN`**, skipping what is taken via `setup.name_taken` rather
  than a second opinion about what taken means. Not named after what the device
  calls itself: a camera name here is a *place*, and what a camera reports is
  its model, so three Dahuas would all arrive called Dahua.

Nothing answering is reported as "nothing answered on this network segment",
never as "there are no cameras": multicast does not cross subnets or VLANs, and
a dedicated camera VLAN is common in exactly the deployments with the most
cameras.

**Leaving the camera page proves every enabled camera works**, and switches off
the ones that do not. This exists because of what the scan leaves behind: five
ticked cameras arrive enabled with no password, and nothing stopped Finish being
pressed right then, which is five cameras configured to fail. A blank credential
is a failed sign-in attempt rather than a free one.

The operator's own account of why the credentials fork went the way it did is
what shaped this, and it is worth recording: **AgentDVR asked for one credential
pair, applied it to every camera it found, and retried until three of them were
locked for thirty minutes**, because not every camera shared a password, which
is good practice rather than a mistake. That failure has two halves and this
project already fixed the second one. `_retry_grab()` refuses to retry an `auth`
failure inside a tick ("the only thing a second attempt can buy is a second
entry on the camera's lockout counter"), and the item 8 ladder above it is two
strikes, then ten minutes of silence, then 31 (`AUTH_RETRY`), which is the
measured 30-minute Reolink lock plus margin. The first half is what this check
closes. Four things about it:

- **The static reasons cost nothing and run first**, so a camera with no
  credentials is **never fetched from**. `camera_not_ready()` is pure and needs
  no network: a make that signs in with a username and password while carrying
  neither is *unfinished*, which is a different answer from *unreachable*.
  Only when **neither** is set, since a username with no password is a
  deliberate answer this has no business overruling, and the live test settles
  that in one attempt.
- **A proof is not repeated.** `proof_key()` is the URL and the credentials
  together, not the camera: a digest camera's URL does not change when its
  password does, so keying on the address would call a camera proved after its
  password had been changed to a wrong one. It is held on the wizard rather
  than the page, because every navigation destroys and rebuilds the page and a
  proof does not stop being true because somebody pressed Back. The Test button
  writes to the same set, so a camera you have already tested is not asked
  again. Without this, pressing Next twice authenticates against the whole
  fleet twice, which is the AgentDVR shape with a person driving it.
- **A camera that fails is switched off, not refused.** The operator's call,
  and better than either option offered them (hard block, or block with an
  escape hatch): a camera being mounted, unplugged or behind a switch that is
  off is not a mistake, and blocking would hold a ten-minute configuration
  hostage to it. Disabling says so in the list, is one tick to undo, and means
  the daemon never contacts that camera at all, so it cannot collect failed
  sign-ins either. The one case that still keeps the operator on the page is
  everything being disabled, which the same popup says rather than a second
  one.
- **One sentence for one verdict.** `UNREACHABLE` is a module constant used by
  both the Test button and this check, pinned by a test that counts it in the
  source, because two wordings for one fault read as two faults.

**"No credentials required" is a declaration, and default off.** Blank
credentials used to mean two things at once, "this camera needs none" and "I
have not filled these in yet", which is the falsy-return shape this project has
now paid for six times. The tick splits them. Default off is what makes an
unsecured camera something the operator *said* rather than something they got
by leaving two boxes empty.

It is not cosmetic, and that is the part worth remembering. Ticking it sets
`auth` to `none`, because leaving a preset's `digest` in place with no
credentials makes `requests` answer the 401 challenge with an empty username,
which is a real failed sign-in against a camera that wanted none. It also
empties the two boxes rather than merely greying them, since a greyed password
still reading `admin` is a credential the config does not have. It is stored as
`no_credentials: true`, keyed on absence like every other optional camera
setting here.

**Whether this case is real was researched rather than assumed** (2026-08-17).
The ONVIF Core Specification requires everything outside the `PRE_AUTH` access
class to be protected by digest authentication, and in the same breath permits
a device to support a custom access policy set by the user. Vendors ship that
switch: Dahua and Amcrest expose Setup, Network, Connection, ONVIF,
Authentication with values Digest or **None**, and it is used in the field as
the workaround for getting newer cameras to talk to an NVR. A 2025 Shodan study
found ONVIF-compatible models with **no option to disable anonymous login at
all**. Open RTSP is more common still. So refusing a credential-less camera
outright would be refusing working hardware, which is the `try_rsync_args`
error; declaring it is the right answer.

Note that "an ONVIF camera with no credentials" is a narrower thing than it
sounds *here*, for the reason recorded in §4.4a: this project never speaks ONVIF
to capture. The `Hikvision (ONVIF)` and `Generic ONVIF snapshot` presets are
plain HTTP GETs on a path with `onvif` in it, and WS-Discovery sends nothing at
all. The vendor toggle governs the SOAP device service, while the snapshot URL
goes through the camera's ordinary web account.

**`setup.fill_template()` is the one place a preset becomes a URL**, reached by
the console wizard, `build_camera()` and `identify_camera()`'s round-trip check
alike, so none of them can disagree about what a preset produces. It drops the
`user:password@` form when **both** are empty, because `rtsp://:@host/stream1`
is a legal but strange spelling of an open stream: it reads as a mistake, and it
invites a client to attempt an authentication nobody asked for. A username with
an empty password is a different and deliberate answer and is left alone, and a
query string carrying credentials is never touched, since a Reolink's
`&user=&password=` is part of the address of the resource rather than how it is
authenticated. `identify_camera()` therefore tries **two shapes per template**;
without the second, an open RTSP stream would read back as Custom URL, which is
the defect that function exists to fix arriving by a new route.

The declaration is **GUI only**, which is a scope decision rather than an
oversight: the operator's direction 2026-08-17 is that on Windows the console
wizard should eventually not be a user-facing path at all, with the GUI the only
place a camera is configured. That is Windows-only. On Linux the GUI refuses to
run (`TestItRefusesOffWindows`), so `timelapse setup` remains the only wizard
there and keeps every question it has. `fill_template()` is shared regardless,
because a URL is not a user interface.

#### Test shows the frame, and picks the stream (0.2.0)

Until this, Test answered with a boolean. `test_camera()` measured the frame,
timed the round trip and printed `OK  412 KB, 2560x1440, 0.31s`, and **every
word of that went nowhere**, because under `pythonw.exe` there is no stdout at
all. The window could say "Test successful" and no more, which cannot tell an
operator that their 4K camera has just answered with a 640x360 thumbnail. The
same split that hid the camera's own complaint hid the picture: a fetched JPEG
was discarded three lines after it arrived.

So `grab_snapshot()` and `grab_snapshot_rtsp()` in `timelapse_setup.py` are now
where a frame is fetched, and they return `(data, detail)`. `detail` carries
everything that used to be printed: the status, the seconds, the byte count,
the dimensions, the camera's own complaint out of a 200-with-an-error body, and
the note lines that go with each. `test_camera()` is the printing wrapper over
it and prints exactly what it always did; the window reads the same finding.
Two front ends, one fetch, and **no way for them to hold different opinions
about one request**, which is `try_rsync_args()`'s rule about living beside the
code it describes.

**The window no longer calls `test_camera()` anywhere**, and that closed a real
defect rather than tidying an interface. `test_camera()` answers a 401 by
asking `ask_yes("Retry with auth 'other'?", True)`. The GUI never calls
`init_tty()`, so `_TTY` is `None`, so `ask()` returns the default: **yes, to a
question nobody was shown**. It then retried against a throwaway dict, so a
camera could report "Test successful" under a scheme that `save()` would not
store. Reachable through Custom URL, where the Authentication picker is shown,
and it would have produced a green tick over a camera that 401s for ever, held
off 31 minutes at a time by the item 8 back-off ladder. A test pins it: no
`setup.test_camera`, `setup.ask_yes` or `setup.ask` call anywhere in the module,
found through the AST rather than by scanning text.

**`jpeg_size()` reads the frame's size out of its own header**, nine bytes into
an SOF segment, rather than shelling out to ffprobe. Not premature: picking a
stream measures three images per test, and three subprocesses to read four
bytes is a poor trade. `probe_sample()` delegates to it and keeps ffprobe as the
fallback for anything it cannot parse, so there is still one answer to "how big
is this frame".

**It has to resynchronise, and the first version did not.** Reported from a real
run: every Dahua reported no dimensions while its picture drew perfectly.
Measured 2026-08-18 against three of them (192.168.2.202, .203 and .205) with
`temp/jpeg_walk.py`, and all three are identical: a `COM` segment declaring
4094 bytes, then **608 zero bytes belonging to no segment at all**, then a
second `COM`, then the frame header. Stepping by the declared length lands in
the padding, and the first version gave up there on the principle that a wrong
answer is worse than none. libjpeg and ffmpeg hunt for the next marker and say
nothing about it, which is exactly why the thumbnail was fine and only the
number was missing. The hunt is now done here too, on a budget
(`RESYNC_BUDGET`, 64 KB, against a measured 608), and **the frame header is
validated before it is believed** (sample precision 8, 12 or 16, both
dimensions non-zero), because after a resync the alternative to checking is a
plausible number read out of the wrong place.

Two things about this are worth carrying. **The fallback hid it**: the console
wizard prints dimensions for a Dahua and always has, because `probe_sample()`
drops through to ffprobe, so the defect existed only where there was nothing to
fall back on. And **the consequence was larger than the symptom**: with every
size `None`, `pick_stream()` skips every candidate and returns `None`, so the
stream picker was *inert on the one make whose selector prompted the feature*.
A missing number on a label was the visible half of a feature that was not
running. `temp/revert_check_jpeg.py` holds the old parser beside the new one and
shows exactly three of six cases moving.

**Streams are picked by measuring, not by believing.** `STREAM_KNOBS` names the
three presets whose URL selects a stream (ONVIF `Profile_N`, Dahua `subtype=N`,
and the last digit of a Hikvision ISAPI channel id, which is the stream rather
than the camera), `stream_candidates()` returns the siblings, and the largest
frame by **pixel count** wins. Deliberately probed even where the default is
documented as the main stream: that is a claim about firmware, and the rule
here is that an advertisement is a claim and a fetch is a measurement, which
`test_onvif_profiles()` learned on a Tapo advertising a 640x360 JPEG profile
that renders as garbage. A tie stays where it is, or a camera whose profiles
are all one size would be rewritten to a different spelling of itself on every
press.

**The configured URL is fetched first, and alone if it fails.** Every candidate
is another sign-in attempt, and the operator's own account of AgentDVR locking
three cameras for half an hour is what that rule is written against: a camera
that has just refused one credential must not be handed two more. At most three
requests, and only one when anything is wrong.

The chosen stream is stored as a **token** (`"subtype=2"`), not as the finished
URL, and only when applying it actually moved the URL. That last condition
settles three cases at once: the make's own default, a token naming a knob this
template does not have, and a make with no selector at all. Keyed on absence
like `interval_seconds` and `framerate`, so changing the address or the password
still rebuilds from the preset. `identify_camera()` winds the selector back to
the template's own before matching and reports it separately, **without which
picking a bigger profile would cost the camera its make** and it would read back
as Custom URL: the defect that function exists to prevent, arriving by a third
route.

**tkinter cannot display a JPEG.** `PhotoImage` reads PNG, GIF and PPM, and
Pillow is not a dependency this project will take, so `render_png()` pipes the
frame through ffmpeg and gets a scaled PNG back on stdout. It never touches
disk: a camera's view of somebody's house has no business becoming a file in a
temp directory this project does not control. ffmpeg is configured on the page
*before* the cameras, so it is always available by then, and a machine without
it loses the picture and nothing else. The `PhotoImage` is held on `state`
rather than only on the widget, because Tk keeps no reference of its own and a
collected image leaves an empty label that looks exactly like a failed fetch.

Clicking the thumbnail opens the frame **scaled to fit 85% of the screen**
rather than 1:1 in a scrolling canvas: at that point the question is "is that
the right view", which is about the whole picture. Four ways out, because an
image filling the screen with no obvious exit is alarming: the Close button, the
window's close box, a click anywhere on the image, and Escape. The button takes
focus so the Escape binding has somewhere to land.

**`WINDOW` grew from 820x620 to 820x660 because of this, and the number was
measured rather than eyeballed** (`temp/pane_height.py`): with a frame on it the
detail pane wanted 527 of the 533 pixels it had, and six pixels is not a margin.
What clips first is the Test and Save buttons, and it would clip on somebody
else's font scaling rather than here. 660 still fits a 1366x768 screen with the
title bar and taskbar.

**Field labels are a fixed 22 characters wide** (`LABELS`), which is what
"Seconds between frames" needs. A `ttk.Label` with a width in characters cuts
what does not fit, silently and only on screen, so this has been reported twice
from real runs: "Discord webhook UR..." and "Seconds between fram". A test now
reads the label literals out of the source and measures them, since nothing
else here can see the difference.

**The destination page offers the mapped drives itself**, next to Browse,
because Windows' own folder browser is shown by *this* process and this process
is elevated. It therefore lists the local disks and no network drives at all,
and the operator is invited to browse for the share they use every day while it
is not in the list: the drive-letter trap in its third disguise, after the
config that stored `U:\TL` verbatim and the check that could not see the
mapping either. `mapped_drives()` reads `HKCU\Network`, which survives
elevation because the split token is still the same user, and the picker puts
the **UNC** in the box before continuing into the ordinary folder dialog
rooted there. A letter is never stored: it belongs to one logon session and the
nightly encode has its own.

**Nothing it starts may put a window on screen.** Testing ffmpeg, and then
pressing Next, flashed up a run of command prompt windows on a real install,
which reads as a development leftover rather than as a program working. The
cause is not ffmpeg and not the wizard: `pythonw.exe` has no console, so
Windows gives a *new* one to every console child, and a new console is a
window. `no_console()` in the platform module returns `CREATE_NO_WINDOW` on
Windows and nothing at all on Linux, where the keyword is rejected outright.

It is applied to **captured children and only captured children**, and a test
holds every call site to that. A child that inherits the console in order to
print to it would have that console taken away and its output would go
nowhere, which is why the `timelapse` dispatcher and the editor `timelapse
config` opens are exempt, along with three modules: the capture daemon, which
imports no sibling at module scope and has no desktop under the SCM anyway,
and `timelapse_update.py` and `timelapse_web.py`, which are Linux-only in
practice.

Measured rather than reasoned about, under `pythonw` and with a control, in
`temp/noflash_check.py`: **six windows without the flag, none with it.** The
first version of that probe counted `ConsoleWindowClass` and reported zero for
both arms, because Windows 11 hosts consoles in Windows Terminal
(`CASCADIA_HOSTING_WINDOW_CLASS`, plus a `PseudoConsoleWindow`). A check that
cannot fail is not a check; it counts every window that appears now, whoever
drew it.

**The notification page fits, and had to be made to.** Three services, each
with a switch, an explanation, up to three fields, a Test button and a verdict,
ran off the bottom of the window with no way to scroll, which reads as there
being no way forward. The switch and the explanation share a line, and the
verdict sits beside the button rather than above it: that row was blank until
the button was pressed, so it read as three unexplained gaps. Anything added
here has to be measured against the window rather than assumed to fit.

**Getting one change right needed `detect_encoders()` to stop printing.** It
used to write "could not run ffmpeg" to stdout itself and return `(None, [])`,
which collided with two other outcomes: an import failure, and ffmpeg running
perfectly while every encoder was refused. Three states behind one falsy answer,
told apart only by something a window cannot see. It returns
`(codec, failures, problem)` now. Fifth instance of that shape here, after
`try_rsync_args`, `service_state`, `sync_unit_readwritepaths` and the update
checker's cached failure.

**The launcher exists because of an ordering problem the GUI cannot solve.**
The wizard is Python and the person it is for may not have Python, so something
that is not Python has to be able to say so. `setup-gui.ps1` finds the
interpreter (skipping the WindowsApps stub, which reports a version and is not
an interpreter), elevates through UAC, and starts the wizard with `pythonw.exe`
so no console flashes behind it. `timelapse-setup.cmd` is two lines so that
double-clicking works, and passes `-ExecutionPolicy Bypass` for that invocation
only: without it the default policy refuses a downloaded script and the window
opens and closes with nothing said, which reads as a broken download. The
launcher registers nothing and installs nothing, pinned by a test, for the same
reason `install.ps1` contains no `sc.exe`.

`install.ps1` places both, and creates an all-users Start menu shortcut, since
a wizard nobody can find is a wizard nobody uses. Failing to create it is a
warning and never fatal: a shortcut is a convenience, and a locked-down profile
refusing one must not cost an install.

### 4.6e The .exe installer (0.2.0)

`installer/timelapse-maker.iss` compiles with Inno Setup 6 into
`timelapse-maker-setup-<version>.exe`, which is what the release page offers.
It completes item 11c.6b: a Windows user downloads one file, double-clicks it,
and ends with a configured install.

**Three layers, and the boundary between them is the whole design.** The .iss
lays the release tree down and calls `installer\prepare.ps1`, which makes sure
Python and ffmpeg exist and calls `install.ps1`, which is the only program in
this project that knows how to install anything. Nothing in the first two
layers registers a service, writes a task, sets an ACL or writes a config. That
is the rule `tools/` was deleted over, met on its third and fourth files, and
it is asserted rather than promised: `tests/test_installer.py` scans both for
`sc.exe`, `schtasks`, `New-Service`, `icacls` and `--install-units`, and
requires `install.ps1` on both the install and the uninstall path.

**Prerequisites are a different question from installation, and that is why
`prepare.ps1` exists rather than `install.ps1` growing a downloader.**
`install.ps1` refuses to install Python or ffmpeg, and both refusals are right
for it: somebody running it from a checkout has a terminal open and can go and
fetch an interpreter. Neither is right for a .exe from a release page, where
"install Python first and run me again" is the entire experience of the thing.
So the split is prerequisites there, installation here, and `install.ps1` is
unchanged apart from a paragraph saying so.

**Neither prerequisite is ever replaced.** Each download needs two conditions,
the operator's checkbox *and* the tool being absent, which is what makes a
ticked box safe to leave ticked. An ffmpeg already on the machine is very often
a deliberate choice made for NVENC or for a codec, and overriding it is what
item 11c.6a refused; nothing here reopens that. The narrow case this exists for
is a clean Windows box, where the alternative is an installer that finishes and
leaves no working product.

**The ffmpeg question is asked of the product, not answered again.**
`prepare.ps1` runs `timelapse_platform.py --find-tool ffmpeg`, so the question
"will the wizard find one" is answered by the function the wizard uses. A
PowerShell reimplementation would be a second opinion that disagrees on the
machines nobody tests. A downloaded build is unpacked to
`%ProgramFiles%\timelapse-maker\ffmpeg\bin`, which `ffmpeg_roots()` now checks
first: it is the one root this project ever writes to, and if it exists then
something has already run the binary once and watched it work. It stays behind
PATH, because `find_tool()` checks PATH before any root and an ffmpeg the
operator put there is a decision.

**Everything downloaded is pinned by version, size and SHA-256** in
`installer/prerequisites.json`, and nothing runs before it matches. The size is
not redundant: a 404 page, a captive portal and a proxy error all arrive as a
successful HTTP response, a trap this project already met in `timelapse
update`, and it was met again while building this, when
`jrsoftware.org/download.php/is.exe` returned ten kilobytes of HTML with a 200.
Reporting that as a hash mismatch reads as a corrupt download; reporting it as
ten kilobytes where thirty megabytes were expected reads as the network.

The pins were verified against the real files rather than against the vendors'
claims, which mattered: the `binaries` field naming the folder *inside* the zip
cannot be read off a checksum sidecar, and neither can whether the build still
carries NVENC. Measured 2026-08-18: the archive holds one top-level folder as
pinned, and `ffmpeg.exe` lists `av1_nvenc`, `hevc_nvenc` and `h264_nvenc`,
which are exactly the three `detect_encoders()` probes for. `temp/check_pins.py`
is that check and should be run at every bump; the release workflow HEADs both
URLs and compares `Content-Length`, which is the cheap half and catches a dead
pin before a release rather than after one.

**Two directories, on purpose.** `{app}` is `%ProgramFiles%\timelapse-maker`
and holds the release tree, the docs, the uninstaller and any downloaded
ffmpeg; the scripts that actually run go to `%ProgramFiles%\timelapse`, which
`install.ps1` owns and has since the first Windows release. They are separate
because the uninstaller lives in the first and deletes the second, and a
program that recursively deletes the directory it is running from mid-uninstall
is a different sort of bug report.

**It runs hidden and logs, which is a decision about what the operator sees.**
`install.ps1` prints things worth reading: the per-user Python warning, the ACL
result, the PATH note. Behind a progress bar all of that goes nowhere unless it
is captured, which is the same trap as a service writing to a stderr the SCM
discards, and it gets the same answer: both streams into
`%ProgramData%\timelapse\install.log`, offered to the operator if a stage
fails. The finish page is rewritten on failure, because Inno's own text says
"Setup has finished installing" whatever happened, and the last thing somebody
reads about a failed install should not be a success.

**No `[Icons]` section, and its absence is a decision.** `install.ps1` already
creates the Start menu entry on both front doors, so a line here would write
the same shortcut twice and hand its removal to a second uninstaller.

**It is not code-signed.** SmartScreen therefore warns, and the answer for now
is to say so in the README and in `install.md` and to publish a `.sha256`
beside every build. A certificate is a real cost with a reputation-building
period attached, and it is not worth it for software still marked
EXPERIMENTAL; revisit it when the audience is larger than the author's own
recorder.

**The build is CI's, not a workstation's.** `.github/workflows/installer.yml`
installs Inno Setup (it was preinstalled on the Server 2022 image and is not on
Server 2025, which `windows-latest` now is), compiles with `/DAppVersion` taken
from the tag, and attaches the exe and its checksum to the release, creating a
draft if the tag has none yet. The tag is checked against `install.ps1`'s
`$VERSION` first: a tag cannot be moved once anybody has fetched it, so a
release built from a tree still carrying the previous version would report the
wrong one from `timelapse version` for ever.

**Starting is not restarting, and Windows had only one of them.**
`restart_units()` touches a service only when it is already running, which is
right because its job is an upgrade: restarting one the operator had stopped
would overrule a decision they made. Nothing did the other half.
`install.sh` ends with `systemctl enable --now`, so on Linux installing gets
you a running capture; on Windows the service was registered delayed
auto-start and left stopped, so a fresh install collected nothing until the
next reboot. What made that a defect rather than a documentation gap is that
the graphical wizard finished by saying "Capture starts on its own and keeps
running" while it sat there stopped, and the only text that said otherwise was
`install.ps1`'s closing line, which the .exe runs hidden. `start_capture()` is
the missing half, called by the wizard after `write_config()` (the same
ordering rule as the restart), and `next_steps()` takes the outcome as an
argument so the dialog cannot assert something nobody checked. The two
scheduled tasks never had this gap: Task Scheduler fires them on its own.

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

`timelapse-watch.service` is templated separately too, and **must be**: the
loop that rewrites `ExecStart` matches any line mentioning `timelapse_encode.py`,
which would silently strip the `--watch` flag and turn a five-minute timer into
a five-minute encode run. Its `ReadWritePaths` is the state directory alone, on
the same reasoning as the web UI's: it reads the capture heartbeat and writes
one small file, and has no business near the frames or the videos. Verified
under systemd, where a process with that unit's writable set gets
`Read-only file system` on the frames, the videos and `/etc/timelapse`.

**An upgrade asks nothing, and changes nothing about what runs.**
`snapshot_services()` records, before any file is written, which of the four
managed units are present, enabled and active; `restore_services()` puts that
back afterwards and prints one line each. The four questions this replaced
(reconfigure, run the pre-flight, enable capture, enable the web UI) were all
answerable from the system itself, and asking them made `timelapse update`
interactive at the moment an operator most wants it to be boring.

Three parts of that are load-bearing. The snapshot must run **before**
`install_units()` and `sync_units()`, which rewrite what it reads. The restart
of a live daemon is no longer offered as a choice, because declining left the
old build serving while every version number said otherwise. And a unit that
was not present before is adopted **only if it is a timer**: a companion timer
added in a later release would otherwise sit installed and inert on every
existing deployment, while a new *service* switched on by an upgrade would
override a deliberate decision, which is exactly what keeps the opt-in web UI
opt-in.

A unit that was active before and is not active after is reported as an error
with its `journalctl` line, since that is the one upgrade outcome worth
interrupting anybody for, and it used to be reported as success.

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
  every night. Since 0.1.6 that arithmetic is `coverage_pct()`, shared by the
  Discord table and the run record. It was inline in the table, and the file
  would have been the copy that drifted.
- **The disk projection sums, it does not multiply.** One camera at 60s and
  five at 5s is not six times any single figure.

**A change takes effect at the next midnight, and nowhere else.** The rule is
one sentence because a day directory enforces it: `.cadence.json` records what
the day began at, and both the daemon and the encoder read it in preference to
the config.

- `begin_day()` is the only place either camera class adopts a cadence. A day
  with a marker adopts the marker; a day without one, which means a day that
  has not begun, reads the config. So a daemon restarted at 14:00 finishes the
  day the way it started it, and there is no window in which an edit lands
  early.
- **The marker is written twice over, deliberately.** Both paths write it as
  they create the day directory (`_dest_path()` for HTTP, `_prepare_day()` for
  RTSP), which is the moment the answer is not in doubt, and
  `record_cadences()` runs once a minute from `main()` as a backstop for any
  directory that exists and lacks one. Before 0.1.7 that backstop was the only
  writer on the RTSP path, on the belief that ffmpeg created those directories;
  it did not, and the whole path was broken. It never *creates* a directory: a
  camera offline all day would otherwise leave an empty one behind every night,
  which the encoder finds, reports as a `SKIP`, and never cleans up.
- **A cadence that changes re-anchors the tick grid** (0.2.1). `_roll_day()`
  owns the whole rule: the day is what decides whether the config is re-read,
  and a change that lands returns `next_slot(fire_t, interval)` rather than
  `fire_t + interval`. Carrying the old phase forward is not a rounding error,
  it is permanent: a day moved from 7s to 60s would name every frame at seven
  seconds past the minute for the rest of that day, and **filenames are the
  sort key**. `next_slot` is strictly after the time given, or a tick that
  happened to sit on both grids would fire twice in the same second and the
  second frame would collide with the first. It is a method rather than four
  lines in the loop because the tick loop is the one part of this daemon no
  test executes, and the rule is worth executing.
- **RTSP gets `-t seconds_to_midnight()`.** ffmpeg carries `fps=1/interval` on
  its command line, so a new cadence means a new process, and the boundary is
  the only moment that may happen. Its `rc=0` is the planned handover and is
  logged at info; logging it as a warning nightly would train people to ignore
  the line that matters. Since 0.1.7 the day is also baked into the output
  path, so **one ffmpeg process writes into exactly one day directory**. If it
  overruns the boundary it writes into the day it started in rather than
  creating the next one, which is the safe direction: `-t` bounds output
  duration, so the last frame's timestamp is before midnight and at worst one
  frame lands in the day it belongs to a second late.
- **The encoder prefers the marker too.** A cadence edit at 14:00 means
  tonight's run is encoding a day that ran on the *old* settings. Reading the
  config would measure yesterday against a cadence yesterday never ran at: a
  complete day at 5s would report 1200% coverage after a change to 60s.

**Checks measure; they do not pattern-match the config.** The pre-flight used
to warn that a CIFS destination with `-a` in `rsync_args` would exit 23 every
night. `-a` does imply `--owner --group` and a share often cannot set them,
but whether rsync actually fails depends on the server and the mount options.
On a real share it does not, so a working configuration was reported as broken
every time the operator ran the check. `try_rsync_args()` now copies one file
with the exact flags the encoder will use, as the account it runs as, so the
check cannot cry wolf and can name the flags that would work when it genuinely
fails. It lives in `timelapse_encode.py` because that is the program that runs
rsync; the wizard and the pre-flight both import it, so they cannot give
different answers.

**Measuring introduces its own way to be wrong: the measurement can fail.**
The probe runs as the service account, which means `runuser`, which needs
root. But `install.sh` already runs the pre-flight through `as_service_user`,
precisely so that permission problems surface at install time rather than at
00:05, so the probe was calling `runuser` a *second* time from inside an
unprivileged process. The nested refusal came back as `FAIL ... exit 1:
runuser: may not be used by non-root users`, followed by "check the share
permissions". The share was fine.

`try_rsync_args()` returns `(None, reason)` for "could not be tested", which
is not `(False, detail)` for "tested, and it fails", and `probe_as()` settles
how to reach the account before running anything: already it (run directly),
root with `runuser` (wrap it), or neither (decline, and name the command that
would work). The first branch is both the reported case and the best one,
since running *as* that account is what makes the answer authoritative; the
third also covers a host with no `runuser`, where the probe used to run
unwrapped and report a result for the wrong account. **A checker's own
limitations are never a finding about the thing being checked.**

**A falsy return must not mean four different things.**
`sync_unit_readwritepaths()` returned the count of units it rewrote, and the
caller treated anything falsy as failure. Running it as root against units
that were already correct, which is the ordinary case because the installer
writes them on every upgrade, therefore told the operator to edit the unit by
hand as root while they were root. It returns `(status, detail)` now, with
`changed`, `current`, `absent`, `denied`, `empty` and `failed` telling apart
outcomes that were previously one value. The same shape as the update
checker writing `checked` on both success and failure, and as `timelapse
cameras` reporting an unreadable config as an absent one: **distinct outcomes
collapsed into one falsy value.**

`timelapse test` has a **Cadence** section reporting what each camera will
produce, because the consequence of these two numbers, how long tonight's
video is, cannot be read off the config. It fails a camera whose frames/day
falls below `encode.min_frames`: the encoder `SKIP`s that, so the camera would
produce nothing at all, every night, without ever failing.

### 4.8a Per-camera motion smoothing

`smooth_frames` on a camera appends `tmix=frames=N` to that camera's filter
chain. It is optional, per camera, and **absent means off**, which is the exact
opposite of §4.8's rule and is the point: interval and frame rate fall back to
a global so an untouched camera still moves when the global does, while
smoothing has no global to fall back to. Averaging suits a roof full of trees
and spoils a doorway, so nothing may turn it on but the camera itself. Every
config written before 0.1.9 is a config where no camera says anything, so an
upgrade changes nothing.

**What it is for.** At a 5s interval each frame is 5 real seconds from its
neighbour, so every transition is a jump. That is temporal aliasing, not a
frame-rate problem: the output is already 60fps with one captured frame per
output frame, and raising the rate cannot help because there is no more
information to show. Three kinds of motion behave differently:

- **Wind in foliage** decorrelates between frames, so leaves boil. This is the
  constant, everyday judder, and averaging genuinely fixes it.
- **Shadows, cloud and light** drift slowly and were never the problem.
- **Anything crossing the frame** is sampled once and cannot be fixed by
  anything. Measured on the deployment's own footage: a car appears in exactly
  one frame, and a 45-second bin collection yielded 3 frames of the lorry and
  1 of the worker. Averaging turns that hard one-frame flash into a soft ghost
  across N frames, which is an improvement, but it is not fluidity and no
  setting will make it so.

**Motion interpolation was tested and rejected**, on quality before cost.
`minterpolate` needs frame-to-frame correspondence, which is precisely what a
5s interval destroys, so it warps the scene where it guesses wrong. Measured at
1440p: `mi_mode=mci` ran 676s for 600 frames (~5.4 hours per camera-day), and
motion-compensated synthetic blur ran 1,359s (~10.9 hours) while scoring
*worse* on frame-to-frame difference than plain `tmix` at 3. The reason is
structural: it simulates a shutter *within* each 5s gap, so consecutive frames
are still 5 seconds apart in content. See `decided-against.md`.

Four things that are not obvious:

- **It is encode-time only, and that inverts §4.8's timing rule.** Capture,
  frame counts, `Cov%` and video length are all untouched, and so is
  `.cadence.json`, which records what a day was *captured* at. A cadence edit
  cannot land mid-day precisely because that marker outranks the config;
  smoothing has no marker, is not a property of the capture, and is read from
  the config at the moment of encoding. So enabling it at 14:00 smooths that
  same afternoon's video, in the 00:05 run, together with any backlog days
  that run picks up. Days already carrying `.encoded.json` are not redone;
  `--force` is the lever. Three tests pin this, including one that puts a
  `.cadence.json` predating the setting in the day directory: if smoothing
  ever moved into that marker, turning it on would silently skip every day
  already under way, which is every day anyone would think to turn it on for.
- **`tmix` is last in the chain**, after `scale`'s full-to-limited conversion
  and `format`. Ahead of them it would average full-range JPEG and hand the
  scaler something it had not been told about.
- **The value is clamped, not trusted.** `SMOOTH_MIN` 3, `SMOOTH_MAX` 30,
  offered default 15. This is read off a file operators edit by hand, and the
  cost of believing a typed 500 is buffering 500 frames of 4K per camera.
  Anything under 3 reads as off, which is what a leftover 0 means.
- **The bounds come from measurement, not taste.** At 3 frames a bin lorry
  stays legible; at 7 flat it is an unreadable wash. Frame-to-frame difference
  keeps improving as the window widens, which is exactly why it is the wrong
  thing to optimise: that metric is maximised by erasing events. 15 suits a
  scene that is mostly foliage with no small detail to lose, which is why it is
  the offered default rather than the floor.

The cost is negligible: measured at under 10% of decode time, against an
encode that dominates either way, with no change to output size. The one
visible loss is that a camera's burnt-in clock blurs its fastest-changing
digits, since they differ in every frame being averaged.

`timelapse test`'s Cadence section reports smoothing but never warns about it:
there is no wrong value, only a preference per scene.

### 4.9 Credential redaction

Reported 2026-08-11, from a real deployment's web UI, and the most serious
defect this project has shipped:

```
WARNING [Doorbell] grab failed (#1): 502 Server Error: Bad Gateway for url:
http://192.168.2.208/cgi-bin/api.cgi?cmd=Snap&...&user=admin&password=<real>
```

The call site is `log.warning("grab failed (#%d): %s", n, err)`. **It never
mentions a URL.** `requests` puts the URL in the exception's text, so the
password went to journald, to `capture.log`, and from there onto a web page.

That is the whole design argument. No rule about how to write log calls could
have prevented it, because the credential arrives inside an object formatted
by a library, so the guarantee has to sit where every record passes:

- **`RedactingFormatter`, not a filter and not a call per site.** A formatter
  runs last, which also covers tracebacks and `log.exception()`; a filter that
  rewrote `record.msg` would let `exc_info` through untouched.
- **`route_exceptions_through_logging()`.** Found while verifying the above on
  real systemd: a camera thread that dies prints its traceback through
  `threading.excepthook`, straight to stderr, meeting no formatter at all.
  `sys.excepthook` is the same hole for the main thread. Both now log.
- **`run_command()` redacts in `timelapse_web.py`**, at the source rather than
  in each renderer. Fixing the daemon does nothing for the entries already in
  the journal, and those are what the page displays.
- **The pre-flight prints rather than logs**, so no formatter covers it and
  each site has to call `redact()` itself. `test_rtsp()` has, since it was
  written, with a comment about scroll-back; `test_http()` beside it did not,
  and until 0.2.1 printed the whole `requests` exception, which for a Reolink
  URL is the password. Measured 2026-09-13 (`temp/c1_leak_probe.py`): a
  refused connection prints `Max retries exceeded with url:
  ...&user=admin&password=<real>`, and a read timeout prints no URL at all,
  which is how it survived casual testing. The fourth place this project has
  put a password in front of somebody, and the reachable one: HTTP is the
  deployment's only method, `install.sh` runs the checker on every install and
  every upgrade, and `timelapse test > report.txt` writes it to a file.

Four shapes are masked, being the four ways a secret reaches text here: query
string (`password=`, and seven other spellings), URL userinfo
(`rtsp://user:pass@host`, which is how ffmpeg quotes back the URL it was
given), and the Discord webhook token, which is not a locator but the
authority to post. `\b` on the key stops `bypass=` being read as `pass=`; over-
redaction is otherwise the safe direction, since masking a non-secret costs a
debugging session and the reverse costs a password.

The rule lives in `timelapse_encode.py` and is **duplicated verbatim** in
`timelapse_capture.py`, for the same reason `load_config()` is: no daemon
should fail to start because a sibling changed. A test asserts the two are
character-identical, because a security rule that exists twice will drift, and
the copy that drifts is the one nobody is looking at. `redact_url()` in the
wizard is now a thin wrapper over the same function; its own local rule knew
only about `password=`, so every camera the wizard's RTSP path added was listed
with its password in full.

**A fix here does not clean up after itself.** The journal and `capture.log`
keep what they already have, so the release notes tell operators to rotate the
camera password and say how to purge both.

#### The config itself: `timelapse config --redacted`

The other way a password leaves this host is that somebody sends it
deliberately, attached to a question. `redact_config()` walks a parsed config
and returns a masked copy, which is what that command prints.

**Both passes are load-bearing, and each alone leaks.** `redact()` over the
serialised text misses `"password": "hunter2"`, which has no `=` in it;
matching key names misses the Reolink `url`, which carries the credential in a
query string. So the walk masks by key name *and* runs the text rule over
every string value. Two revert-checks pin this: field-matching alone fails
four tests, text alone fails three.

What survives is as deliberate as what does not. Hostnames, usernames, paths,
the transfer destination and the webhook's numeric id are kept, because a dump
that masked those would be safe and useless. The operator is told so
explicitly, and the notice travels *inside* the JSON under `_redacted`, a key
the schema already reserves for documentation: the moment this matters is the
moment the text is pasted into an issue, and prose printed beside the blob is
precisely the part that does not travel with it. JSON on stdout, prose on
stderr, so a redirect still yields a file that parses.

#### `timelapse config` and what an editor leaves behind

`/etc/timelapse` is 0750 root:timelapse, so a stray file *inside* it is
already shielded from other users. Measured rather than assumed, on vim: the
backup `config.json~` inherits the original's 0640 root:timelapse, in the
directory or in a `backupdir` elsewhere, so it is the same exposure as the
config rather than a new one. `umask 0077` is set before the editor anyway,
for editors that create from the umask instead.

The real defect was elsewhere. An editor that saves by rename leaves a brand
new file carrying root's umask, so **the config loses its group and the
daemons can no longer read it**, which surfaces at the next restart and looks
nothing like an editing mistake. The wrapper therefore no longer `exec`s the
editor: it restores 0640 root:timelapse afterwards and exits with the editor's
status.

---

<sub>[&uarr; Contents](#contents)</sub>

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
| `state_dir` | Runtime state (§3a), a few KB. Defaults to `/var/lib/timelapse/state` and is not asked by the wizard. Deliberately not derived from the base directory the wizard asks about: that exists because frames are enormous and may want their own disk, and this does not. Absent from every config written before 0.1.6, so it is read with the default; changing it means `install.sh` must run again, because the directory has to exist before the units start. |
| `ffmpeg`, `ffprobe` | Absolute paths. Point at a BtbN static build if the distro build lacks NVENC. |

### `capture`
| Key | Default | Notes |
|---|---|---|
| `interval_seconds` | 5 | 17,280 frames/day → 4:48 at 60fps. Changing this changes `Cov%` maths and video length. |
| `timeout_seconds` | 4 | Must stay below `interval_seconds`, or a slow camera stalls its own next fetch. |
| `min_bytes` | 4096 | Shared with the encoder's validity check. |
| `min_free_gb` | 60 | `0` disables DiskGuard. |
| `log_every_n_failures` | 60 | At 5s intervals, 60 = one log line per 5 minutes of downtime. Clamped to at least 1 where it is read (0.2.1): the throttle is a modulo outside the try, so `0` used to end the camera thread on its second consecutive failure, and nothing restarts a camera thread. |
| `retry_within_tick` | true | One retry per tick when the budget allows. Set false for a camera that degrades under a second request. |
| `notify_auth_failures` | true | Send one notification when a camera is confirmed to be refusing our credentials, and one when it recovers. Needs a configured sink; the back-off itself is unconditional. |

### `encode`
| Key | Default | Notes |
|---|---|---|
| `framerate` | 60 | Applied to both input and output, and asked by the wizard. A day's frames at a 5s interval are 4:48 of video at 60 and 9:36 at 30. |
| `container` | `mkv` | Extension only; ffmpeg infers the muxer. |
| `gop` | 120 | 2s at 60fps. The wizard derives it as `framerate * 2` so that stays true at any rate; set it here to override. Lower = better scrubbing, larger files. |
| `av1_preset` / `av1_cq` | `p6` / 26 | p1 fastest … p7 slowest. Lower cq = higher quality. |
| `hevc_cq`, `x264_crf` | 24, 20 | Fallback encoders only. |
| `min_frames` | 100 | Below this, `SKIP` rather than produce a 2-second video. |
| `delete_frames_on_success` | true | Off keeps the day directory. Safe since 0.1.6: `.encoded.json` is what stops the day being encoded again. There is no age-based sweeper and there is not going to be one (decided-against.md), so kept frames are kept forever, one directory per camera per day. |
| `max_backlog_days` | 7 | Bounds a catch-up run after long downtime. A count of the newest distinct dates *still pending*, not an age cutoff: downtime produces fewer date directories, never more, so it cannot strand a day. |

### `transfer`
| Key | Notes |
|---|---|
| `destination` | A local directory or an rsync remote spec; one code path serves both. On Windows, a local folder or a UNC path: no remote specs, and a mapped drive letter is resolved to its UNC target at wizard time and stored that way. |
| `rsync_args` | Linux only. Defaults include `--remove-source-files`; if you drop that, set `delete_local_after_transfer` accordingly or files accumulate. On a CIFS mount `-a` may exit 23 because owner/group cannot be set; the wizard measures which flags your share accepts and writes those. |
| `require_mountpoint` | Linux only. `false` (default), `true`, or an explicit mount path. Refuses to transfer when the destination is not on a mounted filesystem. Only meaningful for a local destination; ignored for a remote spec. |
| `username`, `password` | Windows only, both optional and absent by default. Credentials for a UNC destination, used with `WNetAddConnection2W` and only after the current account has been refused. Needed because the nightly encode runs as LocalSystem, which presents this machine's name on the network rather than yours; see §4.6c. `redact_config()` masks the password by key name. |
| `delete_local_after_transfer` | `true` by default. On Windows this is what makes the operation a move rather than a copy, since there is no `--remove-source-files` to imply it. |

### `notify`
A list, not a block. Each entry has a `type` and an `enabled`, plus its own
keys: `webhook_url` and `username` for `discord`; `server`, `topic`, optional
`token`, `priority` and `tags` for `ntfy`; `token` and `chat_id` for
`telegram`. An entry with no `type` is a Discord webhook, which is what the
legacy block becomes when the wizard moves it.

Absent from every config written before 0.1.6, and the fallback is the old
top-level `discord` block rather than "no notifications". When this list is
present it is authoritative and that block is ignored.

### `web`
Optional; absent from configs written before the feature existed, so every key
is read with `.get(key, default)`.

| Key | Notes |
|---|---|
| `enabled` | `false` by default. The program exits 0 when false, so the unit may be enabled without the server running. |
| `bind` | `127.0.0.1` by default. There is no TLS, and the optional login below is a door lock rather than a safe; any other value exposes the page to the LAN, and anything wider belongs behind a reverse proxy. A non-loopback bind logs a warning at startup, worded according to whether a login is configured. IPv4 or IPv6; `::` is dual-stack and accepts both. |
| `port` | `8787` by default. |
| `auth` | Optional single login: `{username, password_hash}`. Absent, or either field blank, means no login and the pages behave exactly as they did before it existed. The hash is PBKDF2-SHA256 and self-describing (`pbkdf2_sha256$iters$salt$key`), so the iteration count can rise later without locking anyone out. A username with an unparseable hash **refuses to start**, rather than serving the pages to everyone because the check could not be made. |
| `library_root` | Empty means "work it out": the transfer destination when transfer is enabled, otherwise `video_output`. Set it when the videos are readable here under a different path, typically a remote rsync destination that is *also* mounted locally. **Browsing requires a readable path**, so an SSH-only destination must be mounted; SFTP browsing is refused (§4.5). Not `/tmp` or `/var/tmp`: `PrivateTmp=true` hides those from the unit. |
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
| `smooth_frames` | Optional, 3-30. Averages that many neighbouring frames when encoding (§4.8a). Absent means **off**: there is no global to inherit. Encode-time only. |
| `quality` | RTSP only: ffmpeg `-q:v`, 2 = high. |
| `_note` | Ignored by code. Keys starting with `_` are documentation. |

---

<sub>[&uarr; Contents](#contents)</sub>

## 6. Failure handling matrix

| Failure | Behaviour |
|---|---|
| One camera unreachable | That thread logs (throttled) and keeps trying. Others unaffected. Nightly `Cov%` shows the gap. |
| Camera returns non-JPEG / truncated | Frame rejected at capture; nothing written. |
| Camera rejects our credentials | Classified `auth`. Two refusals, then fetches are withheld for 10 minutes, one attempt, then one every 31 minutes for ever, so this program never holds the camera's account locked. One notification, one all-clear. Other cameras unaffected. |
| A camera's password is rotated without reconfiguring here | Same path. Before 0.1.6 the daemon presented the old credential every tick and the account stayed locked until capture stopped. |
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

<sub>[&uarr; Contents](#contents)</sub>

## 7. Extension points

Ordered roughly by how much of the existing design they disturb.

**Add a camera**: one config entry. No code.

**New snapshot protocol**: subclass `threading.Thread` in
`timelapse_capture.py` following `HttpCamera`, honour `STOP` and `PAUSED`, write
via temp + `os.replace`, and dispatch on `method` in `main()`. Nothing in the
encoder changes.

**Per-camera settings**: interval and frame rate are done (see §4.8), as is
smoothing (§4.8a); resolution and quality would follow the same shape. The
camera dict is passed whole to the thread constructor, so it is
`cam.get(key) or <global>` and nothing structural. Decide first which of the
two shapes a new key has: one that falls back to a global (absent means
"follow it", and the wizard must *remove* the key when the answer equals the
global) or one that does not (absent means off, and there is no global to put
it in). Getting that backwards is how a setting ends up either impossible to
turn off or silently pinned on every camera anyone has opened.

**Another notification sink**: add a function taking
`(sink, title, description, level, fields)` and register it in `SINKS`. The
dispatch, the per-sink error isolation and the config plumbing are already
there. What is not automatic is the wizard: a sink type the wizard cannot
configure is one nobody finds, so `choose_notify()` needs a section too, and
`sink_identity()` needs to know what makes two of them different.

**Camera restart on hang**: **decided against at 0.1.6**, see
decided-against.md. Two independent control loops acting on one camera with no
arbitration will eventually fight, and this one could not tell a fault from a
scheduled reboot. If you fork it anyway, the natural home is a new module
invoked by the capture daemon's failure path, gated on consecutive-failure
count, with a cooldown; detection already exists (`consec_fail`), and note that
it would need an admin credential where the config today needs only one that
can fetch a JPEG.

**Frame retention beyond encode**: **decided against on scope at 0.1.6**, see
decided-against.md; this paragraph describes the extension point, not a plan.
Set `delete_frames_on_success: false` and add a separate age-based sweeper. Do
not put retention logic in `encode_day()`; keep "encode" and "delete"
separable. The marker makes the hard half free, since a kept day carries
`.encoded.json` and is not encoded again, so a sweeper decides only *when* to
delete and never *whether* the work was done. It must take the whole day
directory, markers included, rather than leaving either dotfile behind.

**Parallel encoding**: currently sequential and deliberately so. NVENC session
limits on consumer GeForce cards are low, and the real bottleneck is CPU JPEG
decode, which already uses all cores per job. Parallelism would contend, not
help.

---

<sub>[&uarr; Contents](#contents)</sub>

## 8. Known limitations

- **DST fall-back**: local time repeats an hour, so `HHMMSS` names collide.
  `HttpCamera._dest_path` appends `-1`, `-2` … and keeps everything, at the cost
  of that hour sorting slightly oddly. The RTSP path (ffmpeg `-strftime`) has no
  such guard and will overwrite. Video length varies on DST days (23h or 25h).
- **PTZ cameras** jump-cut between presets in the finished video.
- **Cameras are polled independently**, so frames across cameras are not
  synchronised to the same instant.
- **A frozen-but-reachable camera** produces a full frame count and a static
  video. No automatic detection, and that is now a decision rather than an
  omission (decided-against.md): nobody has observed the fault, the cheap
  detector cannot see a painted lens, and the one that could would have to
  guess. The tell is a suspiciously small output file.
- **Video length varies with capture coverage**: a camera down for 6 hours
  produces a shorter video, not a video with gaps.
- `Cov%` is computed against a nominal `86400/interval` and will read ~104% or
  ~96% on DST days.

---

<sub>[&uarr; Contents](#contents)</sub>

## 9. Testing notes

```bash
python3 -m unittest discover -s tests -t tests -p 'test_*.py'   # fast, no deps
python3 tests/smoke_test.py                                     # needs ffmpeg
```

**Unit tests** (`tests/test_*.py`, stdlib `unittest`, 2,216 cases, about a
minute; `test_web.py` builds real sparse files on disk) cover the pure logic: frame validation, concat-list escaping,
`find_pending` backlog selection, `human_*` formatting, the storage scan's
filtering and deduplication, `_base_device` partition stripping, `recommend`,
`writable_paths`, credential quoting, `_dest_path` including the DST
collision suffixes, and the web UI's library resolution and routing. Anything
needing a camera, a GPU or systemd is out of scope here by design.

**A test that inherits its privilege runs a different program as root, and one
of these mounted a filesystem** (0.2.1). `TestTransferDestinationKind` drove
the transfer menu with `"y\n1\n..."` believing option 1 was the local path. It
is "SMB/CIFS, set it up for me". Unprivileged that branch prints
`Mounting a share needs root privileges.` and returns, which is the only
reason this went unnoticed: the assertion underneath it
(`assertNotIn("SSH key", ...)`) passed against a refusal message, so the
regression that test exists for was unguarded as well.

As root it is a different program. `setup_cifs_share()` passes the privilege
check, `ensure_cifs_utils()` runs the first package manager it finds with no
prompt, and the remaining keystrokes, meant for a completely different
question, become a NAS address of `/tmp/nonexistent-dest/` and a share named
`n`: a 0600 credentials file in `/etc/timelapse`, a `mkdir /mnt/n`, and a
`mount -t cifs`. The Linux test bed this project uses is
`wsl -d Ubuntu -u root`, so that is not a hypothetical configuration. It is
the usual one, and **`/mnt/n` was sitting on this developer's WSL, empty and
root-owned, dated 2026-08-06**: the defect had already fired, months before
anybody looked for it.

Three changes, and the order of importance is the reverse of the order of
size. `drive()` patches `is_root` to False, so the class *declares*
unprivileged rather than inheriting whatever ran it, and the `skipTest` that
used to hide the root case is gone: a test that skips itself exactly when its
subject would do something is not a test. `ensure_cifs_utils` and
`probe_rsync_flags` are stubbed as **tripwires**, not conveniences: with
`is_root()` False neither is reachable, so either one firing means a keystroke
change has walked back into the branch. And the local-path test answers `2`
and asserts the `Destination path` prompt was reached, so a future menu
reordering fails here rather than quietly testing the wrong branch again.

`temp/h7_root_check.sh` is the proof, and it has both halves. It replays the
old keystrokes as root and shows the package manager call being reached (with
the installer itself stubbed, so nothing is installed on anybody's machine),
then runs the whole suite as root and confirms `/etc/timelapse`, `/mnt`, the
package database and the mount table are all exactly as they were. The revert
that removes the declaration is **caught only when the suite runs as root**,
which is the finding restated: unprivileged, everything agrees.

**The pre-flight's camera checks had no test until 0.2.1**, which is how
`test_http()` came to print the camera password on every failed fetch for the
life of the project. The daemon's redaction is tested thoroughly, but the
pre-flight *prints*, so nothing proven about `RedactingFormatter` says anything
about it, and the checker's own tests had been filed by subject into three
other files, which made the gap easy to miss. `tests/test_preflight.py` is the
home for them now. It patches `timelapse_test.requests`, which is the name the
checker resolves (it imports at module level, so standing a fake up in
`sys.modules` the way `test_setup.py` does for `grab_snapshot` would not reach
it), and it asserts both directions: the password is gone, and the host, the
endpoint, the username and the underlying reason are all still there. A
redacted line nobody can diagnose from would be the next defect.

**The suite runs on Windows as well as Linux, and CI runs it on both.** The
`windows-latest` leg was added with `timelapse_platform.py` rather than at the
end of the Windows port, because half of what that module answers is
unreachable from the Linux legs by construction, and this project has twice
been bitten by "a local branch is a branch CI has never seen". `test_platform.py`
is written so that neither leg trusts the other to have looked: `locations()`
takes the platform as an argument, so both branches are asserted on both
runners, and the service and permission functions read `IS_WINDOWS` at call
time so the tests can patch it. The Windows leg runs the unit suite only: the
encode pipeline is shared code the Linux legs exercise on every push, and
fetching an ffmpeg build would put a download into the one job that otherwise
has no network dependency at all.

**The encode pipeline was run natively on Windows on 2026-08-16**, for the
first time, and passed every assertion on the first attempt. That is worth
recording because it was the largest untested surface left after step 3: CI's
Windows leg carries no ffmpeg, every previous smoke test had been in WSL, and
the nightly task had been registered but never fired. It selected `hevc_nvenc`,
so Windows *hardware* encoding is now exercised too. What it settles, none of
which had been through this code on that platform before: the concat list with
native backslash paths (the 0.1.9 research measured ffmpeg accepting them, but
that was a probe, not `write_concat_list()`), the colour conversion and tagging,
the `.encoded.json` marker with `--force` and the re-run skip, `tmix` smoothing,
the `encode.json` run records, and the RTSP writer taking its argv from
`RtspCamera._cmd()`, including the day directory being created in Python and an
empty one being discarded. `av1_nvenc` is still unavailable on the development
GPU, so the AV1 path remains exercised only in the Linux deployment.

Separately confirmed, because the smoke test does not assert it and a scheduled
task's stdout goes nowhere: the encoder writes `encode-YYYYMMDD.log` through
`DailyFileHandler`. Without that, a failed nightly run on Windows would leave no
trace of any kind.

**What CI cannot reach on Windows is the service manager**, because registering
one needs elevation and whether a runner has any is not this project's to
assume. So the split is deliberate: everything *around* the SCM is pure and
unit-tested on both legs (the name mapping, `service_binpath` quoting, the task
XML, the structure layouts, `service_definitions`), and the lifecycle itself is
`temp/step3_check.py`, run once from an Administrator prompt. That script uses
the shipping code path throughout, stands up a local HTTP server as a fake
camera, and checks that the service starts, reports RUNNING only once it is
capturing, writes frames and a log as a different account, and stops cleanly
rather than being killed. Its unprivileged half runs anywhere and separates the
two failure modes: if the daemon captures in the foreground and the dispatcher
is refused with the documented 1063, then anything still broken is in the
hosting and nowhere else.

`temp/step3b_check.ps1` is the same idea for the installer: a real install with
`-NoWizard`, checks that the files, the wrapper, the PATH entry, the ACL and
the registrations all landed, a run of `timelapse version` **through the
wrapper** rather than through Python (a broken `.cmd` is invisible to every
other check and is the first thing anybody touches), then a real uninstall and
checks that it removed all of that and left the recordings alone. CI parses
both PowerShell files and asserts `install.ps1` is ASCII, which is the Windows
analogue of the `shell` job's `bash -n` and shellcheck. That parse step is
globbed over `*.ps1` and `installer/*.ps1` rather than listed, because the
first version of it named `temp/step3b_check.ps1`, which is **gitignored**: it
passed on the branch it was written on and would have failed on the first push
to `main`, on a file CI cannot see. Third time this project has met "a local
branch is a branch CI has never seen", and the first where the trap was in the
CI file itself.

**The .exe installer is checked in four places and none of them is a unit
test.** Inno Setup is not on the Linux runners, and compiling on the Windows
one would prove that the script compiles rather than that it installs. So:
`tests/test_installer.py` holds the properties that are cheap here and
expensive to find there (the pins' shape, the version agreement, the payload
file list, and above all the seam, which is the scan for `sc.exe`, `schtasks`,
`New-Service` and `icacls` in both the .iss and `prepare.ps1`); the release
workflow compiles it on every push to `main` and every tag, so a broken .iss is
found before a release rather than during one; `temp/check_pins.py` downloads
both pinned files and checks the hash, the folder inside the zip and whether
the build still has NVENC; and `prepare.ps1 -NoInstall` runs its whole
detection path on any machine without changing anything, which is what found
its first defect. That defect is worth keeping: `$PSScriptRoot` is not
reliably populated while a param block is being bound on 5.1, so
`[string]$Root = (Split-Path -Parent $PSScriptRoot)` failed **at the bind**,
before a line of the script ran, with an error naming `Split-Path` that reads
as a bad argument rather than a bad default. Same family as the wizard's
`NameError`: the entry point is the worst place to learn this, and running the
thing is the only way to.

**A clean VM then found two more, and the first had shipped.** `install.ps1`
sets `$ErrorActionPreference` to `Stop`, which is right for the cmdlets, and
PowerShell 5.1 wraps every stderr line from a *native* command in a
`NativeCommandError` record, which under `Stop` throws. So `python -c import
requests` on a machine without requests, a probe whose entire purpose is to
fail, killed the installer with a traceback instead of returning 1. It had been
that way since the Windows port shipped, and **install.ps1 had therefore never
been able to install on a machine without requests**: every machine it had run
on already had it, so the probe succeeded, wrote nothing to stderr, and the trap
never fired. `Invoke-Tool` now owns every native call there, relaxing the
preference and putting it back, and seeding `$LASTEXITCODE` with a sentinel
because that variable is only written by a process that actually started.

The second was in the prerequisite stage and is worth keeping for how it was
nearly misdiagnosed. `& $exe -version 2>$null | Select-Object -First 1` reports
a **non-zero exit code**, because `Select-Object -First` stops the pipeline as
soon as it has a line and PowerShell then terminates the upstream process, so
the check reported a perfectly good ffmpeg as one that would not run. The first
measurement of that took **one sample**, hit the one run in forty that finishes
before the pipeline stops it, and cleared the pattern; a second measurement over
forty runs found 39 failures. **A single sample of a race is not evidence**,
which this project already knew from measuring retry recovery against an
unanchored failure phase and had to learn again. Capture with `Out-String`, then
take the line out of the string; a test scans for the shape.

That probe had also broken a rule this project already had, and the rule is the
reason the second bug was findable at all: **never discard a probe's stderr**.
It reported "ffmpeg unpacked but will not run" and nothing else, which is a
conclusion with no evidence under it. It prints the exit code and what the
process said now, and keys on the version banner rather than on the exit code.

The third came from the case the checklist calls the one nothing here can
reach: a machine with **no Python at all**. `Start-Process` joins its
`-ArgumentList` with spaces and does **no quoting of its own**, so
`TargetDir=$target` reached the Python bundle as `TargetDir=C:\Program` plus a
stray `Files\Python314`, and Python was installed to `C:\Program`. The bundle
exited 0, because it had installed a Python; just not where the next line was
about to look. Measured before fixing (`temp/argprobe.ps1`): the bare form
arrives as two argv entries, quoting either the value or the whole pair arrives
as one.

Three things worth keeping about it. The **strict check is what caught it**:
`Install-Python` verifies `python.exe` is at the target rather than trusting
the exit code, so a silent misinstall became a clear refusal instead of a
failure three steps later with nothing pointing back here. The other three
`Start-Process` calls in this tree were **already quoted**, which is how one
unquoted call site got in, so the test scans every argument array rather than
that one line; quoting a value with no space in it costs nothing, so the rule
needs no judgement about which variables can hold one. And the fix carries its
own trap: under `CommandLineToArgvW` a **backslash before the closing quote
escapes it**, so a value ending in one swallows the argument after it
(measured: `AFTER=1` was absorbed into the path). Hence the `TrimEnd`.

### The low-space guard, and three checks that could not see it

Reported from a 29 GB VM 2026-08-18, and the shape is one this project keeps
meeting: **a number that decides everything, which nothing checked against.**

`DiskGuard` pauses capture below `capture.min_free_gb` and resumes only at
110% of it. So a threshold at or above the disk's free space does not make
capture tight, it makes capture **impossible**: paused on the first tick, never
resumed, no error anywhere, one line in `capture.log`. `default_config()` ships
60, which is wrong on any disk with less than about 66 GB free.

Three surfaces all reported a healthy machine, measured before fixing
(`temp/minfree_probe.py`):

| surface | said | why it could not see it |
|---|---|---|
| GUI Storage box | `ok  29.0 GB free` | compared against an unrelated 5 GB floor |
| `timelapse test` | `Headroom fine` | projected a day of frames, never read the threshold |
| console wizard | correct | it asks, and scales its default to the disk |

The console wizard was right all along, which is what makes this drift rather
than an oversight: **the graphical wizard never asked**, so the flat 60 stood on
every Windows install and could only be changed by hand-editing the file the
wizard exists to write. That is the `tools/` rule met from the other side, and
the reason `default_min_free_gb()` is now one function both wizards default
from rather than a formula sitting inside `choose_capture()`.

Four things worth keeping.

**FAIL, not WARN.** A threshold the disk cannot satisfy gets the same verdict
`check_interval()` gives an interval below `encode.min_frames`, for the reason
stated there: a configuration that produces nothing at all, silently and for
ever, is a refusal. The precedent settled the question; it was not re-argued.

**Keying on absence was not enough, and driving the page is what showed it.**
The first cut read `cap.get("min_free_gb")` and scaled only when the key was
missing. `default_config()` always supplies it, so on a fresh config the key is
never missing and the VM would have opened on 60 exactly as before. Unit tests
could not see this: they call `check_storage()` directly and never construct the
page. `guard_for()` now replaces a stored value the disk cannot satisfy, and
`temp/guard_page_probe.py` builds page 1 through `run()`, reads the field back
and types into it. **A decide layer that is fully tested still says nothing
about what the page hands it.**

**The refusal names a number that works.** Telling somebody their configuration
is impossible and leaving them to work out what is not is half an answer, and
the suggestion comes from the same `default_min_free_gb()` the console wizard
defaults from, so the two cannot disagree.

**The pre-flight fix is a Linux fix.** `test_disk()` has never compared the
threshold to free space on any platform. It was found on Windows because that
is where a small disk met a wizard that did not ask, but the check was blind
everywhere, which is the third time a Windows transcript has turned up
something that was wrong on Linux for releases.

**What none of that reaches is a real elevated install from the .exe**, which
is a checklist, on a machine that can be put back:

1. On a box that already has both prerequisites, run it with **both boxes left
   ticked**. Nothing should be downloaded and the log should say so for each,
   naming the Python and the ffmpeg it found. This is the case that would
   otherwise overwrite an operator's own ffmpeg, so it is first.
2. SmartScreen warns; "More info", "Run anyway". Confirm the publisher line
   reads as unsigned rather than as somebody else.
3. The progress page names the prerequisite stage rather than sitting on a bare
   bar, the finish page offers the wizard, and clicking Finish opens it with
   **no console window** anywhere in the sequence.
4. Check the two directories: the release tree and the uninstaller under
   `Program Files\timelapse-maker`, the scripts under `Program Files\timelapse`.
   Then `timelapse version` **in a new terminal**, which exercises the PATH
   entry and the `.cmd` wrapper at once.
5. Run it a second time over the top. It must reconfigure rather than refuse,
   and must not walk the wizard again.
6. Uninstall from Settings, Apps. The service and both tasks are gone, both
   directories are gone, and `%ProgramData%\timelapse` with its config, frames
   and videos is untouched.
7. Only on a machine with **neither** prerequisite: the download path, which is
   the half nothing else here can exercise. Watch for the pinned Python landing
   in `Program Files`, `py -3` working afterwards, and the wizard opening,
   which is the one that proves `Include_tcltk` was passed.

All 21 of its checks passed on 2026-08-16, on the second run. The first failed
11 of 18, and every one of those was the same quoting bug cascading rather than
eleven defects: the interpreter check produced an empty version, so the floor
check stopped the install, so nothing after it had anything to measure. Worth
stating because the opposite reading is expensive. **Count causes, not failed
assertions.** It also produced two findings about the harness itself, both of
the kind that make a check worthless rather than wrong: a helper whose return
value leaked into the output, and a check that asserted the *absence* of a
string and therefore passed against no output at all.

**Reading the passing log then found three more defects**, which is the argument
for reading it rather than trusting the verdict. A scheduled task that has never
run reports `LastTaskResult` 267011 (`SCHED_S_TASK_HAS_NOT_RUN`) and
`--unit-status` printed the number, so a healthy system announced what looks
like a five-figure error code; `-Uninstall` ran the dependency install on its
way past, so removing the program could install a package; and the restricted
ACL the installer now sets made `Path.is_dir()` **raise** for an unelevated
pre-flight, where on Linux it returns False. A pass is not the same as a good
log.

All 17 of `step3_check.py`'s checks passed on the first elevated run,
2026-08-16. Two of the
numbers matter beyond a tick: RUNNING was reported **0.7 seconds** after
`StartService`, against roughly thirty seconds of SCM patience, so the 1053
failure is not being narrowly avoided; and the service captured **six frames in
twelve seconds at a two second cadence**, which is exactly right rather than
approximately right, so the tick grid survived being hosted on a thread the SCM
owns.

**The graphical wizard is tested by splitting it, not by driving it.**
`timelapse_gui.py` is in two halves separated by a banner comment, and the
split is the testing strategy rather than a tidiness preference. Everything
above `# THE SHOW LAYER` takes strings and returns `(level, message)`, so
`test_gui.py` checks every rule the wizard applies without a display; below it
is widgets and calls upward, and nothing there decides anything. Three
structural tests hold that line: no `tkinter` import at module scope (asserted
against the AST, because the module *docstring* legitimately mentions it), no
widget identifiers among the tokens of the upper half (from `tokenize`, for the
same reason), and the colour table covering exactly the levels the checks can
return. A fourth pins the reuse: patching `setup.name_taken` must change what
the GUI's name check answers, which it only can if the GUI has no copy of that
rule.

That leaves the widget layer, which no automated test here reaches.
`temp/gui_smoke.py` builds every page, walks the camera pane through every
camera type, drives the network-share picker, **presses Add, Save and
Remove and checks the config list afterwards**, and **drives the Scan network
window on stubbed replies**, which catches the class of
mistake unit tests structurally
cannot: a widget method that does not exist, two geometry managers mixed in one
container, a trace firing before the widget it writes to exists, a button wired
to nothing. It asserts nothing about appearance. The rest is a click-through
checklist run by hand, below.

Driving it turned up a trap worth stating, because anything reaching into a
built window will meet it: **`ttk.Combobox` is a subclass of `ttk.Entry`**, so
collecting the text boxes with `isinstance(w, ttk.Entry)` picks up the type
picker as well, and the first version of the driver typed a camera's address
into it.

The scan half of the driver stubs `setup.discover_cameras` rather than probing
for real, for two reasons: a smoke run has to say the same thing on a machine
with no cameras on its LAN as on the one with eight, and the four invented
replies are shaped to cover the cases the window has to tell apart (a make it
recognises, one it does not, one already in the config, and a device that is
not a camera). Everything else is a real click on a real widget. `scan_dialog()`
blocks in `wait_window()`, so the driving is scheduled with `after()` *before*
the button is pressed.

It then presses Next on what the scan left behind and **counts the live tests**
rather than only checking their result, because the thing that costs real money
if it is wrong is a camera with no credentials reaching the network at all. One
call, for the one camera that had credentials.

The snapshot half stubs the fetch and uses **real pictures**: two JPEGs made by
the configured ffmpeg, a small one on the make's default stream and a large one
on another, so the switch fires, the thumbnail is really rendered and really
drawn, and the modal is really opened and closed with Escape. That is the case
no unit test can reach, because everything interesting in it happens between
ffmpeg and a Tk `PhotoImage`. Its Escape is scheduled with `after()` before the
click that opens the window, for the same reason the scan's driving is.

Layout, though, is still only visible in a screenshot, and this feature is the
fourth defect that workflow has caught: `temp/shot_snapshot.py` parks the camera
page with a tested camera on it (or the full-size view, with `full`), and
`temp/pane_height.py` measures what the pane wants against what it has, which is
how `WINDOW` came to be 660 rather than a guess.

**The click-through checklist**, in the order that finds problems soonest:

1. **Launch it from the Start menu, not from a prompt.** No console window
   should appear at any point, and the UAC prompt should come before the
   wizard. This is the step that found the shortcut targeting a `.cmd`.
2. Every page opens, Back is disabled on the first page, and the last button
   reads Finish rather than Next.
3. Basics, storage: type a relative path and a nonexistent drive. Both refused
   before Next, and the three derived directories update as you type. **Then
   press Browse: it must open at the folder already in the box.**
4. Basics, ffmpeg: point it at a folder rather than an executable. Both
   binaries are found and the encoder is named. Browse opens where the box
   points. Point it at nothing and the download page is offered.
5. Basics, cadence: enter 900. Refused, saying a day of that produces no video,
   which is the failure that is otherwise silent for ever. **Read every label
   on this page to its last character:** a fixed-width label cuts what does not
   fit and says nothing about it.
6. Cameras: press Add, then name it `NUL`, then `Roof Top!`, then an existing
   name. Refused, corrected with a warning, refused. **Pick each camera type in
   turn:** an address field for the presets and a URL field for Custom, with
   username and password appearing for the makes that need them, and every row
   staying where it was. Reolink says the password goes in the URL. **Tick No
   credentials required:** both credential boxes empty and grey, and the camera
   saves with `no_credentials` and `auth: none` and no username in
   `config.json`. An RTSP camera declared that way gets
   `rtsp://host:554/stream1`, with no `:@` in it.
6a. **Press Test against a real camera.** The verdict names the size in KB, the
   dimensions and the seconds, not just "successful", and **the frame itself
   appears above the button**. Check the picture is the right camera and the
   right way up, which is the one thing this whole feature exists for. On a
   Dahua, Hikvision or ONVIF preset the wizard also tries the other streams: if
   a larger one exists, a Stream row appears saying which, the caption names
   both sizes, and Save is what keeps it. **Click the picture:** it opens as
   large as the screen allows, and closes on the Close button, the window's X,
   a click on the image and Escape, all four. Press Test on a camera with the
   wrong password: no picture, and the reason says 401 rather than a generic
   failure. Press Test on a camera that is off: the reason names the timeout.
7. **Select an existing camera in the list.** One added by make reads back as
   that make with its address in the box; one whose URL nothing here produces
   reads Custom URL. Username, password, cadence, frame rate, smoothing and
   the enable switch are all filled in from the config, and the cadence and
   frame rate boxes are **empty** unless that camera really has an override.
   Change the password, click a different camera, and it must offer to save
   first. Set a cadence equal to the global and save: the key must not appear
   in `config.json`.
8. Cameras: press Add and then Next without filling anything in. Refused,
   naming what to do. Press Add, click away, answer no: the empty row goes.
   Remove asks before it removes.
9. **Press Scan network** with a camera already configured. The window says it
   is listening before it freezes, not after. What comes back is only the
   cameras, counted, with anything else answering mentioned and not listed. The
   configured one is greyed, untickable, reads "already added" and says the same
   on hover. A camera whose make was recognised shows it; one whose make was
   not shows an empty picker and **cannot be ticked until a make is chosen**.
   Tick two, and the button counts them. They arrive as `CameraN`, enabled,
   with the address in the box, no cadence override, and no password.
   **Press Next without touching them:** both are switched off, named in a
   popup as having no credentials, and the page does not change. Nothing
   should have been fetched from them. Then give one its credentials, tick it
   back on, press Test, and press Next: it goes through, and a camera you
   already tested is not tested a second time. Unplug one and press Next: it
   is switched off with a different reason, and the wizard carries on.
10. Transfer: type a local folder, then a UNC path. Username and password appear
    only for the UNC one, and *above* the Test button. Test says which account
    it proved, and warns when that account is not the one that runs nightly.
    **Then press Network:** every drive the operator has mapped is listed with
    where it really points, and picking one puts the UNC in the box and carries
    on into the folder dialog there.
10a. **Type a username and a deliberately wrong password for the share, and
    press Test.** It must fail, naming the refusal, on a machine where you can
    write to that share yourself: that is the whole of H5, and it is the one
    step no test can take, because the decide layer is proven against a
    stubbed `connect_share()` and what this measures is the real one. Then
    press Next without pressing Test at all: a warning names the share and
    asks whether to store them anyway, No stays on the page and Yes goes
    through. Press Test with the right password, then change the password, and
    press Next: the warning must appear again, because the green tick above it
    describes the previous password.
11. Notifications: fill in one service and press its Send a test message. The
    message arrives. Leaving all three empty is allowed and says so. **All
    three services must be visible without resizing the window**, along with
    the Next button.
12. Summary: no password and no webhook URL anywhere on it. The Video row
    names the encoder this machine chose and the quality it will use. Finish
    writes the config, backs up the previous one, and restarts the services.
13. **Run the checks from the completion dialog.** The pre-flight output
    appears in a window. Nothing in the wizard should ever tell the operator to
    open a prompt and type a command.
14. Run it again with that config present. Every page opens on the saved
    answers rather than on defaults.

Steps 6, 6a, 7 and 10 are the ones worth repeating after any change: they are
where
the GUI has to agree with the console wizard, and where disagreeing stays
invisible until an install fails. Ten of the defects in the first operator run
were in this list's territory, and none of them was reachable by a unit test.

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

`test_grammar.py` holds the Python floor, and exists for one specific gap.
`ast.parse(feature_version=(3, 9))` sounds like it settles the question and
does not: it parses with the *running* interpreter's tokenizer, so PEP 701
f-strings, which are 3.12, sail through it. That is the one 3.10+ construct
this project can realistically write by accident, and it fails at **import**,
which means not a broken panel but a service that does not start, on the
distributions this project most expects (RHEL 9, Debian 11) and never on the
machine it was written on. It caught a real one on the day it was added, in
`timelapse_web.py`:

```python
f'<td{" class=\'bad\'" if bad else ""}>'      # SyntaxError on 3.9
```

Build the value on its own line instead. The test was confirmed to fail on
that line before being kept: a guard nobody has seen fail is not a guard.

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

It then runs a **second night with `delete_frames_on_success` off**, because
that is the configuration the encode-once marker exists for and the one a
regression would hide in: the day is encoded, the frames stay, the marker
appears, a re-run encodes nothing and says why, the video's mtime and size are
untouched, and `--force` encodes it again. The unit tests can assert about the
marker; only this can assert that a real run leaves the video alone.

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
- **Runtime state** (0.1.6): Ubuntu 24.04 under real systemd, and this one had
  to be measured because the plan for it was wrong on paper. `future-features`
  claimed no unit change was needed since both units carry
  `ReadWritePaths=/var/lib/timelapse`; that is the shipped *template*, while
  `sync_units()` overwrites the line from the config with three sibling
  directories, so a new sibling would have been read-only on every real
  install while passing every test here. Confirmed after the fix: the
  installer creates `state/` at 0750 `timelapse:timelapse`, both units carry it
  in `ReadWritePaths` and `systemd-analyze verify` is clean, capture publishes
  `capture.json` **from inside the sandbox** and records `running: false` on
  stop, the encoder writes `encode.json` for a run that found nothing to do,
  the pre-flight passes, and the overview renders both panels over HTTP.
  A real disk-guard pause (`min_free_gb` set absurdly high) produced
  `paused: true` and the PAUSED banner. The **upgrade path** was exercised
  separately: v0.1.5 installed from the published tag (no state directory, no
  state in `ReadWritePaths`), then this tree over the top, after which the
  directory exists, the units carry it, and a config that still has no
  `state_dir` key publishes to the default location. That is the hazard worth
  re-testing on any future change here: a `ReadWritePaths` entry that does not
  exist stops **both** daemons, with an error about mount namespaces.
- **Two encode runs at once** (0.2.1, §4.2): Ubuntu 24.04 under real systemd,
  `temp/c4_wsl_serialise.sh`, 15 checks with two genuine processes. A stub
  ffmpeg makes the overlap deterministic rather than a race the test would
  sometimes lose. What only this can answer is whether the lock works *inside
  the sandbox*: the timer-started unit takes it under `ProtectSystem=strict`,
  the file lands in the state directory the unit may write, it names that
  unit's exact pid, a manual `timelapse encode` during the run is refused with
  that pid and deletes nothing, the unit finishes with one video, and a run
  afterwards is not blocked. The primitive itself is measured separately and on
  both platforms in `temp/c4_lock_probe.py`, including the case that decides
  the design: a *killed* holder frees the lock, which a pid file would not.
  **The stub was wrong twice**, both times in the direction of testing nothing:
  it keyed on `lavfi` to tell the encoder probe from the real encode, but
  `list_encoders()` runs ffmpeg once before any probe and carries neither, so
  the 25 second delay landed nowhere near the encode; and its ffprobe printed
  commas where `-of csv=p=0:s=x` means `WIDTHxHEIGHT`, so every day failed
  before ffmpeg was reached. Match the thing you mean.
- **A path with a space in it** (0.2.1, §4.4b): Ubuntu 24.04 under real
  systemd, `temp/c2_wsl_e2e.sh`, a full install with a frames root of
  `/srv/c2e2e/My Passport/frames`. Eleven checks: the emitter quotes it,
  `install.sh` splices the quotes into the unit, `systemd-analyze verify` says
  nothing, `systemctl show` reads it back as **one** entry, and capture starts
  and writes frames and `capture.log` into it. **The control is the check that
  matters**, and it is why this is worth running rather than reasoning about:
  the same unit with the quotes taken out again fails 226/NAMESPACE, so the
  passing half is known to be the quoting and not the directory merely
  existing. Two things went wrong in the harness first, both of them the
  harness agreeing with a broken product for an unrelated reason. Its data root
  was under `/var/tmp`, which `PrivateTmp=true` hides from every unit, so the
  first run reported 226/NAMESPACE on a directory that plainly existed: use
  `/srv` or `/mnt`, as the note above says. And the control's `sed` used `/` as
  its delimiter on paths full of slashes, so it never edited anything and the
  control "passed" against a unit that was already failing.
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
  web unit without a `ReadWritePaths` line and `restore_services()`
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

<sub>[&uarr; Contents](#contents)</sub>

## 9a. Appendix: the destination library, surveyed 2026-08-07

Reference data, not a plan. This is the measurement `parse_name()` was built
against, and the reason it is a chain of six patterns rather than one regex.
It lived in `future-features.md` until that file was trimmed back to being a
plan; the numbers are a record of one real library at one moment, and are not
re-measured.

A read-only survey of the author's own destination (`U:\TL`, an Unraid share
over SMB), the only real library this has ever been measured against. 6,848
files, 2.78 TiB, 2021-06-26 to 2026-08-06, in 55 `YYYY-MM/` folders plus two
named event folders and 1,247 loose files at the root.

**The destination is not a timelapse-maker output directory.** It is five
years of accumulated history from three predecessor systems, and this is the
single most important fact about the index: a parser written against the
native filename format handles **64% of it**.

| Pattern | Files | Era |
|---|---|---|
| `Camera.YYYYMMDD.mkv`, native | 4,384 | 2024-04 → now |
| `YYYY-MM-DD_Camera.mp4` | 1,594 | 2022-09 → 2024-02 |
| `YYYY-MM-DD.mp4`, **no camera name** | 449 | 2021-06 → 2022-09 |
| `Camera_YYYY-MM-DD.mp4` | 415 | 2021-07 → 2022-09 |
| `Camera<date>_<timestamp>.mkv` | 3 | 2024-04 |
| `YYYY-MM-DDTHH-MM-SS[_cam].mp4` | 2 | event folders |

A chain of patterns, tried most-specific first, parses 6,847 of 6,848. The one
failure is `MakeTLALL_backup.ps1`, the PowerShell predecessor still sitting in
the root, which is also the proof that "not a directory" is not a sufficient
test for "is a video".

Constraints this survey establishes, each of which breaks a naive index:

- **`Workshop` (723) and `workshop` (446) stay two entries.** They are almost
  certainly one place typed two ways, but see the rule below: the index does
  not decide that. Sort case-insensitively so they sit adjacent and the reader
  can see it for themselves.
- **449 files have no camera name.** They need a real bucket, not an
  exception. (This is also where `keep_blank_values=True` earns its place: the
  group's key is the empty string, and a `?camera=` link to it is a real
  link.)
- **`(camera, date)` is not unique**: 6,844 keys for 6,848 files. The primary
  key is the relative path. Nothing else is safe.
- **Extension allow-list**, not a directory test. See the `.ps1`.
- **Container changes with era**: mp4 through 2024, mkv after. Do not infer it.
- **Human-made folders carry meaning.** `2023-05-10 - Renovări` and
  `2023-05-12 - Dubios la poartă` are events, not clutter. Keep the folder as
  a label rather than flattening it away.
- **Never merge two names.** `garaj`/`Garage` and `street4k`/`StreetPTZ` look
  like renames and are not: a camera name is a **place**, cameras get
  repurposed over the years, and the same device may cover a different area
  while a new device covers the old one. The name is a location label, not a
  device identity, and the two drift apart deliberately. Corrected by the user
  2026-08-07; do not regress this. Deciding whether `garaj` and `Garage` are
  the same place is the *user's* call, not the index's. Show what is on disk.
- **Some files are broken.** 11 are implausibly small: `Gate.20240727.mkv` at
  17 KB, `Gate.20251109.mkv` at 86 KB, almost certainly failed encodes. List
  them with their **full path**, so they can be checked and removed by other
  means. The UI is read-only and does not delete; naming the path is the whole
  service it can offer.

Two findings make reconciliation cheap:

- **mtime is exact.** Across all 4,384 native files, mtime is precisely one
  day after the filename date: min, median, p95 and max all equal 1. That is
  the 00:05 encode timer. `(size, mtime)` is a dependable change key.
- **The folder never disagrees with the filename.** Zero mismatches in 5,601
  filed files, so `YYYY-MM/` is redundant and the index can key entirely on
  filenames.

**On scan duration: there is no baseline, and the design must not need one.**
A full recursive enumeration measured 1.7 s, but that was from a workstation
with 10G to the server, and the deployment reads over CIFS from Linux on a
different stack. The work is latency-bound rather than bandwidth-bound (size
and mtime arrive with the directory entries, so it is round-trips, not
megabytes), but that only means the number moves for reasons that are hard to
predict. Treat 1.7 s as a floor observed once, never as a budget.

---

<sub>[&uarr; Contents](#contents)</sub>

## 10. File inventory

```
install.sh                       bootstrap installer, 1014 lines
install.ps1                      the Windows one, 578 lines
setup-gui.ps1                    launcher for the graphical wizard, 111 lines
timelapse-setup.cmd              double-click shim for the above
installer/timelapse-maker.iss    the .exe, for Inno Setup 6, 237 lines
installer/prepare.ps1            prerequisites, then install.ps1, 493 lines
installer/prerequisites.json     the pinned Python and ffmpeg downloads
scripts/timelapse_capture.py     daemon, 1584 lines
scripts/timelapse_encode.py      batch job, 2300 lines
scripts/timelapse_test.py        pre-flight checks + usage report, 1049 lines
scripts/timelapse_setup.py       configuration wizard, 4814 lines
scripts/timelapse_gui.py         the same wizard in a window, 2946 lines
scripts/timelapse_update.py      release query + `timelapse update`, 471 lines
scripts/timelapse_platform.py    the only platform branch, 2377 lines
scripts/timelapse_cli.py         the `timelapse` command on Windows, 374 lines
scripts/timelapse_web.py         read-only web UI, 3560 lines
tests/_support.py                path setup and fakes
tests/test_capture.py            unit tests
tests/test_cli.py                unit tests
tests/test_encode.py             unit tests
tests/test_grammar.py            the 3.9 floor, incl. the PEP 701 gap
tests/test_gui.py                the graphical wizard's decide layer
tests/test_installer.py          the .exe, its pins and the seam below it
tests/test_platform.py           unit tests
tests/test_preflight.py          the pre-flight's camera checks
tests/test_setup.py              unit tests
tests/test_update.py             unit tests
tests/test_usage.py              unit tests
tests/test_web.py                unit tests
tests/smoke_test.py              end-to-end encode check, needs ffmpeg
config/config.example.json       template; the real config.json is gitignored
service/timelapse-capture.service
service/timelapse-encode.service
service/timelapse-encode.timer
service/timelapse-watch.service   credential watch, oneshot
service/timelapse-watch.timer     every 5 minutes
service/timelapse-web.service
docs/architecture.md             this file
docs/install.md                  operator guide
docs/future-features.md          planned work, in build order
.github/check_windows_config.py  CI: the wizard's Windows locations
.github/workflows/installer.yml  CI: builds and attaches the .exe
docs/decided-against.md          considered and refused, and why
```

**The Python floor is 3.9 and is a deliberate, machine-checked property**, not
an accident of when this was written. RHEL 9 and its rebuilds ship 3.9 as the
system `python3` and are supported to 2032, and, more to the point, that is the
interpreter their packaged `python3-requests` is built for: pointing the units
at a newer AppStream python would mean pip and a venv on one distro family, in
exchange for syntax this project does not need. It costs nothing to hold, since
stdlib-only with no type hints avoids nearly everything 3.10+ added, and it is
enforced rather than asserted by the CI 3.9 leg and by `tests/test_grammar.py`.
Debian 11 was the other justification and its LTS ends in August 2026, so the
floor now rests on RHEL 9 alone; revisit it if that stops mattering, if anyone
reports friction it actually causes, or if it ever stops being machine-checked,
because it is only cheap while it is automatic.

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

<sub>[&uarr; Contents](#contents)</sub>

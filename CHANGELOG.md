# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, the configuration format may change in any release.

## [Unreleased]

### Changed
- **`timelapse web` now suggests this host's LAN address** rather than
  `127.0.0.1`. A status page reachable only from the machine it describes is
  of little use on a headless recorder. It falls back to `0.0.0.0` when no
  LAN address can be worked out, keeps an address you already have set, and
  says so plainly when accepting the suggestion would move an install that
  was deliberately kept local. The config file default is unchanged: a
  hand-edited `config.json` still starts closed.

### Added
- **The wizard checks the bind address against the kernel** before writing it.
  An address this host does not have is refused at the prompt, with the
  addresses it does have listed, instead of producing a config whose service
  starts, reports success and is unreachable. A privileged port is refused
  too, since the service runs unprivileged.

### Fixed
- **`timelapse web` now restarts the service.** `systemctl enable --now` is a
  no-op on an already-running unit, so changing the bind address or port
  reported success and changed nothing until the next reboot. It now offers
  the restart, and offers to stop the service if you turn the UI off.
- **The web UI no longer logs a traceback when a viewer closes a video.**
  Playing or seeking a timelapse and then quitting the player left a
  `ConnectionResetError` traceback in the journal, tagged as an error. The
  playback itself was fine; the log was reporting normal behaviour as a
  crash. Disconnects are now recorded at debug level, and genuine faults go
  through the logger with a timestamp and a level instead of to bare stderr.

## [0.0.9] - 2026-08-07

### Added
- **`timelapse web`**: an optional, read-only web UI, disabled by default.
  It shows where your finished videos actually live, an index of the videos
  themselves browsable by camera and by date, and, on request, never by
  polling, the output of `systemctl status` for every unit and the recent
  journal for capture, encode or the UI itself, and a Play link that hands
  each video to VLC.

  It never changes anything of yours: no encode triggers, no camera control,
  no config edits, no deleting. The unit enforces that rather than
  trusting the code: `timelapse-web.service` may write one directory, its own
  index, and nothing else on the system.

  It binds `127.0.0.1` by default. There is no login and no HTTPS, so any
  other bind address exposes the page to your LAN; it warns when you do that.

  The status page leads with where the video library resolved to, because that
  is the question worth answering first: transfer moves videos to your NAS with
  `--remove-source-files`, so `paths.video_output` is *empty* after a
  successful night. A remote `user@nas:/...` destination is not a path this
  host can read at all, and the page says so instead of showing an empty list
  that looks like a fault, as does a NAS that simply is not mounted.

  The log pane has the same habit of explaining itself. `journalctl` tells a
  process without journal access that there are simply no entries, which is
  indistinguishable from a quiet service and reads as a broken page. The unit
  asks for `SupplementaryGroups=systemd-journal` so this does not arise; where
  that group does not exist the installer removes the line (naming a missing
  group would stop the service starting at all) and the page then tells you
  which line to add.

  The library index is built to survive a real destination rather than a tidy
  one. Surveying five years of accumulated timelapses turned up **six**
  different filename conventions from successive tools, of which the format
  this project writes accounts for under two thirds, so the index tries a
  chain of patterns and files anything it recognises, including videos whose
  names carry no camera at all. It never decides that two similar names mean
  the same thing: a name is a *place*, cameras get repurposed over the years,
  and whether `garaj` and `Garage` are one thing is yours to judge, not the
  index's. Names are shown as they are on disk, sorted case-insensitively so
  variants sit next to each other.

  Files too small to be a real day of video are listed with their full path, so
  you can check and remove them yourself; this UI never deletes anything.

  The first scan runs in the background, so the page is up immediately and
  reports progress. After that, opening a folder re-reads that one directory
  and opening a file re-stats it, which keeps a browse from walking a NAS.

  Videos play in **your** player, not in the browser. Every listing has a
  *Play* link that hands VLC (or mpv, or whatever opens `.m3u` on your desktop)
  a one-line playlist pointing back at this server, plus a *Download* link and
  the two addresses that need no web UI at all: the share path, for a machine
  that has the mount, and the stream URL for VLC's *Open Network Stream*. This
  is deliberate: the videos are AV1 in Matroska, which browsers handle badly
  and real players handle natively. The playlist is built from the address you
  reached the page on, so a link opened on your phone points at something your
  phone can actually reach.

  There is also **one playlist per day**: open a single file and your player
  queues that day's videos from every place in turn, rather than you opening
  seven of them. Recent days are listed on the library page, and any date in a
  listing links to its own day. It includes whatever was filed under that date,
  whichever naming convention the file came from.

  Seeking works: drag the scrubber and the player jumps, instead of
  re-downloading from the beginning. Interrupted downloads resume, and one
  that resumes against a video that has since been re-encoded quietly starts
  over rather than stitching two versions together.

  The index is the one thing the UI writes, and the systemd unit is scoped to
  exactly that directory: the videos, the captured frames and `config.json`
  stay read-only to it.

  The setup wizard asks about all of this, and **`timelapse web`** reconfigures
  just this part later without walking the whole wizard. It shows you which
  path it will read videos from and why (the answer surprises people), and it
  states plainly that there is no login and no HTTPS before asking what address
  to listen on. Operator guide: [docs/install.md §10](docs/install.md#10-the-web-ui).
- **`timelapse usage`**, a disk report: frames, bytes and date range per camera,
  totals, videos and free space. It also names the directories nothing will
  ever encode: a camera removed from the config (`ORPHAN`) or merely disabled
  keeps everything it captured, because the nightly encode walks only the
  cameras *enabled* in the config. That is usually what is filling the disk,
  and it is invisible to `du`.

### Fixed
- Fixing a broken transfer and re-running the encode by hand did nothing. A
  failed transfer correctly leaves your videos in `video_output` to go out
  next time, but the retry only happened on a run that also had something to
  encode, and re-running the same night there is nothing new. So the obvious
  move after remounting a share was the one path that never retried. The
  backlog now ships even when there is nothing to encode, and a run that
  fails to ship it exits non-zero so it shows up as a failed service instead
  of a quiet success.
- `timelapse status` now shows `timelapse-encode.service` as well as its
  timer, plus the web UI. The encode service is oneshot, so a run that failed
  left it in a failed state while the timer beside it still looked perfectly
  healthy, which is the one place you would have looked.
- A NAS that was not mounted yet cost you the whole video index. The startup
  scan read an empty or missing library, concluded every file had been
  deleted, and emptied the index, which on a real share means re-reading
  thousands of files over the network. Worse, it did not recover when the
  mount came back. The scan now waits for the library to appear and picks it
  up on its own, and a scan that finds nothing at all keeps what it already
  had rather than throwing it away. Deleting some of your videos still updates
  the index normally.
- The video index would serve **any** file sitting inside your video folder,
  not just videos. The extension allow-list was applied when scanning but not
  when serving, and the serving path re-checks files on access, so a request
  naming a `.txt` or a script kept alongside the videos was read, added to the
  index and returned. Now the same allow-list applies to both. Found by a test
  written after the code had already been reviewed and thought finished.
- The wizard stripped documentation keys from the config template by walking a
  hardcoded list of section names, so any section added later kept its
  `_comment` keys and shipped them into live configs. Caught immediately by the
  new `web` section, which put three explanatory paragraphs into every
  generated `config.json`. It now strips every section, which is what the code
  claimed to do; its own comment already warned about a stale `_comment_cifs`
  reaching live configs once before.
- A missing, malformed or unreadable `config.json` produced a raw Python
  traceback from every entry point. Each of the three states needs a different
  action (not configured yet, broken after a hand-edit, or unreadable because
  the file is `0640 root:timelapse`), and a stack trace conveys none of them.
  All three now exit with a sentence naming the fix.
- **`timelapse cameras`**: add, edit, remove, enable/disable and test cameras
  against an existing config, without reinstalling or re-running the whole
  wizard. It restarts `timelapse-capture.service` for you, since the daemon
  reads its camera list only at startup.

  It also refuses to strand frames silently. The nightly encode builds its work
  list from the cameras *enabled* in the config and looks for
  `<frames_root>/<name>/`, so removing a camera, **disabling** one, or renaming
  one without moving its directory all orphan whatever it has already captured,
  permanently. Each warns first and names the `timelapse encode --date` that
  would rescue those days; a rename offers to move the directory instead.
  Disabling being just as destructive as removing is the easy one to miss.

  Passwords carried in a URL query string are masked in the listing;
  `ask_secret()` keeps them out of scroll-back when typed, so printing the
  camera table would defeat it.

## [0.0.8] - 2026-08-06

### Fixed
- **Re-running the installer over a live install did not actually upgrade it.**
  Replacing the scripts on disk changes nothing for a running daemon: it keeps
  executing the code it read at startup, and `systemctl enable --now` is a
  no-op on an already-active unit. So the installer replaced the files, printed
  *"Capture is running"*, and left the previous build serving until the next
  reboot. It now restarts a live `timelapse-capture.service` (after asking, and
  after `ReadWritePaths` has been re-derived), reporting honestly if you decline.
  An encode in flight is deliberately left alone: it is oneshot, so it finishes
  on the build it started with and the next trigger picks up the new one.
- The wizard's camera prompt counted the camera you had just added, offering
  *"Add another camera? (3 of ~9)"* when the next one would be the 4th.
- The pre-flight no longer sends a second Discord test message when the wizard
  already sent one and it succeeded. Setup records a fingerprint of the verified
  webhook; the check honours it for 15 minutes, so a standalone
  `timelapse test` next week still verifies properly. `--force-discord`
  re-sends on demand. The marker holds a truncated digest and a timestamp, never
  the webhook URL.
- The wizard could abort half-configured under `PYTHONIOENCODING=ascii`, where
  printing its own box-drawing headings raises `UnicodeEncodeError`. Characters
  now degrade to `?` instead.

### Added
- **One retry inside the capture tick** (`capture.retry_within_tick`, default
  on). A snapshot endpoint that refuses while busy answers in milliseconds, so
  the tick was being discarded with almost its whole budget unspent.

  Measured, because the scope is narrower than it looks: it recovers ~58% of
  *per-request* failures and **0%** of failures that are a busy window longer
  than one interval. The zero is structural: if the camera is out for longer
  than `interval_seconds`, the next tick already is the retry. A tick whose
  predecessor also failed is therefore not retried, which keeps an outage from
  doubling the request rate against a camera that just said it was busy.

  The retry's timeout comes from the remaining budget, so it provably cannot
  run into the next tick and cost a second frame. A rescued tick counts as a
  success, keeping `Cov%` meaning *frames on disk*.
- `timelapse version` prints the installed version of each script, and warns
  when the running daemon predates the files on disk, the one failure mode a
  version number by itself cannot show you.

### Removed
- A dead `ICON` constant in `timelapse_encode.py`.

### Documentation
- `install.md` gains an **Upgrading** section stating exactly what is kept,
  replaced and restarted, and a troubleshooting entry for the most likely snag
  on a shared host: leaving an NVR's own timelapse or snapshot schedule enabled
  points two clients at one camera, and most cameras answer the loser with
  `500`. A *fixed* number of consecutive failures per burst indicates a
  duration, so it is a second client rather than a flaky one; `Cov%` in the
  nightly summary is the signal to watch.

## [0.0.7] - 2026-08-06

### Added
- **The wizard sets up a network share itself.** The transfer step now offers
  *"A network share (SMB/CIFS) - set it up for me"*: it installs `cifs-utils`,
  asks for the server, share, credentials and mount point, mounts it
  (negotiating the SMB dialect down from 3.1.1), creates the destination
  folder, measures which rsync flags the share accepts, and writes an
  `/etc/fstab` entry with `nofail,x-systemd.automount`.
- `timelapse transfer` (`--transfer-only`) reconfigures just the destination
  against an existing config, without walking the whole wizard again. It also
  updates `ReadWritePaths=` in the installed units, so a share added after the
  initial install does not fail read-only under `ProtectSystem=strict`.

### Removed
- `tools/setup-cifs-transfer.sh`. The wizard does this now, and `install.sh`
  never installed the script anyway, so the wizard was pointing at a file
  that was not on the machine. One implementation instead of two that could
  drift.

### Fixed
- `timelapse setup` run outside the installer wrote the config `0640
  root:root`, leaving the service account unable to read it, a failure that
  only shows up when a unit refuses to start. `write_config()` now sets the
  group when it knows the service user.
- Documentation keys leaked from the example config into generated configs;
  one shipped a `_comment_cifs` still describing the removed script. Every
  `_`-prefixed key is now stripped, not the three that existed at the time.

## [0.0.6] - 2026-08-06

### Fixed
- **Pressing Enter at a yes/no prompt re-prompted forever.** `ask()` returned
  early only when the default was non-empty, and `ask_yes()` passes an empty
  default, so a blank line fell through to the retry loop. The only way past a
  `(Y/n)` prompt was to type `y` or `n`, which contradicts the wizard's one
  promise, that Enter accepts what is in brackets. Blank input now always
  returns the default.

### Changed
- **The transfer step no longer assumes SSH.** It asks how the destination is
  reached (a path on this machine, or another host over SSH), and only
  mentions SSH keys for the SSH option. Reported as confusing when configuring
  a CIFS share, which needs no SSH at all.
- For a local destination the wizard now checks the path, reports the
  filesystem backing it, and **offers `require_mountpoint`** when that is a
  network mount. This finally connects the guard added in 0.0.4 to the wizard
  that writes the config; before, only hand-editing or the CIFS script set it.
- It also **measures which rsync flags the destination accepts** and writes
  those, rather than shipping `-a` and letting the nightly run discover that
  the share cannot set owner/group. `-a` works on some shares and not others,
  so it is tested rather than assumed.
- An unmounted or unwritable destination is called out during setup instead of
  at 00:05 the following morning.

## [0.0.5] - 2026-08-06

### Fixed
- **The encoder probe reported AV1 unavailable on hardware that supports it.**
  `testsrc` emits rgb24 and the probe let ffmpeg negotiate the output format;
  ffmpeg picked `yuv444p`, which `av1_nvenc` advertises but NVENC on Ada
  cannot actually encode. The capability check failed and surfaced as
  `No capable devices found`, so an RTX 4060 with a current driver and
  ffmpeg 8.0.1 was silently downgraded to HEVC. Verbose output named it
  exactly: `YUV444P not supported`.

  Real encodes were never affected (`encode_day()` already ends its filter
  chain in `format=yuv420p`), so this only ever cost people AV1 they could
  have had. The probe now pins `-pix_fmt` to the same `PIX_FMT` constant the
  filter chain uses, so the two cannot drift apart again.
- `encoder_hint()` recognises a pixel-format rejection and says so, instead of
  folding it into the generic "no capable devices" advice.

## [0.0.4] - 2026-08-06

### Added
- `timelapse test --encoders`: full diagnosis of why a hardware encoder is
  unavailable: ffmpeg version and NVENC build flags, whether each NVENC codec
  is compiled in at all, GPU and driver from `nvidia-smi`, and a **verbose**
  probe per codec.
- `probe_encoder_verbose()` recovers the lines ffmpeg logs at
  `AV_LOG_VERBOSE` and discards at error level. This is where the real reason
  lives: an RTX 3090 reports `Codec not supported` before the useless
  `No capable devices found`, along with `Loaded Nvenc version 13.1` and the
  GPU it actually saw.

### Changed
- The hint for `No capable devices found` / `Codec not supported` no longer
  asserts a cause. Both an incapable GPU and an ffmpeg too old to ask the
  driver for the codec produce that same line, and asserting either one was
  the 0.0.3 bug in a new outfit. It now names both and points at
  `--encoders`.
- `nvidia-smi` cannot report NVENC codec capability at all; the diagnosis says
  so explicitly rather than leaving people to hunt for it.

## [0.0.3] - 2026-08-05

Bugs from the first real install on someone else's hardware.

### Fixed
- **Encoder probes discarded ffmpeg's error, so the wizard guessed the cause,
  and guessed wrong.** With `hevc_nvenc` working and `av1_nvenc` not, it stated
  "No AV1 NVENC on this GPU (needs RTX 40-series or newer)", reported on an
  RTX 4060, which encodes AV1 natively. Probes now capture stderr,
  `list_encoders()` checks whether the codec is compiled into the ffmpeg binary
  at all, and `encoder_hint()` derives the cause from ffmpeg's own message.
  `Unknown encoder` means rebuild ffmpeg; `No capable devices found` means the
  GPU or driver. The two are indistinguishable by exit code and need opposite
  fixes.
- **Discord webhooks returned HTTP 403.** Discord is behind Cloudflare, which
  rejects urllib's default `Python-urllib/3.x` User-Agent with error 1010
  before the request reaches Discord. All three webhook callers now go through
  `post_webhook()`, which sends the documented `DiscordBot ($url, $version)`
  form. Verified against Discord's API: the old header gets 403/1010, the new
  one reaches the API and gets a normal `Unknown Webhook` 404.
- **A camera answering 200 OK with an error body reported only "not a JPEG".**
  Reolink returns a JSON error there, e.g. `{"error":{"detail":"login
  failed"}}`. The wizard now parses and shows it, so an auth failure reads as
  an auth failure instead of a URL problem.
- **Credentials in a query string were over-encoded.** `quote()` escaped every
  reserved character; some camera firmware (Reolink notably) does not
  percent-decode query values, so an encoded password that works when typed
  literally would fail. It now escapes only `& = # + %`, space and non-ASCII.

### Changed
- Encoder probe frame is 512×512, up from 256×256. Measured: `hevc_nvenc`
  rejects 128×128 with "invalid param (8): Frame dimensions". Larger costs
  nothing and removes a variable. The architecture note attributing that
  minimum to `av1_nvenc` was wrong and has been corrected.
- `timelapse_test.py` reports the reason each encoder was unavailable, and
  distinguishes a 403 from a 404 on the webhook check.

## [Unreleased]

### Added
- `transfer.require_mountpoint`: refuses to transfer when the destination is
  not on a mounted filesystem. An unmounted CIFS/NFS mountpoint is an ordinary
  empty local directory, so rsync would fill the local disk and
  `--remove-source-files` would then delete the originals. Accepts `true`
  (walk up from the destination) or an explicit mount path (checked with
  `os.path.ismount`, more precise). Off by default.
- `tools/setup-cifs-transfer.sh`: mounts an SMB/CIFS share, determines which
  rsync flags the share actually accepts, performs a real round trip with a
  throwaway file (verifying md5 and that `--remove-source-files` worked),
  writes the `/etc/fstab` entry with `nofail,x-systemd.automount`, and prints
  the exact config.json block and `ReadWritePaths` change needed.
- `timelapse_test.py` now warns when the transfer destination is not on a
  mount, and when `rsync_args` uses `-a` against a CIFS/NFS destination.

### Notes
- Whether `rsync -a` works on CIFS depends on the server and mount options,
  `-a` implies `--owner --group`, which many shares reject with exit 23, but
  `forceuid`/`forcegid` can make it succeed. The tool measures it rather than
  assuming either way.

## [0.0.2] - 2026-08-05

### Added
- A unit test suite (`tests/test_*.py`, stdlib `unittest`, ~115 cases, under a
  second, no third-party dependencies). Covers frame validation, concat-list
  escaping, backlog selection, storage-scan filtering and deduplication,
  partition-name stripping, storage recommendation, `ReadWritePaths` derivation,
  credential quoting, and the DST collision suffixes in `_dest_path`.
  CI runs it on Python 3.9 and 3.12.

### Fixed
- `scan_filesystems()` normalised paths with `pathlib.Path`, which produced
  Windows separators when run off-target. It now uses `PurePosixPath` for
  `writable_paths()` output, which is correct in all cases; the result goes
  into a systemd unit.
- `timelapse_setup.py` could not be imported on a non-POSIX host, because
  `os.statvfs` was evaluated as a default argument. Only affects running the
  tests off-target, but there is no reason to forbid that.
- `install.sh` exited `1` on success whenever it had not downloaded a tarball,
  that is, every install from a local git checkout, every `--uninstall`, and
  `--help`. The `EXIT` trap ended on a failing test (`[ -n "$WORKDIR" ] && …`
  with `WORKDIR` empty), and bash lets a non-zero status from the last command
  in an `EXIT` trap override the script's real exit status. The installation
  itself was correct; only the reported status was wrong, but it would break
  any automation wrapping the installer.

### Changed
- CI asserts installer exit codes explicitly instead of relying on `&&`, and now
  runs a full install → verify → re-install → uninstall cycle on a runner.
- Bumped `actions/checkout` to v5 and `actions/setup-python` to v6, clearing the
  Node 20 deprecation warnings.
- `scan_filesystems()` and `_base_device()` take injectable inputs
  (`mounts_path`, `statvfs`, `rotational`, `sys_block`) so the filtering rules
  can be tested against synthetic input on any machine. No behaviour change.

## [0.0.1] - 2026-08-05

First public release. Previously a single-host private deployment, developed and
run on exactly one machine; see the warning at the top of the README.

### Core programs
- `timelapse_capture.py`: threaded snapshot daemon with drift-free wall-clock
  scheduling, atomic frame writes, throttled failure logging, a free-space guard
  with hysteresis, and an RTSP fallback path for cameras with no HTTP snapshot
  endpoint.
- `timelapse_encode.py`: nightly encoder with NVENC AV1 → HEVC → x264 fallback,
  automatic backlog recovery, per-camera failure isolation, correct full→limited
  range colour conversion, rsync transfer, and a Discord summary.
- `timelapse_test.py`: pre-flight checker for cameras, auth, encoders, disk
  headroom, transfer destination and webhook, plus `--probe-profiles` to find
  which ONVIF profile is actually the main stream.
- systemd units for the capture service, the encode service, and the nightly
  encode timer.

### Installation
- `install.sh`: one-command install. Detects the package manager
  (apt/dnf/yum/pacman/zypper/apk), installs dependencies, creates a `timelapse`
  system account, places the scripts, systemd units and a `timelapse` command
  wrapper, then runs the setup wizard and offers to enable the services.
  Supports `--unattended`, `--no-wizard`, `--ref`, `--prefix` and `--uninstall`.
  Works both from a git checkout and from a downloaded tarball.
- `timelapse_setup.py`: configuration wizard. Scans `/proc/mounts` for real,
  writable, local filesystems, reports free space and SSD/HDD status for each,
  and recommends the roomiest one that is not the OS disk. Every prompt takes
  Enter to accept its default. Also covers ffmpeg paths (reporting which encoder
  you will actually get), the capture interval, a disk budget for your camera
  count, cameras with a live reachability test, transfer and Discord.
- `timelapse` command wrapper: `setup`, `test`, `encode`, `config`, `logs`,
  `status`.

### Project
- MIT license, packaged documentation, and a generic
  `config/config.example.json`. The real `config.json` is gitignored.
- `--version` on every entry point.
- An end-to-end encode smoke test (`tests/smoke_test.py`) plus a CI workflow
  that runs it on Python 3.9 and 3.12 and shellchecks the installer.

### Notes
- The installer derives systemd's `ReadWritePaths=` from the storage chosen in
  the wizard. Getting this wrong by hand is the most common way an install
  fails, because `ProtectSystem=strict` turns it into a read-only error that
  looks nothing like a configuration mistake.
- Credentials that belong in a query string (Reolink-style URLs) are
  URL-encoded automatically. A password containing `&`, `#`, `=` or `%`
  otherwise breaks the URL in a way that presents as an auth failure.
- Neither the installer nor the wizard reads piped stdin for prompts: under
  `curl … | bash` that pipe is the script itself. Both use `/dev/tty`, and fall
  back to defaults when no terminal exists. `--stdin` opts in for scripted runs.

### Fixed before release
Found while reviewing the private codebase for publication:

- `timelapse_test.py` could not import the encoder module after the repository
  was reorganised into subdirectories, so `--probe-profiles` and the encoder
  check both failed with `ModuleNotFoundError`.
- A camera whose first frame failed to probe aborted the entire nightly run
  instead of failing that one camera-day. `probe_dimensions()` now runs inside
  the per-camera error boundary.
- A Discord webhook timeout raised out of `send_discord()` instead of being
  swallowed: `socket.timeout` is not a `URLError`. In the critical-failure
  handler this masked the original exception.
- Replaced the deprecated `datetime.utcnow()` with a timezone-aware timestamp.
- Replaced `os.uname()` with `platform.node()` in the failure reporter.

[0.0.9]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.0.9
[0.0.1]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.0.1

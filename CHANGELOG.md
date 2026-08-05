# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, the configuration format may change in any release.

## [0.0.6] - 2026-08-06

### Fixed
- **Pressing Enter at a yes/no prompt re-prompted forever.** `ask()` returned
  early only when the default was non-empty, and `ask_yes()` passes an empty
  default, so a blank line fell through to the retry loop. The only way past a
  `(Y/n)` prompt was to type `y` or `n` — which contradicts the wizard's one
  promise, that Enter accepts what is in brackets. Blank input now always
  returns the default.

### Changed
- **The transfer step no longer assumes SSH.** It asks how the destination is
  reached — a path on this machine, or another host over SSH — and only
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

  Real encodes were never affected — `encode_day()` already ends its filter
  chain in `format=yuv420p` — so this only ever cost people AV1 they could
  have had. The probe now pins `-pix_fmt` to the same `PIX_FMT` constant the
  filter chain uses, so the two cannot drift apart again.
- `encoder_hint()` recognises a pixel-format rejection and says so, instead of
  folding it into the generic "no capable devices" advice.

## [0.0.4] - 2026-08-06

### Added
- `timelapse test --encoders` — full diagnosis of why a hardware encoder is
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
- **Encoder probes discarded ffmpeg's error, so the wizard guessed the cause —
  and guessed wrong.** With `hevc_nvenc` working and `av1_nvenc` not, it stated
  "No AV1 NVENC on this GPU (needs RTX 40-series or newer)" — reported on an
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
- `transfer.require_mountpoint` — refuses to transfer when the destination is
  not on a mounted filesystem. An unmounted CIFS/NFS mountpoint is an ordinary
  empty local directory, so rsync would fill the local disk and
  `--remove-source-files` would then delete the originals. Accepts `true`
  (walk up from the destination) or an explicit mount path (checked with
  `os.path.ismount`, more precise). Off by default.
- `tools/setup-cifs-transfer.sh` — mounts an SMB/CIFS share, determines which
  rsync flags the share actually accepts, performs a real round trip with a
  throwaway file (verifying md5 and that `--remove-source-files` worked),
  writes the `/etc/fstab` entry with `nofail,x-systemd.automount`, and prints
  the exact config.json block and `ReadWritePaths` change needed.
- `timelapse_test.py` now warns when the transfer destination is not on a
  mount, and when `rsync_args` uses `-a` against a CIFS/NFS destination.

### Notes
- Whether `rsync -a` works on CIFS depends on the server and mount options —
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
  `writable_paths()` output, which is correct in all cases — the result goes
  into a systemd unit.
- `timelapse_setup.py` could not be imported on a non-POSIX host, because
  `os.statvfs` was evaluated as a default argument. Only affects running the
  tests off-target, but there is no reason to forbid that.
- `install.sh` exited `1` on success whenever it had not downloaded a tarball —
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
run on exactly one machine — see the warning at the top of the README.

### Core programs
- `timelapse_capture.py` — threaded snapshot daemon with drift-free wall-clock
  scheduling, atomic frame writes, throttled failure logging, a free-space guard
  with hysteresis, and an RTSP fallback path for cameras with no HTTP snapshot
  endpoint.
- `timelapse_encode.py` — nightly encoder with NVENC AV1 → HEVC → x264 fallback,
  automatic backlog recovery, per-camera failure isolation, correct full→limited
  range colour conversion, rsync transfer, and a Discord summary.
- `timelapse_test.py` — pre-flight checker for cameras, auth, encoders, disk
  headroom, transfer destination and webhook, plus `--probe-profiles` to find
  which ONVIF profile is actually the main stream.
- systemd units for the capture service, the encode service, and the nightly
  encode timer.

### Installation
- `install.sh` — one-command install. Detects the package manager
  (apt/dnf/yum/pacman/zypper/apk), installs dependencies, creates a `timelapse`
  system account, places the scripts, systemd units and a `timelapse` command
  wrapper, then runs the setup wizard and offers to enable the services.
  Supports `--unattended`, `--no-wizard`, `--ref`, `--prefix` and `--uninstall`.
  Works both from a git checkout and from a downloaded tarball.
- `timelapse_setup.py` — configuration wizard. Scans `/proc/mounts` for real,
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
  swallowed — `socket.timeout` is not a `URLError`. In the critical-failure
  handler this masked the original exception.
- Replaced the deprecated `datetime.utcnow()` with a timezone-aware timestamp.
- Replaced `os.uname()` with `platform.node()` in the failure reporter.

[0.0.1]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.0.1

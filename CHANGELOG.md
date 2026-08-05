# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, the configuration format may change in any release.

## [Unreleased]

### Fixed
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

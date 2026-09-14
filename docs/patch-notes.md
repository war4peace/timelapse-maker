# Patch notes

What was fixed in each version, one line per fix, in plain language. Newest version first. The full story of each change (how it was found, how it was tested) is in `CHANGELOG.md` and in the commit messages.

## Contents

- [0.2.1 - 2026-09-14](#021---2026-09-14)

## 0.2.1 - 2026-09-14

A reliability and safety release. Nearly everything in it came from a line-by-line review of the whole program on 2026-09-13, plus three problems found on the author's own machine while that review was being worked through. The biggest themes: passwords that could appear on screen, days of footage that could be lost, checks that reported success when something was wrong, and Windows install and service problems. It also adds many new tests of code that had never actually been run by one.

### New features and improvements

- Windows: ordinary user accounts can now open the finished videos without administrator rights
- Windows: the capture service really does restart itself after it fails, up to three times
- Testing an RTSP camera now says what went wrong: wrong password, wrong stream path, or the camera has no free connections
- `timelapse test --encoders` says when the NVIDIA driver has been updated and the machine needs a reboot
- Linux: once an hour, a warning in the log and one notification when the NVIDIA driver was updated but the old one is still loaded, so you can restart before the nightly encode falls back to the slow encoder; a second notification says when it is fixed
- `timelapse test` now exits with an error code when a check fails, so scripts and the installer can act on it
- Editing a camera in the wizard asks for the username and password separately, and never shows the password
- Typing `-` in the wizard clears an optional setting (ntfy token, web library folder, web index folder)
- Windows: `timelapse version` now says whether the running capture service is older than the installed files
- Linux: the capture, encode and watch services are now locked down as tightly as the web UI
- Only one encode can run at a time; a second one is refused and says which process holds the lock

### Fixes by importance: 4 Critical, 12 High, 36 Medium, 52 Low

Of these, 2 High and 1 Medium were found on the author's own machine, outside the review, and 1 Medium and 5 Low by test installs on Fedora and Arch Linux.

#### Critical

- Security: `timelapse test` showed the camera password on screen when a camera could not be reached
- Bug: A storage folder with a space in its name stopped both capture and encoding from starting on Linux
- Bug: The Windows `timelapse` command could not run from an ordinary window; this is now clearly documented as needing an Administrator window
- Bug: Two encodes running at the same time could destroy that day's frames and video

#### High

- Bug: Testing a camera with no terminal attached sent it about a thousand requests and then crashed
- Bug: A typo at the camera's Auth question silently deleted its stored password
- Bug: The first-run wizard let you add two cameras with the same name
- Bug: The Windows capture service never restarted after a crash, even though it was set up to
- Bug: The Windows share Test button said "successful" even when the password was wrong
- Security: Anyone on the network could make the web UI's index file grow without limit and fill the disk
- Tests: Running the test suite as root could install packages and mount a network share
- Bug: `timelapse test` reported success even when some of its checks had failed
- Bug: `sudo timelapse update` went ahead with the upgrade on Ctrl-C, or when there was no terminal to answer
- Bug: Stopping capture left RTSP cameras' ffmpeg running, which wrote duplicate frames and used up a camera connection
- Bug: A leftover check file on the NAS made every later destination test, and the nightly transfer, fail with "Permission denied"
- Bug: Testing an RTSP camera only said "Operation not permitted", so a working password looked wrong

#### Medium

- Bug: On Fedora, the installer reported ffmpeg as installed when it could not encode anything on a machine without an NVIDIA card, and the install ended without saying so; it now says so at both points and names RPM Fusion as the fix
- Bug: An interrupted encode could leave a half-written video that was then sent to the NAS as if it were complete
- Bug: `timelapse encode --date` could not encode a disabled or removed camera's frames, though the docs said it could
- Bug: Encoding today's date deleted the frames still being captured and skipped the rest of the day
- Bug: A frozen ffmpeg or rsync could block every nightly encode after it
- Bug: On Windows with local copies kept, the whole video library was copied to the share again every night
- Bug: On the two days a year the clocks change, RTSP cameras mixed frames from two different days
- Bug: When the clocks go back, the repeated hour played out of order and flickered
- Bug: A change to the system clock could make capture sleep for an hour, or save frames under the wrong date (even 1970)
- Bug: A snapshot address that was really a video stream could freeze a camera and use unlimited memory
- Security: Editing an RTSP or Reolink camera in the wizard showed its password on screen
- Bug: The wizard accepted a snapshot interval too long to ever produce a video
- Bug: Testing an RTSP camera with no ffmpeg set up crashed instead of saying ffmpeg was missing
- Bug: The web UI setup called an ordinary Windows drive path (like `D:\videos`) a remote address
- Bug: Running the wizard without permission to save made you answer every question, then crashed and lost the answers
- Bug: Some error messages suggested `timelapse setup`, which would have thrown away every configured camera
- Bug: Optional settings could not be cleared once set, though two prompts said leaving them blank would
- Bug: The web UI dropped the connection instead of playing a video when its index could not be written, for example on a full disk
- Bug: Windows `timelapse status` from an ordinary window called installed tasks "not installed"
- Bug: Windows `timelapse logs` said there was no log yet, and ignored a custom log folder
- Bug: `timelapse test` crashed for the second user account to run it, because of a shared temporary folder
- Bug: Uninstalling on Linux left the credential watch service and its timer behind
- Bug: Windows install permissions could fail on non-English Windows, which names built-in accounts differently
- Bug: Installing on Windows rewrote the system PATH so that entries like `%SystemRoot%` stopped updating
- Bug: Windows uninstall deleted the program even when its service could not be removed, and every install showed a false warning
- Bug: On Fedora the installer could fail over curl, and the reason a package failed to install was hidden
- Bug: The nightly log blamed another program for holding the NVIDIA GPU when the real cause was a driver update needing a reboot
- Tests: Some tests sent real camera discovery broadcasts onto the network of whoever ran them
- Tests: Nothing checked that update checks send the identification header GitHub requires
- Tests: The encoder's password masking in logs was never actually run by a test
- Tests: The web UI tests used gigabytes of memory and disk; they now use about 10 MB and run three times faster
- Tests: The low-disk pause and resume was never actually run by a test
- Tests: The capture loop itself was never run by a test; it now is, with a fake clock
- Tests: The Windows service code had no tests on any platform
- Tests: `timelapse test` was never run from start to finish by a test
- Tests: The automated build now installs and uninstalls on Windows too (its first run will be the release)

#### Low

- Bug: On a machine with no NVIDIA card, the setup told you to install NVIDIA encoding support; it now says that no NVIDIA driver is loaded, which is expected there
- Bug: The setup cut ffmpeg's error messages off in the middle of a word
- Bug: On a machine with no NVIDIA card, `timelapse test --encoders` ended with a page of graphics card advice, suggested an `apt` command on Fedora, and never said which encoder the nightly run would use
- Bug: The installer printed a raw internal line (`units changed: ...`) on every first install
- Bug: The setup accepted turning off the low-disk pause on the system disk, right after calling the space tight, without a word; it still accepts it, and now warns
- Bug: Setting `log_every_n_failures` to 0 silently stopped that camera for good
- Bug: One unreadable frames folder could stop the whole capture program
- Bug: If a day's frames folder was deleted during the day, capture failed for that camera until midnight
- Bug: Unusual characters in ffmpeg or rsync output could turn a successful encode into a reported failure
- Bug: `timelapse encode --date` on a day with no frames gave a misleading explanation
- Bug: The transfer sent any file with a dot in its name, not only videos
- Bug: A stray keypress in the terminal could stop an encode
- Bug: Removing a camera warned about frames that had already been encoded
- Bug: The wizard could loop forever when its input ran out
- Bug: Ctrl-C at a password prompt saved an empty password instead of quitting
- Bug: `timelapse cameras -t` crashed on a hand-written config, and called cameras with their own interval slow
- Bug: On Windows, a transfer destination typed as a plain folder name was saved without its full path
- Tidy-up: Unused wizard code removed and system-specific code moved to one place, which exposed a hidden bug
- Bug: A folder named like a video appeared in the web library as a video
- Bug: A web request containing a null character caused an error instead of an answer
- Bug: Signing in to the web UI from a page with non-English characters in its address caused an error
- Security: Logging out was a plain link, so any other website could log you out; it is a button now
- Bug: A video request with a backwards byte range was refused instead of getting the video
- Tidy-up: A duplicate style rule and a helper only the tests used were removed from the web UI
- Bug: On Windows, a service that would not stop reported the wrong error
- Bug: On Windows, starting a service that crashed straight away still waited the full thirty seconds
- Bug: Windows service replies were wrong for some requests, and scheduled task output garbled accented letters
- Bug: Windows `timelapse version` never compared the running service against the installed files
- Bug: On Windows, Ctrl-C printed a crash report, and `timelapse config` failed with some editors
- Bug: The Windows setup window could overwrite a videos folder you had placed yourself, and the network scan could hang
- Tests: Two tests could never fail on the automated build
- Bug: `timelapse test` with no ffmpeg path set printed four confusing lines instead of one clear one
- Bug: The "is not writable by" message named no account on Windows
- Bug: `timelapse update` could treat a pre-release as the newest version
- Security: `timelapse update` checked the downloaded installer less thoroughly than it claimed to
- Bug: A brand new config was missing some settings shown in the example file
- Bug: `timelapse test --camera` with an unknown name now lists the real ones; unused code removed
- Bug: On Arch, the installer refreshed the package list without upgrading, which can break the system
- Bug: `timelapse status` did not show the credential watch, though its help said it did
- Bug: The Linux installer accepted Python 3.8, though the program needs 3.9
- Bug: The Linux installer thought it had a terminal when it had none, and printed two errors per question
- Bug: `install.sh --ref` was silently ignored when run from a downloaded copy of the code
- Security: The Linux capture, encode and watch services now run with the same restrictions as the web UI
- Docs: The upgrade and manual install instructions were out of date
- Bug: Small Windows installer message mistakes, and it now accepts a machine with both an old and a new Python
- Tests: A test meant for the local folder option was actually testing the network share option
- Tests: The test suite now runs silently and tidies up after itself
- Tests: Tests no longer depend on today's date or on the machine running them
- Tidy-up: Stale comments and duplicate code removed from capture; a cadence change can no longer fire a frame twice
- Docs: File counts and changelog links corrected
- Bug: Linux service files are now written in one place, and unusual characters in paths are handled everywhere
- Tests: Shared test helpers moved into one file

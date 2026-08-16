# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, the configuration format may change in any release.

## [Unreleased]

### Added
- **`scripts/timelapse_platform.py`, the one file allowed to know which
  operating system it is running on.** **Nothing changes on a Linux install**:
  every path, every message and every command is byte-for-byte what it was, and
  the whole point of doing it this way was that the existing suite could prove
  that. What it changes is where the answers live. `/var/lib/timelapse/state`
  was spelled out in three scripts and `/etc/timelapse/config.json` in five, so
  "where does this project keep its things" had six copies and no owner; it now
  has one. The module answers a deliberately closed list, the same list twice:
  where the config and state live, whether a service is running, how to restart
  one, what to tell an operator to type, how to protect a file holding camera
  passwords, and which disks could hold frames. The storage scan moved into it
  from the wizard, and its tests moved with it.

  It is the first step of the Windows variant researched at 0.1.9, and it is
  the step that is worth having whether or not the rest is ever built. The
  standing rule that comes with it is that no other file may test the platform,
  and a test scans for that rather than trusting anyone to remember. The one
  exception is the capture daemon, which imports nothing from its siblings so
  that a syntax error elsewhere cannot stop a recording, and which therefore
  carries a pinned copy, held character-identical by a test, exactly as its two
  existing duplications already are.
- **CI now runs the test suite on Windows as well as Linux**, from this commit
  rather than at the end of the port. Half of what the new module answers
  cannot be reached from a Linux runner, and a branch no runner reaches is a
  branch that breaks quietly. The path derivation is written as a function
  taking the platform as an argument, so both platforms' answers are checked on
  both runners and neither leg is trusting the other to have looked.

### Fixed
- **Three more Windows-only defects, and one that is not.** **Nothing changes
  on a Linux install** except the last of them. First, the log file: the
  rotating handler works by renaming, and Windows refuses to rename a file
  another process has open, which is exactly what happens when anything reads
  `capture.log` while the daemon rolls it over. That failure is quieter than a
  crash, because Python's logging catches it, prints the whole traceback and
  carries on, so the daemon would emit a traceback per log line and never
  rotate at all. On Windows the log is now one file per day, named rather than
  renamed and pruned by age, so there is nothing left to refuse; on Linux it is
  the same `capture.log` it has always been. Second, camera names that Windows
  treats as hardware rather than as files, of which only `NUL` actually
  affects this tool: a camera called that records nothing whatsoever. Those
  names are now refused when you type them, on every platform, because a
  config file is meant to be portable between them.
- **The pre-flight now checks camera names**, which is the part that is not
  Windows-only. `timelapse test` reports two cameras whose folders the
  filesystem cannot tell apart. On Linux that means an exact duplicate, which
  a hand-edited config can hold and which quietly makes one video out of two
  cameras' pictures; on Windows it also means two names differing only in case,
  which is the more likely one, and the wizard has always refused it but a
  config carried over from a Linux machine has not been through the wizard.
- **Two defects that only appear when the scripts are run on Windows.**
  **Nothing changes on a Linux install**, which is every supported deployment
  today; these were found while researching a possible Windows variant and are
  fixed now because they are cheap and because they are the kind of thing that
  is much harder to find later. First, the atomic write used for every state
  file and every captured frame renames a temporary file over its destination,
  and Windows refuses that rename while any other process has the destination
  open, so a web UI page load landing in the same millisecond as a daemon's
  write would lose the write. It now waits the reader out and still reports a
  genuine permission problem. Second, the wizard's bind check reported a port
  as free when something was already listening on it, because the option it
  sets to match the server means something different on Windows; it now asks
  for exclusive use where that applies. The second had been recorded as a
  skipped test rather than a bug: the service is Linux-only, but the wizard
  runs wherever it is run, and a platform difference that makes a check lie is
  a defect in the check.
- **A disabled camera's row in `timelapse cameras -l` no longer collides with
  the next column**, printing `no5s/60` where an enabled camera prints
  `yes 5s/60`. The state is dimmed for a disabled camera, and a format width
  counts the escape codes that dimming adds, so padding an already-coloured
  string added nothing. Padding now happens before colouring. Present since
  0.1.8 and reported from a real terminal at 0.1.9; it was invisible to the
  test suite and to CI because colour is disabled when stdout is not a tty,
  which is every automated run. The test that covers it forces colour on and
  asserts that the cadence column starts at the same offset whether or not the
  camera is disabled, rather than checking for one bad string.

## [0.1.9] - 2026-08-15

### Added
- **Optional motion smoothing, per camera.** A camera carrying `smooth_frames`
  has that many neighbouring frames averaged into each output frame when it is
  encoded, which calms the shimmer of wind in trees. That shimmer is most of
  what makes a timelapse look like it is jumping: at one frame every 5 seconds
  the leaves are in a different place in every frame, and averaging settles
  them. The wizard offers it when you add or edit a camera, defaulting to no,
  and to 15 frames once you say yes; any value from 3 to 30 is accepted.
  `timelapse cameras -l` shows it as `+15` beside the cadence, and
  `timelapse test` reports it.

  **It is off unless a camera asks for it, and there is no global setting**,
  which is deliberate and unlike `interval_seconds` and `framerate`: those
  fall back to a default so an untouched camera still follows it, whereas
  smoothing suits a wide view of foliage and spoils a doorway. **Upgrading
  changes nothing**, because every existing config is one where no camera
  says anything.

  Two things worth knowing before turning it on. It cannot help with cars or
  people: anything crossing the frame is captured once, so averaging softens
  the flash into a brief ghost but cannot make it fluid, and nothing else can
  either. And a camera's burnt-in clock will blur its fastest-changing digits,
  because they differ in every frame being averaged.

  This is encode-time only: capture, frame counts, coverage and video length
  are untouched. **Turn it on during the day and that same day is smoothed**,
  because the nightly run encodes the day that has just finished and reads the
  config as it stands then. Unlike a cadence change, which is pinned to what a
  day was actually captured at, there is nothing to wait for. Days already
  encoded are not redone without `timelapse_encode.py --force`.

### Changed
- **The Library tab now tells you how to fix a remote destination**, instead of
  only saying that browsing is unsupported. A `transfer.destination` of
  `user@nas:/path` is not a path this host can read, and the two supported
  answers are named on the page: mount that share and set `web.library_root`
  to the mount point, or turn transfer off and keep the videos local. A
  mounted share is an absolute path, so it browses normally and always has.
  Browsing an SSH-only destination over SFTP has been **refused** rather than
  left as a to-do (see `docs/decided-against.md`): it would give the one
  network-facing service a second outbound connection, over SSH, to the host
  holding every video, and mounting the share costs a line in `/etc/fstab`.
  Only the wording changes; a readable library path is now a stated
  prerequisite rather than a pending feature.
- **The Overview is ordered by how often you need it**: Cameras, Last encode,
  Services, Version. The page had grown in the order the panels were built,
  which put the version number above the cameras.
- **Where the video library lives is now shown on the Library tab**, at the
  head of the panel that already answered every other question about it,
  instead of in its own section on the Overview. A library that cannot be read
  still says so on the Overview, with a link to the tab.
- **Every column in the web UI's tables now starts at the same edge.** The
  counted columns (Frames today, Coverage, and most of the Library tab) were
  right-aligned while their own headers, and every other column, were
  left-aligned.
- **Every timestamp in the web UI is now shown in the same format**,
  `2026-08-15 16:43:21`. The page draws on three sources and each had its own
  idea of what a timestamp looks like: the capture and encode heartbeats gave
  `2026-08-15T16:43:21`, systemd gave `since Sat 2026-08-15 16:42:21 EEST`, and
  the library index gave a time with no seconds. The weekday and the timezone
  are gone with it: the weekday follows from the date, and the timezone is the
  server's own on every row. The `/status` and `Recent log` pages still hold
  command output exactly as it came back, which is what makes them worth
  pasting into a bug report.

## [0.1.8] - 2026-08-15

### Changed
- **The web UI now counts today's frames from disk, and shows coverage per
  camera.** The Cameras panel used to report a counter that reset every time
  the capture service restarted, which said nothing about whether today had
  actually been captured, and showed nothing at all for RTSP cameras. It now
  counts the files in each camera's folder for today and shows that beside a
  **Coverage** percentage measured against the cadence that camera is running
  at. Under 100% means frames are missing, including any part of today before
  capture was started.

  RTSP and HTTP cameras are counted the same way, so the column that read `-`
  for an RTSP camera now shows real numbers.

- **"Last frame" now means the same thing for every camera.** An RTSP camera
  used to report that it was being supervised, where every other camera
  reported when its last frame arrived; it now reports the age of its newest
  frame, like the rest. The failure column is also left empty when there is
  nothing wrong, rather than saying "0 failed" on every healthy row.

  This shows a fault the page could not previously report: an RTSP camera
  whose recorder is running but producing nothing looked healthy, and now
  reads as what it is.

- The Cadence column reads **"5s / frame"** rather than "1 / 5s", which could
  be read as one fifth of a second just as easily as one frame every five
  seconds.

- **Log out no longer looks like one more tab.** It sat immediately beside
  "Recent log", the two sharing the word "log", which made it easy to end your
  session while reaching for the log. It is now set apart from the tabs and
  coloured as an action.

### Fixed
- **The credential watch row now says when it next runs.** It read "Scheduled"
  with an empty Detail, which looked like something was wrong with it. It runs
  on a five-minute interval rather than at a fixed time of day, and systemd
  reports those two kinds of schedule in different places; only one of them was
  being read. Timer rows also say when they last ran.

## [0.1.7] - 2026-08-14

### Added
- **The wizard can find your cameras for you.** Adding a camera now offers a
  scan of the local network; ONVIF cameras answer with their address and model,
  and the vendor template is preselected from what they report. Also available
  on its own as **`timelapse discover`**, which needs no root, writes nothing
  and sends no credentials, so it cannot lock a camera account.

  Typing an address by hand is unchanged and always works. Discovery uses
  multicast, which does not cross subnets or VLANs, so cameras on a separate
  network will not appear even though they work perfectly; the wizard says so
  rather than reporting "no cameras found".

- **The web UI can listen on an IPv6 address.** `web.bind` accepts `::1`, `::`
  or any address this host holds, and the wizard offers them. `::` accepts IPv4
  connections as well, so it is the IPv6 answer to `0.0.0.0`. Nothing changes
  for an existing install: the default bind is unchanged.

  This was a real trap rather than a missing feature. The wizard's bind check
  probes the address for real and reported an IPv6 one as usable, and the
  service then refused to start with `Cannot listen on ::1:8787`.

### Fixed
- **RTSP cameras now capture. They never have.** A camera added with the
  "RTSP only" type passed its test, was written to the config, and then
  captured nothing: the capture log filled with `Could not open file` and
  ffmpeg restarted every ten seconds for ever. The command asked ffmpeg to
  create each day's directory using an option that only one of its other
  output formats supports, and ffmpeg neither used it nor complained. The
  daemon creates the directory itself now.

  This has been broken since the first release. It went unnoticed because the
  test both the wizard and `timelapse test` run grabs a single frame into a
  directory that already exists, so it proved the camera was reachable
  without ever exercising the way frames are really written. That test now
  runs the daemon's exact command.

  Nothing to do beyond upgrading. If you have an RTSP camera configured, it
  starts working when capture restarts; days it missed are gone, since no
  frames were ever written. Reported from a real 0.1.6 install.
- Addresses printed by the installer, the wizard and the web UI are bracketed
  when they are IPv6, so the URLs they offer can be opened. The one that
  mattered was inside `.m3u` playlists generated when a player sends no usable
  `Host` header: those contained `http://::1:8787/...`, which no player can
  open.
- **A camera reachable only over IPv6 can now be added with the wizard's
  vendor presets.** Typing an IPv6 address at "IP address or hostname" built a
  URL whose colons were read as a port number, so the camera could not be
  fetched. The address is bracketed automatically now, and a link-local one
  (`fe80::`) is flagged as needing a zone id. Capture itself always worked over
  IPv6; if you already added such a camera by hand with the "Custom URL"
  option, nothing changes and nothing needs redoing.

### Changed
- **Upgrading no longer asks four questions.** Re-running the installer, or
  `sudo timelapse update`, used to ask whether to reconfigure, whether to run
  the pre-flight, whether to enable capture and the nightly encode, and whether
  to enable the web UI. Every one of those was a decision already made, and
  visible to the installer: it now records which units are enabled and which
  are running *before* it touches anything, and puts exactly that back
  afterwards.

  What was enabled stays enabled, what was running stays running, and anything
  deliberately switched off stays off. One line per unit is printed so you can
  see it happened, and a unit that was running before and is not running after
  is reported as an error rather than passing as success. Reconfiguring is a
  separate job with its own commands (`timelapse setup`, `timelapse config`),
  and the pre-flight is still there as `timelapse test`.

  A first install is unchanged: the wizard runs, and it still offers the
  pre-flight and the services at the end.

  The restart of a running daemon is also no longer a question. Declining it
  left the old build serving while every version number claimed otherwise,
  which is the failure this rule exists to prevent. It costs a second or two of
  frames; stop capture before upgrading if you would rather not pay even that,
  and it will be enabled and stopped afterwards, exactly as you left it.

- A unit introduced by a release is adopted automatically on upgrade, but
  **only if it is a timer**. A new *service* is never switched on for you,
  which is what keeps the web UI opt-in.

## [0.1.6] - 2026-08-14

### Fixed
- **A camera that rejects our credentials is no longer hammered with them.**
  The capture daemon presented the configured credential every
  `interval_seconds`, for ever, whatever the camera answered. Firmware commonly
  locks an account after a handful of failed authentications, so this renewed
  the lock faster than it expired: rotate a camera's password without disabling
  it here first, and the account stayed locked until the daemon was stopped.
  Entering the correct password on the camera did not clear it, because this
  program was still holding the door shut, and camera accounts are usually
  shared with an NVR or another consumer, so the damage was not confined to
  timelapse-maker.

  Failed fetches are now classified as `auth`, `unreachable` or `other`, and
  only a refusal changes anything. On one, the daemon tries once more, then
  withholds fetches for ten minutes, tries a single time, and from then on
  tries once every 31 minutes for ever. That last number is the observed
  30-minute lockout window plus a margin: one attempt per 31 minutes cannot
  reach the "N failures inside a window" threshold that lockout policies are
  built from. Recovery is automatic and immediate, so fixing the password needs
  no restart.

  A refusal is only a refusal when the camera says so: HTTP 401 or 403, or a
  Reolink `rspCode` of -6 or -7. The measured -9, "not support", arrives in the
  identical 200-with-an-error-body shape and is an unknown *command* rather
  than a rejection, so unrecognised codes are logged and treated as ordinary
  failures. An unreachable camera never backs off, since retrying it costs
  nothing and it should recover the instant it returns.

### Added
- **Optional notification when a camera is refusing our credentials**, and
  another when it stops. This is the one camera fault an external uptime
  monitor cannot see, because a monitor holds no credentials: the camera is up,
  answering, and rejecting only us. One message per incident, one all-clear,
  never a repeat; the message states what was observed rather than diagnosing,
  because a camera that has locked the account rejects a *correct* password
  too. Off if you have no notification sinks configured, and controlled by
  `capture.notify_auth_failures` (default true) if you do.

  It runs as a new `timelapse-watch.timer` every five minutes, reading the
  capture heartbeat and sending through the existing sinks. The capture daemon
  still makes no outbound connections of its own, which is what the runtime
  state file was for. Existing installs pick the timer up automatically on
  upgrade.

- `capture.json` now carries a per-camera `error` object: the class, when it
  started, how many ticks it has held, the camera's own words (redacted) and
  whether the back-off has confirmed it. The web UI's overview uses it to say
  "refusing our credentials" instead of leaving a camera merely silent.

### Fixed
- **A day whose frames are kept is no longer encoded again every night.**
  Nothing in the project recorded that a day had been encoded: deleting the
  frames *was* the record, and it works, which is why this went unnoticed for
  five releases. Set `encode.delete_frames_on_success: false` and that record
  disappears with it, so every night the encoder found the same days still
  sitting there and re-encoded the newest `max_backlog_days` of them from
  scratch, then re-transferred the results. On a seven-camera install that is
  forty-nine camera-days of GPU work nightly to produce videos that already
  existed, and nothing in the log said "again".

  A successful encode now writes a small `.encoded.json` into the day
  directory, naming the video, the frame count, the encoder and the time, in
  the manner of the existing `.cadence.json`. Days carrying one are skipped and
  counted, and the count is reported rather than left silent, because a run
  that legitimately does nothing should not look like a broken one.

  **Nothing changes if you use the defaults**: frames are deleted on success,
  so the marker is written and removed a second later. The marker cannot be
  inferred from the video file instead, which is the obvious alternative:
  `transfer()` *moves* the video to the NAS, so by morning the output
  directory is normally empty and every day would look unencoded again.

### Added
- **The nightly summary can go to ntfy and Telegram, as well as Discord, and
  to any combination of them at once.** Configure it with `sudo timelapse
  notify`, which offers a test message for each; `timelapse test` then checks
  every configured sink.

  ntfy needs no account: pick a topic on `ntfy.sh` and subscribe to it, or
  point it at your own server. Telegram needs a bot token from `@BotFather`
  and a chat id. Neither sink can fail the run it is reporting on, and one
  being down does not stop the others.

  **Nothing changes if you use Discord.** The existing `discord` block in your
  config keeps working exactly as it did, and is what gets used until you run
  `timelapse notify`. Then it moves into a `notify` list that can hold several
  sinks, and the old block is switched off rather than left to look configured
  while being ignored.

  Email was considered and left out deliberately: it is a different job with a
  different failure mode (relays, SPF, spam folders), and the two sinks above
  cover phone notifications without any of it.
- **The web UI can now tell you whether your cameras are actually answering.**
  Two new panels on the Overview: one row per camera with the time its last
  frame landed, its cadence, its frame count and its failures; and what last
  night's encode did, with a row per camera showing frames and coverage.

  This is the question `systemctl` structurally cannot answer. A capture
  daemon whose cameras are all refusing connections is "running", and so is
  one that has paused itself because the disk filled up. Both look perfect in
  the Services table and neither is capturing anything. The disk-guard pause
  now says so in as many words.

  Behind it, the two daemons publish what they know into
  `paths.state_dir` (new, default `/var/lib/timelapse/state`): capture rewrites
  `capture.json` once a minute, and the encoder appends to `encode.json` at the
  end of every run, keeping a fortnight. They are plain JSON and versioned, so
  anything you want to write can read them.

  They publish facts and not verdicts: there is no "healthy" field anywhere,
  because whether 42 seconds of silence is a fault depends on that camera's
  interval, and the page can work that out while a file that had already
  decided could not be argued with. RTSP cameras report what they actually
  know, which is process restarts and liveness rather than a last-frame time:
  ffmpeg writes those frames, not us.

  **Upgrading creates the directory for you.** It has to exist before the
  services start, because it is named in their `ReadWritePaths` and systemd
  will not start a unit whose `ReadWritePaths` points at nothing. Your existing
  `config.json` needs no edit: the key is read with a default like every other
  key added since 0.1.0. Both panels say plainly that they need the 0.1.6
  services, so the gap between upgrading and restarting them reads as a
  version skew rather than as a fault.
- `timelapse encode --force` re-encodes days that are already marked. `--date`
  keeps overriding the marker on its own, so re-doing a single day by hand
  needs no new flag.
- `timelapse test` checks the state directory exists and is writable, since a
  missing one stops both daemons with an error that names neither the
  directory nor the release that added it.

## [0.1.5] - 2026-08-12

### Added
- **An optional login for the web UI.** `timelapse web` asks for a username
  and a password; leave it off and everything behaves exactly as it did. Turn
  it on and the Overview, Library, logs and status pages ask for it first. The
  session lasts until you press **Log out**, and expires by itself after 30
  days idle.

  What it is for, said plainly here because it is said plainly in the UI too:
  keeping a household, or a guest on your wifi, out of your video index. It is
  a lock on a door, not a safe. There is no HTTPS, so the password crosses your
  network in clear.

  **The video files stay reachable without it**, and that is deliberate. VLC is
  a separate program with no access to your browser's session, so gating the
  files would break every *Play* link and stop a playlist you saved last month
  from working the moment you logged out. The pages, the camera names and the
  day groupings are behind the login; a request for a file's exact address is
  not.

  The password is stored only as a PBKDF2-SHA256 hash (600,000 iterations,
  per-hash salt), so it cannot be read back out of `config.json`: if you forget
  it, see the new command below. Hashing is right here for the same reason it
  is wrong for the camera passwords: this one is *verified*, and those have to
  be *presented* to a camera. Sessions are held in memory only, so nothing new
  is written to disk and a service restart logs everybody out.

  A wrong password costs three seconds. **Attempts are never capped and
  nothing is ever locked out**, because three seconds is plenty against
  somebody guessing at a keyboard, and a locked account would mostly succeed
  at infuriating whoever mistyped their own password.
- **`sudo timelapse password`**: set or change the web UI's login, and nothing
  else. A username, a password, twice, done, and the UI restarts.
  `sudo timelapse password --disable` removes it again. That one asks nothing
  at all, since it needs no password to carry out and is undone by running the
  command again, so it works unattended and in a script; it is idempotent, and
  `--enable` exists as a synonym for the bare command.

  The login cannot be turned on or off from the web UI itself, deliberately.
  That would need write access to `config.json`, and the one structural
  property this service has is that it writes exactly one directory: its own
  index.

  It never asks for the old password. The command needs root to write the
  config at all, and root can already read every camera password in that same
  file, so the question would prove nothing while locking out the one person
  entitled to fix a forgotten login. There is nothing to recover either, since
  only the hash is stored: forgetting the password is one command, not a
  problem.

### Fixed
- **`timelapse config --redacted` printed a stored password hash in full.**
  The key-name rule anchored on `pass(word|wd)?$`, so `password_hash` went
  straight through. It is offline-crackable and the entire use for that dump
  is pasting it somewhere public. Found while designing the web UI login, so
  no released version has ever written such a key; the rule is fixed anyway,
  because the next one like it should not have to be found twice.
- **The web UI printed the two version numbers in two different shapes**:
  "Installed 0.1.4" beside "Latest v0.1.4". The `v` is the git tag's, not the
  version's, and on two adjacent rows of one list it read as a difference
  between the values rather than as punctuation in one of them. Both now use
  the normalised form. The terminal still prints the tag, where it names a ref
  the installer will fetch.
- **After an upgrade run from the terminal the panel contradicted itself**,
  reporting "Installed 0.1.4 / Latest 0.1.3" until the next daily check.
  Upstream cannot be behind what is installed here, since the tag is where the
  installer got it, so a cached answer older than the running version is out
  of date rather than a finding about GitHub: the panel now reports the
  version it can prove exists, and drops the stored tag and URL with it so a
  link never labels one release and opens another. The cache file is left
  alone, and "Last successful check" still says how old the answer is.
- **"Last encode run" read "Starting" for the whole of the nightly encode.**
  A `Type=oneshot` service is `activating` for as long as its `ExecStart`
  runs, which here is twenty minutes and more, and the word for a daemon
  caught mid-boot describes a job that never got going when it is held for
  that long. It now reads "Running", with the time the run began.

## [0.1.4] - 2026-08-11

### Added
- **`timelapse config --redacted` prints the configuration with the
  credentials taken out**, for pasting into a bug report. "Here is my config,
  why will camera 3 not connect" is a thing people do, and that file holds
  every camera password; asking them to redact it by hand puts the guarantee
  in the least reliable place available, and the shape of the secret is not
  obvious anyway, because a Reolink URL *is* the credential.

  A config hides its secrets in two shapes and each pass misses the other, so
  it does both: `"password": "x"` has no `=` in it for the text rule to find,
  and a Reolink `url` buries the credential in a query string where a rule
  that knew only field names never looks. Masked: passwords under any key
  name, credentials inside HTTP and RTSP URLs, and the Discord webhook token.
  Kept: hostnames, usernames, paths, the transfer destination and the
  webhook's numeric id, because those are what a fault report is about.

  What was masked travels *inside* the dump as a `_redacted` key, since the
  moment it matters is the moment somebody pastes the text into an issue, and
  a warning printed beside the JSON is exactly the part that does not get
  pasted. JSON goes to stdout and the prose to stderr, so
  `timelapse config --redacted > report.json` still produces a file that
  parses.

### Changed
- **Service status folded into the Overview, so the web UI has three tabs.**
  Once that page was four rows rather than a screen of `systemctl status`
  output, it no longer justified a quarter of the navigation, and "is it
  running" belongs next to "where are my videos". *Technical data* links to
  the full output, which stays a page of its own so that an old
  bookmark still lands somewhere useful, and so that the Overview shells out
  once per view rather than twice.
- **"Last encode run" says Successful, in green, rather than Idle.** A
  finished oneshot and one that has never run are both `inactive`; the
  timestamp is the only thing separating them, so a row that already knew the
  encode finished at 00:42 was reporting it in the same words and the same
  colour as a machine that has never encoded anything. It now reads
  *Successful* with the time, *Not yet run* when there is no timestamp, and
  *Failed* if systemd recorded a bad result.
- **The web UI links to the release notes instead of printing them.** The
  "What is new" panel showed the release body as plain text, so its markdown
  arrived intact: `## Camera passwords were being written to the log`,
  backticked commands, and fenced blocks, which reads as this program having
  failed to format something rather than as formatting. Rendering markdown
  properly would mean either a dependency or a parser to maintain, for a
  paragraph nobody reads twice; GitHub already renders it one click away. The
  panel now carries that link, and **it opens in a new tab** rather than
  replacing the page you were on. `sudo timelapse update` still prints the
  notes in the terminal, where plain text is the native format.

  Two consequences worth having: the update cache no longer stores the release
  body, and the changelog fetch that used to fill the panel when a tag had no
  Release behind it is gone, so the web UI's single outbound request is now
  always a single request rather than usually one.

### Fixed
- **`timelapse config` no longer lets an editor leave the config unreadable by
  the daemons.** Editors that save by writing a new file and renaming it over
  the old one (vim with `backupcopy=no`, and every `sed -i`-style tool) leave
  root's umask on the result. The mode widening to 0644 is unwanted, but
  losing the `timelapse` group is worse: the services then cannot read their
  own configuration and nothing says so until the next restart. The command
  now re-asserts 0640 root:timelapse after the editor exits, which is why it
  no longer `exec`s it. The editor's exit status is still the command's.

  It also sets `umask 0077` first, so an editor that creates its backup or
  swap file from the umask rather than from the original's mode creates it
  private. Measured, and worth stating precisely: vim copies the *original's*
  mode onto its backup, so `config.json~` comes out 0640 root:timelapse
  whether or not the umask is set, in `/etc/timelapse` or in a `backupdir`
  elsewhere. That is the same exposure as the config itself rather than a new
  one, so this is defence against editors that behave differently, not a fix
  for an exploitable hole in vim.

- **The pre-flight during `sudo timelapse update` no longer reports a working
  share as broken.** It ended with:

  ```
  FAIL  rsync -a --partial --remove-source-files fails against /mnt/cctv/TL/:
        exit 1: runuser: may not be used by non-root users
  ....  no flag combination worked; check the share permissions for timelapse.
  ```

  Nothing was wrong with the share. The installer runs the pre-flight *as the
  service account* on purpose, so that permission problems surface then rather
  than at 00:05 tonight; the rsync probe then ran `runuser` a second time from
  inside that unprivileged process, and the nested "may not be used by
  non-root users" was printed as rsync's verdict.

  `probe_as()` now works out how to reach that account before running
  anything: already it (run directly, which is both the reported case and the
  authoritative one), root with `runuser` (wrap), or neither (decline, and
  name the command that would work). `try_rsync_args()` returns `None` for
  "could not be tested" separately from `False` for "tested, and it does not
  work", with the reason attached. That also fixes a host with no `runuser`
  installed, where the probe previously ran unwrapped and reported a result
  for the wrong account entirely.

  Fourth instance of one shape in this project, after the update checker
  writing `checked` on a failed poll and `sync_unit_readwritepaths()` returning
  a count: **"could not check" collapsed into "checked, and it is broken"**.

## [0.1.3] - 2026-08-11

A security release. Camera passwords were reaching the log, and from there the
web UI's log page; if you have run any earlier version, rotate the camera
password after upgrading, because this fix cannot unwrite what is already in
your journal. The rest is what the first days of 0.1.2 on a real deployment
turned up: two checks that reported healthy systems as broken, and four places
where the UI said less than it should have.

### Security
- **Camera passwords no longer reach the log, and the web UI no longer shows
  the ones already in it.** A failed snapshot logged the exception `requests`
  raised, and that exception's text carries the URL it was fetching. For a
  Reolink-style camera the URL *is* the credential, so a single 502 wrote the
  password to journald and to `capture.log`, and the web UI's log page then
  served it to anyone who could reach the page.

  Four shapes are masked now: `password=` and seven other spellings in a query
  string, `rtsp://user:pass@host` userinfo (ffmpeg quotes the URL back at you
  in its own errors), and Discord webhook tokens. The masking is a logging
  *formatter*, not a rule about how to write log calls: the leak came from a
  call that never mentioned a URL, so nothing at the call site could have
  known. Uncaught exceptions from a camera thread, which bypass logging
  entirely and print to stderr, were a second route and now go to the log too.
  The web UI redacts command output at the source, which is what covers the
  entries already in your journal.

  **If you ran any earlier version, treat the camera password as exposed.**
  Upgrading stops new leaks; it cannot unwrite the old ones. After upgrading:

  ```
  sudo timelapse update
  sudo timelapse cameras            # set a new password on each camera
  sudo rm -f /var/lib/timelapse/logs/capture.log*
  sudo journalctl --rotate && sudo journalctl --vacuum-time=1s
  ```

  The `journalctl` pair discards the whole journal, not just these lines;
  there is no way to delete selected entries. Anyone in `systemd-journal` or
  `adm` could read them, and so could anyone who could reach the web UI, which
  matters most if you moved `web.bind` off `127.0.0.1`.

### Changed
- **The web UI's Service status page says whether it works, in four words.**
  It was `systemctl status` verbatim: an invocation ID, a cgroup path, a PID,
  a task count and the same documentation URL four times over, to answer a
  question that fits on one line. It is now a row per service, in plain words,
  with what to do about it when something is wrong. The full output is one
  click away under *Everything systemd knows*, because when something *is*
  wrong that is what a bug report needs. Each row says what "not running"
  means for that unit: the nightly encode is a oneshot that sits inactive for
  23 hours 22 minutes a day, and reporting that as "Stopped" would invent a
  fault on a healthy system every time anybody looked. A crash loop reads as
  one, and a service that is running but not enabled says it will not come
  back after a reboot.
- **The page header dropped the word "web"** from `timelapse-maker web 0.1.2`.
  There is no way to be reading that page other than through the web UI.
- **The version panel renders both numbers the same way.** Installed was a
  `<code>` and Latest was not, so a two-row list showed one version number in
  two different fonts, which reads as a rendering fault. Colour still marks
  out an available update, which is the only difference that means anything
  there.

### Fixed
- **`timelapse transfer` no longer tells you to fix something that is already
  right.** It ended with "Add the destination to ReadWritePaths= in
  timelapse-encode.service by hand, or ProtectSystem=strict will fail the
  write read-only. (Run as root to do this automatically.)" when run as root
  against units that were already correct, which is the ordinary case since
  the installer writes them on every upgrade. `sync_unit_readwritepaths()`
  returned the number of units it *rewrote*, and the caller read anything
  falsy as failure, so "nothing to do" and "could not do it" were the same
  answer. It now reports which of six things happened and only warns on the
  two that are actually wrong.
- **The pre-flight measures the rsync flags instead of guessing at them.** It
  warned that a CIFS destination with `-a` in `rsync_args` would exit 23 every
  night. `-a` does imply `--owner --group`, and a share often cannot set them,
  but whether it fails depends on the server and the mount options; on a real
  share it does not, so a working configuration was reported as broken. The
  check now copies one file with the exact flags the encoder will use, as the
  account it runs as, and says which flags would work when they genuinely
  fail. `probe_rsync_flags()` moved to `timelapse_encode.py`, next to the code
  that runs rsync nightly, so the wizard and the pre-flight share one answer.
- **The nightly Discord table no longer wraps.** Discord renders an embed's
  description in a column narrower than an ordinary message, and the table was
  a fixed 62 wide, so its last field folded onto a second line underneath the
  first and the summary read as broken. The date moved out of the column set
  and became a heading, since a run almost always encodes one day and the
  column spent ten characters repeating one value; a catch-up run after an
  outage gets a block per day instead. The remaining widths are measured from
  the content, which also fixes an encode over an hour knocking every column
  after it out of line: `1h 02m 03s` never fitted the fixed 8. The same seven
  cameras now come out at 39 columns rather than 62.

## [0.1.2] - 2026-08-10

A feature release, and the first with a real upgrade path: `sudo timelapse
update` replaces the three-line curl recipe. The largest change is that
capture cadence and frame rate stop being global. Any camera can run on its
own, so a wide courtyard view at one frame a minute and a workbench at three
seconds can share a host, and a change to either takes effect at the next
midnight so a day is never half one rate and half another.

### Added
- **`sudo timelapse update`.** Asks GitHub for the newest release, shows what
  changed, and installs it after one confirmation. It keeps your
  configuration, your captured frames and your videos, and restarts the
  services, because underneath it is the same re-run of the installer that
  has always been the supported upgrade. It downloads `install.sh` **from the
  tag it is about to install**, so the installer and the tree it unpacks are
  the same version, and it checks that what came back is really the installer
  and is valid bash before running it as root.
  `timelapse update --check` reports without installing and is the one command
  here that needs no root at all; it exits 10 when an update is available, so
  a cron job can notify on it. Also `--yes`, `--force` and
  `--ref v0.1.0` for pinning to a specific tag or going back to one.
- **The video frame rate is a wizard question.** `encode.framerate` has been
  in the config since the beginning and is read with a default of 60, but the
  wizard set it and never asked, so the only way to change it was to edit the
  JSON by hand. It is now asked in the Capture section, which shows what each
  rate means for the finished video: the same day's frames are 4:48 at 60fps
  and 9:36 at 30. `encode.gop` is derived from it rather than asked, so a
  keyframe stays two seconds apart at any rate instead of drifting to four
  seconds at 30fps. Existing configs are untouched.
- **Per-camera capture interval and frame rate.** `capture.interval_seconds`
  and `encode.framerate` remain the defaults, and a camera may now carry
  either key itself. A wide courtyard view is fine at one frame a minute
  played at 30fps; a workbench wants three seconds. Set them with
  `sudo timelapse cameras -e:NAME`, which offers the current effective values
  and stores nothing when you answer with the global one, so a camera nobody
  has pinned still moves when the global setting changes.

  **A change takes effect at the next midnight**, so one day is always one
  video at one cadence. Each day directory records the interval and frame rate
  it began at, in a dotfile no frame count sees, and both the daemon and the
  encoder obey that over the config. So a restart at 14:00, a crash or a power
  cut all leave today alone, and there is nothing to time: change it whenever
  you like. It also keeps `Cov%` honest, since tonight's encode is of a day
  that ran on the previous settings.

  Everything downstream follows: the capture thread runs on that cadence (HTTP
  and RTSP alike), the encoder uses that frame rate and derives the keyframe
  interval from it, `Cov%` in the nightly summary is measured against the
  camera's own interval, and `timelapse test` grew a **Cadence** section
  showing what each camera will produce. The fetch timeout is clamped below
  whichever interval applies rather than becoming a third setting. The disk
  projection sums per camera instead of multiplying, since they no longer
  share a cadence. `timelapse cameras -l` shows the effective cadence and
  marks the cameras that are not following the defaults.
- **Rotating config backups, and `sudo timelapse restore`.** A backup is taken
  before every change, five deep, named for when it was taken. `restore` lists
  them with the date, the size and what is actually in each one (camera count,
  interval, frame rate), and puts the one you pick back. It backs up the
  current config first, so a wrong choice is undone by running it again. It
  also works when `config.json` is corrupt or missing entirely, which is when
  you need it. `timelapse restore -l` lists without restoring.
  `timelapse config` takes a backup before opening `$EDITOR`, since that is
  the one write path the wizard does not own.
- **Shortcuts for `timelapse cameras`.** Every action the menu offers is now
  also a flag, so a single change is a single command: `-l` lists, `-a` adds,
  and `-e:CAM`, `-x:CAM`, `-t:CAM` and `-r:CAM` edit, enable/disable, test and
  remove one camera. `CAM` is a name or the number `-l` prints, so `-x:3` and
  `-x:Garage` do the same thing. A name beats a number if you have a camera
  actually called `2`, and `#2` forces the position. Nothing is fuzzy-matched:
  one of these actions is "remove". The warnings about stranding un-encoded
  frames apply exactly as they do in the menu, and `-t` writes nothing.

### Fixed
- **Release notes are no longer cut mid-sentence.** The web UI caps what it
  renders at 4,000 characters, and v0.1.0's own release body was 4,020: the
  panel sliced it three characters into a sentence with nothing to say it had,
  so it read as this program having lost the rest. The cut now lands on a line
  boundary, the page says the notes were shortened, and it links to the full
  text. A cache written by 0.1.0 or 0.1.1 has the same repair applied on load,
  so the fix reaches an install that is already carrying clipped notes rather
  than waiting for its next check.

### Changed
- The version panel's upgrade instructions are now one line,
  `sudo timelapse update`, in place of the three-line `curl` recipe. The
  manual form still works and is unchanged.
- The GitHub release query (which tag is newest, what its notes say, why a
  request failed) moved out of `timelapse_web.py` into the new
  `timelapse_update.py`, which the web UI imports. Two callers needed it, and
  two copies of "compare versions as tuples, not strings" is one copy too
  many. It is the only cross-script import at module level; the others, from
  `timelapse_setup` and `timelapse_test` into `timelapse_encode`, all sit
  inside functions. They are installed side by side, so it resolves for either
  entry point.

## [0.1.1] - 2026-08-09

A maintenance release. Everything here came out of the first day of running
0.1.0 on a real deployment: each item is something an operator hit, not
something found by reading the code.

### Fixed
- **`timelapse cameras` without `sudo` now says so.** It reported "No existing
  config at /etc/timelapse/config.json" about a file that exists but is
  `0640 root:timelapse`, and told you to run the full wizard, which would have
  offered to overwrite the config you could not read. `cameras`, `transfer`
  and `web` now tell permission denied apart from missing, name the mode and
  owner, and say to try again with sudo. Malformed JSON is reported as such
  too, instead of a traceback.
- **The tabs no longer move between pages.** Status and logs use the whole
  window while the other pages keep a narrower reading column, and the tabs
  were positioned by whichever it was, so they jumped about 240 pixels as you
  moved between them. The title and the tabs are now centred on the window
  and stay put whatever the page below them does. The scrollbar's width is
  reserved on every page too, since otherwise a page long enough to scroll
  shifted them another 8 pixels.
- **A failed update check no longer costs you a day.** A momentary DNS
  failure was recorded as though it were a successful check, so the
  once-a-day interval gated the retry: a blip lasting seconds left the panel
  showing an error until tomorrow. A failure now retries in about 15 minutes,
  backing off on repeated failures so a host with no internet settles at the
  normal daily rate rather than asking every quarter hour forever. There is
  also a **Check now** button, and the panel says when it would try anyway.

  An `update.json` written by 0.1.0 is repaired on load, so this reaches the
  installs that actually hit the bug rather than only new ones.
- **Update check failures are now readable.** `URLError: <urlopen error
  [Errno -3] Temporary failure in name resolution>` became "DNS lookup
  failed, so this is your resolver rather than GitHub or this program", with
  the original text kept on the end because that is the part worth searching
  for. Rate limiting, timeouts and TLS failures are named too.
- **The panel no longer claims to have checked when it failed.** It said
  "Checked 09:01" for an attempt that resolved nothing; a successful check
  and an attempt are now tracked separately.

### Added
- **`timelapse --help`.** The bare `timelapse` command printed one line
  listing eleven subcommand names and nothing about what any of them did.
  There is now a real help page: what each command is for, which need `sudo`
  and why, the options worth knowing, and where the config lives. `-h`,
  `help` and a bare `timelapse` all print it; `--help` exits 0 and a bare
  invocation exits 1, since that one is a usage error.

### Documentation
- **README now marks which `timelapse` subcommands need `sudo`.** Anything
  that reads or writes the config does; `status`, `logs` and `version` do not.

## [0.1.0] - 2026-08-09

First release with the web UI in general use, and the first published as a
GitHub Release rather than a bare tag. Everything below came out of running
0.0.9 on a real deployment for two days.

### Changed
- **`timelapse web` now suggests this host's LAN address** rather than
  `127.0.0.1`. A status page reachable only from the machine it describes is
  of little use on a headless recorder. It falls back to `0.0.0.0` when no
  LAN address can be worked out, keeps an address you already have set, and
  says so plainly when accepting the suggestion would move an install that
  was deliberately kept local. The config file default is unchanged: a
  hand-edited `config.json` still starts closed.

### Added
- **An update check on the Overview page.** It says which version you have,
  whether a newer one is tagged, what changed in it, and the commands to
  upgrade. It asks GitHub at most once a day, and only while somebody has the
  page open, so a service nobody looks at never calls out at all.

  This is the **only outbound connection the web UI makes**. It sends no
  configuration, no camera names and nothing about your videos; GitHub sees
  this host's IP and the version string. `timelapse web` asks whether you want
  it and the page says how to turn it off; set `web.update_check` to `false`
  to disable it entirely. An upgrade turns it on, so turn it off there if you
  would rather it stayed quiet.
- **The wizard checks the bind address against the kernel** before writing it.
  An address this host does not have is refused at the prompt, with the
  addresses it does have listed, instead of producing a config whose service
  starts, reports success and is unreachable. A privileged port is refused
  too, since the service runs unprivileged.

### Fixed
- **The indexing line now updates itself.** After pressing Rescan it sat at
  whatever it said when the page was drawn, usually "0 files so far", until
  you reloaded, which read as a stuck process. It now counts up once a second
  while the scan runs and reloads the page once when it finishes. Two causes:
  the line was a static snapshot, and the counter behind it only advanced once
  per 500 files, so a library smaller than that finished still reporting zero.
- **The status and log panes now use the whole window and scroll inside
  themselves.** They were held to the same narrow reading column as the rest
  of the page, so nearly every journal line overflowed, and because the pane
  grew to fit all of them its horizontal scrollbar ended up far below the
  fold. The pane is now as wide as the window and no taller than it.
- **The day view no longer repeats the day.** Every row carried a link back to
  the page you were already reading, under a heading that states the same
  date. The column is dropped there and kept everywhere it distinguishes one
  row from another.
- **Two library groups the page linked to could not be opened.** Clicking
  "(no name in filename)" or the root folder showed the library home page
  again instead of the files, because a blank query value was being discarded
  before the filter was applied. Both groups are large in a real library: 450
  files and 1,246 files respectively on the author's.
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

[0.1.9]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.9
[0.1.8]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.8
[0.1.7]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.7
[0.1.6]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.6
[0.1.5]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.5
[0.1.4]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.4
[0.1.3]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.3
[0.1.2]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.2
[0.1.1]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.1
[0.1.0]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.1.0
[0.0.9]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.0.9
[0.0.1]: https://github.com/war4peace/timelapse-maker/releases/tag/v0.0.1

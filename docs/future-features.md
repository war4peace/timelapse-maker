# Future features

Planned work, in implementation order. Nothing here is committed to a release;
items move up and down as reality intervenes.

**Ordering rule.** The list is double-ordered:

1. Easiest first.
2. *Except* when a harder feature is a prerequisite for an easier one — then the
   prerequisite comes first, however painful it is.

So the list is not a difficulty ranking. It is a build order. When adding an
item, find the earliest position where all of its prerequisites are already
above it, then place it by effort among the remaining candidates. Say what an
item depends on explicitly, or the ordering rots.

---

## 1. Web UI — read-only status and video index

**Effort:** small. **Prerequisites:** none. **Status:** 1a–1d built; 1e (Range/seeking) and 1f (wizard, docs) remain.

### Scope

Deliberately minimal. Four decisions fix the shape of it:

1. **Read-only.** The UI never triggers an encode, restarts a camera, or edits
   config. It has no privileges beyond reading files and running two fixed
   commands. Control is a separate item, if ever.
2. **The video list comes from the destination mount** — the place the nightly
   rsync copies to. Only already-processed videos are listed. The UI never
   reads `frames_root` and never touches an in-progress day.
3. **Status is `timelapse status` and `timelapse logs`, on request only.** No
   polling, no background collection, no metrics. The page runs the command
   when it is loaded or when a button is clicked, and dumps the output in a
   `<pre>`.
4. **Playback is delegated to the user's own video player** — see below. The
   browser is an index, not a player.

That last one, plus (3), removes every hard prerequisite this feature had. No
refactor of `report_usage()`, no capture heartbeat file, no encode run-record
log, no remuxing, no transcoding, no codec compatibility matrix. It drops from
a large feature with three prerequisites to a small self-contained one.

### Handing off to VLC — yes, and the clean way is an `.m3u` playlist

VLC, mpv, MPC-HC and IINA all play from an HTTP URL, and all of them handle
MKV/AV1/HEVC natively — exactly the formats a browser struggles with. So the
right division of labour is: the web service *serves the bytes over HTTP*, and
the local player *decodes them*.

The mechanism to get from a browser click into the local player:

| Approach | Verdict |
|---|---|
| **Serve a one-line `.m3u` playlist** | **Use this.** Portable, no client setup, no custom code. |
| Copy the URL, paste into VLC's "Open Network Stream" | Always works, zero code — the documented fallback. |
| `vlc://` URI scheme | Avoid. Not a registered scheme, association is inconsistent across platforms and installs, and it breaks silently. |
| `file://` links to the mount | Browsers block `file://` navigation from an `http://` page. Dead end. |
| Direct SMB path shown as text | Useful to *display* — the user can paste it into their player and skip the proxy entirely. |

**How the `.m3u` route works.** For each video, expose an endpoint returning a
tiny text file:

```
#EXTM3U
#EXTINF:-1,Court180 2026-08-06
http://<host>:<port>/video/Court180.20260806.mkv
```

Served as `Content-Type: audio/x-mpegurl` with a `Content-Disposition:
attachment` filename. The browser downloads it, the OS opens it with whatever
is registered for `.m3u` — VLC in most installs — and the player streams from
the HTTP URL. Works on Windows, macOS, Linux and Android with nothing installed
on the client side.

Two details that decide whether it actually works:

- **Build the URL from the request's `Host` header, not from config.** An m3u
  containing `http://127.0.0.1:8080/...` is useless the moment it is opened on
  a phone. Whatever address the client used to reach the page is the only
  address known to work.
- **Loopback binding and remote playback are mutually exclusive.** A
  `127.0.0.1` bind means only a browser *and player* on the same host can use
  it. Watching from a phone means binding to the LAN, which is an explicit
  opt-in with the security consequences below.

Minor friction worth knowing: some browsers drop the `.m3u` into Downloads
rather than opening it, so the first click needs a "always open files of this
type". Show the direct URL as copyable text next to every entry so there is
always a path that needs no OS association at all.

**Bandwidth note.** Serving from a NAS mount pulls each file over the network
twice — NAS to host, host to client. If the client can reach the NAS directly
over SMB, displaying the share path lets the user bypass the proxy entirely.
For a single viewer on a LAN the double hop is irrelevant; it is worth
mentioning only because the direct path is free to display.

### What still needs care

Everything else got simpler. These did not.

#### The destination may not be a path at all

`transfer.destination` has two shapes and only one is a filesystem path:

| Destination | Example | Usable |
|---|---|---|
| Local path / mount | `/mnt/nas/timelapse/` | Yes. |
| Remote rsync spec | `user@nas:/mnt/user/timelapse/` | **No.** Not a path — needs SSH/SFTP. |
| Transfer disabled | — | Falls back to `paths.video_output`. |

With a remote spec the UI must say "videos are transferred to a remote host;
browsing is not supported" rather than render an empty list that reads as a bug.
Same for a dropped mount: with `transfer.require_mountpoint` set, an empty
library is the *correct* state during a NAS outage, so show the mount state
next to the list and the emptiness explains itself.

#### `timelapse logs` follows — it will hang the request

The CLI wrapper defines `logs` as `journalctl -u timelapse-capture -f`. That
never returns. A request handler calling it hangs until the client gives up.
The web path must use a bounded, non-following form:

```
journalctl -u timelapse-capture -n 200 --no-pager
systemctl status timelapse-capture.service timelapse-encode.timer --no-pager
```

Add a subprocess timeout regardless.

#### The service user probably cannot read the journal

`systemctl status` works unprivileged, but `journalctl -u ...` returns nothing
useful unless the invoking user is in `systemd-journal` (or `adm`). The
`timelapse` user is in neither. Fix is one line in the unit —
`SupplementaryGroups=systemd-journal` — but without it the logs pane comes back
mysteriously empty and looks like a bug in the UI.

No user input may ever reach either command line. Both are fixed argv with no
shell; keep it that way and there is no injection surface at all.

#### Filenames are the index

`<CameraName>.<YYYYMMDD>.<container>` already encodes everything the list needs.
No database, no sidecar metadata — consistent with the frame-naming rule in
architecture.md §3. Parse right-anchored, because camera names are free text and
may contain dots:

```
^(?P<camera>.+)\.(?P<date>\d{8})\.(?P<ext>[A-Za-z0-9]+)$
```

Cache the directory scan. ~2,500 files for a year of seven cameras is nothing
locally but slow to `stat()` one at a time over CIFS.

#### Range requests

Seeking in a 900 MB file needs HTTP `Range`, and
`SimpleHTTPRequestHandler` does not implement it — it serves from byte zero and
ignores the header, so scrubbing silently fails. VLC seeks via Range like any
other client.

For a first cut this is a legitimate thing to skip: without Range, playback
works start to finish and seeking does not. It is roughly twenty lines
(single-range `bytes=`, `206`, `Content-Range`, `Accept-Ranges`) so it is
cheap to add once the rest works.

#### Hardening and install wiring

A new long-running unit brings the whole CLAUDE.md checklist:

- **Bind `127.0.0.1` by default.** `http.server` is not a hardened
  internet-facing server and stdlib gives no realistic TLS story. A non-loopback
  bind is an explicit config opt-in; anything beyond the LAN goes behind a
  reverse proxy.
- **`ReadWritePaths` must cover every write path.** This service writes nothing
  — frames, videos and config are all read-only — which is worth stating
  explicitly in the unit rather than leaving to chance. `install.sh
  sync_units()` derives these from config and must learn about the new unit.
- **Add it to `restart_upgraded_services()`.** A long-running unit missing from
  that list serves the old build after an upgrade while the installer reports
  success. That already shipped as a bug once, for capture.
- **`config.json` holds camera credentials** — `chmod 640`, and Reolink-style
  URLs carry the password in the query string. Never serve the config, never
  render a camera URL. Read the handful of needed keys at startup.

#### Config and wizard

New `web` section: `enabled`, `bind`, `port`, optional `library_root` override.
Every key read with `.get(key, default)` — upgrades keep the existing
`config.json`, so a key read with `[]` breaks every existing install. Refresh
`config/config.example.json`, the only place users see new keys.

And wire it into `timelapse_setup.py` in the same change. CLAUDE.md is blunt
about this: `require_mountpoint` and the CIFS rsync flags sat in the schema for
two releases before the wizard offered them.

### The existing library, surveyed 2026-08-07

A read-only survey of the author's own destination (`U:\TL`, an Unraid share
over SMB) — the only real library this has been measured against. 6,848 files,
2.78 TiB, 2021-06-26 to 2026-08-06, in 55 `YYYY-MM/` folders plus two named
event folders and 1,247 loose files at the root.

**The destination is not a timelapse-maker output directory.** It is five years
of accumulated history from three predecessor systems, and this is the single
most important fact about the index: a parser written against the native
filename format handles **64% of it**.

| Pattern | Files | Era |
|---|---|---|
| `Camera.YYYYMMDD.mkv` — native | 4,384 | 2024-04 → now |
| `YYYY-MM-DD_Camera.mp4` | 1,594 | 2022-09 → 2024-02 |
| `YYYY-MM-DD.mp4` — **no camera name** | 449 | 2021-06 → 2022-09 |
| `Camera_YYYY-MM-DD.mp4` | 415 | 2021-07 → 2022-09 |
| `Camera<date>_<timestamp>.mkv` | 3 | 2024-04 |
| `YYYY-MM-DDTHH-MM-SS[_cam].mp4` | 2 | event folders |

A chain of patterns, tried most-specific first, parses 6,847 of 6,848. The one
failure is `MakeTLALL_backup.ps1`, the PowerShell predecessor still sitting in
the root — which is also the proof that "not a directory" is not a sufficient
test for "is a video".

Constraints this survey establishes, each of which breaks a naive index:

- **`Workshop` (723) and `workshop` (446) stay two entries.** They are almost
  certainly one place typed two ways, but see the rule below: the index does
  not decide that. Sort case-insensitively so they sit adjacent and the reader
  can see it for themselves.
- **449 files have no camera name.** They need a real bucket, not an exception.
- **`(camera, date)` is not unique** — 6,844 keys for 6,848 files. The primary
  key is the relative path. Nothing else is safe.
- **Extension allow-list**, not a directory test. See the `.ps1`.
- **Container changes with era** — mp4 through 2024, mkv after. Do not infer it.
- **Human-made folders carry meaning.** `2023-05-10 - Renovări` and
  `2023-05-12 - Dubios la poartă` are events, not clutter. Keep the folder as a
  label rather than flattening it away.
- **Never merge two names.** `garaj`/`Garage` and `street4k`/`StreetPTZ` look
  like renames and are not: a camera name is a **place**, cameras get
  repurposed over the years, and the same device may cover a different area
  while a new device covers the old one. The name is a location label, not a
  device identity, and the two drift apart deliberately. Corrected by the user
  2026-08-07 — do not regress this. More generally: people name things badly
  over five years, and deciding whether `garaj` and `Garage` are the same place
  is the *user's* call, not the index's. Show what is on disk. The most the UI
  should do is sort case-insensitively so variants land next to each other.
- **Some files are broken.** 11 are implausibly small — `Gate.20240727.mkv` at
  17 KB, `Gate.20251109.mkv` at 86 KB, almost certainly failed encodes. List
  them with their **full path**, so they can be checked and removed by other
  means. The UI is read-only and does not delete; naming the path is the whole
  service it can offer.

Two findings make reconciliation cheap:

- **mtime is exact.** Across all 4,384 native files, mtime is precisely one day
  after the filename date — min, median, p95 and max all equal 1. That is the
  00:05 encode timer. `(size, mtime)` is a dependable change key.
- **The folder never disagrees with the filename.** Zero mismatches in 5,601
  filed files, so `YYYY-MM/` is redundant and the index can key entirely on
  filenames.

**On scan duration: there is no baseline, and the design must not need one.** A
full recursive enumeration measured 1.7 s, but that was from a workstation with
10G to the server, and the deployment reads over CIFS from Linux on a different
stack. The work is latency-bound rather than bandwidth-bound — size and mtime
arrive with the directory entries, so it is round-trips, not megabytes — but
that only means the number moves for reasons that are hard to predict. Treat
1.7 s as a floor observed once, never as a budget. The scan runs in the
background, never blocks a request, and reports progress; if it takes a
hundred times longer on someone's hardware, nothing about the UI changes.

### Index design

**Storage: sqlite via stdlib `sqlite3`**, at `/var/lib/timelapse/web/index.db`.

This costs the property phase 1a established — `timelapse-web.service` is the
only unit with no `ReadWritePaths`, so `ProtectSystem=strict` leaves the entire
filesystem read-only to it, verified under systemd. A database ends that, and
the honest framing is that the guarantee becomes *scoped* rather than absolute:
the unit gets exactly one writable path, `/var/lib/timelapse/web/`, and nothing
else. The library itself, the frames, and the config all stay read-only. That
is still enforced by systemd rather than by good intentions, which is the part
worth keeping.

`sync_units()` must derive that path like every other, and the read-only
comments in the unit and in architecture.md §4.5 need correcting — they
currently state the unit writes nothing anywhere, which will no longer be true.

**Scan model:**

- **First run scans in the background.** A worker thread, started after the
  server is listening, so the UI is up immediately and reports "indexing, N
  files so far" rather than hanging.
- **Reconcile on access, not on a timer.** Opening a camera or a month
  re-stats that directory and compares its mtime; serving a file re-stats that
  file and compares size and mtime. Anything changed is updated, anything gone
  is evicted. A full rescan stays available as an explicit action.
- **The path is the primary key.** `(camera, date)` is an index, not an
  identity.
- **Store what a directory entry gives**: path, size, mtime, parsed camera,
  parsed date, parsed pattern, folder label. Anything needing ffprobe —
  duration, resolution, codec — is a later phase and must not be on the scan
  path, or the first run stops being metadata-only and becomes 2.78 TiB of I/O.

### Phasing

| Phase | Work |
|---|---|
| 1a | **Done.** `timelapse_web.py`: `ThreadingHTTPServer`, loopback bind, one static page, `/healthz`, library-root resolution. Unit + install wiring + config keys. |
| 1b | **Done.** Status pane — `systemctl status` and bounded `journalctl` on request, output in a `<pre>`. |
| 1c | **Done.** Library index — sqlite store, background first scan, pattern-chain parser, reconcile on access, browse by camera and date, flagged files with full paths. |
| 1d | **Done.** `/video/<path>` file serving + `/play/<path>` `.m3u` handoff. Download link, share path and stream URL shown. |
| 1e | Range request support, so seeking works. |
| 1f | Wizard integration and docs. |

1a–1d is the bare scaffolding. 1e and 1f are the finish.

### Deferred

- In-browser `<video>` playback. The default output is AV1-in-MKV, which is
  close to the worst case for `<video>` — Matroska is not a dependable browser
  container and AV1/HEVC support splits by browser and platform. Delegating to
  a local player sidesteps all of it. If this is ever revisited, the cheap route
  is `container: "mp4"` with `-movflags +faststart` rather than remuxing —
  noting that a truncated MP4 is unplayable where a truncated MKV plays up to
  the cut, which is presumably why MKV is the default.
- Thumbnails / poster frames.
- Live status updates, history, coverage graphs.
- Any control action.
- Remote library browsing over SFTP, for remote-rsync destinations.

---

## Candidates, not yet ordered

Parked here until they have enough shape to be placed. Several come from
`docs/architecture.md` §7, which describes how each would fit the existing
design.

- **Camera restart on hang** — remediation for the frozen-but-reachable case.
  Detection already exists (`consec_fail`); needs a cooldown so it cannot
  reboot-loop a camera.
- **Frozen-camera detection** — a full frame count with a static image currently
  passes every check. Frame-to-frame difference sampling would catch it.
- **Per-camera capture settings** — interval, quality. The camera dict is already
  passed whole to the thread; the encoder's `Cov%` maths assumes the global
  interval and would need the per-camera value threading through.
- **Frame retention beyond encode** — a separate age-based sweeper. Keep
  "encode" and "delete" separable; do not put retention in `encode_day()`.
- **Notification sinks other than Discord** — `send_discord()` is the only
  outbound path and has a clean signature to copy.
- **Machine-readable runtime state** — a capture heartbeat file and an encoder
  run-record JSONL. Not needed by the read-only UI above, but the prerequisite
  for any status view richer than shelling out to the CLI.
- **Remote library browsing over SFTP** — the other half of the destination
  problem. Needs a credential story.

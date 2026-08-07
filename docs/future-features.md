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

**Effort:** small. **Prerequisites:** none. **Status:** scoped, not started.

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

### Phasing

| Phase | Work |
|---|---|
| 1a | **Done.** `timelapse_web.py`: `ThreadingHTTPServer`, loopback bind, one static page, `/healthz`, library-root resolution. Unit + install wiring + config keys. |
| 1b | **Done.** Status pane — `systemctl status` and bounded `journalctl` on request, output in a `<pre>`. |
| 1c | Library index — resolve destination, scan, parse filenames, group by camera and date. Direct URL shown as copyable text. |
| 1d | `/video/<name>` file serving + `/play/<name>.m3u` handoff. Download link. |
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

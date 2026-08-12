# Decided against

Things that were considered and turned down, with the reasoning that turned
them down. Kept because a rejected idea with no record attached comes back
every six months, and because most of these are reasonable-sounding: the
reason they were refused is a constraint elsewhere in the design, or the scope
of the program, and never a lack of interest.

Nothing here is forbidden forever. If the constraint changes, the entry says
which constraint, so the argument can be reopened on the facts rather than
relitigated from scratch. Scope is the harder one to reopen, since it moves on
demand rather than on facts; those entries say so. Things still wanted live in
[future-features.md](future-features.md).

---

## Frame retention beyond encode

**Refused on scope, 2026-08-12. This program makes one timelapse per camera
per day and writes it where you asked. The frames are an intermediate, not an
output.**

It was item 1 in the plan, researched and ready to build, and every argument
for it fell to that one sentence. They are recorded here because each of them
sounds reasonable on its own, and because three of the four turned out to be
solved elsewhere rather than merely outweighed.

| The argument for keeping frames | Why it fails |
|---|---|
| **A verification window.** Hold the source until you have seen that the video is good, or until you know the cadence was right. | The half that matters is already in the code: deletion is gated on `status == "OK"`, so a failed encode keeps its frames with no setting involved. The cadence half is not recoverable in either direction. Nothing revives frames that were never captured, so too slow is unfixable whatever you kept, and too fast merely makes a long video. The frame *rate* needs no source at all: 60 fps is the default so a player can run it at 0.25x, and a 10 fps video speeds up as easily. Change the camera's cadence tomorrow. |
| **Frames are the product**, for photogrammetry, training sets, or a record of individual full-resolution stills. | A different program, and the name of this one says which. Not refused because the use is bad; refused because building for a user who has not appeared is how scope goes. If people ask, that is data, and this entry is where the argument resumes. |
| **Forensics.** Which ticks went missing, and were they clustered or scattered? | The capture log already answers it. `timelapse_capture.py` logs the **first** failure of every burst as well as every `log_every_n_failures`-th, and the recovery line carries the total, so scattered singles and a solid outage do not look alike. Not theoretical: the Court180 frame delta of August 2026 was diagnosed exactly this way, from bursts of two 500s in the log, with no frame directory involved. |
| **Re-encoding later at better settings.** | The lossy knobs (`av1_cq`, `hevc_cq`, `x264_crf`) are config-only, never offered by the wizard, and conservative by default. This is insurance against a user who has already hand-edited JSON onto a path the program does not offer. |

### What this entry does not refuse

Three things exist today and should not be removed on the strength of the
decision above:

- **`encode.delete_frames_on_success` keeps working.** Honouring it costs
  nothing, and removing it would delete frames on somebody's next upgrade,
  which is the most destructive thing this project is capable of. It stays
  unbounded and stays out of the wizard: set it false and frames are kept
  forever, one directory per camera per day.
- **`--keep-frames` keeps working**, for a single manual run.
- **The encode-once marker (`.encoded.json`, 0.1.6) is not retention
  groundwork** and does not depend on this decision. `--keep-frames` alone
  leaves a day directory for the nightly timer to find and encode again, so
  the defect it fixes is reachable from the CLI with no config change at all.

**Reopen if:** users ask for kept frames. Nothing technical stands in the way,
which is unusual for this file: the design was finished. Two researched traps
are worth having back if it happens. A day directory's *name* is the date it
covers and its mtime is not, since an interrupted day gets touched later than
it represents. And a sweeper must delete the whole directory or nothing in it:
removing `.cadence.json` alone makes the encoder measure an old day against
today's config, and removing `.encoded.json` alone puts the day back in the
queue to be encoded a second time.

---

## In-browser `<video>` playback

**Refused because the output format is close to the worst case for a browser,
and changing the output format to suit the browser would make the files worse
at their actual job.**

The default is AV1 in Matroska. Matroska is not a dependable browser
container, and AV1 and HEVC support splits by browser, platform and hardware.
Delegating to a local player sidesteps every part of that: VLC, mpv, MPC-HC
and IINA all play MKV/AV1/HEVC natively, which is exactly the set a browser
struggles with, and the `.m3u` handoff needs nothing installed on the client.

The obvious fix is `container: "mp4"` with `-movflags +faststart`, which is
cheap and does not require remuxing. It is refused for a specific reason
worth keeping: **a truncated MP4 is unplayable, where a truncated MKV plays up
to the cut.** These are unattended overnight encodes on a machine that can
lose power, so the container that degrades gracefully is the right default,
and that is presumably why MKV was chosen before anyone wrote it down.

An operator who wants browser playback can already set `container: "mp4"`
themselves and accept that trade. What is refused is making it the default, or
adding a transcode path to serve both.

**Reopen if:** the deployment stops producing AV1/MKV by default, or browsers
converge on Matroska (they have not in a decade).

---

## Thumbnails and poster frames

**Refused on the cost of the first scan, and on the second writable path.**

Two constraints collide. Generating a poster frame means ffmpeg reading each
video, and the surveyed library is 6,848 files and 2.78 TiB over CIFS: the
index's first scan is deliberately metadata-only for exactly this reason, so
that it costs directory entries rather than terabytes. Adding thumbnails puts
2.78 TiB of reads on the first run of a service that is supposed to come up
immediately.

Caching them somewhere is the obvious answer, and that is the second problem.
`timelapse-web.service` has exactly one writable directory and the claim that
this is *enforced* rather than intended is the strongest structural statement
the project makes. A thumbnail cache is not a reason to weaken it.

**Reopen if:** thumbnails become a background, opt-in, resumable job that
writes into the existing `state_dir` and never blocks a page. That is a real
design, not a small feature, which is why it is here rather than in the plan.

---

## Live status updates, history and coverage graphs

**Refused on the polling, which is the objection that survived.**

It was refused as premature: every status answer came from shelling out to
`systemctl` or `journalctl` on request, so there was no time series to plot and
a graph built by re-running `systemctl` on a timer would have been a polling
loop drawing a straight line.

**Half of that changed at 0.1.6** and the entry is updated rather than
quietly left stale. Runtime state now exists, and it was this entry's stated
prerequisite. What arrived is not symmetrical, though, and the difference is
the whole argument:

- `encode.json` keeps the newest fourteen runs, which *is* a short time
  series. A fortnight of nightly coverage per camera could be drawn from it
  today, with no collector and no new storage.
- `capture.json` is a snapshot and nothing more. It is rewritten in place once
  a minute, so there is no history of camera health to plot, and inventing one
  means a collector.

The deeper objection is the polling, and it is untouched by any of that. The
web UI's rule is that it collects nothing in the background and answers on
request, which is what keeps it cheap enough to run on the same host as an NVR
and honest enough that the page always reflects now rather than a cached
minute ago. Live updates mean a collector, a retention policy and a storage
format: a different service, really.

**Reopen if:** there is a specific question a graph would answer that a number
cannot. That is now the only condition left, and it is the one that was always
doing the work. A fortnight of encode coverage is the cheapest candidate,
since the data is already on disk; live *camera* graphs are not, and should be
argued separately.

---

## Any control action from the web UI

**Refused structurally, and this one is close to permanent.**

The UI never triggers an encode, restarts a camera, or edits the config, and
that is not politeness: `ReadWritePaths` gives the unit one writable directory
and `ProtectSystem=strict` makes the rest of the filesystem read-only,
verified under real systemd rather than asserted. The service *cannot* change
anything, which is a much stronger statement than a service that has chosen
not to.

Any control action breaks it in one of two ways: give the service write access
(and the guarantee is gone), or add a privileged helper it can call (and the
guarantee is now "trust this helper", which is where privilege-escalation bugs
live). Both are a worse trade than "use the CLI over SSH", which is what an
operator with control-level intent already has.

The 0.1.5 login made this concrete: the UI cannot even turn its own login on
or off, because that would mean writing `config.json`. The CLI answer is
`sudo timelapse password --disable`.

**Reopen if:** never, on the current architecture. A control plane would be a
separate, separately-hardened unit, not a feature of this one.

---

## Where the rest of the "no" decisions live

Several refusals are recorded next to the code they constrain, because that is
where they bite. Listed here so this file is a complete index of them, not a
second copy that can drift:

| Decision | Refused because | Recorded in |
|---|---|---|
| Hashing or encrypting the camera passwords | We are the client and must replay the password to the camera. You can hash what you *verify*; you must keep what you *present*. Encrypt-at-rest fails too: an unattended boot puts the key on the same disk, readable by the same account. | architecture.md, CHANGELOG 0.1.4 |
| A lockout after N failed web logins | The gated content is a status page and a video list; the person a lockout stops is the household member who mistyped. A flat 3s delay instead. | architecture.md §4.5, CHANGELOG 0.1.5 |
| Asking for the old password in `timelapse password` | It needs root, and root can already read every camera password in that file. The question proves nothing and locks out the one person entitled to fix a forgotten login. | architecture.md §4.5 |
| Rendering release-note markdown in the update panel | A markdown renderer is a dependency or a parser to maintain, for a paragraph nobody reads twice. GitHub already renders it one click away. | architecture.md, CHANGELOG 0.1.4 |
| A `tools/` directory of helper scripts | It duplicated what the wizard should do, and `install.sh` never shipped it. A wizard must not tell you to go and do it yourself. | CHANGELOG 0.0.x |
| Polling anything in the web UI | On request, never on a timer: a service nobody looks at should cost nothing. | architecture.md §4.5 |

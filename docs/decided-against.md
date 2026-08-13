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

## Frozen-camera detection

**Refused on evidence, 2026-08-13: in five years of running IP cameras for
timelapses, across a fleet, the operator has never seen one keep answering HTTP
while serving an unchanging image.**

It was item 4, and its own design split is what settles it. Two different
faults hide behind "frozen" and they need different detectors:

| Detector | Catches | Why that is not enough |
|---|---|---|
| **Byte-identity** between consecutive JPEGs, nearly free in the capture loop | A stuck encoder re-serving one frame | The fault nobody here has ever observed. And it cannot live on the RTSP path at all, because ffmpeg fetches those frames, not us; it would apply only to HTTP snapshot cameras, where each fetch is an independent request and a stuck encoder is least likely. |
| **Image comparison**, needing an ffmpeg decode per sampled frame | A live sensor pointed at nothing: a painted lens, a turned camera | A live sensor produces different bytes every time from noise alone, so this is the *only* half that could catch vandalism, and it is the half that has to guess. Any threshold must survive a dark, empty driveway all night. This project has shipped that mistake twice already (the rsync flags, the ReadWritePaths warning), which is why "a check that guesses is worse than no check" is written into architecture.md. |

So the cheap half catches a fault never seen, and the expensive half catches a
case that is out of scope anyway, by guessing.

**The second-hand evidence probably is not about cameras.** NVR communities do
discuss "stuck on last frame", and NVRs do ship watchdogs for it. Two things
about that. If the mitigation lives in every NVR then it is the NVR's job, not
this program's. And the freeze itself is usually in the *consumer*: an analogue
camera has no frame buffer to be stuck on, since it emits a continuous
baseband signal and fails as video loss or noise, so a held frame in an
analogue setup is the DVR's decoder. The same is true of a stalled RTSP session
in a viewer, which looks exactly like a frozen camera to whoever is watching.
This program is a different consumer making its own independent request, so
most of those reports would not reproduce here.

**The adjacent need is already met**, which is the third time that has been the
answer in this file. The observed failure is a camera that stops answering, and
since 0.1.6 `capture.json` publishes `last_success` and `consec_fail` per
camera and the Overview turns the row red.

**Reopen if:** somebody reports a real instance, ideally keeping the frames. A
detector built against evidence is a different proposition from one built
against a guess, and the byte-identity half is genuinely cheap if there turns
out to be anything to catch.

---

## Restarting a camera automatically

**Refused structurally, 2026-08-13: two independent control loops acting on one
device, with no shared state and no arbitration, eventually fight. The fighting
is intermittent and horrible to diagnose.**

It was item 5, and unlike the entry above the *fault* is real: cameras do stop
answering. What is refused is this program doing something about it.

**The case that settles it is a real deployment, not a hypothetical.** One
camera fails to switch its warm light off at dawn, so a Home Assistant
automation reboots it at first light; the reboot itself is what clears the
light. This feature would see that camera stop answering, count its failures,
and issue a reboot of its own during or just after the one that was supposed to
happen. The worst version is not a redundant reboot but a second reboot landing
**while the device is still booting**, which is the one moment its firmware is
most exposed.

The general form: **this program cannot tell a fault from an intention.** A
camera that stops answering may be broken, may be rebooting on somebody's
schedule, may be having its lens cleaned, may be on a switch port somebody just
unplugged on purpose. Every one of those looks identical from here, and only
one of them wants a reboot.

Three more, each sufficient on its own:

- **It needs a stronger credential than this project has ever asked for.** The
  config holds whatever fetches a JPEG, which can be a view-only account on
  most cameras. A reboot API needs admin. That raises what a leaked config is
  worth, on a project that has already shipped one credential leak and
  rewritten its logging around it.
- **"Disable it during maintenance" contradicts "set and forget".** The core
  promise here is running untouched for weeks or months. A guard you must
  remember to turn off before touching anything is a guard that fires exactly
  when you are busy.
- **Anyone with several IP cameras already owns something that does this**, and
  better: an NVR, Agent DVR, Home Assistant, Uptime Kuma. This program is
  supplementary by design, and stepping onto ground another tool already holds
  is how it starts conflicting with setups it cannot see.

**What was proposed instead** was an optional notification on repeated snapshot
failures, default off: telling someone is not the same as acting, it cannot
fight another controller, and it needs no new credential. That became item 7,
and it was refused a day later on its own merits. See the next entry.

**Reopen if:** there is real demand *and* an answer to the arbitration
question, which is not "add a config flag". Knowing that a reboot is safe means
knowing what else touches that camera, and nothing in this program's design
gives it that.

---

## Alerting when a camera stops answering

**Refused on scope and duplication, 2026-08-13: a monitoring tool running on
the same box does this better, and the one failure mode it cannot see is the
one this alert would have missed anyway.**

It was item 7, proposed as the survivor of items 4 and 5, and it lasted one
day. The deployment target is a Linux host with a GPU capable of NVENC, so it
is not a constrained machine: whoever runs this can also run Uptime Kuma,
Observium or the NVR they already own, all of which bring schedules, escalation,
maintenance windows and more delivery channels than this program will ever
have. Building a worse one inside the encoder is not a saving.

**This was already the written position**, which is the part worth noticing.
architecture.md has said "alerting on camera health" is explicitly out of scope
since the first version of the document, and the note beside the capture
daemon's failure throttling says in as many words that external uptime
monitoring is the real alerting path and that this log is for post-hoc
diagnosis. Item 7 was proposed without either being consulted. A rejected idea
that comes back in a new costume is exactly what this file exists to catch, and
it caught one written by the same author who had written the rule.

**What already covers it here**, none of it new work:

- the nightly notification carries `Cov%` per camera, so a bad day is legible
  the next morning;
- the web UI shows the capture log, up to 1,000 lines, and the daemon logs the
  first failure of every burst as well as the recovery total;
- since 0.1.6 `capture.json` publishes `last_success` and `consec_fail` per
  camera once a minute, and the Overview turns a silent camera's row red.

### The argument that settles it

The failure modes split in two, and the proposed alert lands on the wrong side
of the split.

| What happened | Can an external monitor see it? | Would item 7 have fired? |
|---|---|---|
| The camera is unreachable: powered off, unplugged, crashed, rebooting | Yes, within its poll interval, and it can page you | Yes, and later than the monitor, from a daemon with no escalation, no schedule and no maintenance window |
| The camera answers HTTP but this program's fetches fail: contention, credentials rotated, snapshot endpoint moved by a firmware update | No. The monitor is checking the device; only this program knows whether *it* is getting frames | Only for the sustained version. The one real instance on record, the Court180 frame delta of August 2026, was bursts of **two** failures from Agent DVR polling the same camera in parallel, so `consec_fail` never exceeded 2 and no usable threshold fires on it |

So the case where this program genuinely knows something no monitor can is
also the case a `consec_fail` threshold is blind to, and the case the threshold
catches is the case something else already catches better.

### What is actually given up

One thing, and it is real: a camera that keeps answering while every fetch
fails, for a reason that persists. A rotated password, or an endpoint that
moved under a firmware update. There `consec_fail` does climb into the
thousands, an external monitor stays green, and the operator finds out at 00:05
from `Cov%` of 0. The cost is bounded at one camera for one day, and
`timelapse test` already exists as the thing to run after touching a camera.
Priced, not overlooked.

### The effort estimate was wrong as well

Item 7 was labelled small because the counter and the notification sinks both
exist. They do, but **the capture daemon has never made an outbound connection
in its life**, and that is deliberate: it is a copy of the redaction rule and
nothing else, importable from nowhere. Sending from it means either importing
`post_webhook()` from `timelapse_encode` (breaking a daemon-independence rule
held since 0.0.1) or a third copy of the transport. The cheapest correct
version crosses the one boundary this project has never crossed.

**Partly answered already.** The refusal prompted a better question the same
day: a camera that rejects our credentials says so in its own response, so that
half needs no threshold and no guessing, and it is invisible to an external
monitor precisely because a monitor holds no credentials. That became
future-features item 8, triggered on how long the refusal has lasted rather
than on how many times it has happened. It is not this feature in disguise: it
reports one deterministic, self-declared condition instead of inferring health
from a counter.

**Reopen the rest if:** somebody loses a day to a camera that was up while its
snapshots failed for a reason the camera did *not* declare. Do not rebuild it
as proposed. Key it on the age of `last_success` rather than on `consec_fail`,
since that covers both shapes of failure at once, and put it in something that
*reads* `capture.json` rather than in the daemon that writes it, which leaves
the daemon exactly as connectionless as it is today.

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

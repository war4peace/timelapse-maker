# Decided against

Things that were considered and turned down, with the reasoning that turned
them down. Kept because a rejected idea with no record attached comes back
every six months, and because most of these are reasonable-sounding: the
reason they were refused is usually a constraint elsewhere in the design, not
a lack of interest.

Nothing here is forbidden forever. If the constraint changes, the entry says
which constraint, so the argument can be reopened on the facts rather than
relitigated from scratch. Things still wanted live in
[future-features.md](future-features.md).

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

**Refused as premature: the data it would draw does not exist yet.**

Every status answer today comes from shelling out to `systemctl` or
`journalctl` on request. There is no time series to plot, and a graph built by
re-running `systemctl` on a timer would be a polling loop drawing a straight
line.

This is also the one entry here with a live prerequisite: *machine-readable
runtime state* (future-features.md §2) is what would make it possible, and
until that exists this is not a feature but a wish.

The deeper objection is the polling. The web UI's rule is that it collects
nothing in the background and answers on request, which is what keeps it
cheap enough to run on the same host as an NVR and honest enough that the page
always reflects now rather than a cached minute ago. Live updates mean a
collector, a retention policy and a storage format: a different service,
really.

**Reopen if:** the runtime-state files land, and there is a specific question
a graph would answer that a number cannot.

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

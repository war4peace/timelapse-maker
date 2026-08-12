# Future features

Planned work, in implementation order. Nothing here is committed to a release;
items move up and down as reality intervenes.

Things considered and turned down live in
[decided-against.md](decided-against.md). Move an item there rather than
deleting it, so the next person does not re-propose it from scratch.

**Ordering rule.** The list is double-ordered:

1. Easiest first.
2. *Except* when a harder feature is a prerequisite for an easier one, then the
   prerequisite comes first, however painful it is.

So the list is not a difficulty ranking. It is a build order. When adding an
item, find the earliest position where all of its prerequisites are already
above it, then place it by effort among the remaining candidates. Say what an
item depends on explicitly, or the ordering rots.

**Shipped items are removed, not ticked.** The web UI plan (phases 1a-1f) was
deleted from this file at 0.1.6 because it was finished; the CHANGELOG says
what shipped and architecture.md §4.5 says how it works. The one part worth
keeping was the library survey that justified the filename parser, which is
now architecture.md §9a. The re-encode defect that briefly sat at the top of
this list as item 0 went the same way once it was fixed: the invariant it
established is in architecture.md §3, where the next person will actually meet
it.

**Numbering is not reused**, which is why this list opens at 2. Item 0 shipped
and item 1 (frame retention) was refused on scope and moved to
[decided-against.md](decided-against.md), where the arguments for it are
recorded rather than left to be re-made.

Everything below is **researched against the code as of 0.1.5**, so each entry
says where it would hook in and what is already there. None of it is designed;
these are pre-plans, and the traps are the point.

---

## 3. Notification sinks other than Discord

**Effort:** small-to-medium. **Prerequisites:** none.

### What exists

`post_webhook()` is a generic JSON POST with the explicit `User-Agent` that
Cloudflare requires. `send_discord()` builds the Discord embed and is called
from exactly three places: the nightly summary, the no-work summary, and the
critical-failure handler. The summary table is already rendered as plain text
before it is wrapped in an embed.

### Shape

The transport is already generic; what is Discord-specific is the payload and
the embed's width. ntfy and gotify are one `post_webhook()` call each with a
different payload shape. Email is `smtplib` plus `email.message`, both stdlib,
so it costs no dependency either.

The honest structure is a list of configured sinks, each with a type, rather
than a second `discord`-shaped block per vendor.

### Traps

- **Failure isolation is a hard requirement here.** `send_discord()` already
  swallows everything with a deliberately broad `except`, because a socket
  timeout is not a `URLError` and a failed notification must never take down
  the run it is reporting on. Several sinks means that guarantee has to hold
  per sink: one broken webhook must not stop the others, and none of them may
  fail the encode. Same rule the cameras get.
- **The 50-column embed lesson does not transfer.** That width was forced by
  Discord's embed rendering; email and ntfy have entirely different
  constraints, so the table must be rendered per sink rather than once.
- **Every sink is a new credential in `config.json`.** `SECRET_KEY_RE` must
  cover whatever the keys are called, or `timelapse config --redacted` leaks
  them: exactly the `password_hash` gap found in 0.1.5.
- SMTP means a password, a port, TLS-or-not, and a from-address: it is the
  only sink here with real configuration surface, and probably the one to do
  last despite being the most requested kind.

---

## 4. Frozen-camera detection

**Effort:** medium. **Prerequisites:** none, but it reports through (2) if
that exists.

### What exists

Capture already inspects every frame: `min_bytes` rejects a truncated
response and the JPEG SOI marker is checked. So there is a per-frame
inspection point already, and it costs nothing extra to look at what is
already in memory.

Nothing looks at *content*. A camera that has frozen but is still serving
produces a full day of valid JPEGs, a perfect frame count, 100% coverage and a
video of a still image. Every check in the project passes.

### Shape, and the split that matters

Two different faults hide behind "frozen", and they need different detectors:

- **A stuck stream** repeats one encoded frame, so successive JPEGs are often
  *byte-identical*. A hash of the response, compared with the previous one, is
  nearly free and catches this in the capture loop.
- **A live sensor pointed at a static scene** produces different bytes every
  time (sensor noise), so byte comparison never fires. Catching that needs
  actual image comparison.

Image comparison needs a decoder, and the stdlib has none: no PIL, no numpy,
and the one third-party dependency this project allows is already spent on
`requests`. **ffmpeg is a hard dependency though**, so the decode can be shelled
out. That puts the expensive detector in the nightly encode, which is already
reading every frame, rather than in the capture loop, which must stay cheap.

So: byte-identity in capture (cheap, catches the stuck stream), optional
sampled comparison in the encoder (catches the static scene), and neither
pretends to be the other.

### Traps

- **A static scene at 03:00 is normal.** Any threshold has to survive a dark,
  empty driveway all night without crying wolf, which is the same false-alarm
  problem the rsync and ReadWritePaths checks got wrong twice.
- Reporting is the hard half, not detection. There is no channel for "camera
  N looks frozen" other than the log and the nightly summary.

---

## 5. Camera restart on hang

**Effort:** medium. **Prerequisites:** none. Detection exists; remediation
does not.

### What exists

`consec_fail` counts consecutive failures per camera thread and drives the log
throttle (`log_every_n_failures`). It is already the number a remediation
would trigger on. Nothing acts on it.

### The thing to decide before any code

**This would be the first action this project takes *against* a camera.**
Everything today is a read: fetch a snapshot, probe a profile. A reboot is a
write, it interrupts whatever else is watching that camera (the NVR, very much
including the one on the same host), and it is the kind of thing that looks
fine in testing and reboot-loops a camera at 3 a.m.

### Shape

No ONVIF client exists here and none should be written casually: device
discovery plus WS-Security digest is a real amount of hand-rolled SOAP.
`probe_profiles()` is the closest thing and it only substitutes a number into
a URL.

The tractable version is a per-camera `reboot_url` in the config, since every
vendor already exposes one over plain HTTP (Dahua `magicBox.cgi?action=reboot`,
Reolink `?cmd=Reboot`, Hikvision ISAPI). The project does not have to know the
vendor; the operator pastes the URL, exactly as they already do for snapshots.

### Traps

- **A cooldown is mandatory**, and it must survive a restart of the daemon, or
  a crash loop becomes a reboot loop. That means it is really an item (2)
  dependency in disguise if it is to be durable.
- The reboot URL carries the same credentials as the snapshot URL, so it is a
  secret: redaction, and never logging the URL.
- A camera that is unreachable cannot be told to reboot. The failure mode that
  most wants this is the one where it least works.
- **Suspect a second client first.** The last two camera-behaviour mysteries
  here were both AgentDVR contention, not the camera.

---

## 6. Remote library browsing over SFTP

**Effort:** large. **Prerequisites:** a decision about the web UI's outbound
connections.

### What exists

`is_remote_spec()` detects `user@host:/path` and `rsync://` destinations, and
the library page reports them honestly as not browsable rather than rendering
an empty list. That is the current, correct behaviour.

### The credential story is better than the old note claimed

All three units run as the same `timelapse` account, and a remote transfer
destination means rsync-over-SSH **already works for that account**: the keys
exist and the host key is already trusted. So the missing credential story is
largely already solved by the transfer setup, which is worth knowing before
anyone designs a second one.

### Why it is still large, and still last

- The stdlib has no SSH client. Staying stdlib-only means shelling out to
  `sftp`/`ssh` with fixed argv, and parsing directory listings from a program
  whose output is not a contract.
- **It puts the network-facing service in the business of making outbound SSH
  connections.** The update checker's rule is that the web UI's one outbound
  connection stays one, opt-out and named on the page. This would be a second,
  to a host holding the whole video archive, with a key that can read it.
- Latency: every reconcile-on-access becomes a round trip over SSH, and the
  index's cheap `(size, mtime)` change key needs a `stat` per file.

An honest alternative that costs nothing: if the same share is *also* mounted
locally, `web.library_root` already solves this today, and that is what the
wizard tells people. This entry only matters for a destination that is
genuinely remote-only.

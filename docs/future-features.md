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

**A number identifies an item; its position orders it.** Numbers are never
reused, so that a commit or a CHANGELOG entry referring to "item 3" still means
something a year later, and the list is therefore not in numeric order. Items
0, 2 and 3 shipped in 0.1.6. Items 1, 4, 5 and 7 were refused and moved to
[decided-against.md](decided-against.md), where the arguments for them are
recorded rather than left to be re-made. Item 8 was added the same day item 7
was refused, and is a good illustration of why the refusals are written down
rather than deleted: it exists *because* of the argument that killed item 7,
and it is a fraction of the size.

Item 3 shipped **narrower than it was planned**, and that is worth noting for
the next entry that lists candidates: it proposed ntfy, gotify and email, and
what was actually wanted was ntfy and **Telegram**, which the entry never
mentioned. Email was refused as a different job with a different failure mode.
A pre-plan is a starting point for the conversation, not the specification.

Everything below is **researched against the code as of 0.1.5**, so each entry
says where it would hook in and what is already there. None of it is designed;
these are pre-plans, and the traps are the point.

---

## 8. Report a camera that refuses our credentials

**Effort:** small. **Prerequisites:** none. Item 2 shipped the state file this
would write into and item 3 shipped the sinks.

### Where it came from

Item 7 was refused because a monitoring tool sees an unreachable camera sooner
and handles it better (decided-against.md). The question that followed it was
sharper: can a rotated password be caught from the camera's own answer? It can,
and **a monitoring tool cannot catch it, because it holds no credentials**.
This is the part of item 7 that survives its own refusal, and it is a much
smaller part than item 7 was.

### Two shapes, and only one of them is an HTTP status

| The camera says | Which cameras | Where it lands today |
|---|---|---|
| **401**, occasionally 403 | Anything doing digest or basic properly: Dahua, Hikvision, Axis | `raise_for_status()` in `_grab()` raises `HTTPError` naming the code |
| **200 OK with a JSON error body** | Reolink | The `\xff\xd8` check, as `response is not a JPEG (bad SOI marker)` |

A status-code-only implementation misses the second, which is the brand this
project's own deployment runs and the one the redaction rules were written
around. `explain_payload()` (`timelapse_setup.py:1186`) already parses that
body and is the thing to reuse.

### What exists

The daemon distinguishes the two shapes already, by accident: both raise, with
different messages. It collapses them into `err` and logs the string, and the
string survives redaction intact, since `redact()` masks the credential *value*
and not the status. So `401 Client Error: ... password=***` is already in
journald, in `capture.log` and on the web log page. What does not exist is any
structured record: `capture_state()` publishes names, timestamps and counters,
and no error of any kind, so the Overview can say a camera is silent and can
never say why. The wizard and `timelapse test` both special-case 401 by hand.

### Shape

Classify where `err` is set in `run()`, never in `_grab()`, which must stay the
fetch and nothing else. Three classes are enough: `auth`, `unreachable`,
`other`. Record the class, **the moment the current class began** and **how
many consecutive ticks it has held**; publish all three in `capture.json`.

**Only a body that names an auth error may be classified `auth`.** The 200
path is the loose one: a camera part way through a firmware update, or sitting
in a maintenance mode, also answers 200 with something that is not a JPEG, and
calling that a refusal would be an invention. A 401 declares itself; a 200 is a
refusal only if `explain_payload()` finds the camera saying so, and everything
else down that path is `other`.

### The trigger: ten refusals AND ten minutes, whichever comes last

Decided 2026-08-13, in two steps, because the first version was wrong at one
end of the range.

**A count alone is not comparable across cameras.** Per-camera
`interval_seconds` shipped in 0.1.2, so five consecutive refusals is 25 seconds
on one camera and 75 minutes on another; the same number on the same page would
mean two different things. A duration is also what the message can state as an
observed fact: "Roof has refused our credentials for the last 10 minutes."

**A duration alone breaks at the sparse end.** At a five-minute cadence, ten
minutes is *two* responses, and two is not evidence. A camera rebooting into a
firmware update, or dropped into a maintenance mode, can produce that much and
then be perfectly fine.

So both floors must be met, and the later one governs:

```
fire when (now - class_began) >= 600 AND consecutive_refusals >= 10
```

Each floor does a job the other cannot, and which one binds is decided by the
cadence. They cross at a **one-minute interval**, where ten refusals and ten
minutes are the same instant:

| Interval | Ten refusals takes | Fires after | Binding floor |
|---|---|---|---|
| 5 s | 50 s | 10 min, by which point 120 refusals | time |
| 60 s | 10 min | 10 min | both at once |
| 5 min | 50 min | 50 min, 10 refusals | count |
| 15 min | 2 h 30 m | 2 h 30 m, 10 refusals | count |

The last row is not worth engineering around. A 15-minute cadence yields 96
frames a day, below the default `encode.min_frames` of 100, so that camera
produces **no video at all** and both the wizard and `timelapse test` already
say so. A setup that sparse has a louder problem than a late alert.

### The transport is the open question

The capture daemon has never made an outbound connection in its life, and that
is deliberate rather than incidental. Three ways out, none free:

1. **Send from the daemon**, importing `post_webhook()` from
   `timelapse_encode`. Breaks the daemon-independence rule held since 0.0.1.
2. **A third copy of the transport** in the daemon. Keeps independence, and the
   redaction rule is already duplicated this way with a test pinning the copies
   together, so there is precedent. It is still a third copy.
3. **A timer-invoked checker** that reads `capture.json` and sends through the
   encoder's existing `notify()`. The daemon stays connectionless and publishes
   facts, which is exactly what item 2 built it to do. Costs a new unit, and
   therefore `sync_units()`, the installer's restart list and a `ReadWritePaths`
   review.

Option 3 is the recommendation: it is the only one where the thing that knows
and the thing that sends stay separate, and the state file already carries
everything the check needs.

### Traps

- **Report the observation, not the diagnosis.** "The camera rejected our
  credentials" is a fact; "your password is wrong" is an inference, and this
  project has already shipped two checks that guessed.
- **The clock and the counter reset together**, on any success and on any
  change of class. A tick that times out between two refusals is an
  `unreachable`, not a refusal, so an intermittent mix never accumulates
  towards either floor. That is the conservative direction and the right one:
  a camera that is sometimes refusing and sometimes unreachable is not a
  diagnosis anybody should be woken for.
- **One tick is one refusal.** `_retry_grab()` already makes its second attempt
  inside the same tick only on the first failure of a burst, and the tick still
  increments `consec_fail` by one. Keep it that way, or the counter stops being
  a count of ticks and the cadence arithmetic above stops holding.
- **Both reset when the daemon does.** Correct, since nothing is known about a
  fresh process's first fetch, but note it is now the *later* of two floors
  being reset: on a slow camera an upgrade can push an alert back by ten
  intervals rather than by ten minutes. State it rather than let someone find
  it.
- **RTSP is out of scope here.** ffmpeg holds the credential and fetches the
  frames, so an auth failure arrives as process stderr, not as a response
  anyone inspects. Say so plainly rather than implying coverage.
- **Recovery is one successful frame, and must be sent.** An alert with no
  all-clear sends somebody to fix a thing that fixed itself.
- **`explain_payload()` is in the wizard only**, and `timelapse test` prints
  raw first bytes for the same Reolink response. Whatever this reuses should
  close that drift rather than become a third copy of it.

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

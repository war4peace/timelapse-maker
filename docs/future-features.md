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

## 8. Back off from a camera that refuses our credentials, and say so

**Effort:** small, and it is **two things**, buildable and shippable
separately:

- **(a) Stop hammering.** A back-off when a camera rejects our credentials.
  This is a **defect fix in shipped code**. It lives entirely inside the
  capture daemon, and needs no egress, no new unit and no sink.
- **(b) Say so.** The notification, which was the original idea and which
  carries all of the transport cost discussed below.

**Prerequisites:** none. Item 2 shipped the state file this writes into and
item 3 shipped the sinks.

### Where it came from

Item 7 was refused because a monitoring tool sees an unreachable camera sooner
and handles it better (decided-against.md). The question that followed it was
sharper: can a rotated password be caught from the camera's own answer? It can,
and **a monitoring tool cannot catch it, because it holds no credentials**.
This is the part of item 7 that survives its own refusal, and it is a much
smaller part than item 7 was.

Part (a) then arrived from the other direction and turned out to matter more.

### The defect that (a) fixes, which is not hypothetical

**Reported from experience, 2026-08-13.** Deploying Agent DVR for the first
time: it asks for one *common* ONVIF credential pair and then probes every
camera with it. The cameras that did not share that pair **locked the account
for 30 minutes**.

Now apply that to this program as it stands. Rotate a camera's credentials
without disabling it here first, and the capture daemon presents the old ones
every `interval_seconds`, indefinitely. On firmware that locks after a handful
of failures, the lock is renewed faster than it expires, so **the account stays
locked until somebody disables that camera or stops the daemon**. Entering the
new password on the camera does not clear it, because we are still holding the
door shut. And a camera account is commonly shared with an NVR or another
consumer, so the collateral is not confined to us.

That is this program causing an outage on a device it was only ever supposed to
read from, and the code that does it is shipped today. It also inverts the
priorities inside this entry: the notification is the nice half, and the
back-off is the half that matters.

### Two shapes, and only one of them is an HTTP status

| The camera says | Which cameras | Where it lands today |
|---|---|---|
| **401**, occasionally 403 | Anything doing digest or basic properly: Dahua, Hikvision, Axis | `raise_for_status()` in `_grab()` raises `HTTPError` naming the code |
| **200 OK with a JSON error body**, served as `text/html` | Reolink | The `\xff\xd8` check, as `response is not a JPEG (bad SOI marker)` |

A status-code-only implementation misses the second, which is the shape the
redaction rules were written around. `explain_payload()`
(`timelapse_setup.py:1186`) already parses that body and is the thing to reuse.

**The author's own deployment is mostly the first row, not the second**, which
is worth stating because the measured evidence below is all from the second.
Of seven enabled cameras: three on `/onvif-http/snapshot`, two on
`/cgi-bin/snapshot.cgi` (the Dahua and Amcrest shape), one Reolink on the query
string, one RTSP. So **six of seven use digest**, and what a wrong password
does on those endpoints has never been observed here. The gap that matters is
the ONVIF one: a snapshot endpoint that answers a failed auth with a 200 and a
SOAP fault would be the Reolink surprise in a different costume, on the most
common endpoint in this deployment.

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

**Classify on the `rspCode`, and only a known auth code may be `auth`.**
Measured against a real Reolink on 2026-08-13, with the URL shape the wizard
builds. All three of these are HTTP **200**, all three carry `code: 1` and an
`error` object, and only the first two are about credentials:

| Request | `rspCode` | `detail` |
|---|---|---|
| Wrong user and password | **-7** | `login failed` |
| No credentials at all | **-6** | `please login first` |
| A command the camera does not have | **-9** | `not support` |

So **`explain_payload()` returning a reason is not a test for authentication**:
it returns one for `-9` as readily as for `-7`. The wizard gets away with that
because it prints the reason to a human who can read it, and a classifier
cannot. Key on the code, treat `detail` as display text only (the string is
firmware and probably locale dependent; the number is not), and file anything
outside the known auth set as `other` **while logging the code**, so the set
grows from evidence rather than from guesses about a table nobody here has.

Two more measured facts that constrain the implementation. The camera serves
that JSON as `Content-Type: text/html`, so nothing may test the content type.
And the camera's root page is a 21 KB HTML document, also 200: a URL typo that
drops the query string lands there, produces no `error` object, and correctly
falls out as `other`.

The general rule behind this: a camera part way through a firmware update, or
in a maintenance mode, answers 200 with something that is not a JPEG too.
Calling any of that a refusal would be an invention. A 401 declares itself; a
200 declares itself only through a code we recognise.

### The ladder: two strikes, ten minutes, one more, then speak

Proposed 2026-08-13, and it **replaces** an earlier "ten refusals AND ten
minutes, whichever comes last" rule rather than amending it. The two cannot
coexist: backing off is precisely a decision not to accumulate ten refusals.

1. **Two consecutive `auth` ticks.** Cheap insurance against a one-off.
2. **Stop fetching this camera for 10 minutes.**
3. **One attempt.** Success resumes the normal cadence and the incident ends.
4. **Still refused: notify**, once.

**Spacing beats counting, which is why the rule it replaces was worse than it
looked.** Ten rapid refusals sample one instant of a device's life ten times
over. Two attempts ten minutes apart sample two different conditions, and a
camera part way through a firmware update or a reboot is in a different state
by the second one. So the ladder is not only safer, it is *faster* where the
old rule was slowest: at a five-minute cadence that rule needed 50 minutes to
collect ten refusals, and this reaches a verdict in about fifteen.

**What a wrong back-off costs** is ten minutes of one camera's frames: 120 of
roughly 17,000 in a day at a 5-second cadence, under 1% of `Cov%`. If the
refusal was genuine, those fetches would have failed anyway, so the expected
cost is lower still. That is what makes two strikes enough rather than five.

**Only the `auth` class backs off.** An unreachable camera must keep being
tried at cadence: the fetch costs nothing, nobody is being locked out, and
recovery should be immediate when it comes. Backing off there would turn a
30-second reboot into a 10-minute hole, and it is the reboot case that
[decided-against.md](decided-against.md) already refused a feature over.

**Suppress the intra-tick retry for `auth`.** `_retry_grab()` makes a second
attempt inside the first failing tick, which against a rejected credential
cannot succeed and is pure lockout fuel. Suppressing it puts the total at
**two** authentication attempts before the daemon goes quiet, comfortably under
the handful that firmware tends to allow.

**After the notification, keep probing, slower, and never stop.** The proposal
leaves this open and the answer matters: resuming the normal cadence would
re-lock the account immediately. An escalating interval (10 minutes, then 30,
then 60, capped) is what escapes a lockout window that cannot be measured from
here, and the one measured window in evidence is **30 minutes**, which a fixed
10-minute probe would sit inside and might keep renewing. Never stopping is
what keeps "fix the password and walk away" true; a daemon that gave up would
need a restart to notice the fix.

**A camera can reject a correct password.** Once a lockout is running, the
right credentials fail too, so the notification can arrive after the operator
has already fixed things. One more reason the message states the observation:
"the camera rejected our credentials" stays true throughout, where "your
password is wrong" would be false and infuriating.

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
  project has already shipped two checks that guessed. It also covers a case
  that is not a wrong password at all: a config saying `basic` where the camera
  wants `digest` produces the same 401, and the wizard already offers to retry
  with the other scheme for exactly that reason.
- **The ladder resets on a success and on any change of class**, both of which
  end the incident. A tick that times out between two refusals is an
  `unreachable`, so an intermittent mix never climbs the ladder at all. That is
  the conservative direction and the right one: a camera that is sometimes
  refusing and sometimes unreachable is not a diagnosis anybody should be woken
  for.
- **A restart re-enters at step 1.** The ladder lives in memory, so every start
  of the daemon spends two more authentication attempts on a camera that is
  already refusing. Ordinarily that is nothing, but a unit in an `on-failure`
  restart loop would be back to hammering the account through the very
  mechanism this exists to stop, just at a slower rate. Persisting the ladder's
  position beside the rest of the runtime state is the obvious answer, and it
  should be decided deliberately rather than by omission.
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

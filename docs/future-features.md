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

**Row 1 measured on two different makes, 2026-08-14**, presenting one wrong
credential exactly once to each:

| Camera, endpoint | No credentials | Wrong credentials |
|---|---|---|
| Dahua, `/cgi-bin/snapshot.cgi` | 401, digest challenge, empty body | **401**, 27 bytes `text/plain`: `Invalid Authority!` |
| Hikvision, `/onvif-http/snapshot?Profile_1` | 401, digest challenge, 255 bytes of HTML | **401**, 233 bytes of HTML: `Digest authentication information verification failed` |

**The feared ONVIF shape does not exist here.** The worry was that an ONVIF
snapshot endpoint might answer a failed auth with 200 and a SOAP fault, which
would have been the Reolink surprise in a different costume on the *most*
common endpoint in this deployment. It answers 401 like any other digest
endpoint, so the design needs no third branch.

**All three shapes in this deployment are now measured**: three Hikvision on
`/onvif-http/snapshot`, two Dahua on `/cgi-bin/snapshot.cgi`, one Reolink on
the query string (below), one RTSP which is out of scope and disabled.

**Every vendor's body is different and not one of them is needed.** Dahua
answers `text/plain`, Hikvision `text/html`, Reolink JSON declared as
`text/html`; and both digest vendors return a *different* body for the
challenge than for the rejection. The only thing stable across the two is the
status code, which is exactly what the classifier is specified to read. Design
confirmed by measurement rather than assumed, and the reason to resist anyone
later "improving" it by parsing the prose.

One negative result worth keeping: `/onvif-http/snapshot?Profile_1` returns
**404** on the Dahua, so endpoint paths are not portable between makes even
within one fleet, and a measurement on one camera says nothing about another.

**A fourth shape exists, and "ONVIF" is the trap word.** The Tapo TC40's ONVIF
service on port 2020 was probed the same way. `GetSystemDateAndTime`, which the
spec requires to work unauthenticated, answers 200. `GetDeviceInformation`
answers **HTTP 400** with a SOAP fault `ter:NotAuthorized` and the reason
`Authority failure`, *identically* whether no security header is sent or a
correctly formed WS-Security UsernameToken carrying a wrong password.

So the word names two unrelated behaviours in this very config:

| Spelled | Protocol | Failed auth |
|---|---|---|
| `/onvif-http/snapshot?Profile_1` | plain HTTP, digest | **401** |
| `/onvif/device_service` | SOAP over HTTP | **400** plus `ter:NotAuthorized` |

Anyone reading "the ONVIF cameras" as one behaviour would be wrong about half
of them. This program only ever fetches the first, so the SOAP shape is **out
of scope** and is recorded only so that nobody later assumes the two are the
same thing. It does reinforce the principle the entry already rests on: an
authentication failure is not reliably a 401, and it is only the transport we
actually use that makes the status trustworthy.

**The wireless camera is not slower**, which is worth writing down because it
was expected to be. Round trips of 6 to 12 ms across five runs, against 8 ms
for the Hikvision, 38 ms for the Dahua and 26 ms for the Reolink on the same
pass. On a LAN, wireless is not a reason to want a longer per-camera timeout.

**How to measure this safely**, since the cost of getting it wrong is a locked
account for half an hour. An unauthenticated GET presents no credential and so
cannot count toward a lockout, yet it already reveals whether an endpoint
challenges with 401, redirects to a login page, or answers 200 with a fault.
Establish reachability and shape for free first; only the question "does a
*wrong* credential differ from no credential" needs a real attempt, and one is
enough.

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

**After the notification, probe every 31 minutes, flat, and never stop.**
Decided 2026-08-13 against an escalating ladder. The observed lockout is **30
minutes** across several cameras of different makes in this deployment, so the
interval is that window plus a minute of margin, and the constant should carry
that measurement in a comment beside it.

Flat beats escalating here for a reason that generalises past the one
deployment: lockout policies are near-universally "N failures inside a window",
and **one attempt every 31 minutes will essentially never reach N** on any such
policy. An escalating ladder buys protection against a longer window at the
cost of a much slower recovery, and the thing it protects against is one this
cadence already cannot sustain. If somebody reports a window longer than 31
minutes, that is evidence and the constant moves; it is not a reason to guess
upward now.

**The cost is bounded and the user has a faster route anyway.** Worst case is
about 25 minutes of one camera's frames after the credentials are fixed. But
rotating a camera's password already requires reconfiguring it here, so the
operator who wants those frames back runs the camera command they were going to
run regardless, and the wait is only for the operator who changes nothing.
Never stopping is what keeps "fix it and walk away" true; a daemon that gave up
would need a restart to notice.

**One notification per incident, and no repeats.** The 31-minute probe is
silent. A camera stuck refusing all day is not worth a message every half hour,
and it is already carried by the nightly summary's `Cov%`, which is the
recurring reminder this does not need to duplicate. Recovery sends a single
all-clear, which is an end, not a repeat.

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
- **A restart re-enters at step 1, deliberately.** The ladder lives in memory
  and is not persisted: a fresh process knows nothing, two attempts settle it,
  and business as usual is the right behaviour. One interaction to keep in
  view rather than design around: `timelapse-capture.service` ships
  `Restart=always` with `RestartSec=15`, and systemd's default start limit
  never trips at that spacing, so a daemon crash-looping for some unrelated
  reason would spend two authentication attempts every 15 seconds. That is a
  crash-loop problem rather than this feature's, but it is the one path where
  the back-off does not hold.
- **Decide what to do about redirects.** `requests` follows them by default, so
  an endpoint answering `302` to a `/login` page arrives as a 200 HTML document
  and classifies as `other`, with the redirect invisible. This shape was
  observed on a **non-camera** device on the same LAN while probing, so it is
  plausible rather than measured: firmware with a modern web front end could
  behave the same way. A redirect away from a snapshot endpoint is never going
  to yield a JPEG, so `allow_redirects=False` would make the failure legible
  instead of disguising it as a content problem. Worth deciding rather than
  inheriting.
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

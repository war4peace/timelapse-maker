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
0, 2 and 3 shipped in 0.1.6. Items 1, 4 and 5 were refused and moved to
[decided-against.md](decided-against.md), where the arguments for them are
recorded rather than left to be re-made.

Item 3 shipped **narrower than it was planned**, and that is worth noting for
the next entry that lists candidates: it proposed ntfy, gotify and email, and
what was actually wanted was ntfy and **Telegram**, which the entry never
mentioned. Email was refused as a different job with a different failure mode.
A pre-plan is a starting point for the conversation, not the specification.

Everything below is **researched against the code as of 0.1.5**, so each entry
says where it would hook in and what is already there. None of it is designed;
these are pre-plans, and the traps are the point.

---

## 7. Alert on repeated snapshot failures

**Effort:** small. **Prerequisites:** none; item 3 shipped the sinks and item 2
shipped the counter this would read. **Default OFF**, and that is part of the
design rather than a courtesy.

### Where it came from

It is what survived items 4 and 5. Both were refused (decided-against.md), and
this is the part of them that was actually wanted: not detecting a frozen
image, not restarting anything, just saying "Court180 has failed 200 times in a
row" to whoever asked to be told.

### What exists

Everything except the decision to send. `consec_fail` is maintained per camera
in the capture daemon, `capture.json` publishes it once a minute, and the
notify sinks take a title, a body and a severity. The daemon already logs the
first failure of every burst and the recovery line with its total.

### Shape

A threshold in the capture config, checked where `consec_fail` is incremented,
firing once per burst and once more on recovery. Never from the encoder: the
encode runs at 00:05 and an alert about a camera that died at 09:00 the
previous morning is not an alert, it is a history lesson.

### Traps

- **Default OFF, and generous when on.** The refusal of item 5 turns on a real
  case: a camera rebooted every dawn by a Home Assistant automation is
  unreachable for a minute by design. A threshold that fires on that trains
  people to ignore the channel, which is worse than never having sent it.
- **Once per burst, not once per failure.** At a 5-second cadence an overnight
  outage is 5,000 ticks. The log already solved this with
  `log_every_n_failures`; a notification has no such tolerance, so it needs
  edge-triggering rather than throttling.
- **Recovery must be as loud as the failure.** An alert with no all-clear
  leaves somebody driving home to check a camera that fixed itself.
- The capture daemon has never made an outbound connection in its life. It
  would need `post_webhook()`, which means either importing from
  `timelapse_encode` (breaking the daemon-independence rule that has been held
  since 0.0.1) or a third copy of the transport. Neither is obviously right,
  and that choice is the whole design question here.

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

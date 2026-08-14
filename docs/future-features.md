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
recorded rather than left to be re-made. **Item 8 shipped in 0.1.6** and was
removed from this file on the usual rule; the design lives in architecture.md
§4.1 and §4.2 now, where the next person will meet it. It is worth remembering
how it arrived, because it is the argument for keeping refusals rather than
deleting them: it existed *because* of the reasoning that killed item 7, and it
turned out to be a defect fix rather than a feature. Item 9 came the same way,
out of the decision against an ONVIF library: it is the part of that idea which
needs no dependency.

Item 3 shipped **narrower than it was planned**, and that is worth noting for
the next entry that lists candidates: it proposed ntfy, gotify and email, and
what was actually wanted was ntfy and **Telegram**, which the entry never
mentioned. Email was refused as a different job with a different failure mode.
A pre-plan is a starting point for the conversation, not the specification.

Everything below is **researched against the code as of 0.1.5**, so each entry
says where it would hook in and what is already there. None of it is designed;
these are pre-plans, and the traps are the point.

---

## 9. Find cameras with WS-Discovery

**Effort:** small to medium. **Prerequisites:** none.

### What it is

A UDP multicast Probe to `239.255.255.250:3702`, to which ONVIF devices answer
with their device service address and a set of scopes. Stdlib sockets and a
regex; no SOAP stack, no WSDL, no dependency. That is why it survived the
decision against an ONVIF library (decided-against.md): it is the one ONVIF
capability worth having here, and it is the one that costs nothing to have.

### What it would buy

The wizard asks for each camera's IP by hand. It would offer a list instead:
address, model and hardware read from the scopes, so an operator picks rather
than types, and the vendor template can be pre-selected from the name it
reports.

### What it does not buy, stated up front

Discovery returns a **device service address and a name**. It does not return a
snapshot URL. Getting from one to the other still needs credentials plus
`GetProfiles` and `GetStreamUri`, which is the roughly 150 lines already
prototyped in `temp/onvif_snapshot_uri.py`. So the honest scope is "fills in
the address and preselects the template", not "configures the camera".

### Traps

- **Multicast does not cross subnets, and rarely crosses VLANs.** A dedicated
  camera VLAN is common in exactly the deployments that have many cameras, so
  this must be an offer and never the only path, and finding nothing must not
  be reported as "there are no cameras".
- **One device can answer several times**, from several addresses, and NVRs
  proxy their cameras. Deduplicate on the device UUID in the response, never on
  the address.
- **The advertised XAddr is not always reachable from here.** Verify by
  fetching, exactly as `test_onvif_profiles()` already does, rather than
  trusting the advertisement. The TC40 measured on 2026-08-14 is the general
  lesson in miniature: it advertises a JPEG profile that renders as garbage, so
  **an ONVIF advertisement is a claim, not a guarantee.**
- **A response is not necessarily a camera.** NVRs, encoders and doorbells all
  answer. The scopes carry a type; use it, and let the operator overrule it.
- **Keep it in the wizard.** The wizard runs as root and outside a unit, while
  the daemons run sandboxed; a multicast bind is exactly the kind of thing that
  would work here and fail under `RestrictAddressFamilies`. The bind probe for
  the web UI already lives in the wizard for the same reason.
- Collect for a fixed window of two or three seconds rather than waiting for
  quiet, since there is no end-of-list marker.

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

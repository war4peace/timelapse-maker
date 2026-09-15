# Future features

Planned work, in implementation order. Nothing here is committed to a release; items move up and down as reality intervenes.

Things considered and turned down live in [decided-against.md](decided-against.md). Move an item there rather than deleting it, so the next person does not re-propose it from scratch.

**Ordering rule.** The list is double-ordered:

1. Easiest first.
2. *Except* when a harder feature is a prerequisite for an easier one, then the prerequisite comes first, however painful it is.

So the list is not a difficulty ranking. It is a build order. When adding an item, find the earliest position where all of its prerequisites are already above it, then place it by effort among the remaining candidates. Say what an item depends on explicitly, or the ordering rots.

**Shipped items are removed, not ticked.** The web UI plan (phases 1a-1f) was deleted from this file at 0.1.6 because it was finished; the CHANGELOG says what shipped and architecture.md §4.5 says how it works. The one part worth keeping was the library survey that justified the filename parser, which is now architecture.md §9a. The re-encode defect that briefly sat at the top of this list as item 0 went the same way once it was fixed: the invariant it established is in architecture.md §3, where the next person will actually meet it. Item 11, the Windows port, shipped at v0.2.0; its research is kept as architecture.md §9b, because it is the record of what was measured before any of it was built, and one line per shipped part is in patch-notes.md.

**A number identifies an item; its position orders it.** Numbers are never reused, so that a commit or a CHANGELOG entry referring to "item 3" still means something a year later. Items 0, 2, 3, 8, 9, 10 and 11 shipped; items 1, 4, 5, 6 and 7 were refused and moved to [decided-against.md](decided-against.md), where the arguments for them are recorded rather than left to be re-made.

---

## Contents

- [What this file taught, while it had things in it](#what-this-file-taught-while-it-had-things-in-it)

**Nothing is planned at the moment.**

## What this file taught, while it had things in it

Worth keeping, because these are the reasons to write the next entry down rather than just building it.

**Most items arrived as the residue of a decision about something else.** Item 8 existed *because* of the reasoning that killed item 7, and turned out to be a defect fix rather than a feature. Item 9 was what remained of the decision against an ONVIF library: the one part of that idea needing no dependency. Item 10 was the same story a third time, left over from the IPv6 camera fix in 0.1.7. That is the argument for writing refusals down rather than deleting them: the good idea is often next to the rejected one.

**Researching an "obvious" fix changed what it was.** Item 10's entry started out claiming `check_bind()` needed changing to match the server. Checking instead of assuming showed the opposite, that it already walked every family `getaddrinfo()` returned and was therefore *passing an address the service would refuse*. A missing feature turned out to be a defect, which moved it to the front of the list. The design is in architecture.md §4.5 now.

**A pre-plan is a starting point, not a specification.** Item 3 shipped narrower than planned: it proposed ntfy, gotify and email, and what was actually wanted was ntfy and **Telegram**, which the entry never mentioned. Email was refused as a different job with a different failure mode.

**An entry can mislead by what it omits.** Item 6 proposed remote library browsing over SFTP and never said that the web UI already browses any readable path, so it read as though the browser were missing. It was refused on 0.1.9 partly on that basis: once the entry said what already worked, the remaining audience was too narrow to justify giving the network-facing service an SSH key to the whole archive. **Say what already exists before saying what is missing.**

**Research produces defect reports, not just plans.** Item 11 set out to scope a port and found a live bug in the current release along the way (`check_bind()` cannot see a port in use on Windows, and the atomic-write idiom can fail there). Both are Windows-only and neither depends on the port being built. This is the second time an entry has done this, after item 10. **Look for what the research says about today's code, not only about the proposed feature.**

<sub>[&uarr; Contents](#contents)</sub>

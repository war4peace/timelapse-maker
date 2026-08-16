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
something a year later. Items 0, 2, 3, 8, 9 and 10 shipped; items 1, 4, 5, 6
and 7 were refused and moved to [decided-against.md](decided-against.md), where
the arguments for them are recorded rather than left to be re-made.

---

## Item 11: a Windows variant, sharing the Python

**Status: research only.** Nothing is designed and nothing is committed. This
entry exists to record what was measured on 2026-08-15, so the decision to
build it or refuse it is made against facts rather than against an impression
of how hard Windows is.

**Constraint from the outset:** the Python must stay common. Two forks of
`timelapse_capture.py` is the outcome this entry exists to avoid, and the
project has already written down what happens when one copy of a rule drifts
from another (the `RedactingFormatter` duplication is pinned by a test for
exactly that reason).

### 11a. Why this is worth considering at all

The deployment model this project already documents is "same host as the NVR"
(install.md §4 Common snags exists because of it). On Windows that host is
usually **Blue Iris**, which is Windows-only and has no Linux equivalent, or
Agent DVR running on Windows rather than Linux. Those users cannot run this
tool at all today, and the continuity problem in §1 of architecture.md is not
a Linux problem: an NVR ties recording to the camera session on either
platform.

**The one thing that is certain: Blue Iris offers no timelapse feature at
all.** Reported by the operator 2026-08-15, who ran it. It saves snapshots,
and building a video from them is left to you. So on Blue Iris this tool is
not competing with a worse timelapse; it is supplying a missing one. That
argument needs no qualification and is the one to lead with.

**The "Blue Iris snapshots are lower quality" argument does not survive
measurement, and must not be used.** The suspicion was reasonable: Blue Iris
never asks for snapshot credentials or a snapshot URL, where Agent DVR does,
which suggests it takes frames from the RTSP stream it already has. And the
operator's Blue Iris era timelapses did look worse. But the library holds both
eras, so it can be checked rather than argued, and the result is that the
**encode** explains it:

| Same camera, same 3840x2160, same AV1, same 60fps | Bitrate | bits/px/frame | Colour |
|---|---|---|---|
| `Street4K` 2024-04 / 2024-05 (Blue Iris era) | 12.2 to 19.3 Mb/s | 0.026 to 0.041 | space untagged |
| `Street4K` 2026-08 (this tool) | 38.2 Mb/s | 0.081 | space=bt709 |

Two facts kill the resolution theory outright. `Street4K` is **3840x2160 in
both eras**, and `Court180` is **3040x1368 in both eras** (that odd figure is
a 180 degree panoramic camera's native output, not something an NVR did to
it). Nothing was downscaled. What actually differs is that the current tool
spends **two to three times the bits per pixel** on identical input, which is
exactly what the old pipeline's `-cq 40 -preset p2` predicts against this
project's `av1_cq 26` / `av1_preset p6`.

**The old command is also the "tagged without converting" predecessor that
architecture.md's colour rule exists because of.** It was
`-vf "hwupload_cuda" -c:v av1_nvenc -preset p2 -tune hq -cq 40`: JPEG decodes
full range, nothing converts it, and the AV1 output is nonetheless tagged
`range=tv`, which is precisely the trap the pipeline's
`in_range=full:out_range=limited` plus explicit bt709 tagging now avoids. The
probe confirms the old files carry `range=tv` with **no colour space at all**.
So there were two independent picture defects in the old encode, either of
which is visible, and neither of them is Blue Iris's fault.

**What remains possible and is still unmeasured:** a frame pulled from an
H.264/H.265 main stream carries inter-frame compression artifacts that a
direct JPEG snapshot does not, *at the same resolution*. That could not be
tested from the library, since both eras were re-encoded to AV1 afterwards.
So the honest position is that Blue Iris's snapshots may or may not be
artifact-laden, and nobody knows. **Claim only that Blue Iris has no timelapse
feature.** Do not claim the snapshots are worse, and note that if the RTSP
theory is right, `method: "rtsp"` in this project would draw from the same
stream and inherit the same artifacts.

**Do not let any of this regress the project's positioning.**
architecture.md §1 says the gap is continuity and *not* "NVRs give you low-res
timelapses", and that correction stands, now with more force than before: the
one time this project's own history offered evidence for a resolution gap, the
evidence turned out to be an encoding difference in *our* predecessor script.

**The cheap alternative is WSL2 and it must be priced in before any of this is
built.** The existing Linux tool runs there unmodified; it is already the
project's test bed, on real systemd 255. A Windows user can have every feature
today with no new code. What that costs them, and it is not nothing:

- **Frames must live inside the WSL2 filesystem, not on `/mnt/d`.** The 9p
  bridge to Windows drives is slow enough to matter for a daemon writing a
  JPEG per camera per interval, and slower again for an encoder reading ten
  thousand of them back. So the frames sit inside a VHDX the user cannot
  browse naturally, which is precisely the sort of surprise this project's
  docs otherwise work to avoid.
- **WSL2 does not start on boot.** It starts when something asks for it. An
  unattended recorder that only records once somebody logs in and opens a
  terminal is not unattended, and the shims for this (a scheduled task running
  `wsl -d Ubuntu -u root -e /bin/true`, or `boot.systemd` plus a Task
  Scheduler trigger) are exactly the sort of undocumented local hack the
  installer exists to replace.
- **Networking.** NAT mode puts the web UI behind a port proxy that must be
  re-established after each reboot because the guest IP moves. Mirrored mode
  (Windows 11 22H2+) fixes this properly, but it is opt-in and not universal.

None of that is fatal, and for a technical user WSL2 is genuinely the right
answer. **The case for a native port is the non-technical user**, which is the
same audience `install.sh` and the wizard already exist for. If we are not
prepared to serve that user, refuse this item and document WSL2 instead: a
half-built port serves nobody, and "run it in WSL2" is a complete answer that
costs one page of docs.

### 11b. What already works, measured

Say what exists before saying what is missing; this file learned that from
item 6.

**The suite passes on Windows today: 1,333 tests, 1 skipped**, run on this dev
box with `python -m unittest discover -s tests -t tests -p 'test_*.py'`. The
single skip is the documented SO_REUSEADDR bind test. That number is the
strongest single argument that the shared-Python plan is realistic, though be
careful how much weight it carries: the suite tests logic, and almost none of
it starts a service or touches systemd.

Also already true, and all of it verified rather than assumed:

- **Imports are stdlib plus `requests`, on every script.** `pwd` is imported
  in exactly two places (`timelapse_encode.whoami`, and the CIFS mount helper
  in `timelapse_setup`), both already inside a `try` or a function that can
  fail. There is no module-level POSIX import to trip over at startup.
- **`paths.ffmpeg` is already a config key**, asked by the wizard and
  defaulted from `find_binary()`, and `choose_ffmpeg()` already verifies the
  answer by running the binary. A Windows default is a different string, not
  different code, and `shutil.which` honours `PATHEXT` so `ffmpeg.exe` is
  found normally. See 11c.6a for the two small additions this needs.
- **ffmpeg's concat demuxer accepts native Windows paths.** Measured on ffmpeg
  8.1: `write_concat_list()`'s current output, `file 'C:\...\070001.jpg'`,
  encodes correctly, and so does the `as_posix()` form. **Do not "fix" this
  into forward slashes.** Both work, and the backslash form is what `str(p)`
  already produces, so the code needs no change here at all. The BOM rule and
  the `'\''` escaping are unaffected.
- **`is_remote_spec()` already handles drive letters**, and its docstring says
  so: `os.path.isabs()` is tested before the colon test, so `D:\videos` is a
  path and `nas:videos` is a remote. UNC (`\\nas\share`) is absolute on
  Windows and therefore also settled correctly.
- **`check_bind()`'s errno comparisons are already right.** Python on Windows
  aliases `errno.EADDRINUSE` to 10048 and `errno.EADDRNOTAVAIL` to 10049, the
  WSA codes, so the existing `exc.errno ==` tests match. The bug is elsewhere;
  see 11d.
- **`host_addresses()` degrades correctly.** It shells out to `ip -j addr`,
  which does not exist on Windows, but its docstring already states it is
  display-only and that validation never depends on it. It returns an empty
  list and the wizard prints one fewer line.
- **`select.select()` is called on sockets only** (the WS-Discovery collector),
  which is the one thing Windows `select` supports.
- **The capture daemon is very nearly portable already.** Two hardcoded FHS
  strings in 1,150 lines, and nothing else: `STATE_DIR_DEFAULT` and the
  `sys.argv[1]` config default.
- **CRLF is already pinned.** `.gitattributes` sets `eol=lf`, which is why the
  0.1.4 `write_text()` incident never reached the repo. That protection stays
  correct for a Windows port. See the exception in 11d.

### 11c. What has to be built

Grouped by what it actually is, not by file. The count of coupling points, by
grep across `scripts/`: 10 `systemctl` call sites, 1 `journalctl`, 7 `mount`,
4 `rsync`, 2 `runuser`, 2 `mount.cifs`, and 29 user-facing strings containing
`sudo `.

**1. A platform module, not sprinkled branches. BUILT 2026-08-16.** The single
most important structural decision here. `if os.name == "nt"` scattered through
six scripts is how the two-forks outcome arrives by increments. One new file,
`timelapse_platform.py`, answering a small closed set of questions: where the
config lives, where state lives, how to ask whether a service is running, how
to restart one, what to tell an operator to type, how to secure a file holding
passwords, which disks could hold frames. Every existing call site is a call
into it. This is a prerequisite for everything else in this item, which under
the ordering rule is why it is first despite being the least visible.

It shipped as an **extraction**: the Linux answers moved in unchanged and the
existing suite held the line, which is why the diff carries no new Linux
behaviour at all. Two questions in the list above are deliberately *not*
answered yet, because each is the substance of a later step rather than a
mechanical move: **reading a service's recent log** and **copying files to a
destination**. The first is the web UI's Log tab (step 5), the second is
transfer (step 4). See architecture.md §4.6a for what was settled; the entry
below is the design that produced it and is left as written.

**2. Service supervision. Real Windows services, not scheduled tasks. BUILT
2026-08-16** (the hosting; see 11f step 3a for what is and is not done).
Requirement from the operator 2026-08-15, and it is right for reasons beyond
appearance. A first draft of this entry recommended Task Scheduler and that
recommendation is **withdrawn**.

One correction to the objection as stated, because it changes nothing about
the conclusion but should not go into the docs wrong: a scheduled task set to
"run whether the user is logged on or not" runs in session 0 with **no console
window at all**, so Task Scheduler does not in fact leave a CMD window open.
The real arguments against it are functional:

- It does not appear in `services.msc`, which is where a Windows administrator
  looks, and the status page would be reporting something no other tool on the
  box agrees is a service.
- No `sc failure` recovery actions (restart after N ms, escalating, reset
  counters), which is the closest analogue to `Restart=always` `RestartSec=15`.
- No service dependencies. The units use `After=network-online.target`, and the
  Windows equivalent is a dependency on `Tcpip` (and on `LanmanWorkstation` if
  the transfer destination is a UNC path). A scheduled task at startup has no
  way to express that and would race the network on every boot.
- No delayed auto-start, which is the standard answer to a recorder that
  starts before its NAS is reachable.

So the options are the three that produce a genuine service:

- `sc.exe create` pointed at a plain `python.exe` **does not work**: the SCM
  expects `StartServiceCtrlDispatcher` within about 30 seconds and kills what
  does not answer, reporting error 1053, which reads as a broken script rather
  than a wrong hosting model.
- `pywin32` gives a real service and breaks the stdlib-only rule with a large
  native dependency. `requests` is the one exception this project allows, and
  it is load-bearing across every camera fetch in a way this would not be.
- **WinSW** or NSSM wrap any executable as a genuine service. WinSW is a
  single MIT-licensed exe plus an XML file, and it is what Jenkins ships. It
  means vendoring a binary, or downloading one at install time and then owning
  the same "is this really the installer" verification problem that
  `timelapse update` already solves for `install.sh`.

**Recommendation: implement the SCM handshake in `ctypes`, which is stdlib.**
This makes the Python process a real service with no dependency at all.

**Prototyped and measured 2026-08-15**, `temp/win_service_proto.py`, Python
3.12.10 64-bit. The bindings for `SERVICE_TABLE_ENTRYW`,
`LPSERVICE_MAIN_FUNCTIONW`, `LPHANDLER_FUNCTION_EX`, `SERVICE_STATUS`,
`StartServiceCtrlDispatcherW`, `RegisterServiceCtrlHandlerExW` and
`SetServiceStatus` all marshal correctly: run from a console the dispatcher
returns FALSE with **error 1063** (`ERROR_FAILED_SERVICE_CONTROLLER_CONNECT`),
which is the documented "you were not launched by the SCM" and is exactly what
must happen. It did not crash, did not raise, and reached the SCM, which is
what proves the structure layout is right. `OpenSCManagerW` connects
read-only unelevated and is refused with error 5 for write, so the same file
also detects elevation without a separate check.

**The elevated end-to-end is PROVEN**, run by the operator 2026-08-15 as
Administrator. The full lifecycle worked on the first attempt:

```
StartServiceW -> True          state after 3s: RUNNING
service_main entered; reporting START_PENDING
reporting RUNNING
tick 1 .. tick 7               (the daemon loop, once a second)
control 1 -> stopping          (a real SERVICE_CONTROL_STOP from the SCM)
loop left after 7 ticks; reporting STOPPED
dispatcher returned TRUE       (service has fully stopped)
```

Every clause of the SCM contract held: the dispatcher connected, `service_main`
ran on the SCM's thread, `START_PENDING` then `RUNNING` were accepted, the
control handler received a genuine stop, and `StartServiceCtrlDispatcherW`
returned **TRUE**. That last line is the one that matters most, because it
means the SCM was satisfied the service shut down cleanly rather than having
given up and killed the process. No 1053 anywhere. The service also ran as
**LocalSystem** with its interpreter under a user profile path, which worked,
though a real install should not rely on that.

**So the stdlib-only rule survives the port.** No pywin32, no vendored WinSW
binary, no scheduled-task compromise for the two daemons.

Four things the prototype settled that the real implementation must not lose,
three of which were bugs found while writing it:

- **Every ctypes function returning a `HANDLE` must declare `restype`.**
  Without it ctypes assumes `c_int`, which is 32-bit, and silently truncates a
  64-bit `SC_HANDLE`. It works whenever the SCM hands out small handle values,
  which makes it the kind of defect that passes here and fails on someone
  else's machine. This is a project-wide rule for the platform module, not a
  detail of the prototype.
- **Nothing in the service path may `print()` unguarded.** Under the SCM there
  is no console and `sys.stdout` may be `None` or a dead handle, so a stray
  print kills `service_main` and the failure presents as "the approach does
  not work". A logging call that cannot raise is a hard requirement, which
  sits well beside the existing rule that a log call cannot know what it is
  about to print.
- **The service entry point needs its own exception guard**, because an
  exception escaping a ctypes callback goes to stderr, and stderr under the
  SCM is nowhere. The service simply appears to hang.
- **The `binPath` must quote the exe and the script separately.** The dev
  box's Python lives under a directory with a space in it, which is the normal
  case for a per-user Python install.

And one behavioural requirement: the control handler must report
`SERVICE_STOP_PENDING` with a wait hint *before* doing slow shutdown work, or
the SCM calls the stop hung. The capture daemon has camera threads to join, so
this is not hypothetical.

The nightly encode and the credential watch do **not** need this. They are
batch jobs, not daemons, so Task Scheduler is the correct host for them and
`sc.exe` would be wrong: a service that exits immediately is a service in a
permanent restart loop. This mirrors the existing split exactly, where capture
and web are `.service` units and the other two are `.timer` units. A daily
trigger at 00:05 translates the encode timer directly, and a 5-minute repeat
translates the watch.

**2a. Which account the services run as.** Decided by the operator
2026-08-15: **an administrative account**, on the precedent that Blue Iris
runs that way on the same class of machine.

That is a reasonable call and it solves the hardest problem in this entry in
one move. The Linux design's unprivileged `timelapse` user works because a
root-owned 0600 credentials file plus a system-wide CIFS mount lets an
unprivileged process reach the NAS while holding no secret at all. Windows has
no equivalent, so the choice really was "privileged account that can reach the
share" or "least-privilege account that cannot", and an unreachable
destination is not a security feature.

Two notes, one practical and one that should be a rule.

Practical: **a real user account and LocalSystem are not the same kind of
administrative.** LocalSystem is fully privileged locally and needs no stored
password, but on the network it presents the *machine* account, so a workgroup
NAS may not grant it. A virtual account (`NT SERVICE\<name>`) has the same
network identity and so does not help either. Domain Managed Service Accounts
need Active Directory, and the target deployment is a workgroup.

**If a real account is used, the operator never creates it and never sees it.**
The installer does what `install.sh` already does with `useradd --system`:

- create it with an installer-generated random password, *password never
  expires*, *user cannot change password*, so nobody ever types it and it
  lives in LSA where service passwords belong;
- grant `SeServiceLogonRight` (`LsaAddAccountRights`), without which the
  service cannot start at all;
- deny `SeDenyInteractiveLogonRight` and `SeDenyRemoteInteractiveLogonRight`,
  so it cannot be logged into even by name;
- hide it from the sign-in screen and the Settings user list, via
  `HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList`
  set to DWORD 0. **The deny rights and the hiding are separate mechanisms and
  both are needed**: denying logon does not remove the account from the
  welcome screen, it only makes clicking it fail.

**The share is authenticated, and Windows credential storage is per user.
This is the hardest remaining problem in the port.** Measured on the
operator's setup 2026-08-15: Credential Manager holds
`Domain:target=tower, Type: Domain Password, User: war`. The share is
therefore *not* public; it authenticates as `war`, and it is transparent to
the operator only because those credentials are saved **in their own profile**.

The consequence is sharp. **A service account gets its own, empty credential
store.** A freshly created `timelapse` account with an installer-generated
password inherits nothing from the operator, so the nightly transfer fails
with access denied while the operator can still browse the share perfectly
well. That is the drive-letter failure all over again, in a second disguise:
the operator sees a working share and the service sees a locked one.

Four ways out, in increasing order of how well they fit this project:

1. **Run the services as the operator's own account.** Everything already
   works, and nothing needs storing. It costs the operator's password in LSA
   and breaks whenever they change it, which for a personal recorder may never
   happen. This is effectively what "run it as an administrative account"
   meant in practice.
2. **Seed the service account's credential store** with `cmdkey`, which must
   run *in that account's logon context*; running it as an administrator puts
   the credential in the administrator's store instead. The usual technique is
   a throwaway scheduled task running as the service account, which the
   installer is already able to create. It works and it is ugly.
3. **Name the service account to match a NAS user**, so the implicit logon
   credentials succeed. Requires the operator to supply both name and password
   and keep them synchronised with the NAS, which is a standing obligation
   rather than a one-time setup.
4. **Connect explicitly at transfer time**, with `WNetAddConnection2` (or
   `net use`) before the copy, using credentials from `config.json`.
   **This is the option most consistent with what already exists**: the Linux
   side stores CIFS credentials in `/etc/timelapse/cifs-<share>.cred` at 0600
   and the tool, not the OS, owns the share credential. The config already
   holds camera passwords, `redact_config()` already covers secrets in it, and
   the file is already ACL restricted by item 7. It also makes the destination
   work identically whichever account the services end up running as, which
   removes a whole class of "works for me" support problem.

**So this strengthens the administrative-account decision rather than
weakening it**, and it retires the idea that LocalSystem might do: LocalSystem
presents the machine account, which `\\tower` would reject. An earlier draft
of this entry inferred a public share from a **truncated** `cmdkey /list`
whose output was cut at 20 lines, roughly forty entries above the one that
mattered. The lesson is one this project already wrote down for rendered text
and had not generalised: **absence of evidence in truncated output is not
evidence of absence.** Do not conclude from what a capped listing does not
show.

**Whichever option is chosen, the wizard must not reason about it, it must
test it**: write one file to the destination *as the account the encoder will
run as*, and report from the result. That is `try_rsync_args()` and
`probe_as()` applied to a new platform, and the "as the account that will do
the work" half of that rule carries far more weight here than on Linux, where
a system-wide mount made every account equivalent.

**Evidence from the predecessor, and what it does not settle.** The operator's
`MakeTimelapse.ps1`, which ran nightly on the Blue Iris machine, moved videos
with `Move-Item -Destination "\\tower\cctv\TL"` and **passed no credentials**.
So the share is definitely reachable from some context on that host. Two
useful things follow and one does not:

- It used a **UNC path, not a drive letter**, which is independent confirmation
  that the UNC form is the right one to store. The predecessor was already
  doing what 11d says the wizard must enforce.
- It passed no credentials, so it inherited whatever token launched it. **That
  token is now known**: the operator confirms it ran from a **Scheduled Task
  under their own user account** (the machine itself is gone, so this is
  recollection, but it is the operator's own configuration and there is no
  competing account it could have been). So option 1 is confirmed working in
  production for years: a scheduled task, running as a user whose Credential
  Manager holds the share credential, reaches `\\tower\cctv` with no
  credential handling in the script at all.

**This lands better than expected, because of which component needs the
share.** Transfer happens in the **encode** job, and encode is a scheduled
task in the Windows design (item 2, batch jobs stay scheduled tasks). So the
one component that touches the NAS is running in exactly the configuration
already proven to work. Capture, the only true service, writes local frames
and never touches the destination.

**One thing to verify before relying on that, and it is a real distinction:**
Credential Manager entries are protected by DPAPI keyed to the user, which
requires the **user profile to be loaded**. Task Scheduler loads the profile
for a task running as a user; a Windows **service** running as a user account
does **not** load it by default. So "run it as the operator's account" is
proven for the scheduled-task half and **not** proven for the service half.
This costs nothing today, since the service half has no reason to reach the
share, but it means **moving transfer into a service context is not a free
refactor** and would need the profile question settled first. Anyone tempted
to unify the two hosts should read this paragraph first.

Rule: **this decision must not silently extend to the web UI.** It is the only
network-facing component, it is read-only by design, and on Linux that claim
is backed by `ProtectSystem=strict` with exactly one writable directory. The
Windows port already loses that sandbox (11d); running the listener as an
administrator as well would turn one weakened property into a genuinely bad
one. The web UI is not in the first release (11f), so this does not need
answering yet, and when it does the answer should be a separate, lower
privileged account. Blue Iris being administrative is not a precedent for it:
Blue Iris is doing hardware work that needs the privilege, and this is a page
that lists files.

**3. The status page needs a new source, and the project's own rule says
which. HALF BUILT 2026-08-16**: the machine-readable sources exist
(`service_state()` through `QueryServiceStatusEx`, `task_info()` through
PowerShell as JSON) and `--unit-status` uses them. The status *page* is step 5. `systemctl show` was chosen over `systemctl status` because the
human-readable output is not a contract. The identical argument applies twice
as hard on Windows, where the human output is not merely unstable but
**localised**: `sc query` and `schtasks /query /FO LIST /V` both print field
names that change on a German or Romanian install, so any parser keyed on them
silently finds nothing on the very installs least able to debug it.

The split from item 2 carries through here, and both halves have a
machine-readable answer:

- **The two services**: `QueryServiceStatusEx` through the same `ctypes`
  binding the host already needs, which returns a struct of integers with no
  text in it at all. That is strictly better than anything `systemctl show`
  offers, since there is nothing to parse. `Get-Service | ConvertTo-Json` is
  the fallback if the binding is not wanted here.
- **The two scheduled tasks**: `Get-ScheduledTask` and `Get-ScheduledTaskInfo`
  piped through `ConvertTo-Json`, which return stable English property names
  on every locale, at the cost of a `powershell.exe` startup per call. The
  page already budgets one `systemctl show` per load and no more, so one
  PowerShell invocation per load is the same shape of budget.

The three-way daemon/timer/oneshot distinction in `STATUS_UNITS` survives the
port intact and is still needed, for the same reason it exists: `LastTaskResult`
on a task that runs nightly is "not running" for 23 hours a day, and calling
that a fault would invent one on every healthy system.

**4. Logging, which currently has no Windows answer at all.** The web UI logs
to stdout only, deliberately, because journald catches it and the unit is
scoped to one writable directory. A scheduled task's stdout goes **nowhere**,
so on Windows the web UI would produce no diagnostics whatsoever. The Log tab
is the other half of the same problem: it shells out to `journalctl`. Both are
answered by having all three services write rotating files and the Log tab
read them, but see the rollover trap in 11d before choosing a handler.

**5. Transfer, with the destination narrowed on purpose.** Decided by the
operator 2026-08-15: **a remote destination requires a writable network path
that already exists; otherwise the destination is a local path.** There is no
Windows equivalent of the `user@nas:/path` rsync spec and none is wanted.

This is the same call already made for the library at 0.1.9 (item 6's
refusal), and making it again here keeps the two consistent rather than having
transfer accept a destination shape the Library tab cannot read. It also
deletes work rather than adding it: no SSH, no keys, no credentials in the
transfer config, and no second class of destination to explain.

**For the first iteration, use `shutil.move` and nothing else.** Once remote
specs are gone the transfer is a file move to a filesystem path, and the
stdlib does that in about fifteen lines. Combined with the destination being a
hard prerequisite, this makes the wizard's probe carry the weight: if writing
one test file as the service account fails, the operator fixes it once, and
the tool ships no credential-management code for a case nobody has hit.

`robocopy` is then the upgrade, not the starting point. It covers what is
actually used (`/MOV` for `--remove-source-files`, `/R:n /W:n` for retries)
and its retries are worth having on an SMB link that blips. **Its exit codes
are a bitmask where anything below 8 is success**, which is the opposite
convention to rsync and will be got wrong once by somebody. It wins on
retries, not on capability.

**Spawning PowerShell to do the move solves nothing, and it is worth writing
down why**, because the predecessor script makes it a natural suggestion. **A
child process inherits its parent's access token.** If the service runs as an
account with no credentials for the share, `powershell.exe` launched by that
service has none either, and `Move-Item` fails exactly as `shutil.move` would.
What made `MakeTimelapse.ps1` work was the account it ran under, not the
language it was written in. The transfer's authentication is a property of the
process token, so the mover's implementation cannot affect it.

The existing `try_rsync_args()` design carries over **unchanged in spirit and
it is the important part**: probe by copying one real file, with the exact
configured flags, as the account that will do it nightly. A check that guesses
is worse than no check, and the account half of that sentence is doing more
work on Windows than it ever did on Linux (see the drive-letter trap in 11d).

**`is_remote_spec()` still matters even though Windows has no remote specs.**
The config is shared between platforms, so a `config.json` written on Linux
can name `user@nas:/path`. The Windows tool must **refuse that clearly**,
naming it as a Linux-only destination shape, rather than treating it as a
relative path and silently creating a directory called `user@nas:` somewhere.

**6. Bootstrap. BUILT 2026-08-16.** `install.ps1` beside `install.sh`, not instead of it. It
does: check for an elevated shell, find Python, lay files under
`%ProgramFiles%\timelapse`, create `%ProgramData%\timelapse`, register the
service and the tasks, run the wizard. ffmpeg is **not** its problem; see 6a.
`timelapse update` needs the matching change: it currently fetches
`install.sh` from the tag and runs `bash -n` on it before executing as root,
and the Windows path needs the same two guards against a captive portal or a
404 page arriving as a successful response. The PowerShell equivalent of the
syntax check is parsing without executing, via
`[ScriptBlock]::Create((Get-Content -Raw $path))`.

**6a. ffmpeg is the operator's to supply, and the installer must not pretend
otherwise. BUILT 2026-08-16.** Decided 2026-08-15. On Linux `install.sh` installs ffmpeg from
the distro, which works because there is exactly one ffmpeg per distro and the
package manager owns it. Windows has no equivalent worth relying on: `winget`
exists on Windows 11 but not on Server, the builds people actually run come
from gyan.dev or BtbN rather than from any package source, and a Blue Iris box
very often **already has an ffmpeg** somewhere that the operator chose
deliberately (the predecessor script in `temp/` names its own `$ffmpegPath`).
Installing a second copy would be the tool overriding a decision that was not
its to make.

So the wizard asks, exactly as it does today, and the shape is already built:
`choose_ffmpeg()` calls `find_binary()` for a default, `ask()`s for the path,
then `detect_encoders()` runs the binary and `fail()`s if it cannot. The
Windows work is three small things, none of them new logic:

- **Default from `shutil.which("ffmpeg")`** rather than `/usr/bin/ffmpeg`, then
  from a short list of the usual install roots (`%ProgramFiles%\ffmpeg\bin`,
  `%LOCALAPPDATA%\Microsoft\WinGet\Links`, `C:\ffmpeg\bin`) before giving up
  and offering no default. `which` honours `PATHEXT`, so `ffmpeg.exe` is found
  without naming the extension (11b).
- **Accept a directory as well as a file.** "The ffmpeg binaries path" is how
  operators think of it on Windows, because the zip unpacks a `bin` folder
  holding `ffmpeg.exe`, `ffprobe.exe` and `ffplay.exe` together. Given a
  directory, resolve both binaries inside it; given a file, keep today's
  behaviour. `paths.ffprobe` is a separate config key and should be derived
  from the same answer rather than asked twice.
- **On failure, say where to get it.** This is the part that does not exist
  today, because on Linux the installer has already solved the problem by the
  time the wizard runs. Name `https://ffmpeg.org/download.html` and note that
  a build with NVENC is wanted if the box has an NVIDIA card, since the
  encoder probe will otherwise report the fallback and the operator will not
  know why. Print it as a **failure with a next step**, not a warning that
  gets scrolled past: without ffmpeg there is no product.

The existing `detect_encoders()` reporting matters more here than on Linux and
must not be weakened. Its whole point is that "Unknown encoder" (a build
without it) and "No capable devices found" (a driver or GPU problem) need
opposite fixes, and on Windows the first is *far* more likely, because which
ffmpeg build you downloaded is a real variable rather than a distro constant.

**6b. The installer should end up as a GUI, and this is deferred, not
dropped.** Stated as a requirement 2026-08-15, explicitly scheduled after a
stable prototype exists. The target is what a Windows user expects: a small
executable downloaded from the GitHub release page and double-clicked, which
elevates itself, lays the files down and walks through the same questions the
console wizard asks.

The reason to write it down now rather than later is that **two decisions made
early either permit it or prevent it**, and both are cheap to honour while the
code is being written and expensive to retrofit:

- **The wizard's questions must stay separable from the way they are asked.**
  Today `timelapse_setup.py` interleaves `ask()`, `note()` and `heading()`
  with the logic that validates each answer and writes the config. A GUI needs
  the validation and the config write without the prompting, so anything new
  on the Windows path should keep "decide" and "ask" in different functions
  even where the console flow would not care. This is not a rewrite of the
  Linux wizard and should not become one; it is a rule for new code.
- **`install.ps1` must remain the thing that actually installs**, with the GUI
  as a front end over it rather than a second implementation. The project has
  been bitten by exactly this shape before: `tools/` duplicated what the
  wizard did, drifted, and was deleted. Two installers that both know how to
  register a service will disagree within one release.

Constraints worth recording before anyone starts:

- **Stdlib only is a real constraint here, and `tkinter` is the answer rather
  than the compromise.** It ships with the python.org installer and is not a
  third-party dependency, which on the one platform with no package manager to
  lean on is worth more than it would be anywhere else. An earlier draft of
  this entry called it ugly and accepted it as a trade; **that was an opinion
  offered as a constraint, and the operator has a shipped counterexample**
  (`github.com/war4peace/image-toolbox`, a substantially more complex GUI than
  anything this item needs, entirely tkinter, wizard included). Record it as
  what it is: tkinter is sufficient here, and the evidence is a working
  program rather than a preference. A prettier toolkit means PyPI, and PyPI
  means the install story.
- **"A small executable" does not require bundling Python.** An earlier draft
  of this entry priced it at an embedded CPython (~10 MB), called the
  alternative "smaller and more fragile", and **had the ranking backwards.**
  The operator's own installer for the tool above is **2 MB**: the executable
  spawns a CLI stage that installs the prerequisites, and only then does the
  real GUI start and the wizard open. That is not a fragile shortcut, it is
  the normal shape of a Windows installer, and it maps onto this project
  unusually well because **the CLI stage it needs already exists**:
  `install.ps1` is exactly the "heavy lifting" half, and the rule above
  already says the GUI must front-end it rather than reimplement it. So the
  staged design is not a compromise made to save 8 MB, it is the design that
  falls out of the constraint that was already recorded. The build step is a
  small launcher, not a Python distribution.
- **It must not become the only way in.** `install.ps1 --unattended` has to
  keep working, for the same reason the Linux installer has it: upgrades,
  scripted deployments, and anybody driving this from a terminal. The GUI is
  an additional front door.

None of this is on the critical path. Steps 1 to 3 of 11f produce a working
capture-and-encode install with a console wizard, and that is what a prototype
needs. This becomes worth building at the point where the audience stops being
the author. It is also **cheaper than this entry first priced it**, per the two
corrections above, which is a reason to keep the deferral honest: it is
deferred because the prototype comes first, not because the item is expensive.

**7. The file-permission model, which does not translate. BUILT 2026-08-16**,
and it turned out to need *less* than this entry expected, for a reason worth
keeping: a new file inherits the **directory's** ACL, so restricting
`%ProgramData%\timelapse` once in `install.ps1` covers `config.json` and every
temporary copy an editor makes in it. The Linux side has to restore the mode
after `$EDITOR` precisely because a rename does *not* inherit anything there;
here the protection is a property of where the file is. `timelapse config`
therefore needs no fix-up step at all on Windows. `0640
root:timelapse` on `config.json` is a claim this project makes repeatedly and
tests for. Windows has no mode bits worth using; the equivalent is breaking
ACL inheritance and granting only SYSTEM, Administrators and the service
account, via `icacls`. `timelapse config` must restore that after `$EDITOR`
exits, for the same reason it restores 0640 today: an editor that saves by
rename creates a **new** file that inherits the *parent directory's* ACL
rather than the original's, which is the same class of bug as the umask one,
with a different mechanism.

**8. The storage scan. BUILT 2026-08-16.** `scan_filesystems()` parses `/proc/mounts` and is
entirely Linux. The Windows shape is different rather than harder: enumerate
drive roots (`os.listdrives()` is 3.12+ and the floor here is 3.9, so either
an `A:` to `Z:` existence loop or `GetLogicalDrives` through `ctypes`, which
is stdlib), then `shutil.disk_usage()` per drive, and `GetDriveTypeW` through
`ctypes` to tell fixed from removable from network. The rotational check has
no cheap equivalent and should simply be dropped rather than approximated.

**9. Share setup, which mostly disappears. BUILT 2026-08-16**, minus the
write probe, which belongs with the transfer it probes for (step 4). `setup_cifs_share()` installs
`cifs-utils`, mounts, writes a 0600 credential file and persists to
`/etc/fstab`. On Windows a UNC path needs no mounting at all, so most of that
function has no counterpart. Given the decision in item 5, what is left is
small and specific:

- **Resolve a mapped drive letter to its UNC target and store the UNC**, never
  the letter. This is the single most likely way a Windows install fails, and
  11d explains why. `Win32_LogicalDisk.ProviderName` gives the mapping, and
  `WNetGetConnectionW` through `ctypes` is the API for it.
- **Verify the destination is writable by the account the encoder will run
  as**, by writing to it, at wizard time. This replaces `require_mountpoint`,
  and it is a better check than the Linux one because it tests the actual
  question rather than a proxy for it.
- **Store credentials for that account if the share needs them** (`cmdkey`
  runs per-account, so it must be run as the service account, not as the
  operator). Whether this is needed at all depends on the account decision in
  item 2a.

**10. The CLI wrapper. BUILT 2026-08-16.** `timelapse.cmd` plus a PATH entry. The `[sudo]`
markers throughout the help text become an elevation check;
`ctypes.windll.shell32.IsUserAnAdmin()` is stdlib and there is no `sudo` to
suggest, so the message has to be "open an Administrator prompt", not a
command to copy.

### 11d. Traps, measured

These were run on Windows 11 26200 with Python 3.12.10 (64-bit) and ffmpeg
8.1; the probes are `temp/windows_probe.py` and `temp/windows_probe2.py`.
Several of them cannot fail on Linux, which is the point.

- **`os.replace()` onto a file another process has open for reading FAILS on
  Windows.** Measured: `PermissionError: [WinError 5] Access is denied`, with
  the identical operation succeeding once the reader closes. This is the
  project's atomic-write idiom and it appears at **seven** call sites
  (`capture.json`, `encode.json`, `update.json`, the config writer, the frame
  writer, and two more). Every one of them is written by a daemon and read by
  the web UI, so the collision is a page load landing in the same millisecond
  as a state write: rare, non-deterministic, and impossible to reproduce on
  the platform the tests run on. **This is the single highest-value finding in
  this entry.** The cause is that CPython's `open()` requests
  `FILE_SHARE_READ|FILE_SHARE_WRITE` but **not** `FILE_SHARE_DELETE`, and
  replacing a file requires delete access to the target. The fix is a retry
  with a short backoff around the replace, wrapped once in the platform module
  rather than at seven call sites.

- **`RotatingFileHandler` rollover fails the same way**, for the same reason,
  with a different error: measured `PermissionError: [WinError 32], the
  process cannot access the file because it is being used by another process`
  when renaming a log a second handle has open. So the moment the Log tab
  reads `capture.log`, the capture daemon's next rollover can throw. **The
  recommendation is to sidestep rotation entirely on Windows**: write
  `capture-YYYYMMDD.log`, never rename anything, and prune by age. That is
  less clever than `RotatingFileHandler` and has no failure mode.

  **BUILT 2026-08-16 exactly as recommended**, and one thing the entry above
  got wrong is worth keeping: it says the rollover "can throw", and it cannot,
  which is worse. `logging` catches handler failures, prints the traceback to
  stderr through `Handler.handleError` and carries on, so the daemon does not
  crash. It emits a full traceback **per log record** and the file simply never
  rotates until the reader lets go. Measured both ways against the daemon's own
  `setup_logging` path (`temp/step2_verify.py`): 58 stderr tracebacks and no
  rotated files from `RotatingFileHandler`, versus zero tracebacks and a
  correct daily file from the replacement. This is the same shape as the
  `socketserver` traceback the web UI had to catch: **anything that writes to
  stderr in a service gets mislabelled, and a swallowed error is harder to find
  than a raised one.**

  Linux is untouched and still writes `capture.log`, with a test asserting so,
  because `docs/install.md` tells operators to grep it and an upgrade that
  quietly renamed it would break that with no message.

- **NTFS is case-insensitive, and this project has a documented invariant that
  says two camera names are two places.** Measured: creating `Workshop` and
  then `workshop` raises `FileExistsError`, and the second name resolves to
  the first directory. The real library survey (architecture.md §9a) contains
  exactly this pair. On Linux they are two cameras writing two directories; on
  Windows they are two cameras **writing into one directory**, interleaving
  their frames into a single day, and the encoder would produce one video from
  two cameras' pictures with no error anywhere. The camera schema has no
  stable id, names *are* the key, and `sanitise_name()` does not case-fold. A
  Windows port must reject a camera name that differs from an existing one
  only by case, at the point of adding it, in the wizard.

  **HANDLED 2026-08-16, and most of it already was.** `name_taken()` has been
  case-insensitive since well before this research, and both the wizard's add
  and edit paths use it, so the wizard has never been able to create this
  pair. Saying what already exists before saying what is missing would have
  caught that; the entry above is what happens when it is not done. What was
  genuinely missing is the config that already holds the collision, which the
  wizard never sees: hand-edited through `timelapse config`, or written on
  Linux and carried across. `check_camera_names()` in the pre-flight covers it
  now, keyed on `os.path.normcase` rather than on a platform test, so it
  reports the exact-duplicate case on Linux and the case-variant case on
  Windows from one code path that both CI legs exercise. Deliberately not
  case-folded on Linux: two directories there genuinely are two directories,
  and reporting a working install as broken is the error `try_rsync_args()`
  exists to avoid.

- **A camera named `NUL` produces nothing. FIXED 2026-08-16, and this entry
  was wrong about how.** It used to say the name "silently destroys its own
  output" and that "the encoder would then report OK and delete the frames".
  Re-measured against this project's actual path shapes before the fix was
  written (`temp/step2_probe.py`), and it does not: `frames/NUL/` "succeeds" as
  a mkdir, and then `frames/NUL/2026-08-16` fails **WinError 3** on every
  single frame, which is the `-strftime_mkdir` failure shape again. Nothing is
  ever written, so there is nothing for the encoder to report OK about or to
  delete; it skips the day for having too few frames. Loud and useless rather
  than silent and destructive.

  The blast radius is narrower than the first pass suggested, too. `CON`,
  `AUX`, `PRN`, `COM1` and `LPT1` all work **perfectly well** as camera
  directories with frames inside them, and all six work as
  `<Camera>.YYYYMMDD.mkv`. In the bare extensionless form only `NUL` vanishes;
  the other five raise `Permission denied`, which the earlier note had not
  said. So exactly one name of nineteen touches this project at all.

  The whole set is refused anyway, and **on both platforms**, which was a
  decision rather than an oversight: a `config.json` is portable by design, so
  a name only one platform accepts is a trap for whoever moves the file, and
  the cost to a Linux operator is a frozenset. `sanitise_name()` deliberately
  does *not* do the refusing, because it strips characters and has no way to
  say why; the prompts refuse it with an explanation, and
  `check_camera_names()` in the pre-flight is the backstop for a config that
  arrived by another route.

  **The general lesson is the one this file already teaches about item 10, met
  again**: researching a fix changed what the fix was for. Measure the hazard
  against your own code's path shapes, not against the folklore, before
  writing the words that will outlive the fix.

- **`SO_REUSEADDR` makes `check_bind()` blind to a port already in use.**
  Measured three ways on the same listener: with `SO_REUSEADDR` (what
  `check_bind` sets, deliberately, to match the server) the second bind
  **succeeds**; with `SO_EXCLUSIVEADDRUSE` it is refused with errno 10048;
  with no option at all it is refused with 10048. So on Windows the wizard
  would report a taken port as available, and the service would then start
  without ever receiving a connection, which is the same class of silent
  failure that the whole "probe a bind address by binding it" design exists to
  prevent. This is already half-known: CLAUDE.md records the skipped test.
  What is new is that it is not merely a test artifact, it is a **live defect
  in the wizard** on that platform. `SO_EXCLUSIVEADDRUSE` is the Windows
  answer and it restores the distinction the code already knows how to report.

- **A service account cannot reach an SMB share the way a Linux mount can.**
  On Linux the CIFS credentials live in a root-owned 0600 file and the mount
  is system-wide, so the unprivileged service account inherits access without
  holding any secret. Windows has no equivalent: a service running as
  `LocalSystem` or a virtual `NT SERVICE\...` account presents the **machine
  account** to the network, so the share must grant `DOMAIN\HOST$`, and a
  workgroup NAS generally cannot. Running the services as a real local user
  with stored credentials works and costs the "the service account holds no
  secrets" property outright. **This deserves deciding before building, not
  during**, because it changes what the wizard asks.

- **A mapped drive letter does not exist for a service, and this is the most
  likely way a Windows install will fail.** Drive mappings are per logon
  session. A service runs in session 0 under its own account and inherits
  none of the interactive user's mappings, so a destination of `U:\TL` fails
  with "path not found" **while the operator can see it perfectly well in
  Explorer**, which is the most confusing possible failure: the tool says a
  path is missing, the operator opens it in another window, and concludes the
  tool is broken. Measured on this dev box, where the real library lives:
  `U:` is `\\tower\cctv`, so `U:\TL` is `\\tower\cctv\TL`. (That `U:` is a
  mapping to that UNC is measured here; that services do not inherit mappings
  is documented Windows behaviour, not something this research ran under the
  SCM.) The fix is entirely on the wizard: **resolve any drive letter to its
  UNC target at configure time and store the UNC**, then say so, because an
  operator who typed `U:\TL` and finds `\\tower\cctv\TL` in the config needs
  to know why. This is a close cousin of the Linux `require_mountpoint` trap,
  where an unmounted destination looks like an empty local directory: in both
  cases the path resolves to the wrong thing rather than to nothing.

- **`os.path.ismount()` means something different.** It is true for drive
  roots and for UNC share roots, so `require_mountpoint`, which exists to stop
  rsync filling the local disk when a NAS is not mounted, does not answer the
  same question. The underlying risk is real on Windows too (a disconnected
  mapped drive), so the check needs re-deriving rather than deleting.

- **`.gitattributes` needs one exception.** `eol=lf` is currently global and
  correct, including for `install.ps1`, which PowerShell reads happily. But
  `cmd.exe` is genuinely unhappy with LF-only batch files in some constructs,
  notably a label at end of file. Pin `*.cmd eol=crlf` when the first one is
  added. Getting this wrong produces a wrapper that fails in ways that look
  like a corrupted download, which is precisely the 0.1.4 failure mode in
  mirror image.

- **The hardening claims do not survive the port, and the docs must say so.**
  `ProtectSystem=strict` plus a one-directory `ReadWritePaths` is described in
  CLAUDE.md as "the whole structural claim this service makes", and it is
  *verified* under systemd rather than asserted. Windows has no equivalent
  worth pretending about. Running the web service as a low-privilege account
  with an ACL that denies it write access to the frames tree gets partway and
  is not the same claim. **Do not let the Windows docs inherit the Linux
  sentence.** Either state the weaker property honestly or state that the
  guarantee is Linux-only.

### 11e. How the codebase stays shared

The requirement is one repository, one set of Python scripts, both platforms
moving together. That is achievable, and the measurements in 11b are what make
it credible rather than hopeful. What it needs is a rule about *where* the
difference is allowed to live.

**The difference is not evenly spread, and that is the whole opportunity.** It
concentrates almost entirely in bootstrap and hosting, and barely touches the
programs themselves:

| Layer | Windows change | Shared? |
|---|---|---|
| Capture, encode, notify, the web UI's HTML and logic | almost none | yes, one copy |
| ffmpeg argv, filename parsing, cadence, smoothing | none measured | yes, one copy |
| Config schema and `config.json` | new defaults only | yes, one copy |
| Service state, log source, transfer, file permissions | different mechanism | behind one module |
| Install, service registration, CLI wrapper | different program | two files, by design |

So the shape is: **one `timelapse_platform.py`, and two installers.** No other
file gets a platform branch. That module answers a closed set of questions
(where does the config live, is a service running, restart it, read its recent
log, copy files to a destination, secure a file that holds passwords, list the
drives), and every existing call site becomes a call into it. `install.sh` and
`install.ps1` sit beside each other and are genuinely separate programs, which
is fine because they were never going to share a line anyway.

The eventual GUI installer (11c.6b) is a **third** Windows-only file and does
not weaken this, provided it stays a front end over `install.ps1` and the
wizard's own validation. It becomes a problem the moment it learns to register
a service by itself, which is why 6b says so before anyone writes it.

**Four rules that keep it honest**, each one earned from something this project
has already been bitten by:

1. **No `if os.name == "nt"` outside the platform module.** The moment that
   test appears in `timelapse_capture.py`, the two-forks outcome has started
   arriving by increments. This is the same reasoning that makes the
   `RedactingFormatter` duplication a *pinned* duplication with a test holding
   the two copies together, rather than a convention.
2. **A Windows CI leg from the first commit, not the last.** The suite already
   passes there, so it starts green and its entire value is catching the
   platform layer regressing. CLAUDE.md records being bitten twice by "a local
   branch is a branch CI has never seen"; developing a Windows port on a
   Windows box with no Windows CI is that mistake with the platforms swapped,
   and it would also let a Linux regression through in the other direction.
3. **A platform-specific fix must state which platform it is for.** The
   existing rule is that a test whose behaviour turns on euid or privilege must
   *declare* what it is rather than inherit it. Same principle: a branch taken
   because of NTFS case-folding should say NTFS, so the next person does not
   read it as a general truth and apply it on Linux.
4. **Docs do not inherit claims across platforms.** The hardening sentence in
   11d is the sharp instance, and it will not be the only one.

**What this costs, stated honestly.** Every platform branch is code that one
CI leg cannot exercise, so the effective test surface per platform is smaller
than the test count suggests. And some features will simply be weaker on
Windows rather than equal: the sandboxing is the known one today. Sharing the
codebase does not make the platforms equivalent, it makes them *consistent*,
which is a different and more achievable goal.

**The alternative, for completeness: a separate repo.** It buys freedom to use
`pywin32` and to shape the installer without regard for the other platform,
and it costs every bug fix twice, forever, with the copies drifting silently.
Given that the shared plan is already measured to work and the platform
difference is this concentrated, a separate repo is the worse trade here.

### 11f. Recommendation

If this is built, build it in this order, and stop at any point where the cost
stops being worth it:

0. ~~Prove the SCM handshake works from `ctypes`.~~ **Done 2026-08-15, it
   works** (see 11c.2). This was the one load-bearing unknown, and it landing
   green is why the rest of this list is written as it is: no dependency, no
   vendored binary, real services.
1. ~~`timelapse_platform.py` and the `os.replace` retry.~~ **Done 2026-08-16**,
   with the `windows-latest` CI leg, and it went in as an extraction rather
   than as new code: today's Linux answers moved behind the module and the
   existing suite proved Linux behaviour unchanged, which is what the count
   going 1,340 to 1,369 with nothing edited on the Linux side means. Scope was
   locations, service control, the config's permissions and the storage scan.
   The web UI's status and log sources were left where they are on purpose:
   they are step 5's work and their Windows shape is a design question
   (`QueryServiceStatusEx` returns integers where `systemctl show` returns
   text), not a mechanical move. Transfer likewise, because step 4 replaces
   rsync wholesale and splitting it now would design the API against a guess.
   Four things settled while building it, all recorded in architecture.md
   §4.6a: the module never prints; "cannot be asked" is a value and is not
   "no"; the Windows half of a question is written only when a caller exists,
   so nothing stubs a lie; and `locations()` takes the platform as an argument
   so both branches are asserted on both CI legs. Two things deliberately did
   not move: `writable_paths()` and `--print-state-path` name the *Linux*
   constant, because a `ReadWritePaths=` line is a POSIX artefact whoever
   generated it, and the capture daemon keeps its independence and carries a
   pinned copy. Windows config and state live under `%ProgramData%\timelapse`,
   decided 2026-08-16: it is machine-wide, survives upgrades and can be ACL
   restricted, none of which `%ProgramFiles%` gives for a file that is edited.
   The module is the seventh versioned script, installed and listed by
   `timelapse version`, because a stale copy of it breaks a daemon exactly as
   a stale script does.
2. ~~The three remaining measured correctness fixes.~~ **Done 2026-08-16.**
   (`SO_EXCLUSIVEADDRUSE` was the fourth and shipped early, with the
   `os.replace` retry, at `1ffec92`.) Two of the three turned out differently
   from how they were filed, and both corrections are written into 11d above
   rather than left here: the camera-name collision was **already prevented by
   the wizard** and what was actually missing was a check on a config that
   arrived by another route, and the reserved-name hazard is **one name, not
   five, and it fails loudly rather than silently**. Only the log-rollover
   sidestep was built as specified, and even there the entry understated it:
   the rollover cannot throw, because logging swallows it.

   Two of the three are *not* platform-guarded in the end, which was a
   decision. Reserved names are refused on both platforms because a
   `config.json` is portable, and the collision check uses `os.path.normcase`
   so the filesystem answers rather than a branch. Only the log handler
   actually forks, and it forks inside `timelapse_platform`.
3. Capture as a real service and encode as a scheduled task, with file
   logging. That is a *useful product on its own*: it captures, it encodes, it
   notifies. Ship or evaluate at this point before going further.

   **3a done 2026-08-16**: the hosting itself. `run_as_service()` in the
   platform module, the SCM query/control/registration bindings, the scheduled
   task definitions, and `timelapse_setup.py --install-units / --remove-units /
   --unit-status`, which is what `install.ps1` will front-end. File logging was
   **3b done 2026-08-16**: `install.ps1`, `timelapse_cli.py` behind a
   `timelapse.cmd` shim, and the wizard's three Windows adaptations (6a's
   ffmpeg question, 8's drive scan, 9's drive-letter resolution). 1,555 tests,
   and the **elevated install passed on the second run**: 21 checks covering a
   real install, the ACL, the PATH entry, the service and both tasks, the
   `timelapse` command through its own `.cmd` wrapper, and an uninstall that
   left the configuration alone. The first run failed 11 of 18, all of it one
   quoting bug cascading rather than eleven defects.
   Four decisions, all put to the user first: the CLI is a **Python dispatcher**
   rather than PowerShell or batch, because it is the only one of the three
   either CI leg can test and because a batch help text would rot; the
   installer covers install, uninstall and unattended but **not upgrade state**,
   since there is no field of Windows installs to preserve and re-running
   overwrites; Python is **required and its path baked**, with a per-user
   install warned about rather than refused; and the transfer question is
   **local path only**, with network destinations arriving at step 4 where the
   copying does.

   Three things measured while building it:

   - **A `.ps1` with no byte order mark is read as ANSI by Windows PowerShell
     5.1**, so install.sh's box-drawing banner arrives as mojibake and the
     installer's first line looks like a corrupted download. Caught by running
     it, not by reading it. `install.ps1` is ASCII throughout and a test
     asserts that, which is cheaper than making it the one file in the repo
     with its own encoding rule.
   - **`os.path` is `ntpath` on the Windows CI leg**, so a Linux path built
     with `os.path.join` comes back as `/opt/b\ffmpeg` and every Linux
     assertion about it fails on one runner only. The module already knew this
     for `locations()`; `find_tool()` and `resolve_tool()` learned it the same
     way, from a test that would otherwise have been quietly platform-specific.
     `posixpath.join` and `ntpath.join` by name, never `os.path`.
   - **`from timelapse_platform import IS_WINDOWS` makes two bindings**, and
     patching the wizard's copy leaves the platform module still answering
     "Windows". Same shape as the four update-checker tests that silently
     started hitting api.github.com: patch the module that owns the name.

   **What is left of the port is item 11f steps 4 to 6**: transfer, the web UI,
   and the GUI installer. Step 3's own goal is met: capture, encode and notify,
   installable and removable.

   1,469 tests, zero skips on both platforms, and the
   **elevated lifecycle passed first time**: registered as LocalSystem, RUNNING
   in 0.7s, six frames in twelve seconds at a two second cadence, a
   date-stamped log and a heartbeat written by the service rather than by the
   console session, a stop that the SCM was satisfied with in 0.5s, and a clean
   deregistration. `temp/step3_check.py` is the harness and it uses the
   shipping code path throughout.
   **What remains of step 3 is 3b: `install.ps1`, `timelapse.cmd`, and the
   wizard's Windows adaptations** (ffmpeg path per 6a, the storage scan per 8,
   drive-letter resolution per 9). Nothing is shippable until those exist.

   Four decisions taken 2026-08-16 before any of it was written:

   - **Capture runs as LocalSystem**, not as a created service account. 2a's
     reasoning for an administrative account is entirely about reaching the
     NAS, and capture never touches it: transfer happens in *encode*, which is
     a scheduled task. So the whole account-creation apparatus (NetUserAdd,
     `LsaAddAccountRights`, the deny rights, the registry hiding) is deleted
     from this step rather than deferred within it, and the machine-account
     objection in 2a does not apply because nothing here presents on the
     network. `ChangeServiceConfigW` can change it when step 4 settles
     transfer. **This does not reopen 2a**; it narrows it to the component the
     argument was actually about.
   - **The daemon imports the platform module, on Windows only.** The rule was
     written about Linux and systemd, and stays exactly true there because the
     import is inside `run_service()`, which Linux never calls. The alternative
     was a sixth pinned duplicate of the SCM handshake, which is neither small
     nor Linux-reachable, and the failure it protects against (a stale sibling
     from a partial upgrade) is what `timelapse version` already catches.
   - **Registration lives in Python, not in `install.ps1`.** For the reason
     `tools/` was deleted: two installers that both know how to register a
     service disagree within one release. This is the same rule 6b states for
     the GUI, applied one level down.
   - **Verification is `temp/step3_check.py`, run from an Administrator
     prompt.** Everything around the SCM is pure and unit-tested on both CI
     legs; the lifecycle needs privilege, and a runner's privilege is not this
     project's to assume.

   Three things measured while building it, all of which changed something:

   - **`ctypes.wintypes` is wrong on Linux.** `wintypes.DWORD` is `c_ulong`,
     which is **eight** bytes on 64-bit Linux and four on Windows, so every
     structure layout would have been silently wrong on three of the four CI
     legs *and every test asserting one would have agreed with it*. The
     structures use `c_uint32` and friends, which is what makes
     `sizeof(SERVICE_STATUS) == 28` assertable from either platform. This is
     the `PIX_FMT` lesson in a new place: a check that cannot fail is not a
     check.
   - **The task XML is verified without elevation**, and the technique is worth
     keeping (`temp/step3_probe.py`). Task Scheduler validates the definition
     *before* it checks who is asking, so a schema mistake and a privilege
     refusal give different errors. Registering the same definitions with a
     least-privilege principal needs no elevation at all and succeeded, and a
     deliberately broken interval was run as a control, because "Access is
     denied" everywhere would otherwise prove nothing. Reading the tasks back
     confirmed the two settings that cannot be expressed on the `schtasks`
     command line survived as meant: `RandomDelay` PT5M on the nightly job, and
     a `Repetition` interval with **no** `Duration` on the watch.
   - **`signal.signal` refuses to run off the main thread**, and the SCM hands
     the service a thread it created. Without a guard the daemon would raise
     `ValueError` before starting a single camera, on the one platform where
     nobody would see it: this is the `-strftime_mkdir` shape again, a call
     that is correct everywhere it has ever been tested and wrong where it has
     not.
4. ~~Transfer via robocopy.~~ **BUILT 2026-08-16**, and **not with robocopy**,
   which was refused on the same grounds as parsing `sc query`: its exit code
   is a bitmask where anything under 8 means success, its output is localised,
   and neither CI leg could exercise it. The mover is stdlib: copy to
   `<name>.part`, verify the length, `replace_atomic()` into place, then delete
   the original, so an interrupted copy can never leave a file that looks
   finished under a name the library index will count. See architecture.md
   §4.6c.

   **The account was the real work, not the copying**, which is what this entry
   under-priced by calling itself "transfer via robocopy". The nightly encode
   is a scheduled task running as LocalSystem, which presents the *machine*
   account on the network, so a destination the operator opens in Explorer
   every day is not necessarily one the job may write, and the symptom is an
   access denied on a demonstrably working path: the drive-letter trap in a
   second disguise, and this one cannot be fixed by rewriting the path.
   Resolved with **option 4 of the four listed above**, explicit
   `WNetAddConnection2W` at transfer time using optional credentials in the
   config, which is what the Linux side already does with its 0600 CIFS
   credentials file. The current token is tried first, so a share that already
   accepts the machine account needs no password stored anywhere.

   The write probe that 11c.9 left for this step is here too, as
   `try_destination()`: it mkdirs the destination, writes a file and deletes
   it, and the wizard, the pre-flight and the encoder all call that one
   function. Existence was never the question, `Path.is_dir()` *raises* on
   Windows for a directory you may not read, and a share can be listable by an
   account that cannot write to it.

   Three things worth keeping. **The wizard disconnects before it probes**, or
   the probe cannot fail: Windows permits one identity per server per session,
   so an existing connection makes new credentials answer
   `ERROR_SESSION_CREDENTIAL_CONFLICT` and the write then succeeds over the old
   connection, reporting an untested password as good. **That same error comes
   back as `None`, not `False`**, because somebody else's connection may be the
   one that works, which is `try_rsync_args()`'s three-way answer again.
   And **`ntpath.splitdrive` answers `\\tower` for a bare `\\tower`**, calling a
   server with no share on it a complete drive, so `share_root()` splits by
   hand; measured, after the first implementation used it.

   1,678 tests, zero skips on both platforms. `temp/step4_check.py` is the
   elevated harness, and the question it exists for is the one no test can
   answer: it registers a throwaway task **as LocalSystem**, runs it, and reads
   back whether that account could write to the real share.

   **Verified on the author's NAS 2026-08-16, all 15 checks.** LocalSystem is
   **refused outright** by `\\tower` (error 5), then reaches the destination
   through `reach_destination()`, then moves a real video that arrives byte for
   byte. That confirms the premise rather than assuming it: without this, every
   Windows install with a permissioned share would encode correctly and fail to
   deliver, nightly, on a folder the operator can write to by hand.

   **The harness had the defect, not the product, and it cost the operator
   real work** (new NAS users, several username forms) before that was clear.
   Its LocalSystem probe called `try_destination()` alone, which is the bare
   token and not what the encoder does, so it reported a working configuration
   as broken while the credential check beside it was already green. Third
   instance of **a probe must produce what the pipeline produces**, after
   `PIX_FMT` and the two RTSP probes. The general form is worth keeping: **when
   a check disagrees with a passing neighbour, suspect the check.**
5. **The GUI installer and wizard (11c.6b). Promoted from last to next,
   2026-08-16**, swapping places with the web UI on the operator's decision.
   Its trigger was filed as "an audience rather than a dependency", and with
   steps 1 to 4 done the audience is the thing that arrives next: someone who
   chose Windows, downloaded a release and has not got an Administrator prompt
   open. The console wizard serves the author; it does not serve them.
6. ~~The web UI.~~ **Deferred indefinitely on the operator's scope decision,
   2026-08-16**, together with the monitoring client that was weighed against
   it. Both are written up in decided-against.md; the short form is that
   Windows already browses the destination and plays the videos, so the
   problem the web UI solves on a CLI box does not exist there, and its
   read-only *structural* guarantee cannot be carried across anyway.
   **Notifications are what make this deferrable**: a failed run already
   reaches the operator without anything being looked at. It is not refused,
   and the constraint that would reopen it is naming the cameras from a phone.

~~**Add a `windows-latest` leg to CI at step 1, not at the end**~~, for the
reasons in 11e. **Done 2026-08-16**, with step 1. Python 3.12, unit suite only:
no ffmpeg, because the encode pipeline is shared code the three Linux legs
exercise on every push and a download would land in the one job that otherwise
has no network dependency. It also runs the wizard headless and checks the
config it writes names Windows locations, which is the only place those get
written to a real file rather than asserted about.

**Decisions taken 2026-08-15, so that the build order above is a plan rather
than a menu:**

- **The web UI is not required in the first release.** It is the largest
  remaining chunk of work (status source, log source, hardening question, and
  now an account question of its own), and deferring it makes step 3 a
  shippable product rather than a milestone.
- **The services run as an administrative account**, per item 2a, with the
  carve-out that this must not be extended to the web UI when it does arrive.
- **A remote destination requires an existing writable network path**, per
  item 5, or the destination is local. No SSH, no rsync remote specs.
- **The installer does not provide ffmpeg**, per item 6a. The wizard asks for
  the path, verifies it by running it, and points at ffmpeg.org when it is not
  there. This is a *smaller* installer than the Linux one, not a larger one.
- **The installer becomes a GUI eventually**, per item 6b, after a stable
  prototype and not before. Recorded now because two design choices have to
  respect it from the start: the wizard's logic stays separable from its
  prompting, and the GUI front-ends `install.ps1` rather than reimplementing
  it.

Together these remove most of what made this entry uncertain. What is left is
effort, not doubt.

**Still genuinely open:**

- Whether to build it at all. Nothing above commits to that.
- ~~Is the audience real?~~ **Settled 2026-08-15; this is no longer an open
  question.** An earlier draft of this entry held "nobody has asked for it"
  against the port. That reasoning was wrong and the operator's correction is
  recorded here because the same mistake is easy to repeat: **nobody asked for
  timelapse-maker either.** It was built by its author, for their own
  recorder, and every feature since has come from running it rather than from
  a request. Demand cannot be observed from a population that structurally
  cannot install the software, so silence from Windows users is selection
  bias, not evidence. The operator's position is "if you build it, they will
  come", and the sharper form of it is: **you cannot market a tool to users
  who cannot run it at all.**

  This is **not** in tension with the refusal of item 7, and the difference is
  worth being precise about, because "check whether the need is already met"
  remains a good rule. Item 7 was refused because the need *was* already met,
  by an external uptime monitor doing the job better, and architecture.md said
  so in two places before I proposed it. Here the need is met for nobody: Blue
  Iris has no timelapse feature, and this tool does not run on the platform.
  The test that matters is whether something already serves the need, not
  whether anyone has filed a request.

  The residual caveat is only that WSL2 partly serves the technical half of
  that audience today. The Blue Iris half is unlikely to be the WSL2 half.

---

## What this file taught, while it had things in it

Worth keeping, because these are the reasons to write the next entry down
rather than just building it.

**Most items arrived as the residue of a decision about something else.** Item
8 existed *because* of the reasoning that killed item 7, and turned out to be a
defect fix rather than a feature. Item 9 was what remained of the decision
against an ONVIF library: the one part of that idea needing no dependency. Item
10 was the same story a third time, left over from the IPv6 camera fix in
0.1.7. That is the argument for writing refusals down rather than deleting
them: the good idea is often next to the rejected one.

**Researching an "obvious" fix changed what it was.** Item 10's entry started
out claiming `check_bind()` needed changing to match the server. Checking
instead of assuming showed the opposite, that it already walked every family
`getaddrinfo()` returned and was therefore *passing an address the service
would refuse*. A missing feature turned out to be a defect, which moved it to
the front of the list. The design is in architecture.md §4.5 now.

**A pre-plan is a starting point, not a specification.** Item 3 shipped
narrower than planned: it proposed ntfy, gotify and email, and what was
actually wanted was ntfy and **Telegram**, which the entry never mentioned.
Email was refused as a different job with a different failure mode.

**An entry can mislead by what it omits.** Item 6 proposed remote library
browsing over SFTP and never said that the web UI already browses any readable
path, so it read as though the browser were missing. It was refused on 0.1.9
partly on that basis: once the entry said what already worked, the remaining
audience was too narrow to justify giving the network-facing service an SSH key
to the whole archive. **Say what already exists before saying what is missing.**

**Research produces defect reports, not just plans.** Item 11 set out to
scope a port and found a live bug in the current release along the way
(`check_bind()` cannot see a port in use on Windows, and the atomic-write
idiom can fail there). Both are Windows-only and neither depends on the port
being built. This is the second time an entry has done this, after item 10.
**Look for what the research says about today's code, not only about the
proposed feature.**

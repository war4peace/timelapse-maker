# Contributing

Bug reports, camera compatibility reports and patches are all welcome.

## Contents

- [Reporting a problem](#reporting-a-problem)
- [Before sending a patch](#before-sending-a-patch)
- [Running the tests](#running-the-tests)
  - [On Linux](#on-linux)
  - [On Windows](#on-windows)
  - [Writing tests](#writing-tests)
- [Where coverage is thin](#where-coverage-is-thin)
- [Scope](#scope)

## Reporting a problem

Open an issue with:

- what you expected and what happened
- the relevant lines from `capture.log` or `encode.log`
- your camera make/model and the *shape* of the snapshot URL
  (**redact credentials, and do not paste your real config**)
- `ffmpeg -version` output if it is an encoding problem
- the output of `timelapse test`, which usually identifies the cause

`timelapse config --redacted` prints your configuration with every password,
webhook URL and credential removed, which is the safe thing to paste.

"Camera X works" reports are useful too; they tell other people what URL form
and authentication scheme to try. Four makes have been exercised against real
hardware, and the presets for the others are built from published URL forms, so
a report either way is real information.

<sub>[&uarr; Contents](#contents)</sub>

## Before sending a patch

1. **Read [docs/architecture.md](docs/architecture.md) first.** It records why
   several non-obvious things are the way they are, including a few that look
   like mistakes and are not: `_dest_path` must not be renamed `_target`
   (`Thread.__init__` would shadow it), the encoder probe must use 512x512
   (`hevc_nvenc` refuses smaller frames), and the colour conversion and the
   colour tags must change together.
2. **Do not break the on-disk contract** in §3 without saying so explicitly.
   Both programs depend on it, and it is the only thing coupling them.
3. **Keep failures isolated.** One camera failing must never stop the others,
   and a failed notification or transfer must never turn a successful encode
   into a failed run.
4. **Platform differences live in one file.** `timelapse_platform.py` is the
   only place allowed to branch on the operating system. Everything else asks
   it. See §4.6a.
5. Match the surrounding style: stdlib where possible, no new dependencies
   without a good reason, comments that explain *why* rather than *what*, and
   no em-dashes in code, comments, documentation or user-facing strings.

<sub>[&uarr; Contents](#contents)</sub>

## Running the tests

The unit suite is stdlib `unittest` with no third-party dependency, and it runs
on both platforms with no skips. Please do not add pytest or any other test
dependency.

### On Linux

```bash
python3 -m unittest discover -s tests -t tests -p 'test_*.py'   # fast, no deps
python3 tests/smoke_test.py                                     # needs ffmpeg
bash -n install.sh && shellcheck --severity=warning install.sh
```

If your machine has an NVIDIA GPU the smoke test will select NVENC, which is
*not* the path CI takes. To exercise the software encoder as well:

```bash
CUDA_VISIBLE_DEVICES= python3 tests/smoke_test.py
```

### On Windows

Everything except the shell checks runs the same way, from a normal (not
elevated) prompt:

```powershell
python -m unittest discover -s tests -t tests -p 'test_*.py'
python tests\smoke_test.py
```

PowerShell's own parse check stands in for `bash -n`, and covers the three
scripts that ship:

```powershell
foreach ($f in @(Get-ChildItem *.ps1) + @(Get-ChildItem installer\*.ps1)) {
  $errors = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile(
    $f.FullName, [ref]$null, [ref]$errors)
  if ($errors) { $errors | ForEach-Object { $_.Message }; throw $f.Name }
  "$($f.Name) parses"
}
```

Two things need **an elevated prompt** and are not part of the suite, because
registering services is not something a test should do to a developer's
machine. Run them deliberately, on a machine you can put back:

```powershell
python scripts\timelapse_setup.py --install-units    # register service + tasks
python scripts\timelapse_setup.py --unit-status      # one line per component
python scripts\timelapse_setup.py --remove-units     # and take them away again
```

The graphical wizard cannot be unit tested below its decide layer. To check
that every page still builds, and that the entry point actually runs (which is
the part a passing suite has missed before):

```powershell
python scripts\timelapse_gui.py
```

Building the installer needs [Inno Setup 6](https://jrsoftware.org/isdl.php);
CI does this on a tag, so a local build is only for testing:

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\timelapse-maker.iss
```

**WSL is the practical way to test the Linux side from a Windows box.**
`wsl -d Ubuntu -u root -- bash <script>` gives real systemd, so unit templating
and live service runs can be exercised for real. Run the unit suite as an
ordinary user there rather than as root: several behaviours turn on privilege,
and root is not what CI is.

### Writing tests

**Check that the rule you mean to test is the one doing the work.** The storage
scan rejects a mount for any of six reasons, so it is easy to write a case that
passes for the wrong one; two of the original tests did exactly that. The cheap
way to confirm: break the rule on purpose and make sure your test fails.

Three failure modes this project has actually shipped, worth knowing before you
add a test:

- **Patch the module that owns the function**, not the one that imported it.
  Four tests once patched the wrong module and started silently making real
  requests to api.github.com, passing against the live repository.
- **Patch below the seam you are testing.** A mock standing in for the thing
  you are integrating tests the mock: one wizard test patched the very function
  whose argument handling was broken, so the argument never reached the code
  under test.
- **A test must declare its platform and privilege, never inherit them.** A
  test that reads `os.geteuid` or `os.name` and adapts will pass here and fail
  on a runner, or worse, quietly write outside its temporary directory. Two
  tests once created directories on the developer's disk and on a real NAS.

Tests must never write outside their own temporary directory.

<sub>[&uarr; Contents](#contents)</sub>

## Where coverage is thin

§9 of the architecture doc covers what is and is not tested, and how the parts
that need a camera, a GPU or systemd were verified by hand. Contributions
particularly welcome in:

- the **RTSP capture path**, which has no automated coverage at all
- **transfer**, which needs a stub `rsync` on `PATH` to exercise the Linux side
- **installer behaviour on a non-apt distro**: only the apt branch has ever
  been run, and the dnf/yum/pacman/zypper/apk branches are written but untested
- **camera makes** other than Dahua, Hikvision, Reolink and TP-Link Tapo

<sub>[&uarr; Contents](#contents)</sub>

## Scope

This tool captures snapshots and encodes them. Motion detection, object
detection and stream recording are out of scope; that is what an NVR is for.

[docs/decided-against.md](docs/decided-against.md) records what has been
proposed and refused, and why. Reading it before opening a feature request will
tell you whether the idea has already been argued through, and the reasoning is
usually more specific than "out of scope".

<sub>[&uarr; Contents](#contents)</sub>

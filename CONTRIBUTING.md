# Contributing

Bug reports, camera compatibility reports and patches are all welcome.

## Reporting a problem

Open an issue with:

- what you expected and what happened
- the relevant lines from `capture.log` or `encode.log`
- your camera make/model and the *shape* of the snapshot URL
  (**redact credentials, and don't paste your real config**)
- `ffmpeg -version` output if it's an encoding problem
- the output of `timelapse_test.py`, which usually identifies the cause

"Camera X works" reports are useful too; they tell other people what URL form
and auth scheme to try.

## Before sending a patch

1. **Read [docs/architecture.md](docs/architecture.md) first.** It records why
   several non-obvious things are the way they are, including a few that look
   like mistakes and are not (`_dest_path` must not be renamed `_target`; the
   encoder probe must use 256×256; the colour conversion and the colour tags
   must change together).
2. **Don't break the on-disk contract** in §3 without saying so explicitly. Both
   programs depend on it, and it is the only thing coupling them.
3. **Keep failures isolated.** One camera failing must never stop the others,
   and a failed notification or transfer must never turn a successful encode
   into a failed run.
4. Match the surrounding style: stdlib where possible, no new dependencies
   without a good reason, comments that explain *why* rather than *what*.

## Testing

```bash
python3 -m unittest discover -s tests -t tests -p 'test_*.py'   # fast, no deps
python3 tests/smoke_test.py                                     # needs ffmpeg
bash -n install.sh && shellcheck --severity=warning install.sh
```

Tests use stdlib `unittest`; please don't add pytest or any other test
dependency. §9 of the architecture doc covers what is and isn't tested, and how
the parts that need a camera, a GPU or systemd were verified by hand.

If you add tests, **check that the rule you mean to test is the one doing the
work.** The storage scan rejects a mount for any of six reasons, so it is easy
to write a case that passes for the wrong one; two of the original tests did
exactly that. The cheap way to confirm: break the rule on purpose and make sure
your test fails.

Coverage is thin in obvious places. Welcome contributions:

- the RTSP capture path, which has no automated coverage at all
- `transfer()`, which currently needs a stub `rsync` on `PATH`
- installer behaviour on a non-apt distro (see below)

## Scope

This tool captures snapshots and encodes them. Motion detection, object
detection and stream recording are out of scope; that is what an NVR is for.

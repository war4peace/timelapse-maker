# Contributing

Bug reports, camera compatibility reports and patches are all welcome.

## Reporting a problem

Open an issue with:

- what you expected and what happened
- the relevant lines from `capture.log` or `encode.log`
- your camera make/model and the *shape* of the snapshot URL
  — **redact credentials, and don't paste your real config**
- `ffmpeg -version` output if it's an encoding problem
- the output of `timelapse_test.py`, which usually identifies the cause

"Camera X works" reports are useful too — they tell other people what URL form
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

There is no unit test suite yet. §9 of the architecture doc describes how each
component was verified manually, including how to generate a synthetic frame set
for an end-to-end encoder run. Please do the equivalent for whatever you touch,
and say in the PR what you ran.

At minimum:

```bash
python3 -m py_compile scripts/*.py
python3 -m json.tool config/config.example.json > /dev/null
```

A real unit test suite would be a very welcome contribution.

## Scope

This tool captures snapshots and encodes them. Motion detection, object
detection and stream recording are out of scope — that is what an NVR is for.

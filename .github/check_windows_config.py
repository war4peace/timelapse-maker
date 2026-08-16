#!/usr/bin/env python3
"""Assert that a wizard-written config names Windows locations, not FHS ones.

A separate file rather than an inline heredoc in the workflow: the Linux legs
use `python - <<'PY'` for the same job, and PowerShell has no equivalent that
survives a path containing a backslash. The check is small enough to read and
sharp enough to be worth having, because the failure it catches is silent:
Path("/var/lib/timelapse/state") is not an error on Windows, it is a directory
on whichever drive happens to be current, so a daemon configured that way
publishes a heartbeat nobody reads.
"""

import json
import sys

BAD_PREFIXES = ("/etc/", "/var/", "/usr/", "/opt/")

KEYS = (("paths", "frames_root"),
        ("paths", "video_output"),
        ("paths", "log_dir"),
        ("paths", "state_dir"))


def main(path):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)

    problems = []
    for section, key in KEYS:
        value = cfg.get(section, {}).get(key) or ""
        if value.startswith(BAD_PREFIXES):
            problems.append(f"{section}.{key} = {value}")
        elif not value:
            problems.append(f"{section}.{key} is empty")

    for line in problems:
        print("FAIL " + line)
    if problems:
        print("The wizard wrote FHS paths on Windows. See timelapse_platform.")
        return 1

    print("OK, config names Windows locations:")
    for section, key in KEYS:
        print(f"  {section}.{key:<12} {cfg[section][key]}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: check_windows_config.py CONFIG")
    sys.exit(main(sys.argv[1]))

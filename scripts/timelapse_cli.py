#!/usr/bin/env python3
"""
timelapse_cli.py: the `timelapse` command, for the platform with no shell.

On Linux this job is done by a bash wrapper that install.sh generates, and that
stays as it is. Windows needs the same front door and cannot have the same one:
`timelapse.cmd` is two lines calling this, because a batch file has no heredoc
and its escaping would turn a page of help text into a wall of `echo` lines
that nobody would ever keep accurate.

It is deliberately a *dispatcher* and not a program. Every command here builds
an argv and runs one of the other scripts; nothing decides anything, so there
is no second implementation of any behaviour to drift from the first. The one
thing it owns is the help text, and that is content rather than logic: the
Windows text differs genuinely (no [sudo], no journalctl, no $EDITOR umask
dance), so it is not a copy of the bash one.

What stops the two drifting is a test, not a promise: `test_cli.py` holds the
command sets from here and from install.sh side by side and fails when one
gains a command the other has not.

Locations are derived rather than baked. The scripts sit beside this file by
construction, since the installer copies them together, and the config comes
from timelapse_platform. So `timelapse.cmd` needs only the interpreter and the
path to this file, and moving the install directory cannot leave a wrapper
pointing at the old one.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

from timelapse_platform import (
    CAPTURE_UNIT, CONFIG_DIR, CONFIG_PATH, LOG_STEMS, SERVICE_STATES,
    elevation_hint, is_elevated, service_state,
)

__version__ = "0.1.9"

HERE = Path(__file__).resolve().parent
SCRIPTS = ("capture", "encode", "test", "setup", "update", "platform", "web",
           "cli")

# command -> (script, flags before the config, takes the config, needs admin)
#
# "takes the config" is what decides both whether --output is passed and
# whether the command is marked as touching camera passwords. Two commands
# deliberately do not: `discover` presents no credentials to anything, and
# `update` neither reads nor writes the config, which is why its --check is the
# one query that needs no elevation at all.
COMMANDS = {
    "setup":     ("setup", [], True, True),
    "cameras":   ("setup", ["--cameras-only"], True, True),
    "transfer":  ("setup", ["--transfer-only"], True, True),
    "notify":    ("setup", ["--notify-only"], True, True),
    "web":       ("setup", ["--web-only"], True, True),
    "password":  ("setup", ["--password-only"], True, True),
    "restore":   ("setup", ["--restore-only"], True, True),
    "discover":  ("setup", ["--discover"], False, False),
    "test":      ("test", [], True, True),
    "usage":     ("test", ["--usage"], True, True),
    "encode":    ("encode", [], True, True),
    "web-serve": ("web", [], True, True),
}


def script(name):
    return str(HERE / ("timelapse_%s.py" % name))


def build_argv(command, extra, python=None, config=None):
    """The full command line for a dispatched command. Pure, so it is tested.

    Everything after the command is passed through, which is what makes
    `timelapse cameras -x:Doorbell` and `timelapse test --probe-profiles` work
    without this file knowing either option exists.
    """
    target, flags, takes_config, _admin = COMMANDS[command]
    argv = [python or sys.executable, script(target)] + list(flags)
    if takes_config:
        # --output for the wizard, positional for everything else, which is
        # each script's existing contract rather than a new one.
        if target == "setup":
            argv += ["--output", config or CONFIG_PATH]
        else:
            argv += [config or CONFIG_PATH]
    return argv + list(extra)


def run(command, extra):
    _target, _flags, _config, needs_admin = COMMANDS[command]
    if needs_admin and not is_elevated():
        # The same shape as the Linux wrapper refusing without sudo: say so and
        # stop, rather than half-running and failing at the write.
        print("  This command reads or writes the configuration, which holds")
        print("  your camera passwords, so it needs an elevated prompt.")
        print("  " + elevation_hint())
        return 1
    try:
        return subprocess.run(build_argv(command, extra)).returncode
    except OSError as exc:
        print("  Could not run it: %s" % exc)
        return 1


def newest_log(stem="capture", log_dir=None):
    """The current day's log file, or None when nothing has been written.

    Newest by name rather than by mtime, which is this project's standing rule
    and is right here too: the daily handler names files YYYYMMDD, so the names
    sort chronologically and a file touched by a reader cannot jump the queue.
    """
    folder = Path(log_dir) if log_dir else Path(CONFIG_DIR) / "logs"
    try:
        found = sorted(folder.glob("%s-*.log" % stem))
    except OSError:
        return None
    return found[-1] if found else None


def follow(path, lines=40):
    """Print the tail and then keep printing. Ctrl-C to stop.

    `journalctl -f` on the other platform. Written out rather than shelled to
    PowerShell's Get-Content -Wait so that Ctrl-C ends this process and not a
    child that leaves the terminal in a strange state.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        tail = fh.readlines()[-lines:]
        sys.stdout.write("".join(tail))
        sys.stdout.flush()
        try:
            while True:
                where = fh.tell()
                line = fh.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
                    fh.seek(where)
        except KeyboardInterrupt:
            print()
    return 0


def do_logs(extra):
    stem = LOG_STEMS.get(CAPTURE_UNIT, "capture")
    if extra and extra[0] in ("encode", "web"):
        stem = extra[0]
    path = newest_log(stem)
    if path is None:
        print("  No %s log yet under %s." % (stem, Path(CONFIG_DIR) / "logs"))
        print("  The log directory is whatever paths.log_dir names in your")
        print("  config; this looks in the default one.")
        return 1
    print("  %s  (Ctrl-C to stop)" % path)
    return follow(path)


def do_status(extra):
    return subprocess.run([sys.executable, script("setup"),
                           "--unit-status"] + list(extra)).returncode


def installed_versions():
    """(name, version) for every script that is actually here."""
    found = []
    for name in SCRIPTS:
        path = HERE / ("timelapse_%s.py" % name)
        version = ""
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("__version__"):
                    version = line.split("=", 1)[1].strip().strip('"\'')
                    break
        except OSError:
            version = "(not installed)"
        found.append((name, version))
    return found


def do_version(_extra):
    for name, version in installed_versions():
        print("  %-9s %s" % (name, version))
    # The daemon runs the code it read at startup, so a service that predates
    # the files on disk is an upgrade installed and never applied: the one
    # failure a version number alone cannot show you.
    if service_state(CAPTURE_UNIT) == 4:
        try:
            newest = max(p.stat().st_mtime for p in HERE.glob("timelapse_*.py"))
        except (OSError, ValueError):
            return 0
        if newest > time.time() - 5:
            return 0
        print()
        print("  Capture is running. If these files are newer than the running")
        print("  service, it is still on the previous build:")
        print("    sc stop \"TimelapseCapture\" && sc start \"TimelapseCapture\"")
    return 0


HELP = """timelapse - unattended daily timelapses from IP cameras

USAGE
  timelapse <command> [options]

Commands marked [admin] read or write the configuration file, which holds your
camera passwords. Without an elevated prompt they say so and stop; nothing
half-runs. There is no sudo here: privilege comes from how the window was
opened, so right-click PowerShell in the Start menu and choose "Run as
administrator".

CONFIGURING
  setup        [admin]  The full wizard: storage, capture interval, cameras,
                        transfer, notifications and the web UI. It rewrites the
                        whole config, backing up the old one first, so prefer
                        the targeted commands below for a single change.
  cameras      [admin]  Add, edit, remove, enable or disable a camera, and test
                        one against the real hardware. With no options it opens
                        a menu. The shortcuts go straight to one camera, by name
                        or by the number 'timelapse cameras -l' shows:
                          -a         add a camera
                          -e:NAME    edit it        -t:NAME   test it
                          -x:NAME    enable/disable -r:NAME   remove it
                        Changes offer to restart capture afterwards, which is
                        what makes them take effect.
  transfer     [admin]  Where finished videos are sent. A folder on this machine
                        or a network path that already exists; a mapped drive
                        letter is stored as its \\\\server\\share form, because a
                        service has no drive mappings of its own.
  notify       [admin]  Where the nightly summary goes: a Discord webhook, ntfy,
                        Telegram, any combination, or none. Needs no restart.
  web          [admin]  Turn the read-only web UI on or off and set its address,
                        port and library path. Not installed as a service yet on
                        Windows; 'timelapse web-serve' runs it in the foreground.
  password     [admin]  Set or change the web UI's login. Only a hash is stored,
                        so there is nothing to recover.
                          --disable    remove the login
  config       [admin]  Open the config in your editor (%EDITOR%, or Notepad)
                        for anything the wizards do not cover, such as the
                        encoder's container and quality. A backup is taken
                        first. Restart capture yourself afterwards.
                          --redacted   print the config with the passwords
                                       masked, to paste into a bug report
  restore      [admin]  Put back an earlier config. One is kept automatically
                        before every change, five deep. 'timelapse restore -l'
                        just lists them.

CHECKING
  discover              List ONVIF cameras answering on this network. Needs no
                        privilege and sends no credentials, so it cannot lock a
                        camera account.
  test         [admin]  Pre-flight, and the thing to run after any change.
                        Fetches one snapshot per enabled camera and reports
                        resolution, size, latency and authentication; probes the
                        encoders; checks disk headroom and the destination.
  usage        [admin]  Disk report: frames, bytes and date range per camera,
                        plus totals, videos and free space.
  status                The capture service and the two scheduled tasks, one
                        line each.
  logs                  Follow the capture log live. Ctrl-C to stop.
                        'timelapse logs encode' follows the encoder's instead.
  version               The installed version of each script.

RUNNING BY HAND
  encode       [admin]  Run the nightly encode now rather than at 00:05. Useful
                        for clearing a backlog.
  web-serve    [admin]  Run the web UI in the foreground to watch its log.

Anything after the command is passed through, so for example:
  timelapse test --probe-profiles   find which ONVIF profile is full resolution
  timelapse encode --dry-run        show what would encode, change nothing
  timelapse setup --defaults        accept every default without asking
  timelapse cameras -x:Doorbell     stop capturing that camera for now
  timelapse cameras --help          the options that command takes

NOT HERE YET
  update                Upgrading in place is Linux-only so far. On Windows,
                        download the new release and run install.ps1 again: it
                        keeps your configuration, your frames and your videos.

FILES
  {config}
      your configuration; it holds your camera passwords
  {confdir}\\config.example.json
      a commented template, replaced on every upgrade so you can diff it
  {confdir}\\logs
      the daily log files, unless paths.log_dir says otherwise
  {prefix}
      the scripts themselves

Guide:  https://github.com/war4peace/timelapse-maker/blob/main/docs/install.md
"""


def show_help():
    print(HELP.format(config=CONFIG_PATH, confdir=CONFIG_DIR, prefix=HERE))


def do_config(extra):
    """The one write path that does not go through the wizard's write_config().

    Simpler than the Linux original, and for a reason worth recording rather
    than a shortcut. There, an editor that saves by writing a new file and
    renaming it leaves root's umask on the result, so the config loses its group
    and the daemons stop being able to read it. Here a new file in that
    directory *inherits the directory's* ACL, which install.ps1 has already
    restricted, so the protection is a property of where the file is rather than
    of what the editor did.
    """
    if not is_elevated():
        print("  This opens the file holding your camera passwords, so it")
        print("  needs an elevated prompt.")
        print("  " + elevation_hint())
        return 1
    if extra and extra[0] == "--redacted":
        return subprocess.run([sys.executable, script("setup"), "--redacted",
                               "--output", CONFIG_PATH]).returncode
    made = subprocess.run([sys.executable, script("setup"), "--backup-now",
                           "--output", CONFIG_PATH])
    if made.returncode != 0:
        print("  (continuing without a backup)")
    editor = os.environ.get("EDITOR") or "notepad"
    try:
        return subprocess.run([editor, CONFIG_PATH]).returncode
    except OSError as exc:
        print("  Could not start %s: %s" % (editor, exc))
        return 1


SPECIAL = {"logs": do_logs, "status": do_status, "version": do_version,
           "config": do_config}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        # Same text as --help, but a bare invocation is a usage error and the
        # exit status has to say so for anything scripting around this.
        show_help()
        return 1
    command, extra = argv[0], argv[1:]
    if command in ("-h", "--help", "help"):
        show_help()
        return 0
    if command in ("-V", "--version"):
        print("timelapse_cli.py %s" % __version__)
        return 0
    if command in SPECIAL:
        return SPECIAL[command](extra)
    if command in COMMANDS:
        return run(command, extra)
    if command == "update":
        print('  "timelapse update" is Linux-only so far.')
        print("  On Windows, download the new release and run install.ps1")
        print("  again: it keeps your configuration, frames and videos.")
        return 1
    print('timelapse: unknown command "%s"\n' % command, file=sys.stderr)
    show_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

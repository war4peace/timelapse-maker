#!/usr/bin/env python3
"""
timelapse_platform.py: the one place a platform difference may live.

Every other script here is platform-neutral and must stay that way. The rule,
from docs/future-features.md item 11e: **no `if os.name == "nt"` outside this
file.** The moment that test appears in timelapse_capture.py, the two-forks
outcome has started arriving by increments, and this project has already
written down what happens when one copy of a rule drifts from another.

It answers a closed set of questions, and closed is the point. A module that
grows a new question per call site is a second copy of the scripts wearing a
different name:

    where does the config live          CONFIG_DIR, CONFIG_PATH
    where does runtime state live       STATE_DIR_DEFAULT, WEB_STATE_DIR_DEFAULT
    where does data live by default     DATA_ROOT_DEFAULT
    is a service running                service_is_active()
    restart one                         restart_service()
    how does an operator drive one      start_hint() and its neighbours
    secure a file that holds passwords  secure_secret_file()
    is this name usable as a filename   is_reserved_name(), same_file_name()
    how is a log file kept              log_handler()
    which disks could hold frames       scan_filesystems()

Not answered here yet, deliberately, because each is the substance of a later
step rather than a mechanical move (item 11f): the machine-readable service
status and log source the web UI reads, the transfer, the service *names*, and
elevation. Each arrives with the step that needs it, so that its shape is
designed against a real caller rather than against a guess about one.

The Windows halves are written where a caller exists today and are absent
where none does. Absent means the honest answer, never a stub that lies: a
service cannot be asked about on Windows yet, and "cannot be asked" is already
a value every caller handles, because it is what a Linux box without systemd
returns too.

Testable on either platform. The path derivation is a pure function taking the
platform as an argument, so the Windows branch is exercised by the Linux CI
legs and the Linux branch by the Windows one. That matters more than it looks:
a platform branch is otherwise code that one CI leg cannot reach, which is the
cost item 11e admits to.
"""

import logging
import logging.handlers
import ntpath
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

__version__ = "0.1.9"

# The one test, made once, so that nothing below has to repeat it and nothing
# outside this file has to make it at all.
IS_WINDOWS = os.name == "nt"


# ----------------------------------------------------------------------------
# Locations
#
# Linux splits these across the FHS: /etc for configuration, /var/lib for
# state. Windows has one tree for both, %ProgramData%, and it is the right one
# rather than a compromise: it is machine-wide rather than per-user, it
# survives an upgrade, and its ACL can be restricted, which %ProgramFiles%
# cannot usefully be because a config is edited and %ProgramFiles% is meant to
# be read-only after an install.
# ----------------------------------------------------------------------------

# The Linux answers by name, needed even when this is not Linux. Two callers
# emit a line into a systemd unit file (writable_paths and web_writable_paths,
# for ReadWritePaths), and a systemd unit is a POSIX artefact whatever platform
# generated it. C:\ProgramData in a ReadWritePaths= would be nonsense; so would
# refusing to generate the unit on the platform the tests run on.
LINUX_CONFIG_DIR = "/etc/timelapse"
LINUX_DATA_ROOT = "/var/lib/timelapse"
LINUX_STATE_DIR = "/var/lib/timelapse/state"
LINUX_WEB_STATE_DIR = "/var/lib/timelapse/web"


def program_data(env=None):
    """%ProgramData%, with the two fallbacks that make it not matter.

    ALLUSERSPROFILE has named the same directory since Vista and is what a
    stripped environment (a service, a scheduled task) tends to keep. The
    literal is the last resort: a machine where neither is set is not a machine
    where guessing a different path would help.
    """
    env = os.environ if env is None else env
    return (env.get("ProgramData") or env.get("ALLUSERSPROFILE")
            or "C:\\ProgramData")


def locations(windows, env=None):
    """Every fixed location, as a dict, for the platform named by `windows`.

    Pure, and takes the platform rather than reading it, so both branches are
    reachable from both CI legs. ntpath.join rather than os.path.join for the
    same reason: it produces a correct Windows path when this runs on Linux,
    which is what makes the Windows branch assertable there.
    """
    if not windows:
        return {"config_dir": LINUX_CONFIG_DIR,
                "config": LINUX_CONFIG_DIR + "/config.json",
                "data_root": LINUX_DATA_ROOT,
                "state": LINUX_STATE_DIR,
                "web_state": LINUX_WEB_STATE_DIR}
    base = ntpath.join(program_data(env), "timelapse")
    return {"config_dir": base,
            "config": ntpath.join(base, "config.json"),
            "data_root": base,
            "state": ntpath.join(base, "state"),
            "web_state": ntpath.join(base, "web")}


_LOC = locations(IS_WINDOWS)

CONFIG_DIR = _LOC["config_dir"]
CONFIG_PATH = _LOC["config"]
# Where the wizard offers to put frames, videos and logs when the storage scan
# finds nothing to choose between. On Linux that is /var/lib/timelapse; on
# Windows it is under %ProgramData%, which is the system drive, so the scan
# offering something roomier matters more there than here.
DATA_ROOT_DEFAULT = _LOC["data_root"]
# Both daemons publish runtime state here. Read with .get(key, default) from
# the config like every other added key; this is only the fallback.
STATE_DIR_DEFAULT = _LOC["state"]
# The web UI's sqlite index, and the only directory that service may write.
# Deliberately not the same directory as the one above: that one is written by
# the daemons and only read by the UI.
WEB_STATE_DIR_DEFAULT = _LOC["web_state"]


# ----------------------------------------------------------------------------
# Service supervision
#
# Only what the wizard needs, which is "is it running" and "restart it". The
# machine-readable status the web UI's table is built from is a different and
# larger question (QueryServiceStatusEx returns a struct of integers where
# systemctl show returns text), and it arrives with the web UI at item 11f
# step 5 rather than being guessed at here.
# ----------------------------------------------------------------------------

def service_is_active(unit):
    """True, False, or None when the service manager cannot be asked at all.

    None is not "stopped", and no caller may treat it as one. It means the
    question could not be put: no systemctl on this box, or a platform whose
    binding is not written yet. Saying "not running" about a service nobody
    asked about is how a check invents a fault on a healthy system.
    """
    if IS_WINDOWS:
        # The SCM binding lands with the services themselves (item 11f step 3).
        # Until then this is genuinely unanswerable here, which is the same
        # answer a Linux box without systemd gets, so every caller already
        # handles it.
        return None
    if not shutil.which("systemctl"):
        return None
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                           timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


def restart_service(unit):
    """Restart a service. Returns (ok, detail).

    `detail` is set only when the restart could not be attempted; a non-zero
    exit comes back as (False, "") because the reason for that is nearly always
    "not root", which the caller words better than an exit code does. Nothing
    here prints: the wizard owns the wording, and a platform module that writes
    to stdout is one a Windows service cannot call (item 11c.2).
    """
    if IS_WINDOWS:
        return False, "restarting a service is not implemented on Windows yet"
    try:
        r = subprocess.run(["systemctl", "restart", unit], timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return (r.returncode == 0), ""


# What to tell an operator to type. Separated from the calls above because a
# hint is shown in cases where nothing was attempted at all: a service that is
# installed but stopped, or a setting changed while it was not running.
#
# These are Linux-shaped and are unreachable on Windows today, which is a
# property of service_is_active() returning None there: every caller returns
# before it reaches one. They gain their Windows forms at step 3, along with
# the service names, which this module deliberately does not own yet.

def start_hint(unit):
    return f"systemctl enable --now {unit}"


def stop_hint(unit):
    return f"systemctl stop {unit}"


def restart_hint(unit):
    return f"systemctl restart {unit}"


def log_hint(unit, lines=40):
    """How to read one service's recent log, as a command to type."""
    return f"journalctl -u {unit.split('.')[0]} -n {lines}"


# ----------------------------------------------------------------------------
# Files that hold secrets
# ----------------------------------------------------------------------------

def secure_secret_file(path, group=None):
    """Restrict a file holding camera passwords to the service account.

    0640 root:<group>. The group is what makes it work: 0640 root:root leaves
    the daemons unable to read their own configuration, and that only shows up
    when a service fails to start.

    Failures are swallowed on purpose. This runs after the file has already
    been written, and refusing to have written a config because its group could
    not be set would be a worse outcome than a config the wizard warns about
    later; `timelapse test` checks the result rather than trusting this.
    """
    if IS_WINDOWS:
        # Not a no-op by oversight. chmod on Windows sets one bit, read-only,
        # so 0640 there would clear it and report success for a file every
        # account on the box can still read: a security claim that is false.
        # The real equivalent is breaking ACL inheritance and granting SYSTEM,
        # Administrators and the service account, which belongs with the
        # installer that creates that account (item 11c.7).
        return
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass
    if group:
        try:
            shutil.chown(path, group=group)
        except (OSError, LookupError):
            pass


# ----------------------------------------------------------------------------
# Names that are not filenames
#
# Enforced on **both** platforms, which is the unusual choice and the
# deliberate one. A camera name is a directory name, `config.json` is portable
# between platforms by design, and refusing a Linux operator a camera called
# `CON` costs nothing anybody will ever notice. A rule that holds on one
# platform only is a seam in the one project trying not to have any.
#
# Measured 2026-08-16 (temp/step2_probe.py), and the folklore is much broader
# than the hazard. Of the six probed, only **NUL** touches this project, and it
# is loud rather than silent: `frames/NUL/` "succeeds" as a mkdir, and then
# `frames/NUL/2026-08-16` fails WinError 3 on every single frame, which is the
# `-strftime_mkdir` failure shape again. CON, AUX, PRN, COM1 and LPT1 all work
# perfectly well as camera directories with frames inside them, and all six
# work as `<Camera>.YYYYMMDD.mkv`. So the research note's "the encoder would
# report OK and delete the frames" does not happen: nothing is ever written and
# the encoder skips the day.
#
# The whole set is still refused, because the cost is a frozenset and the
# alternative is knowing which of six names is safe in which of three path
# shapes, for ever.
# ----------------------------------------------------------------------------

RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


def is_reserved_name(name):
    """True for a name Windows treats as a device rather than as a file.

    Case-insensitive, because the filesystem is. The extension is not
    considered: `NUL.txt` is reserved too, and nothing here produces one.
    """
    return str(name).strip().upper() in RESERVED_NAMES


def same_file_name(a, b):
    """Would these two names reach the same file on this filesystem?

    `os.path.normcase` rather than a platform test, and this is the point:
    identity on POSIX, lowercasing on Windows. So the exact-duplicate case is
    exercised by the Linux CI legs and the case-variant case by the Windows
    one, from a single code path, with no branch to get wrong.

    It answers for *this* filesystem, which is a first-order answer rather than
    a perfect one: a case-insensitive volume mounted on Linux, or a
    case-sensitive directory on Windows, would each be judged by their host's
    default. The wizard is stricter on purpose (see name_taken), because the
    destination the videos are copied to may be case-insensitive whatever the
    recorder runs on.
    """
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


# ----------------------------------------------------------------------------
# Log files
#
# `RotatingFileHandler` renames, and Windows refuses to rename a file another
# process holds open. Measured: `PermissionError: [WinError 32]` the moment
# anything reads `capture.log` while the daemon rolls it over.
#
# The nastier half is what logging does with that. It does **not** propagate:
# `Handler.handleError` prints the whole traceback to stderr and carries on, so
# the daemon does not crash, it emits a traceback per log record and the file
# silently never rotates until the reader lets go. That is the same shape as
# the socketserver traceback the web UI had to catch, in a new place.
#
# So Windows sidesteps rotation entirely rather than defending it: one file per
# day, named rather than renamed, pruned by age. Nothing renames anything, so
# there is no failure mode left to handle. Linux keeps RotatingFileHandler
# exactly as it was, because nothing is wrong with it there.
# ----------------------------------------------------------------------------

class DailyFileHandler(logging.FileHandler):
    """One log file per day, chosen by name. Never renames anything.

    Deliberately has no size cap, unlike the handler it replaces. The trade is
    stated rather than hidden: this is less clever and has no failure mode,
    where a cap costs either a rename or a second file-in-progress convention.
    Volume is bounded in practice because the capture daemon logs the first
    failure of a burst rather than every failure.
    """

    def __init__(self, log_dir, stem, keep_days):
        self.log_dir = Path(log_dir)
        self.stem = stem
        self.keep_days = max(1, int(keep_days))
        self.day = datetime.now().strftime("%Y%m%d")
        logging.FileHandler.__init__(self, self._path(self.day),
                                     encoding="utf-8")
        self._prune()

    def _path(self, day):
        return str(self.log_dir / f"{self.stem}-{day}.log")

    def emit(self, record):
        # A daemon runs for weeks, so the day has to be re-checked here rather
        # than only at startup, or everything lands in the file it opened with.
        today = datetime.now().strftime("%Y%m%d")
        if today != self.day:
            self._switch_to(today)
        logging.FileHandler.emit(self, record)

    def _switch_to(self, today):
        """Open the new file before letting go of the old one.

        The other order was written first and a test found it: it loses the
        record that triggered the switch, and leaves the handler holding a
        closed stream for ever after, so the daemon stops logging to disk
        entirely over one transient failure.
        """
        was_named, was_open = self.baseFilename, self.stream
        self.baseFilename = self._path(today)
        try:
            self.stream = self._open()
        except OSError:
            # Keep the file already open rather than lose the record. A log
            # call must never be the thing that stops the recording, and
            # leaving self.day alone means the next record tries again.
            self.baseFilename, self.stream = was_named, was_open
            return
        self.day = today
        if was_open:
            was_open.close()
        self._prune()

    def _prune(self):
        cutoff = datetime.now() - timedelta(days=self.keep_days)
        for path in self.log_dir.glob(f"{self.stem}-*.log"):
            stamp = path.name[len(self.stem) + 1:-len(".log")]
            try:
                if datetime.strptime(stamp, "%Y%m%d") < cutoff:
                    path.unlink()
            except (ValueError, OSError):
                # Not one of ours, or in use. Either way, leave it alone.
                pass


def log_handler(log_dir, stem, max_bytes=8 * 1024 * 1024, backups=3):
    """The file handler this platform can actually rotate.

    `backups` carries both meanings, because they are the same intent measured
    differently: how much history to keep. On Linux it is that many rotated
    files beside the current one; on Windows that many days beside today.
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        return DailyFileHandler(log_dir, stem, keep_days=backups + 1)
    return logging.handlers.RotatingFileHandler(
        Path(log_dir) / f"{stem}.log", maxBytes=max_bytes, backupCount=backups)


# ----------------------------------------------------------------------------
# Storage discovery
#
# Which filesystems could hold frames. Linux reads /proc/mounts; the Windows
# shape is different rather than harder (drive roots, shutil.disk_usage,
# GetDriveTypeW) and arrives with the wizard at step 3. Until then this returns
# nothing there, which the wizard already handles by asking for a directory
# instead: that path exists because a machine can also have nothing worth
# offering.
# ----------------------------------------------------------------------------

PSEUDO_FS = {
    "tmpfs", "devtmpfs", "sysfs", "proc", "procfs", "cgroup", "cgroup2",
    "overlay", "overlayfs", "squashfs", "devpts", "mqueue", "hugetlbfs",
    "debugfs", "tracefs", "binfmt_misc", "configfs", "securityfs", "pstore",
    "efivarfs", "fusectl", "autofs", "rpc_pipefs", "nsfs", "ramfs", "bpf",
    "rootfs", "selinuxfs", "fuse.snapfuse", "fuse.gvfsd-fuse", "fuse.portal",
    "iso9660", "udf",
}

# Usable, but a bad idea for frames: no atomic rename guarantees across the
# wire, and 17k small writes per camera per day over a network is painful.
NETWORK_FS = {
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "9p", "fuse.sshfs", "ceph",
    "glusterfs", "fuse.rclone", "afs", "lustre",
}

SKIP_PREFIXES = (
    "/proc", "/sys", "/dev", "/run", "/snap", "/var/snap", "/boot",
    "/var/lib/docker", "/var/lib/containers", "/mnt/wsl", "/usr/lib/wsl",
    "/mnt/wslg", "/tmp/.",
)


def _base_device(source, sys_block="/sys/block"):
    """/dev/sda1 -> sda, /dev/nvme0n1p2 -> nvme0n1, /dev/mapper/vg-lv -> dm-N.

    sys_block is injectable so the partition-stripping rules can be tested
    against a fake tree rather than whatever disks this machine happens to have.
    """
    if not source.startswith("/dev/"):
        return None
    name = source[5:]
    if name.startswith("mapper/"):
        try:
            return os.path.basename(os.readlink(source))
        except OSError:
            return None
    if Path(f"{sys_block}/{name}").exists():
        return name
    # Strip a partition suffix: sda1 -> sda, nvme0n1p2 -> nvme0n1, mmcblk0p1.
    for cut in (lambda s: s.rstrip("0123456789"),
                lambda s: s.rsplit("p", 1)[0] if "p" in s else s):
        cand = cut(name)
        if cand and Path(f"{sys_block}/{cand}").exists():
            return cand
    return None


def _is_rotational(source, sys_block="/sys/block"):
    dev = _base_device(source, sys_block)
    if not dev:
        return None
    try:
        p = Path(f"{sys_block}/{dev}/queue/rotational")
        return p.read_text().strip() == "1"
    except OSError:
        return None


def scan_filesystems(mounts_path="/proc/mounts", statvfs=None, rotational=None):
    """Real, writable, local filesystems that could hold frames.

    The three inputs are injectable so the filtering can be tested against a
    synthetic /proc/mounts without needing the machine to have the interesting
    cases (network mounts, read-only duplicates, a device mounted twice) on it.

    Resolved here rather than as default arguments: os.statvfs does not exist on
    Windows, and evaluating it at def time would make the module unimportable
    there - which matters only for running the tests off-target, but there is no
    reason to forbid that.
    """
    if statvfs is None:
        statvfs = getattr(os, "statvfs", None)
        if statvfs is None:
            return []
    if rotational is None:
        rotational = _is_rotational
    try:
        raw = Path(mounts_path).read_text().splitlines()
    except OSError:
        return []

    found = {}
    for line in raw:
        parts = line.split()
        if len(parts) < 4:
            continue
        source, target, fstype, opts = parts[0], parts[1], parts[2], parts[3]
        target = target.replace("\\040", " ").replace("\\011", "\t")

        if fstype in PSEUDO_FS or fstype in NETWORK_FS:
            continue
        if target != "/" and target.startswith(SKIP_PREFIXES):
            continue
        if "ro" in opts.split(","):
            continue
        if not source.startswith("/dev/"):
            continue
        try:
            st = statvfs(target)
        except OSError:
            continue
        if st.f_blocks == 0:
            continue

        entry = {
            "mount": target,
            "source": source,
            "fstype": fstype,
            "free": st.f_bavail * st.f_frsize,
            "total": st.f_blocks * st.f_frsize,
            "rotational": rotational(source),
        }
        # One device can be mounted repeatedly (bind mounts, WSL). Keep the
        # shortest mountpoint, which is the primary one.
        prev = found.get(source)
        if prev is None or len(target) < len(prev["mount"]):
            found[source] = entry

    disks = sorted(found.values(), key=lambda d: -d["free"])
    return disks


if __name__ == "__main__":
    # Not an entry point; it is a library every script imports. But --version
    # keeps it answerable the same way its six siblings are, which is what the
    # installer's version listing reads.
    import sys

    if "--version" in sys.argv[1:]:
        print("timelapse_platform.py " + __version__)
    else:
        print(__doc__.strip())

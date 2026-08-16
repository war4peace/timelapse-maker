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
    what is this component called       native_name(), CAPTURE_UNIT and friends
    is a service running                service_is_active(), service_state()
    restart one                         restart_service()
    register or remove one              install_service(), install_task(), ...
    be one                              run_as_service()
    may this process change the box     is_elevated()
    how does an operator drive one      start_hint() and its neighbours
    secure a file that holds passwords  secure_secret_file()
    is this name usable as a filename   is_reserved_name(), same_file_name()
    how is a log file kept              log_handler()
    which disks could hold frames       scan_filesystems()
    is this path really on the network  network_path(), unc_for_drive()
    where might ffmpeg be               find_tool(), resolve_tool()

Not answered here yet, deliberately, because each is the substance of a later
step rather than a mechanical move (item 11f): the transfer, and the log source
the web UI's Log tab reads. Each arrives with the step that needs it, so that
its shape is designed against a real caller rather than against a guess.

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

import ctypes
import json
import logging
import logging.handlers
import ntpath
import os
import posixpath
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax import saxutils

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
# What each component is called
#
# The rest of this project names components by their systemd unit, because that
# is what it was written against and because an identifier has to be *some*
# spelling. Treat those strings as the internal id they are: this is the only
# file that knows a Windows box calls the same thing something else.
#
# The split into services and scheduled tasks is not an implementation detail,
# it is the same split the units already make. Capture and web are daemons and
# become real services; encode and watch exit within seconds and become
# scheduled tasks, because a service that exits at once is a service in a
# permanent restart loop.
# ----------------------------------------------------------------------------

CAPTURE_UNIT = "timelapse-capture.service"
WEB_UNIT = "timelapse-web.service"
ENCODE_UNIT = "timelapse-encode.timer"
WATCH_UNIT = "timelapse-watch.timer"

WINDOWS_NAMES = {
    CAPTURE_UNIT: "TimelapseCapture",
    WEB_UNIT: "TimelapseWeb",
    ENCODE_UNIT: "Timelapse Encode",
    WATCH_UNIT: "Timelapse Watch",
}

# What services.msc actually shows, which is not the service name: that list is
# sorted by display name, so this is the only string an administrator can find
# the service by. The first version passed the *description* as the display
# name, which filed the capture service under "Camera snapshot capture for
# timelapse" and left an operator looking under T unable to find it at all.
# That is half of item 11c.2's argument for using real services rather than
# scheduled tasks, lost to one misplaced argument.
#
# Spelled to sort together with the two scheduled tasks above, so that
# everything this project installs sits in one block under T wherever it is
# listed.
DISPLAY_NAMES = {
    CAPTURE_UNIT: "Timelapse Capture",
    WEB_UNIT: "Timelapse Web UI",
}

# Which log file a component writes. Two units share one: the credential watch
# is timelapse_encode.py --watch, so it logs where the encoder does.
LOG_STEMS = {
    CAPTURE_UNIT: "capture",
    WEB_UNIT: "web",
    ENCODE_UNIT: "encode",
    WATCH_UNIT: "encode",
}


def native_name(unit):
    """What this platform's service manager calls that component."""
    return WINDOWS_NAMES.get(unit, unit) if IS_WINDOWS else unit


def display_name(unit):
    """What an administrator will see this listed as."""
    return DISPLAY_NAMES.get(unit, native_name(unit))


def is_scheduled(unit):
    """True for the batch jobs, which are timers here and tasks there."""
    return str(unit).endswith(".timer")


# ----------------------------------------------------------------------------
# The Windows SCM, through ctypes
#
# `sc.exe create` pointed at a plain python.exe does not produce a service: the
# SCM expects StartServiceCtrlDispatcher within about 30 seconds and kills what
# does not answer, reporting 1053, which reads as a broken script rather than a
# wrong hosting model. pywin32 answers it and costs a large native dependency
# that this project's one-third-party-package rule will not pay for. So the
# handshake is done here, in ctypes, which is stdlib. Proven end to end under a
# real SCM 2026-08-15 (temp/win_service_proto.py): START_PENDING, RUNNING, a
# genuine SERVICE_CONTROL_STOP, STOPPED, and the dispatcher returning TRUE,
# which is the line that matters because it means the SCM was satisfied the
# process shut down rather than having given up and killed it.
#
# The structures are declared with fixed-width types rather than with
# ctypes.wintypes, and that is deliberate rather than fussy: on 64-bit Linux
# `wintypes.DWORD` is c_ulong, which is **eight** bytes, so every layout below
# would be silently wrong there and every test asserting one would agree with
# it. c_uint32 is 4 bytes on both, which is what makes these assertable from
# the Linux CI legs at all.
# ----------------------------------------------------------------------------

_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_LPWSTR = ctypes.c_wchar_p
_BOOL = ctypes.c_int32

SERVICE_WIN32_OWN_PROCESS = 0x00000010
SERVICE_AUTO_START = 0x00000002
SERVICE_ERROR_NORMAL = 0x00000001

SERVICE_STOPPED = 1
SERVICE_START_PENDING = 2
SERVICE_STOP_PENDING = 3
SERVICE_RUNNING = 4
# Not a service state Windows defines. This module's own answer for "the SCM
# was reachable and has never heard of it", which is a third thing from both
# "running" and "could not ask", and is falsy so that callers reading it as a
# boolean get the right answer without knowing that.
SERVICE_ABSENT = 0

SERVICE_STATES = {
    SERVICE_ABSENT: "not installed",
    SERVICE_STOPPED: "stopped",
    SERVICE_START_PENDING: "starting",
    SERVICE_STOP_PENDING: "stopping",
    SERVICE_RUNNING: "running",
    5: "resuming",
    6: "pausing",
    7: "paused",
}

SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004

SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_INTERROGATE = 0x00000004
SERVICE_CONTROL_SHUTDOWN = 0x00000005

SC_MANAGER_CONNECT = 0x0001
SC_MANAGER_ALL_ACCESS = 0xF003F
SERVICE_ALL_ACCESS = 0xF01FF
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SC_STATUS_PROCESS_INFO = 0

SERVICE_CONFIG_DESCRIPTION = 1
SERVICE_CONFIG_FAILURE_ACTIONS = 2
SERVICE_CONFIG_DELAYED_AUTO_START_INFO = 3
SC_ACTION_RESTART = 1

ERROR_SERVICE_EXISTS = 1073
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [("dwServiceType", _DWORD),
                ("dwCurrentState", _DWORD),
                ("dwControlsAccepted", _DWORD),
                ("dwWin32ExitCode", _DWORD),
                ("dwServiceSpecificExitCode", _DWORD),
                ("dwCheckPoint", _DWORD),
                ("dwWaitHint", _DWORD)]


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = SERVICE_STATUS._fields_ + [("dwProcessId", _DWORD),
                                          ("dwServiceFlags", _DWORD)]


class SC_ACTION(ctypes.Structure):
    _fields_ = [("Type", _DWORD), ("Delay", _DWORD)]


class SERVICE_FAILURE_ACTIONSW(ctypes.Structure):
    _fields_ = [("dwResetPeriod", _DWORD),
                ("lpRebootMsg", _LPWSTR),
                ("lpCommand", _LPWSTR),
                ("cActions", _DWORD),
                ("lpsaActions", ctypes.POINTER(SC_ACTION))]


class SERVICE_DESCRIPTIONW(ctypes.Structure):
    _fields_ = [("lpDescription", _LPWSTR)]


class SERVICE_DELAYED_AUTO_START_INFO(ctypes.Structure):
    _fields_ = [("fDelayedAutostart", _BOOL)]


class _WinApi(object):
    """advapi32 and shell32, bound once, with every restype declared.

    The restypes are not decoration. ctypes defaults a return value to c_int,
    which is 32 bits, and every handle here is a 64-bit SC_HANDLE on x64: it
    would be truncated, silently, and it would still work for as long as the
    SCM happened to hand out small values. That is a defect that passes every
    test on the machine it was written on and fails on someone else's. Found
    while writing the prototype, and it is a rule for this file rather than a
    detail of that one.
    """

    def __init__(self):
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.mpr = ctypes.WinDLL("mpr", use_last_error=True)
        self.MAIN = ctypes.WINFUNCTYPE(None, _DWORD, ctypes.POINTER(_LPWSTR))
        self.HANDLER = ctypes.WINFUNCTYPE(_DWORD, _DWORD, _DWORD,
                                          ctypes.c_void_p, ctypes.c_void_p)

        class SERVICE_TABLE_ENTRYW(ctypes.Structure):
            _fields_ = [("lpServiceName", _LPWSTR),
                        ("lpServiceProc", self.MAIN)]

        self.TABLE = SERVICE_TABLE_ENTRYW

        a = self.advapi32
        a.OpenSCManagerW.argtypes = [_LPWSTR, _LPWSTR, _DWORD]
        a.OpenSCManagerW.restype = _HANDLE
        a.OpenServiceW.argtypes = [_HANDLE, _LPWSTR, _DWORD]
        a.OpenServiceW.restype = _HANDLE
        a.CreateServiceW.argtypes = [_HANDLE, _LPWSTR, _LPWSTR, _DWORD, _DWORD,
                                     _DWORD, _DWORD, _LPWSTR, _LPWSTR,
                                     ctypes.POINTER(_DWORD), ctypes.c_void_p,
                                     _LPWSTR, _LPWSTR]
        a.CreateServiceW.restype = _HANDLE
        a.CloseServiceHandle.argtypes = [_HANDLE]
        a.CloseServiceHandle.restype = _BOOL
        a.DeleteService.argtypes = [_HANDLE]
        a.DeleteService.restype = _BOOL
        a.StartServiceW.argtypes = [_HANDLE, _DWORD, ctypes.c_void_p]
        a.StartServiceW.restype = _BOOL
        a.ControlService.argtypes = [_HANDLE, _DWORD,
                                     ctypes.POINTER(SERVICE_STATUS)]
        a.ControlService.restype = _BOOL
        a.QueryServiceStatusEx.argtypes = [_HANDLE, _DWORD, ctypes.c_void_p,
                                           _DWORD, ctypes.POINTER(_DWORD)]
        a.QueryServiceStatusEx.restype = _BOOL
        a.ChangeServiceConfig2W.argtypes = [_HANDLE, _DWORD, ctypes.c_void_p]
        a.ChangeServiceConfig2W.restype = _BOOL
        a.ChangeServiceConfigW.argtypes = [_HANDLE, _DWORD, _DWORD, _DWORD,
                                           _LPWSTR, _LPWSTR,
                                           ctypes.POINTER(_DWORD),
                                           ctypes.c_void_p, _LPWSTR, _LPWSTR,
                                           _LPWSTR]
        a.ChangeServiceConfigW.restype = _BOOL
        a.SetServiceStatus.argtypes = [_HANDLE, ctypes.POINTER(SERVICE_STATUS)]
        a.SetServiceStatus.restype = _BOOL
        a.RegisterServiceCtrlHandlerExW.argtypes = [_LPWSTR, self.HANDLER,
                                                    ctypes.c_void_p]
        a.RegisterServiceCtrlHandlerExW.restype = _HANDLE
        a.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(self.TABLE)]
        a.StartServiceCtrlDispatcherW.restype = _BOOL

        self.shell32.IsUserAnAdmin.argtypes = []
        self.shell32.IsUserAnAdmin.restype = _BOOL

        k = self.kernel32
        k.GetLogicalDrives.argtypes = []
        k.GetLogicalDrives.restype = _DWORD
        k.GetDriveTypeW.argtypes = [_LPWSTR]
        k.GetDriveTypeW.restype = _DWORD
        k.GetVolumeInformationW.argtypes = [
            _LPWSTR, _LPWSTR, _DWORD, ctypes.POINTER(_DWORD),
            ctypes.POINTER(_DWORD), ctypes.POINTER(_DWORD), _LPWSTR, _DWORD]
        k.GetVolumeInformationW.restype = _BOOL
        k.GetStdHandle.argtypes = [ctypes.c_int32]
        k.GetStdHandle.restype = _HANDLE
        k.GetConsoleMode.argtypes = [_HANDLE, ctypes.POINTER(_DWORD)]
        k.GetConsoleMode.restype = _BOOL
        k.SetConsoleMode.argtypes = [_HANDLE, _DWORD]
        k.SetConsoleMode.restype = _BOOL

        self.mpr.WNetGetConnectionW.argtypes = [_LPWSTR, _LPWSTR,
                                                ctypes.POINTER(_DWORD)]
        self.mpr.WNetGetConnectionW.restype = _DWORD


_API = None


def _win():
    """The bindings, bound on first use. Windows only, by construction.

    Lazy because ctypes.WinDLL does not exist elsewhere, and because a module
    every script imports should not pay for a DLL load it will not use.
    """
    global _API
    if _API is None:
        _API = _WinApi()
    return _API


def is_elevated():
    """True when this process can change the machine, False when it cannot.

    Deliberately not `getattr(os, "geteuid", lambda: 0)() == 0`, which is what
    this project used to write and which answers **0** on a platform that has
    no such call: a Windows box then looks like root to every check that asks.
    0.1.4 shipped a test built on that and it passed here while failing on all
    three CI legs.
    """
    if IS_WINDOWS:
        try:
            return bool(_win().shell32.IsUserAnAdmin())
        except OSError:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _last_error(call):
    err = ctypes.get_last_error()
    return "%s failed (Windows error %d)" % (call, err)


def _scm(access):
    """Open the service control manager, or raise OSError with the code."""
    handle = _win().advapi32.OpenSCManagerW(None, None, access)
    if not handle:
        raise OSError(ctypes.get_last_error(), _last_error("OpenSCManagerW"))
    return handle


def _open_service(scm_handle, unit, access):
    """The service handle, or None when it is simply not installed."""
    return _win().advapi32.OpenServiceW(scm_handle, native_name(unit), access)


def service_binpath(argv):
    """A Windows service command line from an argv list.

    Each element is quoted separately, and that is the whole point: a Python
    installed per user lives under a path with a space in it, which is the
    normal case rather than the awkward one, and one pair of quotes round the
    joined string is a different command line entirely.
    """
    parts = []
    for arg in argv:
        text = str(arg)
        parts.append('"%s"' % text if (not text or " " in text) else text)
    return " ".join(parts)


def service_state(unit):
    """The SCM's state code, SERVICE_ABSENT if unknown to it, None if unasked.

    Three answers rather than two, because they need three different responses:
    a stopped service is started, an absent one is installed, and one that could
    not be asked about is reported as such rather than guessed at.
    """
    if not IS_WINDOWS:
        return None
    try:
        scm = _scm(SC_MANAGER_CONNECT)
    except OSError:
        return None
    api = _win()
    try:
        svc = _open_service(scm, unit, SERVICE_QUERY_STATUS)
        if not svc:
            err = ctypes.get_last_error()
            return (SERVICE_ABSENT if err == ERROR_SERVICE_DOES_NOT_EXIST
                    else None)
        try:
            status = SERVICE_STATUS_PROCESS()
            needed = _DWORD(0)
            ok = api.advapi32.QueryServiceStatusEx(
                svc, SC_STATUS_PROCESS_INFO, ctypes.byref(status),
                ctypes.sizeof(status), ctypes.byref(needed))
            return status.dwCurrentState if ok else None
        finally:
            api.advapi32.CloseServiceHandle(svc)
    finally:
        api.advapi32.CloseServiceHandle(scm)


def install_service(unit, description, argv, account=None, password=None,
                    depends=("Tcpip",), delayed=True, restart_ms=15000):
    """Register one service. Returns (ok, detail). Needs elevation.

    The three ChangeServiceConfig2W calls afterwards are what make this the
    equivalent of the unit file rather than a bare registration, and each maps
    onto a line already in service/timelapse-capture.service:

        Restart=always, RestartSec=15   the failure actions
        After=network-online.target     the Tcpip dependency
        (no systemd equivalent)         delayed auto start, which is the
                                        standard answer to a recorder that
                                        comes up before its network does

    A failure in any of them is reported but does not undo the registration: a
    service that runs without automatic restart is worth far more than no
    service and a rollback.
    """
    if not IS_WINDOWS:
        return False, "not a Windows machine"
    api = _win()
    try:
        scm = _scm(SC_MANAGER_ALL_ACCESS)
    except OSError as exc:
        return False, str(exc.args[-1])
    try:
        dependencies = None
        if depends:
            # A double null terminated multi-string: one NUL after each name,
            # and the buffer's own terminator supplies the second.
            dependencies = ctypes.cast(
                ctypes.create_unicode_buffer("\0".join(depends) + "\0"),
                ctypes.c_void_p)
        svc = api.advapi32.CreateServiceW(
            scm, native_name(unit), display_name(unit), SERVICE_ALL_ACCESS,
            SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
            service_binpath(argv), None, None, dependencies, account, password)
        if not svc:
            if ctypes.get_last_error() != ERROR_SERVICE_EXISTS:
                return False, _last_error("CreateServiceW")
            # Re-running the installer is supposed to be how an upgrade
            # happens, and CreateServiceW refuses outright for a service that
            # already exists. Without this the second install of any version
            # fails on all three components while reporting only a Win32 error
            # number, which is a thing the docs promised did not happen.
            svc = _open_service(scm, unit, SERVICE_ALL_ACCESS)
            if not svc:
                return False, _last_error("OpenServiceW")
            try:
                if not api.advapi32.ChangeServiceConfigW(
                        svc, SERVICE_WIN32_OWN_PROCESS, SERVICE_AUTO_START,
                        SERVICE_ERROR_NORMAL, service_binpath(argv), None,
                        None, dependencies, account, password,
                        display_name(unit)):
                    api.advapi32.CloseServiceHandle(svc)
                    return False, _last_error("ChangeServiceConfigW")
            except OSError as exc:
                api.advapi32.CloseServiceHandle(svc)
                return False, str(exc)
        try:
            problems = []
            info = SERVICE_DESCRIPTIONW(description)
            if not api.advapi32.ChangeServiceConfig2W(
                    svc, SERVICE_CONFIG_DESCRIPTION, ctypes.byref(info)):
                problems.append("description")

            actions = (SC_ACTION * 3)()
            for action in actions:
                action.Type = SC_ACTION_RESTART
                action.Delay = restart_ms
            failure = SERVICE_FAILURE_ACTIONSW(
                86400, None, None, 3,
                ctypes.cast(actions, ctypes.POINTER(SC_ACTION)))
            if not api.advapi32.ChangeServiceConfig2W(
                    svc, SERVICE_CONFIG_FAILURE_ACTIONS, ctypes.byref(failure)):
                problems.append("restart-on-failure")

            if delayed:
                start = SERVICE_DELAYED_AUTO_START_INFO(1)
                if not api.advapi32.ChangeServiceConfig2W(
                        svc, SERVICE_CONFIG_DELAYED_AUTO_START_INFO,
                        ctypes.byref(start)):
                    problems.append("delayed auto start")
            if problems:
                return True, "registered, but could not set: " + \
                    ", ".join(problems)
            return True, ""
        finally:
            api.advapi32.CloseServiceHandle(svc)
    finally:
        api.advapi32.CloseServiceHandle(scm)


def remove_service(unit):
    """Deregister one service. Returns (ok, detail). Absent counts as done."""
    if not IS_WINDOWS:
        return False, "not a Windows machine"
    api = _win()
    try:
        scm = _scm(SC_MANAGER_ALL_ACCESS)
    except OSError as exc:
        return False, str(exc.args[-1])
    try:
        svc = _open_service(scm, unit, SERVICE_ALL_ACCESS)
        if not svc:
            if ctypes.get_last_error() == ERROR_SERVICE_DOES_NOT_EXIST:
                return True, ""
            return False, _last_error("OpenServiceW")
        try:
            if not api.advapi32.DeleteService(svc):
                return False, _last_error("DeleteService")
            return True, ""
        finally:
            api.advapi32.CloseServiceHandle(svc)
    finally:
        api.advapi32.CloseServiceHandle(scm)


def start_service(unit, timeout=30):
    """Start one service and wait for it to report RUNNING. (ok, detail)."""
    if not IS_WINDOWS:
        return False, "not a Windows machine"
    api = _win()
    try:
        scm = _scm(SC_MANAGER_CONNECT)
    except OSError as exc:
        return False, str(exc.args[-1])
    try:
        svc = _open_service(scm, unit, SERVICE_START | SERVICE_QUERY_STATUS)
        if not svc:
            return False, _last_error("OpenServiceW")
        try:
            if not api.advapi32.StartServiceW(svc, 0, None):
                return False, _last_error("StartServiceW")
        finally:
            api.advapi32.CloseServiceHandle(svc)
    finally:
        api.advapi32.CloseServiceHandle(scm)
    state = _await_state(unit, SERVICE_RUNNING, timeout)
    if state == SERVICE_RUNNING:
        return True, ""
    return False, "started, but reported %s" % SERVICE_STATES.get(state, state)


def stop_service(unit, timeout=30):
    """Stop one service and wait for it to report STOPPED. (ok, detail).

    An already stopped service is a success: the caller wanted it stopped.
    """
    if not IS_WINDOWS:
        return False, "not a Windows machine"
    api = _win()
    try:
        scm = _scm(SC_MANAGER_CONNECT)
    except OSError as exc:
        return False, str(exc.args[-1])
    try:
        svc = _open_service(scm, unit, SERVICE_STOP | SERVICE_QUERY_STATUS)
        if not svc:
            return False, _last_error("OpenServiceW")
        try:
            status = SERVICE_STATUS()
            if not api.advapi32.ControlService(svc, SERVICE_CONTROL_STOP,
                                               ctypes.byref(status)):
                if service_state(unit) == SERVICE_STOPPED:
                    return True, ""
                return False, _last_error("ControlService")
        finally:
            api.advapi32.CloseServiceHandle(svc)
    finally:
        api.advapi32.CloseServiceHandle(scm)
    state = _await_state(unit, SERVICE_STOPPED, timeout)
    if state == SERVICE_STOPPED:
        return True, ""
    return False, "stop requested, but it reported %s" % SERVICE_STATES.get(
        state, state)


def _await_state(unit, wanted, timeout):
    """Poll until the service reaches `wanted`, or the patience runs out."""
    deadline = time.time() + timeout
    state = service_state(unit)
    while state != wanted and time.time() < deadline:
        time.sleep(0.5)
        state = service_state(unit)
    return state


def run_as_service(unit, body, request_stop, on_error=None,
                   stop_wait_ms=30000):
    """Host `body` as a real Windows service. Returns a process exit code.

    `body(ready)` is called on the thread the SCM hands us, and must run until
    stopped; it calls `ready()` once it is genuinely up, which is what reports
    RUNNING. `request_stop()` is called from the SCM's own control thread and
    must return immediately: set an event, and let `body` do the joining.

    `on_error(text)` is how anything that goes wrong is witnessed. Nothing here
    may print: under the SCM there is no console, sys.stdout may be None or a
    dead handle, and a stray write kills the service entry point, which then
    presents as "this approach does not work" rather than as the one-line bug
    it is.
    """
    if not IS_WINDOWS:
        return 1
    api = _win()
    name = native_name(unit)
    live = {"handle": None, "state": SERVICE_STOPPED}

    def report(code, wait_hint=0, checkpoint=0):
        if not live["handle"]:
            return False
        status = SERVICE_STATUS()
        status.dwServiceType = SERVICE_WIN32_OWN_PROCESS
        status.dwCurrentState = code
        status.dwControlsAccepted = (
            SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
            if code == SERVICE_RUNNING else 0)
        status.dwWin32ExitCode = 0
        status.dwServiceSpecificExitCode = 0
        status.dwCheckPoint = checkpoint
        status.dwWaitHint = wait_hint
        live["state"] = code
        return bool(api.advapi32.SetServiceStatus(live["handle"],
                                                  ctypes.byref(status)))

    def on_control(control, event_type, event_data, context):
        try:
            if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
                # Before the slow part, never after. Capture has a camera
                # thread per camera to join and each can be holding a fetch
                # open, so an SCM that has not been told to wait calls the
                # stop hung and kills the process mid-write.
                report(SERVICE_STOP_PENDING, wait_hint=stop_wait_ms,
                       checkpoint=1)
                request_stop()
            elif control == SERVICE_CONTROL_INTERROGATE:
                report(live["state"])
        except BaseException:                                    # noqa: BLE001
            pass
        return 0

    def on_main(argc, argv):
        # Every line is inside the guard. An exception escaping a ctypes
        # callback goes to stderr, and stderr under the SCM is nowhere, so the
        # service would simply appear to hang.
        try:
            live["handle"] = api.advapi32.RegisterServiceCtrlHandlerExW(
                name, handler, None)
            if not live["handle"]:
                _report_error(on_error,
                              _last_error("RegisterServiceCtrlHandlerExW"))
                return
            report(SERVICE_START_PENDING, wait_hint=stop_wait_ms, checkpoint=1)
            body(lambda: report(SERVICE_RUNNING))
        except BaseException as exc:                             # noqa: BLE001
            _report_error(on_error, "service_main raised %s: %s"
                          % (type(exc).__name__, exc))
        finally:
            try:
                report(SERVICE_STOPPED)
            except BaseException:                                # noqa: BLE001
                pass

    # Both must outlive the dispatcher call. A callback that Python collects
    # while Windows still holds its address is a crash with no Python in it.
    handler = api.HANDLER(on_control)
    main = api.MAIN(on_main)

    table = (api.TABLE * 2)()
    table[0].lpServiceName = name
    table[0].lpServiceProc = main
    table[1].lpServiceName = None
    table[1].lpServiceProc = api.MAIN()

    if api.advapi32.StartServiceCtrlDispatcherW(table):
        return 0
    err = ctypes.get_last_error()
    if err == ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
        _report_error(on_error, "not launched by the service manager; run this "
                                "through the service, not from a console")
    else:
        _report_error(on_error, _last_error("StartServiceCtrlDispatcherW"))
    return 1


def _report_error(on_error, text):
    if on_error is None:
        return
    try:
        on_error(text)
    except BaseException:                                        # noqa: BLE001
        pass


# ----------------------------------------------------------------------------
# Scheduled tasks, which is where the batch jobs live
#
# The XML is built rather than the command line being used, because the two
# settings that matter most cannot be expressed on the schtasks command line at
# all: a repetition interval with no duration, and StartWhenAvailable, which is
# the Windows spelling of the encode timer's Persistent=true. Every element
# below has a line in service/*.timer behind it.
#
# Reading a task back is the opposite problem and the project already has the
# rule: `schtasks /Query` prints localised field names, so a parser keyed on
# them finds nothing on a German or Romanian install, which are exactly the
# installs least able to debug it. Existence comes from an exit code, and
# anything richer comes from PowerShell as JSON, whose property names are
# English everywhere.
# ----------------------------------------------------------------------------

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
# The SID rather than the name: "SYSTEM" is localised, and S-1-5-18 is not.
LOCAL_SYSTEM_SID = "S-1-5-18"


def daily_trigger(hour, minute, jitter_minutes=0):
    """The CalendarTrigger for OnCalendar=*-*-* HH:MM:00, with its jitter.

    The date in StartBoundary is a lower bound, not a schedule: a daily trigger
    fires every day from that date on, so a fixed past date means "from now",
    which is what the timer unit means too.
    """
    jitter = ("    <RandomDelay>PT%dM</RandomDelay>\n" % jitter_minutes
              if jitter_minutes else "")
    return ("  <CalendarTrigger>\n"
            "    <StartBoundary>2020-01-01T%02d:%02d:00</StartBoundary>\n"
            "    <Enabled>true</Enabled>\n"
            "%s"
            "    <ScheduleByDay>\n"
            "      <DaysInterval>1</DaysInterval>\n"
            "    </ScheduleByDay>\n"
            "  </CalendarTrigger>\n" % (hour, minute, jitter))


def repeating_trigger(minutes):
    """The TimeTrigger for OnBootSec/OnUnitActiveSec, repeating for ever.

    Repetition with an Interval and no Duration is how the schema spells
    "indefinitely"; naming a Duration would give the watch a stop date some
    weeks out that nobody would ever notice passing.
    """
    return ("  <TimeTrigger>\n"
            "    <Repetition>\n"
            "      <Interval>PT%dM</Interval>\n"
            "      <StopAtDurationEnd>false</StopAtDurationEnd>\n"
            "    </Repetition>\n"
            "    <StartBoundary>2020-01-01T00:00:00</StartBoundary>\n"
            "    <Enabled>true</Enabled>\n"
            "  </TimeTrigger>\n" % minutes)


def task_xml(description, argv, triggers, user_id=None, catch_up=True,
             time_limit="PT0S", priority=7):
    """A complete Task Scheduler 1.2 definition.

    `time_limit` PT0S means no limit, which is TimeoutStartSec=infinity: a full
    backlog catch-up legitimately takes hours and must not be killed part way
    through an encode. `priority` 7 is the below-normal band, the equivalent of
    the units' Nice=10, so that a batch job never costs capture a frame.
    """
    command = str(argv[0])
    arguments = service_binpath(argv[1:]) if len(argv) > 1 else ""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="%s">\n'
        "  <RegistrationInfo>\n"
        "    <Description>%s</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n%s  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <UserId>%s</UserId>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>%s</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <ExecutionTimeLimit>%s</ExecutionTimeLimit>\n"
        "    <Priority>%d</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        "      <Command>%s</Command>\n"
        "      <Arguments>%s</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
        % (TASK_NS, _xml(description), triggers,
           _xml(user_id or LOCAL_SYSTEM_SID),
           "true" if catch_up else "false", time_limit, priority,
           _xml(command), _xml(arguments)))


def _xml(text):
    return saxutils.escape(str(text))


def _schtasks(args):
    """Run schtasks and return (ok, output). Its output is never parsed.

    It is localised, so what comes back is passed to the operator verbatim and
    the exit code is what any decision is made on.
    """
    try:
        r = subprocess.run(["schtasks"] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    text = r.stdout.decode("utf-8", "replace").strip() if r.stdout else ""
    return r.returncode == 0, text


def install_task(unit, xml_text):
    """Register one scheduled task from its XML. (ok, detail). Needs elevation.

    The file is written UTF-16: schtasks /XML rejects UTF-8 on some builds with
    a message about a value being incorrectly formatted, which reads as a bug
    in the definition rather than in its encoding.
    """
    if not IS_WINDOWS:
        return False, "not a Windows machine"
    handle, path = tempfile.mkstemp(suffix=".xml", prefix="timelapse-task-")
    os.close(handle)
    try:
        Path(path).write_bytes(xml_text.encode("utf-16"))
        ok, out = _schtasks(["/Create", "/TN", native_name(unit),
                             "/XML", path, "/F"])
        return ok, "" if ok else out
    except OSError as exc:
        return False, str(exc)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def remove_task(unit):
    """Deregister one scheduled task. (ok, detail). Absent counts as done."""
    if not IS_WINDOWS:
        return False, "not a Windows machine"
    if task_exists(unit) is False:
        return True, ""
    ok, out = _schtasks(["/Delete", "/TN", native_name(unit), "/F"])
    return ok, "" if ok else out


def task_exists(unit):
    """True, False, or None when schtasks could not be run at all."""
    if not IS_WINDOWS:
        return None
    if not shutil.which("schtasks"):
        return None
    ok, _ = _schtasks(["/Query", "/TN", native_name(unit)])
    return ok


# What Task Scheduler's LastTaskResult means. It is either the last run's exit
# code or one of these HRESULTs, and the distinction matters because they
# overlap in the worst way: 267011 is "has not run yet", which is the state
# every freshly registered task is in, and printed as a bare number it reads as
# a five-figure error code. That is the same defect as calling the nightly
# oneshot "Stopped" for the 23 hours a day it is not running, met on the other
# platform, and the fix is the same: translate, never print the raw state.
SCHED_RESULTS = {
    0x00000000: "last run succeeded",
    0x00041300: "ready",
    0x00041301: "currently running",
    0x00041302: "disabled",
    0x00041303: "has not run yet",
    0x00041304: "no more runs scheduled",
    0x00041305: "not scheduled",
    0x00041306: "last run was terminated",
    0x00041307: "no valid triggers",
}


def task_result(code):
    """LastTaskResult in words. Never returns a bare number for a good state.

    An unrecognised value is reported as an exit code, because that is what it
    is: anything outside the SCHED_S_ range is whatever the program returned,
    and a non-zero one is a real failure that must not be softened.
    """
    if code is None:
        return "unknown"
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "unknown"
    if code in SCHED_RESULTS:
        return SCHED_RESULTS[code]
    # Signed on the way in from PowerShell for anything above 0x7FFFFFFF.
    if code < 0 and (code + (1 << 32)) in SCHED_RESULTS:
        return SCHED_RESULTS[code + (1 << 32)]
    return "last run failed (0x%08X)" % (code & 0xFFFFFFFF)


def task_info(unit):
    """State, last result and next run as a dict, or None if unanswerable.

    PowerShell rather than schtasks, for the property names: State, LastRunTime
    and LastTaskResult are English on every locale, and the equivalent columns
    of `schtasks /Query /V` are not.
    """
    if not IS_WINDOWS or not shutil.which("powershell"):
        return None
    name = native_name(unit).replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "$t=Get-ScheduledTask -TaskName '%s';"
        "$i=Get-ScheduledTaskInfo -TaskName '%s';"
        "[pscustomobject]@{State=[string]$t.State;"
        "LastResult=$i.LastTaskResult;"
        "LastRun=[string]$i.LastRunTime;"
        "NextRun=[string]$i.NextRunTime} | ConvertTo-Json -Compress" % (name,
                                                                       name))
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", script], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Service supervision, as the wizard and the pre-flight ask about it
# ----------------------------------------------------------------------------

def service_is_active(unit):
    """True, False, or None when the service manager cannot be asked at all.

    None is not "stopped", and no caller may treat it as one. It means the
    question could not be put: no systemctl on this box, no schtasks, a
    permission refusal. Saying "not running" about a service nobody asked about
    is how a check invents a fault on a healthy system.

    For the batch jobs this answers "is it armed", not "is it running now",
    which is the same thing `systemctl is-active` answers about a .timer and is
    the reason the three-way daemon/timer/oneshot split in the status page
    exists at all.
    """
    if IS_WINDOWS:
        if is_scheduled(unit):
            return task_exists(unit)
        state = service_state(unit)
        return None if state is None else state == SERVICE_RUNNING
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

    On Linux `detail` is set only when the restart could not be attempted; a
    non-zero exit comes back as (False, "") because the reason for that is
    nearly always "not root", which the caller words better than an exit code
    does. Nothing here prints: the wizard owns the wording, and a platform
    module that writes to stdout is one a Windows service cannot call.
    """
    if IS_WINDOWS:
        if is_scheduled(unit):
            return False, "a scheduled task has nothing to restart"
        ok, detail = stop_service(unit)
        if not ok:
            return False, detail
        return start_service(unit)
    try:
        r = subprocess.run(["systemctl", "restart", unit], timeout=90)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return (r.returncode == 0), ""


# What to tell an operator to type. Separated from the calls above because a
# hint is shown in cases where nothing was attempted at all: a service that is
# installed but stopped, or a setting changed while it was not running.
#
# There is no sudo to suggest on Windows and no equivalent to suggest either,
# so where a Linux hint carries the privilege in the command, the Windows one
# has to carry it in the sentence around it. That belongs to the caller.

def start_hint(unit):
    if IS_WINDOWS:
        if is_scheduled(unit):
            return 'schtasks /Change /TN "%s" /ENABLE' % native_name(unit)
        return 'sc start "%s"' % native_name(unit)
    return f"systemctl enable --now {unit}"


def stop_hint(unit):
    if IS_WINDOWS:
        if is_scheduled(unit):
            return 'schtasks /Change /TN "%s" /DISABLE' % native_name(unit)
        return 'sc stop "%s"' % native_name(unit)
    return f"systemctl stop {unit}"


def admin_cmd(command):
    """A command the operator must run with privilege, spelled for this box.

    `sudo x` on Linux. On Windows there is no prefix to add, because privilege
    is a property of how the window was opened, so the requirement moves into
    the words beside the command. Printing "sudo timelapse setup" there is
    advice that cannot be followed, and this project ships 29 such strings.
    """
    if IS_WINDOWS:
        return "%s  (from an Administrator prompt)" % command
    return "sudo " + command


def elevation_hint():
    """How to become able to change the machine, as a sentence.

    A sentence rather than a command, because on Windows there is nothing to
    type: privilege is a property of how the shell was launched, so "run it
    with sudo" has no counterpart and telling someone to type `runas` would be
    advice that does not work.
    """
    if IS_WINDOWS:
        return ("Right-click PowerShell or Command Prompt in the Start menu "
                "and choose 'Run as administrator', then run it again.")
    return "Run it again with sudo."


def restart_hint(unit):
    if IS_WINDOWS:
        name = native_name(unit)
        return 'sc stop "%s" && sc start "%s"' % (name, name)
    return f"systemctl restart {unit}"


def log_hint(unit, lines=40, log_dir=None):
    """How to read one component's recent log, as a command to type.

    `log_dir` is ignored on Linux, where the log is the journal, and is the
    whole answer on Windows, where it is a file whose location the operator
    chose. The default is only a default: a caller holding the config should
    pass paths.log_dir, because a hint naming the wrong directory is worse than
    no hint.
    """
    if IS_WINDOWS:
        stem = LOG_STEMS.get(unit, "capture")
        root = log_dir if log_dir else ntpath.join(DATA_ROOT_DEFAULT, "logs")
        return 'Get-Content "%s" -Tail %d' % (
            ntpath.join(str(root), stem + "-*.log"), lines)
    return f"journalctl -u {unit.split('.')[0]} -n {lines}"


# ----------------------------------------------------------------------------
# Whether this terminal can show colour
#
# A Windows console understands ANSI escapes only when a mode bit is set, and
# that bit is **off by default** in conhost, which is what a plain PowerShell
# window from the Start menu is. Without it every escape is printed literally:
# the operator sees `<-[32mOK<-[0m    Registered TimelapseCapture.` where the
# word OK should be green. Reported from the first real Windows install.
#
# What makes it nasty is where it is invisible. Windows Terminal sets the bit
# itself, so a developer using it sees perfect colour; and `_COLOR` is false
# under every test and on every CI runner, because none of them is a terminal.
# So this is the second defect in this project that exists *only* in front of
# the operator, after the padded-then-coloured column at 0.1.9.
# ----------------------------------------------------------------------------

STD_OUTPUT_HANDLE = -11
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004


def enable_ansi():
    """Turn on escape-sequence processing. True if escapes will be understood.

    Not a query: on Windows it sets the bit, because asking is not enough and
    nothing else in the process is going to do it. Idempotent, and false rather
    than raising when the console refuses, which is what an old conhost on
    Server 2016 does.
    """
    if not IS_WINDOWS:
        return True
    try:
        api = _win()
        handle = api.kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        if handle is None or handle == -1:
            return False
        mode = _DWORD(0)
        if not api.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
            return True
        wanted = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(api.kernel32.SetConsoleMode(handle, wanted))
    except (OSError, AttributeError):
        return False


def use_colour(stream=None):
    """Should this program emit colour at all?

    Three questions in the order they can be answered cheaply: has the operator
    said no, is this a terminal at all, and will that terminal understand it.
    The middle one is why `timelapse test > report.txt` stops writing escape
    codes into the file, which it did on every platform until now.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    stream = sys.stdout if stream is None else stream
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    return enable_ansi()


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
# Which filesystems could hold frames. Two implementations answering one
# question, and the dispatcher is `scan_filesystems()`; each half is named for
# what it actually reads, so a test naming `scan_mounts` is visibly a test
# about /proc/mounts rather than about storage in general.
#
# The Windows shape is different rather than harder: enumerate the drive roots,
# ask GetDriveTypeW which are real fixed disks, and take the sizes from
# shutil.disk_usage. The rotational check has no cheap equivalent and is simply
# dropped rather than approximated, because "HDD" is a note beside a number and
# a wrong note is worse than no note.
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


def scan_mounts(mounts_path="/proc/mounts", statvfs=None, rotational=None):
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


DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5


def drive_letters(mask=None):
    """The drive letters that exist, from GetLogicalDrives' bitmask.

    A bitmask rather than `os.listdrives`, which is 3.12 and the floor here is
    3.9, and rather than probing A: to Z: by hand, which spins up a floppy
    controller on hardware old enough to have one and pauses for a removable
    drive with no media in it.
    """
    if mask is None:
        if not IS_WINDOWS:
            return []
        mask = _win().kernel32.GetLogicalDrives()
    return [chr(ord("A") + bit) for bit in range(26) if mask & (1 << bit)]


def drive_kind(root):
    """GetDriveTypeW, or DRIVE_FIXED off Windows so a test can stand in."""
    if not IS_WINDOWS:
        return DRIVE_FIXED
    return int(_win().kernel32.GetDriveTypeW(root))


def volume_name(root):
    """The filesystem on a drive (NTFS, exFAT, ...), or "" if it will not say.

    Cosmetic: it fills the Type column the Linux listing takes from
    /proc/mounts. A drive that refuses the question still gets offered.
    """
    if not IS_WINDOWS:
        return ""
    name = ctypes.create_unicode_buffer(261)
    fs = ctypes.create_unicode_buffer(261)
    ok = _win().kernel32.GetVolumeInformationW(
        root, name, ctypes.sizeof(name) // 2, None, None, None, fs,
        ctypes.sizeof(fs) // 2)
    return fs.value if ok else ""


def scan_drives(mask=None, kind=None, usage=None, volume=None):
    """Fixed local drives that could hold frames, in the shape scan_mounts uses.

    Every input is injectable for the same reason the Linux half's are: the
    cases worth testing (a CD drive, an empty card reader, a mapped share, a
    drive that refuses to report its size) are ones no CI runner will happen to
    have, and this has to be assertable from the Linux legs as well.

    Removable, network and optical drives are excluded, which matches the Linux
    half rather than merely resembling it: a network share is a bad place for
    17k small writes per camera per day, and a drive whose media can be ejected
    is a worse one.
    """
    kind = drive_kind if kind is None else kind
    usage = shutil.disk_usage if usage is None else usage
    volume = volume_name if volume is None else volume

    disks = []
    for letter in drive_letters(mask):
        root = letter + ":\\"
        if kind(root) != DRIVE_FIXED:
            continue
        try:
            total, _used, free = usage(root)
        except OSError:
            continue
        if not total:
            continue
        disks.append({
            "mount": root,
            "source": letter + ":",
            "fstype": volume(root),
            "free": free,
            "total": total,
            # No cheap equivalent of /sys/block/*/queue/rotational, and an
            # expensive wrong guess is worth less than an honest blank.
            "rotational": None,
        })
    return sorted(disks, key=lambda d: -d["free"])


def scan_filesystems():
    """Which disks could hold frames, on whichever platform this is."""
    return scan_drives() if IS_WINDOWS else scan_mounts()


def os_disk_mount(env=None):
    """Where the operating system lives, which is the disk not to fill.

    "/" on Linux, which the wizard used to spell inline; %SystemDrive% plus a
    separator on Windows, which it cannot, because C: is only usually right and
    a wizard that recommends the boot drive on the machine where it is wrong
    has recommended filling it.
    """
    if not IS_WINDOWS:
        return "/"
    env = os.environ if env is None else env
    # %SystemDrive% is already "C:", with no separator and no trailing colon to
    # add. Spelled defensively anyway: this reads an environment variable, and
    # the one thing an environment variable is never guaranteed to be is the
    # shape the documentation says.
    root = (env.get("SystemDrive") or "C:").strip().rstrip("\\/").upper()
    if not root.endswith(":"):
        root += ":"
    return root + "\\"


# ----------------------------------------------------------------------------
# Network paths
#
# A mapped drive letter does not exist for a service. Mappings are per logon
# session, so `U:\TL` is something the operator can open in Explorer and the
# encoder cannot see at all, and the failure is "path not found" on a path that
# demonstrably works: item 11d calls this the single most likely way a Windows
# install fails. The wizard therefore stores the UNC target and says it did.
# ----------------------------------------------------------------------------

ERROR_NOT_CONNECTED = 2250


def unc_for_drive(letter):
    """Where a mapped drive letter really points, or None if it is not mapped.

    Two sources, and the second is not a belt-and-braces addition: it is the
    one that works when it matters. `WNetGetConnectionW` answers about the
    **calling logon session**, and UAC gives an elevated process a different
    session from the desktop that launched it. The wizard always runs elevated.
    So the API sees none of the operator's drive mappings, and the check that
    exists to warn about exactly this failure could not detect it: measured on
    the first real Windows install, where U:\\TL was stored verbatim.

    The registry entry survives, because the split token is still the same
    *user* and so still the same HKCU hive. It covers persistent (reconnect at
    logon) mappings, which is what a drive somebody uses daily is; a mapping
    made with `net use` and no /persistent is not there, and is why the caller
    must still handle None for a letter this machine cannot otherwise see.
    """
    if not IS_WINDOWS:
        return None
    local = str(letter).rstrip("\\/")
    if len(local) != 2 or local[1] != ":":
        return None
    size = _DWORD(1024)
    buf = ctypes.create_unicode_buffer(size.value)
    if _win().mpr.WNetGetConnectionW(local, buf, ctypes.byref(size)) == 0:
        if buf.value:
            return buf.value
    return _mapped_in_registry(local[0])


def _mapped_in_registry(letter):
    """HKCU\\Network\\<letter>\\RemotePath, or None.

    winreg is stdlib and Windows-only, so it is imported here rather than at
    module scope: this file is imported by every script on both platforms.
    """
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            "Network\\" + letter.upper()) as key:
            value, _kind = winreg.QueryValueEx(key, "RemotePath")
    except OSError:
        return None
    return str(value) or None


def drive_is_local(path):
    """True for a fixed disk this session can see. None if there is no drive.

    The backstop for a drive letter that is neither resolvable nor local: from
    an elevated session an unresolvable mapping reports DRIVE_NO_ROOT_DIR,
    which is the same answer as a letter that was simply typed wrong, and both
    mean the same thing to the caller. The destination cannot be used as given.

    Takes a **path**, not just a letter, for the same reason network_path()
    does: every caller has a path in its hand. The first version wanted `U:`
    exactly and answered None for `U:\\TL`, so the wizard's refusal could never
    fire; its test mocked this function with a fixed value and so never passed
    it an argument at all. None still means "no drive letter here", which is
    the right answer for a UNC path and for a POSIX one.
    """
    if not IS_WINDOWS:
        return None
    text = str(path)
    if len(text) < 2 or text[1] != ":":
        return None
    return drive_kind(text[0] + ":\\") == DRIVE_FIXED


def network_path(path, lookup=None):
    """Rewrite a path on a mapped drive to its UNC form. None if unchanged.

    Returning None rather than the input is deliberate: the caller has to tell
    the operator that a substitution happened, and a function that quietly hands
    back either the same path or a different one makes that impossible to say.
    """
    lookup = unc_for_drive if lookup is None else lookup
    text = str(path)
    if len(text) < 2 or text[1] != ":":
        return None
    target = lookup(text[:2])
    if not target:
        return None
    return target.rstrip("\\") + text[2:]


# ----------------------------------------------------------------------------
# Finding ffmpeg
#
# The Linux installer installs it from the distro, so the wizard's default is
# simply /usr/bin/ffmpeg. Windows has no package manager worth relying on, the
# builds people run come from gyan.dev or BtbN rather than from any package
# source, and a recorder very often already has an ffmpeg the operator chose
# deliberately. So the installer does not provide one (item 11c.6a): the wizard
# asks, defaulting to whatever is already here, and verifies by running it.
# ----------------------------------------------------------------------------

FFMPEG_URL = "https://ffmpeg.org/download.html"


def _EXE():
    return ".exe" if IS_WINDOWS else ""


def _join(*parts):
    """Join for the platform being *described*, not the one running.

    ntpath and posixpath by name rather than os.path, which is the same
    reasoning locations() uses and it was a test that insisted on it: os.path
    is ntpath on the Windows CI leg, so a Linux answer built with it comes back
    as /opt/b\\ffmpeg and every Linux assertion here fails on one runner only.
    """
    return (ntpath.join(*parts) if IS_WINDOWS else posixpath.join(*parts))


def ffmpeg_roots(env=None):
    """Directories worth looking in when ffmpeg is not on PATH."""
    if not IS_WINDOWS:
        return ["/usr/bin", "/usr/local/bin"]
    env = os.environ if env is None else env
    roots = []
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        base = env.get(var)
        if base:
            roots.append(ntpath.join(base, "ffmpeg", "bin"))
    local = env.get("LOCALAPPDATA")
    if local:
        # winget puts shims here, and its ffmpeg package is the one most likely
        # to be present without the operator remembering installing it.
        roots.append(ntpath.join(local, "Microsoft", "WinGet", "Links"))
    roots.append("C:\\ffmpeg\\bin")
    return roots


def find_tool(name, roots=None, which=None, exists=None):
    """An absolute path to `name`, or "" when it is nowhere obvious.

    PATH first, because an operator who put ffmpeg on PATH has already answered
    this question. `shutil.which` honours PATHEXT, so "ffmpeg" finds ffmpeg.exe
    without the extension being spelled anywhere.
    """
    which = shutil.which if which is None else which
    exists = os.path.exists if exists is None else exists
    found = which(name)
    if found:
        return found
    for root in (ffmpeg_roots() if roots is None else roots):
        candidate = _join(root, name + _EXE())
        if exists(candidate):
            return candidate
    return ""


def resolve_tool(answer, name, isdir=None, exists=None):
    """Turn what the operator typed into a path to one binary.

    A directory is accepted, and that is the Windows-shaped part: the zip
    unpacks a `bin` folder holding ffmpeg.exe, ffprobe.exe and ffplay.exe
    together, and "the ffmpeg binaries path" is how operators there describe
    it. Given a directory, both binaries come from that one answer rather than
    from two questions with a chance to disagree.
    """
    isdir = os.path.isdir if isdir is None else isdir
    exists = os.path.exists if exists is None else exists
    text = str(answer).strip().strip('"')
    if not text:
        return text
    if not isdir(text):
        return text
    binary = name + _EXE()
    for candidate in (_join(text, binary), _join(text, "bin", binary)):
        if exists(candidate):
            return candidate
    return _join(text, binary)


if __name__ == "__main__":
    # Not an entry point; it is a library every script imports. But --version
    # keeps it answerable the same way its six siblings are, which is what the
    # installer's version listing reads.
    import sys

    if "--version" in sys.argv[1:]:
        print("timelapse_platform.py " + __version__)
    else:
        print(__doc__.strip())

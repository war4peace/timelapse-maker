#!/usr/bin/env python3
"""Windows setup wizard with a window instead of a terminal (item 11c.6b).

The console wizard in `timelapse_setup.py` serves whoever is comfortable in an
Administrator prompt. This serves the person who chose Windows, downloaded a
release and expects to double-click something: same questions, same validation,
same config file, same backups.

Two rules from 11c.6b shape every line here and neither is negotiable.

**This file decides nothing on its own.** Every check and every write is a
function in `timelapse_setup.py` or its neighbours, called from here. The
project has been bitten by a second implementation before (`tools/` duplicated
the wizard, drifted, and was deleted), and two wizards that both know what a
valid camera name is will disagree within one release.

**Deciding is separated from showing**, which is what makes any of this
testable. Everything above the SHOW LAYER banner is pure: it takes strings and
returns `(level, message)`, so both CI legs check the logic without a display,
and the widget half stays thin enough to verify by hand against a checklist.

**tkinter is imported lazily**, inside `run()`, for the same reason
`timelapse_platform` binds Win32 lazily: this module is imported by tests on
machines and CI runners with no window station at all, and an import at module
scope would take them all down.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import timelapse_setup as setup                            # noqa: E402
from timelapse_platform import (CONFIG_PATH, FFMPEG_URL,   # noqa: E402
                                IS_WINDOWS, elevation_hint, is_elevated,
                                is_unc, no_console, share_root)

__version__ = "0.1.9"

OK, WARN, FAIL = "ok", "warn", "fail"

# Wide enough for a UNC path and an explanation of what is wrong with it, and
# for a camera list beside the camera being edited; short enough to sit on a
# 1366x768 laptop, which a recorder often is, taskbar and title bar included.
WINDOW = "820x620"

# Field labels are a fixed width so the boxes line up down a page, which means
# the widest label sets it: "Seconds between frames" is 22 characters, and at
# 20 it arrived as "Seconds between fram". Same defect as the notification
# page's "Discord webhook UR...", and it is invisible to every test here.
LABELS = 22


# ---------------------------------------------------------------------------
# THE DECIDE LAYER
#
# No tkinter, no printing, no config writes. Each check takes what the operator
# typed and returns (level, message), where the message is what a human should
# read and the level is what the window colours. Tested directly.
# ---------------------------------------------------------------------------

def check_storage(base):
    """Is this a usable place to put frames, videos and logs?"""
    text = str(base or "").strip().strip('"')
    if not text:
        return FAIL, "Choose a folder to keep the frames and videos in."
    path = Path(text).expanduser()
    if not path.is_absolute():
        return FAIL, "Give a full path, starting with a drive letter."

    root = path.anchor or str(path)
    try:
        import shutil
        free = shutil.disk_usage(root).free
    except OSError as exc:
        return FAIL, f"{root} cannot be read: {exc}"

    gb = free / 1024 ** 3
    # Not a hard floor: this only has to hold one day of frames at a time, and
    # what "enough" means depends on the cadence and the camera count, which
    # the capture page works out properly. This is the obviously-wrong check.
    if gb < 5:
        return WARN, (f"{setup.human(free)} free on {root}. That is very "
                      f"little for a day of full-resolution frames.")
    return OK, f"{setup.human(free)} free on {root}."


def storage_paths(base):
    """The three directories a base folder implies. Mirrors choose_storage()."""
    root = Path(str(base).strip().strip('"')).expanduser()
    return {"frames_root": str(root / "frames"),
            "video_output": str(root / "videos"),
            "log_dir": str(root / "logs")}


def check_ffmpeg(answer):
    """(level, message, ffmpeg, ffprobe, codec).

    A folder is accepted, which is the Windows-shaped part: the zip unpacks a
    bin folder holding both binaries, and taking them from one answer is what
    stops a machine with two ffmpeg builds getting one from each.
    """
    from timelapse_platform import resolve_tool

    text = str(answer or "").strip().strip('"')
    if not text:
        return (FAIL, "Say where ffmpeg is. Without it, frames are collected "
                      f"and nothing ever becomes a video. Builds: {FFMPEG_URL}",
                "", "", None)

    ffmpeg = resolve_tool(text, "ffmpeg")
    ffprobe = setup.sibling_tool(ffmpeg, "ffprobe") or ""
    codec, failures, problem = setup.detect_encoders(ffmpeg)

    if problem:
        return FAIL, problem[0].upper() + problem[1:], ffmpeg, ffprobe, None
    if codec is None:
        return (FAIL, "ffmpeg runs but cannot encode anything here, so no "
                      "video would ever be produced.", ffmpeg, ffprobe, None)
    if not ffprobe:
        return (WARN, f"{codec} works, but no ffprobe beside ffmpeg. Give the "
                      f"folder holding both.", ffmpeg, ffprobe, codec)
    if codec == "libx264":
        return (WARN, "No NVENC encoder found, so encoding falls back to the "
                      "CPU. Nightly runs will be slower; the output is fine.",
                ffmpeg, ffprobe, codec)
    return OK, f"{codec} available, so the GPU does the encoding.", \
        ffmpeg, ffprobe, codec


def encoder_notes(answer):
    """Why each better encoder was skipped, in ffmpeg's own words.

    Shown beside the verdict rather than folded into it. Claiming the GPU is
    too old when the real cause is which ffmpeg build was downloaded sends
    people down entirely the wrong path, and on Windows that build is a real
    variable rather than a distro constant.
    """
    from timelapse_platform import resolve_tool
    text = str(answer or "").strip().strip('"')
    if not text:
        return []
    _codec, failures, _problem = setup.detect_encoders(
        resolve_tool(text, "ffmpeg"))
    lines = []
    for codec, message, hint in failures:
        detail = hint or (message or "")[:150]
        lines.append(f"{codec}: {detail}" if detail else f"{codec} unavailable")
    return lines


def check_interval(text):
    """(level, message, seconds)."""
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return FAIL, "Give a whole number of seconds.", None
    if not 1 <= value <= 3600:
        return FAIL, "Between 1 and 3600 seconds.", None

    per_day = int(86400 / value)
    # The floor that produces nothing at all, silently and for ever: the
    # encoder skips any day with fewer frames than encode.min_frames.
    if per_day < 100:
        return (FAIL, f"{value}s gives only {per_day} frames a day, and a day "
                      f"with under 100 is skipped, so no video would ever be "
                      f"made.", value)
    if value > 300:
        return WARN, f"{value}s gives {per_day} frames a day, a very short "\
                     f"video.", value
    return OK, f"{per_day} frames a day per camera.", value


def check_framerate(text, interval=None):
    """(level, message, fps). The video's length falls out of the two."""
    try:
        value = int(str(text).strip())
    except (TypeError, ValueError):
        return FAIL, "Give a whole number.", None
    if not 1 <= value <= 240:
        return FAIL, "Between 1 and 240 frames per second.", None
    if not interval:
        return OK, "", value
    per_day = int(86400 / interval)
    return OK, (f"A full day becomes about "
                f"{setup.video_length(per_day, value)} of video."), value


def check_camera_name(name, cameras, skip=None):
    """(level, message, cleaned). Refuses what the filesystem will refuse."""
    from timelapse_platform import is_reserved_name

    raw = str(name or "").strip()
    if not raw:
        return FAIL, "Every camera needs a name.", ""
    cleaned = setup.sanitise_name(raw, "")
    if not cleaned:
        return FAIL, ("A name needs letters or digits in it. Only letters, "
                      "digits, - and _ are kept, because the name becomes a "
                      "folder."), ""
    if is_reserved_name(cleaned):
        return FAIL, (f"{cleaned} is a name Windows reserves for a device, "
                      f"and frames written under it would go nowhere."), ""
    if setup.name_taken(cameras, cleaned, skip=skip):
        return FAIL, f"There is already a camera called {cleaned}.", ""
    if cleaned != raw:
        return WARN, f"Stored as {cleaned}, since the name becomes a folder.", \
            cleaned
    return OK, f"Frames will go in a folder called {cleaned}.", cleaned


def check_camera_url(method, url):
    """(level, message). Shape only: reachability is what Test is for."""
    text = str(url or "").strip()
    if not text:
        return FAIL, "Give the camera's snapshot or stream address.", ""
    if method == "rtsp":
        if not text.lower().startswith("rtsp://"):
            return FAIL, "An RTSP stream address starts with rtsp://.", ""
        return OK, "", text
    if not text.lower().startswith(("http://", "https://")):
        return FAIL, "A snapshot address starts with http:// or https://.", ""
    return OK, "", text


def browse_start(current, isdir=None):
    """Where a Browse button should open, given what is already in the box.

    Reported from the first real run: Browse ignored the prepopulated path and
    opened somewhere else, so the operator had to navigate back to the folder
    the box was already showing them.

    Empty when there is nothing usable, which lets the dialog fall back to its
    own default rather than being pointed at a directory that is not there.
    """
    isdir = os.path.isdir if isdir is None else isdir
    text = str(current or "").strip().strip('"')
    if not text:
        return ""
    try:
        if isdir(text):
            return text
        parent = os.path.dirname(text)
        return parent if parent and isdir(parent) else ""
    except OSError:
        # is_dir() raises on Windows for a directory this account may not
        # read, where on Linux it merely answers False.
        return ""


# ---------------------------------------------------------------------------
# Cameras
#
# The preset list is the console wizard's, read rather than restated: it is the
# answer to "what is a Reolink snapshot URL", and a second copy would become a
# second answer. Credentials go into the URL for the presets whose template
# names them (Reolink, RTSP) and into their own fields for the ones that use
# HTTP digest or basic, which is the same split the schema has always had.
# ---------------------------------------------------------------------------

def camera_types():
    """(label, method, auth, template) for each type the wizard offers."""
    return list(setup.CAMERA_PRESETS)


def preset_named(label):
    """The preset carrying this label, falling back to Custom URL.

    The detail pane holds a label rather than an index, so that reordering the
    presets cannot silently repoint a camera at a different make, which is the
    same reasoning VENDOR_HINTS is keyed on labels for.
    """
    for preset in camera_types():
        if preset[0] == label:
            return preset
    return camera_types()[-1]


def preset_is_custom(preset):
    return preset[3] is None


def preset_wants_credentials(preset):
    """Whether this type needs a username and password at all."""
    _label, method, auth, template = preset
    if template is None:
        return True
    return bool(auth in ("digest", "basic") or method == "rtsp"
                or "{user}" in template)


def credentials_go_in_the_url(preset):
    """True when the template embeds them, which changes what redaction sees.

    Worth surfacing in the dialog: for a Reolink the password *is* the URL, so
    "no password field is filled in" does not mean no password is stored.
    """
    return bool(preset[3] and "{password}" in preset[3])


def identify_camera(cam):
    """(label, address, username, password) for a stored camera, or None.

    Which make a camera was set up as. Reported as an inconsistency from a real
    run: a camera added as a Dahua came back as Custom URL the next time the
    page was opened, because the config records the URL and not the make it was
    built from.

    The old behaviour was deliberate and its reasoning was sound: guessing
    wrong would silently rewrite a working camera. What was wrong was treating
    this as a guess. **A candidate is accepted only when the whole round trip
    reproduces the stored URL exactly**, template filled in from what was
    extracted, credentials put back through `quote()` the same way saving will.
    So claiming a make is a claim that pressing Save changes nothing, which is
    the only thing that had to be true.

    Two templates in the list are identical (Hikvision ONVIF and Generic ONVIF
    snapshot), so one of them cannot be told from the other. The first match
    wins, and it does not matter: they build the same URL, so either answer
    saves the same config.
    """
    import re
    from urllib.parse import unquote

    url = str((cam or {}).get("url") or "")
    if not url:
        return None
    holes = {"ip": r"(?P<ip>[^/]+?)", "user": r"(?P<user>.*?)",
             "password": r"(?P<password>.*?)"}
    for preset in camera_types():
        template = preset[3]
        if template is None:
            continue
        pattern = "".join(holes[part[1:-1]] if part[:1] == "{" else
                          re.escape(part)
                          for part in re.split(r"(\{[a-z]+\})", template))
        found = re.match(pattern + "$", url)
        if not found:
            continue
        got = found.groupdict()
        user = unquote(got.get("user", ""))
        password = unquote(got.get("password", ""))
        address = got["ip"]
        # The round trip, not the match: re-quoting has to land back on the
        # same string, or an operator who presses Save without touching
        # anything would silently get a different URL.
        if template.format(ip=setup.url_host(address),
                           user=setup.quote(user),
                           password=setup.quote(password)) != url:
            continue
        if address.startswith("[") and address.endswith("]"):
            address = address[1:-1]         # an IPv6 literal, as it was typed
        return preset[0], address, user, password
    return None


def build_camera(fields, cameras, cam=None):
    """(level, message, camera) from what the dialog is holding.

    A plain dict in and a config entry out, so the assembly can be tested
    without a window. Mirrors add_one_camera() and edit_one_camera(): a preset
    builds its URL from an address, and a custom camera carries the URL it was
    given.
    """
    level, message, name = check_camera_name(fields.get("name"), cameras,
                                             skip=cam)
    if level == FAIL:
        return FAIL, message, None
    name_note = message if level == WARN else ""

    preset = fields.get("preset") or camera_types()[-1]
    _label, method, auth, template = preset
    user = str(fields.get("username") or "")
    password = str(fields.get("password") or "")

    if template is None:
        method = str(fields.get("method") or "http")
        auth = str(fields.get("auth") or "none").lower()
        url_level, url_message, url = check_camera_url(method,
                                                       fields.get("url"))
        if url_level == FAIL:
            return FAIL, url_message, None
    else:
        address = str(fields.get("address") or "").strip()
        if not address:
            return FAIL, "Give the camera's IP address or hostname.", None
        # url_host brackets an IPv6 literal so it can sit inside a URL. The
        # rule lives in timelapse_encode and is reached through the wizard,
        # which is where every other caller reads it from.
        host = setup.url_host(address)
        url = template.format(ip=host, user=setup.quote(user),
                              password=setup.quote(password))

    camera = dict(cam or {})
    camera.update({"name": name, "method": method, "url": url,
                   "enabled": bool(fields.get("enabled", True))})
    if method == "http":
        camera["auth"] = auth or "none"
        if camera["auth"] in ("digest", "basic"):
            camera["username"] = user
            camera["password"] = password
        else:
            # Not left behind: a stale username under auth "none" reads as a
            # credential that is in use and is not.
            camera.pop("username", None)
            camera.pop("password", None)
    else:
        camera.pop("auth", None)
        camera.pop("username", None)
        camera.pop("password", None)
        camera.setdefault("quality", 2)

    smoothing = fields.get("smoothing")
    if smoothing:
        camera["smooth_frames"] = int(smoothing)
    else:
        camera.pop("smooth_frames", None)

    # Per-camera settings are keyed on ABSENCE, and that is what makes a later
    # change to the global still move this camera. Storing a copy that happens
    # to equal the global would pin every camera anybody had merely opened
    # here, silently, which is the one way this page could do real damage.
    for key in ("interval_seconds", "framerate"):
        value = fields.get(key)
        if value:
            camera[key] = int(value)
        else:
            camera.pop(key, None)

    return (WARN if name_note else OK), name_note, camera


def check_camera_interval(text, default):
    """(level, message, seconds or None). None means follow the global.

    Blank and "the same as the global" are the same answer, and both remove
    the key rather than storing it.
    """
    raw = str(text or "").strip()
    if not raw:
        return OK, "Follows the global setting.", None
    level, message, value = check_interval(raw)
    if level == FAIL:
        return FAIL, message, None
    if value == default:
        return OK, "The same as the global, so it follows it.", None
    return level, message, value


def check_camera_framerate(text, default, interval=None):
    """(level, message, fps or None). None means follow the global."""
    raw = str(text or "").strip()
    if not raw:
        return OK, "Follows the global setting.", None
    level, message, value = check_framerate(raw, interval)
    if level == FAIL:
        return FAIL, message, None
    if value == default:
        return OK, "The same as the global, so it follows it.", None
    return level, message, value


def check_smoothing(text):
    """(level, message, frames or None). Blank means off, which is the default."""
    from timelapse_encode import SMOOTH_MAX, SMOOTH_MIN

    raw = str(text or "").strip()
    if not raw:
        return OK, "", None
    try:
        value = int(raw)
    except ValueError:
        return FAIL, "Give a whole number of frames, or leave it empty.", None
    if not SMOOTH_MIN <= value <= SMOOTH_MAX:
        return FAIL, "Between %d and %d frames, or empty for none." % (
            SMOOTH_MIN, SMOOTH_MAX), None
    return OK, "Each frame is blended with the %d around it." % value, value


def camera_label(cam):
    """The one line the camera list shows for this entry.

    The name, which is what identifies a camera here, plus whether it is
    switched off. Disabling is as destructive as removing (the encoder builds
    its work list from enabled cameras, so a disabled one's frames are
    stranded), and a list that looked identical either way would hide that.
    """
    cam = cam or {}
    name = str(cam.get("name", "") or "").strip()
    if not name:
        return "(new camera)"
    return name if cam.get("enabled", True) else "%s  (disabled)" % name


def camera_form_values(cam):
    """What the detail pane starts out holding for this camera.

    An existing camera opens on the make its URL was built from, worked out by
    identify_camera(), which only answers when rebuilding reproduces that URL
    exactly. Anything it cannot account for opens on Custom URL showing the URL
    itself, which is the honest answer and the console wizard's edit path. A
    new one opens on the first make in the list, where an address is all that
    is wanted.

    The cadence and frame rate boxes are blank when the camera carries neither
    key, because blank is what "follows the global" looks like, and an empty
    box is the only rendering of that which cannot be saved as a copy.
    """
    from timelapse_encode import SMOOTH_DEFAULT

    cam = cam or {}
    types = camera_types()
    smooth = cam.get("smooth_frames") or 0
    known = identify_camera(cam)
    return {"name": str(cam.get("name", "") or ""),
            "type": (known[0] if known else
                     types[-1][0] if cam.get("url") else types[0][0]),
            "address": known[1] if known else "",
            "url": str(cam.get("url", "") or ""),
            "auth": str(cam.get("auth", "") or "digest"),
            # A template that names no credentials extracts none, so the
            # stored fields are what a digest camera's Username and Password
            # come from. Taking the extracted ones unconditionally would
            # blank both for every preset except Reolink and RTSP.
            "username": (known and known[2]) or str(cam.get("username", "")
                                                    or ""),
            "password": (known and known[3]) or str(cam.get("password", "")
                                                    or ""),
            "interval": str(cam.get("interval_seconds") or ""),
            "framerate": str(cam.get("framerate") or ""),
            "smoothing_on": bool(smooth),
            "smoothing": str(smooth or SMOOTH_DEFAULT),
            "enabled": bool(cam.get("enabled", True))}


def form_is_dirty(loaded, current):
    """Has the detail pane been changed since it was filled in?

    Keyed on what was loaded, so a key the pane does not carry cannot make an
    untouched camera look edited. What it buys is the prompt before a click
    somewhere else throws away a typed password, which is the one thing in
    this pane that cannot be recovered by looking at the config.
    """
    return any(str(current.get(key, "")) != str(value)
               for key, value in (loaded or {}).items())


# ---------------------------------------------------------------------------
# Notification sinks
#
# The field names are the schema's, not this file's invention. The first cut
# stored a single "url" for every sink, which would have written entries the
# encoder reads as empty: Discord wants webhook_url, ntfy wants a server and a
# topic, Telegram wants a token and a chat id.
# ---------------------------------------------------------------------------

# kind -> (title, help, [(key, label, secret, default)])
NOTIFY_FIELDS = {
    "discord": ("Discord",
                "Server Settings, Integrations, Webhooks, then Copy Webhook "
                "URL.",
                [("webhook_url", "Webhook URL", False, "")]),
    "ntfy": ("ntfy",
             "Delivers to a phone with no account. Pick a topic nobody else "
             "would guess and subscribe to it in the app.",
             [("server", "Server", False, "https://ntfy.sh"),
              ("topic", "Topic", False, ""),
              ("token", "Access token", True, "")]),
    "telegram": ("Telegram",
                 "A bot token from @BotFather, then message your bot once and "
                 "read the chat id from @userinfobot.",
                 [("token", "Bot token", True, ""),
                  ("chat_id", "Chat id", False, "")]),
}

# What has to be filled in for a sink to be worth enabling at all.
NOTIFY_REQUIRED = {"discord": ("webhook_url",), "ntfy": ("topic",),
                   "telegram": ("token", "chat_id")}


def build_sink(kind, values, enabled=True):
    """(level, message, sink) for one notification target."""
    if kind not in NOTIFY_FIELDS:
        return FAIL, "Unknown notification type.", None

    sink = {"type": kind, "enabled": bool(enabled)}
    for key, _label, _secret, default in NOTIFY_FIELDS[kind][2]:
        sink[key] = str(values.get(key, "") or "").strip() or default
    if kind == "discord":
        sink.setdefault("username", "Timelapse Bot")

    if not enabled:
        return OK, "", sink

    missing = [key for key in NOTIFY_REQUIRED[kind] if not sink.get(key)]
    if missing:
        labels = {k: l for k, l, _s, _d in NOTIFY_FIELDS[kind][2]}
        return FAIL, "%s needs %s." % (
            NOTIFY_FIELDS[kind][0],
            " and ".join(labels.get(k, k) for k in missing)), sink

    if kind == "discord" and not sink["webhook_url"].startswith("https://"):
        return FAIL, "A Discord webhook URL starts with https://.", sink
    if kind == "ntfy" and not sink["server"].startswith(("http://", "https://")):
        return FAIL, "The ntfy server is a URL, such as https://ntfy.sh.", sink
    if kind == "telegram" and ":" not in sink["token"]:
        return FAIL, "A bot token looks like 123456:ABC-DEF...", sink
    if kind == "ntfy" and "ntfy.sh" in sink["server"]:
        return WARN, ("On the public server the topic is the only secret "
                      "there is."), sink
    return OK, "", sink


def sink_values(cfg, kind):
    """What the fields for `kind` should start out holding."""
    current = setup.existing_sink(cfg, kind) or {}
    values = {}
    for key, _label, _secret, default in NOTIFY_FIELDS[kind][2]:
        values[key] = str(current.get(key, "") or "") or default
    return values, bool(current.get("enabled"))


def check_destination(dest):
    """(level, message, stored). What the transfer page saves, not what it read.

    Returning the resolved value is the point. A mapped drive letter belongs to
    one logon session and the nightly encode has its own, so U:\\TL must be
    stored as its \\\\server\\share form or it fails with "path not found" on a
    folder the operator can open in Explorer.
    """
    text = str(dest or "").strip().strip('"')
    if not text:
        return FAIL, "Give a folder or a network path.", ""
    if setup.looks_like_ssh_spec(text):
        return FAIL, ("That is an rsync-over-SSH destination, which is Linux "
                      "only. Give a folder or a \\\\server\\share path."), ""

    unc = setup.network_path(text)
    if unc:
        return WARN, (f"{text[:2]} is a mapped drive, which exists only for "
                      f"you. Saving where it really points: {unc}"), unc
    if setup.drive_is_local(text) is False:
        return FAIL, (f"{text[:2]} is not a drive this machine can use "
                      f"unattended. It is either a mapping that will not "
                      f"survive a reboot or a letter that does not exist. "
                      f"Give the \\\\server\\share path instead."), ""
    return OK, "", text


def network_choices(drives=None):
    """(label, unc) for every mapped drive, for the destination picker.

    Windows' own folder browser is shown by *this* process, and this process is
    elevated, so it lists the local disks and no network drives at all: the
    operator is invited to browse for the share they use daily and it is not
    there. Reported from a real run, and it is the drive-letter trap arriving
    in a third disguise, after the config storing `U:\\TL` verbatim and the
    check that could not see the mapping either.

    The label carries the letter as well as the target, because the letter is
    what the operator recognises and the UNC is what gets stored.
    """
    from timelapse_platform import mapped_drives

    drives = mapped_drives() if drives is None else drives
    return [("%s   %s" % (letter, unc), unc) for letter, unc in drives]


def no_network_advice():
    """What to say when the picker has nothing to offer."""
    return ("No mapped network drives were found for your account. Type the "
            "share as \\\\server\\share instead, which is what gets stored in "
            "any case.")


def destination_needs_credentials(dest):
    """True when the account question applies, which is UNC and only UNC."""
    return bool(is_unc(dest))


def credentials_advice(dest):
    """Why a share is likely to need a sign-in even though you can write to it."""
    return ("The nightly encode runs as the system account, which introduces "
            "itself to %s as this computer rather than as you, so it can be "
            "refused where you are allowed. Storing a username and password "
            "removes the doubt: the encode signs in with them itself."
            % (share_root(dest) or "the server"))


def preflight(cfg):
    """Everything still wrong with this config, as a list of sentences.

    The review page's whole job. Returning sentences rather than a boolean is
    deliberate: "you cannot finish" without saying what to go back and change
    is the kind of dead end a console wizard never produces, because there you
    were told at the question.
    """
    problems = []
    paths = cfg.get("paths", {})
    if not paths.get("frames_root"):
        problems.append("No storage folder chosen.")
    if not paths.get("ffmpeg"):
        problems.append("No ffmpeg, so nothing would ever be encoded.")
    cameras = [c for c in cfg.get("cameras", []) if c.get("enabled", True)]
    if not cameras:
        problems.append("No cameras are enabled, so nothing would be captured.")
    t = cfg.get("transfer", {})
    if t.get("enabled") and not t.get("destination"):
        problems.append("Transfer is on but has no destination.")
    return problems


def encoder_details(cfg, codec):
    """How the video will be encoded: "hevc_nvenc, cq 24, preset p6".

    Read out of the argument list the encoder is actually built with rather
    than restated here, so a change to a preset or a quality setting cannot
    leave this panel describing an older one. The codec comes from the ffmpeg
    check on the first page, which is the same probe the nightly run makes.
    """
    from timelapse_encode import build_candidates

    if not codec:
        return ""
    for candidate in build_candidates(cfg.get("encode", {})):
        if candidate["codec"] != codec:
            continue
        args = candidate["args"]
        bits = [codec]
        for flag, label in (("-cq", "cq"), ("-crf", "crf"),
                            ("-preset", "preset")):
            if flag in args:
                bits.append("%s %s" % (label, args[args.index(flag) + 1]))
        return ", ".join(bits)
    return codec


def summary_lines(cfg, codec=None):
    """(label, value) pairs for the review page. Names, never secrets.

    The same rule summarise() had to learn: a webhook URL is the authority to
    post exactly as a password is, and this panel is what gets screenshotted
    into a bug report.
    """
    paths = cfg.get("paths", {})
    cap = cfg.get("capture", {})
    enc = cfg.get("encode", {})
    t = cfg.get("transfer", {})
    cams = [c for c in cfg.get("cameras", []) if c.get("enabled", True)]

    interval = cap.get("interval_seconds", 0)
    rows = [
        ("Frames", paths.get("frames_root", "")),
        ("Videos", paths.get("video_output", "")),
        ("Logs", paths.get("log_dir", "")),
        ("ffmpeg", paths.get("ffmpeg", "")),
        ("Cadence", f"one frame every {interval}s"
                    f" ({int(86400 / interval)} a day)" if interval else ""),
        ("Video", ", ".join(x for x in
                            (f"{enc.get('framerate', 0)} fps",
                             enc.get("container", "mkv"),
                             encoder_details(cfg, codec)) if x)),
        ("Cameras", ", ".join(c.get("name", "?") for c in cams) or "none"),
    ]
    if t.get("enabled"):
        who = t.get("username")
        rows.append(("Transfer", t["destination"] +
                     (f" (signing in as {who})" if who else "")))
    else:
        rows.append(("Transfer", "off, videos stay in the videos folder"))
    rows.append(("Notifications", setup.summarise_sinks(cfg) or "none"))
    return rows


def next_steps(cfg):
    """What to tell the operator once the config is written.

    No commands. The first version ended by telling them to open an
    Administrator prompt and run `timelapse test`, from a window built so that
    they would not have to, which rather gave the game away. The checks are a
    button on the same dialog now.
    """
    lines = ["Capture starts on its own and keeps running.",
             "The first video appears after midnight, once a whole day has "
             "been captured."]
    t = cfg.get("transfer", {})
    if t.get("enabled"):
        lines.append("Finished videos are then moved to " + t["destination"] +
                     ".")
    else:
        lines.append("Finished videos stay in " +
                     cfg.get("paths", {}).get("video_output", "the videos "
                                                              "folder") + ".")
    return lines


# ---------------------------------------------------------------------------
# THE SHOW LAYER
#
# tkinter below this line and nowhere above it. Every branch here is either a
# widget or a call into the layer above; anything that has to decide something
# belongs up there where it can be tested.
# ---------------------------------------------------------------------------

COLOURS = {OK: "#1a7f37", WARN: "#9a6700", FAIL: "#b42318"}


def run(config_path=None, existing=None):
    """Open the window. Returns the exit code the process should use."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    config_path = config_path or CONFIG_PATH

    class Wizard(tk.Tk):

        def __init__(self):
            tk.Tk.__init__(self)
            self.title("timelapse-maker setup")
            self.geometry(WINDOW)
            self.minsize(700, 520)
            self.cfg = existing or setup.default_config()
            setup.localise_locations(self.cfg)
            self.written = False
            # Which encoder this machine will actually use. Found by the
            # ffmpeg check on the first page, and carried rather than stored:
            # it is a property of the box, not a setting, and the nightly run
            # probes for it again anyway.
            self.codec = None

            self.header = ttk.Label(self, text="", font=("Segoe UI", 14, "bold"))
            self.header.pack(anchor="w", padx=16, pady=(14, 0))
            self.blurb = ttk.Label(self, text="", wraplength=700,
                                   justify="left", foreground="#555")
            self.blurb.pack(anchor="w", padx=16, pady=(2, 8))

            self.body = ttk.Frame(self)
            self.body.pack(fill="both", expand=True, padx=16)

            bar = ttk.Frame(self)
            bar.pack(fill="x", padx=16, pady=12)
            self.back = ttk.Button(bar, text="Back", command=self.go_back)
            self.back.pack(side="left")
            ttk.Button(bar, text="Cancel",
                       command=self.cancel).pack(side="right")
            self.next = ttk.Button(bar, text="Next", command=self.go_next)
            self.next.pack(side="right", padx=(0, 8))

            # Five, not seven. Storage, ffmpeg and cadence were a page each and
            # each held one question: three Next clicks to answer three things
            # that are all "where does this run", and no page filled its window.
            self.pages = [self.page_basics, self.page_cameras,
                          self.page_transfer, self.page_notify,
                          self.page_review]
            self.index = 0
            self.show()

        # -- plumbing ----------------------------------------------------

        def show(self):
            for child in self.body.winfo_children():
                child.destroy()
            self.commit = None
            self.back.state(["!disabled"] if self.index else ["disabled"])
            self.next.config(text="Finish" if self.index == len(self.pages) - 1
                             else "Next")
            self.pages[self.index]()

        def go_next(self):
            if self.commit and not self.commit():
                return
            if self.index == len(self.pages) - 1:
                return self.finish()
            self.index += 1
            self.show()

        def go_back(self):
            if self.index:
                self.index -= 1
                self.show()

        def cancel(self):
            if messagebox.askokcancel(
                    "Close setup",
                    "Close without saving? Nothing has been changed yet."):
                self.destroy()

        def heading(self, title, blurb=""):
            self.header.config(text=title)
            self.blurb.config(text=blurb)

        def status(self, parent):
            label = ttk.Label(parent, text="", wraplength=680, justify="left")
            label.pack(anchor="w", pady=(6, 0))
            return label

        def say(self, label, level, message):
            label.config(text=message, foreground=COLOURS.get(level, "#333"))

        def field(self, parent, label, value="", width=58, secret=False,
                  label_width=22, grow=True):
            # 22, not 18: "Discord webhook URL" is nineteen characters and was
            # arriving as "Discord webhook UR...". A label that truncates makes
            # the field beside it a guess.
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=label, width=label_width).pack(side="left")
            var = tk.StringVar(value=value)
            entry = ttk.Entry(row, textvariable=var, width=width,
                              show="*" if secret else "")
            # grow=False leaves room to the right of a narrow box, which is
            # where a one-line verdict goes on a page holding several of them.
            entry.pack(side="left", fill="x" if grow else None, expand=grow)
            return var, row

        def link(self, parent, text, url):
            """A label that opens a page, since a URL printed is a URL typed.

            webbrowser is stdlib and imported here rather than at module
            scope, on the same footing as every other import in this file that
            only the window needs.
            """
            import webbrowser

            label = ttk.Label(parent, text=text, foreground="#0b5cad",
                              cursor="hand2", font=("Segoe UI", 9,
                                                    "underline"))
            label.bind("<Button-1>", lambda _e: webbrowser.open(url))
            return label

        def browse_into(self, var, directory=True):
            # Opening where the box already points, which the first version did
            # not: it started wherever the dialog felt like and the operator had
            # to navigate back to the folder they were being shown.
            start = browse_start(var.get())
            options = {"initialdir": start} if start else {}
            chosen = (filedialog.askdirectory(**options) if directory
                      else filedialog.askopenfilename(**options))
            if chosen:
                var.set(os.path.normpath(chosen))

        def pick_network(self, var):
            """Choose one of the operator's mapped drives, then browse into it.

            The share goes into the box as its UNC, which is what has to be
            stored: a letter belongs to one logon session and the nightly
            encode has its own. Browsing continues from there, so picking a
            subfolder of the share is the same two clicks it would have been
            if the folder dialog could see the drive at all.
            """
            choices = network_choices()
            if not choices:
                messagebox.showinfo("Network", no_network_advice())
                return

            box = tk.Toplevel(self)
            box.title("Network shares")
            box.transient(self)
            box.grab_set()
            frame = ttk.Frame(box, padding=12)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="Your mapped drives, and where they really "
                                  "point. The address is what gets stored.",
                      wraplength=380, justify="left").pack(anchor="w",
                                                           pady=(0, 8))
            listing = tk.Listbox(frame, height=min(10, len(choices)), width=46)
            listing.pack(fill="both", expand=True)
            for label, _unc in choices:
                listing.insert("end", label)
            listing.selection_set(0)

            def choose(_event=None):
                picked = listing.curselection()
                if not picked:
                    return
                var.set(choices[picked[0]][1])
                box.destroy()
                self.browse_into(var)

            listing.bind("<Double-Button-1>", choose)
            bar = ttk.Frame(frame)
            bar.pack(fill="x", pady=(10, 0))
            ttk.Button(bar, text="Cancel",
                       command=box.destroy).pack(side="right")
            ttk.Button(bar, text="Select network share",
                       command=choose).pack(side="right", padx=(0, 8))

        # -- pages -------------------------------------------------------

        def page_basics(self):
            """Storage, ffmpeg and cadence, which were three pages.

            They belong together: each was one question with a window to
            itself, and all three answer "how does this machine run", where
            cameras and destinations are about everything else. Three
            LabelFrames rather than three pages means the operator can see the
            cadence while choosing the disk, which is the one dependency
            between them worth seeing.
            """
            self.heading(
                "Global configuration",
                "Where the frames go, which ffmpeg turns them into video, and "
                "how often a frame is taken. Frames are deleted once their day "
                "has been encoded, so the folder needs room for about a day at "
                "a time.")
            cap = self.cfg["capture"]
            enc = self.cfg["encode"]

            # -- storage --------------------------------------------------
            store = ttk.LabelFrame(self.body, text="Storage", padding=8)
            store.pack(fill="x")
            current = self.cfg["paths"].get("frames_root", "")
            folder, row = self.field(store, "Data folder",
                                     str(Path(current).parent)
                                     if current else "", width=46,
                                     label_width=LABELS)
            ttk.Button(row, text="Browse",
                       command=lambda: self.browse_into(folder)).pack(
                           side="left", padx=6)
            store_note = self.status(store)
            derived = ttk.Label(store, text="", foreground="#555",
                                justify="left")
            derived.pack(anchor="w", pady=(4, 0))

            # -- ffmpeg ---------------------------------------------------
            tools = ttk.LabelFrame(self.body, text="ffmpeg", padding=8)
            tools.pack(fill="x", pady=(10, 0))
            # The link ends the sentence rather than sitting inside it: three
            # packed labels cannot be kerned into one line of prose, and a
            # gap before a full stop reads as a rendering fault.
            blurb = ttk.Frame(tools)
            blurb.pack(anchor="w", pady=(0, 4))
            ttk.Label(blurb, text="Path to ffmpeg.exe. You can download and "
                                  "install it from",
                      foreground="#555").pack(side="left")
            self.link(blurb, "here", FFMPEG_URL).pack(side="left", padx=(5, 0))
            ff, ff_row = self.field(tools, "Folder or ffmpeg.exe",
                                    self.cfg["paths"].get("ffmpeg", "")
                                    or setup.find_binary("ffmpeg", ""),
                                    width=38, label_width=LABELS)
            ttk.Button(ff_row, text="Browse",
                       command=lambda: self.browse_into(ff)).pack(side="left",
                                                                  padx=6)
            ff_note = self.status(tools)
            ff_detail = ttk.Label(tools, text="", foreground="#555",
                                  wraplength=740, justify="left")
            ff_detail.pack(anchor="w", pady=(4, 0))

            def test_ffmpeg():
                self.config(cursor="watch")
                self.update_idletasks()
                try:
                    level, message, _ffmpeg, ffprobe, _codec = \
                        check_ffmpeg(ff.get())
                    self.say(ff_note, level, message)
                    lines = encoder_notes(ff.get())
                    if ffprobe:
                        lines.insert(0, f"ffprobe -> {ffprobe}")
                    ff_detail.config(text="\n".join(lines))
                finally:
                    self.config(cursor="")

            ttk.Button(ff_row, text="Test",
                       command=test_ffmpeg).pack(side="left")

            # -- cadence --------------------------------------------------
            timing = ttk.LabelFrame(self.body, text="Default capture settings",
                                    padding=8)
            timing.pack(fill="x", pady=(10, 0))
            iv, iv_row = self.field(timing, "Seconds between frames",
                                    str(cap.get("interval_seconds", 5)),
                                    width=8, label_width=LABELS,
                                    grow=False)
            iv_note = ttk.Label(iv_row, text="", wraplength=470,
                                justify="left")
            iv_note.pack(side="left", padx=(10, 0))
            fps, fps_row = self.field(timing, "Frames per second",
                                      str(enc.get("framerate", 60)),
                                      width=8, label_width=LABELS, grow=False)
            fps_note = ttk.Label(fps_row, text="", wraplength=470,
                                 justify="left")
            fps_note.pack(side="left", padx=(10, 0))
            # Said here rather than only on the camera page, because "default"
            # in the title is a promise this sentence has to keep: a camera
            # carrying neither key follows these, which is what lets a change
            # here still move it later.
            ttk.Label(timing,
                      text="You can override these settings for each camera, "
                           "if needed.",
                      foreground="#555").pack(anchor="w", pady=(6, 0))

            def refresh(*_a):
                level, message = check_storage(folder.get())
                self.say(store_note, level, message)
                if folder.get().strip():
                    p = storage_paths(folder.get())
                    derived.config(text="frames  -> %s\nvideos  -> %s\n"
                                        "logs    -> %s" % (p["frames_root"],
                                                           p["video_output"],
                                                           p["log_dir"]))
                else:
                    derived.config(text="")
                level, message, seconds = check_interval(iv.get())
                self.say(iv_note, level, message)
                level, message, _rate = check_framerate(fps.get(), seconds)
                self.say(fps_note, level, message)

            for var in (folder, iv, fps):
                var.trace_add("write", refresh)
            refresh()

            def commit():
                level, message = check_storage(folder.get())
                if level == FAIL:
                    messagebox.showerror("Storage", message)
                    return False
                self.config(cursor="watch")
                self.update_idletasks()
                try:
                    level, message, ffmpeg, ffprobe, _codec = \
                        check_ffmpeg(ff.get())
                finally:
                    self.config(cursor="")
                if level == FAIL:
                    messagebox.showerror("ffmpeg", message)
                    return False
                level, message, interval = check_interval(iv.get())
                if level == FAIL:
                    messagebox.showerror("Cadence", message)
                    return False
                level, message, rate = check_framerate(fps.get(), interval)
                if level == FAIL:
                    messagebox.showerror("Frame rate", message)
                    return False

                self.cfg["paths"].update(storage_paths(folder.get()))
                self.cfg["paths"]["ffmpeg"] = ffmpeg
                if ffprobe:
                    self.cfg["paths"]["ffprobe"] = ffprobe
                self.codec = _codec
                cap["interval_seconds"] = interval
                # The fetch timeout must stay under the interval, or a slow
                # camera's request outlives the tick that asked for it.
                cap["timeout_seconds"] = min(cap.get("timeout_seconds", 4),
                                             max(1, interval - 1))
                enc["framerate"] = rate
                enc["gop"] = rate * setup.GOP_SECONDS
                return True

            self.commit = commit

        def page_cameras(self):
            """The list on the left, the camera it names on the right.

            The first cut put every camera behind a modal dialog, so the only
            way to see what a camera was set to was to open it, and the only
            way to compare two was to remember the first. Editing in place
            beside the list is what an operator with eight cameras needs, and
            it is where the credentials, the test and the enable switch belong
            rather than three clicks away.
            """
            self.heading(
                "Camera setup",
                "Pick one on the left to change it on the right. A camera's "
                "name becomes the folder its frames live in, so renaming one "
                "later means moving that folder.")
            cams = self.cfg.setdefault("cameras", [])
            presets = camera_types()
            labels = [p[0] for p in presets]
            # The camera being edited, by identity rather than by position: a
            # save can rename it and a remove can shift everything after it.
            state = {"cam": None, "loaded": {}}

            panes = ttk.Frame(self.body)
            panes.pack(fill="both", expand=True)

            left = ttk.Frame(panes)
            left.pack(side="left", fill="y")
            # exportselection=False, or clicking into any Entry on the right
            # hands the X selection over and the list silently unhighlights
            # the camera being edited.
            listing = tk.Listbox(left, height=14, width=26,
                                 exportselection=False)
            listing.pack(fill="both", expand=True)
            list_buttons = ttk.Frame(left)
            list_buttons.pack(fill="x", pady=(6, 0))

            right = ttk.LabelFrame(panes, text="Details", padding=10)
            right.pack(side="left", fill="both", expand=True, padx=(12, 0))
            right.columnconfigure(1, weight=1)

            # grid rather than pack for the detail rows, and grid_remove()
            # rather than pack_forget(): a removed cell remembers where it was,
            # so a row that comes back cannot land at the bottom of the pane.
            # That defect has been shipped twice here already.
            place = [0]

            def add_row(label, widget):
                text = ttk.Label(right, text=label)
                text.grid(row=place[0], column=0, sticky="w", pady=3,
                          padx=(0, 8))
                widget.grid(row=place[0], column=1, sticky="we", pady=3)
                place[0] += 1
                return text, widget

            def entry_row(label, secret=False):
                var = tk.StringVar()
                return var, add_row(label, ttk.Entry(
                    right, textvariable=var, width=34,
                    show="*" if secret else ""))

            name, name_row = entry_row("Name")
            kind = tk.StringVar()
            add_row("Camera type",
                    ttk.Combobox(right, textvariable=kind, values=labels,
                                 state="readonly", width=32))
            address, address_row = entry_row("IP address or host")
            url, url_row = entry_row("Snapshot or stream URL")
            auth = tk.StringVar()
            auth_row = add_row("Authentication",
                               ttk.Combobox(right, textvariable=auth,
                                            values=["digest", "basic", "none"],
                                            state="readonly", width=14))
            user, user_row = entry_row("Username")
            password, pw_row = entry_row("Password", secret=True)

            enabled = tk.BooleanVar(value=True)
            # "Enable timelapse", not "Enabled": sitting directly above the
            # smoothing controls, one word could as easily have meant them.
            ttk.Checkbutton(right, text="Enable timelapse",
                            variable=enabled).grid(
                row=place[0], column=0, columnspan=2, sticky="w", pady=(8, 0))
            place[0] += 1

            # The two per-camera overrides. Empty means "follow the global",
            # which is what the schema means by the key being absent, so the
            # box has to be able to *be* empty: a value prefilled from the
            # global would be saved as a copy and pin the camera to today's
            # setting for ever.
            cap = self.cfg.get("capture", {})
            enc_cfg = self.cfg.get("encode", {})
            globals_ = {"interval": int(cap.get("interval_seconds", 5) or 5),
                        "framerate": int(enc_cfg.get("framerate", 60) or 60)}
            interval, framerate = tk.StringVar(), tk.StringVar()
            for label, var, key, unit in (
                    ("Seconds between frames", interval, "interval",
                     "seconds"),
                    ("Frames per second", framerate, "framerate", "fps")):
                holder = ttk.Frame(right)
                ttk.Entry(holder, textvariable=var, width=6).pack(side="left")
                ttk.Label(holder,
                          text="%s   empty follows the global, which is %d"
                               % (unit, globals_[key]),
                          foreground="#555").pack(side="left", padx=(6, 0))
                add_row(label, holder)

            smooth_bar = ttk.Frame(right)
            smooth_bar.grid(row=place[0], column=0, columnspan=2, sticky="w",
                            pady=(4, 0))
            place[0] += 1
            smooth_on = tk.BooleanVar(value=False)
            smoothing = tk.StringVar()
            ttk.Checkbutton(smooth_bar, text="Smooth motion, averaging",
                            variable=smooth_on).pack(side="left")
            count = ttk.Entry(smooth_bar, textvariable=smoothing, width=5)
            count.pack(side="left", padx=6)
            ttk.Label(smooth_bar, text="frames").pack(side="left")

            # Three message labels, not one, and which one is used says where
            # the answer came from. A verdict beside the button that produced
            # it needs no reading to be attributed; one shared line at the
            # bottom made "Saved." and "Test successful." look interchangeable.
            note = ttk.Label(right, text="", wraplength=380, justify="left")
            note.grid(row=place[0], column=0, columnspan=2, sticky="w",
                      pady=(10, 0))
            place[0] += 1
            right.rowconfigure(place[0], weight=1)
            place[0] += 1

            bar = ttk.Frame(right)
            bar.grid(row=place[0], column=0, columnspan=2, sticky="we",
                     pady=(10, 0))
            tested = ttk.Label(bar, text="", wraplength=250, justify="left")
            saved = ttk.Label(bar, text="", wraplength=250, justify="right",
                              anchor="e")

            def index_of(cam):
                for n, other in enumerate(cams):
                    if other is cam:
                        return n
                return None

            def form_values():
                return {"name": name.get(), "type": kind.get(),
                        "address": address.get(), "url": url.get(),
                        "auth": auth.get(), "username": user.get(),
                        "password": password.get(),
                        "interval": interval.get(),
                        "framerate": framerate.get(),
                        "smoothing_on": smooth_on.get(),
                        "smoothing": smoothing.get(), "enabled": enabled.get()}

            def refresh(*_a):
                preset = preset_named(kind.get())
                custom = preset_is_custom(preset)
                wants = preset_wants_credentials(preset)
                for widgets, wanted in ((address_row, not custom),
                                        (url_row, custom),
                                        (auth_row, custom),
                                        (user_row, wants), (pw_row, wants)):
                    for widget in widgets:
                        widget.grid() if wanted else widget.grid_remove()
                count.state(["!disabled"] if smooth_on.get() else ["disabled"])
                self.say(note, WARN,
                         "This make carries the password inside the URL, so "
                         "it is stored there rather than on its own."
                         if credentials_go_in_the_url(preset) else "")

            def fill(values):
                name.set(values["name"])
                kind.set(values["type"])
                address.set(values["address"])
                url.set(values["url"])
                auth.set(values["auth"])
                user.set(values["username"])
                password.set(values["password"])
                interval.set(values["interval"])
                framerate.set(values["framerate"])
                smooth_on.set(values["smoothing_on"])
                smoothing.set(values["smoothing"])
                enabled.set(values["enabled"])
                state["loaded"] = form_values()
                refresh()

            def show_camera(cam):
                state["cam"] = cam
                for widget in bar.winfo_children():
                    widget.state(["!disabled"] if cam else ["disabled"])
                remover.state(["!disabled"] if cam else ["disabled"])
                # Both verdicts belong to the camera that was showing, so they
                # go with it. A "Test successful." left over from the previous
                # row would be read as being about this one.
                self.say(tested, OK, "")
                self.say(saved, OK, "")
                # After fill(), never before: filling runs refresh(), which
                # owns the note and would wipe anything written first.
                fill(camera_form_values(cam))
                if cam is None:
                    self.say(note, OK, "No cameras yet. Add is on the left.")
                    return
                n = index_of(cam)
                listing.selection_clear(0, "end")
                if n is not None:
                    listing.selection_set(n)
                    listing.activate(n)

            def redraw():
                listing.delete(0, "end")
                for cam in cams:
                    listing.insert("end", camera_label(cam))
                n = index_of(state["cam"])
                if n is not None:
                    listing.selection_clear(0, "end")
                    listing.selection_set(n)

            def save():
                cam = state["cam"]
                if cam is None:
                    return True
                values = form_values()
                self.say(tested, OK, "")
                level, message, frames = check_smoothing(
                    values["smoothing"] if values["smoothing_on"] else "")
                if level == FAIL:
                    self.say(saved, FAIL, message)
                    return False
                level, message, seconds = check_camera_interval(
                    values["interval"], globals_["interval"])
                if level == FAIL:
                    self.say(saved, FAIL, message)
                    return False
                level, message, rate = check_camera_framerate(
                    values["framerate"], globals_["framerate"],
                    seconds or globals_["interval"])
                if level == FAIL:
                    self.say(saved, FAIL, message)
                    return False
                fields = {"name": values["name"],
                          "preset": preset_named(values["type"]),
                          "address": values["address"], "url": values["url"],
                          "auth": values["auth"],
                          "username": values["username"],
                          "password": values["password"],
                          "enabled": values["enabled"], "smoothing": frames,
                          "interval_seconds": seconds, "framerate": rate,
                          "method": "rtsp" if values["url"].lower()
                          .startswith("rtsp://") else "http"}
                level, message, built = build_camera(fields, cams, cam)
                if level == FAIL:
                    self.say(saved, FAIL, message)
                    return False
                cam.clear()
                cam.update(built)
                # The name as stored, not as typed: sanitise_name() may have
                # dropped a space, and leaving the old spelling in the box
                # would make a saved camera read as unsaved for ever.
                name.set(built["name"])
                state["loaded"] = form_values()
                redraw()
                self.say(saved, level if message else OK,
                         message or "Saved.")
                return True

            def test():
                cam = state["cam"]
                if cam is None:
                    return
                values = form_values()
                _level, _message, frames = check_smoothing(
                    values["smoothing"] if values["smoothing_on"] else "")
                fields = {"name": values["name"] or "test",
                          "preset": preset_named(values["type"]),
                          "address": values["address"], "url": values["url"],
                          "auth": values["auth"],
                          "username": values["username"],
                          "password": values["password"],
                          "enabled": True, "smoothing": frames,
                          "method": "rtsp" if values["url"].lower()
                          .startswith("rtsp://") else "http"}
                level, message, built = build_camera(fields, cams, cam)
                self.say(saved, OK, "")
                if level == FAIL:
                    return self.say(tested, FAIL, message)
                self.config(cursor="watch")
                self.update_idletasks()
                try:
                    good = (setup.test_camera_rtsp(built, self.cfg)
                            if built.get("method") == "rtsp"
                            else setup.test_camera(built, self.cfg))
                finally:
                    self.config(cursor="")
                self.say(tested, OK if good else FAIL,
                         "Test successful." if good else
                         "No usable image came back. Check the address, and "
                         "the username and password.")

            def leave_current():
                """True when it is all right to stop editing this camera."""
                cam = state["cam"]
                if cam is None or not form_is_dirty(state["loaded"],
                                                    form_values()):
                    return True
                answer = messagebox.askyesnocancel(
                    "Unsaved changes",
                    "Save the changes to %s first?"
                    % (str(state["loaded"].get("name") or "").strip()
                       or "this camera"))
                if answer is None:
                    return False
                if answer:
                    return save()
                # Discarded and never named: an entry Add created and nobody
                # filled in is not a camera, and leaving it would block the
                # page with an error about a row the operator has already
                # decided against.
                if not str(cam.get("name", "") or "").strip():
                    n = index_of(cam)
                    if n is not None:
                        cams.pop(n)
                    state["cam"] = None
                    redraw()
                return True

            def on_select(_event=None):
                picked = listing.curselection()
                if not picked or picked[0] >= len(cams):
                    return
                wanted = cams[picked[0]]
                if wanted is state["cam"]:
                    return
                if not leave_current():
                    return redraw()          # put the highlight back
                show_camera(wanted)

            def add():
                if not leave_current():
                    return
                cams.append({"enabled": True, "method": "http", "url": ""})
                redraw()
                show_camera(cams[-1])
                name_row[1].focus_set()

            def remove():
                cam = state["cam"]
                if cam is None:
                    return
                n = index_of(cam)
                if n is None:
                    return
                if not messagebox.askokcancel(
                        "Remove camera",
                        "Remove %s?\n\nFrames already captured are left where "
                        "they are; this only stops new ones."
                        % (str(cam.get("name", "") or "").strip()
                           or "this camera")):
                    return
                cams.pop(n)
                state["cam"] = None
                redraw()
                show_camera(cams[min(n, len(cams) - 1)] if cams else None)

            ttk.Button(list_buttons, text="Add", command=add,
                       width=11).pack(side="left")
            remover = ttk.Button(list_buttons, text="Remove", command=remove,
                                 width=11)
            remover.pack(side="right")
            ttk.Button(bar, text="Test", command=test).pack(side="left")
            tested.pack(side="left", padx=(8, 0))
            ttk.Button(bar, text="Save", command=save).pack(side="right")
            saved.pack(side="right", padx=(0, 8))

            kind.trace_add("write", refresh)
            smooth_on.trace_add("write", refresh)
            listing.bind("<<ListboxSelect>>", on_select)
            redraw()
            show_camera(cams[0] if cams else None)

            def commit():
                if not leave_current():
                    return False
                unnamed = [c for c in cams
                           if not str(c.get("name", "") or "").strip()]
                if unnamed:
                    messagebox.showerror(
                        "Cameras",
                        "One camera has no name yet. Give it one and press "
                        "Save, or select it and press Remove.")
                    return False
                if not [c for c in cams if c.get("enabled", True)]:
                    messagebox.showerror(
                        "Cameras",
                        "Add at least one camera and tick Enable timelapse, "
                        "or nothing will be captured.")
                    return False
                return True

            self.commit = commit

        def page_transfer(self):
            self.heading(
                "Timelapses destination",
                "A folder on this machine, or a network path such as "
                "\\\\tower\\videos. Leave this off to keep them locally.")
            t = self.cfg.setdefault("transfer", {})
            on = tk.BooleanVar(value=bool(t.get("enabled")))
            ttk.Checkbutton(self.body, text="Move finished videos",
                            variable=on).pack(anchor="w", pady=(0, 8))

            dest, row = self.field(self.body, "Destination",
                                   t.get("destination", ""), width=44)
            ttk.Button(row, text="Browse",
                       command=lambda: self.browse_into(dest)).pack(side="left",
                                                                    padx=6)
            ttk.Button(row, text="Network",
                       command=lambda: self.pick_network(dest)).pack(
                           side="left")
            ttk.Label(self.body,
                      text="Browse shows only local disks, because setup runs "
                           "as Administrator and drive mappings belong to your "
                           "own sign-in. Network lists them instead.",
                      foreground="#555", wraplength=740,
                      justify="left").pack(anchor="w", pady=(2, 0))
            note = self.status(self.body)
            advice = ttk.Label(self.body, text="", foreground="#555",
                               wraplength=680, justify="left")
            advice.pack(anchor="w", pady=(8, 0))
            # One frame rather than two loose rows, and shown with pack(before=)
            # rather than plain pack(). Re-packing a widget appends it, so
            # hiding these and bringing them back would land them underneath
            # the Test button that acts on them.
            creds = ttk.Frame(self.body)
            user, _ = self.field(creds, "Share username", t.get("username", ""))
            password, _ = self.field(creds, "Share password",
                                     t.get("password", ""), secret=True)
            # The button and its verdict on one row, so the answer sits
            # against the thing that produced it rather than under it.
            bar = ttk.Frame(self.body)
            bar.pack(fill="x", pady=(8, 0))
            tester = ttk.Button(bar, text="Test the destination")
            tester.pack(side="left")
            result = ttk.Label(bar, text="", wraplength=520, justify="left")
            result.pack(side="left", padx=(8, 0))

            def refresh(*_a):
                level, message, stored = check_destination(dest.get())
                self.say(note, level, message if dest.get().strip() else "")
                needs = destination_needs_credentials(stored or dest.get())
                advice.config(text=credentials_advice(stored or dest.get())
                              if needs else "")
                if needs:
                    creds.pack(fill="x", pady=(6, 0), before=bar)
                else:
                    creds.pack_forget()

            def test():
                level, _message, stored = check_destination(dest.get())
                if level == FAIL:
                    return self.say(result, FAIL, _message)
                from timelapse_encode import reach_destination
                self.config(cursor="watch")
                self.update_idletasks()
                try:
                    probe = {"destination": stored, "username": user.get(),
                             "password": password.get()}
                    good, why = reach_destination(probe, stored)
                finally:
                    self.config(cursor="")
                if not good:
                    return self.say(result, FAIL, f"Test failed: {why}")
                if is_unc(stored) and not user.get().strip():
                    # The one case where the detail is the point rather than
                    # noise: it passed as *you*, and the nightly encode is
                    # somebody else. Saying only "Test successful" here would
                    # be the check lying by omission.
                    return self.say(
                        result, WARN,
                        "Test successful as your account, which does not prove "
                        "the nightly encode can write there. Give a username "
                        "and password to be sure.")
                self.say(result, OK, "Test successful.")

            tester.config(command=test)
            dest.trace_add("write", refresh)
            on.trace_add("write", refresh)
            refresh()

            def commit():
                if not on.get():
                    t["enabled"] = False
                    return True
                level, message, stored = check_destination(dest.get())
                if level == FAIL:
                    messagebox.showerror("Destination", message)
                    return False
                t.update({"enabled": True, "destination": stored,
                          "require_mountpoint": False})
                # Absent rather than empty, because "" is a real answer to
                # "did you set one?" and an empty username would make
                # reach_destination() think it had something to try.
                for key, value in (("username", user.get().strip()),
                                   ("password", password.get())):
                    if value:
                        t[key] = value
                    else:
                        t.pop(key, None)
                return True

            self.commit = commit

        def page_notify(self):
            """One box per service, each with its own real fields and a Test.

            The first cut stored a single "url" for all three, which the
            encoder reads as an empty sink: Discord wants webhook_url, ntfy
            wants a server and a topic, Telegram wants a token and a chat id.
            It also sent the operator to the command line for a Telegram chat
            id, from a window built so they would not need one.
            """
            self.heading("Tell you when a run finishes?",
                         "One summary per night, to any of these; this is how "
                         "you find out about a failure without looking. If "
                         "every entry is empty, nothing is sent.")

            state = {}
            for kind in ("discord", "ntfy", "telegram"):
                title, blurb, fields = NOTIFY_FIELDS[kind]
                values, on_now = sink_values(self.cfg, kind)

                group = ttk.LabelFrame(self.body, text=title, padding=6)
                group.pack(fill="x", pady=3)
                on = tk.BooleanVar(value=on_now)
                head = ttk.Frame(group)
                head.pack(fill="x")
                # The switch and the instructions on one line: three services
                # each needing a checkbox, a paragraph, its fields, a button
                # and a verdict is more than a 620-pixel page holds, and the
                # first version ran off the bottom with no way to scroll.
                ttk.Checkbutton(head, text="Send here",
                                variable=on).pack(side="left", padx=(0, 10))
                ttk.Label(head, text=blurb, foreground="#555",
                          wraplength=600, justify="left").pack(side="left")
                boxes = {}
                for key, label, secret, _default in fields:
                    var, _row = self.field(group, label, values.get(key, ""),
                                           width=40, secret=secret)
                    boxes[key] = var

                bar = ttk.Frame(group)
                bar.pack(fill="x", pady=(4, 0))
                result = ttk.Label(bar, text="", wraplength=520,
                                   justify="left")
                state[kind] = (on, boxes, result)

                def tester(kind=kind, on=on, boxes=boxes, result=result):
                    def go():
                        level, message, sink = build_sink(
                            kind, {k: v.get() for k, v in boxes.items()},
                            enabled=True)
                        if level == FAIL:
                            return self.say(result, FAIL, message)
                        self.config(cursor="watch")
                        self.update_idletasks()
                        try:
                            # The real sink code, so what is proved is what the
                            # nightly run will do rather than a payload built
                            # here to look like it.
                            good = setup.send_test_notification(sink,
                                                                config_path)
                        except Exception as exc:            # noqa: BLE001
                            return self.say(result, FAIL,
                                            "Test failed: %s" % exc)
                        finally:
                            self.config(cursor="")
                        self.say(result, OK if good else FAIL,
                                 "Test successful; check the app."
                                 if good else
                                 "Test failed: the service did not accept the "
                                 "message. Check the values above.")
                    return go

                # Button and verdict on one line. The verdict used to have a
                # row of its own, which was blank until the button was pressed
                # and so read as three unexplained gaps.
                ttk.Button(bar, text="Send a test message",
                           command=tester()).pack(side="left")
                result.pack(side="left", padx=(10, 0))

            def commit():
                for kind, (on, boxes, _result) in state.items():
                    level, message, sink = build_sink(
                        kind, {k: v.get() for k, v in boxes.items()},
                        enabled=on.get())
                    if level == FAIL:
                        messagebox.showerror(NOTIFY_FIELDS[kind][0], message)
                        return False
                    # Written even when off, so turning one off is recorded
                    # rather than leaving a sink that looks configured.
                    setup.put_sink(self.cfg, kind, sink)
                return True

            self.commit = commit

        def page_review(self):
            self.heading("Summary",
                         f"This writes {config_path} and creates the folders. "
                         f"The previous configuration is backed up first.")
            problems = preflight(self.cfg)
            if problems:
                box = ttk.Label(self.body,
                                text="Not yet:\n  " + "\n  ".join(problems),
                                foreground=COLOURS[FAIL], justify="left")
                box.pack(anchor="w", pady=(0, 10))

            grid = ttk.Frame(self.body)
            grid.pack(fill="both", expand=True)
            for rown, (label, value) in enumerate(
                    summary_lines(self.cfg, self.codec)):
                ttk.Label(grid, text=label, width=14,
                          font=("Segoe UI", 9, "bold")).grid(row=rown, column=0,
                                                             sticky="nw", pady=2)
                ttk.Label(grid, text=value, wraplength=560,
                          justify="left").grid(row=rown, column=1, sticky="w",
                                               pady=2)
            self.next.state(["disabled"] if problems else ["!disabled"])
            self.commit = lambda: not problems

        def finish(self):
            try:
                setup.create_directories(self.cfg)
                setup.create_state_dir(self.cfg)
                setup.write_config(self.cfg, config_path)
            except Exception as exc:                        # noqa: BLE001
                messagebox.showerror(
                    "Could not save",
                    "%s\n\nNothing was changed. An Administrator prompt is "
                    "needed to write there." % exc)
                return
            self.written = True
            # After the config, never during: restarting first would put the
            # running daemon on the new build with the old settings, and
            # nothing about that looks wrong.
            setup.restart_units()
            self.done_dialog()

        def done_dialog(self):
            """Finished, with the checks as a button rather than a command.

            The first version ended by telling the operator to open an
            Administrator prompt and type `timelapse test`, from a window whose
            entire purpose is that they should not have to.
            """
            box = tk.Toplevel(self)
            box.title("Setup complete")
            box.transient(self)
            box.grab_set()
            frame = ttk.Frame(box, padding=16)
            frame.pack(fill="both", expand=True)

            ttk.Label(frame, text="Setup is complete.",
                      font=("Segoe UI", 12, "bold")).pack(anchor="w")
            ttk.Label(frame, text="\n".join(next_steps(self.cfg)),
                      wraplength=520, justify="left").pack(anchor="w",
                                                           pady=(8, 12))
            ttk.Label(frame,
                      text="The checks look at the cameras, ffmpeg, the disk "
                           "and the destination. They take a moment.",
                      foreground="#555", wraplength=520,
                      justify="left").pack(anchor="w")

            bar = ttk.Frame(frame)
            bar.pack(fill="x", pady=(12, 0))
            ttk.Button(bar, text="Run the checks now",
                       command=lambda: self.checks_window(box)).pack(side="left")

            def close():
                box.destroy()
                self.destroy()

            ttk.Button(bar, text="Close", command=close).pack(side="right")
            box.protocol("WM_DELETE_WINDOW", close)

        def checks_window(self, parent):
            """Run the pre-flight and show what it said.

            The same `timelapse_test.py` the command runs, in a subprocess, so
            there is one pre-flight rather than a second opinion. Its output is
            already plain text here because use_colour() is false when nothing
            is attached to a terminal.
            """
            import subprocess

            box = tk.Toplevel(parent)
            box.title("Checks")
            box.geometry("820x560")
            frame = ttk.Frame(box, padding=10)
            frame.pack(fill="both", expand=True)

            text = tk.Text(frame, wrap="none", font=("Consolas", 9))
            bar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=bar.set)
            bar.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)
            text.insert("end", "Running the checks...\n")
            text.update_idletasks()

            argv = [sys.executable,
                    str(Path(__file__).resolve().parent / "timelapse_test.py"),
                    str(config_path)]
            box.config(cursor="watch")
            try:
                done = subprocess.run(argv, capture_output=True, text=True,
                                      timeout=900, **no_console())
                output = (done.stdout or "") + (done.stderr or "")
            except Exception as exc:                        # noqa: BLE001
                output = "Could not run the checks: %s" % exc
            finally:
                box.config(cursor="")

            text.delete("1.0", "end")
            text.insert("end", output or "The checks said nothing at all.")
            text.configure(state="disabled")
            ttk.Button(box, text="Close",
                       command=box.destroy).pack(pady=(0, 10))

    app = Wizard()
    app.mainloop()
    return 0 if app.written else 1


def warn_not_elevated(config_path):
    """Say so in a box, or on the terminal if there is no display.

    Its own function so that main() stays callable from a test. Inlined, the
    non-elevated path would open a real modal dialog during the suite and wait
    for somebody to click it, which is a hang rather than a failure.
    """
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(
            "Administrator needed",
            "Setup writes to %s, which needs an Administrator prompt.\n\n"
            "Close this, right-click the shortcut and choose Run as "
            "administrator." % config_path)
        root.destroy()
    except Exception:                                       # noqa: BLE001
        print(elevation_hint())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--version" in argv:
        print("timelapse_gui.py %s" % __version__)
        return 0

    if not IS_WINDOWS:
        # Not a refusal on principle: the console wizard is the better tool on
        # a machine that has a terminal in front of it, and this exists for the
        # platform where that is not a given.
        print("The graphical wizard is Windows only. Run: sudo timelapse setup")
        return 2

    # Positional, matching every script here except the wizard, which is what
    # the dispatcher already passes for anything that is not timelapse_setup.
    positional = [a for a in argv if not a.startswith("-")]
    config_path = positional[0] if positional else CONFIG_PATH
    existing = setup.load_existing_config(config_path) \
        if os.path.exists(config_path) else None

    if not is_elevated():
        # Said before any questions rather than at the save, because thirty
        # answers and then "you cannot write that" is the worst possible
        # moment to find out.
        warn_not_elevated(config_path)
        return 1

    return run(config_path, existing)


if __name__ == "__main__":
    sys.exit(main())

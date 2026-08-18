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

import base64
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

# One sentence, said in one place: the Test button and the check on the way off
# the camera page are answering the same question, and two wordings for one
# verdict read as two different faults.
UNREACHABLE = ("No usable image came back. Check the address, and the "
               "username and password.")

# The box the fetched frame is scaled into beside the Test button, and the
# share of the screen the full-size view may take. Small enough that a 4K
# snapshot still leaves the detail pane readable, large enough to tell a main
# stream from a sub-stream at a glance, which is the whole point of showing it.
THUMB = (208, 117)
SCREEN_SHARE = 0.85

# Wide enough for a UNC path and an explanation of what is wrong with it, and
# for a camera list beside the camera being edited; short enough to sit on a
# 1366x768 laptop, which a recorder often is, taskbar and title bar included.
#
# 660 rather than the 620 it was until the snapshot preview: with a frame on
# the camera pane that pane wanted 527 of the 533 it had, and six pixels is not
# a margin. What clips first is the Test and Save buttons, which is the worst
# thing on the page to lose, and it would clip on somebody else's font scaling
# rather than here. Measured (temp/pane_height.py), not eyeballed.
WINDOW = "820x660"

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
    """(label, address, username, password, stream) for a camera, or None.

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
        # Two shapes per template, because fill_template() emits two: an open
        # camera's URL carries no `user:password@` at all. Without the second
        # shape an open RTSP stream would read back as Custom URL, which is
        # the very defect identify_camera() was written to fix.
        shapes = [template]
        if "{user}:{password}@" in template:
            shapes.append(template.replace("{user}:{password}@", "", 1))
        # A camera switched to its largest stream carries a URL the template
        # cannot produce, so the selector is wound back to the template's own
        # before matching and reported separately. Without this, picking a
        # bigger profile would cost the camera its make.
        default = setup.stream_token(template)
        token = setup.stream_token(url) if default else ""
        probe = setup.with_stream(url, default) if token else url
        for shape in shapes:
            pattern = "".join(holes[part[1:-1]] if part[:1] == "{" else
                              re.escape(part)
                              for part in re.split(r"(\{[a-z]+\})", shape))
            found = re.match(pattern + "$", probe)
            if not found:
                continue
            got = found.groupdict()
            user = unquote(got.get("user", ""))
            password = unquote(got.get("password", ""))
            address = got["ip"]
            # The round trip, not the match: rebuilding has to land back on
            # the same string, or an operator who presses Save without
            # touching anything would silently get a different URL. Through
            # fill_template(), which is what Save will use, rather than a
            # second opinion about what a template produces.
            if setup.fill_template(template, setup.url_host(address),
                                   user, password) != probe:
                continue
            if address.startswith("[") and address.endswith("]"):
                address = address[1:-1]     # an IPv6 literal, as it was typed
            return (preset[0], address, user, password,
                    token if token != default else "")
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

    # Declared, never inferred. Blank credentials used to mean both "this
    # camera needs none" and "I have not filled these in yet", and one state
    # meaning two things is the shape this project keeps paying for. The box
    # is off unless it is ticked, so an unsecured camera is something the
    # operator said, not something they got by leaving a field empty.
    unsecured = bool(fields.get("no_credentials"))
    if unsecured:
        user = password = ""

    if template is None:
        method = str(fields.get("method") or "http")
        auth = str(fields.get("auth") or "none").lower()
        # A custom URL carries its own stream in the text the operator typed;
        # there is no template to wind it back to, so nothing is stored.
        stream = ""
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
        url = setup.fill_template(template, host, user, password)
        # Measured, not chosen: Test fetches every stream the template's
        # selector can name and keeps whichever carried the most pixels. Held
        # as a token rather than as the finished URL so that changing the
        # address or the password still rebuilds from the preset.
        # Kept only when applying it actually moved the URL, which settles
        # three cases at once: the make's own default, a token naming a knob
        # this template does not have, and a make with no selector at all. A
        # stored stream the URL does not carry would be a claim about the
        # picture that nothing fetches.
        stream = str(fields.get("stream") or "")
        moved = setup.with_stream(url, stream) if stream else url
        if moved != url:
            url = moved
        else:
            stream = ""

    # And the declaration has a functional consequence, which is the whole
    # reason it cannot be cosmetic: leaving `auth` at the preset's digest with
    # no credentials makes `requests` answer the 401 challenge with an empty
    # username, which is a real failed sign-in on a camera that wanted none.
    if unsecured:
        auth = "none"

    camera = dict(cam or {})
    camera.update({"name": name, "method": method, "url": url,
                   "enabled": bool(fields.get("enabled", True))})
    if unsecured:
        camera["no_credentials"] = True
    else:
        camera.pop("no_credentials", None)
    if stream:
        camera["stream"] = stream
    else:
        camera.pop("stream", None)
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
            "no_credentials": bool(cam.get("no_credentials")),
            # From the URL rather than from the stored key, so a config that
            # was hand-edited still shows the stream it is actually fetching.
            "stream": (known and known[4]) or "",
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
# Camera discovery
#
# The probe itself is setup.discover_cameras(), unchanged and unwrapped: one
# multicast WS-Discovery query, stdlib sockets, no credentials sent. What lives
# here is everything the window has to decide about what answered, and it is
# above the banner for the usual reason. "Is this one already configured" and
# "what does a ticked row become" are rules, and a rule the window owns is a
# rule nothing checks.
# ---------------------------------------------------------------------------

def camera_address(cam):
    """The host a configured camera points at, or "" if nothing parses.

    Read out of the URL, because there is no other place it could come from:
    an address is what the wizard asks for and a URL is what the config keeps.
    `wsd_host()` is this project's one answer to "the host of a string that may
    not be a URL", including the ValueError a malformed literal raises on 3.12+
    (one real Dahua advertises `http://[]/onvif/device_service`), so it is
    reused here rather than restated.
    """
    return setup.wsd_host(str((cam or {}).get("url") or ""))


def configured_addresses(cameras):
    """Every host already in the config, folded for comparison."""
    return {a.lower() for a in (camera_address(c) for c in cameras or []) if a}


def scan_type_choices():
    """The makes a scanned camera may be added as.

    Custom URL is deliberately not among them. It builds no URL from an
    address, so choosing it here could only produce a camera with no URL at
    all; turning one into a custom camera afterwards is what the detail pane is
    for.
    """
    return [preset[0] for preset in camera_types() if preset[3] is not None]


def scan_rows(found, cameras):
    """One row per discovered camera, in the order they should be listed.

    Only devices claiming to be video transmitters. An NVR, a doorbell, a
    printer and every Windows machine on the LAN answer this same probe, and
    offering one as a camera would be offering a URL that cannot work.

    `type` is `wsd_preset()`'s answer or "", and the empty string is
    load-bearing: it means the window has to ask, because a preselected make
    that is wrong is a wrong URL that looks deliberate. `added` is decided on
    the address alone, which is the one thing the operator can compare by eye.
    """
    taken = configured_addresses(cameras)
    rows = []
    for dev in found or []:
        if not dev.get("camera"):
            continue
        address = str(dev.get("address") or "")
        if not address:
            continue
        rows.append({"address": address,
                     # The model, not the ONVIF name: three Dahuas here all
                     # call themselves Dahua, and the model tells them apart.
                     "model": str(dev.get("hardware") or dev.get("name")
                                  or "(unnamed)"),
                     "type": setup.wsd_preset(dev),
                     "added": address.lower() in taken})
    return rows


def scan_summary(found, rows):
    """The line above the list: what answered, and what is not being offered."""
    if not rows:
        return ""
    others = len([d for d in found or [] if not d.get("camera")])
    parts = ["%d camera%s answered." % (len(rows),
                                        "" if len(rows) == 1 else "s")]
    if others:
        parts.append("%d other device%s also answered and %s not offered: an "
                     "NVR, a doorbell or a PC answers this too."
                     % (others, "" if others == 1 else "s",
                        "is" if others == 1 else "are"))
    return " ".join(parts)


def nothing_found_advice():
    """Why nothing answering is not the same as there being no cameras."""
    return ("Nothing answered on this network segment.\n\n"
            "That does not mean there are no cameras. Multicast does not "
            "cross subnets or VLANs, a dedicated camera VLAN is common in "
            "exactly the deployments with the most cameras, and many cameras "
            "ship with ONVIF switched off.\n\n"
            "Add them with Add, which always works.")


def next_camera_names(cameras, count):
    """`count` unused CameraN names, in order.

    Numbered rather than named after what each device calls itself, and that
    is a decision rather than laziness: a camera name here is a *place*, and
    what a camera reports is its model, so three Dahuas would all arrive
    called Dahua. Names already in the config are skipped, using the wizard's
    own case-insensitive rule rather than a second opinion about it.
    """
    names, used = [], list(cameras or [])
    number = 1
    while len(names) < max(0, int(count)):
        candidate = "Camera%d" % number
        if not setup.name_taken(used, candidate):
            names.append(candidate)
            used = used + [{"name": candidate}]
        number += 1
    return names


def camera_not_ready(cam):
    """Why this camera cannot work as it stands, or "". No network involved.

    The case that made this necessary is the one Scan network creates: cameras
    arrive with an address and a make and no password, enabled, and nothing
    stopped Finish being pressed right then. A blank credential is still a
    failed authentication attempt rather than a free one, so five scanned
    cameras is five cameras configured to fail.

    Static on purpose, and checked before anything is fetched. A make that
    signs in with a username and password while carrying neither is
    *unfinished*, which is a different answer from *unreachable* and needs no
    camera to establish. Only when **neither** is set: a username with no
    password is a deliberate answer this has no business overruling, and the
    live test settles it in one attempt.
    """
    cam = cam or {}
    if not str(cam.get("url", "") or "").strip():
        return "No address. Select it and give it one."
    if cam.get("no_credentials"):
        # The operator said so, and the live pull is the authority on whether
        # they were right. Refusing here would be the wizard overruling a
        # measurement with a policy, which is the try_rsync_args error: an
        # open RTSP stream and an unauthenticated snapshot endpoint are both
        # real, and one of them cannot even be secured on some hardware.
        return ""

    known = identify_camera(cam)
    template = preset_named(known[0])[3] if known else None
    # Two places credentials live, and both count: the fields for a make that
    # uses HTTP digest or basic, and the URL itself for the makes whose
    # template names them (Reolink, RTSP).
    wants = (str(cam.get("auth", "") or "").lower() in ("digest", "basic")
             or bool(template and "{user}" in template))
    if not wants:
        return ""
    values = camera_form_values(cam)
    if values["username"] or values["password"]:
        return ""
    return "No credentials entered. Please enter camera credentials."


def proof_key(cam):
    """What a successful test proves, so it need not be repeated.

    The address and the credentials together, because a digest camera's URL
    does not change when its password does. Change any of the three and the
    key changes with it, which is exactly when the answer stops being current.

    This is what keeps pressing Next cheap. Testing every enabled camera on
    every press would freeze the window for up to the fetch timeout times the
    camera count, and put one authentication attempt on every camera each
    time, which is the shape that locks accounts.
    """
    cam = cam or {}
    values = camera_form_values(cam)
    return (str(cam.get("url", "") or ""), values["username"],
            values["password"])


def snapshot_line(detail):
    """The one-line verdict for a frame that arrived.

    What the console wizard has always printed, assembled from the same
    finding rather than from a second reading of it. It matters more here than
    there: under pythonw there is no console at all, so a boolean was all the
    graphical wizard could ever say, and "Test successful" cannot tell an
    operator that their 4K camera answered with a 640x360 thumbnail.
    """
    parts = ["%.0f KB" % (detail.get("bytes", 0) / 1024.0)]
    if detail.get("size"):
        parts.append("%dx%d" % tuple(detail["size"]))
    parts.append("%.2fs" % detail.get("seconds", 0.0))
    return ", ".join(parts)


def stream_label(token):
    """"profile=2" as something to show an operator."""
    name, _, value = str(token or "").partition("=")
    return ("%s %s" % (name, value)).strip()


def pick_stream(measured):
    """(token, size) of the largest frame measured, or None.

    `measured` is [(token, size)] with the configured stream first. Ties go to
    whichever came first, which is what stops a camera whose profiles are all
    the same size being rewritten to a different spelling of itself on every
    press of Test.
    """
    best = None
    for token, size in measured or []:
        if not size:
            continue
        if best is None or size[0] * size[1] > best[1][0] * best[1][1]:
            best = (token, tuple(size))
    return best


def stream_report(chosen, current, measured):
    """What to say about the stream that was picked. (level, message).

    Silence when there was nothing to choose between, because a line about
    streams on every single test is noise that hides the one time it matters.

    `measured` is every stream that was fetched, the configured one included,
    so what it replaced can be named with its own size rather than as "was
    smaller". An operator deciding whether to keep the switch needs the number
    it is being compared against.
    """
    if not chosen or chosen[0] == current:
        return OK, ""
    was = dict(measured or []).get(current)
    return WARN, ("Switched to %s, %dx%d (%s was %s)." % (
        stream_label(chosen[0]), chosen[1][0], chosen[1][1],
        stream_label(current) or "the configured stream",
        ("%dx%d" % was) if was else "smaller"))


def fit_box(width, height, maxw, maxh):
    """`width` x `height` scaled to fit inside the box, never enlarged."""
    if width <= 0 or height <= 0:
        return maxw, maxh
    scale = min(1.0, float(maxw) / width, float(maxh) / height)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def disabled_report(failures, still_enabled):
    """The popup naming what was switched off, and why.

    Switched off rather than refused, which is the operator's call and a better
    one than either option offered: a camera being mounted, unplugged or behind
    a switch that is off is not a mistake, and blocking would hold a
    ten-minute configuration hostage to it. Disabling says so in the list, is
    one tick to undo, and means the daemon never contacts that camera at all,
    so it cannot collect failed sign-ins either.
    """
    lines = ["These cameras were switched off, because nothing would be "
             "captured from them as they are:", ""]
    lines += ["    %s: %s" % (name or "(unnamed)", reason)
              for name, reason in failures]
    lines.append("")
    if still_enabled:
        lines.append("Everything else is unchanged. Fix one, tick Enable "
                     "timelapse again, and press Test.")
    else:
        lines.append("That leaves nothing enabled, so nothing at all would be "
                     "captured. Fix at least one and press Test.")
    return "\n".join(lines)


def build_scanned(row, name, cameras):
    """(level, message, camera) for one ticked row.

    `build_camera()` again rather than an assembly of its own, so a camera that
    arrived by a scan is the same shape as one typed in by hand, down to the
    URL its template produces and the keys that are left absent.

    Credentials are empty on purpose. Nothing here has ever been told one, and
    the alternative considered was one shared pair applied to every camera in
    the scan; the operator chose per-camera, so a scan adds an address and a
    make and the detail pane is where a password gets typed.
    """
    return build_camera({"name": name, "preset": preset_named(row["type"]),
                         "address": row["address"], "enabled": True,
                         "username": "", "password": ""}, cameras)


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


def next_steps(cfg, capturing=True):
    """What to tell the operator once the config is written.

    No commands, on the path where everything worked. The first version ended
    by telling them to open an Administrator prompt and run `timelapse test`,
    from a window built so that they would not have to, which rather gave the
    game away. The checks are a button on the same dialog now.

    `capturing` is the exception, and it is why this takes an argument at all.
    The line "Capture starts on its own and keeps running" was printed
    unconditionally and was FALSE on every fresh Windows install: the service
    is registered delayed-auto-start and nothing started it, so it first ran at
    the next reboot while this dialog said it was already going. Reported from
    a real install. The wizard starts it now, and when that does not work this
    says so and names the one command that fixes it, because a remedy is worth
    a command where a routine next step is not.
    """
    if capturing:
        lines = ["Capture starts on its own and keeps running."]
    else:
        lines = ["Capture is registered but is NOT running yet.",
                 "It will start by itself after the next restart, or start it "
                 "now with:    " + setup.start_hint(setup.CAPTURE_UNIT)]
    lines.append("The first video appears after midnight, once a whole day "
                 "has been captured.")
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
            self.capturing = True
            # Which encoder this machine will actually use. Found by the
            # ffmpeg check on the first page, and carried rather than stored:
            # it is a property of the box, not a setting, and the nightly run
            # probes for it again anyway.
            self.codec = None
            # Cameras whose address and credentials have been proved to work,
            # by proof_key(). On the wizard rather than on the page, because
            # every page rebuild destroys the page, and a proof does not stop
            # being true because somebody pressed Back.
            self.proven = set()

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

        def tooltip(self, widget, text):
            """A hover label, since a greyed row has to be able to say why.

            tkinter ships none, and it is a dozen lines: a borderless Toplevel
            placed beside the widget on the way in and destroyed on the way
            out. Bound to the row rather than to the control inside it, because
            a disabled ttk widget is exactly the case this exists for and its
            event handling is the thing being worked around.
            """
            holder = {}

            def enter(_event=None):
                if holder.get("tip") or not text:
                    return
                tip = tk.Toplevel(widget)
                tip.wm_overrideredirect(True)
                tip.wm_geometry("+%d+%d" % (widget.winfo_rootx()
                                            + widget.winfo_width() + 10,
                                            widget.winfo_rooty()))
                ttk.Label(tip, text=text, background="#ffffe0", relief="solid",
                          borderwidth=1, padding=(6, 3)).pack()
                holder["tip"] = tip

            def leave(_event=None):
                tip = holder.pop("tip", None)
                if tip is not None:
                    tip.destroy()

            widget.bind("<Enter>", enter)
            widget.bind("<Leave>", leave)
            # Or a tip whose row goes away sits on top of everything for ever,
            # with no window left to take it back down.
            widget.bind("<Destroy>", leave)
            return widget

        def show_image(self, frame, title):
            """The fetched frame, as large as the screen will take.

            Scaled down to fit rather than shown 1:1 in a scrolling canvas: at
            this point the operator is asking "is that the right view", which
            is a question about the whole picture, and a 4K frame answered
            1:1 would need dragging around to see at all.

            Four ways out, because a picture filling the screen with no
            obvious exit is alarming: the button, the window's own close box,
            a click anywhere on the image, and Escape. The Escape binding is
            why the button takes focus.
            """
            wide, tall = fit_box(*(setup.jpeg_size(frame) or (0, 0)),
                                 maxw=int(self.winfo_screenwidth()
                                          * SCREEN_SHARE),
                                 maxh=int(self.winfo_screenheight()
                                          * SCREEN_SHARE))
            png = setup.render_png(self.cfg, frame, wide, tall)
            if not png:
                return messagebox.showwarning(
                    "Snapshot",
                    "ffmpeg could not turn that frame into something this "
                    "window can draw. The camera answered; only the picture "
                    "is missing.")
            box = tk.Toplevel(self)
            box.title(title)
            box.transient(self)
            image = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
            shown = ttk.Label(box, image=image, cursor="hand2")
            # Tk keeps no reference of its own, and a PhotoImage collected
            # while it is on screen leaves an empty label behind.
            shown.image = image
            shown.pack()
            close = ttk.Button(box, text="Close", command=box.destroy)
            close.pack(pady=6)
            shown.bind("<Button-1>", lambda _e: box.destroy())
            box.bind("<Escape>", lambda _e: box.destroy())
            box.protocol("WM_DELETE_WINDOW", box.destroy)
            # Size and position in one call. A geometry string carrying only a
            # position is a hint this window manager has been measured
            # ignoring, which is how an earlier screenshot run captured the
            # desktop instead of the window.
            box.update_idletasks()
            box.geometry("%dx%d+%d+%d" % (
                box.winfo_reqwidth(), box.winfo_reqheight(),
                max(0, (box.winfo_screenwidth() - box.winfo_reqwidth()) // 2),
                max(0, (box.winfo_screenheight() - box.winfo_reqheight()) // 2)))
            box.grab_set()
            close.focus_set()
            box.wait_window()

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

        def scan_dialog(self, cams):
            """Probe the network, add what is ticked, return what was added.

            The probe blocks for a few seconds, so the window opens *first* and
            says what it is doing. A wizard that freezes with no explanation is
            indistinguishable from one that has crashed, and this one runs
            under pythonw with no console to say otherwise.
            """
            box = tk.Toplevel(self)
            box.title("Scan network")
            box.transient(self)
            box.grab_set()
            box.minsize(600, 250)
            frame = ttk.Frame(box, padding=12)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="One multicast query, then a few seconds of "
                                  "listening. No username or password is sent, "
                                  "so this cannot lock a camera account.",
                      wraplength=560, justify="left").pack(anchor="w")
            heading = ttk.Label(frame, wraplength=560, justify="left",
                                text="Listening for %d seconds ..."
                                     % round(setup.WSD_WINDOW))
            heading.pack(anchor="w", pady=(8, 6))

            # A scrolling list, not a growing window: eight cameras is an
            # ordinary fleet, and a form running past the bottom edge with no
            # way to scroll was reported from the notifications page.
            area = ttk.Frame(frame)
            area.pack(fill="both", expand=True)
            canvas = tk.Canvas(area, height=210, highlightthickness=0,
                               borderwidth=0, background=box.cget("background"))
            scroll = ttk.Scrollbar(area, orient="vertical",
                                   command=canvas.yview)
            rows_frame = ttk.Frame(canvas)
            rows_frame.bind("<Configure>", lambda _e: canvas.configure(
                scrollregion=canvas.bbox("all")))
            canvas.create_window((0, 0), window=rows_frame, anchor="nw")
            canvas.configure(yscrollcommand=scroll.set)
            canvas.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")

            hint = ttk.Label(frame, text="", foreground="#555", wraplength=560,
                             justify="left")
            hint.pack(anchor="w", pady=(6, 0))

            buttons = ttk.Frame(frame)
            buttons.pack(fill="x", pady=(10, 0))
            ttk.Button(buttons, text="Cancel",
                       command=box.destroy).pack(side="right")
            adder = ttk.Button(buttons, text="Add")
            adder.pack(side="right", padx=(0, 8))
            adder.state(["disabled"])

            picks, added = [], []

            def tally(*_a):
                ticked = len([1 for on, _row, _var in picks if on.get()])
                adder.config(text="Add %d camera%s"
                                  % (ticked, "" if ticked == 1 else "s")
                             if ticked else "Add")
                adder.state(["!disabled"] if ticked else ["disabled"])

            def build_rows(rows):
                for row in rows:
                    line = ttk.Frame(rows_frame)
                    line.pack(fill="x", pady=2)
                    on = tk.BooleanVar(value=False)
                    tick = ttk.Checkbutton(line, variable=on)
                    tick.pack(side="left")
                    grey = {"foreground": "#8a8a8a"} if row["added"] else {}
                    ttk.Label(line, text=row["address"], width=18,
                              **grey).pack(side="left")
                    ttk.Label(line, text=row["model"][:20], width=21,
                              **grey).pack(side="left")

                    if row["added"]:
                        # Explicit greying rather than the disabled state: a
                        # ttk.Label greys on disable only if the theme says so,
                        # and this has to be visible on every theme.
                        tick.state(["disabled"])
                        ttk.Label(line, text="already added",
                                  **grey).pack(side="left", padx=(2, 0))
                        # On the row as well as in the tooltip: a hover message
                        # has to be found before it can be read, and greying
                        # alone says "not available" without saying why.
                        self.tooltip(line, "Already added. Select it in the "
                                           "camera list to change it.")
                        continue

                    chosen = tk.StringVar(value=row["type"])
                    ttk.Combobox(line, textvariable=chosen,
                                 values=scan_type_choices(), state="readonly",
                                 width=26).pack(side="left", padx=(2, 0))

                    def gate(*_a, chosen=chosen, tick=tick, on=on):
                        # Nothing may be ticked without a make. wsd_preset()
                        # names six of eight real cameras and returns "" for
                        # the rest, and adding one of those on a guess would
                        # write a URL that looks chosen rather than invented.
                        if chosen.get():
                            tick.state(["!disabled"])
                        else:
                            on.set(False)
                            tick.state(["disabled"])
                        tally()

                    chosen.trace_add("write", gate)
                    on.trace_add("write", tally)
                    picks.append((on, row, chosen))
                    gate()

                unknown = len([1 for _on, row, var in picks if not var.get()])
                if unknown:
                    hint.config(text="%d of these did not say what make it "
                                     "is. Choose one beside it before ticking "
                                     "it: a camera reports its model, and "
                                     "some models name no vendor at all."
                                     % unknown)

            def accept():
                wanted = [(row, var.get()) for on, row, var in picks
                          if on.get()]
                if not wanted:
                    return
                names = next_camera_names(cams, len(wanted))
                problems = []
                for (row, label), name in zip(wanted, names):
                    level, message, cam = build_scanned(dict(row, type=label),
                                                        name, cams)
                    if level == FAIL or cam is None:
                        problems.append("%s: %s" % (row["address"], message))
                        continue
                    cams.append(cam)
                    added.append(cam)
                box.destroy()
                if problems:
                    messagebox.showerror("Scan network", "\n".join(problems))

            adder.config(command=accept)

            def scan_now():
                self.config(cursor="watch")
                box.config(cursor="watch")
                self.update_idletasks()
                try:
                    found = setup.discover_cameras()
                except Exception as exc:            # noqa: BLE001
                    # Discovery is an offer. Anything at all going wrong here
                    # must leave the operator adding cameras by hand, never
                    # looking at a stack trace.
                    found, failure = [], "%s: %s" % (type(exc).__name__, exc)
                else:
                    failure = ""
                finally:
                    self.config(cursor="")
                    box.config(cursor="")

                rows = scan_rows(found, cams)
                if not rows:
                    box.destroy()
                    messagebox.showinfo(
                        "Scan network",
                        ("Discovery failed (%s).\n\n" % failure if failure
                         else "") + nothing_found_advice())
                    return
                heading.config(text=scan_summary(found, rows))
                build_rows(rows)
                # Sized to what answered, up to the cap: four cameras in a box
                # of empty space looks like something failed to load. The
                # geometry has to be cleared as well as the height set, or the
                # window keeps whatever size it was given while it was still
                # saying "listening".
                canvas.config(height=min(240, max(56, 30 * len(rows))))
                box.geometry("")
                tally()

            box.update_idletasks()
            box.after(50, scan_now)
            self.wait_window(box)
            return added

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
            state = {"cam": None, "loaded": {}, "frame": None, "thumb": None}

            panes = ttk.Frame(self.body)
            panes.pack(fill="both", expand=True)

            left = ttk.Frame(panes)
            left.pack(side="left", fill="y")
            # exportselection=False, or clicking into any Entry on the right
            # hands the X selection over and the list silently unhighlights
            # the camera being edited.
            # 12 rather than 14: the scan button below it has to fit without
            # pushing the pane past the bottom of a 1366x768 screen.
            listing = tk.Listbox(left, height=12, width=26,
                                 exportselection=False)
            listing.pack(fill="both", expand=True)
            scan_bar = ttk.Frame(left)
            scan_bar.pack(fill="x", pady=(6, 0))
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
            # Not a picker, and not typed: which stream carries the most pixels
            # is a measurement Test makes, and this is where it reports what it
            # settled on. Hidden while the camera is on its make's default,
            # because a row saying "profile 1" on every camera is furniture.
            stream, stream_text = tk.StringVar(), tk.StringVar()
            stream_row = add_row("Stream", ttk.Label(
                right, textvariable=stream_text, foreground="#555"))
            url, url_row = entry_row("Snapshot or stream URL")
            auth = tk.StringVar()
            auth_row = add_row("Authentication",
                               ttk.Combobox(right, textvariable=auth,
                                            values=["digest", "basic", "none"],
                                            state="readonly", width=14))
            user, user_row = entry_row("Username")
            password, pw_row = entry_row("Password", secret=True)

            # Off unless ticked, which is the point: an unsecured camera has
            # to be declared rather than arrived at by leaving two boxes
            # empty. Sits under the two boxes it switches off, so what it
            # applies to needs no explaining.
            unsecured = tk.BooleanVar(value=False)
            open_row = add_row("", ttk.Checkbutton(
                right, text="No credentials required", variable=unsecured))

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

            # The frame the camera last sent, directly above the button that
            # fetched it. A picture settles in one glance what no verdict can
            # say: that the lens is pointed somewhere useful, that it is in
            # focus, that this is the stream the operator meant. Empty and
            # hidden until a test has actually produced one.
            shot_bar = ttk.Frame(right)
            shot_bar.grid(row=place[0], column=0, columnspan=2, sticky="w",
                          pady=(6, 0))
            place[0] += 1
            thumb = ttk.Label(shot_bar, cursor="hand2")
            thumb.pack(side="left")
            shot_note = ttk.Label(shot_bar, text="", foreground="#555",
                                  wraplength=200, justify="left")
            shot_note.pack(side="left", padx=(8, 0))
            shot_bar.grid_remove()

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
                        "no_credentials": unsecured.get(),
                        "stream": stream.get(),
                        "interval": interval.get(),
                        "framerate": framerate.get(),
                        "smoothing_on": smooth_on.get(),
                        "smoothing": smoothing.get(), "enabled": enabled.get()}

            def refresh(*_a):
                preset = preset_named(kind.get())
                custom = preset_is_custom(preset)
                wants = preset_wants_credentials(preset)
                stream_text.set(stream_label(stream.get()))
                for widgets, wanted in ((address_row, not custom),
                                        (url_row, custom),
                                        (auth_row, custom),
                                        (stream_row, bool(stream.get())
                                         and not custom),
                                        (user_row, wants), (pw_row, wants),
                                        (open_row, wants)):
                    for widget in widgets:
                        widget.grid() if wanted else widget.grid_remove()
                # Emptied as well as disabled, so what is stored is what is
                # shown: a greyed-out password still reading admin would be a
                # credential the config does not have.
                for box in (user_row[1], pw_row[1]):
                    box.state(["disabled"] if unsecured.get() else
                              ["!disabled"])
                if unsecured.get():
                    user.set("")
                    password.set("")
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
                unsecured.set(values["no_credentials"])
                stream.set(values["stream"])
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
                show_shot(None, "")
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
                          "no_credentials": values["no_credentials"],
                          "stream": values["stream"],
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

            def show_shot(frame, caption):
                """Hold the frame Test fetched, and show it small.

                The PhotoImage is kept on `state` rather than only on the
                widget: Tk holds no reference of its own to an image, so one
                that goes out of scope here is garbage collected and the label
                renders empty, which looks exactly like a failed fetch.
                """
                state["frame"] = frame
                png = (setup.render_png(self.cfg, frame, *THUMB)
                       if frame else None)
                if not png:
                    state["thumb"] = None
                    thumb.config(image="")
                    shot_bar.grid_remove()
                    return
                state["thumb"] = tk.PhotoImage(
                    data=base64.b64encode(png).decode("ascii"))
                thumb.config(image=state["thumb"])
                shot_note.config(text=caption)
                shot_bar.grid()

            def fetch(built):
                """One frame, then the other streams worth measuring.

                The configured URL is fetched first and alone if it fails.
                Every candidate is another sign-in attempt, and a camera that
                has just refused one credential must not be handed two more:
                that is the shape which locked three of the operator's cameras
                for half an hour under another tool.
                """
                if built.get("method") == "rtsp":
                    return setup.grab_snapshot_rtsp(built, self.cfg), []
                data, detail = setup.grab_snapshot(built, self.cfg)
                if data is None:
                    return (data, detail), []
                others = []
                for token, other in setup.stream_candidates(built["url"]):
                    got, _ = setup.grab_snapshot(dict(built, url=other),
                                                 self.cfg)
                    others.append((token, setup.jpeg_size(got) if got else None,
                                   got))
                return (data, detail), others

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
                          "no_credentials": values["no_credentials"],
                          "stream": values["stream"],
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
                    (frame, detail), others = fetch(built)
                finally:
                    self.config(cursor="")
                if frame is None:
                    show_shot(None, "")
                    # Three answers, not two: nothing was fetched because
                    # requests is missing is not the camera's fault and must
                    # not be reported as the camera refusing.
                    return self.say(tested,
                                    WARN if detail["skipped"] else FAIL,
                                    detail["reason"] or UNREACHABLE)

                current = setup.stream_token(built["url"])
                measured = ([(current, detail["size"])] +
                            [(token, size) for token, size, _ in others])
                chosen = pick_stream(measured)
                level, news = stream_report(chosen, current, measured)
                if news:
                    for token, size, got in others:
                        if token == chosen[0]:
                            frame, detail = got, dict(detail, size=size,
                                                      bytes=len(got))
                    # Into the form, so the ordinary Save carries it. Setting
                    # the var re-runs refresh(), which is what makes the
                    # Stream row appear.
                    stream.set(chosen[0])
                    fields["stream"] = chosen[0]
                    _l, _m, built = build_camera(fields, cams, cam)

                # So Next need not ask this camera again. Keyed on the address
                # and credentials that were proved, not on the camera, so
                # changing any of them re-opens the question.
                self.proven.add(proof_key(built))
                show_shot(frame, (news + " " if news else "") +
                          "Click the picture to see it full size.")
                self.say(tested, level, "Test successful: %s"
                         % snapshot_line(detail))

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

            def scan():
                # leave_current() first, for the same reason Add calls it: the
                # scan can append several cameras and select one of them, and
                # a half-typed password on the camera showing now would go
                # with no warning at all.
                if not leave_current():
                    return
                fresh = self.scan_dialog(cams)
                if not fresh:
                    return
                redraw()
                show_camera(fresh[0])

            ttk.Button(scan_bar, text="Scan network",
                       command=scan).pack(fill="x")
            ttk.Button(list_buttons, text="Add", command=add,
                       width=11).pack(side="left")
            remover = ttk.Button(list_buttons, text="Remove", command=remove,
                                 width=11)
            remover.pack(side="right")
            ttk.Button(bar, text="Test", command=test).pack(side="left")
            tested.pack(side="left", padx=(8, 0))
            ttk.Button(bar, text="Save", command=save).pack(side="right")
            saved.pack(side="right", padx=(0, 8))

            thumb.bind("<Button-1>", lambda _e: state["frame"] and
                       self.show_image(state["frame"],
                                       str(name.get() or "Camera")))
            kind.trace_add("write", refresh)
            smooth_on.trace_add("write", refresh)
            unsecured.trace_add("write", refresh)
            stream.trace_add("write", refresh)
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

                # Every enabled camera has to be able to work before the page
                # is left. The static reasons cost nothing; the live test runs
                # only for a camera whose address or credentials have not
                # already been proved, which is what stops a second Next press
                # authenticating against the whole fleet again.
                failures = []
                self.config(cursor="watch")
                self.update_idletasks()
                try:
                    for cam in cams:
                        if not cam.get("enabled", True):
                            continue
                        reason = camera_not_ready(cam)
                        if not reason:
                            key = proof_key(cam)
                            if key in self.proven:
                                continue
                            # grab_snapshot rather than test_camera: the
                            # console version asks whether to retry a 401
                            # under the other auth scheme, and with no
                            # terminal behind this window ask() hands back the
                            # default, so a graphical run would answer yes to
                            # a question nobody was shown, silently.
                            got, detail = (
                                setup.grab_snapshot_rtsp(cam, self.cfg)
                                if cam.get("method") == "rtsp"
                                else setup.grab_snapshot(cam, self.cfg))
                            if got is not None or detail["skipped"]:
                                self.proven.add(key)
                                continue
                            reason = detail["reason"] or UNREACHABLE
                        cam["enabled"] = False
                        failures.append((cam.get("name"), reason))
                finally:
                    self.config(cursor="")

                if not failures:
                    return True
                left = [c for c in cams if c.get("enabled", True)]
                redraw()
                if state["cam"] is not None:
                    # The pane is showing one of these, and its Enable box
                    # still says otherwise until it is filled in again.
                    show_camera(state["cam"])
                messagebox.showwarning("Cameras",
                                       disabled_report(failures, bool(left)))
                return bool(left)

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
            # And started, which is the half Windows was missing entirely.
            # restart_units() touches only what is already running, correctly,
            # so on a fresh install nothing ever started the capture service.
            self.capturing = setup.start_capture()[0]
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
            ttk.Label(frame, text="\n".join(next_steps(self.cfg, self.capturing)),
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

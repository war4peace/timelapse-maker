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
                                is_unc, share_root)

__version__ = "0.1.9"

OK, WARN, FAIL = "ok", "warn", "fail"

# Wide enough for a UNC path and an explanation of what is wrong with it,
# narrow enough to sit on a 1366x768 laptop, which a recorder often is.
WINDOW = "760x560"


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


def check_notify_target(kind, value):
    """(level, message) for the one field each sink actually needs."""
    text = str(value or "").strip()
    if not text:
        return FAIL, "Leave this off, or fill it in.", ""
    if kind == "discord":
        if not text.startswith("https://"):
            return FAIL, "A Discord webhook URL starts with https://.", ""
        if "discord.com/api/webhooks/" not in text:
            return WARN, "That does not look like a webhook URL.", text
    elif kind == "ntfy" and not text.startswith(("http://", "https://")):
        return FAIL, "Give the full topic URL, such as https://ntfy.sh/mytopic.", ""
    elif kind == "telegram" and ":" not in text:
        return FAIL, "A bot token looks like 123456:ABC-DEF...", ""
    return OK, "", text


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


def summary_lines(cfg):
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
        ("Video", f"{enc.get('framerate', 0)} fps, "
                  f"{enc.get('container', 'mkv')}"),
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
    """What to tell the operator once the config is written."""
    lines = ["Capture starts on its own and runs continuously.",
             "The first video appears after midnight, once a day has finished."]
    t = cfg.get("transfer", {})
    if t.get("enabled"):
        lines.append("Finished videos are then moved to " + t["destination"])
    lines.append("Check everything with 'timelapse test' in an "
                 "Administrator prompt.")
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

            self.pages = [self.page_storage, self.page_tools, self.page_capture,
                          self.page_cameras, self.page_transfer,
                          self.page_notify, self.page_review]
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

        def field(self, parent, label, value="", width=58, secret=False):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text=label, width=18).pack(side="left")
            var = tk.StringVar(value=value)
            entry = ttk.Entry(row, textvariable=var, width=width,
                              show="*" if secret else "")
            entry.pack(side="left", fill="x", expand=True)
            return var, row

        def browse_into(self, var, directory=True):
            chosen = (filedialog.askdirectory() if directory
                      else filedialog.askopenfilename())
            if chosen:
                var.set(os.path.normpath(chosen))

        # -- pages -------------------------------------------------------

        def page_storage(self):
            self.heading(
                "Where should the frames go?",
                "Frames are written continuously and deleted once the day has "
                "been encoded, so this needs room for about one day at a time. "
                "A spinning disk is fine; the access pattern is sequential.")
            current = self.cfg["paths"].get("frames_root", "")
            base = str(Path(current).parent) if current else ""
            var, row = self.field(self.body, "Folder", base)
            ttk.Button(row, text="Browse",
                       command=lambda: self.browse_into(var)).pack(side="left",
                                                                   padx=6)
            note = self.status(self.body)
            paths = ttk.Label(self.body, text="", foreground="#555",
                              justify="left")
            paths.pack(anchor="w", pady=(10, 0))

            def refresh(*_a):
                level, message = check_storage(var.get())
                self.say(note, level, message)
                if var.get().strip():
                    p = storage_paths(var.get())
                    paths.config(text="frames  -> %s\nvideos  -> %s\nlogs    "
                                      "-> %s" % (p["frames_root"],
                                                 p["video_output"],
                                                 p["log_dir"]))
                else:
                    paths.config(text="")

            var.trace_add("write", refresh)
            refresh()

            def commit():
                level, message = check_storage(var.get())
                if level == FAIL:
                    messagebox.showerror("Storage", message)
                    return False
                self.cfg["paths"].update(storage_paths(var.get()))
                return True

            self.commit = commit

        def page_tools(self):
            self.heading(
                "Where is ffmpeg?",
                "This installer does not supply ffmpeg, because a recorder "
                "usually already has one its owner chose deliberately. Give "
                "the folder holding ffmpeg.exe and ffprobe.exe and both are "
                "taken from it.")
            var, row = self.field(self.body, "ffmpeg",
                                  self.cfg["paths"].get("ffmpeg", "")
                                  or setup.find_binary("ffmpeg", ""))
            ttk.Button(row, text="Browse",
                       command=lambda: self.browse_into(var)).pack(side="left",
                                                                   padx=6)
            note = self.status(self.body)
            detail = ttk.Label(self.body, text="", foreground="#555",
                               wraplength=680, justify="left")
            detail.pack(anchor="w", pady=(10, 0))
            link = ttk.Label(self.body, text=f"Builds for Windows: {FFMPEG_URL}",
                             foreground="#555")

            def test():
                self.config(cursor="watch")
                self.update_idletasks()
                try:
                    level, message, ffmpeg, ffprobe, _codec = \
                        check_ffmpeg(var.get())
                    self.say(note, level, message)
                    lines = encoder_notes(var.get())
                    if ffprobe:
                        lines.insert(0, f"ffprobe -> {ffprobe}")
                    detail.config(text="\n".join(lines))
                    link.pack(anchor="w", pady=(8, 0)) if level == FAIL \
                        else link.pack_forget()
                finally:
                    self.config(cursor="")

            ttk.Button(self.body, text="Test this ffmpeg",
                       command=test).pack(anchor="w", pady=(8, 0))

            def commit():
                level, message, ffmpeg, ffprobe, _codec = check_ffmpeg(var.get())
                if level == FAIL:
                    messagebox.showerror("ffmpeg", message)
                    return False
                self.cfg["paths"]["ffmpeg"] = ffmpeg
                if ffprobe:
                    self.cfg["paths"]["ffprobe"] = ffprobe
                return True

            self.commit = commit

        def page_capture(self):
            self.heading(
                "How often, and how fast?",
                "One frame every few seconds, played back at the frame rate "
                "below. A cadence slower than about fifteen minutes produces "
                "no video at all, so the wizard refuses it.")
            cap = self.cfg["capture"]
            enc = self.cfg["encode"]
            iv, _ = self.field(self.body, "Seconds between",
                               str(cap.get("interval_seconds", 5)), width=10)
            iv_note = self.status(self.body)
            fps, _ = self.field(self.body, "Frames per second",
                                str(enc.get("framerate", 60)), width=10)
            fps_note = self.status(self.body)

            def refresh(*_a):
                level, message, value = check_interval(iv.get())
                self.say(iv_note, level, message)
                level2, message2, _v = check_framerate(fps.get(), value)
                self.say(fps_note, level2, message2)

            iv.trace_add("write", refresh)
            fps.trace_add("write", refresh)
            refresh()

            def commit():
                level, message, interval = check_interval(iv.get())
                if level == FAIL:
                    messagebox.showerror("Cadence", message)
                    return False
                level, message, rate = check_framerate(fps.get(), interval)
                if level == FAIL:
                    messagebox.showerror("Frame rate", message)
                    return False
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
            self.heading("Which cameras?",
                         "A camera's name becomes the folder its frames live "
                         "in, so renaming one later means moving that folder.")
            cams = self.cfg.setdefault("cameras", [])

            listing = tk.Listbox(self.body, height=9)
            listing.pack(fill="both", expand=True, side="left")
            side = ttk.Frame(self.body)
            side.pack(side="left", fill="y", padx=(10, 0))

            def redraw():
                listing.delete(0, "end")
                for cam in cams:
                    mark = "" if cam.get("enabled", True) else "  (disabled)"
                    listing.insert("end", "%s   %s%s" % (
                        cam.get("name", "?"), cam.get("method", "http"), mark))

            def add():
                self.camera_dialog(cams, None, redraw)

            def edit():
                picked = listing.curselection()
                if picked:
                    self.camera_dialog(cams, cams[picked[0]], redraw)

            def remove():
                picked = listing.curselection()
                if not picked:
                    return
                cam = cams[picked[0]]
                if messagebox.askokcancel(
                        "Remove camera",
                        "Remove %s?\n\nFrames already captured are left where "
                        "they are; this only stops new ones." % cam["name"]):
                    cams.remove(cam)
                    redraw()

            for text, command in (("Add", add), ("Edit", edit),
                                  ("Remove", remove)):
                ttk.Button(side, text=text, command=command,
                           width=10).pack(pady=3)
            redraw()

            def commit():
                if not [c for c in cams if c.get("enabled", True)]:
                    messagebox.showerror(
                        "Cameras",
                        "Add at least one camera, or nothing will be captured.")
                    return False
                return True

            self.commit = commit

        def camera_dialog(self, cams, cam, redraw):
            box = tk.Toplevel(self)
            box.title("Camera")
            box.transient(self)
            box.grab_set()
            frame = ttk.Frame(box, padding=14)
            frame.pack(fill="both", expand=True)

            name, _ = self.field(frame, "Name", (cam or {}).get("name", ""))
            method = tk.StringVar(value=(cam or {}).get("method", "http"))
            row = ttk.Frame(frame)
            row.pack(fill="x", pady=4)
            ttk.Label(row, text="Type", width=18).pack(side="left")
            for label, value in (("Snapshot (HTTP)", "http"),
                                 ("Stream (RTSP)", "rtsp")):
                ttk.Radiobutton(row, text=label, value=value,
                                variable=method).pack(side="left", padx=(0, 10))
            url, _ = self.field(frame, "Address", (cam or {}).get("url", ""))
            note = self.status(frame)

            def test():
                level, message, address = check_camera_url(method.get(),
                                                           url.get())
                if level == FAIL:
                    return self.say(note, FAIL, message)
                box.config(cursor="watch")
                box.update_idletasks()
                try:
                    probe = dict(cam or {}, name=name.get() or "camera",
                                 method=method.get(), url=address)
                    good = (setup.test_camera_rtsp(probe, self.cfg)
                            if method.get() == "rtsp"
                            else setup.test_camera(probe, self.cfg))
                    self.say(note, OK if good else FAIL,
                             "The camera answered." if good else
                             "No usable image came back. Check the address "
                             "and the credentials in it.")
                finally:
                    box.config(cursor="")

            def save():
                level, message, cleaned = check_camera_name(
                    name.get(), cams, skip=cam)
                if level == FAIL:
                    return self.say(note, FAIL, message)
                level2, message2, address = check_camera_url(method.get(),
                                                             url.get())
                if level2 == FAIL:
                    return self.say(note, FAIL, message2)
                target = cam if cam is not None else {}
                target.update({"name": cleaned, "method": method.get(),
                               "url": address, "enabled": True})
                if cam is None:
                    cams.append(target)
                redraw()
                box.destroy()

            bar = ttk.Frame(frame)
            bar.pack(fill="x", pady=(12, 0))
            ttk.Button(bar, text="Test", command=test).pack(side="left")
            ttk.Button(bar, text="Cancel",
                       command=box.destroy).pack(side="right")
            ttk.Button(bar, text="Save", command=save).pack(side="right",
                                                            padx=(0, 8))

        def page_transfer(self):
            self.heading(
                "Send finished videos somewhere?",
                "A folder on this machine, or a network path such as "
                "\\\\tower\\videos. Leave this off to keep them locally.")
            t = self.cfg.setdefault("transfer", {})
            on = tk.BooleanVar(value=bool(t.get("enabled")))
            ttk.Checkbutton(self.body, text="Move finished videos",
                            variable=on).pack(anchor="w", pady=(0, 8))

            dest, row = self.field(self.body, "Destination",
                                   t.get("destination", ""))
            ttk.Button(row, text="Browse",
                       command=lambda: self.browse_into(dest)).pack(side="left",
                                                                    padx=6)
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
            tester = ttk.Button(self.body, text="Test the destination")
            tester.pack(anchor="w", pady=(8, 0))
            result = self.status(self.body)

            def refresh(*_a):
                level, message, stored = check_destination(dest.get())
                self.say(note, level, message if dest.get().strip() else "")
                needs = destination_needs_credentials(stored or dest.get())
                advice.config(text=credentials_advice(stored or dest.get())
                              if needs else "")
                if needs:
                    creds.pack(fill="x", pady=(6, 0), before=tester)
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
                    return self.say(result, FAIL, f"Could not write there: {why}")
                if is_unc(stored) and not user.get().strip():
                    return self.say(
                        result, WARN,
                        "Written as you, which does not prove the nightly "
                        "encode can. Give a username and password to be sure.")
                self.say(result, OK, "Wrote a file there, as the account the "
                                     "nightly encode will use.")

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
            self.heading("Tell you when a run finishes?",
                         "One summary per night, to any of these. This is how "
                         "you find out about a failure without looking.")
            kinds = (("discord", "Discord webhook URL", "url"),
                     ("ntfy", "ntfy topic URL", "url"),
                     ("telegram", "Telegram bot token", "token"))
            state = {}
            for kind, label, field in kinds:
                sink = setup.existing_sink(self.cfg, kind) or {}
                on = tk.BooleanVar(value=bool(sink.get("enabled")))
                ttk.Checkbutton(self.body, text=label.split(" ")[0],
                                variable=on).pack(anchor="w", pady=(6, 0))
                var, _row = self.field(self.body, label,
                                       sink.get(field, ""))
                state[kind] = (on, var, field, sink)
            note = self.status(self.body)

            def commit():
                for kind, (on, var, field, sink) in state.items():
                    if not on.get():
                        if sink:
                            sink["enabled"] = False
                            setup.put_sink(self.cfg, kind, sink)
                        continue
                    level, message, value = check_notify_target(kind, var.get())
                    if level == FAIL:
                        messagebox.showerror(kind.title(), message)
                        return False
                    entry = dict(sink, type=kind, enabled=True)
                    entry[field] = value
                    if kind == "telegram" and not entry.get("chat_id"):
                        messagebox.showerror(
                            "Telegram",
                            "Telegram also needs a chat id. Use 'timelapse "
                            "notify' in an Administrator prompt for that one.")
                        return False
                    setup.put_sink(self.cfg, kind, entry)
                return True

            self.say(note, OK, "All optional. Leave them off to be told "
                               "nothing.")
            self.commit = commit

        def page_review(self):
            self.heading("Ready to save",
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
            for rown, (label, value) in enumerate(summary_lines(self.cfg)):
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
            messagebox.showinfo("Saved",
                                "Setup is complete.\n\n" +
                                "\n".join(next_steps(self.cfg)))
            self.destroy()

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

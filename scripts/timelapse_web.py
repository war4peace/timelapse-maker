#!/usr/bin/env python3
"""
timelapse_web.py — read-only web UI.

Serves a small status page and, in later phases, an index of finished videos
from the transfer destination. It is strictly read-only: it never triggers an
encode, restarts a camera, edits the config, or writes anything at all. That
is a design constraint, not an accident - it is what lets the unit run with
ProtectSystem=strict and no ReadWritePaths, so the whole filesystem is
read-only to this process.

Playback is deliberately not done in the browser. The default output is AV1 in
Matroska, which browsers handle poorly; VLC, mpv and friends handle it
natively. Later phases serve the bytes over HTTP and hand off via a one-line
.m3u playlist.

Binds to 127.0.0.1 by default. http.server is not a hardened internet-facing
server and there is no TLS here - anything beyond loopback is an explicit
opt-in, and anything beyond the LAN belongs behind a reverse proxy.

Run under systemd. Logs to stdout (journald).

Phase 1a: server, config, page, /healthz, library-root resolution.
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

__version__ = "0.0.8"

log = logging.getLogger("web")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8787

# A request handler must never outlive the client that abandoned it. This
# belongs on the handler, not the server: ThreadingHTTPServer.timeout is only
# consulted by handle_request(), which serve_forever() never calls, so setting
# it there looks like a timeout and is not one. On the handler it reaches
# socket.settimeout() via StreamRequestHandler.setup().
SOCKET_TIMEOUT = 30


def load_config(path):
    """Duplicated from timelapse_capture rather than imported, for the same
    reason it is duplicated there: no daemon should be able to fail because a
    sibling changed. The distinct messages matter more than the duplication -
    journald showing "run timelapse setup" beats a traceback."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        sys.exit(f"No config at {path}. Run: sudo timelapse setup")
    except PermissionError:
        sys.exit(f"Cannot read {path} - it is 0640 root:timelapse and this "
                 f"process is not in that group.")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    except OSError as exc:
        sys.exit(f"Cannot read {path}: {exc}")


def setup_logging():
    """journald only. The other tools also write a rotating file, but this one
    writes nothing anywhere by design - a log file would be the single reason
    the unit needed a writable path."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


# ----------------------------------------------------------------------------
# Library root
# ----------------------------------------------------------------------------

def is_remote_spec(dest):
    """True for an rsync remote such as 'user@nas:/path' or 'rsync://host/mod'.

    These are not filesystem paths and cannot be listed without SSH/SFTP.

    An absolute path is settled first, before the colon test. That is what
    distinguishes '/mnt/odd:name/videos' from 'nas:videos' - and on Windows it
    is what stops a drive letter being read as a hostname, which is only a
    developer-machine concern (the tools run on Linux) but made the unit tests
    disagree with CI.
    """
    if dest.startswith("rsync://"):
        return True
    if os.path.isabs(dest):
        return False
    return ":" in dest.split("/", 1)[0]


def resolve_library(cfg):
    """Work out where finished videos actually live.

    The trap this exists for: transfer runs rsync with --remove-source-files
    and transfer.delete_local_after_transfer defaults to true, so after a
    successful night paths.video_output is EMPTY. Reading it would show an
    empty library on every correctly configured install.

    Returns a dict rather than a path because "why is it empty" is the question
    the page has to answer, and only this function knows.
    """
    web = cfg.get("web", {})
    trans = cfg.get("transfer", {})
    out = {"path": None, "source": "", "usable": False, "note": ""}

    override = (web.get("library_root") or "").strip()
    if override:
        out["path"], out["source"] = Path(override), "web.library_root"
    elif trans.get("enabled", False) and (trans.get("destination") or "").strip():
        dest = trans["destination"].strip()
        if is_remote_spec(dest):
            out["source"] = "transfer.destination (remote)"
            out["note"] = (f"Videos are transferred to {dest}, which is a remote "
                           f"rsync target, not a path this host can read. "
                           f"Browsing is not supported. Set web.library_root if "
                           f"the same files are reachable locally.")
            return out
        out["path"], out["source"] = Path(dest), "transfer.destination"
    else:
        out["path"] = Path(cfg["paths"]["video_output"])
        out["source"] = "paths.video_output (transfer disabled)"

    if not out["path"].is_dir():
        out["note"] = (f"{out['path']} does not exist or is not readable. "
                       f"If it is a NAS mount, it may simply not be mounted.")
        return out

    out["usable"] = True
    return out


# ----------------------------------------------------------------------------
# Service status and logs
# ----------------------------------------------------------------------------

# Run only when asked - a page load or a click. Nothing polls, nothing is
# collected in the background.

COMMAND_TIMEOUT = 10

STATUS_UNITS = ("timelapse-capture.service", "timelapse-encode.timer",
                "timelapse-encode.service", "timelapse-web.service")

# Request values pick a key; the *value* is what reaches the command line. No
# string from a request is ever interpolated into an argv, so there is no
# injection surface even in principle. Keep it that way.
LOG_UNITS = {
    "capture": "timelapse-capture",
    "encode": "timelapse-encode",
    "web": "timelapse-web",
}
LOG_LINES = {"200": "200", "1000": "1000"}
DEFAULT_LOG_UNIT = "capture"
DEFAULT_LOG_LINES = "200"

JOURNAL_DENIED = (
    "No entries. If the unit has been running, this process cannot read the "
    "journal rather than the journal being empty: journalctl shows nothing to "
    "a user outside the systemd-journal group, and says so no more loudly "
    "than this. Add SupplementaryGroups=systemd-journal to "
    "timelapse-web.service."
)


def run_command(argv):
    """Run a fixed argv. Returns (output, problem).

    `problem` is set only when the command could not be run at all. A non-zero
    exit is not a problem: `systemctl status` exits 3 for an inactive unit and
    4 for one that does not exist, and that output is precisely what the page
    is for. Treating those as failures would replace the answer with an error.

    Never shell=True, and argv is always built from constants.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=COMMAND_TIMEOUT)
    except FileNotFoundError:
        return "", (f"{argv[0]} is not installed here. The status pane needs "
                    f"systemd.")
    except subprocess.TimeoutExpired:
        return "", f"{argv[0]} did not answer within {COMMAND_TIMEOUT}s."
    except OSError as exc:
        return "", f"Could not run {argv[0]}: {exc}"

    # stderr matters as much as stdout: journalctl explains itself there.
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return out, ""


def status_report():
    """`systemctl status` for every unit this project installs.

    --lines=0 suppresses the journal excerpt systemctl normally appends. That
    excerpt needs journal access, so without it the output looks mysteriously
    truncated; the logs page asks for logs explicitly instead.
    """
    argv = ["systemctl", "status", "--no-pager", "--lines=0"] + list(STATUS_UNITS)
    out, problem = run_command(argv)
    return {"command": " ".join(argv), "output": out, "problem": problem,
            "hint": ""}


def journal_report(unit_key, lines_key):
    unit = LOG_UNITS.get(unit_key, LOG_UNITS[DEFAULT_LOG_UNIT])
    lines = LOG_LINES.get(lines_key, LOG_LINES[DEFAULT_LOG_LINES])
    argv = ["journalctl", "-u", unit, "-n", lines, "--no-pager"]
    out, problem = run_command(argv)

    # -f would never return and would hang the request until the client gave
    # up; the `timelapse logs` wrapper follows, and this deliberately does not.
    hint = ""
    if not problem and out.strip().lower().strip("- ") in ("no entries", ""):
        hint = JOURNAL_DENIED
    return {"command": " ".join(argv), "output": out, "problem": problem,
            "hint": hint}


# ----------------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------------

LAYOUT = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>timelapse-maker</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 2rem 1.25rem;
         max-width: 54rem; margin-inline: auto; }}
  h1 {{ font-size: 1.3rem; margin: 0; }}
  h2 {{ font-size: .95rem; text-transform: uppercase; letter-spacing: .06em;
        opacity: .6; margin: 0 0 .6rem; }}
  header {{ display: flex; align-items: baseline; gap: .75rem;
            border-bottom: 1px solid rgba(128,128,128,.3);
            padding-bottom: .9rem; margin-bottom: 1.5rem; }}
  .ver {{ opacity: .55; font-size: .85rem; }}
  section {{ border: 1px solid rgba(128,128,128,.3); border-radius: 8px;
             padding: 1rem 1.1rem; margin-bottom: 1rem; }}
  dl {{ display: grid; grid-template-columns: max-content 1fr;
        gap: .35rem .9rem; margin: 0; }}
  dt {{ opacity: .6; }}
  dd {{ margin: 0; overflow-wrap: anywhere; }}
  code {{ font-family: ui-monospace, monospace; font-size: .9em; }}
  .note {{ margin: .8rem 0 0; padding: .6rem .75rem; border-radius: 6px;
           background: rgba(200,140,0,.14); font-size: .9rem; }}
  .ok {{ color: #1a7f37; }} .bad {{ color: #b3261e; }}
  @media (prefers-color-scheme: dark) {{
    .ok {{ color: #4ac26b; }} .bad {{ color: #ff7b72; }}
  }}
  ul.todo {{ margin: 0; padding-left: 1.1rem; opacity: .65; font-size: .9rem; }}
  nav {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1.25rem; }}
  nav a {{ text-decoration: none; border: 1px solid rgba(128,128,128,.35);
           border-radius: 999px; padding: .25rem .8rem; font-size: .9rem;
           color: inherit; }}
  nav a.on {{ background: rgba(128,128,128,.18); font-weight: 600; }}
  pre {{ overflow-x: auto; background: rgba(128,128,128,.12); border-radius: 6px;
         padding: .8rem .9rem; font-size: .82rem; line-height: 1.45;
         margin: 0; }}
  .cmd {{ font-size: .8rem; opacity: .55; margin: 0 0 .5rem; }}
  .sub {{ display: flex; gap: .5rem; flex-wrap: wrap; margin: 0 0 .8rem;
          font-size: .85rem; }}
  .sub a {{ color: inherit; }}
</style>
<header>
  <h1>timelapse-maker</h1>
  <span class="ver">web {version}</span>
</header>
<nav>
  <a href="/" class="{on_home}">Overview</a>
  <a href="/status" class="{on_status}">Service status</a>
  <a href="/logs" class="{on_logs}">Recent log</a>
</nav>
{content}
"""

OVERVIEW = """<section>
  <h2>Video library</h2>
  <dl>
    <dt>Location</dt><dd><code>{lib_path}</code></dd>
    <dt>Resolved from</dt><dd>{lib_source}</dd>
    <dt>Readable</dt><dd class="{lib_class}">{lib_state}</dd>
  </dl>
  {lib_note}
</section>

<section>
  <h2>Not built yet</h2>
  <ul class="todo">
    <li>Video index &mdash; browse by camera and date</li>
    <li>Playback handoff &mdash; <code>.m3u</code> to VLC, plus a download link</li>
  </ul>
</section>
"""


class Handler(BaseHTTPRequestHandler):

    # Keep-alive needs an accurate Content-Length on every response, which the
    # helpers below always send.
    protocol_version = "HTTP/1.1"

    timeout = SOCKET_TIMEOUT

    # Default is "BaseHTTP/x.y Python/3.z", which advertises the interpreter
    # version to anything that connects. Nothing needs to know that.
    server_version = "timelapse-web"
    sys_version = ""

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # Nothing here is cacheable: the whole point is what is true right now.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        route = path.rstrip("/") or "/"
        args = parse_qs(query)

        if route == "/":
            self._send(200, self._render("home", self._overview()))
        elif route == "/status":
            self._send(200, self._render("status", self._report(status_report())))
        elif route == "/logs":
            self._send(200, self._render("logs", self._logs(args)))
        elif route == "/healthz":
            self._send(200, "ok\n", "text/plain; charset=utf-8")
        else:
            self._send(404, "not found\n", "text/plain; charset=utf-8")

    do_HEAD = do_GET

    def _render(self, page, content):
        return LAYOUT.format(
            version=__version__,
            on_home="on" if page == "home" else "",
            on_status="on" if page == "status" else "",
            on_logs="on" if page == "logs" else "",
            content=content,
        )

    def _overview(self):
        # Re-resolved per request rather than cached at startup: a NAS mount
        # comes and goes, and a page that reports a stale answer is worse than
        # no page. It is two stat() calls.
        lib = resolve_library(self.server.cfg)
        note = f'<p class="note">{escape(lib["note"])}</p>' if lib["note"] else ""
        return OVERVIEW.format(
            lib_path=escape(str(lib["path"]) if lib["path"] else "-"),
            lib_source=escape(lib["source"] or "-"),
            lib_class="ok" if lib["usable"] else "bad",
            lib_state="yes" if lib["usable"] else "no",
            lib_note=note,
        )

    def _logs(self, args):
        unit = (args.get("unit") or [DEFAULT_LOG_UNIT])[0]
        lines = (args.get("n") or [DEFAULT_LOG_LINES])[0]
        # Unknown values fall back rather than 400: these come from links, and
        # a stale bookmark should show the default log, not an error.
        if unit not in LOG_UNITS:
            unit = DEFAULT_LOG_UNIT
        if lines not in LOG_LINES:
            lines = DEFAULT_LOG_LINES

        picker = ['<p class="sub">']
        for key in LOG_UNITS:
            mark = "<strong>%s</strong>" % key if key == unit else key
            picker.append(f'<a href="/logs?unit={key}&amp;n={lines}">{mark}</a>')
        picker.append("&nbsp;|&nbsp;")
        for key in LOG_LINES:
            mark = "<strong>%s</strong>" % key if key == lines else key
            picker.append(f'<a href="/logs?unit={unit}&amp;n={key}">{mark}</a>')
        picker.append("</p>")

        return "".join(picker) + self._report(journal_report(unit, lines))

    @staticmethod
    def _report(rep):
        """One command's output in a <pre>, with whatever went wrong instead."""
        parts = [f'<section><p class="cmd"><code>{escape(rep["command"])}</code></p>']
        if rep["problem"]:
            parts.append(f'<p class="note">{escape(rep["problem"])}</p>')
        else:
            parts.append(f'<pre>{escape(rep["output"]) or "(no output)"}</pre>')
        if rep["hint"]:
            parts.append(f'<p class="note">{escape(rep["hint"])}</p>')
        parts.append("</section>")
        return "".join(parts)

    def log_message(self, fmt, *args):
        # Default writes to stderr, which journald tags as an error. These are
        # ordinary access lines.
        log.info("%s %s", self.address_string(), fmt % args)


def escape(text):
    """Minimal HTML escaping. Everything rendered so far comes from the config
    or the filesystem, not from the request - but a camera name or a path is
    still user-supplied text reaching a browser."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ----------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, cfg):
        super().__init__(addr, handler)
        self.cfg = cfg


def main():
    ap = argparse.ArgumentParser(
        prog="timelapse_web.py",
        description="Read-only web UI for timelapse-maker.")
    ap.add_argument("config", nargs="?", default="/etc/timelapse/config.json")
    ap.add_argument("--bind", help=f"address to listen on (default {DEFAULT_BIND})")
    ap.add_argument("--port", type=int, help=f"port (default {DEFAULT_PORT})")
    ap.add_argument("--force", action="store_true",
                    help="run even when web.enabled is false")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging()

    web = cfg.get("web", {})
    # .get() throughout: an upgrade keeps the existing config.json, so a key
    # read with [] would break every install that predates this feature.
    if not web.get("enabled", False) and not args.force:
        log.info("web.enabled is false in %s; nothing to serve.", args.config)
        return 0

    bind = args.bind or web.get("bind", DEFAULT_BIND)
    port = args.port or int(web.get("port", DEFAULT_PORT))

    if bind not in ("127.0.0.1", "::1", "localhost"):
        log.warning("Listening on %s - this server has no authentication and "
                    "no TLS. Put a reverse proxy in front of it for anything "
                    "beyond a trusted LAN.", bind)

    lib = resolve_library(cfg)
    log.info("Library: %s (from %s)%s",
             lib["path"] or "-", lib["source"], "" if lib["usable"] else " [UNUSABLE]")
    if lib["note"]:
        log.warning("%s", lib["note"])

    try:
        httpd = Server((bind, port), Handler, cfg)
    except OSError as exc:
        # Almost always "address already in use" or a bind address that does
        # not exist on this host. Both are config errors, not crashes.
        sys.exit(f"Cannot listen on {bind}:{port}: {exc}")

    def on_signal(signum, _frame):
        log.info("signal %s received, shutting down", signum)
        # shutdown() blocks until serve_forever returns, so it cannot be called
        # from the handler thread itself.
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    log.info("Serving on http://%s:%d/ (pid %d)", bind, port, os.getpid())
    httpd.serve_forever()
    httpd.server_close()
    log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

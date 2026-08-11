#!/usr/bin/env python3
"""
timelapse_update.py: ask GitHub what the latest version is, and install it.

Two jobs in one file, because they are the same knowledge. The lower half is
the release query (which tag is newest, what its notes say, why a failed
request failed); the upper half is the command an operator runs:

    timelapse update              check, show what is new, confirm, upgrade
    timelapse update --check      report only, change nothing, no root needed
    timelapse update --yes        no questions
    timelapse update --ref v0.1.0 install a specific tag or branch

The release query is imported by timelapse_web.py for the overview panel. It
lives here rather than there so the two callers cannot drift: comparing
versions as strings instead of tuples is a mistake worth making only once
(0.0.10 sorts below 0.0.9 lexically), and the same is true of GitHub's
mandatory User-Agent and of this repo's tags-without-Releases history.

Upgrading is re-running the installer, which is what the documentation has
always said. This command downloads that installer from the tag it is about to
install and runs it, so the installer and the tree it unpacks are the same
version. It keeps the config: install.sh asks "Reconfigure it?" and defaults
to no.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

__version__ = "0.1.4"

GITHUB_REPO = "war4peace/timelapse-maker"
GITHUB_API = "https://api.github.com/repos/" + GITHUB_REPO
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}"
CHANGELOG_URL = f"{RAW_BASE}/main/CHANGELOG.md"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"

# GitHub rejects a request with no User-Agent outright. Exactly the trap
# post_webhook() documents for Discord's Cloudflare, from a different vendor.
UPDATE_UA = f"timelapse-maker/{__version__} (+https://github.com/{GITHUB_REPO})"

UPDATE_TIMEOUT = 10
# A release body is somebody's prose and goes on a page; cap what is rendered.
NOTES_LIMIT = 4000

VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")

# socket.timeout is a deprecated alias for TimeoutError from 3.10, but on the
# 3.9 floor it is a separate OSError subclass, so neither name alone covers
# both. Named through getattr for the same reason the web server's DISCONNECTED
# tuple is: an alias that finally disappears must not take an import down.
TIMEOUTS = (TimeoutError, getattr(socket, "timeout", TimeoutError))

# The installer is small; anything much larger than this is not it.
INSTALLER_LIMIT = 512 * 1024
# Proof the download is the installer and not a 404 page, a login wall or a
# captive portal's idea of helpful. Checked before running it as root.
INSTALLER_MARKERS = ("timelapse-maker installer", "timelapse_capture.py")


def parse_version(text):
    """(major, minor, patch) or None. None sorts nothing and compares to
    nothing, which is what an unparseable tag deserves."""
    m = VERSION_RE.match((text or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def version_text(ver):
    return ".".join(str(n) for n in ver)


def fetch_json(url, timeout=UPDATE_TIMEOUT):
    req = urllib.request.Request(url, headers={
        "User-Agent": UPDATE_UA,
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_text(url, limit, timeout=UPDATE_TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UPDATE_UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode("utf-8", "replace")


def friendly_error(exc):
    """A failed update check, said in words an operator can act on.

    The raw text is kept on the end because it is the part worth searching
    for, but leading with "URLError: <urlopen error [Errno -3] Temporary
    failure in name resolution>" tells somebody nothing about whose fault it
    is. It is almost never this program's.
    """
    raw = str(exc) or type(exc).__name__
    low = raw.lower()
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403 and "rate limit" in low:
            lead = "GitHub is rate limiting this address"
        elif exc.code == 404:
            lead = "GitHub has no release or tag to report"
        else:
            lead = f"GitHub answered {exc.code}"
    elif "name resolution" in low or "getaddrinfo" in low or "name or service" in low:
        lead = ("DNS lookup failed, so this is your resolver rather than "
                "GitHub or this program")
    elif "timed out" in low or isinstance(exc, TIMEOUTS):
        lead = "The connection to GitHub timed out"
    elif "certificate" in low or "ssl" in low:
        lead = "The TLS connection to GitHub could not be verified"
    elif isinstance(exc, urllib.error.URLError):
        lead = "Could not reach GitHub"
    else:
        lead = "The update check failed"
    return f"{lead} ({raw})"


def clip_notes(text, limit=NOTES_LIMIT):
    """(notes, truncated). Release notes cut where a reader can tell.

    A hard slice at the limit stops mid-word, and the result reads as though
    the program lost the rest rather than declined to show it: v0.1.0's own
    4020-character body was cut three characters into a sentence. Cut at the
    last line break instead, so what survives is whole lines, and report that
    something was dropped so the caller can offer the full text elsewhere.

    Falls back to a word boundary, then to a blunt cut, for release notes
    written as one long paragraph. The half-limit floor is what stops a file
    whose only newline is near the start from being trimmed to nothing.
    """
    text = text or ""
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    for sep in ("\n", " "):
        cut = head.rfind(sep)
        if cut >= limit // 2:
            return head[:cut].rstrip(), True
    return head.rstrip(), True


def changelog_section(text, version):
    """The one release's entry out of a Keep a Changelog file.

    Used when the tag has no GitHub Release behind it, which was the case for
    this repo until 0.1.0: every version was a plain git tag, so
    /releases/latest 404d and there was no release body to show. The changelog
    is then the only place the "what's new" actually exists, and nine older
    tags still have no Release behind them.
    """
    want = f"## [{version}]"
    out, taking = [], False
    for line in (text or "").splitlines():
        if line.startswith("## "):
            if taking:
                break
            taking = line.startswith(want)
            continue
        if taking:
            out.append(line)
    return "\n".join(out).strip()


def latest_release():
    """(version, tag, url, notes). Raises on a network or parse failure.

    Tries Releases first, because a published release carries the notes with
    it. Falls back to tags: this repo has nine tags with no Release behind
    them, so an implementation that only knew about /releases/latest would
    report "up to date" forever, on its own project.
    """
    try:
        rel = fetch_json(GITHUB_API + "/releases/latest")
        tag = rel.get("tag_name") or ""
        if parse_version(tag):
            return (parse_version(tag), tag,
                    rel.get("html_url") or RELEASES_URL,
                    (rel.get("body") or "").strip())
    except urllib.error.HTTPError as exc:
        # 404 is the normal answer for a repo that tags without releasing.
        if exc.code != 404:
            raise

    best = None
    for item in fetch_json(GITHUB_API + "/tags"):
        tag = item.get("name") or ""
        ver = parse_version(tag)
        # Highest version, not first in the list: the API's order is its own
        # business and not documented as sorted.
        if ver and (best is None or ver > best[0]):
            best = (ver, tag)
    if best is None:
        raise ValueError("no version tags found")
    ver, tag = best
    return ver, tag, f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}", ""


def release_notes(ver, notes):
    """Notes for a version, falling back to the changelog when a tag has none."""
    if notes:
        return notes
    try:
        return changelog_section(fetch_text(CHANGELOG_URL, 256 * 1024),
                                 version_text(ver))
    except Exception as exc:                        # noqa: BLE001
        # Losing the notes is cosmetic; the version number is the answer.
        print(f"  (could not fetch the changelog: {exc})")
        return ""


# ----------------------------------------------------------------------------
# The command
# ----------------------------------------------------------------------------

if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
    B, DIM, R, G, Y, N = ("\033[1m", "\033[2m", "\033[31m", "\033[32m",
                          "\033[33m", "\033[0m")
else:
    B = DIM = R = G = Y = N = ""


def say(msg=""):
    print(f"  {msg}" if msg else "")


def note(msg):
    print(f"  {DIM}{msg}{N}")


def good(msg):
    print(f"  {G}OK{N}    {msg}")


def warn(msg):
    print(f"  {Y}WARN{N}  {msg}")


def fail(msg):
    print(f"  {R}FAIL{N}  {msg}", file=sys.stderr)


def ask_yes(question, default=True):
    """Yes/no from the terminal, never from stdin.

    Same reason the wizard and the installer do it: under `curl | bash` stdin
    is the script itself. Without a terminal, take the default rather than
    block forever.
    """
    prompt = "Y/n" if default else "y/N"
    try:
        tty = open("/dev/tty")
    except OSError:
        note(f"{question} ({prompt}) -> {'yes' if default else 'no'} "
             f"(no terminal)")
        return default
    try:
        print(f"  {question} ({prompt}): ", end="", flush=True)
        answer = tty.readline().strip().lower()
    except (OSError, KeyboardInterrupt):
        return default
    finally:
        tty.close()
    if not answer:
        return default
    return answer in ("y", "yes")


def fetch_installer(ref, dest_dir):
    """Download install.sh for `ref` into dest_dir and return its path.

    From the tag, not from main: the installer unpacks a tarball of the ref it
    is given, and an installer newer than that tree can expect files it does
    not contain. Downloaded into a directory of its own so the installer's
    obtain_source() does not mistake it for a checkout and skip the download.
    """
    url = f"{RAW_BASE}/{ref}/install.sh"
    note(f"Downloading {url}")
    text = fetch_text(url, INSTALLER_LIMIT, timeout=60)
    if not any(marker in text for marker in INSTALLER_MARKERS):
        raise ValueError(f"what came back from {url} is not the installer")
    path = os.path.join(dest_dir, "install.sh")
    # Written with LF endings whatever the platform: bash reads \r as part of
    # the command and fails in ways that read as a corrupt download.
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.chmod(path, 0o700)
    return path


def run_installer(path, ref, unattended):
    """Hand over to install.sh. Its exit status becomes ours."""
    cmd = ["bash", path, "--ref", ref]
    if unattended:
        cmd.append("--unattended")
    say()
    note(f"Running: {' '.join(cmd)}")
    try:
        # Not captured: the installer talks to the operator, asks questions on
        # /dev/tty and takes minutes. Swallowing that would leave a root
        # process apparently hung.
        return subprocess.call(cmd)
    except OSError as exc:
        fail(f"Could not run the installer: {exc}")
        return 1


def check_syntax(path):
    """`bash -n` before running a downloaded script as root. True if sane."""
    if not shutil.which("bash"):
        return True                 # Nothing to check with; the run will say.
    try:
        r = subprocess.run(["bash", "-n", path],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True
    if r.returncode == 0:
        return True
    fail("The downloaded installer is not valid bash, so it was not run.")
    note(r.stdout.decode("utf-8", "replace").strip()[:400])
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="timelapse update",
        description="Check for a new release and install it.",
        epilog="Exit status: 0 up to date or upgraded, 10 an update is "
               "available (--check only), 1 something failed.")
    ap.add_argument("--check", action="store_true",
                    help="report what is available and stop; needs no root")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="do not ask, and answer the installer's questions "
                         "with their defaults")
    ap.add_argument("--force", action="store_true",
                    help="reinstall even when already up to date")
    ap.add_argument("--ref", default=None, metavar="REF",
                    help="install this tag or branch instead of the latest "
                         "release (implies --force)")
    ap.add_argument("--current", default=__version__, metavar="VERSION",
                    help=argparse.SUPPRESS)   # tests, and manual rehearsals
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {__version__}")
    args = ap.parse_args(argv)

    say()
    say(f"{B}timelapse-maker update{N}")
    say()

    current = parse_version(args.current)
    say(f"Installed  {args.current}")

    # An explicit ref is the operator overriding the question, so do not ask
    # GitHub which version is newest: they have said which one they want.
    if args.ref:
        ref, newer, notes = args.ref, True, ""
        say(f"Requested  {ref}")
    else:
        try:
            ver, ref, url, body = latest_release()
        except Exception as exc:                    # noqa: BLE001
            fail(friendly_error(exc))
            note("Nothing was changed. Try again, or upgrade by hand:")
            note(f"  {RELEASES_URL}")
            return 1
        newer = bool(current and ver > current)
        say(f"Latest     {ref}   {DIM}{url}{N}")
        notes = release_notes(ver, body) if newer else ""

    say()
    if notes:
        text, clipped = clip_notes(notes)
        say(f"{B}What is new{N}")
        for line in text.splitlines():
            print(f"    {line}")
        if clipped:
            note(f"  (notes shortened; the rest is at {RELEASES_URL})")
        say()

    if not newer and not args.force:
        good("Already up to date.")
        say()
        return 0

    if args.check:
        # --check reports; it never installs. The distinct status is for a
        # cron job that wants to notify without a human reading this.
        warn(f"An update is available: {ref}")
        note("Install it with: sudo timelapse update")
        say()
        return 10

    if not newer:
        note(f"{args.current} is current; reinstalling anyway.")

    # Checked here rather than at the top so --check works unprivileged: an
    # operator should be able to ask the question without sudo.
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        fail("Installing needs root, because it writes /opt/timelapse.")
        note("Run: sudo timelapse update")
        say()
        return 1

    say("Upgrading re-runs the installer, which keeps your configuration,")
    say("your captured frames and your videos, and restarts the services.")
    if not args.yes and not ask_yes(f"Install {ref} now?", True):
        note("Nothing was changed.")
        say()
        return 0

    workdir = tempfile.mkdtemp(prefix="timelapse-update-")
    try:
        try:
            path = fetch_installer(ref, workdir)
        except Exception as exc:                    # noqa: BLE001
            fail(friendly_error(exc))
            note("Nothing was changed.")
            return 1
        if not check_syntax(path):
            return 1
        rc = run_installer(path, ref, args.yes)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    say()
    if rc == 0:
        good(f"Updated to {ref}.")
        note("Check it with: timelapse version")
    else:
        fail(f"The installer exited {rc}; this install may be half-upgraded.")
        note("Re-run 'sudo timelapse update' once the cause is fixed.")
    say()
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

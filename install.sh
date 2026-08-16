#!/usr/bin/env bash
#
# timelapse-maker installer.
#
#   curl -sL https://raw.githubusercontent.com/war4peace/timelapse-maker/main/install.sh -o install_timelapse.sh
#   sudo bash install_timelapse.sh
#   rm install_timelapse.sh
#
# Installs dependencies, the scripts, and the systemd units, then runs an
# interactive wizard that proposes where to store frames based on the disks it
# finds. Re-running is safe: it upgrades the scripts and keeps your config.
#
#   --unattended     no questions; sane defaults, does not enable services
#   --no-wizard      install files only, skip configuration
#   --uninstall      remove everything except captured data
#   --ref REF        install a specific branch or tag (default: main)
#   --prefix DIR     install location (default: /opt/timelapse)

set -euo pipefail

REPO="war4peace/timelapse-maker"
REF="${TIMELAPSE_REF:-main}"
PREFIX="${TIMELAPSE_PREFIX:-/opt/timelapse}"
CONFDIR="${TIMELAPSE_CONFDIR:-/etc/timelapse}"
SVCUSER="${TIMELAPSE_USER:-timelapse}"
UNITDIR="/etc/systemd/system"
CONFIG="$CONFDIR/config.json"

UNATTENDED=0
RUN_WIZARD=1
DO_UNINSTALL=0
WORKDIR=""

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'
    Y=$'\033[33m'; C=$'\033[36m'; N=$'\033[0m'
else
    B=""; DIM=""; R=""; G=""; Y=""; C=""; N=""
fi

say()   { printf '  %s\n' "$*"; }
note()  { printf '  %s%s%s\n' "$DIM" "$*" "$N"; }
ok()    { printf '  %sOK%s    %s\n' "$G" "$N" "$*"; }
warn()  { printf '  %sWARN%s  %s\n' "$Y" "$N" "$*"; }
err()   { printf '  %sFAIL%s  %s\n' "$R" "$N" "$*" >&2; }
step() {
    local pad="" i n=$(( 55 - ${#1} ))
    for (( i = 0; i < n; i++ )); do pad+="─"; done
    printf '\n%s── %s %s%s\n' "$C$B" "$1" "$pad" "$N"
}
die()   { err "$*"; exit 1; }

# Prompts must come from the terminal, not from stdin, so that the
# `curl ... | sudo bash` form still works.
ask_yn() {
    local q="$1" def="${2:-y}" ans prompt
    [ "$def" = "y" ] && prompt="Y/n" || prompt="y/N"
    if [ "$UNATTENDED" = "1" ] || [ ! -r /dev/tty ]; then
        [ "$def" = "y" ]
        return
    fi
    printf '  %s (%s): ' "$q" "$prompt" > /dev/tty
    read -r ans < /dev/tty || ans=""
    ans="$(printf '%s' "$ans" | tr '[:upper:]' '[:lower:]')"
    [ -z "$ans" ] && ans="$def"
    [ "$ans" = "y" ] || [ "$ans" = "yes" ]
}

# Must end on a success. Bash lets a non-zero status from the last command in an
# EXIT trap override the script's real exit status, so the bare `[ -n "$WORKDIR" ]
# && ...` form made every run that never downloaded a tarball - any install from
# a local checkout, and every --uninstall - exit 1 despite succeeding.
cleanup() {
    if [ -n "$WORKDIR" ] && [ -d "$WORKDIR" ]; then
        rm -rf "$WORKDIR"
    fi
    return 0
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

require_root() {
    [ "$(id -u)" = "0" ] || die "Run as root:  sudo bash $0"
}

check_platform() {
    [ "$(uname -s)" = "Linux" ] || die "This installer targets Linux (found $(uname -s))."
    if [ ! -d /run/systemd/system ]; then
        warn "systemd is not running. Files will be installed, but services"
        warn "cannot be enabled here."
    fi
}

detect_pkg() {
    for mgr in apt-get dnf yum pacman zypper apk; do
        if command -v "$mgr" >/dev/null 2>&1; then PKG="$mgr"; return; fi
    done
    PKG=""
}

install_deps() {
    step "Dependencies"
    detect_pkg
    if [ -z "$PKG" ]; then
        warn "No supported package manager found."
        note "Install manually: python3, python3-requests, ffmpeg, rsync"
        return
    fi
    note "Using $PKG"

    local pyreq ffm
    case "$PKG" in
        apt-get)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq || warn "apt-get update failed; continuing"
            pyreq="python3 python3-requests rsync curl"; ffm="ffmpeg" ;;
        dnf|yum)
            pyreq="python3 python3-requests rsync curl"; ffm="ffmpeg" ;;
        pacman)
            pacman -Sy --noconfirm >/dev/null 2>&1 || true
            pyreq="python python-requests rsync curl"; ffm="ffmpeg" ;;
        zypper)
            pyreq="python3 python3-requests rsync curl"; ffm="ffmpeg" ;;
        apk)
            pyreq="python3 py3-requests rsync curl"; ffm="ffmpeg" ;;
    esac

    pkg_install $pyreq || warn "Some base packages failed to install."

    if command -v ffmpeg >/dev/null 2>&1; then
        ok "ffmpeg already present: $(command -v ffmpeg)"
    elif pkg_install $ffm; then
        ok "Installed ffmpeg"
    else
        warn "Could not install ffmpeg automatically."
        note "On RHEL/Fedora it lives in RPM Fusion. You can also use a static"
        note "build from BtbN or jellyfin-ffmpeg; the wizard will ask for the path."
    fi

    command -v python3 >/dev/null 2>&1 || die "python3 is required but missing."
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
        || die "Python 3.8+ is required (found $(python3 -V 2>&1))."

    if ! python3 -c 'import requests' >/dev/null 2>&1; then
        warn "The 'requests' module is missing."
        if ask_yn "Install it with pip into the system environment?" y; then
            python3 -m pip install --break-system-packages requests 2>/dev/null \
                || python3 -m pip install requests \
                || warn "pip install failed; install python3-requests manually."
        fi
    fi
}

pkg_install() {
    case "$PKG" in
        apt-get) apt-get install -y -qq "$@" >/dev/null 2>&1 ;;
        dnf)     dnf install -y -q "$@" >/dev/null 2>&1 ;;
        yum)     yum install -y -q "$@" >/dev/null 2>&1 ;;
        pacman)  pacman -S --noconfirm --needed "$@" >/dev/null 2>&1 ;;
        zypper)  zypper --non-interactive install "$@" >/dev/null 2>&1 ;;
        apk)     apk add --quiet "$@" >/dev/null 2>&1 ;;
        *)       return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

# Use the checkout we are running from if there is one; otherwise fetch a
# tarball. This makes the same script work for `git clone && sudo ./install.sh`
# and for the piped one-liner.
obtain_source() {
    step "Source"
    local self_dir=""
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
        self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi

    if [ -n "$self_dir" ] && [ -f "$self_dir/scripts/timelapse_capture.py" ]; then
        SRC="$self_dir"
        ok "Using local checkout: $SRC"
        return
    fi

    command -v curl >/dev/null 2>&1 || die "curl is required to download the source."
    WORKDIR="$(mktemp -d)"
    local url="https://codeload.github.com/$REPO/tar.gz/$REF"
    note "Downloading $REPO @ $REF"
    curl -fsSL "$url" | tar xz -C "$WORKDIR" --strip-components=1 \
        || die "Download failed. Check the network, or that '$REF' exists."
    SRC="$WORKDIR"
    [ -f "$SRC/scripts/timelapse_capture.py" ] || die "Downloaded archive looks wrong."
    ok "Downloaded to $SRC"
}

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

create_user() {
    if id "$SVCUSER" >/dev/null 2>&1; then
        note "Service account '$SVCUSER' already exists"
        return
    fi
    if command -v useradd >/dev/null 2>&1; then
        useradd --system --no-create-home --shell /usr/sbin/nologin "$SVCUSER" \
            2>/dev/null \
            || useradd --system --no-create-home --shell /sbin/nologin "$SVCUSER" \
            || die "Could not create the '$SVCUSER' account."
    elif command -v adduser >/dev/null 2>&1; then
        adduser -S -D -H "$SVCUSER" || die "Could not create '$SVCUSER'."
    else
        die "No useradd/adduser available; create '$SVCUSER' manually."
    fi
    ok "Created service account '$SVCUSER'"
}

install_files() {
    step "Installing"
    create_user

    install -d -m 0755 "$PREFIX"
    # timelapse_platform.py is a library rather than an entry point, but it is
    # installed and versioned like the rest: every other script imports it, so
    # a stale copy left behind by a partial upgrade breaks a daemon exactly as
    # a stale script does, and that is the failure `timelapse version` exists
    # to catch.
    #
    # timelapse_cli.py is the Windows front door and is dead weight here, where
    # this wrapper does that job. timelapse_gui.py is the same: it refuses to
    # run off Windows and says which command to use instead. Both installed
    # anyway, so that the two platforms have one file set rather than two: a
    # version listing that differs by platform is a listing somebody has to
    # remember the exception to, and they are a few kilobytes of text.
    install -m 0755 "$SRC/scripts/timelapse_capture.py" \
                    "$SRC/scripts/timelapse_encode.py" \
                    "$SRC/scripts/timelapse_test.py" \
                    "$SRC/scripts/timelapse_setup.py" \
                    "$SRC/scripts/timelapse_update.py" \
                    "$SRC/scripts/timelapse_platform.py" \
                    "$SRC/scripts/timelapse_cli.py" \
                    "$SRC/scripts/timelapse_gui.py" \
                    "$SRC/scripts/timelapse_web.py" "$PREFIX/"
    ok "Scripts -> $PREFIX"

    install -d -m 0750 "$CONFDIR"
    chgrp "$SVCUSER" "$CONFDIR" 2>/dev/null || true
    install -m 0644 "$SRC/config/config.example.json" "$CONFDIR/config.example.json"

    # Convenience wrappers so the tools are on PATH.
    cat > /usr/local/bin/timelapse <<EOF
#!/usr/bin/env bash
# timelapse-maker command wrapper
#
# The help text below is inside a quoted heredoc so nothing in it expands at
# runtime, but this outer heredoc is unquoted, which is how the paths get
# baked in at install time. Anything meant to survive as a literal dollar
# sign therefore needs escaping here, the same as \$@ does above.
show_help() {
    cat <<'HELP'
timelapse - unattended daily timelapses from IP cameras

USAGE
  timelapse <command> [options]

Commands marked [sudo] read or write the configuration file, which is mode
0640 root:timelapse because it holds your camera passwords. Without sudo
they say so and stop; nothing half-runs. The path is under FILES below: it
is not inlined here because it moves with --prefix and would then wrap.

CONFIGURING
  setup        [sudo]  The full wizard: storage, capture interval, cameras,
                       transfer, Discord and the web UI. It rewrites the
                       whole config, backing up the old one first, so prefer
                       the targeted commands below for a single change.
  cameras      [sudo]  Add, edit, remove, enable or disable a camera, and
                       test one against the real hardware. With no options it
                       opens a menu. The shortcuts go straight to one camera,
                       by name or by the number 'timelapse cameras -l' shows:
                         -a         add a camera
                         -e:NAME    edit it        -t:NAME   test it
                         -x:NAME    enable/disable -r:NAME   remove it
                       Changes offer to restart capture afterwards, which is
                       what makes them take effect.
  transfer     [sudo]  Reconfigure where finished videos are sent, including
                       mounting an SMB/CIFS share and re-deriving the
                       ReadWritePaths= the systemd units need.
  notify       [sudo]  Where the nightly summary goes: a Discord webhook,
                       ntfy (ntfy.sh or your own server), Telegram, any
                       combination, or none. Offers a test message for each.
                       Needs no restart: the encode job reads the config when
                       it runs.
  web          [sudo]  Turn the read-only web UI on or off and set its
                       address, port and library path, including whether it
                       asks for a login. Offers to restart it.
  password     [sudo]  Set or change the web UI's login: a username, a
                       password, twice, and done. It never asks for the old
                       one - this already needs root, and root can read every
                       camera password in that same file, so the question
                       would prove nothing and would lock out the one person
                       entitled to fix a forgotten password. Only a hash is
                       stored, so there is nothing to recover. Restarts the
                       UI, which logs everybody out.
                         --disable    remove the login; the pages then open
                                      to anyone who can reach them
  config       [sudo]  Open the config in \$EDITOR for anything the wizards
                       do not cover, such as the encoder's container and
                       quality. A backup is taken first. You restart capture
                       yourself after.
                         --redacted   print the config with the passwords
                                      masked, to paste into a bug report
  restore      [sudo]  Put back an earlier config. One is kept automatically
                       before every change, five deep, and the listing says
                       when each was taken and what is in it. Restoring backs
                       up the current one too, so it is reversible.
                       'timelapse restore -l' just lists them.

CHECKING
  discover             List ONVIF cameras answering on this network, with
                       their address and model. Needs no root and sends no
                       credentials, so it cannot lock a camera account.
                       Finds nothing across a subnet or a VLAN boundary,
                       which is a property of multicast, not a fault.
  test         [sudo]  Pre-flight, and the thing to run after any change.
                       Fetches one snapshot per enabled camera and reports
                       resolution, size, latency and authentication; probes
                       the encoders; checks disk headroom, the transfer
                       destination and the Discord webhook.
  usage        [sudo]  Disk report: frames, bytes and date range per camera,
                       plus totals, videos and free space. Also names frame
                       folders that no enabled camera will ever encode, which
                       is how stranded frames get found.
  status               systemctl status for all four units at once: capture,
                       the encode timer and the encode service, and the web
                       UI. The encode service is listed separately from its
                       timer on purpose, because a failed run leaves the
                       service failed while the timer still looks healthy.
  logs                 Follow the capture journal live. Ctrl-C to stop.
  version              The installed version of each script, and a warning
                       if the running daemon started before they were
                       installed and is therefore still the previous build.

STAYING CURRENT
  update       [sudo]  Ask GitHub for the newest release, show what is new,
                       and install it after one confirmation. Re-runs the
                       installer, so it keeps your configuration, your frames
                       and your videos, and restarts the services.
                       'timelapse update --check' only reports, and is the
                       one command here that needs no root at all.

RUNNING BY HAND
  encode       [sudo]  Run the nightly encode now rather than at 00:05.
                       Useful for clearing a backlog.
  web-serve    [sudo]  Run the web UI in the foreground to watch its log.
                       The systemd service normally does this for you.

Anything after the command is passed through, so for example:
  timelapse test --probe-profiles   find which ONVIF profile is full resolution
  timelapse encode --dry-run        show what would encode, change nothing
  timelapse setup --defaults        accept every default without asking
  timelapse update --check          is there a new version? (no root needed)
  timelapse cameras -x:Doorbell     stop capturing that camera for now
  timelapse cameras --help          the options that command takes

FILES
  $CONFIG
      your configuration, mode 0640 root:timelapse
  $CONFDIR/config.example.json
      a commented template, replaced on every upgrade so you can diff it
  $PREFIX/
      the scripts themselves

Guide:  https://github.com/war4peace/timelapse-maker/blob/main/docs/install.md
HELP
}

case "\${1:-}" in
    test)      shift; exec python3 $PREFIX/timelapse_test.py "$CONFIG" "\$@" ;;
    encode)    shift; exec python3 $PREFIX/timelapse_encode.py "$CONFIG" "\$@" ;;
    setup)     shift; exec python3 $PREFIX/timelapse_setup.py --output "$CONFIG" \\
                          --template "$CONFDIR/config.example.json" \\
                          --owner "$SVCUSER" "\$@" ;;
    gui)       echo "  The graphical wizard is Windows only: there the console"
               echo "  wizard cannot be assumed to have a terminal in front of"
               echo "  it, and here it can."
               echo "  Run:  sudo timelapse setup"
               exit 2 ;;
    usage)     shift; exec python3 $PREFIX/timelapse_test.py "$CONFIG" --usage "\$@" ;;
    cameras)   shift; exec python3 $PREFIX/timelapse_setup.py --cameras-only \\
                          --output "$CONFIG" --owner "$SVCUSER" "\$@" ;;
    transfer)  shift; exec python3 $PREFIX/timelapse_setup.py --transfer-only \\
                          --output "$CONFIG" --owner "$SVCUSER" "\$@" ;;
    web)       shift; exec python3 $PREFIX/timelapse_setup.py --web-only \\
                          --output "$CONFIG" --owner "$SVCUSER" "\$@" ;;
    password)  shift; exec python3 $PREFIX/timelapse_setup.py --password-only \\
                          --output "$CONFIG" --owner "$SVCUSER" "\$@" ;;
    notify)    shift; exec python3 $PREFIX/timelapse_setup.py --notify-only \\
                          --output "$CONFIG" --owner "$SVCUSER" "\$@" ;;
    # No "$CONFIG" and no root: it neither reads nor writes the config, and
    # it presents no credentials to anything, so it cannot lock an account.
    discover)  shift; exec python3 $PREFIX/timelapse_setup.py --discover "\$@" ;;
    web-serve) shift; exec python3 $PREFIX/timelapse_web.py "$CONFIG" "\$@" ;;
    # No "$CONFIG": updating neither reads nor writes it, which is why
    # 'timelapse update --check' is the one configuring command that needs
    # no root at all.
    update)    shift; exec python3 $PREFIX/timelapse_update.py "\$@" ;;
    restore)   shift; exec python3 $PREFIX/timelapse_setup.py --restore-only \\
                          --output "$CONFIG" --owner "$SVCUSER" "\$@" ;;
    # The one write path that does not go through the wizard's write_config(),
    # so it takes its own backup first. Not fatal if that fails: refusing to
    # open an editor because a copy could not be made would be worse.
    config)
        shift
        # A dump for a bug report is not an edit: no backup, no editor, and it
        # stays pipeable, so it goes before any of that.
        if [ "\${1:-}" = "--redacted" ]; then
            exec python3 $PREFIX/timelapse_setup.py --redacted --output "$CONFIG"
        fi
        python3 $PREFIX/timelapse_setup.py --backup-now --output "$CONFIG" \\
            || echo "  (continuing without a backup)"
        # An editor writes more than the file you named it: a backup copy, a
        # swap file, an undo file. $CONFDIR is 0750 so anything landing *here*
        # is already shielded, but 'set backupdir' and 'set directory' send
        # them somewhere else entirely, and each one is a copy of your camera
        # passwords. 0600 whatever this creates, wherever it creates it.
        umask 0077
        \${EDITOR:-nano} "$CONFIG"
        rc=\$?
        # Not exec'd, because of these two lines. An editor that saves by
        # writing a new file and renaming it over the old one leaves root's
        # umask on the result: 0644 is unwanted, but losing the group is worse,
        # because the daemons then cannot read their own config and nothing
        # says so until the next restart.
        chgrp $SVCUSER "$CONFIG" 2>/dev/null || true
        chmod 0640 "$CONFIG" 2>/dev/null || true
        exit \$rc ;;
    logs)      exec journalctl -u timelapse-capture -f ;;
    # timelapse-encode.service is listed, not just its timer: it is oneshot,
    # so a run that failed (a broken transfer, say) leaves the *service* in a
    # failed state while the timer still looks perfectly healthy.
    status)    exec systemctl status --lines=5 \\
                    timelapse-capture.service \\
                    timelapse-encode.timer timelapse-encode.service \\
                    timelapse-web.service ;;
    version)
        for f in capture encode test setup update platform cli web; do
            printf '  %-8s %s\n' "\$f" \\
                "\$(sed -n 's/^__version__ = "\(.*\)"/\1/p' $PREFIX/timelapse_\$f.py)"
        done
        # The daemon runs the code it read at startup. If it predates the files
        # on disk, an upgrade was installed but never applied - the one failure
        # mode a version number alone cannot show you.
        if systemctl is-active --quiet timelapse-capture.service 2>/dev/null; then
            started=\$(date -d "\$(systemctl show timelapse-capture \\
                -p ExecMainStartTimestamp --value)" +%s 2>/dev/null || echo 0)
            mtime=\$(stat -c %Y $PREFIX/timelapse_capture.py 2>/dev/null || echo 0)
            if [ "\$started" -gt 0 ] && [ "\$mtime" -gt "\$started" ]; then
                echo
                echo "  WARNING: capture started before these files were installed,"
                echo "           so it is still running the previous build."
                echo "           sudo systemctl restart timelapse-capture.service"
            fi
        fi
        ;;
    -h|--help|help)
        show_help ;;
    "")
        # Same text as --help, but a bare invocation is a usage error, so the
        # exit status says so for anything scripting around this.
        show_help; exit 1 ;;
    *)
        printf 'timelapse: unknown command "%s"\\n\\n' "\$1" >&2
        show_help; exit 1 ;;
esac
EOF
    chmod 0755 /usr/local/bin/timelapse
    ok "Command wrapper -> /usr/local/bin/timelapse"
}

install_units() {
    for unit in timelapse-capture.service timelapse-encode.service \
                timelapse-encode.timer timelapse-web.service \
                timelapse-watch.service timelapse-watch.timer; do
        install -m 0644 "$SRC/service/$unit" "$UNITDIR/$unit"
    done
    sync_units
    ok "systemd units -> $UNITDIR"
}

# The units run with ProtectSystem=strict, so ReadWritePaths must name every
# directory the daemons write to. Deriving it from the config is the whole
# reason this is scripted: getting it wrong produces a baffling read-only error.
sync_units() {
    local user_line="$SVCUSER" rw="/var/lib/timelapse"
    if [ -f "$CONFIG" ]; then
        rw="$(python3 "$PREFIX/timelapse_setup.py" --print-paths "$CONFIG" 2>/dev/null)" \
            || rw="/var/lib/timelapse"
        [ -z "$rw" ] && rw="/var/lib/timelapse"
    fi
    # The web UI gets its OWN ReadWritePaths, not this one. It writes exactly
    # one thing - its sqlite index - and scoping the unit to that directory is
    # what keeps the library, the frames and the config read-only to it. Giving
    # it $rw would hand it write access to every captured frame for no reason.
    local webrw="/var/lib/timelapse/web"
    if [ -f "$CONFIG" ]; then
        webrw="$(python3 "$PREFIX/timelapse_setup.py" --print-web-paths "$CONFIG" 2>/dev/null)" \
            || webrw="/var/lib/timelapse/web"
        [ -z "$webrw" ] && webrw="/var/lib/timelapse/web"
    fi
    # Where the daemons publish runtime state, and now part of $rw above. It is
    # made HERE rather than only by the wizard, because an upgrade that answers
    # "don't reconfigure" never runs the wizard: without this, ReadWritePaths
    # would name a directory that does not exist and BOTH daemons would stop
    # starting, reporting a mount namespace error that names neither the
    # directory nor the release that added it.
    local staterw="/var/lib/timelapse/state"
    if [ -f "$CONFIG" ]; then
        staterw="$(python3 "$PREFIX/timelapse_setup.py" --print-state-path "$CONFIG" 2>/dev/null)" \
            || staterw="/var/lib/timelapse/state"
        [ -z "$staterw" ] && staterw="/var/lib/timelapse/state"
    fi
    install -d -m 0750 "$staterw" 2>/dev/null || true
    chown "$SVCUSER:$SVCUSER" "$staterw" 2>/dev/null || true

    for unit in timelapse-capture.service timelapse-encode.service; do
        local f="$UNITDIR/$unit"
        [ -f "$f" ] || continue
        sed -i \
            -e "s|^User=.*|User=$user_line|" \
            -e "s|^Group=.*|Group=$user_line|" \
            -e "s|^ReadWritePaths=.*|ReadWritePaths=$rw|" \
            -e "s|^ExecStart=.*timelapse_capture.py.*|ExecStart=/usr/bin/python3 $PREFIX/timelapse_capture.py $CONFIG|" \
            -e "s|^ExecStart=.*timelapse_encode.py.*|ExecStart=/usr/bin/python3 $PREFIX/timelapse_encode.py $CONFIG|" \
            "$f"
    done

    if [ -f "$UNITDIR/timelapse-web.service" ]; then
        sed -i \
            -e "s|^User=.*|User=$user_line|" \
            -e "s|^Group=.*|Group=$user_line|" \
            -e "s|^ReadWritePaths=.*|ReadWritePaths=$webrw|" \
            -e "s|^ExecStart=.*timelapse_web.py.*|ExecStart=/usr/bin/python3 $PREFIX/timelapse_web.py $CONFIG|" \
            "$UNITDIR/timelapse-web.service"
        # ReadWritePaths on a directory that does not exist stops the unit
        # dead, and the service cannot create it: its parent is read-only to
        # it by then. So it is made here, before anything tries to start.
        install -d -m 0750 "$webrw" 2>/dev/null || true
        chown "$SVCUSER:$SVCUSER" "$webrw" 2>/dev/null || true
    fi

    # The credential watch gets the state directory and nothing else, for the
    # same reason the web UI gets its index directory and nothing else: it
    # reads the capture heartbeat and writes one small file recording what it
    # has already reported. It has no business near the frames or the videos.
    #
    # Its ExecStart is rewritten separately too, and must be: the loop above
    # matches any line mentioning timelapse_encode.py, which would silently
    # strip the --watch flag and turn this timer into an encode run every five
    # minutes.
    if [ -f "$UNITDIR/timelapse-watch.service" ]; then
        sed -i \
            -e "s|^User=.*|User=$user_line|" \
            -e "s|^Group=.*|Group=$user_line|" \
            -e "s|^ReadWritePaths=.*|ReadWritePaths=$staterw|" \
            -e "s|^ExecStart=.*|ExecStart=/usr/bin/python3 $PREFIX/timelapse_encode.py --watch $CONFIG|" \
            "$UNITDIR/timelapse-watch.service"
    fi

    # SupplementaryGroups naming a group that does not exist stops the unit
    # from starting outright, so it is cheaper to drop the line than to have
    # the web UI fail to boot on a distro we have never tried. The cost of
    # dropping it is only that the log pane comes back empty - which the page
    # explains, rather than leaving the reader guessing.
    if [ -f "$UNITDIR/timelapse-web.service" ] \
       && ! getent group systemd-journal >/dev/null 2>&1; then
        sed -i '/^SupplementaryGroups=systemd-journal$/d' \
            "$UNITDIR/timelapse-web.service"
        note "No systemd-journal group here; the web log pane will be empty."
    fi

    [ -d /run/systemd/system ] && systemctl daemon-reload || true
    note "ReadWritePaths=$rw"
    note "Web index dir=$webrw"
}

run_wizard() {
    step "Configuration"
    # An upgrade never reconfigures. It used to ask, and the answer was "no"
    # essentially every time: reconfiguring is a separate job with its own
    # commands, and walking the whole wizard is a strange thing to be offered
    # by something you ran to get a bug fix. New keys arrive with defaults, so
    # an untouched config keeps working.
    if [ -f "$CONFIG" ]; then
        note "Keeping the existing configuration at $CONFIG"
        note "To change it:  timelapse setup   (or: timelapse config)"
        sync_units
        return
    fi
    local wizard_args=(--output "$CONFIG"
                       --template "$CONFDIR/config.example.json"
                       --owner "$SVCUSER")
    [ "$UNATTENDED" = "1" ] && wizard_args+=(--defaults)
    python3 "$PREFIX/timelapse_setup.py" "${wizard_args[@]}" \
        || die "Setup wizard did not complete."
    chown "root:$SVCUSER" "$CONFIG" 2>/dev/null || true
    chmod 0640 "$CONFIG" 2>/dev/null || true
    sync_units
}

# ---------------------------------------------------------------------------
# Service state across an upgrade
#
# An upgrade must not change what is running. This used to be settled by asking
# four questions (reconfigure, run the pre-flight, enable capture, enable the
# web UI) and every one of those answers was already knowable from the system
# itself: whatever was enabled stays enabled, whatever was running stays
# running, and whatever was off stays off. Asking also made `timelapse update`
# interactive at exactly the moment an operator wants it to be boring.
#
# Captured BEFORE anything is written, because install_units() and sync_units()
# both rewrite the files this reads.
# ---------------------------------------------------------------------------

MANAGED_UNITS=(timelapse-capture.service timelapse-encode.timer
               timelapse-watch.timer timelapse-web.service)

IS_UPGRADE=0
UNITS_ENABLED_BEFORE=" "
UNITS_ACTIVE_BEFORE=" "
UNITS_PRESENT_BEFORE=" "

snapshot_services() {
    # An existing config is what makes this an upgrade rather than a first
    # install: it is the same signal the wizard has always used, and it is true
    # before any of our own files are written.
    if [ -f "$CONFIG" ]; then
        IS_UPGRADE=1
    fi
    [ -d /run/systemd/system ] || return 0

    # `if` blocks throughout, never `test && VAR+=...`: under `set -e` an
    # AND-list whose first command fails takes the list's status with it, so a
    # single stopped unit would abort the installer here.
    local unit
    for unit in "${MANAGED_UNITS[@]}"; do
        if [ -f "$UNITDIR/$unit" ]; then
            UNITS_PRESENT_BEFORE="$UNITS_PRESENT_BEFORE$unit "
        fi
        if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
            UNITS_ENABLED_BEFORE="$UNITS_ENABLED_BEFORE$unit "
        fi
        if systemctl is-active --quiet "$unit" 2>/dev/null; then
            UNITS_ACTIVE_BEFORE="$UNITS_ACTIVE_BEFORE$unit "
        fi
    done
    return 0
}

# Substring match on a space-delimited list. Associative arrays would read
# better and would need bash 4, which is one more thing to be wrong about on a
# distro nobody here has tried.
was_in() {
    case "$1" in
        *" $2 "*) return 0 ;;
    esac
    return 1
}

# A daemon keeps executing the code it read at startup, so replacing the files
# on disk upgrades nothing until the unit restarts - and `systemctl enable
# --now` is a no-op on an already-active unit. Without this the installer
# replaced the scripts, printed "Capture is running", and left the *old* build
# serving. Verified in WSL: identical MainPID and ExecMainStartTimestamp across
# an upgrade of a live install.
#
# Every long-running unit must be listed here. A unit that is missing gets
# replaced on disk and keeps serving the old build, with the installer
# reporting success - which is exactly the bug this function exists to fix.
# The encoder is deliberately absent: it is oneshot, so an encode in flight
# finishes on the code it started with and the next trigger picks up the new.
RESTART_UNITS=(timelapse-capture.service timelapse-web.service)

restore_services() {
    [ -d /run/systemd/system ] || return 0
    step "Services"

    local unit

    # Restart what was running, so it executes the build just installed. Not
    # offered as a choice: the operator asked for this version, and declining
    # leaves the old one serving while every version number says otherwise. It
    # costs the frames due during the restart, a second or two.
    for unit in "${RESTART_UNITS[@]}"; do
        if was_in "$UNITS_ACTIVE_BEFORE" "$unit"; then
            systemctl restart "$unit" >/dev/null 2>&1 || true
        fi
    done
    sleep 2

    for unit in "${MANAGED_UNITS[@]}"; do
        [ -f "$UNITDIR/$unit" ] || continue

        if was_in "$UNITS_ENABLED_BEFORE" "$unit"; then
            if ! systemctl is-enabled --quiet "$unit" 2>/dev/null; then
                systemctl enable "$unit" >/dev/null 2>&1 || true
            fi
        fi
        if was_in "$UNITS_ACTIVE_BEFORE" "$unit"; then
            if ! systemctl is-active --quiet "$unit" 2>/dev/null; then
                systemctl start "$unit" >/dev/null 2>&1 || true
            fi
        fi

        # A unit this release introduced has no earlier state to restore, so it
        # follows the deployment as a whole: a live install adopts it, a
        # files-only one does not. Timers only. A *service* that was never
        # present must not be switched on by an upgrade, and the web UI in
        # particular is opt-in through the config rather than through here.
        case "$unit" in
            *.timer)
                if ! was_in "$UNITS_PRESENT_BEFORE" "$unit" \
                   && was_in "$UNITS_ENABLED_BEFORE" "timelapse-capture.service"; then
                    systemctl enable --now "$unit" >/dev/null 2>&1 || true
                fi ;;
        esac

        report_unit "$unit"
    done
    return 0
}

# One line per unit: what it is doing now, and whether that is what it was
# doing before. The second half is the point. An upgrade that quietly leaves
# capture stopped is the worst outcome this script has, and it used to be
# reported as success.
report_unit() {
    local unit="$1" enabled active
    # `systemctl is-enabled` PRINTS the state and exits non-zero for anything
    # that is not enabled, so the obvious `|| echo disabled` appends a second
    # word and the line comes out as "disabled\ndisabled". Take the output when
    # there is any, and only substitute when there is none (an unknown unit).
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null)" || true
    active="$(systemctl is-active "$unit" 2>/dev/null)" || true
    [ -n "$enabled" ] || enabled="disabled"
    [ -n "$active" ] || active="inactive"

    case "$active" in
        active)   active="running" ;;
        inactive) active="stopped" ;;
    esac
    # A timer sitting between firings is idle, not broken, and a oneshot's
    # timer is the thing to look at anyway.
    case "$unit" in
        *.timer) [ "$active" = "running" ] && active="waiting" ;;
    esac

    if was_in "$UNITS_ACTIVE_BEFORE" "$unit" \
       && ! systemctl is-active --quiet "$unit" 2>/dev/null; then
        err "$(printf '%-30s %s, %s - it was running before this upgrade' \
                "$unit" "$enabled" "$active")"
        note "See: journalctl -u ${unit%.*} -n 40"
        return 0
    fi
    if [ "$enabled" = "enabled" ]; then
        ok "$(printf '%-30s %s, %s' "$unit" "$enabled" "$active")"
    else
        # Six spaces so the unit names line up under the "OK" rows: ok() emits
        # a six-character status column that note() does not have.
        note "$(printf '%6s%-30s %s, %s' "" "$unit" "$enabled" "$active")"
    fi
    return 0
}

offer_enable() {
    # On an upgrade the services have already been put back exactly as they
    # were found, so there is nothing to offer: the pre-flight, enabling
    # capture and enabling the web UI are all decisions that were made once,
    # already, and re-asking them every release is how a two-minute update
    # turns into a five-question interview.
    if [ "$IS_UPGRADE" = "1" ]; then
        step "Next steps"
        say "Nothing to do; the services are as you left them."
        say "Check the cameras any time with:  timelapse test"
        printf '\n'
        say "${B}timelapse${N} status | logs | test | usage | encode | config | cameras | transfer | web | update"
        return
    fi

    step "Next steps"
    if [ ! -f "$CONFIG" ]; then
        say "1. Configure:   timelapse setup"
        say "2. Verify:      timelapse test"
        say "3. Enable:      systemctl enable --now timelapse-capture.service"
        say "                systemctl enable --now timelapse-encode.timer"
        say "                systemctl enable --now timelapse-watch.timer"
        return
    fi

    local cams
    cams="$(python3 -c "import json;print(len(json.load(open('$CONFIG'))['cameras']))" 2>/dev/null || echo 0)"
    if [ "$cams" = "0" ]; then
        warn "No cameras configured yet."
        say "Add them with:  timelapse config     (then: timelapse test)"
        return
    fi

    if [ ! -d /run/systemd/system ]; then
        say "systemd is not running here; enable the services on the real host."
        return
    fi

    if ask_yn "Run the pre-flight check now?" y; then
        # Run as the service account, so permission problems surface here
        # rather than at 00:05 tonight.
        as_service_user python3 "$PREFIX/timelapse_test.py" "$CONFIG" \
            || warn "Pre-flight reported problems - fix them before enabling."
    fi

    if ask_yn "Enable capture and the nightly encode now?" y; then
        systemctl enable --now timelapse-capture.service
        systemctl enable --now timelapse-encode.timer
        systemctl enable --now timelapse-watch.timer
        sleep 2
        if systemctl is-active --quiet timelapse-capture.service; then
            ok "Capture is running."
        else
            err "Capture failed to start. See: journalctl -u timelapse-capture -n 40"
        fi
    else
        say "Enable later with:"
        say "  systemctl enable --now timelapse-capture.service"
        say "  systemctl enable --now timelapse-encode.timer"
        say "  systemctl enable --now timelapse-watch.timer"
    fi

    # Separate from the pair above: the web UI is optional, off by default, and
    # only worth offering when the config actually asks for it.
    local web_on web_url
    web_on="$(python3 -c "import json;c=json.load(open('$CONFIG')).get('web',{});print('1' if c.get('enabled') else '0')" 2>/dev/null || echo 0)"
    if [ "$web_on" = "1" ]; then
        # hostport() rather than a format string, so an IPv6 bind is printed
        # bracketed and the URL offered here can actually be opened.
        web_url="$(python3 -c "import json,sys;sys.path.insert(0,'$PREFIX');from timelapse_encode import hostport;c=json.load(open('$CONFIG')).get('web',{});print('http://%s/' % hostport(c.get('bind','127.0.0.1'), c.get('port',8787)))" 2>/dev/null)"
        if ask_yn "Enable the web UI now ($web_url)?" y; then
            systemctl enable --now timelapse-web.service
            sleep 2
            if systemctl is-active --quiet timelapse-web.service; then
                ok "Web UI is running at $web_url"
            else
                err "Web UI failed to start. See: journalctl -u timelapse-web -n 40"
            fi
        else
            say "Enable later with: systemctl enable --now timelapse-web.service"
        fi
    fi

    printf '\n'
    say "${B}timelapse${N} status | logs | test | usage | encode | config | cameras | transfer | web | update"
}

as_service_user() {
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$SVCUSER" -- "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -u "$SVCUSER" "$@"
    else
        warn "Neither runuser nor sudo found; running as root instead."
        "$@"
    fi
}

do_uninstall() {
    step "Uninstall"
    if [ -d /run/systemd/system ]; then
        systemctl disable --now timelapse-capture.service 2>/dev/null || true
        systemctl disable --now timelapse-encode.timer 2>/dev/null || true
        systemctl disable --now timelapse-encode.service 2>/dev/null || true
        systemctl disable --now timelapse-web.service 2>/dev/null || true
        systemctl disable --now timelapse-watch.timer 2>/dev/null || true
        systemctl disable --now timelapse-watch.service 2>/dev/null || true
    fi
    rm -f "$UNITDIR"/timelapse-capture.service \
          "$UNITDIR"/timelapse-encode.service \
          "$UNITDIR"/timelapse-encode.timer \
          "$UNITDIR"/timelapse-web.service
    [ -d /run/systemd/system ] && systemctl daemon-reload || true
    rm -rf "$PREFIX"
    rm -f /usr/local/bin/timelapse
    ok "Removed programs, units and the command wrapper."

    if [ -f "$CONFIG" ]; then
        if ask_yn "Also delete $CONFDIR (your camera credentials)?" n; then
            rm -rf "$CONFDIR"; ok "Removed $CONFDIR"
        else
            note "Kept $CONFDIR"
        fi
    fi
    note "Captured frames and videos were left untouched."
    if id "$SVCUSER" >/dev/null 2>&1 && ask_yn "Delete the '$SVCUSER' account?" n; then
        userdel "$SVCUSER" 2>/dev/null && ok "Deleted '$SVCUSER'" || true
    fi
}

# ---------------------------------------------------------------------------

# Inlined rather than read from $0, because $0 is "bash" when piped from curl.
usage() {
    cat <<'USAGE'
timelapse-maker installer

  sudo bash install.sh [options]

  --unattended   no questions; sane defaults, does not enable services
  --no-wizard    install files only, skip configuration
  --uninstall    remove everything except captured data
  --ref REF      install a specific branch or tag (default: main)
  --prefix DIR   install location (default: /opt/timelapse)
  -h, --help     this text
USAGE
    exit 0
}

main() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --unattended) UNATTENDED=1 ;;
            --no-wizard)  RUN_WIZARD=0 ;;
            --uninstall)  DO_UNINSTALL=1 ;;
            --ref)        REF="$2"; shift ;;
            --prefix)     PREFIX="$2"; shift ;;
            -h|--help)    usage ;;
            *)            die "Unknown option: $1  (try --help)" ;;
        esac
        shift
    done

    require_root
    check_platform

    printf '\n%s' "$B"
    cat <<'BANNER'
  ╔══════════════════════════════════════════════════════════╗
  ║   timelapse-maker · unattended IP camera timelapses      ║
  ╚══════════════════════════════════════════════════════════╝
BANNER
    printf '%s' "$N"
    printf '  %sEXPERIMENTAL (v0.1.9)%s - early software, tested on one machine.\n' "$Y$B" "$N"
    note "Config format may change between versions. Not for production use."

    if [ "$DO_UNINSTALL" = "1" ]; then
        do_uninstall
        printf '\n'
        return 0
    fi

    # Before install_deps: everything after this point rewrites the very files
    # and unit states it reads, and an upgrade has to be able to put them back.
    snapshot_services

    install_deps
    obtain_source
    install_files
    install_units
    [ "$RUN_WIZARD" = "1" ] && run_wizard
    restore_services                # after sync_units, so units are current
    offer_enable
    printf '\n'
}

main "$@"

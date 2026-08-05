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
    install -m 0755 "$SRC/scripts/timelapse_capture.py" \
                    "$SRC/scripts/timelapse_encode.py" \
                    "$SRC/scripts/timelapse_test.py" \
                    "$SRC/scripts/timelapse_setup.py" "$PREFIX/"
    ok "Scripts -> $PREFIX"

    install -d -m 0750 "$CONFDIR"
    chgrp "$SVCUSER" "$CONFDIR" 2>/dev/null || true
    install -m 0644 "$SRC/config/config.example.json" "$CONFDIR/config.example.json"

    # Convenience wrappers so the tools are on PATH.
    cat > /usr/local/bin/timelapse <<EOF
#!/usr/bin/env bash
# timelapse-maker command wrapper
case "\${1:-}" in
    test)      shift; exec python3 $PREFIX/timelapse_test.py "$CONFIG" "\$@" ;;
    encode)    shift; exec python3 $PREFIX/timelapse_encode.py "$CONFIG" "\$@" ;;
    setup)     shift; exec python3 $PREFIX/timelapse_setup.py --output "$CONFIG" \\
                          --template "$CONFDIR/config.example.json" \\
                          --owner "$SVCUSER" "\$@" ;;
    config)    exec \${EDITOR:-nano} "$CONFIG" ;;
    logs)      exec journalctl -u timelapse-capture -f ;;
    status)    exec systemctl status timelapse-capture.service timelapse-encode.timer ;;
    *)
        echo "usage: timelapse {setup|test|encode|config|logs|status}"
        exit 1 ;;
esac
EOF
    chmod 0755 /usr/local/bin/timelapse
    ok "Command wrapper -> /usr/local/bin/timelapse"
}

install_units() {
    for unit in timelapse-capture.service timelapse-encode.service \
                timelapse-encode.timer; do
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
    [ -d /run/systemd/system ] && systemctl daemon-reload || true
    note "ReadWritePaths=$rw"
}

run_wizard() {
    step "Configuration"
    if [ -f "$CONFIG" ]; then
        note "An existing config was found at $CONFIG"
        if ! ask_yn "Reconfigure it?" n; then
            note "Keeping the existing configuration."
            sync_units
            return
        fi
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

offer_enable() {
    step "Next steps"
    if [ ! -f "$CONFIG" ]; then
        say "1. Configure:   timelapse setup"
        say "2. Verify:      timelapse test"
        say "3. Enable:      systemctl enable --now timelapse-capture.service"
        say "                systemctl enable --now timelapse-encode.timer"
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
    fi

    printf '\n'
    say "${B}timelapse${N} status | logs | test | encode | config | setup"
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
    fi
    rm -f "$UNITDIR"/timelapse-capture.service \
          "$UNITDIR"/timelapse-encode.service \
          "$UNITDIR"/timelapse-encode.timer
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
    printf '  %sEXPERIMENTAL (v0.0.6)%s - early software, tested on one machine.\n' "$Y$B" "$N"
    note "Config format may change between versions. Not for production use."

    if [ "$DO_UNINSTALL" = "1" ]; then
        do_uninstall
        printf '\n'
        return 0
    fi

    install_deps
    obtain_source
    install_files
    install_units
    [ "$RUN_WIZARD" = "1" ] && run_wizard
    offer_enable
    printf '\n'
}

main "$@"

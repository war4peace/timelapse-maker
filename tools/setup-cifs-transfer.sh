#!/usr/bin/env bash
#
# setup-cifs-transfer.sh — configure and verify an SMB/CIFS transfer target.
#
# Mounts a NAS share, works out which rsync flags actually succeed on it, does a
# real round-trip with a throwaway file, and prints the exact config.json values
# to use. Run it on the machine that will do the encoding.
#
#   sudo bash tools/setup-cifs-transfer.sh --server 192.168.2.10 --share cctv
#
# Options (all have defaults; -n/--dry-run changes nothing on disk):
#   --server HOST     SMB server            (default 192.168.2.10)
#   --share NAME      share name            (default cctv)
#   --subdir PATH     folder inside share   (default TL)
#   --mountpoint DIR  local mount point     (default /mnt/unraid-cctv)
#   --user NAME       SMB username          (prompted if omitted)
#   --svcuser NAME    service account       (default timelapse)
#   --cred FILE       credentials file      (default /etc/timelapse/cifs.cred)
#   -n, --dry-run     probe and report only; no fstab or credential changes
#   -h, --help
#
# Safe to re-run. It backs up /etc/fstab before touching it.

set -euo pipefail

SERVER="192.168.2.10"
SHARE="cctv"
SUBDIR="TL"
MOUNTPOINT="/mnt/unraid-cctv"
SMBUSER=""
SVCUSER="timelapse"
CREDFILE="/etc/timelapse/cifs.cred"
DRYRUN=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'
    Y=$'\033[33m'; C=$'\033[36m'; N=$'\033[0m'
else
    B=""; DIM=""; R=""; G=""; Y=""; C=""; N=""
fi

say()  { printf '  %s\n' "$*"; }
note() { printf '  %s%s%s\n' "$DIM" "$*" "$N"; }
ok()   { printf '  %sOK%s    %s\n' "$G" "$N" "$*"; }
warn() { printf '  %sWARN%s  %s\n' "$Y" "$N" "$*"; }
err()  { printf '  %sFAIL%s  %s\n' "$R" "$N" "$*" >&2; }
die()  { err "$*"; exit 1; }
step() {
    local pad="" i n=$(( 55 - ${#1} ))
    for (( i = 0; i < n; i++ )); do pad+="─"; done
    printf '\n%s── %s %s%s\n' "$C$B" "$1" "$pad" "$N"
}

FAILURES=0
record_fail() { FAILURES=$((FAILURES + 1)); err "$*"; }

usage() { sed -n '3,26p' "$0" | sed 's/^#\{0,1\} \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --server)     SERVER="$2"; shift ;;
        --share)      SHARE="$2"; shift ;;
        --subdir)     SUBDIR="$2"; shift ;;
        --mountpoint) MOUNTPOINT="$2"; shift ;;
        --user)       SMBUSER="$2"; shift ;;
        --svcuser)    SVCUSER="$2"; shift ;;
        --cred)       CREDFILE="$2"; shift ;;
        -n|--dry-run) DRYRUN=1 ;;
        -h|--help)    usage ;;
        *)            die "Unknown option: $1 (try --help)" ;;
    esac
    shift
done

UNC="//${SERVER}/${SHARE}"
DEST="${MOUNTPOINT}/${SUBDIR}"

[ "$(id -u)" = "0" ] || die "Run as root: sudo bash $0"

printf '\n%s' "$B"
cat <<'BANNER'
  ╔══════════════════════════════════════════════════════════╗
  ║   CIFS transfer target — setup and verification          ║
  ╚══════════════════════════════════════════════════════════╝
BANNER
printf '%s' "$N"
say "Share      $UNC"
say "Mount      $MOUNTPOINT"
say "Destination $DEST"
[ "$DRYRUN" = "1" ] && note "DRY RUN - nothing will be changed on disk."

# ---------------------------------------------------------------------------
step "Prerequisites"
# ---------------------------------------------------------------------------

if command -v mount.cifs >/dev/null 2>&1; then
    ok "cifs-utils present"
elif [ "$DRYRUN" = "1" ]; then
    warn "cifs-utils missing (dry run - not installing)"
else
    note "Installing cifs-utils..."
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cifs-utils >/dev/null
    elif command -v dnf >/dev/null 2>&1; then dnf install -y -q cifs-utils >/dev/null
    elif command -v pacman >/dev/null 2>&1; then pacman -S --noconfirm --needed cifs-utils >/dev/null
    elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install cifs-utils >/dev/null
    else die "Install cifs-utils manually, then re-run."; fi
    command -v mount.cifs >/dev/null 2>&1 || die "cifs-utils still not available."
    ok "Installed cifs-utils"
fi

command -v rsync >/dev/null 2>&1 && ok "rsync present" || record_fail "rsync is not installed"

if id "$SVCUSER" >/dev/null 2>&1; then
    SVC_UID=$(id -u "$SVCUSER"); SVC_GID=$(id -g "$SVCUSER")
    ok "Service account '$SVCUSER' (uid=$SVC_UID gid=$SVC_GID)"
else
    warn "Service account '$SVCUSER' does not exist yet."
    note "Run the timelapse installer first, or pass --svcuser. Falling back to root."
    SVC_UID=0; SVC_GID=0; SVCUSER="root"
fi

# ---------------------------------------------------------------------------
step "Reaching the server"
# ---------------------------------------------------------------------------

if ping -c 2 -W 3 "$SERVER" >/dev/null 2>&1; then
    ok "$SERVER responds to ping"
else
    warn "$SERVER does not respond to ping (may just be firewalled)"
fi

# SMB is 445. Prefer a real TCP check over assuming.
if command -v nc >/dev/null 2>&1; then
    nc -z -w 5 "$SERVER" 445 >/dev/null 2>&1 \
        && ok "TCP 445 open" || record_fail "TCP 445 closed on $SERVER"
elif command -v timeout >/dev/null 2>&1; then
    timeout 5 bash -c "cat < /dev/null > /dev/tcp/$SERVER/445" 2>/dev/null \
        && ok "TCP 445 open" || record_fail "TCP 445 closed on $SERVER"
else
    note "No nc/timeout available; skipping the port check."
fi

# ---------------------------------------------------------------------------
step "Credentials"
# ---------------------------------------------------------------------------

if [ -s "$CREDFILE" ] && [ -z "$SMBUSER" ]; then
    ok "Using existing $CREDFILE"
    note "Delete it and re-run to change the username or password."
elif [ "$DRYRUN" = "1" ]; then
    warn "No credentials file; dry run will not create one."
else
    [ -n "$SMBUSER" ] || { printf '  SMB username: ' > /dev/tty
                           read -r SMBUSER < /dev/tty; }
    printf '  SMB password for %s: ' "$SMBUSER" > /dev/tty
    read -rs SMBPASS < /dev/tty; printf '\n'
    [ -n "$SMBUSER" ] || die "A username is required."

    install -d -m 0750 "$(dirname "$CREDFILE")"
    ( umask 077
      # Written with a heredoc so the password never appears in the process
      # list, which is world-readable.
      cat > "$CREDFILE" <<EOF
username=$SMBUSER
password=$SMBPASS
EOF
    )
    chown root:root "$CREDFILE"; chmod 600 "$CREDFILE"
    unset SMBPASS
    ok "Wrote $CREDFILE (mode 600, root only)"
fi

# ---------------------------------------------------------------------------
step "Mounting"
# ---------------------------------------------------------------------------

BASE_OPTS="credentials=${CREDFILE},uid=${SVC_UID},gid=${SVC_GID}"
BASE_OPTS="${BASE_OPTS},file_mode=0664,dir_mode=0775,iocharset=utf8"

mkdir -p "$MOUNTPOINT"

if mountpoint -q "$MOUNTPOINT"; then
    ok "Already mounted at $MOUNTPOINT"
    VERS=""
elif [ "$DRYRUN" = "1" ]; then
    warn "Dry run - not mounting."
    VERS=""
else
    # Negotiate down. Modern Unraid speaks 3.1.1; older boxes may need 3.0 or
    # 2.1. SMB1 is deliberately not attempted.
    VERS=""
    for v in "" "vers=3.1.1" "vers=3.0" "vers=2.1"; do
        opts="$BASE_OPTS${v:+,$v}"
        if mount -t cifs "$UNC" "$MOUNTPOINT" -o "$opts" 2>/tmp/.cifs-err; then
            VERS="$v"
            ok "Mounted${v:+ with $v}"
            break
        fi
    done
    if ! mountpoint -q "$MOUNTPOINT"; then
        record_fail "Could not mount $UNC"
        say ""
        say "  Last error:"
        sed 's/^/      /' /tmp/.cifs-err 2>/dev/null | head -5
        say ""
        note "Common causes:"
        note "  - wrong username/password (delete $CREDFILE and re-run)"
        note "  - the share is not exported to this user on the NAS"
        note "  - share name is wrong; on Unraid it is the share, not the path"
        rm -f /tmp/.cifs-err
        exit 1
    fi
    rm -f /tmp/.cifs-err
fi

# --target would report the *parent* mount's options when nothing is mounted
# here, which reads as though the share were up. Only report a real mount.
if mountpoint -q "$MOUNTPOINT"; then
    ACTUAL=$(findmnt -no OPTIONS "$MOUNTPOINT" 2>/dev/null | cut -c1-100)
    note "Options: ${ACTUAL:-unknown}"
fi

# ---------------------------------------------------------------------------
step "Write access"
# ---------------------------------------------------------------------------

if [ "$DRYRUN" = "1" ] && ! mountpoint -q "$MOUNTPOINT"; then
    warn "Not mounted; skipping write tests."
else
    mkdir -p "$DEST" 2>/dev/null \
        && ok "Destination directory $DEST exists" \
        || record_fail "Could not create $DEST on the share"

    if [ "$SVCUSER" != "root" ]; then
        if runuser -u "$SVCUSER" -- test -w "$DEST" 2>/dev/null; then
            ok "$SVCUSER can write to $DEST"
        else
            record_fail "$SVCUSER cannot write to $DEST"
            note "Check uid=/gid= mount options and the share's SMB permissions."
        fi
    fi
fi

# ---------------------------------------------------------------------------
step "rsync flags that actually work here"
# ---------------------------------------------------------------------------
#
# This is the point of the script. CIFS cannot honour chown/chgrp when mounted
# with uid=/gid=, so `rsync -a` (which implies -o -g -p) fails with exit 23 and
# the nightly transfer reports failure even though the files arrived.

WORKING_ARGS=""
if mountpoint -q "$MOUNTPOINT"; then
    SRC=$(mktemp -d)
    # mktemp -d is 0700 and we are root, so the service account could not even
    # read the source; rsync would fail as *sender* and look like a CIFS problem.
    # It also has to OWN the directory: --remove-source-files unlinks from it,
    # which needs write permission on the directory, not just on the file. This
    # mirrors reality, where video_output belongs to the service account.
    chmod 0755 "$SRC"
    [ "$SVCUSER" != "root" ] && chown "$SVCUSER" "$SRC"
    head -c 1048576 /dev/urandom > "$SRC/probe.bin"   # 1 MB, like a small video
    SUM_BEFORE=$(md5sum "$SRC/probe.bin" | cut -d' ' -f1)

    try_rsync() {
        local label="$1"; shift
        local out rc
        cp "$SRC/probe.bin" "$SRC/try.bin"
        if [ "$SVCUSER" != "root" ]; then chown "$SVCUSER" "$SRC/try.bin"; fi
        set +e
        if [ "$SVCUSER" != "root" ]; then
            out=$(runuser -u "$SVCUSER" -- rsync "$@" "$SRC/try.bin" "$DEST/" 2>&1); rc=$?
        else
            out=$(rsync "$@" "$SRC/try.bin" "$DEST/" 2>&1); rc=$?
        fi
        set -e
        if [ "$rc" = "0" ]; then
            ok "$label -> exit 0"
            [ -z "$WORKING_ARGS" ] && WORKING_ARGS="$*"
        else
            warn "$label -> exit $rc"
            printf '%s\n' "$out" | head -3 | sed 's/^/          /'
        fi
        rm -f "$DEST/try.bin" "$SRC/try.bin" 2>/dev/null || true
        return 0
    }

    try_rsync "-a --partial              (project default)" -a --partial
    try_rsync "-rt --partial             (no perms/owner)"  -rt --partial
    try_rsync "-a --no-perms --no-owner --no-group" \
              -a --no-perms --no-owner --no-group --partial

    if [ -n "$WORKING_ARGS" ]; then
        ok "Use: $WORKING_ARGS --remove-source-files"
    else
        record_fail "No rsync flag combination succeeded"
    fi

    # ---------------------------------------------------------------------
    step "Round trip with --remove-source-files"
    # ---------------------------------------------------------------------

    if [ -n "$WORKING_ARGS" ]; then
        cp "$SRC/probe.bin" "$SRC/roundtrip.bin"
        [ "$SVCUSER" != "root" ] && chown "$SVCUSER" "$SRC/roundtrip.bin"
        set +e
        if [ "$SVCUSER" != "root" ]; then
            runuser -u "$SVCUSER" -- rsync $WORKING_ARGS --remove-source-files \
                "$SRC/roundtrip.bin" "$DEST/" >/dev/null 2>&1; rc=$?
        else
            rsync $WORKING_ARGS --remove-source-files \
                "$SRC/roundtrip.bin" "$DEST/" >/dev/null 2>&1; rc=$?
        fi
        set -e
        [ "$rc" = "0" ] && ok "rsync exited 0" || record_fail "rsync exited $rc"

        if [ -f "$DEST/roundtrip.bin" ]; then
            SUM_AFTER=$(md5sum "$DEST/roundtrip.bin" | cut -d' ' -f1)
            [ "$SUM_AFTER" = "$SUM_BEFORE" ] \
                && ok "File arrived intact (md5 matches)" \
                || record_fail "File arrived CORRUPTED (md5 differs)"
        else
            record_fail "File did not arrive at $DEST"
        fi

        [ -f "$SRC/roundtrip.bin" ] \
            && record_fail "Source file was not removed (--remove-source-files failed)" \
            || ok "Source file removed after transfer"

        rm -f "$DEST/roundtrip.bin" 2>/dev/null || true
    fi
    rm -rf "$SRC"
fi

# ---------------------------------------------------------------------------
step "Persistence across reboots"
# ---------------------------------------------------------------------------

FSTAB_OPTS="${BASE_OPTS}${VERS:+,$VERS},_netdev,nofail,x-systemd.automount"
FSTAB_OPTS="${FSTAB_OPTS},x-systemd.mount-timeout=30"
FSTAB_LINE="$UNC  $MOUNTPOINT  cifs  $FSTAB_OPTS  0  0"

if grep -qsF " $MOUNTPOINT " /etc/fstab; then
    ok "/etc/fstab already has an entry for $MOUNTPOINT"
    note "$(grep -F " $MOUNTPOINT " /etc/fstab | head -1 | cut -c1-90)"
elif [ "$DRYRUN" = "1" ]; then
    warn "Dry run - not editing /etc/fstab. It would add:"
    printf '      %s\n' "$FSTAB_LINE"
else
    cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
    printf '\n# timelapse-maker CIFS transfer target\n%s\n' "$FSTAB_LINE" >> /etc/fstab
    ok "Added to /etc/fstab (backup taken)"
    note "nofail + x-systemd.automount: a NAS that is down will not block boot,"
    note "and the share mounts on first access instead."
    systemctl daemon-reload 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
step "Stale-mount guard"
# ---------------------------------------------------------------------------
#
# The real danger of the CIFS route: if the share is not mounted, the
# mountpoint is just an empty local directory, and rsync fills the local disk.

if mountpoint -q "$MOUNTPOINT"; then
    ok "mountpoint -q detects the live mount"
    note "If the NAS goes away this returns non-zero, which is what the"
    note "encoder's transfer.require_mountpoint option checks before writing."
else
    warn "Not currently mounted, so the guard could not be demonstrated."
fi

# ---------------------------------------------------------------------------
step "Result"
# ---------------------------------------------------------------------------

if [ "$FAILURES" -gt 0 ]; then
    err "$FAILURES check(s) failed - fix these before enabling the transfer."
    exit 1
fi

if [ "$DRYRUN" = "1" ]; then
    warn "Dry run: nothing was mounted, so nothing was actually verified."
    note "Re-run without --dry-run to configure and test for real."
    note "The values below are what it would produce, not measured results."
else
    ok "Everything passed."
fi
say ""
say "${B}Put this in the transfer block of /etc/timelapse/config.json:${N}"
say ""
ARGS_JSON=$(printf '%s' "${WORKING_ARGS:--rt --partial}" \
            | tr ' ' '\n' | sed 's/.*/"&"/' | paste -sd, -)
cat <<EOF
      "transfer": {
        "enabled": true,
        "destination": "$DEST/",
        "rsync_args": [$ARGS_JSON, "--remove-source-files"],
        "delete_local_after_transfer": true,
        "require_mountpoint": true
      }
EOF
say ""
say "${B}And add the mountpoint to the encoder unit:${N}"
say ""
say "  systemctl edit --full timelapse-encode.service"
say "  # append $MOUNTPOINT to the existing ReadWritePaths= line"
say ""
note "ProtectSystem=strict means an unlisted path fails read-only."
say ""
say "Then: ${B}timelapse test${N}"
say ""

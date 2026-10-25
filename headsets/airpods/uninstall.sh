#!/usr/bin/env bash
# Remove this user's airpodsd package and the unchanged unit installed by install.sh.
set -euo pipefail

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { echo "$*" >&2; exit 1; }
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
STATE_DIR="$CONFIG_HOME/airpodsd"
UNIT="$CONFIG_HOME/systemd/user/airpodsd.service"

OWNED=0
if [[ -f "$UNIT" && -f "$STATE_DIR/unit-cksum" ]]; then
    CURRENT_CKSUM="$(cksum "$UNIT" | awk '{print $1 ":" $2}')"
    EXPECTED_CKSUM="$(cat "$STATE_DIR/unit-cksum")"
    [[ "$CURRENT_CKSUM" == "$EXPECTED_CKSUM" ]] && OWNED=1
fi
if [[ -f "$UNIT" && "$OWNED" -ne 1 ]]; then
    die "refusing to uninstall: $UNIT is not the unchanged unit recorded by this installer"
fi

PYTHON="${AIRPODSD_PYTHON:-}"
if [[ -z "$PYTHON" && -f "$STATE_DIR/python" ]]; then PYTHON="$(cat "$STATE_DIR/python")"; fi
if [[ -z "$PYTHON" ]]; then
    die "refusing to guess which Python environment owns airpodsd; set AIRPODSD_PYTHON to the intended interpreter"
fi
[[ -x "$PYTHON" ]] || die "recorded Python interpreter is unavailable: $PYTHON"

# All ownership checks complete before touching the unit or Python environment.
if [[ "$OWNED" -eq 1 ]]; then
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
        systemctl --user disable --now airpodsd.service || warn "could not disable/stop airpodsd.service"
        systemctl --user daemon-reload || warn "could not reload the user systemd manager"
    fi
    rm -f "$UNIT"
    info "Removed the unchanged airpodsd unit installed by this script"
fi

"$PYTHON" -m pip uninstall -y airpodsd || warn "could not uninstall airpodsd with $PYTHON"
rm -f "$STATE_DIR/unit-cksum" "$STATE_DIR/python"
rmdir "$STATE_DIR" 2>/dev/null || true
printf 'Configuration is left in place:\n  %s/airpodsd/\n' "$CONFIG_HOME"

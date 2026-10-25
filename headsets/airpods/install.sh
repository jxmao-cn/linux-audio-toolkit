#!/usr/bin/env bash
# Install airpodsd for the current user.
# Default: install the package and user unit without enabling or starting it.
#   --enable       enable and start the user service after installation
#   --replace-unit back up and replace a different existing user unit
#   --no-service   install the CLI only
#   --no-install   install only the unit (use AIRPODSD_BIN)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

WITH_SERVICE=1
INSTALL_PACKAGE=1
ENABLE_SERVICE=0
REPLACE_UNIT=0
while (($#)); do
    case "$1" in
        --no-service) WITH_SERVICE=0 ;;
        --no-install) INSTALL_PACKAGE=0 ;;
        --enable) ENABLE_SERVICE=1 ;;
        --replace-unit) REPLACE_UNIT=1 ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { echo "$*" >&2; exit 1; }

PYTHON="${AIRPODSD_PYTHON:-$(command -v python3 || true)}"
[[ -n "$PYTHON" && -x "$PYTHON" ]] || die "AIRPODSD_PYTHON (or python3) must point at an executable interpreter"
PYTHON="$(cd "$(dirname "$PYTHON")" && pwd)/$(basename "$PYTHON")"
PYTHON_INFO="$("$PYTHON" -c 'import os, site, sys, sysconfig; isolated = sys.prefix != getattr(sys, "base_prefix", sys.prefix) or hasattr(sys, "real_prefix") or os.path.isdir(os.path.join(sys.prefix, "conda-meta")) or os.environ.get("CONDA_PREFIX") == sys.prefix; scripts = sysconfig.get_path("scripts") if isolated else os.path.join(site.getuserbase(), "bin"); print("%d.%d\t%d\t%s" % (sys.version_info[0], sys.version_info[1], isolated, scripts))')" || die "cannot run Python: $PYTHON"
IFS=$'\t' read -r PYTHON_VERSION PYTHON_ISOLATED PYTHON_SCRIPTS <<< "$PYTHON_INFO"
info "Using Python $PYTHON_VERSION ($PYTHON)"

if ! "$PYTHON" -c 'import dbus, gi' >/dev/null 2>&1; then
    warn "Python D-Bus and/or GI bindings are missing; the daemon needs them at runtime."
    echo "       Install python3-dbus and python3-gi, or use a venv with --system-site-packages." >&2
fi
if ! command -v pactl >/dev/null 2>&1; then
    warn "pactl is missing (install pulseaudio-utils or pipewire-pulse)."
fi

if [[ -n "${AIRPODSD_BIN:-}" ]]; then
    BIN="$AIRPODSD_BIN"
elif [[ "$INSTALL_PACKAGE" -eq 1 ]]; then
    BIN="$PYTHON_SCRIPTS/airpodsd"
else
    BIN="$(command -v airpodsd || true)"
    if [[ -z "$BIN" ]]; then BIN="$HOME/.local/bin/airpodsd"; fi
fi
if [[ "$BIN" == *$'\n'* || "$BIN" == *$'\r'* ]]; then die "AIRPODSD_BIN must not contain a newline"; fi
if [[ "$BIN" != /* ]]; then BIN="$ROOT/$BIN"; fi
if [[ "$INSTALL_PACKAGE" -eq 0 && ! -x "$BIN" ]]; then
    die "AIRPODSD_BIN must point at an executable airpodsd"
fi
if [[ "$INSTALL_PACKAGE" -eq 1 && -e "$BIN" && ! -x "$BIN" ]]; then
    die "airpodsd path exists but is not executable: $BIN"
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/airpodsd.service"
STATE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/airpodsd"
UNIT_TMP=""
cleanup() { [[ -z "$UNIT_TMP" ]] || rm -f "$UNIT_TMP"; }
trap cleanup EXIT

if [[ "$WITH_SERVICE" -eq 1 ]]; then
    # systemd uses quoted C-style arguments. Escape syntax and specifiers, and
    # reject line breaks before they can create additional unit directives.
    [[ "$BIN" != *$'\n'* && "$BIN" != *$'\r'* ]] || die "executable path must not contain a newline"
    ESCAPED_BIN=${BIN//\\/\\\\}
    ESCAPED_BIN=${ESCAPED_BIN//\"/\\\"}
    ESCAPED_BIN=${ESCAPED_BIN//%/%%}
    ESCAPED_BIN=${ESCAPED_BIN//\$/\$\$}
    SED_BIN=${ESCAPED_BIN//\\/\\\\}
    SED_BIN=${SED_BIN//&/\\&}
    SED_BIN=${SED_BIN//|/\\|}
    mkdir -p "$UNIT_DIR"
    UNIT_TMP="$(mktemp "$UNIT_DIR/.airpodsd.service.XXXXXX")"
    sed "s|@EXEC@|\"$SED_BIN\"|g" systemd/airpodsd.service.in > "$UNIT_TMP"
    if [[ -f "$UNIT" ]] && ! cmp -s "$UNIT_TMP" "$UNIT"; then
        if [[ "$REPLACE_UNIT" -ne 1 ]]; then
            die "existing unit differs: $UNIT (use --replace-unit to back it up and replace it)"
        fi
    fi
fi

if [[ "$INSTALL_PACKAGE" -eq 1 ]]; then
    "$PYTHON" -m pip --version >/dev/null 2>&1 || die "pip is required for $PYTHON (or use --no-install)"
    info "Installing the Python package with $PYTHON"
    PIP_TARGET=()
    if [[ "$PYTHON_ISOLATED" -eq 0 ]]; then PIP_TARGET=(--user); fi
    if ! "$PYTHON" -m pip install "${PIP_TARGET[@]}" --upgrade .; then
        warn "isolated build failed; retrying without build isolation"
        if ! "$PYTHON" -m pip install "${PIP_TARGET[@]}" --upgrade --no-build-isolation .; then
            cat >&2 <<'MSG'

error: pip could not install the package. For a PEP 668 system Python, use a
venv that can still see the system D-Bus bindings:

  python3 -m venv --system-site-packages ~/.local/share/airpodsd/venv
  ~/.local/share/airpodsd/venv/bin/pip install .
  ~/.local/share/airpodsd/venv/bin/airpodsctl doctor
  AIRPODSD_BIN=~/.local/share/airpodsd/venv/bin/airpodsd ./install.sh --no-install
MSG
            exit 1
        fi
    fi
    if ! "$PYTHON" -c 'import airpods' >/dev/null 2>&1; then
        die "the installed package cannot be imported; inspect this interpreter's pip output and retry"
    fi
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$PYTHON" > "$STATE_DIR/python"
fi

if [[ "$WITH_SERVICE" -eq 1 ]]; then
    if [[ -f "$UNIT" ]] && ! cmp -s "$UNIT_TMP" "$UNIT"; then
        BACKUP="$UNIT.$(date +%Y%m%d%H%M%S).bak"
        [[ ! -e "$BACKUP" ]] || die "backup already exists: $BACKUP"
        cp -p "$UNIT" "$BACKUP"
        info "Backed up existing unit to $BACKUP"
    fi
    if ! cmp -s "$UNIT_TMP" "$UNIT" 2>/dev/null; then mv -f "$UNIT_TMP" "$UNIT"; UNIT_TMP=""; else rm -f "$UNIT_TMP"; UNIT_TMP=""; fi
    mkdir -p "$STATE_DIR"
    printf '%s\n' "$PYTHON" > "$STATE_DIR/python"
    cksum "$UNIT" | awk '{print $1 ":" $2}' > "$STATE_DIR/unit-cksum"
    info "Installed systemd user unit to $UNIT"

    if [[ "$ENABLE_SERVICE" -eq 1 ]]; then
        if systemctl --user show-environment >/dev/null 2>&1; then
            systemctl --user daemon-reload
            systemctl --user enable --now airpodsd.service || warn "could not enable/start the service; run: systemctl --user status airpodsd"
        else
            warn "no systemd user session found; the unit is installed but could not be enabled or started"
        fi
    fi
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) warn "$HOME/.local/bin is not on PATH; add it to your shell profile." ;; esac
info "Done. Next steps:"
echo "  1. Put the AirPods in pairing mode (case open, hold the case button)."
echo "  2. Run: airpodsctl pair"
echo "  3. Check: airpodsctl status && airpodsctl doctor"

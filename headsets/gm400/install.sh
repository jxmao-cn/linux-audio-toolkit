#!/bin/sh
set -eu
usage() {
    cat <<'EOF'
Usage: install.sh [--help]

Installs gm400-watch.sh and its user systemd unit. Installation does not
enable or start the service. Afterwards, enable it explicitly with:
  systemctl --user enable --now gm400-watch.service

Offline staging is supported with DESTDIR and XDG_CONFIG_HOME. XDG_BIN_HOME
must remain at its default ($HOME/.local/bin) because the unit uses %h/.local/bin.
Existing different files are never overwritten; move them aside yourself.
EOF
}
case "${1:-}" in --help) usage; exit 0;; "") ;; *) usage >&2; exit 2;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destdir=${DESTDIR:-}
install_home=${HOME:?HOME must be set}
bin_home=${XDG_BIN_HOME:-$install_home/.local/bin}
if [ "$bin_home" != "$install_home/.local/bin" ]; then
    printf 'Unsupported XDG_BIN_HOME: this unit uses %%h/.local/bin; use the default %s\n' "$install_home/.local/bin" >&2
    exit 1
fi
config_home=${XDG_CONFIG_HOME:-${HOME:?HOME must be set}/.config}
bin_dir=$destdir$bin_home
unit_dir=$destdir$config_home/systemd/user
preflight_one() {
    source=$1 target=$2
    if [ -e "$target" ]; then
        if cmp -s "$source" "$target"; then
            return
        fi
        printf 'Refusing to overwrite different file: %s\n' "$target" >&2
        exit 1
    fi
}
install_one() {
    source=$1 target=$2 mode=$3
    [ -e "$target" ] && { printf 'Already installed: %s\n' "$target"; return; }
    mkdir -p "$(dirname -- "$target")"
    install -m "$mode" "$source" "$target"
    printf 'Installed: %s\n' "$target"
}
preflight_one "$script_dir/gm400-watch.sh" "$bin_dir/gm400-watch.sh"
preflight_one "$script_dir/gm400-watch.service" "$unit_dir/gm400-watch.service"
install_one "$script_dir/gm400-watch.sh" "$bin_dir/gm400-watch.sh" 755
install_one "$script_dir/gm400-watch.service" "$unit_dir/gm400-watch.service" 644
printf '%s\n' 'Installation complete. No service was enabled or started.'

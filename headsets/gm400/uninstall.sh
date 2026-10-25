#!/bin/sh
set -eu
usage() {
    cat <<'EOF'
Usage: uninstall.sh [--help]

Removes the installed watcher and unit if they exactly match this package.
Does not stop or disable the service. Disable it yourself first if needed:
  systemctl --user disable --now gm400-watch.service

DESTDIR and XDG_CONFIG_HOME select the same paths as install.sh. XDG_BIN_HOME
must remain at its default ($HOME/.local/bin) because the unit uses %h/.local/bin.
EOF
}
case "${1:-}" in --help) usage; exit 0;; "") ;; *) usage >&2; exit 2;; esac
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
destdir=${DESTDIR:-}
install_home=${HOME:?HOME must be set}
bin_home=${XDG_BIN_HOME:-$install_home/.local/bin}
if [ "$bin_home" != "$install_home/.local/bin" ]; then
    printf 'Unsupported XDG_BIN_HOME: this unit uses %%h/.local/bin; expected %s\n' "$install_home/.local/bin" >&2
    exit 1
fi
config_home=${XDG_CONFIG_HOME:-${HOME:?HOME must be set}/.config}
remove_one() {
    source=$1 target=$2
    if [ ! -e "$target" ]; then printf 'Not installed: %s\n' "$target"; return; fi
    if ! cmp -s "$source" "$target"; then
        printf 'Refusing to remove different file: %s\n' "$target" >&2
        exit 1
    fi
    rm -- "$target"
    printf 'Removed: %s\n' "$target"
}
remove_one "$script_dir/gm400-watch.sh" "$destdir$bin_home/gm400-watch.sh"
remove_one "$script_dir/gm400-watch.service" "$destdir$config_home/systemd/user/gm400-watch.service"

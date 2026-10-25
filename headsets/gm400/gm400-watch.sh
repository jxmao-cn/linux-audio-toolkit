#!/bin/sh
# PulseAudio recovery watcher for an ALSA card whose id defaults to GM400.
# Never unload module-udev-detect: it would tear down unrelated sound cards.

GM400_READY_DELAY=${GM400_READY_DELAY:-120}
GM400_PROC_ASOUND=${GM400_PROC_ASOUND:-/proc/asound}
GM400_CARD_ID=${GM400_CARD_ID:-GM400}
GM400_PA_CARD_MATCH=${GM400_PA_CARD_MATCH:-GM400}
GM400_PA_SINK_MATCH=${GM400_PA_SINK_MATCH:-GM400}
GM400_PA_SOURCE_MATCH=${GM400_PA_SOURCE_MATCH:-GM400}
GM400_MODULE_NAME=${GM400_MODULE_NAME:-usb-GM400}
GM400_MODULE_CARD_NAME=${GM400_MODULE_CARD_NAME:-alsa_card.usb-GM400}
GM400_INTERVAL=${GM400_INTERVAL:-3}

if [ -z "${PULSE_SERVER:-}" ] && [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    PULSE_SERVER="unix:${XDG_RUNTIME_DIR}/pulse/native"
    export PULSE_SERVER
fi

find_target_card() {
    for card_id_file in "$GM400_PROC_ASOUND"/card*/id; do
        [ -r "$card_id_file" ] || continue
        IFS= read -r card_name < "$card_id_file" || card_name=
        [ "$card_name" = "$GM400_CARD_ID" ] || continue
        card_num=${card_id_file#"$GM400_PROC_ASOUND"/card}
        card_num=${card_num%/id}
        printf '%s\n' "$card_num"
        return 0
    done
    return 1
}

check_once() {
    card_num=$(find_target_card) || {
        seen_card=
        card_seen_at=0
        probe_attempted=0
        return 0
    }

    if [ "$card_num" != "${seen_card:-}" ]; then
        seen_card=$card_num
        card_seen_at=$(date +%s)
        probe_attempted=0
    fi

    now=$(date +%s)
    elapsed=$((now - ${card_seen_at:-$now}))
    [ "$elapsed" -lt "$GM400_READY_DELAY" ] && return 0

    cards=$(timeout 5s pactl list cards 2>/dev/null) || return 0
    card_loaded=0
    if printf '%s\n' "$cards" | awk -v card_num="$card_num" -v needle="$GM400_PA_CARD_MATCH" '
        /^[^ \t]/ { have_num=0; have_name=0 }
        $0 ~ "alsa.card = \"" card_num "\"" { have_num=1 }
        index($0, "alsa.card_name = \"") && index($0, needle) { have_name=1 }
        have_num && have_name { found=1 }
        END { exit(found ? 0 : 1) }
    '; then
        card_loaded=1
        sinks=$(timeout 5s pactl list short sinks 2>/dev/null) || return 0
        sources=$(timeout 5s pactl list short sources 2>/dev/null) || return 0
        target_sink=$(printf '%s\n' "$sinks" | awk -v needle="$GM400_PA_SINK_MATCH" 'BEGIN{FS="\t"} index($2,needle){print $2; exit}')
        target_source=$(printf '%s\n' "$sources" | awk -v needle="$GM400_PA_SOURCE_MATCH" 'BEGIN{FS="\t"} index($2,needle) && tolower($2) !~ /monitor/{print $2; exit}')
        if [ -n "$target_sink" ] && [ -n "$target_source" ]; then
            current_sink=$(pactl get-default-sink 2>/dev/null) || return 0
            if [ "$current_sink" != "$target_sink" ]; then
                pactl set-default-sink "$target_sink" >/dev/null 2>&1 || return 0
            fi
            current_source=$(pactl get-default-source 2>/dev/null) || return 0
            if [ "$current_source" != "$target_source" ]; then
                pactl set-default-source "$target_source" >/dev/null 2>&1 || return 0
            fi
            return 0
        fi
    fi

    [ "${probe_attempted:-0}" -eq 1 ] && return 0
    probe_attempted=1

    if [ "$card_loaded" -eq 1 ]; then
        action=pulseaudio-restart
        if output=$(timeout 100s systemctl --user restart pulseaudio.service 2>&1); then status=0; else status=$?; fi
    else
        action=module
        if output=$(timeout 8s pactl load-module module-alsa-card \
            "device_id=$card_num" "name=$GM400_MODULE_NAME" \
            "card_name=$GM400_MODULE_CARD_NAME" namereg_fail=false \
            tsched=yes fixed_latency_range=no ignore_dB=no deferred_volume=yes \
            use_ucm=yes avoid_resampling=no \
            card_properties=module-udev-detect.discovered=1 2>&1); then status=0; else status=$?; fi
    fi
    if [ "$status" -eq 0 ]; then
        printf 'GM400 card %s recovery via %s succeeded: %s\n' "$card_num" "$action" "$output"
    else
        printf 'GM400 card %s recovery via %s failed (status %s): %s; waiting for USB re-enumeration\n' "$card_num" "$action" "$status" "$output" >&2
        return "$status"
    fi
}

gm400_usage() {
    cat <<'EOF'
Usage: gm400-watch.sh [--help | --once]
  --help  Show this help.
  --once  Check once and exit; still honors the 120-second enumeration wait.

The default ALSA card id is GM400. GM400_CARD_ID, GM400_PA_CARD_MATCH,
GM400_PA_SINK_MATCH, and GM400_PA_SOURCE_MATCH can override matching.
Set GM400_WATCH_LIBRARY=1 before sourcing to load functions without running.
EOF
}

seen_card=
card_seen_at=0
probe_attempted=0
if [ "${GM400_WATCH_LIBRARY:-0}" = 1 ]; then
    return 0 2>/dev/null || exit 0
fi
case "${1:-}" in
    --help) gm400_usage; exit 0 ;;
    --once) check_once; exit $? ;;
    "") ;;
    *) gm400_usage >&2; exit 2 ;;
esac
while :; do
    check_once
    sleep "$GM400_INTERVAL"
done

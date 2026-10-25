#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"; [ -z "${stage:-}" ] || rm -rf "$stage"' EXIT HUP INT TERM
mkdir -p "$tmp/bin" "$tmp/proc/asound/card2"
printf 'GM400\n' > "$tmp/proc/asound/card2/id"
export TEST_LOG="$tmp/log" TEST_MODE=missing TEST_NOW=0
cat > "$tmp/bin/date" <<'EOF'
#!/bin/sh
printf '%s\n' "$TEST_NOW"
EOF
cat > "$tmp/bin/timeout" <<'EOF'
#!/bin/sh
shift
exec "$@"
EOF
cat > "$tmp/bin/pactl" <<'EOF'
#!/bin/sh
case "$*" in
  'list cards')
    if [ "$TEST_MODE" != missing ]; then
      printf 'Card #2\n\talsa.card = "2"\n\talsa.card_name = "HECATE GM400"\n'
    else
      printf 'Card #0\n\talsa.card = "0"\n\talsa.card_name = "Other"\n'
    fi ;;
  'list short sinks')
    if [ "$TEST_MODE" != loaded-incomplete ]; then
      printf '1\talsa_output.GM400.analog-stereo\tmodule-alsa-card.c\n'
    fi ;;
  'list short sources')
    if [ "$TEST_MODE" = healthy ]; then
      printf '2\talsa_output.GM400.analog-stereo.monitor\tmodule-alsa-card.c\n'
      printf '3\talsa_input.GM400.analog-stereo\tmodule-alsa-card.c\n'
    elif [ "$TEST_MODE" != loaded-incomplete ]; then
      printf '2\talsa_input.GM400.analog-stereo\tmodule-alsa-card.c\n'
    fi ;;
  'get-default-sink') printf '%s\n' "${TEST_DEFAULT_SINK:-other}" ;;
  'get-default-source') printf '%s\n' "${TEST_DEFAULT_SOURCE:-other}" ;;
  'set-default-sink '*|'set-default-source '*) printf '%s\n' "$*" >> "$TEST_LOG" ;;
  'load-module '*) printf '%s\n' "$*" >> "$TEST_LOG"; printf 'simulated module failure\n' >&2; exit 7 ;;
  *) exit 0 ;;
esac
EOF
cat > "$tmp/bin/systemctl" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >> "$TEST_LOG"
printf 'simulated restart failure\n' >&2
exit 9
EOF
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH" GM400_PROC_ASOUND="$tmp/proc/asound" GM400_READY_DELAY=120
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
assert_empty_log() { [ ! -s "$TEST_LOG" ] || fail "$1: unexpected audio mutation: $(cat "$TEST_LOG")"; }

# Sourceable entry exposes stateful checks without launching the infinite loop.
GM400_WATCH_LIBRARY=1 . "$root/gm400-watch.sh"

# No matching card must not query or mutate the audio server.
rm "$tmp/proc/asound/card2/id"
check_once
assert_empty_log 'missing card'
printf 'GM400\n' > "$tmp/proc/asound/card2/id"

# The 120-second firmware window must not probe; after it, one failed probe is
# reported as failure and is not repeated during the same enumeration.
TEST_NOW=100
check_once
TEST_NOW=219
check_once
assert_empty_log 'waiting period'
TEST_NOW=220
if check_once 2>"$tmp/failure"; then fail 'failed module probe returned success'; fi
grep -q 'recovery via module failed (status 7)' "$tmp/failure" || fail 'module failure was not reported'
check_once 2>/dev/null || :
[ "$(wc -l < "$TEST_LOG")" -eq 1 ] || fail 'more than one probe for the enumeration'
grep -q 'load-module module-alsa-card' "$TEST_LOG" || fail 'expected one module probe'

# Card disappearance resets enumeration state, so a new card can be probed.
rm "$tmp/proc/asound/card2/id"
check_once
mkdir -p "$tmp/proc/asound/card3"
printf 'GM400\n' > "$tmp/proc/asound/card3/id"
TEST_NOW=300
check_once
TEST_NOW=420
if check_once 2>"$tmp/reprobe-failure"; then fail 're-enumerated probe should preserve failure'; fi
[ "$(wc -l < "$TEST_LOG")" -eq 2 ] || fail 'new enumeration did not receive one new probe'

# A healthy card with an already-default input/output causes no changes, and
# its .monitor source is never selected as the microphone.
: > "$TEST_LOG"
rm -rf "$tmp/proc/asound/card3"
mkdir -p "$tmp/proc/asound/card2"
printf 'GM400\n' > "$tmp/proc/asound/card2/id"
seen_card= card_seen_at=0 probe_attempted=0
TEST_MODE=healthy TEST_DEFAULT_SINK=alsa_output.GM400.analog-stereo
TEST_DEFAULT_SOURCE=alsa_input.GM400.analog-stereo TEST_NOW=500
export TEST_MODE TEST_DEFAULT_SINK TEST_DEFAULT_SOURCE TEST_NOW
check_once
TEST_NOW=620
check_once
assert_empty_log 'already-default healthy card'
TEST_DEFAULT_SINK=other TEST_DEFAULT_SOURCE=other
check_once
[ "$(wc -l < "$TEST_LOG")" -eq 2 ] || fail 'healthy card should select two defaults'
grep -q '^set-default-source alsa_input.GM400.analog-stereo$' "$TEST_LOG" || fail 'microphone selection failed'
if grep -q '\.monitor' "$TEST_LOG"; then fail 'monitor selected as microphone'; fi

# A mismatching unit must fail preflight before installing the executable.
stage=$(mktemp -d)
mkdir -p "$stage/home/test/.config/systemd/user"
printf 'local unit\n' > "$stage/home/test/.config/systemd/user/gm400-watch.service"
if HOME=/home/test DESTDIR="$stage" "$root/install.sh" >"$tmp/install-conflict" 2>&1; then fail 'install accepted conflicting unit'; fi
[ ! -e "$stage/home/test/.local/bin/gm400-watch.sh" ] || fail 'install partially wrote executable before conflict'
grep -q 'local unit' "$stage/home/test/.config/systemd/user/gm400-watch.service" || fail 'conflicting unit was overwritten'
rm -rf "$stage"

# Custom executable location is rejected to prevent a unit/path mismatch.
if HOME=/home/test XDG_BIN_HOME=/tmp/custom-bin "$root/install.sh" >"$tmp/bin-home-error" 2>&1; then fail 'custom XDG_BIN_HOME was accepted'; fi
grep -q 'Unsupported XDG_BIN_HOME' "$tmp/bin-home-error" || fail 'custom XDG_BIN_HOME rejection was unclear'

# Complete offline install and removal; verify the staged unit still uses %h.
stage=$(mktemp -d)
HOME=/home/test DESTDIR="$stage" "$root/install.sh" >"$tmp/install-ok"
test -x "$stage/home/test/.local/bin/gm400-watch.sh" || fail 'staged watcher missing'
grep -Fq 'ExecStart=%h/.local/bin/gm400-watch.sh' "$stage/home/test/.config/systemd/user/gm400-watch.service" || fail 'unit path does not use %h'
HOME=/home/test DESTDIR="$stage" "$root/uninstall.sh" >"$tmp/uninstall-ok"
test ! -e "$stage/home/test/.local/bin/gm400-watch.sh" || fail 'staged watcher not removed'
rm -rf "$stage"

# A loaded but incomplete card uses PulseAudio restart; preserve its real error.
: > "$TEST_LOG"
seen_card= card_seen_at=0 probe_attempted=0
TEST_MODE=loaded-incomplete TEST_NOW=1000
check_once
TEST_NOW=1120
if check_once 2>"$tmp/restart-failure"; then fail 'failed restart returned success'; fi
grep -q 'recovery via pulseaudio-restart failed (status 9)' "$tmp/restart-failure" || fail 'restart failure was not reported'
check_once 2>/dev/null || :
[ "$(wc -l < "$TEST_LOG")" -eq 1 ] || fail 'restart repeated for same enumeration'
grep -q 'restart pulseaudio.service' "$TEST_LOG" || fail 'expected PulseAudio restart'

# No global udev-detect unload path may exist in implementation.
if grep -Eiq 'unload-module[^\n]*udev-detect|module-udev-detect[^\n]*unload' "$root/gm400-watch.sh"; then
    fail 'global module-udev-detect unload found'
fi
printf 'PASS: missing card, 120-second wait, one attempt per enumeration, failure status, and no global udev-detect unload\n'

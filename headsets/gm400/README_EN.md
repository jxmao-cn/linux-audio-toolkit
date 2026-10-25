# GM400 PulseAudio watcher

A user service for the HECATE GM400 USB headset. It waits 120 seconds after
ALSA enumeration and allows at most one recovery probe per enumeration. It
never unloads global `module-udev-detect`. The default ALSA id is `GM400`; card,
sink, and source matching can be configured through environment variables.

This is intended for PulseAudio. PipeWire compatibility has not been validated
and is not claimed. Successful recovery may change the default input and
output. If a card is present but its endpoints are incomplete, the watcher may
restart `pulseaudio.service`, interrupting all PulseAudio streams.

Known device: HECATE GM400, USB `35bb:a20f`, ALSA id `GM400`. The original
recovery setup came from this machine's GM400 configuration. This portable
version has only been checked with hardware-independent stubs; it has not been
revalidated on the physical headset. Other cards and naming schemes need
independent validation.

Run `./install.sh` to install. It does not enable or start the service. Enable
it explicitly with `systemctl --user enable --now gm400-watch.service`. Both
destinations are checked before installation, so conflicts do not leave a
partial install. `XDG_BIN_HOME` must remain `~/.local/bin` because the unit
uses `%h/.local/bin`; `XDG_CONFIG_HOME` and `DESTDIR` support offline staging.
Run `./uninstall.sh` to remove matching package files; it does not stop or
disable the service. Use `--help` for command help and `gm400-watch.sh --once`
for one diagnostic check. `--once` still honors the 120-second wait after a
new enumeration.

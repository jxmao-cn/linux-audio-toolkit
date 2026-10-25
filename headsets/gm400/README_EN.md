# HECATE GM400 wired headset: Linux audio discovery and recovery

**HECATE GM400 is the model name of my wired headset.** It connects to my Linux computer through USB for playback and microphone input. `gm400-watch` is the recovery script written for this headset.

## Why I built it

I encountered a practical problem: the headset was plugged in and ALSA could see its USB sound card, but the desktop audio server sometimes had no usable playback or recording endpoint. That prevented me from listening, using the microphone, or relying on it for voice typing.

The script grew out of the steps needed to make this setup usable day to day. It waits for the device to become ready, then attempts recovery so I have less manual audio troubleshooting to repeat. It is shared for people with the same headset and similar symptoms.

## Headset model and specifications

GM400 here means the **Edifier HECATE GM400 Dual-Engine wired in-ear headset**; it is a headset model, not a Linux device or software name. The maintainer's unit is the USB-C digital-interface version. Its recorded USB ID is `35bb:a20f`, and ALSA labels its card `GM400`. This USB ID comes from the maintainer's device record; the headset was not connected during the current check.

| Specification | Value |
| --- | --- |
| Connection | Wired USB-C digital interface (maintainer's unit) |
| Drivers | 4 × 6 mm dynamic drivers |
| Frequency response | 20 Hz–40 kHz |
| Impedance | 16 Ω |
| Sensitivity | 100 dB |
| Cable length | About 1.3 m |

These product figures are listed on a [public product specification page](https://www.voptronics.com/edifier-hecate-gm400-dual-engine-in-ear-wired-headphones.html). Edifier's current official site did not provide a GM400 specification page; treat the figures as model reference, and use the local device identifiers and validation scope below for Linux compatibility.

If your headset already plays and records correctly, the normal system audio settings or [Voice Input](../../voice-input/README_EN.md) may be all you need. This module focuses on GM400 discovery and recovery.

## How it works

A user service watches the ALSA card identified as `GM400`. It waits 120 seconds after
ALSA enumeration and allows at most one recovery probe per enumeration. It
never unloads global `module-udev-detect`. The default ALSA id is `GM400`; card,
sink, and source matching can be configured through environment variables.

This is intended for PulseAudio. PipeWire compatibility has not been validated
and is not claimed. Successful recovery may change the default input and
output. If a card is present but its endpoints are incomplete, the watcher may
restart `pulseaudio.service`, interrupting all PulseAudio streams.

Known device: HECATE GM400, USB `35bb:a20f`, ALSA id `GM400`. The original
recovery setup was developed for my daily-use headset. This portable
version has only been checked with hardware-independent stubs; it has not been
revalidated on the physical headset. Other cards and naming schemes need
independent validation.

## Installation

From the repository root:

```sh
cd headsets/gm400
./install.sh
```

The installer copies the script and user service unit. It does not enable or start the service. After installation, activate it explicitly:

```sh
systemctl --user daemon-reload
systemctl --user enable --now gm400-watch.service
```

Both destinations are checked before installation, so conflicts do not leave a
partial install. `XDG_BIN_HOME` must remain `~/.local/bin` because the unit
uses `%h/.local/bin`; `XDG_CONFIG_HOME` and `DESTDIR` support offline staging.
Run `./uninstall.sh` to remove matching package files; it does not stop or
disable the service. Use `--help` for command help and `gm400-watch.sh --once`
for one diagnostic check. `--once` still honors the 120-second wait after a
new enumeration.

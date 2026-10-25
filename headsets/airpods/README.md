# airpodsd

**A userspace AirPods driver for Linux.** Pairing, auto-reconnect, battery
reporting, audio-profile switching and diagnostics — in one small CLI plus a
systemd user daemon, with no third-party pip packages.

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey.svg)](#requirements)
[![中文](https://img.shields.io/badge/文档-简体中文-red.svg)](README.zh-CN.md)

```text
$ airpodsctl status
AirPods Pro (2nd generation)
  Address    AC:12:34:56:78:90
  Adapter    hci0
  Link       connected · -48 dBm
  Audio      A2DP (SBC) · bluez_output.AC_12_34_56_78_90.1
  Battery    L 82%   R 79%   Case 65%
```

---

## Why another AirPods project?

AirPods are standard Bluetooth audio devices. Linux already has **everything
needed in the kernel**: `btusb`, the Bluetooth core, A2DP, HFP and the GATT
battery service. A kernel module would not make them sound better, and it could
not manage the desktop audio graph anyway.

What Linux is missing is the **glue** that Apple's ecosystem provides:

- a pairing flow that survives Apple's "Just Works" quirks;
- reconnecting when the buds come out of the case;
- showing the battery cells the firmware exposes, instead of one vague percentage;
- choosing A2DP vs. HFP without hunting through `pavucontrol`;
- telling you *why* pairing failed.

`airpodsd` is that glue. It is a **userspace driver**: a thin
layer over BlueZ's D-Bus API and PulseAudio/PipeWire's `pactl` interface.

> **Not affiliated with Apple.** AirPods is a trademark of Apple Inc. This is
> an independent, reverse-engineering-free integration built on public
> Bluetooth standards.

## Features

| | |
|---|---|
| **Pairing wizard** | `airpodsctl pair` scans, selects, pairs, trusts and connects — with a constrained BlueZ `Agent1` |
| **Auto-reconnect** | `airpodsd` periodically looks for your paired AirPods and reconnects them |
| **Battery** | Reads the GATT battery service and shows the cells the firmware exposes (often left / right / case) |
| **Notifications** | Connect, disconnect and low-battery desktop notifications |
| **Audio profiles** | One command to switch between high-quality A2DP and microphone-capable HFP |
| **Diagnostics** | `airpodsctl doctor` checks BlueZ, rfkill, controller mode, PulseAudio modules and A2DP endpoints |
| **No root** | Runs entirely as your user; no udev rules, kernel modules or setuid helpers |
| **No pip dependencies** | Only the standard library plus the system `dbus-python` / PyGObject packages |
| **PulseAudio + PipeWire** | Uses `pactl`, which both servers implement |
| **Testable** | `--mock` backend works without any Bluetooth hardware; CI needs no devices |

## What works, and what does not

| Capability | Status | Notes |
|---|---|---|
| A2DP playback | ✅ | SBC on stock Ubuntu 22.04; codec availability depends on your distro |
| HFP microphone | ✅ | `airpodsctl audio hfp`; narrow-band speech quality is expected |
| Per-pod battery | ⚠️ | Best effort: GATT `0x180F` / `0x2A19`; cell order is configurable, some firmware exposes one aggregate |
| Auto-reconnect | ✅ | While the daemon is running |
| Low-battery notifications | ✅ | Thresholds configurable |
| AAC / LDAC | ⚠️ | Determined by BlueZ + PulseAudio/PipeWire; `airpodsd` does not bundle codecs |
| ANC / Transparency | ❌ | Needs Apple's proprietary AAP protocol (roadmap) |
| Ear detection | ❌ | Same; roadmap |
| Spatial audio / head tracking | ❌ | Apple-only today |
| Firmware updates | ❌ | Use an Apple device |
| Multipoint management | ❌ | AirPods connect to one host at a time |

## Requirements

- Linux with **BlueZ ≥ 5.55** (5.64+ recommended)
- **PulseAudio ≥ 13** or **PipeWire ≥ 0.3.48** (with `pipewire-pulse`)
- Python **3.9+**
- `python3-dbus`, `python3-gi`, `pactl` (`pulseaudio-utils`)

The original development environment was Ubuntu 22.04 / BlueZ 5.64 /
PulseAudio. This monorepo import is validated with headless tests; it does
not establish a physical-device compatibility matrix. PipeWire profile aliases
are covered by parser tests, not by a fresh PipeWire hardware test.

## Install

```bash
git clone https://github.com/jxmao-cn/linux-audio-toolkit.git
cd linux-audio-toolkit/headsets/airpods
./install.sh
```

`install.sh` installs the package for your user, writes
`~/.config/systemd/user/airpodsd.service`. It does not enable or start the
daemon unless `--enable` is specified. Use `--no-service` for the CLI only.
A different existing unit is preserved; `--replace-unit` explicitly backs it
up before replacement. Set `AIRPODSD_PYTHON` to choose the installation
interpreter, and use the same interpreter for uninstallation.

Manual alternative:

```bash
python3 -m pip install --user .
airpodsctl doctor
```

If your distribution blocks `pip install --user` (PEP 668), use a venv that can
see the system D-Bus bindings:

```bash
python3 -m venv --system-site-packages ~/.local/share/airpodsd/venv
~/.local/share/airpodsd/venv/bin/pip install .
~/.local/share/airpodsd/venv/bin/airpodsctl doctor

# install the systemd unit against the venv (activation is a separate step)
AIRPODSD_PYTHON=~/.local/share/airpodsd/venv/bin/python \
AIRPODSD_BIN=~/.local/share/airpodsd/venv/bin/airpodsd ./install.sh --no-install
```

Uninstall with `./uninstall.sh`.

## Pair your AirPods

1. Put both buds **in the case** and open the lid.
2. Put them in pairing mode, keeping the lid open:
   - **AirPods 4 / 4 ANC have no button:** double-tap the **front of the case,
     just below the LED** until the light flashes white.
   - **AirPods 1–3, Pro, Max:** press and hold the **case button** until the
     light flashes white.
3. Run:

```bash
airpodsctl pair
```

```text
Scanning for 30s...
Put the AirPods in pairing mode: open the case, keep the buds inside.
AirPods 1-3 / Pro / Max: hold the case button. AirPods 4 / 4 ANC: double-tap the front of the case below the LED.
Wait for the light to flash white.

Pairing with AirPods Pro (2nd generation) (AC:12:34:56:78:90)...
Paired. Connecting...
Connected.  AirPods Pro (2nd generation)
Audio profile: A2DP (SBC) · bluez_output.AC_12_34_56_78_90.1
Battery: L 82%   R 79%   Case 65%

Default device saved to /home/you/.config/airpodsd/config.json
```

The address is saved as the default, so later commands need no arguments.

```bash
airpodsctl connect       # reconnect
airpodsctl status        # link, audio profile, battery
airpodsctl battery       # battery with a small bar chart
airpodsctl disconnect
airpodsctl forget        # remove the pairing (for a clean reset)
```

## Run the daemon

```bash
systemctl --user daemon-reload
systemctl --user enable --now airpodsd
systemctl --user status airpodsd
journalctl --user -u airpodsd -f
```

The daemon:

- starts on login and waits for Bluetooth;
- scans for your AirPods every 2 minutes while they are disconnected;
- reconnects them when they appear;
- polls battery every minute and notifies at 20% / 10%;
- prefers A2DP after connecting (configurable);
- writes `~/.local/state/airpodsd/state.json` for status bars and scripts.

Pairing remains an explicit action: the daemon never accepts a new device on its
own.

## Audio profiles

```bash
airpodsctl audio          # current card/profile/sink/source
airpodsctl audio a2dp     # music: high quality, no microphone
airpodsctl audio hfp      # calls: microphone works, lower playback quality
```

PulseAudio's `module-bluetooth-policy` may switch to HFP automatically when an
application opens the microphone. `airpodsctl` simply gives you manual control
when it guesses wrong.

For a microphone-ready connection after each reconnect, set `audio_profile` to
`"hfp"` in `~/.config/airpodsd/config.json` and restart `airpodsd.service`.
The daemon selects the AirPods input as the default source, retrying if the
audio server has not created it yet. Both PulseAudio underscore names (such as
`handsfree_head_unit`) and PipeWire hyphenated names are supported. HFP reduces
playback quality compared with A2DP; use `"keep"` to leave profile choice to the
desktop instead.

## Diagnostics

```bash
airpodsctl doctor
```

```text
airpodsd doctor

[ OK ] Python — 3.10.12
[ OK ] Python D-Bus bindings — dbus-python and PyGObject are available
[ OK ] Bluetooth service — active
[ OK ] BlueZ — 5.64
[ OK ] Bluetooth adapter — hci0 (00:11:22:33:44:55)
[ OK ] rfkill — no blocks
[ OK ] Controller mode — default (dual)
[ OK ] Audio server — pulseaudio 15.99.1
[ OK ] Bluetooth audio modules — discover and policy modules are loaded
[ OK ] A2DP endpoints — the audio server registered an A2DP sink
[WARN] Paired AirPods — no AirPods are paired yet
       → put the AirPods in pairing mode and run `airpodsctl pair`
[ OK ] Configuration — /home/you/.config/airpodsd/config.json

0 failure(s), 1 warning(s)
```

Exit code is `1` when a real failure is found, which makes it useful in bug
reports and scripts.

## Configuration

`airpodsctl config path` prints the file;
`airpodsctl config init` creates it. Restart the daemon after editing.

```json
{
  "address": "AC:12:34:56:78:90",
  "adapter": null,
  "auto_connect": true,
  "scan_interval": 120,
  "scan_duration": 8,
  "connect_interval": 30,
  "battery_interval": 60,
  "battery_labels": ["left", "right", "case"],
  "notifications": true,
  "low_battery_threshold": 20,
  "critical_battery_threshold": 10,
  "audio_profile": "a2dp",
  "set_default_sink": false,
  "log_level": "info"
}
```

| Key | Default | Meaning |
|---|---|---|
| `address` | auto | Default AirPods MAC; set by `airpodsctl pair` |
| `adapter` | auto | Controller id (`hci0`) or address |
| `auto_connect` | `true` | Let the daemon reconnect |
| `scan_interval` | `120` | Seconds between background scans |
| `scan_duration` | `8` | Seconds per scan |
| `connect_interval` | `30` | Minimum seconds between connect attempts |
| `battery_interval` | `60` | Battery poll interval |
| `battery_labels` | `["left","right","case"]` | Labels for the GATT cells when more than one is exposed |
| `notifications` | `true` | Desktop notifications |
| `low_battery_threshold` | `20` | Warning threshold |
| `critical_battery_threshold` | `10` | Urgent threshold |
| `audio_profile` | `"a2dp"` | `a2dp`, `hfp` or `keep` after connecting |
| `set_default_sink` | `false` | Make AirPods the default output (HFP input selection is independent) |
| `log_level` | `"info"` | Daemon log level |

**Battery cells.** Many AirPods firmware revisions expose three *unlabelled*
GATT battery characteristics; some expose a single aggregate value (shown as
`Battery`). The default assumption for three cells is left, right, case. If your
unit reports them in another order, set `battery_labels` to match. Contributions
with hardware captures are welcome.

## How it works

```text
┌──────────────────────────────┐
│ airpodsctl / airpodsd        │
│ config · state · doctor      │
└──────────────┬───────────────┘
               │
      ┌────────┴─────────┐
      │                  │
┌─────▼──────┐    ┌──────▼─────────┐
│ BlueZ      │    │ PulseAudio /   │
│ system bus │    │ PipeWire       │
│            │    │ (pactl)        │
│ Agent1     │    │ card profiles  │
│ Device1    │    │ sinks/sources  │
│ GATT 0x180F│    │ default sink   │
└────────────┘    └────────────────┘
```

- Pairing registers a temporary `org.bluez.Agent1` that auto-confirms **only**
  the selected address, then pairs, marks the device trusted and connects.
- Battery is read from the standard GATT Battery Service (`0x180F`) /
  Battery Level characteristic (`0x2A19`), with a fallback to BlueZ's
  aggregated `org.bluez.Battery1`.
- Audio control parses `pactl list cards` and selects `a2dp-sink` or
  `headset-head-unit` from the profiles the card actually offers.
- The daemon's tick is a plain state machine with a fake clock in tests; GLib
  is only the outer event loop.

More detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Troubleshooting

| Symptom | First thing to try |
|---|---|
| AirPods not found | Case open, buds inside, white flash (AirPods 4: double-tap the front); `airpodsctl scan --all` |
| Pairing fails | Remove them from the iPhone's Bluetooth list first; `airpodsctl doctor` |
| Connected but no sound | `airpodsctl audio a2dp`, then check `pactl list cards` |
| Microphone missing | `airpodsctl audio hfp` |
| Battery unavailable | Take the buds out of the case; run `airpodsctl doctor` |
| Choppy audio | Move closer, disable 2.4 GHz congestion; try `a2dp-sink-sbc_xq` if offered |
| AirPods keep returning to iPhone | Turn off "Automatically connect" on the iPhone or disable Bluetooth there |
| Found but pairing fails on Ubuntu 22.04 | Try `airpodsctl pair --bredr`; if that fails, temporarily set `ControllerMode = bredr` — see [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |

Full guide: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Development

```bash
make test     # unit tests, no hardware needed
make demo     # airpodsctl --mock status
make lint
```

The test suite exercises battery parsing, `pactl` parsing, config validation,
the daemon state machine and the CLI against an in-memory backend.

See [CONTRIBUTING.md](CONTRIBUTING.md) if you want to add a model, improve the
battery mapping or implement AAP support.

## Roadmap

- **AAP over L2CAP** for exact per-pod battery, ear detection, ANC/transparency
  and adaptive audio.
- **Status-bar integrations** (Waybar module, KDE plasmoid, GNOME extension).
- **Automatic HFP switching** based on real microphone activity, not just
  PulseAudio policy.
- **Beats and other Apple audio devices.**
- **Packaging**: AUR, PPA, Nix flake.
- **Localisation** of the CLI.

## FAQ

**Does this need root?**
No. Install the distro packages once, then everything runs as your user.

**Is this a kernel driver?**
No, and it does not need to be. See [Why another AirPods project?](#why-another-airpods-project).

**Will it improve sound quality?**
No. It gives you control and visibility; the codec is negotiated by BlueZ and
your audio server.

**Does it work on Wayland?**
The implementation has no X11 dependency; individual desktops still need validation. The CLI and daemon do not depend on X11. Desktop notifications use the
freedesktop notification service.

**Can I use it without the daemon?**
Yes. `airpodsctl connect`, `status` and `battery` work on demand.

## License

MIT — see [LICENSE](LICENSE).

AirPods and Apple are trademarks of Apple Inc. This project is not affiliated
with or endorsed by Apple.

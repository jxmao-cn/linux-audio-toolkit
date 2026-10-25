# Architecture

`airpodsd` is deliberately small. It does not implement Bluetooth or audio; it
orchestrates the two services that already do, and adds the Apple-specific
policy that desktop Linux lacks.

## Goals and non-goals

**Goals**

- One-command, reliable first-time pairing.
- Reconnect without user interaction.
- Expose battery cells where the firmware provides them.
- Make A2DP/HFP switching explicit.
- Explain failures instead of silently doing nothing.
- Run unprivileged, with no pip-installed runtime dependencies.

**Non-goals**

- A kernel module or a new Bluetooth stack.
- Audio decoding, codec implementations or DSP.
- Apple account integration, firmware updates or iCloud features.
- A GUI. The CLI is scriptable and the daemon writes a state file.

## Process model

```text
┌─────────────────────────────┐        ┌─────────────────────────────┐
│ airpodsctl                  │        │ airpodsd                    │
│ short-lived, interactive    │        │ long-lived, per-user        │
│ pair / connect / status ... │        │ auto-connect / battery      │
└──────────────┬──────────────┘        └──────────────┬──────────────┘
               │                                      │
               └──────────────┬───────────────────────┘
                              │
                 ┌────────────┴────────────┐
                 │                         │
        ┌────────▼────────┐       ┌────────▼─────────┐
        │ BlueZ (D-Bus)   │       │ pactl            │
        │ system bus      │       │ PulseAudio /     │
        │                 │       │ PipeWire         │
        └─────────────────┘       └──────────────────┘
```

The CLI and daemon share the backend, audio, config, state and UI modules, but
run in separate processes. The daemon writes
`$XDG_STATE_HOME/airpodsd/state.json`; the CLI reads it only for the "last
daemon update" line and otherwise queries live state.

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing, output, command implementations |
| `daemon.py` | Auto-connect/battery state machine and GLib loop |
| `backend.py` | `Backend` protocol (the seam used by tests) |
| `bluez.py` | BlueZ D-Bus implementation |
| `agent.py` | `org.bluez.Agent1` pairing agent |
| `audio.py` | `pactl` parsing and profile/sink control |
| `doctor.py` | Environment checks |
| `config.py` | JSON config, validation, XDG paths |
| `state.py` | Atomic state-file writes |
| `models.py` | Dataclasses shared across layers |
| `mock.py` | In-memory backend + audio for demos and CI |
| `ui.py` | ANSI formatting, battery bars |

## Pairing sequence

```text
airpodsctl                 bluetoothd                 AirPods
    │                          │                        │
    │ StartDiscovery           │                        │
    │─────────────────────────>│                        │
    │                          │<──── advertisement ────│
    │ GetManagedObjects        │                        │
    │─────────────────────────>│                        │
    │<── Device1 (name, RSSI) ─│                        │
    │                          │                        │
    │ RegisterAgent(KeyboardDisplay)                    │
    │─────────────────────────>│                        │
    │ Pair(address)            │                        │
    │─────────────────────────>│──── SSP / Just Works ─>│
    │                          │<── RequestConfirmation │
    │<─────────────────────────│                        │
    │ confirm only if address matches the selection     │
    │─────────────────────────>│                        │
    │                          │──── pairing OK ───────>│
    │ Trusted = true           │                        │
    │─────────────────────────>│                        │
    │ Connect()                │                        │
    │─────────────────────────>│──── A2DP/HFP setup ───>│
    │                          │                        │
    │ wait for Connected=true  │                        │
    │ pactl set-card-profile   │                        │
```

Key details:

- The agent is registered **after** the user selects a device, and its callback
  compares the `dev_XX_...` object path with that address. All other
  confirmations are rejected.
- `Pair()` and `Connect()` are invoked asynchronously while a nested GLib main
  loop runs, because the agent's method calls must be dispatched by that same
  process. A synchronous blocking call would deadlock.
- After pairing, the address is written to the config so later commands target
  the right device even if several are nearby.
- The device is marked trusted so future reconnects do not require an agent.

## Audio control

`pactl` is used rather than libpulse to keep the dependency surface at zero and
to work identically on PipeWire's PulseAudio-compatible server.

- Bluetooth cards are named `bluez_card.<MAC with underscores>`.
- Sinks are `bluez_output.<MAC>.<n>` (PulseAudio ≥ 15) or
  `bluez_sink.<MAC>.<profile>` (older releases).
- Sources are `bluez_input.<MAC>` / `bluez_source.<MAC>.<profile>`.
- `parse_cards()` reads `Name`, `Profiles` and `Active Profile` and ignores
  ports/properties, which vary between versions.
- `choose_profile()` prefers the short stable names (`a2dp-sink`,
  `headset-head-unit`) over codec-suffixed variants.
- All `pactl` subprocesses run with `LC_ALL=C`, because field names are
  translated in localised environments.

The daemon applies the configured profile **once per connection**, so a manual
`airpodsctl audio hfp` is not immediately undone on the next tick.

## Battery

AirPods expose the standard GATT Battery Service:

- service `0000180f-0000-1000-8000-00805f9b34fb`
- characteristic `00002a19-0000-1000-8000-00805f9b34fb`

`BlueZBackend.battery()`:

1. waits up to 8 s for `ServicesResolved`;
2. enumerates all battery services/characteristics below the device path;
3. sorts characteristics by object path and calls `ReadValue({})` on each;
4. falls back to `org.bluez.Battery1.Percentage` if no GATT cell is readable;
5. labels the cells using `battery_labels` (default left, right, case), or
   labels a single value as `battery`.

The protocol does not label the cells, and the order can differ between
firmware versions; some revisions expose only one aggregate value. This is why
the labels are configuration, not a hard-coded fact. A future AAP implementation
can replace the heuristic with exact Apple metadata.

## Daemon state machine

```text
             ┌──────────────┐
             │   no device  │
             └──────┬───────┘
                    │ scan interval elapsed
                    ▼
             ┌──────────────┐   device appears
             │   scanning   │──────────────────┐
             └──────┬───────┘                  │
                    │ scan window over         ▼
                    │                  ┌──────────────┐
                    │                  │  connecting  │
                    │                  └──────┬───────┘
                    │                         │ Connected=true
                    │                         ▼
                    │                  ┌──────────────┐
                    │                  │  connected   │
                    │                  │ battery tick │
                    │                  │ profile once │
                    │                  └──────┬───────┘
                    │                         │ disconnected
                    └─────────────────────────┘
```

Implementation notes:

- `tick()` is a pure-ish method with an injected clock; unit tests drive it
  without sleeping.
- A tick runs every 2 s. Battery is polled every `battery_interval` seconds,
  scans start every `scan_interval` seconds and last `scan_duration` seconds.
- Connect attempts are throttled by `connect_interval` so a sleeping/absent
  device does not cause a tight loop.
- Notifications are emitted on connect/disconnect transitions and when a cell
  crosses the low/critical thresholds. Threshold notifications are suppressed
  until the level recovers by 5 percentage points.
- State is written only when the serialisable payload changes, so status-bar
  watchers do not wake up every 2 s for nothing.

## D-Bus surface

| Interface | Used for |
|---|---|
| `org.freedesktop.DBus.ObjectManager` | Enumerate adapters/devices/GATT objects |
| `org.bluez.Adapter1` | Power, discovery filter, start/stop discovery, remove device |
| `org.bluez.Device1` | Pair, connect, disconnect, Trusted, RSSI, ServicesResolved |
| `org.bluez.AgentManager1` | Register/unregister the pairing agent |
| `org.bluez.Agent1` | Confirmation/authorization callbacks |
| `org.bluez.GattService1` | Find the battery service |
| `org.bluez.GattCharacteristic1` | `ReadValue` for battery level |
| `org.bluez.Battery1` | Aggregated battery fallback |

No private D-Bus interfaces or raw HCI sockets are used.

## State file

```json
{
  "version": 1,
  "updated_at": "2026-10-18T07:40:12.123456+00:00",
  "daemon": { "pid": 4242, "started_at": "..." },
  "device": {
    "address": "AC:12:34:56:78:90",
    "name": "AirPods Pro",
    "model": "AirPods Pro",
    "connected": true,
    "paired": true,
    "trusted": true,
    "rssi": -48,
    "adapter": "/org/bluez/hci0",
    "is_airpods": true,
    "apple": true
  },
  "battery": {
    "source": "gatt",
    "lowest": 65,
    "cells": [
      { "label": "left", "level": 82 },
      { "label": "right", "level": 79 },
      { "label": "case", "level": 65 }
    ]
  },
  "audio": {
    "card": "bluez_card.AC_12_34_56_78_90",
    "profile": "a2dp-sink",
    "codec": "SBC",
    "sink": "bluez_output.AC_12_34_56_78_90.1",
    "source": "bluez_input.AC_12_34_56_78_90"
  },
  "discovering": false
}
```

Writes use a temporary file + `os.replace`, so readers never observe a partial
JSON document. The daemon removes the file on clean shutdown.

## Error handling and security

- All user-facing failures are `AirPodsError` with an optional `hint`; the CLI
  prints them without a traceback.
- The pairing agent rejects every device except the selected address.
- The daemon does not register a default agent and never pairs on its own.
- No shell interpolation: `pactl` is invoked with an argument vector and a
  fixed environment.
- Configuration files are written with mode `0600`; the state file is `0644`.

## Testing strategy

- Pure functions (model detection, config validation, `pactl` parsing, rfkill
  and `main.conf` parsing) have direct unit tests.
- `MockBackend`, `MockAudio` and a fake clock exercise the daemon state machine
  and the CLI end to end.
- The real BlueZ backend is import-safe without `dbus-python`; its parsing
  helpers are tested with plain dictionaries.
- CI runs on Python 3.9–3.12 without Bluetooth hardware.

## Extending

- **New AirPods model**: add a regex to `MODEL_PATTERNS` in
  `constants.py` and a test case.
- **Different backend**: implement the `Backend` protocol in `backend.py`.
- **AAP support**: add a new module that opens the L2CAP PSM `0x1001` channel
  and expose it through `battery()`; keep the GATT fallback.
- **GUI/status bar**: read `state.json` or call `airpodsctl status --json`.

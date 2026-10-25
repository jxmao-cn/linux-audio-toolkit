# Architecture

```text
linux-audio-toolkit/
├── voice-input/           # Independent Python application and configuration
├── headsets/
│   ├── gm400/             # PulseAudio watcher and systemd user unit
│   └── airpods/           # BlueZ / pactl CLI and user daemon
├── docs/
└── .github/workflows/
```

All three modules are included. Importing source does not migrate an installed service. CI validates software behavior; physical-device acceptance is recorded separately.

## Module boundary

Headset modules use the system audio stack to make sinks/sources available. Voice Input selects a PulseAudio source (or the system default), captures speech, and runs the ASR/postprocessing/input pipeline. Neither module imports the other or needs it installed. Do not introduce a shared daemon solely to combine repositories.

Device priority remains an explicit user choice: the GM400 watcher may select its source/sink as system defaults, while Voice Input can select a configured preferred source. AirPods defaults to A2DP without changing the default output (`set_default_sink=false`). When explicitly configured for HFP, it selects its input as the system default after the source appears. GM400 periodically selects its own defaults, so running both automatic policies can restore GM400 as default after AirPods has selected its source. While using AirPods, stop the GM400 watcher or choose a dedicated preferred source in Voice Input. This repository does not yet provide a shared device-priority coordinator.

## Headset integration contract

1. Keep each headset implementation and its dependency/install metadata in its own `headsets/` directory.
2. Retain its license and attribution. Exclude personal documents, real Bluetooth addresses, local state, pairing records and logs.
3. Keep configuration and service names independent. Use user-relative or installer-resolved paths rather than developer home paths.
4. Installation must not automatically replace a working service or switch profiles without an explicit action.
5. Add hardware-independent tests to root CI. Document physical model, audio backend and desktop validation separately.
6. Expose A2DP/HFP changes through the system audio stack rather than importing Voice Input internals.

## Evidence boundary

Headless tests cover program logic, configuration and simulated recovery. They do not validate USB firmware timing, Bluetooth pairing, recognition accuracy, end-to-end latency, or clipboard behavior under every Wayland compositor.

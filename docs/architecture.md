# Architecture and AirPods integration

```text
linux-audio-toolkit/
├── voice-input/           # Independent Python application and configuration
├── headsets/
│   ├── gm400/             # PulseAudio watcher and systemd user unit
│   └── airpods/           # Future location; not shipped yet
├── docs/
└── .github/workflows/
```

AirPods is being developed separately. This release includes no AirPods implementation, service migration or claim of physical AirPods acceptance.

## Module boundary

Headset modules use the system audio stack to make sinks/sources available. Voice Input selects a PulseAudio source (or the system default), captures speech, and runs the ASR/postprocessing/input pipeline. Neither module imports the other or needs it installed. Do not introduce a shared daemon solely to combine repositories.

Device priority remains an explicit user choice: the GM400 watcher may select its source/sink as system defaults, while Voice Input can select a configured preferred source. A future AirPods module must document whether it changes defaults; simultaneous default-changing services need a user-selected policy rather than competing silently.

## Future AirPods contribution

1. Place the implementation and its dependency/install metadata under `headsets/airpods/`.
2. Retain its license and attribution. Exclude personal documents, Bluetooth addresses, local state, pairing records and logs.
3. Keep configuration and service names independent. Use user-relative or installer-resolved paths rather than developer home paths.
4. Installation must not automatically replace a working service or switch profiles without an explicit action.
5. Add hardware-independent tests to root CI. Document physical model, audio backend and desktop validation separately.
6. Expose A2DP/HFP changes through the system audio stack rather than importing Voice Input internals.

## Evidence boundary

Headless tests cover program logic, configuration and simulated recovery. They do not validate USB firmware timing, Bluetooth pairing, recognition accuracy, end-to-end latency, or clipboard behavior under every Wayland compositor.

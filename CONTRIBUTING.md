# Contributing

Keep headset modules independently installable and Voice Input independent of a specific headset. Add regression tests for changed behavior, using stubs for system services and external providers.

Run the headless tests described in each module. Do not run hardware recovery, microphone capture or clipboard injection in ordinary CI. Report hardware validation separately with device, distribution, desktop and audio backend details.

Do not include personal YAML configuration, credentials, model weights, recordings, logs, Bluetooth addresses or local state. Share a redacted example and the smallest reproducible case instead.

Contributions are made under the repository MIT license unless a module carries additional third-party licensing.

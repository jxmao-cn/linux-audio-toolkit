# Migration notes

## Voice input

The original `jxmao-cn/linux-voice-input` repository is the provenance of the voice component. This monorepo combines distributable project files with subsequent local work, including whole-utterance accumulation, regression tests and session replay. Existing deployment fixes and example configuration are retained during synchronization.

The original working installation is not moved or overwritten. After checking the new checkout, users can copy their own configuration and download or reuse existing models locally. Never commit those files.

Do not run two voice daemons or dashboards concurrently: they use the same default PID paths, hotkeys and dashboard port. Stop the old instance explicitly before starting another checkout. Publication does not perform that switch.

The former standalone repository remains available. New combined development belongs here; no archival or destructive change to the original repository is part of this migration.

## GM400

The module packages an existing user-level watcher and makes installation reproducible. It does not add a kernel driver. Follow the module README to inspect or replace an existing installation; publication does not update the installed service.

## AirPods

AirPods is now included under `headsets/airpods/`. The import uses the tracked project files from the original local `airpodsd` repository at baseline commit `de42859`, plus its subsequent working-tree fixes for PulseAudio profile aliases and delayed HFP input creation. Personal documents, local pairing/configuration/state, logs, build artifacts and unrelated untracked files are excluded. Its MIT license is retained in the module.

The original source checkout and installed daemon are retained. Installing the monorepo copy is a separate user action. The installer defaults to writing files without enabling the service; replacement of a different existing unit requires `--replace-unit` and keeps a backup. Use the same Python environment for installation and uninstallation. See the [AirPods README](../headsets/airpods/README.md) and [architecture](architecture.md).

# Migration notes

## Voice input

The original `jxmao-cn/linux-voice-input` repository is the provenance of the voice component. This monorepo combines distributable project files with subsequent local work, including whole-utterance accumulation, regression tests and session replay. Existing deployment fixes and example configuration are retained during synchronization.

The original working installation is not moved or overwritten. After checking the new checkout, users can copy their own configuration and download or reuse existing models locally. Never commit those files.

Do not run two voice daemons or dashboards concurrently: they use the same default PID paths, hotkeys and dashboard port. Stop the old instance explicitly before starting another checkout. Publication does not perform that switch.

The former standalone repository remains available. New combined development belongs here; no archival or destructive change to the original repository is part of this migration.

## GM400

The module packages an existing user-level watcher and makes installation reproducible. It does not add a kernel driver. Follow the module README to inspect or replace an existing installation; publication does not update the installed service.

## AirPods

The reserved path is `headsets/airpods/`. No code from its separate working directory is copied in this release. Follow the [integration contract](architecture.md) once that work is ready.

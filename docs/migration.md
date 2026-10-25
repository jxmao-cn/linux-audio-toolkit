# Migration notes

## Which voice input version is included?

The voice module combines two pre-existing working directories. Its feature source is the author's daily-use version, which continued to receive changes after the standalone public release.

| Source or destination | Role in the migration |
| --- | --- |
| The original daily-use directory named `voice-input`, outside this repository | Source of the newer capture pipeline, whole-utterance dictation, accumulator tests and session replay tool |
| The former standalone `linux-voice-input` checkout, at `47264ac` | Earlier public release; source of the release history, example configuration, deployment initialization and desktop integration |
| This repository's `voice-input/` | Combined, maintained module: newer daily-use features, retained deployment improvements, plus the integration-time safety fix |

In the combined version, uninterrupted speech is checked against the hard safety limit before the soft duration target. This fixes a branch-order problem in the daily-use accumulator, where continuous speech could prevent the hard-limit branch from running. Its regression test verifies that consecutive emitted segments contain the original samples without gaps or duplication.

The audio capture implementation and replay tool were imported from the daily-use directory. Public defaults use the system microphone and disable LLM correction until configured. Personal settings, model files, logs and recordings are kept outside version control.

The initial combination is recorded in commit `fc04241`. The standalone project has been retired; its release history remains in this repository. Ongoing combined development belongs here.

### Switching a local installation

The daily-use directory is still a separate copy. This repository does not link to it or automatically synchronize later changes. Changes made to that older directory must be explicitly ported; publishing this repository did not change which program an existing installation launches.

To switch to this checkout, prepare its dependencies, copy personal configuration locally, and reuse or download the required models. Stop the previous daemon and dashboard before starting the new ones: both checkouts use the same default PID paths, hotkeys and dashboard port. Personal configuration and models must remain untracked.

## GM400

The module packages an existing user-level watcher and makes installation reproducible. It does not add a kernel driver. Follow the module README to inspect or replace an existing installation; publication does not update the installed service.

## AirPods

AirPods is now included under `headsets/airpods/`. The import uses the tracked project files from the original local `airpodsd` repository at baseline commit `de42859`, plus its subsequent working-tree fixes for PulseAudio profile aliases and delayed HFP input creation. Personal documents, local pairing/configuration/state, logs, build artifacts and unrelated untracked files are excluded. Its MIT license is retained in the module.

The original source checkout and installed daemon are retained. Installing the monorepo copy is a separate user action. The installer defaults to writing files without enabling the service; replacement of a different existing unit requires `--replace-unit` and keeps a backup. Use the same Python environment for installation and uninstallation. See the [AirPods README](../headsets/airpods/README.md) and [architecture](architecture.md).

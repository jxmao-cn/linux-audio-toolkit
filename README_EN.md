# Linux Audio Toolkit

[简体中文](README.md) · [MIT License](LICENSE)

This repository grew out of a few everyday problems on my Linux desktop: getting a wired headset ready for both playback and recording, making my AirPods easier to connect and switch between listening and microphone use, and speaking into a headset to enter text in the focused window.

**HECATE GM400 is the model name of the wired headset I use.** It connects to this computer through USB. The GM400 module addresses discovery and audio recovery for that headset. The AirPods module handles my Bluetooth earbuds, while Voice Input turns an available microphone into a desktop dictation tool.

I built these tools to make my own computer more convenient to use. Sharing the programs, scripts and troubleshooting notes may help others with similar setups reuse them or adapt them to their own devices.

## What each tool helps with

| Everyday situation | Tool | What it does |
| --- | --- | --- |
| A wired headset is plugged in, but the desktop has no usable playback or recording endpoint | [HECATE GM400 wired headset support](headsets/gm400/README_EN.md) | Waits for USB audio initialization and helps PulseAudio discover or recover the device |
| I want to use AirPods for listening or speaking on Linux, with convenient connection and profile controls | [AirPods management](headsets/airpods/README.md) | Provides pairing, connection, battery and A2DP/HFP controls, plus an optional reconnect daemon |
| I want spoken words to appear in the editor, terminal or chat window I am using | [Linux Voice Input](voice-input/README_EN.md) | Supports push-to-talk and continuous dictation, then pastes recognized text into the focused window |

Each module can be used independently. Voice Input can use the system microphone without either headset module. The GM400 recovery script is specific to my headset and PulseAudio setup; other headsets need their own configuration and validation.

## Start with your use case

```bash
git clone https://github.com/jxmao-cn/linux-audio-toolkit.git
cd linux-audio-toolkit
```

- **For dictation**, follow the [Voice Input guide](voice-input/README_EN.md) to prepare the environment and models, then start the hotkey listener.
- **For a GM400 discovery problem**, read the [wired headset guide](headsets/gm400/README_EN.md), check the symptoms and audio backend, then install the recovery service if appropriate.
- **For AirPods**, follow the [installation and pairing guide](headsets/airpods/README.md) to prepare BlueZ, audio tools and Python.

Headset services require explicit activation after installation. When both headsets are connected, choose the default input/output policy you want. The modules do not yet share a device-priority coordinator; see [architecture](docs/architecture.md).

## Which voice input version is included?

The `voice-input/` module incorporates continuous dictation, whole-utterance accumulation and replay tools from my daily-use version, while retaining configuration initialization, installation and desktop integration improvements from the earlier standalone release. Integration also fixed the safety time limit for uninterrupted speech.

Voice input is now maintained in this combined repository. The former standalone project has been retired, and its commit history is preserved here. See [migration notes](docs/migration.md) for the source relationship.

## Configuration and personal data

Initialize personal settings from `voice-input/config.example.yaml`. Keep keys, models, logs, recordings and Bluetooth state on your machine.

ASR and rule-based cleanup can run locally; LLM correction is disabled by default. Enabling a cloud backend or OpenCode CLI sends recognized text to the selected service. The control panel defaults to loopback (`127.0.0.1`).

## Development and validation

Run each command from the repository root, with the dependencies described by its module:

```bash
# Voice Input: Python 3.10+
(cd voice-input && python -m unittest discover -s tests -v && python tests/test_utterance_accumulator.py)

# GM400: simulated audio devices
headsets/gm400/tests/run.sh

# AirPods: Python 3.9+
(cd headsets/airpods && PYTHONPATH=src python -m unittest discover -s tests -t . -v)
```

CI covers program logic, installation and packaging. Physical headsets, firmware, desktops and audio backends still require hardware validation. Each module documents its current scope.

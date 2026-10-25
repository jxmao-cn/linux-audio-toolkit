# Linux Audio Toolkit

[简体中文](README.md) · [MIT License](LICENSE)

A modular Linux desktop toolkit for headset integration and voice typing. Headset modules expose audio devices to the system; Voice Input captures speech, transcribes it, and pastes text into the focused window. Install modules independently.

| Module | Purpose | Status |
| --- | --- | --- |
| [Voice Input](voice-input/README_EN.md) | Local SenseVoice ASR, push-to-talk, continuous dictation, optional correction and control panel | Includes subsequent local changes, whole-utterance accumulation and offline replay |
| [HECATE GM400](headsets/gm400/README_EN.md) | PulseAudio discovery and recovery after delayed USB initialization | Watcher, user service and installation tools included |
| AirPods | Bluetooth pairing, connection and audio profiles | Not included yet; see the [integration contract](docs/architecture.md) |

## Quick start

```bash
git clone https://github.com/jxmao-cn/linux-audio-toolkit.git
cd linux-audio-toolkit/voice-input
./setup.sh
./run.sh start
```

See the [GM400 module](headsets/gm400/README_EN.md) for installation and recovery side effects. Voice Input also works independently with a system microphone.

## Configuration and privacy

Personal configuration, API keys, model weights, recordings and logs are excluded from Git. Start from `voice-input/config.example.yaml`.

LLM correction is disabled by default. ASR and rules run locally; cloud correction or OpenCode CLI sends recognized text to the selected provider. The entire pipeline is then no longer offline.

The control panel defaults to loopback (`127.0.0.1`) and can manage local processes and optional model containers. Use it only in a trusted local session. The GM400 watcher can change default audio devices and perform the recovery actions documented by that module.

## Validation

```bash
cd voice-input
python -m unittest discover -s tests -v
python tests/test_utterance_accumulator.py
cd ../headsets/gm400
./tests/run.sh
```

Use a Python 3.10+ development environment with the module dependencies installed. Headless tests require neither a microphone nor ASR models. CI validates offline logic; physical devices and desktop/backend combinations require separate hardware testing.

The voice component originates from [linux-voice-input](https://github.com/jxmao-cn/linux-voice-input) and includes subsequent local work. See [migration notes](docs/migration.md) and [architecture](docs/architecture.md).

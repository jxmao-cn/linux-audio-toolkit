# Linux Voice Input

A local speech input tool for Linux desktops with push-to-talk (PTT) and continuous dictation (Toggle). Speech recognition uses a local `sherpa-onnx` / SenseVoice model. Recognized text can pass through rule-based cleanup and, when configured, an OpenAI-compatible local or remote service.

## Continuous dictation

In Toggle mode, VAD marks speech regions. Pauses shorter than the commit threshold remain inside the same audio segment. Once silence reaches `audio.commit_silence_duration`, the accumulated audio is submitted to ASR as a whole. `audio.max_utterance_seconds` is a soft target for splitting at a natural pause: after it is reached, audio is split at the next available pause. If speech continues without a pause, a 60-second hard safety limit forces a submission. Pressing the Toggle key again stops recording and submits any remaining valid audio.

Key settings in the `audio` section of `config.yaml`:

| Setting | Default | Purpose |
|---|---:|---|
| `min_silence_duration` | `0.6` s | VAD pause granularity |
| `commit_silence_duration` | `1.6` s | Silence required before submitting accumulated audio |
| `max_utterance_seconds` | `20.0` s | Soft target; split at a natural pause, with a 60-second hard safety limit for uninterrupted speech |
| `session_dump_dir` | empty | Optional directory for WAV debug recordings; empty disables recording dumps |

## Install and run

This project lives in the `voice-input/` directory at the monorepo root. From the `linux-audio-toolkit` repository root:

```bash
cd voice-input
./setup.sh
./run.sh start
```

If `config.yaml` does not exist, `setup.sh` initializes it from `config.example.yaml`; an existing config is retained. Models and system dependencies must be prepared for the target Linux distribution. Audio capture, desktop text injection, and Wayland behavior depend on the host environment; no specific hardware or Wayland setup is claimed as verified here.

Common commands:

```bash
./run.sh status
./run.sh stop
./run.sh test-unit
./run.sh test
./run.sh dashboard
```

`test-unit` runs logic tests without a microphone or model. `test` runs an end-to-end self-check and may access the current audio or desktop environment.

## Configuration and privacy

`config.example.yaml` is the tracked template. Keep real API credentials only in the local `config.yaml` and do not commit that file. Session audio dumping is disabled by default; WAV files are written only when `audio.session_dump_dir` is explicitly configured.

## License

MIT; see the repository root `LICENSE`.

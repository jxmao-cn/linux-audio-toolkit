# Linux Voice Input: dictate into the focused window

I built this for everyday text entry on Linux: while wearing a headset, I want to hold a key, say something, and have the text appear in my editor, terminal or chat window. For longer dictation, a brief pause should not split a sentence into fragments too early.

This is the speech-to-text part of the repository. The headset modules handle device access; Voice Input uses an available system microphone and can run independently of either headset model.

## What a session looks like

- **Push-to-talk**: hold Right Shift or F9, speak, then release to recognize and paste the whole segment into the focused window.
- **Continuous dictation**: press Right Ctrl to start. Short pauses remain inside a segment; longer silence triggers recognition. Press again to stop and submit the remaining audio.
- **Optional correction**: local SenseVoice recognition is followed by rule-based cleanup. A local or cloud LLM can be configured if wanted; LLM correction is disabled by default.

Keys are configurable. Text is pasted through the clipboard using the appropriate shortcut for terminals or other applications; behavior depends on the desktop and target application.

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

### Start on login (optional)

To avoid running `./run.sh start` after every login, install an XDG autostart entry:

```bash
./run.sh install-autostart     # writes ~/.config/autostart/voice-input.desktop
./run.sh uninstall-autostart   # removes it
```

The installer renders `@PROJECT_DIR@` in `voice-input-autostart.desktop.in` to the project's absolute path, so reinstall after moving the repository. `run.sh autostart` waits for the login session and audio service, retries on failure, and records the outcome in `autostart.log`. The entry relies on X11 global hotkeys and the session audio service; it targets X11 desktops such as GNOME on Xorg.

## Configuration and privacy

`config.example.yaml` is the tracked template. Keep real API credentials only in the local `config.yaml` and do not commit that file. Session audio dumping is disabled by default; WAV files are written only when `audio.session_dump_dir` is explicitly configured.

## License

MIT; see the repository root `LICENSE`.

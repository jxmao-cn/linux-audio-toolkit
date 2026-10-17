# linux-voice-input — Low-Latency Offline Voice Input for the Linux Desktop

[简体中文](README.md) | [English](README_EN.md)

[![Python 3.10+](assets/badge_python.svg)](setup.sh)
[![Platform](assets/badge_platform.svg)](voice_input/text_injector.py)
[![ASR](assets/badge_asr.svg)](voice_input/asr_engine.py)
[![License: MIT](assets/badge_license.svg)](LICENSE)

> Low-latency offline voice input for the Linux desktop, optimized for Chinese and
> Chinese–English code-switching. Supports push-to-talk (PTT) and continuous dictation (Toggle),
> commits recognized text to the focused window via clipboard transactions, and provides a
> four-tier pluggable post-processing and homophone-correction chain (automatic fallback that
> never blocks screen commit) plus a Web control center.

---

## Contents

- [Features](#features)
- [Ecosystem Survey: State and Gaps of Linux Voice Input](#ecosystem-survey-state-and-gaps-of-linux-voice-input)
- [Head-to-Head Comparison (9 Dimensions)](#head-to-head-comparison-9-dimensions)
- [Core Technical Advantages](#core-technical-advantages)
- [System Architecture](#system-architecture)
- [Dual-Key State Machine and Misfire Protection](#dual-key-state-machine-and-misfire-protection)
- [4-Tier Post-Processing and LLM Correction](#4-tier-post-processing-and-llm-correction)
- [Web Control Center](#web-control-center)
- [Quick Start](#quick-start)
- [CLI Cheat Sheet](#cli-cheat-sheet)
- [Configuration Reference](#configuration-reference)
- [Troubleshooting](#troubleshooting)
- [Repository Layout](#repository-layout)
- [License and Acknowledgements](#license-and-acknowledgements)

---

## Features

- **Offline-first Chinese ASR engine**: runs `SenseVoice-Small (INT8)` on `sherpa-onnx` with pure-CPU
  inference (~100ms per utterance), zero GPU VRAM, built-in punctuation restoration and
  inverse text normalization (ITN).
- **Four-tier pluggable post-processing**: Tier A (deterministic rules, 0ms) / Tier B (local OpenAI-compatible
  backend) / Tier C (cloud OpenAI-compatible API) / Tier D (free tier via OpenCode CLI); any backend
  timeout or failure silently falls back within milliseconds and never blocks screen commit.
- **Input-method agnostic**: works alongside existing IBus or Fcitx5 setups without replacing them;
  supports both X11 and Wayland.
- **Window-aware clipboard-transaction commit**: distinguishes terminal emulators from regular GUI apps and
  injects `Ctrl+Shift+V` / `Ctrl+V` accordingly, then restores the user's original clipboard content.
- **Dual-key interaction**: physically separated PTT and Toggle keys with hold threshold, combo exemption,
  repeat filtering, pre-roll buffering, and VAD-based auto-segmentation.
- **Web control center**: backend implemented with the Python standard library only (zero third-party
  dependencies); covers live resource monitoring, hot tier switching, container control, connectivity
  diagnostics, and log viewing.
- **Rootless, relocatable deployment**: packaged around the `conda-forge` toolchain; `./setup.sh` completes
  environment, dependency, and model setup in one pass. All relative paths resolve against the config
  file's directory.

---

## Ecosystem Survey: State and Gaps of Linux Voice Input

Based on a structural review of 8 representative open-source projects on GitHub, the current Linux
voice-input ecosystem falls into four schools. Star counts are approximate at the time of survey and
serve only as an indicator of community attention, not quality.

### School 1: Early Minimalist Command-Line Tools

Representative: `ideasman42/nerd-dictation` (~1.9k stars).

These tools aim at minimal viable dictation. They typically build on previous-generation lightweight
models (Vosk / Kaldi family), ship no persistent daemon or voice-activity-detection (VAD) segmentation,
and commit text via `xdotool type` keystroke injection. That path handles multi-byte (CJK) characters
poorly — garbling or dropping characters in Chinese — and offers high word error rates with no
post-processing correction chain.

### School 2: Whisper Hotkey Utilities and Standalone Newcomers

Representatives: `savbell/whisper-writer` (~1.1k stars), `VocaHQ/vocalinux` (~945 stars),
`pavlenkosa/wayvoice` (~80 stars).

These projects are architecturally lightweight: a global hotkey triggers recording, a Whisper-family
model transcribes, and the result is pasted. Their shared constraint is the strong binding to the
Whisper model family:

- large models (e.g. `large-v3`) consume several GB of VRAM, competing with local training jobs;
- small models show a sharp accuracy drop in Chinese and tend to hallucinate repeated text on silence;
- dedicated post-processing for Chinese homophones and mixed-language layout is generally absent.

### School 3: Heavy Multi-Feature Desktop Clients

Representatives: `mkiol/dsnote` (Speech Note, ~1.7k stars), `zugaldia/speedofsound` (~210 stars),
along with peers such as `Buzz` / `Vibe`.

These clients cover broad functionality, some supporting 100+ languages plus translation and subtitling,
but their design center is offline audio-file or note transcription rather than a global low-latency
"dictate where the cursor is" input loop. Implementations commonly depend on heavy GUI runtimes
(full Qt stack, JVM + GTK4), with correspondingly larger memory footprints and startup costs that suit
transcription apps better than an always-on input daemon.

### School 4: Ecosystem-Coupled Solutions

Representatives: `xifan2333/fcitx5-vinput` (~574 stars), `goodroot/hyprwhspr` (~1.2k stars).

These solutions excel inside their target ecosystem at the cost of portability:

- `fcitx5-vinput` is a Fcitx5 input-method plugin with mature Chinese behavior, but it requires the
  Fcitx5 framework — stock Ubuntu IBus sessions or plain-English setups cannot use it directly;
- `hyprwhspr` is tuned for the Hyprland / wlroots compositor, with higher reuse cost on other desktops
  (GNOME / KDE / X11), and generally offers no local-plus-cloud multi-tier LLM correction or Web console.

In short, the ecosystem shows a clear gap: **Chinese and code-switched quality, zero-VRAM offline
inference, input-method-independent commit transactions, multi-tier LLM correction with fallback
guarantees, and graphical administration** rarely coexist in one project. That gap defines this
project's design point.

---

## Head-to-Head Comparison (9 Dimensions)

The table compares this project against the 8 representatives above. Stars are approximate at survey
time; cells summarize each project's public documentation and default configuration, and behavior may
vary with model choice and version.

| Project | Form and stars | Core ASR engine | Chinese and code-switching | VRAM and latency | Input-method / desktop decoupling | Trigger interaction | Commit mechanism and terminal support | LLM correction and fallback | Web control panel |
|---------|---------------|-----------------|---------------------------|------------------|----------------------------------|--------------------|--------------------------------------|----------------------------|-------------------|
| **linux-voice-input (this project)** | Lightweight daemon + Web panel | `sherpa-onnx` + `SenseVoice-Small` (INT8) | Optimized for Chinese and CJK-English term mixing; built-in punctuation + ITN; no silence hallucination | **0 VRAM** (pure CPU, ~100ms/utterance on 4 threads) | Fully decoupled from IBus / Fcitx5; X11 + Wayland | Dual independent keys: PTT (hold-to-talk) + Toggle (continuous) with combo-key guards | Clipboard transaction: `Ctrl+V` for GUI, `Ctrl+Shift+V` for terminals; restores clipboard after ~350ms | **4 pluggable tiers (A/B/C/D)** with hard-timeout silent fallback; never blocks commit | **Yes** (stdlib-only: status / tiers / containers / diagnostics / logs) |
| [ideasman42/nerd-dictation](https://github.com/ideasman42/nerd-dictation) (~1.9k★) | Minimal Python CLI script | Vosk (Kaldi-family) | High Chinese word error rate; no code-switching or punctuation optimization | CPU-bound; lightweight but accuracy-limited | IM-independent; requires external keybinding setup | External CLI `begin` / `end` calls | `xdotool type` / `wtype` keystroke simulation; prone to CJK garbling/drops | None (Python string hook only) | None |
| [mkiol/dsnote](https://github.com/mkiol/dsnote) (~1.7k★) | Heavy C++/Qt desktop suite (Speech Note) | Vosk / `whisper.cpp` / Faster-Whisper | Depends on large models; not specialized for code-switching | Multi-GB VRAM for large models; heavy client footprint | Standalone app; focused on in-app notes rather than global dictation | GUI button or hotkey recording | Lands in in-app notepad / manual copy-paste; lacks seamless global cursor injection | None (offline machine translation only) | None (native Qt UI) |
| [goodroot/hyprwhspr](https://github.com/goodroot/hyprwhspr) (~1.2k★) | Python systemd service + OSD | Parakeet TDT / Whisper / Qwen3-ASR / Cloud | Strong Chinese when configured with Qwen3-ASR; English-first defaults | Depends on chosen model; GPU/CPU selectable | Tightly tuned for Hyprland / wlroots compositors | Single hotkey trigger (`Super+Alt+D` default) | `wtype` / `xdotool` + clipboard paste | Weak (word replacement & prompts; no multi-tier LLM pipeline) | None (Waybar integration) |
| [savbell/whisper-writer](https://github.com/savbell/whisper-writer) (~1.1k★) | Python + PyQt5 hotkey utility | `faster-whisper` / OpenAI API | Higher Chinese CER on Whisper; prone to silence hallucinations | Multi-GB VRAM for `large-v3`; weak Chinese on small models | IM-independent; global hotkeys limited under Wayland | Single hotkey (choose 1 of 4 modes) | `pynput` / `pyautogui` character-by-character typing; fragile for CJK | None | None (PyQt settings dialog) |
| [VocaHQ/vocalinux](https://github.com/VocaHQ/vocalinux) (~945★) | Python desktop tray app | `whisper.cpp` / Faster-Whisper / Vosk | Generic Whisper models; no Chinese homophone specialization | VRAM-heavy for large models; lower accuracy on small models | Standalone tray app; broad distro packaging | Hold `Right Alt` to talk | Multi-backend (`xdotool` / IBus / `wtype` / `ydotool` / clipboard) | None | None (GTK tray settings) |
| [xifan2333/fcitx5-vinput](https://github.com/xifan2333/fcitx5-vinput) (~574★) | C++/Qt6 Fcitx5 input-method plugin | Local `sherpa-onnx` (SenseVoice) + Cloud ASR | Mature Chinese and code-switching accuracy | Lightweight on CPU | **Strictly coupled to Fcitx5** (unusable on stock Ubuntu IBus sessions) | `Alt_R` single-key tap/hold dual mode + command mode | Native Fcitx5 `commit_text` (within Fcitx5 only) | Yes (scene-based LLM rewriting & polishing) | None (Qt6 config dialog) |
| [zugaldia/speedofsound](https://github.com/zugaldia/speedofsound) (~210★) | Kotlin + GTK4/libadwaita app | `sherpa-onnx` (Whisper / Parakeet / Canary) | Built-in multilingual Whisper; no SenseVoice Chinese specialization | Larger JVM + GTK4 memory and cold-start footprint | XDG Desktop Portal based (X11 & Wayland) | In-app button / global shortcut / tray | XDG RemoteDesktop Portal synthetic typing | Yes (optional local or cloud LLM text polishing) | None (GTK4 desktop UI) |
| [pavlenkosa/wayvoice](https://github.com/pavlenkosa/wayvoice) (~80★) | Python + GTK/libadwaita utility | Faster-Whisper / `whisper.cpp` / Parakeet | Generic Whisper models; lacks Chinese code-switching post-processing | Depends on Whisper model size | Targets GNOME Wayland only (Alpha stage) | Hotkey-triggered single-shot | Wayland clipboard delivery + optional `ydotool` paste | None (static word replacements only) | None |

---

## Core Technical Advantages

### 1. Zero-VRAM engine optimized for Chinese and code-switched terminology

The project runs `SenseVoice-Small (INT8)` on the `sherpa-onnx` runtime. With the default 4 CPU threads,
per-utterance latency is around 100ms at zero GPU VRAM, so local training or inference workloads are
never disturbed. The model emits punctuated text with inverse text normalization (digits and symbols
normalized) out of the box, and mixed Chinese–English terminology needs no external post-pass. Compared
with the Whisper family, this path fundamentally avoids silence-segment hallucination loops, which suits
the short-utterance, high-frequency, start-stop-anytime nature of desktop input.

### 2. Four-tier pluggable post-processing with fast homophone correction (hard-timeout silent fallback)

Post-processing follows a Tier A + B + C + D architecture (see the dedicated section below):

- Tier A is the deterministic rule layer (filler removal, CJK–ASCII spacing, custom dictation phrases)
  at 0ms latency and always applies first;
- Tier B targets a local OpenAI-compatible inference backend (the example configuration uses vLLM and
  is interchangeable with Ollama / llama.cpp); parameters such as `enable_thinking=false` request
  direct corrected-text output, keeping latency in the hundreds of milliseconds;
- Tier C targets a cloud OpenAI-compatible API with zero local VRAM, suited to GPUs saturated by
  training jobs;
- Tier D goes through the OpenCode CLI reusing the local login session for a free-model tier, with no
  API-key management.

Every LLM tier carries length, symbol, and connectivity guards plus a hard timeout; any network jitter,
stopped service, or malformed output silently falls back to the Tier-A result within milliseconds.
**No failure in the correction chain ever blocks text commit** — an invariant enforced across the pipeline.

### 3. Lossless X11 / Wayland clipboard transactions without changing the input-method framework

The project never asks to replace the system input method (IBus and Fcitx5 coexist untouched). Commit
is a clipboard transaction: back up the current clipboard, write the recognized text, inject the paste
shortcut into the focused window, and restore the original clipboard after ~350ms. Before injecting, the
focused window class distinguishes terminal emulators (`Ctrl+Shift+V`) from regular GUI apps (`Ctrl+V`).
This avoids the garbling and dropped-character failures of `xdotool type` keystroke injection on Chinese
and multi-byte text.

### 4. Dual-key state machine with production-grade misfire engineering

Hold-to-talk (PTT) and continuous dictation (Toggle) use two physically independent keys and state
machines, eliminating single-key-reuse ambiguity. Details include a ~200ms hold threshold (filters
IM-switch-style taps), automatic combo exemption (e.g. `Right Shift + letter` for capitals or
`Right Ctrl + C` never starts recording), X11 auto-repeat filtering, a ~250ms ring pre-roll buffer
(no clipped first characters), and Silero VAD tail auto-flush in continuous mode. Both bindings,
thresholds, and VAD parameters are configurable.

### 5. Zero-dependency Web control center with rootless one-step migration

The panel backend uses only the Python standard library (`http.server`) with no added third-party
dependencies, covering live resource monitoring, hot tier switching, local inference container control,
cloud connectivity diagnostics, and log viewing. Deployment is packaged around the `conda-forge`
toolchain: `./setup.sh` completes environment, dependency, and model setup in one pass even on machines
without `sudo`; missing system tools follow a best-effort policy (auto-install with privileges, manual
hints without, recognition itself never blocked). All relative paths resolve against the config file's
directory, so a relocated project folder works without rewriting absolute paths.

---

## System Architecture

The voice path is a serialized pipeline — capture, recognition, post-processing, injection
(`main.py:VoiceInputApp._process_and_inject`, guarded by `_pipeline_lock` so concurrent commits cannot
interrupt each other) — with the control panel running as an independent process for operations.

<p align="center"><img src="assets/architecture.svg" width="100%" alt="System Architecture"/></p>

| Module | File | Responsibility |
|--------|------|---------------|
| Hotkey daemon | `voice_input/hotkey_daemon.py` | Listens for PTT / Toggle keys; emits record start/stop events |
| Audio capture | `voice_input/audio_capture.py` | Microphone capture, 16kHz resampling, pre-roll, VAD segmentation, USB hot-plug reconnect |
| ASR engine | `voice_input/asr_engine.py` | sherpa-onnx + SenseVoice inference, warmup, ITN |
| Post-processor | `voice_input/post_processor.py` | Tier-A rules + Tier-B/C/D LLM correction and fallback |
| Text injector | `voice_input/text_injector.py` | Clipboard transactions, X11 / Wayland dual-stack paste |
| Control center | `voice_input/control_center.py` + `dashboard.html` | Web backend and frontend; tier, container, and service management |

---

## Dual-Key State Machine and Misfire Protection

| Mode | Gesture | Behavior |
|------|---------|----------|
| Push-to-talk (PTT) | Hold the PTT key (default `Right Shift` or `F9`) | Recording starts past `hold_threshold_sec` (default 0.20s) with no other key pressed (`start.wav`); release commits immediately (`stop.wav`) |
| Continuous dictation (Toggle) | Tap the Toggle key (default `Right Ctrl`) | Starts continuous dictation; Silero VAD segments on `min_silence_duration` (default 0.6s silence) sentence by sentence; tap again to stop, trailing speech (at least 0.25s) auto-flushed through recognition |

<p align="center"><img src="assets/hotkey_flow.svg" width="100%" alt="Hotkey State Machine"/></p>

Misfire protection (`voice_input/hotkey_daemon.py`):

- Short taps of the PTT key (e.g. IM language switching) stay below the hold threshold and never fire;
- Any other key during the hold (e.g. `Right Shift + letter` for capitals) exempts the whole hold;
- Combos on the Toggle key (e.g. `Right Ctrl + C`) count as ordinary shortcuts and never fire;
- X11 auto-repeat events are filtered so recording cannot start/stop repeatedly;
- Both bindings are customizable in the `hotkey` section of `config.yaml` (`ptt_key` accepts
  comma-separated keys).

---

## 4-Tier Post-Processing and LLM Correction

Every tier passes through the Tier-A rule layer first; Tiers B / C / D add LLM correction on top.
Any timeout or error silently falls back to the rule result.

<p align="center"><img src="assets/tier_comparison.svg" width="100%" alt="4-Tier Post-Processing Comparison"/></p>

| Tier | Backend | Latency and resources | When to use |
|------|---------|----------------------|-------------|
| A: pure-local rules (0ms) | Regex + string replacement, zero delay | 0ms, zero VRAM | Default safety net; fallback target of every LLM failure |
| B: local inference backend | Local OpenAI-compatible endpoint (vLLM in the example; Ollama / llama.cpp are interchangeable), direct output via `enable_thinking=false` | Hundreds of ms; footprint depends on the deployed model | Local GPU available and data must stay on-machine |
| C: cloud API | Cloud OpenAI-compatible endpoint (e.g. DeepSeek `deepseek-chat`) | Zero local VRAM, pay-as-you-go, network-dependent latency | Local GPU saturated by training |
| D: free tier via OpenCode CLI | Local OpenCode login calling the CLI (example `opencode/deepseek-v4.1-flash-free`) | No API key, ~2–3s per call | Free correction without key management |

Tier-A rule details (`voice_input/post_processor.py`):

- **Filler removal**: leading or isolated fillers and interjections, guarded so content words survive;
- **Automatic CJK–ASCII spacing**: one space at Han/ASCII-letter-or-digit boundaries (`你好world` → `你好 world`);
- **Custom dictation replacements**: longest-match-first, e.g. a phrase for "new line" maps to `\n`;
  freely extendable in `custom_replacements`.

LLM-tier guards: requests carry hard timeouts (1.5s local, 8.0s cloud by default); responses must pass
length, symbol, and connectivity checks, otherwise they are discarded in favor of the Tier-A result.

---

## Web Control Center

<p align="center"><img src="assets/dashboard_preview.png" width="95%" alt="Web Control Center Dashboard"/></p>

```bash
./run.sh dashboard        # start in background; port from control_center.port (default 17880)
./run.sh dashboard 17881  # start on an explicit port
./run.sh dashboard-stop   # stop the panel
```

Open `http://127.0.0.1:<port>/` (loopback only). Alternatively launch "Voice Input Dashboard" from the
app menu (`voice-input-dashboard.desktop`, system `audio-input-microphone` icon; its `Exec` resolves its
own directory via `%k` before calling `run.sh`, so it contains no hardcoded absolute paths).

| Panel area | Capability |
|------------|------------|
| Top status bar | Daemon state, active correction tier, local-model container state, GPU memory/power, manual refresh |
| Four tier cards | A pure-local / B local inference / C cloud API / D OpenCode free — one click switches tiers and restarts the daemon |
| Local-model row | Start/stop the local inference container (`docker compose -f <compose> up -d` / `stop`); shows the compose path or "no local model deployed" |
| Cloud row | Save the cloud API key (written only to the local `config.yaml`) + one-click connectivity test (`GET /api/deepseek_test`) |
| Service row | Start/stop/restart the daemon, end-to-end self-test (returns last 40 lines) |
| Log box | Daemon log tail, auto + manual refresh (`GET /api/logs?lines=60`) |
| Danger zone | "Stop all services" / "Power off", double-confirmed (two dialogs, the second requires `confirm:"YES"`); power-off is irreversible |

Backend API (`voice_input/control_center.py`, stdlib `http.server` only, zero new dependencies):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Daemon process / tier / container tri-state / resource usage (all best-effort, `null` when unavailable) |
| POST | `/api/mode` | `{"mode":"off"\|"local_qwen"\|"deepseek"\|"opencode"}` — rewrites `post_processor` and restarts the daemon |
| POST | `/api/qwen` | `{"action":"start"\|"stop"}` — start/stop the local inference container |
| POST | `/api/voice` | `{"action":"start"\|"stop"\|"restart"\|"test"}` |
| POST | `/api/deepseek_key` | `{"api_key":"…"}` — writes `llm_modes.deepseek.api_key` |
| GET | `/api/deepseek_test` | Requests `{api_base}/models` with the current key (10s timeout) |
| POST | `/api/opencode_test` | One CLI correction probe, returns latency + output (90s timeout) |
| GET | `/api/logs?lines=60` | Daemon log tail |
| POST | `/api/power` | `{"action":"stop_all"\|"shutdown","confirm":"YES"}` — double confirmation required |

---

## Quick Start

```bash
git clone https://github.com/<your-username>/linux-voice-input.git
cd linux-voice-input
./setup.sh          # create conda env, install deps, download SenseVoice + VAD models, self-test
./run.sh start      # start the background voice daemon
./run.sh dashboard  # start the control panel (http://127.0.0.1:17880/)
```

Notes:

1. `setup.sh` auto-detects or installs conda, creates the `voice-input` env (Python 3.10), installs
   Python dependencies and missing models, then runs `./run.sh test`. System tools
   (`xclip`/`xdotool`/`wl-clipboard`) install on a best-effort basis: with `sudo` they install
   automatically; without it the script skips them with a manual-install hint and recognition still works.
2. For proxied downloads, export first:
   `export http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897`.
3. Copy the config once before first use: `cp config.example.yaml config.yaml` (see Configuration Reference).

---

## CLI Cheat Sheet

```bash
./run.sh start            # start daemon in background (PID: /tmp/voice-input.pid, log: voice-input.log)
./run.sh stop             # stop the daemon
./run.sh restart          # restart the daemon
./run.sh status           # show status + last 15 log lines
./run.sh fg               # run in foreground (live log, Ctrl+C to quit)
./run.sh test             # end-to-end self-test (env / mic / ASR / post-proc / LLM / clipboard / recording)
./run.sh dashboard [port] # start the control panel (PID: /tmp/voice-input-dashboard.pid, log: dashboard.log)
./run.sh dashboard-stop   # stop the control panel
./run.sh mode <tier>      # switch correction tier: off (pure-local) | local_qwen | deepseek | opencode (free)
./run.sh install-desktop  # install the app-menu shortcut (copy voice-input-dashboard.desktop to ~/.local/share/applications/)
```

Direct debugging with the env Python:

```bash
# Record 3s -> recognize -> correct -> inject into the focused window
"$(conda info --base)/envs/voice-input/bin/python" main.py --once 3

# Start with an explicit config / run the self-test
"$(conda info --base)/envs/voice-input/bin/python" main.py --config config.yaml
"$(conda info --base)/envs/voice-input/bin/python" main.py --config config.yaml --test-pipeline
```

`VOICE_INPUT_PYTHON` can point at an existing interpreter to skip conda env discovery.

---

## Configuration Reference

The repo tracks **only `config.example.yaml`; `config.yaml` is git-ignored**.

- `config.example.yaml`: fully commented template with tier presets and defaults — safe to commit;
- `config.yaml`: the real local config, created via `cp config.example.yaml config.yaml`, then edited
  (cloud API key, mic substring, hotkeys, port, …);
- This separation keeps API keys and per-machine settings from ever being committed to a public repo;
  the panel likewise writes keys only to the local `config.yaml`;
- At startup the app loads `config.yaml` and back-fills missing keys from `main.py:DEFAULT_CONFIG`,
  so older configs keep working when the template gains new fields.

| Section | Key items | Description |
|---------|-----------|-------------|
| `audio` | `preferred_device_substring` | Preferred PulseAudio source substring (case-insensitive); empty or unmatched falls back to the system default mic. Example values include `"USB"` or a headset-model substring |
| | `pre_roll_ms` | Pre-roll before recording (default 250ms, avoids clipping the first character) |
| | `vad_model_path` / `vad_threshold` / `min_silence_duration` | Silero VAD model + segmentation params; degrades to RMS-energy detection when the model is absent |
| `hotkey` | `ptt_key` / `toggle_key` / `hold_threshold_sec` | Defaults `shift_r,f9` / `ctrl_r` / 0.20s |
| `asr` | `model_dir` / `num_threads` / `use_itn` / `language` / `provider` | SenseVoice dir / threads / ITN / `auto` / `cpu` or `cuda` |
| `post_processor` | `clean_fillers` / `auto_space_cjk_ascii` / `custom_replacements` | Filler removal / CJK-ASCII spacing / dictation replacements |
| | `enable_llm` / `api_base` / `api_key` / `model` / `timeout` / `disable_thinking` | Local or cloud OpenAI-compatible endpoint (template default `http://127.0.0.1:8000/v1`, 1.5s hard timeout; interchangeable with Ollama / llama.cpp or any compatible backend) |
| | `llm_backend` / `opencode_model` / `opencode_timeout` | `openai` or `opencode_cli` (Tier D goes through the local `opencode run` subprocess) |
| `injector` | `restore_clipboard` / `restore_delay_sec` | Restore the original clipboard after commit / restore delay (default 0.35s) |
| `feedback` | `enable_sound` / `start_wav` / `stop_wav` / `enable_notification` | Cue sounds + desktop notifications (sound only by default, so popups never steal focus) |
| `llm_modes` | `local_qwen` / `deepseek` / `opencode` | The three LLM presets; Tier A is `post_processor.enable_llm=false` |
| `control_center` | `port` / `qwen_compose_file` / `proxy` | Panel port (default 17880, 127.0.0.1 only) / local inference compose file / cloud proxy |

All relative paths (models, cue sounds) resolve against the config file's directory, so a relocated
folder works as-is.

---

## Troubleshooting

1. **Key injection is restricted on Wayland**: prefers `wtype`/`ydotool`, falls back to a dual-stack
   compatible path when absent; `ydotool` needs its background service/`uinput` permission — follow
   the distribution's documentation.
2. **Local inference tier unavailable**: if the local model container is stopped or VRAM is short, the
   hard timeout degrades to Tier A automatically; a fallback notice in the log is expected, not a crash.
3. **Cloud tier needs network + a valid key**: the panel refuses to switch with an empty key; for
   restricted networks set `control_center.proxy` or `http_proxy`/`https_proxy`.
4. **OpenCode tier takes ~2–3s per call**: it shells out to `opencode run`, so latency follows login state
   and network; CLI errors/timeouts degrade automatically. The free quota is CLI-only — direct HTTP calls
   are rejected — which is why the project ships no HTTP integration for it
   (see `voice_input/post_processor.py:polish_opencode_cli`).
5. **Terminal paste binding**: the daemon auto-classifies terminals vs regular windows; if a terminal uses
   a custom paste binding, file an issue with the `xprop WM_CLASS` output.
6. **Missing clipboard tools**: X11 needs `xclip`/`xsel` + `xdotool`, Wayland needs `wl-clipboard`;
   `main.py` prepends the conda env `bin/` to `PATH` at startup — install the system packages if it
   still fails.
7. **`DISPLAY` probing**: daemon starts without `DISPLAY` probe `/tmp/.X11-unix/X*`;
   pure-Wayland sessions return `None` and take the Wayland path. When starting over SSH, check
   `echo $DISPLAY` first.
8. **USB mic hot-plug**: the capture thread runs continuously and reconnects on unplug;
   leave `preferred_device_substring` empty to always use the system default mic.
9. **No cue sound**: verify `paplay` exists, `feedback.enable_sound: true`, and `assets/*.wav` present.

Still stuck? Run in order and paste the output in the issue: `./run.sh status` → `./run.sh test` →
`tail -n 50 voice-input.log` / `tail -n 50 dashboard.log`.

---

## Repository Layout

- `main.py` — main app (daemon / self-test / `--once` single-shot)
- `config.example.yaml` — config template (tracked)
- `config.yaml` — real local config (git-ignored; copy from the example)
- `run.sh` — manager (start / stop / status / fg / test / dashboard / mode / install-desktop)
- `setup.sh` — one-shot setup (conda env / deps / models / self-test, sudo-free)
- `voice-input-dashboard.desktop` — app-menu shortcut
- `README.md` / `README_EN.md` — Chinese / English docs
- `voice_input/` — modules
  - `audio_capture.py` — mic capture + VAD (default-mic fallback / hot-plug reconnect)
  - `asr_engine.py` — SenseVoice recognition (INT8 / warmup / ITN)
  - `post_processor.py` — two-stage post-processing (rule layer + Tier-B/C/D LLM)
  - `text_injector.py` — clipboard injection (X11 + Wayland, terminal auto-detect)
  - `hotkey_daemon.py` — dual-key dual-mode (PTT + Toggle, combo exemption)
  - `control_center.py` — panel backend (stdlib `http.server`, zero new deps)
  - `dashboard.html` — panel frontend
- `models/` — SenseVoice + Silero VAD models (auto-downloaded by `setup.sh`)
- `assets/` — architecture and flow charts, cue sounds (`architecture.svg`, `hotkey_flow.svg`, `tier_comparison.svg`, `dashboard_preview.png`, `start.wav`, `stop.wav`)

---

## License and Acknowledgements

- This project is under the **MIT License**. See [LICENSE](LICENSE).
- Thanks to:
  - [Sherpa-ONNX](https://github.com/k2-fsa/sherpa-onnx) — cross-platform on-device speech inference;
  - [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) — multilingual ASR model (zh / en / yue / ja / ko);
  - [Silero VAD](https://github.com/snakers4/silero-vad) — lightweight voice-activity detection
    for automatic segmentation in dictation mode.

# linux-voice-input — Low-Latency Offline Voice Typing for the Linux Desktop

[简体中文](README.md) | [English](README_EN.md)

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)](setup.sh)
[![Platform](https://img.shields.io/badge/Platform-Linux%20X11%20%26%20Wayland-green)](voice_input/text_injector.py)
[![ASR](https://img.shields.io/badge/ASR-Sherpa--ONNX%20SenseVoice-orange)](voice_input/asr_engine.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Offline, low-latency voice input for the Linux desktop, purpose-built for **Chinese and
> Chinese–English code-switching**. Hold to talk, release to commit; toggle continuous
> dictation with automatic segmentation. Ships with a Web control panel and a
> **4-tier LLM homophone correction pipeline that never blocks screen commit**.

---

## Contents

- [Why This Project?](#why-this-project)
- [Architecture & Dual-Key Interaction](#architecture--dual-key-interaction)
- [Quick Start in 3 Steps (sudo-free)](#quick-start-in-3-steps-sudo-free)
- [Web Dashboard](#web-dashboard)
- [CLI Cheat Sheet](#cli-cheat-sheet)
- [Configuration (`config.example.yaml` → `config.yaml` safety model)](#configuration-configexampleyaml--configyaml-safety-model)
- [Known Limitations & Troubleshooting](#known-limitations--troubleshooting)
- [Repository Layout](#repository-layout)
- [License & Acknowledgements](#license--acknowledgements)

---

## Why This Project?

Existing open-source voice-input options on Linux each have a weak spot: some are welded to one
input-method framework, some handle Chinese / mixed Chinese–English poorly, some suffer high latency
or Whisper-style hallucination on silence, and some garble or drop CJK characters on commit.
`linux-voice-input` fixes all four at once:

| Aspect | nerd-dictation | whisper-writer | fcitx5-vinput | Buzz / Vibe | **linux-voice-input (this project)** |
|--------|----------------|----------------|---------------|-------------|--------------------------------------|
| **No input-method lock-in** | Tied to specific IM integration | Manual hotkey/script wiring | **Requires Fcitx5**; unusable on stock Ubuntu IBus or plain-English setups | Standalone app, IM-agnostic | **Plug-and-play**: stock Ubuntu IBus, Fcitx5, or plain-English sessions all work; never touches the IM framework |
| **Chinese + English term mixing (code-switching)** | Vosk tiny models, weak Chinese | Generic Whisper, needs post-processing for mixed text | Depends on online engines / large models | Whisper-based, transcription-oriented | **SenseVoice-Small (int8)**: zero GPU VRAM on CPU, ~0.1s per utterance, built-in punctuation + ITN number normalization, **no Whisper silence-hallucination** |
| **Homophone/grammar correction (never blocks commit)** | None | None / weak | None | None | **4 tiers, automatic fallback, never blocks commit** (see below) |
| **Lossless commit to the focused window (X11 / Wayland)** | `xdotool type` garbles/drops CJK | Incomplete clipboard handling | Commits via IM, unavailable outside the framework | Mostly copy-paste | **Clipboard transaction**: auto-detects terminals (`Ctrl+Shift+V`) vs regular GUI apps (`Ctrl+V`), restores your original clipboard 0.35s after paste |

### Four-tier post-processing & homophone/grammar correction (never blocks commit)

Every tier passes through the Tier-A rule layer first; tiers B/C/D add LLM correction on top.
Any timeout or error **silently falls back to the rule-based result** — text always lands on screen:

| Tier | Backend | Latency / cost | When to use |
|------|---------|----------------|-------------|
| **Tier A (pure-local rules, 0ms)** | Regex + string replacement, zero delay | 0ms, 0 VRAM | Default safety net; every LLM failure falls back here |
| **Tier B (local vLLM Qwen Qwen3.8-27B, ~0.3s)** | `http://127.0.0.1:8000/v1` with `enable_thinking=false` for instant output | ~0.3s, ~21GB VRAM | Local GPU available, data must never leave the machine |
| **Tier C (cloud DeepSeek API)** | `https://api.deepseek.com/v1`, `deepseek-chat` | Zero local VRAM, pay-as-you-go | GPU is busy running experiments/training |
| **Tier D (free model via OpenCode CLI)** | Reuses the local OpenCode login to call `opencode/deepseek-v4.1-flash-free` | Zero API key, ~2–3s per call | Free correction, no key management |

Tier-A rule details (`voice_input/post_processor.py`, `Level 1`):

- **Filler removal**: leading/isolated 嗯, 呃, 啊, 那个, 就是说, … (guarded so real words like 好啊 / 呃逆 survive).
- **Auto CJK–ASCII spacing**: one space at Han/ASCII-digit boundaries (`你好world` → `你好 world`).
- **Custom dictation replacements**: longest-match-first, e.g. `"新的一行"` (Chinese for "new line") → `\n`; extend freely in `custom_replacements`.

---

## Architecture & Dual-Key Interaction

### ASCII architecture diagram

```
                        ┌────────────────────────────────────────────────┐
                        │            HotkeyDaemon (dual-key, dual-mode)    │
                        │  Hold RightShift/F9 (>=0.20s, no other key)=PTT │
                        │  Press RightCtrl = Toggle on/off                │
                        └───────┬────────────────────────┬───────────────┘
                                │ on_ptt_start/stop      │ on_toggle_start/stop
                                v                        v
┌─────────┐   ┌──────────────────────────┐   ┌─────────────────┐   ┌──────────────────┐   ┌───────────────────┐
│Microphone│──▶│ AudioCapture             │──▶│ ASREngine       │──▶│ PostProcessor    │──▶│ TextInjector      │──▶ focused window
│PulseAudio│   │ 250ms pre-roll / 16kHz   │   │ SenseVoice int8 │   │ A rules (0ms)    │   │ Clipboard txn:    │   terminal:C-S-V
│auto      │   │ Silero VAD cut (Toggle)  │   │ ~0.1s/utt on CPU│   │ B/C/D LLM polish │   │ backup→write→paste│   normal: C-V
│fallback  │   │ USB hot-plug reconnect   │   │ ITN punct./nums │   │ 1.5s hard timeout│   │ →restore after    │   X11/Wayland
└─────────┘   └──────────────────────────┘   └─────────────────┘   └──────────────────┘   │ 0.35s             │
                                                                                          └───────────────────┘
                                                        │
                                                        v
                                              ┌──────────────────┐
                                              │ control_center   │
                                              │ Web panel :17880 │
                                              │ tiers/containers │
                                              └──────────────────┘
```

Pipeline (`main.py:VoiceInputApp._process_and_inject`): **recognize → post-process → inject** is fully
serialized (`_pipeline_lock`) so concurrent commits can't interrupt each other; LLM correction runs
inside the same pipeline with a hard timeout and degrades to the rule result on expiry.

### Dual-key interaction

| Mode | Gesture | Behavior |
|------|---------|----------|
| **Push-to-talk (PTT)** | Hold `Right Shift` or `F9` | Holding past `hold_threshold_sec` (default 0.20s) with no other key pressed starts recording (`start.wav`); release commits text immediately (`stop.wav`) |
| **Continuous dictation (Toggle)** | Tap `Right Ctrl` | Starts continuous dictation; Silero VAD segments on `min_silence_duration` (default 0.6s silence) and commits sentence by sentence; tap again to stop, trailing unsegmented speech (≥0.25s) is auto-flushed through recognition |

**Anti-misfire design** (`voice_input/hotkey_daemon.py`):

- A quick tap of `Right Shift` (IM Chinese/English switch) is under 0.20s → **never fires**.
- `Right Shift + letter` (capital letters) presses another key mid-hold → **100% exempted**.
- Combos like `Right Ctrl + C` (another key before release) are treated as ordinary shortcuts → **never fire**.
- Both bindings are customizable in the `hotkey` section of `config.yaml` (`ptt_key` accepts comma-separated keys).

---

## Quick Start in 3 Steps (sudo-free)

```bash
git clone https://github.com/<your-username>/linux-voice-input.git
cd linux-voice-input
./setup.sh          # create conda env, install rootless deps, download SenseVoice + VAD models, self-test
./run.sh start      # start the background voice daemon
./run.sh dashboard  # start the control panel (http://127.0.0.1:17880/)
```

Notes:

1. `setup.sh` auto-detects or installs conda, creates the `voice-input` env (Python 3.10), installs
   Python deps and missing models, then runs `./run.sh test`. System tools
   (`xclip`/`xdotool`/`wl-clipboard`) install on a best-effort basis: **with sudo they install
   automatically; without sudo the script skips them with a manual-install hint and recognition still works**.
2. For downloads outside mainland networks, set a proxy first:
   `export http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897`.
3. Copy the config once before first use: `cp config.example.yaml config.yaml` (see below).

---

## Web Dashboard

```bash
./run.sh dashboard        # start in background; port comes from control_center.port (default 17880)
./run.sh dashboard 17881  # start on an explicit port
./run.sh dashboard-stop   # stop the panel
```

Open `http://127.0.0.1:<port>/` (loopback only). Or launch **"Voice Input Dashboard"** from the app
menu (`voice-input-dashboard.desktop`, system `audio-input-microphone` icon; its `Exec` resolves its
own directory via `%k` before calling `run.sh`, so it contains no hardcoded absolute paths).

| Panel area | Capability |
|------------|------------|
| Top status bar | Daemon state, active correction tier, Qwen container state, GPU memory/power, manual refresh |
| Four tier cards | A pure-local / B local Qwen / C cloud DeepSeek / D OpenCode free — one click switches tiers and restarts the daemon |
| Local-model row | Start/stop the Qwen container (`docker compose -f <compose> up -d` / `stop`); shows compose path or "no local model deployed" |
| Cloud row | Save the DeepSeek API key (written only to the local `config.yaml`) + one-click connectivity test (`GET /api/deepseek_test`) |
| Service row | Start/stop/restart the daemon, end-to-end self-test (returns last 40 lines) |
| Log box | Daemon log tail, auto + manual refresh (`GET /api/logs?lines=60`) |
| Danger zone | "Stop all services" / "Power off", **double-confirmed** (two dialogs, the second requires `confirm:"YES"`); power-off is irreversible |

Backend API (`voice_input/control_center.py`, stdlib `http.server` only, zero new dependencies):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/status` | Daemon process / tier / container tri-state / resource usage (all best-effort, `null` when unavailable) |
| POST | `/api/mode` | `{"mode":"off"\|"local_qwen"\|"deepseek"\|"opencode"}` — rewrites `post_processor` and restarts the daemon |
| POST | `/api/qwen` | `{"action":"start"\|"stop"}` — start/stop the local Qwen container |
| POST | `/api/voice` | `{"action":"start"\|"stop"\|"restart"\|"test"}` |
| POST | `/api/deepseek_key` | `{"api_key":"…"}` — writes `llm_modes.deepseek.api_key` |
| GET | `/api/deepseek_test` | Requests `{api_base}/models` with the current key (10s timeout) |
| POST | `/api/opencode_test` | One CLI correction probe, returns latency + output (90s timeout) |
| GET | `/api/logs?lines=60` | Daemon log tail |
| POST | `/api/power` | `{"action":"stop_all"\|"shutdown","confirm":"YES"}` — double confirmation required |

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

## Configuration (`config.example.yaml` → `config.yaml` safety model)

The repo tracks **only `config.example.yaml`; `config.yaml` is git-ignored**.

- `config.example.yaml`: fully commented template with the four-tier `llm_modes` presets and defaults — safe to commit.
- `config.yaml`: your real local config, created via `cp config.example.yaml config.yaml`, then edited
  (DeepSeek key, mic substring, hotkeys, port, …).
- This keeps **API keys and per-machine settings from ever being `git add .`-ed into a public repo**;
  the panel likewise writes keys only to the local `config.yaml`, never to the template.
- At startup the app loads `config.yaml` and back-fills missing keys from `main.py:DEFAULT_CONFIG`,
  so older configs keep working when the template gains new fields.

| Section | Key items | Description |
|---------|-----------|-------------|
| `audio` | `preferred_device_substring` | Preferred PulseAudio source substring; wrong/empty falls back to the system default mic |
| | `pre_roll_ms` | Pre-roll before recording (default 250ms, avoids clipping the first character) |
| | `vad_model_path` / `vad_threshold` / `min_silence_duration` | Silero VAD model + segmentation params; degrades to RMS-energy detection when the model is absent |
| `hotkey` | `ptt_key` / `toggle_key` / `hold_threshold_sec` | Defaults `shift_r,f9` / `ctrl_r` / 0.20s |
| `asr` | `model_dir` / `num_threads` / `use_itn` / `language` / `provider` | SenseVoice dir / threads / ITN / `auto` / `cpu` or `cuda` |
| `post_processor` | `clean_fillers` / `auto_space_cjk_ascii` / `custom_replacements` | Filler removal / CJK-ASCII spacing / dictation replacements |
| | `enable_llm` / `api_base` / `api_key` / `model` / `timeout` / `disable_thinking` | Tier B/C OpenAI-compatible endpoint (default `http://127.0.0.1:8000/v1`, 1.5s hard timeout) |
| | `llm_backend` / `opencode_model` / `opencode_timeout` | `openai` or `opencode_cli` (Tier D goes through the local `opencode run` subprocess) |
| `injector` | `restore_clipboard` / `restore_delay_sec` | Restore the original clipboard after commit / restore delay (default 0.35s) |
| `feedback` | `enable_sound` / `start_wav` / `stop_wav` / `enable_notification` | Cue sounds + desktop notifications (sound only by default, so popups never steal focus) |
| `llm_modes` | `local_qwen` / `deepseek` / `opencode` | The three LLM presets; Tier A is just `post_processor.enable_llm=false` |
| `control_center` | `port` / `qwen_compose_file` / `proxy` | Panel port (default 17880, 127.0.0.1 only) / Qwen compose file / cloud proxy |

All relative paths (models, cue sounds) resolve **against the config file's directory**, so the whole
folder can be copied to any machine and used as-is.

---

## Known Limitations & Troubleshooting

1. **Key injection is restricted on Wayland**: prefers `wtype`/`ydotool`, falls back to a dual-stack
   compatible path when absent; `ydotool` needs its background service/`uinput` permission — follow
   your distro's docs.
2. **Tier B needs ~21GB VRAM**: if the local Qwen3.8-27B isn't running or VRAM is short, the 1.5s
   timeout degrades to Tier A automatically (`LLM correction failed, falling back to Level 1` in the
   log is expected, not a bug).
3. **Tier C needs network + a valid key**: the panel refuses to switch with an empty key; for overseas
   APIs set `control_center.proxy` or `http_proxy`/`https_proxy`.
4. **Tier D takes ~2–3s per call**: it shells out to `opencode run`, so latency follows login state and
   network; CLI errors/timeouts degrade automatically. Note the OpenCode free quota is **CLI-only** —
   direct HTTP calls are rejected with `FreeTierError`, which is why this project has no HTTP
   integration (see `voice_input/post_processor.py:polish_opencode_cli`).
5. **Terminals paste with `Ctrl+Shift+V`**: the daemon auto-classifies the focused window; if your
   terminal uses a custom paste binding, file an issue with your `xprop WM_CLASS` output.
6. **Missing clipboard tools**: X11 needs `xclip`/`xsel` + `xdotool`, Wayland needs `wl-clipboard`;
   `main.py` prepends the conda env `bin/` to `PATH` at startup — if it still fails,
   `sudo apt-get install -y xclip xdotool`.
7. **`DISPLAY` probing**: headless/daemon starts without `DISPLAY` probe `/tmp/.X11-unix/X*`;
   pure-Wayland sessions return `None` and take the Wayland path. When starting over SSH, check
   `echo $DISPLAY` first.
8. **USB mic hot-plug**: the capture thread runs continuously and reconnects on unplug;
   leave `preferred_device_substring` empty to always use the system default mic.
9. **No cue sound**: verify `paplay` exists, `feedback.enable_sound: true`, and `assets/*.wav` present.

Still stuck? Run in order and paste the output in your issue: `./run.sh status` → `./run.sh test` →
`tail -n 50 voice-input.log` / `tail -n 50 dashboard.log`.

---

## Repository Layout

```
linux-voice-input/
├── main.py                        # main app (daemon / self-test / --once)
├── config.example.yaml            # config template (tracked)
├── config.yaml                    # real local config (git-ignored; cp from the example)
├── run.sh                         # manager (start/stop/status/fg/test/dashboard/mode/install-desktop)
├── setup.sh                       # one-shot setup (conda env / deps / models / self-test, sudo-free)
├── voice-input-dashboard.desktop  # app-menu shortcut
├── README.md / README_EN.md       # Chinese / English docs
├── voice_input/                   # modules
│   ├── audio_capture.py           # mic capture + VAD (default-mic fallback / hot-plug reconnect)
│   ├── asr_engine.py              # SenseVoice recognition (int8 + warmup + ITN)
│   ├── post_processor.py          # two-stage post-proc (Level 1 rules + Level 2/B/C/D LLM)
│   ├── text_injector.py           # clipboard injection (X11 + Wayland, terminal auto-detect)
│   ├── hotkey_daemon.py           # dual-key dual-mode (PTT + Toggle, combo exemption)
│   ├── control_center.py          # panel backend (stdlib http.server, zero new deps)
│   └── dashboard.html             # panel frontend
├── models/                        # SenseVoice + Silero VAD models (auto-downloaded by setup.sh)
└── assets/                        # start.wav / stop.wav cue sounds
```

---

## License & Acknowledgements

- This project is under the **MIT License**. See [LICENSE](LICENSE) (if the file is missing,
  SPDX `MIT` applies — PRs adding the formal text are welcome).
- Thanks to:
  - [Sherpa-ONNX](https://github.com/k2-fsa/sherpa-onnx) — cross-platform on-device speech inference;
  - [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) (Alibaba Tongyi) — multilingual
    (zh/en/yue/ja/ko) ASR model;
  - [Silero VAD](https://github.com/snakers4/silero-vad) — lightweight voice-activity detection
    for automatic segmentation in dictation mode.

# linux-voice-input —— Linux 桌面低延迟离线语音输入工具

[简体中文](README.md) | [English](README_EN.md)

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue)](setup.sh)
[![Platform](https://img.shields.io/badge/Platform-Linux%20X11%20%26%20Wayland-green)](voice_input/text_injector.py)
[![ASR](https://img.shields.io/badge/ASR-Sherpa--ONNX%20SenseVoice-orange)](voice_input/asr_engine.py)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 专为**中文与中英混杂（Code-Switching）**场景打造的 Linux 桌面低延迟离线语音输入工具。
> 按住说话、松开上屏；连续听写、自动断句。附 Web 管控面板与**四档 LLM 同音字纠错（永不阻塞上屏）**。

---

## 目录

- [为什么做这个项目？](#为什么做这个项目)
- [系统架构与双键交互设计](#系统架构与双键交互设计)
- [3 步快速开始（支持免 sudo 部署）](#3-步快速开始支持免-sudo-部署)
- [图形化管控面板（Web Dashboard）](#图形化管控面板web-dashboard)
- [CLI 命令速查](#cli-命令速查)
- [配置说明（`config.example.yaml` → `config.yaml` 安全机制）](#配置说明configexampleyaml--configyaml-安全机制)
- [已知限制与常见问题排障](#已知限制与常见问题排障known-limitations--troubleshooting)
- [目录结构](#目录结构)
- [开源协议与致谢](#开源协议与致谢)

---

## 为什么做这个项目？

Linux 上已有的开源语音输入方案各有短板：要么强绑某一输入法框架，要么对中文/中英混输不友好，
要么延迟高、有静音幻觉，要么上屏方式在中文环境下丢字乱码。`linux-voice-input` 一次解决这四个痛点：

| 维度 | nerd-dictation | whisper-writer | fcitx5-vinput | Buzz / Vibe | **linux-voice-input（本项目）** |
|------|----------------|----------------|---------------|-------------|--------------------------------|
| **免换输入法框架** | 依赖特定输入法集成 | 需手动配快捷键/脚本 | **强绑 Fcitx5**，GNOME 默认 IBus / 纯英文环境不可用 | 独立 App，与输入法无关 | **即插即用**：Ubuntu 默认 IBus、Fcitx5、纯英文环境均可用，不碰输入法框架 |
| **中文 + 英文术语混输（Code-Switching）** | Vosk 小模型，中文弱 | Whisper 通用模型，中英混排需后处理 | 依赖在线引擎或大模型 | Whisper 系，桌面转写为主 | **SenseVoice-Small（int8）**：CPU 零显存占用，单句约 0.1s，自带标点与数字规范化（ITN），**无 Whisper 静音幻觉问题** |
| **同音字/语法纠错（永不阻塞上屏）** | 无 | 无/弱 | 无 | 无 | **四档可选，失败自动降级，绝不阻塞上屏**（见下） |
| **X11 / Wayland 焦点窗口无损上屏** | `xdotool type` 打中文乱码/丢字 | 剪贴板方案不完整 | 输入法提交，框架外不可用 | 复制粘贴为主 | **剪贴板事务**：自动识别终端窗口（发 `Ctrl+Shift+V`）与普通 GUI 窗口（发 `Ctrl+V`），粘贴后 0.35s 自动还原用户原剪贴板内容 |

### 四档后处理与同音字/语法纠错（永不阻塞上屏）

所有档位都先过 A 档规则层；B/C/D 档在此基础上做 LLM 兜底纠错，
任何超时/报错都**静默回退到规则结果**，用户永远先看到文字上屏：

| 档位 | 后端 | 延迟/资源 | 适用场景 |
|------|------|-----------|----------|
| **A 档（纯本地规则，0ms）** | 正则 + 字符串替换，零延迟 | 0ms，0 显存 | 默认兜底；任何 LLM 失败时自动回退到此档 |
| **B 档（本机 vLLM 千问 Qwen3.8-27B，约 0.3s）** | `http://127.0.0.1:8000/v1`，`enable_thinking=false` 极速直出 | 约 0.3s，约 21GB 显存 | 本机有 GPU，想让数据 100% 不出本机 |
| **C 档（云端 DeepSeek API）** | `https://api.deepseek.com/v1`，`deepseek-chat` | 零本地显存，按量计费 | 跑科研实验/训练占满 GPU 时使用 |
| **D 档（OpenCode 免费模型 CLI）** | 复用本机 OpenCode 登录态调用 `opencode/deepseek-v4.1-flash-free` | 零 API Key，单次约 2~3s | 免费纠错，不想填 Key |

A 档规则细节（`voice_input/post_processor.py`，`Level 1`）：

- **去口头禅**：句首/句中孤立的嗯、呃、啊、那个、就是说等（带护栏，避免误删“好啊”“呃逆”等实义）。
- **中英文自动空格**：汉字与 ASCII 字母/数字交界处补空格（`你好world` → `你好 world`）。
- **自定义口述词替换**：长词优先，如 `“新的一行”` → `\n`，可在 `config.yaml` 的 `custom_replacements` 自由增删。

---

## 系统架构与双键交互设计

### ASCII 架构图

```
                        ┌────────────────────────────────────────────────┐
                        │              HotkeyDaemon（双键独立双模）       │
                        │  按住 右Shift/F9 (>=0.20s,无其它键) = PTT      │
                        │  按一下 右Ctrl = Toggle 开/关                   │
                        └───────┬────────────────────────┬───────────────┘
                                │ on_ptt_start/stop      │ on_toggle_start/stop
                                v                        v
┌─────────┐   ┌──────────────────────────┐   ┌─────────────────┐   ┌──────────────────┐   ┌───────────────────┐
│麦克风    │──▶│ AudioCapture             │──▶│ ASREngine       │──▶│ PostProcessor    │──▶│ TextInjector      │──▶ 焦点窗口
│PulseAudio│   │ 250ms预卷 / 重采样16k    │   │ SenseVoice int8 │   │ A规则(0ms)       │   │ 剪贴板事务：      │   终端:C-S-V
│自动回退  │   │ Silero VAD断句(Toggle)   │   │ CPU约0.1s/句    │   │ B/C/D LLM纠错    │   │ 备份→写入→粘贴    │   普通: C-V
│默认设备  │   │ USB热插拔自动重连        │   │ ITN标点数字规范 │   │ 超时1.5s即降级   │   │ →0.35s后还原剪贴板 │   X11/Wayland双栈
└─────────┘   └──────────────────────────┘   └─────────────────┘   └──────────────────┘   └───────────────────┘
                                                        │
                                                        v
                                              ┌──────────────────┐
                                              │ control_center   │
                                              │ Web面板 :17880   │
                                              │ 切档/启停容器/日志│
                                              └──────────────────┘
```

数据流（`main.py:VoiceInputApp._process_and_inject`）：**识别 → 后处理 → 注入**全程串行化
（`_pipeline_lock`），避免并发注入互相打断；LLM 纠错跑在同一流水线内但带硬超时，
超时即用规则结果上屏。

### 双键独立交互设计

| 模式 | 操作 | 行为 |
|------|------|------|
| **按住说话（PTT）** | 按住 `右 Shift` 或 `F9` | 按住满 `hold_threshold_sec`（默认 0.20s）且期间无其它按键 → 开始录音（`start.wav`）；松开 → 立即识别上屏（`stop.wav`） |
| **连续听写（Toggle）** | 按一下 `右 Ctrl` | 开启连续听写，Silero VAD 按 `min_silence_duration`（默认 0.6s 静音）自动断句、逐句实时上屏；再按一下结束，尾部未断句语音（≥0.25s）自动 flush 识别 |

**防误触设计**（`voice_input/hotkey_daemon.py`）：

- 短按 `右 Shift`（输入法中英切换）因不足 0.20s **不触发**。
- `右 Shift + 字母`（打大写字母）因期间出现其它按键 **100% 豁免**。
- `右 Ctrl + C` 等组合键因松开前敲过其它键，同样判定为普通组合键 **不触发**。
- 两组按键均可在 `config.yaml` 的 `hotkey` 段自定义（`ptt_key` 支持逗号分隔多个键）。

---

## 3 步快速开始（支持免 sudo 部署）

```bash
git clone https://github.com/<your-username>/linux-voice-input.git
cd linux-voice-input
./setup.sh          # 自动创建 conda 环境、安装免 root 依赖、下载 SenseVoice 与 VAD 模型并自检
./run.sh start      # 启动后台语音守护进程
./run.sh dashboard  # 启动图形化管控面板 (http://127.0.0.1:17880/)
```

说明：

1. `setup.sh` 会自动定位或安装 conda、创建 `voice-input` 环境（Python 3.10）、装 Python 依赖、
   下载缺失模型，最后跑 `./run.sh test` 自检。系统工具（`xclip`/`xdotool`/`wl-clipboard`）走
   best-effort 安装：**有 sudo 则自动装，无 sudo 则跳过并提示手动安装，不阻塞识别本身**。
2. 境外网络下载模型前建议先设代理：`export http_proxy=http://127.0.0.1:7897 https_proxy=http://127.0.0.1:7897`。
3. 首次使用前复制一份配置：`cp config.example.yaml config.yaml`（详见下一节）。

---

## 图形化管控面板（Web Dashboard）

```bash
./run.sh dashboard        # 后台启动，端口取 config.yaml 的 control_center.port（默认 17880）
./run.sh dashboard 17881  # 指定端口启动
./run.sh dashboard-stop   # 停止面板
```

浏览器访问 `http://127.0.0.1:<port>/`（仅监听本机回环）。也可在应用菜单点击
**「语音输入控制面板」**（`voice-input-dashboard.desktop`，用系统自带 `audio-input-microphone`
图标，`Exec` 经 `%k` 定位自身目录再调 `run.sh`，不含任何本机绝对路径）。

| 面板区域 | 能力 |
|----------|------|
| 顶部状态条 | 语音服务运行状态、当前纠错档、千问容器状态、GPU 显存/功耗、手动刷新 |
| 四张模式卡 | A 纯本地规则 / B 本地千问 / C 云端 DeepSeek / D OpenCode 免费，一键切换并自动重启守护进程 |
| 本地模型行 | 启动/停止千问容器（`docker compose -f <compose> up -d` / `stop`），显示 compose 路径；文件不存在则显示“未部署本地模型” |
| 云端行 | 填写并保存 DeepSeek API Key（只写入本机 `config.yaml`）、一键连通性测试（`GET /api/deepseek_test`） |
| 服务行 | 启动/停止/重启语音守护进程、端到端自检（返回输出尾部 40 行） |
| 日志框 | 语音服务日志尾部，自动 + 手动刷新（`GET /api/logs?lines=60`） |
| 危险区 | 「停止全部服务」「关闭电脑」，**双重确认**（两次弹窗，第二次需 `confirm:"YES"`）；关机不可撤销 |

后端 API（`voice_input/control_center.py`，标准库 `http.server` 实现，零新增依赖）：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/status` | 语音进程/档位/容器三态/资源占用（全部 best-effort，取不到返回 `null`） |
| POST | `/api/mode` | `{"mode":"off"\|"local_qwen"\|"deepseek"\|"opencode"}` 改写 `post_processor` 并重启守护进程 |
| POST | `/api/qwen` | `{"action":"start"\|"stop"}` 启停本地千问容器 |
| POST | `/api/voice` | `{"action":"start"\|"stop"\|"restart"\|"test"}` |
| POST | `/api/deepseek_key` | `{"api_key":"…"}` 写入 `llm_modes.deepseek.api_key` |
| GET | `/api/deepseek_test` | 用当前 Key 请求 `{api_base}/models`（10s 超时） |
| POST | `/api/opencode_test` | 调一次 CLI 纠错短句，返回耗时与输出（90s 超时） |
| GET | `/api/logs?lines=60` | 语音服务日志尾部 |
| POST | `/api/power` | `{"action":"stop_all"\|"shutdown","confirm":"YES"}`，必须双确认 |

---

## CLI 命令速查

```bash
./run.sh start            # 后台启动守护进程（PID: /tmp/voice-input.pid，日志: voice-input.log）
./run.sh stop             # 停止后台守护进程
./run.sh restart          # 重启后台守护进程
./run.sh status           # 查看运行状态 + 最近 15 行日志
./run.sh fg               # 前台运行（实时看日志，Ctrl+C 退出）
./run.sh test             # 端到端自检（环境 / 麦克风 / ASR / 后处理 / LLM / 剪贴板 / 录音）
./run.sh dashboard [port] # 后台启动图形化管理面板（PID: /tmp/voice-input-dashboard.pid，日志: dashboard.log）
./run.sh dashboard-stop   # 停止图形化管理面板
./run.sh mode <档>        # 快捷切换纠错档：off（纯本地规则）| local_qwen（本地千问）| deepseek（云端）| opencode（OpenCode 免费）
./run.sh install-desktop  # 安装应用菜单快捷方式（复制 voice-input-dashboard.desktop 到 ~/.local/share/applications/）
```

命令行调试入口（直接用环境里的 Python）：

```bash
# 立即录音 3 秒 -> 识别 -> 纠错 -> 注入当前焦点窗口
"$(conda info --base)/envs/voice-input/bin/python" main.py --once 3

# 指定配置文件启动 / 自检
"$(conda info --base)/envs/voice-input/bin/python" main.py --config config.yaml
"$(conda info --base)/envs/voice-input/bin/python" main.py --config config.yaml --test-pipeline
```

也可用环境变量 `VOICE_INPUT_PYTHON` 指定解释器，跳过 conda 环境查找。

---

## 配置说明（`config.example.yaml` → `config.yaml` 安全机制）

仓库**只追踪 `config.example.yaml`，不追踪 `config.yaml`**（`.gitignore` 忽略后者）。

- `config.example.yaml`：带完整注释的模板，含四档 `llm_modes` 预置与各段默认值，可安全提交。
- `config.yaml`：用户本地真实配置，由 `cp config.example.yaml config.yaml` 创建后自行修改
  （填 DeepSeek Key、改麦克风子串、改热键、改端口等）。
- 这样设计是为了**保护用户 API Key 与个人设备配置不被意外 `git add .` 提交到公开仓库**；
  面板写入 Key 时也只写本机 `config.yaml`，从不触碰模板。
- 程序启动时以 `config.yaml` 为准、缺失项自动用 `main.py:DEFAULT_CONFIG` 补齐；
  所以即使模板新增字段，老用户不更新 `config.yaml` 也不会崩。

| 段 | 关键项 | 说明 |
|----|--------|------|
| `audio` | `preferred_device_substring` | 优先匹配的 PulseAudio 输入源子串；填错/留空自动回退系统默认麦克风 |
| | `pre_roll_ms` | 录音前预卷（默认 250ms，防吞首字） |
| | `vad_model_path` / `vad_threshold` / `min_silence_duration` | Silero VAD 模型与断句参数；模型缺失自动退化为 RMS 能量检测 |
| `hotkey` | `ptt_key` / `toggle_key` / `hold_threshold_sec` | 默认 `shift_r,f9` / `ctrl_r` / 0.20s |
| `asr` | `model_dir` / `num_threads` / `use_itn` / `language` / `provider` | SenseVoice 模型目录 / 推理线程数 / 逆文本规范化 / `auto` / `cpu` 或 `cuda` |
| `post_processor` | `clean_fillers` / `auto_space_cjk_ascii` / `custom_replacements` | 去口头禅 / 中英混排空格 / 口述词替换 |
| | `enable_llm` / `api_base` / `api_key` / `model` / `timeout` / `disable_thinking` | B/C 档 OpenAI 兼容接口（默认 `http://127.0.0.1:8000/v1`，1.5s 硬超时） |
| | `llm_backend` / `opencode_model` / `opencode_timeout` | `openai` 或 `opencode_cli`（D 档走本机 `opencode run` 子进程） |
| `injector` | `restore_clipboard` / `restore_delay_sec` | 注入后恢复原剪贴板 / 恢复延迟（默认 0.35s） |
| `feedback` | `enable_sound` / `start_wav` / `stop_wav` / `enable_notification` | 提示音与桌面通知（默认仅声音，避免弹窗抢焦点） |
| `llm_modes` | `local_qwen` / `deepseek` / `opencode` | 三档模板；A 档即 `post_processor.enable_llm=false` |
| `control_center` | `port` / `qwen_compose_file` / `proxy` | 面板端口（默认 17880，仅 127.0.0.1）/ 千问 compose 文件 / 云端代理 |

所有相对路径（模型、提示音）均**相对于配置文件所在目录**解析，整目录拷贝到任意机器可直接用。

---

## 已知限制与常见问题排障（Known Limitations & Troubleshooting）

1. **Wayland 下按键注入受限**：优先 `wtype`/`ydotool`，缺失时回退双栈兼容方案；
   `ydotool` 需后台服务权限，请按发行版文档配置 `uinput`。
2. **B 档需约 21GB 显存**：本地 Qwen3.8-27B 未启动或显存不足时，1.5s 超时后自动降级为 A 档上屏，
   可在日志看到 `LLM 纠错失败，降级为 Level 1 结果`，属正常现象。
3. **C 档需联网 + 有效 Key**：Key 为空时面板拒绝切换；境外 API 请在 `control_center.proxy`
   或环境变量 `http_proxy`/`https_proxy` 中配置代理。
4. **D 档单次约 2~3s**：走 `opencode run` 子进程，受登录态与网络影响；CLI 异常/超时自动降级。
   注意 OpenCode 免费额度**只能走本机 CLI**，直接 HTTP 调推理接口会被 `FreeTierError` 拒绝，
   故本项目不做 HTTP 对接（见 `voice_input/post_processor.py:polish_opencode_cli`）。
5. **终端粘贴用 `Ctrl+Shift+V`**：面板/守护进程自动检测焦点窗口类别；若某终端模拟器用了
   自定义粘贴键，请提 issue 附 `xprop WM_CLASS` 输出。
6. **剪贴板工具缺失**：X11 需 `xclip`/`xsel` + `xdotool`，Wayland 需 `wl-clipboard`；
   `main.py` 启动时会自动把 conda 环境 `bin/` 前置进 `PATH`，仍报错请 `sudo apt-get install -y xclip xdotool`。
7. **`DISPLAY` 探测**：后台启动无 `DISPLAY` 时按 `/tmp/.X11-unix/X*` 自动探测；
   纯 Wayland 会话返回 `None` 走 Wayland 路径。SSH 远程启动请先 `echo $DISPLAY` 确认。
8. **USB 麦克风热插拔**：采集线程持续运行、断开自动重连；换设备无需改代码，
   `preferred_device_substring` 留空即永远用系统默认麦。
9. **提示音不响**：确认 `paplay` 可用且 `feedback.enable_sound: true`，`assets/*.wav` 存在。

仍未解决？请按顺序执行并贴输出提 issue：`./run.sh status` → `./run.sh test` →
`tail -n 50 voice-input.log` / `tail -n 50 dashboard.log`。

---

## 目录结构

```
linux-voice-input/
├── main.py                        # 主控程序（守护进程 / 自检 / --once）
├── config.example.yaml            # 配置模板（仓库追踪）
├── config.yaml                    # 本地真实配置（git 忽略，需自行 cp 创建）
├── run.sh                         # 管理脚本（start/stop/status/fg/test/dashboard/mode/install-desktop）
├── setup.sh                       # 一键部署（conda 环境 / 依赖 / 模型 / 自检，免 sudo 可用）
├── voice-input-dashboard.desktop  # 应用菜单快捷方式
├── README.md / README_EN.md       # 中英文档
├── voice_input/                   # 功能模块
│   ├── audio_capture.py           # 麦克风采集 + VAD（自动回退默认麦 / 热插拔重连）
│   ├── asr_engine.py              # SenseVoice 识别（int8 + warmup + ITN）
│   ├── post_processor.py          # 两级后处理（Level 1 规则 + Level 2/B/C/D LLM 纠错）
│   ├── text_injector.py           # 剪贴板注入（X11 + Wayland 双栈，终端自动判别）
│   ├── hotkey_daemon.py           # 双键独立双模（PTT + Toggle，组合键豁免）
│   ├── control_center.py          # 面板后端（标准库 http.server，零新增依赖）
│   └── dashboard.html             # 面板前端
├── models/                        # SenseVoice + Silero VAD 模型（setup.sh 自动下载）
└── assets/                        # start.wav / stop.wav 提示音
```

---

## 开源协议与致谢

- 本项目采用 **MIT License**。详见 [LICENSE](LICENSE)（若缺失则以 SPDX `MIT` 为准，
  欢迎提 PR 补上正式文本）。
- 致谢：
  - [Sherpa-ONNX](https://github.com/k2-fsa/sherpa-onnx) —— 跨平台本地语音推理引擎；
  - [SenseVoice](https://github.com/FunAudioLLM/SenseVoice)（阿里通义）—— 中英粤日韩多语言 ASR 模型；
  - [Silero VAD](https://github.com/snakers4/silero-vad) —— 轻量语音端点检测，用于连续听写自动断句。

# Linux Voice Input：把口述内容输入当前窗口

这个工具来自我在 Linux 上日常输入文字的需求：戴着耳机时，希望按住一个键说话，再把识别出的文字直接输入正在使用的编辑器、终端或聊天窗口；需要连续说一段时，也希望短暂停顿不会把一句话切得过碎。

它是总仓库里负责“语音变成文字”的部分。GM400 和 AirPods 模块分别解决耳机接入问题；本模块使用系统已有的麦克风，可以独立运行，不要求某一款耳机。

## 一次使用是什么样的

- **按住说话**：按住右 Shift 或 F9，说完松开，整段音频识别后粘贴到当前焦点窗口。
- **连续听写**：按一下右 Ctrl 开始，短暂停顿保留在同一段中，较长静音后整体识别；再按一下结束并提交剩余内容。
- **可选纠错**：本地 SenseVoice 识别后先做规则清洗；按自己的需要，再配置本地或云端 LLM。默认关闭 LLM。

按键可以修改。文本通过剪贴板粘贴；终端与普通应用使用相应的粘贴快捷键，实际效果仍取决于桌面环境与目标应用。

## 连续听写

Toggle 模式下，VAD 先标记语音段。相邻语音之间短于提交阈值的停顿会保留在同一段音频中；静音达到 `audio.commit_silence_duration` 后，累积音频整体送去识别。`audio.max_utterance_seconds` 是自然停顿切分的软目标：超过该时长后，会在下一个可用停顿处切分。若持续没有停顿，60 秒硬安全上限会强制提交当前音频。再次按 Toggle 键会结束录音并提交剩余有效语音。

主要设置位于 `config.yaml` 的 `audio` 段：

| 设置 | 默认值 | 作用 |
|---|---:|---|
| `min_silence_duration` | `0.6` 秒 | VAD 标记停顿的粒度 |
| `commit_silence_duration` | `1.6` 秒 | 静音达到该值后整体提交累积音频 |
| `max_utterance_seconds` | `20.0` 秒 | 超过后等待自然停顿再切分；持续无停顿时由 60 秒硬安全上限兜底 |
| `session_dump_dir` | 空 | 可选 WAV 调试记录目录；留空时不保存 |

## 安装和运行

本项目位于 monorepo 根目录下的 `voice-input/`。从 `linux-audio-toolkit` 仓库根目录运行：

```bash
cd voice-input
./setup.sh
./run.sh start
```

`setup.sh` 在 `config.yaml` 不存在时从 `config.example.yaml` 创建初始配置，已有配置会保留。模型和系统依赖需按目标 Linux 发行版准备；桌面输入、音频采集及 Wayland 行为取决于主机环境，本仓库未对特定硬件或 Wayland 配置作验证声明。

常用命令：

```bash
./run.sh status
./run.sh stop
./run.sh test-unit
./run.sh test
./run.sh dashboard
```

`test-unit` 运行不需要麦克风或模型的逻辑单测；`test` 会运行端到端自检，并可能访问当前音频/桌面环境。

### 登录自启（可选）

希望登录后不用手动 `./run.sh start`，可以安装 XDG autostart 入口：

```bash
./run.sh install-autostart     # 写入 ~/.config/autostart/voice-input.desktop
./run.sh uninstall-autostart   # 移除该入口
```

安装时会把 `voice-input-autostart.desktop.in` 中的 `@PROJECT_DIR@` 渲染成当前项目的绝对路径，因此仓库移动后需要重新安装。`run.sh autostart` 会先等待几秒让登录会话和音频服务就绪，启动失败会重试，结果记录在 `autostart.log`。该入口依赖 X11 全局热键和登录会话的音频服务，主要用于 X11 桌面（如 GNOME on Xorg）。

## 配置与隐私

`config.example.yaml` 是可提交的模板。真实 API 凭据应仅放在本地 `config.yaml`，不要提交该文件。会话录音默认关闭；仅在明确配置 `audio.session_dump_dir` 时写入 WAV 文件，请妥善处理这些录音。

## 许可证

MIT，见仓库根目录的 `LICENSE`。

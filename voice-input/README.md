# Linux Voice Input

Linux 桌面本地语音输入工具，支持按住说话（PTT）和连续听写（Toggle）。语音识别使用本地 `sherpa-onnx` / SenseVoice 模型；识别文本可经过规则后处理，并可按配置调用 OpenAI 兼容的本地或远端接口。

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

## 配置与隐私

`config.example.yaml` 是可提交的模板。真实 API 凭据应仅放在本地 `config.yaml`，不要提交该文件。会话录音默认关闭；仅在明确配置 `audio.session_dump_dir` 时写入 WAV 文件，请妥善处理这些录音。

## 许可证

MIT，见仓库根目录的 `LICENSE`。

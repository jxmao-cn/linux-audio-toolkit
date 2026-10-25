# Linux Audio Toolkit

[English](README_EN.md) · [MIT License](LICENSE)

这个仓库来自我日常使用 Linux 时遇到的几件小事：把有线耳机接上电脑后能正常听声音和用麦克风，让 AirPods 更方便地配对、重连和切换用途，以及直接对着耳机说话，把文字输入当前窗口。

**HECATE GM400 是我日常使用的一副有线耳机的型号。** 它在这台电脑上通过 USB 接入。下面的 GM400 模块就是为了解决这副耳机在 Linux 上的识别和音频恢复问题；AirPods 模块处理我另一副蓝牙耳机的连接，Voice Input 则让可用的麦克风变成语音输入工具。

这些工具起初是为了让自己的电脑用起来更顺手。现在把相关脚本、程序和排障经验整理到一起，希望遇到相似问题的人也能复用，或者据此适配自己的设备。

## 这里的工具分别解决什么

| 日常场景 | 对应工具 | 它做的事情 |
| --- | --- | --- |
| 有线耳机接上了，但桌面里没有可用的播放或录音设备 | [HECATE GM400 有线耳机适配](headsets/gm400/README.md) | 等待 USB 音频设备就绪，在需要时帮助 PulseAudio 重新发现设备、恢复输入输出 |
| 想在 Linux 上使用 AirPods 听声音或说话，方便地处理配对、重连和音频模式 | [AirPods 管理工具](headsets/airpods/README.zh-CN.md) | 提供连接、状态、电量和 A2DP/HFP 切换命令，以及可选的后台重连服务 |
| 想把口述内容直接写进正在使用的编辑器、终端或聊天窗口 | [Linux 语音输入](voice-input/README.md) | 按住快捷键说话，或开启连续听写；本地识别后把文字粘贴到当前窗口 |

这些工具通过 Linux 的音频系统配合，各模块可以单独使用。语音输入可以使用系统默认麦克风，不要求拥有 GM400 或 AirPods。GM400 的恢复脚本则针对我的这款设备和 PulseAudio 环境，其他耳机需要按实际情况适配。

## 当前记录的系统环境

下面是维护者这台电脑在 2026-10-25 查询到的版本，用来说明项目开发和日常使用的基线。它不代表所有 Linux 发行版的测试结果；不同发行版和音频后端仍需单独验证。

| 项目 | 当前版本 |
| --- | --- |
| 发行版 | Ubuntu 22.04.5 LTS（Jammy Jellyfish），x86_64 |
| Linux 内核 | 6.8.0-138-generic |
| BlueZ | 5.64 |
| PulseAudio | 15.99.1 |

本机的 GM400 有线耳机通过 USB-C 接入，对应 USB 标识 `35bb:a20f`，ALSA 声卡 ID 为 `GM400`。AirPods 项目实际面向 **AirPods 4（主动降噪款）**。这次查询时两个耳机都没有连接，因此这些设备信息是项目维护记录，不代表本次进行了音频实测。

## 从自己的需求开始

先下载仓库，再按需要选择一个模块：

```bash
git clone https://github.com/jxmao-cn/linux-audio-toolkit.git
cd linux-audio-toolkit
```

- **想试语音输入**：从 [Voice Input 使用说明](voice-input/README.md) 开始，创建环境、准备模型，再启动快捷键听写。
- **同样在用 GM400，遇到识别问题**：先读 [有线耳机模块说明](headsets/gm400/README.md)，确认现象和音频后端，再安装恢复服务。
- **想管理 AirPods**：按 [AirPods 安装和配对说明](headsets/airpods/README.zh-CN.md) 准备 BlueZ、音频工具和 Python 环境。

耳机模块安装后，需要显式启用相应服务。如果 GM400 与 AirPods 同时连接，还要选择自己想用的默认输入输出；目前两个模块没有统一的设备优先级管理器，具体行为见 [架构说明](docs/architecture.md)。

## 配置与个人数据

个人配置从 `voice-input/config.example.yaml` 初始化。API 密钥、模型、日志、录音和蓝牙设备状态留在本机。

语音识别与规则清洗可以在本地完成，默认关闭 LLM 纠错。主动启用云端纠错或 OpenCode CLI 后，识别文本会发送给所选服务。语音管理面板默认只监听本机 `127.0.0.1`。

## 开发与验证

从仓库根目录分别执行，各模块依赖见对应说明：

```bash
# 语音输入：Python 3.10+
(cd voice-input && python -m unittest discover -s tests -v && python tests/test_utterance_accumulator.py)

# GM400：不连接实际耳机的模拟测试
headsets/gm400/tests/run.sh

# AirPods：Python 3.9+
(cd headsets/airpods && PYTHONPATH=src python -m unittest discover -s tests -t . -v)
```

CI 检查程序逻辑、安装行为与打包。实际耳机、固件、桌面和音频后端的差异仍需实机确认；各模块说明中列出了目前的适用范围。

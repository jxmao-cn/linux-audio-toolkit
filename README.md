# Linux Audio Toolkit

[English](README_EN.md) · [MIT License](LICENSE)

Linux 桌面的耳机接入与语音输入工具集。设备接入层负责让系统看见耳机与麦克风，语音输入层负责采集、识别和向当前窗口输入文字。各模块可以单独安装。

| 模块 | 作用 | 状态与入口 |
| --- | --- | --- |
| [Voice Input](voice-input/README.md) | SenseVoice 本地识别、按住说话、连续听写、可选 LLM 纠错与管理面板 | 收录本机后续修改，包含整段语音累积和离线回放 |
| [HECATE GM400](headsets/gm400/README.md) | USB 声卡延迟就绪后的 PulseAudio 发现与恢复 | watcher、用户服务、安装与卸载工具 |
| AirPods | 蓝牙配对、连接与音频模式管理 | 尚未收录；后续集成约定见 [架构说明](docs/architecture.md) |

## 开始使用

```bash
git clone https://github.com/jxmao-cn/linux-audio-toolkit.git
cd linux-audio-toolkit/voice-input
./setup.sh
./run.sh start
```

GM400 的安装方式、服务启动方式及恢复动作见 [模块说明](headsets/gm400/README.md)。不使用 GM400 时，语音输入仍然可以使用系统默认麦克风。

## 本地配置与数据

- 从 `voice-input/config.example.yaml` 生成个人 `config.yaml`；个人配置、密钥、模型、日志和录音不提交。
- 默认关闭 LLM：ASR 与规则清洗在本机执行。启用云端纠错或 OpenCode CLI 后，识别文本会发往所选服务，整条链路不再是离线运行。
- GM400 watcher 可以改变系统默认输入输出；语音输入也可以通过配置优先选择特定设备。
- 管理面板默认监听 `127.0.0.1`，可管理本机进程与可选模型容器，仅供可信的本机用户使用。

## 开发与验证

```bash
# Python 3.10+ 开发环境；不需要麦克风或模型
cd voice-input
python -m unittest discover -s tests -v
python tests/test_utterance_accumulator.py
cd ../headsets/gm400
./tests/run.sh
```

CI 检查 Python 单元测试与 shell 脚本。通过这些检查只说明离线逻辑通过；实体设备、不同桌面和音频后端仍需要实机验证。

## 来源与迁移

语音输入源于 [linux-voice-input](https://github.com/jxmao-cn/linux-voice-input)，本仓库同步了其后续本地修改。原仓库作为早期版本入口保留。各模块的安装与运行目录仍独立，迁移无需替换正在使用的程序。详见 [迁移说明](docs/migration.md)。

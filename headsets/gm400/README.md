# HECATE GM400 有线耳机：Linux 音频识别与恢复

**HECATE GM400 是我日常使用的一副有线耳机的型号。** 在这台 Linux 电脑上，它通过 USB 接入，可以用来听声音和通过麦克风说话。这里的 `gm400-watch` 是围绕这副耳机编写的恢复脚本。

## 为什么会有这个工具

我遇到的现象是：耳机已经接上，Linux 底层也能看到这张 USB 声卡，但桌面的音频系统里有时还没有可用的播放或录音设备。这样一来，听声音、使用耳机麦克风，以及后续的语音输入都会受影响。

原始脚本就是为了解决这个日常使用问题：让电脑等耳机准备好后再恢复音频设备，减少反复检查声卡、重新探测和手动选择输入输出的操作。现在把脚本和安装方式整理出来，供使用同款耳机、遇到相似问题的人参考。

如果你的耳机已经能正常播放和录音，可以直接使用系统音频设置或 [语音输入模块](../../voice-input/README.md)。这个模块主要适合排查 GM400 的识别和恢复问题。

## 它怎样工作

这款设备在 ALSA 中出现时，USB 固件可能仍未准备好，而 PulseAudio 可能错过后续的设备就绪。因此脚本：

1. 识别声卡 ID 为 `GM400` 的设备，发现后等待 120 秒。
2. 检查 PulseAudio 是否已有对应的播放与录音端点；齐全时选择它们作为默认输入输出。
3. 如果设备不完整，尝试加载对应声卡模块，或重启 PulseAudio 恢复端点。每次观察到的声卡枚举最多执行一次恢复探测。

脚本不会卸载全局 `module-udev-detect`。默认声卡 ID 和名称匹配片段可配置；它按 `/proc/asound/card*/id` 找设备，不依赖某台电脑上的固定声卡编号。

## 支持范围与限制

本模块面向 PulseAudio。PipeWire 的 PulseAudio 兼容服务尚未验证，因此不宣称兼容。恢复成功时，看护器可能把默认输入和默认输出切换到 GM400。如果 PulseAudio 已有声卡、但输入或输出端点不完整，看护器可能重启 `pulseaudio.service`；这会中断所有 PulseAudio 音频流，也可能影响其他应用。

目标设备是上述 HECATE GM400 有线耳机；本机 USB 标识为 `35bb:a20f`，ALSA 声卡 ID 为 `GM400`。原始恢复方案来自本机对此设备的使用配置；本次整理后的通用脚本只经过下文所述的 stub 模拟测试，尚未重新进行实机验收。其他声卡 ID、音频 profile 或 PulseAudio 命名方式需要配置并单独验证。

## 安装

从总仓库根目录进入本模块后执行：

```sh
cd headsets/gm400
./install.sh
```

脚本把看护器安装到 `~/.local/bin`，把 systemd unit 安装到 `${XDG_CONFIG_HOME:-~/.config}/systemd/user`。安装不会启用或启动服务。需要使用时，请显式执行：

```sh
systemctl --user daemon-reload
systemctl --user enable --now gm400-watch.service
```

写入前，安装脚本会检查两个目标。如果目标中存在与本包不同的文件，安装会停止，不覆盖文件，也不会留下只安装一半的结果。请先自行检查或备份冲突文件再重试。由于 unit 使用 `%h/.local/bin`，`XDG_BIN_HOME` 必须保持默认值 `~/.local/bin`；`XDG_CONFIG_HOME` 可以自定义。

若要删除仍与本包完全一致的文件，运行 `./uninstall.sh`。卸载脚本不会停止或禁用服务；如需停用，请显式执行：

```sh
systemctl --user disable --now gm400-watch.service
```

离线安装检查可以设置 `DESTDIR`，例如：

```sh
DESTDIR=/tmp/gm400-stage ./install.sh
```

也可以设置 `XDG_CONFIG_HOME` 指向临时配置目录。命令帮助见 `./install.sh --help` 和 `./uninstall.sh --help`。

## 配置

可通过 systemd 用户服务 drop-in 设置以下环境变量。unit 使用 `%h` 解析 home 目录下的程序路径。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `GM400_CARD_ID` | `GM400` | 要监测的精确 ALSA 声卡 ID |
| `GM400_READY_DELAY` | `120` | 枚举后等待多少秒再探测 |
| `GM400_PA_CARD_MATCH` | `GM400` | PulseAudio 声卡属性 `alsa.card_name` 的匹配片段 |
| `GM400_PA_SINK_MATCH` | `GM400` | 查找输出 sink 的名称片段 |
| `GM400_PA_SOURCE_MATCH` | `GM400` | 查找输入 source 的名称片段 |
| `GM400_MODULE_NAME` | `usb-GM400` | 传给 `module-alsa-card` 的名称 |
| `GM400_MODULE_CARD_NAME` | `alsa_card.usb-GM400` | 传给模块的声卡名称 |

不要在这些配置值中放入凭据或其他秘密。例如，名称不同的兼容设备可以设置 `GM400_CARD_ID=USB_AUDIO`、`GM400_PA_SINK_MATCH=USB` 和 `GM400_PA_SOURCE_MATCH=USB`。使用前应先查看实际 `pactl` 输出并验证匹配结果。

`~/.local/bin/gm400-watch.sh --once` 执行一次检查后退出。它仍会遵循新枚举后的 120 秒等待期；如果在等待期内运行，脚本会直接退出，不会探测或绕过延迟。设置 `GM400_WATCH_LIBRARY=1` 后 source 脚本，则只加载 `find_target_card` 与 `check_once` 函数，不启动看护循环。

## 验证边界

测试使用临时 ALSA proc 数据，以及 PATH 中的 `pactl`、`systemctl`、`timeout` 和时钟 stub。测试覆盖无设备时不修改音频状态、等待期内不探测、每次枚举最多一次探测、恢复失败返回真实状态、已健康设备且默认输入输出正确时不改动默认设备、不会选择 `.monitor` source、设备重新枚举后允许再次探测，以及不卸载全局 `module-udev-detect`。这些模拟测试不代表已在真实 GM400、其他固件版本或桌面音频栈上重新验收。

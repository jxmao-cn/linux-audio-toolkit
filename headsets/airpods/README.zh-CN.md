# airpodsd

**Linux 上的 AirPods 用户态驱动。** 配对、自动重连、电量读取、音频配置切换、
故障诊断——一个无第三方 pip 依赖的命令行工具，外加一个 systemd 用户服务。

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey.svg)](#环境要求)
[![English](https://img.shields.io/badge/docs-English-blue.svg)](README.md)

下面的状态和配对输出使用模拟数据（AirPods Pro 2 和虚构地址），只用于展示命令格式；我实际使用的型号是 AirPods 4 主动降噪款。

```text
$ airpodsctl status
AirPods Pro (2nd generation)
  Address    AC:12:34:56:78:90
  Adapter    hci0
  Link       connected · -48 dBm
  Audio      A2DP (SBC) · bluez_output.AC_12_34_56_78_90.1
  Battery    L 82%   R 79%   Case 65%
```

---

## 为什么做这个工具

这是我日常 Linux 使用环境的一部分：除了 HECATE GM400 有线耳机，我也使用 AirPods，希望在同一台电脑上方便地连接耳机、听声音和使用麦克风。连接不顺利时，我需要知道问题出在蓝牙连接，还是音频模式与输入源。

因此这个模块把配对、重连、状态查询和 A2DP/HFP 切换整理成命令行工具，并提供可选的后台服务。它使用系统已有的 BlueZ 和音频服务，目标是减少自己日常使用时的重复操作，也给遇到类似问题的人一个可以检查、修改和复用的实现。

## 我的耳机型号：AirPods 4 主动降噪款

本模块针对我实际使用的 **AirPods 4（主动降噪款，AirPods 4 with Active Noise Cancellation）**。Apple 公布的主要参数如下：

| 参数 | 规格 |
| --- | --- |
| 发布年份 | 2024 |
| 芯片 | H2 |
| 无线连接 | Bluetooth 5.3 |
| 麦克风 | 双波束成形麦克风，另有内向麦克风 |
| 防护等级 | IP54（耳机与充电盒） |
| 单只耳机重量 | 4.3 g |
| 充电盒 | USB-C，带扬声器，34.7 g |
| 单次充电续航 | 开启主动降噪时最长约 4 小时；关闭噪声控制时最长约 5 小时 |
| 加充电盒续航 | 开启主动降噪时最长约 20 小时；关闭噪声控制时最长约 30 小时 |

尺寸、续航等为 Apple 在其测试条件下公布的数值；本项目通过 Linux 蓝牙音频栈提供连接与音频控制，不承诺在 Linux 上提供 Apple 生态中的全部功能。详见 [Apple AirPods 4 主动降噪款技术规格](https://support.apple.com/en-gb/121204)。

如果还想把麦克风中的话直接输入当前窗口，可以配合 [语音输入模块](../../voice-input/README.md)。听声音和说话会涉及不同的蓝牙音频模式；具体设备和系统的支持情况见下文。

> **与 Apple 无关。** AirPods 是 Apple Inc. 的商标。本项目是独立实现，只使用
> 公开的蓝牙标准，不包含任何逆向工程代码。

## 功能

| | |
|---|---|
| **配对向导** | `airpodsctl pair` 扫描、选择、配对、信任并连接；BlueZ Agent 只确认你选中的设备 |
| **自动重连** | `airpodsd` 周期性寻找已配对的 AirPods 并重连 |
| **电量** | 读取标准 GATT 电池服务，显示固件暴露的电池格（通常是左耳、右耳、充电盒） |
| **通知** | 连接、断开、低电量桌面通知 |
| **音频配置** | 一条命令在高质量 A2DP 与可用麦克风的 HFP 之间切换 |
| **诊断** | `airpodsctl doctor` 检查 BlueZ、rfkill、ControllerMode、PulseAudio 模块与 A2DP 端点 |
| **无需 root** | 全程以普通用户运行；不装 udev 规则、不编译内核模块、不需要 setuid |
| **无 pip 依赖** | 只用 Python 标准库；`dbus-python` 是桌面发行版自带组件 |
| **PulseAudio + PipeWire** | 通过 `pactl` 控制，两者都兼容 |
| **可测试** | `--mock` 模拟后端，没有蓝牙硬件也能跑通全部界面与测试 |

## 支持情况

| 能力 | 状态 | 说明 |
|---|---|---|
| A2DP 播放 | ✅ | Ubuntu 22.04 默认 SBC；可用编解码器取决于发行版 |
| HFP 麦克风 | ✅ | `airpodsctl audio hfp`；窄带语音质量属正常现象 |
| 左右耳/盒电量 | ⚠️ | 尽力而为：GATT `0x180F` / `0x2A19`；顺序可配置，部分固件只暴露一个总值 |
| 自动重连 | ✅ | 守护进程运行时 |
| 低电量通知 | ✅ | 阈值可配置 |
| AAC / LDAC | ⚠️ | 由 BlueZ 与 PulseAudio/PipeWire 决定；本项目不捆绑编解码器 |
| 降噪 / 通透 | ❌ | 需要 Apple 私有 AAP 协议（路线图） |
| 入耳检测 | ❌ | 同上 |
| 空间音频 / 头部追踪 | ❌ | 目前为 Apple 独占 |
| 固件升级 | ❌ | 请使用 Apple 设备 |
| 多点连接管理 | ❌ | AirPods 同一时间只连接一台主机 |

## 环境要求

- Linux，**BlueZ ≥ 5.55**（推荐 5.64+）
- **PulseAudio ≥ 13** 或 **PipeWire ≥ 0.3.48**（含 `pipewire-pulse`）
- Python **3.9+**
- `python3-dbus`、`python3-gi`、`pactl`（`pulseaudio-utils`）

原项目开发环境为 Ubuntu 22.04 / BlueZ 5.64 / PulseAudio。本次集成通过
无硬件单元测试验收，没有重新执行配对、录音等实机测试。PipeWire 的 profile
名称兼容由解析测试覆盖，不能据此推断全部设备和音频后端均已通过实机验证。

## 安装

```bash
git clone https://github.com/jxmao-cn/linux-audio-toolkit.git
cd linux-audio-toolkit/headsets/airpods
./install.sh
```

`install.sh` 会把包装到用户目录，写入
`~/.config/systemd/user/airpodsd.service`。默认不启用或启动服务；显式传入
`--enable` 才会启用。已有不同内容的服务文件默认保留；`--replace-unit` 会先
备份再替换。可用 `AIRPODSD_PYTHON` 指定解释器，卸载时应使用同一解释器。
只想装命令行工具：

```bash
./install.sh --no-service
```

手动安装：

```bash
python3 -m pip install --user .
airpodsctl doctor
```

如果发行版禁止 `pip install --user`（PEP 668），可以创建一个能看到系统
D-Bus 绑定的 venv：

```bash
python3 -m venv --system-site-packages ~/.local/share/airpodsd/venv
~/.local/share/airpodsd/venv/bin/pip install .
~/.local/share/airpodsd/venv/bin/airpodsctl doctor

# 用这个 venv 安装 systemd 用户服务（启用服务是独立操作）
AIRPODSD_PYTHON=~/.local/share/airpodsd/venv/bin/python \
AIRPODSD_BIN=~/.local/share/airpodsd/venv/bin/airpodsd ./install.sh --no-install
```

卸载：`./uninstall.sh`。

## 配对

1. 把两只耳机**放回充电盒**，打开盒盖。
2. 让耳机进入配对模式，保持盒盖打开：
   - **AirPods 4 / 4 ANC 没有实体按钮**：快速**双击充电盒正面、指示灯下方**，
     直到指示灯**白色闪烁**。
   - **AirPods 1–3、Pro、Max**：长按充电盒背面的按钮，直到指示灯白色闪烁。
3. 运行：

```bash
airpodsctl pair
```

```text
Scanning for 30s...
Put the AirPods in pairing mode: open the case, keep the buds inside.
AirPods 1-3 / Pro / Max: hold the case button. AirPods 4 / 4 ANC: double-tap the front of the case below the LED.
Wait for the light to flash white.

Pairing with AirPods Pro (2nd generation) (AC:12:34:56:78:90)...
Paired. Connecting...
Connected.  AirPods Pro (2nd generation)
Audio profile: A2DP (SBC) · bluez_output.AC_12_34_56_78_90.1
Battery: L 82%   R 79%   Case 65%

Default device saved to /home/you/.config/airpodsd/config.json
```

地址会保存为默认设备，后续命令无需再带 MAC。

```bash
airpodsctl connect       # 重新连接
airpodsctl status        # 连接、音频配置、电量
airpodsctl battery       # 带进度条的电量
airpodsctl disconnect
airpodsctl forget        # 删除配对，便于彻底重置
```

## 守护进程

```bash
systemctl --user daemon-reload
systemctl --user enable --now airpodsd
systemctl --user status airpodsd
journalctl --user -u airpodsd -f
```

守护进程会：

- 登录后自动启动并等待蓝牙可用；
- 耳机断开时每 2 分钟扫描一次；
- 发现耳机后自动重连；
- 每分钟读取电量，在 20% / 10% 时通知；
- 连接后默认切到 A2DP（可配置）；
- 把状态写入 `~/.local/state/airpodsd/state.json`，方便状态栏或脚本读取。

配对始终是显式操作：守护进程不会自行接受新设备。

## 音频配置

```bash
airpodsctl audio          # 当前 card / profile / sink / source
airpodsctl audio a2dp     # 听音乐：高质量，无麦克风
airpodsctl audio hfp      # 通话：麦克风可用，播放质量下降
```

PulseAudio 的 `module-bluetooth-policy` 可能在应用打开麦克风时自动切到 HFP。
`airpodsctl` 用于在它判断错误时手动纠正。

若希望每次重连后都能使用麦克风，将 `~/.config/airpodsd/config.json` 中的
`audio_profile` 设为 `"hfp"`，然后重启 `airpodsd.service`。守护进程会将
AirPods 输入设为默认麦克风；音频服务尚未创建输入源时会重试。兼容 PulseAudio
的下划线命名（如 `handsfree_head_unit`）和 PipeWire 的连字符命名。HFP 的播放
质量低于 A2DP；如需交由桌面决定模式，可设为 `"keep"`。

## 诊断

```bash
airpodsctl doctor
```

```text
airpodsd doctor

[ OK ] Python — 3.10.12
[ OK ] Python D-Bus bindings — dbus-python and PyGObject are available
[ OK ] Bluetooth service — active
[ OK ] BlueZ — 5.64
[ OK ] Bluetooth adapter — hci0 (00:11:22:33:44:55)
[ OK ] rfkill — no blocks
[ OK ] Controller mode — default (dual)
[ OK ] Audio server — pulseaudio 15.99.1
[ OK ] Bluetooth audio modules — discover and policy modules are loaded
[ OK ] A2DP endpoints — the audio server registered an A2DP sink
[WARN] Paired AirPods — no AirPods are paired yet
       → put the AirPods in pairing mode and run `airpodsctl pair`
[ OK ] Configuration — /home/you/.config/airpodsd/config.json

0 failure(s), 1 warning(s)
```

发现真正的失败时退出码为 `1`，便于写进脚本或粘贴到 issue 里。

## 配置

`airpodsctl config path` 显示配置文件位置，`airpodsctl config init` 生成默认
配置。修改后重启守护进程。

```json
{
  "address": "AC:12:34:56:78:90",
  "adapter": null,
  "auto_connect": true,
  "scan_interval": 120,
  "scan_duration": 8,
  "connect_interval": 30,
  "battery_interval": 60,
  "battery_labels": ["left", "right", "case"],
  "notifications": true,
  "low_battery_threshold": 20,
  "critical_battery_threshold": 10,
  "audio_profile": "a2dp",
  "set_default_sink": false,
  "log_level": "info"
}
```

| 键 | 默认值 | 含义 |
|---|---|---|
| `address` | 自动 | 默认 AirPods MAC，由 `airpodsctl pair` 写入 |
| `adapter` | 自动 | 控制器 id（`hci0`）或地址 |
| `auto_connect` | `true` | 是否自动重连 |
| `scan_interval` | `120` | 后台扫描间隔（秒） |
| `scan_duration` | `8` | 每次扫描时长（秒） |
| `connect_interval` | `30` | 两次连接尝试的最小间隔（秒） |
| `battery_interval` | `60` | 电量轮询间隔（秒） |
| `battery_labels` | `["left","right","case"]` | 暴露多格电池时使用的标签 |
| `notifications` | `true` | 桌面通知 |
| `low_battery_threshold` | `20` | 低电量阈值 |
| `critical_battery_threshold` | `10` | 紧急低电量阈值 |
| `audio_profile` | `"a2dp"` | 连接后切换到 `a2dp`、`hfp` 或 `keep` |
| `set_default_sink` | `false` | 是否把 AirPods 设为默认输出 |
| `log_level` | `"info"` | 守护进程日志级别 |

**关于电池。** 不少 AirPods 固件会暴露三个**没有标签**的 GATT 电池特征；
也有固件只暴露一个总值（显示为 `Battery`）。三格时默认按左耳、右耳、充电盒
解释；如果你的设备顺序不同，请修改 `battery_labels`。欢迎提交带硬件抓包的 PR。

## 工作原理

```text
┌──────────────────────────────┐
│ airpodsctl / airpodsd        │
│ config · state · doctor      │
└──────────────┬───────────────┘
               │
      ┌────────┴─────────┐
      │                  │
┌─────▼──────┐    ┌──────▼─────────┐
│ BlueZ      │    │ PulseAudio /   │
│ system bus │    │ PipeWire       │
│            │    │ (pactl)        │
│ Agent1     │    │ card profiles  │
│ Device1    │    │ sinks/sources  │
│ GATT 0x180F│    │ default sink   │
└────────────┘    └────────────────┘
```

- 配对时注册一个临时 `org.bluez.Agent1`，**只**自动确认你选中的地址，然后
  配对、标记为受信任并连接。
- 电量来自标准 GATT Battery Service（`0x180F`）/ Battery Level
  （`0x2A19`），失败时回退到 BlueZ 聚合的 `org.bluez.Battery1`。
- 音频控制解析 `pactl list cards`，从声卡实际提供的 profile 中选择
  `a2dp-sink` 或 `headset-head-unit`。
- 守护进程的 tick 是纯状态机，测试里用假时钟驱动；GLib 只负责外层事件循环。

更多细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 故障排查

| 现象 | 先试这个 |
|---|---|
| 扫描不到 AirPods | 开盖、耳机在盒内、白灯闪烁（AirPods 4 双击正面）；运行 `airpodsctl scan --all` |
| 配对失败 | 先在 iPhone 的蓝牙列表里忽略它；运行 `airpodsctl doctor` |
| 已连接但没声音 | `airpodsctl audio a2dp`，再检查 `pactl list cards` |
| 没有麦克风 | `airpodsctl audio hfp` |
| 读不到电量 | 把耳机从盒子里拿出来；运行 `airpodsctl doctor` |
| 声音卡顿 | 靠近电脑，减少 2.4 GHz 干扰；若可用尝试 `a2dp-sink-sbc_xq` |
| 总被 iPhone 抢回去 | 在 iPhone 上关闭“自动连接”，或直接关闭 iPhone 蓝牙 |
| Ubuntu 22.04 能找到但配对失败 | 先试 `airpodsctl pair --bredr`；仍失败则临时设置 `ControllerMode = bredr`，见 [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) |

完整指南：[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)。

## 开发

```bash
make test     # 单元测试，无需硬件
make demo     # airpodsctl --mock status
make lint
```

测试覆盖电量解析、`pactl` 输出解析、配置校验、守护进程状态机，以及基于内存
后端的 CLI。

想添加机型、改进电池映射或实现 AAP，请阅读
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 路线图

- **基于 L2CAP 的 AAP**：精确电量、入耳检测、降噪/通透、自适应音频。
- **状态栏集成**（Waybar 模块、KDE plasmoid、GNOME 扩展）。
- **按真实麦克风占用自动切换 HFP**，而不只依赖 PulseAudio 策略。
- **Beats 及其他 Apple 音频设备。**
- **打包**：AUR、PPA、Nix flake。
- **CLI 本地化。**

## 常见问题

**需要 root 吗？**
不需要。只需用发行版包管理器安装依赖，之后全部以普通用户运行。

**这是内核驱动吗？**
不是。程序通过 BlueZ 管理蓝牙连接，通过 `pactl` 管理桌面音频，不包含内核模块。

**能提升音质吗？**
不能。它提供控制与可见性；编解码器由 BlueZ 和音频服务器协商。

**支持 Wayland 吗？**
支持。CLI 与守护进程不依赖 X11；通知走 freedesktop 通知服务。

**可以不用守护进程吗？**
可以。`airpodsctl connect`、`status`、`battery` 都按需工作。

## 许可证

MIT，见 [LICENSE](LICENSE)。

AirPods 与 Apple 是 Apple Inc. 的商标。本项目与 Apple 无关联，也未获其背书。

#!/usr/bin/env bash
# =============================================================================
# voice-input 一键迁移部署脚本
# -----------------------------------------------------------------------------
# 把整个 voice-input 文件夹拷贝到任意一台 Linux 机器上后，只需执行：
#
#     ./setup.sh
#
# 脚本将自动完成：
#   1. 检测 conda（miniconda3 / anaconda3 / miniforge3 / mambaforge），
#      找不到时可自动引导安装 Miniconda（可用 SKIP_CONDA_INSTALL=1 跳过）；
#   2. 创建名为 voice-input 的 conda 环境（Python 3.10）；
#   3. 安装 Python 依赖（numpy / scipy / requests / pyyaml / sounddevice /
#      sherpa-onnx / python-xlib）以及可选的系统命令行工具
#      （xclip / xdotool / paplay / pactl / parecord；Wayland 下 wl-clipboard / wtype）；
#   4. 下载缺失的模型（SenseVoice + Silero VAD）；
#   5. 运行端到端自检（./run.sh test）。
#
# 常用环境变量：
#   VOICE_INPUT_PYTHON   指定已存在的解释器，跳过创建环境
#   SKIP_CONDA_INSTALL=1 未检测到 conda 时放弃自动安装（直接报错退出）
#   http_proxy/https_proxy  境外网络下载时建议设置（如 http://127.0.0.1:7897）
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
ENV_NAME="voice-input"
PY_VERSION="3.10"

SENSEVOICE_DIR="$PROJECT_DIR/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
SENSEVOICE_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
SILERO_VAD_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
SILERO_VAD_PATH="$PROJECT_DIR/models/silero_vad.onnx"

info()  { printf '\033[1;32m[setup]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[setup]\033[0m %s\n' "$*" >&2; }
error() { printf '\033[1;31m[setup]\033[0m %s\n' "$*" >&2; }
die()   { error "$*"; exit 1; }

cd "$PROJECT_DIR" || die "无法进入项目目录：$PROJECT_DIR"

command -v curl >/dev/null 2>&1 || die "缺少 curl，请先安装：sudo apt-get install -y curl"

if [ -n "${http_proxy:-}${https_proxy:-}" ]; then
    info "检测到代理：http_proxy=${http_proxy:-未设} https_proxy=${https_proxy:-未设}"
fi

# --------------------------------------------------------------------------- #
# 1. 定位（或安装）conda
# --------------------------------------------------------------------------- #
find_conda() {
    if command -v conda >/dev/null 2>&1; then
        command -v conda
        return 0
    fi
    local base
    for base in \
        "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" \
        "$HOME/mambaforge" "$HOME/miniconda" "$HOME/anaconda" "$HOME/.conda" \
        /opt/miniconda3 /opt/anaconda3 /opt/miniforge3 /opt/conda; do
        if [ -x "$base/bin/conda" ]; then
            echo "$base/bin/conda"
            return 0
        fi
    done
    return 1
}

install_miniconda() {
    if [ "${SKIP_CONDA_INSTALL:-0}" = "1" ]; then
        return 1
    fi
    local installer="$PROJECT_DIR/.miniconda_installer.sh"
    local prefix="${HOME}/miniconda3"
    info "未检测到 conda，尝试自动安装 Miniconda 到 $prefix …"
    local arch url
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) url="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh" ;;
        aarch64|arm64) url="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-aarch64.sh" ;;
        *) warn "不支持的 CPU 架构：$arch"; return 1 ;;
    esac
    if ! curl -fL --retry 3 -o "$installer" "$url"; then
        warn "下载 Miniconda 安装器失败"
        return 1
    fi
    if ! bash "$installer" -b -p "$prefix"; then
        warn "Miniconda 安装失败"
        rm -f "$installer"
        return 1
    fi
    rm -f "$installer"
    info "Miniconda 安装完成：$prefix"
    echo "$prefix/bin/conda"
}

CONDA="$(find_conda || true)"
if [ -z "${CONDA:-}" ] && [ -z "${VOICE_INPUT_PYTHON:-}" ]; then
    CONDA="$(install_miniconda || true)"
fi
if [ -z "${CONDA:-}" ] && [ -z "${VOICE_INPUT_PYTHON:-}" ]; then
    die "未找到 conda，且自动安装失败。请手动安装 Miniconda 后重试，或用 VOICE_INPUT_PYTHON 指定解释器。"
fi
[ -n "${CONDA:-}" ] && info "使用 conda：$CONDA"

# --------------------------------------------------------------------------- #
# 2. 解析 / 创建 voice-input 环境
# --------------------------------------------------------------------------- #
ENV_PY=""

if [ -n "${VOICE_INPUT_PYTHON:-}" ]; then
    [ -x "$VOICE_INPUT_PYTHON" ] || die "VOICE_INPUT_PYTHON=$VOICE_INPUT_PYTHON 不可执行"
    ENV_PY="$VOICE_INPUT_PYTHON"
    ENV_DIR="$(dirname "$(dirname "$ENV_PY")")"
    info "使用用户指定的解释器：$ENV_PY"
else
    CONDA_BASE="$("$CONDA" info --base 2>/dev/null || true)"
    [ -n "$CONDA_BASE" ] || die "无法获取 conda base 路径"
    ENV_DIR="$CONDA_BASE/envs/$ENV_NAME"
    ENV_PY="$ENV_DIR/bin/python"

    if [ ! -x "$ENV_PY" ]; then
        info "创建 conda 环境 $ENV_NAME (python=$PY_VERSION) …"
        "$CONDA" create -y -n "$ENV_NAME" "python=$PY_VERSION" portaudio xclip xdotool -c conda-forge \
            || die "conda 创建环境失败"
    else
        info "已存在 conda 环境 $ENV_NAME：$ENV_PY"
    fi
fi

[ -x "$ENV_PY" ] || die "未找到可用解释器：$ENV_PY"

# --------------------------------------------------------------------------- #
# 3. 安装 Python 依赖
# --------------------------------------------------------------------------- #
info "升级 pip 并安装 Python 依赖（numpy / scipy / requests / pyyaml / sounddevice / sherpa-onnx / onnxruntime / pynput / python-xlib）…"
"$ENV_PY" -m pip install --upgrade pip || warn "升级 pip 失败，继续尝试安装依赖"
"$ENV_PY" -m pip install -U \
    "numpy" "scipy" "requests" "pyyaml" "sounddevice" "sherpa-onnx>=1.10" "onnxruntime" "pynput" "python-xlib" \
    || die "Python 依赖安装失败（若为网络问题，请设置 http_proxy/https_proxy 后重试）"

# --------------------------------------------------------------------------- #
# 4. 安装可选的系统命令行工具（best-effort，需 sudo）
# --------------------------------------------------------------------------- #
APT_PKGS="xclip xdotool pulseaudio-utils"
if [ "$(command -v wl-copy 2>/dev/null || true)" = "" ] || [ "${INSTALL_WAYLAND_TOOLS:-0}" = "1" ]; then
    APT_PKGS="$APT_PKGS wl-clipboard"
fi
if command -v apt-get >/dev/null 2>&1; then
    SUDO=""
    [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
    info "尝试安装系统工具：$APT_PKGS（可能需要 sudo 密码）…"
    if $SUDO apt-get install -y $APT_PKGS >/dev/null 2>&1; then
        info "系统工具安装完成。"
    else
        warn "系统工具安装失败或未授权，请手动安装：sudo apt-get install -y $APT_PKGS"
        warn "（仅影响剪贴板 / 按键注入，不影响识别本身）"
    fi
else
    warn "未检测到 apt-get，请手动确保已安装：$APT_PKGS"
fi

# 把 conda 环境 bin 加入 PATH 后，xclip/xdotool 等由该环境提供的工具也能被找到。
if [ -n "${ENV_DIR:-}" ] && [ -x "$ENV_DIR/bin/xclip" ]; then
    info "检测到环境内置的 xclip/xdotool：$ENV_DIR/bin"
fi

# --------------------------------------------------------------------------- #
# 5. 下载缺失的模型
# --------------------------------------------------------------------------- #
mkdir -p "$PROJECT_DIR/models"

# 5.1 Silero VAD
if [ -f "$SILERO_VAD_PATH" ]; then
    info "Silero VAD 已存在：$SILERO_VAD_PATH"
else
    info "下载 Silero VAD 模型 …"
    if curl -fL --retry 3 -o "$SILERO_VAD_PATH.part" "$SILERO_VAD_URL"; then
        mv "$SILERO_VAD_PATH.part" "$SILERO_VAD_PATH"
        info "Silero VAD 下载完成。"
    else
        rm -f "$SILERO_VAD_PATH.part"
        warn "Silero VAD 下载失败（toggle 模式将退化为 RMS 能量端点检测）。"
    fi
fi

# 5.2 SenseVoice
if [ -f "$SENSEVOICE_DIR/model.int8.onnx" ] || [ -f "$SENSEVOICE_DIR/model.onnx" ]; then
    info "SenseVoice 模型已存在：$SENSEVOICE_DIR"
else
    info "下载 SenseVoice 模型（约 150MB，请耐心等待）…"
    tmp_tar="$PROJECT_DIR/.sensevoice.tar.bz2"
    if curl -fL --retry 3 -o "$tmp_tar" "$SENSEVOICE_URL"; then
        if tar -xjf "$tmp_tar" -C "$PROJECT_DIR/models"; then
            info "SenseVoice 模型解压完成。"
        else
            warn "SenseVoice 解压失败，请手动解压 $tmp_tar 到 models/。"
        fi
        rm -f "$tmp_tar"
    else
        rm -f "$tmp_tar"
        die "SenseVoice 模型下载失败（ASR 无法工作）。请检查网络/代理后重试。"
    fi
fi

# 默认删除冗余的未量化 model.onnx（约 895MB），仅保留 model.int8.onnx 与 tokens.txt；
# 如需保留完整 FP32 模型，设置 KEEP_FP32_MODEL=1。
if [ "${KEEP_FP32_MODEL:-0}" != "1" ]; then
    if [ -f "$SENSEVOICE_DIR/model.onnx" ]; then
        rm -f "$SENSEVOICE_DIR/model.onnx"
        info "已删除冗余 FP32 模型 model.onnx 以节省磁盘（保留 model.int8.onnx 与 tokens.txt）。"
    fi
fi

# --------------------------------------------------------------------------- #
# 6. 自检
# --------------------------------------------------------------------------- #
if [ ! -f "$PROJECT_DIR/config.yaml" ] && [ -f "$PROJECT_DIR/config.example.yaml" ]; then
    cp "$PROJECT_DIR/config.example.yaml" "$PROJECT_DIR/config.yaml"
    info "已从 config.example.yaml 初始化 config.yaml。"
fi
if [ -f "$PROJECT_DIR/config.yaml" ]; then
    info "运行端到端自检 …"
    "$ENV_PY" "$PROJECT_DIR/main.py" --config "$PROJECT_DIR/config.yaml" --test-pipeline
    rc=$?
    if [ "$rc" -eq 0 ]; then
        info "✅ 自检通过，部署完成！可用 ./run.sh start 启动守护进程。"
    else
        warn "自检返回码 $rc，请查看上方报告（部分环节如麦克风/显示环境可能需按机器实际情况调整）。"
    fi
else
    warn "未找到 config.yaml，跳出自检。"
fi

info "环境路径：$ENV_PY"
info "提示：本地 LLM 纠错（post_processor.enable_llm）指向 http://127.0.0.1:8000/v1，"
info "      若本机未部署该服务，会自动无感降级为纯规则清洗，不影响上屏。"

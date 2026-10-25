#!/usr/bin/env bash
# =============================================================================
# voice-input 管理脚本（跨设备可迁移）
# -----------------------------------------------------------------------------
# 用法：
#   ./run.sh start     后台启动守护进程（PID 写入 /tmp/voice-input.pid）
#   ./run.sh stop      停止后台守护进程
#   ./run.sh restart   重启后台守护进程
#   ./run.sh status    查看运行状态与最近 15 行日志
#   ./run.sh fg        前台运行（实时查看日志，Ctrl+C 退出）
#   ./run.sh test-unit 运行纯逻辑单元测试（不需要麦克风/模型）
#   ./run.sh test      运行端到端自检
#   ./run.sh dashboard [port]   后台启动图形化管理面板（默认端口取 config.yaml）
#   ./run.sh dashboard-stop     停止图形化管理面板
#   ./run.sh mode <档>          快捷切换纠错档：off | local_qwen | deepseek | opencode
# -----------------------------------------------------------------------------
# 本脚本不写死任何绝对路径：项目目录由脚本自身位置推导，Python 解释器自动
# 在 conda / miniconda3 / anaconda3 / mambaforge 中查找名为 voice-input 的环境，
# 因此整个 voice-input 文件夹拷到任意机器上都能直接使用。
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
MAIN="$PROJECT_DIR/main.py"
PIDFILE="${VOICE_INPUT_PIDFILE:-/tmp/voice-input.pid}"
LOGFILE="$PROJECT_DIR/voice-input.log"
CONFIG="$PROJECT_DIR/config.yaml"
DASH_PIDFILE="${VOICE_INPUT_DASHBOARD_PIDFILE:-/tmp/voice-input-dashboard.pid}"
DASH_LOG="$PROJECT_DIR/dashboard.log"

cd "$PROJECT_DIR" || { echo "找不到项目目录: $PROJECT_DIR" >&2; exit 1; }

# Git 仅追踪 config.example.yaml：config.yaml 缺失时自动从模板初始化（用户 Key 永不入库）。
if [ ! -f "$CONFIG" ] && [ -f "$PROJECT_DIR/config.example.yaml" ]; then
    cp "$PROJECT_DIR/config.example.yaml" "$CONFIG"
fi

# --------------------------------------------------------------------------- #
# 自动定位 Python 解释器（优先级由高到低）
# --------------------------------------------------------------------------- #
resolve_python() {
    # 1) 用户显式指定
    if [ -n "${VOICE_INPUT_PYTHON:-}" ]; then
        if [ -x "$VOICE_INPUT_PYTHON" ]; then
            echo "$VOICE_INPUT_PYTHON"; return 0
        fi
        echo "VOICE_INPUT_PYTHON=$VOICE_INPUT_PYTHON 不可执行" >&2
    fi

    # 2) 当前已激活的 conda 环境
    if [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
        echo "$CONDA_PREFIX/bin/python"; return 0
    fi

    # 3) 常见 conda 发行版安装目录下的 voice-input 环境
    local base
    for base in \
        "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" \
        "$HOME/mambaforge" "$HOME/.conda" "$HOME/miniconda" "$HOME/anaconda" \
        /opt/miniconda3 /opt/anaconda3 /opt/conda /opt/miniforge3 /usr/local/miniconda3; do
        if [ -x "$base/envs/voice-input/bin/python" ]; then
            echo "$base/envs/voice-input/bin/python"; return 0
        fi
    done

    # 4) 通过 conda 命令反查 base 前缀
    if command -v conda >/dev/null 2>&1; then
        local cbase
        cbase="$(conda info --base 2>/dev/null || true)"
        if [ -n "$cbase" ] && [ -x "$cbase/envs/voice-input/bin/python" ]; then
            echo "$cbase/envs/voice-input/bin/python"; return 0
        fi
    fi

    # 5) 回退到 PATH 中的 python3 / python
    command -v python3 || command -v python || true
}

PYTHON="$(resolve_python)"

if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    echo "找不到可用的 Python 解释器。" >&2
    echo "请先运行 ./setup.sh 创建 voice-input 环境，或用环境变量" >&2
    echo "VOICE_INPUT_PYTHON 指定解释器路径。" >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
is_running() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    # kill -0 探测进程是否存在且属于当前用户。
    kill -0 "$pid" 2>/dev/null
}

# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #
do_start() {
    if is_running; then
        echo "voice-input 已在运行 (PID $(cat "$PIDFILE"))。"
        return 0
    fi

    echo "正在启动 voice-input 守护进程…（Python: $PYTHON）"
    nohup "$PYTHON" "$MAIN" --config "$CONFIG" >> "$LOGFILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PIDFILE"

    # 等待 ASR 模型加载完成（约 1~3 秒），再确认进程是否存活。
    sleep 3

    if is_running; then
        echo "✅ voice-input 已启动 (PID $pid)"
        echo "   监听：按住 右Shift/F9 = 按住说话；按一下 右Ctrl = 连续听写"
        echo "   日志：$LOGFILE"
        return 0
    fi

    echo "❌ 启动失败，进程已退出。最近日志：" >&2
    tail -n 25 "$LOGFILE" 2>/dev/null >&2
    rm -f "$PIDFILE"
    return 1
}

do_stop() {
    if ! is_running; then
        echo "voice-input 未在运行。"
        rm -f "$PIDFILE"
        return 0
    fi

    local pid
    pid="$(cat "$PIDFILE")"
    echo "正在停止 voice-input (PID $pid)…"
    kill "$pid" 2>/dev/null

    local i
    for i in $(seq 1 20); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done

    if kill -0 "$pid" 2>/dev/null; then
        echo "优雅退出超时，强制结束…"
        kill -9 "$pid" 2>/dev/null
        sleep 0.5
    fi

    rm -f "$PIDFILE"
    echo "✅ voice-input 已停止。"
    return 0
}

do_restart() {
    do_stop
    sleep 1
    do_start
}

do_status() {
    if is_running; then
        local pid
        pid="$(cat "$PIDFILE")"
        echo "✅ voice-input 正在运行 (PID $pid)"
        echo "   监听：按住 右Shift/F9 = 按住说话；按一下 右Ctrl = 连续听写"
        echo "   Python: $PYTHON"
        ps -o pid,etime,%cpu,%mem,cmd -p "$pid" 2>/dev/null | sed 's/^/   /'
    else
        echo "❌ voice-input 未在运行。"
    fi
    echo "--- 最近 15 行日志 ($LOGFILE) ---"
    if [ -f "$LOGFILE" ]; then
        tail -n 15 "$LOGFILE"
    else
        echo "(暂无日志)"
    fi
    return 0
}

do_fg() {
    echo "前台运行 voice-input（Ctrl+C 退出）…"
    echo "---"
    exec "$PYTHON" "$MAIN" --config "$CONFIG"
}

do_test() {
    echo "运行 voice-input 端到端自检…"
    echo "---"
    "$PYTHON" "$MAIN" --config "$CONFIG" --test-pipeline
}

do_test_unit() {
    echo "运行整段累积器单元测试（不需要麦克风 / 模型 / 网络）…"
    echo "---"
    "$PYTHON" "$PROJECT_DIR/tests/test_utterance_accumulator.py"
}

# --------------------------------------------------------------------------- #
# 图形化管理面板
# --------------------------------------------------------------------------- #
dashboard_is_running() {
    [ -f "$DASH_PIDFILE" ] || return 1
    local pid
    pid="$(cat "$DASH_PIDFILE" 2>/dev/null || true)"
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

config_dashboard_port() {
    "$PYTHON" -c 'import sys, yaml
try:
    cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
    print(int((cfg.get("control_center") or {}).get("port") or 17880))
except Exception:
    print(17880)' "$CONFIG" 2>/dev/null || echo 17880
}

do_dashboard() {
    local port="${1:-}"
    local url_port
    url_port="${port:-$(config_dashboard_port)}"

    if dashboard_is_running; then
        echo "✅ 控制面板已在运行 (PID $(cat "$DASH_PIDFILE"))。"
        echo "   访问：http://127.0.0.1:${url_port}/"
        return 0
    fi

    local -a args=()
    [ -n "$port" ] && args=(--port "$port")

    echo "正在启动 voice-input 控制面板…（日志：$DASH_LOG）"
    nohup "$PYTHON" -m voice_input.control_center --config "$CONFIG" ${args[@]+"${args[@]}"} >> "$DASH_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$DASH_PIDFILE"
    sleep 1.5

    if dashboard_is_running; then
        echo "✅ 控制面板已启动 (PID $pid)"
        echo "   访问：http://127.0.0.1:${url_port}/"
        echo "   日志：$DASH_LOG"
        return 0
    fi

    echo "❌ 控制面板启动失败（端口 ${url_port} 可能被占用）。最近日志：" >&2
    tail -n 20 "$DASH_LOG" 2>/dev/null >&2
    rm -f "$DASH_PIDFILE"
    return 1
}

do_dashboard_stop() {
    if ! dashboard_is_running; then
        echo "控制面板未在运行。"
        rm -f "$DASH_PIDFILE"
        return 0
    fi

    local pid
    pid="$(cat "$DASH_PIDFILE")"
    echo "正在停止控制面板 (PID $pid)…"
    kill "$pid" 2>/dev/null

    local i
    for i in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.3
    done
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null
    fi
    rm -f "$DASH_PIDFILE"
    echo "✅ 控制面板已停止。"
    return 0
}

do_mode() {
    local mode="${1:-}"
    case "$mode" in
        off|local_qwen|deepseek|opencode) ;;
        *) echo "未知档位：'$mode'（可选 off | local_qwen | deepseek | opencode）" >&2; return 1 ;;
    esac
    "$PYTHON" -m voice_input.control_center --config "$CONFIG" --set-mode "$mode"
}

do_install_desktop() {
    local src="$PROJECT_DIR/voice-input-dashboard.desktop"
    local dest="$HOME/.local/share/applications/voice-input-dashboard.desktop"
    mkdir -p "$(dirname "$dest")"
    if [ ! -f "$src" ]; then
        echo "找不到桌面入口模板：$src" >&2
        return 1
    fi
    # 将执行路径渲染为当前项目绝对路径，避免 .desktop 随仓库拷贝后路径失效。
    sed "s|^Exec=.*|Exec=$PROJECT_DIR/run.sh dashboard|" "$src" > "$dest"
    chmod +x "$dest"
    echo "✅ 已安装桌面入口：$dest"
    echo "   Exec=$PROJECT_DIR/run.sh dashboard"
    return 0
}

usage() {
    cat <<'EOF'
voice-input 管理脚本

用法：./run.sh <command>

命令：
  start            后台启动守护进程（PID: /tmp/voice-input.pid，日志: voice-input.log）
  stop             停止后台守护进程
  restart          重启后台守护进程
  status           查看运行状态与最近 15 行日志
  fg               前台运行（实时查看日志，Ctrl+C 退出）
  test-unit        运行纯逻辑单元测试（不需要麦克风 / 模型）
  test             运行端到端自检
  dashboard [port] 后台启动图形化管理面板（PID: /tmp/voice-input-dashboard.pid，
                   日志: dashboard.log；不传 port 时取 config.yaml 的 control_center.port）
  dashboard-stop   停止图形化管理面板
  mode <档位>      快捷切换纠错档：off（纯本地规则）| local_qwen（本地千问）| deepseek（云端）| opencode（OpenCode 免费）
  install-desktop  安装桌面入口到 ~/.local/share/applications/
EOF
}

# --------------------------------------------------------------------------- #
# 分发
# --------------------------------------------------------------------------- #
case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_restart ;;
    status)  do_status ;;
    fg)      shift; do_fg "$@" ;;
    test)    do_test ;;
    test-unit) do_test_unit ;;
    dashboard)      shift; do_dashboard "$@" ;;
    dashboard-stop) do_dashboard_stop ;;
    mode)           shift; do_mode "$@" ;;
    install-desktop) do_install_desktop ;;
    ""|help|-h|--help) usage ;;
    *)       echo "未知命令：$1" >&2; usage >&2; exit 1 ;;
esac

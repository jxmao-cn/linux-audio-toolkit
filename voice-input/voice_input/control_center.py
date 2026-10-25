#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voice-input 图形化管理面板（后端 + 静态页托管）。

设计目标
========
* **零新增依赖**：HTTP 服务只用标准库 ``http.server``；配置读写用项目已有的
  PyYAML（``yaml.safe_load`` 校验 / 读入字典，写回时做"只改目标标量"的行级
  编辑，从而最大限度保留 config.yaml 里的注释与未知键）。
* **零硬编码路径**：项目根由本文件位置推导（优先"本包上一级目录"，其中必须
  存在 ``run.sh``），配置文件、日志、PID 文件、compose 文件路径全部可配。
* **绝不 500**：状态接口里每一项（进程 / GPU / 容器）都是 best-effort，
  取不到就返回 ``null``，而不是抛异常。

接口一览
========
``GET  /``                    管理面板（同目录 ``dashboard.html``）
``GET  /api/status``          运行状态 + 当前档位 + 资源占用
``POST /api/mode``            切换纠错档（off / local_qwen / deepseek / opencode）
``POST /api/qwen``            启停本地千问容器
``POST /api/voice``           启停重启语音守护进程 / 自检
``POST /api/deepseek_key``    保存 DeepSeek API Key
``GET  /api/deepseek_test``   DeepSeek 连通性测试
``POST /api/opencode_test``   OpenCode 免费档连通性测试（调一次 CLI 纠错短句）
``GET  /api/logs``            语音服务日志尾部
``POST /api/power``           停止全部服务 / 关闭电脑（需 confirm=YES）

命令行
======
::

    python -m voice_input.control_center --port 17880
    python -m voice_input.control_center --set-mode local_qwen
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

try:  # PyYAML 是 voice-input 的既有依赖（main.py 亦依赖它）
    import yaml
except ImportError as _exc:  # pragma: no cover - 仅环境缺依赖时触发
    yaml = None  # type: ignore[assignment]
    _YAML_IMPORT_ERROR: Optional[BaseException] = _exc
else:
    _YAML_IMPORT_ERROR = None


# --------------------------------------------------------------------------- #
# 路径推导（严禁本机绝对路径）
# --------------------------------------------------------------------------- #
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def _detect_project_root() -> str:
    """推导项目根目录。

    依次尝试"本包目录的上一级"与"本包目录自身"，取第一个含 ``run.sh`` 的目录；
    都不满足时回退到上一级。这样既支持 ``voice_input/control_center.py`` 位于
    项目根的子包中的常见布局，也支持脚本被平铺到项目根的情形。
    """
    candidates = (os.path.dirname(_PKG_DIR), _PKG_DIR)
    for cand in candidates:
        if os.path.isfile(os.path.join(cand, "run.sh")):
            return cand
    return os.path.dirname(_PKG_DIR)


PROJECT_ROOT = _detect_project_root()
DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
DEFAULT_RUN_SH = os.path.join(PROJECT_ROOT, "run.sh")
DEFAULT_PIDFILE = os.environ.get("VOICE_INPUT_PIDFILE") or "/tmp/voice-input.pid"
DEFAULT_LOG = os.environ.get("VOICE_INPUT_LOG") or os.path.join(PROJECT_ROOT, "voice-input.log")
DASHBOARD_HTML = os.path.join(_PKG_DIR, "dashboard.html")

DEFAULT_PORT = 17880

#: 三档切换时需要整组复制进 ``post_processor`` 的键。
MODE_KEYS: Tuple[str, ...] = ("api_base", "api_key", "model", "timeout", "disable_thinking")
#: D 档（opencode_cli）切换时整组复制的键（llm_modes.opencode 全量键）。
OPENCODE_MODE_KEYS: Tuple[str, ...] = (
    "llm_backend",
    "opencode_bin",
    "opencode_model",
    "opencode_agent",
    "opencode_timeout",
)
VALID_MODES: Tuple[str, ...] = ("off", "local_qwen", "deepseek", "opencode")

logger = logging.getLogger("voice-input.control-center")


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _run(
    cmd: Sequence[str],
    timeout: float,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """执行外部命令，任何失败都转成结构化结果（绝不向外抛异常）。"""
    started = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - 参数均为内部构造的常量/配置值
            list(cmd),
            cwd=cwd or PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "elapsed": round(time.monotonic() - started, 2),
        }
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return {
            "returncode": -1,
            "stdout": out,
            "stderr": (err + f"\n超时（{timeout}s）").strip(),
            "elapsed": round(time.monotonic() - started, 2),
            "timeout": True,
        }
    except FileNotFoundError as exc:
        return {
            "returncode": -2,
            "stdout": "",
            "stderr": f"命令不存在：{exc}",
            "elapsed": round(time.monotonic() - started, 2),
        }
    except Exception as exc:  # noqa: BLE001 - 兜底：面板不应因此崩溃
        return {
            "returncode": -3,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "elapsed": round(time.monotonic() - started, 2),
        }


def _tail(text: str, n: int = 20) -> str:
    """取文本末尾 n 行。"""
    lines = (text or "").splitlines()
    return "\n".join(lines[-n:]) if lines else ""


def _tail_file(path: str, n: int = 60) -> str:
    """读取文件末尾 n 行（文件不存在/不可读时返回空串）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-n:])
    except Exception:  # noqa: BLE001
        return ""


def _mask_key(key: Any) -> str:
    """隐去 API Key 中间位（保留首尾各 4 位，短 key 全遮蔽）。"""
    text = str(key or "")
    if not text:
        return ""
    if len(text) <= 12:
        return "*" * len(text)
    return f"{text[:4]}{'*' * (len(text) - 8)}{text[-4:]}"


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


# --------------------------------------------------------------------------- #
# 配置读写：PyYAML 读入 + 行级标量改写（保留注释与未知键）
# --------------------------------------------------------------------------- #
def ensure_config_exists(path: str) -> str:
    """若 ``path`` 不存在但同目录下存在 ``config.example.yaml``，自动复制一份。

    Git 只追踪模板，用户的 ``config.yaml``（可能含 API Key）永不入库。
    返回传入的 ``path``（便于调用方链式使用）。
    """
    if not os.path.isfile(path):
        example_path = os.path.join(os.path.dirname(os.path.abspath(path)), "config.example.yaml")
        if os.path.isfile(example_path):
            try:
                shutil.copyfile(example_path, path)
                logger.info("配置文件不存在，已从模板自动初始化：%s -> %s", example_path, path)
            except Exception:  # noqa: BLE001
                logger.warning("从模板复制配置文件失败：%s", example_path, exc_info=True)
    return path


def load_config_dict(path: str) -> Dict[str, Any]:
    """读取 YAML 配置为字典；文件缺失/损坏时返回空字典。"""
    if yaml is None:
        raise RuntimeError(f"未安装 PyYAML，无法读写配置：{_YAML_IMPORT_ERROR}")
    ensure_config_exists(path)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data if isinstance(data, dict) else {}


def _yaml_scalar(value: Any) -> str:
    """把 Python 标量序列化为单行 YAML 字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _split_value_comment(rest: str) -> Tuple[str, str]:
    """从 ``key: 后面的剩余文本`` 中切出 (值, 行尾注释)。

    引号内的 ``#`` 不算注释，只有"前导空白的 #"才是注释起始。
    """
    in_single = in_double = False
    for idx, ch in enumerate(rest):
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or rest[idx - 1] in " \t":
                return rest[:idx].rstrip(), rest[idx:]
    return rest.rstrip(), ""


def _section_range(
    lines: List[str],
    path: Sequence[str],
    lo: int = 0,
    hi: Optional[int] = None,
) -> Optional[Tuple[int, int, int, int]]:
    """定位嵌套段落，返回 ``(标题行号, 内容起始, 内容结束, 标题缩进)``。"""
    hi = len(lines) if hi is None else hi
    indent = -1
    header = lo
    for depth, name in enumerate(path):
        found: Optional[Tuple[int, int]] = None
        for idx in range(lo, hi):
            line = lines[idx]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = re.match(r"^(\s*)([^:\s#]+):\s*(?:#.*)?$", line)
            if not match or match.group(2) != name:
                continue
            ind = len(match.group(1))
            if depth == 0:
                if ind == 0:
                    found = (idx, ind)
                    break
            elif ind > indent:
                found = (idx, ind)
                break
        if found is None:
            return None
        header, indent = found
        end = hi
        for idx in range(header + 1, hi):
            line = lines[idx]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            ind = len(line) - len(line.lstrip())
            if ind <= indent:
                end = idx
                break
        lo, hi = header + 1, end
    return header, lo, hi, indent


def _set_key_in_lines(lines: List[str], path: Sequence[str], key: str, value: Any) -> None:
    """在 ``path`` 段落内把 ``key`` 设为 ``value``（就地修改 lines）。"""
    block = _section_range(lines, path)
    literal = _yaml_scalar(value)

    if block is None:
        # 整段都不存在：在文件末尾追加最小骨架（2 空格缩进，与现有风格一致）。
        lines.append("")
        for depth, name in enumerate(path):
            lines.append(f"{'  ' * depth}{name}:")
        lines.append(f"{'  ' * len(path)}{key}: {literal}")
        return

    header, start, end, _ = block
    indent: Optional[str] = None
    key_re = re.compile(r"^(\s+)" + re.escape(key) + r":(?:\s|$)(.*)$")
    for idx in range(start, end):
        match = key_re.match(lines[idx])
        if match:
            _, comment = _split_value_comment(match.group(2))
            sep = "  " if comment else ""
            lines[idx] = f"{match.group(1)}{key}: {literal}{sep}{comment}"
            return
        if indent is None:
            probe = re.match(r"^(\s+)[^\s#]", lines[idx])
            if probe:
                indent = probe.group(1)

    # 键不存在：插到段落内容末尾（跳过尾随空行/注释组）。
    indent = indent or "  "
    pos = end
    while pos - 1 > start and not lines[pos - 1].strip():
        pos -= 1
    lines.insert(pos, f"{indent}{key}: {literal}")


def update_config_values(path: str, updates: Sequence[Tuple[Sequence[str], str, Any]]) -> Dict[str, Any]:
    """按 ``((section, ...), key, value)`` 列表改写配置，返回改写后的完整字典。

    * 读入：PyYAML（保证语法合法、未知键完整保留）；
    * 写回：行级标量替换，因此注释、键序、未知段落都不会丢；
    * 落盘：临时文件 + ``os.replace`` 原子替换，并保留一份 ``*.bak`` 备份。
    """
    if yaml is None:
        raise RuntimeError(f"未安装 PyYAML，无法读写配置：{_YAML_IMPORT_ERROR}")

    original = ""
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            original = fh.read()
    lines = original.splitlines()

    for section_path, key, value in updates:
        _set_key_in_lines(lines, section_path, key, value)

    new_text = "\n".join(lines) + "\n"
    parsed = yaml.safe_load(new_text) or {}
    if not isinstance(parsed, dict):
        raise ValueError("改写后的配置顶层不是映射，已放弃写入")
    # 逐项回读校验，避免行级编辑出现"看起来对、实际没生效"。
    for section_path, key, value in updates:
        node: Any = parsed
        for name in section_path:
            node = (node or {}).get(name) if isinstance(node, dict) else None
        actual = node.get(key) if isinstance(node, dict) else None
        if isinstance(value, bool):
            ok = bool(actual) is value
        elif isinstance(value, (int, float)):
            ok = _as_float(actual) is not None and abs(float(actual) - float(value)) < 1e-9
        else:
            ok = str(actual) == str(value)
        if not ok:
            raise ValueError(f"配置校验失败：{'.'.join(section_path)}.{key} 写入后为 {actual!r}")

    backup = f"{path}.bak"
    if original and not os.path.isfile(backup):
        try:
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write(original)
        except OSError:
            logger.debug("写配置备份失败：%s", backup, exc_info=True)

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    os.replace(tmp, path)
    return parsed


def infer_mode(post_processor: Dict[str, Any]) -> str:
    """由 ``post_processor`` 平铺配置推断当前生效档。"""
    if not post_processor.get("enable_llm"):
        return "off"
    if str(post_processor.get("llm_backend") or "") == "opencode_cli":
        return "opencode"
    base = str(post_processor.get("api_base") or "")
    if "127.0.0.1:8000" in base or "localhost:8000" in base:
        return "local_qwen"
    if "api.deepseek.com" in base:
        return "deepseek"
    return "custom"


# --------------------------------------------------------------------------- #
# 环境探测（进程 / GPU / Docker）
# --------------------------------------------------------------------------- #
_COMPOSE_BASE: Optional[List[str]] = None
_COMPOSE_LOCK = threading.Lock()


def compose_base_command() -> List[str]:
    """探测可用的 compose 命令（优先 docker compose v2）。"""
    global _COMPOSE_BASE
    with _COMPOSE_LOCK:
        if _COMPOSE_BASE is not None:
            return list(_COMPOSE_BASE)
        if shutil.which("docker"):
            probe = _run(["docker", "compose", "version"], timeout=8)
            if probe.get("returncode") == 0:
                _COMPOSE_BASE = ["docker", "compose"]
                return list(_COMPOSE_BASE)
        if shutil.which("docker-compose"):
            _COMPOSE_BASE = ["docker-compose"]
            return list(_COMPOSE_BASE)
        _COMPOSE_BASE = []
        return []


def compose_container_name(compose_path: str) -> Optional[str]:
    """从 compose 文件解析容器名（container_name 优先，其次首个服务名）。"""
    if yaml is None or not os.path.isfile(compose_path):
        return None
    try:
        with open(compose_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001
        return None
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict) or not services:
        return None
    for name, svc in services.items():
        if isinstance(svc, dict) and svc.get("container_name"):
            return str(svc["container_name"])
    return str(next(iter(services)))


def resolve_compose_path(config: Dict[str, Any]) -> str:
    """解析 ``control_center.qwen_compose_file``（支持 ``~`` 与相对项目根路径）。"""
    cc_cfg = config.get("control_center") or {}
    raw = str(cc_cfg.get("qwen_compose_file") or os.environ.get("QWEN_COMPOSE_FILE") or "").strip()
    if not raw:
        return ""
    expanded = os.path.expanduser(raw)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(PROJECT_ROOT, expanded))


def voice_process_state(pidfile: str) -> Tuple[bool, Optional[int]]:
    """读取 PID 文件并判断语音守护进程是否存活。"""
    try:
        with open(pidfile, "r", encoding="utf-8") as fh:
            pid = int((fh.read() or "").strip())
    except Exception:  # noqa: BLE001
        return False, None
    return _pid_alive(pid), pid


def qwen_container_state(compose_path: str) -> Dict[str, Any]:
    """返回本地千问容器状态：``running`` / ``stopped`` / ``missing``。"""
    result: Dict[str, Any] = {
        "state": "missing",
        "container": None,
        "compose": compose_path,
        "compose_exists": bool(compose_path and os.path.isfile(compose_path)),
        "docker": bool(shutil.which("docker")),
        "detail": "",
    }
    if not result["compose_exists"]:
        result["detail"] = "未找到 compose 文件，本地模型未部署"
        return result
    name = compose_container_name(compose_path)
    result["container"] = name
    if not result["docker"]:
        result["detail"] = "未安装 docker"
        return result
    if not name:
        result["detail"] = "compose 文件缺少 services 段"
        return result

    running = _run(["docker", "ps", "--format", "{{.Names}}"], timeout=8)
    if running.get("returncode") == 0:
        if name in (running.get("stdout") or "").split():
            result["state"] = "running"
            result["detail"] = "容器运行中"
            return result
    else:
        result["detail"] = _tail(running.get("stderr", ""), 3) or "docker ps 失败"
        return result

    all_containers = _run(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout=8)
    if all_containers.get("returncode") == 0 and name in (
        all_containers.get("stdout") or ""
    ).split():
        result["state"] = "stopped"
        result["detail"] = "容器已创建但未运行"
    else:
        result["state"] = "missing"
        result["detail"] = "容器尚未创建（首次启动会拉取镜像/加载模型）"
    return result


def _gpu_stats() -> Tuple[Optional[float], Optional[float]]:
    """返回 (已用显存 MiB, 功耗 W)，取不到则 (None, None)。"""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None, None
    probe = _run(
        [exe, "--query-gpu=memory.used,power.draw", "--format=csv,noheader,nounits"],
        timeout=5,
    )
    if probe.get("returncode") != 0:
        return None, None
    first = next((ln for ln in (probe.get("stdout") or "").splitlines() if ln.strip()), "")
    if not first:
        return None, None
    parts = [p.strip() for p in first.split(",")]
    used = _as_float(parts[0]) if parts else None
    power = _as_float(parts[1]) if len(parts) > 1 else None
    return used, power


def _container_mem_mb(name: Optional[str]) -> Optional[float]:
    """读取容器内存占用（MiB），best-effort。"""
    if not name or not shutil.which("docker"):
        return None
    probe = _run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name],
        timeout=8,
    )
    if probe.get("returncode") != 0:
        return None
    line = next((ln for ln in (probe.get("stdout") or "").splitlines() if ln.strip()), "")
    if not line:
        return None
    used = line.split("/")[0].strip().replace(" ", "")
    match = re.match(r"^([0-9.]+)\s*([KMGTP]i?B|B)$", used, flags=re.IGNORECASE)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    factor = {
        "b": 1.0 / (1024 * 1024),
        "kib": 1.0 / 1024,
        "kb": 1.0 / 1000,
        "mib": 1.0,
        "mb": 1.0,
        "gib": 1024.0,
        "gb": 1000.0,
        "tib": 1024.0 * 1024.0,
        "tb": 1000.0 * 1000.0,
        "pib": 1024.0 * 1024.0 * 1024.0,
        "pb": 1000.0 * 1000.0 * 1000.0,
    }.get(unit)
    if factor is None:
        return None
    return round(amount * factor, 1)


_RES_LOCK = threading.Lock()
_RES_CACHE: Dict[str, Any] = {"ts": 0.0, "key": None, "data": None}


def gather_resources(pid: Optional[int], container: Optional[str], ttl: float = 2.5) -> Dict[str, Any]:
    """汇总面板需要的资源指标（带短 TTL 缓存，避免 5 秒轮询压垮 docker）。"""
    cache_key = (pid, container)
    now = time.monotonic()
    with _RES_LOCK:
        if (
            _RES_CACHE["data"] is not None
            and _RES_CACHE["key"] == cache_key
            and now - _RES_CACHE["ts"] < ttl
        ):
            return dict(_RES_CACHE["data"])

    data: Dict[str, Any] = {
        "voice_cpu": None,
        "voice_mem_mb": None,
        "gpu_used_mb": None,
        "gpu_power_w": None,
        "qwen_mem": None,
    }
    if pid and _pid_alive(pid):
        probe = _run(["ps", "-o", "%cpu=,rss=", "-p", str(pid)], timeout=5)
        if probe.get("returncode") == 0:
            fields = (probe.get("stdout") or "").split()
            if len(fields) >= 2:
                data["voice_cpu"] = _as_float(fields[0])
                mem_kb = _as_float(fields[1])
                data["voice_mem_mb"] = round(mem_kb / 1024.0, 1) if mem_kb is not None else None

    gpu_used, gpu_power = _gpu_stats()
    data["gpu_used_mb"] = gpu_used
    data["gpu_power_w"] = gpu_power

    mem = _container_mem_mb(container)
    if mem is not None:
        data["qwen_mem"] = mem

    with _RES_LOCK:
        _RES_CACHE.update({"ts": now, "key": cache_key, "data": dict(data)})
    return data


# --------------------------------------------------------------------------- #
# 控制中心
# --------------------------------------------------------------------------- #
class ControlCenter:
    """voice-input 管理面板后端。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        config_path: Optional[str] = None,
        pidfile: Optional[str] = None,
        logfile: Optional[str] = None,
        run_sh: Optional[str] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.config_path = os.path.abspath(os.path.expanduser(config_path or DEFAULT_CONFIG_PATH))
        self.pidfile = pidfile or DEFAULT_PIDFILE
        self.logfile = logfile or DEFAULT_LOG
        self.run_sh = run_sh or DEFAULT_RUN_SH
        # 串行化所有"会改变系统状态"的操作（切换档 / 启停 / 关机），避免并发互踩。
        self._action_lock = threading.Lock()
        self.httpd: Optional[ThreadingHTTPServer] = None

    # ------------------------------------------------------------------ #
    # 基础读取
    # ------------------------------------------------------------------ #
    def _config(self, path: Optional[str] = None) -> Dict[str, Any]:
        target = path or self.config_path
        try:
            return load_config_dict(target)
        except Exception:  # noqa: BLE001
            logger.exception("读取配置失败：%s", target)
            return {}

    def _shell(self, command: str, timeout: float) -> Dict[str, Any]:
        """调用 run.sh 子命令。"""
        if os.path.isfile(self.run_sh) and os.access(self.run_sh, os.X_OK):
            cmd = [self.run_sh, command]
        else:
            cmd = ["/bin/bash", self.run_sh, command]
        result = _run(cmd, timeout=timeout)
        result["command"] = " ".join(cmd)
        result["output"] = _tail(
            "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if part),
            40,
        )
        return result

    # ------------------------------------------------------------------ #
    # /api/status
    # ------------------------------------------------------------------ #
    def status(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        """返回面板状态快照；内部逐项容错，绝不抛异常。"""
        path = os.path.abspath(os.path.expanduser(config_path or self.config_path))
        payload: Dict[str, Any] = {
            "ok": True,
            "config_path": path,
            "project_root": PROJECT_ROOT,
            "voice_running": False,
            "voice_pid": None,
            "llm_mode": "off",
            "post_processor": {},
            "llm_modes": {},
            "qwen_container": {"state": "missing", "container": None, "compose": ""},
            "resources": {},
            "deepseek_key_set": False,
        }

        try:
            running, pid = voice_process_state(self.pidfile)
            payload["voice_running"] = bool(running)
            payload["voice_pid"] = pid if running else None
        except Exception:  # noqa: BLE001
            logger.debug("读取语音进程状态失败", exc_info=True)

        cfg = self._config(path)
        pp = dict(cfg.get("post_processor") or {}) if isinstance(cfg, dict) else {}
        snapshot = dict(pp)
        if "api_key" in snapshot:
            snapshot["api_key"] = _mask_key(snapshot.get("api_key"))
        payload["post_processor"] = snapshot
        payload["llm_mode"] = infer_mode(pp)
        payload["opencode_model"] = str(
            pp.get("opencode_model") or "opencode/deepseek-v4.1-flash-free"
        )

        modes = cfg.get("llm_modes") if isinstance(cfg, dict) else {}
        modes = modes if isinstance(modes, dict) else {}
        payload["llm_modes"] = {
            name: {
                "api_base": (modes.get(name) or {}).get("api_base"),
                "model": (modes.get(name) or {}).get("model"),
                "timeout": (modes.get(name) or {}).get("timeout"),
                "disable_thinking": (modes.get(name) or {}).get("disable_thinking"),
                "api_key": _mask_key((modes.get(name) or {}).get("api_key")),
                "llm_backend": (modes.get(name) or {}).get("llm_backend"),
                "opencode_model": (modes.get(name) or {}).get("opencode_model"),
                "opencode_timeout": (modes.get(name) or {}).get("opencode_timeout"),
            }
            for name in ("local_qwen", "deepseek", "opencode")
            if isinstance(modes.get(name), dict)
        }
        deepseek = modes.get("deepseek") or {}
        payload["deepseek_key_set"] = bool(str(deepseek.get("api_key") or "").strip())

        compose_path = resolve_compose_path(cfg)
        try:
            qwen = qwen_container_state(compose_path)
        except Exception as exc:  # noqa: BLE001
            qwen = {
                "state": "missing",
                "container": None,
                "compose": compose_path,
                "compose_exists": bool(compose_path and os.path.isfile(compose_path)),
                "detail": f"{type(exc).__name__}: {exc}",
            }
        payload["qwen_container"] = qwen

        try:
            payload["resources"] = gather_resources(payload["voice_pid"], qwen.get("container"))
        except Exception:  # noqa: BLE001
            payload["resources"] = {
                "voice_cpu": None,
                "voice_mem_mb": None,
                "gpu_used_mb": None,
                "gpu_power_w": None,
                "qwen_mem": None,
            }
        return payload

    # ------------------------------------------------------------------ #
    # /api/mode
    # ------------------------------------------------------------------ #
    def switch_mode(
        self,
        mode: str,
        config_path: Optional[str] = None,
        opencode_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """切换纠错档并重启语音守护进程。

        ``opencode_model`` 仅在 ``mode == "opencode"`` 时有效：非空则同步写入
        ``llm_modes.opencode`` 与 ``post_processor``（面板 D 档「保存并切换」用）。
        """
        mode = str(mode or "").strip()
        if mode not in VALID_MODES:
            return {"ok": False, "message": f"未知档位：{mode!r}，可选 {', '.join(VALID_MODES)}"}
        path = os.path.abspath(os.path.expanduser(config_path or self.config_path))

        with self._action_lock:
            cfg = self._config(path)
            if not cfg:
                return {"ok": False, "message": f"无法读取配置：{path}"}
            modes = cfg.get("llm_modes") if isinstance(cfg.get("llm_modes"), dict) else {}
            messages: List[str] = []
            qwen_result: Optional[Dict[str, Any]] = None

            if mode == "off":
                updates: List[Tuple[Sequence[str], str, Any]] = [
                    (("post_processor",), "enable_llm", False),
                ]
                messages.append("已切换为 A 档：纯本地规则纠错（零延迟、不占 GPU）")
            elif mode == "opencode":
                source = modes.get("opencode")
                if not isinstance(source, dict) or not source:
                    return {
                        "ok": False,
                        "message": "config.yaml 缺少 llm_modes.opencode 配置段，无法切换",
                    }
                model_override = str(opencode_model or "").strip()
                if model_override:
                    # 先落盘到模板段，再随整组复制进 post_processor。
                    updates = [(("llm_modes", "opencode"), "opencode_model", model_override)]
                    try:
                        cfg = update_config_values(path, updates) or {}
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("写入配置失败")
                        return {"ok": False, "message": f"写入配置失败：{type(exc).__name__}: {exc}"}
                    source = ((cfg.get("llm_modes") or {}).get("opencode")) or source
                updates = [(("post_processor",), "enable_llm", True)]
                for key in OPENCODE_MODE_KEYS:
                    if key in source:
                        updates.append((("post_processor",), key, source.get(key)))
                # 模板段缺 llm_backend 时兜底，保证档位推断正确。
                if "llm_backend" not in source:
                    updates.append((("post_processor",), "llm_backend", "opencode_cli"))
                messages.append(
                    "已切换为 D 档：OpenCode 免费纠错 "
                    f"（{source.get('opencode_model') or 'opencode/deepseek-v4.1-flash-free'}，"
                    "复用本机 OpenCode 登录态、零 Key）"
                )
            else:
                source = modes.get(mode)
                if not isinstance(source, dict) or not source:
                    return {
                        "ok": False,
                        "message": f"config.yaml 缺少 llm_modes.{mode} 配置段，无法切换",
                    }
                missing = [key for key in MODE_KEYS if key not in source]
                if missing:
                    return {
                        "ok": False,
                        "message": f"llm_modes.{mode} 缺少键：{', '.join(missing)}",
                    }
                if mode == "deepseek" and not str(source.get("api_key") or "").strip():
                    return {
                        "ok": False,
                        "message": "尚未填写 DeepSeek API Key，请先在面板「云端纠错」中保存 Key 再切换",
                    }

                if mode == "local_qwen":
                    compose_path = resolve_compose_path(cfg)
                    qwen_result = qwen_container_state(compose_path)
                    if qwen_result.get("state") != "running":
                        if not qwen_result.get("compose_exists"):
                            messages.append("本地模型未部署（找不到 compose 文件），已切换配置但纠错会降级为规则清洗")
                        else:
                            start = self._compose_action("start", compose_path, timeout=300)
                            qwen_result = qwen_container_state(compose_path)
                            if start.get("ok"):
                                messages.append("已尝试启动本地千问容器")
                            else:
                                messages.append(
                                    "本地千问容器启动失败，将降级为规则清洗："
                                    + _tail(start.get("output", ""), 3)
                                )
                    else:
                        messages.append("本地千问容器已在运行")

                updates = [(("post_processor",), "enable_llm", True)]
                updates += [(("post_processor",), key, source.get(key)) for key in MODE_KEYS]
                # 模板段若未声明 llm_backend 则回落 openai，避免之前 D 档残留的
                # "opencode_cli" 导致档位推断与纠错路由错误。
                updates.append(
                    (("post_processor",), "llm_backend", source.get("llm_backend") or "openai")
                )
                label = "B 档：本地千问 Qwen3.8-27B" if mode == "local_qwen" else "C 档：云端 DeepSeek"
                messages.append(f"已切换为 {label}")
                if mode == "local_qwen" and float(source.get("timeout") or 0) < 1.0:
                    messages.append("提示：本地档超时较短，容器冷启动期间可能触发降级")

            try:
                update_config_values(path, updates)
            except Exception as exc:  # noqa: BLE001
                logger.exception("写入配置失败")
                return {"ok": False, "message": f"写入配置失败：{type(exc).__name__}: {exc}"}

            restart = self._shell("restart", timeout=60)
            ok = restart.get("returncode") == 0
            if not ok:
                messages.append("配置已写入，但语音守护进程重启失败")
            result: Dict[str, Any] = {
                "ok": ok,
                "mode": mode,
                "message": "；".join(messages),
                "restart": {
                    "returncode": restart.get("returncode"),
                    "output": restart.get("output"),
                    "elapsed": restart.get("elapsed"),
                },
            }
            if qwen_result is not None:
                result["qwen"] = qwen_result
            logger.info("切换纠错档 -> %s（restart ok=%s）", mode, ok)
            return result

    # ------------------------------------------------------------------ #
    # 本地千问容器
    # ------------------------------------------------------------------ #
    @staticmethod
    def _compose_action(action: str, compose_path: str, timeout: float = 120) -> Dict[str, Any]:
        if not compose_path or not os.path.isfile(compose_path):
            return {
                "ok": False,
                "message": "未找到本地模型 compose 文件，请先在 config.yaml 的 control_center.qwen_compose_file 中配置",
                "output": "",
            }
        base = compose_base_command()
        if not base:
            return {"ok": False, "message": "未检测到 docker compose，请先安装 Docker", "output": ""}
        cmd = base + ["-f", compose_path, action] + (["-d"] if action == "up" else [])
        probe = _run(cmd, timeout=timeout)
        output = _tail(
            "\n".join(part for part in (probe.get("stdout"), probe.get("stderr")) if part), 25
        )
        return {
            "ok": probe.get("returncode") == 0,
            "returncode": probe.get("returncode"),
            "output": output,
            "elapsed": probe.get("elapsed"),
            "command": " ".join(cmd),
        }

    def qwen_action(self, action: str, config_path: Optional[str] = None) -> Dict[str, Any]:
        action = str(action or "").strip()
        if action not in ("start", "stop"):
            return {"ok": False, "message": "action 只能是 start 或 stop"}
        path = os.path.abspath(os.path.expanduser(config_path or self.config_path))
        cfg = self._config(path)
        compose_path = resolve_compose_path(cfg)
        with self._action_lock:
            result = self._compose_action(action, compose_path, timeout=300 if action == "start" else 120)
        result["state"] = qwen_container_state(compose_path)
        if result.get("ok"):
            result["message"] = "本地千问容器已启动（模型加载可能需要 1~3 分钟）" if action == "start" else "本地千问容器已停止"
        else:
            result.setdefault("message", "操作失败")
        logger.info("本地千问容器 %s -> ok=%s", action, result.get("ok"))
        return result

    # ------------------------------------------------------------------ #
    # 语音守护进程
    # ------------------------------------------------------------------ #
    def voice_action(self, action: str) -> Dict[str, Any]:
        action = str(action or "").strip()
        if action not in ("start", "stop", "restart", "test"):
            return {"ok": False, "message": "action 只能是 start/stop/restart/test"}
        timeout = 300 if action == "test" else (60 if action == "restart" else 45)
        with self._action_lock:
            result = self._shell(action, timeout=timeout)
        running, pid = voice_process_state(self.pidfile)
        return {
            "ok": result.get("returncode") == 0,
            "action": action,
            "returncode": result.get("returncode"),
            "output": result.get("output") if action == "test" else _tail(result.get("output", ""), 15),
            "elapsed": result.get("elapsed"),
            "voice_running": running,
            "voice_pid": pid if running else None,
        }

    # ------------------------------------------------------------------ #
    # DeepSeek
    # ------------------------------------------------------------------ #
    def save_deepseek_key(self, api_key: str, config_path: Optional[str] = None) -> Dict[str, Any]:
        key = str(api_key or "").strip()
        if not key:
            return {"ok": False, "message": "API Key 不能为空"}
        if any(ch.isspace() for ch in key):
            return {"ok": False, "message": "API Key 不能包含空白字符"}
        path = os.path.abspath(os.path.expanduser(config_path or self.config_path))

        with self._action_lock:
            cfg = self._config(path)
            pp = cfg.get("post_processor") or {}
            updates: List[Tuple[Sequence[str], str, Any]] = [
                (("llm_modes", "deepseek"), "api_key", key),
            ]
            synced = infer_mode(pp) == "deepseek"
            if synced:
                updates.append((("post_processor",), "api_key", key))
            try:
                update_config_values(path, updates)
            except Exception as exc:  # noqa: BLE001
                logger.exception("保存 DeepSeek Key 失败")
                return {"ok": False, "message": f"写入配置失败：{type(exc).__name__}: {exc}"}

        message = "已保存 DeepSeek API Key"
        if synced:
            message += "，并已同步到当前生效档（重启语音服务后生效）"
        else:
            message += "（切换到 C 档时自动生效）"
        return {
            "ok": True,
            "message": message,
            "saved_to": path,
            "key_masked": _mask_key(key),
            "synced_current_mode": synced,
        }

    def deepseek_test(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        path = os.path.abspath(os.path.expanduser(config_path or self.config_path))
        cfg = self._config(path)
        modes = cfg.get("llm_modes") if isinstance(cfg.get("llm_modes"), dict) else {}
        deepseek = modes.get("deepseek") if isinstance(modes.get("deepseek"), dict) else {}
        pp = cfg.get("post_processor") or {}

        api_base = str(deepseek.get("api_base") or pp.get("api_base") or "").rstrip("/")
        key = str(deepseek.get("api_key") or "").strip()
        if not key and infer_mode(pp) == "deepseek":
            key = str(pp.get("api_key") or "").strip()
        if not key:
            return {"ok": False, "message": "尚未填写 DeepSeek API Key", "api_base": api_base}
        if not api_base:
            return {"ok": False, "message": "未配置 api_base"}

        url = f"{api_base}/models"
        proxy = str(((cfg.get("control_center") or {}).get("proxy") or "")).strip()
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        opener = urllib.request.build_opener(*handlers)

        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        started = time.monotonic()
        try:
            with opener.open(request, timeout=10) as resp:
                body = resp.read(65536).decode("utf-8", "replace")
                status = getattr(resp, "status", 200)
        except urllib.error.HTTPError as exc:
            elapsed = round(time.monotonic() - started, 2)
            detail = ""
            try:
                detail = exc.read(2048).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            return {
                "ok": False,
                "api_base": api_base,
                "status": exc.code,
                "elapsed": elapsed,
                "message": f"HTTP {exc.code}：鉴权或参数错误",
                "detail": _tail(detail, 5),
            }
        except Exception as exc:  # noqa: BLE001 - 网络错误统一报告
            return {
                "ok": False,
                "api_base": api_base,
                "status": None,
                "elapsed": round(time.monotonic() - started, 2),
                "message": f"连接失败：{type(exc).__name__}: {exc}",
            }

        elapsed = round(time.monotonic() - started, 2)
        models: List[str] = []
        try:
            data = json.loads(body)
            models = [
                str(item.get("id"))
                for item in (data.get("data") or [])
                if isinstance(item, dict) and item.get("id")
            ]
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": status == 200,
            "api_base": api_base,
            "status": status,
            "elapsed": elapsed,
            "models": models[:20],
            "message": f"连通正常（HTTP {status}，{elapsed}s，{len(models)} 个模型）"
            if status == 200
            else f"返回 HTTP {status}",
        }

    # ------------------------------------------------------------------ #
    # OpenCode 免费档连通性测试
    # ------------------------------------------------------------------ #
    def opencode_test(
        self, model: Optional[str] = None, config_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """调一次 opencode_cli 纠错短句，验证本机 OpenCode 登录态与免费模型可用性。"""
        path = os.path.abspath(os.path.expanduser(config_path or self.config_path))
        cfg = self._config(path)
        pp = cfg.get("post_processor") if isinstance(cfg.get("post_processor"), dict) else {}
        modes = cfg.get("llm_modes") if isinstance(cfg.get("llm_modes"), dict) else {}
        preset = modes.get("opencode") if isinstance(modes.get("opencode"), dict) else {}

        chosen = (
            str(model or "").strip()
            or str(pp.get("opencode_model") or "").strip()
            or str(preset.get("opencode_model") or "").strip()
            or "opencode/deepseek-v4.1-flash-free"
        )
        try:
            timeout = float(pp.get("opencode_timeout") or preset.get("opencode_timeout") or 30.0)
        except (TypeError, ValueError):
            timeout = 30.0
        timeout = min(max(timeout, 5.0), 90.0)

        try:
            from voice_input.post_processor import PostProcessor
        except ImportError as exc:
            return {"ok": False, "model": chosen, "message": f"无法加载后处理模块：{exc}"}

        probe = PostProcessor(
            {
                "enable_llm": True,
                "llm_backend": "opencode_cli",
                "opencode_bin": str(pp.get("opencode_bin") or preset.get("opencode_bin") or "opencode"),
                "opencode_model": chosen,
                "opencode_agent": str(pp.get("opencode_agent") or preset.get("opencode_agent") or ""),
                "opencode_timeout": timeout,
            }
        )
        sentence = "这的是测试"
        message = (
            f"{probe.llm_system_prompt}\n\n待纠错文本：\n{sentence}"
            "\n\n只输出修正后的最终文本，不要调用任何工具。"
        )
        cmd = [probe.opencode_bin, "run", "--model", chosen]
        if probe.opencode_agent:
            cmd += ["--agent", probe.opencode_agent]
        cmd += ["--format", "json", message]

        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - argv 传参，无 shell 注入面
                cmd, capture_output=True, text=True, timeout=90.0, env=dict(os.environ),
            )
        except subprocess.TimeoutExpired as exc:
            err = exc.stderr or ""
            if isinstance(err, bytes):
                err = err.decode("utf-8", "replace")
            return {
                "ok": False,
                "model": chosen,
                "elapsed": round(time.monotonic() - started, 2),
                "message": "opencode CLI 调用超时（90s）",
                "stderr": _tail(err, 10),
            }
        except (FileNotFoundError, OSError) as exc:
            return {
                "ok": False,
                "model": chosen,
                "elapsed": round(time.monotonic() - started, 2),
                "message": f"找不到 opencode 可执行文件：{exc}",
                "stderr": "",
            }
        elapsed = round(time.monotonic() - started, 2)
        stderr_tail = _tail(proc.stderr or "", 10)
        if proc.returncode != 0:
            return {
                "ok": False,
                "model": chosen,
                "elapsed": elapsed,
                "message": f"opencode CLI 非零退出（{proc.returncode}）",
                "stderr": stderr_tail,
            }
        parsed = PostProcessor._parse_opencode_stdout(proc.stdout or "")
        output = PostProcessor._sanitize_llm_output(sentence, parsed) or sentence
        if output == sentence:
            return {
                "ok": False,
                "model": chosen,
                "elapsed": elapsed,
                "input": sentence,
                "output": output,
                "message": "纠错无变化（已降级为原文）",
                "stderr": stderr_tail,
            }
        return {
            "ok": True,
            "model": chosen,
            "elapsed": elapsed,
            "input": sentence,
            "output": output,
            "message": f"连通正常（{elapsed}s）：{sentence} -> {output}",
        }

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    def logs(self, lines: int = 60) -> Dict[str, Any]:
        lines = max(1, min(500, int(lines or 60)))
        return {
            "ok": True,
            "path": self.logfile,
            "lines": lines,
            "text": _tail_file(self.logfile, lines),
            "exists": os.path.isfile(self.logfile),
        }

    # ------------------------------------------------------------------ #
    # 危险操作
    # ------------------------------------------------------------------ #
    def power_action(self, action: str, confirm: str) -> Dict[str, Any]:
        action = str(action or "").strip()
        if str(confirm or "") != "YES":
            return {"ok": False, "message": "危险操作需要通过双重确认（confirm 必须为 YES）"}
        if action not in ("stop_all", "shutdown"):
            return {"ok": False, "message": "action 只能是 stop_all 或 shutdown"}

        with self._action_lock:
            stopped_voice = self._shell("stop", timeout=45)
            cfg = self._config()
            compose_path = resolve_compose_path(cfg)
            stopped_qwen = self._compose_action("stop", compose_path, timeout=120)
            summary = {
                "voice": stopped_voice.get("returncode") == 0,
                "qwen": stopped_qwen.get("ok"),
            }
            if action == "stop_all":
                parts = ["语音服务已停止" if summary["voice"] else "语音服务停止失败"]
                compose_ok = bool(compose_path and os.path.isfile(compose_path))
                if compose_ok:
                    parts.append("本地模型容器已停止" if summary["qwen"] else "本地模型容器停止失败")
                else:
                    parts.append("未部署本地模型容器，已跳过")
                return {
                    "ok": summary["voice"] and (summary["qwen"] if compose_ok else True),
                    "action": action,
                    "message": "；".join(parts),
                    "result": summary,
                    "voice_output": _tail(stopped_voice.get("output", ""), 8),
                    "qwen_output": _tail(stopped_qwen.get("output", ""), 8),
                }

            attempts: List[Dict[str, Any]] = []
            for cmd in (
                ["systemctl", "poweroff"],
                ["shutdown", "-h", "now"],
                ["gnome-session-quit", "--power-off", "--no-prompt"],
            ):
                probe = _run(cmd, timeout=20)
                attempts.append(
                    {
                        "command": " ".join(cmd),
                        "returncode": probe.get("returncode"),
                        "output": _tail(probe.get("stderr") or probe.get("stdout") or "", 3),
                    }
                )
                if probe.get("returncode") == 0:
                    logger.warning("执行关机命令成功：%s", " ".join(cmd))
                    return {
                        "ok": True,
                        "action": action,
                        "message": f"已执行关机：{' '.join(cmd)}（不可撤销）",
                        "result": summary,
                        "attempts": attempts,
                    }
            logger.error("所有关机命令均失败：%s", attempts)
            return {
                "ok": False,
                "action": action,
                "message": "已停止全部服务，但关机命令全部失败（可能缺少权限），请手动关机",
                "result": summary,
                "attempts": attempts,
            }

    # ------------------------------------------------------------------ #
    # 静态页面
    # ------------------------------------------------------------------ #
    @staticmethod
    def dashboard_html() -> bytes:
        try:
            with open(DASHBOARD_HTML, "rb") as fh:
                return fh.read()
        except Exception:  # noqa: BLE001
            fallback = (
                "<!doctype html><meta charset='utf-8'><title>voice-input 控制面板</title>"
                "<body style='background:#111;color:#eee;font-family:sans-serif;padding:2rem'>"
                "<h1>控制面板页面缺失</h1>"
                "<p>未找到 dashboard.html，API 仍可正常使用。</p>"
                "<ul>"
                "<li><code>GET /api/status</code></li>"
                "<li><code>POST /api/mode</code></li>"
                "<li><code>POST /api/qwen</code></li>"
                "<li><code>POST /api/voice</code></li>"
                "<li><code>GET /api/logs</code></li>"
                "</ul></body>"
            )
            return fallback.encode("utf-8")

    # ------------------------------------------------------------------ #
    # 服务器生命周期
    # ------------------------------------------------------------------ #
    def build_server(self) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer((self.host, self.port), _ControlCenterHandler)
        httpd.daemon_threads = True
        httpd.control_center = self  # type: ignore[attr-defined]
        self.port = int(httpd.server_address[1])
        self.httpd = httpd
        return httpd

    def serve_forever(self) -> None:
        httpd = self.build_server()
        logger.info(
            "voice-input 控制面板已启动：http://%s:%d/ （配置：%s）",
            self.host,
            self.port,
            self.config_path,
        )
        try:
            httpd.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在退出控制面板…")
        finally:
            httpd.server_close()

    def shutdown(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()


# --------------------------------------------------------------------------- #
# HTTP 处理器
# --------------------------------------------------------------------------- #
class _ControlCenterHandler(BaseHTTPRequestHandler):
    """轻量路由：GET 读状态 / POST 改状态。"""

    server_version = "VoiceInputControlCenter/1.0"
    protocol_version = "HTTP/1.0"

    # -- 基础输出 ------------------------------------------------------- #
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_html(self, body: bytes) -> None:
        self._send(200, body, "text/html; charset=utf-8")

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # -- 工具 ----------------------------------------------------------- #
    @property
    def center(self) -> ControlCenter:
        return self.server.control_center  # type: ignore[attr-defined]

    def _config_path(self, query: Dict[str, List[str]]) -> Optional[str]:
        raw = (query.get("config") or [""])[0].strip()
        return os.path.abspath(os.path.expanduser(raw)) if raw else None

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1 << 20))
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    # -- GET ------------------------------------------------------------ #
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path or "/"
        if path.endswith("/") and path != "/":
            path = path.rstrip("/")
        query = parse_qs(parts.query)

        if path in ("/", "/index.html"):
            self._send_html(self.center.dashboard_html())
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/status":
            try:
                payload = self.center.status(self._config_path(query))
            except Exception as exc:  # noqa: BLE001 - 状态接口绝不 500
                logger.exception("构建状态失败")
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self._send_json(payload)
            return
        if path == "/api/deepseek_test":
            try:
                payload = self.center.deepseek_test(self._config_path(query))
            except Exception as exc:  # noqa: BLE001
                logger.exception("DeepSeek 连通性测试失败")
                payload = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
            self._send_json(payload)
            return
        if path == "/api/logs":
            raw_lines = (query.get("lines") or ["60"])[0]
            try:
                lines = int(raw_lines)
            except (TypeError, ValueError):
                lines = 60
            self._send_json(self.center.logs(lines))
            return
        self._send_json({"ok": False, "error": f"未知接口：{path}"}, status=404)

    # -- POST ----------------------------------------------------------- #
    def do_POST(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        path = parts.path or "/"
        query = parse_qs(parts.query)
        body = self._read_json_body()
        if body is None:
            self._send_json({"ok": False, "error": "请求体必须是合法 JSON 对象"}, status=400)
            return

        config_path = self._config_path(query)
        try:
            if path == "/api/mode":
                payload = self.center.switch_mode(
                    str(body.get("mode") or ""),
                    config_path,
                    opencode_model=str(body.get("opencode_model") or "") or None,
                )
            elif path == "/api/qwen":
                payload = self.center.qwen_action(str(body.get("action") or ""), config_path)
            elif path == "/api/voice":
                payload = self.center.voice_action(str(body.get("action") or ""))
            elif path == "/api/deepseek_key":
                payload = self.center.save_deepseek_key(
                    str(body.get("api_key") or ""), config_path
                )
            elif path == "/api/opencode_test":
                try:
                    payload = self.center.opencode_test(
                        str(body.get("model") or "") or None, config_path
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("OpenCode 连通性测试失败")
                    payload = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
            elif path == "/api/power":
                payload = self.center.power_action(
                    str(body.get("action") or ""), str(body.get("confirm") or "")
                )
            else:
                self._send_json({"ok": False, "error": f"未知接口：{path}"}, status=404)
                return
        except Exception as exc:  # noqa: BLE001 - 单次操作失败不应拖垮服务
            logger.exception("处理 %s 失败", path)
            payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self._send_json(payload)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _default_port_from_config(config_path: str) -> int:
    try:
        cfg = load_config_dict(config_path)
        port = (cfg.get("control_center") or {}).get("port")
        if port:
            return int(port)
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_PORT


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-input-control-center",
        description="voice-input 图形化管理面板后端（切换纠错档 / 管理容器 / 查看日志）。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认取 config.yaml）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    parser.add_argument(
        "--set-mode",
        choices=list(VALID_MODES),
        default=None,
        help="不启动服务，只切换纠错档并重启语音守护进程（供 run.sh mode 调用）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="不启动服务，打印一次状态快照（JSON）后退出",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    config_path = os.path.abspath(os.path.expanduser(args.config))
    ensure_config_exists(config_path)
    port = args.port or _default_port_from_config(config_path)
    center = ControlCenter(host=args.host, port=port, config_path=config_path)

    if args.set_mode:
        result = center.switch_mode(args.set_mode, config_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    if args.status:
        print(json.dumps(center.status(config_path), ensure_ascii=False, indent=2))
        return 0

    center.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

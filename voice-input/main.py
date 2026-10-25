#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voice-input 主控程序。

把各功能模块（音频采集 / ASR / 后处理 / 文本注入 / 热键）组装成完整的
"按住右 Shift / F9 说话"本地语音输入工具，并提供命令行调试入口。

命令行
======
* 默认（无参数）：启动后台热键守护进程（阻塞，Ctrl+C 退出）。
* ``--config <path>``   ：指定配置文件，默认同目录 ``config.yaml``。
* ``--test-pipeline``   ：端到端自检（不进入阻塞热键循环），输出报告后退出。
* ``--once <seconds>``  ：立即录音 N 秒 -> 识别 -> 注入当前焦点窗口 -> 退出。

热键语义（双键独立双模）
========================
* 按住 ``ptt_key``（默认 右 Shift / F9）满 ``hold_threshold_sec`` = 按住说话 (PTT)，
  松开立即上屏；
* 按一下 ``toggle_key``（默认 右 Ctrl） = 连续听写开关 (Toggle)，再按一下结束。

用法::

    python main.py                 # 启动守护进程
    python main.py --test-pipeline # 自检
    python main.py --once 3        # 录 3 秒并上屏
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 环境准备：必须在导入任何依赖 shutil.which 的模块之前完成！
# --------------------------------------------------------------------------- #
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# 当前 Python 解释器所在目录（通常是 conda 环境的 bin）：xclip / xdotool 等
# 注入所需工具一般装在这里。动态获取，避免写死某台机器的绝对路径。
ENV_BIN = os.path.dirname(os.path.abspath(sys.executable or ""))
"""当前 Python 解释器所在的 bin 目录（含 xclip / xdotool 等注入工具）。"""


def _detect_display() -> Optional[str]:
    """探测可用的 X11 ``DISPLAY``。

    优先级：

    1. 环境变量中已有的 ``DISPLAY``（用户／桌面会话显式指定，最可信）；
    2. 扫描 ``/tmp/.X11-unix/X*`` 套接字，选取编号最小的可用显示（通常 ``:0`` / ``:1``）；
    3. 纯 Wayland 会话（无 XWayland 套接字）时不强行设置，交由注入模块走 Wayland 路径；
    4. 其余情况回退 ``:0``。

    Returns:
        形如 ``":0"`` / ``":1"`` 的显示串；纯 Wayland 无 X11 时返回 ``None``。
    """
    existing = os.environ.get("DISPLAY")
    if existing:
        return existing
    try:
        import glob as _glob

        for path in sorted(_glob.glob("/tmp/.X11-unix/X*")):
            num = os.path.basename(path)[1:]
            if num.isdigit():
                return f":{num}"
    except Exception:  # noqa: BLE001 - 探测失败不应影响启动
        pass
    if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        return None
    return ":0"


def _prepare_env() -> None:
    """把当前解释器所在 bin 目录前置进 PATH，并保证 DISPLAY 有合理默认值。

    这样即使用系统 python 或全路径 python 启动，也能找到环境内的
    ``xclip`` / ``xdotool``；同时避免无 DISPLAY 时 X11 相关调用直接失败。
    """
    if ENV_BIN:
        path = os.environ.get("PATH", "")
        entries = path.split(os.pathsep) if path else []
        if ENV_BIN not in entries:
            os.environ["PATH"] = ENV_BIN + os.pathsep + path if path else ENV_BIN
    if not os.environ.get("DISPLAY"):
        detected = _detect_display()
        if detected:
            os.environ["DISPLAY"] = detected
            # 此时 logging 尚未配置，用 stderr 打印一行便于用户确认探测结果。
            print(f"[voice-input] 自动探测 DISPLAY={detected}", file=sys.stderr)


_prepare_env()

# 让脚本所在目录始终可导入（支持从任意 CWD 以全路径启动）。
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import numpy as np  # noqa: E402  (必须在 _prepare_env 之后导入)

from voice_input import (  # noqa: E402
    ASREngine,
    AudioCapture,
    HotkeyDaemon,
    PostProcessor,
    TextInjector,
    find_pulse_source,
)

try:  # PyYAML
    import yaml
except ImportError as _exc:  # pragma: no cover
    yaml = None  # type: ignore[assignment]
    _YAML_IMPORT_ERROR: Optional[BaseException] = _exc
else:
    _YAML_IMPORT_ERROR = None


logger = logging.getLogger("voice-input")

DEFAULT_CONFIG_PATH = os.path.join(PROJECT_DIR, "config.yaml")

# 连续听写（Toggle）"整段累积"默认参数（与 voice_input/audio_capture.py 保持一致，
# 仅作为配置缺省兜底；config.yaml 的 audio 段是唯一权威来源）。
DEFAULT_AUDIO_COMMIT_SILENCE_SEC = 1.6
DEFAULT_AUDIO_MAX_UTTERANCE_SEC = 20.0

# 连续模式结束时，残余语音低于该时长则直接丢弃。
RESIDUAL_MIN_SEC = 0.25


# --------------------------------------------------------------------------- #
# 默认配置（与 config.yaml 保持一致，作为缺省兜底）
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: Dict[str, Any] = {
    "audio": {
        "preferred_device_substring": "",
        "target_sample_rate": 16000,
        "pre_roll_ms": 250,
        "vad_model_path": "models/silero_vad.onnx",
        "vad_threshold": 0.45,
        "min_silence_duration": 0.6,
        "commit_silence_duration": 1.6,
        "max_utterance_seconds": 20.0,
        "session_dump_dir": "",
    },
    "hotkey": {
        "toggle_key": "ctrl_r",
        "ptt_key": "shift_r,f9",
        "hold_threshold_sec": 0.20,
    },
    "asr": {
        "model_dir": "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "num_threads": 4,
        "use_itn": True,
        "language": "auto",
        "provider": "cpu",
    },
    "post_processor": {
        "clean_fillers": True,
        "auto_space_cjk_ascii": True,
        "custom_replacements": {"新的一行": "\n", "回车换行": "\n"},
        "enable_llm": False,
        "api_base": "http://127.0.0.1:8000/v1",
        "api_key": "EMPTY",
        "model": "qwen3.8-27b",
        "timeout": 1.5,
        "disable_thinking": True,
    },
    "injector": {"restore_clipboard": True, "restore_delay_sec": 0.35},
    "feedback": {
        "enable_sound": True,
        "start_wav": "assets/start.wav",
        "stop_wav": "assets/stop.wav",
        "enable_notification": False,
    },
}


# --------------------------------------------------------------------------- #
# 配置加载与工具函数
# --------------------------------------------------------------------------- #
def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并字典：``override`` 中的键覆盖 ``base``，未给出的键沿用 ``base``。"""
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str) -> Dict[str, Any]:
    """读取 YAML 配置并与默认配置合并（缺失项用默认值补齐）。"""
    if yaml is None:
        raise RuntimeError(
            "未安装 PyYAML，无法解析配置文件。"
            f"请在 voice-input 环境中 pip install pyyaml（原始错误：{_YAML_IMPORT_ERROR}）"
        )
    if not os.path.isfile(path):
        logger.warning("配置文件不存在，使用内置默认配置：%s", path)
        return _deep_merge(DEFAULT_CONFIG, {})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        logger.error("解析配置文件失败（%s），改用默认配置：%s", path, exc)
        return _deep_merge(DEFAULT_CONFIG, {})
    if not isinstance(user_cfg, dict):
        logger.error("配置文件顶层不是映射（dict），改用默认配置：%s", path)
        return _deep_merge(DEFAULT_CONFIG, {})
    return _deep_merge(DEFAULT_CONFIG, user_cfg)


def resolve_path(path: Optional[str], base_dir: str) -> Optional[str]:
    """把配置里的相对路径解析为绝对路径（基于配置文件所在目录）。"""
    if not path:
        return path
    expanded = os.path.expanduser(str(path))
    if os.path.isabs(expanded):
        return expanded
    return os.path.normpath(os.path.join(base_dir, expanded))


def setup_logging(verbose: bool = False) -> None:
    """配置日志：输出到 stdout（守护进程由 run.sh 重定向到日志文件）。"""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def read_wav_16k_mono(path: str) -> np.ndarray:
    """读取 WAV 文件并返回 16 kHz 单声道 float32 波形（[-1, 1]）。"""
    import wave

    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())

    if sampwidth != 2:
        raise ValueError(f"仅支持 16-bit PCM WAV，当前 sampwidth={sampwidth}")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)

    if rate != 16000 and audio.size:
        try:
            from scipy.signal import resample_poly

            from math import gcd

            g = gcd(int(rate), 16000)
            audio = resample_poly(audio, 16000 // g, int(rate) // g).astype(np.float32)
        except Exception:  # noqa: BLE001 - 兜底线性插值
            n_out = int(round(audio.size * 16000.0 / rate))
            xp = np.linspace(0.0, 1.0, audio.size, endpoint=False)
            xq = np.linspace(0.0, 1.0, n_out, endpoint=False)
            audio = np.interp(xq, xp, audio).astype(np.float32)
    return np.ascontiguousarray(audio, dtype=np.float32)


# --------------------------------------------------------------------------- #
# 主应用
# --------------------------------------------------------------------------- #
class VoiceInputApp:
    """把各模块组装起来的语音输入应用。"""

    def __init__(self, config: Dict[str, Any], base_dir: str) -> None:
        self.cfg = config
        self.base_dir = base_dir

        self.audio: Optional[AudioCapture] = None
        self.asr: Optional[ASREngine] = None
        self.post_processor: Optional[PostProcessor] = None
        self.injector: Optional[TextInjector] = None
        self.hotkey: Optional[HotkeyDaemon] = None

        # 串行化"识别 -> 后处理 -> 注入"，避免并发注入互相打断。
        self._pipeline_lock = threading.Lock()
        self._shutdown = threading.Event()

    # ------------------------------------------------------------------ #
    # 模块构建
    # ------------------------------------------------------------------ #
    def build_audio(self) -> AudioCapture:
        cfg = self.cfg["audio"]
        self.audio = AudioCapture(
            preferred_device_substring=str(cfg["preferred_device_substring"]),
            target_sample_rate=int(cfg["target_sample_rate"]),
            pre_roll_ms=int(cfg["pre_roll_ms"]),
            vad_model_path=resolve_path(cfg.get("vad_model_path"), self.base_dir),
            vad_threshold=float(cfg["vad_threshold"]),
            min_silence_duration=float(cfg["min_silence_duration"]),
            commit_silence_duration=float(
                cfg.get("commit_silence_duration", DEFAULT_AUDIO_COMMIT_SILENCE_SEC)
            ),
            max_utterance_seconds=float(
                cfg.get("max_utterance_seconds", DEFAULT_AUDIO_MAX_UTTERANCE_SEC)
            ),
            session_dump_dir=resolve_path(cfg.get("session_dump_dir"), self.base_dir),
        )
        return self.audio

    def build_asr(self) -> ASREngine:
        cfg = self.cfg["asr"]
        self.asr = ASREngine(
            model_dir=resolve_path(cfg["model_dir"], self.base_dir),
            num_threads=int(cfg["num_threads"]),
            use_itn=bool(cfg["use_itn"]),
            language=str(cfg["language"]),
            provider=str(cfg["provider"]),
        )
        return self.asr

    def build_post_processor(self) -> PostProcessor:
        self.post_processor = PostProcessor(self.cfg.get("post_processor") or {})
        return self.post_processor

    def build_injector(self) -> TextInjector:
        cfg = self.cfg["injector"]
        self.injector = TextInjector(
            restore_clipboard=bool(cfg["restore_clipboard"]),
            restore_delay_sec=float(cfg["restore_delay_sec"]),
        )
        return self.injector

    # ------------------------------------------------------------------ #
    # 反馈（提示音 / 通知）
    # ------------------------------------------------------------------ #
    def _play_cue(self, wav_path: Optional[str]) -> None:
        """异步、非阻塞地播放提示音（paplay）。"""
        if not self.cfg.get("feedback", {}).get("enable_sound", True):
            return
        path = resolve_path(wav_path, self.base_dir)
        if not path or not os.path.isfile(path):
            logger.debug("提示音文件不存在，跳过播放：%s", path)
            return
        try:
            subprocess.Popen(
                ["paplay", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001 - 播放失败不应影响主流程
            logger.debug("播放提示音失败：%s", path, exc_info=True)

    def _notify(self, message: str) -> None:
        """可选的桌面通知（默认关闭，避免弹窗抢焦点）。"""
        if not self.cfg.get("feedback", {}).get("enable_notification", False):
            return
        try:
            subprocess.Popen(
                ["notify-send", "语音输入", message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            logger.debug("发送通知失败", exc_info=True)

    def _cue(self, which: str) -> None:
        """按名播放提示音：which in {"start", "stop"}。"""
        self._play_cue(self.cfg["feedback"].get(f"{which}_wav"))

    # ------------------------------------------------------------------ #
    # 核心流水线：识别 -> 后处理 -> 注入
    # ------------------------------------------------------------------ #
    def _process_and_inject(self, samples: Optional[np.ndarray], tag: str = "ASR") -> None:
        """对一段音频执行完整流水线并把文本注入当前焦点窗口。"""
        if samples is None or getattr(samples, "size", 0) == 0:
            logger.info("[%s] 无有效音频，跳过识别", tag)
            return
        if self.asr is None:
            logger.error("[%s] ASR 引擎未初始化，无法识别", tag)
            return

        audio_sec = float(samples.size) / float(self.cfg["audio"]["target_sample_rate"])

        with self._pipeline_lock:
            t0 = time.monotonic()
            try:
                raw_text = self.asr.transcribe(samples)
            except Exception:  # noqa: BLE001
                logger.exception("[%s] 语音识别失败", tag)
                return
            asr_sec = time.monotonic() - t0

            try:
                final_text = (
                    self.post_processor.process(raw_text)
                    if self.post_processor is not None
                    else raw_text
                )
            except Exception:  # noqa: BLE001
                logger.exception("[%s] 后处理失败，直接使用原始识别结果", tag)
                final_text = raw_text

            if not final_text or not final_text.strip():
                logger.info(
                    "[%s] 识别耗时 %.2fs（音频 %.2fs），未识别到有效文本",
                    tag,
                    asr_sec,
                    audio_sec,
                )
                return

            # 直观展示后处理（千问同音字纠错）的修复效果：ASR 原稿 -> 纠错后。
            if final_text != raw_text:
                logger.info(
                    '[%s] ASR: "%s" -> 修正: "%s"',
                    tag,
                    raw_text.replace("\n", "\\n"),
                    final_text.replace("\n", "\\n"),
                )

            injected = False
            if self.injector is not None:
                try:
                    injected = self.injector.inject_text(final_text)
                except Exception:  # noqa: BLE001
                    logger.exception("[%s] 文本注入失败", tag)

            logger.info(
                "[%s] 识别耗时 %.2fs（音频 %.2fs）| 上屏%s: %s",
                tag,
                asr_sec,
                audio_sec,
                "" if injected else "(注入失败!)",
                final_text.replace("\n", "\\n"),
            )

    def _handle_utterance(self, samples: np.ndarray) -> None:
        """Toggle 模式下每检测到一句结束时的回调。"""
        self._process_and_inject(samples, tag="Toggle")

    # ------------------------------------------------------------------ #
    # 热键回调
    # ------------------------------------------------------------------ #
    def on_ptt_start(self) -> None:
        """按住 PTT 键（右 Shift / F9）达阈值：开始录音。"""
        self._cue("start")
        if self.audio is None:
            logger.error("[PTT] 音频模块未初始化")
            return
        logger.info("[PTT] 开始录音 (松开 PTT 键立即上屏)...")
        try:
            self.audio.start_recording(mode="ptt")
        except Exception:  # noqa: BLE001
            logger.exception("[PTT] 启动录音失败")

    def on_ptt_stop(self) -> None:
        """松开 PTT 键：结束录音并在后台线程识别上屏。"""
        self._cue("stop")
        threading.Thread(target=self._finish_ptt, name="ptt-finish", daemon=True).start()

    def _finish_ptt(self) -> None:
        if self.audio is None:
            return
        try:
            samples = self.audio.stop_recording()
        except Exception:  # noqa: BLE001
            logger.exception("[PTT] 停止录音失败")
            return
        self._process_and_inject(samples, tag="PTT")

    def on_toggle_start(self) -> None:
        """按一下 Toggle 键（右 Ctrl）：进入连续听写模式。"""
        self._cue("start")
        if self.audio is None:
            logger.error("[Toggle] 音频模块未初始化")
            return
        logger.info("[Toggle] 开启连续语音输入 (再按一下右Ctrl结束)...")
        try:
            self.audio.start_recording(
                mode="toggle", on_utterance_callback=self._handle_utterance
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Toggle] 启动连续录音失败")

    def on_toggle_stop(self) -> None:
        """再按一下 Toggle 键（右 Ctrl）：结束连续听写，处理残余语音。"""
        self._cue("stop")
        if self.audio is None:
            return
        try:
            samples = self.audio.stop_recording()
        except Exception:  # noqa: BLE001
            logger.exception("[Toggle] 停止连续录音失败")
            return

        min_samples = int(
            RESIDUAL_MIN_SEC * float(self.cfg["audio"]["target_sample_rate"])
        )
        if samples is not None and samples.size >= min_samples:
            logger.info(
                "[Toggle] 结束，待提交整段语音 %.2fs，送入识别",
                samples.size / float(self.cfg["audio"]["target_sample_rate"]),
            )
            self._handle_utterance(samples)
        else:
            logger.info("[Toggle] 结束连续语音输入")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def build_all(self) -> None:
        """构建全部模块（音频 -> ASR -> 后处理 -> 注入）。"""
        self.build_audio()
        self.build_post_processor()
        self.build_injector()
        self.build_asr()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, _frame):  # noqa: ANN001
            logger.info("收到信号 %s，准备退出…", signum)
            self._shutdown.set()
            if self.hotkey is not None:
                self.hotkey.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # 非主线程等情况
                pass

    def start_hotkey(self) -> None:
        cfg = self.cfg["hotkey"]
        self.hotkey = HotkeyDaemon(
            ptt_key=cfg.get("ptt_key", "shift_r,f9"),
            toggle_key=cfg.get("toggle_key", "ctrl_r"),
            hold_threshold_sec=float(cfg.get("hold_threshold_sec", 0.20)),
            on_ptt_start=self.on_ptt_start,
            on_ptt_stop=self.on_ptt_stop,
            on_toggle_start=self.on_toggle_start,
            on_toggle_stop=self.on_toggle_stop,
        )
        self.hotkey.start()

    def run_forever(self) -> int:
        """启动守护进程并阻塞，直到 Ctrl+C / SIGTERM。"""
        self.build_all()
        self._install_signal_handlers()
        self.start_hotkey()
        logger.info(
            "voice-input 已就绪：按住 右Shift/F9 说话 / 按一下 右Ctrl 连续听写。按 Ctrl+C 退出。"
        )
        self._notify("语音输入已启动")
        try:
            while not self._shutdown.is_set():
                time.sleep(0.3)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在退出…")
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        """停止热键监听并释放音频资源。"""
        self._shutdown.set()
        if self.hotkey is not None:
            try:
                self.hotkey.stop()
            except Exception:  # noqa: BLE001
                logger.debug("停止热键守护进程异常", exc_info=True)
        if self.audio is not None:
            try:
                self.audio.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭音频采集异常", exc_info=True)
        logger.info("voice-input 已退出")


def _probe_toggle_merge(
    audio: AudioCapture, wave_audio: np.ndarray, audio_cfg: Dict[str, Any]
) -> Dict[str, Any]:
    """用合成音频验证连续听写的"整段累积"逻辑（不依赖麦克风）。

    构造两段相同的合成音频，唯一区别是句内停顿长短：

    * **短停顿**（0.6~commit_silence 之间）：应当被合并，最终只提交 1 段；
    * **长停顿**（> commit_silence_duration）：应当在停顿处切开，提交 2 段。

    Returns:
        ``{"ok": bool, "detail": str}``，detail 形如
        ``短停顿 -> 1 段(4.83s)；长停顿 -> 2 段(2.31s, 2.31s)``。
    """
    sr = 16000
    commit = float(
        audio_cfg.get("commit_silence_duration", DEFAULT_AUDIO_COMMIT_SILENCE_SEC)
    )

    # 取测试音频中能量最高的一段真实语音（2 秒），保证两段都是有效语音。
    best_start, best_rms = 0, -1.0
    piece = min(2 * sr, max(sr, wave_audio.size // 2))
    for start in range(0, max(1, wave_audio.size - piece), sr // 4):
        window = wave_audio[start : start + piece]
        rms = float(np.sqrt(np.mean(np.square(window)) + 1e-12))
        if rms > best_rms:
            best_rms, best_start = rms, start
    speech = wave_audio[best_start : best_start + piece]
    if speech.size < sr:
        return {"ok": False, "detail": f"测试音频可用语音不足（{speech.size} 样本）"}

    def silence(seconds: float) -> np.ndarray:
        return np.zeros(int(seconds * sr), dtype=np.float32)

    def run_case(gap_seconds: float, gain: float = 1.0, vads_disabled: bool = False) -> List[float]:
        """喂入 [语音][停顿][语音][长静音]，返回每次提交的音频时长（秒）。"""
        collected: List[float] = []
        lock = threading.Lock()

        def _on_utt(samples: np.ndarray) -> None:
            with lock:
                collected.append(float(samples.size) / sr)

        with audio._lock:  # noqa: SLF001 - 自检专用，直接驱动整段累积状态机
            audio._mode = "toggle"  # noqa: SLF001
            audio._on_utterance = _on_utt  # noqa: SLF001
            audio._recording = True  # noqa: SLF001
            audio._acc.reset_session()  # noqa: SLF001
            prev_vad_failed = audio._vad_failed  # noqa: SLF001
            if vads_disabled:
                # 模拟"VAD 不可用 / 完全漏判"：整段累积必须靠峰值判定兜住。
                audio._vad = None  # noqa: SLF001
                audio._vad_failed = True  # noqa: SLF001

        try:
            stream = np.concatenate(
                [speech, silence(gap_seconds), speech, silence(max(2.0 * commit, 2.0))]
            ) * gain
            for i in range(0, stream.size, 512):
                audio._on_block(stream[i : i + 512])  # noqa: SLF001

            # 结束时会返回"尚未提交的整段"（正常情况应为空：上面已由长静音触发提交）。
            tail = audio.stop_recording()
            if tail is not None and tail.size >= int(0.25 * sr):
                with lock:
                    collected.append(float(tail.size) / sr)
        finally:
            with audio._lock:  # noqa: SLF001
                audio._vad_failed = prev_vad_failed  # noqa: SLF001

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with lock:
                if len(collected) >= 2:
                    break
            time.sleep(0.02)
        with lock:
            return list(collected)

    # 短停顿取"刚超过 VAD 断句粒度、又明显小于提交阈值"的值：
    # 这样既能验证"跨 VAD 边界合并"，也不会因 commit 配置很小而误判。
    short_gap = min(
        max(audio.min_silence_duration + 0.15, 0.2), max(0.2, commit - 0.5)
    )
    long_gap = commit + 1.0
    merged = run_case(short_gap)
    split = run_case(long_gap)
    # 回归用例：VAD 不可用 + 轻声说话（历史事故：整段被丢弃，只剩最后一小句）。
    quiet = run_case(short_gap, gain=0.05, vads_disabled=True)
    detail = (
        f"短停顿(%.1fs) -> %d 段%s；长停顿(%.1fs) -> %d 段%s；"
        f"VAD 不可用 + 轻声 -> %d 段%s"
        % (
            short_gap,
            len(merged),
            "(" + ", ".join(f"{d:.2f}s" for d in merged) + ")" if merged else "",
            long_gap,
            len(split),
            "(" + ", ".join(f"{d:.2f}s" for d in split) + ")" if split else "",
            len(quiet),
            "(" + ", ".join(f"{d:.2f}s" for d in quiet) + ")" if quiet else "",
        )
    )
    ok = (
        len(merged) == 1
        and len(split) >= 2
        and merged
        and merged[0] > speech.size / sr  # 合并后的整段必须比单段语音更长
        # 轻声 + VAD 失灵时也必须把两句话都收进来（不允许只剩尾巴）
        and len(quiet) == 1
        and quiet[0] > 2.0 * speech.size / sr
    )
    return {"ok": bool(ok), "detail": detail}


# --------------------------------------------------------------------------- #
# 子命令：端到端自检
# --------------------------------------------------------------------------- #
def _check_tool(names: List[str]) -> Tuple[bool, str]:
    found = {n: shutil.which(n) for n in names}
    missing = [n for n, p in found.items() if not p]
    detail = "  ".join(f"{n}={found[n] or '缺失'}" for n in names)
    return (not missing), detail


def run_selftest(cfg: Dict[str, Any], base_dir: str) -> int:
    """端到端自检：不进入阻塞热键循环，输出报告后退出。"""
    results: List[Tuple[str, bool, str, bool]] = []  # (名称, 通过, 详情, 是否关键)

    def record(name: str, ok: bool, detail: str = "", critical: bool = True) -> None:
        results.append((name, ok, detail, critical))
        flag = "PASS" if ok else ("FAIL" if critical else "WARN")
        logger.info("[%s] %s%s", flag, name, f" — {detail}" if detail else "")

    logger.info("=" * 68)
    logger.info("voice-input 端到端自检开始")
    logger.info("=" * 68)

    # ---- 0. 运行环境 -------------------------------------------------- #
    path_first = os.environ.get("PATH", "").split(os.pathsep)[0]
    record(
        "环境: PATH 首位为 conda 环境 bin",
        path_first == ENV_BIN,
        f"PATH[0]={path_first}",
        critical=False,
    )
    display = os.environ.get("DISPLAY", "")
    record("环境: DISPLAY 已设置", bool(display), f"DISPLAY={display}")

    tool_ok, tool_detail = _check_tool(["xclip", "xdotool", "paplay", "pactl", "parecord"])
    record("环境: 外部工具可用 (xclip/xdotool/paplay/pactl/parecord)", tool_ok, tool_detail)

    # ---- 1. PulseAudio 麦克风 ---------------------------------------- #
    preferred_source = str(cfg["audio"]["preferred_device_substring"] or "").strip()
    source = find_pulse_source(preferred_source)
    source_check = (
        f"音频: 找到 PulseAudio 输入源 (含 '{preferred_source}')"
        if preferred_source
        else "音频: 找到 PulseAudio 默认输入源"
    )
    record(
        source_check,
        source is not None,
        f"source={source}",
    )

    audio: Optional[AudioCapture] = None
    try:
        audio = AudioCapture(
            preferred_device_substring=str(cfg["audio"]["preferred_device_substring"]),
            target_sample_rate=int(cfg["audio"]["target_sample_rate"]),
            pre_roll_ms=int(cfg["audio"]["pre_roll_ms"]),
            vad_model_path=resolve_path(cfg["audio"].get("vad_model_path"), base_dir),
            vad_threshold=float(cfg["audio"]["vad_threshold"]),
            min_silence_duration=float(cfg["audio"]["min_silence_duration"]),
            commit_silence_duration=float(
                cfg["audio"].get("commit_silence_duration", DEFAULT_AUDIO_COMMIT_SILENCE_SEC)
            ),
            max_utterance_seconds=float(
                cfg["audio"].get("max_utterance_seconds", DEFAULT_AUDIO_MAX_UTTERANCE_SEC)
            ),
            session_dump_dir=resolve_path(cfg["audio"].get("session_dump_dir"), base_dir),
        )
        record(
            "音频: AudioCapture 初始化成功",
            True,
            f"source={audio.source_name}",
        )
    except Exception as exc:  # noqa: BLE001
        record("音频: AudioCapture 初始化成功", False, f"{type(exc).__name__}: {exc}")

    # ---- 2. ASR + 后处理 --------------------------------------------- #
    asr: Optional[ASREngine] = None
    asr_text = ""
    try:
        t0 = time.monotonic()
        asr = ASREngine(
            model_dir=resolve_path(cfg["asr"]["model_dir"], base_dir),
            num_threads=int(cfg["asr"]["num_threads"]),
            use_itn=bool(cfg["asr"]["use_itn"]),
            language=str(cfg["asr"]["language"]),
            provider=str(cfg["asr"]["provider"]),
        )
        load_sec = time.monotonic() - t0
        record("ASR: SenseVoice 模型加载 + warmup", True, f"耗时 {load_sec:.2f}s")
    except Exception as exc:  # noqa: BLE001
        record("ASR: SenseVoice 模型加载 + warmup", False, f"{type(exc).__name__}: {exc}")

    test_wav = None
    if asr is not None:
        model_dir = resolve_path(cfg["asr"]["model_dir"], base_dir) or ""
        for name in ("zh.wav", "en.wav", "yue.wav", "ja.wav", "ko.wav"):
            candidate = os.path.join(model_dir, "test_wavs", name)
            if os.path.isfile(candidate):
                test_wav = candidate
                break
        if test_wav is None:
            record("ASR: 找到测试音频", False, f"未在 {model_dir}/test_wavs 找到任何 wav")

    if asr is not None and test_wav is not None:
        try:
            wave_audio = read_wav_16k_mono(test_wav)
            t0 = time.monotonic()
            asr_text = asr.transcribe(wave_audio)
            asr_sec = time.monotonic() - t0
            ok = bool(asr_text and asr_text.strip())
            record(
                "ASR: 真实识别测试音频",
                ok,
                f"{os.path.basename(test_wav)} ({wave_audio.size / 16000:.1f}s) -> "
                f"{asr_text!r} ({asr_sec:.2f}s)",
            )
        except Exception as exc:  # noqa: BLE001
            record("ASR: 真实识别测试音频", False, f"{type(exc).__name__}: {exc}")

        try:
            pp = PostProcessor(cfg.get("post_processor") or {})
            processed = pp.process(asr_text or "嗯，这个 test 新的一行 你好world")
            record(
                "后处理: PostProcessor 清洗 + 自定义替换",
                bool(processed),
                f"{processed!r}",
                critical=False,
            )
        except Exception as exc:  # noqa: BLE001
            record("后处理: PostProcessor 清洗 + 自定义替换", False,
                   f"{type(exc).__name__}: {exc}")

    # ---- 2b. Level 2：LLM 端点连通性（可选，非关键） ----------------- #
    pp_cfg = cfg.get("post_processor") or {}
    if pp_cfg.get("enable_llm"):
        base = str(pp_cfg.get("api_base") or "").rstrip("/")
        try:
            import requests as _requests

            t0 = time.monotonic()
            resp = _requests.get(f"{base}/models", timeout=2.0)
            record(
                "后处理: LLM 端点可达 (Level 2)",
                resp.status_code == 200,
                f"{base}/models -> HTTP {resp.status_code}（{time.monotonic() - t0:.2f}s，"
                f"model={pp_cfg.get('model')}）",
                critical=False,
            )
        except Exception as exc:  # noqa: BLE001
            record(
                "后处理: LLM 端点可达 (Level 2)",
                False,
                f"{base} 不可达（{type(exc).__name__}），将自动降级为仅 Level 1",
                critical=False,
            )

    # ---- 3. 剪贴板 + 焦点窗口 ---------------------------------------- #
    try:
        injector = TextInjector(
            restore_clipboard=bool(cfg["injector"]["restore_clipboard"]),
            restore_delay_sec=float(cfg["injector"]["restore_delay_sec"]),
        )
        tools_ok = injector._tool("xclip") or injector._tool("xsel")  # noqa: SLF001
        record(
            "注入: 剪贴板工具可用 (xclip/xsel)",
            tools_ok,
            "xclip=%s xsel=%s" % (injector._tool("xclip"), injector._tool("xsel")),  # noqa: SLF001
        )

        backup = injector._read_selection("clipboard")  # noqa: SLF001
        marker = "__voice_input_selftest__"
        wrote = injector._write_selection("clipboard", marker)  # noqa: SLF001
        readback = injector._read_selection("clipboard") if wrote else None  # noqa: SLF001
        # 无论成功与否都尽力恢复原剪贴板，绝不污染用户内容。
        injector._write_selection("clipboard", backup if backup is not None else "")  # noqa: SLF001
        record(
            "注入: 剪贴板读写往返一致",
            bool(wrote and readback == marker),
            f"write={wrote} readback={readback!r}（已恢复原内容）",
        )

        win_id = injector._active_window_id_xlib() or injector._active_window_id_xprop()  # noqa: SLF001
        is_term = injector.is_terminal_window()
        record(
            "注入: 焦点窗口检测",
            win_id is not None,
            f"active_window={hex(win_id) if win_id else None} 终端={is_term}",
            critical=False,
        )
    except Exception as exc:  # noqa: BLE001
        record("注入: 剪贴板读写往返一致", False, f"{type(exc).__name__}: {exc}")

    # ---- 4. 录音 0.5 秒 ---------------------------------------------- #
    if audio is not None:
        try:
            audio.start_recording(mode="ptt")
            time.sleep(0.5)
            samples = audio.stop_recording()
            dur = float(samples.size) / float(cfg["audio"]["target_sample_rate"])
            peak = float(np.max(np.abs(samples))) if samples.size else 0.0
            record(
                "音频: 录音 0.5s 采集链路验证",
                samples.size > 0,
                f"采集 {dur:.2f}s / {samples.size} 样本，峰值 {peak:.4f}",
            )
        except Exception as exc:  # noqa: BLE001
            record("音频: 录音 0.5s 采集链路验证", False, f"{type(exc).__name__}: {exc}")

    # ---- 4b. 连续听写"整段累积"逻辑（合成音频，不依赖麦克风） ---------- #
    if audio is not None and test_wav is not None:
        try:
            # 关掉采集线程，避免真实环境声音混进合成测试音频。
            audio.close()
            merge_detail = _probe_toggle_merge(
                audio, read_wav_16k_mono(test_wav), cfg["audio"]
            )
            record(
                "音频: 连续听写整段累积（短停顿合并 / 长停顿切分）",
                bool(merge_detail.get("ok")),
                str(merge_detail.get("detail", "")),
            )
        except Exception as exc:  # noqa: BLE001
            record(
                "音频: 连续听写整段累积（短停顿合并 / 长停顿切分）",
                False,
                f"{type(exc).__name__}: {exc}",
            )

    # ---- 5. 释放资源 -------------------------------------------------- #
    if audio is not None:
        try:
            audio.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- 汇总 --------------------------------------------------------- #
    n_pass = sum(1 for _, ok, _, _ in results if ok)
    n_fail = sum(1 for _, ok, _, crit in results if not ok and crit)
    n_warn = sum(1 for _, ok, _, crit in results if not ok and not crit)

    logger.info("-" * 68)
    logger.info("自检汇总：%d 项通过，%d 项失败，%d 项告警", n_pass, n_fail, n_warn)
    if n_fail == 0:
        logger.info("✅ 全链路自检通过！")
    else:
        logger.error("❌ 自检存在 %d 项关键失败，请检查上面的报告。", n_fail)
    logger.info("=" * 68)

    return 0 if n_fail == 0 else 1


# --------------------------------------------------------------------------- #
# 子命令：一次性录音上屏
# --------------------------------------------------------------------------- #
def run_once(cfg: Dict[str, Any], base_dir: str, seconds: float) -> int:
    """立即录音 N 秒 -> 识别 -> 注入当前焦点窗口 -> 退出。"""
    app = VoiceInputApp(cfg, base_dir)
    try:
        app.build_audio()
        app.build_post_processor()
        app.build_injector()
        app.build_asr()
    except Exception:  # noqa: BLE001
        logger.exception("初始化失败，无法执行 --once")
        app.shutdown()
        return 1

    try:
        logger.info(
            "[Once] 开始录音 %.2f 秒（请对着麦克风说话，优先设备关键字：%s）…",
            seconds,
            cfg["audio"]["preferred_device_substring"],
        )
        assert app.audio is not None
        app.audio.start_recording(mode="ptt")
        time.sleep(max(0.1, float(seconds)))
        samples = app.audio.stop_recording()
        logger.info("[Once] 录音结束，开始识别并注入当前焦点窗口…")
        app._process_and_inject(samples, tag="Once")  # noqa: SLF001
    finally:
        app.shutdown()
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-input",
        description="本地离线语音输入：按住 右Shift/F9 说话，按一下 右Ctrl 连续听写。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="配置文件路径",
    )
    parser.add_argument(
        "--test-pipeline",
        action="store_true",
        help="端到端自检（不进入阻塞热键循环），输出报告后退出",
    )
    parser.add_argument(
        "--once",
        type=float,
        metavar="SECONDS",
        default=None,
        help="立即录音指定秒数，识别后注入当前焦点窗口并退出",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出调试级别日志",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=bool(args.verbose))

    config_path = os.path.abspath(os.path.expanduser(args.config))
    base_dir = os.path.dirname(config_path) or PROJECT_DIR
    cfg = load_config(config_path)

    logger.info("使用配置文件：%s", config_path)

    if args.test_pipeline:
        return run_selftest(cfg, base_dir)

    if args.once is not None:
        return run_once(cfg, base_dir, float(args.once))

    app = VoiceInputApp(cfg, base_dir)
    try:
        return app.run_forever()
    except Exception:  # noqa: BLE001
        logger.exception("voice-input 运行失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())

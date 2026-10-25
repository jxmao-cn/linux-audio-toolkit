"""PulseAudio 麦克风采集：USB 耳机 -> 16 kHz 单声道 float32。

设计要点
========

1. **精确绑定设备**：PulseAudio 下 ``parecord`` 支持 ``--device=<source>``，
   可以 100% 精确绑定某个 USB 耳机 source（例如 ``HECATE_GM400``）。
   因此主路径用 ``parecord`` 采集原生 ``s16le 2ch 48000Hz`` 音频流，
   再在 Python 中做下采样，避免 PortAudio 设备选择的不确定性。

2. **智能双声道合并**：很多 USB 耳机只有一个声道有真实信号，另一声道接近
   静音。若直接 ``(L+R)/2``，音量会掉 6 dB。这里先比较两声道能量：
   若某一声道 RMS 明显更低（< 0.05 倍），则只取有信号的那个声道，否则取均值。

3. **精确重采样**：``48000 -> 16000`` 用 ``scipy.signal.resample_poly(mono, 1, 3)``
   做多相滤波下采样。每块读 1536 帧 @48k（32 ms），下采样后正好得到
   **512 个样本 @16k**，恰好是 Silero VAD 的标准窗口大小。
   （若 scipy 不可用，退化为线性插值，仅用于兜底。）

4. **热插拔容错**：采集线程持续运行，后端异常（USB 拔出、Pipe 断开）时自动
   重新发现 source 并重建后端；预卷环形缓冲在录音开始前一直滚动，
   这样 PTT 模式可以补回按下按键之前的 ``pre_roll_ms`` 音频，避免吞首字。

5. **连续听写"整段累积"**：toggle 模式的 VAD 只用来找"停顿"，不再一断句就
   立刻识别。相邻 VAD 段之间的短停顿（说长句时换气的零点几秒到一点几秒）
   会被并回同一段音频，等**真正说完**（静音达到 ``commit_silence_duration``）
   才把累积的连续音频**一次性**送去识别。这样 SenseVoice 拿到的是整句
   上下文，而不是几秒钟一个的碎片；单段上限 ``max_utterance_seconds`` 保证
   延迟与内存有界，且到上限时优先在自然停顿处切分。

对外接口
========

* :func:`find_pulse_source`
* :class:`AudioCapture`
"""

from __future__ import annotations

import logging
import os
import queue
import select
import shutil
import subprocess
import threading
import time
from collections import deque
from math import gcd
from typing import Callable, List, Optional

import numpy as np

from .utterance_accumulator import AccumulatorConfig, UtteranceAccumulator

logger = logging.getLogger(__name__)

# Silero VAD 的标准窗口（样本数 @16k）。
SILERO_WINDOW_FRAMES = 512

# 单块采集时长（毫秒）。48 kHz 下 32 ms == 1536 帧，下采样 1/3 后 == 512。
BLOCK_DURATION_MS = 32

# PTT 模式停止录音时额外补收的尾音时长（防止吞尾字）。
PTT_TAIL_MS = 120

# RMS 能量 VAD 的最短有效语音时长（过滤键盘敲击之类的瞬时噪声）。
MIN_RMS_SPEECH_SECONDS = 0.20

# 单句最长时长，超过则强制切断，避免内存无限增长。
MAX_UTTERANCE_SECONDS = 30.0

# --------------------------------------------------------------------------- #
# 连续听写（Toggle）"整段累积"参数
# --------------------------------------------------------------------------- #
# 判定逻辑全部在 voice_input/utterance_accumulator.py（纯逻辑 + 单测），
# 这里只保留构造它所需的默认值和"采集侧"参数。
DEFAULT_COMMIT_SILENCE_SEC = 1.6
DEFAULT_MAX_UTTERANCE_SEC = 20.0

# 固定最小可听门限 = AUDIBLE_MIN_RATIO * energy_threshold：
# 起句与"是否属于敲击噪声"的兜底判据（不随环境自适应，避免漏收轻声语音）。
AUDIBLE_MIN_RATIO = 0.25

# 采集后端无数据后重连前的等待时间（秒）。
RECONNECT_DELAY_S = 0.3

# 单次管道读取的最长等待（秒）：超过则认为采集链路已卡死（USB 挂起 /
# PulseAudio 设备休眠），由上层重连。
STREAM_READ_TIMEOUT_S = 1.0

# 判定"数字静音"（设备挂起 / 通道断开时常见）的块峰值门限。
DIGITAL_SILENCE_PEAK = 1e-5

# 连续数字静音达到该时长就强制重连采集后端。
DEAD_STREAM_SECONDS = 8.0


# --------------------------------------------------------------------------- #
# 模块级工具函数
# --------------------------------------------------------------------------- #
def _run_cmd(cmd: List[str], timeout: float = 3.0) -> subprocess.CompletedProcess:
    """执行外部命令并捕获输出（文本模式）。"""
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def find_pulse_source(preferred_substring: str = "HECATE_GM400") -> Optional[str]:
    """查找 PulseAudio 输入源名称。

    优先级：

    1. ``pactl list short sources`` 中名称包含 ``preferred_substring``
       且 **不以 ``.monitor`` 结尾** 的输入源；
    2. 若未找到，返回当前 ``pactl get-default-source``（同样排除 ``.monitor``）；
    3. 都没有则返回 ``None``。

    Args:
        preferred_substring: 期望匹配的设备名子串（大小写不敏感）。

    Returns:
        输入源名称；找不到返回 ``None``。
    """
    if shutil.which("pactl") is None:
        logger.warning("未找到 pactl，无法枚举 PulseAudio 输入源")
        return None

    input_sources: List[str] = []
    try:
        result = _run_cmd(["pactl", "list", "short", "sources"])
        if result.returncode != 0:
            logger.warning(
                "pactl list short sources 返回码 %s：%s",
                result.returncode,
                (result.stderr or "").strip(),
            )
        for line in (result.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[1].strip()
            if not name or name.endswith(".monitor"):
                continue
            input_sources.append(name)
    except FileNotFoundError:
        logger.warning("pactl 不可用（FileNotFoundError）")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("pactl list short sources 超时")
    except Exception:  # noqa: BLE001 - 采集场景要求永不因枚举失败而崩溃
        logger.exception("枚举 PulseAudio 输入源时发生未预期错误")

    if preferred_substring:
        needle = preferred_substring.lower()
        for name in input_sources:
            if needle in name.lower():
                logger.info("匹配到首选音频输入源：%s", name)
                return name
        logger.warning(
            "未找到包含 '%s' 的输入源，候选：%s",
            preferred_substring,
            input_sources or "（无）",
        )

    # 回退到系统默认输入源。
    try:
        result = _run_cmd(["pactl", "get-default-source"])
        default = (result.stdout or "").strip()
        if default and not default.endswith(".monitor"):
            logger.info("回退到默认音频输入源：%s", default)
            return default
        if default:
            logger.warning("默认音频源是 monitor 设备，忽略：%s", default)
    except Exception:  # noqa: BLE001
        logger.exception("获取默认音频输入源失败")

    return None


def _resample(x: np.ndarray, native_rate: int, target_rate: int) -> np.ndarray:
    """把 ``x``（float32 单声道）从 ``native_rate`` 重采样到 ``target_rate``。

    优先使用 ``scipy.signal.resample_poly``（多相滤波，质量好且快）；
    若 scipy 不可用则退化为线性插值。
    """
    x = np.asarray(x, dtype=np.float32)
    if native_rate == target_rate or x.size == 0:
        return x
    try:
        from scipy.signal import resample_poly  # 延迟导入，避免硬依赖

        g = gcd(int(native_rate), int(target_rate))
        up = int(target_rate) // g
        down = int(native_rate) // g
        return np.asarray(resample_poly(x, up, down), dtype=np.float32)
    except Exception:  # noqa: BLE001 - 兜底：线性插值
        n_out = int(round(x.size * float(target_rate) / float(native_rate)))
        if n_out <= 0:
            return np.zeros(0, dtype=np.float32)
        xp = np.linspace(0.0, 1.0, x.size, endpoint=False)
        xq = np.linspace(0.0, 1.0, n_out, endpoint=False)
        return np.interp(xq, xp, x).astype(np.float32)


def _merge_channels_to_mono(block: np.ndarray) -> np.ndarray:
    """把 ``(frames, channels)`` 的 int16 帧合并为单声道 float32。

    智能策略：比较各声道 RMS，若某声道能量极低（< 0.05 倍最强声道），
    只取有信号的声道（避免平均后音量掉 6 dB）；否则对各声道求平均。
    """
    if block.ndim != 2:
        raise ValueError(f"预期二维帧数组，收到 shape={block.shape}")

    x = block.astype(np.float32) / 32768.0
    n_ch = x.shape[1]
    if n_ch == 1:
        return x[:, 0]

    rms = np.sqrt(np.mean(np.square(x), axis=0) + 1e-12)
    dominant = int(np.argmax(rms))
    strongest = float(rms[dominant])

    if strongest > 1e-6:
        weak = [c for c in range(n_ch) if rms[c] < 0.05 * strongest]
        # 只有部分声道有信号：只保留有信号声道，避免平均导致音量下降。
        if len(weak) >= n_ch - 1:
            return x[:, dominant]

    return np.mean(x, axis=1).astype(np.float32)


def _to_mono_16k(
    raw_i16: bytes,
    channels: int,
    native_rate: int,
    target_rate: int,
) -> np.ndarray:
    """把原始 int16 交错字节流转换为目标采样率的单声道 float32。"""
    if channels < 1:
        raise ValueError("channels 必须 >= 1")
    samples = np.frombuffer(raw_i16, dtype="<i2")
    usable = (samples.size // channels) * channels
    if usable == 0:
        return np.zeros(0, dtype=np.float32)
    frames = samples[:usable].reshape(-1, channels)
    mono = _merge_channels_to_mono(frames)
    return _resample(mono, native_rate, target_rate)


def _concat(chunks: List[np.ndarray]) -> np.ndarray:
    """拼接若干一维 float32 数组。"""
    chunks = [c for c in chunks if c is not None and c.size > 0]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    if len(chunks) == 1:
        return np.asarray(chunks[0], dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32, copy=False)


# --------------------------------------------------------------------------- #
# 采集后端
# --------------------------------------------------------------------------- #
class _BaseBackend:
    """采集后端抽象：每次 ``read_block`` 返回一块 16k 单声道 float32。"""

    name = "base"

    def read_block(self) -> Optional[np.ndarray]:
        """返回一块音频；数据流结束（EOF/关闭）时返回 ``None``。"""
        raise NotImplementedError

    def is_alive(self) -> bool:
        """后端当前是否处于可用状态。"""
        return not self._closed

    def close(self) -> None:
        raise NotImplementedError


class _ParecordBackend(_BaseBackend):
    """基于 ``parecord --device=<source>`` 的后端（主路径）。"""

    name = "parecord"

    def __init__(
        self,
        source_name: Optional[str],
        native_rate: int,
        native_channels: int,
        target_rate: int,
    ) -> None:
        self.native_rate = int(native_rate)
        self.native_channels = int(native_channels)
        self.target_rate = int(target_rate)
        self.source_name = source_name
        self._closed = False

        # 32 ms 的原生帧数（48k -> 1536）。
        self.frames_per_block = max(
            1, int(round(self.native_rate * BLOCK_DURATION_MS / 1000.0))
        )
        self.bytes_per_block = self.frames_per_block * self.native_channels * 2

        cmd = [
            "parecord",
            "--raw",
            "--format=s16le",
            f"--rate={self.native_rate}",
            f"--channels={self.native_channels}",
            "--latency-msec=20",
            "--client-name=voice-input-capture",
        ]
        if source_name:
            cmd.append(f"--device={source_name}")

        logger.info("启动 parecord 后端：%s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stdout = self._proc.stdout
        self._stderr = self._proc.stderr

    def _read_exact(self, nbytes: int) -> Optional[bytes]:
        """从管道读取 ``nbytes`` 字节；EOF / 卡死时返回 ``None``。

        用 ``select`` + ``os.read`` 而不是阻塞式 ``read``：这样即使设备被
        挂起、管道卡住，也能在 ``STREAM_READ_TIMEOUT_S`` 后放弃并让上层重连，
        不会让采集线程永久卡死（表现为"录音没反应 / 丢内容"）。
        """
        chunks: List[bytes] = []
        remaining = nbytes
        fd = self._stdout.fileno()
        while remaining > 0:
            try:
                ready, _, _ = select.select([fd], [], [], STREAM_READ_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - select 失败则退回阻塞读
                ready = [fd]
            if not ready:
                logger.warning(
                    "parecord %.1fs 无数据（设备休眠 / 管道阻塞），按断流处理并重连",
                    STREAM_READ_TIMEOUT_S,
                )
                return None
            try:
                data = os.read(fd, remaining)
            except (BlockingIOError, InterruptedError):
                continue
            if not data:
                if self._proc.poll() is not None:
                    err = b""
                    try:
                        err = self._stderr.read() or b""
                    except Exception:  # noqa: BLE001
                        pass
                    logger.warning(
                        "parecord 进程退出（code=%s）：%s",
                        self._proc.returncode,
                        err.decode("utf-8", "replace").strip(),
                    )
                return None
            chunks.append(data)
            remaining -= len(data)
        return b"".join(chunks)

    def is_alive(self) -> bool:
        """parecord 子进程是否仍在运行。"""
        return self._proc.poll() is None

    def read_block(self) -> Optional[np.ndarray]:
        raw = self._read_exact(self.bytes_per_block)
        if raw is None or len(raw) < self.bytes_per_block:
            return None
        return _to_mono_16k(
            raw, self.native_channels, self.native_rate, self.target_rate
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            logger.debug("关闭 parecord 进程时出现异常", exc_info=True)
        for stream in (self._stdout, self._stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass


class _SounddeviceBackend(_BaseBackend):
    """基于 ``sounddevice`` (PortAudio) 的后端（fallback）。"""

    name = "sounddevice"

    def __init__(
        self,
        preferred_substring: str,
        native_rate: int,
        native_channels: int,
        target_rate: int,
    ) -> None:
        import sounddevice as sd  # 延迟导入

        self._sd = sd
        self.target_rate = int(target_rate)
        self._closed = False
        self._queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=256)

        device = self._find_device(preferred_substring)

        # 依次尝试若干 (采样率, 声道数) 组合，优先设备原生格式以保持质量。
        # 先尝试设备端口报告的默认采样率（自动适配 44.1kHz / 48kHz 等），
        # 再依次尝试传入的原生格式与 target 采样率的单/双声道组合。
        candidates: list[tuple[int, int]] = []
        try:
            dev_info = (
                sd.query_devices(device)
                if device is not None
                else sd.query_devices(kind="input")
            )
            native_default = int(round(float(dev_info.get("default_samplerate") or 0)))
            if native_default > 0:
                candidates.append((native_default, int(native_channels)))
                candidates.append((native_default, 1))
        except Exception as exc:  # noqa: BLE001 - 查询失败则用固定候选
            logger.debug("查询设备默认采样率失败：%s", exc)

        candidates += [
            (int(native_rate), int(native_channels)),
            (int(native_rate), 1),
            (int(target_rate), 1),
            (int(target_rate), 2),
            (44100, 1),
            (44100, 2),
        ]
        # 去重并保序，避免重复尝试同一组合。
        seen: set = set()
        candidates = [c for c in candidates if not (c in seen or seen.add(c))]

        last_err: Optional[Exception] = None
        self._stream = None
        for rate, channels in candidates:
            try:
                self._stream = sd.InputStream(
                    samplerate=rate,
                    channels=channels,
                    dtype="int16",
                    device=device,
                    blocksize=max(1, int(round(rate * BLOCK_DURATION_MS / 1000.0))),
                    callback=self._callback,
                )
                self._stream.start()
                self.native_rate = rate
                self.native_channels = channels
                logger.info(
                    "启动 sounddevice 后端：device=%s rate=%d channels=%d",
                    device,
                    rate,
                    channels,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                self._stream = None
                logger.debug(
                    "sounddevice 打开失败 rate=%d channels=%d：%s", rate, channels, exc
                )
        if self._stream is None:
            raise RuntimeError(f"sounddevice 无法打开任何输入配置：{last_err}")

    def _find_device(self, preferred_substring: str) -> Optional[int]:
        """在 PortAudio 设备列表中匹配子串，找不到则用默认输入设备。"""
        try:
            devices = self._sd.query_devices()
        except Exception:  # noqa: BLE001
            return None
        needle = (preferred_substring or "").lower()
        if needle:
            for idx, dev in enumerate(devices):
                if dev.get("max_input_channels", 0) > 0 and needle in str(
                    dev.get("name", "")
                ).lower():
                    logger.info("sounddevice 匹配到设备 #%d：%s", idx, dev.get("name"))
                    return idx
        try:
            default = self._sd.default.device
            return int(default[0]) if default[0] is not None and default[0] >= 0 else None
        except Exception:  # noqa: BLE001
            return None

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            logger.debug("sounddevice 状态告警：%s", status)
        try:
            self._queue.put_nowait(bytes(indata))
        except queue.Full:
            # 消费太慢：丢弃最旧的一块，保证实时性。
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(bytes(indata))
            except Exception:  # noqa: BLE001
                pass

    def read_block(self) -> Optional[np.ndarray]:
        while not self._closed:
            try:
                data = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if data is None:
                return None
            return _to_mono_16k(
                data, self.native_channels, self.native_rate, self.target_rate
            )
        return None

    def is_alive(self) -> bool:
        return not self._closed and self._stream is not None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭 sounddevice 流时出现异常", exc_info=True)


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
class AudioCapture:
    """高可靠麦克风采集器（16 kHz 单声道 float32）。

    Args:
        preferred_device_substring: 优先匹配的 PulseAudio 输入源子串。
        target_sample_rate: 输出采样率，默认 16000。
        pre_roll_ms: 录音开始前保留的预卷时长（毫秒），防止吞首字。
        vad_model_path: Silero VAD (``sherpa_onnx``) 模型路径；为 ``None``
            或文件不存在时，toggle 模式退化为 RMS 能量端点检测。
        vad_threshold: Silero VAD 语音概率阈值。
        min_silence_duration: VAD 判定"一个停顿"所需的最短静音（秒），
            也是"整段累积"里判定停顿的粒度。
        commit_silence_duration: 连续听写里判定"这句说完了、可以整体识别"
            所需的静音时长（秒）。比 ``min_silence_duration`` 大，正是为了让
            说长句时的换气停顿不被切成碎片。默认 1.6s。
        max_utterance_seconds: 单次整段识别的最长语音时长（秒）。达到后在
            最近一次自然停顿处提交，避免延迟与内存无限增长。默认 20s。
        native_sample_rate: 采集后端的原生采样率，默认 48000。
        native_channels: 采集后端的原生声道数，默认 2。
        energy_threshold: RMS 兜底 VAD 的语音能量阈值（float32 归一化后）。
        prefer_parecord: 是否优先使用 ``parecord`` 后端，默认 ``True``。
    """

    def __init__(
        self,
        preferred_device_substring: str = "HECATE_GM400",
        target_sample_rate: int = 16000,
        pre_roll_ms: int = 250,
        vad_model_path: Optional[str] = None,
        vad_threshold: float = 0.45,
        min_silence_duration: float = 0.6,
        commit_silence_duration: float = DEFAULT_COMMIT_SILENCE_SEC,
        max_utterance_seconds: float = DEFAULT_MAX_UTTERANCE_SEC,
        native_sample_rate: int = 48000,
        native_channels: int = 2,
        energy_threshold: float = 0.012,
        prefer_parecord: bool = True,
        session_dump_dir: Optional[str] = None,
    ) -> None:
        self.preferred_device_substring = preferred_device_substring
        self.target_sample_rate = int(target_sample_rate)
        self.pre_roll_ms = int(pre_roll_ms)
        self.vad_model_path = vad_model_path
        self.vad_threshold = float(vad_threshold)
        self.min_silence_duration = float(min_silence_duration)
        self.commit_silence_duration = float(commit_silence_duration)
        self.max_utterance_seconds = float(max_utterance_seconds)
        if self.commit_silence_duration < self.min_silence_duration:
            logger.warning(
                "commit_silence_duration(%.2fs) 小于 min_silence_duration(%.2fs)，"
                "已按 %.2fs 处理（否则一整段会被拆成碎片）",
                self.commit_silence_duration,
                self.min_silence_duration,
                self.min_silence_duration,
            )
            self.commit_silence_duration = self.min_silence_duration
        self.native_sample_rate = int(native_sample_rate)
        self.native_channels = int(native_channels)
        self.energy_threshold = float(energy_threshold)
        self.prefer_parecord = bool(prefer_parecord)
        # 非空时把每次录音的原始音频写成 wav（供 tools/session_replay.py 确定性重放）。
        self.session_dump_dir = session_dump_dir or None

        self._pre_roll_samples = max(
            0, int(self.pre_roll_ms / 1000.0 * self.target_sample_rate)
        )

        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        # 预卷环形缓冲：始终滚动保存最近 pre_roll_ms 的音频。
        self._ring: "deque[np.ndarray]" = deque()
        self._ring_len = 0

        # 当前录音状态。
        self._recording = False
        self._mode = "ptt"
        self._on_utterance: Optional[Callable[[np.ndarray], None]] = None
        self._record_chunks: List[np.ndarray] = []

        # toggle 模式的端点检测状态。
        self._vad = None
        self._vad_failed = False
        self._vad_flag_ok = True  # sherpa_onnx 是否支持 is_speech_detected()
        # 整段累积器：所有"什么时候算说完 / 该不该丢"的判定都在这个纯逻辑模块里
        # （单测：tests/test_utterance_accumulator.py）。
        self._acc = UtteranceAccumulator(self._build_accumulator_config())
        # 采集链路健康检查状态（详见 _check_stream_health_locked）。
        self._dead_seconds = 0.0
        self._dead_reconnect_done = False
        self._ever_had_audio = False
        # 可选的原始音频落盘缓冲（调试用，默认关闭）。
        self._dump_chunks: List[np.ndarray] = []

        # 采集后端与线程。
        self._backend: Optional[_BaseBackend] = None
        self._reader_thread: Optional[threading.Thread] = None

        self._source: Optional[str] = None
        self._last_error: Optional[str] = None

        logger.info(
            "AudioCapture 初始化：device_substr=%s target=%dHz pre_roll=%dms "
            "vad_model=%s min_silence=%.2fs commit_silence=%.2fs max_utterance=%.1fs",
            self.preferred_device_substring,
            self.target_sample_rate,
            self.pre_roll_ms,
            self.vad_model_path or "（无，使用 RMS）",
            self.min_silence_duration,
            self.commit_silence_duration,
            self.max_utterance_seconds,
        )

        # 启动采集线程（best-effort），让预卷环形缓冲先滚动起来。
        try:
            self._start_stream()
        except Exception:  # noqa: BLE001 - 构造阶段不因设备缺失而失败
            logger.exception("初始化时启动音频流失败，将在录音时重试")

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #
    @property
    def source_name(self) -> Optional[str]:
        """当前绑定的 PulseAudio 输入源名（可能为 ``None``）。"""
        return self._source

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _start_stream(self) -> None:
        """启动采集线程（幂等）。

        会先 **同步** 打开一次后端，让预卷环形缓冲尽快滚动起来
        （这样 PTT 首次按下也能补回按键前的音频）。
        """
        with self._lock:
            if self._reader_thread is not None and self._reader_thread.is_alive():
                return
            if self._backend is None:
                try:
                    self._backend = self._open_backend()
                    self._last_error = None
                except Exception as exc:  # noqa: BLE001 - 采集线程会持续重试
                    self._last_error = str(exc)
                    logger.warning("预打开采集后端失败（将由采集线程重试）：%s", exc)
            self._stop_event.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_loop,
                name="voice-input-reader",
                daemon=True,
            )
            self._reader_thread.start()

    def _open_backend(self) -> _BaseBackend:
        """发现设备并打开采集后端（优先 parecord，失败回退 sounddevice）。"""
        self._source = find_pulse_source(self.preferred_device_substring)

        errors: List[str] = []

        if self.prefer_parecord and shutil.which("parecord"):
            try:
                backend = _ParecordBackend(
                    source_name=self._source,
                    native_rate=self.native_sample_rate,
                    native_channels=self.native_channels,
                    target_rate=self.target_sample_rate,
                )
                # 给 parecord 一点时间；若立刻退出说明绑定失败。
                time.sleep(0.15)
                if backend.is_alive():
                    logger.info("采集后端就绪：parecord（source=%s）", self._source)
                    return backend
                backend.close()
                errors.append("parecord 进程立即退出（设备绑定失败？）")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"parecord 启动失败：{exc}")
                logger.warning("parecord 后端不可用：%s", exc)
        elif self.prefer_parecord:
            errors.append("系统未安装 parecord")

        # 回退 sounddevice
        try:
            backend = _SounddeviceBackend(
                preferred_substring=self.preferred_device_substring,
                native_rate=self.native_sample_rate,
                native_channels=self.native_channels,
                target_rate=self.target_sample_rate,
            )
            logger.info("采集后端就绪：sounddevice")
            return backend
        except ImportError as exc:
            errors.append(f"sounddevice 未安装：{exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"sounddevice 启动失败：{exc}")
            logger.warning("sounddevice 后端不可用：%s", exc)

        self._last_error = "；".join(errors) if errors else "无可用采集后端"
        raise RuntimeError(self._last_error)

    def _reader_loop(self) -> None:
        """后台采集线程：持续读取音频、维护预卷缓冲、分发到录音逻辑。"""
        logger.debug("采集线程启动")
        while not self._stop_event.is_set():
            backend = self._backend
            if backend is None:
                try:
                    backend = self._open_backend()
                    self._backend = backend
                    self._last_error = None
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                    logger.warning("打开采集后端失败，%.1fs 后重试：%s", RECONNECT_DELAY_S, exc)
                    if self._stop_event.wait(RECONNECT_DELAY_S):
                        break
                    continue

            try:
                block = backend.read_block()
            except Exception as exc:  # noqa: BLE001
                logger.exception("读取音频块异常：%s", exc)
                block = None

            if block is None:
                if self._stop_event.is_set():
                    break
                # 数据流中断（USB 热插拔 / 管道断开）：关闭并重连。
                logger.warning("采集后端无数据，尝试重连…")
                try:
                    backend.close()
                except Exception:  # noqa: BLE001
                    pass
                self._backend = None
                if self._stop_event.wait(RECONNECT_DELAY_S):
                    break
                continue

            if block.size == 0:
                continue

            self._on_block(block)

        logger.debug("采集线程退出")

    # ------------------------------------------------------------------ #
    # 单块处理
    # ------------------------------------------------------------------ #
    def _on_block(self, block: np.ndarray) -> None:
        """处理一块 16k 单声道 float32 音频。"""
        emissions: List[np.ndarray] = []

        with self._lock:
            # 起句时要并入"本块之前"的预卷音频：必须在把当前块压入环形缓冲**之前**
            # 取快照，否则当前块会被重复计入（32ms 重复会破坏音频连续性）。
            prefix = None
            if (
                self._recording
                and self._mode == "toggle"
                and not self._acc.active
            ):
                prefix = self._ring_snapshot_locked()

            self._push_ring_locked(block)
            if self._dump_chunks is not None and self._recording and self.session_dump_dir:
                self._dump_chunks.append(block)

            if not self._recording:
                return

            self._check_stream_health_locked(block)

            if self._mode == "ptt":
                self._record_chunks.append(block)
            else:
                self._handle_toggle_locked(block, prefix, emissions)

        for samples in emissions:
            self._emit_utterance(samples)

    def _check_stream_health_locked(self, block: np.ndarray) -> None:
        """采集链路健康检查（调用方持锁，仅录音期间执行）。

        真实麦克风总有一点底噪。若**本次录音里已经采到过正常电平**，之后却连续
        ``DEAD_STREAM_SECONDS`` 收到"数字静音"（峰值 ~0），基本可以断定设备被
        挂起 / 通道断开（USB 自动挂起、PulseAudio 休眠等），音频会整段丢失。
        这种情况下主动强制重连一次后端，避免"说了一长串却什么都没录到"。
        每个录音会话最多触发一次，防止反复重连。
        """
        peak = float(np.max(np.abs(block))) if block.size else 0.0
        if peak < DIGITAL_SILENCE_PEAK:
            self._dead_seconds += block.size / float(self.target_sample_rate)
        else:
            self._dead_seconds = 0.0
            self._ever_had_audio = True

        if self._dead_reconnect_done or not self._ever_had_audio:
            return
        if self._dead_seconds < DEAD_STREAM_SECONDS:
            return

        self._dead_reconnect_done = True
        self._dead_seconds = 0.0
        logger.warning(
            "采集链路疑似异常：录音期间连续 %.1fs 收到全静音（峰值 < %.0e），"
            "强制重连音频后端（USB 挂起 / 设备休眠常见现象）",
            DEAD_STREAM_SECONDS,
            DIGITAL_SILENCE_PEAK,
        )
        self._force_reconnect_locked()

    def _force_reconnect_locked(self) -> None:
        """关闭当前采集后端，让采集线程重新发现设备并重建（调用方持锁）。"""
        backend = self._backend
        self._backend = None
        if backend is not None:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                logger.debug("强制重连时关闭后端异常", exc_info=True)


    def _push_ring_locked(self, block: np.ndarray) -> None:
        """维护预卷环形缓冲（调用方需持锁）。"""
        self._ring.append(block)
        self._ring_len += block.size
        while self._ring_len > self._pre_roll_samples and len(self._ring) > 1:
            self._ring_len -= self._ring.popleft().size

    def _ring_snapshot_locked(self) -> List[np.ndarray]:
        return list(self._ring)

    def _handle_toggle_locked(
        self,
        block: np.ndarray,
        prefix: Optional[List[np.ndarray]],
        emissions: List[np.ndarray],
    ) -> None:
        """toggle 模式：用 VAD 给出"是否语音 / 是否停顿边界"，其余交给整段累积器。"""
        vad = self._get_vad_locked()
        vad_speech = False
        vad_boundary = False
        if vad is not None:
            vad_speech, vad_boundary = self._vad_observe_locked(vad, block)
        self._accumulate_locked(block, vad_speech, vad_boundary, prefix, emissions)

    def _accumulate_locked(
        self,
        block: np.ndarray,
        vad_speech: bool,
        vad_boundary: bool,
        prefix: Optional[List[np.ndarray]],
        emissions: List[np.ndarray],
    ) -> None:
        """把本块交给整段累积器，并把产出的整段交给回调（调用方持锁）。"""
        for utterance in self._acc.push(
            block,
            vad_speech=vad_speech,
            vad_boundary=vad_boundary,
            prefix=prefix,
        ):
            logger.info(utterance.describe())
            emissions.append(utterance.samples)

    def _get_vad_locked(self):  # noqa: ANN202
        """惰性构建 sherpa_onnx VAD 对象；失败则永久退化为 RMS（持锁）。"""
        if self._vad is not None or self._vad_failed:
            return self._vad
        if not self.vad_model_path or not os.path.isfile(self.vad_model_path):
            if self.vad_model_path:
                logger.warning("VAD 模型不存在：%s，退化为 RMS 能量检测", self.vad_model_path)
            else:
                logger.info("未提供 VAD 模型，使用 RMS 能量检测")
            self._vad_failed = True
            return None
        try:
            import sherpa_onnx

            config = sherpa_onnx.VadModelConfig()
            silero = config.silero_vad
            silero.model = self.vad_model_path
            silero.threshold = float(self.vad_threshold)
            silero.min_silence_duration = float(self.min_silence_duration)
            silero.min_speech_duration = float(MIN_RMS_SPEECH_SECONDS)
            # VAD 内部的强制切断点：留出余地，让"整段累积"优先在自然停顿处切分。
            silero.max_speech_duration = float(
                max(MAX_UTTERANCE_SECONDS, self.max_utterance_seconds + 10.0)
            )
            silero.window_size = int(SILERO_WINDOW_FRAMES)
            config.sample_rate = int(self.target_sample_rate)

            try:
                self._vad = sherpa_onnx.VoiceActivityDetector(
                    config, buffer_size_in_seconds=30
                )
            except TypeError:
                # 兼容旧版 API（无 buffer_size_in_seconds 参数）。
                self._vad = sherpa_onnx.VoiceActivityDetector(config)
            # 旧版 sherpa-onnx 没有 is_speech_detected()：此时只用 RMS 判定语音，
            # 不能让"VAD 段边界"参与静音计时（会重复计数）。
            self._vad_flag_ok = callable(getattr(self._vad, "is_speech_detected", None))
            if not self._vad_flag_ok:
                logger.warning(
                    "当前 sherpa-onnx 无 is_speech_detected()，整段累积改用 RMS 能量判定语音"
                )
            logger.info("已启用 Silero VAD（sherpa_onnx）：%s", self.vad_model_path)
            return self._vad
        except Exception as exc:  # noqa: BLE001
            logger.warning("构建 Silero VAD 失败，退化为 RMS：%s", exc)
            self._vad_failed = True
            self._vad = None
            return None

    def _vad_observe_locked(self, vad, block: np.ndarray):
        """让 VAD 看一眼本块，返回 ``(是否语音, 是否有段边界)``（调用方持锁）。

        VAD 只当传感器用：段音频本身不采用（连续音频由累积器自己维护，
        避免拼接缝隙），仅提供"语音 / 停顿 / 段边界"三个信息。
        """
        try:
            vad.accept_waveform(np.ascontiguousarray(block, dtype=np.float32))
            # 先读语音标记（反映本块处理后的判定），再把段队列清空（防止内存增长）。
            flag = getattr(vad, "is_speech_detected", None)
            vad_speech = bool(flag()) if self._vad_flag_ok and callable(flag) else False
            boundary = False
            while not vad.empty():
                vad.pop()
                boundary = True
            if not self._vad_flag_ok:
                # 旧版 sherpa-onnx 没有语音标记：此时"段边界"不能参与静音计时
                # （会把静音重复算两次），只用它做强制切分点。
                vad_speech = False
            return vad_speech, boundary
        except Exception as exc:  # noqa: BLE001
            logger.warning("Silero VAD 处理失败，退化为 RMS：%s", exc)
            self._vad = None
            self._vad_failed = True
            return False, False

    def _build_accumulator_config(self) -> AccumulatorConfig:
        """把采集侧配置翻译成整段累积器的配置（判定逻辑见该模块文档）。"""
        return AccumulatorConfig(
            sample_rate=self.target_sample_rate,
            commit_silence_sec=self.commit_silence_duration,
            max_utterance_sec=self.max_utterance_seconds,
            min_silence_sec=self.min_silence_duration,
            min_level=AUDIBLE_MIN_RATIO * self.energy_threshold,
        )

    def reset_utterance_state(self) -> None:
        """清空整段累积状态（开始一次新的连续听写时调用）。"""
        with self._lock:
            self._acc.reset_session()

    def _emit_utterance(self, samples: np.ndarray) -> None:
        """异步回调一句话音频（不阻塞采集线程）。"""
        callback = self._on_utterance
        if callback is None or samples.size == 0:
            return
        samples = np.ascontiguousarray(samples, dtype=np.float32)

        def _runner() -> None:
            try:
                callback(samples)
            except Exception:  # noqa: BLE001
                logger.exception("on_utterance_callback 执行异常")

        threading.Thread(target=_runner, name="voice-input-cb", daemon=True).start()

    # ------------------------------------------------------------------ #
    # 公开录音接口
    # ------------------------------------------------------------------ #
    def start_recording(
        self,
        mode: str = "ptt",
        on_utterance_callback: Optional[Callable[[np.ndarray], None]] = None,
    ) -> None:
        """开始录音。

        Args:
            mode: ``"ptt"``（按住说话）或 ``"toggle"``（连续听写）。
            on_utterance_callback: toggle 模式下每次"整段说完"（静音达到
                ``commit_silence_duration``）或整段超过 ``max_utterance_seconds``
                时调用，入参为 16 kHz float32 一维数组（完整整段音频）。
        """
        if mode not in ("ptt", "toggle"):
            raise ValueError(f"不支持的录音模式：{mode!r}（应为 'ptt' 或 'toggle'）")

        if self._recording:
            logger.warning("录音已在进行中，先停止上一次录音")
            self.stop_recording()

        try:
            self._start_stream()
        except Exception:  # noqa: BLE001
            logger.exception("启动采集线程失败")

        with self._lock:
            self._mode = mode
            self._on_utterance = on_utterance_callback
            self._recording = True
            # 清空整段累积状态与会话统计（判定逻辑在 utterance_accumulator.py）。
            self._acc.reset_session()
            self._dump_chunks = []
            self._dead_seconds = 0.0
            self._dead_reconnect_done = False
            self._ever_had_audio = False

            if mode == "ptt":
                # 用预卷环形缓冲初始化，补回按键前的音频。
                self._record_chunks = self._ring_snapshot_locked()
            else:
                self._record_chunks = []
                # 提前构建 VAD，避免首块处理时延。
                vad = self._get_vad_locked()
                if vad is not None:
                    # 关键：清空上一次 Toggle 可能残留在 VAD 内部缓冲区的音频，
                    # 否则残留语音会在下一次开启 Toggle 时突然弹出来。
                    try:
                        vad.reset()
                    except Exception:  # noqa: BLE001
                        logger.debug("重置 Silero VAD 失败", exc_info=True)
                    # 把预卷缓冲喂给 VAD，避免吞掉连续听写的第一句话开头。
                    for chunk in self._ring_snapshot_locked():
                        if chunk is None or chunk.size == 0:
                            continue
                        try:
                            vad.accept_waveform(
                                np.ascontiguousarray(chunk, dtype=np.float32)
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug("预卷喂给 Silero VAD 失败", exc_info=True)
                            break

        logger.info(
            "开始录音：mode=%s（预卷 %d 样本）",
            mode,
            sum(c.size for c in self._record_chunks),
        )

    def stop_recording(self) -> np.ndarray:
        """停止录音并返回完整的一维 ``np.float32``（16 kHz）数组。

        PTT 模式下会额外补收约 120 ms 尾音，防止吞尾字。
        """
        with self._lock:
            was_ptt = self._mode == "ptt"
            was_recording = self._recording

        if not was_recording:
            logger.warning("stop_recording 在未录音状态下被调用，返回空数组")
            return np.zeros(0, dtype=np.float32)

        # PTT：继续采集一小段时间作为尾音。
        if was_ptt:
            deadline = time.monotonic() + PTT_TAIL_MS / 1000.0
            while time.monotonic() < deadline and not self._stop_event.is_set():
                time.sleep(0.01)

        with self._lock:
            self._recording = False
            if was_ptt:
                audio = _concat(self._record_chunks)
                self._record_chunks = []
            else:
                # toggle：让累积器交出还没提交的整段（它就是连续音频，不需要
                # 再拼 VAD 残余段——那部分音频早已包含在内，拼接会重复）。
                utterance = self._acc.flush()
                if utterance is not None:
                    # 这里**不做**"疑似噪声"过滤：会话结尾那半句可能正是用户想说的
                    # 最后一句，宁可由 ASR 判定（全静音会返回空文本），绝不在此丢内容。
                    logger.info(utterance.describe())
                    audio = utterance.samples
                else:
                    # 兜底：累积器里没有内容（例如全程未触发起句）时退回 VAD 残余段。
                    audio = self._drain_vad_residue_locked()
                try:
                    if self._vad is not None:
                        self._vad.reset()
                except Exception:  # noqa: BLE001
                    logger.debug("重置 Silero VAD 失败", exc_info=True)
            dump = self._take_dump_locked() if self.session_dump_dir else None
            self._on_utterance = None

        if dump is not None:
            self._write_dump(dump)

        duration = audio.size / float(self.target_sample_rate)
        logger.info(
            "停止录音：mode=%s %s %.3fs（%d 样本）",
            "ptt" if was_ptt else "toggle",
            "采集" if was_ptt else "待提交整段",
            duration,
            audio.size,
        )
        if not was_ptt:
            # 会话小结（由累积器统计）：用于事后核对"是不是有内容被丢了"。
            stats = self._acc.stats
            logger.info(
                stats.describe(self._acc.level_floor, self._acc.threshold)
            )
            if stats.max_peak < DIGITAL_SILENCE_PEAK:
                logger.warning(
                    "本次连续听写采集到的音频几乎全为数字静音（峰值 %.1e）："
                    "麦克风被挂起 / 静音 / 采集链路异常，请检查音频设备",
                    stats.max_peak,
                )
        return audio

    # ------------------------------------------------------------------ #
    # 可选的原始音频落盘（调试：供 tools/session_replay.py 确定性重放）
    # ------------------------------------------------------------------ #
    def _take_dump_locked(self) -> Optional[np.ndarray]:
        """取出本次录音的原始音频并清空缓冲（调用方持锁）。"""
        chunks = self._dump_chunks
        self._dump_chunks = []
        return _concat(chunks) if chunks else None

    def _write_dump(self, audio: np.ndarray) -> None:
        """把原始音频写成 16kHz 单声道 16bit wav（失败不影响主流程）。"""
        import wave

        try:
            os.makedirs(self.session_dump_dir, exist_ok=True)  # type: ignore[arg-type]
            name = time.strftime("session-%Y%m%d-%H%M%S.wav")
            path = os.path.join(self.session_dump_dir, name)  # type: ignore[arg-type]
            pcm = np.clip(audio, -1.0, 1.0)
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.target_sample_rate)
                wf.writeframes((pcm * 32767.0).astype("<i2").tobytes())
            logger.info("本次录音已落盘：%s（%.2fs）", path, audio.size / self.target_sample_rate)
        except Exception:  # noqa: BLE001
            logger.warning("录音落盘失败（不影响识别）", exc_info=True)

    def _drain_vad_residue_locked(self) -> np.ndarray:
        """兜底：flush Silero VAD 并取出内部残留段（调用方持锁）。"""
        vad = self._vad
        if vad is None:
            return np.zeros(0, dtype=np.float32)
        chunks: List[np.ndarray] = []
        try:
            vad.flush()
            while not vad.empty():
                segment = vad.front
                seg = np.asarray(segment.samples, dtype=np.float32)
                vad.pop()
                if seg.size > 0:
                    chunks.append(np.ascontiguousarray(seg, dtype=np.float32))
        except Exception:  # noqa: BLE001
            logger.warning("刷新 Silero VAD 残留缓冲失败", exc_info=True)
        return _concat(chunks)

    def close(self) -> None:
        """停止后台线程并释放子进程 / 音频流。"""
        logger.info("关闭 AudioCapture")
        self._stop_event.set()
        with self._lock:
            self._recording = False

        backend = self._backend
        self._backend = None
        if backend is not None:
            try:
                backend.close()
            except Exception:  # noqa: BLE001
                logger.debug("关闭采集后端异常", exc_info=True)

        thread = self._reader_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                logger.warning("采集线程未能及时退出")
        self._reader_thread = None

        vad = self._vad
        self._vad = None
        if vad is not None:
            try:
                del vad
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # 上下文管理 / 析构
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "AudioCapture":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()

    def __del__(self) -> None:  # pragma: no cover - 解释器退出时的尽力清理
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

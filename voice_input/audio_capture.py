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

对外接口
========

* :func:`find_pulse_source`
* :class:`AudioCapture`
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from math import gcd
from typing import Callable, List, Optional

import numpy as np

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

# 采集后端无数据后重连前的等待时间（秒）。
RECONNECT_DELAY_S = 0.3


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
        """从管道精确读取 ``nbytes`` 字节；EOF 时返回 ``None``。"""
        chunks: List[bytes] = []
        remaining = nbytes
        while remaining > 0:
            data = self._stdout.read(remaining)
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
        min_silence_duration: 判定一句结束所需的静音时长（秒）。
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
        native_sample_rate: int = 48000,
        native_channels: int = 2,
        energy_threshold: float = 0.012,
        prefer_parecord: bool = True,
    ) -> None:
        self.preferred_device_substring = preferred_device_substring
        self.target_sample_rate = int(target_sample_rate)
        self.pre_roll_ms = int(pre_roll_ms)
        self.vad_model_path = vad_model_path
        self.vad_threshold = float(vad_threshold)
        self.min_silence_duration = float(min_silence_duration)
        self.native_sample_rate = int(native_sample_rate)
        self.native_channels = int(native_channels)
        self.energy_threshold = float(energy_threshold)
        self.prefer_parecord = bool(prefer_parecord)

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
        self._speech_active = False
        self._silence_seconds = 0.0
        self._utt_chunks: List[np.ndarray] = []
        self._utt_samples = 0

        # 采集后端与线程。
        self._backend: Optional[_BaseBackend] = None
        self._reader_thread: Optional[threading.Thread] = None

        self._source: Optional[str] = None
        self._last_error: Optional[str] = None

        logger.info(
            "AudioCapture 初始化：device_substr=%s target=%dHz pre_roll=%dms "
            "vad_model=%s min_silence=%.2fs",
            self.preferred_device_substring,
            self.target_sample_rate,
            self.pre_roll_ms,
            self.vad_model_path or "（无，使用 RMS）",
            self.min_silence_duration,
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
            self._push_ring_locked(block)

            if not self._recording:
                return

            if self._mode == "ptt":
                self._record_chunks.append(block)
            else:
                self._handle_toggle_locked(block, emissions)

        for samples in emissions:
            self._emit_utterance(samples)

    def _push_ring_locked(self, block: np.ndarray) -> None:
        """维护预卷环形缓冲（调用方需持锁）。"""
        self._ring.append(block)
        self._ring_len += block.size
        while self._ring_len > self._pre_roll_samples and len(self._ring) > 1:
            self._ring_len -= self._ring.popleft().size

    def _ring_snapshot_locked(self) -> List[np.ndarray]:
        return list(self._ring)

    def _handle_toggle_locked(
        self, block: np.ndarray, emissions: List[np.ndarray]
    ) -> None:
        """toggle 模式的端点检测（调用方需持锁）。"""
        vad = self._get_vad_locked()
        if vad is not None:
            self._handle_toggle_sherpa_locked(vad, block, emissions)
        else:
            self._handle_toggle_rms_locked(block, emissions)

    # ---- Silero VAD (sherpa_onnx) ------------------------------------ #
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
            silero.max_speech_duration = float(MAX_UTTERANCE_SECONDS)
            silero.window_size = int(SILERO_WINDOW_FRAMES)
            config.sample_rate = int(self.target_sample_rate)

            try:
                self._vad = sherpa_onnx.VoiceActivityDetector(
                    config, buffer_size_in_seconds=30
                )
            except TypeError:
                # 兼容旧版 API（无 buffer_size_in_seconds 参数）。
                self._vad = sherpa_onnx.VoiceActivityDetector(config)
            logger.info("已启用 Silero VAD（sherpa_onnx）：%s", self.vad_model_path)
            return self._vad
        except Exception as exc:  # noqa: BLE001
            logger.warning("构建 Silero VAD 失败，退化为 RMS：%s", exc)
            self._vad_failed = True
            self._vad = None
            return None

    def _handle_toggle_sherpa_locked(
        self, vad, block: np.ndarray, emissions: List[np.ndarray]
    ) -> None:
        try:
            vad.accept_waveform(np.ascontiguousarray(block, dtype=np.float32))
            while not vad.empty():
                segment = vad.front
                samples = np.asarray(segment.samples, dtype=np.float32)
                vad.pop()
                if samples.size > 0:
                    emissions.append(samples)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Silero VAD 处理失败，退化为 RMS：%s", exc)
            self._vad = None
            self._vad_failed = True
            self._handle_toggle_rms_locked(block, emissions)

    # ---- RMS 能量 VAD ------------------------------------------------- #
    def _handle_toggle_rms_locked(
        self, block: np.ndarray, emissions: List[np.ndarray]
    ) -> None:
        rms = float(np.sqrt(np.mean(np.square(block)) + 1e-12))
        is_speech = rms >= self.energy_threshold
        block_seconds = block.size / float(self.target_sample_rate)

        if is_speech:
            if not self._speech_active:
                # 语音开始：用预卷缓冲补回起始部分，避免吞首字。
                self._speech_active = True
                self._silence_seconds = 0.0
                self._utt_chunks = self._ring_snapshot_locked()
                self._utt_samples = sum(c.size for c in self._utt_chunks)
            self._utt_chunks.append(block)
            self._utt_samples += block.size
            self._silence_seconds = 0.0
        elif self._speech_active:
            self._utt_chunks.append(block)
            self._utt_samples += block.size
            self._silence_seconds += block_seconds

            min_speech_samples = int(
                MIN_RMS_SPEECH_SECONDS * self.target_sample_rate
            )
            too_long = self._utt_samples >= MAX_UTTERANCE_SECONDS * self.target_sample_rate

            if self._silence_seconds >= self.min_silence_duration or too_long:
                if self._utt_samples >= min_speech_samples:
                    emissions.append(_concat(self._utt_chunks))
                self._speech_active = False
                self._silence_seconds = 0.0
                self._utt_chunks = []
                self._utt_samples = 0

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
            mode: ``"ptt"``（按住说话）或 ``"toggle"``（连续听写 + 自动断句）。
            on_utterance_callback: toggle 模式下每检测到一句结束时调用，
                入参为 16 kHz float32 一维数组。
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
            self._speech_active = False
            self._silence_seconds = 0.0
            self._utt_chunks = []
            self._utt_samples = 0

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
                self._speech_active = False
                self._silence_seconds = 0.0
                self._utt_chunks = []
                self._utt_samples = 0
            else:
                # toggle：先 flush Silero VAD，把内部缓冲区里尚未断句的残留语音
                # 全部取出（否则用户一按结束键就丢掉最后半句话，且残留会在下一次
                # 开启 Toggle 时突然弹出）；RMS 模式则返回 _utt_chunks 残留。
                vad = self._vad
                chunks: List[np.ndarray] = []
                if vad is not None:
                    try:
                        vad.flush()
                        while not vad.empty():
                            segment = vad.front
                            seg = np.asarray(segment.samples, dtype=np.float32)
                            vad.pop()
                            if seg.size > 0:
                                chunks.append(
                                    np.ascontiguousarray(seg, dtype=np.float32)
                                )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "刷新 Silero VAD 残留缓冲失败，改用 RMS 残留", exc_info=True
                        )
                    try:
                        vad.reset()
                    except Exception:  # noqa: BLE001
                        logger.debug("重置 Silero VAD 失败", exc_info=True)
                else:
                    chunks = list(self._utt_chunks)
                audio = _concat(chunks)
                self._speech_active = False
                self._silence_seconds = 0.0
                self._utt_chunks = []
                self._utt_samples = 0
            self._on_utterance = None

        duration = audio.size / float(self.target_sample_rate)
        logger.info(
            "停止录音：mode=%s 采集 %.3fs（%d 样本）",
            "ptt" if was_ptt else "toggle",
            duration,
            audio.size,
        )
        return audio

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

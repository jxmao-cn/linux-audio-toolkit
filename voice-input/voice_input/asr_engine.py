"""SenseVoice 语音识别引擎封装。

基于 ``sherpa-onnx`` 的 ``OfflineRecognizer``（SenseVoice 模型）做本地离线
语音转文字。设计目标是"按键说话、松手出字"的低延迟体验，因此：

* 构造时优先加载 int8 量化模型（``model.int8.onnx``），速度更快；
* 构造完成后自动跑一次静音 warmup，把首次推理的冷启动开销提前摊掉；
* ``transcribe`` 对过短 / 全静音音频做短路，避免无谓推理。

依赖：``numpy``、``sherpa-onnx``。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

try:  # sherpa-onnx 是可选的重依赖，导入失败时给出清晰提示而不是 import 报错
    import sherpa_onnx
except ImportError as _exc:  # pragma: no cover - 取决于运行环境
    sherpa_onnx = None  # type: ignore[assignment]
    _SHERPA_IMPORT_ERROR: Optional[BaseException] = _exc
else:
    _SHERPA_IMPORT_ERROR = None


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
SAMPLE_RATE = 16000
"""SenseVoice / sherpa-onnx 要求的采样率（Hz）。"""

MIN_DURATION_SEC = 0.15
"""低于该时长的音频直接视为无效输入，不做推理。"""

MIN_SAMPLES = int(SAMPLE_RATE * MIN_DURATION_SEC)
"""0.15 秒对应的采样点数（2400）。"""

SILENCE_PEAK_THRESHOLD = 1e-4
"""峰值低于该阈值即认为是全静音（float32 归一化到 [-1, 1]）。"""

# SenseVoice 会在文本里夹带语言 / 情感 / 事件标签，如
# ``<|zh|><|NEUTRAL|><|Speech|><|woitn|>``，需要清理掉。
SENSEVOICE_TAG_RE = re.compile(r"<\|.*?\|>")

WARMUP_SECONDS = 0.5
"""warmup 用的静音时长（秒）。"""


class ASREngine:
    """SenseVoice 离线识别引擎。

    典型用法::

        engine = ASREngine("/path/to/sense-voice-model")
        text = engine.transcribe(audio_float32_16k)
    """

    def __init__(
        self,
        model_dir: str,
        num_threads: int = 4,
        use_itn: bool = True,
        language: str = "auto",
        provider: str = "cpu",
    ) -> None:
        """加载 SenseVoice 模型并完成 warmup。

        Args:
            model_dir: 模型目录，需包含 ``model.int8.onnx`` / ``model.onnx``
                与 ``tokens.txt``。
            num_threads: CPU 推理线程数。
            use_itn: 是否开启逆文本规范化（数字、标点恢复等）。
            language: 语言提示，``"auto"`` 时自动识别中/英/粤/日/韩。
            provider: 推理后端，``"cpu"`` 或 ``"cuda"`` 等。
        """
        if sherpa_onnx is None:
            raise RuntimeError(
                "未安装 sherpa-onnx，无法使用 ASREngine。"
                f"请先 pip install sherpa-onnx（原始错误：{_SHERPA_IMPORT_ERROR}）"
            )

        self.model_dir = os.path.expanduser(str(model_dir))
        self.num_threads = int(num_threads)
        self.use_itn = bool(use_itn)
        self.language = language
        self.provider = provider

        if not os.path.isdir(self.model_dir):
            raise FileNotFoundError(f"模型目录不存在：{self.model_dir}")

        self.model_path = self._resolve_model_path()
        self.tokens_path = os.path.join(self.model_dir, "tokens.txt")
        if not os.path.isfile(self.tokens_path):
            raise FileNotFoundError(f"未找到 tokens.txt：{self.tokens_path}")

        logger.info(
            "加载 SenseVoice 模型：model=%s tokens=%s threads=%d itn=%s lang=%s provider=%s",
            os.path.basename(self.model_path),
            self.tokens_path,
            self.num_threads,
            self.use_itn,
            self.language,
            self.provider,
        )

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=self.model_path,
            tokens=self.tokens_path,
            num_threads=self.num_threads,
            use_itn=self.use_itn,
            language=self.language,
            provider=self.provider,
        )

        # 构造后立即 warmup，消除首次按键说话时的冷启动延迟。
        self.warmup()

    # ------------------------------------------------------------------ #
    # 初始化辅助
    # ------------------------------------------------------------------ #
    def _resolve_model_path(self) -> str:
        """优先返回 int8 量化模型，其次返回全精度模型。"""
        candidates = ("model.int8.onnx", "model.onnx")
        for name in candidates:
            path = os.path.join(self.model_dir, name)
            if os.path.isfile(path):
                return path
        raise FileNotFoundError(
            f"在 {self.model_dir} 下未找到 {' 或 '.join(candidates)}"
        )

    def warmup(self, repeats: int = 1) -> None:
        """用一段静音音频跑若干次解码，把冷启动开销提前支付掉。

        Args:
            repeats: 预热次数（默认 1 次即可）。
        """
        silence = np.zeros(int(SAMPLE_RATE * WARMUP_SECONDS), dtype=np.float32)
        for _ in range(max(1, int(repeats))):
            try:
                self._decode(silence)
            except Exception:  # pragma: no cover - warmup 失败不应致命
                logger.warning("warmup 解码失败（已忽略）", exc_info=True)
                break
        logger.debug("ASREngine warmup 完成")

    # ------------------------------------------------------------------ #
    # 推理
    # ------------------------------------------------------------------ #
    def _decode(self, audio: np.ndarray) -> str:
        """执行一次真实的 sherpa-onnx 解码，返回原始（未清洗）文本。"""
        stream = self.recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        self.recognizer.decode_stream(stream)
        text = getattr(stream.result, "text", "") or ""
        return text.strip()

    @staticmethod
    def _clean_text(text: str) -> str:
        """清理 SenseVoice 的语言 / 情感 / 事件标签并压缩空白。"""
        if not text:
            return ""
        text = SENSEVOICE_TAG_RE.sub("", text)
        # 清理标签后可能留下多余空白
        return text.strip()

    def transcribe(self, audio_16k_float32: np.ndarray) -> str:
        """把 16kHz 单声道 float32 音频转成文本。

        Args:
            audio_16k_float32: 采样率 16000Hz、范围约 [-1, 1] 的 float32 波形。

        Returns:
            识别出的干净文本；输入无效（过短 / 全静音）时返回空字符串。
        """
        if audio_16k_float32 is None:
            return ""

        audio = np.asarray(audio_16k_float32, dtype=np.float32).reshape(-1)

        # 1) 时长过短：直接跳过，省一次推理。
        if audio.size < MIN_SAMPLES:
            logger.debug("音频过短（%d samples），跳过识别", audio.size)
            return ""

        # 2) 非有限值兜底，避免把 NaN/inf 喂进推理后端。
        if not np.all(np.isfinite(audio)):
            audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)

        # 3) 全静音（能量极低）：直接返回空串。
        peak = float(np.max(np.abs(audio)))
        if peak < SILENCE_PEAK_THRESHOLD:
            logger.debug("音频为静音（peak=%.2e），跳过识别", peak)
            return ""

        raw = self._decode(audio)
        return self._clean_text(raw)

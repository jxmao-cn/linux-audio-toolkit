"""整段累积器（UtteranceAccumulator）——连续听写模式的**全部判定逻辑**。

为什么单独成模块
================

「按住说话」(PTT) 是"按下到松开之间的音频无条件全收"，没有判定，因此极少出错；
「连续听写」(Toggle) 必须**在线推断**"哪段是语音 / 哪段是停顿 / 什么时候该提交"，
所有"猜错就丢内容"的风险都集中在这里。所以把这块逻辑从采集/线程/日志里剥出来，
做成一个**不碰麦克风、不碰线程、不写文件**的纯逻辑模块：既能被单测穷举，
也能被人快速读完（tests/test_utterance_accumulator.py 逐条验证下面的不变量）。

不变量（Invariants，测试逐条覆盖）
=================================

I1. **连续性**：输出的每段音频都是"输入块序列的连续子序列"，逐样本相等——
    不拼接、不重排、不复制。因此不可能出现拼接缝隙或 32ms 重复。
I2. **提交时机**：只有在
    (a) 静音 >= ``commit_silence_sec``（说完了），或
    (b) 语音 >= ``max_utterance_sec`` 且此刻正处在停顿（切在自然停顿处），或
    (c) 待提交音频 >= ``hard_limit_sec``（兜底安全阀），或
    (d) 调用方主动 ``flush()``（结束连续听写）
    时才产出音频。
I3. **绝不丢内容**：起句只需"块峰值 > 固定最小门限(``min_level``)"，与 VAD 判定、
    麦克风增益、环境噪声都无关；唯一允许丢弃的情况是"高于 ``min_level`` 的内容
    少于 ``min_sounded_sec``"（一次敲击只有几十毫秒），且丢弃会计入
    ``stats.drops``，由调用方写 INFO 日志——不存在静默丢弃路径。
I4. **不重复计入**：起句时并入的预卷 ``prefix`` 由调用方保证"不含当前块"
    （采集侧在压入本块**之前**取快照），模块内不再做 identity 检查。
I5. **计数自洽**：``pending_samples == Σ(各块长度)``，
    且 speech/audible/sounded 计数都 <= ``pending_samples``（``debug_check()`` 可断言）。

命名
====

* ``sounded``：块峰值 > 固定最小门限（与"是不是语音"无关，只说明"有声音"）。
* ``audible``：块峰值 > 门限 ``max(min_level, floor_ratio * 环境底噪)``。
  环境底噪 = 最近 ``level_window_sec`` 内**最小的块峰值**（只降不升，见下）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Sequence, Tuple

import numpy as np


# --------------------------------------------------------------------------- #
# 配置与结果对象
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AccumulatorConfig:
    """整段累积参数（默认值即 config.yaml 的默认值）。"""

    sample_rate: int = 16000
    # 判定"说完了"的静音时长（秒）：说长句时的换气停顿会被合并，直到静音达到本值。
    commit_silence_sec: float = 1.6
    # 单次整段识别的最长语音时长（秒）：超过后在自然停顿处切开。
    max_utterance_sec: float = 20.0
    # VAD 的停顿粒度（秒）：VAD 段边界意味着"至少已静音这么久"，用于补齐静音计时。
    min_silence_sec: float = 0.6
    # 固定最小可听峰值门限：起句与"是否丢弃"的兜底判据（不随环境自适应）。
    min_level: float = 0.003
    # 停顿门限 = max(min_level, floor_ratio * 环境底噪)，但不超过
    # high_cap_ratio * 最近峰值包络（防止门限反过来盖住真实语音）。
    floor_ratio: float = 2.5
    high_cap_ratio: float = 0.5
    # 最近峰值包络的衰减率（每块）：用于得到"上限"参考电平，约 3~5 秒内淡出。
    recent_peak_decay: float = 0.995
    # 环境底噪的统计窗口（秒）。
    level_window_sec: float = 60.0
    # 提交时保留的尾部静音（秒）。
    tail_keep_sec: float = 0.35
    # 兜底安全上限（秒）：任何情况下不允许待提交音频无限增长。
    hard_limit_sec: float = 60.0
    # 判定"这是一个可切分的短停顿"的最小静音（秒），用于上限切分。
    cut_pause_sec: float = 0.30
    # 低于该时长的"高于最小门限的内容"视为敲击/瞬态噪声（会被丢弃并计数）。
    min_sounded_sec: float = 0.15


@dataclass
class Utterance:
    """一次提交的整段音频及其可核对度量。"""

    samples: np.ndarray
    reason: str
    speech_seconds: float = 0.0
    audible_seconds: float = 0.0
    sounded_seconds: float = 0.0
    boundaries: int = 0
    peak: float = 0.0
    level_floor: float = 0.0
    threshold: float = 0.0
    sample_rate: int = 16000

    @property
    def seconds(self) -> float:
        return self.samples.size / float(self.sample_rate)

    def describe(self, tag: str = "Toggle 整段提交") -> str:
        """日志用的一行摘要（所有量都是可独立核对的测量值）。"""
        return (
            "%s：音频 %.2fs（有声音 %.2fs / 声音内容 %.2fs / 峰值 %.4f / 停顿点 %d 个 / "
            "底噪 %.4f 停顿门限 %.4f / %s）"
            % (
                tag,
                self.seconds,
                self.audible_seconds,
                self.sounded_seconds,
                self.peak,
                self.boundaries,
                self.level_floor,
                self.threshold,
                self.reason,
            )
        )


@dataclass
class AccumulatorStats:
    """会话级统计（供"连续听写小结"核对是否丢内容）。"""

    commits: int = 0
    commit_seconds: float = 0.0
    drops: int = 0
    blocks: int = 0
    audible_blocks: int = 0
    max_peak: float = 0.0

    def describe(self, level_floor: float, threshold: float) -> str:
        ratio = 100.0 * self.audible_blocks / self.blocks if self.blocks else 0.0
        return (
            "连续听写小结：本次共提交 %d 段 / 共 %.2fs，丢弃 %d 段；"
            "采集峰值 %.4f、有声音块占比 %.0f%%、环境底噪 %.4f、停顿门限 %.4f"
            % (
                self.commits,
                self.commit_seconds,
                self.drops,
                self.max_peak,
                ratio,
                level_floor,
                threshold,
            )
        )


# --------------------------------------------------------------------------- #
# 主类
# --------------------------------------------------------------------------- #
class UtteranceAccumulator:
    """把连续音频块聚成"整段"；只有判定说完/超限/主动 flush 时才吐出音频。"""

    def __init__(self, config: Optional[AccumulatorConfig] = None) -> None:
        self.cfg = config or AccumulatorConfig()

        # 环境底噪窗口：保存最近 N 个块的峰值，取最小值作为底噪。
        # 用最小值而不是均值，是因为语音会把均值拖高；一旦门限被拖高，
        # 真实语音就会被判成静音，整段内容直接丢失（历史事故根因）。
        window_blocks = max(
            1, int(self.cfg.level_window_sec * self.cfg.sample_rate / 512)
        )
        self._level_window: Deque[float] = deque(maxlen=window_blocks)

        self.stats = AccumulatorStats()
        self.reset()

    # ------------------------------------------------------------------ #
    # 公开属性
    # ------------------------------------------------------------------ #
    @property
    def active(self) -> bool:
        """当前是否已有正在累积的整段音频。"""
        return self._active

    @property
    def level_floor(self) -> float:
        """环境底噪估计（窗口内最小块峰值）。"""
        return self._level_floor

    @property
    def threshold(self) -> float:
        """当前的停顿判定门限。

        取 max(min_level, floor_ratio * 环境底噪) 之后，再**用最近的峰值包络压一次上限**
        （``high_cap_ratio * recent_peak``）：防止"连续说话、窗口里一直没有真正安静过"
        导致底噪被语音自己顶高、门限反过来盖住语音（历史事故根因之一）。
        """
        raw = max(self.cfg.min_level, self.cfg.floor_ratio * self._level_floor)
        cap = self.cfg.high_cap_ratio * self._recent_peak
        if cap <= 0.0:
            return self.cfg.min_level
        return max(self.cfg.min_level, min(raw, cap))

    # ------------------------------------------------------------------ #
    # 主循环接口
    # ------------------------------------------------------------------ #
    def push(
        self,
        block: np.ndarray,
        vad_speech: bool = False,
        vad_boundary: bool = False,
        prefix: Optional[Sequence[np.ndarray]] = None,
    ) -> List[Utterance]:
        """喂入一块音频，返回本次产出的整段（0 或 1 个）。

        Args:
            block: 一块 16k 单声道 float32 音频（通常 32ms）。
            vad_speech: VAD 是否认为本块是语音（漏判无害，只影响静音计时）。
            vad_boundary: 本块处理时 VAD 是否产出段边界（= 刚静默了
                ``min_silence_sec``，用于把静音计时补齐到该值）。
            prefix: 起句时并入的预卷音频（调用方保证**不含** block）。
        """
        block = np.asarray(block, dtype=np.float32).reshape(-1)
        peak = float(np.max(np.abs(block))) if block.size else 0.0
        self._track_level(peak)

        seconds = block.size / float(self.cfg.sample_rate)
        audible = peak > self.threshold
        sounded = peak > self.cfg.min_level
        speech = bool(vad_speech or audible)

        # 会话统计（供小结核对）。
        self.stats.blocks += 1
        self.stats.max_peak = max(self.stats.max_peak, peak)
        if audible:
            self.stats.audible_blocks += 1

        if not self._active:
            # 起句条件：VAD 判为语音，或**仅仅有声音**（固定门限，与环境无关）。
            if not (speech or sounded):
                return []
            self._start(prefix, block, seconds)
        else:
            self._append(block, seconds)

        if audible:
            self._audible_samples += block.size
        if sounded:
            self._sounded_samples += block.size
        if speech:
            self._speech_samples += block.size
            self._silence_seconds = 0.0
        else:
            self._silence_seconds += seconds
        if vad_boundary:
            self._boundaries += 1
            # 段边界 = 至少已静音 min_silence_sec，把计时补齐（不做重复累计）。
            self._silence_seconds = max(self._silence_seconds, self.cfg.min_silence_sec)

        return self._maybe_emit(speech_just_now=speech, at_pause=bool(vad_boundary))

    def flush(self) -> Optional[Utterance]:
        """结束连续听写：把未提交的整段原样吐出（不做丢弃判定）。"""
        if not self._active or self._pending_samples <= 0:
            self.reset()
            return None
        return self._emit("结束连续听写时提交", drop_check=False)

    def reset(self) -> None:
        """清空累积状态（环境底噪窗口与会话统计保留/由调用方决定）。"""
        self._active = False
        self._chunks: List[np.ndarray] = []
        self._recent_peak = 0.0
        self._pending_samples = 0
        self._speech_samples = 0
        self._audible_samples = 0
        self._sounded_samples = 0
        self._silence_seconds = 0.0
        self._boundaries = 0

    def reset_session(self) -> None:
        """开始一次新的连续听写：清空累积状态与会话统计。"""
        self.reset()
        self.stats = AccumulatorStats()

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _track_level(self, peak: float) -> None:
        self._level_window.append(peak)
        self._level_floor = min(self._level_window) if self._level_window else 0.0
        # 峰值包络：立刻跟上更大的峰值，之后按 recent_peak_decay 缓慢淡出。
        self._recent_peak = max(peak, self._recent_peak * self.cfg.recent_peak_decay)

    def _start(
        self, prefix: Optional[Sequence[np.ndarray]], block: np.ndarray, seconds: float
    ) -> None:
        self.reset()
        self._active = True
        if prefix:
            kept = [c for c in prefix if c is not None and getattr(c, "size", 0)]
            self._chunks.extend(kept)
            self._pending_samples += sum(int(c.size) for c in kept)
        self._append(block, seconds)

    def _append(self, block: np.ndarray, seconds: float) -> None:
        self._chunks.append(block)
        self._pending_samples += block.size

    def _maybe_emit(self, speech_just_now: bool, at_pause: bool) -> List[Utterance]:
        cfg = self.cfg
        reason = None
        if self._silence_seconds >= cfg.commit_silence_sec:
            reason = "停顿 %.2fs，判定说完" % self._silence_seconds
        elif self._pending_samples >= cfg.hard_limit_sec * cfg.sample_rate:
            # 硬安全上限必须先于 max_utterance 软目标检查，否则持续语音会
            # 一直停留在 max_utterance 分支里，永远无法触发此兜底切分。
            reason = "到达兜底安全上限 %.0fs" % cfg.hard_limit_sec
        elif self._speech_samples >= cfg.max_utterance_sec * cfg.sample_rate:
            # 说得很久还没停：只在自然停顿处切，避免从词中间切断。
            if at_pause or (
                not speech_just_now and self._silence_seconds >= cfg.cut_pause_sec
            ):
                reason = "语音已达 %.1fs 上限，在停顿处切分" % (
                    self._speech_samples / float(cfg.sample_rate)
                )
        if reason is None:
            return []
        utterance = self._emit(reason)
        return [utterance] if utterance is not None else []

    def _emit(self, reason: str, drop_check: bool = True) -> Optional[Utterance]:
        chunks = self._trim_tail(self._chunks)
        utterance = Utterance(
            samples=_concat(chunks),
            reason=reason,
            speech_seconds=self._speech_samples / float(self.cfg.sample_rate),
            audible_seconds=self._audible_samples / float(self.cfg.sample_rate),
            sounded_seconds=self._sounded_samples / float(self.cfg.sample_rate),
            boundaries=self._boundaries,
            peak=float(np.max(np.abs(_concat(chunks)))) if chunks else 0.0,
            level_floor=self._level_floor,
            threshold=self.threshold,
            sample_rate=self.cfg.sample_rate,
        )
        self.reset()
        if utterance.samples.size == 0:
            return None
        # I3：只有"几乎没有声音内容"（敲击/瞬态噪声）才允许丢弃，计入 drops。
        if drop_check and utterance.sounded_seconds < self.cfg.min_sounded_sec:
            self.stats.drops += 1
            return None
        self.stats.commits += 1
        self.stats.commit_seconds += utterance.seconds
        return utterance

    def _trim_tail(self, chunks: List[np.ndarray]) -> List[np.ndarray]:
        """裁掉尾部多余静音，最多裁 ``tail_keep_sec``（有界，不会吃掉正文）。"""
        keep = int(self.cfg.tail_keep_sec * self.cfg.sample_rate)
        threshold = self.threshold
        trimmed = list(chunks)
        removed = 0
        while trimmed and removed < keep:
            tail = trimmed[-1]
            if tail.size == 0:
                trimmed.pop()
                continue
            if float(np.max(np.abs(tail))) > threshold:
                break
            if removed + tail.size > keep:
                break
            removed += tail.size
            trimmed.pop()
        return trimmed

    # ------------------------------------------------------------------ #
    # 自检（供单测 / 运行时断点使用）
    # ------------------------------------------------------------------ #
    def debug_check(self) -> Tuple[bool, str]:
        """校验 I5（计数自洽），返回 ``(是否通过, 说明)``。"""
        total = sum(int(c.size) for c in self._chunks)
        if total != self._pending_samples:
            return False, "pending_samples=%d 与块总长=%d 不一致" % (
                self._pending_samples,
                total,
            )
        for name, value in (
            ("speech", self._speech_samples),
            ("audible", self._audible_samples),
            ("sounded", self._sounded_samples),
        ):
            if value > self._pending_samples:
                return False, "%s_samples=%d 超过 pending_samples=%d" % (
                    name,
                    value,
                    self._pending_samples,
                )
        return True, "ok"


def _concat(chunks: List[np.ndarray]) -> np.ndarray:
    chunks = [c for c in chunks if c is not None and getattr(c, "size", 0) > 0]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    if len(chunks) == 1:
        return np.asarray(chunks[0], dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32, copy=False)

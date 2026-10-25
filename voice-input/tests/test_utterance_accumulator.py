#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UtteranceAccumulator 单元测试（纯逻辑，不需要麦克风 / 模型 / 网络）。

直接运行（约 1 秒）::

    python tests/test_utterance_accumulator.py

每个用例对应模块文档里的一条不变量，命名即断言内容：

    [I1] 连续性        —— 输出必须是输入块的连续子序列（逐样本相等）
    [I2] 提交时机      —— 只有说完 / 超限且停顿 / flush 才产出
    [I3] 绝不丢内容    —— 轻声 + VAD 失灵也要整段收全；敲击丢弃要计数
    [I4] 预卷不重复    —— 起句并入的 prefix 恰好出现一次
    [I5] 计数自洽      —— debug_check() 始终成立

这些用例都是"事故回归"：I3 的两条分别对应历史上真实发生过的两次丢内容事故。
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input.utterance_accumulator import (  # noqa: E402
    AccumulatorConfig,
    UtteranceAccumulator,
)

SR = 16000
BLOCK = 512


# --------------------------------------------------------------------------- #
# 测试小工具
# --------------------------------------------------------------------------- #
def tone(seconds: float, peak: float = 0.2, freq: float = 180.0) -> np.ndarray:
    """合成一段"像语音"的正弦（峰值可调，用于模拟音量大小）。"""
    n = int(seconds * SR)
    t = np.arange(n, dtype=np.float32) / SR
    return (peak * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def feed(
    acc: UtteranceAccumulator,
    audio: np.ndarray,
    vad_speech=None,
    vad_boundary=None,
    prefix=None,
    vad_flag_at: float = 1e9,
) -> list:
    """按 512 样本一块喂入；返回所有产出的 Utterance。

    ``vad_flag_at``：模拟"VAD 从第 N 秒起才判定为语音"（VAD 不灵敏时成立）。
    """
    out = []
    for i in range(0, audio.size, BLOCK):
        blk = audio[i : i + BLOCK]
        t = i / SR
        speech = (t >= vad_flag_at) if vad_speech is None else bool(vad_speech)
        out.extend(acc.push(blk, vad_speech=speech, vad_boundary=bool(vad_boundary), prefix=prefix))
    return out


def locate(stream: np.ndarray, chunk: np.ndarray):
    """在 stream 中定位 chunk（块对齐搜索），返回偏移；找不到返回 None。"""
    for off in range(0, stream.size - chunk.size + 1, BLOCK):
        if np.array_equal(stream[off : off + chunk.size], chunk):
            return off
    return None


def assert_contiguous(stream: np.ndarray, utterances, name: str) -> None:
    """[I1] 每段输出都必须是输入流的连续子序列。"""
    for k, utt in enumerate(utterances):
        off = locate(stream, utt.samples)
        assert off is not None, f"{name}: 第 {k} 段音频不是输入流的连续切片"
        assert np.array_equal(stream[off : off + utt.samples.size], utt.samples), (
            f"{name}: 第 {k} 段音频与输入流逐样本不一致"
        )


# --------------------------------------------------------------------------- #
# 用例
# --------------------------------------------------------------------------- #
def test_I2_short_pauses_merge() -> str:
    """[I2] 句内短停顿（< commit_silence）合并成一段。"""
    acc = UtteranceAccumulator(AccumulatorConfig())
    for gap in (0.4, 0.8, 1.2, 1.5):
        stream = np.concatenate([tone(1.5), silence(gap), tone(1.5), silence(3.0)])
        utils = feed(acc, stream)
        assert len(utils) == 1, f"停顿 {gap}s 应合并为 1 段，实际 {len(utils)} 段"
        assert_contiguous(stream, utils, f"gap={gap}")
        assert utils[0].seconds > 2.9, f"合并后的整段太短：{utils[0].seconds:.2f}s"
    return "停顿 0.4/0.8/1.2/1.5s -> 均为 1 段"


def test_I2_long_pauses_split() -> str:
    """[I2] 长停顿（>= commit_silence）在停顿处切开。"""
    acc = UtteranceAccumulator(AccumulatorConfig())
    for gap in (1.8, 2.5):
        stream = np.concatenate([tone(1.5), silence(gap), tone(1.5), silence(3.0)])
        utils = feed(acc, stream)
        assert len(utils) == 2, f"停顿 {gap}s 应切为 2 段，实际 {len(utils)} 段"
        assert_contiguous(stream, utils, f"gap={gap}")
    return "停顿 1.8/2.5s -> 均为 2 段"


def test_I3_quiet_voice_with_dead_vad_still_captured() -> str:
    """[I3 事故回归一] 轻声（峰值 0.004）+ VAD 完全失灵，也必须整段收全。"""
    acc = UtteranceAccumulator(AccumulatorConfig())
    stream = np.concatenate([tone(1.5, 0.004), silence(1.2), tone(1.5, 0.004), silence(3.0)])
    utils = feed(acc, stream, vad_speech=False, vad_boundary=False)
    assert len(utils) == 1, f"应整段收全（1 段），实际 {len(utils)} 段"
    assert utils[0].seconds > 2.9, f"丢失内容：整段只有 {utils[0].seconds:.2f}s"
    assert_contiguous(stream, utils, "轻声+VAD失灵")
    return f"峰值 0.004 + VAD 恒 False -> 1 段（{utils[0].seconds:.2f}s）"


def test_I3_floor_not_dragged_by_speech() -> str:
    """[I3 事故回归一的核心] 连续说话本身绝不能把"停顿门限"顶到语音之上。

    历史事故：底噪用均值/自适应更新，VAD 漏判时语音被当成噪声去更新底噪，
    门限升到语音电平的 3 倍 → 语音被判成静音 → 整段内容收不进来。
    这里断言"停顿时门限必须低于说话电平"，并要求随后的停顿能正常提交。
    """
    acc = UtteranceAccumulator(AccumulatorConfig())
    speech_level = 0.02
    for _ in range(3):  # 连续说 15 秒且 VAD 全失灵
        feed(acc, tone(5.0, speech_level), vad_speech=False, vad_boundary=False)
    assert acc.threshold < speech_level, (
        f"门限被语音拖高到 {acc.threshold:.5f}（>= 说话电平 {speech_level}），"
        "语音会被误判为静音"
    )
    floor_ok, detail = acc.debug_check()
    assert floor_ok, detail
    # 随后的长停顿必须能触发提交（且把整段收全）
    utils = feed(acc, silence(3.0), vad_speech=False, vad_boundary=False)
    assert len(utils) == 1, f"长停顿后应提交 1 段，实际 {len(utils)} 段"
    assert utils[0].seconds > 14.0, f"整段内容被丢：只有 {utils[0].seconds:.2f}s"
    return f"连说 15s 后门限 {acc.threshold:.4f} < 说话电平 {speech_level}"


def test_I3_click_is_dropped_and_counted() -> str:
    """[I3] 单击/瞬态噪声被丢弃，但必须计入 drops（供调用方写日志）。"""
    acc = UtteranceAccumulator(AccumulatorConfig())
    click = silence(3.0)
    click[int(1.0 * SR) : int(1.03 * SR)] = tone(0.03, 0.3)
    utils = feed(acc, click)
    assert utils == [], "单击不应产出整段"
    assert acc.stats.drops == 1, f"丢弃应计数，实际 drops={acc.stats.drops}"
    assert acc.stats.commits == 0
    return "30ms 单击 -> 0 段 + drops=1"


def test_I3_short_real_word_is_kept() -> str:
    """[I3] 真实的短词（0.25s）不能被当成敲击丢掉。"""
    acc = UtteranceAccumulator(AccumulatorConfig())
    stream = silence(1.0)
    stream = np.concatenate([stream, tone(0.25, 0.05), silence(2.0)])
    utils = feed(acc, stream)
    assert len(utils) == 1, f"0.25s 真实短词应保留，实际 {len(utils)} 段"
    return f"0.25s 短词 -> 1 段（{utils[0].seconds:.2f}s）"


def test_I4_prefix_included_once() -> str:
    """[I4] 起句并入的预卷 prefix 恰好出现一次，且在音频最前面。"""
    acc = UtteranceAccumulator(AccumulatorConfig())
    prefix = [tone(0.16, 0.05), tone(0.16, 0.05)]  # 模拟 250ms 预卷
    body = np.concatenate([tone(1.0), silence(2.5)])
    utils = feed(acc, body, prefix=prefix)
    assert len(utils) == 1
    head = np.concatenate(prefix)
    assert utils[0].samples.size >= head.size + int(1.0 * SR) - BLOCK
    assert np.array_equal(utils[0].samples[: head.size], head), "预卷未出现在最前面"
    # 预卷只在最前面出现一次：其后的音频应等于 body 的前缀
    assert_contiguous(np.concatenate([head, body]), utils, "prefix")
    return f"预卷 {head.size / SR:.2f}s 恰好并入一次"


def test_I2_max_utterance_cut_at_pause() -> str:
    """[I2] 语音超过 max_utterance 后，在**紧随其后的自然停顿**处切分（不切在词中间）。"""
    cfg = AccumulatorConfig(max_utterance_sec=5.0)
    acc = UtteranceAccumulator(cfg)
    # 先说 5.5 秒（已超过 5s 上限），再 0.5s 短停顿（不够 commit，但够 cut_pause），再说 2 秒
    stream = np.concatenate([tone(5.5), silence(0.5), tone(2.0), silence(3.0)])
    utils = feed(acc, stream)
    assert len(utils) == 2, f"应切为 2 段，实际 {len(utils)} 段"
    assert "上限" in utils[0].reason, f"第一段的切分原因不对：{utils[0].reason}"
    assert utils[0].seconds < 7.0, f"第一段跑飞了：{utils[0].seconds:.2f}s"
    assert_contiguous(stream, utils, "max_utterance")
    return (
        f"5.5s 连说（5s 上限）-> 在 0.5s 停顿处切分"
        f"（{utils[0].seconds:.2f}s / {utils[1].seconds:.2f}s）"
    )


def test_I2_hard_limit_cuts_unbroken_speech_without_gaps() -> str:
    """[I2] 连续语音没有自然停顿时，硬安全上限仍会切分且不丢不重。"""
    cfg = AccumulatorConfig(max_utterance_sec=2.0, hard_limit_sec=4.0)
    acc = UtteranceAccumulator(cfg)
    stream = tone(9.0, 0.05)
    emitted = feed(acc, stream)
    tail = acc.flush()
    assert tail is not None, "硬上限切分后仍应有尾段等待 flush"
    utterances = [*emitted, tail]
    assert len(emitted) == 2, f"4s 硬上限应在 9s 连续语音中触发两次，实际 {len(emitted)} 次"
    for index, utt in enumerate(emitted):
        assert "安全上限" in utt.reason, f"第 {index} 段未由硬安全上限触发：{utt.reason}"
        assert utt.samples.size == 4 * SR, (
            f"第 {index} 段应在 4s 阈值处切分，实际 {utt.seconds:.3f}s"
        )
    assert_contiguous(stream, utterances, "hard_limit")
    reconstructed = np.concatenate([utt.samples for utt in utterances])
    assert np.array_equal(reconstructed, stream), "硬上限切分后出现音频缺口或重复"
    return "9s 无停顿语音 -> 4s + 4s + 1s；连续样本无缺口、无重复"


def test_I2_pure_silence_and_flush() -> str:
    """[I2] 纯静音不产出；flush 返回未提交的整段。"""
    acc = UtteranceAccumulator(AccumulatorConfig())
    assert feed(acc, silence(5.0)) == [], "纯静音不应产出"
    assert acc.flush() is None, "无累积时 flush 应返回 None"

    utils = feed(acc, np.concatenate([tone(1.2), silence(0.2)]))
    assert utils == [], "1.4s 内容不该在 flush 前提交"
    tail = acc.flush()
    assert tail is not None and tail.seconds > 1.0, "flush 应返回未提交的整段"
    return f"静音 0 段；flush 取回 {tail.seconds:.2f}s"


def test_I5_debug_check() -> str:
    """[I5] 累积过程中计数始终自洽。"""
    acc = UtteranceAccumulator(AccumulatorConfig())
    for _ in range(50):
        acc.push(tone(0.032, 0.05), vad_speech=True, vad_boundary=False)
        ok, detail = acc.debug_check()
        assert ok, detail
    ok, detail = acc.debug_check()
    assert ok and acc.active, detail
    return "50 块累积期间 debug_check() 全部通过"


# --------------------------------------------------------------------------- #
# 运行器
# --------------------------------------------------------------------------- #
CASES = [
    test_I2_short_pauses_merge,
    test_I2_long_pauses_split,
    test_I2_max_utterance_cut_at_pause,
    test_I2_hard_limit_cuts_unbroken_speech_without_gaps,
    test_I2_pure_silence_and_flush,
    test_I3_quiet_voice_with_dead_vad_still_captured,
    test_I3_floor_not_dragged_by_speech,
    test_I3_click_is_dropped_and_counted,
    test_I3_short_real_word_is_kept,
    test_I4_prefix_included_once,
    test_I5_debug_check,
]


def main() -> int:
    print("UtteranceAccumulator 单元测试（不依赖麦克风 / 模型 / 网络）")
    print("-" * 72)
    failed = 0
    for case in CASES:
        name = case.__name__.replace("test_", "")
        try:
            detail = case()
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {name}\n       {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[ERROR] {name}\n        {type(exc).__name__}: {exc}")
        else:
            print(f"[PASS] {name} — {detail}")
    print("-" * 72)
    print(f"共 {len(CASES)} 项：{len(CASES) - failed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

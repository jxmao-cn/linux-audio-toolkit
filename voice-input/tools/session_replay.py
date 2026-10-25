#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""会话重放：把录下来的原始音频按"整段累积"逻辑重放一遍，逐段打印判定结果。

用途（把"AI 说修好了"变成你自己能跑的实验）
==========================================

1. 打开落盘（`config.yaml` 里 `audio.session_dump_dir: "sessions"`）→ 重启服务 →
   正常用一次连续听写，`sessions/` 下会多出一个 wav；
2. 重放：`python tools/session_replay.py sessions/session-*.wav`；
3. 看输出：每段音频的时长、提交原因、底噪/门限、以及**无损校验**（每段音频必须
   是原始音频的连续切片，逐样本相等）。

可选参数与 config.yaml 对应，便于对照调参::

    --commit 1.6        # commit_silence_duration
    --max-utterance 20  # max_utterance_seconds
    --no-vad            # 不用 Silero VAD（模拟 VAD 失灵）
"""

from __future__ import annotations

import argparse
import os
import sys
import wave

import numpy as np

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

from voice_input.utterance_accumulator import (  # noqa: E402
    AccumulatorConfig,
    UtteranceAccumulator,
)

BLOCK = 512


def read_wav_16k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        ch = wf.getnchannels()
        width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if width != 2:
        raise SystemExit(f"只支持 16bit wav，实际 sampwidth={width}")
    audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1)
    if rate != 16000:  # 落盘的都是 16k，这里只是防御
        raise SystemExit(f"只支持 16kHz wav，实际 {rate}Hz")
    return audio


def make_vad(model_path: str, cfg: AccumulatorConfig):
    """可选：加载 Silero VAD 作为"语音/停顿"传感器。"""
    try:
        import sherpa_onnx

        vad_config = sherpa_onnx.VadModelConfig()
        silero = vad_config.silero_vad
        silero.model = model_path
        silero.threshold = 0.45
        silero.min_silence_duration = cfg.min_silence_sec
        silero.min_speech_duration = 0.2
        silero.max_speech_duration = max(30.0, cfg.max_utterance_sec + 10.0)
        silero.window_size = BLOCK
        vad_config.sample_rate = cfg.sample_rate
        vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)
        return vad
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 无法加载 VAD（{exc}），改用纯峰值判定")
        return None


def locate(stream: np.ndarray, chunk: np.ndarray):
    for off in range(0, stream.size - chunk.size + 1, BLOCK):
        if np.array_equal(stream[off : off + chunk.size], chunk):
            return off
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="重放一段会话音频，打印整段累积的判定过程")
    ap.add_argument("wav", help="16kHz 单声道 wav（由 audio.session_dump_dir 落盘）")
    ap.add_argument("--commit", type=float, default=1.6, help="commit_silence_duration")
    ap.add_argument("--max-utterance", type=float, default=20.0, help="max_utterance_seconds")
    ap.add_argument("--min-silence", type=float, default=0.6, help="min_silence_duration")
    ap.add_argument("--min-level", type=float, default=0.003, help="固定最小可听峰值")
    ap.add_argument("--no-vad", action="store_true", help="不使用 Silero VAD")
    ap.add_argument(
        "--vad-model",
        default=os.path.join(PROJECT_DIR, "models", "silero_vad.onnx"),
        help="Silero VAD 模型路径",
    )
    args = ap.parse_args()

    audio = read_wav_16k(args.wav)
    cfg = AccumulatorConfig(
        commit_silence_sec=args.commit,
        max_utterance_sec=args.max_utterance,
        min_silence_sec=args.min_silence,
        min_level=args.min_level,
    )
    acc = UtteranceAccumulator(cfg)
    vad = None if args.no_vad else make_vad(args.vad_model, cfg)

    print(f"输入：{args.wav}（{audio.size / cfg.sample_rate:.2f}s，峰值 {float(np.abs(audio).max()):.4f}）")
    print(f"参数：commit={cfg.commit_silence_sec}s max={cfg.max_utterance_sec}s "
          f"VAD={'关闭' if vad is None else '开启'}")

    utterances = []
    for i in range(0, audio.size, BLOCK):
        block = audio[i : i + BLOCK]
        vad_speech = vad_boundary = False
        if vad is not None:
            vad.accept_waveform(np.ascontiguousarray(block, dtype=np.float32))
            flag = getattr(vad, "is_speech_detected", None)
            vad_speech = bool(flag()) if callable(flag) else False
            while not vad.empty():
                vad.pop()
                vad_boundary = True
        utterances.extend(acc.push(block, vad_speech=vad_speech, vad_boundary=vad_boundary))

    tail = acc.flush()
    if tail is not None:
        tail.reason = "结束重放时提交"
        utterances.append(tail)

    ok = True
    print("-" * 72)
    for k, utt in enumerate(utterances):
        off = locate(audio, utt.samples)
        contiguous = off is not None and np.array_equal(
            audio[off : off + utt.samples.size], utt.samples
        )
        ok = ok and contiguous
        print(f"[{k}] {utt.describe()}")
        print(
            f"    无损校验：起点 {off / cfg.sample_rate if off is not None else '?'}s，"
            f"与原始音频逐样本一致 = {contiguous}"
        )
    stats = acc.stats
    print("-" * 72)
    print(stats.describe(acc.level_floor, acc.threshold))
    print("无损校验：" + ("全部通过（每段都是原始音频的连续切片）" if ok else "失败！"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

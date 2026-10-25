# 审阅指南（REVIEW.md）

给"不想读一大坨 AI 生成代码"的人用：**只需要看 1 个文件 + 跑 2 条命令**。
本文件本身也只有一屏，其余细节都在代码注释里。

## 1. 一句话结论

连续听写的复杂度只来自一件事：**必须从音频里"猜"出哪里是停顿、什么时候算说完**。
所以我把它全部收进一个纯逻辑模块，其余代码只负责"搬音频"。

```
音频块 ──► AudioCapture（采集/线程/设备，哑管道）
              │  block + VAD 观测(是否语音/是否停顿边界) + 预卷前缀
              ▼
        UtteranceAccumulator（唯一有判定的地方，纯逻辑，可单测）
              │  产出的"整段音频"
              ▼
        ASR → LLM 纠错 → 上屏（未改动）
```

## 2. 需要人工看的文件（按优先级）

| 文件 | 行数 | 看什么 |
|------|------|--------|
| `voice_input/utterance_accumulator.py` | ~400 | **唯一的判定逻辑**。看开头的 5 条不变量 I1–I5，再扫一遍 `push()/flush()/threshold`。 |
| `tests/test_utterance_accumulator.py` | ~270 | 每条不变量对应一个用例，命名即断言内容；用例全是历史事故回归。 |
| `voice_input/audio_capture.py` | 只看 5 处 | `_on_block`（取预卷前缀的顺序）、`_vad_observe_locked`（VAD 只当传感器）、`_accumulate_locked`、`_check_stream_health_locked`（采集看门狗）、`stop_recording`（flush + 小结 + 落盘）。 |

## 3. 五条不变量（审阅时只需逐条确认"代码 + 用例都覆盖了"）

| 编号 | 不变量 | 代码位置 | 用例 |
|------|--------|----------|------|
| I1 | 输出的每段音频都是输入块的**连续子序列**（逐样本相等，不拼接/不重复） | `_chunks` 只 append、`_concat` 只拼接 | `test_I2_short_pauses_merge` / `test_I4_prefix_included_once` 里的 `assert_contiguous` |
| I2 | 只有"静音≥commit / 语音超限且正处在停顿 / 兜底上限 / flush"才产出 | `_maybe_emit` | `test_I2_*` 四个用例 |
| I3 | **绝不丢内容**：起句只要求"有声音"（固定门限），唯一允许丢弃的是"高于门限的内容<0.15s"（敲击），且计入 `stats.drops` 由调用方写 INFO 日志 | `push` 起句分支 / `_emit(drop_check)` | `test_I3_quiet_voice_with_dead_vad_still_captured`、`test_I3_floor_not_dragged_by_speech`、`test_I3_click_is_dropped_and_counted`、`test_I3_short_real_word_is_kept` |
| I4 | 起句并入的预卷前缀恰好出现一次（快照在压入当前块**之前**取） | `_on_block` 里 `_ring_snapshot_locked()` 的时机 | `test_I4_prefix_included_once` |
| I5 | 计数自洽（`pending_samples == Σ块长`） | `debug_check()` | `test_I5_debug_check` |

## 4. 你可以自己跑的两条命令

```bash
cd voice-input

./run.sh test-unit    # 约 1 秒：纯逻辑单测（不需要麦克风 / 模型 / 网络）
./run.sh test         # 约 10 秒：端到端自检（含麦克风采集、真实 ASR、注入链路）
```

`test-unit` 里的用例是**历史事故回归**，也就是说这些 bug 一旦复发会立刻红：

- `I3_quiet_voice_with_dead_vad_still_captured`：轻声（峰值 0.004）+ VAD 完全失灵 → 必须整段收全
  （对应事故一：底噪估计被语音拖高 → 门限关闭 → 16 秒语音只剩最后 1.66 秒）
- `I3_floor_not_dragged_by_speech`：连续说 15 秒后，门限必须仍低于说话电平
- `I3_click_is_dropped_and_counted`：敲击噪声可以丢，但必须**计数（可被日志看见）**
  （对应事故二：静默丢弃路径只写 DEBUG 日志）

## 5. 用你自己的真实会话验收（不依赖我的测试）

```bash
# config.yaml: audio.session_dump_dir: "sessions"  然后 ./run.sh restart
# 正常说一段长语音，结束后：
python tools/session_replay.py sessions/session-*.wav
```

重放会逐段打印：时长、提交原因、底噪/停顿门限，并做**无损校验**
（每段音频必须是原始录音的连续切片，逐样本相等）。这样"某段没识别好"就变成
可复现、可审阅的实验，而不是靠我说"我测过了"。

## 6. 仍未覆盖的部分（如实列出）

- `audio_capture.py` 的设备/线程/后端重连代码**没有**单元测试，只有端到端自检与运行期看门狗；
- VAD 模型本身（Silero ONNX）是外部依赖，行为只在自检与单测的合成信号上抽样验证过；
- 真实声学环境（房间混响、远场、多人说话）无法在单测里覆盖，需要靠第 5 节的真实会话重放。

"""voice-input: 本地离线语音输入。

子模块：

* :mod:`voice_input.utterance_accumulator` —— 连续听写"整段累积"判定逻辑
  （纯逻辑，无麦克风/线程依赖，可单测；不变量与用例见 REVIEW.md）。
* :mod:`voice_input.audio_capture` —— USB 麦克风采集（16 kHz 单声道 float32）、
  VAD 传感器与采集链路看门狗。
* 以及配套的 ASR / 热键 / 文本注入等模块（若存在则一并导出）。

本包聚合导出各子模块的公共符号，任一子模块导入失败都不会影响其他子模块
（例如缺少可选依赖 ``sounddevice`` / ``sherpa_onnx`` 时）。
"""

# 核心音频采集接口（本模块必备）。
from .audio_capture import AudioCapture, find_pulse_source
from .utterance_accumulator import AccumulatorConfig, Utterance, UtteranceAccumulator

__all__ = [
    "AudioCapture",
    "find_pulse_source",
    "AccumulatorConfig",
    "Utterance",
    "UtteranceAccumulator",
]
__version__ = "0.1.0"


def _try_export(module_name: str, names: list) -> None:
    """尽力从子模块导出符号；子模块缺失/依赖不全时静默跳过。"""
    try:
        module = __import__(f"{__name__}.{module_name}", fromlist=names)
    except Exception:  # noqa: BLE001 - 聚合导入不应影响核心功能
        return
    for name in names:
        if hasattr(module, name):
            globals()[name] = getattr(module, name)
            if name not in __all__:
                __all__.append(name)


# 可选子模块（由其他开发者并行维护），存在则一并导出。
_try_export("asr_engine", ["ASREngine"])
_try_export("post_processor", ["PostProcessor"])
_try_export("text_injector", ["TextInjector", "inject_text"])
_try_export("hotkey_daemon", ["HotkeyDaemon", "State"])

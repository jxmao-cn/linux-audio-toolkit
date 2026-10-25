#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HotkeyDaemon 双键独立状态机单元测试。

无头可跑：直接驱动 ``_on_press`` / ``_on_release``，全程不创建
``pynput.Listener``（也就不需要 X11 连接）。

CI/无 DISPLAY 环境下 ``from pynput import keyboard`` 会导入失败，
此时用最小 fake 后端（Key 哨兵 + KeyCode.from_char）替代；有 X11 的
本机则直接用真实 pynput 键对象。状态机逻辑本身始终是被测的真实代码。
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_input.hotkey_daemon as hd
from voice_input.hotkey_daemon import HotkeyDaemon, State


# --------------------------------------------------------------------------- #
# fake keyboard 后端（仅无 pynput 时启用）
# --------------------------------------------------------------------------- #
class _FakeKey:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"Key.{self.name}"


class _FakeKeys:
    pass


_FAKE_KEY_NAMES = (
    "shift",
    "shift_l",
    "shift_r",
    "ctrl",
    "ctrl_l",
    "ctrl_r",
    "alt",
    "alt_l",
    "alt_r",
    "cmd",
    "cmd_l",
    "cmd_r",
    "f9",
    "enter",
    "esc",
)


class _FakeKeyCode:
    __slots__ = ("char",)

    def __init__(self, char):
        self.char = char

    @classmethod
    def from_char(cls, char):
        return cls(char)

    def __eq__(self, other):
        return isinstance(other, _FakeKeyCode) and other.char == self.char

    def __hash__(self):
        return hash(self.char)

    def __repr__(self):
        return f"KeyCode({self.char!r})"


def _load_keys():
    """返回 (Key 命名空间, KeyCode, 是否真实 pynput)。"""
    try:
        from pynput.keyboard import Key as RealKey, KeyCode as RealKeyCode

        # 真实后端可用性以“能解析出全部目标键”为准。
        tmp = hd._resolve_key_spec("shift_r,f9")
        if tmp:
            return RealKey, RealKeyCode, True
    except Exception:  # noqa: BLE001 - 无头环境的预期路径
        pass
    fake_keys = _FakeKeys()
    for _n in _FAKE_KEY_NAMES:
        setattr(fake_keys, _n, _FakeKey(_n))
    return fake_keys, _FakeKeyCode, False


FAKE_KEYS, FAKE_KEYCODE, HAS_REAL = _load_keys()

CTRL_R = getattr(FAKE_KEYS, "ctrl_r")
SHIFT_R = getattr(FAKE_KEYS, "shift_r")
F9 = getattr(FAKE_KEYS, "f9")


def char_key(ch):
    return FAKE_KEYCODE.from_char(ch)


class HotkeyTestBase(unittest.TestCase):
    threshold = 0.05

    def setUp(self):
        # 无头时把 fake 后端注入被测模块（有 X11 时则是同一套真实对象，无影响）。
        if not HAS_REAL:
            self._patcher = mock.patch.object(hd, "keyboard", mock.Mock())
            fake_mod = self._patcher.start()
            fake_mod.Key = FAKE_KEYS
            fake_mod.KeyCode = FAKE_KEYCODE
            self.addCleanup(self._patcher.stop)
        self.calls = []
        self.daemon = HotkeyDaemon(
            ptt_key="shift_r,f9",
            toggle_key="ctrl_r",
            hold_threshold_sec=self.threshold,
            on_ptt_start=lambda: self.calls.append("ptt_start"),
            on_ptt_stop=lambda: self.calls.append("ptt_stop"),
            on_toggle_start=lambda: self.calls.append("toggle_start"),
            on_toggle_stop=lambda: self.calls.append("toggle_stop"),
        )
        # 键集合非空是状态机可测的前提（真/伪后端都要满足）。
        self.assertTrue(self.daemon._ptt_keys, "PTT 键集解析为空")
        self.assertTrue(self.daemon._toggle_keys, "Toggle 键集解析为空")

    def tearDown(self):
        self.daemon.stop()


class TestToggleKey(HotkeyTestBase):
    def test_toggle_click_starts_and_stops(self):
        self.daemon._on_press(CTRL_R)
        self.daemon._on_release(CTRL_R)
        self.assertEqual(self.daemon.state, State.TOGGLE_RECORDING)
        self.assertEqual(self.calls, ["toggle_start"])

        self.daemon._on_press(CTRL_R)
        self.daemon._on_release(CTRL_R)
        self.assertEqual(self.daemon.state, State.IDLE)
        self.assertEqual(self.calls, ["toggle_start", "toggle_stop"])

    def test_toggle_combo_with_other_key_aborted(self):
        # 按住 ctrl_r 期间按下其它键 -> 松开时不触发。
        self.daemon._on_press(CTRL_R)
        self.daemon._on_press(char_key("c"))
        self.daemon._on_release(char_key("c"))
        self.daemon._on_release(CTRL_R)
        self.assertEqual(self.daemon.state, State.IDLE)
        self.assertEqual(self.calls, [])

    def test_toggle_autorepeat_fires_once(self):
        # X11 长按自动连发：重复 on_press 不重复触发。
        self.daemon._on_press(CTRL_R)
        self.daemon._on_press(CTRL_R)
        self.daemon._on_press(CTRL_R)
        self.daemon._on_release(CTRL_R)
        self.assertEqual(self.calls, ["toggle_start"])
        self.assertEqual(self.daemon.state, State.TOGGLE_RECORDING)

    def test_toggle_abort_does_not_poison_next_click(self):
        self.daemon._on_press(CTRL_R)
        self.daemon._on_press(char_key("c"))
        self.daemon._on_release(char_key("c"))
        self.daemon._on_release(CTRL_R)
        self.assertEqual(self.calls, [])
        # 下一次干净单击仍可正常进入。
        self.daemon._on_press(CTRL_R)
        self.daemon._on_release(CTRL_R)
        self.assertEqual(self.calls, ["toggle_start"])


class TestPttKey(HotkeyTestBase):
    def test_ptt_hold_triggers_start_stop(self):
        self.daemon._on_press(SHIFT_R)
        time.sleep(self.threshold + 0.10)
        self.assertEqual(self.calls, ["ptt_start"])
        self.assertEqual(self.daemon.state, State.PTT_RECORDING)
        self.daemon._on_release(SHIFT_R)
        self.assertEqual(self.calls, ["ptt_start", "ptt_stop"])
        self.assertEqual(self.daemon.state, State.IDLE)

    def test_ptt_short_press_ignored(self):
        # 短按（小于阈值，如点按右 Shift 切输入法）绝不触发。
        self.daemon._on_press(SHIFT_R)
        self.daemon._on_release(SHIFT_R)
        time.sleep(self.threshold + 0.15)  # 等待：确认定时器不会迟发
        self.assertEqual(self.calls, [])
        self.assertEqual(self.daemon.state, State.IDLE)

    def test_ptt_with_letter_key_ignored(self):
        # 按住期间敲字母键（打大写字母）100% 不触发 on_ptt_start。
        self.daemon._on_press(SHIFT_R)
        self.daemon._on_press(char_key("A"))
        self.daemon._on_release(char_key("A"))
        time.sleep(self.threshold + 0.15)
        self.assertEqual(self.calls, [])
        self.daemon._on_release(SHIFT_R)
        time.sleep(0.05)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.daemon.state, State.IDLE)

    def test_ptt_autorepeat_fires_once(self):
        self.daemon._on_press(SHIFT_R)
        self.daemon._on_press(SHIFT_R)  # X11 自动连发
        time.sleep(self.threshold + 0.10)
        self.assertEqual(self.calls, ["ptt_start"])
        self.daemon._on_release(SHIFT_R)
        self.assertEqual(self.calls, ["ptt_start", "ptt_stop"])

    def test_ptt_f9_also_works(self):
        self.daemon._on_press(F9)
        time.sleep(self.threshold + 0.10)
        self.assertEqual(self.calls, ["ptt_start"])
        self.daemon._on_release(F9)
        self.assertEqual(self.calls, ["ptt_start", "ptt_stop"])


if __name__ == "__main__":
    unittest.main()

"""全局热键守护进程 (Hotkey Daemon)。

监听全局键盘事件，实现**双键独立双模**：

    * **按住说话 (PTT)**：默认 ``右 Shift`` / ``F9``（``ptt_key="shift_r,f9"``）。
      按下后若**按住满 hold_threshold_sec (0.20s) 且期间未按其它键**，
      进入 PTT_RECORDING 并调用 ``on_ptt_start``；松开立即 ``on_ptt_stop``。
      因此短按右 Shift 切输入法、或右 Shift + 字母输入大写，都 100% 不会误触发。
    * **连续听写 (Toggle)**：默认 ``右 Ctrl``（``toggle_key="ctrl_r"``）。
      在 IDLE 时按下并**在松开前未按其它键**，松开即进入 TOGGLE_RECORDING
      并调用 ``on_toggle_start``；再次按下并松开则回到 IDLE 并 ``on_toggle_stop``。

防误触保护：
    1. **组合键豁免**：按下目标键后、松开之前若敲了任何其它键
       （例如 Right-Ctrl + C / Right-Shift + A），立即判定为普通组合键，
       取消本次语音触发。
    2. **按住阈值**：PTT 必须真正按住 ``hold_threshold_sec`` 秒才触发。
    3. **自动连发过滤**：X11 长按会产生连续的 on_press(key)，用按下集合吃掉重复事件。

状态机：

        IDLE ──按住 ptt_key >= 0.20s──▶ PTT_RECORDING ──松开 ptt_key──▶ IDLE
          │                              (on_ptt_start)      (on_ptt_stop)
          │
          └──按下并松开 toggle_key(无其它键)──▶ TOGGLE_RECORDING
                (on_toggle_start)                    │
                             再按+松开 toggle_key ────┘
                                  (on_toggle_stop) ──▶ IDLE

任意待判定阶段出现"其它按键" -> combo_aborted，丢弃本次触发。
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Callable, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

try:  # pynput 为运行期依赖
    from pynput import keyboard  # type: ignore
except Exception as _exc:  # pragma: no cover - 取决于环境
    keyboard = None  # type: ignore
    _IMPORT_ERROR = _exc
else:
    _IMPORT_ERROR = None


class State(str, Enum):
    IDLE = "IDLE"
    PTT_RECORDING = "PTT_RECORDING"
    TOGGLE_RECORDING = "TOGGLE_RECORDING"


# 名称别名：把常见写法归一化到 pynput.Key 的成员名。
_KEY_ALIASES = {
    "control": "ctrl",
    "control_l": "ctrl_l",
    "control_r": "ctrl_r",
    "escape": "esc",
    "return": "enter",
    "win": "cmd",
    "win_l": "cmd_l",
    "win_r": "cmd_r",
    "super": "cmd",
    "super_l": "cmd_l",
    "super_r": "cmd_r",
    "command": "cmd",
}

# 把一个"通用修饰键"名称展开为左右具体键，例如 "shift" -> 左右 Shift 均可。
_MODIFIER_GROUPS = {
    "shift": ("shift", "shift_l", "shift_r"),
    "ctrl": ("ctrl", "ctrl_l", "ctrl_r"),
    "alt": ("alt", "alt_l", "alt_r"),
    "cmd": ("cmd", "cmd_l", "cmd_r"),
}


def _split_spec(spec) -> List[str]:
    """把 ``"shift_r,f9"`` / ``["shift_r", "f9"]`` 统一拆成 token 列表。"""
    if spec is None:
        return []
    tokens: List[str] = []
    if isinstance(spec, str):
        raw = [spec]
    elif isinstance(spec, Iterable):
        raw = [str(item) for item in spec]
    else:
        raw = [str(spec)]
    for item in raw:
        for piece in str(item).split(","):
            piece = piece.strip()
            if piece:
                tokens.append(piece)
    return tokens


def _resolve_key_token(token: str) -> Set[object]:
    """把单个按键名解析为 pynput 键对象集合。

    支持：

    * ``pynput.Key`` 成员名（``shift_r`` / ``f9`` / ``alt_r`` / ``ctrl_r`` …）；
    * 通用修饰键（``shift`` / ``ctrl`` / ``alt`` / ``cmd``）展开为左右两侧；
    * 单字符（``a`` / ``1`` / ``;``）通过 ``KeyCode.from_char`` 匹配。
    """
    if keyboard is None:
        return set()

    name = token.strip().lower().replace(" ", "_").replace("-", "_")
    name = _KEY_ALIASES.get(name, name)

    if name in _MODIFIER_GROUPS:
        keys: Set[object] = set()
        for member in _MODIFIER_GROUPS[name]:
            obj = getattr(keyboard.Key, member, None)
            if obj is not None:
                keys.add(obj)
        return keys

    obj = getattr(keyboard.Key, name, None)
    if obj is not None:
        return {obj}

    if len(token) == 1:
        try:
            return {keyboard.KeyCode.from_char(token)}
        except Exception:  # noqa: BLE001
            return set()

    return set()


def _resolve_key_spec(spec) -> Set[object]:
    """解析逗号分隔 / 列表形式的按键规格，返回键对象集合。"""
    keys: Set[object] = set()
    for token in _split_spec(spec):
        resolved = _resolve_key_token(token)
        if not resolved:
            logger.warning("无法识别的按键名，忽略：%r", token)
        keys |= resolved
    return keys


class HotkeyDaemon:
    """双键双模（PTT + Toggle）全局热键守护进程。

    参数
    ----
    ptt_key:
        按住说话按键。字符串（逗号分隔，如 ``"shift_r,f9"``）或列表。
    toggle_key:
        连续听写开关按键，默认 ``"ctrl_r"``。
    hold_threshold_sec:
        PTT 触发阈值（秒）。默认 0.20，按住满该时长且无其它键才触发。
    on_ptt_start / on_ptt_stop:
        PTT 模式开始 / 结束（松开）时回调。
    on_toggle_start / on_toggle_stop:
        Toggle 模式开始 / 结束（再次按下并松开）时回调。
    """

    def __init__(
        self,
        ptt_key="shift_r,f9",
        toggle_key="ctrl_r",
        hold_threshold_sec: float = 0.20,
        on_ptt_start: Optional[Callable[[], None]] = None,
        on_ptt_stop: Optional[Callable[[], None]] = None,
        on_toggle_start: Optional[Callable[[], None]] = None,
        on_toggle_stop: Optional[Callable[[], None]] = None,
    ) -> None:
        self.hold_threshold_sec = float(hold_threshold_sec)
        self.ptt_key_spec = ptt_key
        self.toggle_key_spec = toggle_key
        self.on_ptt_start = on_ptt_start
        self.on_ptt_stop = on_ptt_stop
        self.on_toggle_start = on_toggle_start
        self.on_toggle_stop = on_toggle_stop

        # 解析按键集合；同一个键不能既是 PTT 又是 Toggle（Toggle 优先）。
        self._ptt_keys: Set[object] = _resolve_key_spec(ptt_key)
        self._toggle_keys: Set[object] = _resolve_key_spec(toggle_key)
        overlap = self._ptt_keys & self._toggle_keys
        if overlap:
            self._ptt_keys -= overlap
            logger.warning("ptt_key 与 toggle_key 存在重叠按键，已从 PTT 移除：%s", overlap)

        # 状态 + 相关计时/标志，全部受 _lock 保护。
        self._lock = threading.RLock()
        self._state: State = State.IDLE

        # PTT 状态：当前物理按下的 ptt 键集合（过滤自动连发）+ 定时器。
        self._ptt_down: Set[object] = set()
        self._ptt_other_key: bool = False   # 本次 PTT 待判定期间是否出现其它键
        self._ptt_timer: Optional[threading.Timer] = None

        # Toggle 状态：当前物理按下的 toggle 键集合。
        self._toggle_down: Set[object] = set()
        self._toggle_pending: bool = False  # 已按下 toggle，待松开时判定
        self._toggle_other_key: bool = False

        self._listener = None
        self._running = False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _match(self, keys: Set[object], key) -> bool:
        """判断 key 是否命中给定集合（普通对象用相等比较）。"""
        if not keys:
            return False
        return key in keys

    @staticmethod
    def _modifier_family(key) -> Optional[str]:
        """返回 key 所属的修饰键家族（shift/ctrl/alt/cmd），非修饰键返回 None。"""
        if keyboard is None:
            return None
        for family in ("shift", "ctrl", "alt", "cmd"):
            for member in (family, f"{family}_l", f"{family}_r"):
                obj = getattr(keyboard.Key, member, None)
                if obj is not None and obj == key:
                    return family
        return None

    def _is_held_modifier_sibling_locked(self, key) -> bool:
        """key 是否只是当前已按下修饰键的"另一种表示"。

        某些 X11 / xdotool 场景下，按住右键（如右 Shift）会同时产生
        ``Key.shift_r`` 与其通用名 ``Key.shift``；若把后者当成"其它键"，
        会误判成组合键并取消本次触发。这里对同一家族的修饰键做去重。
        """
        family = self._modifier_family(key)
        if family is None:
            return False
        for held in list(self._ptt_down) + list(self._toggle_down):
            if self._modifier_family(held) == family:
                return True
        return False

    def _cancel_ptt_timer_locked(self) -> None:
        if self._ptt_timer is not None:
            self._ptt_timer.cancel()
            self._ptt_timer = None

    def _start_ptt_timer_locked(self) -> None:
        self._cancel_ptt_timer_locked()
        self._ptt_timer = threading.Timer(
            self.hold_threshold_sec, self._on_ptt_hold_timeout
        )
        self._ptt_timer.daemon = True
        self._ptt_timer.start()

    def _fire(self, name: str, cb: Optional[Callable[[], None]]) -> None:
        """在锁外安全地调用回调，异常不外泄。"""
        if cb is None:
            return
        try:
            cb()
        except Exception:  # pragma: no cover - 用户回调责任自负
            logger.exception("回调 %s 抛出异常", name)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def _on_press(self, key) -> None:
        with self._lock:
            # ---- PTT 键 ----
            if self._match(self._ptt_keys, key):
                if key in self._ptt_down:  # 过滤 X11 自动连发
                    return
                self._ptt_down.add(key)
                if len(self._ptt_down) == 1:
                    self._ptt_other_key = False
                if self._state == State.IDLE:
                    # 开始 hold_threshold 长按判定。
                    self._start_ptt_timer_locked()
                return

            # ---- Toggle 键 ----
            if self._match(self._toggle_keys, key):
                if key in self._toggle_down:  # 过滤自动连发
                    return
                self._toggle_down.add(key)
                if len(self._toggle_down) == 1:
                    self._toggle_other_key = False
                if self._state in (State.IDLE, State.TOGGLE_RECORDING):
                    # 标记待判定：松开时再决定开始 / 结束。
                    self._toggle_pending = True
                return

            # ---- 其它键：打断待判定的触发 ----
            # 同一修饰键家族的另一种表示（如右 Shift 事件伴随的 Key.shift）不算"其它键"。
            if self._is_held_modifier_sibling_locked(key):
                return
            if self._state == State.IDLE:
                if self._ptt_down or self._ptt_timer is not None:
                    self._ptt_other_key = True
                    self._cancel_ptt_timer_locked()
                    logger.debug("PTT 待判定期间出现其它按键 (vk=%s)，取消触发", key)
                if self._toggle_pending:
                    self._toggle_other_key = True
            elif self._state == State.TOGGLE_RECORDING:
                if self._toggle_pending:
                    self._toggle_other_key = True
            # PTT_RECORDING：已开始录音，其它键不打断（极罕见，仅记录）。

    def _on_ptt_hold_timeout(self) -> None:
        """hold_threshold_sec 定时器到点：仍按住且未被打断 -> 进入 PTT。"""
        should_fire = False
        with self._lock:
            self._ptt_timer = None
            if (
                self._state == State.IDLE
                and self._ptt_down
                and not self._ptt_other_key
            ):
                self._state = State.PTT_RECORDING
                logger.debug("进入 PTT_RECORDING")
                should_fire = True
            # 否则：已松开 / 已打断，交给 _on_release 或直接丢弃。
        if should_fire:
            self._fire("on_ptt_start", self.on_ptt_start)

    def _on_release(self, key) -> None:
        pending_fire: Optional[tuple] = None

        with self._lock:
            # ---- PTT 键松开 ----
            if self._match(self._ptt_keys, key):
                if key in self._ptt_down:
                    self._ptt_down.discard(key)
                    if not self._ptt_down:  # 不再按着任何 ptt 键
                        self._cancel_ptt_timer_locked()
                        if self._state == State.PTT_RECORDING:
                            self._state = State.IDLE
                            self._ptt_other_key = False
                            logger.debug("结束 PTT_RECORDING")
                            pending_fire = ("on_ptt_stop", self.on_ptt_stop)
                        else:
                            # 未达阈值（短按）或已被组合键打断 -> 丢弃，不触发。
                            self._ptt_other_key = False

            # ---- Toggle 键松开 ----
            elif self._match(self._toggle_keys, key):
                if key in self._toggle_down:
                    self._toggle_down.discard(key)
                    if not self._toggle_down:
                        pending = self._toggle_pending
                        aborted = self._toggle_other_key
                        self._toggle_pending = False
                        self._toggle_other_key = False
                        if pending and not aborted:
                            if self._state == State.IDLE:
                                self._state = State.TOGGLE_RECORDING
                                logger.debug("Toggle 键松开 -> 进入 TOGGLE_RECORDING")
                                pending_fire = ("on_toggle_start", self.on_toggle_start)
                            elif self._state == State.TOGGLE_RECORDING:
                                self._state = State.IDLE
                                logger.debug("Toggle 键松开 -> 结束 TOGGLE_RECORDING")
                                pending_fire = ("on_toggle_stop", self.on_toggle_stop)

        if pending_fire is not None:
            self._fire(*pending_fire)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动后台监听（非阻塞）。"""
        if keyboard is None:
            raise RuntimeError(f"pynput 未安装或导入失败: {_IMPORT_ERROR}")
        with self._lock:
            if self._running:
                return
            self._running = True
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.daemon = True
        self._listener.start()
        logger.info(
            "HotkeyDaemon 已启动 (ptt=%s, toggle=%s, hold_threshold=%.2fs)",
            self.ptt_key_spec,
            self.toggle_key_spec,
            self.hold_threshold_sec,
        )

    def stop(self) -> None:
        """停止监听并清理定时器。"""
        with self._lock:
            self._running = False
            self._cancel_ptt_timer_locked()
            self._ptt_down = set()
            self._toggle_down = set()
            self._toggle_pending = False
            self._ptt_other_key = False
            self._toggle_other_key = False
            self._state = State.IDLE
            listener = self._listener
            self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # pragma: no cover
                logger.exception("停止 key listener 失败")
        logger.info("HotkeyDaemon 已停止")

    def join(self) -> None:
        if self._listener is not None:
            self._listener.join()

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def __enter__(self) -> "HotkeyDaemon":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


if __name__ == "__main__":  # pragma: no cover - 手工调试
    logging.basicConfig(
        level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    print("监听热键：按住 右Shift/F9 说话；按一下 右Ctrl 连续听写。Ctrl-C 退出。")

    def ptt_start():
        print(">> PTT 开始录音")

    def ptt_stop():
        print(">> PTT 结束录音")

    def tog_start():
        print(">> Toggle 开始录音")

    def tog_stop():
        print(">> Toggle 结束录音")

    with HotkeyDaemon(
        ptt_key="shift_r,f9",
        toggle_key="ctrl_r",
        hold_threshold_sec=0.20,
        on_ptt_start=ptt_start,
        on_ptt_stop=ptt_stop,
        on_toggle_start=tog_start,
        on_toggle_stop=tog_stop,
    ) as daemon:
        try:
            daemon.join()
        except KeyboardInterrupt:
            pass

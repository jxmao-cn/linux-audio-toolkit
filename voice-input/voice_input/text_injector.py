"""文本注入器 (Text Injector)。

在 Ubuntu 22.04 / X11 (DISPLAY=:1, IBus) 以及 Wayland 环境下，把识别出的
**中英文混合文本**快速、无损地写入当前鼠标点击选中的焦点窗口的光标处。

跨显示协议（X11 + Wayland 双栈）
================================
* **X11**：剪贴板用 ``xclip`` / ``xsel``，按键用 ``xdotool``，并回退
  ``python-xlib`` + ``XTEST``；窗口类型检测优先 python-xlib，回退 ``xprop``。
* **Wayland**：自动检测 ``XDG_SESSION_TYPE=wayland`` 或 ``WAYLAND_DISPLAY``，
  若装有 ``wl-copy`` / ``wl-paste`` 则优先走 Wayland 剪贴板，
  若装有 ``wtype`` / ``ydotool`` 则优先走 Wayland 原生按键注入；
  工具缺失时自动回退到 XWayland 下的 X11 方案（xclip + xdotool）。

核心思路（"剪贴板事务"）：
    1. 备份当前 CLIPBOARD(以及 PRIMARY) 的内容；
    2. 把待注入文本写入 CLIPBOARD + PRIMARY；
    3. 依据焦点窗口类型发送粘贴快捷键
       - 终端类窗口 (gnome-terminal / kitty / ...):  ctrl+shift+v
       - 普通 GUI 窗口 (VSCode / Chrome / 编辑器 ...): ctrl+v
    4. 延迟一小段时间后把原剪贴板内容写回，做到"无损"。

为什么用剪贴板而不是逐字符 type？
    - 逐字符注入无法可靠输入中文字符（需要 XKB / 输入法映射，且对多字节
      字符经常丢字 / 错位）；剪贴板粘贴对中英文混排是 100% 无损的。
    - 剪贴板路径唯一的风险是"污染用户剪贴板"，用备份 + 延迟恢复即可消除。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 终端窗口标识（不区分大小写，命中 WM_CLASS 中任意一个即视为终端）
# ---------------------------------------------------------------------------
TERMINAL_MARKERS: tuple = (
    "gnome-terminal",
    "kitty",
    "alacritty",
    "wezterm",
    "konsole",
    "xterm",
    "terminator",
    "tilix",
    "xfce4-terminal",
)


def _which(program: str) -> Optional[str]:
    """查找可执行文件路径（带缓存由 shutil 自己维护）。"""
    return shutil.which(program)


class TextInjector:
    """把文本无损地"粘贴"进当前焦点窗口。

    参数
    ----
    restore_clipboard:
        注入完成后是否恢复用户原有的剪贴板内容。默认 ``True``。
    restore_delay_sec:
        恢复剪贴板前的等待时间。目标窗口收到 ctrl+v 之后是**异步**读取
        剪贴板的，因此必须等待它读取完成后再写回原内容，否则会粘贴到
        "恢复后的旧内容"。默认 0.35s。
    """

    def __init__(self, restore_clipboard: bool = True, restore_delay_sec: float = 0.35) -> None:
        self.restore_clipboard = bool(restore_clipboard)
        self.restore_delay_sec = float(restore_delay_sec)

        # 序列化注入事务，避免多个线程同时改写剪贴板
        self._lock = threading.RLock()
        # 事务代次：只有"最新一代"的恢复定时器才允许写回剪贴板
        self._txn_id: int = 0

        # 工具可用性（懒探测一次）
        self._have_xclip: Optional[bool] = None
        self._have_xsel: Optional[bool] = None
        self._have_xdotool: Optional[bool] = None
        self._have_xprop: Optional[bool] = None
        self._have_wl_copy: Optional[bool] = None
        self._have_wl_paste: Optional[bool] = None
        self._have_wtype: Optional[bool] = None
        self._have_ydotool: Optional[bool] = None

        # 会话类型（懒探测一次）：True 表示 Wayland。
        self._wayland: Optional[bool] = None

        # python-xlib 延迟导入（可选依赖）
        self._xlib_mod = None
        self._xlib_display = None
        self._xlib_tried = False

    # ------------------------------------------------------------------
    # 显示会话类型探测
    # ------------------------------------------------------------------
    def _is_wayland(self) -> bool:
        """当前是否为 Wayland 会话（``XDG_SESSION_TYPE`` 或 ``WAYLAND_DISPLAY``）。"""
        if self._wayland is None:
            session = os.environ.get("XDG_SESSION_TYPE", "").lower()
            self._wayland = session == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
        return self._wayland

    def session_type(self) -> str:
        """返回当前会话类型字符串：``"wayland"`` 或 ``"x11"``。"""
        return "wayland" if self._is_wayland() else "x11"

    # ------------------------------------------------------------------
    # 依赖探测
    # ------------------------------------------------------------------
    def _tool(self, name: str) -> bool:
        attr = {
            "xclip": "_have_xclip",
            "xsel": "_have_xsel",
            "xdotool": "_have_xdotool",
            "xprop": "_have_xprop",
            "wl-copy": "_have_wl_copy",
            "wl-paste": "_have_wl_paste",
            "wtype": "_have_wtype",
            "ydotool": "_have_ydotool",
        }[name]
        cached = getattr(self, attr)
        if cached is None:
            cached = _which(name) is not None
            setattr(self, attr, cached)
        return cached

    def _xlib(self):
        """返回 (display, X, xtest, XK) 元组；不可用则返回 None。"""
        if self._xlib_mod is None and not self._xlib_tried:
            self._xlib_tried = True
            try:
                from Xlib import X, display as xdisplay  # type: ignore
                from Xlib.ext import xtest  # type: ignore
                from Xlib import XK  # type: ignore

                self._xlib_mod = {
                    "display": xdisplay,
                    "X": X,
                    "xtest": xtest,
                    "XK": XK,
                }
            except Exception as exc:  # pragma: no cover - 取决于环境
                logger.debug("python-xlib 不可用: %s", exc)
                self._xlib_mod = None
        return self._xlib_mod

    def _display(self):
        """返回一个可复用的 Xlib Display（失败返回 None）。"""
        mod = self._xlib()
        if mod is None:
            return None
        if self._xlib_display is None:
            try:
                self._xlib_display = mod["display"].Display()
            except Exception as exc:  # pragma: no cover
                logger.debug("Xlib Display 打开失败: %s", exc)
                self._xlib_display = False  # type: ignore[assignment]
        return self._xlib_display or None

    # ------------------------------------------------------------------
    # 焦点窗口类型检测
    # ------------------------------------------------------------------
    def _active_window_id_xlib(self) -> Optional[int]:
        d = self._display()
        if d is None:
            return None
        mod = self._xlib()
        try:
            root = d.screen().root
            atom = d.intern_atom("_NET_ACTIVE_WINDOW")
            prop = root.get_full_property(atom, mod["X"].AnyPropertyType)  # type: ignore[index]
            if prop is not None and prop.value:
                return int(prop.value[0])
            # 回退：直接问输入焦点
            focus = d.get_input_focus().focus
            if hasattr(focus, "id"):
                return int(focus.id)
        except Exception as exc:
            logger.debug("xlib 获取活动窗口失败: %s", exc)
        return None

    def _active_window_id_xprop(self) -> Optional[int]:
        if not self._tool("xprop"):
            return None
        try:
            out = subprocess.run(
                ["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                capture_output=True,
                text=True,
                timeout=2.0,
            ).stdout
        except Exception as exc:
            logger.debug("xprop 获取活动窗口失败: %s", exc)
            return None
        # 形如: _NET_ACTIVE_WINDOW(WINDOW): window id # 0x3600007
        if "#" not in out:
            return None
        token = out.split("#", 1)[1].strip().split()[0] if out.split("#", 1)[1].strip() else ""
        try:
            return int(token, 16)
        except ValueError:
            return None

    def _wm_class_xlib(self, win_id: int) -> Optional[str]:
        d = self._display()
        if d is None:
            return None
        try:
            win = d.create_resource_object("window", win_id)
            pair = win.get_wm_class()  # -> (instance_name, class_name) 或 None
            if pair:
                return " ".join(str(x) for x in pair if x)
            return None
        except Exception as exc:
            logger.debug("xlib 读取 WM_CLASS 失败: %s", exc)
            return None

    def _wm_class_xprop(self, win_id: int) -> Optional[str]:
        if not self._tool("xprop"):
            return None
        try:
            out = subprocess.run(
                ["xprop", "-id", hex(win_id), "WM_CLASS"],
                capture_output=True,
                text=True,
                timeout=2.0,
            ).stdout
        except Exception as exc:
            logger.debug("xprop 读取 WM_CLASS 失败: %s", exc)
            return None
        # 形如: WM_CLASS(STRING) = "kitty", "kitty"
        if "=" not in out:
            return None
        return out.split("=", 1)[1].strip()

    def is_terminal_window(self) -> bool:
        """当前活动(焦点)窗口是否为常见终端。"""
        win_id = self._active_window_id_xlib()
        wm_class = self._wm_class_xlib(win_id) if win_id else None

        if wm_class is None:  # python-xlib 不可用或拿不到，用 xprop 回退
            win_id = win_id or self._active_window_id_xprop()
            if win_id is None:
                logger.debug("无法确定活动窗口，按非终端处理")
                return False
            wm_class = self._wm_class_xprop(win_id)

        if not wm_class:
            return False

        low = wm_class.lower()
        return any(marker in low for marker in TERMINAL_MARKERS)

    # ------------------------------------------------------------------
    # 剪贴板读写（CLIPBOARD / PRIMARY）
    # ------------------------------------------------------------------
    def _read_selection(self, selection: str) -> Optional[str]:
        """读取选择区内容；空/失败返回 None。

        Wayland 会话优先使用 ``wl-paste``（缺省无换行），否则回退到
        ``xclip`` / ``xsel``（含 XWayland 场景）。
        """
        cmd: Optional[List[str]] = None
        if self._is_wayland() and self._tool("wl-paste"):
            cmd = ["wl-paste", "--no-newline"]
            if selection == "primary":
                cmd.append("--primary")
        elif self._tool("xclip"):
            cmd = ["xclip", "-o", "-selection", selection]
        elif self._tool("xsel"):
            flag = "--clipboard" if selection == "clipboard" else "--primary"
            cmd = ["xsel", "-o", flag]
        if cmd is None:
            return None
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=1.5)
        except Exception as exc:
            logger.debug("读取 %s 失败: %s", selection, exc)
            return None
        if proc.returncode != 0:
            return None
        try:
            return proc.stdout.decode("utf-8", errors="replace")
        except Exception:
            return None

    def _write_selection(self, selection: str, text: str) -> bool:
        """把文本写入选择区，返回是否成功。

        Wayland 会话优先使用 ``wl-copy``；否则回退 ``xclip`` / ``xsel``。

        注意：xclip/xsel/wl-copy 都会 fork 到后台持有 selection 的所有权，
        因此这里的 subprocess 会在写入后正常返回。
        """
        cmd: Optional[List[str]] = None
        if self._is_wayland() and self._tool("wl-copy"):
            cmd = ["wl-copy"]
            if selection == "primary":
                cmd.append("--primary")
        elif self._tool("xclip"):
            cmd = ["xclip", "-selection", selection, "-in"]
        elif self._tool("xsel"):
            flag = "--clipboard" if selection == "clipboard" else "--primary"
            cmd = ["xsel", "-i", flag]
        else:
            logger.warning("wl-copy / xclip / xsel 均不可用，无法写入剪贴板")
            return False
        try:
            proc = subprocess.run(cmd, input=text.encode("utf-8"), timeout=3.0)
            return proc.returncode == 0
        except Exception as exc:
            logger.debug("写入 %s 失败: %s", selection, exc)
            return False

    # ------------------------------------------------------------------
    # 发送粘贴快捷键
    # ------------------------------------------------------------------
    def _send_paste_xdotool(self, terminal: bool) -> bool:
        if not self._tool("xdotool"):
            return False
        combo = "ctrl+shift+v" if terminal else "ctrl+v"
        try:
            proc = subprocess.run(
                ["xdotool", "key", "--clearmodifiers", combo],
                capture_output=True,
                timeout=3.0,
            )
            return proc.returncode == 0
        except Exception as exc:
            logger.debug("xdotool 发送 %s 失败: %s", combo, exc)
            return False

    def _send_paste_xlib(self, terminal: bool) -> bool:
        mod = self._xlib()
        d = self._display()
        if mod is None or d is None:
            return False
        X = mod["X"]  # type: ignore[index]
        XK = mod["XK"]  # type: ignore[index]
        xtest = mod["xtest"]  # type: ignore[index]

        def ks(name: str) -> int:
            return d.keysym_to_keycode(XK.string_to_keysym(name))

        def tap(keycode: int) -> None:
            xtest.fake_input(d, X.KeyPress, keycode)
            d.sync()
            xtest.fake_input(d, X.KeyRelease, keycode)
            d.sync()

        try:
            # 用户此刻可能仍按着右 Ctrl（PTT 手柄），先确保释放物理 Control_R，
            # 否则我们合成的 ctrl+v 会变成 ctrl+ctrl+v 之类。
            ctrl_r = ks("Control_R")
            xtest.fake_input(d, X.KeyRelease, ctrl_r)
            d.sync()
            time.sleep(0.01)

            ctrl_l = ks("Control_L")
            shift_l = ks("Shift_L")
            v = ks("v")

            xtest.fake_input(d, X.KeyPress, ctrl_l)
            if terminal:
                xtest.fake_input(d, X.KeyPress, shift_l)
            d.sync()
            tap(v)
            if terminal:
                xtest.fake_input(d, X.KeyRelease, shift_l)
            xtest.fake_input(d, X.KeyRelease, ctrl_l)
            d.sync()
            return True
        except Exception as exc:
            logger.debug("xlib 发送粘贴键失败: %s", exc)
            return False

    def _send_paste_wayland(self, terminal: bool) -> bool:
        """Wayland 原生按键注入：优先 ``wtype``，回退 ``ydotool``。

        ``wtype`` 在 GNOME / KDE 的 Wayland 会话下通常需要相应的虚拟键盘
        协议支持；若不可用则自动回退到 ``ydotool`` 或 XWayland 下的 xdotool。
        """
        if self._tool("wtype"):
            args = ["wtype"]
            if terminal:
                args += ["-M", "ctrl", "-M", "shift", "v", "-m", "shift", "-m", "ctrl"]
            else:
                args += ["-M", "ctrl", "v", "-m", "ctrl"]
            try:
                proc = subprocess.run(args, capture_output=True, timeout=3.0)
                if proc.returncode == 0:
                    return True
                logger.debug(
                    "wtype 发送粘贴键失败：%s",
                    proc.stderr.decode("utf-8", "replace").strip(),
                )
            except Exception as exc:
                logger.debug("wtype 发送粘贴键异常: %s", exc)

        if self._tool("ydotool"):
            # Linux evdev 键码：ctrl=29 shift=42 v=47。
            keys = ["29:1"]
            if terminal:
                keys.append("42:1")
            keys += ["47:1", "47:0"]
            if terminal:
                keys.append("42:0")
            keys.append("29:0")
            try:
                proc = subprocess.run(
                    ["ydotool", "key", *keys], capture_output=True, timeout=3.0
                )
                if proc.returncode == 0:
                    return True
                logger.debug(
                    "ydotool 发送粘贴键失败：%s",
                    proc.stderr.decode("utf-8", "replace").strip(),
                )
            except Exception as exc:
                logger.debug("ydotool 发送粘贴键异常: %s", exc)

        return False

    def _send_paste(self, terminal: bool) -> bool:
        """Wayland 会话优先 wtype/ydotool；否则 xdotool -> python-xlib。"""
        if self._is_wayland() and self._send_paste_wayland(terminal):
            return True
        if self._send_paste_xdotool(terminal):
            return True
        return self._send_paste_xlib(terminal)

    # ------------------------------------------------------------------
    # 恢复剪贴板
    # ------------------------------------------------------------------
    def _schedule_restore(self, txn_id: int, backup: dict) -> None:
        """在后台线程延迟写回原剪贴板内容。"""

        def _restore() -> None:
            time.sleep(self.restore_delay_sec)
            with self._lock:
                # 若期间又有新的注入事务，则放弃本次恢复（新事务拥有剪贴板）
                if txn_id != self._txn_id:
                    logger.debug("跳过过期剪贴板恢复 txn=%s (cur=%s)", txn_id, self._txn_id)
                    return
                for selection, content in backup.items():
                    self._write_selection(selection, content if content is not None else "")
                logger.debug("已恢复剪贴板 txn=%s", txn_id)

        t = threading.Thread(target=_restore, name="clipboard-restore", daemon=True)
        t.start()

    # ------------------------------------------------------------------
    # 对外主接口
    # ------------------------------------------------------------------
    def inject_text(self, text: str) -> bool:
        """把 ``text`` 注入到当前焦点窗口光标处。

        返回是否成功发送了粘贴动作。空文本直接返回 True（无事可做）。
        """
        if not text:
            return True

        with self._lock:
            # 1) 备份当前剪贴板内容（失败/空 -> None，恢复时清空即可）
            backup = {
                "clipboard": self._read_selection("clipboard"),
                "primary": self._read_selection("primary"),
            }

            # 2) 写入待注入文本
            ok = self._write_selection("clipboard", text)
            self._write_selection("primary", text)  # PRIMARY 失败不致命

            if not ok:
                # 连剪贴板都写不进去，回退到逐字符注入（至少英文能进）
                logger.warning("剪贴板写入失败，尝试逐字符注入回退")
                if self._is_wayland() and self._tool("wtype"):
                    try:
                        subprocess.run(["wtype", "--", text], timeout=10.0)
                        return True
                    except Exception as exc:
                        logger.error("wtype 回退也失败: %s", exc)
                if self._tool("xdotool"):
                    try:
                        subprocess.run(
                            ["xdotool", "type", "--clearmodifiers", "--", text],
                            timeout=10.0,
                        )
                        return True
                    except Exception as exc:
                        logger.error("xdotool type 回退也失败: %s", exc)
                return False

            # 3) 等剪贴板就绪后发送粘贴键
            time.sleep(0.03)
            terminal = self.is_terminal_window()
            sent = self._send_paste(terminal)
            logger.info(
                "注入 %d 字符 -> %s 窗口（%s 会话），快捷键 %s (%s)",
                len(text),
                "终端" if terminal else "GUI",
                self.session_type(),
                "ctrl+shift+v" if terminal else "ctrl+v",
                "ok" if sent else "FAILED",
            )

            # 4) 安排延迟恢复
            self._txn_id += 1
            if self.restore_clipboard and any(v for v in backup.values()):
                self._schedule_restore(self._txn_id, backup)

            return sent


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
def inject_text(text: str, restore_clipboard: bool = True) -> bool:
    """一次性注入（每次新建实例，适合脚本调用）。"""
    return TextInjector(restore_clipboard=restore_clipboard).inject_text(text)


if __name__ == "__main__":  # pragma: no cover - 手工调试
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import sys

    demo = " ".join(sys.argv[1:]) or "你好，world! 这是一段中英文混合的测试文本。"
    inj = TextInjector()
    print(f"DISPLAY={os.environ.get('DISPLAY')} 终端={inj.is_terminal_window()}")
    print("3 秒内请点击目标输入框...")
    time.sleep(3.0)
    print("injected:", inj.inject_text(demo))

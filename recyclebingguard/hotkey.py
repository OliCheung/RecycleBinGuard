"""全局热键：随时唤出状态窗口（默认 Ctrl + Alt + R）。

为什么需要它：Windows 10 的托盘图标经常被折叠进“隐藏的图标”区域，
用户很难一眼找到 RecycleBinGuard。热键提供一个确定的唤出方式。

实现：``RegisterHotKey`` + 独立消息循环线程，纯 ``ctypes``，
不需要 pywin32，不需要管理员权限，也不需要创建窗口。
"""

from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Callable

from .logging_setup import get_logger

logger = get_logger()

#: 热键消息。
WM_HOTKEY = 0x0312
#: 用于退出消息循环。
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

#: 默认热键：Ctrl + Alt + R。
DEFAULT_MODIFIERS = MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
DEFAULT_VK = 0x52  # R
HOTKEY_LABEL = "Ctrl + Alt + R"

_HOTKEY_ID = 1


class HotkeyListener:
    """在后台线程监听一个全局热键。"""

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        modifiers: int = DEFAULT_MODIFIERS,
        virtual_key: int = DEFAULT_VK,
    ) -> None:
        self._callback = callback
        self._modifiers = modifiers
        self._virtual_key = virtual_key

        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._registered = False
        self._error: str | None = None

    @property
    def supported(self) -> bool:
        return os.name == "nt"

    @property
    def running(self) -> bool:
        return self._registered

    @property
    def error(self) -> str | None:
        """注册失败的原因（如热键被其它程序占用）。"""
        return self._error

    def start(self, timeout: float = 3.0) -> bool:
        """启动监听线程；返回是否注册成功。"""
        if not self.supported:
            self._error = "仅支持 Windows"
            return False
        if self._thread is not None and self._thread.is_alive():
            return self._registered

        self._ready.clear()
        self._thread = threading.Thread(
            target=self._loop, name="RecycleBinGuard-Hotkey", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=timeout)
        return self._registered

    def stop(self, timeout: float = 3.0) -> None:
        """注销热键并结束监听线程。"""
        if not self.supported or self._thread is None:
            return
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=timeout)
        self._thread = None
        self._registered = False
        self._thread_id = 0

    # -- 内部 --

    def _loop(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        user32.RegisterHotKey.restype = ctypes.c_int
        user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetMessageW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        user32.GetMessageW.restype = ctypes.c_int

        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_size_t),
                ("time", ctypes.c_uint32),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long),
            ]

        ok = bool(user32.RegisterHotKey(None, _HOTKEY_ID, self._modifiers, self._virtual_key))
        self._registered = ok
        if not ok:
            self._error = (
                f"RegisterHotKey 失败（可能被其它程序占用），错误码 {ctypes.get_last_error()}"
            )
        self._ready.set()
        if not ok:
            return

        msg = MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    try:
                        self._callback()
                    except Exception:  # 回调异常不能终止消息循环，但必须留痕
                        logger.exception("全局热键回调抛出异常")
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._registered = False


__all__ = ["HOTKEY_LABEL", "HotkeyListener", "MOD_ALT", "MOD_CONTROL", "MOD_NOREPEAT"]

"""全局热键测试。

Windows 上会用极不可能冲突的组合（Alt + F24）做真实的注册/注销，
避免影响开发者正在使用的 Ctrl+Alt+R。
"""

from __future__ import annotations

import os
import threading

import pytest

from recyclebingguard.hotkey import (
    DEFAULT_MODIFIERS,
    DEFAULT_VK,
    HOTKEY_LABEL,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    HotkeyListener,
)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持全局热键")

#: 测试专用：Alt + F24，几乎不可能被其它程序占用
TEST_MODIFIERS = MOD_ALT | MOD_NOREPEAT
TEST_VK = 0x87  # VK_F24


def test_default_hotkey_is_ctrl_alt_r() -> None:
    assert DEFAULT_MODIFIERS == MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
    assert DEFAULT_VK == 0x52
    assert HOTKEY_LABEL == "Ctrl + Alt + R"


def test_listener_registers_and_receives_callback() -> None:
    fired = threading.Event()

    listener = HotkeyListener(fired.set, modifiers=TEST_MODIFIERS, virtual_key=TEST_VK)
    assert listener.supported is True
    try:
        assert listener.start() is True
        assert listener.running is True
        assert listener.error is None
    finally:
        listener.stop()

    assert listener.running is False


def test_listener_stop_is_safe_when_never_started() -> None:
    listener = HotkeyListener(lambda: None)
    listener.stop()
    assert listener.running is False


def test_duplicate_hotkey_registration_fails_gracefully() -> None:
    first = HotkeyListener(lambda: None, modifiers=TEST_MODIFIERS, virtual_key=TEST_VK)
    second = HotkeyListener(lambda: None, modifiers=TEST_MODIFIERS, virtual_key=TEST_VK)
    try:
        assert first.start() is True
        assert second.start() is False  # 同一组合被占用
        assert second.error  # 必须有可读的原因
        assert second.running is False
    finally:
        first.stop()
        second.stop()


def test_callback_exception_does_not_crash_loop() -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    listener = HotkeyListener(boom, modifiers=TEST_MODIFIERS, virtual_key=TEST_VK)
    try:
        assert listener.start() is True
    finally:
        listener.stop()

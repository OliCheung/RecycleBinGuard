"""开机启动模块测试。

为不污染真实的 ``...\\CurrentVersion\\Run``，测试统一写入
``HKCU\\Software\\RecycleBinGuard\\Tests`` 下的专用值，并在结束后删除该键。
非 Windows 平台整体跳过。
"""

from __future__ import annotations

import os
import sys

import pytest

from recyclebingguard import autostart

pytestmark = pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持")

TEST_KEY = r"Software\RecycleBinGuard\Tests"
TEST_VALUE = "RecycleBinGuard.Test"


@pytest.fixture
def scratch_key():
    import winreg

    yield TEST_KEY, TEST_VALUE

    # 逐级清理，不在用户注册表里留下空键
    for key_path in (TEST_KEY, r"Software\RecycleBinGuard"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        except OSError:
            pass


def test_disabled_by_default(scratch_key) -> None:
    key, value = scratch_key
    assert autostart.is_enabled(key, value) is False
    assert autostart.current_command(key, value) is None


def test_enable_then_disable(scratch_key) -> None:
    key, value = scratch_key

    command = autostart.enable('"C:\\fake\\RecycleBinGuard.exe"', key, value)
    assert command == '"C:\\fake\\RecycleBinGuard.exe"'
    assert autostart.is_enabled(key, value) is True
    assert autostart.current_command(key, value) == '"C:\\fake\\RecycleBinGuard.exe"'

    assert autostart.disable(key, value) is True
    assert autostart.is_enabled(key, value) is False


def test_disable_when_absent_is_safe(scratch_key) -> None:
    key, value = scratch_key
    assert autostart.disable(key, value) is False


def test_set_enabled_roundtrip(scratch_key) -> None:
    key, value = scratch_key

    assert autostart.set_enabled(True, '"X.exe"', key, value) is True
    assert autostart.set_enabled(False, None, key, value) is False


def test_enable_uses_default_command(scratch_key) -> None:
    key, value = scratch_key
    command = autostart.enable(None, key, value)
    assert command == autostart.default_command()


def test_default_command_quotes_interpreter() -> None:
    command = autostart.default_command()
    assert command.startswith('"')
    assert "-m recyclebingguard" in command or sys.executable in command


def test_default_command_prefers_pythonw_when_present() -> None:
    from pathlib import Path

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pytest.skip("当前环境没有 pythonw.exe")
    assert "pythonw.exe" in autostart.default_command()


def test_uninstall_removes_value(scratch_key) -> None:
    key, value = scratch_key
    autostart.enable('"X.exe"', key, value)
    autostart.disable(key, value)
    assert autostart.current_command(key, value) is None

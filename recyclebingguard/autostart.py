"""T4：当前用户登录后自动启动。

实现方式：写当前用户的 ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
注册表值。选择它的理由：

- **仅限当前用户**，不需要管理员权限（符合 PRD “默认不以管理员身份运行”）；
- 不需要 COM / pywin32 等额外依赖；
- 卸载即删除该值，干净可查；
- 用户可在“任务管理器 → 启动”中看到并自行禁用。

不使用系统级 ``HKLM`` 或“任务计划程序”，避免提权与副作用。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import APP_NAME
from .logging_setup import get_logger

logger = get_logger()

#: 当前用户 Run 键。
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: 注册表值名。
VALUE_NAME = APP_NAME

IS_WINDOWS = os.name == "nt"


class AutostartUnsupportedError(RuntimeError):
    """当前平台不支持开机启动设置。"""


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise AutostartUnsupportedError("开机启动设置仅支持 Windows")


#: 登录自启时的附加参数：登录后不要弹出状态窗口，只在托盘静默运行。
MINIMIZED_ARG = "--start-minimized"


def default_command() -> str:
    """返回登录时应执行的命令行（带引号，可直接写入注册表）。

    带上 ``--start-minimized``：开机自启属于后台行为，不应该每次登录都弹窗；
    而用户双击快捷方式时没有这个参数，会正常显示状态窗口。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 打包后：直接运行自身 exe
        return f'"{sys.executable}" {MINIMIZED_ARG}'

    # 开发环境：优先用免控制台的 pythonw.exe
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else python
    return f'"{interpreter}" -m recyclebingguard {MINIMIZED_ARG}'


def current_command(key_path: str = RUN_KEY_PATH, value_name: str = VALUE_NAME) -> str | None:
    """读取已注册的启动命令；未设置返回 ``None``。"""
    _require_windows()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        logger.exception("读取启动项失败")
        return None


def is_enabled(key_path: str = RUN_KEY_PATH, value_name: str = VALUE_NAME) -> bool:
    """是否已设置开机启动。"""
    return current_command(key_path, value_name) is not None


def enable(
    command: str | None = None,
    key_path: str = RUN_KEY_PATH,
    value_name: str = VALUE_NAME,
) -> str:
    """设置开机启动，返回写入的命令行。"""
    _require_windows()
    import winreg

    if command is None:
        command = default_command()

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)

    logger.info("已启用开机启动：%s = %s", value_name, command)
    return command


def disable(key_path: str = RUN_KEY_PATH, value_name: str = VALUE_NAME) -> bool:
    """移除开机启动项；返回是否确实删除了内容。"""
    _require_windows()
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        logger.info("开机启动项本就不存在：%s", value_name)
        return False
    except OSError:
        logger.exception("删除开机启动项失败")
        return False

    logger.info("已关闭开机启动：%s", value_name)
    return True


def set_enabled(
    enabled: bool,
    command: str | None = None,
    key_path: str = RUN_KEY_PATH,
    value_name: str = VALUE_NAME,
) -> bool:
    """统一入口：启用/关闭开机启动，返回设置后的状态。"""
    if enabled:
        enable(command, key_path, value_name)
    else:
        disable(key_path, value_name)
    return is_enabled(key_path, value_name)


def open_startup_settings() -> None:
    """跳转到 Windows 的启动应用设置界面（可选功能，失败不抛异常）。"""
    if not IS_WINDOWS:  # pragma: no cover - 平台分支
        raise AutostartUnsupportedError("仅支持 Windows")
    try:
        subprocess.Popen(["cmd", "/c", "start", "", "ms-settings:startupapps"])
    except OSError:  # pragma: no cover - 打不开设置页面不算致命
        logger.debug("打开启动设置失败", exc_info=True)


__all__ = [
    "AutostartUnsupportedError",
    "RUN_KEY_PATH",
    "VALUE_NAME",
    "current_command",
    "default_command",
    "disable",
    "enable",
    "is_enabled",
    "open_startup_settings",
    "set_enabled",
]

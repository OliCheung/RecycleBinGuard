"""当前用户范围的快捷方式（开始菜单 + 桌面）。

目的：给用户一个确定的“打开键”。托盘图标在 Windows 10 里经常被折叠，
桌面 / 开始菜单快捷方式是最直观的入口。

实现要点：

- 通过 ``WScript.Shell`` COM 创建 ``.lnk``，**不需要** pywin32（用 PowerShell 中转）；
- 路径全部通过环境变量传递，PowerShell 脚本本身是纯 ASCII，
  避免 PowerShell 5.1 按 ANSI 解析 UTF-8 脚本导致的中文路径乱码；
- 只在**当前用户**范围操作，不需要管理员权限。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import APP_NAME
from .logging_setup import get_logger

logger = get_logger()

IS_WINDOWS = os.name == "nt"

#: 创建快捷方式的 PowerShell 脚本（纯 ASCII，路径走环境变量）。
_CREATE_SCRIPT = (
    "$ws = New-Object -ComObject WScript.Shell; "
    "$s = $ws.CreateShortcut($env:RBG_LINK); "
    "$s.TargetPath = $env:RBG_TARGET; "
    "if ($env:RBG_ARGS) { $s.Arguments = $env:RBG_ARGS }; "
    "$s.WorkingDirectory = $env:RBG_WORKDIR; "
    "if ($env:RBG_ICON -and (Test-Path -LiteralPath $env:RBG_ICON)) "
    "{ $s.IconLocation = ($env:RBG_ICON + ',0') }; "
    "$s.Description = 'RecycleBinGuard - Recycle Bin Guard'; "
    "$s.Save()"
)


class ShortcutUnsupportedError(RuntimeError):
    """当前平台不支持创建快捷方式。"""


def _known_folder(value_name: str) -> Path | None:
    """从注册表读取当前用户 Shell 文件夹路径（支持重定向 / OneDrive）。"""
    if not IS_WINDOWS:
        return None
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    return Path(os.path.expandvars(str(value)))


def desktop_dir() -> Path | None:
    """当前用户桌面目录。"""
    folder = _known_folder("Desktop")
    if folder is not None:
        return folder
    return Path.home() / "Desktop"


def start_menu_dir() -> Path | None:
    """开始菜单中程序的专用子目录（``...\\程序\\RecycleBinGuard``）。"""
    programs = _known_folder("Programs")
    if programs is None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        programs = Path(appdata) / r"Microsoft\Windows\Start Menu\Programs"
    return programs / APP_NAME


def shortcut_paths() -> list[Path]:
    """计划创建的快捷方式路径。"""
    paths: list[Path] = []
    start_menu = start_menu_dir()
    if start_menu is not None:
        paths.append(start_menu / f"{APP_NAME}.lnk")
    desktop = desktop_dir()
    if desktop is not None:
        paths.append(desktop / f"{APP_NAME}.lnk")
    return paths


def launch_target() -> tuple[str, str]:
    """返回 ``(目标程序, 参数)``，适配“已打包”与“源码运行”两种情况。"""
    import sys

    if getattr(sys, "frozen", False):
        return sys.executable, ""
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else python), "-m recyclebingguard"


def install_shortcuts(
    icon_path: str | None = None, *, desktop: bool = True, start_menu: bool = True
) -> list[Path]:
    """创建快捷方式，返回成功创建的路径列表。"""
    if not IS_WINDOWS:
        raise ShortcutUnsupportedError("快捷方式仅支持 Windows")

    target, arguments = launch_target()
    workdir = str(Path(target).parent)
    created: list[Path] = []

    for path in shortcut_paths():
        if not desktop and path.parent == desktop_dir():
            continue
        if not start_menu and path.parent == start_menu_dir():
            continue

        path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.update(
            {
                "RBG_LINK": str(path),
                "RBG_TARGET": target,
                "RBG_ARGS": arguments,
                "RBG_WORKDIR": workdir,
                "RBG_ICON": icon_path or "",
            }
        )
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    _CREATE_SCRIPT,
                ],
                env=env,
                check=False,
                timeout=30,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            logger.exception("创建快捷方式失败：%s", path)
            continue

        if path.exists():
            created.append(path)
            logger.info("已创建快捷方式：%s", path)
        else:
            logger.error("快捷方式未生成：%s", path)

    return created


def uninstall_shortcuts() -> list[Path]:
    """删除快捷方式（并清理空的开始菜单子目录），返回被删除的路径。"""
    removed: list[Path] = []
    for path in shortcut_paths():
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            logger.exception("删除快捷方式失败：%s", path)
            continue
        removed.append(path)
        logger.info("已删除快捷方式：%s", path)

    start_menu = start_menu_dir()
    if start_menu is not None and start_menu.is_dir():
        try:
            if not any(start_menu.iterdir()):
                start_menu.rmdir()
        except OSError:  # pragma: no cover - 目录非空或无权限都可以忽略
            pass

    return removed


def is_installed() -> bool:
    """是否至少存在一个快捷方式。"""
    return any(path.exists() for path in shortcut_paths())

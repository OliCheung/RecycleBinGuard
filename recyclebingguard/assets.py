"""图标资源定位与加载。

本项目只有一份图标源文件：``recyclebingguard/assets/icon.png``（1024 或 512 见方、
透明背景的不锈钢菱形网格废纸篓）。托盘、窗口 / 任务栏、EXE 图标、快捷方式
全部由它派生，避免出现多份互相不一致的图标。

两种运行形态下资源位置不同：

- 源码运行：``recyclebingguard/assets/``；
- PyInstaller 打包：``sys._MEIPASS/recyclebingguard/assets/``（见 spec 的 ``datas``）。

资源缺失时所有函数都返回 ``None``，由调用方回退到内置画法 —— 图标不该成为
程序启动的硬依赖。
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image

#: 资源目录名（位于包内）。
ASSETS_DIR_NAME = "assets"

#: 图标源文件名。
ICON_FILE_NAME = "icon.png"

#: 显示图标时使用的最大边长：.ico 里最大一档就是 256，再大没有意义。
MAX_ICON_SIZE = 256


def assets_dir() -> Path:
    """返回图标资源目录（打包后指向解包目录）。"""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "recyclebingguard" / ASSETS_DIR_NAME
    return Path(__file__).resolve().parent / ASSETS_DIR_NAME


def icon_file() -> Path:
    """返回图标源文件路径（不保证存在）。"""
    return assets_dir() / ICON_FILE_NAME


@lru_cache(maxsize=1)
def load_source() -> Image.Image | None:
    """加载并缓存原始图标（RGBA）；文件缺失或损坏时返回 ``None``。

    缓存是必要的：托盘状态变化会反复重建图标，每次读盘 + 解码 1MB PNG
    会让刷新（每 500ms 一次）变得毫无必要地昂贵。
    """
    path = icon_file()
    try:
        with Image.open(path) as raw:
            raw.load()
            return raw.convert("RGBA")
    except (OSError, ValueError):
        return None


def load_icon(size: int = MAX_ICON_SIZE) -> Image.Image | None:
    """按指定边长加载图标；资源缺失时返回 ``None``。"""
    source = load_source()
    if source is None:
        return None
    if source.width == source.height == size:
        return source.copy()
    return source.resize((size, size), Image.Resampling.LANCZOS)


def shortcut_icon_path() -> Path | None:
    """快捷方式应使用的图标文件。

    - 打包后：EXE 自身已内嵌图标，直接指向它，避免额外分发文件；
    - 源码运行：使用 ``tools/make_icon.py`` 生成的 .ico（存在时才用）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    ico = Path(__file__).resolve().parents[1] / "packaging" / "recyclebingguard.ico"
    return ico if ico.exists() else None


__all__ = [
    "MAX_ICON_SIZE",
    "assets_dir",
    "icon_file",
    "load_icon",
    "load_source",
    "shortcut_icon_path",
]

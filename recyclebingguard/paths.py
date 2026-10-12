"""应用数据目录与文件路径解析。

所有用户数据统一放在当前用户的应用数据目录：

    %LOCALAPPDATA%\\RecycleBinGuard

测试或便携场景下可用环境变量 ``RECYCLEBINGUARD_HOME`` 覆盖基准目录，
从而避免污染真实用户数据。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import APP_NAME

#: 覆盖基准目录的环境变量名（测试用）。
HOME_ENV_VAR = "RECYCLEBINGUARD_HOME"


def app_home() -> Path:
    """返回应用数据根目录（不保证已创建）。

    优先级：``RECYCLEBINGUARD_HOME`` > ``%LOCALAPPDATA%\\RecycleBinGuard`` >
    用户主目录下的 ``.RecycleBinGuard``（非 Windows / 缺少环境变量时的兜底）。
    """
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override)

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / APP_NAME

    return Path.home() / f".{APP_NAME}"


def config_path() -> Path:
    """配置文件路径。"""
    return app_home() / "config.json"


def logs_dir() -> Path:
    """日志目录路径。"""
    return app_home() / "logs"


def log_file_path() -> Path:
    """主日志文件路径。"""
    return logs_dir() / "recyclebingguard.log"


def lock_file_path() -> Path:
    """单实例锁文件路径。"""
    return app_home() / f"{APP_NAME}.lock"


def clean_history_path() -> Path:
    """清理历史文件路径（持久化“上一次什么时候清空过回收站”）。

    与轮转日志互补：日志会按大小轮转、最终被覆盖，不适合回答
    “上一次清理是什么时候”，所以另存一份结构化记录。
    """
    return app_home() / "clean_history.json"


def wake_request_path() -> Path:
    """“唤醒已有实例”请求文件路径。

    重复启动本程序时，新实例不再只是弹提示，而是留下这个文件；
    已在运行的实例每轮刷新时检查它，一旦存在就显示状态窗口。
    用文件而不是 IPC，是因为它不需要权限、不需要额外依赖，
    且进程被强杀也不会残留有效状态（只是多一个无用文件，会被自动清理）。
    """
    return app_home() / "wake.request"


def ensure_app_home() -> Path:
    """确保应用数据根目录存在并返回。"""
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    return home

"""应用数据目录与文件路径解析。

所有用户数据默认放在**程序所在目录**，便于就地查看与编辑：

- 源码运行：项目根目录（``<项目>/recyclebingguard/paths.py`` 的上两级）；
- 打包成 EXE：EXE 所在目录。

布局：

    <程序目录>/config.json                    配置
    <程序目录>/logs/recyclebingguard.log      轮转日志
    <程序目录>/clean_history.json             清理历史（缓存）
    <程序目录>/RecycleBinGuard.lock           单实例锁
    <程序目录>/wake.request                   唤醒请求
    <程序目录>/crash.log                      崩溃日志

这些运行时文件**不应提交到仓库**（见 ``.gitignore``）。

测试或便携场景下可用环境变量 ``RECYCLEBINGUARD_HOME`` 覆盖基准目录，
从而避免污染程序目录。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME

#: 覆盖基准目录的环境变量名（测试 / 便携用）。
HOME_ENV_VAR = "RECYCLEBINGUARD_HOME"


def default_base_dir() -> Path:
    """返回默认基准目录（程序所在目录）。

    - 打包后（``sys.frozen``）：EXE 所在目录；
    - 源码运行：项目根目录。

    注意：若程序被安装在只读目录（如 ``Program Files``），写配置与日志会失败；
    此时应通过 ``RECYCLEBINGUARD_HOME`` 指向可写目录。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 本文件位于 <项目根>/recyclebingguard/paths.py
    return Path(__file__).resolve().parent.parent


def app_home() -> Path:
    """返回应用数据根目录（不保证已创建）。

    优先使用 ``RECYCLEBINGUARD_HOME``，否则使用 :func:`default_base_dir`。
    """
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override)
    return default_base_dir()


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


def crash_log_path() -> Path:
    """崩溃日志路径。"""
    return app_home() / "crash.log"


def update_signal_path() -> Path:
    """本地“更新信号”文件路径（离线兜底的自动升级来源）。

    与 :func:`wake_request_path` 同一思路：外部（安装器 / 脚本 / 手动）写入一个
    JSON 文件指向新 EXE，运行实例轮询到后校验并换包。GitHub 在线检查失败时
    作为后备。文件格式见 :mod:`recyclebingguard.updater`。
    """
    return app_home() / "update.signal"


def runtime_paths() -> list[Path]:
    """本程序产生的运行时文件与目录（供 ``--uninstall --purge`` 精确清理）。

    刻意**不**返回 :func:`app_home` 本身：数据目录现在就是程序目录，
    直接整体删除会连同源码 / EXE 一起删掉。这里只列出本程序自己的产物。
    """
    return [
        config_path(),
        clean_history_path(),
        lock_file_path(),
        wake_request_path(),
        update_signal_path(),
        crash_log_path(),
        logs_dir(),
    ]


def ensure_app_home() -> Path:
    """确保应用数据根目录存在并返回。"""
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    return home

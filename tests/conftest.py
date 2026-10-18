"""pytest 共享夹具。

所有涉及文件系统的测试都必须使用 :func:`isolated_home`，
避免读写开发者真实的程序目录（默认数据目录即程序 / 项目目录）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from recyclebingguard import paths
from recyclebingguard.logging_setup import LOGGER_NAME


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把应用数据目录重定向到临时目录，测试结束后自动还原。"""
    home = tmp_path / "RecycleBinGuard"
    monkeypatch.setenv(paths.HOME_ENV_VAR, str(home))
    return home


@pytest.fixture(autouse=True)
def _no_real_dialogs(monkeypatch: pytest.MonkeyPatch):
    """测试期间禁止真实弹出 tkinter 对话框。

    一旦有测试漏掉 monkeypatch，模态对话框会阻塞并挂住整个测试进程
    （曾出现过一个用例被卡 60 秒的情况），所以这里统一改成抛错，
    让问题立刻暴露而不是无限等待。
    """
    try:
        import tkinter.messagebox as messagebox
    except Exception:  # pragma: no cover - 无 tkinter 时跳过保护
        return

    def boom(*_args, **_kwargs):
        raise AssertionError(
            "测试中不允许弹出真实对话框，请先 monkeypatch tkinter.messagebox 对应函数"
        )

    for name in (
        "showinfo",
        "showwarning",
        "showerror",
        "askyesno",
        "askokcancel",
        "askyesnocancel",
        "askretrycancel",
    ):
        if hasattr(messagebox, name):
            monkeypatch.setattr(messagebox, name, boom)


@pytest.fixture(autouse=True)
def _isolate_clean_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把清理历史文件重定向到临时目录。

    监控器默认会往程序目录下的 ``clean_history.json`` 写清理记录；
    不隔离的话，跑一次测试就会污染开发者真实的数据目录。
    """
    target = tmp_path / "clean_history.json"
    monkeypatch.setattr("recyclebingguard.paths.clean_history_path", lambda: target)
    return target


@pytest.fixture(autouse=True)
def _reset_logger():
    """每个测试后关闭并移除全局 logger 的 handler，避免句柄泄漏。"""
    yield
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

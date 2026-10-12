"""轮转日志配置。

日志写入 ``%LOCALAPPDATA%\\RecycleBinGuard\\logs\\``，使用
:class:`logging.handlers.RotatingFileHandler` 限制单文件大小与历史份数。

日志只记录时间、阈值、统计结果、清理结果与错误，**不记录文件内容**。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import paths
from .config import Config

#: 全局 logger 名称。
LOGGER_NAME = "recyclebingguard"

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def get_logger() -> logging.Logger:
    """获取 RecycleBinGuard 的 logger（不自动配置）。"""
    return logging.getLogger(LOGGER_NAME)


def setup_logging(config: Config, *, to_console: bool = False) -> logging.Logger:
    """根据配置初始化日志，返回 logger。

    重复调用是幂等的：会先移除并关闭本模块此前添加的 handler。

    :param config: 提供 ``log_max_bytes`` 与 ``log_backup_count``。
    :param to_console: 是否额外输出到标准错误（调试用；打包为 GUI 时应关闭）。
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    paths.ensure_app_home()
    log_dir = paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    file_handler = RotatingFileHandler(
        paths.log_file_path(),
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if to_console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger

"""日志模块测试：写入位置、轮转参数、幂等性。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from recyclebingguard import paths
from recyclebingguard.config import Config
from recyclebingguard.logging_setup import get_logger, setup_logging


def test_setup_logging_creates_file_and_writes(isolated_home: Path) -> None:
    logger = setup_logging(Config())
    logger.info("hello")

    for handler in logger.handlers:
        handler.flush()

    log_file = paths.log_file_path()
    assert log_file.exists()
    assert "hello" in log_file.read_text(encoding="utf-8")


def test_setup_logging_uses_rotation_config(isolated_home: Path) -> None:
    cfg = Config(log_max_bytes=2048, log_backup_count=3)
    logger = setup_logging(cfg)

    files = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(files) == 1
    assert files[0].maxBytes == 2048
    assert files[0].backupCount == 3


def test_setup_logging_is_idempotent(isolated_home: Path) -> None:
    logger = setup_logging(Config())
    first = list(logger.handlers)
    second_logger = setup_logging(Config())
    assert second_logger is logger
    assert len(logger.handlers) == len(first) == 1


def test_console_handler_optional(isolated_home: Path) -> None:
    assert len(setup_logging(Config(), to_console=True).handlers) == 2
    assert len(setup_logging(Config(), to_console=False).handlers) == 1


def test_console_disabled_by_default(isolated_home: Path) -> None:
    logger = setup_logging(Config())
    assert all(isinstance(h, RotatingFileHandler) for h in logger.handlers)


def test_get_logger_does_not_configure(isolated_home: Path) -> None:
    logger = get_logger()
    assert logger.name == "recyclebingguard"
    # 只断言“未添加本模块的 handler”，避免受 pytest 自身日志插件影响。
    assert not any(isinstance(h, RotatingFileHandler) for h in logger.handlers)
    assert not paths.log_file_path().exists()


def test_level_is_info(isolated_home: Path) -> None:
    assert setup_logging(Config()).level == logging.INFO

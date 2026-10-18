"""单实例保护。

使用**操作系统级文件锁**而不是“锁文件是否存在”判断：

- Windows：``msvcrt.locking``（底层 ``LockFile``，按句柄的强制字节区间锁）；
- 其它平台：``fcntl.flock``（仅供开发环境跑测试用）。

这样做的关键好处是**异常终止后可自恢复**：进程无论正常退出还是被强杀，
操作系统都会释放锁，不会留下导致后续启动失败的“僵尸锁文件”。
"""

from __future__ import annotations

import os
from pathlib import Path

from . import paths
from .logging_setup import get_logger

logger = get_logger()

if os.name == "nt":  # pragma: no cover - 平台分支
    import msvcrt

    _LOCK_BYTES = 1

    def _try_lock(handle: int) -> bool:
        try:
            msvcrt.locking(handle, msvcrt.LK_NBLCK, _LOCK_BYTES)
            return True
        except OSError:
            return False

    def _unlock(handle: int) -> None:
        try:
            msvcrt.locking(handle, msvcrt.LK_UNLCK, _LOCK_BYTES)
        except OSError:
            pass

else:  # pragma: no cover - 平台分支
    import fcntl

    def _try_lock(handle: int) -> bool:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    def _unlock(handle: int) -> None:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except OSError:
            pass


class SingleInstance:
    """进程级单实例锁，可用作上下文管理器。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else paths.lock_file_path()
        self._handle: int | None = None
        self._locked = False

    @property
    def locked(self) -> bool:
        """当前是否持有锁。"""
        return self._locked

    def acquire(self) -> bool:
        """尝试获取锁；已被其它实例持有时返回 ``False``。"""
        if self._locked:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.path, os.O_CREAT | os.O_RDWR)
        if not _try_lock(handle):
            os.close(handle)
            logger.warning("单实例锁已被占用：%s", self.path)
            return False

        self._handle = handle
        self._locked = True
        try:
            os.truncate(handle, 0)
            os.write(handle, f"{os.getpid()}\n".encode("ascii"))
        except OSError:  # pragma: no cover - 记录 PID 失败不影响锁语义
            logger.debug("写入 PID 失败", exc_info=True)
        logger.info("已获取单实例锁：%s（pid=%d）", self.path, os.getpid())
        return True

    def release(self) -> None:
        """释放锁并尽力删除锁文件。"""
        if self._handle is None:
            return
        _unlock(self._handle)
        try:
            os.close(self._handle)
        finally:
            self._handle = None
            self._locked = False
        try:
            self.path.unlink()
        except OSError:  # pragma: no cover - 文件可能已被删除
            pass
        logger.info("已释放单实例锁")

    def __enter__(self) -> SingleInstance:
        if not self.acquire():
            raise RuntimeError("已有 RecycleBinGuard 实例正在运行")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def request_wake() -> bool:
    """请求已在运行的实例显示它的状态窗口。

    重复启动时调用：留下一个唤醒请求文件，已运行的实例会在下一次刷新
    （最多 0.5 秒）时读取并弹窗。返回是否成功留下请求。
    """
    try:
        paths.ensure_app_home()
        paths.wake_request_path().write_text(f"{os.getpid()}\n", encoding="utf-8")
        logger.info("已请求唤醒已有实例")
        return True
    except OSError:
        logger.exception("写入唤醒请求失败")
        return False


def take_wake_request() -> bool:
    """消费一个唤醒请求；返回是否确实存在请求。"""
    target = paths.wake_request_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        logger.debug("删除唤醒请求失败", exc_info=True)
        return False
    logger.info("收到唤醒请求，准备显示状态窗口")
    return True

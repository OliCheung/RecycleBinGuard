"""单实例锁测试（使用临时锁文件，不影响真实运行中的实例）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from recyclebingguard import paths
from recyclebingguard.single_instance import (
    SingleInstance,
    request_wake,
    take_wake_request,
)


def test_first_acquire_succeeds(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.lock"
    instance = SingleInstance(lock_path)

    assert instance.acquire() is True
    assert instance.locked is True
    assert lock_path.exists()

    instance.release()
    assert instance.locked is False


def test_second_acquire_fails_while_held(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.lock"
    first = SingleInstance(lock_path)
    second = SingleInstance(lock_path)

    assert first.acquire() is True
    assert second.acquire() is False
    assert second.locked is False

    first.release()


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.lock"
    first = SingleInstance(lock_path)
    assert first.acquire()
    first.release()

    second = SingleInstance(lock_path)
    assert second.acquire() is True
    second.release()


def test_lock_recovers_after_process_dies(tmp_path: Path) -> None:
    """模拟异常终止：父进程结束时操作系统应释放锁，后续实例可正常获取。"""
    import subprocess
    import sys

    lock_path = tmp_path / "app.lock"
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{str(Path(__file__).resolve().parents[1])}');"
        "from recyclebingguard.single_instance import SingleInstance;"
        f"i = SingleInstance(r'{str(lock_path)}');"
        "assert i.acquire();"
        "print('LOCKED', flush=True);"
        "import time; time.sleep(30)"
    )

    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline().strip()
        assert line == "LOCKED", (
            f"子进程未能加锁：{line} {child.stderr.read() if child.stderr else ''}"
        )

        # 子进程持锁期间，本进程无法获取
        assert SingleInstance(lock_path).acquire() is False
    finally:
        child.kill()  # 模拟异常终止（非正常释放）
        child.wait(timeout=10)

    # 子进程被强杀后，锁应自动释放
    recovered = SingleInstance(lock_path)
    assert recovered.acquire() is True, "异常终止后锁未被操作系统释放"
    recovered.release()


def test_context_manager_raises_when_locked(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.lock"
    holder = SingleInstance(lock_path)
    holder.acquire()
    try:
        with pytest.raises(RuntimeError):
            with SingleInstance(lock_path):
                pass
    finally:
        holder.release()


def test_context_manager_releases_on_exit(tmp_path: Path) -> None:
    lock_path = tmp_path / "app.lock"
    with SingleInstance(lock_path) as instance:
        assert instance.locked is True
    assert SingleInstance(lock_path).acquire() is True


def test_release_without_acquire_is_safe(tmp_path: Path) -> None:
    SingleInstance(tmp_path / "app.lock").release()


def test_acquire_is_idempotent(tmp_path: Path) -> None:
    instance = SingleInstance(tmp_path / "app.lock")
    assert instance.acquire() is True
    assert instance.acquire() is True
    instance.release()


def test_acquire_treats_permission_error_as_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 重复启动的核心路径：``os.open`` 被拒（Errno 13）。

    已有实例加了强制字节锁后，第二个进程连打开锁文件都会失败。
    这必须被当成“已有实例在运行”，绝不能让异常冒泡把进程搞崩
    （崩了就永远走不到“唤醒已有实例”那一步了）。
    """
    import os

    from recyclebingguard import single_instance

    lock_path = tmp_path / "app.lock"
    real_open = os.open

    def boom(path, flags, *args, **kwargs):
        if str(path) == str(lock_path):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(single_instance.os, "open", boom)

    instance = SingleInstance(lock_path)
    assert instance.acquire() is False
    assert instance.locked is False


def test_acquire_treats_other_oserror_as_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """打不开锁文件时宁可“不启动”，也不冒险放第二个实例上去清理回收站。"""
    import os

    from recyclebingguard import single_instance

    lock_path = tmp_path / "app.lock"
    real_open = os.open

    def boom(path, flags, *args, **kwargs):
        if str(path) == str(lock_path):
            raise OSError(5, "Input/output error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(single_instance.os, "open", boom)

    assert SingleInstance(lock_path).acquire() is False


def test_acquire_survives_real_cross_process_lock(tmp_path: Path) -> None:
    """真实跨进程：另一个 Python 进程持锁时，``acquire()`` 必须返回 ``False``。

    不关心它到底是死在哪一步（``open`` 还是 ``locking``），只保证：
    既不崩溃、也不给出“我拿到锁了”的假象。
    """
    import subprocess
    import sys

    lock_path = tmp_path / "app.lock"
    code = (
        "import sys;"
        f"sys.path.insert(0, r'{str(Path(__file__).resolve().parents[1])}');"
        "from recyclebingguard.single_instance import SingleInstance;"
        f"i = SingleInstance(r'{str(lock_path)}');"
        "assert i.acquire();"
        "print('LOCKED', flush=True);"
        "import time; time.sleep(30)"
    )

    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"

        assert SingleInstance(lock_path).acquire() is False
    finally:
        child.kill()
        child.wait(timeout=10)


# --------------------------------------------------------------------------
# 唤醒已有实例
# --------------------------------------------------------------------------


def test_request_wake_creates_file(isolated_home: Path) -> None:
    target = paths.wake_request_path()
    assert not target.exists()

    assert request_wake() is True
    assert target.exists()

    assert take_wake_request() is True  # 被消费一次
    assert not target.exists()


def test_take_wake_request_without_request(isolated_home: Path) -> None:
    assert take_wake_request() is False


def test_wake_request_is_consumed_only_once(isolated_home: Path) -> None:
    request_wake()
    assert take_wake_request() is True
    assert take_wake_request() is False

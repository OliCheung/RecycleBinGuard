"""路径解析测试。

默认数据目录是**程序 / 项目目录**（便于就地查看配置与日志）；
测试用 ``RECYCLEBINGUARD_HOME`` 覆盖，避免污染真实项目目录。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from recyclebingguard import paths


def test_env_override_wins(isolated_home: Path) -> None:
    assert paths.app_home() == isolated_home
    assert paths.config_path() == isolated_home / "config.json"
    assert paths.logs_dir() == isolated_home / "logs"
    assert paths.log_file_path() == isolated_home / "logs" / "recyclebingguard.log"
    assert paths.lock_file_path().name == "RecycleBinGuard.lock"


def test_default_base_dir_is_project_dir() -> None:
    """源码运行时，数据目录就是项目根目录。"""
    project_root = Path(paths.__file__).resolve().parents[1]
    assert paths.default_base_dir() == project_root
    assert paths.default_base_dir().joinpath("recyclebingguard", "paths.py").exists()


def test_default_home_is_base_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.HOME_ENV_VAR, raising=False)
    assert paths.app_home() == paths.default_base_dir()


def test_frozen_base_dir_is_executable_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """打包后数据目录 = EXE 所在目录。"""
    exe = tmp_path / "dist" / "RecycleBinGuard.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert paths.default_base_dir() == exe.parent.resolve()


def test_runtime_paths_are_inside_home(isolated_home: Path) -> None:
    targets = paths.runtime_paths()
    assert isolated_home not in targets  # 绝不能整体删除数据目录
    assert {target.name for target in targets} == {
        "config.json",
        "clean_history.json",
        "RecycleBinGuard.lock",
        "wake.request",
        "update.signal",
        "crash.log",
        "logs",
    }


def test_ensure_app_home_creates_directory(isolated_home: Path) -> None:
    assert not isolated_home.exists()
    created = paths.ensure_app_home()
    assert created == isolated_home
    assert created.is_dir()

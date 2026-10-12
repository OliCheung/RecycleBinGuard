"""路径解析测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from recyclebingguard import paths


def test_env_override_wins(isolated_home: Path) -> None:
    assert paths.app_home() == isolated_home
    assert paths.config_path() == isolated_home / "config.json"
    assert paths.logs_dir() == isolated_home / "logs"
    assert paths.log_file_path() == isolated_home / "logs" / "recyclebingguard.log"
    assert paths.lock_file_path().name == "RecycleBinGuard.lock"


def test_local_appdata_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.HOME_ENV_VAR, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert paths.app_home() == tmp_path / "RecycleBinGuard"


def test_home_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(paths.HOME_ENV_VAR, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert paths.app_home() == Path.home() / ".RecycleBinGuard"


def test_ensure_app_home_creates_directory(isolated_home: Path) -> None:
    assert not isolated_home.exists()
    created = paths.ensure_app_home()
    assert created == isolated_home
    assert created.is_dir()

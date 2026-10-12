"""快捷方式测试。

为避免在开发者真实的桌面 / 开始菜单里创建文件，测试统一把目标目录
重定向到临时目录（``shortcut_paths()`` 依赖的目录函数被 monkeypatch）。
真实的 ``.lnk`` 创建仍然通过 PowerShell 的 ``WScript.Shell`` 完成。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from recyclebingguard import shortcuts

pytestmark = pytest.mark.skipif(os.name != "nt", reason="仅 Windows 支持快捷方式")


@pytest.fixture
def fake_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    desktop = tmp_path / "Desktop"
    start_menu = tmp_path / "StartMenu" / "Programs" / "RecycleBinGuard"
    desktop.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(shortcuts, "desktop_dir", lambda: desktop)
    monkeypatch.setattr(shortcuts, "start_menu_dir", lambda: start_menu)
    return desktop, start_menu


def test_shortcut_paths_targets_both_locations(fake_dirs) -> None:
    desktop, start_menu = fake_dirs
    paths = shortcuts.shortcut_paths()

    assert len(paths) == 2
    assert desktop / "RecycleBinGuard.lnk" in paths
    assert start_menu / "RecycleBinGuard.lnk" in paths


def test_install_creates_real_lnk_files(fake_dirs) -> None:
    desktop, start_menu = fake_dirs

    created = shortcuts.install_shortcuts()

    assert len(created) == 2
    for path in created:
        assert path.exists()
        assert path.suffix == ".lnk"
        assert path.stat().st_size > 0
    assert shortcuts.is_installed() is True


def test_install_can_skip_desktop(fake_dirs) -> None:
    desktop, _ = fake_dirs

    created = shortcuts.install_shortcuts(desktop=False)

    assert created
    assert all(path.parent != desktop for path in created)


def test_install_is_idempotent(fake_dirs) -> None:
    shortcuts.install_shortcuts()
    created = shortcuts.install_shortcuts()

    assert len(created) == 2
    assert shortcuts.is_installed() is True


def test_uninstall_removes_files(fake_dirs) -> None:
    desktop, start_menu = fake_dirs
    shortcuts.install_shortcuts()

    removed = shortcuts.uninstall_shortcuts()

    assert len(removed) == 2
    assert not (desktop / "RecycleBinGuard.lnk").exists()
    assert not (start_menu / "RecycleBinGuard.lnk").exists()
    assert shortcuts.is_installed() is False


def test_uninstall_when_absent_is_safe(fake_dirs) -> None:
    assert shortcuts.uninstall_shortcuts() == []
    assert shortcuts.is_installed() is False


def test_is_installed_is_false_by_default(fake_dirs) -> None:
    assert shortcuts.is_installed() is False


def test_launch_target_in_source_mode() -> None:
    target, arguments = shortcuts.launch_target()

    assert arguments == "-m recyclebingguard"
    assert Path(target).name.lower().startswith("python")


def test_launch_target_in_frozen_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    target, arguments = shortcuts.launch_target()

    assert target == sys.executable
    assert arguments == ""


def test_known_folder_reads_registry() -> None:
    folder = shortcuts._known_folder("Desktop")  # noqa: SLF001 - 直接验证注册表读取

    assert folder is not None
    assert folder.is_dir()

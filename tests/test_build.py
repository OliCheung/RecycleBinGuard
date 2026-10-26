"""构建脚本测试：版本读取、进程探测、运行中拦截。

不实际调用 PyInstaller（太慢且依赖外部环境），只覆盖可以安全快速验证的纯逻辑。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_build_module():
    """``build.py`` 不在包内，用 spec 方式单独加载。"""
    spec = importlib.util.spec_from_file_location("rbg_build", ROOT / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load_build_module()


def test_read_version_matches_package() -> None:
    from recyclebingguard import __version__

    assert build.read_version() == __version__


def test_read_version_is_semver_like() -> None:
    version = build.read_version()
    parts = version.split(".")
    assert len(parts) >= 2
    assert all(part.isdigit() for part in parts[:2])


def test_find_running_instances_returns_list() -> None:
    """无论有没有实例在跑，都必须返回列表而不是抛异常。"""
    found = build.find_running_instances()
    assert isinstance(found, list)
    for item in found:
        assert isinstance(item, tuple)
        assert len(item) == 2
        assert isinstance(item[0], int)


def test_ensure_target_writable_reports_running(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(build, "find_running_instances", lambda: [(1234, "RecycleBinGuard.exe")])
    assert build.ensure_target_writable() is False
    captured = capsys.readouterr()
    assert "1234" in captured.err
    assert "taskkill" in captured.err


def test_ensure_target_writable_ok_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "find_running_instances", lambda: [])
    assert build.ensure_target_writable() is True


def test_find_running_instances_parses_tasklist_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """tasklist 的 CSV 输出格式："name","pid",... —— 解析必须稳。"""
    payload = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            '"RecycleBinGuard.exe","1111","Console","1","7,008 K"\n'
            '"RecycleBinGuard.exe","2222","Console","1","17,788 K"\n'
            "INFO: No tasks are running which match the specified criteria.\n"
        ),
    )
    monkeypatch.setattr(build.os, "name", "nt")
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: payload)

    found = build.find_running_instances()

    assert found == [(1111, "RecycleBinGuard.exe"), (2222, "RecycleBinGuard.exe")]


def test_find_running_instances_ignores_non_matching_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='"OtherApp.exe","999","Console","1","1 K"\n'
    )
    monkeypatch.setattr(build.os, "name", "nt")
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: payload)

    assert build.find_running_instances() == []


def test_find_running_instances_survives_tasklist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise OSError("tasklist not found")

    monkeypatch.setattr(build.os, "name", "nt")
    monkeypatch.setattr(build.subprocess, "run", boom)

    assert build.find_running_instances() == []


def test_main_aborts_with_code_2_when_running(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """程序在跑时必须立刻退出，不能白跑一遍测试和打包。"""
    monkeypatch.setattr(build, "ensure_target_writable", lambda: False)
    called: list[list[str]] = []
    monkeypatch.setattr(build, "run", lambda step, args: called.append(args))

    assert build.main(["--skip-tests"]) == 2
    assert called == []


def test_read_version_returns_unknown_for_missing_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(build, "ROOT", tmp_path)
    (tmp_path / "recyclebingguard").mkdir()
    (tmp_path / "recyclebingguard" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    assert build.read_version() == "unknown"

"""入口脚本测试（不启动托盘、不触碰真实回收站）。"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from recyclebingguard import paths
from recyclebingguard.__main__ import (
    EXIT_ALREADY_RUNNING,
    EXIT_CONFIG_ERROR,
    _ensure_std_streams,
    _make_emit,
    _open_report,
    main,
)
from recyclebingguard.single_instance import SingleInstance


def test_open_report_none_yields_none() -> None:
    with _open_report(None) as report:
        assert report is None


def test_open_report_creates_parent_and_appends(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.txt"

    with _open_report(str(target)) as write:
        assert write is not None
        write("a")

    with _open_report(str(target)) as write:
        assert write is not None
        write("b")

    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_make_emit_without_report_prints(capsys: pytest.CaptureFixture[str]) -> None:
    _make_emit(None)("hello")
    assert "hello" in capsys.readouterr().out


def test_make_emit_with_report_writes_both(
    capsys: pytest.CaptureFixture[str],
) -> None:
    collected: list[str] = []
    _make_emit(collected.append)("hello")

    assert collected == ["hello"]
    assert "hello" in capsys.readouterr().out


def test_ensure_std_streams_fills_missing_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """打包为无控制台 GUI 程序后 stdout/stderr 为 None，必须兜底。"""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    _ensure_std_streams()

    assert sys.stdout is not None
    assert sys.stderr is not None


def test_ensure_std_streams_keeps_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = sys.stdout
    _ensure_std_streams()
    assert sys.stdout is sentinel


def test_version_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "RecycleBinGuard" in capsys.readouterr().out


def test_init_config_writes_safe_defaults(isolated_home: Path) -> None:
    assert main(["--init-config"]) == 0

    text = paths.config_path().read_text(encoding="utf-8")
    assert '"auto_clean_enabled": false' in text
    assert '"auto_clean_confirmed": false' in text
    assert '"dry_run": true' in text


def test_corrupt_config_returns_error_code(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    isolated_home.mkdir(parents=True, exist_ok=True)
    paths.config_path().write_text("{ broken", encoding="utf-8")

    assert main([]) == EXIT_CONFIG_ERROR
    assert "配置错误" in capsys.readouterr().err


def test_second_instance_wakes_existing_one(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复启动时优先“唤醒”已有实例（弹窗给它看），而不是只弹提示。"""
    warnings: list[int] = []
    monkeypatch.setattr(
        "recyclebingguard.__main__._warn_already_running", lambda: warnings.append(1)
    )

    holder = SingleInstance(paths.lock_file_path())
    assert holder.acquire()
    try:
        assert not paths.wake_request_path().exists()
        assert main([]) == EXIT_ALREADY_RUNNING
        assert paths.wake_request_path().exists()   # 留下了唤醒请求
        assert warnings == []                       # 不再弹提示
    finally:
        holder.release()


def test_second_instance_falls_back_to_dialog(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """唤醒请求写不进去时，仍然弹提示兜底。"""
    warnings: list[int] = []
    monkeypatch.setattr(
        "recyclebingguard.__main__._warn_already_running", lambda: warnings.append(1)
    )
    monkeypatch.setattr("recyclebingguard.__main__.request_wake", lambda: False)

    holder = SingleInstance(paths.lock_file_path())
    assert holder.acquire()
    try:
        assert main([]) == EXIT_ALREADY_RUNNING
        assert warnings == [1]
    finally:
        holder.release()


def test_autostart_command_is_persisted_to_config(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--autostart 必须同步写配置，否则启动时的自愈逻辑会把启动项删掉。"""
    calls: list[bool] = []
    monkeypatch.setattr(
        "recyclebingguard.autostart.set_enabled", lambda *a, **k: calls.append(a[0]) or True
    )
    monkeypatch.setattr("recyclebingguard.autostart.is_enabled", lambda *a, **k: True)

    assert main(["--autostart", "on"]) == 0
    assert calls == [True]

    saved = json.loads(paths.config_path().read_text(encoding="utf-8"))
    assert saved["start_on_login"] is True


def test_autostart_status_does_not_rewrite_config(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("recyclebingguard.autostart.is_enabled", lambda *a, **k: False)
    monkeypatch.setattr("recyclebingguard.autostart.current_command", lambda *a, **k: None)

    assert main(["--autostart", "status"]) == 0
    assert not paths.config_path().exists()


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 平台")
def test_check_once_prints_readonly_stats(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--check-once"]) == 0

    out = capsys.readouterr().out
    assert "项目总数" in out
    assert "统计完整" in out


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 平台")
def test_check_once_with_report_writes_file(
    isolated_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """打包成无控制台 GUI 后唯一能拿到输出的方式。"""
    target = tmp_path / "check.txt"

    assert main(["--check-once", "--report", str(target)]) == 0

    text = target.read_text(encoding="utf-8")
    assert "项目总数" in text
    assert "统计完整" in text
    assert "项目总数" in capsys.readouterr().out   # 有控制台时仍然打印

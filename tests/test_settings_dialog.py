"""设置对话框测试。

不启动事件循环、不显示窗口（Tk 根窗口创建后立即 withdraw）。
无图形环境时整体跳过。
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path

import pytest

from recyclebingguard.app import AppController
from recyclebingguard.config import Config
from recyclebingguard.interfaces import CleanOutcome, CleanResult, ScanResult
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor
from recyclebingguard.settings_dialog import MB, SettingsDialog


class FakeService:
    def __init__(self) -> None:
        self.result = ScanResult(total_items=0, total_size_bytes=0, scanned_roots=("C:\\",))

    def scan(self) -> ScanResult:
        return self.result

    def empty(self, *, verified_scan: ScanResult | None = None) -> CleanResult:
        return CleanResult(outcome=CleanOutcome.SUCCESS)


@pytest.fixture
def controller(tmp_path: Path):
    config = Config()
    ctl = AppController(
        config,
        service=FakeService(),  # type: ignore[arg-type]
        monitor=RecycleBinMonitor(FakeService(), config),
        config_path=tmp_path / "config.json",
        log_path=tmp_path / "recyclebingguard.log",
    )
    yield ctl
    ctl.shutdown()


@pytest.fixture
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"无可用图形环境：{exc}")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:  # pragma: no cover
        pass


def test_dialog_prefills_current_settings(controller: AppController, root: tk.Tk) -> None:
    controller.config.threshold_items = 777
    dialog = SettingsDialog(root, controller)
    try:
        collected = dialog.collect_settings()
        assert collected["threshold_items"] == 777
        assert collected["poll_interval_seconds"] == controller.config.poll_interval_seconds
        assert collected["threshold_size_bytes"] == 0
        assert collected["excluded_roots"] == []
    finally:
        dialog.close()


def test_save_applies_and_persists(controller: AppController, root: tk.Tk) -> None:
    saved: list[int] = []
    dialog = SettingsDialog(root, controller, on_saved=lambda: saved.append(1))
    try:
        dialog._var_threshold.set("20000")  # noqa: SLF001 - 直接驱动界面状态
        dialog._var_interval.set("900")  # noqa: SLF001
        dialog._var_size_mb.set("2")  # noqa: SLF001
        dialog._var_excluded.set("d:; e:")  # noqa: SLF001
        dialog.save()
    finally:
        dialog.close()

    assert controller.config.threshold_items == 20000
    assert controller.config.poll_interval_seconds == 900
    assert controller.config.threshold_size_bytes == 2 * MB
    assert controller.config.excluded_roots == ["D:\\", "E:\\"]
    assert saved == [1]


def test_invalid_input_shows_warning_and_keeps_window(
    controller: AppController, root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(
        "recyclebingguard.settings_dialog.messagebox.showwarning",
        lambda title, message, **kwargs: warnings.append(message),
    )

    dialog = SettingsDialog(root, controller)
    try:
        dialog._var_threshold.set("abc")  # noqa: SLF001
        dialog.save()
        assert warnings and "整数" in warnings[0]
        assert controller.config.threshold_items == 10000
        assert dialog.window.winfo_exists()
    finally:
        dialog.close()


def test_invalid_excluded_root_shows_error(
    controller: AppController, root: tk.Tk, monkeypatch: pytest.MonkeyPatch
) -> None:
    errors: list[str] = []
    monkeypatch.setattr(
        "recyclebingguard.settings_dialog.messagebox.showerror",
        lambda title, message, **kwargs: errors.append(message),
    )

    dialog = SettingsDialog(root, controller)
    try:
        dialog._var_excluded.set("not a drive")  # noqa: SLF001
        dialog.save()
        assert errors and "excluded_roots" in errors[0]
        assert controller.config.excluded_roots == []
    finally:
        dialog.close()


def test_dialog_must_be_created_on_main_thread(controller: AppController, root: tk.Tk) -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            SettingsDialog(root, controller)
        except BaseException as exc:  # noqa: BLE001 - 记录后断言
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)

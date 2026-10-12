"""T3 控制器测试。

全部使用 fake 服务与临时配置路径，不启动界面、不触碰真实回收站。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from recyclebingguard.app import (
    LABEL_AUTO_OFF,
    LABEL_AUTO_ON,
    LABEL_CLEAN_FAILED,
    LABEL_DRY_RUN,
    LABEL_INCOMPLETE,
    LABEL_MONITORING,
    LABEL_PAUSED,
    AppController,
    RiskConfirmationRequired,
)
from recyclebingguard.config import Config, load_config
from recyclebingguard.interfaces import (
    CleanOutcome,
    CleanResult,
    MonitorState,
    ScanResult,
)
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor


class FakeService:
    def __init__(self) -> None:
        self.result = ScanResult(total_items=0, total_size_bytes=0, scanned_roots=("C:\\",))
        self.empty_result = CleanResult(outcome=CleanOutcome.SUCCESS)
        self.empty_calls = 0

    def scan(self) -> ScanResult:
        return self.result

    def empty(self) -> CleanResult:
        self.empty_calls += 1
        return self.empty_result


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = Config()
    service = FakeService()
    monitor = RecycleBinMonitor(service, config)
    ctl = AppController(
        config,
        service=service,  # type: ignore[arg-type]
        monitor=monitor,
        config_path=tmp_path / "config.json",
        log_path=tmp_path / "recyclebingguard.log",
    )
    yield ctl
    ctl.shutdown()


# --------------------------------------------------------------------------
# 状态标签
# --------------------------------------------------------------------------


def test_default_labels(controller: AppController) -> None:
    labels = controller.state_labels()
    assert labels == [LABEL_MONITORING, LABEL_DRY_RUN, LABEL_AUTO_OFF]
    assert LABEL_MONITORING in controller.state_summary()


def test_labels_when_auto_clean_enabled(controller: AppController) -> None:
    controller.config.dry_run = False
    controller.config.auto_clean_enabled = True
    controller.monitor.config.dry_run = False
    controller.monitor.config.auto_clean_enabled = True

    labels = controller.state_labels()
    assert labels == [LABEL_MONITORING, LABEL_AUTO_ON]


def test_labels_flag_incomplete_scan(controller: AppController) -> None:
    controller.monitor.last_scan = ScanResult(
        total_items=100, total_size_bytes=0, scanned_roots=("C:\\",), failed_roots=("D:\\",)
    )
    assert LABEL_INCOMPLETE in controller.state_labels()


def test_labels_flag_clean_failure(controller: AppController) -> None:
    controller.monitor._set_state(MonitorState.ERROR)  # noqa: SLF001 - 测试需要构造错误状态
    assert LABEL_CLEAN_FAILED in controller.state_labels()


def test_labels_after_pause(controller: AppController) -> None:
    controller.monitor.pause()
    assert controller.state_labels()[0] == LABEL_PAUSED


# --------------------------------------------------------------------------
# 风险确认
# --------------------------------------------------------------------------


def test_enable_requires_risk_confirmation(controller: AppController) -> None:
    assert controller.needs_risk_confirmation() is True

    with pytest.raises(RiskConfirmationRequired):
        controller.set_auto_clean_enabled(True)

    assert controller.config.auto_clean_enabled is False
    assert controller.monitor.config.auto_clean_enabled is False
    assert not controller.config_path.exists()   # 未确认就不持久化


def test_enable_with_acknowledgement_persists(controller: AppController) -> None:
    controller.set_auto_clean_enabled(True, risk_acknowledged=True)

    assert controller.config.auto_clean_enabled is True
    assert controller.config.auto_clean_confirmed is True
    assert controller.monitor.config.auto_clean_enabled is True

    saved = json.loads(controller.config_path.read_text(encoding="utf-8"))
    assert saved["auto_clean_enabled"] is True
    assert saved["auto_clean_confirmed"] is True

    reloaded = load_config(controller.config_path)
    assert reloaded.auto_clean_enabled is True
    assert reloaded.auto_clean_confirmed is True


def test_second_enable_does_not_require_confirmation(controller: AppController) -> None:
    controller.set_auto_clean_enabled(True, risk_acknowledged=True)
    controller.set_auto_clean_enabled(False)

    assert controller.needs_risk_confirmation() is False
    controller.set_auto_clean_enabled(True)      # 不再抛异常
    assert controller.config.auto_clean_enabled is True


def test_disable_does_not_require_confirmation(controller: AppController) -> None:
    controller.set_auto_clean_enabled(False)
    assert controller.config.auto_clean_enabled is False
    assert controller.config_path.exists()       # 关闭动作应当持久化


def test_toggle_auto_clean_roundtrip(controller: AppController) -> None:
    assert controller.toggle_auto_clean(risk_acknowledged=True) is True
    assert controller.toggle_auto_clean() is False


# --------------------------------------------------------------------------
# 其它操作
# --------------------------------------------------------------------------


def test_toggle_dry_run_persists(controller: AppController) -> None:
    assert controller.toggle_dry_run() is False
    assert controller.config.dry_run is False
    saved = json.loads(controller.config_path.read_text(encoding="utf-8"))
    assert saved["dry_run"] is False


def test_toggle_hotkey_persists(controller: AppController) -> None:
    assert controller.config.hotkey_enabled is True
    assert controller.toggle_hotkey() is False
    assert controller.config.hotkey_enabled is False
    saved = json.loads(controller.config_path.read_text(encoding="utf-8"))
    assert saved["hotkey_enabled"] is False


def test_install_shortcuts_delegates(
    controller: AppController, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "recyclebingguard.shortcuts.install_shortcuts", lambda *a, **k: calls.append(1) or []
    )
    controller.install_shortcuts()
    assert calls == [1]


def test_install_shortcuts_failure_is_not_fatal(
    controller: AppController, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("no com")

    monkeypatch.setattr("recyclebingguard.shortcuts.install_shortcuts", boom)
    assert controller.install_shortcuts() == []


def test_toggle_pause(controller: AppController) -> None:
    assert controller.toggle_pause() is True
    assert controller.is_paused() is True
    assert controller.toggle_pause() is False
    assert controller.is_paused() is False


def test_request_check_runs_in_background(controller: AppController) -> None:
    controller.service.result = ScanResult(  # type: ignore[attr-defined]
        total_items=42, total_size_bytes=0, scanned_roots=("C:\\",)
    )
    done = threading.Event()
    observed: list[int] = []

    def callback(status) -> None:
        observed.append(status.last_scan.total_items if status.last_scan else -1)
        done.set()

    controller.request_check(callback)

    assert done.wait(timeout=5), "后台检查未回调"
    assert observed == [42]


def test_request_check_is_not_swallowed_on_error(controller: AppController) -> None:
    errors: list[str] = []
    controller.on_error = errors.append

    def boom() -> ScanResult:
        raise RuntimeError("scan exploded")

    controller.service.scan = boom  # type: ignore[method-assign]

    done = threading.Event()
    controller.on_status = lambda status: done.set()
    controller.request_check()
    assert done.wait(timeout=5)

    assert errors and "scan exploded" in errors[0]


def test_request_check_after_shutdown_is_noop(controller: AppController) -> None:
    controller.shutdown()
    controller.request_check()   # 不应抛异常


def test_shutdown_is_idempotent(controller: AppController) -> None:
    controller.shutdown()
    controller.shutdown()


# --------------------------------------------------------------------------
# 日志读取
# --------------------------------------------------------------------------


def test_recent_logs_reads_tail(controller: AppController) -> None:
    controller.log_path.write_text(
        "\n".join(f"line-{i}" for i in range(10)) + "\n", encoding="utf-8"
    )
    lines = controller.recent_logs(limit=3)
    assert lines == ["line-7", "line-8", "line-9"]


def test_recent_logs_missing_file(controller: AppController) -> None:
    assert controller.recent_logs() == []


# --------------------------------------------------------------------------
# 健壮性
# --------------------------------------------------------------------------


def test_persist_failure_is_not_fatal(controller: AppController, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("recyclebingguard.app.save_config", boom)

    controller.set_auto_clean_enabled(True, risk_acknowledged=True)
    assert controller.config.auto_clean_enabled is True   # 内存状态仍生效


def test_monitor_must_share_config_object(tmp_path: Path) -> None:
    config = Config()
    monitor = RecycleBinMonitor(FakeService(), Config())  # 不同的 Config 对象

    with pytest.raises(ValueError):
        AppController(config, service=FakeService(), monitor=monitor, config_path=tmp_path / "c.json")

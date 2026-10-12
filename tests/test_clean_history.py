"""清理历史测试：持久化“上一次什么时候清空过回收站”。

全部使用临时目录与 fake 服务，不触碰真实回收站，也不写入真实用户数据目录
（另见 ``conftest._isolate_clean_history``）。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from recyclebingguard.app import AppController
from recyclebingguard.clean_history import (
    MAX_RECORDS,
    CleanHistory,
    CleanRecord,
)
from recyclebingguard.config import Config
from recyclebingguard.confirmation import ConfirmationDecision, ConfirmationRequester
from recyclebingguard.interfaces import (
    CleanOutcome,
    CleanResult,
    MonitorState,
    ScanResult,
)
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor

THRESHOLD = 10000


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def ok_scan(items: int) -> ScanResult:
    return ScanResult(total_items=items, total_size_bytes=0, scanned_roots=("C:\\",))


def bad_scan(items: int) -> ScanResult:
    return ScanResult(
        total_items=items,
        total_size_bytes=0,
        scanned_roots=("C:\\", "D:\\"),
        failed_roots=("D:\\",),
    )


class FakeBinService:
    def __init__(self) -> None:
        self.default_scan: ScanResult = ok_scan(0)
        self.default_empty = CleanResult(outcome=CleanOutcome.SUCCESS)
        self.empty_calls = 0

    def scan(self) -> ScanResult:
        return self.default_scan

    def empty(self) -> CleanResult:
        self.empty_calls += 1
        return self.default_empty


class AutoRequester:
    def __init__(self, decision: ConfirmationDecision = ConfirmationDecision.CONFIRMED):
        self.decision = decision
        self.monitor: RecycleBinMonitor | None = None

    def request_clean_confirmation(self, request) -> None:
        if self.monitor is not None:
            self.monitor.resolve_confirmation(request.token, self.decision)


def make_record(
    *, minutes_ago: int = 0, outcome: str = CleanOutcome.SUCCESS.value, **kwargs
) -> CleanRecord:
    base = dict(
        finished_at=datetime.now() - timedelta(minutes=minutes_ago),
        outcome=outcome,
        trigger="auto",
        items_before=12345,
        items_after=0,
        threshold_items=THRESHOLD,
        duration_seconds=1.5,
    )
    base.update(kwargs)
    return CleanRecord(**base)


def make_monitor(tmp_path: Path, **cfg) -> tuple[RecycleBinMonitor, FakeBinService]:
    base = dict(
        threshold_items=THRESHOLD,
        poll_interval_seconds=180,
        auto_clean_enabled=True,
        dry_run=False,
        confirm_before_clean=False,
    )
    base.update(cfg)
    service = FakeBinService()
    monitor = RecycleBinMonitor(
        service,
        Config(**base),
        history=CleanHistory(tmp_path / "clean_history.json"),
    )
    return monitor, service


# --------------------------------------------------------------------------
# 一、CleanRecord
# --------------------------------------------------------------------------


def test_record_roundtrip() -> None:
    record = make_record(trigger="confirmed", error=None)
    restored = CleanRecord.from_dict(record.to_dict())
    assert restored.outcome == record.outcome
    assert restored.trigger == "confirmed"
    assert restored.items_before == 12345
    # 序列化精度到秒（isoformat(timespec="seconds")），微秒被截断属预期
    assert restored.finished_at == record.finished_at.replace(microsecond=0)
    assert restored.succeeded is True


def test_record_succeeded_only_for_success() -> None:
    assert make_record().succeeded is True
    for outcome in ("partial", "failed", "incomplete"):
        assert make_record(outcome=outcome).succeeded is False


def test_record_describe_contains_time_and_counts() -> None:
    text = make_record().describe()
    assert "成功" in text
    assert "12,345" in text
    assert make_record(outcome="failed").describe().startswith(tuple("0123456789"))


def test_record_bad_timestamp_is_rejected() -> None:
    with pytest.raises(Exception):
        CleanRecord.from_dict({"finished_at": "not-a-time"})


# --------------------------------------------------------------------------
# 二、CleanHistory 读写
# --------------------------------------------------------------------------


def test_empty_history(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    assert history.load() == []
    assert history.last() is None
    assert history.last_success() is None


def test_append_and_reload(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    assert history.append(make_record()) is True

    records = history.load()
    assert len(records) == 1
    assert records[0].items_before == 12345
    assert history.last() is not None
    assert history.last_success() is not None


def test_records_are_sorted_newest_first(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    history.append(make_record(minutes_ago=30))
    history.append(make_record(minutes_ago=5))

    records = history.load()
    assert [r.finished_at for r in records] == sorted(
        (r.finished_at for r in records), reverse=True
    )
    assert history.last().finished_at == records[0].finished_at


def test_history_is_capped(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    for index in range(MAX_RECORDS + 12):
        history.append(make_record(minutes_ago=MAX_RECORDS + 12 - index))

    records = history.load()
    assert len(records) == MAX_RECORDS
    # 保留的是最新的那些
    assert records[0].finished_at >= records[-1].finished_at


def test_last_success_skips_failures(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    history.append(make_record(minutes_ago=10, outcome="success"))
    history.append(make_record(minutes_ago=1, outcome="failed", error="模拟失败"))

    assert history.last().outcome == "failed"
    assert history.last_success().outcome == "success"


def test_last_success_is_none_when_never_succeeded(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    history.append(make_record(outcome="partial"))
    assert history.last_success() is None


def test_corrupt_file_is_treated_as_empty(tmp_path: Path) -> None:
    target = tmp_path / "h.json"
    target.write_text("{ not json", encoding="utf-8")
    history = CleanHistory(target)

    assert history.load() == []
    assert history.last() is None
    assert history.append(make_record()) is True   # 仍可自愈写入
    assert len(history.load()) == 1


def test_invalid_root_is_treated_as_empty(tmp_path: Path) -> None:
    target = tmp_path / "h.json"
    target.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert CleanHistory(target).load() == []


def test_invalid_single_record_is_skipped(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    history.append(make_record())
    raw = json.loads(history.path.read_text(encoding="utf-8"))
    raw["records"].insert(0, {"finished_at": "bad"})
    history.path.write_text(json.dumps(raw), encoding="utf-8")

    records = history.load()
    assert len(records) == 1


def test_write_failure_is_not_fatal(tmp_path: Path) -> None:
    """历史文件写不进去时只记日志，绝不抛异常打断清理流程。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file", encoding="utf-8")

    history = CleanHistory(blocker / "nested" / "h.json")
    assert history.append(make_record()) is False


def test_clear_removes_file(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    history.append(make_record())
    history.clear()
    assert not history.path.exists()


def test_history_file_is_json_with_version(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    history.append(make_record())
    raw = json.loads(history.path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert isinstance(raw["records"], list)
    assert "finished_at" in raw["records"][0]


# --------------------------------------------------------------------------
# 三、监控器集成
# --------------------------------------------------------------------------


def test_successful_clean_is_recorded(tmp_path: Path) -> None:
    monitor, service = make_monitor(tmp_path)
    service.default_scan = ok_scan(10000)
    service.default_empty = CleanResult(
        outcome=CleanOutcome.SUCCESS, items_before=10000, items_after=0
    )

    monitor.tick()

    record = monitor.last_clean_record()
    assert record is not None
    assert record.succeeded is True
    assert record.items_before == 10000
    assert record.threshold_items == THRESHOLD
    assert monitor.status().last_clean_at == record.finished_at


def test_failed_clean_is_also_recorded(tmp_path: Path) -> None:
    monitor, service = make_monitor(tmp_path)
    service.default_scan = ok_scan(10000)
    service.default_empty = CleanResult(
        outcome=CleanOutcome.FAILED, items_before=10000, items_after=800, error="模拟失败"
    )

    monitor.tick()

    record = monitor.last_clean_record()
    assert record is not None
    assert record.outcome == "failed"
    assert record.error == "模拟失败"
    assert monitor.last_successful_clean() is None


def test_exception_during_clean_is_recorded(tmp_path: Path) -> None:
    monitor, service = make_monitor(tmp_path)
    service.default_scan = ok_scan(10000)

    def boom() -> CleanResult:
        raise RuntimeError("explode")

    service.empty = boom  # type: ignore[method-assign]

    monitor.tick()

    record = monitor.last_clean_record()
    assert record is not None
    assert record.outcome == "failed"
    assert "explode" in (record.error or "")


def test_dry_run_is_not_recorded(tmp_path: Path) -> None:
    monitor, service = make_monitor(tmp_path, dry_run=True)
    service.default_scan = ok_scan(50000)

    monitor.tick()

    assert service.empty_calls == 0
    assert monitor.last_clean_record() is None


def test_cancelled_confirmation_is_not_recorded(tmp_path: Path) -> None:
    service = FakeBinService()
    service.default_scan = ok_scan(10000)
    requester = AutoRequester(ConfirmationDecision.CANCELLED)
    monitor = RecycleBinMonitor(
        service,
        Config(threshold_items=THRESHOLD, auto_clean_enabled=True, dry_run=False),
        history=CleanHistory(tmp_path / "clean_history.json"),
        confirmation_requester=requester,
    )
    requester.monitor = monitor

    monitor.tick()

    assert service.empty_calls == 0
    assert monitor.last_clean_record() is None


def test_confirmed_clean_records_trigger(tmp_path: Path) -> None:
    service = FakeBinService()
    service.default_scan = ok_scan(10000)
    requester = AutoRequester(ConfirmationDecision.CONFIRMED)
    monitor = RecycleBinMonitor(
        service,
        Config(threshold_items=THRESHOLD, auto_clean_enabled=True, dry_run=False),
        history=CleanHistory(tmp_path / "clean_history.json"),
        confirmation_requester=requester,
    )
    requester.monitor = monitor

    monitor.tick()

    record = monitor.last_clean_record()
    assert record is not None
    assert record.trigger == "confirmed"


def test_timeout_clean_records_timeout_trigger(tmp_path: Path) -> None:
    service = FakeBinService()
    service.default_scan = ok_scan(10000)
    requester = AutoRequester(ConfirmationDecision.TIMEOUT)
    monitor = RecycleBinMonitor(
        service,
        Config(threshold_items=THRESHOLD, auto_clean_enabled=True, dry_run=False),
        history=CleanHistory(tmp_path / "clean_history.json"),
        confirmation_requester=requester,
    )
    requester.monitor = monitor

    monitor.tick()

    assert monitor.last_clean_record().trigger == "timeout"


def test_recheck_failure_is_not_recorded(tmp_path: Path) -> None:
    """复核未通过 = 没有调用清空 API = 不写历史。"""
    service = FakeBinService()
    calls = {"n": 0}

    def scan() -> ScanResult:
        calls["n"] += 1
        return ok_scan(10000) if calls["n"] == 1 else bad_scan(10000)

    service.scan = scan  # type: ignore[method-assign]
    requester = AutoRequester(ConfirmationDecision.CONFIRMED)
    monitor = RecycleBinMonitor(
        service,
        Config(threshold_items=THRESHOLD, auto_clean_enabled=True, dry_run=False),
        history=CleanHistory(tmp_path / "clean_history.json"),
        confirmation_requester=requester,
    )
    requester.monitor = monitor

    monitor.tick()

    assert service.empty_calls == 0
    assert monitor.last_clean_record() is None


def test_history_survives_restart(tmp_path: Path) -> None:
    """模拟重启：新的监控器实例能从磁盘读回上次清理时间。"""
    target = tmp_path / "clean_history.json"
    monitor, service = make_monitor(tmp_path)
    service.default_scan = ok_scan(10000)
    monitor.tick()
    first = monitor.last_clean_record()
    assert first is not None

    restarted = RecycleBinMonitor(FakeBinService(), Config(), history=CleanHistory(target))
    record = restarted.last_clean_record()

    assert record is not None
    assert record.finished_at == first.finished_at.replace(microsecond=0)
    assert record.items_before == first.items_before


def test_history_path_defaults_to_app_home(monkeypatch) -> None:
    called: list[Path] = []
    monkeypatch.setattr(
        "recyclebingguard.paths.clean_history_path",
        lambda: called.append(Path("x")) or Path("x"),
    )
    CleanHistory()
    assert called == [Path("x")]


def test_concurrent_appends_are_serialized(tmp_path: Path) -> None:
    history = CleanHistory(tmp_path / "h.json")
    errors: list[BaseException] = []

    def write(index: int) -> None:
        try:
            history.append(make_record(minutes_ago=index))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert errors == []
    assert len(history.load()) == 8


# --------------------------------------------------------------------------
# 四、控制器与界面
# --------------------------------------------------------------------------


def _controller(tmp_path: Path) -> tuple[AppController, FakeBinService, CleanHistory]:
    config = Config(
        threshold_items=THRESHOLD,
        auto_clean_enabled=True,
        dry_run=False,
        confirm_before_clean=False,
    )
    service = FakeBinService()
    history = CleanHistory(tmp_path / "clean_history.json")
    monitor = RecycleBinMonitor(service, config, history=history)
    ctl = AppController(
        config,
        service=service,  # type: ignore[arg-type]
        monitor=monitor,
        config_path=tmp_path / "config.json",
        log_path=tmp_path / "recyclebingguard.log",
    )
    return ctl, service, history


def test_controller_summary_without_records(tmp_path: Path) -> None:
    ctl, _, _ = _controller(tmp_path)
    assert ctl.last_clean_summary() == "尚无清理记录"
    ctl.shutdown()


def test_controller_summary_after_success(tmp_path: Path) -> None:
    ctl, service, _ = _controller(tmp_path)
    service.default_scan = ok_scan(10000)
    service.default_empty = CleanResult(
        outcome=CleanOutcome.SUCCESS, items_before=10000, items_after=0
    )

    ctl.monitor.tick()

    assert "成功" in ctl.last_clean_summary()
    assert ctl.last_successful_clean() is not None
    ctl.shutdown()


def test_controller_summary_shows_last_success_when_failed(tmp_path: Path) -> None:
    ctl, service, history = _controller(tmp_path)
    history.append(make_record(minutes_ago=60, outcome="success"))
    ctl.monitor._history_loaded = False  # noqa: SLF001 - 强制从磁盘重新加载

    service.default_scan = ok_scan(10000)
    service.default_empty = CleanResult(outcome=CleanOutcome.FAILED, error="模拟失败")
    ctl.monitor.tick()

    assert "失败" in ctl.last_clean_summary()
    assert "上次成功" in ctl.last_clean_summary()
    ctl.shutdown()


def test_controller_summary_when_never_succeeded(tmp_path: Path) -> None:
    ctl, service, _ = _controller(tmp_path)
    service.default_scan = ok_scan(10000)
    service.default_empty = CleanResult(outcome=CleanOutcome.PARTIAL, items_after=5)

    ctl.monitor.tick()

    assert "从未成功清理过" in ctl.last_clean_summary()
    ctl.shutdown()


def test_controller_recent_cleans(tmp_path: Path) -> None:
    ctl, _, history = _controller(tmp_path)
    for index in range(3):
        history.append(make_record(minutes_ago=index))

    assert len(ctl.recent_cleans(limit=2)) == 2
    ctl.shutdown()


def test_monitor_state_recovers_after_failure(tmp_path: Path) -> None:
    """清理失败写入历史后，状态机仍回到可继续监控的状态。"""
    monitor, service = make_monitor(tmp_path)
    service.default_scan = ok_scan(10000)
    service.default_empty = CleanResult(outcome=CleanOutcome.FAILED, error="模拟失败")

    monitor.tick()

    assert monitor.state is MonitorState.ERROR
    assert monitor.last_clean_record() is not None

"""T2 监控器测试。

全部使用 fake 服务与假时钟，不触碰真实回收站。线程相关用例通过事件同步，
不使用固定 ``sleep`` 断言，避免脆弱。
"""

from __future__ import annotations

import threading
import time

import pytest

from recyclebingguard.config import Config
from recyclebingguard.interfaces import (
    CleanOutcome,
    CleanResult,
    MonitorState,
    ScanResult,
)
from recyclebingguard.interfaces import (
    RecycleBinMonitor as RecycleBinMonitorProtocol,
)
from recyclebingguard.recycle_bin_monitor import MonitorStatus, RecycleBinMonitor

THRESHOLD = 10000
BACKOFF = 300


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def ok_scan(items: int, roots: tuple[str, ...] = ("C:\\",)) -> ScanResult:
    return ScanResult(total_items=items, total_size_bytes=0, scanned_roots=roots)


def bad_scan(items: int, failed: tuple[str, ...] = ("D:\\",)) -> ScanResult:
    return ScanResult(
        total_items=items,
        total_size_bytes=0,
        scanned_roots=("C:\\", "D:\\"),
        failed_roots=failed,
    )


def clean(outcome: CleanOutcome, remaining: int | None = None) -> CleanResult:
    return CleanResult(
        outcome=outcome,
        remaining_items=remaining,
        error=None if outcome is CleanOutcome.SUCCESS else "模拟失败",
    )


class FakeBinService:
    def __init__(self) -> None:
        self.scan_queue: list[ScanResult] = []
        self.default_scan: ScanResult = ok_scan(0)
        self.empty_queue: list[CleanResult] = []
        self.default_empty: CleanResult = CleanResult(outcome=CleanOutcome.SUCCESS)
        self.scan_calls = 0
        self.empty_calls = 0
        self.empty_verified_scan: ScanResult | None = None
        self.empty_gate: threading.Event | None = None
        self.empty_started = threading.Event()

    def scan(self) -> ScanResult:
        self.scan_calls += 1
        if self.scan_queue:
            return self.scan_queue.pop(0)
        return self.default_scan

    def empty(self, *, verified_scan: ScanResult | None = None) -> CleanResult:
        self.empty_calls += 1
        self.empty_verified_scan = verified_scan
        self.empty_started.set()
        if self.empty_gate is not None:
            self.empty_gate.wait(timeout=5)
        if self.empty_queue:
            return self.empty_queue.pop(0)
        return self.default_empty


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_config(**overrides) -> Config:
    # confirm_before_clean=False：T2 原有的“阈值 -> 复核 -> 清理”语义保持不变；
    # 清理前确认弹窗的用例集中在 tests/test_confirmation.py。
    base = dict(
        threshold_items=THRESHOLD,
        poll_interval_seconds=180,
        auto_clean_enabled=True,
        confirm_before_clean=False,
        failure_backoff_seconds=BACKOFF,
    )
    base.update(overrides)
    return Config(**base)


def make_monitor(service: FakeBinService | None = None, clock: FakeClock | None = None, **cfg):
    service = service or FakeBinService()
    clock = clock or FakeClock()
    monitor = RecycleBinMonitor(service, make_config(**cfg), clock=clock)
    return monitor, service, clock


# --------------------------------------------------------------------------
# 阈值边界
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("items", "should_clean"),
    [(9999, False), (10000, True), (10001, True)],
)
def test_threshold_boundaries(items: int, should_clean: bool) -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(items)

    monitor.tick()

    assert service.empty_calls == (1 if should_clean else 0)
    assert monitor.status().threshold_hits == (1 if should_clean else 0)


def test_second_check_below_threshold_cancels_clean() -> None:
    monitor, service, _ = make_monitor()
    service.scan_queue = [ok_scan(10000), ok_scan(9999)]

    monitor.tick()

    assert service.empty_calls == 0
    assert service.scan_calls == 2
    assert monitor.state is MonitorState.IDLE


def test_recheck_result_is_reused_by_clean() -> None:
    """复核结果直接交给清空流程，避免清空前再扫一遍。"""
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(10000)

    monitor.tick()

    assert service.empty_calls == 1
    assert service.empty_verified_scan is not None
    assert service.empty_verified_scan.total_items == 10000


def test_size_threshold_triggers_without_item_threshold() -> None:
    monitor, service, _ = make_monitor(threshold_size_bytes=5 * 1024**3)
    service.default_scan = ScanResult(
        total_items=1, total_size_bytes=5 * 1024**3, scanned_roots=("C:\\",)
    )

    monitor.tick()

    assert service.empty_calls == 1
    assert monitor.status().threshold_hits == 1


def test_seconds_until_next_check_counts_down() -> None:
    monitor, service, clock = make_monitor()
    service.default_scan = ok_scan(0)

    assert monitor.seconds_until_next_check() == 180  # 尚未检查

    monitor.tick()
    assert monitor.seconds_until_next_check() == 180  # 刚检查完

    clock.advance(60)
    assert monitor.seconds_until_next_check() == 120


def test_seconds_until_next_check_reports_backoff() -> None:
    monitor, service, clock = make_monitor()
    service.default_scan = ok_scan(10000)
    service.default_empty = CleanResult(outcome=CleanOutcome.FAILED, error="模拟失败")

    monitor.tick()  # 清理失败 -> 进入退避

    assert monitor.seconds_until_next_check() == BACKOFF
    clock.advance(100)
    assert monitor.seconds_until_next_check() == BACKOFF - 100


def test_second_check_incomplete_cancels_clean() -> None:
    monitor, service, _ = make_monitor()
    service.scan_queue = [ok_scan(10000), bad_scan(10000)]

    monitor.tick()

    assert service.empty_calls == 0


# --------------------------------------------------------------------------
# 统计不完整
# --------------------------------------------------------------------------


def test_incomplete_scan_does_not_trigger() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = bad_scan(50000)

    monitor.tick()

    assert service.empty_calls == 0
    assert monitor.state is MonitorState.IDLE
    assert monitor.status().threshold_hits == 0


def test_scan_error_does_not_trigger() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ScanResult(
        total_items=50000, total_size_bytes=0, error="未枚举到固定磁盘"
    )

    monitor.tick()

    assert service.empty_calls == 0


# --------------------------------------------------------------------------
# 安全开关
# --------------------------------------------------------------------------


def test_auto_clean_disabled_only_records() -> None:
    monitor, service, _ = make_monitor(auto_clean_enabled=False)
    service.default_scan = ok_scan(50000)

    monitor.tick()

    assert service.empty_calls == 0
    assert monitor.status().threshold_hits == 1
    assert monitor.state is MonitorState.IDLE


def test_disabling_auto_clean_before_tick_blocks_clean() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(50000)
    monitor.set_auto_clean_enabled(False)

    monitor.tick()

    assert service.empty_calls == 0


def test_disabling_auto_clean_during_recheck_blocks_clean() -> None:
    """用户在阈值判定之后、真正清空之前关闭自动清理，仍必须被拦住。"""
    monitor, service, _ = make_monitor()
    calls = {"n": 0}

    def scan() -> ScanResult:
        calls["n"] += 1
        if calls["n"] == 2:  # 二次核验这一轮，用户关闭了自动清理
            monitor.set_auto_clean_enabled(False)
        return ok_scan(50000)

    service.scan = scan  # type: ignore[method-assign]

    monitor.tick()

    assert service.empty_calls == 0


def test_pause_prevents_checks_and_cleaning() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(50000)

    monitor.pause()
    state = monitor.tick()

    assert state is MonitorState.PAUSED
    assert service.scan_calls == 0  # 暂停时连统计都不做
    assert service.empty_calls == 0

    monitor.resume()
    assert monitor.state is MonitorState.IDLE


# --------------------------------------------------------------------------
# 并发保护
# --------------------------------------------------------------------------


def test_no_concurrent_clean() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(50000)
    service.empty_gate = threading.Event()

    first = threading.Thread(target=monitor.tick, name="clean-1")
    first.start()
    assert service.empty_started.wait(timeout=5), "首个清理未开始"

    second = threading.Thread(target=monitor.tick, name="clean-2")
    second.start()
    second.join(timeout=5)
    assert not second.is_alive()

    service.empty_gate.set()
    first.join(timeout=5)
    assert not first.is_alive()

    assert service.empty_calls == 1


# --------------------------------------------------------------------------
# 失败退避
# --------------------------------------------------------------------------


def test_failure_backoff_prevents_immediate_retry() -> None:
    monitor, service, clock = make_monitor()
    service.default_scan = ok_scan(50000)
    service.default_empty = clean(CleanOutcome.FAILED)

    monitor.tick()
    assert service.empty_calls == 1
    assert monitor.state is MonitorState.ERROR
    assert monitor.status().backoff_remaining_seconds == pytest.approx(BACKOFF)

    # 退避期内：不重试
    monitor.tick()
    assert service.empty_calls == 1

    clock.advance(BACKOFF - 1)
    monitor.tick()
    assert service.empty_calls == 1

    # 退避结束后：允许重试
    clock.advance(1)
    monitor.tick()
    assert service.empty_calls == 2


def test_partial_outcome_is_treated_as_failure() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(50000)
    service.default_empty = clean(CleanOutcome.PARTIAL, remaining=123)

    monitor.tick()

    assert monitor.state is MonitorState.ERROR
    assert monitor.status().clean_failure_count == 1
    assert "partial" in (monitor.status().last_error or "")


def test_incomplete_outcome_is_treated_as_failure() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(50000)
    service.default_empty = clean(CleanOutcome.INCOMPLETE)

    monitor.tick()

    assert monitor.state is MonitorState.ERROR
    assert monitor.status().clean_failure_count == 1


def test_service_exception_is_not_swallowed() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(50000)

    def boom(*args: object, **kwargs: object) -> CleanResult:
        raise RuntimeError("explode")

    service.empty = boom  # type: ignore[method-assign]

    state = monitor.tick()

    assert state is MonitorState.ERROR
    assert "explode" in (monitor.status().last_error or "")
    assert monitor.status().clean_failure_count == 1


def test_no_infinite_retry_in_single_round() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(50000)
    service.default_empty = clean(CleanOutcome.FAILED)

    monitor.tick()

    assert service.empty_calls == 1  # 同一轮只清空一次


# --------------------------------------------------------------------------
# 成功后重新统计
# --------------------------------------------------------------------------


def test_success_rescans_and_records_result() -> None:
    monitor, service, _ = make_monitor()
    service.scan_queue = [ok_scan(50000), ok_scan(50000), ok_scan(0)]
    service.default_empty = clean(CleanOutcome.SUCCESS)

    monitor.tick()

    status = monitor.status()
    assert status.state is MonitorState.IDLE
    assert status.clean_success_count == 1
    assert status.last_clean_result is not None
    assert status.last_clean_result.outcome is CleanOutcome.SUCCESS
    assert status.last_scan is not None
    assert status.last_scan.total_items == 0  # 清理后已重新统计
    assert status.last_error is None


def test_state_transitions_are_observed() -> None:
    monitor, service, _ = make_monitor()
    service.scan_queue = [ok_scan(50000), ok_scan(50000), ok_scan(0)]

    seen: list[MonitorState] = []
    original = monitor._set_state  # noqa: SLF001 - 测试需要观察状态迁移

    def spy(state: MonitorState) -> None:
        seen.append(state)
        original(state)

    monitor._set_state = spy  # type: ignore[method-assign]
    monitor.tick()

    assert seen == [
        MonitorState.CHECKING,
        MonitorState.THRESHOLD_REACHED,
        MonitorState.CLEANING,
        MonitorState.VERIFYING,
        MonitorState.IDLE,
    ]


# --------------------------------------------------------------------------
# 线程生命周期
# --------------------------------------------------------------------------


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """轮询等待条件成立，超时返回 ``False``（不使用固定 sleep 断言）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_start_runs_checks_and_stop_cleans_up() -> None:
    monitor, service, _ = make_monitor(poll_interval_seconds=1)
    service.default_scan = ok_scan(0)

    monitor.start()
    assert _wait_until(lambda: service.scan_calls >= 1), "后台线程未执行检查"

    monitor.stop()
    assert monitor.status().scan_count >= 1
    assert monitor.state is MonitorState.IDLE
    assert monitor._thread is None  # noqa: SLF001 - 断言线程已回收


def test_start_is_idempotent() -> None:
    monitor, service, _ = make_monitor(poll_interval_seconds=1)
    service.default_scan = ok_scan(0)

    monitor.start()
    thread = monitor._thread  # noqa: SLF001
    monitor.start()  # 重复启动不应新建线程
    assert monitor._thread is thread  # noqa: SLF001

    monitor.stop()


def test_stop_can_be_called_twice() -> None:
    monitor, service, _ = make_monitor(poll_interval_seconds=1)
    monitor.start()
    monitor.stop()
    monitor.stop()
    assert monitor._thread is None  # noqa: SLF001


def test_check_now_returns_latest_scan() -> None:
    monitor, service, _ = make_monitor()
    service.default_scan = ok_scan(123)

    result = monitor.check_now()

    assert result is not None
    assert result.total_items == 123
    assert service.scan_calls == 1


def test_thread_exception_does_not_kill_monitor() -> None:
    monitor, service, _ = make_monitor(poll_interval_seconds=1)

    def boom() -> ScanResult:
        raise RuntimeError("scan failed")

    service.scan = boom  # type: ignore[method-assign]

    monitor.start()
    assert _wait_until(lambda: monitor.state is MonitorState.ERROR), "异常未反映到状态"
    monitor.stop()
    assert monitor._thread is None  # noqa: SLF001


# --------------------------------------------------------------------------
# 状态快照 / 协议
# --------------------------------------------------------------------------


def test_status_snapshot_defaults() -> None:
    monitor, _, _ = make_monitor()
    status = monitor.status()

    assert isinstance(status, MonitorStatus)
    assert status.state is MonitorState.IDLE
    assert status.auto_clean_enabled is True
    assert status.interval_seconds == 180
    assert status.last_scan is None
    assert status.last_clean_result is None
    assert status.backoff_remaining_seconds == 0.0
    assert status.is_running is True


def test_paused_status_is_not_running() -> None:
    monitor, _, _ = make_monitor()
    monitor.pause()
    assert monitor.status().is_running is False


def test_monitor_satisfies_protocol() -> None:
    monitor, _, _ = make_monitor()
    assert isinstance(monitor, RecycleBinMonitorProtocol)


def test_describe_state_in_chinese() -> None:
    monitor, _, _ = make_monitor()
    assert monitor.describe_state() == "正在监控"
    monitor.pause()
    assert monitor.describe_state() == "已暂停"

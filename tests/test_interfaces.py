"""接口与数据类型测试，重点覆盖安全语义。"""

from __future__ import annotations

from datetime import datetime

import pytest

from recyclebingguard.interfaces import (
    INCLUDED_DRIVE_KINDS,
    CleanOutcome,
    CleanResult,
    DiskScanResult,
    DriveKind,
    MonitorState,
    RecycleBinMonitor,
    RecycleBinService,
    ScanResult,
    normalize_root,
)


def _scan(total: int, failed: tuple[str, ...] = ()) -> ScanResult:
    return ScanResult(
        total_items=total,
        total_size_bytes=0,
        scanned_roots=("C:\\",),
        failed_roots=failed,
    )


def test_only_fixed_drives_are_included_in_v1() -> None:
    assert INCLUDED_DRIVE_KINDS == {DriveKind.FIXED}
    assert DriveKind.NETWORK not in INCLUDED_DRIVE_KINDS
    assert DriveKind.REMOVABLE not in INCLUDED_DRIVE_KINDS


def test_scan_completeness() -> None:
    assert _scan(0).is_complete is True
    assert _scan(0, failed=("D:\\",)).is_complete is False


@pytest.mark.parametrize(
    ("total", "expected"),
    [(9999, False), (10000, True), (10001, True), (0, False)],
)
def test_threshold_boundaries(total: int, expected: bool) -> None:
    assert _scan(total).should_trigger(10000) is expected


def test_incomplete_scan_never_triggers() -> None:
    result = _scan(50000, failed=("D:\\",))
    assert result.is_complete is False
    assert result.should_trigger(10000) is False


def test_scan_level_error_blocks_trigger() -> None:
    result = ScanResult(total_items=50000, total_size_bytes=0, error="未枚举到固定磁盘")
    assert result.is_complete is False
    assert result.should_trigger(10000) is False


def test_size_threshold_is_an_independent_trigger() -> None:
    """占用大小阈值是第二条触发条件（``0`` = 不启用）。"""
    result = ScanResult(total_items=10, total_size_bytes=5 * 1024**3, scanned_roots=("C:\\",))

    assert result.should_trigger(10000, 0) is False
    assert result.should_trigger(10000, 5 * 1024**3) is True
    assert result.should_trigger(5, 0) is True


def test_size_threshold_is_ignored_when_zero() -> None:
    result = ScanResult(total_items=1, total_size_bytes=10**12, scanned_roots=("C:\\",))
    assert result.should_trigger(2, 0) is False


def test_incomplete_scan_never_triggers_on_size() -> None:
    result = ScanResult(
        total_items=1,
        total_size_bytes=10**12,
        scanned_roots=("C:\\",),
        failed_roots=("D:\\",),
    )
    assert result.should_trigger(1, 1) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("d:", "D:\\"),
        ("D:/", "D:\\"),
        ("D:\\", "D:\\"),
        (" c: ", "C:\\"),
        ("", ""),
        ("not a drive", ""),
        ("\\\\server\\share", ""),
        ("1:", ""),
    ],
)
def test_normalize_root(raw: str, expected: str) -> None:
    assert normalize_root(raw) == expected


def test_scan_result_defaults() -> None:
    result = ScanResult(total_items=0, total_size_bytes=0)
    assert result.scanned_roots == ()
    assert result.failed_roots == ()
    assert isinstance(result.queried_at, datetime)
    assert result.is_complete is True


def test_disk_scan_result_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="num_items"):
        DiskScanResult(root="C:\\", ok=True, num_items=-1)
    with pytest.raises(ValueError, match="size_bytes"):
        DiskScanResult(root="C:\\", ok=True, size_bytes=-1)


def test_disk_scan_result_allows_negative_when_not_ok() -> None:
    # 未成功时不做非负校验，便于记录原始错误值。
    DiskScanResult(root="C:\\", ok=False, num_items=-1, hresult=-1)


def test_clean_result_succeeded_only_for_success() -> None:
    assert CleanResult(CleanOutcome.SUCCESS).succeeded is True
    for outcome in (
        CleanOutcome.PARTIAL,
        CleanOutcome.FAILED,
        CleanOutcome.INCOMPLETE,
    ):
        assert CleanResult(outcome).succeeded is False


def test_monitor_state_members() -> None:
    assert {s.name for s in MonitorState} == {
        "IDLE",
        "CHECKING",
        "THRESHOLD_REACHED",
        "AWAITING_CONFIRMATION",
        "CLEANING",
        "VERIFYING",
        "ERROR",
        "PAUSED",
    }


def test_protocols_are_runtime_checkable() -> None:
    class FakeService:
        def scan(self) -> ScanResult:  # pragma: no cover - 仅用于结构检查
            raise NotImplementedError

        def empty(
            self, *, verified_scan: ScanResult | None = None
        ) -> CleanResult:  # pragma: no cover
            raise NotImplementedError

    class FakeMonitor:
        state = MonitorState.IDLE

        def start(self) -> None: ...
        def stop(self, timeout: float | None = None) -> None: ...
        def pause(self) -> None: ...
        def resume(self) -> None: ...
        def check_now(self) -> ScanResult:  # pragma: no cover
            raise NotImplementedError

    assert isinstance(FakeService(), RecycleBinService)
    assert isinstance(FakeMonitor(), RecycleBinMonitor)
    assert not isinstance(object(), RecycleBinService)

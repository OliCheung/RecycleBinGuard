"""T1 核心服务测试。

原则：

- 覆盖正常返回、异常计数、API 错误、部分磁盘失败、清空失败等路径；
- 默认全部使用 :class:`FakeShellApi` 隔离 Windows API；
- 真实 API 仅做**只读**查询验证（``test_real_*``），绝不执行清空。
"""

from __future__ import annotations

import ctypes
import os

import pytest

from recyclebingguard.interfaces import CleanOutcome, DriveKind
from recyclebingguard.recycle_bin_service import (
    DEFAULT_EMPTY_FLAGS,
    EXPECTED_SHQUERYRBINFO_SIZE,
    SHERB_NOCONFIRMATION,
    SHERB_NOPROGRESSUI,
    SHERB_NOSOUND,
    SHQUERYRBINFO,
    RawQuery,
    RecycleBinService,
    ShellApi,
    hresult_message,
    hresult_ok,
)

E_FAIL = -2147467259            # 0x80004005
E_UNEXPECTED = -2147418113      # 0x8000FFFF
E_ACCESSDENIED = -2147024891    # 0x80070005


class FakeShellApi:
    """可配置的 :class:`ShellApi` 替身，记录所有调用。"""

    def __init__(self, drives: list[tuple[str, DriveKind]]) -> None:
        self.drives = list(drives)
        self.counts: dict[str, RawQuery] = {}
        self.empty_results: dict[str | None, int] = {}
        default_empty = 0
        self._default_empty = default_empty
        self.on_empty = None  # 可选回调：callable(root) -> None，用于模拟清空后的状态
        self.query_calls: list[str] = []
        self.empty_calls: list[tuple[str | None, int]] = []

    def set_count(self, root: str, num_items: int, size_bytes: int = 0) -> None:
        self.counts[root] = RawQuery(hresult=0, num_items=num_items, size_bytes=size_bytes)

    def set_error(self, root: str, hresult: int) -> None:
        self.counts[root] = RawQuery(hresult=hresult)

    def set_empty_result(self, root: str | None, hresult: int) -> None:
        self.empty_results[root] = hresult

    # -- ShellApi --

    def iter_drives(self) -> list[tuple[str, DriveKind]]:
        return list(self.drives)

    def query(self, root: str) -> RawQuery:
        self.query_calls.append(root)
        return self.counts.get(root, RawQuery(hresult=0))

    def empty(self, root: str | None, flags: int) -> int:
        self.empty_calls.append((root, flags))
        if self.on_empty is not None:
            self.on_empty(root)
        return self.empty_results.get(root, self._default_empty)


def make_service(api: FakeShellApi, **kwargs) -> RecycleBinService:
    return RecycleBinService(api, **kwargs)


# --------------------------------------------------------------------------
# 常量 / 结构体
# --------------------------------------------------------------------------


def test_default_empty_flags_only_disable_confirmation_and_progress() -> None:
    assert DEFAULT_EMPTY_FLAGS == SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI
    assert DEFAULT_EMPTY_FLAGS == 0x3
    assert not DEFAULT_EMPTY_FLAGS & SHERB_NOSOUND


def test_shqueryrbinfo_layout_matches_windows_header() -> None:
    assert ctypes.sizeof(SHQUERYRBINFO) == EXPECTED_SHQUERYRBINFO_SIZE == 24
    assert SHQUERYRBINFO.cbSize.offset == 0
    assert SHQUERYRBINFO.i64Size.offset == 8
    assert SHQUERYRBINFO.i64NumItems.offset == 16


def test_hresult_helpers() -> None:
    assert hresult_ok(0)
    assert hresult_ok(1)  # S_FALSE
    assert not hresult_ok(E_FAIL)
    assert hresult_message(0) == "S_OK"
    assert "S_FALSE" in hresult_message(1)
    assert "80004005" in hresult_message(E_FAIL).upper()


def test_fake_api_satisfies_shell_api_protocol() -> None:
    assert isinstance(FakeShellApi([]), ShellApi)


# --------------------------------------------------------------------------
# scan —— 只读统计
# --------------------------------------------------------------------------


def test_scan_sums_all_fixed_drives() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 6000, 1024)
    api.set_count("D:\\", 4000, 512)

    result = make_service(api).scan()

    assert result.total_items == 10000
    assert result.total_size_bytes == 1536
    assert result.scanned_roots == ("C:\\", "D:\\")
    assert result.failed_roots == ()
    assert result.is_complete is True
    assert result.error is None
    assert result.should_trigger(10000) is True


def test_scan_excludes_removable_network_and_optical_drives() -> None:
    api = FakeShellApi(
        [
            ("C:\\", DriveKind.FIXED),
            ("E:\\", DriveKind.REMOVABLE),
            ("Z:\\", DriveKind.NETWORK),
            ("F:\\", DriveKind.CDROM),
            ("R:\\", DriveKind.RAMDISK),
            ("Q:\\", DriveKind.UNKNOWN),
        ]
    )
    api.set_count("C:\\", 10)
    api.set_count("E:\\", 999999)
    api.set_count("Z:\\", 999999)
    api.set_count("F:\\", 999999)

    service = make_service(api)
    result = service.scan()

    assert result.scanned_roots == ("C:\\",)
    assert result.total_items == 10
    # 诊断接口仍能看到全部磁盘
    assert len(service.list_all_drives()) == 6
    assert "E:\\" not in api.query_calls
    assert "Z:\\" not in api.query_calls


def test_scan_marks_incomplete_when_one_disk_fails() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 100)
    api.set_error("D:\\", E_FAIL)

    result = make_service(api).scan()

    assert result.total_items == 100
    assert result.failed_roots == ("D:\\",)
    assert result.is_complete is False
    assert result.should_trigger(50) is False  # 不完整时绝不触发
    failed = [d for d in result.disks if not d.ok]
    assert len(failed) == 1
    assert failed[0].root == "D:\\"
    assert failed[0].hresult == E_FAIL
    assert "80004005" in (failed[0].error or "").upper()


def test_scan_marks_incomplete_on_negative_counts() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 100)
    api.counts["D:\\"] = RawQuery(hresult=0, num_items=-1, size_bytes=0)

    result = make_service(api).scan()

    assert result.is_complete is False
    assert result.failed_roots == ("D:\\",)
    assert "异常计数" in (result.disks[1].error or "")


def test_scan_marks_incomplete_when_no_fixed_drive_found() -> None:
    api = FakeShellApi([("E:\\", DriveKind.REMOVABLE)])

    result = make_service(api).scan()

    assert result.scanned_roots == ()
    assert result.total_items == 0
    assert result.is_complete is False
    assert result.error is not None
    assert result.should_trigger(0) is False


def test_scan_has_no_side_effects() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 20000)

    make_service(api).scan()

    assert api.empty_calls == []


def test_scan_records_disk_details() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 7, 70)

    result = make_service(api).scan()

    assert len(result.disks) == 1
    disk = result.disks[0]
    assert (disk.root, disk.kind, disk.ok, disk.num_items, disk.size_bytes) == (
        "C:\\",
        DriveKind.FIXED,
        True,
        7,
        70,
    )


def test_scan_can_restrict_included_kinds() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("E:\\", DriveKind.REMOVABLE)])
    api.set_count("C:\\", 1)
    api.set_count("E:\\", 5)

    service = RecycleBinService(api, included_kinds=frozenset({DriveKind.REMOVABLE}))
    result = service.scan()

    assert result.scanned_roots == ("E:\\",)
    assert result.total_items == 5


# --------------------------------------------------------------------------
# empty —— 清空
# --------------------------------------------------------------------------


def test_empty_refuses_when_pre_scan_incomplete() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 50000)
    api.set_error("D:\\", E_FAIL)

    result = make_service(api).empty()

    assert result.outcome is CleanOutcome.INCOMPLETE
    assert api.empty_calls == []          # 关键：清空 API 未被调用
    assert result.items_before is None
    assert result.items_after is None
    assert "复核统计不完整" in (result.error or "")


def test_empty_refuses_when_no_fixed_drive() -> None:
    api = FakeShellApi([("E:\\", DriveKind.REMOVABLE)])

    result = make_service(api).empty()

    assert result.outcome is CleanOutcome.INCOMPLETE
    assert api.empty_calls == []


def test_empty_success_rescans_and_confirms_zero() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 100)
    api.set_count("D:\\", 50)
    api.on_empty = lambda root: api.set_count(root, 0)

    result = make_service(api).empty()

    assert result.outcome is CleanOutcome.SUCCESS
    assert result.succeeded is True
    assert result.items_before == 150
    assert result.items_after == 0
    assert result.remaining_items == 0
    assert result.hresult == 0
    assert result.duration_seconds >= 0
    # 逐盘清空，绝不传 NULL（NULL 会波及未统计的可移动盘）
    assert api.empty_calls == [
        ("C:\\", DEFAULT_EMPTY_FLAGS),
        ("D:\\", DEFAULT_EMPTY_FLAGS),
    ]


def test_empty_returns_partial_without_retrying() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 100)
    api.set_count("D:\\", 50)
    # 回调不改变计数：模拟清空未生效

    result = make_service(api).empty()

    assert result.outcome is CleanOutcome.PARTIAL
    assert result.remaining_items == 150
    assert result.items_after == 150
    assert len(api.empty_calls) == 2       # 只清空一轮，不无限重试


def test_empty_returns_failed_when_all_calls_fail() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 30000)
    api.set_empty_result("C:\\", E_ACCESSDENIED)

    result = make_service(api).empty()

    assert result.outcome is CleanOutcome.FAILED
    assert result.hresult == E_ACCESSDENIED
    assert result.items_before == 30000
    assert result.remaining_items == 30000
    assert len(api.empty_calls) == 1


def test_empty_reports_partial_when_only_some_drives_fail() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 100)
    api.set_count("D:\\", 100)
    api.set_empty_result("D:\\", E_FAIL)
    api.on_empty = lambda root: api.set_count(root, 0) if root == "C:\\" else None

    result = make_service(api).empty()

    assert result.outcome is CleanOutcome.PARTIAL
    assert result.remaining_items == 100
    assert result.hresult == E_FAIL


def test_empty_success_even_if_a_call_errored_but_bin_is_empty() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 100)
    api.set_count("D:\\", 0)
    api.set_empty_result("D:\\", E_UNEXPECTED)
    api.on_empty = lambda root: api.set_count(root, 0)

    result = make_service(api).empty()

    assert result.outcome is CleanOutcome.SUCCESS   # 复核为最终依据
    assert result.hresult == E_UNEXPECTED


def test_empty_incomplete_when_post_scan_fails() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 100)
    api.set_count("D:\\", 100)

    def corrupt(root: str | None) -> None:
        if root == "D:\\":
            api.set_error("D:\\", E_FAIL)

    api.on_empty = corrupt

    result = make_service(api).empty()

    assert result.outcome is CleanOutcome.INCOMPLETE
    assert result.items_before == 200
    assert result.items_after is None
    assert "无法确认清理结果" in (result.error or "")


def test_empty_uses_configured_flags() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 5)
    api.on_empty = lambda root: api.set_count(root, 0)

    service = RecycleBinService(api, empty_flags=SHERB_NOCONFIRMATION)
    service.empty()

    assert api.empty_calls == [("C:\\", SHERB_NOCONFIRMATION)]


def test_empty_never_passes_none_to_api_empty() -> None:
    api = FakeShellApi([("C:\\", DriveKind.FIXED), ("D:\\", DriveKind.FIXED)])
    api.set_count("C:\\", 1)
    api.set_count("D:\\", 1)
    api.on_empty = lambda root: api.set_count(root, 0)

    make_service(api).empty()

    assert all(root is not None for root, _ in api.empty_calls)


# --------------------------------------------------------------------------
# 真实 API 只读核验（不执行清空）
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 平台")
def test_real_api_readonly_scan() -> None:
    service = RecycleBinService()

    drives = service.list_all_drives()
    assert drives, "应至少枚举到一个盘符"
    assert any(kind is DriveKind.FIXED for _, kind in drives)

    result = service.scan()

    assert result.scanned_roots, "应至少统计一个固定磁盘"
    assert result.total_items >= 0
    for disk in result.disks:
        if disk.ok:
            assert disk.num_items >= 0
            assert disk.size_bytes >= 0
    # 只读验证：绝不调用清空

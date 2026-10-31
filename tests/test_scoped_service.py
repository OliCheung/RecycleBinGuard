"""按范围工作服务测试：只删垃圾模式的统计、清空与安全边界。

关键回归点：**清空后复核不能把保留条目重复计入**，否则清理永远显示 PARTIAL。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from recyclebingguard.config import SCOPE_EVERYTHING, Config
from recyclebingguard.interfaces import CleanOutcome
from recyclebingguard.recycle_bin_index import DriveIndex, RecycleEntry, RecycleIndex
from recyclebingguard.scoped_service import ScopedRecycleBinService
from recyclebingguard.selective_cleaner import SelectiveCleaner

BASE = datetime(2026, 5, 1, 10, 0, 0)


class FakeStore:
    """可变条目集合，模拟清理前后回收站内容的变化。"""

    def __init__(self, entries: list[RecycleEntry]) -> None:
        self.entries = list(entries)
        self.scan_count = 0
        self.failed_roots: list[str] = []

    def build_index(self, roots=None) -> RecycleIndex:
        self.scan_count += 1
        return RecycleIndex(
            drives=(DriveIndex(root="C:\\", entries=tuple(self.entries)),),
            failed_roots=tuple(self.failed_roots),
        )


def entry(path: str, *, recursive: int = 1, seconds: int = 0, size: int = 0) -> RecycleEntry:
    key = f"k{abs(hash(path)) % 10**6}{seconds}"
    return RecycleEntry(
        original_path=path,
        deleted_at=BASE + timedelta(seconds=seconds),
        size_bytes=size,
        recursive_entries=recursive,
        root="C:\\",
        meta_path=f"meta-{key}",
        data_path=f"data-{key}",
    )


class DeletingCleaner(SelectiveCleaner):
    """删除时同步从 FakeStore 里移除条目，模拟真实清理效果。"""

    def __init__(self, store: FakeStore, *, fail_paths: set[str] | None = None) -> None:
        self._store = store
        self._fail = fail_paths or set()
        super().__init__(delete_paths=self._delete, remove_meta=lambda path: None)

    def _delete(self, paths: list[str], flags: int) -> int:
        if any(path in self._fail for path in paths):
            return 0x87
        removed = {path for path in paths}
        self._store.entries = [
            item for item in self._store.entries if item.data_path not in removed
        ]
        return 0


def make_config(**overrides) -> Config:
    config = Config()
    config.garbage_path_patterns = ["*\\build\\*", "*\\node_modules\\*"]
    config.keep_path_patterns = ["*\\Desktop\\*"]
    config.burst_enabled = False
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def make_service(config: Config, store: FakeStore, *, fail_paths=None):
    return ScopedRecycleBinService(
        config,
        fallback=None,
        indexer=store,
        cleaner=DeletingCleaner(store, fail_paths=fail_paths),
    )


# --------------------------------------------------------------------------
# scan
# --------------------------------------------------------------------------


def test_scan_counts_only_garbage_recursively() -> None:
    store = FakeStore(
        [
            entry(r"C:\proj\build\out.apk", recursive=500),
            entry(r"C:\Users\me\Desktop\photo.jpg", recursive=1),
            entry(r"C:\Users\me\notes\a.docx", recursive=1),
        ]
    )
    service = make_service(make_config(), store)

    result = service.scan()

    assert result.total_items == 500  # 只有垃圾被计数
    assert result.is_complete is True
    assert result.scanned_roots == ("C:\\",)


def test_scan_exposes_reference_totals() -> None:
    """阈值口径只算垃圾，但参考值必须给出回收站里实际有多少内容。"""
    store = FakeStore(
        [
            entry(r"C:\proj\build\out.apk", recursive=500),
            entry(r"C:\Users\me\Desktop\photo.jpg", recursive=1),
            entry(r"C:\Users\me\notes\a.docx", recursive=3),
        ]
    )
    service = make_service(make_config(), store)

    result = service.scan()

    assert result.total_items == 500  # 阈值口径：仅垃圾
    assert result.reference_items == 3  # 实际顶层条目
    assert result.reference_entries == 504  # 实际递归条目（500 + 1 + 3）


def test_scan_top_level_when_recursive_disabled() -> None:
    store = FakeStore([entry(r"C:\proj\build\out.apk", recursive=500)])
    service = make_service(make_config(count_recursive=False), store)

    assert service.scan().total_items == 1


def test_scan_marks_incomplete_when_drive_fails() -> None:
    store = FakeStore([entry(r"C:\proj\build\a.bin", recursive=99999)])
    store.failed_roots = ["D:\\"]
    service = make_service(make_config(), store)

    result = service.scan()

    assert result.is_complete is False
    assert result.should_trigger(1) is False


def test_scan_size_uses_garbage_bytes_only() -> None:
    store = FakeStore(
        [
            entry(r"C:\proj\build\big.bin", size=10_000),
            entry(r"C:\Users\me\Desktop\video.mp4", size=999_999),
        ]
    )
    service = make_service(make_config(), store)

    assert service.scan().total_size_bytes == 10_000


# --------------------------------------------------------------------------
# empty
# --------------------------------------------------------------------------


def test_empty_deletes_only_garbage_and_keeps_user_files() -> None:
    store = FakeStore(
        [
            entry(r"C:\proj\build\out.apk", recursive=10),
            entry(r"C:\proj\node_modules\x\i.js", recursive=5),
            entry(r"C:\Users\me\Desktop\photo.jpg"),
            entry(r"C:\Users\me\notes\a.docx"),
        ]
    )
    service = make_service(make_config(), store)

    result = service.empty()

    assert result.outcome is CleanOutcome.SUCCESS
    paths = {item.original_path for item in store.entries}
    assert paths == {r"C:\Users\me\Desktop\photo.jpg", r"C:\Users\me\notes\a.docx"}


def test_empty_success_does_not_repeat_partial_forever() -> None:
    """回归：清理后复核若把保留条目计入，会永远得到 PARTIAL。"""
    store = FakeStore(
        [
            entry(r"C:\proj\build\out.apk", recursive=10),
            entry(r"C:\Users\me\notes\a.docx"),
        ]
    )
    service = make_service(make_config(), store)

    result = service.empty()

    assert result.outcome is CleanOutcome.SUCCESS
    assert result.items_after == 0


def test_empty_without_garbage_calls_no_delete_api() -> None:
    store = FakeStore([entry(r"C:\Users\me\notes\a.docx")])
    cleaner_calls: list[list[str]] = []

    class TrackingCleaner(SelectiveCleaner):
        def _delete(self, paths, flags):  # pragma: no cover - 不应被调用
            cleaner_calls.append(paths)
            return 0

    service = ScopedRecycleBinService(
        make_config(),
        fallback=None,
        indexer=store,
        cleaner=TrackingCleaner(delete_paths=TrackingCleaner._delete),
    )

    result = service.empty()

    assert result.outcome is CleanOutcome.SUCCESS
    assert cleaner_calls == []


def test_empty_refuses_when_scan_incomplete() -> None:
    store = FakeStore([entry(r"C:\proj\build\a.bin")])
    store.failed_roots = ["D:\\"]
    service = make_service(make_config(), store)

    result = service.empty()

    assert result.outcome is CleanOutcome.INCOMPLETE
    assert len(store.entries) == 1  # 什么都没删


def test_empty_partial_when_some_deletes_fail() -> None:
    bad = entry(r"C:\proj\build\locked.bin")
    store = FakeStore([entry(r"C:\proj\build\ok.bin"), bad])
    service = make_service(make_config(), store, fail_paths={bad.data_path})

    result = service.empty()

    assert result.outcome is CleanOutcome.PARTIAL
    assert result.remaining_items == 1


def test_empty_failed_when_all_deletes_fail() -> None:
    bad = entry(r"C:\proj\build\locked.bin")
    store = FakeStore([bad])
    service = make_service(make_config(), store, fail_paths={bad.data_path})

    result = service.empty()

    assert result.outcome is CleanOutcome.FAILED
    assert result.error is not None


def test_empty_ignores_whitelist_entries_completely() -> None:
    store = FakeStore(
        [
            entry(r"C:\Users\me\Desktop\proj\build\out.exe"),
            entry(r"C:\Users\me\Desktop\photo.jpg"),
        ]
    )
    service = make_service(make_config(), store)

    result = service.empty()

    # 桌面下的 build 目录属于白名单，不该被删
    assert result.outcome is CleanOutcome.SUCCESS
    assert len(store.entries) == 2


# --------------------------------------------------------------------------
# everything 模式委托
# --------------------------------------------------------------------------


class FakeFullService:
    """整盘清空替身，记录调用。"""

    def __init__(self) -> None:
        self.scanned = 0
        self.emptied = 0
        self.excluded: object = None

    def set_excluded_roots(self, roots) -> None:
        self.excluded = list(roots)

    def scan(self):
        from recyclebingguard.interfaces import ScanResult

        self.scanned += 1
        return ScanResult(total_items=7, total_size_bytes=0, scanned_roots=("C:\\",))

    def empty(self, *, verified_scan=None):
        from recyclebingguard.interfaces import CleanResult

        self.emptied += 1
        return CleanResult(outcome=CleanOutcome.SUCCESS)


def test_everything_mode_delegates_scan_and_empty() -> None:
    full = FakeFullService()
    service = ScopedRecycleBinService(make_config(clean_scope=SCOPE_EVERYTHING), fallback=full)

    scan = service.scan()
    result = service.empty()

    assert full.scanned == 1
    assert full.emptied == 1
    assert scan.total_items == 7
    assert result.outcome is CleanOutcome.SUCCESS


def test_excluded_roots_forwarded_to_fallback() -> None:
    full = FakeFullService()
    service = ScopedRecycleBinService(make_config(), fallback=full)

    service.set_excluded_roots(["D:", "E:/"])

    assert full.excluded == ["D:\\", "E:\\"]
    assert service.excluded_roots == frozenset({"D:\\", "E:\\"})

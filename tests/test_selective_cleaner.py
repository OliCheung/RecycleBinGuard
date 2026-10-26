"""选择性清理测试：删除顺序、失败处理、$I 残留、批量行为。

全部使用注入的假删除函数，绝不触碰真实回收站。
"""

from __future__ import annotations

from recyclebingguard.recycle_bin_index import RecycleEntry
from recyclebingguard.selective_cleaner import (
    DEFAULT_DELETE_FLAGS,
    FOF_NOCONFIRMATION,
    FOF_NOERRORUI,
    FOF_SILENT,
    SelectiveCleaner,
    describe_delete_code,
)


class FakeFs:
    """记录删除调用的假文件系统。"""

    def __init__(self) -> None:
        self.deleted: list[list[str]] = []
        self.removed_meta: list[str] = []
        #: 数据路径 -> 返回码
        self.codes: dict[str, int] = {}
        #: meta 路径 -> 错误码（None 表示成功）
        self.meta_errors: dict[str, int] = {}

    def delete_paths(self, paths: list[str], flags: int) -> int:
        self.deleted.append(list(paths))
        for path in paths:
            code = self.codes.get(path, 0)
            if code:
                return code
        return 0

    def remove_meta(self, path: str) -> int | None:
        self.removed_meta.append(path)
        return self.meta_errors.get(path)


def make_entry(name: str = "a", *, recursive: int = 1, opaque: bool = False) -> RecycleEntry:
    return RecycleEntry(
        original_path=rf"C:\proj\build\{name}.bin",
        is_dir=False,
        recursive_entries=recursive,
        opaque=opaque,
        meta_path=rf"C:\$Recycle.Bin\S-1-5-21-1\$I{name}",
        data_path=rf"C:\$Recycle.Bin\S-1-5-21-1\$R{name}",
    )


def test_default_flags_do_not_allow_undo() -> None:
    """条目本就在回收站里，再“移到回收站”会失败，因此不能带 FOF_ALLOWUNDO。"""
    assert DEFAULT_DELETE_FLAGS & 0x0040 == 0
    assert DEFAULT_DELETE_FLAGS & FOF_NOCONFIRMATION
    assert DEFAULT_DELETE_FLAGS & FOF_SILENT
    assert DEFAULT_DELETE_FLAGS & FOF_NOERRORUI


def test_deletes_data_before_meta() -> None:
    """顺序关键：先删 $R，再删 $I。"""
    fs = FakeFs()
    order: list[str] = []
    cleaner = SelectiveCleaner(
        delete_paths=lambda paths, flags: (order.append("data"), fs.delete_paths(paths, flags))[1],
        remove_meta=lambda path: (order.append("meta"), fs.remove_meta(path))[1],
    )

    outcome = cleaner.delete_entry(make_entry())

    assert outcome.ok is True
    assert order == ["data", "meta"]


def test_failure_keeps_meta_intact() -> None:
    """数据删除失败时 $I 必须保持不动，条目才能继续留在回收站。"""
    fs = FakeFs()
    fs.codes[r"C:\$Recycle.Bin\S-1-5-21-1\$Ra"] = 0x79  # 拒绝访问
    cleaner = SelectiveCleaner(delete_paths=fs.delete_paths, remove_meta=fs.remove_meta)

    outcome = cleaner.delete_entry(make_entry())

    assert outcome.ok is False
    assert fs.removed_meta == []
    assert "拒绝访问" in (outcome.error or "")


def test_meta_failure_reports_orphan_but_counts_as_deleted() -> None:
    fs = FakeFs()
    fs.meta_errors[r"C:\$Recycle.Bin\S-1-5-21-1\$Ia"] = 5
    cleaner = SelectiveCleaner(delete_paths=fs.delete_paths, remove_meta=fs.remove_meta)

    outcome = cleaner.delete_entry(make_entry())

    assert outcome.ok is True  # 数据已不可恢复
    assert outcome.meta_orphaned is True
    assert "$I" in (outcome.error or "")


def test_opaque_entry_is_refused() -> None:
    fs = FakeFs()
    cleaner = SelectiveCleaner(delete_paths=fs.delete_paths, remove_meta=fs.remove_meta)

    outcome = cleaner.delete_entry(make_entry(opaque=True))

    assert outcome.ok is False
    assert fs.deleted == []


def test_entry_without_data_path_is_refused() -> None:
    fs = FakeFs()
    cleaner = SelectiveCleaner(delete_paths=fs.delete_paths, remove_meta=fs.remove_meta)
    entry = RecycleEntry(original_path=r"C:\x\y", meta_path="m")

    outcome = cleaner.delete_entry(entry)

    assert outcome.ok is False
    assert fs.deleted == []


def test_batch_continues_after_failure() -> None:
    """单个文件被占用不应中断整批清理。"""
    fs = FakeFs()
    fs.codes[r"C:\$Recycle.Bin\S-1-5-21-1\$Rb"] = 0x87  # 文件已被占用
    cleaner = SelectiveCleaner(delete_paths=fs.delete_paths, remove_meta=fs.remove_meta)

    result = cleaner.delete_entries([make_entry("a"), make_entry("b"), make_entry("c")])

    assert result.deleted_items == 2
    assert result.failed_items == 1
    assert result.succeeded is False
    assert "占用" in (result.first_error() or "")


def test_batch_counts_recursive_entries() -> None:
    fs = FakeFs()
    cleaner = SelectiveCleaner(delete_paths=fs.delete_paths, remove_meta=fs.remove_meta)

    result = cleaner.delete_entries(
        [make_entry("a", recursive=500), make_entry("b", recursive=300)]
    )

    assert result.deleted_items == 2
    assert result.deleted_entries == 800


def test_batch_all_success() -> None:
    fs = FakeFs()
    cleaner = SelectiveCleaner(delete_paths=fs.delete_paths, remove_meta=fs.remove_meta)

    result = cleaner.delete_entries([make_entry("a"), make_entry("b")])

    assert result.succeeded is True
    assert result.deleted_items == 2
    assert "跳过 3 项" in result.describe() or "跳过 0 项" in result.describe()


def test_describe_delete_code() -> None:
    assert describe_delete_code(0) == "成功"
    assert "拒绝访问" in describe_delete_code(0x79)
    assert "占用" in describe_delete_code(0x87)
    assert "未知错误" in describe_delete_code(0x999)


def test_extra_data_paths_are_deleted_together() -> None:
    """多段 $R 记录（同一 key 的多个数据文件）必须一起删。"""
    fs = FakeFs()
    cleaner = SelectiveCleaner(delete_paths=fs.delete_paths, remove_meta=fs.remove_meta)
    entry = RecycleEntry(
        original_path=r"C:\x\big.iso",
        meta_path="m",
        data_path="d1",
        extra_data_paths=("d2", "d3"),
    )

    outcome = cleaner.delete_entry(entry)

    assert outcome.ok is True
    assert fs.deleted == [["d1", "d2", "d3"]]

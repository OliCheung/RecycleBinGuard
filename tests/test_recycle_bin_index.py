"""``$I`` 索引解析与递归计数测试。

全部使用临时目录构造假的回收站结构，不触碰真实回收站。
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta
from pathlib import Path

from recyclebingguard.recycle_bin_index import (
    WindowsRecycleBinIndexer,
    count_tree_entries,
    filetime_to_datetime,
    parse_meta,
)

#: 2026-01-01 00:00:00 的 FILETIME（100ns，自 1601-01-01）。
_EPOCH_OFFSET = 11644473600


def _filetime(moment: datetime) -> int:
    seconds = moment.timestamp() + _EPOCH_OFFSET
    return int(seconds * 10_000_000)


def _meta_bytes(original_path: str, *, deleted_at: datetime, size: int = 0) -> bytes:
    """构造一条版本 2 的 ``$I`` 记录。"""
    payload = original_path.encode("utf-16-le") + b"\x00\x00"
    header = struct.pack("<qqq", 2, size, _filetime(deleted_at))
    return header + struct.pack("<i", len(payload) // 2) + payload


def _make_bin(tmp_path: Path, drive_letter: str = "C") -> Path:
    """在临时目录下构造 ``<tmp>\\C:\\$Recycle.Bin\\S-1-5-21-1`` 结构。"""
    folder = tmp_path / "bins" / f"{drive_letter}" / "$Recycle.Bin" / "S-1-5-21-1"
    folder.mkdir(parents=True)
    return folder


def _add_entry(folder: Path, key: str, original_path: str, *, deleted_at: datetime) -> None:
    (folder / f"$I{key}").write_bytes(_meta_bytes(original_path, deleted_at=deleted_at))
    (folder / f"$R{key}").write_text("x", encoding="utf-8")


# --------------------------------------------------------------------------
# parse_meta
# --------------------------------------------------------------------------


def test_parse_meta_utf16_version() -> None:
    moment = datetime(2026, 3, 1, 12, 30, 0)
    raw = _meta_bytes(r"C:\proj\build\out.bin", deleted_at=moment, size=1234)
    record = parse_meta(raw)

    assert record is not None
    assert record.version == 2
    assert record.original_path == r"C:\proj\build\out.bin"
    assert record.size_bytes == 1234
    assert record.deleted_at is not None
    assert abs((record.deleted_at - moment).total_seconds()) < 2


def test_parse_meta_rejects_short_and_unknown() -> None:
    assert parse_meta(b"") is None
    assert parse_meta(b"\x00" * 10) is None
    # 未知版本号
    assert parse_meta(struct.pack("<qqq", 99, 1, 0) + b"abc") is None


def test_filetime_to_datetime_handles_invalid() -> None:
    assert filetime_to_datetime(0) is None
    assert filetime_to_datetime(-5) is None
    assert filetime_to_datetime(10_000_000) is None  # 早于 1601 之后的负秒


# --------------------------------------------------------------------------
# count_tree_entries
# --------------------------------------------------------------------------


def test_count_tree_entries_counts_files_and_dirs(tmp_path: Path) -> None:
    folder = tmp_path / "many"
    (folder / "sub" / "deeper").mkdir(parents=True)
    for index in range(4):
        (folder / f"f{index}.txt").write_text("x", encoding="utf-8")
    (folder / "sub" / "nested.txt").write_text("x", encoding="utf-8")

    # 4 个文件 + sub + deeper + nested.txt = 7
    assert count_tree_entries(str(folder)) == 7
    assert count_tree_entries(str(tmp_path / "missing")) == 0


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------


def test_index_pairs_meta_and_data_by_key(tmp_path: Path) -> None:
    folder = _make_bin(tmp_path)
    now = datetime(2026, 3, 1, 12, 0, 0)
    _add_entry(folder, "ABC123", r"C:\Users\me\AppData\Local\Temp\t.txt", deleted_at=now)

    indexer = WindowsRecycleBinIndexer()
    # 直接对构造出来的回收站目录做一次条目解析（避开盘符 API）
    entries = indexer._entries_from_folder(
        "C:\\", str(folder), sorted(p.name for p in folder.iterdir())
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.opaque is False
    assert entry.original_path == r"C:\Users\me\AppData\Local\Temp\t.txt"
    assert entry.recursive_entries == 1
    assert entry.meta_path.endswith("$IABC123")
    assert entry.data_path.endswith("$RABC123")


def test_index_reports_directory_recursive_count(tmp_path: Path) -> None:
    folder = _make_bin(tmp_path)
    key = "DIR001"
    (folder / "$IDIR001").write_bytes(
        _meta_bytes(r"C:\proj\node_modules", deleted_at=datetime(2026, 3, 1))
    )
    data = folder / f"$R{key}"
    (data / "pkg" / "lib").mkdir(parents=True)
    (data / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (data / "pkg" / "lib" / "a.js").write_text("x", encoding="utf-8")

    indexer = WindowsRecycleBinIndexer()
    entries = indexer._entries_from_folder(
        "C:\\", str(folder), sorted(p.name for p in folder.iterdir())
    )

    assert len(entries) == 1
    entry = entries[0]
    assert entry.is_dir is True
    # pkg + index.js + lib + a.js = 4
    assert entry.recursive_entries == 4


def test_index_marks_orphan_data_as_opaque(tmp_path: Path) -> None:
    folder = _make_bin(tmp_path)
    (folder / "$RORPHAN").write_text("x", encoding="utf-8")

    indexer = WindowsRecycleBinIndexer()
    entries = indexer._entries_from_folder(
        "C:\\", str(folder), sorted(p.name for p in folder.iterdir())
    )

    assert len(entries) == 1
    assert entries[0].opaque is True
    assert entries[0].original_path == ""


def test_index_marks_unreadable_user_dir_as_opaque(tmp_path: Path, monkeypatch) -> None:
    side = tmp_path / "bins" / "C" / "$Recycle.Bin"
    (side / "S-1-5-21-1").mkdir(parents=True)
    (side / "S-1-5-21-2").mkdir()
    real_listdir = __import__("os").listdir

    def fake_listdir(path):
        if str(path).endswith("S-1-5-21-2"):
            raise PermissionError(13, "denied")
        return real_listdir(path)

    monkeypatch.setattr("os.listdir", fake_listdir)
    indexer = WindowsRecycleBinIndexer()
    drive = indexer.index_drive("C:\\", side_dir=str(side))

    assert drive.ok is True
    assert drive.opaque_user_dirs == 1
    assert drive.readable_user_dirs == 1
    assert any(entry.opaque for entry in drive.entries)


def test_index_drive_missing_dir_reports_error(tmp_path: Path) -> None:
    indexer = WindowsRecycleBinIndexer()
    drive = indexer.index_drive("C:\\", side_dir=str(tmp_path / "nonexistent" / "$Recycle.Bin"))
    assert drive.ok is False
    assert drive.error is not None


def test_entries_sorted_and_timed(tmp_path: Path) -> None:
    folder = _make_bin(tmp_path)
    base = datetime(2026, 5, 1, 10, 0, 0)
    for index in range(3):
        _add_entry(
            folder,
            f"K{index}",
            f"C:\\proj\\build\\f{index}.bin",
            deleted_at=base + timedelta(seconds=index),
        )

    indexer = WindowsRecycleBinIndexer()
    entries = indexer._entries_from_folder(
        "C:\\", str(folder), sorted(p.name for p in folder.iterdir())
    )
    assert len(entries) == 3
    times = [entry.deleted_at for entry in entries]
    assert all(moment is not None for moment in times)

"""清理预览测试：阈值口径、示例输出、只读语义。"""

from __future__ import annotations

from datetime import datetime, timedelta

from recyclebingguard.clean_preview import build_preview, classify_config_from
from recyclebingguard.config import SCOPE_EVERYTHING, SCOPE_VIBECODING, Config
from recyclebingguard.garbage_rules import RecycleReason
from recyclebingguard.recycle_bin_index import DriveIndex, RecycleEntry, RecycleIndex

BASE = datetime(2026, 5, 1, 10, 0, 0)


class FakeIndexer:
    """返回预设条目的索引器替身。"""

    def __init__(self, entries: list[RecycleEntry], *, failed: list[str] | None = None) -> None:
        self._entries = entries
        self._failed = failed or []

    def build_index(self, roots=None) -> RecycleIndex:
        return RecycleIndex(
            drives=(DriveIndex(root="C:\\", entries=tuple(self._entries)),),
            failed_roots=tuple(self._failed),
        )


def entry(path: str, *, seconds: int = 0, size: int = 0, recursive: int = 1) -> RecycleEntry:
    return RecycleEntry(
        original_path=path,
        deleted_at=BASE + timedelta(seconds=seconds),
        size_bytes=size,
        recursive_entries=recursive,
    )


def make_config(**overrides) -> Config:
    config = Config()
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def test_classify_config_from_copies_patterns() -> None:
    config = make_config(garbage_path_patterns=["*\\foo\\*"], keep_path_patterns=["*\\bar\\*"])
    classify = classify_config_from(config)
    assert classify.garbage_patterns == ("*\\foo\\*",)
    assert classify.keep_patterns == ("*\\bar\\*",)
    assert classify.burst_min_items == config.burst_min_items


def test_preview_splits_garbage_and_kept() -> None:
    indexer = FakeIndexer(
        [
            entry(r"C:\proj\node_modules\a\b.js", recursive=1),
            entry(r"C:\proj\build\out.exe", recursive=1),
            entry(r"C:\Users\me\notes\a.docx"),
        ]
    )
    config = make_config(
        garbage_path_patterns=["*\\node_modules\\*", "*\\build\\*"],
        burst_enabled=False,
    )

    preview = build_preview(config, indexer=indexer)

    assert len(preview.garbage) == 2
    assert len(preview.kept) == 1
    assert preview.summary.garbage_items == 2
    assert preview.summary.keep_items == 1


def test_preview_examples_collapse_duplicate_paths() -> None:
    indexer = FakeIndexer(
        [
            entry(r"C:\proj\build\a.apk", size=10),
            entry(r"C:\proj\build\a.apk", size=10),
            entry(r"C:\proj\build\a.apk", size=10),
        ]
    )
    config = make_config(garbage_path_patterns=["*\\build\\*"], burst_enabled=False)

    examples = build_preview(config, indexer=indexer).examples(limit=10)

    assert len(examples) == 1
    assert "共 3 次" in examples[0][0]


def test_preview_examples_sorted_by_size_desc() -> None:
    indexer = FakeIndexer(
        [
            entry(r"C:\proj\build\small.bin", size=1),
            entry(r"C:\proj\build\big.bin", size=100),
        ]
    )
    config = make_config(garbage_path_patterns=["*\\build\\*"], burst_enabled=False)

    preview = build_preview(config, indexer=indexer)
    examples = preview.examples(limit=2)

    assert [path.rsplit("\\", 1)[-1] for path, _, _ in examples] == ["big.bin", "small.bin"]
    assert examples[0][1] is RecycleReason.GARBAGE_PATH


def test_threshold_uses_recursive_garbage_count() -> None:
    indexer = FakeIndexer([entry(r"C:\proj\build\dist", recursive=9000)])
    config = make_config(
        garbage_path_patterns=["*\\build\\*"],
        burst_enabled=False,
        threshold_items=8000,
        count_recursive=True,
        clean_scope=SCOPE_VIBECODING,
    )

    preview = build_preview(config, indexer=indexer)

    assert preview.counted_entries(config) == 9000
    assert preview.should_trigger(config) is True


def test_threshold_without_recursive_uses_top_level_items() -> None:
    indexer = FakeIndexer([entry(r"C:\proj\build\dist", recursive=9000)])
    config = make_config(
        garbage_path_patterns=["*\\build\\*"],
        burst_enabled=False,
        threshold_items=8000,
        count_recursive=False,
        clean_scope=SCOPE_VIBECODING,
    )

    preview = build_preview(config, indexer=indexer)

    assert preview.counted_entries(config) == 1
    assert preview.should_trigger(config) is False


def test_everything_scope_uses_top_level_total() -> None:
    indexer = FakeIndexer(
        [
            entry(r"C:\proj\build\a.bin"),
            entry(r"C:\Users\me\Desktop\photo.jpg"),
            entry(r"C:\Users\me\notes\doc.docx"),
        ]
    )
    config = make_config(
        garbage_path_patterns=["*\\build\\*"],
        burst_enabled=False,
        threshold_items=3,
        clean_scope=SCOPE_EVERYTHING,
    )

    preview = build_preview(config, indexer=indexer)

    # everything 模式不看垃圾判定，直接按顶层条目总数
    assert preview.should_trigger(config) is True


def test_incomplete_index_never_triggers() -> None:
    indexer = FakeIndexer(
        [entry(r"C:\proj\build\a.bin", recursive=99999)],
        failed=["D:\\"],
    )
    config = make_config(
        garbage_path_patterns=["*\\build\\*"],
        burst_enabled=False,
        threshold_items=1,
    )

    preview = build_preview(config, indexer=indexer)

    assert preview.index.is_complete is False
    assert preview.should_trigger(config) is False


def test_size_threshold_triggers() -> None:
    indexer = FakeIndexer([entry(r"C:\proj\build\a.bin", size=10_000)])
    config = make_config(
        garbage_path_patterns=["*\\build\\*"],
        burst_enabled=False,
        threshold_items=1_000_000,
        threshold_size_bytes=5_000,
    )

    preview = build_preview(config, indexer=indexer)

    assert preview.should_trigger(config) is True


def test_reason_counts_covers_all_reasons() -> None:
    indexer = FakeIndexer(
        [
            entry(r"C:\proj\build\a.bin"),
            entry(r"C:\Users\me\Desktop\p.jpg"),
            entry(r"C:\Users\me\notes\d.docx"),
        ]
    )
    config = make_config(garbage_path_patterns=["*\\build\\*"], burst_enabled=False)

    counts = build_preview(config, indexer=indexer).reason_counts()

    assert counts[RecycleReason.GARBAGE_PATH] == 1
    assert counts[RecycleReason.KEEP_WHITELIST] == 1
    assert counts[RecycleReason.KEEP_UNMATCHED] == 1


def test_preview_never_mutates_or_deletes() -> None:
    """预览必须纯只读：条目数量与内容在预览前后保持不变。"""
    entries = [entry(r"C:\proj\build\a.bin"), entry(r"C:\Users\me\a.docx")]
    indexer = FakeIndexer(entries)
    config = make_config(garbage_path_patterns=["*\\build\\*"], burst_enabled=False)

    build_preview(config, indexer=indexer)

    assert len(entries) == 2
    assert entries[0].original_path == r"C:\proj\build\a.bin"

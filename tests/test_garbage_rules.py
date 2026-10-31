"""垃圾判定规则测试：白名单优先级、路径模式、时间突发、汇总口径。"""

from __future__ import annotations

from datetime import datetime, timedelta

from recyclebingguard.garbage_rules import (
    ClassifyConfig,
    RecycleReason,
    classify_entries,
    classify_entry,
    matches_patterns,
    matches_zone,
    summarize,
)
from recyclebingguard.recycle_bin_index import RecycleEntry

BASE = datetime(2026, 5, 1, 10, 0, 0)


def entry(path: str, *, seconds: int = 0, size: int = 0, recursive: int = 1) -> RecycleEntry:
    return RecycleEntry(
        original_path=path,
        deleted_at=BASE + timedelta(seconds=seconds),
        size_bytes=size,
        recursive_entries=recursive,
    )


# --------------------------------------------------------------------------
# 模式匹配
# --------------------------------------------------------------------------


def test_matches_patterns_is_case_and_slash_insensitive() -> None:
    patterns = ("*\\node_modules\\*",)
    assert matches_patterns(r"C:\proj\node_modules\x.js", patterns) == patterns[0]
    assert matches_patterns("C:/proj/NODE_MODULES/x.js", patterns) == patterns[0]
    assert matches_patterns(r"C:\proj\src\x.js", patterns) is None


def test_matches_patterns_auto_wraps_stars() -> None:
    assert matches_patterns(r"C:\a\.venv\lib", (".venv",)) == ".venv"
    assert matches_patterns(r"C:\a\b\keep.txt", ("keep.txt",)) == "keep.txt"


def test_matches_patterns_skips_empty() -> None:
    assert matches_patterns(r"C:\a\b", ("", "  ")) is None


# --------------------------------------------------------------------------
# 单条判定
# --------------------------------------------------------------------------


def test_whitelist_beats_garbage_patterns() -> None:
    """桌面下的 build 目录也属于用户管理范围，白名单必须优先。"""
    item = classify_entry(
        entry(r"C:\Users\me\Desktop\proj\build\out.exe"),
        ClassifyConfig(
            garbage_patterns=("*\\build\\*",),
            keep_patterns=("*\\Desktop\\*",),
        ),
    )
    assert item.reason is RecycleReason.KEEP_WHITELIST
    assert item.is_garbage is False


def test_garbage_path_matched() -> None:
    item = classify_entry(
        entry(r"C:\proj\node_modules\react\index.js"),
        ClassifyConfig(garbage_patterns=("*\\node_modules\\*",), keep_patterns=()),
    )
    assert item.reason is RecycleReason.GARBAGE_PATH
    assert item.pattern == "*\\node_modules\\*"


def test_opaque_always_kept() -> None:
    opaque = RecycleEntry(original_path="", opaque=True, is_dir=True, recursive_entries=0)
    item = classify_entry(opaque, ClassifyConfig())
    assert item.reason is RecycleReason.KEEP_OPAQUE


def test_missing_path_kept() -> None:
    item = classify_entry(entry(""), ClassifyConfig())
    assert item.reason is RecycleReason.KEEP_NO_PATH


def test_unmatched_kept_by_default() -> None:
    item = classify_entry(
        entry(r"C:\Users\me\notes\report.docx"),
        ClassifyConfig(garbage_patterns=("*\\build\\*",), keep_patterns=()),
    )
    assert item.reason is RecycleReason.KEEP_UNMATCHED
    assert item.is_garbage is False


# --------------------------------------------------------------------------
# 时间突发
# --------------------------------------------------------------------------


def _many(count: int, *, path_prefix: str = r"C:\proj\src\f", step: int = 1) -> list[RecycleEntry]:
    return [entry(f"{path_prefix}{index}.py", seconds=index * step) for index in range(count)]


def test_burst_detected_when_many_deleted_in_window() -> None:
    items = _many(60, step=1)  # 60 条集中在 60 秒内
    classified = classify_entries(
        items,
        ClassifyConfig(
            garbage_patterns=(),
            keep_patterns=(),
            burst_enabled=True,
            burst_window_seconds=300,
            burst_min_items=50,
        ),
    )
    assert all(item.reason is RecycleReason.GARBAGE_BURST for item in classified)


def test_burst_not_triggered_below_min_items() -> None:
    items = _many(10, step=1)
    classified = classify_entries(
        items,
        ClassifyConfig(
            garbage_patterns=(),
            keep_patterns=(),
            burst_enabled=True,
            burst_window_seconds=300,
            burst_min_items=50,
        ),
    )
    assert all(item.reason is RecycleReason.KEEP_UNMATCHED for item in classified)


def test_burst_not_triggered_when_spread_out() -> None:
    # 60 条，但每条间隔 1 小时：300 秒窗口内只有 1 条
    items = _many(60, step=3600)
    classified = classify_entries(
        items,
        ClassifyConfig(
            garbage_patterns=(),
            keep_patterns=(),
            burst_enabled=True,
            burst_window_seconds=300,
            burst_min_items=50,
        ),
    )
    assert all(item.reason is RecycleReason.KEEP_UNMATCHED for item in classified)


def test_burst_ignores_whitelisted_and_path_matched() -> None:
    items = _many(60, step=1)
    items.append(entry(r"C:\Users\me\Desktop\photo.jpg", seconds=5))
    items.append(entry(r"C:\proj\node_modules\a\b.js", seconds=6))
    classified = classify_entries(
        items,
        ClassifyConfig(
            garbage_patterns=("*\\node_modules\\*",),
            keep_patterns=("*\\Desktop\\*",),
            burst_enabled=True,
            burst_window_seconds=300,
            burst_min_items=50,
        ),
    )
    reasons = {item.entry.original_path: item.reason for item in classified}
    assert reasons[r"C:\Users\me\Desktop\photo.jpg"] is RecycleReason.KEEP_WHITELIST
    assert reasons[r"C:\proj\node_modules\a\b.js"] is RecycleReason.GARBAGE_PATH


def test_burst_disabled_keeps_everything() -> None:
    items = _many(60, step=1)
    classified = classify_entries(
        items,
        ClassifyConfig(
            garbage_patterns=(),
            keep_patterns=(),
            burst_enabled=False,
            burst_window_seconds=300,
            burst_min_items=50,
        ),
    )
    assert all(not item.is_garbage for item in classified)


def test_burst_handles_entries_without_time() -> None:
    items = [entry("", seconds=0)] * 0
    items = [
        RecycleEntry(original_path=rf"C:\x\{index}.txt", deleted_at=None) for index in range(60)
    ]
    classified = classify_entries(
        items,
        ClassifyConfig(
            garbage_patterns=(),
            keep_patterns=(),
            burst_enabled=True,
            burst_window_seconds=300,
            burst_min_items=50,
        ),
    )
    assert all(not item.is_garbage for item in classified)


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------


def test_summarize_counts_entries_and_items() -> None:
    classified = [
        _classified(entry(r"C:\proj\build\a.bin", recursive=10), RecycleReason.GARBAGE_PATH),
        _classified(entry(r"C:\proj\build\b.bin", recursive=5), RecycleReason.GARBAGE_PATH),
        _classified(entry(r"C:\Users\me\a.docx", recursive=1), RecycleReason.KEEP_UNMATCHED),
        _classified(RecycleEntry(original_path="", opaque=True), RecycleReason.KEEP_OPAQUE),
    ]
    summary = summarize(classified)

    assert summary.garbage_items == 2
    assert summary.garbage_entries == 15
    assert summary.keep_items == 1
    assert summary.keep_entries == 1
    assert summary.opaque_items == 1
    assert summary.total_items == 4
    assert summary.total_entries == 16


def _classified(item: RecycleEntry, reason: RecycleReason):
    from recyclebingguard.garbage_rules import ClassifiedEntry

    return ClassifiedEntry(entry=item, reason=reason)


def test_directory_itself_matches_child_pattern() -> None:
    r"""删掉整个目录时原始路径末尾没有反斜杠，也必须命中 *\build\*。"""
    patterns = (r"*\build\*",)
    assert matches_patterns(r"C:\proj\build", patterns) == patterns[0]
    assert matches_patterns(r"C:\proj\build\out.bin", patterns) == patterns[0]
    assert matches_patterns(r"C:\proj\rebuild", patterns) is None


def test_node_modules_directory_itself_is_garbage() -> None:
    item = classify_entry(
        entry(r"C:\proj\node_modules"),
        ClassifyConfig(garbage_patterns=(r"*\node_modules\*",), keep_patterns=()),
    )
    assert item.reason is RecycleReason.GARBAGE_PATH


def test_whitelist_matches_directory_itself() -> None:
    item = classify_entry(
        entry(r"C:\Users\me\Desktop"),
        ClassifyConfig(garbage_patterns=(), keep_patterns=(r"*\Desktop\*",)),
    )
    assert item.reason is RecycleReason.KEEP_WHITELIST


# --------------------------------------------------------------------------
# 可丢区（按位置整批判定）
# --------------------------------------------------------------------------


def _zone_config(**overrides) -> ClassifyConfig:
    base = dict(
        garbage_patterns=(),
        keep_patterns=(),
        burst_enabled=False,
        discard_zones=(r"D:\VibeCoding",),
        zone_min_items=5,
        zone_enabled=True,
    )
    base.update(overrides)
    return ClassifyConfig(**base)


def _zone_entries(count: int, prefix: str = r"D:\VibeCoding\proj") -> list[RecycleEntry]:
    return [entry(f"{prefix}\file{index}.png", seconds=index * 60) for index in range(count)]


def test_zone_matches_prefix_only() -> None:
    assert matches_zone(r"D:\VibeCoding\proj\a.py", (r"D:\VibeCoding",)) == r"D:\VibeCoding"
    assert matches_zone(r"d:/vibecoding/PROJ/a.py", (r"D:\VibeCoding",)) == r"D:\VibeCoding"
    # 兄弟目录不能被前缀误伤
    assert matches_zone(r"D:\VibeCoding-old\a.py", (r"D:\VibeCoding",)) is None
    assert matches_zone(r"D:\VibeCodingX\a.py", (r"D:\VibeCoding",)) is None


def test_zone_entries_become_garbage_when_threshold_reached() -> None:
    classified = classify_entries(_zone_entries(6), _zone_config())
    assert all(item.reason is RecycleReason.GARBAGE_ZONE for item in classified)


def test_zone_entries_kept_below_threshold() -> None:
    classified = classify_entries(_zone_entries(4), _zone_config())
    assert all(item.reason is RecycleReason.KEEP_ZONE_WAITING for item in classified)
    assert not any(item.is_garbage for item in classified)


def test_zone_does_not_apply_outside_zone() -> None:
    """不在可丢区里的条目，即使同样多也不受影响。"""
    entries = [entry(rf"C:\Users\me\notes\f{index}.txt") for index in range(20)]
    classified = classify_entries(entries, _zone_config())
    assert all(item.reason is RecycleReason.KEEP_UNMATCHED for item in classified)


def test_zone_covers_any_file_type() -> None:
    """可丢区判的是位置，不关心文件类型 —— 截图、脚本、源码一视同仁。"""
    paths = [
        r"D:\VibeCoding\p\screen.png",
        r"D:\VibeCoding\p\test.js",
        r"D:\VibeCoding\p\main.py",
        r"D:\VibeCoding\p\notes.md",
        r"D:\VibeCoding\p\data.csv",
    ]
    entries = [entry(path, seconds=index) for index, path in enumerate(paths)]
    classified = classify_entries(entries, _zone_config(zone_min_items=5))
    assert all(item.is_garbage for item in classified)


def test_zone_whitelist_still_wins() -> None:
    """白名单优先级最高：可丢区里也保得住指定路径。"""
    entries = _zone_entries(10)
    config = _zone_config(keep_patterns=("*.md",))
    entries.append(entry(r"D:\VibeCoding\proj\README.md"))
    classified = classify_entries(entries, config)
    reasons = {item.entry.original_path: item.reason for item in classified}
    assert reasons[r"D:\VibeCoding\proj\README.md"] is RecycleReason.KEEP_WHITELIST


def test_zone_disabled_keeps_everything() -> None:
    classified = classify_entries(_zone_entries(50), _zone_config(zone_enabled=False))
    assert all(not item.is_garbage for item in classified)


def test_zone_summary_counts_waiting_items() -> None:
    summary = summarize(classify_entries(_zone_entries(3), _zone_config()))
    assert summary.zone_waiting_items == 3
    assert summary.zone_waiting_entries == 3
    assert summary.garbage_items == 0


def test_zone_single_large_folder_triggers_by_recursive_entries() -> None:
    """删除一个含上万文件的整项目文件夹（只有 1 条顶层记录）也算够门槛。"""
    folder = entry(r"D:\VibeCoding\Stellora", recursive=20000)
    classified = classify_entries([folder], _zone_config(zone_min_items=5))
    assert classified[0].reason is RecycleReason.GARBAGE_ZONE


def test_zone_below_both_thresholds_stays_waiting() -> None:
    folder = entry(r"D:\VibeCoding\Stellora", recursive=4)
    classified = classify_entries([folder], _zone_config(zone_min_items=5))
    assert classified[0].reason is RecycleReason.KEEP_ZONE_WAITING


def test_zone_many_medium_folders_trigger_by_recursive_entries() -> None:
    """10 个各含 2000 文件的文件夹 = 10 条顶层记录，但递归数足以触发。"""
    folders = [
        entry(rf"D:\VibeCoding\project{index}", recursive=2000) for index in range(10)
    ]
    classified = classify_entries(folders, _zone_config(zone_min_items=100))
    assert all(item.reason is RecycleReason.GARBAGE_ZONE for item in classified)


def test_zone_takes_precedence_over_burst() -> None:
    """可丢区判定在突发规则之前，命中后可丢区条目不会再被改成 burst 原因。"""
    entries = _zone_entries(60)
    config = _zone_config(burst_enabled=True, burst_min_items=10)
    classified = classify_entries(entries, config)
    assert all(item.reason is RecycleReason.GARBAGE_ZONE for item in classified)

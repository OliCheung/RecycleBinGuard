"""清理预览：只读地算出「哪些条目会被清理、哪些会保留」。

设计意图：

- 用户在执行真正的清理之前，能先看到判定结果与理由，确认规则判得对；
- 本模块**绝不删除任何东西**，也不影响监控状态机；
- 命令行 ``--check-once`` 与托盘「预览清理」共用同一份逻辑，保证两处结论一致。

与 T1 的关系：T1 的 :class:`~recyclebingguard.recycle_bin_service.ScanResult` 来自
``SHQueryRecycleBinW``（只给顶层条目数）；本模块的索引来自 ``$I`` 记录，能给出
递归条目数与原路径。两者在 ``clean_scope == "everything"`` 时保持一致，
在 ``"vibecoding"`` 时本模块才是阈值判断的依据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .config import SCOPE_EVERYTHING, Config
from .garbage_rules import (
    ClassifiedEntry,
    ClassifyConfig,
    ClassifySummary,
    RecycleReason,
    classify_entries,
    summarize,
)
from .logging_setup import get_logger
from .recycle_bin_index import RecycleBinIndexer, RecycleIndex, WindowsRecycleBinIndexer

logger = get_logger()

#: 预览里最多展示多少条示例路径。
DEFAULT_SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class CleanPreview:
    """一次清理预览的完整结果（只读）。"""

    index: RecycleIndex
    classified: tuple[ClassifiedEntry, ...]
    summary: ClassifySummary
    created_at: datetime = field(default_factory=datetime.now)
    #: 本次预览使用的判定配置快照（便于在 UI 上解释「为什么这么判」）。
    scope: str = "vibecoding"

    # -- 便捷访问 --

    @property
    def garbage(self) -> list[ClassifiedEntry]:
        return [item for item in self.classified if item.is_garbage]

    @property
    def kept(self) -> list[ClassifiedEntry]:
        return [item for item in self.classified if not item.is_garbage]

    def garbage_entries(self) -> int:
        """垃圾条目的递归条目数之和（= 按文件计数时会被清理的数量）。"""
        return self.summary.garbage_entries

    def examples(
        self, limit: int = DEFAULT_SAMPLE_LIMIT, *, garbage: bool = True
    ) -> list[tuple[str, RecycleReason, str | None]]:
        """返回示例 ``(原始路径, 原因, 命中模式)``，垃圾条目按大小降序。

        同一条原始路径被反复删除（例如同一个 ``build`` 目录删了十几次）时只保留
        一条并标注次数，否则示例列表会被同一行刷满，看不出全貌。
        """
        source = self.garbage if garbage else self.kept
        ordered = sorted(source, key=lambda item: item.entry.size_bytes, reverse=True)
        seen: dict[str, int] = {}
        result: list[tuple[str, RecycleReason, str | None]] = []
        for item in ordered:
            path = item.entry.original_path or "(无法读取原路径)"
            if path in seen:
                seen[path] += 1
                continue
            seen[path] = 1
            if len(result) < max(0, limit):
                result.append((path, item.reason, item.pattern))
        # 把重复次数补进路径文字里（保持既有三元组签名不变）
        return [
            (f"{path}（共 {seen[path]} 次）" if seen[path] > 1 else path, reason, pattern)
            for path, reason, pattern in result
        ]

    def reason_counts(self) -> dict[RecycleReason, int]:
        """各判定原因的条目数量（顶层条目计）。"""
        counts: dict[RecycleReason, int] = {}
        for item in self.classified:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return counts

    def should_trigger(self, config: Config) -> bool:
        """按配置判断是否达到清理阈值。

        - ``clean_scope == "everything"``：沿用顶层条目数（与 T1 口径一致）；
        - 否则按「垃圾条目的递归条目数」判断，并在 ``count_recursive=False``
          时退化为垃圾**顶层条目数**。
        """
        if not self.index.is_complete:
            return False
        if config.clean_scope == SCOPE_EVERYTHING:
            total_items = self.summary.total_items
            if total_items >= config.threshold_items:
                return True
            return (
                config.threshold_size_bytes > 0
                and self._total_bytes() >= config.threshold_size_bytes
            )
        counted = self.counted_entries(config)
        if counted >= config.threshold_items:
            return True
        return (
            config.threshold_size_bytes > 0 and self._garbage_bytes() >= config.threshold_size_bytes
        )

    def counted_entries(self, config: Config) -> int:
        """按配置口径，用于和阈值比较的「条目数」。"""
        if config.count_recursive:
            return self.summary.garbage_entries
        return self.summary.garbage_items

    def _garbage_bytes(self) -> int:
        return sum(item.entry.size_bytes for item in self.garbage)

    def _total_bytes(self) -> int:
        return sum(item.entry.size_bytes for item in self.classified)


def classify_config_from(config: Config) -> ClassifyConfig:
    """把完整配置裁剪成分类器需要的子集。"""
    return ClassifyConfig(
        garbage_patterns=tuple(config.garbage_path_patterns),
        keep_patterns=tuple(config.keep_path_patterns),
        burst_enabled=config.burst_enabled,
        burst_window_seconds=config.burst_window_seconds,
        burst_min_items=config.burst_min_items,
        discard_zones=tuple(config.discard_zones),
        zone_min_items=config.zone_min_items,
        zone_enabled=config.zone_enabled,
    )


def build_preview(
    config: Config,
    *,
    indexer: RecycleBinIndexer | None = None,
    roots: list[tuple[str, object]] | None = None,
) -> CleanPreview:
    """建立一次清理预览（只读，不清理）。

    :param config: 当前配置（决定判定模式与阈值）。
    :param indexer: 索引器；``None`` 时使用真实文件系统实现。
    :param roots: 要索引的盘符（``[(盘符, 类型)]``）；``None`` 表示自动枚举固定磁盘。
    """
    engine = indexer if indexer is not None else WindowsRecycleBinIndexer()
    index = engine.build_index(roots)
    classified = classify_entries(list(index.entries), classify_config_from(config))
    summary = summarize(classified)
    preview = CleanPreview(
        index=index,
        classified=tuple(classified),
        summary=summary,
        scope=config.clean_scope,
    )
    logger.info(
        "清理预览：模式=%s 垃圾条目=%d 垃圾文件数=%d 保留条目=%d 不透明=%d 失败磁盘=%s",
        config.clean_scope,
        summary.garbage_items,
        summary.garbage_entries,
        summary.keep_items,
        summary.opaque_items,
        list(index.failed_roots),
    )
    return preview


__all__ = [
    "CleanPreview",
    "DEFAULT_SAMPLE_LIMIT",
    "build_preview",
    "classify_config_from",
]

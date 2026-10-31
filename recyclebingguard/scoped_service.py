"""按清理范围工作的服务包装：让监控器可以在「只删垃圾」模式下运行。

背景：T2 的 :class:`~recyclebingguard.recycle_bin_monitor.RecycleBinMonitor` 只依赖
``RecycleBinService`` 协议（``scan()`` / ``empty()``），它并不关心底层是整盘清空
还是选择性删除。本模块就是这两种实现的适配层：

- ``clean_scope == "everything"`` → 完全委托给 T1 的整盘清空实现（行为不变）；
- ``clean_scope == "vibecoding"``（默认） → ``scan()`` 用 ``$I`` 索引 + 垃圾判定，
  ``empty()`` 只删除判定为垃圾的条目。

这样监控器、确认弹窗、清理历史、失败退避等全部既有逻辑一行都不用改。

安全边界（沿用并在必要时加强 T1 的约定）：

- 索引不完整（任一磁盘失败）→ ``scan()`` 返回 ``is_complete=False``，监控器拒绝清理；
- 判定为垃圾的条目为 0 → ``empty()`` 直接返回 ``SUCCESS`` 且**不调用任何删除 API**；
- 复核后仍有垃圾 → ``PARTIAL``（不重复删除，避免无限循环）；
- 删除失败 → ``FAILED`` 并带上首个错误原因，由监控器触发退避重试。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime

from .clean_preview import classify_config_from
from .config import SCOPE_EVERYTHING, Config
from .garbage_rules import ClassifiedEntry, ClassifyConfig, classify_entries
from .interfaces import CleanOutcome, CleanResult, DiskScanResult, DriveKind, ScanResult
from .logging_setup import get_logger
from .recycle_bin_index import RecycleBinIndexer, RecycleIndex
from .selective_cleaner import SelectiveCleaner, SelectiveCleanResult

logger = get_logger()


class ScopedRecycleBinService:
    """实现 ``RecycleBinService`` 协议：按配置决定整盘清空还是只删垃圾。"""

    def __init__(
        self,
        config: Config,
        *,
        fallback: object = None,
        indexer: RecycleBinIndexer | None = None,
        cleaner: SelectiveCleaner | None = None,
    ) -> None:
        """
        :param config: 共享配置对象（与监控器必须是同一个）。
        :param fallback: 整盘清空实现（T1 的 ``RecycleBinService``）；``None`` 时
            在第一次需要时惰性创建，避免无 Windows 环境下导入即失败。
        :param indexer: 索引器（测试可注入替身）。
        :param cleaner: 选择性清理器（测试可注入替身）。
        """
        self.config = config
        self._fallback = fallback
        self._indexer = indexer
        self._cleaner = cleaner or SelectiveCleaner()
        #: 最近一次 ``scan()`` 用到的分类结果，供 ``empty()`` 复用，避免重复扫描。
        self._last_classified: list[ClassifiedEntry] = []
        self._last_index: RecycleIndex | None = None

        self._excluded: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    # 范围过滤（与 T1 保持同一套语义）
    # ------------------------------------------------------------------

    def set_excluded_roots(self, roots: Iterable[str]) -> None:
        """更新排除盘符（不改动配置，仅影响本服务下一轮扫描）。

        向下转发时按**配置里的原始顺序**去重后传递，而不是直接传 ``frozenset``：
        ``frozenset`` 的迭代顺序依赖哈希种子，同一份配置在不同进程里可能得到
        不同顺序，会让日志和测试变得不可复现。
        """
        from .interfaces import normalize_root

        ordered: list[str] = []
        for value in roots:
            root = normalize_root(value)
            if root and root not in ordered:
                ordered.append(root)
        self._excluded = frozenset(ordered)
        fallback = self._fallback
        setter = getattr(fallback, "set_excluded_roots", None)
        if callable(setter):
            setter(ordered)

    @property
    def excluded_roots(self) -> frozenset[str]:
        return self._excluded

    # ------------------------------------------------------------------
    # 实现细节
    # ------------------------------------------------------------------

    @property
    def _everything_mode(self) -> bool:
        return self.config.clean_scope == SCOPE_EVERYTHING

    def _delegate(self) -> object:
        """惰性获取整盘清空实现。"""
        if self._fallback is None:
            from .recycle_bin_service import RecycleBinService

            self._fallback = RecycleBinService(excluded_roots=self._excluded)
        return self._fallback

    def _get_indexer(self) -> RecycleBinIndexer:
        if self._indexer is None:
            from .recycle_bin_index import WindowsRecycleBinIndexer

            self._indexer = WindowsRecycleBinIndexer()
        return self._indexer

    def _classify_config(self) -> ClassifyConfig:
        return classify_config_from(self.config)

    # ------------------------------------------------------------------
    # 协议实现：scan
    # ------------------------------------------------------------------

    def scan(self) -> ScanResult:
        """统计。模式为 ``everything`` 时委托给 T1，否则按垃圾条目的递归数统计。"""
        if self._everything_mode:
            return self._delegate().scan()  # type: ignore[attr-defined,no-any-return]

        started = datetime.now()
        index = self._get_indexer().build_index()
        classified = classify_entries(list(index.entries), self._classify_config())
        self._last_index = index
        self._last_classified = classified

        from .garbage_rules import summarize

        summary = summarize(classified)
        counted = summary.garbage_entries if self.config.count_recursive else summary.garbage_items
        garbage_bytes = sum(item.entry.size_bytes for item in classified if item.is_garbage)

        disks = tuple(
            DiskScanResult(
                root=drive.root,
                kind=DriveKind.FIXED,
                ok=drive.ok,
                num_items=sum(1 for entry in drive.entries if not entry.opaque),
                size_bytes=sum(entry.size_bytes for entry in drive.entries),
                error=drive.error,
            )
            for drive in index.drives
        )

        result = ScanResult(
            # 注意：``total_items`` 沿用「阈值口径计数」，因此 ``should_trigger``
            # 对项目数的判断与本模式完全一致；大小阈值仍是原义（垃圾占用字节）。
            total_items=counted,
            total_size_bytes=garbage_bytes,
            scanned_roots=tuple(drive.root for drive in index.drives),
            failed_roots=index.failed_roots,
            queried_at=started,
            disks=disks,
            error=None,
            # 参考值：回收站里实际有多少内容（含保留项），供 UI 展示与排查
            # 「有几万项却没触发清理」的疑惑。
            reference_items=summary.total_items,
            reference_entries=summary.total_entries,
        )
        logger.info(
            "回收站统计（只删垃圾模式）：实际条目=%d 实际文件数=%d "
            "垃圾条目=%d 垃圾文件数=%d 保留=%d 不透明=%d 阈值口径计数=%d 失败磁盘=%s",
            summary.total_items,
            summary.total_entries,
            summary.garbage_items,
            summary.garbage_entries,
            summary.keep_items,
            summary.opaque_items,
            counted,
            list(index.failed_roots),
        )
        return result

    # ------------------------------------------------------------------
    # 协议实现：empty
    # ------------------------------------------------------------------

    def empty(self, *, verified_scan: ScanResult | None = None) -> CleanResult:
        """删除判定为垃圾的条目。``everything`` 模式委托给 T1 整盘清空。"""
        if self._everything_mode:
            return self._delegate().empty(verified_scan=verified_scan)  # type: ignore[attr-defined,no-any-return]

        started_at = datetime.now()
        started = time.perf_counter()

        def elapsed() -> float:
            return round(time.perf_counter() - started, 6)

        # 清空前复核：绝不沿用旧结果（文件可能刚被用户恢复 / 新增）。
        pre = verified_scan if verified_scan is not None else self.scan()
        if not pre.is_complete:
            message = "清空前复核统计不完整，已拒绝清空" + (
                f"（失败磁盘：{list(pre.failed_roots)}）" if pre.failed_roots else ""
            )
            logger.error("%s；未调用任何删除 API", message)
            return CleanResult(
                outcome=CleanOutcome.INCOMPLETE,
                started_at=started_at,
                duration_seconds=elapsed(),
                error=message,
            )

        garbage = [item.entry for item in self._last_classified if item.is_garbage]
        skipped = sum(1 for item in self._last_classified if not item.is_garbage)

        if not garbage:
            logger.info("复核后没有判定为垃圾的条目，无需清理")
            return CleanResult(
                outcome=CleanOutcome.SUCCESS,
                started_at=started_at,
                duration_seconds=elapsed(),
                items_before=pre.total_items,
                items_after=0,
                remaining_items=0,
            )

        logger.warning(
            "开始选择性清理：待删条目=%d 跳过=%d 清理前计数=%d",
            len(garbage),
            skipped,
            pre.total_items,
        )
        cleaning: SelectiveCleanResult = self._cleaner.delete_entries(
            garbage, skipped_items=skipped
        )

        # 清空后复核：重新索引，确认垃圾是否真的清掉了。
        post = self.scan()
        remaining = post.total_items if post.is_complete else None

        if not post.is_complete:
            message = "清空后复核统计不完整，无法确认清理结果"
            logger.error("%s；%s", message, cleaning.describe())
            return CleanResult(
                outcome=CleanOutcome.INCOMPLETE,
                started_at=started_at,
                duration_seconds=elapsed(),
                items_before=pre.total_items,
                items_after=None,
                error=message,
            )

        if cleaning.failed_items == 0:
            logger.info(
                "选择性清理成功：%s 清理前=%d 清理后=%d",
                cleaning.describe(),
                pre.total_items,
                remaining,
            )
            return CleanResult(
                outcome=CleanOutcome.SUCCESS,
                started_at=started_at,
                duration_seconds=elapsed(),
                items_before=pre.total_items,
                items_after=remaining,
                remaining_items=remaining,
            )

        if cleaning.deleted_items == 0:
            message = f"全部条目删除失败：{cleaning.first_error()}"
            logger.error("%s", message)
            return CleanResult(
                outcome=CleanOutcome.FAILED,
                started_at=started_at,
                duration_seconds=elapsed(),
                items_before=pre.total_items,
                items_after=remaining,
                remaining_items=remaining,
                error=message,
            )

        message = (
            f"部分条目删除失败（{cleaning.failed_items} 项，不重复删除）：{cleaning.first_error()}"
        )
        logger.warning("%s；%s", message, cleaning.describe())
        return CleanResult(
            outcome=CleanOutcome.PARTIAL,
            started_at=started_at,
            duration_seconds=elapsed(),
            items_before=pre.total_items,
            items_after=remaining,
            remaining_items=remaining,
            error=message,
        )


__all__ = ["ScopedRecycleBinService"]

"""垃圾条目判定规则：把回收站条目分成「垃圾」与「保留」两类。

判定顺序（先命中先返回，白名单优先级最高）：

1. **保留白名单**：原始路径命中即永不清理（桌面 / 文档 / 下载等）；
2. **不透明项**：无法读取原路径的条目一律保留（别人的回收站、损坏的 ``$I``）；
3. **路径特征**：原始路径命中临时目录 / 依赖目录 / 构建产物等模式；
4. **可丢区**：原始路径位于用户指定的可丢区目录（默认 ``D:\\VibeCoding``），
   且累计够多（**顶层条目数或递归文件数任一达到门槛**）→ 整批评为垃圾；
5. **时间突发**：同一时间窗口内集中删除的大量条目 —— vibecoding 的典型特征
   （一次构建 / 一次会话吐出成百上千个零散文件）；
6. 以上都不命中 → **保留**（默认不删，宁可漏删不可误删）。

第 4 条为什么同时看递归文件数：用户删除一个整项目文件夹时，回收站里只有
**1 条**顶层记录，但里面可能有上万文件。若只按顶层条目数算门槛，这种最常见
的「整个项目丢进回收站」永远不会被判定为垃圾，阈值也就永远触发不了。

为什么时间突发按**顶层条目数**判定：用户手动删一个装 500 张照片的文件夹，
在回收站里只会产生 **1 条**记录（实测验证），不会被误判成突发；而一次构建
吐出几千个零散文件会产生几千条记录，正好落在突发规则里。

本模块是纯函数，不碰文件系统、不依赖 Qt/tkinter，可直接单测。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .garbage_patterns import (
    DEFAULT_DISCARD_ZONES,
    DEFAULT_GARBAGE_PATTERNS,
    DEFAULT_KEEP_PATTERNS,
)
from .recycle_bin_index import RecycleEntry


class RecycleReason(Enum):
    """一个条目被归入「垃圾」或「保留」的原因（用于 UI 展示与日志）。"""

    KEEP_WHITELIST = "keep_whitelist"  # 命中保留白名单
    KEEP_OPAQUE = "keep_opaque"  # 无法读取原路径 / 他人的回收站
    KEEP_NO_PATH = "keep_no_path"  # 有 $I 但路径解析失败
    GARBAGE_PATH = "garbage_path"  # 命中路径特征
    GARBAGE_BURST = "garbage_burst"  # 命中时间突发
    GARBAGE_ZONE = "garbage_zone"  # 删除前位于「可丢区」（如 VibeCoding）
    KEEP_ZONE_WAITING = "keep_zone_waiting"  # 位于可丢区但数量未达门槛
    KEEP_UNMATCHED = "keep_unmatched"  # 未命中任何规则，默认保留

    @property
    def is_garbage(self) -> bool:
        return self.value.startswith("garbage")


@dataclass(frozen=True)
class ClassifiedEntry:
    """带分类结果的条目。"""

    entry: RecycleEntry
    reason: RecycleReason
    #: 命中的模式（路径规则 / 白名单），突发规则为 ``None``。
    pattern: str | None = None

    @property
    def is_garbage(self) -> bool:
        return self.reason.is_garbage


def _normalize(path: str) -> str:
    return path.replace("/", "\\").lower()


def matches_patterns(path: str, patterns: tuple[str, ...] | list[str]) -> str | None:
    """返回第一个命中的 glob 模式；都不命中返回 ``None``。

    比较时统一小写并把 ``/`` 归一成 ``\\``，因此在 JSON 里写哪种斜杠都能用。
    模式两侧自动补 ``*``，所以 ``*\\node_modules\\*`` 与 ``node_modules`` 等价。

    **目录本身也要算命中**：删掉整个 ``build`` 目录时，回收站里记录的原始路径是
    ``C:\\proj\\build``（末尾没有反斜杠），而不是 ``C:\\proj\\build\\xxx``。
    若只按 ``*\\build\\*`` 匹配，这种最常见的情况反而漏判。因此除了原样匹配，
    还会把路径当成目录再试一次（补一个尾部反斜杠）。
    """
    target = _normalize(path)
    candidates = (target, target + "\\")
    for pattern in patterns:
        if not pattern:
            continue
        probe = _normalize(pattern)
        if not probe.startswith("*"):
            probe = "*" + probe
        if not probe.endswith("*"):
            probe = probe + "*"
        if any(fnmatch.fnmatchcase(candidate, probe) for candidate in candidates):
            return pattern
    return None


def _burst_keys(entries: list[RecycleEntry], window_seconds: int, min_items: int) -> set[int]:
    """找出落在「突发窗口」内的条目下标（按删除时间聚类）。

    规则：把有条目时间的条目按时间排序，用滑动窗口找任意长度为
    ``window_seconds`` 的时间段；该段内条目数 ``>= min_items`` 时，段内条目
    全部视为突发垃圾。用排序 + 双指针，O(n log n)，万级条目无压力。

    :returns: 命中条目的**下标集合**（相对于传入列表）。
    """
    #: 只保留有时间戳的条目；``datetime`` 一定非 None，下面的比较才成立。
    timed: list[tuple[int, datetime]] = [
        (index, entry.deleted_at)
        for index, entry in enumerate(entries)
        if entry.deleted_at is not None
    ]
    if len(timed) < min_items:
        return set()
    timed.sort(key=lambda pair: pair[1])

    result: set[int] = set()
    left = 0
    for right in range(len(timed)):
        right_time = timed[right][1]
        while (right_time - timed[left][1]).total_seconds() > window_seconds:
            left += 1
        if right - left + 1 >= min_items:
            for position in range(left, right + 1):
                result.add(timed[position][0])
    return result


@dataclass(frozen=True)
class ClassifyConfig:
    """分类所需的配置子集（与 :class:`~recyclebingguard.config.Config` 解耦）。"""

    garbage_patterns: tuple[str, ...] = DEFAULT_GARBAGE_PATTERNS
    keep_patterns: tuple[str, ...] = DEFAULT_KEEP_PATTERNS
    burst_enabled: bool = True
    burst_window_seconds: int = 300
    burst_min_items: int = 50
    #: 「可丢区」前缀：删除前位于这些目录下的条目，只要累计够多就整批评为垃圾。
    #: 判的是**位置**而不是文件特征，因此不受项目内文件类型影响。
    discard_zones: tuple[str, ...] = DEFAULT_DISCARD_ZONES
    #: 可丢区条目的清理门槛。**顶层条目数**或**递归文件条目数**任一达到即触发。
    #: 刻意与 ``burst_min_items`` 分开：这个门槛越低，清理越积极。
    #: 递归口径保证「删掉一个含上万文件的整项目文件夹」也能被识别（那只是 1 条记录）。
    zone_min_items: int = 100
    #: 是否启用可丢区规则。
    zone_enabled: bool = True


def classify_entry(entry: RecycleEntry, config: ClassifyConfig) -> ClassifiedEntry:
    """对单个条目做**非突发**规则判定（突发规则需要全局视野，见 :func:`classify_entries`）。"""
    if entry.opaque:
        return ClassifiedEntry(entry, RecycleReason.KEEP_OPAQUE)
    path = entry.original_path
    if not path:
        return ClassifiedEntry(entry, RecycleReason.KEEP_NO_PATH)

    hit = matches_patterns(path, config.keep_patterns)
    if hit is not None:
        return ClassifiedEntry(entry, RecycleReason.KEEP_WHITELIST, hit)

    hit = matches_patterns(path, config.garbage_patterns)
    if hit is not None:
        return ClassifiedEntry(entry, RecycleReason.GARBAGE_PATH, hit)

    return ClassifiedEntry(entry, RecycleReason.KEEP_UNMATCHED)


def matches_zone(path: str, zones: tuple[str, ...] | list[str]) -> str | None:
    """判断路径是否位于某个「可丢区」下；命中返回该区前缀。

    与 :func:`matches_patterns` 的区别：这里做的是**目录前缀包含**判定，
    而不是 glob。``D:\\VibeCoding`` 应当匹配 ``D:\\VibeCoding\\proj\\a.py``，
    但**不应**匹配 ``D:\\VibeCoding-old\\a.py``（避免前缀误伤兄弟目录）。
    """
    target = _normalize(path).rstrip("\\")
    for zone in zones:
        probe = _normalize(zone).rstrip("\\")
        if not probe:
            continue
        if target == probe or target.startswith(probe + "\\"):
            return zone
    return None


def classify_entries(
    entries: tuple[RecycleEntry, ...] | list[RecycleEntry],
    config: ClassifyConfig,
) -> list[ClassifiedEntry]:
    """对全部条目分类，包含时间突发判定。

    突发规则**只作用于路径规则未命中的条目**，且不会把白名单 / 不透明项
    拉进垃圾集合 —— 这样即使某个窗口里同时有人工删除的文件，白名单也能兜住。
    """
    items = list(entries)
    results = [classify_entry(entry, config) for entry in items]

    # ---- 可丢区：按位置整批判定（在突发规则之前，优先级更高）----
    zone_positions: list[int] = []
    zone_entries = 0
    if config.zone_enabled and config.zone_min_items > 0 and items:
        for index, (entry, classified) in enumerate(zip(items, results, strict=True)):
            # 白名单 / 不透明 / 解析失败一律不动；路径规则已经判为垃圾的也无需再看
            if classified.reason is not RecycleReason.KEEP_UNMATCHED:
                continue
            zone = matches_zone(entry.original_path, config.discard_zones)
            if zone is None:
                continue
            zone_positions.append(index)
            zone_entries += entry.recursive_entries
            results[index] = ClassifiedEntry(entry, RecycleReason.KEEP_ZONE_WAITING, zone)
        # 够门槛就整批评为垃圾：判的是「这些是从可丢区删的」，与文件类型无关。
        # 顶层条目数与递归条目数**任一**达到门槛即可：删掉一个含上万文件的整项目
        # 文件夹在回收站里只是 1 条记录（顶层数=1），但递归数可能远超门槛。
        if len(zone_positions) >= config.zone_min_items or zone_entries >= config.zone_min_items:
            for index in zone_positions:
                results[index] = ClassifiedEntry(
                    items[index], RecycleReason.GARBAGE_ZONE, results[index].pattern
                )

    if not config.burst_enabled or config.burst_min_items <= 1 or not items:
        return results

    #: 参与突发判定的候选：位置 -> 条目。只有「未命中任何模式」的条目才可能被突发规则接管。
    candidate_positions = [
        index
        for index, classified in enumerate(results)
        if classified.reason is RecycleReason.KEEP_UNMATCHED
    ]
    if len(candidate_positions) < config.burst_min_items:
        return results

    candidates = [items[index] for index in candidate_positions]
    burst_positions = _burst_keys(
        candidates,
        max(1, config.burst_window_seconds),
        max(2, config.burst_min_items),
    )
    for position in burst_positions:
        index = candidate_positions[position]
        results[index] = ClassifiedEntry(items[index], RecycleReason.GARBAGE_BURST)
    return results


@dataclass(frozen=True)
class ClassifySummary:
    """分类汇总，供 UI 与命令行展示。"""

    garbage_entries: int = 0  # 垃圾条目的递归条目数之和
    garbage_items: int = 0  # 垃圾条目数（顶层条目，即真正要删的条目数）
    keep_entries: int = 0
    keep_items: int = 0
    opaque_items: int = 0
    total_entries: int = 0
    total_items: int = 0
    #: 位于可丢区但尚未达到门槛的**顶层条目数**（对外单独展示，便于理解「还差多少」）。
    zone_waiting_items: int = 0
    #: 上述待触发条目的**递归文件条目数**（与顶层数任一达到门槛即触发）。
    zone_waiting_entries: int = 0


def summarize(classified: list[ClassifiedEntry]) -> ClassifySummary:
    """把分类结果汇总成便于展示的数字。"""
    garbage_entries = garbage_items = keep_entries = keep_items = opaque = 0
    zone_waiting = zone_waiting_entries = 0
    for item in classified:
        entry = item.entry
        if item.is_garbage:
            garbage_items += 1
            garbage_entries += entry.recursive_entries
        elif entry.opaque:
            opaque += 1
        else:
            keep_items += 1
            keep_entries += entry.recursive_entries
            if item.reason is RecycleReason.KEEP_ZONE_WAITING:
                zone_waiting += 1
                zone_waiting_entries += entry.recursive_entries
    return ClassifySummary(
        garbage_entries=garbage_entries,
        garbage_items=garbage_items,
        keep_entries=keep_entries,
        keep_items=keep_items,
        opaque_items=opaque,
        total_entries=garbage_entries + keep_entries,
        total_items=garbage_items + keep_items + opaque,
        zone_waiting_items=zone_waiting,
        zone_waiting_entries=zone_waiting_entries,
    )


__all__ = [
    "ClassifiedEntry",
    "ClassifyConfig",
    "ClassifySummary",
    "DEFAULT_DISCARD_ZONES",
    "DEFAULT_GARBAGE_PATTERNS",
    "DEFAULT_KEEP_PATTERNS",
    "RecycleReason",
    "classify_entries",
    "classify_entry",
    "matches_patterns",
    "matches_zone",
    "summarize",
]

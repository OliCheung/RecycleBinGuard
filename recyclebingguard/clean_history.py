"""清理历史记录：持久化“上一次什么时候清空过回收站”。

为什么需要它（与轮转日志的分工）：

- ``logs/recyclebingguard.log`` 会按 ``log_max_bytes`` 轮转，旧内容最终被覆盖，
  不适合回答“上一次清理是什么时候”；
- :class:`MonitorStatus.last_clean_result` 只存在于内存，程序重启即丢失。

本模块把**真正调用过清空 API** 的清理（含成功 / 部分 / 失败）追加写入
``%LOCALAPPDATA%\\RecycleBinGuard\\clean_history.json``，程序重启后仍可查询。

安全与稳健约定：

- 只记录时间、数量、结果类型与错误信息，**不记录任何文件名或文件内容**；
- 试运行（dry-run）、取消确认、复核未通过等“没有调用清空 API”的情况**不写入**；
- 文件损坏时返回空历史并记日志，**不抛异常、不影响监控**；
- 写入采用“先写 .tmp 再 replace”的原子方式，最多保留 :data:`MAX_RECORDS` 条。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths
from .interfaces import CleanOutcome
from .logging_setup import get_logger

logger = get_logger()

#: 最多保留的历史条数（超出后丢弃最旧的）。
MAX_RECORDS = 50

#: 文件格式版本，便于将来兼容升级。
HISTORY_VERSION = 1


class CleanHistoryError(RuntimeError):
    """清理历史读写失败（调用方应记录日志，但不影响主流程）。"""


@dataclass(frozen=True)
class CleanRecord:
    """一次**已执行**清理的记录。

    :param finished_at: 清理完成（含清理后复核）的时刻。
    :param outcome: :class:`CleanOutcome` 的字符串值。
    :param trigger: 触发方式：``confirmed``（点击立即清理）/ ``timeout``（倒计时归零）
        / ``auto``（未开启确认弹窗时的自动清理）/ ``error``（清理过程中异常）。
    :param items_before: 清理前项目数。
    :param items_after: 清理后复核的项目数。
    :param threshold_items: 当时的触发阈值。
    :param duration_seconds: 清空调用耗时。
    :param error: 失败原因（成功时为 ``None``）。
    """

    finished_at: datetime
    outcome: str
    trigger: str = "auto"
    items_before: int | None = None
    items_after: int | None = None
    threshold_items: int = 0
    duration_seconds: float = 0.0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """是否为彻底清空成功。"""
        return self.outcome == CleanOutcome.SUCCESS.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "finished_at": self.finished_at.isoformat(timespec="seconds"),
            "outcome": self.outcome,
            "trigger": self.trigger,
            "items_before": self.items_before,
            "items_after": self.items_after,
            "threshold_items": self.threshold_items,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CleanRecord":
        try:
            finished_at = datetime.fromisoformat(str(data["finished_at"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CleanHistoryError(f"历史记录时间字段非法：{data!r}") from exc
        return cls(
            finished_at=finished_at,
            outcome=str(data.get("outcome", CleanOutcome.INCOMPLETE.value)),
            trigger=str(data.get("trigger", "auto")),
            items_before=data.get("items_before"),
            items_after=data.get("items_after"),
            threshold_items=int(data.get("threshold_items") or 0),
            duration_seconds=float(data.get("duration_seconds") or 0.0),
            error=data.get("error"),
        )

    # -- 展示 --

    def format_time(self) -> str:
        """``MM-DD HH:MM:SS``；跨年时带上年份。"""
        text = self.finished_at.strftime("%m-%d %H:%M:%S")
        if self.finished_at.year != datetime.now().year:
            text = self.finished_at.strftime("%Y-") + text
        return text

    def format_full_time(self) -> str:
        return self.finished_at.strftime("%Y-%m-%d %H:%M:%S")

    def describe(self) -> str:
        """单行摘要，例如 ``10-12 02:31:05 成功（清理前 12,345 → 0）``。"""
        outcome_text = {
            CleanOutcome.SUCCESS.value: "成功",
            CleanOutcome.PARTIAL.value: "部分清理",
            CleanOutcome.FAILED.value: "失败",
            CleanOutcome.INCOMPLETE.value: "结果未确认",
        }.get(self.outcome, self.outcome)
        detail = ""
        if self.items_before is not None:
            after = "?" if self.items_after is None else f"{self.items_after:,}"
            detail = f"（清理前 {self.items_before:,} → {after}）"
        return f"{self.format_time()} {outcome_text}{detail}"


class CleanHistory:
    """清理历史的读写入口（线程安全）。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else paths.clean_history_path()
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[CleanRecord]:
        """读取全部历史（最新在前）；文件不存在或损坏时返回空列表。"""
        with self._lock:
            if not self._path.exists():
                return []
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("清理历史读取失败（按空历史处理）：%s（%s）", self._path, exc)
                return []

            if not isinstance(raw, dict) or not isinstance(raw.get("records"), list):
                logger.error("清理历史格式非法（按空历史处理）：%s", self._path)
                return []

            records: list[CleanRecord] = []
            for item in raw["records"]:
                if not isinstance(item, dict):
                    continue
                try:
                    records.append(CleanRecord.from_dict(item))
                except CleanHistoryError as exc:
                    logger.warning("跳过一条非法清理历史：%s", exc)
            records.sort(key=lambda r: r.finished_at, reverse=True)
            return records

    def last(self) -> CleanRecord | None:
        """最近一次**执行过**的清理（不论成功失败）。"""
        records = self.load()
        return records[0] if records else None

    def last_success(self) -> CleanRecord | None:
        """最近一次**成功**清理。"""
        for record in self.load():
            if record.succeeded:
                return record
        return None

    def append(self, record: CleanRecord) -> bool:
        """追加一条记录，返回是否写入成功（失败时已记日志，不抛异常）。"""
        with self._lock:
            records = self.load()
            records.insert(0, record)
            records.sort(key=lambda r: r.finished_at, reverse=True)
            records = records[:MAX_RECORDS]

            payload = json.dumps(
                {"version": HISTORY_VERSION, "records": [r.to_dict() for r in records]},
                ensure_ascii=False,
                indent=2,
            ) + "\n"
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(self._path.suffix + ".tmp")
                tmp.write_text(payload, encoding="utf-8")
                tmp.replace(self._path)
            except OSError as exc:
                logger.error("清理历史写入失败：%s（%s）", self._path, exc)
                return False

        logger.info("已记录清理历史：%s", record.describe())
        return True

    def clear(self) -> None:
        """清空历史文件（供卸载 / 测试使用）。"""
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - 权限等极端情况
                logger.error("清理历史删除失败：%s（%s）", self._path, exc)


__all__ = ["CleanHistory", "CleanHistoryError", "CleanRecord", "MAX_RECORDS"]

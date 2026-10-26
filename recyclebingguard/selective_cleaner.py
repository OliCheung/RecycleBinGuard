"""选择性清理：只删除判定为垃圾的回收站条目，保留其余。

与 T1 的整盘清空（``SHEmptyRecycleBinW``）的区别：

======================== ============================== ==========================
维度                     整盘清空（clean_scope=everything） 选择性清理（默认）
======================== ============================== ==========================
删除范围                 回收站里的一切                     只删判定为垃圾的条目
用户手动删除的文件       一起被删                           永久保留
实现                     一次 Shell API 调用                逐条 ``SHFileOperationW``
可撤销性                 无                                 删除前条目仍在回收站
======================== ============================== ==========================

**删除顺序是安全关键**：先删 ``$R`` 数据，成功后才删配对的 ``$I`` 记录。

- 先删 ``$R`` 失败 → ``$I`` 保持不动，条目仍完整地留在回收站里，可重试；
- 若反过来先删 ``$I``，``$R`` 就变成用户看不见、也回收不了空间的孤儿文件。

本模块只处理**自己索引出来的、非不透明**的条目；任何无法确认归属的条目
一律跳过并计入 ``skipped``，绝不猜测。
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from .logging_setup import get_logger
from .recycle_bin_index import RecycleEntry

logger = get_logger()

#: ``SHFILEOPSTRUCTW`` 的删除操作码（shellapi.h）。
FO_DELETE = 3

#: ``SHFileOperationW`` 标志：不弹确认框 / 不显示进度 / 静默 / 出错不弹 UI / 保留撤销。
FOF_ALLOWUNDO = 0x0040
FOF_NOCONFIRMATION = 0x0010
FOF_SILENT = 0x0004
FOF_NOERRORUI = 0x0400
FOF_NOCONFIRMMKDIR = 0x0200

#: 删除垃圾条目时使用的标志。
#:
#: 刻意**不用** ``FOF_ALLOWUNDO``：这些文件已经在回收站里了，而 ``$R`` 本身是隐藏的
#: 系统目录，再“移到回收站”会失败或产生嵌套的回收站条目。直接用 Shell 删除，
#: 语义就是“从回收站里彻底移除”。
#:
#: ``FOF_NOCONFIRMATION`` 只是关闭系统确认框 —— 我们自己的确认弹窗已经问过了；
#: 用户取消确认弹窗时根本不会走到这里。
DEFAULT_DELETE_FLAGS = FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR

#: 单个条目的删除结果。
ERROR_ACCESS_DENIED = 5


@dataclass(frozen=True)
class DeleteOutcome:
    """单个条目的删除结果。"""

    entry: RecycleEntry
    ok: bool
    #: ``SHFileOperationW`` 的返回码；``0`` 表示成功。
    code: int = 0
    error: str | None = None
    #: 数据已删除但 ``$I`` 记录清理失败：此时条目会从回收站里「消失」但记录残留。
    meta_orphaned: bool = False

    @property
    def paths(self) -> tuple[str, ...]:
        return self.entry.data_paths


@dataclass
class SelectiveCleanResult:
    """一次选择性清理的汇总。"""

    started_at: datetime = field(default_factory=datetime.now)
    outcomes: list[DeleteOutcome] = field(default_factory=list)
    #: 未参与清理的条目数（不透明 / 无 ``$R`` / 被跳过）。
    skipped_items: int = 0
    duration_seconds: float = 0.0

    @property
    def deleted_items(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.ok)

    @property
    def failed_items(self) -> int:
        return sum(1 for outcome in self.outcomes if not outcome.ok)

    @property
    def deleted_entries(self) -> int:
        """实际删除的条目递归数（= 按文件计数时清掉的个数）。"""
        return sum(outcome.entry.recursive_entries for outcome in self.outcomes if outcome.ok)

    @property
    def meta_orphans(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.meta_orphaned)

    @property
    def succeeded(self) -> bool:
        """没有任何失败条目才算成功。"""
        return self.failed_items == 0

    def first_error(self) -> str | None:
        for outcome in self.outcomes:
            if outcome.error:
                return f"{outcome.entry.original_path or outcome.entry.data_path}：{outcome.error}"
        return None

    def describe(self) -> str:
        text = (
            f"删除 {self.deleted_items} 项（{self.deleted_entries} 个文件）"
            f"，跳过 {self.skipped_items} 项"
        )
        if self.failed_items:
            text += f"，失败 {self.failed_items} 项"
        if self.meta_orphans:
            text += f"，记录残留 {self.meta_orphans} 项"
        return text


class SHFILEOPSTRUCTW(ctypes.Structure):
    """对应 shellapi.h 的 ``SHFILEOPSTRUCTW``（64 位下 ``fFlags`` 为 ``FILEOP_FLAGS``）。"""

    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("wFunc", ctypes.c_uint32),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_uint16),
        ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def _delete_paths(paths: list[str], flags: int) -> int:
    """调用 ``SHFileOperationW`` 删除一批路径，返回其原始返回码（0 = 成功）。"""
    if not paths:
        return 0
    # pFrom 必须是「\0 分隔、双 \0 结尾」的多字符串。
    joined = "\0".join(paths) + "\0\0"
    operation = SHFILEOPSTRUCTW(
        hwnd=None,
        wFunc=FO_DELETE,
        pFrom=joined,
        pTo=None,
        fFlags=flags,
        fAnyOperationsAborted=0,
        hNameMappings=None,
        lpszProgressTitle=None,
    )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    shell32.SHFileOperationW.restype = ctypes.c_int
    return int(shell32.SHFileOperationW(ctypes.byref(operation)))


def _remove_meta(path: str) -> int | None:
    """删除 ``$I`` 记录，返回错误码（``None`` = 成功）。"""
    try:
        os.remove(path)
        return None
    except FileNotFoundError:
        return None  # 已经不在了，视为成功
    except OSError as exc:
        return exc.winerror or exc.errno or -1


def describe_delete_code(code: int) -> str:
    """把 ``SHFileOperationW`` 返回码翻译成可读文本。

    注意：该 API 返回的是 ``DE_*`` 错误码，**不是** ``HRESULT``，不能套用
    :func:`recyclebingguard.recycle_bin_service.hresult_message`。
    """
    if code == 0:
        return "成功"
    messages = {
        0x71: "操作被取消",
        0x74: "源文件不存在",
        0x75: "无法读取源文件",
        0x78: "无法删除文件",
        0x79: "拒绝访问",
        0x7C: "路径无效",
        0x7D: "目标路径无效",
        0x7E: "文件名无效",
        0x7F: "路径过长",
        0x81: "无法移动文件",
        0x83: "无法重命名",
        0x84: "源文件和目标文件相同",
        0x86: "无法找到文件",
        0x87: "文件已被占用",
        0x88: "磁盘已满",
        0x89: "磁盘被写保护",
        0x8B: "文件已存在",
        0x8E: "文件被占用",
        0x10000 + 0x74: "源文件不存在（扩展码）",
    }
    return f"{messages.get(code, '未知错误')}（DE 码 0x{code:04X}）"


class SelectiveCleaner:
    """按条目删除回收站内容；可注入删除函数便于测试。"""

    def __init__(
        self,
        *,
        delete_paths: object = None,
        remove_meta: object = None,
        flags: int = DEFAULT_DELETE_FLAGS,
    ) -> None:
        #: 注入点：签名 ``(paths: list[str], flags: int) -> int``。
        self._delete_paths = delete_paths or _delete_paths
        #: 注入点：签名 ``(path: str) -> int | None``。
        self._remove_meta = remove_meta or _remove_meta
        self._flags = flags

    def delete_entry(self, entry: RecycleEntry) -> DeleteOutcome:
        """删除单个条目：先删 ``$R`` 数据，成功后再删 ``$I`` 记录。"""
        if entry.opaque:
            return DeleteOutcome(entry, ok=False, error="不透明条目：拒绝删除")
        data_paths = list(entry.data_paths)
        if not data_paths:
            return DeleteOutcome(entry, ok=False, error="条目没有对应的 $R 数据路径")

        code = self._delete_paths(data_paths, self._flags)  # type: ignore[operator]
        if code != 0:
            reason = describe_delete_code(code)
            logger.error(
                "删除回收站条目失败：%s 原因=%s（$I 记录保留，条目仍在回收站中）",
                entry.original_path or data_paths[0],
                reason,
            )
            return DeleteOutcome(entry, ok=False, code=code, error=reason)

        # 数据已删除：无论 $I 清理是否成功，数据都不可能恢复了。
        meta_code = None
        if entry.meta_path:
            meta_code = self._remove_meta(entry.meta_path)  # type: ignore[operator]
        if meta_code is not None:
            logger.warning("数据已删除但 $I 记录清理失败：%s 错误码=%s", entry.meta_path, meta_code)
            return DeleteOutcome(
                entry, ok=True, code=0, meta_orphaned=True, error=f"$I 清理失败（{meta_code}）"
            )

        logger.info(
            "已删除回收站条目：%s（递归条目数=%d）",
            entry.original_path or data_paths[0],
            entry.recursive_entries,
        )
        return DeleteOutcome(entry, ok=True, code=0)

    def delete_entries(
        self, entries: list[RecycleEntry], *, skipped_items: int = 0
    ) -> SelectiveCleanResult:
        """批量删除；单个条目失败不会中断其余条目的清理。

        刻意**不**在失败时中止：一次清理里某个文件被占用是常见情况，
        中止只会让其余垃圾一直堆着。失败条目保持原样，下一轮还能重试。
        """
        started = time.perf_counter()
        result = SelectiveCleanResult(skipped_items=skipped_items)
        for entry in entries:
            outcome = self.delete_entry(entry)
            result.outcomes.append(outcome)
            if not outcome.ok:
                logger.debug("跳过失败条目：%s", outcome.error)
        result.duration_seconds = round(time.perf_counter() - started, 6)
        logger.info("选择性清理结束：%s（耗时 %.3fs）", result.describe(), result.duration_seconds)
        return result


__all__ = [
    "DEFAULT_DELETE_FLAGS",
    "DeleteOutcome",
    "FO_DELETE",
    "FOF_NOCONFIRMATION",
    "FOF_NOERRORUI",
    "FOF_SILENT",
    "SHFILEOPSTRUCTW",
    "SelectiveCleanResult",
    "SelectiveCleaner",
    "describe_delete_code",
]

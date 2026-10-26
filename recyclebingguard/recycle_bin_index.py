"""回收站条目索引：读取 ``$I`` 记录，还原「原始路径 / 删除时间 / 条目树」。

为什么需要它：

- ``SHQueryRecycleBinW`` 只给出**顶层条目数**，删一个含 500 个文件的文件夹只算 1 项，
  因此无法回答“回收站里到底堆了多少文件”；
- ``$I`` 记录里保存着每个条目被删除前的**完整路径**与**删除时间**，这是区分
  “vibecoding 产生的临时文件”和“用户手动删除的文件”唯一的依据。

实现边界（务必注意）：

- 只读取**当前用户 SID** 下的 ``$I`` / ``$R``；其它用户的目录没有读取权限
  （实测 ``WinError 5``），这些条目记为 :attr:`RecycleEntry.opaque` 且**永不参与清理**；
- 本模块**只读**：不删除、不改名、不写入回收站目录；
- 文件损坏、版本未知、路径解不出来时优雅降级，绝不因为单条异常中断整次扫描；
- 单盘读取失败只把该盘记为失败，由上层沿用“统计不完整就不清理”的既有安全策略。
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from .logging_setup import get_logger

logger = get_logger()

#: 回收站目录名（每个磁盘根各一个）。
RECYCLE_DIR_NAME = "$Recycle.Bin"

#: ``$I`` 记录头的固定长度：版本 + 文件大小 + 删除时间（FILETIME）。
_HEADER_SIZE = 24

#: ``$I`` 版本 2 在头部后紧跟路径长度（4 字节）与 UTF-16 路径。
_VERSION_UTF16 = 2

#: 单个 ``$I`` 文件的大小上限（防御损坏文件导致超大读取）。
_MAX_META_BYTES = 64 * 1024

#: Windows FILETIME 起点与 Unix 起点的秒差。
_FILETIME_EPOCH_OFFSET = 11644473600

#: 目录遍历时单个路径的最大深度，避免符号链接成环时栈溢出。
_MAX_WALK_DEPTH = 64


def filetime_to_datetime(value: int) -> datetime | None:
    """把 Windows FILETIME（100ns 自 1601-01-01）转成本地时间；非法值返回 ``None``。"""
    if value <= 0:
        return None
    try:
        seconds = value / 10_000_000 - _FILETIME_EPOCH_OFFSET
        if seconds < 0:
            return None
        return datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class RecycleEntry:
    """回收站里的一个**顶层条目**（一条 ``$I`` + 对应的 ``$R``）。"""

    #: 原始完整路径（删除前的位置）；解析失败时为空串。
    original_path: str
    #: 删除时间；``$I`` 损坏时为 ``None``。
    deleted_at: datetime | None = None
    #: 条目自身大小（``$I`` 记录的大小字段；目录通常为 0）。
    size_bytes: int = 0
    #: 该条目是否为目录。
    is_dir: bool = False
    #: 递归统计的条目数（文件 + 子目录）；非目录为 1。不透明项为 0。
    recursive_entries: int = 1
    #: 是否位于无法读取的用户目录：**永不参与清理**。
    opaque: bool = False
    #: 所属磁盘根（形如 ``C:\\``）。
    root: str = ""
    #: ``$I`` 文件路径（清理时用于成对删除），不透明项为空。
    meta_path: str = ""
    #: ``$R`` 数据路径，不透明项为空。
    data_path: str = ""
    #: 可能的多个 ``$R`` 文件（多段记录）；正常只有一项。
    extra_data_paths: tuple[str, ...] = ()

    @property
    def data_paths(self) -> tuple[str, ...]:
        """该条目在 ``$R`` 下对应的全部路径。"""
        paths: list[str] = []
        if self.data_path:
            paths.append(self.data_path)
        paths.extend(self.extra_data_paths)
        return tuple(paths)


@dataclass(frozen=True)
class DriveIndex:
    """单个磁盘的索引结果。"""

    root: str
    entries: tuple[RecycleEntry, ...] = ()
    #: 当前用户 SID 目录的数量（正常为 1）。
    readable_user_dirs: int = 0
    #: 无法读取的用户目录数量（其中的条目记为不透明项）。
    opaque_user_dirs: int = 0
    #: 读取失败原因；``None`` 表示成功。
    error: str | None = None
    #: 扫描耗时（秒）。
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class RecycleIndex:
    """全部纳入磁盘的索引结果。"""

    drives: tuple[DriveIndex, ...] = ()
    scanned_at: datetime = field(default_factory=datetime.now)
    #: 不参与索引的磁盘根（排除 / 非固定盘）。
    skipped_roots: tuple[str, ...] = ()
    #: 无法列出 ``$Recycle.Bin`` 的磁盘（如未启用回收站的盘）。
    failed_roots: tuple[str, ...] = ()

    @property
    def entries(self) -> tuple[RecycleEntry, ...]:
        return tuple(entry for drive in self.drives for entry in drive.entries)

    @property
    def is_complete(self) -> bool:
        return not self.failed_roots

    def describe(self) -> str:
        total = len(self.entries)
        opaque = sum(1 for entry in self.entries if entry.opaque)
        return (
            f"回收站索引：磁盘={[d.root for d in self.drives]} "
            f"条目={total} 不透明={opaque} 失败磁盘={list(self.failed_roots)}"
        )


@runtime_checkable
class RecycleBinIndexer(Protocol):
    """回收站索引器的契约（便于测试注入替身）。"""

    def build_index(self, roots: list[tuple[str, object]] | None = None) -> RecycleIndex:
        """为给定盘符建立索引；``None`` 表示自动枚举本机固定磁盘。"""
        ...


# --------------------------------------------------------------------------
# 路径工具
# --------------------------------------------------------------------------


def recycle_bin_dir(root: str) -> str:
    """返回某磁盘的回收站目录（形如 ``C:\\$Recycle.Bin``）。"""
    return os.path.join(root, RECYCLE_DIR_NAME)


def current_sid() -> str | None:
    """返回当前进程用户的 SID 字符串；失败时返回 ``None``。

    回收站按 SID 分目录存放，只有自己的目录可读。拿不到 SID 时调用方应
    退化为“只读取 ``$Recycle.Bin`` 下所有**可读**的子目录”。
    """
    if os.name != "nt":
        return None
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        advapi32.OpenProcessToken.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.OpenProcessToken.restype = ctypes.c_int
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        token = ctypes.c_void_p()
        # TOKEN_QUERY = 0x0008
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return None
        try:
            size = ctypes.c_uint32(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            if size.value <= 0:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, 1, buffer, size, ctypes.byref(size)
            ):  # pragma: no cover - 极少失败
                return None
            sid_ptr = ctypes.c_void_p.from_buffer(buffer).value
            if not sid_ptr:  # pragma: no cover - 结构异常
                return None
            advapi32.ConvertSidToStringSidW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
            text = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(text)):
                return None
            try:
                return text.value
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # pragma: no cover - 非 Windows / 极端环境
        logger.debug("读取当前用户 SID 失败", exc_info=True)
        return None


def count_tree_entries(path: str, *, max_depth: int = _MAX_WALK_DEPTH) -> int:
    """递归统计目录下的条目数（文件 + 子目录，不含目录自身）。

    遍历失败（权限、路径消失）只计入已读到的部分，绝不抛异常。
    """
    total = 0
    stack: list[tuple[str, int]] = [(path, 0)]
    while stack:
        current, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            with os.scandir(current) as it:
                for item in it:
                    total += 1
                    try:
                        if item.is_dir(follow_symlinks=False):
                            stack.append((item.path, depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return total


# --------------------------------------------------------------------------
# ``$I`` 解析
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _MetaRecord:
    version: int
    size_bytes: int
    deleted_at: datetime | None
    original_path: str


def _decode_path_utf16(payload: bytes) -> str:
    text = payload.decode("utf-16-le", "replace")
    return text.split("\x00", 1)[0]


def parse_meta(raw: bytes) -> _MetaRecord | None:
    """解析一条 ``$I`` 记录；无法识别时返回 ``None``。"""
    if len(raw) < _HEADER_SIZE:
        return None
    version, size_bytes, filetime = struct.unpack("<qqq", raw[0:24])
    deleted_at = filetime_to_datetime(filetime)
    if version == _VERSION_UTF16:
        if len(raw) < 28:
            return None
        (chars,) = struct.unpack("<i", raw[24:28])
        if chars <= 0:
            return None
        end = 28 + chars * 2
        payload = raw[28:end]
        if not payload:
            return None
        return _MetaRecord(version, size_bytes, deleted_at, _decode_path_utf16(payload))
    if version == 1:
        payload = raw[24:]
        if not payload:
            return None
        text = payload.decode("mbcs", "replace").split("\x00", 1)[0]
        if not text:
            return None
        return _MetaRecord(version, size_bytes, deleted_at, text)
    return None


# --------------------------------------------------------------------------
# 索引器
# --------------------------------------------------------------------------


class WindowsRecycleBinIndexer:
    """基于文件系统的只读索引器。"""

    def __init__(self, *, included_roots: list[str] | None = None) -> None:
        #: ``None`` 表示按固定磁盘自动枚举（与 T1 的统计范围保持一致）。
        self._included_roots = included_roots
        self._sid = current_sid()

    # -- 磁盘范围 --

    def _fixed_roots(self) -> list[str]:
        if self._included_roots is not None:
            return list(self._included_roots)
        if os.name != "nt":
            return []
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetLogicalDrives.argtypes = []
        kernel32.GetLogicalDrives.restype = ctypes.c_uint32
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint32
        mask = int(kernel32.GetLogicalDrives())
        roots: list[str] = []
        for index in range(26):
            if not mask & (1 << index):
                continue
            root = f"{chr(ord('A') + index)}:\\"
            if int(kernel32.GetDriveTypeW(root)) == 3:  # DRIVE_FIXED
                roots.append(root)
        return roots

    # -- 主入口 --

    def build_index(self, roots: list[tuple[str, object]] | None = None) -> RecycleIndex:
        """建立索引。``roots`` 为 ``[(盘符, 类型)]`` 时只索引这些盘。"""
        if roots is not None:
            targets = [root for root, _ in roots]
        else:
            targets = self._fixed_roots()

        drives: list[DriveIndex] = []
        failed: list[str] = []
        for root in targets:
            index = self.index_drive(root)
            drives.append(index)
            if not index.ok:
                failed.append(root)
                logger.warning("回收站索引失败：磁盘=%s 原因=%s", root, index.error)

        result = RecycleIndex(drives=tuple(drives), failed_roots=tuple(failed))
        logger.info("%s（耗时 %.3fs）", result.describe(), sum(d.seconds for d in drives))
        return result

    def index_drive(self, root: str, *, side_dir: str | None = None) -> DriveIndex:
        """索引单个磁盘的回收站目录。

        :param root: 磁盘根（形如 ``C:\\``），只用于标记条目归属。
        :param side_dir: 回收站目录的真实路径；``None`` 表示按 ``root`` 推导。
            测试可指向临时目录，从而完全不触碰真实回收站。
        """
        started = time.perf_counter()
        side = side_dir if side_dir is not None else recycle_bin_dir(root)
        if not os.path.isdir(side):
            return DriveIndex(
                root=root,
                error=f"回收站目录不可用：{side}",
                seconds=time.perf_counter() - started,
            )

        try:
            user_dirs = [name for name in os.listdir(side) if _is_sid_like(name)]
        except OSError as exc:
            return DriveIndex(root=root, error=f"无法列出回收站：{exc}", seconds=0.0)

        entries: list[RecycleEntry] = []
        readable = 0
        opaque_dirs = 0

        for name in user_dirs:
            folder = os.path.join(side, name)
            if not os.path.isdir(folder):
                continue
            try:
                names = os.listdir(folder)
            except OSError:
                # 其它用户的回收站目录：无权限，只能记一个不透明项。
                opaque_dirs += 1
                entries.append(
                    RecycleEntry(
                        original_path="",
                        is_dir=True,
                        recursive_entries=0,
                        opaque=True,
                        root=root,
                    )
                )
                continue
            readable += 1
            entries.extend(self._entries_from_folder(root, folder, names))

        return DriveIndex(
            root=root,
            entries=tuple(entries),
            readable_user_dirs=readable,
            opaque_user_dirs=opaque_dirs,
            seconds=time.perf_counter() - started,
        )

    def _entries_from_folder(self, root: str, folder: str, names: list[str]) -> list[RecycleEntry]:
        """把单个用户目录下的 ``$I`` / ``$R`` 配对成条目。"""
        meta_names = {name for name in names if name.startswith("$I")}
        data_names = {name for name in names if name.startswith("$R")}
        # ``$I<key><ext>`` 与 ``$R<key><ext>`` 共用同一个 key，必须用**同一套**
        # 归一化规则取 key（早期版本对 $I 直接截前两字符，导致几乎全部 $R 被误判成孤儿）。
        meta_by_key: dict[str, list[str]] = {}
        for name in meta_names:
            meta_by_key.setdefault(_entry_key(name), []).append(name)
        data_by_key: dict[str, list[str]] = {}
        for name in data_names:
            data_by_key.setdefault(_entry_key(name), []).append(name)

        entries: list[RecycleEntry] = []
        for key in sorted(meta_by_key):
            meta_name = sorted(meta_by_key[key])[0]
            meta_path = os.path.join(folder, meta_name)
            record = self._read_meta(meta_path)
            candidates = sorted(data_by_key.pop(key, []))
            data_paths = tuple(os.path.join(folder, name) for name in candidates)
            head = data_paths[0] if data_paths else ""
            is_dir = bool(head) and os.path.isdir(head)
            recursive = count_tree_entries(head) if is_dir else (1 if head else 0)
            entries.append(
                RecycleEntry(
                    original_path=record.original_path if record else "",
                    deleted_at=record.deleted_at if record else None,
                    size_bytes=record.size_bytes if record else 0,
                    is_dir=is_dir,
                    recursive_entries=recursive,
                    opaque=False,
                    root=root,
                    meta_path=meta_path,
                    data_path=head,
                    extra_data_paths=data_paths[1:],
                )
            )

        # 只有 $R 没有 $I 的孤儿数据（记录被外部删掉）：无法得知原路径，
        # 保守地记为不透明项，不参与清理，但让它出现在索引里以便诊断。
        for leftover in sorted(data_by_key.values()):
            for name in sorted(leftover):
                entries.append(
                    RecycleEntry(
                        original_path="",
                        is_dir=True,
                        recursive_entries=0,
                        opaque=True,
                        root=root,
                        data_path=os.path.join(folder, name),
                    )
                )
        return entries

    @staticmethod
    def _read_meta(path: str) -> _MetaRecord | None:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(_MAX_META_BYTES)
        except OSError:
            logger.debug("读取 $I 失败：%s", path, exc_info=True)
            return None
        record = parse_meta(raw)
        if record is None:
            logger.debug("无法解析 $I：%s", path)
        return record


def _is_sid_like(name: str) -> bool:
    """回收站下的用户目录名形如 ``S-1-5-21-...``。"""
    return name.upper().startswith("S-1-") or name.upper() == "S-1-5-18"


def _entry_key(name: str) -> str:
    """把 ``$I<key>`` / ``$R<key>``（含多段形式 ``$R<key>-1.ext``）归一成同一个 key。

    取前两个字符之后的全部内容作为 key：``$I`` 记录自己的扩展名通常是随机的
    （实测形如 ``$I06LBNF.jpg``），而多段 ``$R`` 用 ``<key>-<n>.<ext>``，
    因此必须保留后缀部分才能对上。大小写按 Windows 惯例统一。
    """
    return name[2:].upper()


__all__ = [
    "DriveIndex",
    "RECYCLE_DIR_NAME",
    "RecycleBinIndexer",
    "RecycleEntry",
    "RecycleIndex",
    "WindowsRecycleBinIndexer",
    "count_tree_entries",
    "current_sid",
    "filetime_to_datetime",
    "parse_meta",
    "recycle_bin_dir",
]

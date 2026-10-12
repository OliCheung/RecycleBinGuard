"""T1：Windows 回收站统计与清空核心服务。

设计约束（PRD 第 6 节）：

- 只使用 Windows 原生 Shell API：``SHQueryRecycleBinW`` / ``SHEmptyRecycleBinW``；
- **不**遍历、删除或重命名 ``$Recycle.Bin`` 内的 ``$I`` / ``$R`` 文件；
- 统计按磁盘进行后求和，任一应纳入磁盘查询失败即视为 ``incomplete``；
- 清空必须显式调用，:meth:`RecycleBinService.scan` 无任何副作用；
- 清空前二次核验、清空后重新统计复核，且不无限重复清空；
- 清空范围 = 已纳入统计的本机固定磁盘；逐盘调用，避免误清可移动盘/网络盘；
- 本模块不含定时器、托盘 UI 与开机启动逻辑。

统计口径：单个磁盘取 ``SHQUERYRBINFO.i64NumItems``，总数为所有成功查询的
本机固定磁盘之和；不使用目录内 ``$I`` / ``$R`` 文件数量。

第一版磁盘策略：

============ ==================
磁盘类型     策略
============ ==================
固定磁盘     纳入统计与清空
可移动盘     不统计
网络盘       不扫描
光驱/内存盘  不统计
未挂载卷     仅支持盘符，卷挂载点（文件夹挂载）不纳入
============ ==================
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from .interfaces import (
    INCLUDED_DRIVE_KINDS,
    CleanOutcome,
    CleanResult,
    DiskScanResult,
    DriveKind,
    ScanResult,
)
from .logging_setup import get_logger

logger = get_logger()

# --------------------------------------------------------------------------
# SHEmptyRecycleBinW 标志（shellapi.h）
# --------------------------------------------------------------------------

SHERB_NOCONFIRMATION = 0x00000001
SHERB_NOPROGRESSUI = 0x00000002
SHERB_NOSOUND = 0x00000004

#: 默认清空标志：不弹系统确认框、不显示进度对话框。
#: 刻意不使用任何“静默错误”式标志，错误必须由本模块显式处理与上报。
DEFAULT_EMPTY_FLAGS = SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI

# GetDriveTypeW 返回值（winbase.h）
_DRIVE_UNKNOWN = 0
_DRIVE_NO_ROOT_DIR = 1
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_CDROM = 5
_DRIVE_RAMDISK = 6

_DRIVE_KIND_BY_TYPE: dict[int, DriveKind] = {
    _DRIVE_FIXED: DriveKind.FIXED,
    _DRIVE_REMOVABLE: DriveKind.REMOVABLE,
    _DRIVE_REMOTE: DriveKind.NETWORK,
    _DRIVE_CDROM: DriveKind.CDROM,
    _DRIVE_RAMDISK: DriveKind.RAMDISK,
    _DRIVE_UNKNOWN: DriveKind.UNKNOWN,
    _DRIVE_NO_ROOT_DIR: DriveKind.UNKNOWN,
}


# --------------------------------------------------------------------------
# 结构体与 HRESULT 工具
# --------------------------------------------------------------------------


class SHQUERYRBINFO(ctypes.Structure):
    """对应 shellapi.h 的 ``SHQUERYRBINFO``。

    ``_pack_ = 8`` 强制 8 字节对齐，使 ``cbSize`` 在 32/64 位下都等于
    Windows 头文件中的 ``sizeof(SHQUERYRBINFO)``（24 字节）。
    使用固定宽度类型而非 ``wintypes``，避免在非 Windows 上因 ``c_ulong``
    宽度不同导致结构体大小错误。
    """

    _pack_ = 8
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("i64Size", ctypes.c_int64),
        ("i64NumItems", ctypes.c_int64),
    ]


#: Windows 头文件中的期望大小，用于自检。
EXPECTED_SHQUERYRBINFO_SIZE = 24


def hresult_ok(hresult: int) -> bool:
    """``SUCCEEDED(hresult)``：非负即成功（含 ``S_FALSE``）。"""
    return hresult >= 0


def hresult_message(hresult: int) -> str:
    """把 HRESULT 转成可诊断的文本。"""
    if hresult >= 0:
        return "S_OK" if hresult == 0 else f"S_FALSE (0x{hresult:08X})"
    if os.name == "nt":
        try:
            return f"{ctypes.FormatError(hresult)} (0x{hresult & 0xFFFFFFFF:08X})"
        except Exception:  # pragma: no cover - 格式化失败时退化为十六进制
            pass
    return f"HRESULT 0x{hresult & 0xFFFFFFFF:08X}"


# --------------------------------------------------------------------------
# Shell API 抽象层（便于测试注入 fake）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RawQuery:
    """``SHQueryRecycleBinW`` 的原始返回。"""

    hresult: int
    num_items: int = 0
    size_bytes: int = 0


@runtime_checkable
class ShellApi(Protocol):
    """Windows Shell 的最小抽象，:class:`RecycleBinService` 只依赖这三个方法。"""

    def iter_drives(self) -> list[tuple[str, DriveKind]]:
        """枚举本机盘符及其类型，形如 ``[("C:\\\\", DriveKind.FIXED)]``。"""
        ...

    def query(self, root: str) -> RawQuery:
        """查询单个盘符的回收站统计，不产生副作用。"""
        ...

    def empty(self, root: str | None, flags: int) -> int:
        """清空回收站并返回 HRESULT。

        ``root`` 为 ``None`` 表示清空所有磁盘的回收站。
        """
        ...


class WindowsShellApi:
    """基于 ``ctypes`` 的真实实现；仅在 Windows 上可实例化。"""

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("WindowsShellApi 仅支持 Windows 平台")
        if ctypes.sizeof(SHQUERYRBINFO) != EXPECTED_SHQUERYRBINFO_SIZE:
            raise RuntimeError(
                "SHQUERYRBINFO 结构体大小异常："
                f"{ctypes.sizeof(SHQUERYRBINFO)} != {EXPECTED_SHQUERYRBINFO_SIZE}"
            )

        self._shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        self._shell32.SHQueryRecycleBinW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(SHQUERYRBINFO),
        ]
        self._shell32.SHQueryRecycleBinW.restype = ctypes.c_int32

        self._shell32.SHEmptyRecycleBinW.argtypes = [
            ctypes.c_void_p,   # HWND，None 表示无属主窗口
            ctypes.c_wchar_p,  # pszRootPath，None 表示所有驱动器
            ctypes.c_uint32,   # dwFlags
        ]
        self._shell32.SHEmptyRecycleBinW.restype = ctypes.c_int32

        self._kernel32.GetLogicalDrives.argtypes = []
        self._kernel32.GetLogicalDrives.restype = ctypes.c_uint32

        self._kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        self._kernel32.GetDriveTypeW.restype = ctypes.c_uint32

    # -- ShellApi 实现 --

    def iter_drives(self) -> list[tuple[str, DriveKind]]:
        mask = int(self._kernel32.GetLogicalDrives())
        drives: list[tuple[str, DriveKind]] = []
        for index in range(26):
            if not mask & (1 << index):
                continue
            root = f"{chr(ord('A') + index)}:\\"
            drives.append((root, self._drive_kind(root)))
        return drives

    def query(self, root: str) -> RawQuery:
        info = SHQUERYRBINFO(cbSize=ctypes.sizeof(SHQUERYRBINFO))
        hresult = int(self._shell32.SHQueryRecycleBinW(root, ctypes.byref(info)))
        if not hresult_ok(hresult):
            # 失败时结构体内容不可信，计数一律归零。
            return RawQuery(hresult=hresult)
        return RawQuery(
            hresult=hresult,
            num_items=int(info.i64NumItems),
            size_bytes=int(info.i64Size),
        )

    def empty(self, root: str | None, flags: int) -> int:
        return int(self._shell32.SHEmptyRecycleBinW(None, root, flags))

    def _drive_kind(self, root: str) -> DriveKind:
        drive_type = int(self._kernel32.GetDriveTypeW(root))
        return _DRIVE_KIND_BY_TYPE.get(drive_type, DriveKind.UNKNOWN)


class RecordingShellApi:
    """包装任意 :class:`ShellApi`，记录调用序列（供测试断言“未调用清空”）。"""

    def __init__(self, inner: ShellApi) -> None:
        self._inner = inner
        self.queries: list[str] = []
        self.empties: list[tuple[str | None, int]] = []

    def iter_drives(self) -> list[tuple[str, DriveKind]]:
        return self._inner.iter_drives()

    def query(self, root: str) -> RawQuery:
        self.queries.append(root)
        return self._inner.query(root)

    def empty(self, root: str | None, flags: int) -> int:
        self.empties.append((root, flags))
        return self._inner.empty(root, flags)


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------


class RecycleBinService:
    """回收站统计与清空服务。

    实现 :class:`recyclebingguard.interfaces.RecycleBinService` 协议。
    """

    def __init__(
        self,
        api: ShellApi | None = None,
        *,
        included_kinds: frozenset[DriveKind] = INCLUDED_DRIVE_KINDS,
        empty_flags: int = DEFAULT_EMPTY_FLAGS,
    ) -> None:
        self._api: ShellApi = api if api is not None else WindowsShellApi()
        self._included_kinds = included_kinds
        self._empty_flags = empty_flags

    # -- 只读统计 --

    def list_all_drives(self) -> list[tuple[str, DriveKind]]:
        """枚举本机所有盘符及其类型（含不纳入统计的类型，供诊断使用）。"""
        return list(self._api.iter_drives())

    def scan(self) -> ScanResult:
        """统计所有应纳入磁盘的回收站项目数。

        纯只读操作，绝不触发清空。任一应纳入磁盘查询失败、或未枚举到任何
        固定磁盘时，返回结果的 :attr:`ScanResult.is_complete` 为 ``False``。
        """
        drives = self._api.iter_drives()
        included = [
            (root, kind) for root, kind in drives if kind in self._included_kinds
        ]
        skipped = [
            (root, kind) for root, kind in drives if kind not in self._included_kinds
        ]

        disks: list[DiskScanResult] = []
        total_items = 0
        total_size = 0
        failed_roots: list[str] = []

        for root, kind in included:
            raw = self._api.query(root)
            if hresult_ok(raw.hresult) and raw.num_items >= 0 and raw.size_bytes >= 0:
                disks.append(
                    DiskScanResult(
                        root=root,
                        kind=kind,
                        ok=True,
                        num_items=raw.num_items,
                        size_bytes=raw.size_bytes,
                        hresult=raw.hresult,
                    )
                )
                total_items += raw.num_items
                total_size += raw.size_bytes
            else:
                reason = (
                    hresult_message(raw.hresult)
                    if not hresult_ok(raw.hresult)
                    else f"接口返回异常计数（items={raw.num_items}, size={raw.size_bytes}）"
                )
                disks.append(
                    DiskScanResult(
                        root=root,
                        kind=kind,
                        ok=False,
                        num_items=0,
                        size_bytes=0,
                        hresult=raw.hresult,
                        error=reason,
                    )
                )
                failed_roots.append(root)
                logger.warning("回收站统计失败：磁盘=%s 原因=%s", root, reason)

        error: str | None = None
        if not included:
            error = "未枚举到任何本机固定磁盘，统计不可用"
            logger.warning("回收站统计不可用：%s；跳过的磁盘=%s", error, skipped)

        result = ScanResult(
            total_items=total_items,
            total_size_bytes=total_size,
            scanned_roots=tuple(root for root, _ in included),
            failed_roots=tuple(failed_roots),
            queried_at=datetime.now(),
            disks=tuple(disks),
            error=error,
        )
        logger.info(
            "回收站统计：总数=%d 大小=%d 纳入磁盘=%s 失败磁盘=%s 完整=%s",
            result.total_items,
            result.total_size_bytes,
            list(result.scanned_roots),
            list(result.failed_roots),
            result.is_complete,
        )
        return result

    # -- 清空 --

    def empty(self) -> CleanResult:
        """清空回收站。

        流程：**清空前复核** → 对每个已纳入统计的固定磁盘调用
        ``SHEmptyRecycleBinW`` → **清空后复核**。

        清空范围刻意限定为“已纳入统计的本机固定磁盘”，逐盘调用而非传
        ``NULL``（``NULL`` 会清空所有驱动器，包括本次未统计的可移动盘与网络盘）。

        结果判定：

        - 清空前复核不完整（含未枚举到固定磁盘）：``INCOMPLETE``，
          **不调用**清空 API；
        - 所有盘清空调用都失败且复核仍有项目：``FAILED``；
        - 部分调用成功、复核仍有项目：``PARTIAL``（**不重复清空**）；
        - 清空后复核不完整：``INCOMPLETE``（结果未确认）；
        - 清空后复核为 0：``SUCCESS``（复核为最终依据）。
        """
        started_at = datetime.now()
        started_perf = time.perf_counter()

        def elapsed() -> float:
            return round(time.perf_counter() - started_perf, 6)

        # 1) 清空前二次核验
        pre = self.scan()
        if not pre.is_complete or not pre.scanned_roots:
            message = (
                "清空前复核统计不完整，已拒绝清空"
                + (f"：{pre.error}" if pre.error else "")
                + (
                    f"（失败磁盘：{list(pre.failed_roots)}）"
                    if pre.failed_roots
                    else ""
                )
            )
            logger.error("%s；清空 API 未被调用", message)
            return CleanResult(
                outcome=CleanOutcome.INCOMPLETE,
                started_at=started_at,
                duration_seconds=elapsed(),
                error=message,
            )

        # 2) 逐盘清空
        logger.info(
            "开始清空回收站：复核总数=%d 目标磁盘=%s flags=0x%X",
            pre.total_items,
            list(pre.scanned_roots),
            self._empty_flags,
        )
        calls: list[tuple[str, int]] = []
        for root in pre.scanned_roots:
            hresult = self._api.empty(root, self._empty_flags)
            calls.append((root, hresult))
            if hresult_ok(hresult):
                logger.info("清空磁盘回收站：磁盘=%s HRESULT=%s", root, hresult_message(hresult))
            else:
                logger.error(
                    "清空磁盘回收站失败：磁盘=%s HRESULT=%s",
                    root,
                    hresult_message(hresult),
                )

        failed_calls = [(root, hr) for root, hr in calls if not hresult_ok(hr)]
        # 聚合 HRESULT：全部成功为 S_OK，否则上报首个失败码。
        aggregate_hresult = failed_calls[0][1] if failed_calls else 0

        # 3) 清空后复核
        post = self.scan()
        if not post.is_complete:
            message = (
                "清空后复核统计不完整，无法确认清理结果"
                + (f"：{post.error}" if post.error else "")
                + (
                    f"（失败磁盘：{list(post.failed_roots)}）"
                    if post.failed_roots
                    else ""
                )
            )
            logger.error("%s；HRESULT=%s", message, hresult_message(aggregate_hresult))
            return CleanResult(
                outcome=CleanOutcome.INCOMPLETE,
                hresult=aggregate_hresult,
                started_at=started_at,
                duration_seconds=elapsed(),
                items_before=pre.total_items,
                items_after=None,
                error=message,
            )

        remaining = post.total_items
        if remaining == 0:
            if failed_calls:
                logger.warning(
                    "回收站已清空，但存在失败的清空调用：%s",
                    [(root, hresult_message(hr)) for root, hr in failed_calls],
                )
            logger.info(
                "回收站清空成功：清理前=%d 耗时=%.3fs", pre.total_items, elapsed()
            )
            return CleanResult(
                outcome=CleanOutcome.SUCCESS,
                hresult=aggregate_hresult,
                started_at=started_at,
                duration_seconds=elapsed(),
                items_before=pre.total_items,
                items_after=0,
                remaining_items=0,
            )

        if len(failed_calls) == len(calls):
            message = (
                "全部磁盘清空调用失败，复核后仍有 "
                f"{remaining} 个项目：{[(root, hresult_message(hr)) for root, hr in failed_calls]}"
            )
            logger.error("%s", message)
            return CleanResult(
                outcome=CleanOutcome.FAILED,
                hresult=aggregate_hresult,
                started_at=started_at,
                duration_seconds=elapsed(),
                items_before=pre.total_items,
                items_after=remaining,
                remaining_items=remaining,
                error=message,
            )

        message = f"清空后仍有 {remaining} 个项目（不重复清空）"
        logger.warning("%s；清理前=%d", message, pre.total_items)
        return CleanResult(
            outcome=CleanOutcome.PARTIAL,
            hresult=aggregate_hresult,
            started_at=started_at,
            duration_seconds=elapsed(),
            items_before=pre.total_items,
            items_after=remaining,
            remaining_items=remaining,
            error=message,
        )

"""T1 / T2 核心接口与数据类型定义。

本模块只定义契约，不包含任何 Windows API 调用；具体实现将在
T1（:mod:`recyclebingguard.recycle_bin_service`）与
T2（:mod:`recyclebingguard.recycle_bin_monitor`）中提供。

安全语义集中在此处表达，避免各实现重复定义：

- 统计结果不完整（``ScanResult.is_complete is False``）时，**不允许**触发清理；
- ``ScanResult.should_trigger()`` 是唯一推荐的阈值判断入口；
- 清空结果用 :class:`CleanOutcome` 明确区分 success / partial / failed / incomplete。

清理前确认：达到阈值后进入 ``MonitorState.AWAITING_CONFIRMATION``，只有在用户
（或倒计时）给出“继续”的决定、且**重新复核**仍达阈值时才允许清空；
取消 / 关闭 / 超时取消（``confirmation_timeout_seconds == 0`` 时不自动倒计时）
一律不调用清空 API。详见 :mod:`recyclebingguard.confirmation`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable


class DriveKind(Enum):
    """磁盘类型（对应 Windows ``GetDriveTypeW`` 的语义）。"""

    FIXED = "fixed"          # 本机固定磁盘，第一版唯一纳入统计的类型
    REMOVABLE = "removable"  # 可移动盘：第一版不统计
    NETWORK = "network"      # 网络盘：第一版不扫描
    CDROM = "cdrom"          # 光驱：第一版不统计
    RAMDISK = "ramdisk"      # 内存盘：第一版不统计
    UNKNOWN = "unknown"      # 未知/未挂载：不统计


#: 第一版纳入统计的磁盘类型。
INCLUDED_DRIVE_KINDS: frozenset[DriveKind] = frozenset({DriveKind.FIXED})


class CleanOutcome(Enum):
    """清空操作的最终结果类型。"""

    SUCCESS = "success"          # 清空后复核为 0
    PARTIAL = "partial"          # 清空执行了，但复核后仍有剩余项目
    FAILED = "failed"            # API 返回错误
    INCOMPLETE = "incomplete"    # 前置复核或后置复核失败/不完整，未确认结果


class MonitorState(Enum):
    """监控状态机的状态（PRD 第 7.3 节）。"""

    IDLE = "idle"
    CHECKING = "checking"
    THRESHOLD_REACHED = "threshold_reached"
    #: 等待用户在确认弹窗上做决定（取消/关闭/超时都不会清理）。
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CLEANING = "cleaning"
    VERIFYING = "verifying"
    ERROR = "error"
    PAUSED = "paused"


@dataclass(frozen=True)
class DiskScanResult:
    """单个磁盘的回收站查询结果。"""

    root: str                      # 磁盘根，如 "C:\\"
    kind: DriveKind = DriveKind.UNKNOWN
    ok: bool = False               # 查询是否成功
    num_items: int = 0             # SHQUERYRBINFO.i64NumItems
    size_bytes: int = 0            # SHQUERYRBINFO.i64Size
    hresult: int = 0               # API 返回的 HRESULT
    error: str | None = None       # 失败原因（人类可读）

    def __post_init__(self) -> None:
        if self.ok and self.num_items < 0:
            raise ValueError(
                f"磁盘 {self.root} 的 num_items 不能为负数：{self.num_items}"
            )
        if self.ok and self.size_bytes < 0:
            raise ValueError(
                f"磁盘 {self.root} 的 size_bytes 不能为负数：{self.size_bytes}"
            )


@dataclass(frozen=True)
class ScanResult:
    """一次全盘统计的结果（PRD 第 4 节统计口径）。"""

    total_items: int
    total_size_bytes: int
    scanned_roots: tuple[str, ...] = ()
    failed_roots: tuple[str, ...] = ()
    queried_at: datetime = field(default_factory=datetime.now)
    disks: tuple[DiskScanResult, ...] = ()
    error: str | None = None

    @property
    def is_complete(self) -> bool:
        """统计可用时才为 ``True``。

        满足以下任一条件即为不可用（``False``），且**不允许**据此触发清理：

        - 有任一应纳入统计的磁盘查询失败（``failed_roots`` 非空）；
        - 扫描级错误（``error`` 非空），例如一个固定磁盘都没枚举到。
        """
        return not self.failed_roots and self.error is None

    def should_trigger(self, threshold_items: int) -> bool:
        """判断是否达到清理阈值。

        **统计不完整时永远返回 False**，避免把未知磁盘当成 0 导致误判。
        """
        if not self.is_complete:
            return False
        return self.total_items >= threshold_items


@dataclass(frozen=True)
class CleanResult:
    """一次清空操作的结果。"""

    outcome: CleanOutcome
    hresult: int = 0
    started_at: datetime = field(default_factory=datetime.now)
    duration_seconds: float = 0.0
    items_before: int | None = None
    items_after: int | None = None
    remaining_items: int | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """是否确认为彻底清空成功。"""
        return self.outcome is CleanOutcome.SUCCESS


@runtime_checkable
class RecycleBinService(Protocol):
    """回收站统计与清空服务契约（T1 实现）。"""

    def scan(self) -> ScanResult:
        """统计所有应纳入磁盘的回收站项目数，不产生任何副作用。"""
        ...

    def empty(self) -> CleanResult:
        """清空整个回收站。

        实现必须自行完成：清空前复核 -> 调用清空 API -> 清空后复核，
        且不允许被 :meth:`scan` 隐式调用。
        """
        ...


@runtime_checkable
class RecycleBinMonitor(Protocol):
    """阈值监控器契约（T2 实现）。"""

    @property
    def state(self) -> MonitorState:
        """当前状态机状态。"""
        ...

    def start(self) -> None:
        """启动后台监控线程。"""
        ...

    def stop(self, timeout: float | None = None) -> None:
        """停止监控并等待退出，不残留后台任务。"""
        ...

    def pause(self) -> None:
        """暂停监控；不得执行已排队的清理任务。"""
        ...

    def resume(self) -> None:
        """恢复监控。"""
        ...

    def check_now(self) -> ScanResult:
        """立即执行一次检查，返回统计结果。"""
        ...

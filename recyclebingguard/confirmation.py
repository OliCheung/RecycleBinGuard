"""清理前确认的请求 / 决策契约（与任何 UI 框架无关）。

本模块只定义数据与控制流，**不导入** ``tkinter`` / ``pystray``，因此可以在无
图形环境下单元测试，也不会把界面依赖泄漏进监控器。

线程模型（重要）：

- 监控线程（后台）**不等待**弹窗结果：它只负责发起请求并把状态置为
  ``MonitorState.AWAITING_CONFIRMATION``，随后照常回到事件循环；
- 弹窗由 UI 主线程创建与更新，用户决定通过回调回传，清理动作在后台线程执行；
- 因此“弹窗等待用户操作”期间，托盘菜单与其它界面功能不会被阻塞。

防误清理的核心：**每次请求携带单调递增的 ``token``**。只有 token 与当前待确认
请求一致的决定才会被接受，从而杜绝：

- 过期 / 重复弹窗的决定触发第二次清理；
- 用户取消、程序退出或暂停之后，残留的旧决定仍然执行清理；
- 倒计时到期与点击取消同时发生时产生两个决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class ConfirmationDecision(Enum):
    """确认弹窗的最终决定。

    :attr:`proceeds` 是唯一判断“是否允许进入清理前复核”的入口；
    任何异常路径都落入 ``ABORTED``，绝不默认清理。
    """

    #: 用户点击「立即清理」。
    CONFIRMED = "confirmed"
    #: 倒计时归零（仅 ``confirmation_timeout_seconds > 0`` 时可能产生）。
    TIMEOUT = "timeout"
    #: 用户点击「取消本次清理」。
    CANCELLED = "cancelled"
    #: 用户直接关闭窗口（等同取消）。
    CLOSED = "closed"
    #: 程序退出 / 监控暂停 / 弹窗创建或运行异常。
    ABORTED = "aborted"

    @property
    def proceeds(self) -> bool:
        """是否允许进入清理前复核流程（仍须复核通过才会真正清理）。"""
        return self is ConfirmationDecision.CONFIRMED or self is ConfirmationDecision.TIMEOUT

    @property
    def is_cancelled(self) -> bool:
        """是否属于“不清理”的决定。"""
        return not self.proceeds


@dataclass(frozen=True)
class CleanConfirmationRequest:
    """一次清理确认请求。

    :param token: 请求标识，用于丢弃过期 / 重复的决定。
    :param total_items: 触发本轮确认的回收站项目数（实际统计结果）。
    :param threshold_items: 当前配置的自动清理阈值。
    :param timeout_seconds: 倒计时秒数；``0`` 表示不自动倒计时，弹窗一直等待。
    """

    token: int
    total_items: int
    threshold_items: int
    timeout_seconds: int = 0

    @property
    def countdown_enabled(self) -> bool:
        """是否启用自动倒计时（``timeout_seconds > 0``）。"""
        return self.timeout_seconds > 0


@runtime_checkable
class ConfirmationRequester(Protocol):
    """清理确认请求的提交方（由 UI 层实现）。

    实现必须**立即返回**，不得阻塞调用线程（调用方是监控线程或后台线程）：
    典型做法是投递到 UI 主线程队列后返回。
    """

    def request_clean_confirmation(self, request: CleanConfirmationRequest) -> None:
        """提交一次确认请求；实现负责保证同一时刻只有一个弹窗。"""
        ...


__all__ = [
    "CleanConfirmationRequest",
    "ConfirmationDecision",
    "ConfirmationRequester",
]

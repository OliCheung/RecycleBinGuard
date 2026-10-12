"""清理前确认弹窗（``tkinter``，**只能在 UI 主线程创建与更新**）。

行为约定（与需求一致）：

- 标题 ``RecycleBinGuard — 回收站清理确认``；
- 显示当前项目数、阈值、永久删除警告与倒计时；
- 倒计时使用 tkinter 主线程的 ``after()``，**不使用**阻塞式 ``sleep``、
  不使用 ``grab_set()`` / ``wait_window()``，因此托盘菜单与其它界面功能不会被阻塞；
- ``confirmation_timeout_seconds > 0`` 时逐秒显示 ``10、9 … 1``，归零后进入复核；
  ``= 0`` 时不自动倒计时，弹窗一直等待用户操作；
- 关闭窗口等同于取消；
- **不设置任何默认按钮 / 回车快捷键**，不存在“默认执行”；
- ``立即清理`` / ``取消本次清理`` / 倒计时到期 / 关闭窗口之间只会产生
  **一个**最终决定（由 ``_decided`` 一次性闸门保证）。
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .confirmation import CleanConfirmationRequest, ConfirmationDecision
from .logging_setup import get_logger

logger = get_logger()

#: 弹窗标题。
CONFIRMATION_TITLE = "RecycleBinGuard — 回收站清理确认"

#: 倒计时提示模板（每秒刷新）。
COUNTDOWN_TEMPLATE = "{seconds} 秒后自动清空回收站"

#: 未启用倒计时时的提示。
NO_COUNTDOWN_TEXT = "不会自动清理，等待你的操作"

#: 倒计时归零后的提示（随后立即进入清理前复核）。
COUNTDOWN_DONE_TEXT = "倒计时结束，正在复核…"

#: 永久删除警告。
WARNING_TEXT = (
    "警告：本次操作将清空整个 Windows 回收站，"
    "所有待恢复文件都可能被永久删除。"
)

#: 取消后的行为提示。
HINT_TEXT = "取消后程序会继续监控，下一次检查仍达到阈值时可以再次提示。"

#: 倒计时步进（毫秒）。
COUNTDOWN_TICK_MS = 1000


class CleanConfirmDialog:
    """一次清理确认弹窗。

    :param parent: 父窗口（仅用于归属，不设 transient，避免主窗口隐藏时被一起隐藏）。
    :param request: 确认请求（含 token / 项目数 / 阈值 / 倒计时秒数）。
    :param on_decision: 决定回调，签名 ``(token, decision)``；保证**只调用一次**。
    :param countdown_ms: 倒计时步进，默认 1000ms（测试可注入更小值或假调度器）。
    :param scheduler: 可选调度器 ``(ms, callback) -> timer_id``，默认用 ``after``。
    """

    def __init__(
        self,
        parent: tk.Misc,
        request: CleanConfirmationRequest,
        on_decision: Callable[[int, ConfirmationDecision], None],
        *,
        countdown_ms: int = COUNTDOWN_TICK_MS,
        scheduler: Callable[[int, Callable[[], None]], object] | None = None,
    ) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("CleanConfirmDialog 必须在 UI 主线程创建")

        self._request = request
        self._on_decision = on_decision
        self._countdown_ms = countdown_ms
        self._decided = False
        self._after_id: object = None
        self._decision: ConfirmationDecision | None = None
        self._remaining: int | None = (
            request.timeout_seconds if request.countdown_enabled else None
        )

        self._window = tk.Toplevel(parent)
        self._window.title(CONFIRMATION_TITLE)
        self._window.resizable(False, False)
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)
        self._scheduler = scheduler or self._window.after

        self._build()
        self._window.deiconify()
        self._window.lift()
        self._window.attributes("-topmost", True)
        self._window.focus_force()

        if self._remaining is not None:
            self._schedule()

        logger.info(
            "清理确认弹窗已显示：token=%d 项目数=%d 阈值=%d 倒计时=%s",
            request.token,
            request.total_items,
            request.threshold_items,
            self._remaining if self._remaining is not None else "未启用",
        )

    # ------------------------------------------------------------------
    # 只读状态（供 UI / 测试查询）
    # ------------------------------------------------------------------

    @property
    def request(self) -> CleanConfirmationRequest:
        return self._request

    @property
    def decision(self) -> ConfirmationDecision | None:
        """最终决定；未决定前为 ``None``。"""
        return self._decision

    @property
    def remaining_seconds(self) -> int | None:
        """剩余倒计时秒数；未启用倒计时时为 ``None``。"""
        return self._remaining

    @property
    def timer_id(self) -> object:
        """当前挂起的 ``after`` 定时器 id；无则为 ``None``。"""
        return self._after_id

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------

    def _build(self) -> None:
        frame = ttk.Frame(self._window, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(
            frame,
            text=f"当前回收站项目数：{self._request.total_items:,}",
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        ttk.Label(
            frame, text=f"自动清理阈值：{self._request.threshold_items:,}"
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Separator(frame, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=10
        )

        ttk.Label(
            frame,
            text=WARNING_TEXT,
            foreground="#C62828",
            wraplength=420,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w")

        self._lbl_countdown = ttk.Label(
            frame,
            text=self._countdown_text(),
            font=("Segoe UI", 11, "bold"),
            foreground="#EF6C00",
        )
        self._lbl_countdown.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))

        ttk.Label(frame, text=HINT_TEXT, wraplength=420, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        # 刻意不设置 default=：不存在“回车即确认”的默认行为。
        ttk.Button(buttons, text="取消本次清理", command=self._on_cancel).pack(
            side="right", padx=(8, 0)
        )
        ttk.Button(buttons, text="立即清理", command=self._on_confirm).pack(side="right")

    def _countdown_text(self) -> str:
        if self._remaining is None:
            return NO_COUNTDOWN_TEXT
        return COUNTDOWN_TEMPLATE.format(seconds=self._remaining)

    # ------------------------------------------------------------------
    # 倒计时
    # ------------------------------------------------------------------

    def _schedule(self) -> None:
        self._after_id = self._scheduler(self._countdown_ms, self._on_tick)

    def _cancel_timer(self) -> None:
        if self._after_id is None:
            return
        try:
            self._window.after_cancel(self._after_id)  # type: ignore[arg-type]
        except (tk.TclError, ValueError):  # pragma: no cover - 定时器可能已触发
            logger.debug("取消倒计时定时器失败", exc_info=True)
        finally:
            self._after_id = None

    def _on_tick(self) -> None:
        """一次倒计时步进（由 ``after`` 在主线程触发，也可由测试直接驱动）。"""
        if self._decided or self._remaining is None:
            return
        self._after_id = None
        self._remaining -= 1
        if self._remaining <= 0:
            self._remaining = 0
            self._lbl_countdown.configure(text=COUNTDOWN_DONE_TEXT)
            self._decide(ConfirmationDecision.TIMEOUT)
            return
        self._lbl_countdown.configure(text=self._countdown_text())
        self._schedule()

    # ------------------------------------------------------------------
    # 用户动作
    # ------------------------------------------------------------------

    def _on_confirm(self) -> None:
        self._decide(ConfirmationDecision.CONFIRMED)

    def _on_cancel(self) -> None:
        self._decide(ConfirmationDecision.CANCELLED)

    def _on_close(self) -> None:
        self._decide(ConfirmationDecision.CLOSED)

    def abort(self, reason: str = "程序退出或监控已暂停") -> None:
        """外部主动作废弹窗（退出 / 暂停），等同取消。"""
        logger.warning("清理确认弹窗被作废：token=%d 原因=%s", self._request.token, reason)
        self._decide(ConfirmationDecision.ABORTED)

    def _decide(self, decision: ConfirmationDecision) -> None:
        """一次性闸门：无论多少条路径竞争，最终只会有一个决定被回传。"""
        if self._decided:
            return
        self._decided = True
        self._decision = decision
        self._cancel_timer()
        try:
            self._window.destroy()
        except tk.TclError:  # pragma: no cover - 窗口可能已销毁
            pass
        try:
            self._on_decision(self._request.token, decision)
        except Exception:  # 回调异常不能吞掉，也不能影响窗口已销毁的事实
            logger.exception("清理确认回调抛出异常")


__all__ = [
    "CONFIRMATION_TITLE",
    "COUNTDOWN_TEMPLATE",
    "CleanConfirmDialog",
]

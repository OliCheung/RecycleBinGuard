"""清理前确认弹窗（``tkinter``，**只能在 UI 主线程创建与更新**）。

行为约定（与需求一致）：

- 标题 ``RecycleBinGuard — 回收站清理确认``；
- **只说必要的事**：「已触发清理」+ 项目数 + 倒计时 + 两个按钮（``取消`` / ``清理``）；
  阈值 / 超出量 / 大段警告一律不出现（那是设置与说明文档的事）；
- 倒计时使用 tkinter 主线程的 ``after()``，**不使用**阻塞式 ``sleep``、
  不使用 ``grab_set()`` / ``wait_window()``，因此托盘菜单与其它界面功能不会被阻塞；
- ``confirmation_timeout_seconds > 0`` 时逐秒显示 ``10、9 … 1``，归零后进入复核；
  ``= 0`` 时不自动倒计时，弹窗一直等待用户操作；
- 关闭窗口等同于取消；
- **不设置任何默认按钮 / 回车快捷键**，不存在“默认执行”；
- ``立即清理`` / ``取消本次清理`` / 倒计时到期 / 关闭窗口之间只会产生
  **一个**最终决定（由 ``_decided`` 一次性闸门保证）。

视觉与动效：

- 一张小卡片贴在工作区右下角，内容压缩到三行：标题 + 倒计时、项目数、按钮行；
- 弹窗像系统通知一样出现在**屏幕右下角**：从屏幕下缘**升起**到位；
- **倒计时到点**时先向下**降出**屏幕，动画播完才把 ``TIMEOUT`` 决定交给上层
  （决定在动画开始前就已锁定，因此期间点击取消 / 关闭都不会产生第二个决定）；
- 用户主动点按钮 / 关闭窗口则立刻收窗，不做动画（保持响应速度）；
- 配色跟随 :class:`~recyclebingguard.theme.ThemeManager`，支持在线换肤。
"""

from __future__ import annotations

import ctypes
import os
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from .confirmation import CleanConfirmationRequest, ConfirmationDecision
from .logging_setup import get_logger
from .theme import (
    LIGHT,
    Card,
    Palette,
    ThemedFrame,
    ThemedLabel,
    ThemeManager,
    default_font_family,
    retheme_tree,
    sync_cards,
)

logger = get_logger()

#: 弹窗标题。
CONFIRMATION_TITLE = "RecycleBinGuard — 回收站清理确认"

#: 倒计时提示模板（每秒刷新）。刻意短，避免在右下角占宽度。
COUNTDOWN_TEMPLATE = "{seconds} 秒后自动清理"

#: 未启用倒计时时的提示。
NO_COUNTDOWN_TEXT = "等待你的操作"

#: 倒计时归零后的提示（随后立即进入清理前复核）。
COUNTDOWN_DONE_TEXT = "倒计时结束，正在清理…"

#: 主标题：只回答“发生了什么”。
HEADLINE_TEXT = "回收站已触发清理"

#: 副标题：唯一的量化信息，同时带出“不可逆”。
DETAIL_TEMPLATE = "{items:,} 项将被永久删除"

#: 倒计时步进（毫秒）。
COUNTDOWN_TICK_MS = 1000

#: 小卡片的左右/上下留白（比普通卡片更紧，弹窗才显小巧）。
FRAME_PADDING = 12
CARD_PADDING = 13

#: 「升起 / 降下」动画参数（毫秒与像素）。
SLIDE_DURATION_MS = 260
#: 每帧间隔（约 60fps）。
SLIDE_FRAME_MS = 16
#: 停留位置距工作区右 / 下边缘的留白。
SLIDE_MARGIN_X = 24
SLIDE_MARGIN_Y = 24
#: 起始位置相对屏幕下缘再向下探出的像素（保证初始完全在屏幕外）。
SLIDE_OVERHANG = 32

#: ``SystemParametersInfoW`` 的 ``SPI_GETWORKAREA``（工作区 = 屏幕减去任务栏）。
_SPI_GETWORKAREA = 0x0030


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def work_area(window: tk.Misc) -> tuple[int, int, int, int]:
    """返回 ``(left, top, right, bottom)``：弹窗可用区域（已排除任务栏）。

    Windows 下读主显示器的工作区；任何一步失败（非 Windows、API 不可用）就退回
    Tk 报告的整块屏幕尺寸，保证弹窗仍然可见。
    """
    fallback = (0, 0, window.winfo_screenwidth(), window.winfo_screenheight())
    if os.name != "nt":  # pragma: no cover - 本项目目标平台为 Windows
        return fallback
    try:
        rect = _RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(  # type: ignore[attr-defined]
            _SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        )
    except (AttributeError, OSError):  # pragma: no cover - 极端环境
        logger.debug("读取显示器工作区失败", exc_info=True)
        return fallback
    if not ok or rect.right <= rect.left or rect.bottom <= rect.top:
        return fallback
    return rect.left, rect.top, rect.right, rect.bottom


class CleanConfirmDialog:
    """一次清理确认弹窗。

    :param parent: 父窗口（仅用于归属，不设 transient，避免主窗口隐藏时被一起隐藏）。
    :param request: 确认请求（含 token / 项目数 / 阈值 / 倒计时秒数）。
    :param on_decision: 决定回调，签名 ``(token, decision)``；保证**只调用一次**。
    :param countdown_ms: 倒计时步进，默认 1000ms（测试可注入更小值或假调度器）。
    :param scheduler: 可选调度器 ``(ms, callback) -> timer_id``，默认用 ``after``；
        只用于**倒计时**（动画走真实的 ``after``，不受它影响）。
    :param theme: 可选皮肤管理器；提供后弹窗会跟随皮肤（含在线切换）。
    :param animate: 是否启用右下角升入 / 超时降出的动画；``False`` 时直接就位，
        供测试与无事件循环的场景使用。
    """

    def __init__(
        self,
        parent: tk.Misc,
        request: CleanConfirmationRequest,
        on_decision: Callable[[int, ConfirmationDecision], None],
        *,
        countdown_ms: int = COUNTDOWN_TICK_MS,
        scheduler: Callable[[int, Callable[[], None]], object] | None = None,
        theme: ThemeManager | None = None,
        animate: bool = True,
    ) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("CleanConfirmDialog 必须在 UI 主线程创建")

        self._request = request
        self._on_decision = on_decision
        self._countdown_ms = countdown_ms
        self._decided = False
        self._delivered = False
        self._after_id: object = None
        self._anim_id: object = None
        self._animating = False
        self._decision: ConfirmationDecision | None = None
        self._remaining: int | None = request.timeout_seconds if request.countdown_enabled else None
        self._theme = theme
        self._palette: Palette = theme.palette if theme is not None else LIGHT
        self._font = theme.font_family if theme is not None else default_font_family(parent)
        self._unregister_theme: Callable[[], None] | None = None
        self._animate = animate

        self._window = tk.Toplevel(parent)
        self._window.title(CONFIRMATION_TITLE)
        self._window.resizable(False, False)
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)
        self._scheduler = scheduler or self._window.after

        self._build()
        self._apply_palette(self._palette)
        self._show(animate=animate)

        if self._theme is not None:
            self._unregister_theme = self._theme.register(self._apply_palette)

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
        """已锁定的最终决定；未决定前为 ``None``。

        注意：动画期间决定可能**已经锁定但尚未交付**（``on_decision`` 还没被调用），
        此时该属性已经不是 ``None``。
        """
        return self._decision

    @property
    def remaining_seconds(self) -> int | None:
        """剩余倒计时秒数；未启用倒计时时为 ``None``。"""
        return self._remaining

    @property
    def timer_id(self) -> object:
        """当前挂起的倒计时 ``after`` 定时器 id；无则为 ``None``。"""
        return self._after_id

    @property
    def animation_id(self) -> object:
        """当前挂起的动画（或等尺寸）定时器 id；无则为 ``None``。"""
        return self._anim_id

    @property
    def animating(self) -> bool:
        """弹窗是否正在播动画（升起 / 降下途中）。

        用它判断“升起动画是否播完”比 ``animation_id is None`` 可靠：动画开始**之前**
        该 id 也是 ``None``。
        """
        return self._animating

    @property
    def animation_enabled(self) -> bool:
        return self._animate

    @property
    def window(self) -> tk.Toplevel:
        return self._window

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------

    def _build(self) -> None:
        """三行小卡：标题 + 倒计时 / 项目数 / 按钮行。

        刻意不放环形倒计时、阈值、超出量与大段警告——右下角通知的职责是
        “让人一秒看懂并做决定”，细节交给设置窗口与状态窗口。
        """
        palette = self._palette
        font = self._font

        outer = ThemedFrame(self._window, palette)
        outer.pack(fill="both", expand=True, padx=FRAME_PADDING, pady=FRAME_PADDING)

        card = Card(outer, palette, stretch=True, padding=CARD_PADDING)
        card.pack(fill="x")
        card.body.columnconfigure(0, weight=1)

        # 第一行：主标题（左）+ 倒计时（右）
        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text",
            text=HEADLINE_TEXT,
            font=(font, 11, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self._lbl_countdown = ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="accent",
            text=self._countdown_text(),
            font=(font, 9),
            anchor="e",
        )
        self._lbl_countdown.grid(row=0, column=1, sticky="e", padx=(10, 0))

        # 第二行：项目数 + 不可逆提示
        self._lbl_items = ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text_muted",
            text=DETAIL_TEMPLATE.format(items=self._request.total_items),
            font=(font, 9),
            anchor="w",
        )
        self._lbl_items.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # 第三行：取消 / 清理（右对齐，不给任何默认按钮）
        buttons = ThemedFrame(card.body, palette, bg="surface")
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(12, 0))
        buttons.columnconfigure(0, weight=1)
        # 刻意不设置 default=：不存在“回车即确认”的默认行为。
        self._btn_cancel = ttk.Button(buttons, text="取消", command=self._on_cancel)
        self._btn_cancel.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self._btn_confirm = ttk.Button(
            buttons, text="清理", style="Danger.TButton", command=self._on_confirm
        )
        self._btn_confirm.grid(row=0, column=2, sticky="e")

    def _countdown_text(self) -> str:
        if self._remaining is None:
            return NO_COUNTDOWN_TEXT
        return COUNTDOWN_TEMPLATE.format(seconds=self._remaining)

    def _apply_palette(self, palette: Palette) -> None:
        """换肤：窗口底色 + 递归刷新内部控件。"""
        self._palette = palette
        try:
            self._window.configure(bg=palette.canvas)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return
        retheme_tree(self._window, palette)

    # ------------------------------------------------------------------
    # 位置与动画（右下角升起 / 超时降下）
    # ------------------------------------------------------------------

    def _rest_position(self) -> tuple[int, int]:
        """弹窗静止时的位置：工作区右下角，留出边距。

        必须在 :meth:`_show` 里 ``sync_cards()`` + ``update_idletasks()`` **之后**调用：
        ``Card`` 的 ``Canvas`` 默认请求尺寸很大（378x265），不同步一次的话整窗请求高度
        会虚高（实测 642 vs 真实 291），落位就会偏。
        """
        left, top, right, bottom = work_area(self._window)
        width = max(1, self._window.winfo_reqwidth())
        height = max(1, self._window.winfo_reqheight())
        return (
            max(left, right - width - SLIDE_MARGIN_X),
            max(top, bottom - height - SLIDE_MARGIN_Y),
        )

    def _offscreen_y(self) -> int:
        """屏幕下缘之外（完全看不见）的 y 坐标。"""
        _, _, _, bottom = work_area(self._window)
        return bottom + SLIDE_OVERHANG

    def _show(self, *, animate: bool) -> None:
        """先落位到工作区右下角；``animate`` 为真时从屏幕下缘升起。

        量尺寸前要先让卡片按内容同步一次（:func:`~recyclebingguard.theme.sync_cards`），
        否则拿到的是 ``Canvas`` 的默认请求尺寸，窗口会先按错误高度落位再自己收缩，
        看起来就是“在错的位置闪一下”。同步之后窗口在映射前就有真实尺寸，
        ``animate=True`` 时先停到屏幕下缘之外，用户只看到它缓缓升起。
        """
        # 顺序要紧：先让布局管理器算出卡片内容的请求尺寸（不先跑一次的话
        # body.winfo_reqheight() 还是 1），再让卡片把内容尺寸交给 Canvas，
        # 最后再跑一次让整窗请求尺寸反映卡片的新尺寸。
        self._window.update_idletasks()
        sync_cards(self._window)
        self._window.update_idletasks()

        rest = self._rest_position()
        start = (rest[0], self._offscreen_y()) if animate else rest
        self._place(start)

        self._window.deiconify()
        self._window.lift()
        self._window.attributes("-topmost", True)
        self._window.focus_force()

        if animate:
            self._slide(start, rest, ease_out=True)
        else:
            self._animating = False

    def _place(self, position: tuple[float, float]) -> bool:
        """把窗口移动到指定位置；窗口已销毁时返回 ``False``。"""
        try:
            self._window.geometry(f"+{int(position[0])}+{int(position[1])}")
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return False
        return True

    def _slide(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        *,
        ease_out: bool,
        on_done: Callable[[], None] | None = None,
        duration_ms: int = SLIDE_DURATION_MS,
    ) -> None:
        """在 ``start`` → ``end`` 之间逐帧移动（``after`` 驱动，不阻塞主线程）。"""
        frames = max(1, duration_ms // SLIDE_FRAME_MS)
        self._animating = True
        self._place(start)

        def frame(index: int) -> None:
            self._anim_id = None
            progress = index / frames
            # 升入用 ease-out（先快后慢，像被“托住”），降下用 ease-in（越走越快）
            eased = 1 - (1 - progress) ** 3 if ease_out else progress**3
            moved = self._place(
                (
                    start[0] + (end[0] - start[0]) * eased,
                    start[1] + (end[1] - start[1]) * eased,
                )
            )
            if not moved:  # 窗口已销毁：静默结束
                self._animating = False
                return
            if index < frames:
                self._schedule_frame(frame, index + 1)
                return
            self._animating = False
            if on_done is not None:
                on_done()

        self._schedule_frame(frame, 1)

    def _schedule_frame(self, callback: Callable[[int], None], index: int) -> None:
        """排下一帧；窗口已销毁时忽略。"""
        try:
            self._anim_id = self._window.after(SLIDE_FRAME_MS, callback, index)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            self._anim_id = None
            self._animating = False

    def _cancel_animation(self) -> None:
        self._animating = False
        if self._anim_id is None:
            return
        try:
            self._window.after_cancel(self._anim_id)  # type: ignore[arg-type]
        except (tk.TclError, ValueError):  # pragma: no cover - 定时器可能已触发
            logger.debug("取消动画定时器失败", exc_info=True)
        finally:
            self._anim_id = None

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
            self._set_countdown_text(COUNTDOWN_DONE_TEXT)
            logger.info("清理确认倒计时结束，token=%d 进入清理前复核", self._request.token)
            self._decide(ConfirmationDecision.TIMEOUT, animate=self._animate)
            return
        self._set_countdown_text(self._countdown_text())
        self._schedule()

    def _set_countdown_text(self, text: str) -> None:
        self._lbl_countdown.configure(text=text)

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
        """外部主动作废弹窗（退出 / 暂停），等同取消。

        若倒计时已锁定 ``TIMEOUT``（正在降下动画中），则**立刻把已锁定的决定送达**，
        不再改成 ``ABORTED``：此时暂停 / 退出由上层的 token 校验兜底，行为与
        “动画播完再送达”完全一致。
        """
        if self._decided:
            logger.info("清理确认弹窗作废（已锁定决定，直接送达）：token=%d", self._request.token)
            self._deliver()
            return
        logger.warning("清理确认弹窗被作废：token=%d 原因=%s", self._request.token, reason)
        self._decide(ConfirmationDecision.ABORTED)

    def _decide(self, decision: ConfirmationDecision, *, animate: bool = False) -> None:
        """一次性闸门 + 交付。

        - 决定在函数开始就**锁定**，之后任何点击 / 关闭 / 外部 abort 都不会产生第二个；
        - ``animate`` 为真（且弹窗启用了动画）时先播“降下”动画，动画结束再交付；
          此时 ``decision`` 属性已经是最终值，只是 ``on_decision`` 还没被调用。
        """
        if self._decided:
            return
        self._decided = True
        self._decision = decision
        self._cancel_timer()
        if animate and self._animate:
            self._disable_buttons()
            # 若升入动画（或“等尺寸”轮询）还没结束，先停掉，避免两段动画打架
            self._cancel_animation()
            try:
                origin_x, origin_y = self._window.winfo_x(), self._window.winfo_y()
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                self._deliver()
                return
            self._slide(
                (origin_x, origin_y),
                (origin_x, self._offscreen_y()),
                ease_out=False,
                on_done=self._deliver,
            )
            return
        self._deliver()

    def _deliver(self) -> None:
        """销毁窗口并回调（保证只回调一次）。"""
        if self._delivered:
            return
        self._delivered = True
        self._cancel_animation()
        if self._unregister_theme is not None:
            self._unregister_theme()
            self._unregister_theme = None
        try:
            self._window.destroy()
        except tk.TclError:  # pragma: no cover - 窗口可能已销毁
            pass
        try:
            self._on_decision(self._request.token, self._decision)  # type: ignore[arg-type]
        except Exception:  # 回调异常不能吞掉，也不能影响窗口已销毁的事实
            logger.exception("清理确认回调抛出异常")

    def _disable_buttons(self) -> None:
        """动画期间禁用按钮：决定已锁定，避免用户以为还能改主意。"""
        for button in (self._btn_cancel, self._btn_confirm):
            try:
                button.state(["disabled"])
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                logger.debug("禁用按钮失败", exc_info=True)


__all__ = [
    "CARD_PADDING",
    "CONFIRMATION_TITLE",
    "COUNTDOWN_DONE_TEXT",
    "COUNTDOWN_TEMPLATE",
    "DETAIL_TEMPLATE",
    "FRAME_PADDING",
    "HEADLINE_TEXT",
    "NO_COUNTDOWN_TEXT",
    "SLIDE_DURATION_MS",
    "SLIDE_FRAME_MS",
    "SLIDE_MARGIN_X",
    "SLIDE_MARGIN_Y",
    "SLIDE_OVERHANG",
    "CleanConfirmDialog",
    "work_area",
]

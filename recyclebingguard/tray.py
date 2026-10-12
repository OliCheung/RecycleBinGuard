"""T3：系统托盘与状态窗口。

- 托盘图标与右键菜单使用 ``pystray``；
- 状态 / 日志窗口使用标准库 ``tkinter``（ttk）。

线程模型（重要）：

- ``tkinter`` 只能在主线程使用，因此 ``mainloop`` 跑在主线程；
- ``pystray.Icon.run`` 放在守护线程；
- 托盘菜单回调运行在 pystray 线程，**不直接操作控件**，而是把闭包投入
  :class:`queue.Queue`，由主线程定时取出执行 —— 这是 tkinter 唯一线程安全的做法；
- 耗时的“立即检查 / 清空”由 :class:`~recyclebingguard.app.AppController`
  派发到后台线程，UI 不会冻结。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from . import APP_NAME, __version__
from .app import (
    LABEL_AUTO_ON,
    RISK_NOTICE,
    RISK_NOTICE_TITLE,
    AppController,
    RiskConfirmationRequired,
)
from .confirm_dialog import CleanConfirmDialog
from .confirmation import CleanConfirmationRequest, ConfirmationDecision
from .hotkey import HOTKEY_LABEL, HotkeyListener
from .interfaces import CleanOutcome, MonitorState
from .logging_setup import get_logger
from .single_instance import take_wake_request

logger = get_logger()

#: 主线程轮询间隔（毫秒）：既用于刷新界面，也用于处理托盘线程投递的请求。
UI_TICK_MS = 500

_STATE_COLORS: dict[MonitorState, str] = {
    MonitorState.IDLE: "#2E7D32",
    MonitorState.CHECKING: "#2E7D32",
    MonitorState.PAUSED: "#616161",
    MonitorState.THRESHOLD_REACHED: "#EF6C00",
    MonitorState.AWAITING_CONFIRMATION: "#EF6C00",
    MonitorState.CLEANING: "#EF6C00",
    MonitorState.VERIFYING: "#EF6C00",
    MonitorState.ERROR: "#C62828",
}

_CLEAN_OUTCOME_TEXT: dict[CleanOutcome, str] = {
    CleanOutcome.SUCCESS: "清理成功",
    CleanOutcome.PARTIAL: "部分清理（仍有剩余）",
    CleanOutcome.FAILED: "清理失败",
    CleanOutcome.INCOMPLETE: "结果未确认（统计不完整）",
}


def create_icon_image(color: str = "#2E7D32", size: int = 64) -> Image.Image:
    """生成托盘图标（一个简洁的回收站形状）。"""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    body = (size * 0.22, size * 0.30, size * 0.78, size * 0.86)
    draw.rounded_rectangle(body, radius=size * 0.07, fill=color)
    draw.rounded_rectangle(
        (size * 0.14, size * 0.16, size * 0.86, size * 0.30),
        radius=size * 0.05,
        fill=color,
    )
    for ratio in (0.38, 0.50, 0.62):
        x = size * ratio
        draw.line(
            [(x, size * 0.40), (x, size * 0.78)],
            fill=(255, 255, 255, 170),
            width=max(1, int(size * 0.04)),
        )
    return image


def _format_time(value: datetime | None) -> str:
    return value.strftime("%H:%M:%S") if value is not None else "—"


class TrayApplication:
    """托盘应用：组合托盘图标与 tkinter 状态窗口。"""

    def __init__(self, controller: AppController, *, start_minimized: bool = False) -> None:
        self._controller = controller
        self._ui_queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._stopping = False
        #: 登录自启时为 True：静默驻留托盘，不弹窗。
        self._start_minimized = start_minimized
        self._hotkey = HotkeyListener(self._post_hotkey)

        self._last_error_seen: str | None = None
        self._last_clean_seen: object = None
        self._last_icon_color: str | None = None
        self._tick_count = 0
        self._autostart_cached = False

        #: 同一时刻最多一个清理确认弹窗。
        self._confirm_dialog: CleanConfirmDialog | None = None
        # 监控线程（后台）通过控制器回调提交弹窗请求，这里负责投递到主线程。
        controller.on_confirmation_request = self._request_clean_confirmation

        self._icon_ready = threading.Event()

        self._root = tk.Tk()
        # 立刻隐藏，避免启动瞬间闪出一个空白窗口
        self._root.withdraw()
        self._root.title(f"{APP_NAME} v{__version__}")
        self._root.protocol("WM_DELETE_WINDOW", self._hide_window)

        self._build_window()
        # 托盘图标延迟到 run() 才真正创建：
        # pystray 的 win32 Icon 一旦被构造却从未 run()，垃圾回收时 __del__ 会抛
        # “不可捕获异常”（_icon_handle 未初始化），在测试里表现为随机的
        # PytestUnraisableExceptionWarning。延迟创建既避开这个坑，
        # 也让 __init__ 不产生任何操作系统资源。
        self._icon: pystray.Icon | None = None

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------

    def _build_window(self) -> None:
        root = self._root
        root.minsize(420, 400)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        self._lbl_state = ttk.Label(frame, text="", font=("Segoe UI", 11))
        self._lbl_state.grid(row=0, column=0, columnspan=2, sticky="w")

        self._lbl_count = ttk.Label(frame, text="—", font=("Segoe UI", 26, "bold"))
        self._lbl_count.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Label(frame, text="回收站项目总数 / 触发阈值").grid(
            row=2, column=0, columnspan=2, sticky="w"
        )

        ttk.Separator(frame, orient="horizontal").grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=12
        )

        self._rows: dict[str, ttk.Label] = {}
        for index, (key, title) in enumerate(
            (
                ("threshold", "触发阈值"),
                ("last_check", "最近检查"),
                ("next_check", "下次检查"),
                ("last_clean", "最近清理结果"),
                ("last_clean_at", "上次清理"),
                ("autostart", "开机启动"),
                ("error", "最近错误"),
                ("config", "配置文件"),
            ),
            start=4,
        ):
            ttk.Label(frame, text=f"{title}：").grid(row=index, column=0, sticky="w", pady=2)
            value = ttk.Label(frame, text="—", wraplength=280, justify="left")
            value.grid(row=index, column=1, sticky="w", pady=2)
            self._rows[key] = value

        options = ttk.LabelFrame(frame, text="选项", padding=10)
        options.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(12, 0))

        self._var_auto = tk.BooleanVar(value=False)
        self._var_dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options,
            text="自动清理（达到阈值后自动清空回收站）",
            variable=self._var_auto,
            command=self._on_auto_checkbox,
        ).pack(anchor="w")
        ttk.Checkbutton(
            options,
            text="试运行模式（只记录，不调用清空）",
            variable=self._var_dry,
            command=self._on_dry_checkbox,
        ).pack(anchor="w")

        buttons = ttk.Frame(frame)
        buttons.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        for column in range(2):
            buttons.columnconfigure(column, weight=1)

        ttk.Button(buttons, text="立即检查", command=self._on_check_now).grid(
            row=0, column=0, sticky="ew", padx=(0, 4), pady=4
        )
        self._btn_pause = ttk.Button(buttons, text="暂停监控", command=self._on_toggle_pause)
        self._btn_pause.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=4)

        ttk.Button(buttons, text="查看最近日志", command=self._show_logs).grid(
            row=1, column=0, sticky="ew", padx=(0, 4), pady=4
        )
        ttk.Button(buttons, text="隐藏窗口", command=self._hide_window).grid(
            row=1, column=1, sticky="ew", padx=(4, 0), pady=4
        )

        ttk.Button(frame, text="退出程序", command=self._quit).grid(
            row=14, column=0, columnspan=2, sticky="ew", pady=(4, 0)
        )

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("打开状态窗口", self._menu_open_window, default=True),
            pystray.MenuItem("立即检查", self._menu_check_now),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._auto_clean_menu_text, self._menu_toggle_auto_clean),
            pystray.MenuItem(
                "试运行模式",
                self._menu_toggle_dry_run,
                checked=lambda item: self._controller.config.dry_run,
            ),
            pystray.MenuItem(
                f"打开热键（{HOTKEY_LABEL}）",
                self._menu_toggle_hotkey,
                checked=lambda item: self._controller.config.hotkey_enabled,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("安装开始菜单 / 桌面快捷方式", self._menu_install_shortcuts),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("查看最近清理日志", self._menu_show_logs),
            pystray.MenuItem("查看清理历史", self._menu_show_clean_history),
            pystray.MenuItem(
                "开机启动设置",
                self._menu_toggle_autostart,
                checked=self._autostart_checked,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出程序", self._menu_quit),
        )

    def _auto_clean_menu_text(self, item: pystray.MenuItem) -> str:
        return "关闭自动清理" if self._controller.config.auto_clean_enabled else "启用自动清理"

    def _autostart_checked(self, item: pystray.MenuItem) -> bool:
        try:
            return bool(self._controller.is_start_on_login_enabled())
        except NotImplementedError:
            return False

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> None:
        """启动托盘图标与主窗口事件循环（阻塞直到退出）。"""
        self._controller.start()

        self._icon = pystray.Icon(
            APP_NAME,
            create_icon_image(),
            APP_NAME,
            menu=self._build_menu(),
        )
        threading.Thread(
            target=self._icon.run,
            kwargs={"setup": self._on_icon_ready},
            name="RecycleBinGuard-Tray",
            daemon=True,
        ).start()

        if self._controller.config.hotkey_enabled:
            self._enable_hotkey()

        # 首次启动 / 双击快捷方式时直接显示窗口，避免用户“找不到程序在哪”
        if not self._start_minimized:
            self._show_window()

        self._root.after(UI_TICK_MS, self._tick)
        self._root.mainloop()
        logger.info("界面已退出")

    def _on_icon_ready(self, icon: pystray.Icon) -> None:
        """托盘图标就绪后回调（运行在托盘线程）。"""
        icon.visible = True
        self._icon_ready.set()

    # -- 热键 --

    def _post_hotkey(self) -> None:
        """热键回调（运行在热键线程）：投递到主线程后再显示窗口。"""
        self._post(self._show_window)

    def _enable_hotkey(self) -> None:
        if not self._hotkey.start():
            logger.warning("全局热键未启用：%s", self._hotkey.error or "不支持")

    def _disable_hotkey(self) -> None:
        self._hotkey.stop()

    def _tick(self) -> None:
        if self._stopping:
            return
        self._drain_ui_queue()
        # 重复启动时留下的“唤醒请求”：直接把已有窗口显示出来
        if take_wake_request():
            self._show_window()
        self._refresh()
        self._root.after(UI_TICK_MS, self._tick)

    def _drain_ui_queue(self) -> None:
        while True:
            try:
                action = self._ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                action()
            except Exception:  # 单条请求失败不能影响界面循环
                logger.exception("处理界面请求失败")

    def _post(self, action: Callable[[], None]) -> None:
        """从任意线程安全地把操作投递到主线程执行。"""
        self._ui_queue.put(action)

    # ------------------------------------------------------------------
    # 清理前确认弹窗
    # ------------------------------------------------------------------

    def _request_clean_confirmation(self, request: CleanConfirmationRequest) -> None:
        """确认请求入口（可能运行在监控线程）：只投递，立即返回。"""
        self._post(lambda: self._show_clean_confirmation(request))

    def _show_clean_confirmation(self, request: CleanConfirmationRequest) -> None:
        """主线程：创建确认弹窗；已有弹窗时忽略，保证最多一个。"""
        if self._confirm_dialog is not None:
            logger.info(
                "已有清理确认弹窗（token=%s），忽略重复请求 token=%d",
                self._confirm_dialog.request.token,
                request.token,
            )
            return
        try:
            self._confirm_dialog = CleanConfirmDialog(
                self._root, request, self._on_confirmation_decided
            )
        except Exception:
            logger.exception("创建清理确认弹窗失败，本轮取消清理")
            self._controller.submit_confirmation(ConfirmationDecision.ABORTED, request.token)

    def _on_confirmation_decided(self, token: int, decision: ConfirmationDecision) -> None:
        """主线程：把用户决定交给控制器（复核与清理在后台线程执行）。"""
        self._confirm_dialog = None
        logger.info("清理确认弹窗已关闭：token=%d 决定=%s", token, decision.value)
        self._controller.submit_confirmation(decision, token)

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        status = self._controller.status()
        scan = status.last_scan

        self._lbl_state.configure(text=self._controller.state_summary(status))
        self._lbl_count.configure(
            text=f"{scan.total_items:,}" if scan is not None else "—"
        )
        self._rows["threshold"].configure(text=f"{self._controller.config.threshold_items:,}")

        last_check = _format_time(scan.queried_at) if scan is not None else "尚未检查"
        if scan is not None and not scan.is_complete:
            last_check += "  ⚠ 统计不完整"
        self._rows["last_check"].configure(text=last_check)
        self._rows["next_check"].configure(
            text=f"约 {status.interval_seconds} 秒后" if not self._controller.is_paused() else "已暂停"
        )

        last_clean = status.last_clean_result
        if last_clean is None:
            self._rows["last_clean"].configure(text="尚无清理记录")
        else:
            outcome = _CLEAN_OUTCOME_TEXT.get(last_clean.outcome, last_clean.outcome.value)
            self._rows["last_clean"].configure(
                text=f"{_format_time(last_clean.started_at)}  {outcome}"
                + (f"（剩余 {last_clean.remaining_items}）" if last_clean.remaining_items else "")
            )

        # 读注册表有成本，每 20 次刷新（约 10 秒）才真正查询一次
        self._tick_count += 1
        if self._tick_count == 1 or self._tick_count % 20 == 0:
            try:
                self._autostart_cached = self._controller.is_start_on_login_enabled()
            except Exception:
                logger.debug("读取开机启动状态失败", exc_info=True)
                self._autostart_cached = False
        self._rows["autostart"].configure(
            text="已启用（登录后自动运行）" if self._autostart_cached else "未启用"
        )
        self._rows["last_clean_at"].configure(text=self._controller.last_clean_summary())
        self._rows["error"].configure(text=status.last_error or "无")
        self._rows["config"].configure(text=str(self._controller.config_path))

        self._var_auto.set(status.auto_clean_enabled)
        self._var_dry.set(status.dry_run)
        self._btn_pause.configure(
            text="恢复监控" if self._controller.is_paused() else "暂停监控"
        )

        self._update_icon(status.state)
        self._notify_changes(status)

    def _update_icon(self, state: MonitorState) -> None:
        if not self._icon_ready.is_set():
            return
        color = _STATE_COLORS.get(state, "#2E7D32")
        if color == self._last_icon_color:
            return
        self._last_icon_color = color
        try:
            self._icon.icon = create_icon_image(color)
            self._icon.title = f"{APP_NAME} — {self._controller.state_summary()}"
        except Exception:  # pragma: no cover - 图标更新失败不应影响主流程
            logger.debug("更新托盘图标失败", exc_info=True)

    def _notify_changes(self, status) -> None:
        """错误与清理结果通过托盘气泡通知，不静默。"""
        if status.last_error and status.last_error != self._last_error_seen:
            self._last_error_seen = status.last_error
            self._notify("清理或检查出现问题", status.last_error)
        elif not status.last_error:
            self._last_error_seen = None

        if status.last_clean_result is not None and status.last_clean_result is not self._last_clean_seen:
            self._last_clean_seen = status.last_clean_result
            outcome = status.last_clean_result
            if outcome.succeeded:
                self._notify("回收站已清空", f"清理了 {outcome.items_before} 个项目")

    def _notify(self, title: str, message: str) -> None:
        if not self._icon_ready.is_set():
            return
        try:
            self._icon.notify(message, title)
        except Exception:  # pragma: no cover - 某些后端不支持气泡通知
            logger.debug("托盘通知失败", exc_info=True)

    # ------------------------------------------------------------------
    # 托盘菜单回调（运行在 pystray 线程）
    # ------------------------------------------------------------------

    def _menu_open_window(self, icon=None, item=None) -> None:
        self._post(self._show_window)

    def _menu_check_now(self, icon=None, item=None) -> None:
        self._post(self._on_check_now)

    def _menu_toggle_auto_clean(self, icon=None, item=None) -> None:
        self._post(self._on_toggle_auto_clean)

    def _menu_toggle_dry_run(self, icon=None, item=None) -> None:
        self._post(self._on_toggle_dry_run)

    def _menu_toggle_hotkey(self, icon=None, item=None) -> None:
        self._post(self._on_toggle_hotkey)

    def _menu_install_shortcuts(self, icon=None, item=None) -> None:
        self._post(self._on_install_shortcuts)

    def _menu_show_logs(self, icon=None, item=None) -> None:
        self._post(self._show_logs)

    def _menu_show_clean_history(self, icon=None, item=None) -> None:
        self._post(self._show_clean_history)

    def _menu_toggle_autostart(self, icon=None, item=None) -> None:
        self._post(self._on_toggle_autostart)

    def _menu_quit(self, icon=None, item=None) -> None:
        self._post(self._quit)

    # ------------------------------------------------------------------
    # 界面动作（主线程）
    # ------------------------------------------------------------------

    def _show_window(self) -> None:
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()

    def _hide_window(self) -> None:
        self._root.withdraw()

    def _on_check_now(self) -> None:
        self._rows["last_check"].configure(text="正在检查…")
        self._controller.request_check()

    def _on_toggle_pause(self) -> None:
        paused = self._controller.toggle_pause()
        logger.info("用户%s监控", "暂停" if paused else "恢复")
        if paused and self._confirm_dialog is not None:
            # 暂停会作废待确认请求，弹窗同步关闭，避免用户点了“立即清理”却什么也没发生。
            self._confirm_dialog.abort("监控已暂停")
        self._refresh()

    def _on_toggle_auto_clean(self) -> None:
        enabled = self._controller.config.auto_clean_enabled
        try:
            if enabled:
                self._controller.set_auto_clean_enabled(False)
            elif self._controller.needs_risk_confirmation():
                confirmed = messagebox.askyesno(
                    RISK_NOTICE_TITLE,
                    RISK_NOTICE + "\n\n确认启用自动清理吗？",
                    icon="warning",
                    default="no",
                    parent=self._root,
                )
                if not confirmed:
                    logger.warning("用户取消了启用自动清理")
                    return
                self._controller.set_auto_clean_enabled(True, risk_acknowledged=True)
            else:
                self._controller.set_auto_clean_enabled(True)
        except RiskConfirmationRequired as exc:
            messagebox.showwarning(RISK_NOTICE_TITLE, str(exc), parent=self._root)
        except Exception as exc:  # 不静默
            logger.exception("切换自动清理失败")
            messagebox.showerror(APP_NAME, f"切换自动清理失败：{exc}", parent=self._root)
        self._refresh()

    def _on_auto_checkbox(self) -> None:
        desired = bool(self._var_auto.get())
        if desired == self._controller.config.auto_clean_enabled:
            return
        self._on_toggle_auto_clean()

    def _on_toggle_dry_run(self) -> None:
        enabled = self._controller.toggle_dry_run()
        logger.info("用户%s试运行模式", "开启" if enabled else "关闭")
        self._refresh()

    def _on_dry_checkbox(self) -> None:
        desired = bool(self._var_dry.get())
        if desired == self._controller.config.dry_run:
            return
        self._on_toggle_dry_run()

    def _on_toggle_hotkey(self) -> None:
        """开关全局热键（Ctrl + Alt + R）。"""
        enabled = self._controller.toggle_hotkey()
        if enabled:
            self._enable_hotkey()
        else:
            self._disable_hotkey()
        logger.info("全局热键已%s", "启用" if enabled else "关闭")
        self._refresh()

    def _on_install_shortcuts(self) -> None:
        created = self._controller.install_shortcuts()
        if created:
            messagebox.showinfo(
                APP_NAME,
                "已创建快捷方式：\n" + "\n".join(str(p) for p in created),
                parent=self._root,
            )
        else:
            messagebox.showwarning(
                APP_NAME, "快捷方式创建失败，请查看日志。", parent=self._root
            )

    def _on_toggle_autostart(self) -> None:
        try:
            state = self._controller.set_start_on_login(
                not self._controller.is_start_on_login_enabled()
            )
            logger.info("开机启动已%s", "启用" if state else "关闭")
        except Exception as exc:  # 不静默
            logger.exception("切换开机启动失败")
            messagebox.showerror(APP_NAME, f"切换开机启动失败：{exc}", parent=self._root)
        self._refresh()

    def _open_text_window(self, title: str, lines: list[str], footer: str) -> None:
        """打开一个只读文本窗口（日志 / 清理历史共用）。

        刻意**不**调用 ``transient(self._root)``：托盘常驻时主窗口是隐藏的，
        transient 会让子窗口跟着隐藏，用户点了菜单却看不到内容。
        """
        window = tk.Toplevel(self._root)
        window.title(f"{APP_NAME} — {title}")
        window.geometry("760x420")

        text = scrolledtext.ScrolledText(window, wrap="none", font=("Consolas", 9))
        text.pack(fill="both", expand=True)
        text.insert("end", "\n".join(lines) if lines else "（暂无记录）")
        text.configure(state="disabled")
        text.see("end")

        ttk.Label(window, text=footer).pack(anchor="w", padx=6, pady=4)
        ttk.Button(window, text="关闭", command=window.destroy).pack(pady=(0, 8))
        window.lift()

    def _show_logs(self) -> None:
        self._open_text_window(
            "最近日志",
            self._controller.recent_logs(limit=200),
            str(self._controller.log_path),
        )

    def _show_clean_history(self) -> None:
        """清理历史：什么时候清空过、清理前后各多少、结果如何。"""
        records = self._controller.recent_cleans(limit=50)
        lines = []
        for index, record in enumerate(records, start=1):
            line = f"{index:>2}. {record.format_full_time()}  {record.describe()}"
            line += f"  触发={record.trigger}"
            if record.error:
                line += f"\n      错误：{record.error}"
            lines.append(line)
        self._open_text_window(
            "清理历史（最近 50 次）", lines, str(self._controller.monitor.history.path)
        )

    def _quit(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        logger.info("用户请求退出程序")
        try:
            self._disable_hotkey()
            # 退出前先作废待确认的清理请求：关闭弹窗不会触发任何清理。
            dialog = self._confirm_dialog
            if dialog is not None:
                dialog.abort("用户退出程序")
            self._controller.shutdown()
        finally:
            try:
                if self._icon_ready.is_set() and self._icon is not None:
                    self._icon.stop()
            except Exception:  # pragma: no cover
                logger.debug("停止托盘图标失败", exc_info=True)
            try:
                self._root.quit()
                self._root.destroy()
            except tk.TclError:  # pragma: no cover - 窗口可能已被销毁
                pass


def run_tray(controller: AppController, *, start_minimized: bool = False) -> None:
    """创建并运行托盘应用（阻塞）。

    :param start_minimized: 为 ``True`` 时静默驻留托盘（开机自启场景），
        否则启动后立刻显示状态窗口，避免用户找不到程序在哪。
    """
    TrayApplication(controller, start_minimized=start_minimized).run()


__all__ = ["TrayApplication", "create_icon_image", "run_tray", "LABEL_AUTO_ON"]

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

import os
import queue
import tempfile
import threading
import time
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

import pystray
from PIL import Image, ImageColor, ImageDraw

from . import APP_NAME, __version__, assets
from . import updater as updater_module
from .app import (
    LABEL_AUTO_ON,
    RISK_NOTICE,
    RISK_NOTICE_TITLE,
    AppController,
    RiskConfirmationRequired,
)
from .appearance import AUTO_RECHECK_SECONDS
from .confirm_dialog import CleanConfirmDialog
from .confirmation import CleanConfirmationRequest, ConfirmationDecision
from .hotkey import HOTKEY_LABEL, HotkeyListener
from .interfaces import CleanOutcome, MonitorState
from .logging_setup import get_logger
from .settings_dialog import SettingsDialog
from .single_instance import take_wake_request
from .theme import (
    Card,
    HeroCard,
    Palette,
    Switch,
    ThemedFrame,
    ThemedLabel,
    ThemeManager,
    retheme_tree,
)
from .updater import LocalUpdate, ReleaseInfo

logger = get_logger()

#: 主线程轮询间隔（毫秒）：既用于刷新界面，也用于处理托盘线程投递的请求。
UI_TICK_MS = 500

#: 状态 -> 托盘图标配色。``None`` 表示保留不锈钢原色（不重着色）。
#: 约定：正常=原色、暂停=橙、待确认=黄、清理中=蓝、错误=红。
_STATE_COLORS: dict[MonitorState, str | None] = {
    MonitorState.IDLE: None,
    MonitorState.CHECKING: None,
    MonitorState.PAUSED: "#EF6C00",
    MonitorState.THRESHOLD_REACHED: "#FBC02D",
    MonitorState.AWAITING_CONFIRMATION: "#FBC02D",
    MonitorState.CLEANING: "#1565C0",
    MonitorState.VERIFYING: "#1565C0",
    MonitorState.ERROR: "#C62828",
}

_CLEAN_OUTCOME_TEXT: dict[CleanOutcome, str] = {
    CleanOutcome.SUCCESS: "清理成功",
    CleanOutcome.PARTIAL: "部分清理（仍有剩余）",
    CleanOutcome.FAILED: "清理失败",
    CleanOutcome.INCOMPLETE: "结果未确认（统计不完整）",
}

#: 任务栏分组标识：不设置的话 Windows 会把窗口归到 python.exe / 解释器图标下。
APP_USER_MODEL_ID = f"{APP_NAME}.App"


def _set_app_user_model_id() -> None:
    """向 Windows 声明本程序身份，任务栏与通知才用得上我们自己的图标。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # pragma: no cover - 老系统 / 受限环境
        logger.debug("设置 AppUserModelID 失败", exc_info=True)


#: 资源缺失时内置画法使用的颜色（与 IDLE 状态一致）。
DEFAULT_ICON_COLOR = "#2E7D32"


def _parse_color(color: str) -> tuple[int, int, int]:
    """把 ``#RRGGBB`` / 颜色名解析成 RGB 三元组。"""
    r, g, b = ImageColor.getrgb(color)[:3]
    return int(r), int(g), int(b)


def _tint(image: Image.Image, color: str) -> Image.Image:
    """把图标重着色为 ``color``，同时保留金属高光与明暗层次。

    做法：以原图亮度为系数缩放目标色（高光接近原色、暗部压暗），alpha 通道
    原样保留 —— 这样菱形网格的镂空处仍然是透明的。
    """
    red, green, blue = _parse_color(color)
    luminance = image.convert("L").point(lambda v: int(26 + 0.88 * v))
    return Image.merge(
        "RGBA",
        (
            luminance.point(lambda v: min(255, red * v // 255)),
            luminance.point(lambda v: min(255, green * v // 255)),
            luminance.point(lambda v: min(255, blue * v // 255)),
            image.getchannel("A"),
        ),
    )


def _draw_fallback_icon(color: str, size: int) -> Image.Image:
    """内置的矢量画法：仅在图标资源缺失时兜底，保证托盘永远有图标。"""
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


def create_icon_image(color: str | None = DEFAULT_ICON_COLOR, size: int = 64) -> Image.Image:
    """生成托盘图标：以 ``assets/icon.png``（真实照片）为底，按状态着色。

    :param color: 状态色；传 ``None`` 保留不锈钢原色（用于 .ico 等品牌场景）。
    :param size: 输出边长（正方形）。
    """
    source = assets.load_icon(size)
    if source is None:  # 资源缺失：退回内置画法，图标不至于消失
        return _draw_fallback_icon(color or DEFAULT_ICON_COLOR, size)
    if color is None:
        return source
    return _tint(source, color)


def _format_time(value: datetime | None) -> str:
    return value.strftime("%H:%M:%S") if value is not None else "—"


class TrayApplication:
    """托盘应用：组合托盘图标与 tkinter 状态窗口。"""

    def __init__(
        self,
        controller: AppController,
        *,
        start_minimized: bool = False,
        on_apply_update: Callable[[Path], None] | None = None,
    ) -> None:
        self._controller = controller
        self._ui_queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._stopping = False
        #: 登录自启时为 True：静默驻留托盘，不弹窗。
        self._start_minimized = start_minimized
        #: 收到并校验新版本后的换包回调（由 __main__ 注入，释放锁 + 启动新 EXE + 退出）。
        self._on_apply_update = on_apply_update
        self._hotkey = HotkeyListener(self._post_hotkey)

        self._last_error_seen: str | None = None
        self._last_clean_seen: object = None
        self._last_icon_color: str | None = None
        self._tick_count = 0
        self._autostart_cached = False

        # 自动升级：首次检查延迟约 30s，之后每 6h 一次（节流在 _maybe_check_update）。
        self._last_update_check = time.monotonic() - (
            updater_module.UPDATE_CHECK_INTERVAL_SECONDS
            - updater_module.UPDATE_FIRST_CHECK_DELAY_SECONDS
        )
        self._update_checking = False
        self._pending_update: ReleaseInfo | LocalUpdate | None = None

        #: 同一时刻最多一个清理确认弹窗。
        self._confirm_dialog: CleanConfirmDialog | None = None
        #: 同一时刻最多一个设置对话框。
        self._settings_dialog: SettingsDialog | None = None
        # 监控线程（后台）通过控制器回调提交弹窗请求，这里负责投递到主线程。
        controller.on_confirmation_request = self._request_clean_confirmation

        self._icon_ready = threading.Event()

        self._root = tk.Tk()
        # 立刻隐藏，避免启动瞬间闪出一个空白窗口
        self._root.withdraw()
        self._root.title(f"{APP_NAME} v{__version__}")
        self._root.protocol("WM_DELETE_WINDOW", self._hide_window)
        #: 皮肤管理器：模式来自配置（auto / light / dark），自动模式按系统时间判定。
        self._theme = ThemeManager(self._root, mode=self._controller.config.theme)
        #: 自动模式的周期性判定计数（每 ``AUTO_RECHECK_SECONDS`` 秒检查一次分界）。
        self._theme_ticks = 0
        #: 窗口 / 任务栏图标（必须持有引用：PhotoImage 被回收后图标会变空白）。
        self._window_icon: Any = None
        self._apply_window_icon()

        self._build_window()
        # 注册换肤回调：之后每次切换皮肤都会刷新主窗口（含在线换肤）
        self._unregister_theme = self._theme.register(self._apply_palette)
        # 托盘图标延迟到 run() 才真正创建：
        # pystray 的 win32 Icon 一旦被构造却从未 run()，垃圾回收时 __del__ 会抛
        # “不可捕获异常”（_icon_handle 未初始化），在测试里表现为随机的
        # PytestUnraisableExceptionWarning。延迟创建既避开这个坑，
        # 也让 __init__ 不产生任何操作系统资源。
        self._icon: pystray.Icon | None = None

    # ------------------------------------------------------------------
    # 图标
    # ------------------------------------------------------------------

    def _apply_window_icon(self) -> None:
        """给主窗口 / 任务栏 / 后续对话框设置图标（资源缺失时静默跳过）。"""
        image = assets.load_icon(assets.MAX_ICON_SIZE)
        if image is None:  # 缺资源不值得让界面起不来
            logger.debug("未找到图标资源：%s", assets.icon_file())
            return
        try:
            from PIL import ImageTk

            self._window_icon = ImageTk.PhotoImage(image)
            # default=True：之后创建的 Toplevel（设置 / 清理确认对话框）自动继承
            self._root.iconphoto(True, self._window_icon)
        except Exception:  # pragma: no cover - 极端环境下没有 Tk 图像支持
            logger.debug("设置窗口图标失败", exc_info=True)

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------

    def _build_window(self) -> None:
        """构建状态窗口：英雄卡（状态 + 大数字 + 进度）+ 概览 + 选项 + 操作。"""
        root = self._root
        palette = self._theme.palette
        font = self._theme.font_family
        root.minsize(420, 400)

        outer = ThemedFrame(root, palette)
        outer.pack(fill="both", expand=True, padx=16, pady=16)

        # -- 顶部英雄卡 --
        self._hero = HeroCard(outer, palette)
        self._hero.pack(fill="x")
        # 沿用旧属性名，避免破坏既有调用点（测试 / 菜单）
        self._lbl_state = self._hero.state_label
        self._lbl_count = self._hero.count_label

        # -- 运行概览 --
        ThemedLabel(
            outer,
            palette,
            bg="canvas",
            fg="text_muted",
            text="运行概览",
            font=(font, 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(14, 6))

        overview = Card(outer, palette, stretch=True, padding=12)
        overview.pack(fill="x")
        overview.body.columnconfigure(1, weight=1)

        self._rows: dict[str, ThemedLabel] = {}
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
            )
        ):
            ThemedLabel(
                overview.body,
                palette,
                bg="surface",
                fg="text_muted",
                text=title,
                font=(font, 9),
                anchor="w",
            ).grid(row=index, column=0, sticky="nw", pady=3)
            value = ThemedLabel(
                overview.body,
                palette,
                bg="surface",
                fg="text",
                text="—",
                wraplength=248,
                justify="left",
                anchor="w",
                font=(font, 9),
            )
            value.grid(row=index, column=1, sticky="w", pady=3, padx=(10, 0))
            self._rows[key] = value

        # -- 选项（开关样式）--
        ThemedLabel(
            outer,
            palette,
            bg="canvas",
            fg="text_muted",
            text="选项",
            font=(font, 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(14, 6))

        options = Card(outer, palette, stretch=True, padding=12)
        options.pack(fill="x")
        options.body.columnconfigure(0, weight=1)

        self._var_auto = tk.BooleanVar(value=False)
        self._build_option_row(
            options.body,
            0,
            "自动清理",
            "达到阈值后自动清空回收站（清理前会确认）",
            self._var_auto,
            self._on_auto_checkbox,
        )

        # -- 操作按钮 --
        buttons = ThemedFrame(outer, palette)
        buttons.pack(fill="x", pady=(14, 0))
        for column in range(2):
            buttons.columnconfigure(column, weight=1)

        ttk.Button(
            buttons, text="立即检查", style="Accent.TButton", command=self._on_check_now
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=4)
        self._btn_pause = ttk.Button(buttons, text="暂停监控", command=self._on_toggle_pause)
        self._btn_pause.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=4)

        ttk.Button(buttons, text="查看最近日志", command=self._show_logs).grid(
            row=1, column=0, sticky="ew", padx=(0, 4), pady=4
        )
        ttk.Button(buttons, text="隐藏窗口", command=self._hide_window).grid(
            row=1, column=1, sticky="ew", padx=(4, 0), pady=4
        )

        ttk.Button(buttons, text="设置…", command=self._on_settings).grid(
            row=2, column=0, sticky="ew", padx=(0, 4), pady=4
        )
        self._btn_theme = ttk.Button(
            buttons, text=self._theme_button_text(), command=self._on_cycle_theme
        )
        self._btn_theme.grid(row=2, column=1, sticky="ew", padx=(4, 0), pady=4)

        ttk.Button(buttons, text="退出程序", command=self._quit).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0)
        )

    def _build_option_row(
        self,
        parent: tk.Misc,
        row: int,
        title: str,
        hint: str,
        variable: tk.BooleanVar,
        command: Callable[[], None],
    ) -> None:
        """一行“标题 + 说明 + 开关”。"""
        palette = self._theme.palette
        font = self._theme.font_family
        holder = ThemedFrame(parent, palette, bg="surface")
        holder.grid(row=row, column=0, sticky="ew", pady=(0 if row == 0 else 8, 0))
        holder.columnconfigure(0, weight=1)

        ThemedLabel(
            holder,
            palette,
            bg="surface",
            fg="text",
            text=title,
            font=(font, 9),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ThemedLabel(
            holder,
            palette,
            bg="surface",
            fg="text_muted",
            text=hint,
            font=(font, 8),
            anchor="w",
        ).grid(row=1, column=0, sticky="w")

        Switch(holder, variable, palette, command=command, bg="surface").grid(
            row=0, column=1, rowspan=2, sticky="e", padx=(10, 0)
        )

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("打开状态窗口", self._menu_open_window, default=True),
            pystray.MenuItem("立即检查", self._menu_check_now),
            pystray.MenuItem("设置…", self._menu_settings),
            pystray.MenuItem(self._theme_menu_text, self._menu_cycle_theme),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._auto_clean_menu_text, self._menu_toggle_auto_clean),
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
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("检查更新", self._menu_check_update),
            pystray.MenuItem("安装更新", self._menu_install_update),
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

    def _theme_menu_text(self, item: pystray.MenuItem) -> str:
        return self._theme_button_text()

    def _theme_button_text(self) -> str:
        """按钮 / 菜单项文案：当前模式 + 自动模式下的实际皮肤。"""
        return f"皮肤：{self._theme.label()}"

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
        _set_app_user_model_id()
        self._controller.start()

        self._icon = pystray.Icon(
            APP_NAME,
            # 初始即“正常”状态：保留不锈钢原色，之后按 _STATE_COLORS 切换。
            create_icon_image(None),
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
        self._maybe_refresh_theme()
        self._maybe_check_update()
        self._root.after(UI_TICK_MS, self._tick)

    def _maybe_refresh_theme(self) -> None:
        """自动模式下周期性检查：跨过白天 / 夜间分界时自动换肤。"""
        self._theme_ticks += 1
        if self._theme_ticks * UI_TICK_MS < AUTO_RECHECK_SECONDS * 1000:
            return
        self._theme_ticks = 0
        try:
            if self._theme.refresh():
                logger.info("皮肤已按系统时间自动切换为 %s", self._theme.resolved)
            self._refresh_theme_button()
        except Exception:  # 换肤失败不能影响主循环
            logger.exception("自动切换皮肤失败")

    def _refresh_theme_button(self) -> None:
        try:
            self._btn_theme.configure(text=self._theme_button_text())
        except (tk.TclError, AttributeError):  # pragma: no cover - 窗口已销毁
            logger.debug("刷新皮肤按钮失败", exc_info=True)

    def _on_cycle_theme(self) -> None:
        """手动换皮肤：跟随系统时间 -> 浅色 -> 深色 -> 跟随系统时间。"""
        try:
            mode = self._controller.cycle_theme()
        except Exception as exc:  # 不静默
            logger.exception("切换皮肤失败")
            messagebox.showerror(APP_NAME, f"切换皮肤失败：{exc}", parent=self._root)
            return
        self._theme.set_mode(mode)
        self._refresh_theme_button()
        logger.info("用户切换皮肤：%s", mode)

    def _apply_palette(self, palette: Palette) -> None:
        """换肤回调：刷新主窗口底色与全部自绘控件。"""
        try:
            self._root.configure(bg=palette.canvas)
        except tk.TclError:  # pragma: no cover - 根窗口已销毁
            return
        retheme_tree(self._root, palette)
        self._refresh_theme_button()

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
                self._root, request, self._on_confirmation_decided, theme=self._theme
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

        # 英雄卡：状态徽标 + 大字号项目数 + 距阈值进度
        count_text = f"{scan.total_items:,}" if scan is not None else "—"
        threshold_items = max(1, self._controller.config.threshold_items)
        ratio = (scan.total_items / threshold_items) if scan is not None else 0.0
        self._hero.set_values(
            state_text=self._controller.state_summary(status),
            count_text=count_text,
            caption=f"回收站项目总数 / 触发阈值 {threshold_items:,}",
            ratio=ratio,
        )
        self._set_row("threshold", f"{threshold_items:,}", tone="text")

        last_check = _format_time(scan.queried_at) if scan is not None else "尚未检查"
        incomplete = scan is not None and not scan.is_complete
        if incomplete:
            last_check += "  ⚠ 统计不完整"
        self._set_row("last_check", last_check, tone="warn" if incomplete else "text")
        self._set_row(
            "next_check",
            (
                f"约 {self._controller.seconds_until_next_check()} 秒后"
                if not self._controller.is_paused()
                else "已暂停"
            ),
            tone="text",
        )

        last_clean = status.last_clean_result
        if last_clean is None:
            self._set_row("last_clean", "尚无清理记录", tone="text_muted")
        else:
            outcome = _CLEAN_OUTCOME_TEXT.get(last_clean.outcome, last_clean.outcome.value)
            self._set_row(
                "last_clean",
                f"{_format_time(last_clean.started_at)}  {outcome}"
                + (f"（剩余 {last_clean.remaining_items}）" if last_clean.remaining_items else ""),
                tone="ok" if last_clean.succeeded else "danger",
            )

        # 读注册表有成本，每 20 次刷新（约 10 秒）才真正查询一次
        self._tick_count += 1
        if self._tick_count == 1 or self._tick_count % 20 == 0:
            try:
                self._autostart_cached = self._controller.is_start_on_login_enabled()
            except Exception:
                logger.debug("读取开机启动状态失败", exc_info=True)
                self._autostart_cached = False
        self._set_row(
            "autostart",
            "已启用（登录后自动运行）" if self._autostart_cached else "未启用",
            tone="ok" if self._autostart_cached else "text_muted",
        )
        self._set_row("last_clean_at", self._controller.last_clean_summary(), tone="text_muted")
        self._set_row(
            "error", status.last_error or "无", tone="danger" if status.last_error else "text_muted"
        )
        self._set_row("config", str(self._controller.config_path), tone="text_muted")

        self._var_auto.set(status.auto_clean_enabled)
        self._btn_pause.configure(text="恢复监控" if self._controller.is_paused() else "暂停监控")

        self._update_icon(status.state)
        self._notify_changes(status)

    def _set_row(self, key: str, text: str, *, tone: str = "text") -> None:
        """刷新一行概览：文字 + 语义配色（成功绿 / 失败红 / 提醒橙 / 中性灰）。"""
        label = self._rows[key]
        label.configure(text=text)
        label.set_color(fg=tone)

    def _update_icon(self, state: MonitorState) -> None:
        if not self._icon_ready.is_set() or self._icon is None:
            return
        # 未登记的状态按“正常”处理（保留原色）。
        color = _STATE_COLORS.get(state)
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

        if (
            status.last_clean_result is not None
            and status.last_clean_result is not self._last_clean_seen
        ):
            self._last_clean_seen = status.last_clean_result
            outcome = status.last_clean_result
            if outcome.succeeded:
                self._notify("回收站已清空", f"清理了 {outcome.items_before} 个项目")

    def _notify(self, title: str, message: str) -> None:
        if not self._icon_ready.is_set() or self._icon is None:
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

    def _menu_settings(self, icon=None, item=None) -> None:
        self._post(self._on_settings)

    def _menu_cycle_theme(self, icon=None, item=None) -> None:
        self._post(self._on_cycle_theme)

    def _menu_toggle_auto_clean(self, icon=None, item=None) -> None:
        self._post(self._on_toggle_auto_clean)

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
    # 自动升级（GitHub 拉取 + 本地信号兜底）
    # ------------------------------------------------------------------

    def _menu_check_update(self, icon=None, item=None) -> None:
        """手动触发一次更新检查（忽略节流）。"""
        self._last_update_check = 0.0
        self._maybe_check_update(force=True)

    def _menu_install_update(self, icon=None, item=None) -> None:
        """手动安装已发现的新版本（仅当存在 pending 更新）。"""
        if self._pending_update is None:
            self._notify("无需更新", "当前已是最新版本")
            return
        self._prepare_and_apply(self._pending_update)

    def _maybe_check_update(self, force: bool = False) -> None:
        """节流地发起一次更新检查（网络调用在后台线程，不阻塞 UI）。"""
        if self._update_checking:
            return
        now = time.monotonic()
        interval = updater_module.UPDATE_CHECK_INTERVAL_SECONDS
        if not force and (now - self._last_update_check) < interval:
            return
        self._last_update_check = now
        self._update_checking = True
        threading.Thread(target=self._do_update_check, daemon=True).start()

    def _do_update_check(self) -> None:
        try:
            result = updater_module.check_for_update(current_version=__version__)
            self._post(lambda: self._on_update_checked(result))
        except Exception:  # 检查异常不影响主循环
            logger.exception("更新检查异常")
        finally:
            self._update_checking = False

    def _on_update_checked(self, result) -> None:
        """主线程处理检查结果。"""
        if result.status == "error":
            logger.warning("更新检查失败：%s", result.error)
            return
        if result.status == "up_to_date":
            self._pending_update = None
            return

        update = result.release if result.release is not None else result.local
        self._pending_update = update
        version = update.version
        if self._controller.config.auto_update_on_signal:
            logger.info("发现新版本 v%s，开始自动下载并换包", version)
            if isinstance(update, ReleaseInfo) and update.expected_sha256 is None:
                self._notify("正在更新", f"v{version} 未提供校验和，已跳过完整性校验")
            else:
                self._notify("正在更新", f"v{version} 正在后台下载并安装")
            self._prepare_and_apply(update)
        else:
            logger.info("发现新版本 v%s（自动更新已关闭，请手动安装）", version)
            self._notify("发现新版本", f"v{version} 可用，请在托盘菜单“安装更新”")

    def _prepare_and_apply(self, update: ReleaseInfo | LocalUpdate) -> None:
        """后台线程：下载（GitHub）或直接使用（本地信号）并校验，再交回主线程换包。"""
        threading.Thread(target=self._do_prepare_and_apply, args=(update,), daemon=True).start()

    def _do_prepare_and_apply(self, update: ReleaseInfo | LocalUpdate) -> None:
        try:
            if isinstance(update, ReleaseInfo):
                dest = Path(tempfile.gettempdir()) / f"RecycleBinGuard-{update.version}.exe"
                updater_module.prepare_release(update, dest, timeout=60.0)
                local_exe = dest
            else:  # LocalUpdate：文件已在本地
                local_exe = update.new_exe
            self._post(lambda: self._on_ready_to_apply(local_exe))
        except Exception as exc:  # 准备失败：HOLD，绝不换包
            message = str(exc)
            logger.exception("准备更新失败")
            # 必须在 except 块内把异常信息拷到普通局部变量：
            # Python 会在 except 结束后删除 exc，lambda 延迟执行时会 NameError。
            self._post(lambda: self._notify("更新失败", message))

    def _on_ready_to_apply(self, local_exe: Path) -> None:
        """主线程：执行换包并退出（回调由 __main__ 注入）。"""
        if self._on_apply_update is not None:
            self._on_apply_update(local_exe)
        else:
            logger.warning("未提供换包回调，无法完成更新")

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

    def _on_settings(self) -> None:
        """打开设置对话框；已有对话框时把它提到前台，保证最多一个。"""
        if self._settings_dialog is not None:
            try:
                self._settings_dialog.window.lift()
                self._settings_dialog.window.focus_force()
                return
            except tk.TclError:  # 窗口已被销毁
                self._settings_dialog = None
        try:
            self._settings_dialog = SettingsDialog(
                self._root, self._controller, self._on_settings_saved, theme=self._theme
            )
        except Exception:  # 不静默
            logger.exception("创建设置对话框失败")
            messagebox.showerror(APP_NAME, "无法打开设置窗口，详情见日志。", parent=self._root)

    def _on_settings_saved(self) -> None:
        self._settings_dialog = None
        # 设置里可能改了皮肤：立刻套用新配置
        self._theme.set_mode(self._controller.config.theme)
        self._refresh_theme_button()
        self._refresh()

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
            messagebox.showwarning(APP_NAME, "快捷方式创建失败，请查看日志。", parent=self._root)

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
        palette = self._theme.palette
        window = tk.Toplevel(self._root)
        window.title(f"{APP_NAME} — {title}")
        window.geometry("760x420")
        window.configure(bg=palette.canvas)

        text = scrolledtext.ScrolledText(
            window,
            wrap="none",
            font=("Consolas", 9),
            bg=palette.code,
            fg=palette.code_text,
            insertbackground=palette.code_text,
            highlightthickness=0,
            bd=0,
        )
        text.pack(fill="both", expand=True)
        text.insert("end", "\n".join(lines) if lines else "（暂无记录）")
        text.configure(state="disabled")
        text.see("end")

        ThemedLabel(
            window,
            palette,
            bg="canvas",
            fg="text_muted",
            text=footer,
            anchor="w",
        ).pack(fill="x", padx=8, pady=4)
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


def run_tray(
    controller: AppController,
    *,
    start_minimized: bool = False,
    on_apply_update: Callable[[Path], None] | None = None,
) -> None:
    """创建并运行托盘应用（阻塞）。

    :param start_minimized: 为 ``True`` 时静默驻留托盘（开机自启场景），
        否则启动后立刻显示状态窗口，避免用户找不到程序在哪。
    :param on_apply_update: 收到并校验新版本后，由主程序提供的“换包并退出”回调；
        为 ``None`` 时仅提示、不自动换包。
    """
    TrayApplication(
        controller, start_minimized=start_minimized, on_apply_update=on_apply_update
    ).run()


__all__ = [
    "APP_USER_MODEL_ID",
    "DEFAULT_ICON_COLOR",
    "LABEL_AUTO_ON",
    "TrayApplication",
    "create_icon_image",
    "run_tray",
]

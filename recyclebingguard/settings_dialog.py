"""设置对话框（``tkinter``，**只能在 UI 主线程创建与更新**）。

用于让用户自己调整那些原本只能手改 ``config.json`` 的项：

- 触发阈值（回收站项目数）；
- 占用大小阈值（MB，``0`` = 不启用）；
- 检查间隔（秒）；
- 排除的盘符（不统计、也不清空，如 ``D:``）；
- 开机启动（登录 Windows 后在托盘静默运行）；
- 界面皮肤（跟随系统时间 / 浅色 / 深色）。

校验与持久化都交给 :meth:`AppController.apply_settings`：这里只负责把界面输入
解析成参数，非法输入给出明确提示且**不关闭**窗口。

视觉（本次改版）：每个设置项是一张卡片（标题 + 输入框 + 灰色帮助文案），
底部为主按钮（保存）+ 次按钮（取消），配色跟随
:class:`~recyclebingguard.theme.ThemeManager`。
"""

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk
from typing import Any

from . import autostart
from .app import AppController
from .appearance import THEME_LABELS, THEME_MODES
from .config import CLEAN_SCOPES, SCOPE_EVERYTHING, SCOPE_VIBECODING, ConfigError
from .logging_setup import get_logger
from .theme import (
    LIGHT,
    Card,
    Palette,
    Switch,
    ThemedFrame,
    ThemedLabel,
    ThemeManager,
    default_font_family,
    retheme_tree,
)

logger = get_logger()

#: 对话框标题。
SETTINGS_TITLE = "RecycleBinGuard — 设置"

#: 1 MB = 1024 * 1024 字节。
MB = 1024 * 1024

#: 皮肤下拉框：界面文案列表（顺序与 :data:`~recyclebingguard.appearance.THEME_MODES` 一致）。
THEME_CHOICES = tuple(THEME_LABELS[mode] for mode in THEME_MODES)

#: 设置窗口最大高度占屏幕高度的比例（超出部分滚动查看）。
MAX_SCREEN_FRACTION = 0.85

#: 清理范围下拉框文案 <-> 配置值。
SCOPE_LABELS: dict[str, str] = {
    SCOPE_VIBECODING: "只删 vibecoding 垃圾（推荐）",
    SCOPE_EVERYTHING: "清空整个回收站",
}
#: 文案列表（顺序与 :data:`~recyclebingguard.config.CLEAN_SCOPES` 一致）。
SCOPE_CHOICES = tuple(SCOPE_LABELS[mode] for mode in CLEAN_SCOPES)


class SettingsDialog:
    """一次性设置对话框。

    :param parent: 父窗口（不设 transient，避免主窗口隐藏时被一起隐藏）。
    :param controller: 应用控制器，负责校验与持久化。
    :param on_saved: 保存成功后的回调（用于刷新状态窗口）。
    :param theme: 可选皮肤管理器；提供后对话框会跟随皮肤（含在线切换）。
    """

    def __init__(
        self,
        parent: tk.Misc,
        controller: AppController,
        on_saved: Callable[[], None] | None = None,
        *,
        theme: ThemeManager | None = None,
    ) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("SettingsDialog 必须在 UI 主线程创建")

        self._controller = controller
        self._on_saved = on_saved
        config = controller.config

        self._var_threshold = tk.StringVar(value=str(config.threshold_items))
        self._var_interval = tk.StringVar(value=str(config.poll_interval_seconds))
        self._var_size_mb = tk.StringVar(value=str(round(config.threshold_size_bytes / MB)))
        self._var_excluded = tk.StringVar(value=", ".join(config.excluded_roots))
        self._var_theme = tk.StringVar(value=THEME_LABELS.get(config.theme, THEME_CHOICES[0]))
        self._var_scope = tk.StringVar(value=SCOPE_LABELS.get(config.clean_scope, SCOPE_CHOICES[0]))
        self._initial_patterns = "\n".join(config.garbage_path_patterns)
        self._initial_keep = "\n".join(config.keep_path_patterns)
        self._initial_zones = "\n".join(config.discard_zones)
        self._var_zone_min = tk.StringVar(value=str(config.zone_min_items))
        # 以**注册表实际状态**为准（而非配置值）：配置可能因崩溃残留而与实际不一致。
        self._initial_start_on_login = self._read_start_on_login()
        self._var_start_on_login = tk.BooleanVar(value=self._initial_start_on_login)

        self._theme = theme
        self._palette: Palette = theme.palette if theme is not None else LIGHT
        self._font = theme.font_family if theme is not None else default_font_family(parent)
        self._unregister_theme: Callable[[], None] | None = None

        self._window = tk.Toplevel(parent)
        self._window.title(SETTINGS_TITLE)
        self._window.resizable(False, False)
        self._window.protocol("WM_DELETE_WINDOW", self.close)

        self._build()
        self._apply_palette(self._palette)
        self._center()
        self._window.deiconify()
        self._window.lift()
        self._window.focus_force()

        if self._theme is not None:
            self._unregister_theme = self._theme.register(self._apply_palette)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------

    def _build(self) -> None:
        palette = self._palette
        font = self._font

        # 设置项变多后可能高过屏幕（1024×768 或缩放 150% 时尤其明显），
        # 因此把内容放进可滚动画布：窗口自适应屏幕高度，超出部分滚动查看。
        shell = ThemedFrame(self._window, palette)
        shell.pack(fill="both", expand=True)

        canvas = tk.Canvas(
            shell,
            bg=palette.canvas,
            highlightthickness=0,
            bd=0,
        )
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        outer = ThemedFrame(canvas, palette)
        window_id = canvas.create_window((0, 0), window=outer, anchor="nw")

        def on_outer_configure(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_configure(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        outer.bind("<Configure>", on_outer_configure)
        canvas.bind("<Configure>", on_canvas_configure)

        def on_wheel(event) -> None:
            # 只在确实需要滚动时才吃滚轮事件，短窗口保持原生日志区行为。
            first, last = canvas.yview()
            if first <= 0.0 and last >= 1.0:
                return
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        canvas.bind_all("<MouseWheel>", on_wheel)
        outer.pack_configure(padx=16, pady=16)

        ThemedLabel(
            outer,
            palette,
            bg="canvas",
            fg="text",
            text="清理规则",
            font=(font, 10, "bold"),
            anchor="w",
        ).pack(fill="x")

        rows = (
            ("触发阈值（项目数）", self._var_threshold, "达到该数量即触发清理，最小 1"),
            ("占用大小阈值（MB）", self._var_size_mb, "0 = 只按项目数判断"),
            ("检查间隔（秒）", self._var_interval, "默认 600（10 分钟）"),
            ("排除的盘符", self._var_excluded, "逗号分隔，如 D:, E:（留空 = 所有固定磁盘）"),
        )
        for title, variable, hint in rows:
            self._build_field(outer, title, variable, hint)

        ThemedLabel(
            outer,
            palette,
            bg="canvas",
            fg="text",
            text="清理范围与垃圾判定",
            font=(font, 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(14, 0))

        self._build_scope_field(outer)
        self._build_zone_field(outer)
        self._build_patterns_field(outer)

        ThemedLabel(
            outer,
            palette,
            bg="canvas",
            fg="text",
            text="启动",
            font=(font, 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(14, 0))

        self._build_start_on_login_field(outer)

        ThemedLabel(
            outer,
            palette,
            bg="canvas",
            fg="text",
            text="界面",
            font=(font, 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(14, 0))

        self._build_theme_field(outer)

        buttons = ThemedFrame(outer, palette)
        buttons.pack(fill="x", pady=(16, 0))
        buttons.columnconfigure(0, weight=1)
        ttk.Button(buttons, text="取消", command=self.close).grid(
            row=0, column=1, sticky="e", padx=(8, 0)
        )
        ttk.Button(buttons, text="保存", style="Accent.TButton", command=self.save).grid(
            row=0, column=2, sticky="e"
        )

    def _build_field(self, parent: tk.Misc, title: str, variable: tk.StringVar, hint: str) -> None:
        """一条“标题 + 输入框 + 帮助文案”的设置卡片。"""
        palette = self._palette
        font = self._font
        card = Card(parent, palette, stretch=True, padding=10)
        card.pack(fill="x", pady=(8, 0))
        card.body.columnconfigure(0, weight=1)

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text",
            text=title,
            font=(font, 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        entry = ttk.Entry(card.body, textvariable=variable, width=28)
        entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text_muted",
            text=hint,
            font=(font, 8),
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))

    def _build_zone_field(self, parent: tk.Misc) -> None:
        """可丢区：一行一个目录前缀 + 清理门槛。

        判的是「删除前位于哪个目录」，不关心文件类型 —— 放在这些目录里的项目
        无论是一次性的还是长期更新的，删进回收站够多就一起清。
        """
        palette = self._palette
        font = self._font

        card = Card(parent, palette, stretch=True, padding=10)
        card.pack(fill="x", pady=(8, 0))
        card.body.columnconfigure(0, weight=1)

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text",
            text="可丢区目录（按位置判定，不看文件类型）",
            font=(font, 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        box = tk.Text(
            card.body,
            height=3,
            wrap="none",
            font=("Consolas", 8),
            bg=palette.code,
            fg=palette.code_text,
            insertbackground=palette.code_text,
            highlightthickness=0,
            bd=0,
        )
        box.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        box.insert("1.0", self._initial_zones)
        self._text_zones = box

        threshold = ThemedFrame(card.body, palette, bg="surface")
        threshold.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        threshold.columnconfigure(0, weight=1)
        ThemedLabel(
            threshold,
            palette,
            bg="surface",
            fg="text_muted",
            text="累计多少项后清理",
            font=(font, 8),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        tk.Entry(
            threshold,
            textvariable=self._var_zone_min,
            width=8,
            justify="right",
            font=(font, 9),
        ).grid(row=0, column=1, sticky="e")

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text_muted",
            text="一行一个目录，如 D:\\VibeCoding；累计够门槛就整批清理，0 = 关闭",
            font=(font, 8),
            anchor="w",
        ).grid(row=3, column=0, sticky="w", pady=(4, 0))

    def _build_scope_field(self, parent: tk.Misc) -> None:
        """清理范围下拉框：只删垃圾 / 整盘清空。"""
        palette = self._palette
        font = self._font
        card = Card(parent, palette, stretch=True, padding=10)
        card.pack(fill="x", pady=(8, 0))
        card.body.columnconfigure(0, weight=1)

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text",
            text="清理范围",
            font=(font, 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self._combo_scope = ttk.Combobox(
            card.body,
            textvariable=self._var_scope,
            values=list(SCOPE_CHOICES),
            state="readonly",
            width=28,
        )
        self._combo_scope.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text_muted",
            text="只删垃圾：命中路径规则或时间突发的条目才会被删，其余永久保留",
            font=(font, 8),
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))

    def _build_patterns_field(self, parent: tk.Misc) -> None:
        """垃圾 / 保留两个路径规则文本框（一行一条 glob）。"""
        palette = self._palette
        font = self._font

        for title, initial, hint in (
            (
                "判定为垃圾的路径",
                self._initial_patterns,
                "一行一条 glob，大小写不敏感，作用于删除前的原始路径",
            ),
            (
                "永不清理的路径（优先级最高）",
                self._initial_keep,
                "命中这里的条目一律保留，例如桌面 / 文档 / 下载",
            ),
        ):
            card = Card(parent, palette, stretch=True, padding=10)
            card.pack(fill="x", pady=(8, 0))
            card.body.columnconfigure(0, weight=1)

            ThemedLabel(
                card.body,
                palette,
                bg="surface",
                fg="text",
                text=title,
                font=(font, 9, "bold"),
                anchor="w",
            ).grid(row=0, column=0, sticky="w")

            box = tk.Text(
                card.body,
                height=4,
                wrap="none",
                font=("Consolas", 8),
                bg=palette.code,
                fg=palette.code_text,
                insertbackground=palette.code_text,
                highlightthickness=0,
                bd=0,
            )
            box.grid(row=1, column=0, sticky="ew", pady=(6, 0))
            box.insert("1.0", initial)
            # 保存时再从控件里读回内容（Text 不像 Entry 那样能双向绑定变量）。
            if title.startswith("判定为垃圾"):
                self._text_patterns = box
            else:
                self._text_keep = box

            ThemedLabel(
                card.body,
                palette,
                bg="surface",
                fg="text_muted",
                text=hint,
                font=(font, 8),
                anchor="w",
            ).grid(row=2, column=0, sticky="w", pady=(4, 0))

    def _build_start_on_login_field(self, parent: tk.Misc) -> None:
        """一行“开机启动 + 说明 + 开关”。

        非 Windows 平台不支持（注册表 Run 键仅 Windows 有），此时开关置灰并说明原因，
        而不是让用户点了一个注定失败的开关。
        """
        palette = self._palette
        font = self._font
        supported = autostart.IS_WINDOWS

        card = Card(parent, palette, stretch=True, padding=10)
        card.pack(fill="x", pady=(8, 0))
        card.body.columnconfigure(0, weight=1)

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text",
            text="开机启动",
            font=(font, 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text_muted",
            text=(
                "登录 Windows 后在托盘静默启动（不弹窗）"
                if supported
                else "仅 Windows 支持开机启动"
            ),
            font=(font, 8),
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        self._switch_start_on_login = Switch(
            card.body,
            self._var_start_on_login,
            palette,
            bg="surface",
            command=None if supported else lambda: self._var_start_on_login.set(False),
        )
        self._switch_start_on_login.grid(row=0, column=1, rowspan=2, sticky="e", padx=(10, 0))
        if not supported:
            self._var_start_on_login.set(False)
            self._switch_start_on_login.configure(cursor="arrow")

    def _build_theme_field(self, parent: tk.Misc) -> None:
        palette = self._palette
        font = self._font
        card = Card(parent, palette, stretch=True, padding=10)
        card.pack(fill="x", pady=(8, 0))
        card.body.columnconfigure(0, weight=1)

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text",
            text="界面皮肤",
            font=(font, 9, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self._combo_theme = ttk.Combobox(
            card.body,
            textvariable=self._var_theme,
            values=list(THEME_CHOICES),
            state="readonly",
            width=26,
        )
        self._combo_theme.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        ThemedLabel(
            card.body,
            palette,
            bg="surface",
            fg="text_muted",
            text="跟随系统时间：07:00–19:00 浅色，其余时间深色",
            font=(font, 8),
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(4, 0))

    def _center(self) -> None:
        """居中并对窗口高度封顶，超出部分由内部画布滚动。

        不封顶的话，设置项一多窗口就会高过屏幕（实测 1536px），
        底部按钮会被挤出可见区域。
        """
        try:
            self._window.update_idletasks()
            width = self._window.winfo_reqwidth()
            height = self._window.winfo_reqheight()
            max_height = int(self._window.winfo_screenheight() * MAX_SCREEN_FRACTION)
            height = min(height, max_height)
            x = max(0, (self._window.winfo_screenwidth() - width) // 2)
            y = max(0, (self._window.winfo_screenheight() - height) // 3)
            self._window.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:  # pragma: no cover - 无显示器等极端情况
            logger.debug("对话框居中失败", exc_info=True)

    def _apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        try:
            self._window.configure(bg=palette.canvas)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            return
        retheme_tree(self._window, palette)

    # ------------------------------------------------------------------
    # 只读状态（供 UI / 测试查询）
    # ------------------------------------------------------------------

    @property
    def window(self) -> tk.Toplevel:
        return self._window

    def _read_start_on_login(self) -> bool:
        """读取开机启动的**实际**状态；读取失败按未启用处理（不阻塞打开设置）。"""
        try:
            return bool(self._controller.is_start_on_login_enabled())
        except Exception:  # 注册表不可读等：不影响对话框打开
            logger.debug("读取开机启动状态失败", exc_info=True)
            return False

    def desired_start_on_login(self) -> bool:
        """界面上选择的开机启动状态。"""
        return bool(self._var_start_on_login.get())

    def collect_settings(self) -> dict[str, Any]:
        """把界面输入解析成 :meth:`AppController.apply_settings` 的关键字参数。

        非法输入抛出 :class:`ValueError`（消息可直接展示给用户）。
        """
        threshold = self._parse_int(self._var_threshold.get(), "触发阈值（项目数）", minimum=1)
        interval = self._parse_int(self._var_interval.get(), "检查间隔（秒）", minimum=1)
        size_mb = self._parse_int(self._var_size_mb.get(), "占用大小阈值（MB）", minimum=0)
        excluded = self._parse_roots(self._var_excluded.get())
        return {
            "threshold_items": threshold,
            "poll_interval_seconds": interval,
            "threshold_size_bytes": size_mb * MB,
            "excluded_roots": excluded,
            "theme": self._parse_theme(self._var_theme.get()),
            "clean_scope": self._parse_scope(self._var_scope.get()),
            "garbage_path_patterns": self._parse_patterns(self._text_patterns.get("1.0", "end")),
            "keep_path_patterns": self._parse_patterns(self._text_keep.get("1.0", "end")),
            "discard_zones": self._parse_patterns(self._text_zones.get("1.0", "end")),
            "zone_min_items": self._parse_int(
                self._var_zone_min.get(), "可丢区清理门槛", minimum=0
            ),
        }

    @staticmethod
    def _parse_scope(text: str) -> str:
        """把下拉框文案翻译成配置值（未知文案保持默认的只删垃圾）。"""
        cleaned = text.strip()
        for mode, label in SCOPE_LABELS.items():
            if label == cleaned:
                return mode
        return cleaned.lower() if cleaned.lower() in CLEAN_SCOPES else SCOPE_VIBECODING

    @staticmethod
    def _parse_patterns(raw: str) -> list[str]:
        """一行一条 glob；忽略空行与 ``#`` 开头的注释行。"""
        return [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    @staticmethod
    def _parse_int(raw: str, label: str, *, minimum: int) -> int:
        text = raw.strip()
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(f"{label} 必须是整数：{raw!r}") from exc
        if value < minimum:
            raise ValueError(f"{label} 不能小于 {minimum}，实际为 {value}")
        return value

    @staticmethod
    def _parse_roots(raw: str) -> list[str]:
        parts = raw.replace(";", ",").replace("，", ",").split(",")
        return [part.strip() for part in parts if part.strip()]

    @staticmethod
    def _parse_theme(text: str) -> str:
        """把下拉框文案翻译成配置值（未知文案保持原配置）。"""
        cleaned = text.strip()
        for mode in THEME_MODES:
            if THEME_LABELS[mode] == cleaned:
                return mode
        return cleaned.lower() if cleaned.lower() in THEME_MODES else "auto"

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------

    def save(self) -> None:
        """校验并保存；失败时提示且保持窗口打开。"""
        try:
            settings = self.collect_settings()
        except ValueError as exc:
            messagebox.showwarning(SETTINGS_TITLE, str(exc), parent=self._window)
            return

        try:
            self._controller.apply_settings(**settings)
        except ConfigError as exc:
            messagebox.showerror(SETTINGS_TITLE, f"设置未生效：{exc}", parent=self._window)
            return
        except Exception as exc:  # 不静默
            logger.exception("应用设置失败")
            messagebox.showerror(SETTINGS_TITLE, f"应用设置失败：{exc}", parent=self._window)
            return

        logger.info("设置已保存")

        # 开机启动要写注册表，不能只改配置：配置改了而注册表没改，
        # 下次启动的 sync_start_on_login() 会把它当成“残留”再改回去。
        if not self._apply_start_on_login():
            return  # 提示已弹出，窗口保持打开让用户重试

        if self._on_saved is not None:
            try:
                self._on_saved()
            except Exception:  # pragma: no cover - 回调异常不影响保存结果
                logger.exception("设置保存回调异常")
        self.close()

    def _apply_start_on_login(self) -> bool:
        """把界面上的开机启动选择写入注册表。

        :returns: ``True`` = 已生效（含“本来就是这个状态”）；``False`` = 失败或未达预期，
            此时已向用户提示，调用方应保持窗口打开。
        """
        desired = self.desired_start_on_login()
        # 用户没动这个开关就绝不写注册表：避免“打开设置 → 保存”顺手改动启动项。
        if desired == self._initial_start_on_login:
            return True

        try:
            state = self._controller.set_start_on_login(desired)
        except Exception as exc:  # 不静默
            logger.exception("应用开机启动设置失败")
            messagebox.showerror(SETTINGS_TITLE, f"开机启动设置失败：{exc}", parent=self._window)
            return False

        if state != desired:
            # 注册表没写成功（权限 / 安全软件拦截等）：把开关拨回真实状态再提示。
            self._var_start_on_login.set(state)
            messagebox.showwarning(
                SETTINGS_TITLE,
                "开机启动未能{}，注册表写入可能被拦截。\n当前状态：{}".format(
                    "启用" if desired else "关闭",
                    "已启用" if state else "未启用",
                ),
                parent=self._window,
            )
            return False

        logger.info("开机启动已%s（来自设置窗口）", "启用" if state else "关闭")
        return True

    def close(self) -> None:
        if self._unregister_theme is not None:
            self._unregister_theme()
            self._unregister_theme = None
        # 滚轮绑定用的是 bind_all，必须解绑，否则窗口关掉后仍会拦截全局滚轮。
        try:
            self._window.unbind_all("<MouseWheel>")
        except tk.TclError:  # pragma: no cover - 窗口可能已销毁
            pass
        try:
            self._window.destroy()
        except tk.TclError:  # pragma: no cover - 窗口可能已销毁
            pass


__all__ = ["MB", "SETTINGS_TITLE", "THEME_CHOICES", "SettingsDialog"]

"""设置对话框（``tkinter``，**只能在 UI 主线程创建与更新**）。

用于让用户自己调整那些原本只能手改 ``config.json`` 的项：

- 触发阈值（回收站项目数）；
- 占用大小阈值（MB，``0`` = 不启用）；
- 检查间隔（秒）；
- 排除的盘符（不统计、也不清空，如 ``D:``）；
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

from .app import AppController
from .appearance import THEME_LABELS, THEME_MODES
from .config import ConfigError
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
)

logger = get_logger()

#: 对话框标题。
SETTINGS_TITLE = "RecycleBinGuard — 设置"

#: 1 MB = 1024 * 1024 字节。
MB = 1024 * 1024

#: 皮肤下拉框：界面文案列表（顺序与 :data:`~recyclebingguard.appearance.THEME_MODES` 一致）。
THEME_CHOICES = tuple(THEME_LABELS[mode] for mode in THEME_MODES)


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

        outer = ThemedFrame(self._window, palette)
        outer.pack(fill="both", expand=True, padx=16, pady=16)

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
        try:
            self._window.update_idletasks()
            width = self._window.winfo_reqwidth()
            height = self._window.winfo_reqheight()
            x = max(0, (self._window.winfo_screenwidth() - width) // 2)
            y = max(0, (self._window.winfo_screenheight() - height) // 3)
            self._window.geometry(f"+{x}+{y}")
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
        }

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
        if self._on_saved is not None:
            try:
                self._on_saved()
            except Exception:  # pragma: no cover - 回调异常不影响保存结果
                logger.exception("设置保存回调异常")
        self.close()

    def close(self) -> None:
        if self._unregister_theme is not None:
            self._unregister_theme()
            self._unregister_theme = None
        try:
            self._window.destroy()
        except tk.TclError:  # pragma: no cover - 窗口可能已销毁
            pass


__all__ = ["MB", "SETTINGS_TITLE", "THEME_CHOICES", "SettingsDialog"]

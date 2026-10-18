"""界面主题：浅色 / 深色配色、ttk 样式与若干自绘控件。

设计要点：

- **配色集中在一处**（:data:`PALETTES`），其它界面模块只通过
  :class:`Palette` 取色，不再硬编码颜色字面量；
- ttk 样式是**按 Tk 解释器全局生效**的，因此 :func:`configure_styles` 一次配置，
  之后创建的 ``Toplevel``（设置 / 清理确认对话框）自动继承新皮肤；
- 自绘控件（:class:`Card` / :class:`HeroCard` / :class:`Switch` /
  :class:`ProgressBar` / :class:`CountdownRing` / :class:`Badge`）都实现
  ``apply_palette(palette)``，配合 :func:`retheme_tree` 可以**在线换肤**，
  不需要重建窗口；
- 皮肤模式（自动 / 浅色 / 深色）与时间判定在 :mod:`recyclebingguard.appearance`
  中，本模块只负责“把某个皮肤画出来”。

自绘原因：tkinter 没有圆角容器、开关、进度条与环形进度，而这些控件是本次
UI 改版的核心视觉元素；用 ``Canvas`` 自绘可以在两种皮肤下保持一致外观。
"""

from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from .appearance import (
    THEME_AUTO,
    mode_label,
    next_mode,
    normalize_mode,
    resolve_theme,
)
from .logging_setup import get_logger

logger = get_logger()

#: 首选字体（Windows 自带；缺失时退回 Tk 默认字体）。
PREFERRED_FONTS = ("Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei")

#: ttk 基础主题：只有 ``clam`` 允许完全自定义颜色（vista/xp 会被系统外观覆盖）。
BASE_TTK_THEME = "clam"


def pick_font_family(root: tk.Misc) -> str:
    """挑选一个可用字体族名（找不到就用 Tk 默认）。"""
    try:
        available = {name.lower() for name in tkfont.families(root)}
    except tk.TclError:  # pragma: no cover - 极端环境
        return "TkDefaultFont"
    for candidate in PREFERRED_FONTS:
        if candidate.lower() in available:
            return candidate
    return "TkDefaultFont"


#: 进程内缓存：``tkfont.families()`` 每次枚举字体表都要几毫秒，没必要反复调用。
_FONT_FAMILY_CACHE: str | None = None


def default_font_family(root: tk.Misc | None = None) -> str:
    """自绘控件使用的字体族（结果缓存）。"""
    global _FONT_FAMILY_CACHE
    if _FONT_FAMILY_CACHE is None:
        _FONT_FAMILY_CACHE = pick_font_family(root) if root is not None else PREFERRED_FONTS[0]
    return _FONT_FAMILY_CACHE


@dataclass(frozen=True)
class Palette:
    """一套皮肤用到的全部颜色角色。"""

    name: str
    #: 窗口底色（无卡片区域）。
    canvas: str
    #: 卡片 / 输入框所在表面。
    surface: str
    #: 次级表面（悬停、进度槽、代码区）。
    surface_alt: str
    #: 分隔线与描边。
    border: str
    text: str
    text_muted: str
    #: 强调色（主按钮、进度、选中态）。
    accent: str
    accent_hover: str
    accent_text: str
    #: 强调色的浅底 / 其上的文字。
    accent_soft: str
    accent_soft_text: str
    #: 顶部“英雄卡”底色与其中文字。
    hero: str
    hero_text: str
    hero_sub: str
    hero_pill: str
    hero_pill_text: str
    hero_track: str
    hero_bar: str
    #: 语义色。
    ok: str
    ok_soft: str
    ok_text: str
    warn: str
    warn_soft: str
    warn_text: str
    danger: str
    danger_hover: str
    danger_soft: str
    danger_text: str
    #: 中性标签色（“无”“已启用”之类）。
    neutral_soft: str
    neutral_text: str
    #: 输入控件。
    field: str
    field_border: str
    #: 只读文本窗口（日志）底色。
    code: str
    code_text: str


LIGHT = Palette(
    name="light",
    canvas="#F0F3F7",
    surface="#FFFFFF",
    surface_alt="#EEF1F5",
    border="#E3E6EA",
    text="#1F2329",
    text_muted="#5F6672",
    accent="#1256C4",
    accent_hover="#0F49A8",
    accent_text="#FFFFFF",
    accent_soft="#E8F0FE",
    accent_soft_text="#1256C4",
    hero="#1256C4",
    hero_text="#FFFFFF",
    hero_sub="#D3E1FF",
    hero_pill="#3B74D6",
    hero_pill_text="#FFFFFF",
    hero_track="#3B74D6",
    hero_bar="#A9C6FF",
    ok="#0F9D58",
    ok_soft="#E6F4EA",
    ok_text="#0B6E3F",
    warn="#E8710A",
    warn_soft="#FFF4E5",
    warn_text="#8A4B08",
    danger="#B3261E",
    danger_hover="#8C1D18",
    danger_soft="#FDECEA",
    danger_text="#8C1D18",
    neutral_soft="#EEF0F3",
    neutral_text="#5F6672",
    field="#FFFFFF",
    field_border="#E3E6EA",
    code="#FFFFFF",
    code_text="#1F2329",
)

DARK = Palette(
    name="dark",
    canvas="#1B1D22",
    surface="#26292F",
    surface_alt="#2E323A",
    border="#3A3F48",
    text="#E8EAF0",
    text_muted="#8B93A1",
    accent="#2F7BFF",
    accent_hover="#559BFF",
    accent_text="#FFFFFF",
    accent_soft="#1E2C46",
    accent_soft_text="#8FBAFF",
    hero="#1B4FA8",
    hero_text="#FFFFFF",
    hero_sub="#C7D8F7",
    hero_pill="#2C62BE",
    hero_pill_text="#FFFFFF",
    hero_track="#2C62BE",
    hero_bar="#9CC0FF",
    ok="#3FBF7F",
    ok_soft="#16351F",
    ok_text="#7FE0AC",
    warn="#FFB45E",
    warn_soft="#3A2A16",
    warn_text="#FFC98A",
    danger="#E0524A",
    danger_hover="#F06A62",
    danger_soft="#3D1D1A",
    danger_text="#FFB4AE",
    neutral_soft="#2E323A",
    neutral_text="#9AA3B2",
    field="#212429",
    field_border="#3A3F48",
    code="#212429",
    code_text="#E8EAF0",
)

#: 皮肤名 -> 配色。
PALETTES: dict[str, Palette] = {LIGHT.name: LIGHT, DARK.name: DARK}


# ----------------------------------------------------------------------
# ttk 样式
# ----------------------------------------------------------------------


def configure_styles(
    root: tk.Misc, palette: Palette, *, font_family: str | None = None
) -> ttk.Style:
    """把配色写入 ttk 样式表（全局生效，含之后创建的窗口）。"""
    family = font_family or pick_font_family(root)
    style = ttk.Style(root)
    try:
        style.theme_use(BASE_TTK_THEME)
    except tk.TclError:  # pragma: no cover - 极少见的受限 Tk
        logger.debug("设置 ttk 基础主题失败", exc_info=True)

    style.configure(
        ".",
        background=palette.canvas,
        foreground=palette.text,
        fieldbackground=palette.field,
        bordercolor=palette.border,
        lightcolor=palette.canvas,
        darkcolor=palette.canvas,
        focuscolor=palette.accent,
        font=(family, 9),
    )

    # 容器与文本
    style.configure("TFrame", background=palette.canvas)
    style.configure("Surface.TFrame", background=palette.surface)
    style.configure("TLabel", background=palette.canvas, foreground=palette.text)
    style.configure("Surface.TLabel", background=palette.surface, foreground=palette.text)
    style.configure("Muted.TLabel", background=palette.canvas, foreground=palette.text_muted)
    style.configure(
        "SurfaceMuted.TLabel", background=palette.surface, foreground=palette.text_muted
    )
    style.configure(
        "Title.TLabel",
        background=palette.canvas,
        foreground=palette.text,
        font=(family, 13, "bold"),
    )
    style.configure(
        "Section.TLabel", background=palette.canvas, foreground=palette.text_muted, font=(family, 9)
    )
    style.configure("TSeparator", background=palette.border)

    # 按钮：普通 / 强调 / 危险
    style.configure(
        "TButton",
        background=palette.surface,
        foreground=palette.text,
        bordercolor=palette.border,
        lightcolor=palette.surface,
        darkcolor=palette.surface,
        focuscolor=palette.accent,
        relief="flat",
        borderwidth=1,
        padding=(12, 7),
    )
    style.map(
        "TButton",
        background=[("pressed", palette.surface_alt), ("active", palette.surface_alt)],
        foreground=[("disabled", palette.text_muted)],
        bordercolor=[("focus", palette.accent)],
    )
    style.configure(
        "Accent.TButton",
        background=palette.accent,
        foreground=palette.accent_text,
        bordercolor=palette.accent,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
        relief="flat",
        borderwidth=1,
        padding=(12, 8),
    )
    style.map(
        "Accent.TButton",
        background=[("pressed", palette.accent_hover), ("active", palette.accent_hover)],
        foreground=[("disabled", palette.text_muted)],
        bordercolor=[("focus", palette.accent_hover)],
    )
    style.configure(
        "Danger.TButton",
        background=palette.danger,
        foreground=palette.accent_text,
        bordercolor=palette.danger,
        lightcolor=palette.danger,
        darkcolor=palette.danger,
        relief="flat",
        borderwidth=1,
        padding=(12, 8),
    )
    style.map(
        "Danger.TButton",
        background=[("pressed", palette.danger_hover), ("active", palette.danger_hover)],
        foreground=[("disabled", palette.text_muted)],
    )

    # 输入控件
    style.configure(
        "TEntry",
        fieldbackground=palette.field,
        foreground=palette.text,
        insertcolor=palette.text,
        bordercolor=palette.field_border,
        lightcolor=palette.field_border,
        darkcolor=palette.field_border,
        padding=6,
        relief="flat",
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", palette.accent)],
        lightcolor=[("focus", palette.accent)],
        darkcolor=[("focus", palette.accent)],
    )
    style.configure(
        "TCombobox",
        fieldbackground=palette.field,
        background=palette.field,
        foreground=palette.text,
        arrowcolor=palette.text_muted,
        bordercolor=palette.field_border,
        lightcolor=palette.field_border,
        darkcolor=palette.field_border,
        padding=5,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette.field)],
        foreground=[("readonly", palette.text)],
        bordercolor=[("focus", palette.accent)],
    )
    style.configure("TCheckbutton", background=palette.canvas, foreground=palette.text)
    style.map(
        "TCheckbutton",
        background=[("active", palette.canvas)],
        foreground=[("disabled", palette.text_muted)],
        indicatorcolor=[("selected", palette.accent), ("!selected", palette.field)],
    )
    style.configure(
        "Horizontal.TProgressbar",
        background=palette.accent,
        troughcolor=palette.surface_alt,
        bordercolor=palette.accent,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
    )
    style.configure(
        "Vertical.TScrollbar",
        background=palette.surface_alt,
        troughcolor=palette.canvas,
        bordercolor=palette.canvas,
        arrowcolor=palette.text_muted,
        lightcolor=palette.surface_alt,
        darkcolor=palette.surface_alt,
    )
    style.configure(
        "Horizontal.TScrollbar",
        background=palette.surface_alt,
        troughcolor=palette.canvas,
        bordercolor=palette.canvas,
        arrowcolor=palette.text_muted,
        lightcolor=palette.surface_alt,
        darkcolor=palette.surface_alt,
    )
    return style


def _configure_option_db(root: tk.Misc, palette: Palette) -> None:
    """让“之后创建”的原生 tk 控件与下拉列表也用上新皮肤。"""
    options = (
        ("*Background", palette.canvas),
        ("*Foreground", palette.text),
        ("*Toplevel.background", palette.canvas),
        ("*Menu.background", palette.surface),
        ("*Menu.foreground", palette.text),
        ("*Menu.activeBackground", palette.accent),
        ("*Menu.activeForeground", palette.accent_text),
        ("*TCombobox*Listbox.background", palette.surface),
        ("*TCombobox*Listbox.foreground", palette.text),
        ("*TCombobox*Listbox.selectBackground", palette.accent),
        ("*TCombobox*Listbox.selectForeground", palette.accent_text),
    )
    for pattern, color in options:
        try:
            root.option_add(pattern, color)
        except tk.TclError:  # pragma: no cover
            logger.debug("设置选项数据库失败：%s", pattern, exc_info=True)


# ----------------------------------------------------------------------
# 自绘小工具
# ----------------------------------------------------------------------


def rounded_rectangle(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    *,
    steps: int = 6,
    **kwargs: Any,
) -> int:
    """在画布上画一个圆角矩形，返回 item id。

    用四段圆弧采样成多边形（而不是 ``smooth=True`` 的简陋多边形：在高度很小时
    后者会退化成“透镜形”，开关这种细长控件会明显走样）。
    """
    r = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points: list[float] = []
    corners = (
        (x1 + r, y1 + r, 180.0, 270.0),  # 左上
        (x2 - r, y1 + r, 270.0, 360.0),  # 右上
        (x2 - r, y2 - r, 0.0, 90.0),  # 右下
        (x1 + r, y2 - r, 90.0, 180.0),  # 左下
    )
    for cx, cy, start, end in corners:
        for index in range(steps + 1):
            angle = math.radians(start + (end - start) * index / steps)
            points.append(cx + r * math.cos(angle))
            points.append(cy + r * math.sin(angle))
    return canvas.create_polygon(points, **kwargs)


class ThemedFrame(tk.Frame):
    """颜色取自 :class:`Palette` 字段名的容器（支持在线换肤）。"""

    def __init__(
        self, parent: tk.Misc, palette: Palette, *, bg: str = "canvas", **kwargs: object
    ) -> None:
        super().__init__(parent, bd=0, highlightthickness=0, **kwargs)  # type: ignore[arg-type]
        self._bg_key = bg
        self.apply_palette(palette)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.configure(bg=getattr(palette, self._bg_key))

    def set_color(self, *, bg: str | None = None) -> None:
        if bg is not None:
            self._bg_key = bg
        self.apply_palette(self._palette)


class Card(tk.Frame):
    """圆角卡片：``Canvas`` 画底，``body`` 放内容。

    :param fill: :class:`Palette` 中的颜色字段名（如 ``"surface"`` / ``"danger_soft"``）。
    :param stretch: ``True`` 时宽度跟随父容器（``fill="x"``），高度自适应内容；
        否则宽高都自适应内容。
    """

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        *,
        radius: int = 10,
        padding: int = 12,
        fill: str = "surface",
        stretch: bool = False,
        outline: bool = True,
    ) -> None:
        super().__init__(parent, bg=palette.canvas, bd=0, highlightthickness=0)
        self._palette = palette
        self._radius = radius
        self._padding = padding
        self._fill_key = fill
        self._stretch = stretch
        self._outline = outline

        self._canvas = tk.Canvas(self, bg=palette.canvas, highlightthickness=0, bd=0)
        self._canvas.pack(fill="both", expand=True)
        self.body = ThemedFrame(self._canvas, palette, bg=fill)
        self._item = self._canvas.create_window(padding, padding, anchor="nw", window=self.body)

        self.body.bind("<Configure>", self._on_body_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

    # -- 内部 --

    @property
    def palette(self) -> Palette:
        return self._palette

    def _fill_color(self) -> str:
        return getattr(self._palette, self._fill_key)

    def _on_body_configure(self, event: tk.Event) -> None:
        if self._stretch:
            needed = self.body.winfo_reqheight() + 2 * self._padding
            if self._canvas.winfo_height() != needed:
                self._canvas.configure(height=needed)
        else:
            self._canvas.configure(
                width=event.width + 2 * self._padding,
                height=event.height + 2 * self._padding,
            )
        self._redraw()

    def _on_canvas_configure(self, event: tk.Event) -> None:
        if self._stretch:
            self._canvas.itemconfigure(self._item, width=max(1, event.width - 2 * self._padding))
        self._redraw()

    def _redraw(self) -> None:
        self._canvas.delete("card-bg")
        width = self._canvas.winfo_width()
        height = self._canvas.winfo_height()
        if width <= 2 or height <= 2:
            return
        rounded_rectangle(
            self._canvas,
            1,
            1,
            width - 1,
            height - 1,
            self._radius,
            fill=self._fill_color(),
            outline=self._palette.border if self._outline else self._fill_color(),
            width=1,
            tags="card-bg",
        )
        self._canvas.tag_lower("card-bg")

    # -- 换肤 --

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.configure(bg=palette.canvas)
        self._canvas.configure(bg=palette.canvas)
        self.body.set_color(bg=self._fill_key)
        self._redraw()

    # -- 尺寸 --

    def sync_size(self) -> None:
        """按当前内容强制同步一次请求尺寸（不依赖 ``<Configure>`` 事件）。

        ``tk.Canvas`` 的默认请求尺寸很大（378x265），卡片原本靠 ``<Configure>`` 回调
        才能收敛；在窗口**映射之前**调用本方法即可拿到真实请求尺寸，供“右下角落位”
        这类需要在显示前就知道窗口大小的场景使用。

        调用前请先 ``update_idletasks()``：``body.winfo_reqheight()`` 要布局管理器
        算过一次才准。
        """
        padding = 2 * self._padding
        if self._stretch:
            self._canvas.configure(height=self.body.winfo_reqheight() + padding)
        else:
            self._canvas.configure(
                width=self.body.winfo_reqwidth() + padding,
                height=self.body.winfo_reqheight() + padding,
            )


def sync_cards(widget: tk.Misc) -> int:
    """递归让 ``widget`` 下所有 :class:`Card` 同步一次尺寸，返回处理数量。

    调用前请先 ``widget.update_idletasks()``（见 :meth:`Card.sync_size`）。
    """
    count = 0
    for child in widget.winfo_children():
        if isinstance(child, Card):
            child.sync_size()
            count += 1
        count += sync_cards(child)
    return count


class ProgressBar(tk.Canvas):
    """细长圆角进度条（用于“距阈值”进度）。"""

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        *,
        height: int = 6,
        track: str = "hero_track",
        bar: str = "hero_bar",
        bg: str = "hero",
        width: int = 220,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            highlightthickness=0,
            bd=0,
            bg=getattr(palette, bg),
        )
        self._palette = palette
        self._height = height
        self._track_key = track
        self._bar_key = bar
        self._bg_key = bg
        self._value = 0.0
        self.bind("<Configure>", lambda _event: self._redraw())

    def set(self, ratio: float) -> None:
        """设置进度（0.0 ~ 1.0，超出范围自动截断）。"""
        clamped = max(0.0, min(1.0, float(ratio)))
        if abs(clamped - self._value) > 1e-6:
            self._value = clamped
            self._redraw()

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.configure(bg=getattr(palette, self._bg_key))
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width()
        height = self._height
        if width <= 2:
            return
        radius = height / 2
        rounded_rectangle(
            self,
            0,
            0,
            width,
            height,
            radius,
            fill=getattr(self._palette, self._track_key),
            tags="track",
        )
        filled = width * self._value
        if filled >= 1:
            rounded_rectangle(
                self,
                0,
                0,
                max(filled, radius * 2),
                height,
                radius,
                fill=getattr(self._palette, self._bar_key),
                tags="bar",
            )


class HeroCard(Card):
    """顶部“英雄卡”：状态徽标 + 大字号项目数 + 距阈值进度条。

    使用强调色实底（tkinter 无法方便地做渐变色标签背景），内部子控件全部用
    同一个底色，视觉上是一整块卡片。
    """

    def __init__(self, parent: tk.Misc, palette: Palette, *, padding: int = 14) -> None:
        super().__init__(parent, palette, radius=10, padding=padding, fill="hero", stretch=True)
        family = default_font_family(self)
        self.body.columnconfigure(0, weight=1)

        self.state_label = ThemedLabel(
            self.body,
            palette,
            bg="hero_pill",
            fg="hero_pill_text",
            text="",
            font=(family, 9),
            anchor="w",
            padx=10,
            pady=3,
        )
        self.state_label.grid(row=0, column=0, sticky="w")

        self.count_label = ThemedLabel(
            self.body,
            palette,
            bg="hero",
            fg="hero_text",
            text="—",
            font=(family, 24, "bold"),
            anchor="w",
        )
        self.count_label.grid(row=1, column=0, sticky="w", pady=(10, 0))

        self.caption_label = ThemedLabel(
            self.body,
            palette,
            bg="hero",
            fg="hero_sub",
            text="",
            font=(family, 9),
            anchor="w",
        )
        self.caption_label.grid(row=2, column=0, sticky="w", pady=(2, 0))

        self.progress = ProgressBar(self.body, palette, height=6, width=200)
        self.progress.grid(row=3, column=0, sticky="ew", pady=(10, 0))

    def set_values(self, state_text: str, count_text: str, caption: str, ratio: float) -> None:
        """刷新英雄卡内容。"""
        self.state_label.configure(text=state_text)
        self.count_label.configure(text=count_text)
        self.caption_label.configure(text=caption)
        self.progress.set(ratio)


class ThemedLabel(tk.Label):
    """颜色取自 :class:`Palette` 字段名的标签（支持在线换肤）。

    :param bg: 背景色角色名，如 ``"canvas"`` / ``"surface"`` / ``"hero"``。
    :param fg: 前景色角色名，如 ``"text"`` / ``"text_muted"``。
    """

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        *,
        bg: str = "canvas",
        fg: str = "text",
        **kwargs: object,
    ) -> None:
        super().__init__(parent, bd=0, highlightthickness=0, **kwargs)  # type: ignore[arg-type]
        self._bg_key = bg
        self._fg_key = fg
        self.apply_palette(palette)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.configure(bg=getattr(palette, self._bg_key), fg=getattr(palette, self._fg_key))

    def set_color(self, *, bg: str | None = None, fg: str | None = None) -> None:
        """就地改变颜色角色（如把状态值标成绿色 / 红色）。"""
        if bg is not None:
            self._bg_key = bg
        if fg is not None:
            self._fg_key = fg
        self.apply_palette(self._palette)


class Switch(tk.Canvas):
    """开关（替代 ``Checkbutton``），绑定一个 ``BooleanVar``。"""

    WIDTH = 38
    HEIGHT = 20

    def __init__(
        self,
        parent: tk.Misc,
        variable: tk.BooleanVar,
        palette: Palette,
        *,
        command: Callable[[], None] | None = None,
        bg: str = "surface",
    ) -> None:
        super().__init__(
            parent,
            width=self.WIDTH,
            height=self.HEIGHT,
            highlightthickness=0,
            bd=0,
            bg=getattr(palette, bg),
            cursor="hand2",
        )
        self._variable = variable
        self._command = command
        self._palette = palette
        self._bg_key = bg
        self.bind("<Button-1>", self._on_click)
        self._trace = variable.trace_add("write", lambda *_: self._redraw())
        self._redraw()

    def _on_click(self, _event: tk.Event | None = None) -> None:
        self._variable.set(not bool(self._variable.get()))
        if self._command is not None:
            self._command()

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.configure(bg=getattr(palette, self._bg_key))
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        palette = self._palette
        on = bool(self._variable.get())
        track = palette.accent if on else palette.field_border
        rounded_rectangle(
            self, 1, 1, self.WIDTH - 1, self.HEIGHT - 1, (self.HEIGHT - 2) / 2, fill=track
        )
        knob = self.HEIGHT - 6
        left = self.WIDTH - knob - 3 if on else 3
        self.create_oval(left, 3, left + knob, 3 + knob, fill="#FFFFFF", outline="#FFFFFF")


class CountdownRing(tk.Canvas):
    """环形倒计时（中间显示剩余秒数）。"""

    def __init__(
        self,
        parent: tk.Misc,
        palette: Palette,
        *,
        size: int = 68,
        thickness: int = 6,
        bg: str = "surface",
    ) -> None:
        super().__init__(
            parent,
            width=size,
            height=size,
            highlightthickness=0,
            bd=0,
            bg=getattr(palette, bg),
        )
        self._palette = palette
        self._size = size
        self._thickness = thickness
        self._bg_key = bg
        self._seconds: int | None = None
        self._total: int | None = None

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        self.configure(bg=getattr(palette, self._bg_key))
        self._redraw()

    def set_seconds(self, seconds: int | None, total: int | None = None) -> None:
        """设置剩余秒数；``None`` 表示不自动倒计时（画成空心等待态）。"""
        self._seconds = seconds
        self._total = total if total else None
        self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        palette = self._palette
        size = self._size
        pad = self._thickness / 2 + 1
        box = (pad, pad, size - pad, size - pad)

        self.create_oval(*box, outline=palette.surface_alt, width=self._thickness)
        seconds = self._seconds
        if seconds is not None and self._total:
            ratio = max(0.0, min(1.0, seconds / self._total))
            if ratio > 0:
                self.create_arc(
                    *box,
                    start=90,
                    extent=-359.9 * ratio,
                    style="arc",
                    outline=palette.accent,
                    width=self._thickness,
                )
            text = f"{seconds}"
        else:
            text = "等"
        self.create_text(
            size / 2,
            size / 2,
            text=text,
            fill=palette.accent if seconds is not None else palette.text_muted,
            font=(default_font_family(self), 14, "bold"),
        )


class Badge(tk.Label):
    """彩色小标签（成功 / 失败 / 中性）。"""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        palette: Palette,
        *,
        tone: str = "neutral",
    ) -> None:
        self._tone = tone
        super().__init__(parent, text=text, bd=0, highlightthickness=0, padx=8, pady=1)
        self.apply_palette(palette)

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.apply_palette(self._palette)

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        background, foreground = _tone_colors(palette, self._tone)
        self.configure(bg=background, fg=foreground)


def _tone_colors(palette: Palette, tone: str) -> tuple[str, str]:
    if tone == "ok":
        return palette.ok_soft, palette.ok_text
    if tone == "warn":
        return palette.warn_soft, palette.warn_text
    if tone == "danger":
        return palette.danger_soft, palette.danger_text
    return palette.neutral_soft, palette.neutral_text


def retheme_tree(widget: tk.Misc, palette: Palette) -> None:
    """递归换肤：每个控件自己实现 ``apply_palette``（``ThemedFrame`` /
    ``ThemedLabel`` / ``Card`` / ``Switch`` 等），本函数只负责遍历。"""
    for child in widget.winfo_children():
        hook = getattr(child, "apply_palette", None)
        if callable(hook):
            try:
                hook(palette)
            except Exception:  # 单个控件换肤失败不影响其它控件
                logger.exception("控件换肤失败：%s", type(child).__name__)
        retheme_tree(child, palette)


# ----------------------------------------------------------------------
# 主题管理器
# ----------------------------------------------------------------------


class ThemeManager:
    """当前皮肤与切换逻辑；负责把配色应用到 Tk 根窗口与注册过的窗口。"""

    def __init__(
        self,
        root: tk.Tk | tk.Toplevel,
        *,
        mode: str = THEME_AUTO,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = root
        self._clock = clock or datetime.now
        self._mode = normalize_mode(mode)
        self._resolved = resolve_theme(self._mode, self._clock())
        self._font_family = pick_font_family(root)
        self._style: ttk.Style | None = None
        self._listeners: list[Callable[[Palette], None]] = []
        self._listener_ids: dict[int, Callable[[Palette], None]] = {}
        self._next_listener_id = 0
        self.apply()

    # -- 只读状态 --

    @property
    def mode(self) -> str:
        """配置里的皮肤模式（``auto`` / ``light`` / ``dark``）。"""
        return self._mode

    @property
    def resolved(self) -> str:
        """当前实际皮肤名（``light`` / ``dark``）。"""
        return self._resolved

    @property
    def palette(self) -> Palette:
        return PALETTES[self._resolved]

    @property
    def style(self) -> ttk.Style | None:
        return self._style

    @property
    def font_family(self) -> str:
        return self._font_family

    def label(self) -> str:
        """给按钮 / 菜单项用的文案。"""
        return mode_label(self._mode, self._resolved)

    # -- 应用 / 切换 --

    def resolve_now(self) -> str:
        """按当前模式与时间重新判定皮肤名（不改动界面）。"""
        return resolve_theme(self._mode, self._clock())

    def apply(self) -> bool:
        """把当前皮肤写入 Tk（样式 + 选项数据库 + 已注册窗口）。返回皮肤是否变化。"""
        resolved = self.resolve_now()
        changed = resolved != self._resolved or self._style is None
        self._resolved = resolved
        palette = self.palette

        try:
            self._root.configure(bg=palette.canvas)
        except tk.TclError:  # pragma: no cover - 根窗口可能已销毁
            return changed
        self._style = configure_styles(self._root, palette, font_family=self._font_family)
        _configure_option_db(self._root, palette)

        for listener in list(self._listeners):
            try:
                listener(palette)
            except Exception:  # 单个窗口换肤失败不影响其它窗口
                logger.exception("窗口换肤失败")
        return changed

    def set_mode(self, mode: str) -> bool:
        """切换模式并立即应用；返回实际皮肤是否发生变化。"""
        self._mode = normalize_mode(mode)
        return self.apply()

    def cycle(self) -> str:
        """按 ``auto -> light -> dark`` 循环，返回切换后的模式。"""
        self.set_mode(next_mode(self._mode))
        return self._mode

    def refresh(self) -> bool:
        """自动模式的周期性检查：跨过白天 / 夜间分界时才重新应用。"""
        if self.resolve_now() == self._resolved:
            return False
        return self.apply()

    # -- 订阅 --

    def register(self, listener: Callable[[Palette], None]) -> Callable[[], None]:
        """注册换肤回调（对话框窗口），返回注销函数。"""
        self._next_listener_id += 1
        key = self._next_listener_id
        self._listener_ids[key] = listener
        self._listeners.append(listener)
        try:
            listener(self.palette)  # 立即套用当前皮肤
        except Exception:  # 单个窗口换肤失败不影响其它窗口
            logger.exception("窗口换肤失败")

        def unregister() -> None:
            self._listener_ids.pop(key, None)
            try:
                self._listeners.remove(listener)
            except ValueError:  # pragma: no cover - 已注销
                pass

        return unregister


__all__ = [
    "BASE_TTK_THEME",
    "Badge",
    "Card",
    "CountdownRing",
    "DARK",
    "HeroCard",
    "LIGHT",
    "PALETTES",
    "PREFERRED_FONTS",
    "Palette",
    "ProgressBar",
    "Switch",
    "ThemeManager",
    "ThemedFrame",
    "ThemedLabel",
    "configure_styles",
    "default_font_family",
    "pick_font_family",
    "retheme_tree",
    "rounded_rectangle",
    "sync_cards",
]

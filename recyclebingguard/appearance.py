"""界面皮肤的取值与“跟随系统时间”判定（**纯逻辑，不导入 tkinter**）。

拆成独立模块的原因：

- :mod:`recyclebingguard.config` 需要校验 ``theme`` 字段的合法取值；
- :mod:`recyclebingguard.app` 需要在不引入任何 GUI 依赖的前提下切换皮肤
  （控制器必须能在无图形环境下单元测试）；
- 真正的配色与控件在 :mod:`recyclebingguard.theme` 中实现。

皮肤模式：

- ``auto``：按系统时间自动选择 —— 白天（``DAY_START_HOUR``～``NIGHT_START_HOUR``
  之间）用浅色，夜间用深色；
- ``light`` / ``dark``：用户手动固定。
"""

from __future__ import annotations

from datetime import datetime

#: 跟随系统时间。
THEME_AUTO = "auto"
#: 固定浅色。
THEME_LIGHT = "light"
#: 固定深色。
THEME_DARK = "dark"

#: 合法取值（顺序即切换按钮的循环顺序）。
THEME_MODES: tuple[str, ...] = (THEME_AUTO, THEME_LIGHT, THEME_DARK)

#: 模式 -> 界面文案。
THEME_LABELS: dict[str, str] = {
    THEME_AUTO: "跟随系统时间",
    THEME_LIGHT: "浅色",
    THEME_DARK: "深色",
}

#: 名称 -> 界面文案（自动模式的“当前实际皮肤”）。
APPEARANCE_LABELS: dict[str, str] = {THEME_LIGHT: "浅色", THEME_DARK: "深色"}

#: 自动模式下：>= 该小时算白天（浅色）。
DAY_START_HOUR = 7

#: 自动模式下：>= 该小时算夜间（深色）。
NIGHT_START_HOUR = 19

#: 自动模式跨过分界后重新判定皮肤的最小间隔（秒）；供 UI 定时器使用。
AUTO_RECHECK_SECONDS = 30


def normalize_mode(mode: object) -> str:
    """把任意输入收敛成合法模式；非法值一律退回 ``auto``。"""
    if isinstance(mode, str):
        candidate = mode.strip().lower()
        if candidate in THEME_MODES:
            return candidate
    return THEME_AUTO


def resolve_theme(mode: object, now: datetime | None = None) -> str:
    """把模式解析成实际皮肤名（``light`` / ``dark``）。

    :param mode: ``auto`` / ``light`` / ``dark``（非法值按 ``auto`` 处理）。
    :param now: 判定用时间，默认取当前本地时间（测试可注入）。
    """
    normalized = normalize_mode(mode)
    if normalized != THEME_AUTO:
        return normalized
    moment = now if now is not None else datetime.now()
    daytime = DAY_START_HOUR <= moment.hour < NIGHT_START_HOUR
    return THEME_LIGHT if daytime else THEME_DARK


def mode_label(mode: object, resolved: str | None = None) -> str:
    """给按钮 / 菜单用的文案，例如 ``跟随系统时间（当前浅色）``。"""
    normalized = normalize_mode(mode)
    base = THEME_LABELS[normalized]
    if normalized != THEME_AUTO:
        return base
    appearance = APPEARANCE_LABELS.get(normalize_mode(resolved or ""), "")
    return f"{base}（当前{appearance}）" if appearance else base


def next_mode(mode: object) -> str:
    """返回循环切换的下一个模式。"""
    normalized = normalize_mode(mode)
    index = THEME_MODES.index(normalized)
    return THEME_MODES[(index + 1) % len(THEME_MODES)]


__all__ = [
    "APPEARANCE_LABELS",
    "AUTO_RECHECK_SECONDS",
    "DAY_START_HOUR",
    "NIGHT_START_HOUR",
    "THEME_AUTO",
    "THEME_DARK",
    "THEME_LABELS",
    "THEME_LIGHT",
    "THEME_MODES",
    "mode_label",
    "next_mode",
    "normalize_mode",
    "resolve_theme",
]

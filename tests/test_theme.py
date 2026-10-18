"""皮肤（主题）模块测试：时间判定、配置校验、样式与自绘控件。

- 纯逻辑部分（:mod:`recyclebingguard.appearance`）无条件执行；
- 需要 Tk 的控件与样式测试在无图形环境时整体跳过。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from recyclebingguard import theme as theme_module
from recyclebingguard.app import AppController
from recyclebingguard.appearance import (
    DAY_START_HOUR,
    NIGHT_START_HOUR,
    THEME_AUTO,
    THEME_DARK,
    THEME_LIGHT,
    THEME_MODES,
    mode_label,
    next_mode,
    normalize_mode,
    resolve_theme,
)
from recyclebingguard.config import Config, ConfigError, load_config, save_config
from recyclebingguard.interfaces import CleanOutcome, CleanResult, ScanResult
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor
from recyclebingguard.theme import PALETTES, ThemeManager

# --------------------------------------------------------------------------
# 纯逻辑：模式与时间判定
# --------------------------------------------------------------------------


def test_resolve_theme_explicit_modes() -> None:
    # 显式指定的模式与时间无关
    assert resolve_theme(THEME_LIGHT, datetime(2026, 10, 18, 23, 0)) == THEME_LIGHT
    assert resolve_theme(THEME_DARK, datetime(2026, 10, 18, 10, 0)) == THEME_DARK


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (0, THEME_DARK),
        (6, THEME_DARK),
        (DAY_START_HOUR, THEME_LIGHT),
        (12, THEME_LIGHT),
        (NIGHT_START_HOUR - 1, THEME_LIGHT),
        (NIGHT_START_HOUR, THEME_DARK),
        (23, THEME_DARK),
    ],
)
def test_resolve_theme_auto_follows_clock(hour: int, expected: str) -> None:
    moment = datetime(2026, 10, 18, hour, 30)
    assert resolve_theme(THEME_AUTO, moment) == expected


def test_normalize_mode_falls_back_to_auto() -> None:
    assert normalize_mode("DARK") == THEME_DARK
    assert normalize_mode(" light ") == THEME_LIGHT
    assert normalize_mode("neon") == THEME_AUTO
    assert normalize_mode(None) == THEME_AUTO


def test_next_mode_cycles_through_all_modes() -> None:
    order = [THEME_AUTO]
    for _ in range(len(THEME_MODES) - 1):
        order.append(next_mode(order[-1]))
    assert order == list(THEME_MODES)
    assert next_mode(order[-1]) == THEME_AUTO  # 回到起点


def test_mode_label_mentions_current_look_in_auto_mode() -> None:
    assert mode_label(THEME_AUTO, THEME_LIGHT) == "跟随系统时间（当前浅色）"
    assert mode_label(THEME_AUTO, THEME_DARK) == "跟随系统时间（当前深色）"
    assert mode_label(THEME_LIGHT, THEME_LIGHT) == "浅色"


# --------------------------------------------------------------------------
# 配置：theme 字段
# --------------------------------------------------------------------------


def test_theme_defaults_to_auto() -> None:
    assert Config().theme == THEME_AUTO


def test_theme_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    save_config(Config(theme=THEME_DARK), target)
    assert load_config(target).theme == THEME_DARK


@pytest.mark.parametrize("value", ["auto", "LIGHT", " dark "])
def test_theme_accepts_known_values(value: str) -> None:
    assert Config.from_dict({"theme": value}).theme in THEME_MODES


@pytest.mark.parametrize("value", ["neon", "", 1, None, True])
def test_theme_rejects_unknown_values(value: object) -> None:
    with pytest.raises(ConfigError, match="theme"):
        Config.from_dict({"theme": value})


def test_hand_built_invalid_theme_is_rejected() -> None:
    config = Config()
    config.theme = "neon"
    with pytest.raises(ConfigError, match="theme"):
        config.validate()


def test_config_example_contains_theme() -> None:
    example = Path(__file__).resolve().parents[1] / "config.example.json"
    assert '"theme": "auto"' in example.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 控制器：切换皮肤并持久化
# --------------------------------------------------------------------------


class FakeService:
    def __init__(self) -> None:
        self.result = ScanResult(total_items=0, total_size_bytes=0, scanned_roots=("C:\\",))

    def scan(self) -> ScanResult:
        return self.result

    def empty(self, *, verified_scan: ScanResult | None = None) -> CleanResult:
        return CleanResult(outcome=CleanOutcome.SUCCESS)


@pytest.fixture
def controller(tmp_path: Path):
    config = Config()
    ctl = AppController(
        config,
        service=FakeService(),  # type: ignore[arg-type]
        monitor=RecycleBinMonitor(FakeService(), config),
        config_path=tmp_path / "config.json",
        log_path=tmp_path / "recyclebingguard.log",
    )
    yield ctl
    ctl.shutdown()


def test_set_theme_persists(controller: AppController) -> None:
    assert controller.set_theme(THEME_DARK) == THEME_DARK
    assert controller.config.theme == THEME_DARK
    assert '"theme": "dark"' in controller.config_path.read_text(encoding="utf-8")


def test_set_theme_rejects_unknown_mode(controller: AppController) -> None:
    with pytest.raises(ConfigError):
        controller.set_theme("neon")
    assert controller.config.theme == THEME_AUTO
    assert not controller.config_path.exists()  # 失败时不落盘


def test_cycle_theme_order(controller: AppController) -> None:
    assert controller.cycle_theme() == THEME_LIGHT
    assert controller.cycle_theme() == THEME_DARK
    assert controller.cycle_theme() == THEME_AUTO


# --------------------------------------------------------------------------
# 配色与 ttk 样式（需要 Tk）
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root():
    """整个模块共用一个 Tk 根窗口。

    同一进程里反复 ``Tk()`` / ``destroy()`` 会让 Tcl 解释器初始化偶发失败
    （``Can't find a usable init.tcl``），因此这里**只创建一次**；各用例内部
    自行销毁自己创建的控件。
    """
    tk = pytest.importorskip("tkinter")
    try:
        window = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"无可用图形环境：{exc}")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:  # pragma: no cover
        pass


def test_palettes_have_all_fields_and_differ() -> None:
    light, dark = PALETTES[THEME_LIGHT], PALETTES[THEME_DARK]
    assert light.canvas != dark.canvas
    assert light.text != dark.text
    for palette in (light, dark):
        for field in theme_module.Palette.__dataclass_fields__:
            value = getattr(palette, field)
            assert value, f"{palette.name}.{field} 不应为空"


def test_manager_applies_styles_and_follows_clock(root) -> None:
    manager = ThemeManager(root, mode=THEME_AUTO, clock=lambda: datetime(2026, 10, 18, 10, 0))
    assert manager.resolved == THEME_LIGHT
    style = manager.style
    assert style is not None
    assert style.lookup("TLabel", "background") == manager.palette.canvas
    assert style.lookup("Accent.TButton", "background") == manager.palette.accent
    assert style.lookup("Danger.TButton", "background") == manager.palette.danger

    # 时间跨过夜间分界：refresh 会自动换成深色
    night = datetime(2026, 10, 18, 22, 0)
    manager._clock = lambda: night  # noqa: SLF001 - 通过注入时间模拟跨天
    assert manager.refresh() is True
    assert manager.resolved == THEME_DARK
    assert style.lookup("TLabel", "background") == manager.palette.canvas


def test_manager_set_mode_and_label(root) -> None:
    manager = ThemeManager(root, mode=THEME_AUTO, clock=lambda: datetime(2026, 10, 18, 10, 0))
    assert manager.label() == "跟随系统时间（当前浅色）"
    manager.set_mode(THEME_DARK)
    assert manager.resolved == THEME_DARK
    assert manager.label() == "深色"

    assert manager.cycle() == THEME_AUTO  # auto -> light -> dark -> auto 的推进
    assert manager.cycle() == THEME_LIGHT


def test_manager_notifies_registered_listeners(root) -> None:
    manager = ThemeManager(root, mode=THEME_LIGHT)
    received: list[str] = []
    unregister = manager.register(lambda palette: received.append(palette.name))
    assert received == [THEME_LIGHT]  # 注册时立即套用一次

    manager.set_mode(THEME_DARK)
    assert received == [THEME_LIGHT, THEME_DARK]

    unregister()
    manager.set_mode(THEME_LIGHT)
    assert received == [THEME_LIGHT, THEME_DARK]  # 注销后不再收到


def test_listener_failure_does_not_break_theme_switch(root) -> None:
    manager = ThemeManager(root, mode=THEME_LIGHT)
    received: list[str] = []

    def boom(_palette) -> None:
        raise RuntimeError("boom")

    manager.register(boom)
    manager.register(lambda palette: received.append(palette.name))
    manager.set_mode(THEME_DARK)
    assert received == [THEME_LIGHT, THEME_DARK]


# --------------------------------------------------------------------------
# 自绘控件（需要 Tk）
# --------------------------------------------------------------------------


def test_switch_binds_boolean_variable(root) -> None:
    import tkinter as tk

    manager = ThemeManager(root, mode=THEME_LIGHT)
    variable = tk.BooleanVar(value=False)
    calls: list[bool] = []
    widget = theme_module.Switch(
        root, variable, manager.palette, command=lambda: calls.append(bool(variable.get()))
    )
    try:
        widget._on_click()  # noqa: SLF001 - 模拟点击（不依赖真实事件循环）
        assert variable.get() is True
        assert calls == [True]
        widget._on_click()  # noqa: SLF001
        assert variable.get() is False
        assert calls == [True, False]
    finally:
        widget.destroy()


def test_switch_recolors_on_theme_change(root) -> None:
    import tkinter as tk

    manager = ThemeManager(root, mode=THEME_LIGHT)
    variable = tk.BooleanVar(value=True)
    widget = theme_module.Switch(root, variable, manager.palette)
    try:
        before = widget.cget("bg")
        widget.apply_palette(PALETTES[THEME_DARK])
        assert widget.cget("bg") == PALETTES[THEME_DARK].surface
        assert before != widget.cget("bg")
    finally:
        widget.destroy()


def test_hero_card_shows_values_and_recolors(root) -> None:
    manager = ThemeManager(root, mode=THEME_LIGHT)
    hero = theme_module.HeroCard(root, manager.palette)
    try:
        hero.set_values("正在监控", "1,372", "回收站项目总数 / 触发阈值 100", 0.137)
        assert hero.count_label.cget("text") == "1,372"
        assert hero.state_label.cget("text") == "正在监控"
        assert "触发阈值" in hero.caption_label.cget("text")

        hero.apply_palette(PALETTES[THEME_DARK])
        assert hero.count_label.cget("fg") == PALETTES[THEME_DARK].hero_text
    finally:
        hero.destroy()


def test_countdown_ring_handles_waiting_state(root) -> None:
    manager = ThemeManager(root, mode=THEME_LIGHT)
    ring = theme_module.CountdownRing(root, manager.palette)
    try:
        ring.set_seconds(10, 10)
        assert ring.find_all()  # 画了圆环与数字
        ring.set_seconds(None, None)
        assert ring.find_all()  # 等待态同样有内容，不会空白
    finally:
        ring.destroy()


def test_retheme_tree_updates_themed_widgets(root) -> None:
    manager = ThemeManager(root, mode=THEME_LIGHT)
    frame = theme_module.ThemedFrame(root, manager.palette)
    label = theme_module.ThemedLabel(frame, manager.palette, text="hi", bg="surface", fg="text")
    try:
        assert frame.cget("bg") == PALETTES[THEME_LIGHT].canvas
        theme_module.retheme_tree(root, PALETTES[THEME_DARK])
        assert frame.cget("bg") == PALETTES[THEME_DARK].canvas
        assert label.cget("bg") == PALETTES[THEME_DARK].surface
        assert label.cget("fg") == PALETTES[THEME_DARK].text
    finally:
        frame.destroy()


def test_rounded_rectangle_is_a_polygon(root) -> None:
    import tkinter as tk

    canvas = tk.Canvas(root)
    try:
        item = theme_module.rounded_rectangle(canvas, 0, 0, 36, 18, 9, fill="#000")
        assert canvas.type(item) == "polygon"
        coords = canvas.coords(item)
        assert len(coords) >= 20  # 四角都是圆弧采样，不会退化成细长菱形
    finally:
        canvas.destroy()


# --------------------------------------------------------------------------
# 对话框跟随皮肤（设置 / 清理确认）
# --------------------------------------------------------------------------


def test_settings_dialog_collects_and_persists_theme(controller: AppController, root) -> None:
    from recyclebingguard.settings_dialog import SettingsDialog

    manager = ThemeManager(root, mode=THEME_LIGHT)
    dialog = SettingsDialog(root, controller, theme=manager)
    try:
        assert dialog.collect_settings()["theme"] == THEME_AUTO

        dialog._var_theme.set("深色")  # noqa: SLF001 - 模拟用户在下拉框里的选择
        dialog.save()  # 成功路径不会弹窗
        assert controller.config.theme == THEME_DARK
        assert '"theme": "dark"' in controller.config_path.read_text(encoding="utf-8")
    finally:
        dialog.close()


def test_settings_dialog_follows_online_theme_switch(controller: AppController, root) -> None:
    from recyclebingguard.settings_dialog import SettingsDialog

    manager = ThemeManager(root, mode=THEME_LIGHT)
    dialog = SettingsDialog(root, controller, theme=manager)
    try:
        assert dialog.window.cget("bg") == PALETTES[THEME_LIGHT].canvas
        manager.set_mode(THEME_DARK)
        assert dialog.window.cget("bg") == PALETTES[THEME_DARK].canvas
        manager.set_mode(THEME_LIGHT)
        assert dialog.window.cget("bg") == PALETTES[THEME_LIGHT].canvas
    finally:
        dialog.close()


def test_confirm_dialog_follows_online_theme_switch(root) -> None:
    from recyclebingguard.confirm_dialog import CleanConfirmDialog
    from recyclebingguard.confirmation import CleanConfirmationRequest

    manager = ThemeManager(root, mode=THEME_LIGHT)
    request = CleanConfirmationRequest(
        token=1, total_items=12345, threshold_items=10000, timeout_seconds=10
    )
    decisions: list[tuple[int, object]] = []
    dialog = CleanConfirmDialog(
        root,
        request,
        lambda token, decision: decisions.append((token, decision)),
        theme=manager,
        animate=False,
    )
    try:
        assert dialog.window.cget("bg") == PALETTES[THEME_LIGHT].canvas
        manager.set_mode(THEME_DARK)
        assert dialog.window.cget("bg") == PALETTES[THEME_DARK].canvas
        assert dialog.window.title() == "RecycleBinGuard — 回收站清理确认"
    finally:
        dialog.abort("测试结束")


def test_settings_dialog_works_without_theme_manager(controller: AppController, root) -> None:
    """不传皮肤管理器时退回默认浅色，接口保持兼容。"""
    from recyclebingguard.settings_dialog import SettingsDialog

    dialog = SettingsDialog(root, controller)
    try:
        assert dialog.window.cget("bg") == PALETTES[THEME_LIGHT].canvas
        assert dialog._var_theme.get() == "跟随系统时间"  # noqa: SLF001
    finally:
        dialog.close()

"""T3 托盘/状态窗口测试。

不启动事件循环、不显示窗口（Tk 根窗口创建后立即 withdraw），
也不会产生真实托盘图标（pystray 的 ``Icon`` 只有 ``run()`` 之后才可见）。
无图形环境时整体跳过。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest

from recyclebingguard.app import AppController
from recyclebingguard.config import Config
from recyclebingguard.interfaces import CleanOutcome, CleanResult, ScanResult
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor
from recyclebingguard.tray import TrayApplication, create_icon_image


class FakeService:
    def __init__(self) -> None:
        self.result = ScanResult(total_items=8420, total_size_bytes=0, scanned_roots=("C:\\",))

    def scan(self) -> ScanResult:
        return self.result

    def empty(self) -> CleanResult:
        return CleanResult(outcome=CleanOutcome.SUCCESS)


def _make_controller(tmp_path: Path) -> AppController:
    config = Config()
    return AppController(
        config,
        service=FakeService(),  # type: ignore[arg-type]
        monitor=RecycleBinMonitor(FakeService(), config),
        config_path=tmp_path / "config.json",
        log_path=tmp_path / "recyclebingguard.log",
    )


@pytest.fixture
def controller(tmp_path: Path):
    ctl = _make_controller(tmp_path)
    yield ctl
    ctl.shutdown()


@pytest.fixture
def tray(controller: AppController, isolated_home: Path):
    try:
        app = TrayApplication(controller)
    except tk.TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"无可用图形环境：{exc}")
    yield app
    try:
        if app._root.winfo_exists():  # noqa: SLF001
            app._root.destroy()       # noqa: SLF001
    except Exception:
        pass


# --------------------------------------------------------------------------
# 图标
# --------------------------------------------------------------------------


def test_create_icon_image_shape() -> None:
    image = create_icon_image("#2E7D32", size=64)
    assert image.size == (64, 64)
    assert image.mode == "RGBA"
    # 中心应有不透明像素
    assert image.getpixel((32, 40))[3] > 0


def test_create_icon_image_colors_differ() -> None:
    green = list(create_icon_image("#2E7D32").getdata())
    red = list(create_icon_image("#C62828").getdata())
    assert green != red


# --------------------------------------------------------------------------
# 菜单结构
# --------------------------------------------------------------------------


def _menu_texts(app: TrayApplication) -> list[str]:
    menu = app._build_menu()  # noqa: SLF001
    texts = []
    for item in menu:
        if item is None:  # 分隔符在 pystray 中为 None
            continue
        if isinstance(item, str):  # pragma: no cover - 兼容不同版本
            continue
        texts.append(item.text)
    return texts


def test_menu_contains_required_items(tray: TrayApplication) -> None:
    texts = _menu_texts(tray)
    assert "打开状态窗口" in texts
    assert "立即检查" in texts
    assert "启用自动清理" in texts
    assert "查看最近清理日志" in texts
    assert "开机启动设置" in texts
    assert "退出程序" in texts


def test_menu_contains_open_hotkey_and_shortcut_items(tray: TrayApplication) -> None:
    texts = _menu_texts(tray)
    assert any(t.startswith("打开热键") for t in texts)
    assert any("快捷方式" in t for t in texts)


def test_hotkey_menu_item_reflects_config(tray: TrayApplication, controller: AppController) -> None:
    controller.config.hotkey_enabled = True
    menu = tray._build_menu()  # noqa: SLF001
    item = next(i for i in menu if i is not None and i.text.startswith("打开热键"))
    assert item.checked is True

    controller.config.hotkey_enabled = False
    menu = tray._build_menu()  # noqa: SLF001
    item = next(i for i in menu if i is not None and i.text.startswith("打开热键"))
    assert item.checked is False


def test_menu_default_item_opens_window(tray: TrayApplication) -> None:
    defaults = [item for item in tray._build_menu() if item is not None and item.default]  # noqa: SLF001
    assert [item.text for item in defaults] == ["打开状态窗口"]


def test_auto_clean_menu_text_toggles(tray: TrayApplication, controller: AppController) -> None:
    assert "启用自动清理" in _menu_texts(tray)
    controller.config.auto_clean_enabled = True
    assert "关闭自动清理" in _menu_texts(tray)


def test_autostart_menu_item_is_a_checkbox(tray: TrayApplication, controller: AppController) -> None:
    controller.is_start_on_login_enabled = lambda: True  # type: ignore[method-assign]
    menu = tray._build_menu()  # noqa: SLF001
    item = next(i for i in menu if i is not None and i.text == "开机启动设置")
    assert item.checked is True


def test_autostart_row_is_cached_not_polled(
    tray: TrayApplication, controller: AppController
) -> None:
    calls: list[int] = []

    def fake() -> bool:
        calls.append(1)
        return True

    controller.is_start_on_login_enabled = fake  # type: ignore[method-assign]

    tray._refresh()  # noqa: SLF001  第一次会查询
    assert calls == [1]
    assert "已启用" in tray._rows["autostart"].cget("text")  # noqa: SLF001

    for _ in range(5):
        tray._refresh()  # noqa: SLF001
    assert calls == [1]        # 后续刷新走缓存


# --------------------------------------------------------------------------
# 线程安全投递
# --------------------------------------------------------------------------


def test_menu_callbacks_are_marshalled_to_main_thread(tray: TrayApplication) -> None:
    import threading

    class Recorder:
        def __init__(self) -> None:
            self.threads: list[int] = []

        def __call__(self) -> None:
            self.threads.append(threading.get_ident())

    recorder = Recorder()
    tray._menu_check_now = lambda icon=None, item=None: tray._post(recorder)  # noqa: SLF001

    # 模拟托盘线程触发菜单
    worker = threading.Thread(target=lambda: tray._menu_check_now())  # noqa: SLF001
    worker.start()
    worker.join()

    assert recorder.threads == []          # 尚未在主线程执行
    tray._drain_ui_queue()                 # noqa: SLF001
    assert recorder.threads == [threading.get_ident()]


def test_ui_queue_isolates_failures(tray: TrayApplication) -> None:
    calls: list[str] = []

    def boom() -> None:
        raise RuntimeError("boom")

    tray._post(boom)                       # noqa: SLF001
    tray._post(lambda: calls.append("after"))  # noqa: SLF001
    tray._drain_ui_queue()                 # noqa: SLF001

    assert calls == ["after"]


def test_check_now_shows_pending_state(tray: TrayApplication) -> None:
    tray._on_check_now()                   # noqa: SLF001
    assert "正在检查" in tray._rows["last_check"].cget("text")  # noqa: SLF001


# --------------------------------------------------------------------------
# 风险确认
# --------------------------------------------------------------------------


def test_enable_auto_clean_requires_confirmation(
    tray: TrayApplication, controller: AppController, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[str] = []

    def ask(*args, **kwargs):
        asked.append(args[0] if args else "")
        return False

    monkeypatch.setattr("recyclebingguard.tray.messagebox.askyesno", ask)
    tray._on_toggle_auto_clean()  # noqa: SLF001

    assert asked, "未展示风险说明"
    assert controller.config.auto_clean_enabled is False
    assert not controller.config_path.exists()


def test_enable_auto_clean_after_confirmation_persists(
    tray: TrayApplication, controller: AppController, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("recyclebingguard.tray.messagebox.askyesno", lambda *a, **k: True)
    tray._on_toggle_auto_clean()  # noqa: SLF001

    assert controller.config.auto_clean_enabled is True
    assert controller.config.auto_clean_confirmed is True
    assert controller.config_path.exists()


def test_no_confirmation_after_first_time(
    tray: TrayApplication, controller: AppController, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def ask(*args, **kwargs):
        calls.append(1)
        return True

    monkeypatch.setattr("recyclebingguard.tray.messagebox.askyesno", ask)

    tray._on_toggle_auto_clean()  # noqa: SLF001  第一次：需要确认
    tray._on_toggle_auto_clean()  # noqa: SLF001  关闭
    tray._on_toggle_auto_clean()  # noqa: SLF001  再次启用：不再确认

    assert len(calls) == 1
    assert controller.config.auto_clean_enabled is True


# --------------------------------------------------------------------------
# 刷新与退出
# --------------------------------------------------------------------------


def test_refresh_updates_labels(tray: TrayApplication, controller: AppController) -> None:
    controller.monitor.last_scan = ScanResult(
        total_items=8420, total_size_bytes=0, scanned_roots=("C:\\",)
    )
    tray._refresh()  # noqa: SLF001

    assert tray._lbl_count.cget("text") == "8,420"   # noqa: SLF001
    assert "正在监控" in tray._lbl_state.cget("text")  # noqa: SLF001


def test_refresh_marks_incomplete_scan(tray: TrayApplication, controller: AppController) -> None:
    controller.monitor.last_scan = ScanResult(
        total_items=100, total_size_bytes=0, scanned_roots=("C:\\",), failed_roots=("D:\\",)
    )
    tray._refresh()  # noqa: SLF001

    assert "统计不完整" in tray._rows["last_check"].cget("text")  # noqa: SLF001
    assert "统计不完整" in tray._lbl_state.cget("text")           # noqa: SLF001


def test_refresh_does_not_call_notify_before_icon_ready(tray: TrayApplication) -> None:
    notified: list[str] = []
    tray._notify = lambda title, message: notified.append(title)  # noqa: SLF001
    tray._refresh()  # noqa: SLF001
    assert notified == []


def test_pause_and_resume_via_ui(tray: TrayApplication, controller: AppController) -> None:
    tray._on_toggle_pause()  # noqa: SLF001
    assert controller.is_paused() is True
    assert tray._btn_pause.cget("text") == "恢复监控"  # noqa: SLF001

    tray._on_toggle_pause()  # noqa: SLF001
    assert controller.is_paused() is False
    assert tray._btn_pause.cget("text") == "暂停监控"  # noqa: SLF001


def test_toggle_dry_run_via_ui(tray: TrayApplication, controller: AppController) -> None:
    tray._on_toggle_dry_run()  # noqa: SLF001
    assert controller.config.dry_run is False


def test_start_minimized_flag_is_remembered(controller: AppController) -> None:
    app = TrayApplication(controller, start_minimized=True)
    try:
        assert app._start_minimized is True  # noqa: SLF001
    finally:
        app._root.destroy()  # noqa: SLF001

    app2 = TrayApplication(controller)
    try:
        assert app2._start_minimized is False  # noqa: SLF001
    finally:
        app2._root.destroy()  # noqa: SLF001


def test_wake_request_shows_existing_window(
    tray: TrayApplication, controller: AppController, isolated_home: Path
) -> None:
    from recyclebingguard import paths as paths_module
    from recyclebingguard.single_instance import request_wake

    tray._hide_window()  # noqa: SLF001
    assert tray._root.state() == "withdrawn"  # noqa: SLF001

    assert request_wake() is True
    assert paths_module.wake_request_path().exists()

    tray._tick()  # noqa: SLF001  模拟主循环的一次刷新

    assert tray._root.state() == "normal"  # noqa: SLF001
    assert not paths_module.wake_request_path().exists()   # 已被消费


def test_tick_without_wake_request_keeps_window_hidden(tray: TrayApplication) -> None:
    tray._hide_window()  # noqa: SLF001
    tray._tick()  # noqa: SLF001
    assert tray._root.state() == "withdrawn"  # noqa: SLF001


def test_install_shortcuts_from_menu(
    tray: TrayApplication,
    controller: AppController,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = [tmp_path / "RecycleBinGuard.lnk"]

    class Recorder:
        def __init__(self) -> None:
            self.calls = 0
            self.shown: list[str] = []

        def install(self) -> list[Path]:
            self.calls += 1
            return fake

        def showinfo(self, *args, **kwargs) -> None:
            self.shown.append("info")

        def showwarning(self, *args, **kwargs) -> None:
            self.shown.append("warning")

    recorder = Recorder()
    monkeypatch.setattr(controller, "install_shortcuts", recorder.install)
    # 两个对话框都必须替换掉，否则测试会弹出模态框阻塞
    monkeypatch.setattr("recyclebingguard.tray.messagebox.showinfo", recorder.showinfo)
    monkeypatch.setattr("recyclebingguard.tray.messagebox.showwarning", recorder.showwarning)

    tray._on_install_shortcuts()  # noqa: SLF001

    assert recorder.calls == 1
    assert recorder.shown == ["info"]


def test_install_shortcuts_failure_warns(
    tray: TrayApplication, controller: AppController, monkeypatch: pytest.MonkeyPatch
) -> None:
    shown: list[str] = []
    monkeypatch.setattr(controller, "install_shortcuts", lambda: [])
    monkeypatch.setattr("recyclebingguard.tray.messagebox.showinfo", lambda *a, **k: None)
    monkeypatch.setattr(
        "recyclebingguard.tray.messagebox.showwarning", lambda *a, **k: shown.append("warning")
    )

    tray._on_install_shortcuts()  # noqa: SLF001

    assert shown == ["warning"]


def test_quit_stops_everything(tray: TrayApplication, controller: AppController) -> None:
    tray._quit()  # noqa: SLF001
    tray._quit()  # noqa: SLF001  幂等

    # 退出后不应再接受新的后台请求
    controller.request_check()

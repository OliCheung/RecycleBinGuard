"""清理前确认弹窗测试。

覆盖两份需求清单：

- 弹窗行为（9999 不弹 / 10000、10001 弹；确认 -> 复核 -> 只清理一次；
  取消 / 关闭 / 超时取消 / 复核失败 / 复核低于阈值一律不清理）；
- 10 秒倒计时（默认 10、逐秒更新、到期进复核、取消或关闭后计时器停止、
  到期与取消竞争只产生一个决定、暂停或退出后到期回调不清理、
  配置为 0 或非法值不自动清理）；
- 并发与残留（两个监控周期最多一个弹窗、清理期间不并发、
  退出 / 暂停后未执行的请求不清理、UI 主线程不被阻塞）。

全部使用 fake 服务 / 假时钟 / 假调度器，**不触碰真实回收站**，也不等待真实时间。
"""

from __future__ import annotations

import gc
import threading
import time
import tkinter as tk
from pathlib import Path

import pytest

from recyclebingguard.app import AppController
from recyclebingguard.config import Config, ConfigError, load_config, save_config
from recyclebingguard.confirm_dialog import (
    CONFIRMATION_TITLE,
    COUNTDOWN_DONE_TEXT,
    COUNTDOWN_TEMPLATE,
    NO_COUNTDOWN_TEXT,
    SLIDE_MARGIN_X,
    SLIDE_MARGIN_Y,
    CleanConfirmDialog,
    work_area,
)
from recyclebingguard.confirmation import (
    CleanConfirmationRequest,
    ConfirmationDecision,
)
from recyclebingguard.interfaces import (
    CleanOutcome,
    CleanResult,
    MonitorState,
    ScanResult,
)
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor

THRESHOLD = 10000


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def ok_scan(items: int) -> ScanResult:
    return ScanResult(total_items=items, total_size_bytes=0, scanned_roots=("C:\\",))


def bad_scan(items: int) -> ScanResult:
    return ScanResult(
        total_items=items,
        total_size_bytes=0,
        scanned_roots=("C:\\", "D:\\"),
        failed_roots=("D:\\",),
    )


class FakeBinService:
    def __init__(self) -> None:
        self.scan_queue: list[ScanResult] = []
        self.default_scan: ScanResult = ok_scan(0)
        self.default_empty = CleanResult(outcome=CleanOutcome.SUCCESS)
        self.scan_calls = 0
        self.empty_calls = 0
        self.empty_verified_scan: ScanResult | None = None
        self.empty_gate: threading.Event | None = None
        self.empty_started = threading.Event()

    def scan(self) -> ScanResult:
        self.scan_calls += 1
        if self.scan_queue:
            return self.scan_queue.pop(0)
        return self.default_scan

    def empty(self, *, verified_scan: ScanResult | None = None) -> CleanResult:
        self.empty_calls += 1
        self.empty_verified_scan = verified_scan
        self.empty_started.set()
        if self.empty_gate is not None:
            self.empty_gate.wait(timeout=5)
        return self.default_empty


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class RecordingRequester:
    """记录所有确认请求；可选地在收到请求时立即回传一个决定。"""

    def __init__(self, auto: ConfirmationDecision | None = None, *, raise_error=False) -> None:
        self.requests: list[CleanConfirmationRequest] = []
        self.auto = auto
        self.raise_error = raise_error
        self.monitor: RecycleBinMonitor | None = None

    def request_clean_confirmation(self, request: CleanConfirmationRequest) -> None:
        self.requests.append(request)
        if self.raise_error:
            raise RuntimeError("UI 不可用")
        if self.auto is not None and self.monitor is not None:
            self.monitor.resolve_confirmation(request.token, self.auto)

    @property
    def count(self) -> int:
        return len(self.requests)


def make_config(**overrides) -> Config:
    base = dict(
        threshold_items=THRESHOLD,
        poll_interval_seconds=180,
        auto_clean_enabled=True,
        confirm_before_clean=True,
        confirmation_timeout_seconds=10,
        failure_backoff_seconds=300,
    )
    base.update(overrides)
    return Config(**base)


def make_monitor(**cfg):
    """返回 (monitor, service, requester, clock)。"""
    service = FakeBinService()
    clock = FakeClock()
    monitor = RecycleBinMonitor(service, make_config(**cfg), clock=clock)
    return monitor, service, None, clock


def make_with_requester(auto: ConfirmationDecision | None = None, **cfg):
    service = FakeBinService()
    clock = FakeClock()
    requester = RecordingRequester(auto)
    monitor = RecycleBinMonitor(
        service, make_config(**cfg), clock=clock, confirmation_requester=requester
    )
    requester.monitor = monitor
    return monitor, service, requester, clock


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------
# 一、配置
# --------------------------------------------------------------------------


def test_confirmation_defaults() -> None:
    cfg = Config()
    assert cfg.confirm_before_clean is True
    assert cfg.confirmation_timeout_seconds == 10


def test_timeout_zero_is_allowed() -> None:
    assert Config(confirmation_timeout_seconds=0).confirmation_timeout_seconds == 0


@pytest.mark.parametrize("value", [-1, -10, 1.5, "10", True, None])
def test_invalid_timeout_is_rejected(value) -> None:
    with pytest.raises(ConfigError, match="confirmation_timeout_seconds"):
        Config.from_dict({"confirmation_timeout_seconds": value})


def test_confirm_before_clean_must_be_bool() -> None:
    with pytest.raises(ConfigError, match="confirm_before_clean"):
        Config.from_dict({"confirm_before_clean": "yes"})
    assert Config.from_dict({"confirm_before_clean": False}).confirm_before_clean is False


def test_config_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    original = Config(confirm_before_clean=True, confirmation_timeout_seconds=25)
    save_config(original, target)
    reloaded = load_config(target)
    assert reloaded.confirm_before_clean is True
    assert reloaded.confirmation_timeout_seconds == 25


def test_corrupt_timeout_config_raises_instead_of_falling_back(tmp_path: Path) -> None:
    """配置解析失败必须报错退出，绝不静默启用立即自动清理。"""
    target = tmp_path / "config.json"
    target.write_text('{"confirmation_timeout_seconds": -3}', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(target)


@pytest.mark.parametrize("bad_value", ["10", None, 1.5, True, -5, [10]])
def test_invalid_timeout_value_never_enables_auto_clean(bad_value) -> None:
    """绕过校验的非法值在监控器侧也必须按 0（不自动倒计时）处理。"""
    service = FakeBinService()
    service.default_scan = ok_scan(50000)
    monitor = RecycleBinMonitor(service, make_config(), confirmation_requester=RecordingRequester())
    monitor.config.confirmation_timeout_seconds = bad_value  # type: ignore[assignment]

    monitor.tick()

    assert monitor._confirmation_timeout_seconds() == 0  # noqa: SLF001
    assert monitor.pending_confirmation_token is not None
    assert service.empty_calls == 0


# --------------------------------------------------------------------------
# 二、弹窗触发边界（9999 / 10000 / 10001）
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("items", "should_prompt"),
    [(9999, False), (10000, True), (10001, True)],
)
def test_prompt_boundaries(items: int, should_prompt: bool) -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(items)

    monitor.tick()

    assert requester.count == (1 if should_prompt else 0)
    assert monitor.state is (
        MonitorState.AWAITING_CONFIRMATION if should_prompt else MonitorState.IDLE
    )
    assert service.empty_calls == 0  # 弹窗阶段绝不清理


def test_request_carries_scan_and_threshold() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(12345)

    monitor.tick()

    assert requester.count == 1
    request = requester.requests[0]
    assert request.total_items == 12345
    assert request.threshold_items == THRESHOLD
    assert request.timeout_seconds == 10
    assert request.countdown_enabled is True


def test_incomplete_scan_never_prompts() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = bad_scan(50000)

    monitor.tick()

    assert requester.count == 0
    assert service.empty_calls == 0


def test_below_threshold_resumes_monitoring() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(9999)

    monitor.tick()

    assert requester.count == 0
    assert monitor.state is MonitorState.IDLE


# --------------------------------------------------------------------------
# 三、用户决定
# --------------------------------------------------------------------------


def test_confirm_cleans_exactly_once_after_recheck() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    token = monitor.pending_confirmation_token
    assert token is not None

    assert monitor.resolve_confirmation(token, ConfirmationDecision.CONFIRMED) is True

    assert service.empty_calls == 1
    # 触发统计 + 确认后复核 + 清理后复核 = 3 次
    assert service.scan_calls == 3
    assert monitor.state is MonitorState.IDLE
    assert monitor.status().clean_success_count == 1


@pytest.mark.parametrize(
    "decision",
    [
        ConfirmationDecision.CANCELLED,
        ConfirmationDecision.CLOSED,
        ConfirmationDecision.ABORTED,
    ],
)
def test_non_proceeding_decisions_never_clean(decision: ConfirmationDecision) -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    token = monitor.pending_confirmation_token
    monitor.resolve_confirmation(token, decision)

    assert service.empty_calls == 0
    assert monitor.pending_confirmation_token is None
    assert monitor.state is MonitorState.IDLE


def test_timeout_decision_proceeds_only_through_recheck() -> None:
    """倒计时归零 = 进入复核，不是直接清理。"""
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    token = monitor.pending_confirmation_token
    monitor.resolve_confirmation(token, ConfirmationDecision.TIMEOUT)

    assert service.empty_calls == 1
    assert service.scan_calls == 3  # 触发 + 确认后复核 + 清理后复核


def test_recheck_incomplete_cancels_clean() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.scan_queue = [ok_scan(10000), bad_scan(10000)]

    monitor.tick()
    monitor.resolve_confirmation(monitor.pending_confirmation_token, ConfirmationDecision.CONFIRMED)

    assert service.empty_calls == 0
    assert monitor.state is MonitorState.IDLE


def test_recheck_below_threshold_cancels_clean() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.scan_queue = [ok_scan(10000), ok_scan(9999)]

    monitor.tick()
    monitor.resolve_confirmation(monitor.pending_confirmation_token, ConfirmationDecision.CONFIRMED)

    assert service.empty_calls == 0
    assert monitor.state is MonitorState.IDLE


def test_double_resolve_only_cleans_once() -> None:
    """同一次确认被回传两次（重复点击 / 倒计时与按钮竞争）只清理一次。"""
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    token = monitor.pending_confirmation_token
    monitor.resolve_confirmation(token, ConfirmationDecision.CONFIRMED)
    assert monitor.resolve_confirmation(token, ConfirmationDecision.CONFIRMED) is False

    assert service.empty_calls == 1


def test_stale_token_is_ignored() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    assert monitor.resolve_confirmation(999, ConfirmationDecision.CONFIRMED) is False
    assert service.empty_calls == 0


# --------------------------------------------------------------------------
# 四、并发与残留请求
# --------------------------------------------------------------------------


def test_two_monitor_cycles_create_at_most_one_prompt() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    monitor.tick()
    monitor.tick()

    assert requester.count == 1
    assert service.empty_calls == 0


def test_no_second_clean_while_cleaning() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)
    service.empty_gate = threading.Event()

    monitor.tick()
    token = monitor.pending_confirmation_token

    worker = threading.Thread(
        target=lambda: monitor.resolve_confirmation(token, ConfirmationDecision.CONFIRMED)
    )
    worker.start()
    assert service.empty_started.wait(timeout=5), "清理未开始"

    monitor.tick()  # 清理/等待期间再次触发：不得并发
    monitor.resolve_confirmation(token, ConfirmationDecision.CONFIRMED)

    service.empty_gate.set()
    worker.join(timeout=5)

    assert service.empty_calls == 1


def test_pause_after_request_blocks_late_confirmation() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    token = monitor.pending_confirmation_token
    monitor.pause()

    assert monitor.pending_confirmation_token is None
    monitor.resolve_confirmation(token, ConfirmationDecision.CONFIRMED)

    assert service.empty_calls == 0
    assert monitor.state is MonitorState.PAUSED


def test_stop_after_request_blocks_late_confirmation() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    token = monitor.pending_confirmation_token
    monitor.stop()

    assert monitor.pending_confirmation_token is None
    monitor.resolve_confirmation(token, ConfirmationDecision.CONFIRMED)

    assert service.empty_calls == 0


def test_auto_clean_disabled_after_request_blocks_confirmation() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    token = monitor.pending_confirmation_token
    monitor.set_auto_clean_enabled(False)

    monitor.resolve_confirmation(token, ConfirmationDecision.CONFIRMED)

    assert service.empty_calls == 0
    assert monitor.state is MonitorState.IDLE


def test_no_requester_means_no_clean() -> None:
    """无界面（headless）时达到阈值只记录，绝不清理。"""
    monitor, service, _, _ = make_monitor()
    service.default_scan = ok_scan(50000)

    monitor.tick()

    assert service.empty_calls == 0
    assert monitor.state is MonitorState.IDLE


def test_requester_failure_cancels_round() -> None:
    service = FakeBinService()
    service.default_scan = ok_scan(50000)
    requester = RecordingRequester(raise_error=True)
    monitor = RecycleBinMonitor(service, make_config(), confirmation_requester=requester)

    monitor.tick()

    assert requester.count == 1
    assert monitor.pending_confirmation_token is None
    assert monitor.state is MonitorState.IDLE
    assert service.empty_calls == 0


def test_confirm_disabled_keeps_legacy_behaviour() -> None:
    """confirm_before_clean=false 时保持原有“复核 -> 清理”路径。"""
    monitor, service, _, _ = make_monitor(confirm_before_clean=False)
    service.default_scan = ok_scan(10000)

    monitor.tick()

    assert service.empty_calls == 1


def test_clean_failure_is_recorded_and_monitoring_can_prompt_again() -> None:
    monitor, service, requester, clock = make_with_requester()
    service.default_scan = ok_scan(10000)
    service.default_empty = CleanResult(outcome=CleanOutcome.FAILED, error="模拟失败")

    monitor.tick()
    monitor.resolve_confirmation(monitor.pending_confirmation_token, ConfirmationDecision.CONFIRMED)

    assert monitor.state is MonitorState.ERROR
    assert monitor.status().clean_failure_count == 1
    assert monitor.status().last_error is not None

    # 失败退避结束后，下一轮仍可重新弹窗
    clock.advance(300)
    monitor.tick()
    assert requester.count == 2


def test_next_round_can_prompt_again_after_success() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    monitor.resolve_confirmation(monitor.pending_confirmation_token, ConfirmationDecision.CANCELLED)
    monitor.tick()

    assert requester.count == 2
    assert service.empty_calls == 0


def test_status_exposes_pending_confirmation() -> None:
    monitor, service, requester, _ = make_with_requester()
    service.default_scan = ok_scan(10000)

    monitor.tick()
    assert monitor.status().pending_confirmation is True

    monitor.resolve_confirmation(monitor.pending_confirmation_token, ConfirmationDecision.CANCELLED)
    assert monitor.status().pending_confirmation is False


# --------------------------------------------------------------------------
# 五、确认弹窗 UI（tkinter，主线程）
# --------------------------------------------------------------------------


class Decisions:
    def __init__(self) -> None:
        self.items: list[tuple[int, ConfirmationDecision]] = []

    def __call__(self, token: int, decision: ConfirmationDecision) -> None:
        self.items.append((token, decision))

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def only(self) -> ConfirmationDecision:
        assert len(self.items) == 1
        return self.items[0][1]


class FakeScheduler:
    """替代 ``after``：记录调度请求，不真正等待时间。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []

    def __call__(self, ms: int, callback) -> object:
        self.calls.append((ms, callback))
        return f"timer-{len(self.calls)}"


@pytest.fixture(autouse=True)
def _collect_tk_garbage_in_main_thread():
    """tkinter 对象必须由**主线程**回收。

    Tcl 解释器不是线程安全的：如果 Toplevel / Tk 对象被后台线程（例如
    ``AppController`` 的执行器线程）的 GC 回收，Tcl 会在错误的线程上收尾，
    表现为进程级 ``Windows fatal exception``（不是普通的测试失败）。
    因此每个用例结束后在主线程显式回收一次。
    """
    yield
    gc.collect()


@pytest.fixture
def decisions() -> Decisions:
    return Decisions()


@pytest.fixture
def root():
    try:
        holder = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"无可用图形环境：{exc}")
    holder.withdraw()
    yield holder
    try:
        holder.destroy()
    except tk.TclError:  # pragma: no cover
        pass


def make_dialog(
    root,
    decisions: Decisions,
    *,
    timeout: int = 10,
    token: int = 7,
    scheduler=None,
    animate: bool = False,
) -> CleanConfirmDialog:
    """建一个确认弹窗。

    默认 ``animate=False``：本文件的用例大多直接调用 ``_on_tick()`` 驱动倒计时，
    不跑事件循环；关掉动画才能保持“归零即产生决定”的同步语义。
    动画本身由专门的事件循环用例覆盖（见文末“动效”一节）。
    """
    request = CleanConfirmationRequest(
        token=token, total_items=12345, threshold_items=THRESHOLD, timeout_seconds=timeout
    )
    return CleanConfirmDialog(root, request, decisions, scheduler=scheduler, animate=animate)


def _all_texts(widget: tk.Misc) -> list[str]:
    """递归收集窗口内所有带文本控件的文本。"""
    texts: list[str] = []
    for child in widget.winfo_children():
        try:
            texts.append(str(child.cget("text")))
        except tk.TclError:  # 容器控件没有 -text
            pass
        texts.extend(_all_texts(child))
    return texts


def test_dialog_shows_items_threshold_and_warning(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    try:
        assert dialog._window.title() == CONFIRMATION_TITLE  # noqa: SLF001
        texts = _all_texts(dialog._window)  # noqa: SLF001
        assert "当前回收站项目数：12,345" in texts
        assert f"自动清理阈值：{THRESHOLD:,}" in texts
        assert any("永久删除" in t for t in texts)
        assert any("取消后程序会继续监控" in t for t in texts)
        assert dialog.remaining_seconds == 10
        assert decisions.count == 0  # 创建时不产生任何决定
        assert dialog.decision is None
    finally:
        dialog.abort("测试结束")


def test_dialog_default_countdown_is_ten_seconds(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    try:
        assert dialog.remaining_seconds == 10
        assert (
            dialog._lbl_countdown.cget("text")  # noqa: SLF001
            == COUNTDOWN_TEMPLATE.format(seconds=10)
        )
    finally:
        dialog.abort("测试结束")


def test_countdown_updates_every_second(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    try:
        texts = [dialog._lbl_countdown.cget("text")]  # noqa: SLF001
        for _ in range(9):
            dialog._on_tick()  # noqa: SLF001
            texts.append(dialog._lbl_countdown.cget("text"))  # noqa: SLF001

        assert texts == [COUNTDOWN_TEMPLATE.format(seconds=s) for s in range(10, 0, -1)]
        assert dialog.remaining_seconds == 1
        assert decisions.count == 0  # 归零前不产生决定
    finally:
        dialog.abort("测试结束")


def test_countdown_expiry_enters_recheck_decision(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    for _ in range(10):
        dialog._on_tick()  # noqa: SLF001

    assert decisions.count == 1
    assert decisions.only is ConfirmationDecision.TIMEOUT
    assert ConfirmationDecision.TIMEOUT.proceeds is True
    assert decisions.items[0][0] == 7  # 决定携带原 token
    assert dialog.remaining_seconds == 0


def test_confirm_click_decides_once(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    dialog._on_confirm()  # noqa: SLF001
    dialog._on_confirm()  # noqa: SLF001  重复点击无效
    dialog._on_tick()  # noqa: SLF001  计时器也不再产生决定

    assert decisions.count == 1
    assert decisions.only is ConfirmationDecision.CONFIRMED


def test_cancel_click_stops_timer(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    dialog._on_cancel()  # noqa: SLF001

    assert dialog.timer_id is None
    assert decisions.only is ConfirmationDecision.CANCELLED

    dialog._on_tick()  # noqa: SLF001
    assert decisions.count == 1


def test_closing_window_equals_cancel(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    assert dialog._window.protocol("WM_DELETE_WINDOW")  # noqa: SLF001  关闭按钮已绑定
    dialog._on_close()  # noqa: SLF001

    assert dialog.timer_id is None
    assert decisions.count == 1
    assert decisions.only is ConfirmationDecision.CLOSED
    assert ConfirmationDecision.CLOSED.proceeds is False


def test_timeout_and_cancel_race_produce_one_decision(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    for _ in range(10):
        dialog._on_tick()  # noqa: SLF001  倒计时到期
    dialog._on_cancel()  # noqa: SLF001  同时点击取消

    assert decisions.count == 1


def test_zero_timeout_never_auto_cleans(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions, timeout=0)
    try:
        assert dialog.remaining_seconds is None
        assert dialog._lbl_countdown.cget("text") == NO_COUNTDOWN_TEXT  # noqa: SLF001
        for _ in range(30):
            dialog._on_tick()  # noqa: SLF001
        assert decisions.count == 0
    finally:
        dialog.abort("测试结束")


def test_countdown_uses_after_not_blocking_wait(
    root, decisions: Decisions, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler = FakeScheduler()
    monkeypatch.setattr(tk.Misc, "grab_set", lambda *_a, **_k: pytest.fail("不得使用模态抓取"))
    monkeypatch.setattr(tk.Misc, "wait_window", lambda *_a, **_k: pytest.fail("不得阻塞等待窗口"))
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: pytest.fail("不得使用阻塞式 sleep"))

    dialog = make_dialog(root, decisions, scheduler=scheduler)
    try:
        assert scheduler.calls, "倒计时未通过 after() 调度"
        assert scheduler.calls[0][0] == 1000  # 每秒一次
        assert dialog._window.grab_status() in (None, "none")  # noqa: SLF001
    finally:
        dialog.abort("测试结束")


def test_dialog_must_be_created_on_main_thread(root, decisions: Decisions) -> None:
    errors: list[BaseException] = []

    def build() -> None:
        try:
            make_dialog(root, decisions)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=build)
    worker.start()
    worker.join(timeout=5)

    assert errors and isinstance(errors[0], RuntimeError)
    assert decisions.count == 0


def test_abort_from_outside_decides_aborted(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions)
    dialog.abort("程序退出")

    assert decisions.only is ConfirmationDecision.ABORTED
    assert ConfirmationDecision.ABORTED.proceeds is False


# --------------------------------------------------------------------------
# 五之二、动效：右下角升起 / 超时降下（需要真实事件循环）
# --------------------------------------------------------------------------


def _pump(root: tk.Misc, predicate, timeout: float = 4.0) -> bool:
    """驱动事件循环直到 ``predicate`` 为真或超时（动画依赖真实 ``after``）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _window_geometry(window: tk.Misc) -> tuple[int, int]:
    """解析 ``geometry()`` 里的 ``+x+y`` 偏移。"""
    import re

    match = re.search(r"\+(-?\d+)\+(-?\d+)", window.geometry())
    assert match is not None, f"意外的 geometry：{window.geometry()}"
    return int(match.group(1)), int(match.group(2))


def test_dialog_slides_in_to_bottom_right(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions, animate=True)
    try:
        assert dialog.animation_enabled is True
        _, _, right, bottom = work_area(dialog._window)  # noqa: SLF001

        # 起始位置在屏幕下缘之外（看不见），位置取工作区右侧留白处
        start_x, start_y = _window_geometry(dialog._window)  # noqa: SLF001
        assert start_y >= bottom
        assert start_x == right - dialog._window.winfo_reqwidth() - SLIDE_MARGIN_X  # noqa: SLF001

        assert _pump(root, lambda: not dialog.animating), "升起动画未在预期时间内播完"

        x, y = _window_geometry(dialog._window)  # noqa: SLF001
        width, height = dialog._window.winfo_width(), dialog._window.winfo_height()  # noqa: SLF001
        assert abs(x - (right - width - SLIDE_MARGIN_X)) <= 2
        assert abs(y - (bottom - height - SLIDE_MARGIN_Y)) <= 2
        assert decisions.count == 0  # 动画期间不产生决定
    finally:
        dialog.abort("测试结束")


def test_timeout_slides_out_then_enters_recheck(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions, animate=True)
    for _ in range(10):
        dialog._on_tick()  # noqa: SLF001

    # 决定立刻锁定（不会再出现第二个决定），但要等“降下”动画播完才交付
    assert dialog.decision is ConfirmationDecision.TIMEOUT
    assert dialog.timer_id is None
    assert dialog._lbl_countdown.cget("text") == COUNTDOWN_DONE_TEXT  # noqa: SLF001
    assert decisions.count == 0

    assert _pump(root, lambda: decisions.count == 1), "降下动画未在预期时间内交付决定"
    assert decisions.only is ConfirmationDecision.TIMEOUT
    assert dialog.window.winfo_exists() == 0  # 交付即收窗


def test_slide_out_ignores_cancel_click(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions, animate=True)
    for _ in range(10):
        dialog._on_tick()  # noqa: SLF001
    dialog._on_cancel()  # noqa: SLF001  降下动画期间点击取消
    dialog._on_confirm()  # noqa: SLF001  再点立即清理

    assert _pump(root, lambda: decisions.count == 1)
    assert decisions.count == 1
    assert decisions.only is ConfirmationDecision.TIMEOUT  # 已锁定的超时决定胜出


def test_abort_during_slide_out_delivers_locked_decision(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions, animate=True)
    for _ in range(10):
        dialog._on_tick()  # noqa: SLF001
    assert decisions.count == 0

    dialog.abort("程序退出")  # 立即送达，不必等动画播完

    assert decisions.count == 1
    assert decisions.only is ConfirmationDecision.TIMEOUT
    assert dialog.window.winfo_exists() == 0


def test_animation_can_be_disabled_for_headless_tests(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions, animate=False)
    try:
        assert dialog.animation_enabled is False
        for _ in range(10):
            dialog._on_tick()  # noqa: SLF001
        # 关掉动画后决定同步交付：不需要跑事件循环（倒计时逻辑用例正是这么用的）
        assert decisions.count == 1
        assert decisions.only is ConfirmationDecision.TIMEOUT
    finally:
        dialog.abort("测试结束")


def test_headless_mode_still_lands_bottom_right(root, decisions: Decisions) -> None:
    dialog = make_dialog(root, decisions, animate=False)
    try:
        assert _pump(root, lambda: not dialog.animating), "无动画模式未在预期时间内就位"
        x, y = _window_geometry(dialog._window)  # noqa: SLF001
        _, _, right, bottom = work_area(dialog._window)  # noqa: SLF001
        width, height = dialog._window.winfo_width(), dialog._window.winfo_height()  # noqa: SLF001
        assert abs(x - (right - width - SLIDE_MARGIN_X)) <= 2
        assert abs(y - (bottom - height - SLIDE_MARGIN_Y)) <= 2
    finally:
        dialog.abort("测试结束")


# --------------------------------------------------------------------------
# 六、控制器接线
# --------------------------------------------------------------------------


def _controller(tmp_path: Path, **cfg) -> tuple[AppController, FakeBinService]:
    config = make_config(**cfg)
    service = FakeBinService()
    monitor = RecycleBinMonitor(service, config)
    ctl = AppController(
        config,
        service=service,  # type: ignore[arg-type]
        monitor=monitor,
        config_path=tmp_path / "config.json",
        log_path=tmp_path / "recyclebingguard.log",
    )
    return ctl, service


def test_controller_forwards_request_to_ui(tmp_path: Path) -> None:
    ctl, service = _controller(tmp_path)
    service.default_scan = ok_scan(10000)
    seen: list[CleanConfirmationRequest] = []
    ctl.on_confirmation_request = seen.append

    ctl.monitor.tick()

    assert len(seen) == 1
    assert seen[0].total_items == 10000
    assert ctl.pending_confirmation_token() == seen[0].token
    ctl.shutdown()


def test_controller_confirmation_cleans_once(tmp_path: Path) -> None:
    ctl, service = _controller(tmp_path)
    service.default_scan = ok_scan(10000)
    seen: list[CleanConfirmationRequest] = []
    ctl.on_confirmation_request = seen.append

    ctl.monitor.tick()
    ctl.submit_confirmation(ConfirmationDecision.CONFIRMED, seen[0].token)

    assert _wait_until(lambda: service.empty_calls == 1), "确认后未执行清理"
    time.sleep(0.05)  # 再给一点时间确认没有第二次
    assert service.empty_calls == 1
    ctl.shutdown()


def test_controller_cancel_never_cleans(tmp_path: Path) -> None:
    ctl, service = _controller(tmp_path)
    service.default_scan = ok_scan(10000)
    seen: list[CleanConfirmationRequest] = []
    ctl.on_confirmation_request = seen.append

    ctl.monitor.tick()
    ctl.submit_confirmation(ConfirmationDecision.CANCELLED, seen[0].token)

    assert _wait_until(lambda: ctl.pending_confirmation_token() is None)
    time.sleep(0.05)
    assert service.empty_calls == 0
    ctl.shutdown()


def test_controller_without_ui_never_cleans(tmp_path: Path) -> None:
    ctl, service = _controller(tmp_path)
    service.default_scan = ok_scan(10000)
    ctl.on_confirmation_request = None

    ctl.monitor.tick()

    assert _wait_until(lambda: ctl.pending_confirmation_token() is None), "未注册的弹窗未作废"
    time.sleep(0.05)
    assert service.empty_calls == 0
    ctl.shutdown()


def test_shutdown_blocks_queued_confirmation(tmp_path: Path) -> None:
    ctl, service = _controller(tmp_path)
    service.default_scan = ok_scan(10000)
    seen: list[CleanConfirmationRequest] = []
    ctl.on_confirmation_request = seen.append

    ctl.monitor.tick()
    ctl.shutdown()
    ctl.submit_confirmation(ConfirmationDecision.CONFIRMED, seen[0].token)

    time.sleep(0.05)
    assert service.empty_calls == 0


# --------------------------------------------------------------------------
# 七、托盘接线
# --------------------------------------------------------------------------


@pytest.fixture
def tray_app(tmp_path: Path, isolated_home: Path):
    from recyclebingguard.tray import TrayApplication

    ctl, service = _controller(tmp_path)
    try:
        app = TrayApplication(ctl)
    except tk.TclError as exc:  # pragma: no cover - 无显示环境
        ctl.shutdown()
        pytest.skip(f"无可用图形环境：{exc}")
    yield app, ctl, service
    try:
        if app._root.winfo_exists():  # noqa: SLF001
            app._root.destroy()  # noqa: SLF001
    except Exception:
        pass
    ctl.shutdown()


def test_tray_creates_dialog_on_main_thread_only_after_drain(tray_app) -> None:
    app, ctl, service = tray_app
    service.default_scan = ok_scan(10000)

    ctl.monitor.tick()  # 监控线程侧发起请求
    assert app._confirm_dialog is None  # noqa: SLF001  尚未投递到主线程
    app._drain_ui_queue()  # noqa: SLF001  主线程取出并执行
    assert app._confirm_dialog is not None  # noqa: SLF001
    assert service.empty_calls == 0


def test_tray_ignores_second_confirmation_request(tray_app) -> None:
    app, ctl, service = tray_app
    service.default_scan = ok_scan(10000)

    ctl.monitor.tick()
    app._drain_ui_queue()  # noqa: SLF001
    first = app._confirm_dialog  # noqa: SLF001

    app._show_clean_confirmation(  # noqa: SLF001
        CleanConfirmationRequest(token=999, total_items=20000, threshold_items=THRESHOLD)
    )
    assert app._confirm_dialog is first  # noqa: SLF001

    first.abort("测试结束")  # noqa: SLF001
    assert app._confirm_dialog is None  # noqa: SLF001


def test_tray_forwards_decision_to_controller(tray_app) -> None:
    app, ctl, service = tray_app
    service.default_scan = ok_scan(10000)
    submitted: list[tuple[ConfirmationDecision, int]] = []
    ctl.submit_confirmation = lambda decision, token: submitted.append((decision, token))  # type: ignore[method-assign]

    ctl.monitor.tick()
    app._drain_ui_queue()  # noqa: SLF001
    token = app._confirm_dialog.request.token  # noqa: SLF001
    app._confirm_dialog._on_confirm()  # noqa: SLF001

    assert submitted == [(ConfirmationDecision.CONFIRMED, token)]
    assert service.empty_calls == 0  # 复核与清理由控制器后台线程执行


def test_tray_pause_closes_pending_dialog(tray_app) -> None:
    app, ctl, service = tray_app
    service.default_scan = ok_scan(10000)

    ctl.monitor.tick()
    app._drain_ui_queue()  # noqa: SLF001
    assert app._confirm_dialog is not None  # noqa: SLF001

    app._on_toggle_pause()  # noqa: SLF001

    assert app._confirm_dialog is None  # noqa: SLF001
    assert ctl.is_paused() is True
    assert ctl.pending_confirmation_token() is None
    time.sleep(0.05)
    assert service.empty_calls == 0


def test_tray_quit_aborts_pending_confirmation(tray_app) -> None:
    app, ctl, service = tray_app
    service.default_scan = ok_scan(10000)

    ctl.monitor.tick()
    app._drain_ui_queue()  # noqa: SLF001
    assert app._confirm_dialog is not None  # noqa: SLF001

    app._quit()  # noqa: SLF001

    assert app._confirm_dialog is None  # noqa: SLF001
    assert ctl.pending_confirmation_token() is None
    time.sleep(0.05)
    assert service.empty_calls == 0

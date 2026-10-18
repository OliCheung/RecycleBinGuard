"""T2：阈值监控与安全状态机。

在 T1 的 :class:`~recyclebingguard.recycle_bin_service.RecycleBinService` 之上
加入定时轮询、阈值判断、防并发、失败退避。

安全边界（PRD 第 7 节）：

- 首次安装 ``auto_clean_enabled = True``（默认开启自动清理，每次清理前仍弹确认框）；
- 只有**所有应纳入磁盘查询成功**时才判断阈值；
- 达到阈值且开启 ``confirm_before_clean`` 时，先进入 ``AWAITING_CONFIRMATION``
  并请求确认弹窗；**只有用户（或倒计时）明确继续**后才进入复核；
- 清空前二次核验，仍达阈值才清空；
- 同一时刻只允许一个清理任务与一个待确认请求；
- 清理完成后重新统计，不在同一轮无限重试；
- 失败后至少等待 ``failure_backoff_seconds`` 再重试；
- 暂停、停止或关闭自动清理后，不会执行残留的清理或过期的确认决定。

本模块不含托盘 UI 与开机启动逻辑。

可测试性：时钟与休眠函数都可注入，:meth:`RecycleBinMonitor.tick` 可脱离线程
单独驱动，从而无需真实等待即可覆盖所有分支。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from .clean_history import CleanHistory, CleanRecord
from .config import Config
from .confirmation import (
    CleanConfirmationRequest,
    ConfirmationDecision,
    ConfirmationRequester,
)
from .interfaces import (
    CleanOutcome,
    CleanResult,
    MonitorState,
    RecycleBinService,
    ScanResult,
)
from .logging_setup import get_logger

logger = get_logger()

#: 线程名，便于调试。
MONITOR_THREAD_NAME = "RecycleBinGuard-Monitor"


@dataclass(frozen=True)
class MonitorStatus:
    """供 UI 展示的监控快照（T3 使用）。"""

    state: MonitorState
    auto_clean_enabled: bool
    interval_seconds: int
    last_scan: ScanResult | None = None
    last_clean_result: CleanResult | None = None
    last_error: str | None = None
    backoff_remaining_seconds: float = 0.0
    scan_count: int = 0
    threshold_hits: int = 0
    clean_success_count: int = 0
    clean_failure_count: int = 0
    #: 是否有一个确认弹窗正在等待用户决定（UI 可据此显示“等待确认”）。
    pending_confirmation: bool = False
    #: 最近一次**执行过**清理的时间（持久化历史，重启后仍在）。
    last_clean_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        """是否处于可执行清理的活动状态。"""
        return self.state is not MonitorState.PAUSED


class RecycleBinMonitor:
    """按固定间隔检查回收站并在达到阈值时（可选）自动清空。

    实现 :class:`recyclebingguard.interfaces.RecycleBinMonitor` 协议。
    """

    def __init__(
        self,
        service: RecycleBinService,
        config: Config,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] | None = None,
        confirmation_requester: ConfirmationRequester | None = None,
        history: CleanHistory | None = None,
    ) -> None:
        self._service = service
        self.config = config
        self._clock = clock
        self._sleeper = sleeper
        #: 清理历史（持久化）；注入自定义路径便于测试隔离。
        self.history: CleanHistory = history if history is not None else CleanHistory()

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

        #: 确认弹窗提交方（UI 层实现）；为 None 时达到阈值只能记录、不清理。
        self._confirmation_requester: ConfirmationRequester | None = confirmation_requester
        #: 当前待确认请求的 token；None 表示没有待确认请求。
        self._pending_token: int | None = None
        self._next_token = 1

        self._state = MonitorState.IDLE
        self._cleaning = False
        self._backoff_until = 0.0
        #: 最近一次真正做过统计的时刻（单调时钟），供“下次检查”倒计时使用。
        self._last_check_monotonic: float | None = None

        self.last_scan: ScanResult | None = None
        self.last_clean_result: CleanResult | None = None
        self.last_error: str | None = None

        self.scan_count = 0
        self.threshold_hits = 0
        self.clean_success_count = 0
        self.clean_failure_count = 0

        # 清理历史惰性加载：构造时不碰磁盘，首次查询才读文件。
        self._last_clean_record: CleanRecord | None = None
        self._last_successful_record: CleanRecord | None = None
        self._history_loaded = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动后台监控线程（已启动时为空操作）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                logger.info("监控线程已在运行，忽略重复启动")
                return
            self._stop_event.clear()
            self._wake_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name=MONITOR_THREAD_NAME, daemon=True
            )
            self._thread.start()
        logger.info(
            "监控已启动：间隔=%ds 阈值=%d 自动清理=%s",
            self.config.poll_interval_seconds,
            self.config.threshold_items,
            self.config.auto_clean_enabled,
        )

    def stop(self, timeout: float | None = 10.0) -> None:
        """停止监控并等待线程退出，不残留后台任务、不残留待确认请求。"""
        self._stop_event.set()
        self._wake_event.set()
        # 先作废待确认请求：即使弹窗随后才回传“立即清理”，也不会再执行。
        self.abort_pending_confirmation("监控已停止")
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            self._thread = None
        logger.info("监控已停止（状态=%s）", self._state.value)

    def pause(self) -> None:
        """暂停监控；暂停期间不进入阈值判断与清理，待确认请求一并作废。"""
        with self._lock:
            self._state = MonitorState.PAUSED
        logger.warning("监控已暂停")
        self.abort_pending_confirmation("监控已暂停")

    def resume(self) -> None:
        """恢复监控。"""
        with self._lock:
            if self._state is MonitorState.PAUSED:
                self._state = MonitorState.IDLE
        logger.info("监控已恢复")

    # ------------------------------------------------------------------
    # 清理前确认
    # ------------------------------------------------------------------

    def set_confirmation_requester(self, requester: ConfirmationRequester | None) -> None:
        """注册 / 移除确认弹窗提交方。

        未注册时达到阈值只会记录日志，**绝不清理**——这是无界面（headless）
        场景下的安全默认。
        """
        with self._lock:
            self._confirmation_requester = requester
        logger.info("清理确认回调已%s", "注册" if requester is not None else "移除")

    @property
    def pending_confirmation_token(self) -> int | None:
        """当前待确认请求的 token（无则 ``None``）。"""
        with self._lock:
            return self._pending_token

    def abort_pending_confirmation(self, reason: str) -> None:
        """作废当前待确认请求（退出 / 暂停时调用）。"""
        with self._lock:
            token = self._pending_token
            self._pending_token = None
            paused = self._state is MonitorState.PAUSED
        if token is None:
            return
        logger.warning("已作废待确认的清理请求：token=%d 原因=%s", token, reason)
        if not paused:
            self._set_state(MonitorState.IDLE)

    def resolve_confirmation(self, token: int, decision: ConfirmationDecision) -> bool:
        """处理确认弹窗的最终决定，返回是否**进入**了清理流程。

        安全闸门（任一不成立即不清理，且不留残留状态）：

        - token 必须与当前待确认请求一致（过期 / 重复决定直接丢弃）；
        - 监控未停止、未暂停、自动清理仍启用；
        - 决定必须是“继续”（``CONFIRMED`` / ``TIMEOUT``）；
        - **重新统计复核**：统计完整且仍达阈值才允许清空。
        """
        with self._lock:
            pending = self._pending_token
            stopped = self._stop_event.is_set()
            paused = self._state is MonitorState.PAUSED
            enabled = self.config.auto_clean_enabled

        if pending is None or pending != token:
            # 过期 / 重复决定：直接丢弃，**不动**当前待确认请求。
            logger.warning(
                "忽略过期或重复的清理确认：收到 token=%s 当前待确认=%s 决定=%s",
                token,
                pending,
                decision.value,
            )
            with self._lock:
                stuck = (
                    self._state is MonitorState.AWAITING_CONFIRMATION
                    and pending is None
                    and not self._cleaning
                )
            if stuck and not stopped and not paused:
                self._to_idle()  # 防御：状态卡在“等待确认”时兜底恢复
            return False

        # 一个 token 只允许被消费一次，先作废再处理。
        with self._lock:
            self._pending_token = None

        if stopped:
            logger.warning("监控已停止，丢弃清理确认：token=%d 决定=%s", token, decision.value)
            return self._finish_confirmation(False)
        if paused:
            logger.warning("监控已暂停，丢弃清理确认：token=%d 决定=%s", token, decision.value)
            return self._finish_confirmation(False)
        if not enabled:
            logger.warning("自动清理已关闭，丢弃清理确认：token=%d 决定=%s", token, decision.value)
            return self._finish_confirmation(False)

        if not decision.proceeds:
            logger.warning(
                "用户取消本轮清理：token=%d 决定=%s（清理 API 未被调用）",
                token,
                decision.value,
            )
            return self._finish_confirmation(False)

        logger.warning("用户确认清理：token=%d 决定=%s，开始清理前复核", token, decision.value)
        # 复核：必须重新查询，绝不沿用触发时的旧数量。
        recheck = self._service.scan()
        with self._lock:
            self.last_scan = recheck
            self.scan_count += 1

        if not recheck.is_complete:
            logger.error(
                "确认后复核统计不完整，取消清理：失败磁盘=%s%s",
                list(recheck.failed_roots),
                f"（{recheck.error}）" if recheck.error else "",
            )
            return self._finish_confirmation(False)
        if not recheck.should_trigger(
            self.config.threshold_items, self.config.threshold_size_bytes
        ):
            logger.warning(
                "确认后复核未达阈值（项目数=%d 阈值=%d），取消清理",
                recheck.total_items,
                self.config.threshold_items,
            )
            return self._finish_confirmation(False)

        logger.info(
            "确认后复核通过：项目数=%d 阈值=%d", recheck.total_items, self.config.threshold_items
        )
        # 复核结果直接交给清空流程复用，避免清空前再扫一遍。
        self._clean(trigger=decision.value, verified_scan=recheck)  # confirmed / timeout
        return True

    def _finish_confirmation(self, cleaned: bool) -> bool:
        """结束一轮确认流程并恢复可继续监控的状态。"""
        self._to_idle()
        return cleaned

    # ------------------------------------------------------------------
    # 运行时配置
    # ------------------------------------------------------------------

    def set_auto_clean_enabled(self, enabled: bool) -> None:
        """启用/关闭自动清理。

        关闭后，本轮之后的清理都会被拒绝（不会执行残留的清理）。
        调用方（T3）负责把变更持久化到配置文件。
        """
        with self._lock:
            self.config.auto_clean_enabled = bool(enabled)
        logger.warning("自动清理已%s", "启用" if enabled else "关闭")

    # ------------------------------------------------------------------
    # 单轮检查
    # ------------------------------------------------------------------

    def check_now(self) -> ScanResult | None:
        """立即执行一次完整检查（含阈值判断），返回最近一次统计结果。"""
        self.tick()
        with self._lock:
            return self.last_scan

    def tick(self) -> MonitorState:
        """执行一轮检查，返回本轮结束后的状态。

        可在任意线程调用；内部使用可重入锁与 ``_cleaning`` 标志保证不会并发清理。
        """
        with self._lock:
            if self._state is MonitorState.PAUSED:
                logger.info("监控已暂停，跳过本轮检查")
                return self._state
            if self._cleaning:
                logger.info("已有清理任务在执行，跳过本轮检查")
                return self._state
            if self._state is MonitorState.AWAITING_CONFIRMATION:
                # 已有一个确认弹窗在等用户：绝不再弹第二个，也不再重复统计触发。
                logger.info("已有确认弹窗等待用户决定，跳过本轮检查")
                return self._state

        self._set_state(MonitorState.CHECKING)
        scan = self._service.scan()
        with self._lock:
            self.last_scan = scan
            self.scan_count += 1
            self._last_check_monotonic = self._clock()

        if not scan.is_complete:
            logger.warning(
                "统计不完整，本轮不做任何清理：失败磁盘=%s%s",
                list(scan.failed_roots),
                f"（{scan.error}）" if scan.error else "",
            )
            return self._to_idle()

        threshold = self.config.threshold_items
        size_threshold = self.config.threshold_size_bytes
        if not scan.should_trigger(threshold, size_threshold):
            logger.info(
                "回收站未达阈值（项目数=%d 阈值=%d 占用=%d 大小阈值=%d），继续监控",
                scan.total_items,
                threshold,
                scan.total_size_bytes,
                size_threshold,
            )
            return self._to_idle()

        with self._lock:
            self.threshold_hits += 1
        self._set_state(MonitorState.THRESHOLD_REACHED)
        logger.warning(
            "达到阈值：项目数=%d 阈值=%d 占用=%d 大小阈值=%d",
            scan.total_items,
            threshold,
            scan.total_size_bytes,
            size_threshold,
        )
        return self._handle_threshold(scan)

    # ------------------------------------------------------------------
    # 内部：阈值处理与清理
    # ------------------------------------------------------------------

    def _handle_threshold(self, scan: ScanResult) -> MonitorState:
        with self._lock:
            paused = self._state is MonitorState.PAUSED
            enabled = self.config.auto_clean_enabled
            remaining = self._backoff_until - self._clock()

        if paused or not enabled:
            logger.warning(
                "达到阈值但%s，仅记录不清理",
                "监控已暂停" if paused else "自动清理未启用",
            )
            return self._to_idle()

        if remaining > 0:
            logger.warning("处于失败退避期，剩余 %.1fs，跳过本次清理", remaining)
            return self._to_idle()

        if self.config.confirm_before_clean:
            return self._request_confirmation(scan)

        # 未开启清理前确认：沿用原有的“执行前二次核验 -> 清理”路径。
        recheck = self._service.scan()
        with self._lock:
            self.last_scan = recheck
            self.scan_count += 1

        if not recheck.is_complete:
            logger.error("二次核验统计不完整，取消清理：失败磁盘=%s", list(recheck.failed_roots))
            return self._to_idle()
        if not recheck.should_trigger(
            self.config.threshold_items, self.config.threshold_size_bytes
        ):
            logger.warning("二次核验后未达阈值（项目数=%d），取消清理", recheck.total_items)
            return self._to_idle()

        # 复核结果直接交给清空流程复用，避免清空前再扫一遍。
        return self._clean(verified_scan=recheck)

    def _request_confirmation(self, scan: ScanResult) -> MonitorState:
        """发起一次清理确认请求（不阻塞，不清理）。

        同一时刻只允许一个待确认请求：已存在时直接跳过，绝不会创建第二个弹窗。
        没有注册确认回调（无界面）时，只记录并放弃本轮，绝不清理。
        """
        with self._lock:
            requester = self._confirmation_requester
            if self._pending_token is not None:
                logger.info("已有待确认请求（token=%d），不再重复弹窗", self._pending_token)
                return self._state
            if requester is None:
                logger.error("已开启清理前确认但未注册确认回调，本轮放弃清理（不调用清空 API）")
                return self._to_idle()
            token = self._next_token
            self._next_token += 1
            self._pending_token = token
            timeout = self._confirmation_timeout_seconds()

        request = CleanConfirmationRequest(
            token=token,
            total_items=scan.total_items,
            threshold_items=self.config.threshold_items,
            timeout_seconds=timeout,
        )
        self._set_state(MonitorState.AWAITING_CONFIRMATION)
        logger.warning(
            "请求清理确认：token=%d 项目数=%d 阈值=%d 倒计时=%ds（%s）",
            token,
            request.total_items,
            request.threshold_items,
            timeout,
            "到期自动复核" if request.countdown_enabled else "不自动倒计时",
        )

        try:
            requester.request_clean_confirmation(request)
        except Exception:  # 提交失败必须收回待确认状态，绝不能卡死或误清理
            with self._lock:
                self._pending_token = None
            logger.exception("提交清理确认请求失败，本轮放弃清理")
            return self._to_idle()
        return self._state

    def _confirmation_timeout_seconds(self) -> int:
        """读取倒计时配置：必须是非负整数，**否则一律按 0**（永不自动清理）。

        这样即便有人绕过 :meth:`Config.validate` 直接改字段，也不会意外启用
        “立即 / 自动”清理。
        """
        value = self.config.confirmation_timeout_seconds
        if isinstance(value, bool) or not isinstance(value, int):
            logger.error(
                "confirmation_timeout_seconds 不是整数（%r），按 0 处理：不自动倒计时", value
            )
            return 0
        if value < 0:
            logger.error(
                "confirmation_timeout_seconds 为负数（%r），按 0 处理：不自动倒计时", value
            )
            return 0
        return value

    def _clean(
        self, trigger: str = "auto", *, verified_scan: ScanResult | None = None
    ) -> MonitorState:
        with self._lock:
            if self._cleaning:
                logger.warning("已有清理任务在执行，放弃本次清理")
                return self._state
            if self._state is MonitorState.PAUSED or not self.config.auto_clean_enabled:
                logger.warning("清理前检测到暂停或自动清理已关闭，取消本次清理")
                return self._to_idle()
            self._cleaning = True

        try:
            self._set_state(MonitorState.CLEANING)
            result = self._service.empty(verified_scan=verified_scan)
            with self._lock:
                self.last_clean_result = result

            self._set_state(MonitorState.VERIFYING)
            post = self._service.scan()
            with self._lock:
                self.last_scan = post
                self.scan_count += 1

            # 只要真正调用了清空 API 就落一条历史（成功 / 部分 / 失败都记）。
            self._record_clean(result, trigger)

            if result.outcome is CleanOutcome.SUCCESS:
                with self._lock:
                    self.clean_success_count += 1
                    self.last_error = None
                logger.info(
                    "清理成功：清理前=%s 清理后=%s 耗时=%.3fs",
                    result.items_before,
                    result.items_after,
                    result.duration_seconds,
                )
                return self._to_idle()

            message = (
                f"清理未成功：outcome={result.outcome.value}"
                f"；清理前={result.items_before} 清理后={result.items_after}"
                f" 复核={post.total_items if post.is_complete else '不可用'}"
                + (f"；{result.error}" if result.error else "")
            )
            with self._lock:
                self.clean_failure_count += 1
                self.last_error = message
                self._arm_backoff()
            logger.error("%s（将退避 %ds 后重试）", message, self.config.failure_backoff_seconds)
            self._set_state(MonitorState.ERROR)
            return self._state
        except Exception as exc:  # 防御性兜底：异常也必须进入退避，不得静默
            with self._lock:
                self.clean_failure_count += 1
                self.last_error = f"清理过程中发生异常：{exc!r}"
                self._arm_backoff()
            self._record_clean(None, "error", error=f"清理过程中发生异常：{exc!r}")
            logger.exception("清理过程中发生异常")
            self._set_state(MonitorState.ERROR)
            return self._state
        finally:
            with self._lock:
                self._cleaning = False

    # ------------------------------------------------------------------
    # 清理历史
    # ------------------------------------------------------------------

    def last_clean_record(self) -> CleanRecord | None:
        """最近一次**执行过**的清理（含失败）；首次调用时从磁盘加载。"""
        with self._lock:
            self._ensure_history_loaded()
            return self._last_clean_record

    def last_successful_clean(self) -> CleanRecord | None:
        """最近一次**成功**清理；首次调用时从磁盘加载（之后走内存缓存）。

        缓存是关键：状态窗口每 0.5 秒刷新一次，不能每次都读 ``clean_history.json``。
        """
        with self._lock:
            self._ensure_history_loaded()
            return self._last_successful_record

    def _ensure_history_loaded(self) -> None:
        """惰性加载清理历史并填充内存缓存（调用方需持有 ``self._lock``）。"""
        if self._history_loaded:
            return
        self._history_loaded = True
        records = self.history.load()
        self._last_clean_record = records[0] if records else None
        self._last_successful_record = next((r for r in records if r.succeeded), None)

    def _record_clean(
        self, result: CleanResult | None, trigger: str, *, error: str | None = None
    ) -> None:
        """把一次清理写入持久化历史；写入失败只记日志，不影响监控。"""
        record = CleanRecord(
            finished_at=datetime.now(),
            outcome=(result.outcome.value if result is not None else CleanOutcome.FAILED.value),
            trigger=trigger,
            items_before=result.items_before if result is not None else None,
            items_after=result.items_after if result is not None else None,
            threshold_items=self.config.threshold_items,
            duration_seconds=result.duration_seconds if result is not None else 0.0,
            error=(result.error if result is not None else None) or error,
        )
        with self._lock:
            # 先补齐历史缓存，避免后来的“失败记录”把更早的成功记录挤掉。
            self._ensure_history_loaded()
            self._last_clean_record = record
            if record.succeeded:
                self._last_successful_record = record
        self.history.append(record)

    # ------------------------------------------------------------------
    # 内部：循环
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:  # 线程内异常不得终止监控
                with self._lock:
                    self.last_error = f"检查过程中发生异常：{exc!r}"
                    self._arm_backoff()
                logger.exception("检查过程中发生异常")
                self._set_state(MonitorState.ERROR)

            if self._stop_event.is_set():
                break
            self._sleep(self._sleep_seconds())

    def _sleep(self, seconds: float) -> None:
        if self._sleeper is not None:
            self._sleeper(seconds)
            return
        self._wake_event.wait(timeout=max(seconds, 0.0))
        self._wake_event.clear()

    def _sleep_seconds(self) -> float:
        interval = float(self.config.poll_interval_seconds)
        remaining = self._backoff_until - self._clock()
        if remaining > 0:
            return min(interval, remaining)
        return interval

    # ------------------------------------------------------------------
    # 内部：状态与工具
    # ------------------------------------------------------------------

    def _set_state(self, state: MonitorState) -> None:
        with self._lock:
            if self._state is state:
                return
            logger.info("状态迁移：%s -> %s", self._state.value, state.value)
            self._state = state

    def _to_idle(self) -> MonitorState:
        with self._lock:
            if self._state is MonitorState.PAUSED:
                return self._state
        self._set_state(MonitorState.IDLE)
        return self._state

    def _arm_backoff(self) -> None:
        with self._lock:
            self._backoff_until = self._clock() + self.config.failure_backoff_seconds

    @property
    def state(self) -> MonitorState:
        with self._lock:
            return self._state

    def status(self) -> MonitorStatus:
        """返回当前监控状态快照。"""
        with self._lock:
            self.last_clean_record()  # 确保清理历史已加载（惰性、只加载一次）
            remaining = max(0.0, self._backoff_until - self._clock())
            return MonitorStatus(
                state=self._state,
                auto_clean_enabled=self.config.auto_clean_enabled,
                interval_seconds=self.config.poll_interval_seconds,
                last_scan=self.last_scan,
                last_clean_result=self.last_clean_result,
                last_error=self.last_error,
                backoff_remaining_seconds=round(remaining, 3),
                scan_count=self.scan_count,
                threshold_hits=self.threshold_hits,
                clean_success_count=self.clean_success_count,
                clean_failure_count=self.clean_failure_count,
                pending_confirmation=self._pending_token is not None,
                last_clean_at=(
                    self._last_clean_record.finished_at
                    if self._last_clean_record is not None
                    else None
                ),
            )

    def seconds_until_next_check(self) -> int:
        """下一次检查的近似剩余秒数（供 UI 展示）。

        退避期内返回剩余退避时间；否则按“上次统计时刻 + 轮询间隔”估算，
        因此清理耗时较长时也会体现在倒计时里。
        """
        with self._lock:
            interval = float(max(self.config.poll_interval_seconds, 0))
            backoff_remaining = self._backoff_until - self._clock()
            last_check = self._last_check_monotonic

        if backoff_remaining > 0:
            return int(max(0.0, backoff_remaining))
        if last_check is None:
            return int(interval)
        elapsed = self._clock() - last_check
        return int(max(0.0, interval - elapsed))

    def describe_state(self) -> str:
        """把状态翻译成 UI 可用的中文短语。"""
        mapping = {
            MonitorState.IDLE: "正在监控",
            MonitorState.CHECKING: "正在检查",
            MonitorState.THRESHOLD_REACHED: "已达阈值",
            MonitorState.AWAITING_CONFIRMATION: "等待确认清理",
            MonitorState.CLEANING: "正在清理",
            MonitorState.VERIFYING: "正在复核",
            MonitorState.ERROR: "清理失败",
            MonitorState.PAUSED: "已暂停",
        }
        with self._lock:
            state = self._state
        return mapping[state]


__all__ = ["MonitorStatus", "RecycleBinMonitor", "MonitorState"]

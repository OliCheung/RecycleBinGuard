"""T3：应用控制器（与 UI 无关的逻辑层）。

把配置、日志、T1 服务、T2 监控器组装成一个可被托盘界面调用的对象。
本模块**不导入** pystray / tkinter，因此完全可以在无 GUI 环境下单元测试。

关键约定：

- 所有耗时操作（立即检查、清理前复核与清空）都通过单线程执行器在**后台**运行，
  托盘回调只负责派发，不会阻塞 UI；
- 启用自动清理必须先通过“不可逆删除”风险确认（仅首次需要）；
- 达到阈值后的**清理前确认弹窗**由 UI 主线程创建；监控线程只投递请求，
  用户决定再回到后台线程执行“复核 -> 清理”，绝不在 UI 线程调用清空 API；
- 任何异常都写入日志并通过 ``last_error`` 暴露，不静默吞掉。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from . import autostart, paths, shortcuts
from .clean_history import CleanRecord
from .config import Config, save_config
from .confirmation import (
    CleanConfirmationRequest,
    ConfirmationDecision,
    ConfirmationRequester,
)
from .interfaces import MonitorState
from .logging_setup import get_logger
from .recycle_bin_monitor import MonitorStatus, RecycleBinMonitor
from .recycle_bin_service import RecycleBinService

logger = get_logger()

#: 启用自动清理前必须向用户展示的风险说明（不可逆删除）。
RISK_NOTICE_TITLE = "启用自动清理前请确认"
RISK_NOTICE = (
    "启用自动清理后，一旦回收站项目数达到阈值，程序会立即清空回收站。\n\n"
    "该操作不可撤销：你之前手动删除、可能还想恢复的文件也会一并被删除。\n\n"
    "请确认你理解并接受这一风险。"
)

#: 状态标签（PRD 要求托盘能够明确区分这些状态）。
LABEL_MONITORING = "正在监控"
LABEL_DRY_RUN = "试运行（只记录，不清空）"
LABEL_AUTO_ON = "自动清理已启用"
LABEL_AUTO_OFF = "自动清理已关闭"
LABEL_INCOMPLETE = "统计不完整"
LABEL_PAUSED = "已暂停"
LABEL_CLEAN_FAILED = "清理失败"
LABEL_AWAITING_CONFIRMATION = "等待确认清理"


class RiskConfirmationRequired(RuntimeError):
    """启用自动清理前需要用户先确认不可逆删除风险。"""


class AppController:
    """RecycleBinGuard 应用控制器。"""

    def __init__(
        self,
        config: Config,
        *,
        service: RecycleBinService | None = None,
        monitor: RecycleBinMonitor | None = None,
        config_path: str | Path | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self.config_path = Path(config_path) if config_path else paths.config_path()
        self.log_path = Path(log_path) if log_path else paths.log_file_path()

        self.service = service if service is not None else RecycleBinService()
        self.monitor = (
            monitor if monitor is not None else RecycleBinMonitor(self.service, config)
        )
        if self.monitor.config is not config:
            raise ValueError("monitor 必须与控制器共享同一个 Config 对象")

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="RBG-Action")
        self._lock = threading.RLock()
        self._shutdown = False

        #: 供 UI 注册的变更回调（可能在后台线程被调用）。
        self.on_status: Callable[[MonitorStatus], None] | None = None
        #: 供 UI 注册的错误回调（可能在后台线程被调用）。
        self.on_error: Callable[[str], None] | None = None
        #: 供 UI 注册的“清理确认弹窗”回调（可能在后台线程被调用）。
        #: UI 负责把它投递到主线程；未注册时达到阈值只会记录，绝不清理。
        self.on_confirmation_request: Callable[[CleanConfirmationRequest], None] | None = None

        # 监控器通过控制器向 UI 提交确认请求：这是“后台线程 -> UI 主线程”的唯一入口。
        self.monitor.set_confirmation_requester(self)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """启动后台监控，并自愈开机启动项。"""
        self.sync_start_on_login()
        self.monitor.start()

    def shutdown(self) -> None:
        """停止监控与后台执行器，不残留任务、不残留待确认的清理请求。"""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        # 先作废待确认请求，再停监控：退出后即便弹窗才回传“立即清理”也不执行。
        self.abort_pending_confirmation("程序退出")
        self.monitor.stop()
        self._executor.shutdown(wait=False, cancel_futures=True)
        logger.info("控制器已关闭")

    # ------------------------------------------------------------------
    # 清理前确认
    # ------------------------------------------------------------------

    def request_clean_confirmation(self, request: CleanConfirmationRequest) -> None:
        """实现 :class:`~recyclebingguard.confirmation.ConfirmationRequester`。

        监控线程调用：把弹窗请求交给 UI（立即返回，绝不阻塞监控线程）。
        """
        handler = self.on_confirmation_request
        if handler is None:
            logger.error(
                "未注册确认弹窗回调，本轮取消清理：token=%d 项目数=%d",
                request.token,
                request.total_items,
            )
            self.submit_confirmation(ConfirmationDecision.ABORTED, request.token)
            return
        try:
            handler(request)
        except Exception:
            logger.exception("投递清理确认请求失败，本轮取消清理")
            self.submit_confirmation(ConfirmationDecision.ABORTED, request.token)

    def submit_confirmation(self, decision: ConfirmationDecision, token: int) -> None:
        """回传确认弹窗的最终决定；复核与清理在后台线程执行，不阻塞 UI。"""
        with self._lock:
            if self._shutdown:
                logger.warning(
                    "控制器已关闭，丢弃清理确认：token=%d 决定=%s", token, decision.value
                )
                return
            self._executor.submit(self._run_confirmation, decision, token)

    def abort_pending_confirmation(self, reason: str) -> None:
        """作废当前待确认的清理请求（退出 / 暂停 / 主动取消）。"""
        self.monitor.abort_pending_confirmation(reason)

    def pending_confirmation_token(self) -> int | None:
        """当前待确认请求的 token（UI 可据此判断是否已有弹窗）。"""
        return self.monitor.pending_confirmation_token

    def _run_confirmation(self, decision: ConfirmationDecision, token: int) -> None:
        try:
            self.monitor.resolve_confirmation(token, decision)
        except Exception as exc:  # 不静默
            message = f"处理清理确认失败：{exc!r}"
            logger.exception(message)
            if self.on_error is not None:
                self.on_error(message)
        finally:
            status = self.status()
            if self.on_status is not None:
                try:
                    self.on_status(status)
                except Exception:  # pragma: no cover
                    logger.exception("状态回调异常")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def status(self) -> MonitorStatus:
        """当前监控状态快照。"""
        return self.monitor.status()

    def state_labels(self, status: MonitorStatus | None = None) -> list[str]:
        """把状态翻译成 UI 标签列表，明确区分各种运行形态。"""
        status = status if status is not None else self.status()
        labels: list[str] = []

        labels.append(LABEL_PAUSED if status.state is MonitorState.PAUSED else LABEL_MONITORING)
        if status.pending_confirmation or status.state is MonitorState.AWAITING_CONFIRMATION:
            labels.append(LABEL_AWAITING_CONFIRMATION)

        if status.dry_run:
            labels.append(LABEL_DRY_RUN)
        labels.append(LABEL_AUTO_ON if status.auto_clean_enabled else LABEL_AUTO_OFF)

        scan = status.last_scan
        if scan is not None and not scan.is_complete:
            labels.append(LABEL_INCOMPLETE)
        if status.state is MonitorState.ERROR:
            labels.append(LABEL_CLEAN_FAILED)
        return labels

    def state_summary(self, status: MonitorStatus | None = None) -> str:
        """单行状态摘要。"""
        return " · ".join(self.state_labels(status))

    def is_paused(self) -> bool:
        return self.monitor.state is MonitorState.PAUSED

    # -- 清理历史（持久化，重启后仍在）--

    def last_clean_record(self) -> CleanRecord | None:
        """最近一次**执行过**的清理（含失败），没有则为 ``None``。"""
        return self.monitor.last_clean_record()

    def last_successful_clean(self) -> CleanRecord | None:
        """最近一次**成功**的清理。"""
        return self.monitor.last_successful_clean()

    def recent_cleans(self, limit: int = 20) -> list[CleanRecord]:
        """最近若干条清理记录（最新在前）。"""
        return self.monitor.history.load()[:limit]

    def last_clean_summary(self) -> str:
        """状态窗口用的一行摘要；无记录时给出明确文案。"""
        record = self.last_clean_record()
        if record is None:
            return "尚无清理记录"
        if record.succeeded:
            return record.describe()

        success = self.last_successful_clean()
        if success is None:
            return f"{record.describe()}（从未成功清理过）"
        return f"{record.describe()}（上次成功：{success.format_time()}）"

    def recent_logs(self, limit: int = 200) -> list[str]:
        """读取最近若干行日志（不记录文件内容，只回显日志行）。"""
        if not self.log_path.exists():
            return []
        try:
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            logger.exception("读取日志失败")
            return []
        return [line.rstrip("\r\n") for line in lines[-limit:]]

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------

    def request_check(self, done: Callable[[MonitorStatus], None] | None = None) -> None:
        """派发一次“立即检查”到后台线程，避免阻塞 UI。"""
        with self._lock:
            if self._shutdown:
                return
            self._executor.submit(self._run_check, done)

    def _run_check(self, done: Callable[[MonitorStatus], None] | None) -> None:
        try:
            self.monitor.check_now()
        except Exception as exc:  # 不静默：记录并上报
            message = f"立即检查失败：{exc!r}"
            logger.exception(message)
            if self.on_error is not None:
                self.on_error(message)
        finally:
            status = self.status()
            if done is not None:
                try:
                    done(status)
                except Exception:  # pragma: no cover - 回调异常不影响控制器
                    logger.exception("检查完成回调异常")
            if self.on_status is not None:
                try:
                    self.on_status(status)
                except Exception:  # pragma: no cover
                    logger.exception("状态回调异常")

    def toggle_pause(self) -> bool:
        """暂停/恢复监控，返回切换后是否处于暂停状态。"""
        if self.monitor.state is MonitorState.PAUSED:
            self.monitor.resume()
        else:
            self.monitor.pause()
        return self.is_paused()

    # -- 自动清理开关 --

    def needs_risk_confirmation(self) -> bool:
        """启用自动清理前是否还需要风险确认（仅在首次需要）。"""
        return not self.config.auto_clean_confirmed

    def set_auto_clean_enabled(
        self, enabled: bool, *, risk_acknowledged: bool = False
    ) -> None:
        """启用/关闭自动清理并持久化配置。

        :param enabled: 目标状态。
        :param risk_acknowledged: 是否已向用户展示了不可逆删除风险并获得确认。
        :raises RiskConfirmationRequired: 首次启用但未确认风险。
        """
        if enabled and self.needs_risk_confirmation() and not risk_acknowledged:
            raise RiskConfirmationRequired(
                "首次启用自动清理前必须向用户展示不可逆删除风险说明"
            )

        if enabled and not self.config.auto_clean_confirmed:
            self.config.auto_clean_confirmed = True
            logger.warning("用户已确认不可逆删除风险")

        self.monitor.set_auto_clean_enabled(enabled)
        self._persist()

    def toggle_auto_clean(self, *, risk_acknowledged: bool = False) -> bool:
        """翻转自动清理开关，返回切换后的状态。"""
        target = not self.config.auto_clean_enabled
        self.set_auto_clean_enabled(target, risk_acknowledged=risk_acknowledged)
        return self.config.auto_clean_enabled

    def set_dry_run(self, dry_run: bool) -> None:
        """切换试运行模式并持久化。"""
        self.monitor.set_dry_run(dry_run)
        self._persist()

    def toggle_dry_run(self) -> bool:
        target = not self.config.dry_run
        self.set_dry_run(target)
        return target

    # -- 打开入口（热键 / 快捷方式）--

    def toggle_hotkey(self) -> bool:
        """开关全局热键（Ctrl + Alt + R），返回开关后的状态。"""
        self.config.hotkey_enabled = not self.config.hotkey_enabled
        self._persist()
        return self.config.hotkey_enabled

    def install_shortcuts(self) -> list[Path]:
        """创建开始菜单 / 桌面快捷方式，返回成功创建的路径。"""
        try:
            created = shortcuts.install_shortcuts()
        except shortcuts.ShortcutUnsupportedError as exc:
            logger.warning("%s", exc)
            return []
        except Exception:  # 不静默
            logger.exception("创建快捷方式失败")
            return []
        logger.info("已创建 %d 个快捷方式", len(created))
        return created

    # -- 开机启动 --

    def is_start_on_login_enabled(self) -> bool:
        """开机启动是否已生效（以注册表实际状态为准）。"""
        try:
            return autostart.is_enabled()
        except autostart.AutostartUnsupportedError:
            return False

    def set_start_on_login(self, enabled: bool) -> bool:
        """启用/关闭开机启动并持久化配置，返回设置后的状态。"""
        try:
            state = autostart.set_enabled(enabled)
        except autostart.AutostartUnsupportedError:
            logger.warning("当前平台不支持开机启动设置")
            return self.is_start_on_login_enabled()

        self.config.start_on_login = state
        self._persist()
        return state

    def sync_start_on_login(self) -> None:
        """自愈：配置要求开机启动但注册表项缺失（如崩溃残留/被清理）时补写。"""
        try:
            if self.config.start_on_login and not autostart.is_enabled():
                logger.warning("配置要求开机启动但启动项缺失，正在恢复")
                autostart.enable()
            elif not self.config.start_on_login and autostart.is_enabled():
                logger.warning("配置已关闭开机启动但启动项仍存在，正在移除")
                autostart.disable()
        except autostart.AutostartUnsupportedError:
            pass
        except Exception:  # 自愈失败不影响主流程，但必须留痕
            logger.exception("同步开机启动状态失败")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        try:
            save_config(self.config, self.config_path)
        except Exception:  # 配置写入失败不能影响程序运行，但必须留痕
            logger.exception("配置持久化失败")

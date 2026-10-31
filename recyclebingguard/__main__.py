"""程序入口。

职责：解析命令行、加载配置、初始化日志、保证单实例、启动托盘应用。
重量级依赖（pystray / tkinter）只在真正需要界面时才导入，便于
``--check-once`` 这类诊断命令在无 GUI 场景下运行。
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from . import APP_NAME, __version__, paths
from .config import Config, ConfigError, load_config, save_config
from .logging_setup import setup_logging, shutdown_logging
from .single_instance import SingleInstance, request_wake

EXIT_OK = 0
#: 保留：配置损坏时现在会自动备份并回退默认配置（见 ``_load_config_with_recovery``），
#: 正常路径不再返回该退出码，但保留常量以免破坏既有脚本对退出码的引用。
EXIT_CONFIG_ERROR = 2
EXIT_ALREADY_RUNNING = 3
EXIT_UNSUPPORTED = 4

#: CLI 文本输出去向（默认直接打印到标准输出）。
Emit = Callable[[str], None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Windows 10 回收站后台监控与自动清理工具",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="在程序目录写入一份默认（安全）配置后退出",
    )
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="只做一次统计与清理预览后退出（不启动托盘，也不清空任何东西）",
    )
    parser.add_argument(
        "--examples",
        action="store_true",
        help="配合 --check-once：额外列出将被清理的示例路径",
    )
    parser.add_argument(
        "--console-log",
        action="store_true",
        help="同时把日志输出到标准错误（调试用）",
    )
    parser.add_argument(
        "--autostart",
        choices=["on", "off", "status"],
        help="设置 / 查询当前用户开机启动项后退出",
    )
    parser.add_argument(
        "--start-minimized",
        action="store_true",
        help="只驻留托盘，不弹出状态窗口（开机自启时使用）",
    )
    parser.add_argument(
        "--install-shortcuts",
        action="store_true",
        help="创建开始菜单与桌面快捷方式（当前用户范围）",
    )
    parser.add_argument(
        "--remove-shortcuts",
        action="store_true",
        help="删除本程序创建的快捷方式",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="移除开机启动项与快捷方式（默认保留配置与日志）",
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="配合 --uninstall：同时删除配置与日志目录",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help=(
            "把命令行输出同时追加写入指定文件"
            "（打包成无控制台的 GUI 程序时用于排查，例如 --check-once --report out.txt）"
        ),
    )
    parser.add_argument(
        "--replace-target",
        metavar="PATH",
        help="（内部）升级换包时使用：新实例把自身覆盖到 PATH 指向的旧 EXE",
    )
    return parser


@contextmanager
def _open_report(path: str | None) -> Iterator[Emit | None]:
    """打开 ``--report`` 指定的文件，返回写入函数；未指定时返回 ``None``。"""
    if not path:
        yield None
        return

    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    handle = target.open("a", encoding="utf-8")
    try:

        def write(text: str = "") -> None:
            handle.write(f"{text}\n")
            handle.flush()

        yield write
    finally:
        handle.close()


def _make_emit(report: Emit | None) -> Emit:
    """构造 CLI 输出函数：始终打印到标准输出（可能是 os.devnull），可选再写文件。"""
    if report is None:
        return print

    def emit(text: str = "") -> None:
        print(text)
        report(text)

    return emit


def _warn_already_running() -> None:
    """提示已有实例：优先弹窗，失败则退回控制台输出。"""
    message = f"{APP_NAME} 已在运行，请查看系统托盘图标。"
    try:
        import tkinter as tk
        from tkinter import messagebox

        from . import assets

        root = tk.Tk()
        root.withdraw()
        icon = assets.load_icon(assets.MAX_ICON_SIZE)
        if icon is not None:
            from PIL import ImageTk

            # 局部变量持有引用，否则 PhotoImage 被回收后弹窗会显示空白图标
            photo: Any = ImageTk.PhotoImage(icon)
            root.iconphoto(True, photo)
        messagebox.showinfo(APP_NAME, message, parent=root)
        root.destroy()
    except Exception:
        print(f"[{APP_NAME}] {message}", file=sys.stderr)


def _run_check_once(emit: Emit, config: Config, *, show_examples: bool = False) -> int:
    from .clean_preview import build_preview
    from .garbage_rules import RecycleReason
    from .recycle_bin_service import RecycleBinService

    result = RecycleBinService().scan()
    emit(f"{APP_NAME} 统计结果（只读）")
    emit(f"  项目总数   : {result.total_items}")
    emit(f"  占用空间   : {result.total_size_bytes} 字节")
    emit(f"  查询时间   : {result.queried_at:%Y-%m-%d %H:%M:%S}")
    emit(f"  统计完整   : {result.is_complete}")
    for disk in result.disks:
        state = "OK" if disk.ok else f"失败({disk.error})"
        emit(f"    {disk.root:<5} {disk.kind.value:<9} {state} items={disk.num_items}")
    if result.failed_roots:
        emit(f"  失败磁盘   : {list(result.failed_roots)}")
    if result.error:
        emit(f"  扫描错误   : {result.error}")

    preview = build_preview(config)
    summary = preview.summary
    emit("")
    emit(f"清理预览（只读，不删除任何东西）  模式={config.clean_scope}")
    emit(f"  回收站顶层条目 : {summary.total_items}（其中不透明 {summary.opaque_items}）")
    emit(f"  递归文件条目数 : {summary.total_entries}")
    emit(f"  判定为垃圾     : {summary.garbage_items} 项 / {summary.garbage_entries} 个文件")
    emit(f"  将会保留       : {summary.keep_items} 项 / {summary.keep_entries} 个文件")
    if summary.zone_waiting_items:
        remaining = max(
            0,
            min(
                config.zone_min_items - summary.zone_waiting_items,
                config.zone_min_items - summary.zone_waiting_entries,
            ),
        )
        emit(
            f"  可丢区待触发   : {summary.zone_waiting_items} 项 / "
            f"{summary.zone_waiting_entries} 个文件"
            f"（门槛 {config.zone_min_items}，还差 {remaining}）"
        )
    if config.discard_zones:
        emit(f"  可丢区目录     : {', '.join(config.discard_zones)}")
    emit(f"  阈值口径计数   : {preview.counted_entries(config)} / {config.threshold_items}")
    emit(f"  会触发清理     : {preview.should_trigger(config)}")
    if not preview.index.is_complete:
        emit(f"  索引不完整     : 失败磁盘={list(preview.index.failed_roots)}")

    reasons = preview.reason_counts()
    if reasons:
        emit("  判定原因分布   :")
        for reason in RecycleReason:
            count = reasons.get(reason)
            if count:
                emit(f"    {reason.value:<16} {count}")

    if show_examples:
        examples = preview.examples(limit=30)
        if examples:
            emit("  将被清理的示例 :")
            for path, reason, pattern in examples:
                suffix = f"（命中 {pattern}）" if pattern else f"（{reason.value}）"
                emit(f"    {path} {suffix}")
    return EXIT_OK


def _handle_autostart(action: str, emit: Emit, config: Config) -> int:
    from . import autostart

    try:
        if action == "status":
            enabled = autostart.is_enabled()
            command = autostart.current_command()
            emit(f"{APP_NAME} 开机启动：{'已启用' if enabled else '未启用'}")
            if command:
                emit(f"  启动命令：{command}")
            return EXIT_OK
        state = autostart.set_enabled(action == "on")
    except autostart.AutostartUnsupportedError as exc:
        print(f"[{APP_NAME}] {exc}", file=sys.stderr)
        return EXIT_UNSUPPORTED

    # 必须同步回配置文件：否则启动时 sync_start_on_login() 的自愈逻辑
    # 会认为“配置没要求自启但注册表里有”，反过来把启动项删掉。
    try:
        config.start_on_login = state
        save_config(config)
    except ConfigError as exc:
        print(f"[{APP_NAME}] 开机启动已设置，但配置未能同步：{exc}", file=sys.stderr)
    except OSError as exc:
        print(f"[{APP_NAME}] 开机启动已设置，但配置写入失败：{exc}", file=sys.stderr)

    emit(f"{APP_NAME} 开机启动已{'启用' if state else '关闭'}")
    return EXIT_OK


def _handle_shortcuts(install: bool, emit: Emit) -> int:
    from . import shortcuts

    if not install:
        removed = shortcuts.uninstall_shortcuts()
        if removed:
            for path in removed:
                emit(f"[{APP_NAME}] 已删除快捷方式：{path}")
        else:
            emit(f"[{APP_NAME}] 未找到需要删除的快捷方式")
        return EXIT_OK

    try:
        created = shortcuts.install_shortcuts()
    except shortcuts.ShortcutUnsupportedError as exc:
        emit(f"[{APP_NAME}] {exc}")
        return EXIT_UNSUPPORTED
    if not created:
        emit(f"[{APP_NAME}] 快捷方式创建失败，请查看日志")
        return EXIT_UNSUPPORTED
    for path in created:
        emit(f"[{APP_NAME}] 已创建快捷方式：{path}")
    return EXIT_OK


def _handle_uninstall(purge: bool, emit: Emit) -> int:
    from . import autostart, shortcuts

    try:
        autostart.disable()
        emit(f"[{APP_NAME}] 已移除开机启动项")
    except autostart.AutostartUnsupportedError as exc:
        print(f"[{APP_NAME}] {exc}", file=sys.stderr)

    try:
        removed = shortcuts.uninstall_shortcuts()
    except shortcuts.ShortcutUnsupportedError:
        removed = []
    for path in removed:
        emit(f"[{APP_NAME}] 已删除快捷方式：{path}")

    home = paths.app_home()
    if not purge:
        emit(f"[{APP_NAME}] 已保留配置与日志：{home}（如需彻底删除请加 --purge）")
        return EXIT_OK

    # 数据目录现在就是程序目录：绝不能整目录删除，只清理本程序自己的产物。
    # 先释放日志句柄，否则 Windows 上删不掉 logs/。
    shutdown_logging()
    for target in paths.runtime_paths():
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[{APP_NAME}] 删除失败：{target}（{exc}）", file=sys.stderr)
    emit(f"[{APP_NAME}] 已删除配置、日志与缓存：{home}")
    return EXIT_OK


def _ensure_std_streams() -> None:
    """GUI 子系统下 ``sys.stdout`` / ``sys.stderr`` 可能为 ``None``。

    打包成“无控制台”的 EXE 后，从资源管理器双击启动时没有标准输出，
    此时 ``print`` / ``argparse`` 会抛异常。这里兜底到 ``os.devnull``，
    既保证命令行参数（如 ``--version``）不会被输出问题打断，
    也保证程序仍然按 GUI 方式安静启动。
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _apply_replace_target(target: Path) -> None:
    """新实例：把自身（``sys.executable``）覆盖到 ``target``（旧 EXE）。

    旧版本先备份为 ``target.bak``；若旧实例仍占用文件（``PermissionError``），
    最多重试约 10 秒，等待其退出后再覆盖。覆盖失败仅记录错误，本实例仍正常运行。
    """
    log = logging.getLogger(APP_NAME)
    backup = target.with_name(target.name + ".bak")
    for attempt in range(20):
        try:
            if target.exists():
                try:
                    if backup.exists():
                        backup.unlink()
                    shutil.copyfile(target, backup)
                except OSError as exc:
                    log.warning("备份旧版本失败：%s（%s）", backup, exc)
                target.unlink()
            shutil.copyfile(sys.executable, target)
            log.info("已将新版本写入 %s（旧版本备份为 %s）", target, backup)
            return
        except PermissionError:
            if attempt == 0:
                log.info("等待旧实例退出以覆盖 %s", target)
            time.sleep(0.5)
    log.error("覆盖 %s 失败：旧实例可能仍占用文件", target)


def main(argv: list[str] | None = None) -> int:
    _ensure_std_streams()
    args = build_parser().parse_args(argv)

    with _open_report(args.report) as report:
        emit = _make_emit(report)
        return _dispatch(args, emit)


def _quarantine_config(path: Path) -> Path | None:
    """把损坏的配置文件改名备份（``config.json.bad-<时间戳>``）。"""
    try:
        if not path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bad-{stamp}")
        path.replace(backup)
        return backup
    except OSError:
        return None


def _load_config_with_recovery() -> tuple[Config, str | None]:
    """读取配置；损坏时备份原文件、回退默认配置并写入。

    返回 ``(配置, 恢复说明)``；``恢复说明`` 为 ``None`` 表示配置正常。
    这样单一坏文件不会把程序挡在启动之外，同时改动前可追溯（原文件被保留）。
    """
    try:
        return load_config(), None
    except ConfigError as exc:
        backup = _quarantine_config(paths.config_path())
        message = f"配置文件不可用，已回退默认配置：{exc}"
        if backup is not None:
            message += f"（原文件已备份为 {backup.name}）"
        config = Config()
        try:
            save_config(config)
        except OSError:
            pass
        return config, message


def _dispatch(args: argparse.Namespace, emit: Emit) -> int:
    # 配置优先“自愈加载”：即使 config.json 损坏也能启动，并把问题写进日志。
    config, recovery = _load_config_with_recovery()
    logger = setup_logging(config, to_console=args.console_log)
    if recovery:
        logger.warning("%s", recovery)
        emit(f"[{APP_NAME}] {recovery}")

    if args.autostart:
        return _handle_autostart(args.autostart, emit, config)

    if args.install_shortcuts:
        return _handle_shortcuts(install=True, emit=emit)

    if args.remove_shortcuts:
        return _handle_shortcuts(install=False, emit=emit)

    if args.uninstall:
        return _handle_uninstall(args.purge, emit)

    if args.init_config:
        emit(f"[{APP_NAME}] 已写入默认配置：{save_config(config)}")
        return EXIT_OK

    if args.check_once:
        return _run_check_once(emit, config, show_examples=args.examples)

    instance = SingleInstance()
    if not instance.acquire():
        logger.warning("检测到已有实例在运行，本次启动终止")
        # 先尝试“唤醒”已有实例：把它的状态窗口显示出来，这是最好的反馈
        if request_wake():
            logger.info("已请求已有实例显示状态窗口")
        else:
            _warn_already_running()
        return EXIT_ALREADY_RUNNING

    # 升级换包：若本实例由更新机制启动（携带 --replace-target），
    # 先把自身覆盖到旧 EXE，再作为活动实例正常运行。
    if args.replace_target:
        _apply_replace_target(Path(args.replace_target))

    try:
        from .app import AppController
        from .tray import run_tray

        controller = AppController(config)
        logger.info(
            "%s %s 启动：阈值=%d 间隔=%ds 自动清理=%s",
            APP_NAME,
            __version__,
            config.threshold_items,
            config.poll_interval_seconds,
            config.auto_clean_enabled,
        )

        def on_apply_update(new_exe: Path) -> None:
            """自动升级：释放单实例锁、启动新 EXE（由其覆盖旧文件）、本实例退出。"""
            instance.release()
            try:
                subprocess.Popen([str(new_exe), "--replace-target", str(Path(sys.executable))])
            except OSError:
                logger.exception("启动新版本失败")
                return
            logger.info("已启动新版本，本实例即将退出以释放文件锁")
            os._exit(0)

        run_tray(
            controller,
            start_minimized=args.start_minimized,
            on_apply_update=on_apply_update,
        )
    except Exception:
        logger.exception("程序异常退出")
        raise
    finally:
        instance.release()
        logger.info("%s 已退出", APP_NAME)
    return EXIT_OK


def crash_log_path() -> Path:
    """崩溃日志位置（与配置、日志同目录）。"""
    return paths.crash_log_path()


def _write_crash_log(exc: BaseException) -> Path | None:
    """把未捕获异常写入崩溃日志；打包成 GUI 后这是唯一的线索来源。"""
    try:
        paths.ensure_app_home()
        target = crash_log_path()
        with target.open("a", encoding="utf-8") as handle:
            handle.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} {APP_NAME} {__version__} ---\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=handle)
        return target
    except Exception:  # pragma: no cover - 连日志都写不了就只能放弃
        return None


def entry(argv: list[str] | None = None) -> int:
    """真正的入口：捕获未处理异常并落盘崩溃日志，绝不静默退出。"""
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 顶层兜底
        target = _write_crash_log(exc)
        if target is not None:
            print(f"[{APP_NAME}] 发生未处理异常，详情见：{target}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(entry())

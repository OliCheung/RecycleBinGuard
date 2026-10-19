"""演示 / 目视验证：单独弹出「清理前确认」弹窗。

**这个脚本不会清空回收站**——它只实例化弹窗控件，不接 AppController、
不调用 ``RecycleBinService.empty()``，所以无论点「立即清理」还是「取消」，
都只是在日志里记录一个决定。

用法::

    python tools/demo_confirm_dialog.py           # 10 秒倒计时（与生产默认一致）
    python tools/demo_confirm_dialog.py 0         # 不倒计时，悬着等你点（最多 45 秒）

自检结果写入 ``%TEMP%\\rbg_confirm_demo.log``。
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recyclebingguard.appearance import resolve_theme  # noqa: E402
from recyclebingguard.confirm_dialog import CleanConfirmDialog  # noqa: E402
from recyclebingguard.confirmation import (  # noqa: E402
    CleanConfirmationRequest,
    ConfirmationDecision,
)
from recyclebingguard.recycle_bin_service import RecycleBinService  # noqa: E402
from recyclebingguard.theme import ThemeManager  # noqa: E402

import tempfile  # noqa: E402

LOG = Path(tempfile.gettempdir()) / "rbg_confirm_demo.log"
#: 等待模式下的最长等待秒数，避免留下没人管的孤儿窗口。
WAIT_MODE_TIMEOUT_S = 45


def write(text: str) -> None:
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{text}\n")


def main() -> int:
    LOG.write_text("", encoding="utf-8")
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 10

    # 只读统计：让弹窗里显示的项目数是真实数字
    scan = RecycleBinService().scan()
    write(
        f"真实统计：items={scan.total_items} "
        f"size={scan.total_size_bytes} complete={scan.is_complete}"
    )

    root = tk.Tk()
    root.withdraw()
    theme = ThemeManager(root, mode=resolve_theme("auto"))

    request = CleanConfirmationRequest(
        token=1,
        total_items=scan.total_items,
        threshold_items=max(1, scan.total_items),  # 演示：假装刚好达标
        timeout_seconds=timeout,  # 0 = 不自动倒计时，一直等用户决定
    )

    result: list[str] = []

    def on_decision(token: int, decision: ConfirmationDecision) -> None:
        result.append(f"token={token} decision={decision.value}")
        write(f"决定：token={token} decision={decision.value}")

    dialog = CleanConfirmDialog(root, request, on_decision, theme=theme, animate=True)
    write(
        f"弹窗已显示：title={dialog.window.title()} "
        f"countdown_enabled={request.countdown_enabled}"
    )

    def sample(seconds: int) -> None:
        write(
            f"[{seconds}s] viewable={dialog.window.winfo_viewable()} "
            f"remaining={dialog.remaining_seconds} decided={dialog.decision}"
        )

    root.after(1500, lambda: sample(2))
    root.after(4500, lambda: sample(5))

    def finish() -> None:
        write(f"最终：{result or '未产生决定（用户未点击）'}")
        write("回收站未被清空（演示未调用 empty）")
        try:
            root.quit()
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            pass

    if timeout > 0:
        # 倒计时版：无人点击则归零 -> 降下动画 -> TIMEOUT -> 收工
        root.after((timeout + 6) * 1000, finish)
    else:
        # 等待版：弹窗悬着等用户亲手点，超时则作废窗口
        write(f"等待用户点击（不倒计时，最多 {WAIT_MODE_TIMEOUT_S} 秒）")

        def give_up() -> None:
            if dialog.decision is None:
                write(f"{WAIT_MODE_TIMEOUT_S} 秒无人点击，演示窗口即将关闭")
                dialog.abort("演示超时")
            root.after(1000, finish)

        root.after(WAIT_MODE_TIMEOUT_S * 1000, give_up)

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

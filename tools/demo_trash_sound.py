"""试听清理音效（**不会清空回收站**）。

直接调用 :mod:`recyclebingguard.sound`，不经过监控与清空流程，
所以听到的就是清理成功后那一声，但**没有任何文件会被删除**。

用法::

    python tools/demo_trash_sound.py          # 播一次
    python tools/demo_trash_sound.py 3        # 连播 3 次（感受一致性）
    python tools/demo_trash_sound.py --save   # 顺带导出 WAV 到 %TEMP% 便于分享

非 Windows 平台只会生成 WAV，不会发声。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recyclebingguard.sound import (  # noqa: E402
    is_supported,
    play_trash_sound,
    trash_sound_wav,
)


def main(argv: list[str]) -> int:
    save = "--save" in argv
    repeats = next((int(arg) for arg in argv if arg.isdigit()), 1)

    import io
    import wave as _wave

    data = trash_sound_wav()
    with _wave.open(io.BytesIO(data), "rb") as wf:
        seconds = wf.getnframes() / wf.getframerate() if wf.getframerate() else 0.0
        rate = wf.getframerate()
    print(f"打包音效：{seconds:.2f}s @ {rate}Hz 单声道 16-bit，"
          f"{len(data):,} 字节（来自 assets/trash.wav，已缓存）")
    print(f"平台支持：{is_supported()}（False 时只生成文件，不发声）")

    if save:
        target = Path(tempfile.gettempdir()) / "rbg_empty_trash.wav"
        target.write_bytes(data)
        print(f"已导出：{target}")

    for index in range(max(1, repeats)):
        started = time.perf_counter()
        ok = play_trash_sound(wait=True)
        elapsed = time.perf_counter() - started
        print(f"[{index + 1}/{repeats}] 播放={ok} 耗时={elapsed:.2f}s")

    print("提示：这一声就是清理成功后播放的音效；本次演示未调用清空 API。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

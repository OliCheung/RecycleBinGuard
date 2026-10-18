"""由 ``recyclebingguard/assets/icon.png`` 生成打包 / 快捷方式用的 .ico。

一张 ICO 里放多个尺寸，各自用最合适的画法：

- **64 及以上**：原图（不锈钢原色），细节最丰富，用于大图标视图；
- **48 及以下**：加深成深钢灰，否则浅色背景（资源管理器默认）上会发飘、糊成一片。

这两个画法与托盘图标共用 :func:`recyclebingguard.tray.create_icon_image`，
保证全项目只有一套图标资源。

用法::

    python tools/make_icon.py [输出路径]

默认输出到 ``packaging/recyclebingguard.ico``。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from recyclebingguard.tray import create_icon_image  # noqa: E402

#: Windows 图标常用尺寸（从小到大写入）。
SIZES = [16, 24, 32, 48, 64, 128, 256]

#: 小尺寸需要更高的对比度：深钢灰在浅色 / 深色背景上都还看得见。
SMALL_SIZE_COLOR = "#7A8390"

#: 小于该边长就改用加深配色。
SMALL_SIZE_THRESHOLD = 64


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else ROOT / "packaging" / "recyclebingguard.ico"
    target.parent.mkdir(parents=True, exist_ok=True)

    frames = []
    for size in SIZES:
        color = None if size >= SMALL_SIZE_THRESHOLD else SMALL_SIZE_COLOR
        frames.append(create_icon_image(color, size=size))

    # 第一张作为主图（最大尺寸），其余通过 append_images 逐档提供，
    # 避免 Pillow 用同一种画法把所有尺寸统一缩放。
    largest = frames[-1]
    largest.save(target, format="ICO", sizes=[(s, s) for s in SIZES], append_images=frames[:-1])
    print(f"已生成图标：{target}（{', '.join(str(s) for s in SIZES)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

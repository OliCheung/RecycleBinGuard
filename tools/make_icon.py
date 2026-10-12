"""生成打包用的 .ico 文件。

图标绘制逻辑与托盘图标共用 :func:`recyclebingguard.tray.create_icon_image`，
避免出现两份不一致的图标资源。

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

#: Windows 图标常用尺寸。
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else ROOT / "packaging" / "recyclebingguard.ico"
    target.parent.mkdir(parents=True, exist_ok=True)

    base = create_icon_image(size=256)
    base.save(target, format="ICO", sizes=SIZES)
    print(f"已生成图标：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

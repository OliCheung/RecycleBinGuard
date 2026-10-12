"""构建 RecycleBinGuard 独立 EXE（目录式）。

步骤：

1. 运行单元测试（失败即中止）；
2. 生成 ``packaging/recyclebingguard.ico``；
3. 调用 PyInstaller 按 ``packaging/RecycleBinGuard.spec`` 构建；
4. 冒烟验证 ``--version``。

用法::

    python build.py            # 完整构建
    python build.py --skip-tests
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "packaging" / "RecycleBinGuard.spec"
EXE = ROOT / "dist" / "RecycleBinGuard" / "RecycleBinGuard.exe"


def run(step: str, args: list[str]) -> None:
    print(f"==> {step}")
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"步骤失败（退出码 {result.returncode}）：{' '.join(args)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 RecycleBinGuard EXE")
    parser.add_argument("--skip-tests", action="store_true", help="跳过测试步骤")
    args = parser.parse_args(argv)

    if not args.skip_tests:
        run("1/4 运行测试", [sys.executable, "-m", "pytest", "-q"])

    run("2/4 生成图标", [sys.executable, str(ROOT / "tools" / "make_icon.py")])

    run(
        "3/4 PyInstaller 构建",
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
    )

    if not EXE.exists():
        raise SystemExit(f"未找到构建产物：{EXE}")

    run("4/4 冒烟验证", [str(EXE), "--version"])

    print()
    print(f"构建完成：{EXE}")
    print("提示：--check-once 会读取真实回收站统计，请手动运行验证；")
    print("      托盘界面请双击 EXE 或运行 --init-config 后再启动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

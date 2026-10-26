"""构建 RecycleBinGuard 独立 EXE（单文件 onefile）。

步骤：

1. 运行单元测试（失败即中止）；
2. 生成 ``packaging/recyclebingguard.ico``；
3. 调用 PyInstaller 按 ``packaging/RecycleBinGuard.spec`` 构建（单文件）；
4. 冒烟验证 ``--version``。

用法::

    python build.py                  # 完整构建
    python build.py --skip-tests     # 跳过测试（仅调试构建流程时用）
    python build.py --release        # 构建 + 打 tag + 可选推送到 GitHub

退出码约定（供仓库根目录的 ``一键出包.bat`` 判断成败）::

    0  成功
    1  失败（测试、构建或冒烟验证未通过）
    2  程序正在运行，无法覆盖旧的 EXE
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "packaging" / "RecycleBinGuard.spec"
EXE = ROOT / "dist" / "RecycleBinGuard.exe"
APP_NAME = "RecycleBinGuard"


def run(step: str, args: list[str]) -> None:
    print(f"==> {step}", flush=True)
    result = subprocess.run(args, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"步骤失败（退出码 {result.returncode}）：{' '.join(args)}")


def find_running_instances() -> list[tuple[int, str]]:
    """返回正在运行的 RecycleBinGuard 进程 ``[(pid, 路径)]``。

    用 ``tasklist`` 而不是 psutil：不引入额外依赖，且只在 Windows 上需要。

    PyInstaller 的 onefile 程序会同时存在两个同名进程（bootloader 父进程 +
    真正的应用子进程），两者都要识别出来。
    """
    if os.name != "nt":  # pragma: no cover - 本项目只发 Windows
        return []
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {APP_NAME}.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
        )
    except OSError:  # pragma: no cover - tasklist 不可用
        return []
    found: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        cells = [cell.strip('"') for cell in line.split('","')]
        if len(cells) < 2 or not cells[0].lower().startswith(APP_NAME.lower()):
            continue
        try:
            found.append((int(cells[1].strip('"')), cells[0].strip('"')))
        except ValueError:
            continue
    return found


def ensure_target_writable() -> bool:
    """确认旧 EXE 没被占用；被占用时给出可操作的提示。

    PyInstaller 用 ``--noconfirm`` 会直接覆盖 ``dist/``，但如果程序正在运行，
    Windows 会锁住那个文件，构建到一半才失败、耗时且提示难懂。所以先检查。
    """
    running = find_running_instances()
    if not running:
        return True
    print(f"[{APP_NAME}] 检测到 {len(running)} 个正在运行的实例：", file=sys.stderr)
    for pid, _ in running:
        print(f"  PID {pid}", file=sys.stderr)
    print("请先从托盘菜单「退出程序」，或运行：", file=sys.stderr)
    print(f"  taskkill /IM {APP_NAME}.exe /F", file=sys.stderr)
    return False


def print_summary(version: str, elapsed: float) -> None:
    size_mb = EXE.stat().st_size / (1024 * 1024) if EXE.exists() else 0.0
    print()
    print("=" * 60)
    print(f"  构建完成：{EXE}")
    print(f"  版本    ：{version}")
    print(f"  大小    ：{size_mb:.1f} MB")
    print(f"  耗时    ：{elapsed:.1f} 秒")
    print("=" * 60)
    print("提示：--check-once 会读取真实回收站统计，可手动运行验证；")
    print("      双击 EXE 或运行 --init-config 后即可启动托盘界面。")


def read_version() -> str:
    """从包内读取版本号，避免与 ``recyclebingguard/__init__.py`` 漂移。"""
    init = ROOT / "recyclebingguard" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "unknown"


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def do_release(version: str, *, push: bool) -> int:
    """打 tag（可选推送）。任何一步失败都不影响已经构建好的 EXE。"""
    tag = f"v{version}"
    existing = git("tag", "-l", tag).stdout.strip()
    if existing:
        print(f"[发布] tag {tag} 已存在，跳过打 tag")
    else:
        status = git("status", "--porcelain").stdout.strip()
        if status:
            print("[发布] 工作区有未提交改动，先提交再打 tag：", file=sys.stderr)
            print(status, file=sys.stderr)
            return 1
        git("tag", "-a", tag, "-m", f"{APP_NAME} {tag}")
        print(f"[发布] 已创建 tag {tag}")

    if push:
        git("push", "origin", "HEAD")
        git("push", "origin", tag)
        print(f"[发布] 已推送代码与 tag {tag} 到 origin")
    else:
        print(f"[发布] 未推送。需要发布时运行：git push origin HEAD && git push origin {tag}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"构建 {APP_NAME} EXE")
    parser.add_argument("--skip-tests", action="store_true", help="跳过测试步骤")
    parser.add_argument("--release", action="store_true", help="构建成功后打 tag")
    parser.add_argument("--push", action="store_true", help="配合 --release：同时推送到 origin")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    version = read_version()

    if not ensure_target_writable():
        return 2

    if not args.skip_tests:
        run("1/4 运行测试", [sys.executable, "-m", "pytest", "-q"])

    run("2/4 生成图标", [sys.executable, str(ROOT / "tools" / "make_icon.py")])

    run(
        "3/4 PyInstaller 构建",
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC)],
    )

    if not EXE.exists():
        print(f"未找到构建产物：{EXE}", file=sys.stderr)
        return 1

    run("4/4 冒烟验证", [str(EXE), "--version"])

    print_summary(version, time.perf_counter() - started)

    if args.release:
        return do_release(version, push=args.push)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

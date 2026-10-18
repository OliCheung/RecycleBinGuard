# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件构建 onefile）。

按用户选择采用“单文件（onefile）”：最终产物只有一个 ``dist/RecycleBinGuard.exe``，
便于分发与就地升级（升级机制会把新 EXE 直接覆盖到旧 EXE 自身）。

单文件模式每次启动由 bootloader 解压到临时目录再运行；本项目体量小，
解压开销可接受，换来“一个文件走天下 + 自动升级自替换”的体验。

构建：
    python -m PyInstaller --noconfirm --clean packaging/RecycleBinGuard.spec
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ICON = ROOT / "packaging" / "recyclebingguard.ico"
#: 图标源文件必须在运行时可见：托盘 / 窗口图标是从 assets/icon.png 现算的。
ASSETS = ROOT / "recyclebingguard" / "assets"

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ASSETS), "recyclebingguard/assets")] if ASSETS.is_dir() else [],
    hiddenimports=[
        # 托盘与打包后动态导入的模块，显式声明避免被裁剪
        "pystray._win32",
        "PIL._tkinter_finder",
        "PIL.ImageTk",
        "tkinter",
        "tkinter.ttk",
        "tkinter.scrolledtext",
        "tkinter.messagebox",
        "winreg",
        "msvcrt",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确排除：本工具不联网、不建库、不调用大模型
    excludes=[
        "numpy",
        "scipy",
        "pandas",
        "matplotlib",
        "requests",
        "urllib3",
        "sqlite3",
        "pytest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="RecycleBinGuard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 程序：不弹控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.exists() else None,
)

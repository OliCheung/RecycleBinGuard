# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（目录式构建）。

选择“目录式（onedir）”而不是“单文件（onefile）”：
单文件模式每次启动都要解压到临时目录，启动慢、杀软误报多、出错难排查。
稳定运行一段时间后如有需要再考虑 onefile。

构建：
    python -m PyInstaller --noconfirm --clean packaging/RecycleBinGuard.spec
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
ICON = ROOT / "packaging" / "recyclebingguard.ico"

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # 托盘与打包后动态导入的模块，显式声明避免被裁剪
        "pystray._win32",
        "PIL._tkinter_finder",
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
    [],
    exclude_binaries=True,
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="RecycleBinGuard",
)

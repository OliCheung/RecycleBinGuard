"""PyInstaller 打包入口。

PyInstaller 会把入口脚本当作顶层模块执行，此时包内的相对导入
（``from . import ...``）会失败。因此这里用一个极薄的启动器，
以绝对导入方式调用真正的入口 :func:`recyclebingguard.__main__.entry`。
"""

from __future__ import annotations

import multiprocessing
import sys

if __name__ == "__main__":
    # 打包为 EXE 后必须调用，避免子进程反复启动主程序
    multiprocessing.freeze_support()

    from recyclebingguard.__main__ import entry

    sys.exit(entry())

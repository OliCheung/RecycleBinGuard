"""RecycleBinGuard —— Windows 10 回收站后台监控与自动清理工具。

常规功能已全部实现并测试覆盖：只读统计、阈值判断、清理前确认、
逐盘清空、清理历史、托盘界面、全局热键、开机启动与快捷方式。

设计要点（详见 PRD.md）：

- 默认安全：``auto_clean_enabled=true``（默认开启自动清理，每次清理前仍弹确认框）；
- 统计不完整时永不清理（未知 ≠ 0）；
- 数据（配置 / 日志 / 历史）默认放在程序所在目录，可用
  ``RECYCLEBINGUARD_HOME`` 覆盖。
"""

from __future__ import annotations

__all__ = ["__version__", "APP_NAME"]

__version__ = "0.4.0"

#: 应用名，用于数据目录、日志与启动项命名。
APP_NAME = "RecycleBinGuard"

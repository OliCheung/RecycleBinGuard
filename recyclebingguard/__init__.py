"""RecycleBinGuard —— Windows 10 回收站后台监控与自动清理工具。

当前阶段：项目骨架（配置 / 日志 / 接口定义 / 测试框架）。
核心统计与清空逻辑将在 T1 中实现，见 PRD.md。
"""

from __future__ import annotations

__all__ = ["__version__", "APP_NAME"]

__version__ = "0.1.0"

#: 应用名，用于数据目录、日志与启动项命名。
APP_NAME = "RecycleBinGuard"

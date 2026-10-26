"""垃圾判定的默认模式表。

单独成模块的原因：:mod:`recyclebingguard.config` 需要这些默认值，而
:mod:`recyclebingguard.garbage_rules` 又依赖 ``recycle_bin_index`` -> ``logging_setup``
-> ``config``，直接互相导入会形成循环。本模块**不导入任何项目内模块**，
可以安全地被任意一侧引用。

模式语法：大小写不敏感的 glob，作用于条目被删除前的**原始完整路径**；
两侧的 ``*`` 可省略（匹配时会自动补上）。
"""

from __future__ import annotations

#: 默认的垃圾路径特征：临时目录、依赖目录、构建产物、缓存、临时文件扩展名。
DEFAULT_GARBAGE_PATTERNS: tuple[str, ...] = (
    # 临时目录
    "*\\AppData\\Local\\Temp\\*",
    "*\\Windows\\Temp\\*",
    # 依赖 / 虚拟环境
    "*\\node_modules\\*",
    "*\\.venv\\*",
    "*\\venv\\*",
    "*\\site-packages\\*",
    # Python / 测试缓存
    "*\\__pycache__\\*",
    "*\\.pytest_cache\\*",
    "*\\.ruff_cache\\*",
    "*\\.mypy_cache\\*",
    "*\\.tox\\*",
    # 构建产物
    "*\\dist\\*",
    "*\\build\\*",
    "*\\target\\*",
    "*\\.next\\*",
    "*\\.nuxt\\*",
    "*\\.cache\\*",
    "*\\coverage\\*",
    "*\\.parcel-cache\\*",
    # 包管理器缓存
    "*\\AppData\\Local\\pip\\cache\\*",
    "*\\AppData\\Local\\npm-cache\\*",
    "*\\AppData\\Local\\pnpm-store\\*",
    "*\\AppData\\Local\\Yarn\\Cache\\*",
    "*\\.gradle\\*",
    # 临时 / 备份类单文件
    "*.pyc",
    "*.pyo",
    "*.tmp",
    "*.temp",
    "*.log",
    "*.bak",
    "*.crdownload",
    "*.part",
)

#: 默认的保留白名单：用户手动管理文件的地方，命中即永不清理（优先级最高）。
DEFAULT_KEEP_PATTERNS: tuple[str, ...] = (
    "*\\Desktop\\*",
    "*\\桌面\\*",
    "*\\Documents\\*",
    "*\\文档\\*",
    "*\\Downloads\\*",
    "*\\下载\\*",
    "*\\Pictures\\*",
    "*\\图片\\*",
    "*\\Music\\*",
    "*\\音乐\\*",
    "*\\Videos\\*",
    "*\\视频\\*",
    "*\\OneDrive\\*",
)

#: 默认的「可丢区」目录前缀：删除前位于这些目录下的条目，只要累计数量达到门槛
#: 就整批评为垃圾 —— 判的是**位置**，不看文件类型，因此项目里的临时截图、
#: 调试脚本、构建产物都会被一并覆盖。
#:
#: 注意这是目录前缀包含判定（不是 glob），``D:\VibeCoding`` 会匹配它下面的所有
#: 子目录，但不会误伤 ``D:\VibeCoding-old``。
DEFAULT_DISCARD_ZONES: tuple[str, ...] = ("D:\\VibeCoding",)

__all__ = [
    "DEFAULT_DISCARD_ZONES",
    "DEFAULT_GARBAGE_PATTERNS",
    "DEFAULT_KEEP_PATTERNS",
]

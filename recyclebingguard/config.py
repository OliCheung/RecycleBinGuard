"""配置模型与读写。

默认配置遵循“安全优先”原则：

- ``auto_clean_enabled = False``：首次安装只监控，不自动清理；
- ``dry_run = True``：试运行，只记录“本会触发清理”，绝不调用清空 API。

配置文件为 JSON，默认位于 :func:`recyclebingguard.paths.config_path`。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths


class ConfigError(ValueError):
    """配置内容非法（字段缺失、类型错误、取值越界或 JSON 解析失败）。"""


#: 默认安全配置值，PRD 第 11 节。
DEFAULTS: dict[str, Any] = {
    "threshold_items": 10000,
    "poll_interval_seconds": 180,
    "auto_clean_enabled": False,
    "auto_clean_confirmed": False,
    "dry_run": True,
    "confirm_before_clean": True,
    "confirmation_timeout_seconds": 10,
    "failure_backoff_seconds": 300,
    "start_on_login": False,
    "hotkey_enabled": True,
    "log_max_bytes": 1048576,
    "log_backup_count": 5,
}

_BOOL_FIELDS = frozenset(
    {
        "auto_clean_enabled",
        "auto_clean_confirmed",
        "dry_run",
        "confirm_before_clean",
        "start_on_login",
        "hotkey_enabled",
    }
)

#: 字段 -> (最小值, 最大值)，最大值 None 表示不设上限。
_INT_BOUNDS: dict[str, tuple[int, int | None]] = {
    "threshold_items": (1, None),
    "poll_interval_seconds": (1, None),
    #: 0 = 不自动倒计时（弹窗一直等用户）；负数非法（校验失败即报错，绝不静默自动清理）。
    "confirmation_timeout_seconds": (0, None),
    "failure_backoff_seconds": (0, None),
    "log_max_bytes": (1024, None),
    "log_backup_count": (0, None),
}


@dataclass
class Config:
    """RecycleBinGuard 运行时配置。"""

    threshold_items: int = 10000
    poll_interval_seconds: int = 180
    auto_clean_enabled: bool = False
    #: 用户是否已经看过并确认过“不可逆删除”风险说明（只确认一次）。
    auto_clean_confirmed: bool = False
    dry_run: bool = True
    #: 达到阈值后是否先弹确认窗（True = 先确认，默认开启）。
    confirm_before_clean: bool = True
    #: 确认弹窗倒计时秒数：>0 倒计时归零后自动进入复核；0 = 不自动，弹窗一直等待用户。
    confirmation_timeout_seconds: int = 10
    failure_backoff_seconds: int = 300
    start_on_login: bool = False
    #: 是否启用全局热键（Ctrl + Alt + R）唤出状态窗口。
    hotkey_enabled: bool = True
    log_max_bytes: int = 1048576
    log_backup_count: int = 5

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的字典。"""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """从字典构建配置，未知键忽略，已知键做类型与取值校验。"""
        if not isinstance(data, dict):
            raise ConfigError(f"配置根节点必须是对象，实际为 {type(data).__name__}")

        kwargs: dict[str, Any] = {}
        for key in DEFAULTS:
            if key not in data:
                continue
            kwargs[key] = _coerce(key, data[key])

        cfg = cls(**kwargs)
        cfg.validate()
        return cfg

    # ---- 校验 ----

    def validate(self) -> None:
        """校验字段类型与取值范围，非法时抛出 :class:`ConfigError`。"""
        for name in _BOOL_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise ConfigError(f"{name} 必须是布尔值，实际为 {type(value).__name__}")
        for name, (low, high) in _INT_BOUNDS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError(f"{name} 必须是整数，实际为 {type(value).__name__}")
            if value < low:
                raise ConfigError(f"{name} 不能小于 {low}，实际为 {value}")
            if high is not None and value > high:
                raise ConfigError(f"{name} 不能大于 {high}，实际为 {value}")


def _coerce(key: str, value: Any) -> Any:
    """把 JSON 读入的原始值转成期望类型，失败时抛出 :class:`ConfigError`。"""
    if key in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise ConfigError(f"{key} 必须是布尔值（true/false），实际为 {value!r}")
        return value

    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数，实际为 {value!r}")
    return value


def load_config(path: str | Path | None = None) -> Config:
    """读取配置。

    - 文件不存在时返回默认（安全）配置，并**不**落盘；
    - JSON 损坏或字段非法时抛出 :class:`ConfigError`，避免静默使用错误配置。
    """
    target = Path(path) if path is not None else paths.config_path()
    if not target.exists():
        return Config()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{target}（{exc}）") from exc

    return Config.from_dict(raw)


def save_config(config: Config, path: str | Path | None = None) -> Path:
    """校验并原子写入配置，返回写入路径。"""
    config.validate()
    target = Path(path) if path is not None else paths.config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(config.to_dict(), indent=2, ensure_ascii=False) + "\n"
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(target)
    return target

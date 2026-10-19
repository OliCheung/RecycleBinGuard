"""配置模型与读写。

默认配置遵循“安全优先”原则：

- ``auto_clean_enabled = True``：首次安装即开启自动清理；
  每次真实清理前仍会弹确认框（``confirm_before_clean`` 默认开启），不会静默删除。
- ``dry_run`` 试运行模式已移除：程序要么真清理（经用户确认），要么只监控。

配置文件为 JSON，默认位于 :func:`recyclebingguard.paths.config_path`。
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from .appearance import THEME_AUTO, THEME_MODES
from .interfaces import normalize_root


class ConfigError(ValueError):
    """配置内容非法（字段缺失、类型错误、取值越界或 JSON 解析失败）。"""


#: 默认安全配置值，PRD 第 11 节。
DEFAULTS: dict[str, Any] = {
    "threshold_items": 10000,
    #: 占用大小阈值（字节）；0 = 不启用，只看项目数。
    "threshold_size_bytes": 0,
    "poll_interval_seconds": 600,
    "auto_clean_enabled": True,
    "auto_clean_confirmed": False,
    "auto_update_on_signal": True,
    "confirm_before_clean": True,
    "confirmation_timeout_seconds": 10,
    "failure_backoff_seconds": 300,
    #: 不纳入统计与清空的盘符，如 ``["D:\\"]``；空 = 所有固定磁盘。
    "excluded_roots": [],
    "start_on_login": False,
    "hotkey_enabled": True,
    #: 清理完成后是否播放提示音。
    "clean_sound_enabled": True,
    #: 界面皮肤：``auto``（按系统时间自动）/ ``light`` / ``dark``。
    "theme": "auto",
    "log_max_bytes": 1048576,
    "log_backup_count": 5,
}

_BOOL_FIELDS = frozenset(
    {
        "auto_clean_enabled",
        "auto_clean_confirmed",
        "auto_update_on_signal",
        "confirm_before_clean",
        "start_on_login",
        "hotkey_enabled",
        "clean_sound_enabled",
    }
)

#: 字符串枚举字段 -> 合法取值（顺序即界面切换顺序）。
_STR_CHOICES: dict[str, tuple[str, ...]] = {
    "theme": THEME_MODES,
}

#: 字段 -> (最小值, 最大值)，最大值 None 表示不设上限。
_INT_BOUNDS: dict[str, tuple[int, int | None]] = {
    "threshold_items": (1, None),
    #: 0 = 不启用占用大小阈值。
    "threshold_size_bytes": (0, None),
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
    #: 占用大小阈值（字节）；0 = 不启用。
    threshold_size_bytes: int = 0
    poll_interval_seconds: int = 600
    auto_clean_enabled: bool = True
    #: 用户是否已经看过并确认过“不可逆删除”风险说明（只确认一次）。
    auto_clean_confirmed: bool = False
    #: 收到 GitHub 发布的新版本时是否自动下载并换包（默认开启；关闭则只提示）。
    auto_update_on_signal: bool = True
    #: 达到阈值后是否先弹确认窗（True = 先确认，默认开启）。
    confirm_before_clean: bool = True
    #: 确认弹窗倒计时秒数：>0 倒计时归零后自动进入复核；0 = 不自动，弹窗一直等待用户。
    confirmation_timeout_seconds: int = 10
    failure_backoff_seconds: int = 300
    #: 不纳入统计与清空的盘符（如 ``["D:\\"]``）；空 = 所有固定磁盘。
    excluded_roots: list[str] = field(default_factory=list)
    start_on_login: bool = False
    #: 是否启用全局热键（Ctrl + Alt + R）唤出状态窗口。
    hotkey_enabled: bool = True
    #: 清理完成后是否播放一声提示音（合成的“倒垃圾”音效）。
    clean_sound_enabled: bool = True
    #: 界面皮肤：``auto``（按系统时间自动切换）/ ``light`` / ``dark``。
    theme: str = THEME_AUTO
    log_max_bytes: int = 1048576
    log_backup_count: int = 5

    def __post_init__(self) -> None:
        # 容错：把 "d:" / "D:/" 统一成 "D:\\"（非法项原样保留，交给 validate 报错）。
        if isinstance(self.excluded_roots, list):
            self.excluded_roots = [
                (normalize_root(value) or value) if isinstance(value, str) else value
                for value in self.excluded_roots
            ]
        # 容错：大小写 / 空白不影响取值；非法值由 validate / _coerce 报错。
        if isinstance(self.theme, str):
            self.theme = self.theme.strip().lower()

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的字典。"""
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
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
        self._validate_choices()
        self._validate_excluded_roots()

    def _validate_choices(self) -> None:
        for name, choices in _STR_CHOICES.items():
            value = getattr(self, name)
            if not isinstance(value, str) or value not in choices:
                allowed = " / ".join(choices)
                raise ConfigError(f"{name} 必须是 {allowed} 之一，实际为 {value!r}")

    def _validate_excluded_roots(self) -> None:
        if not isinstance(self.excluded_roots, list):
            raise ConfigError(
                f"excluded_roots 必须是字符串数组，实际为 {type(self.excluded_roots).__name__}"
            )
        normalized: list[str] = []
        for item in self.excluded_roots:
            if not isinstance(item, str):
                raise ConfigError(f"excluded_roots 的每一项都必须是字符串，实际为 {item!r}")
            root = normalize_root(item)
            if not root:
                raise ConfigError(f'excluded_roots 含非法盘符：{item!r}（应形如 "D:\\"）')
            if root not in normalized:
                normalized.append(root)
        self.excluded_roots = normalized


def _coerce(key: str, value: Any) -> Any:
    """把 JSON 读入的原始值转成期望类型，失败时抛出 :class:`ConfigError`。"""
    if key in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise ConfigError(f"{key} 必须是布尔值（true/false），实际为 {value!r}")
        return value

    if key in _STR_CHOICES:
        if not isinstance(value, str):
            raise ConfigError(f"{key} 必须是字符串，实际为 {value!r}")
        candidate = value.strip().lower()
        if candidate not in _STR_CHOICES[key]:
            allowed = " / ".join(_STR_CHOICES[key])
            raise ConfigError(f"{key} 必须是 {allowed} 之一，实际为 {value!r}")
        return candidate

    if key == "excluded_roots":
        if not isinstance(value, list):
            raise ConfigError(f"excluded_roots 必须是字符串数组，实际为 {value!r}")
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
        # utf-8-sig：容忍 Windows 记事本 / PowerShell 5.1 写入的 UTF-8 BOM。
        raw = json.loads(target.read_text(encoding="utf-8-sig"))
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

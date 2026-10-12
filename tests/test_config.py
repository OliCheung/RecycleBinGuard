"""配置模块测试：默认安全值、读写往返、非法输入拒绝。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recyclebingguard.config import Config, ConfigError, load_config, save_config


def test_defaults_are_safe() -> None:
    cfg = Config()
    assert cfg.threshold_items == 10000
    assert cfg.poll_interval_seconds == 180
    assert cfg.auto_clean_enabled is False
    assert cfg.auto_clean_confirmed is False
    assert cfg.dry_run is True
    assert cfg.failure_backoff_seconds == 300
    assert cfg.start_on_login is False
    assert cfg.hotkey_enabled is True


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.json")
    assert cfg == Config()


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    original = Config(threshold_items=5000, auto_clean_enabled=True, dry_run=False)
    save_config(original, target)

    assert json.loads(target.read_text(encoding="utf-8"))["threshold_items"] == 5000
    assert load_config(target) == original


def test_save_leaves_no_tmp_file(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    save_config(Config(), target)
    assert list(tmp_path.iterdir()) == [target]


def test_partial_json_fills_missing_with_defaults(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"threshold_items": 42}), encoding="utf-8")
    cfg = load_config(target)
    assert cfg.threshold_items == 42
    assert cfg.dry_run is True
    assert cfg.auto_clean_enabled is False


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"threshold_items": 10, "future_key": 1}), encoding="utf-8")
    assert load_config(target).threshold_items == 10


def test_corrupt_json_raises(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    target.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON"):
        load_config(target)


def test_non_object_root_raises() -> None:
    with pytest.raises(ConfigError):
        Config.from_dict([1, 2, 3])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"threshold_items": -1}, "threshold_items"),
        ({"threshold_items": 0}, "threshold_items"),
        ({"poll_interval_seconds": 0}, "poll_interval_seconds"),
        ({"failure_backoff_seconds": -5}, "failure_backoff_seconds"),
        ({"log_max_bytes": 10}, "log_max_bytes"),
        ({"log_backup_count": -1}, "log_backup_count"),
        ({"threshold_items": 1.5}, "threshold_items"),
        ({"threshold_items": True}, "threshold_items"),
        ({"dry_run": "yes"}, "dry_run"),
        ({"auto_clean_enabled": 1}, "auto_clean_enabled"),
    ],
)
def test_invalid_values_rejected(payload: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        Config.from_dict(payload)


def test_validate_rejects_hand_built_invalid_config() -> None:
    cfg = Config()
    cfg.threshold_items = -1
    with pytest.raises(ConfigError):
        cfg.validate()
    with pytest.raises(ConfigError):
        save_config(cfg, Path("unused.json"))


def test_to_dict_matches_defaults_keys() -> None:
    assert set(Config().to_dict()) == {
        "threshold_items",
        "poll_interval_seconds",
        "auto_clean_enabled",
        "auto_clean_confirmed",
        "dry_run",
        "confirm_before_clean",
        "confirmation_timeout_seconds",
        "failure_backoff_seconds",
        "start_on_login",
        "hotkey_enabled",
        "log_max_bytes",
        "log_backup_count",
    }

"""配置模块测试：默认安全值、读写往返、非法输入拒绝。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recyclebingguard.config import (
    DEFAULTS,
    Config,
    ConfigError,
    load_config,
    save_config,
)


def test_defaults_are_safe() -> None:
    cfg = Config()
    assert cfg.threshold_items == 10000
    assert cfg.threshold_size_bytes == 0
    assert cfg.poll_interval_seconds == 600
    assert cfg.auto_clean_enabled is True
    assert cfg.auto_clean_confirmed is False
    assert cfg.failure_backoff_seconds == 300
    assert cfg.excluded_roots == []
    assert cfg.start_on_login is False
    assert cfg.hotkey_enabled is True


def test_to_dict_equals_defaults() -> None:
    assert Config().to_dict() == DEFAULTS


def test_config_example_matches_defaults() -> None:
    """config.example.json 必须与默认值完全一致，避免文档漂移。"""
    example = Path(__file__).resolve().parents[1] / "config.example.json"
    assert json.loads(example.read_text(encoding="utf-8")) == DEFAULTS


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "does-not-exist.json")
    assert cfg == Config()


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    original = Config(threshold_items=5000, auto_clean_enabled=True)
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
    assert cfg.auto_clean_enabled is True


def test_bom_prefixed_config_is_accepted(tmp_path: Path) -> None:
    """记事本 / PowerShell 5.1 会写入 UTF-8 BOM，不能因此判为损坏。"""
    target = tmp_path / "config.json"
    target.write_bytes(b"\xef\xbb\xbf" + json.dumps({"threshold_items": 321}).encode("utf-8"))

    assert load_config(target).threshold_items == 321


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
    assert set(Config().to_dict()) == set(DEFAULTS)


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("d:", "D:\\"),
        ("D:/", "D:\\"),
        ("D:\\", "D:\\"),
        (" e: ", "E:\\"),
    ],
)
def test_excluded_roots_are_normalized(raw: str, normalized: str) -> None:
    cfg = Config.from_dict({"excluded_roots": [raw]})
    assert cfg.excluded_roots == [normalized]


def test_excluded_roots_deduplicates() -> None:
    cfg = Config.from_dict({"excluded_roots": ["d:", "D:\\", "D:/"]})
    assert cfg.excluded_roots == ["D:\\"]


@pytest.mark.parametrize(
    "payload",
    [
        {"excluded_roots": "C:"},
        {"excluded_roots": [123]},
        {"excluded_roots": ["not a drive"]},
        {"excluded_roots": ["\\\\server\\share"]},
    ],
)
def test_invalid_excluded_roots_rejected(payload: dict) -> None:
    with pytest.raises(ConfigError, match="excluded_roots"):
        Config.from_dict(payload)


def test_threshold_size_bytes_bounds() -> None:
    assert Config.from_dict({"threshold_size_bytes": 0}).threshold_size_bytes == 0
    with pytest.raises(ConfigError, match="threshold_size_bytes"):
        Config.from_dict({"threshold_size_bytes": -1})

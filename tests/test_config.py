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


# --------------------------------------------------------------------------
# v0.4.0：清理范围与垃圾判定配置
# --------------------------------------------------------------------------


def test_default_scope_is_vibecoding() -> None:
    """默认必须只删垃圾，绝不默认整盘清空。"""
    cfg = Config()
    assert cfg.clean_scope == "vibecoding"
    assert cfg.count_recursive is True
    assert cfg.burst_enabled is True


def test_scope_rejects_unknown_value() -> None:
    with pytest.raises(ConfigError):
        Config.from_dict({"clean_scope": "delete-everything"})


def test_scope_accepts_both_values() -> None:
    assert Config.from_dict({"clean_scope": "everything"}).clean_scope == "everything"
    assert Config.from_dict({"clean_scope": "VIBECODING"}).clean_scope == "vibecoding"


def test_pattern_lists_are_cleaned_and_deduplicated() -> None:
    cfg = Config.from_dict(
        {
            "garbage_path_patterns": [r"  *\build\*  ", r"*\build\*", "", "   "],
            "keep_path_patterns": [r"*\Desktop\*"],
        }
    )
    assert cfg.garbage_path_patterns == [r"*\build\*"]
    assert cfg.keep_path_patterns == [r"*\Desktop\*"]


def test_pattern_lists_reject_non_strings() -> None:
    with pytest.raises(ConfigError):
        Config.from_dict({"garbage_path_patterns": [1, 2]})
    with pytest.raises(ConfigError):
        Config.from_dict({"keep_path_patterns": "not-a-list"})


def test_burst_bounds() -> None:
    with pytest.raises(ConfigError):
        Config.from_dict({"burst_window_seconds": 0})
    with pytest.raises(ConfigError):
        Config.from_dict({"burst_min_items": 0})


def test_count_recursive_must_be_bool() -> None:
    with pytest.raises(ConfigError):
        Config.from_dict({"count_recursive": 1})


def test_old_config_without_new_keys_uses_defaults() -> None:
    """升级兼容：v0.3.0 的配置里没有这些键，必须回落到安全默认值。"""
    legacy = {"threshold_items": 2000, "auto_clean_enabled": True}
    cfg = Config.from_dict(legacy)
    assert cfg.threshold_items == 2000
    assert cfg.clean_scope == "vibecoding"
    assert cfg.count_recursive is True
    assert cfg.garbage_path_patterns


def test_discard_zone_defaults() -> None:
    """可丢区默认只在 VibeCoding，门槛 100（宁可慢，不可误删）。"""
    cfg = Config()
    assert cfg.zone_enabled is True
    assert cfg.discard_zones == [r"D:\VibeCoding"]
    assert cfg.zone_min_items == 100


def test_discard_zones_normalized_like_patterns() -> None:
    cfg = Config.from_dict({"discard_zones": [r"  D:\VibeCoding  ", "", "  "]})
    assert cfg.discard_zones == [r"D:\VibeCoding"]


def test_zone_min_items_zero_disables() -> None:
    assert Config.from_dict({"zone_min_items": 0}).zone_min_items == 0
    with pytest.raises(ConfigError):
        Config.from_dict({"zone_min_items": -1})


def test_default_zone_does_not_cover_other_projects() -> None:
    """默认配置下，mi4-dashboard 这类不在可丢区的项目不受影响。"""
    from recyclebingguard.garbage_rules import matches_zone

    cfg = Config()
    assert matches_zone(r"C:\Users\OLI\mi4-dashboard\a.png", tuple(cfg.discard_zones)) is None
    assert matches_zone(r"D:\VibeCoding\AITutor\a.png", tuple(cfg.discard_zones)) is not None

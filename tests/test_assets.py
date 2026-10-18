"""图标资源测试：``recyclebingguard/assets/icon.png`` 必须存在且可用。

所有图标（托盘 / 窗口 / EXE / 快捷方式）都从这一张图派生，因此它的存在性、
通道格式与尺寸稳定性属于契约：缺了它程序虽然还能跑（有内置兜底画法），
但用户看到的就不是项目图标了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from recyclebingguard import assets


@pytest.fixture(autouse=True)
def _clear_icon_cache():
    """每个用例前后清掉图片缓存，避免 monkeypatch 互相污染。"""
    assets.load_source.cache_clear()
    yield
    assets.load_source.cache_clear()


def test_icon_file_exists() -> None:
    assert assets.icon_file().is_file(), f"缺少图标资源：{assets.icon_file()}"


def test_source_is_square_rgba() -> None:
    source = assets.load_source()
    assert source is not None
    assert source.mode == "RGBA"
    assert source.width == source.height
    # 至少要有 256 的边长，否则放大成 .ico 大图标会糊
    assert source.width >= 256


def test_source_background_is_transparent() -> None:
    source = assets.load_source()
    assert source is not None
    lowest, highest = source.getchannel("A").getextrema()
    assert lowest == 0, "背景应完全透明（否则托盘里会顶着一块白底）"
    assert highest == 255, "网格主体应完全不透明"


def test_load_icon_scales_to_requested_size() -> None:
    icon = assets.load_icon(64)
    assert icon is not None
    assert icon.size == (64, 64)
    assert icon.mode == "RGBA"


def test_load_icon_returns_copy_not_cached_source() -> None:
    """缩放结果不能是缓存对象本身，否则调用方改图会污染全局图标。"""
    source = assets.load_source()
    assert source is not None
    icon = assets.load_icon(source.width)
    assert icon is not source


def test_missing_asset_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(assets, "icon_file", lambda: tmp_path / "not-there.png")
    assert assets.load_source() is None
    assert assets.load_icon(32) is None


def test_broken_asset_returns_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """损坏的 PNG 不能让启动崩掉，只能退回内置画法。"""
    broken = tmp_path / "icon.png"
    broken.write_bytes(b"not a png")
    monkeypatch.setattr(assets, "icon_file", lambda: broken)
    assert assets.load_source() is None


def test_shortcut_icon_path_is_usable() -> None:
    icon = assets.shortcut_icon_path()
    assert icon is None or icon.exists()

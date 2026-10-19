"""清理音效测试（合成波形 + 播放路径 + 与监控的接线）。

全部使用假 winsound / 假 platform，**不产生任何真实声音**，也不断言音频听感——
听感只能人耳验证，代码能保证的是：波形不削波、格式合法、播放路径可控可关闭。
"""

from __future__ import annotations

import math
import struct
from types import SimpleNamespace

import pytest

from recyclebingguard import sound
from recyclebingguard.config import Config
from recyclebingguard.interfaces import CleanOutcome, CleanResult, ScanResult
from recyclebingguard.recycle_bin_monitor import RecycleBinMonitor
from recyclebingguard.sound import (
    SAMPLE_RATE,
    TRASH_SOUND_SECONDS,
    is_supported,
    play_trash_sound,
    render_trash_samples,
    trash_sound_wav,
)


class FakeWinsound:
    """记录 ``PlaySound`` 调用；可令播放抛异常以验证容错。"""

    SND_MEMORY = 4
    SND_ASYNC = 1

    def __init__(self, *, raise_on_play: bool = False) -> None:
        self.calls: list[tuple[bytes, int]] = []
        self.raise_on_play = raise_on_play

    def PlaySound(self, data: bytes, flags: int) -> None:  # noqa: N802 - 对齐 winsound API
        self.calls.append((data, flags))
        if self.raise_on_play:
            raise RuntimeError("模拟播放失败")


@pytest.fixture
def fake_winsound(monkeypatch: pytest.MonkeyPatch, request):
    """把 :mod:`winsound` 换成替身，并强制走 Windows 分支。"""
    import sys

    fake = FakeWinsound(raise_on_play=getattr(request, "param", {}).get("raise", False))
    monkeypatch.setitem(sys.modules, "winsound", fake)  # _play_sync 里 import 会命中它
    monkeypatch.setattr(sound, "is_supported", lambda: True)
    return fake


# --------------------------------------------------------------------------
# 波形合成
# --------------------------------------------------------------------------


def test_render_produces_expected_length_and_normalized_samples() -> None:
    samples = render_trash_samples()

    assert len(samples) == round(SAMPLE_RATE * TRASH_SOUND_SECONDS)
    assert all(-1.0 <= value <= 1.0 for value in samples)
    peak = max(abs(value) for value in samples)
    assert pytest.approx(peak, abs=1e-3) == 0.82  # _PEAK_LEVEL：归一化的目标峰值


def test_render_is_deterministic_across_calls() -> None:
    """固定噪声种子：任何一次清理听到的声音都完全一致。"""
    assert render_trash_samples() == render_trash_samples()


def _rms(values: list[float]) -> float:
    return math.sqrt(sum(v * v for v in values) / len(values))


def test_render_starts_quiet_then_decays() -> None:
    """起音极快、能量集中在开头：这是“噗”的一声而非一段长音。

    用 RMS 而不是单帧比较——单帧噪声会随机过零，断言会飘。
    """
    samples = render_trash_samples()
    total = len(samples)
    head = samples[: total // 10]
    middle = samples[total // 5 : total // 3]
    tail = samples[-total // 10 :]

    assert abs(samples[0]) < 1e-9  # 第一帧：起音尚未开始
    assert _rms(head) > _rms(middle) > _rms(tail)  # 能量单调衰减
    assert _rms(tail) < _rms(head) * 0.2  # 尾部基本收干净

    # 峰值出现在开头 20% 之内，也就是“一瞬间的爆发”
    peak_index = max(range(total), key=lambda i: abs(samples[i]))
    assert peak_index < total * 0.2


def test_wav_stream_is_valid_pcm_and_cached() -> None:
    data = trash_sound_wav()

    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"
    channels, rate = struct.unpack_from("<HI", data, 22)
    assert channels == 1
    assert rate == SAMPLE_RATE
    sampwidth = struct.unpack_from("<H", data, 34)[0]
    assert sampwidth == 16  # 16-bit PCM，winsound 才能直接播
    # 单声道 16-bit：数据块长度应为正且为偶数（字节数 = 采样数 × 2）
    data_size = struct.unpack_from("<I", data, 40)[0]
    assert data_size > 0 and data_size % 2 == 0

    # 缓存：第二次取到的是同一个 bytes 对象（避免每次清理都重算）
    assert trash_sound_wav() is data


# --------------------------------------------------------------------------
# 播放路径
# --------------------------------------------------------------------------


def test_disabled_sound_is_a_noop(fake_winsound: FakeWinsound) -> None:
    assert play_trash_sound(enabled=False) is False
    assert fake_winsound.calls == []


def test_unsupported_platform_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只替换本模块看到的 os，避免动到进程级的 os.name
    monkeypatch.setattr(sound, "os", SimpleNamespace(name="posix"))

    assert is_supported() is False
    assert play_trash_sound() is False


def test_sync_playback_uses_memory_flag(fake_winsound: FakeWinsound) -> None:
    assert play_trash_sound(wait=True) is True

    assert len(fake_winsound.calls) == 1
    data, flags = fake_winsound.calls[0]
    assert data == trash_sound_wav()
    assert flags & FakeWinsound.SND_MEMORY


def test_async_playback_returns_immediately(fake_winsound: FakeWinsound) -> None:
    assert play_trash_sound() is True

    # 异步播放在守护线程里；主线程不等待，但最终一定会调用一次
    import time

    deadline = time.monotonic() + 2.0
    while not fake_winsound.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(fake_winsound.calls) == 1


@pytest.mark.parametrize("fake_winsound", [{"raise": True}], indirect=True)
def test_playback_failure_is_swallowed(fake_winsound: FakeWinsound) -> None:
    """音效失败绝不能冒泡：清理已经完成，不能因为它报错。"""
    assert play_trash_sound(wait=True) is False


# --------------------------------------------------------------------------
# 与清理流程的接线
# --------------------------------------------------------------------------


class _FixedBinService:
    """总是“扫描达标 + 清理成功”的替身，用于观察清理后的音效接线。"""

    def scan(self) -> ScanResult:
        return ScanResult(total_items=100, total_size_bytes=0, scanned_roots=("C:\\",))

    def empty(self, *, verified_scan: ScanResult | None = None) -> CleanResult:
        return CleanResult(outcome=CleanOutcome.SUCCESS)


def _tick_once_with_sound(
    monkeypatch: pytest.MonkeyPatch, sound_enabled: bool, calls: list[dict]
) -> None:
    """跑一次完整的“达阈值 → 清理”，并记录音效调用。

    ``conftest`` 的全局静音在这里被覆盖成可观测的替身（monkeypatch LIFO 恢复）。
    """
    if sound_enabled:
        monkeypatch.setattr(
            "recyclebingguard.recycle_bin_monitor.play_trash_sound",
            lambda **kwargs: calls.append(kwargs) or True,
        )

    monitor = RecycleBinMonitor(
        _FixedBinService(),
        Config(
            threshold_items=10,
            poll_interval_seconds=60,
            auto_clean_enabled=True,
            confirm_before_clean=False,
            clean_sound_enabled=sound_enabled,
        ),
    )
    monitor.tick()


def test_monitor_plays_sound_after_successful_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []
    _tick_once_with_sound(monkeypatch, True, calls)

    assert len(calls) == 1


def test_monitor_stays_silent_when_sound_disabled_in_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    _tick_once_with_sound(monkeypatch, False, calls)

    assert calls == []  # 配置关闭 -> 连 _play_clean_sound 内部都不会往下走

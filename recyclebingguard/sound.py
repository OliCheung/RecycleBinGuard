"""回收站清理音效。

清理成功后播放一声「空垃圾桶」音效。声音来源是 **打包的 WAV 资源**
（``recyclebingguard/assets/trash.wav``，由用户提供的 MP3 经
``tools/convert_trash_sound.py`` 转换得到，随包分发）；若该资源因故缺失，
则降级为 :func:`render_trash_samples` 运行时**程序合成**的波形，保证清理流程
无论如何仍有声音。

设计约束：

- **零第三方依赖**：Windows 用标准库 :mod:`winsound`，其它平台（或没有
  ``winsound``）静默返回 ``False``；
- **绝不抛异常**：音效失败只会写一行 debug 日志，**不会影响清理主流程**；
- **不阻塞调用线程**：默认在守护线程里播放，监控线程不会被拖住。

用法::

    from recyclebingguard.sound import play_trash_sound

    play_trash_sound()                 # 异步播一声
    play_trash_sound(enabled=False)    # 按配置静音（什么都不做，返回 False）
    play_trash_sound(wait=True)        # 同步播（工具 / 自检用）
"""

from __future__ import annotations

import io
import math
import os
import random
import threading
import wave

from .assets import assets_dir
from .logging_setup import get_logger

logger = get_logger()

#: 采样率（16-bit 单声道）。
SAMPLE_RATE = 44100

#: 音效时长（秒）：与合成的衰减曲线配合，约 0.4 秒刚好"一声"。
TRASH_SOUND_SECONDS = 0.42

#: 合成过程中的音量上限（0~1），留出余量避免多段叠加后削波失真。
_PEAK_LEVEL = 0.82

#: 混合比例：低频冲击 : 气流噪声。
_THUMP_MIX = 0.55
_NOISE_MIX = 0.85

#: 低频冲击的起止频率（Hz）：起 180Hz、落到 55Hz，形成"沉下去"的听感。
_THUMP_START_HZ = 180.0
_THUMP_END_HZ = 55.0

#: 低通截止频率的区间（Hz）：跟着包络一起下扫，噪声从"沙"变成"闷"。
_CUTOFF_START_HZ = 5200.0
_CUTOFF_END_HZ = 900.0

#: 起音时长（秒）：极快起音，才有"噗"的爆发感。
_ATTACK_SECONDS = 0.012

#: 噪声随机种子：固定值保证每次听到的声音完全一致。
_NOISE_SEED = 0x5EED

#: 打包的清理音效资源文件名（WAV，PCM 16-bit 单声道）。
#: 由 ``tools/convert_trash_sound.py`` 从用户提供的 MP3 转换得到，随包分发。
TRASH_WAV_NAME = "trash.wav"

_wav_cache: bytes | None = None
_wav_cache_ready: bool = False


def _samples_to_wav(samples: list[float]) -> bytes:
    """把归一化采样序列编码成 RIFF/WAVE（16-bit 单声道）。"""
    frames = bytearray()
    for sample in samples:
        clamped = max(-1.0, min(1.0, sample))
        frames += int(clamped * 32767).to_bytes(2, "little", signed=True)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))

    return buffer.getvalue()


def render_trash_samples(
    *, rate: int = SAMPLE_RATE, seconds: float = TRASH_SOUND_SECONDS
) -> list[float]:
    """合成空垃圾桶音效的原始波形，返回归一化后的 ``[-1, 1]`` 采样序列。

    两段成分叠加后再整体归一化，所以无论怎么调比例都不会削波。
    """
    total = max(1, int(rate * seconds))
    rng = random.Random(_NOISE_SEED)

    samples: list[float] = []
    phase = 0.0
    lowpass = 0.0  # 一阶低通的状态（上一次输出）
    for index in range(total):
        progress = index / total  # 0 → 1

        # 包络：线性极快起音 + 指数衰减（约 -40dB）
        attack = min(1.0, index / max(1.0, rate * _ATTACK_SECONDS))
        envelope = attack * math.exp(-4.6 * progress)

        # ① 低频冲击：频率指数下滑的 sine
        frequency = _THUMP_END_HZ + (_THUMP_START_HZ - _THUMP_END_HZ) * math.exp(
            -7.0 * progress
        )
        phase += 2.0 * math.pi * frequency / rate
        thump = math.sin(phase)

        # ② 气流：白噪声过一阶低通，截止频率同步下滑
        cutoff = _CUTOFF_END_HZ + (_CUTOFF_START_HZ - _CUTOFF_END_HZ) * math.exp(
            -9.0 * progress
        )
        alpha = 1.0 - math.exp(-2.0 * math.pi * min(cutoff, rate * 0.45) / rate)
        lowpass += alpha * (rng.uniform(-1.0, 1.0) - lowpass)

        samples.append(envelope * (_THUMP_MIX * thump + _NOISE_MIX * lowpass))

    peak = max((abs(value) for value in samples), default=0.0) or 1.0
    scale = _PEAK_LEVEL / peak
    return [value * scale for value in samples]


def trash_sound_wav() -> bytes:
    """返回清理音效的 **WAV 字节流**（RIFF / PCM 16-bit 单声道），带进程内缓存。

    优先使用打包的 ``assets/trash.wav``（来自用户指定的 MP3）；若该资源因故缺失，
    降级为 :func:`render_trash_samples` 合成的波形，保证清理流程仍有声音。
    """
    global _wav_cache, _wav_cache_ready
    if _wav_cache_ready:
        return _wav_cache  # type: ignore[return-value]

    bundled = assets_dir() / TRASH_WAV_NAME
    try:
        data = bundled.read_bytes()
    except OSError:
        logger.debug("未找到打包音效 %s，降级为合成波形", bundled)
        data = _samples_to_wav(render_trash_samples())

    _wav_cache = data
    _wav_cache_ready = True
    return _wav_cache


def is_supported() -> bool:
    """当前平台能否播放音效（Windows 且有 :mod:`winsound`）。"""
    if os.name != "nt":
        return False
    try:  # pragma: no cover - 极端环境下 winsound 可能不可用
        import winsound  # noqa: F401, PLC0415
    except Exception:
        logger.debug("winsound 不可用，已跳过清理音效", exc_info=True)
        return False
    return True


def _play_sync(data: bytes) -> bool:
    """真正播放（同步阻塞约 0.4 秒）；任何失败都吞掉并返回 ``False``。"""
    try:
        import winsound  # noqa: PLC0415

        winsound.PlaySound(data, winsound.SND_MEMORY)
        return True
    except Exception:
        logger.debug("播放清理音效失败", exc_info=True)
        return False


def play_trash_sound(*, enabled: bool = True, wait: bool = False) -> bool:
    """播放"空垃圾桶"音效。

    :param enabled: 关闭时直接返回 ``False``（用于尊重用户在配置里的静音选择）。
    :param wait: 为真时在当前线程阻塞播放；默认起守护线程后台播放。
    :return: 是否**成功发起**了播放；静音 / 平台不支持时为 ``False``。
    """
    if not enabled:
        logger.debug("清理音效已按配置关闭")
        return False
    if not is_supported():
        return False

    data = trash_sound_wav()
    if wait:
        return _play_sync(data)

    threading.Thread(
        target=_play_sync, args=(data,), name="RBG-TrashSound", daemon=True
    ).start()
    return True


__all__ = [
    "SAMPLE_RATE",
    "TRASH_SOUND_SECONDS",
    "TRASH_WAV_NAME",
    "is_supported",
    "play_trash_sound",
    "render_trash_samples",
    "trash_sound_wav",
]

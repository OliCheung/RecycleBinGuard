"""把任意 MP3 转成清理音效资源 ``recyclebinguard/assets/trash.wav``。

清理音效默认是程序合成的；若想换成自己的声音（比如 macOS 那种「倒垃圾」音），
把 MP3 丢进来跑一下本脚本即可，无需改代码。

用法::

    python tools/convert_trash_sound.py                 # 用默认源（见 DEFAULT_SOURCE）
    python tools/convert_trash_sound.py "路径/音效.mp3"   # 指定源文件
    python tools/convert_trash_sound.py --check         # 只检查 ffmpeg 是否可用

输出约定（与 :mod:`recyclebingguard.sound` 配套）：

- 单声道（``-ac 1``）；
- 16-bit PCM（``-sample_fmt s16``），``winsound`` 才能用 ``SND_MEMORY`` 直接播；
- 采样率 44100（与 ``SAMPLE_RATE`` 一致）；
- 裁掉首尾静音，避免「声音前后拖一长段空白」。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "recyclebingguard" / "assets" / "trash.wav"

#: 默认源：用户提供的垃圾桶音效（中文文件名，请用引号包裹）。
DEFAULT_SOURCE = Path.home() / "Downloads" / "垃圾桶音效.MP3"


def find_ffmpeg() -> Path | None:
    """优先用 PATH 里的 ffmpeg，找不到再补一组常见安装位置。"""
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)

    candidates = [
        Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "ffmpeg",
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def convert(source: Path, ffmpeg: Path) -> int:
    if not source.is_file():
        print(f"[转换] 源文件不存在：{source}", file=sys.stderr)
        return 2

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg),
        "-y",
        "-i",
        str(source),
        "-af",
        "silenceremove=start_periods=1:start_duration=0.08:start_threshold=-50dB,"
        "areverse,"
        "silenceremove=start_periods=1:start_duration=0.08:start_threshold=-50dB,"
        "areverse",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-sample_fmt",
        "s16",
        str(OUTPUT),
    ]
    print(f"[转换] {source.name} → {OUTPUT.name}")
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:  # pragma: no cover - 环境相关
        print(exc.stderr, file=sys.stderr)
        return 1

    seconds = (OUTPUT.stat().st_size - 44) / (44100 * 2)
    print(f"[转换] 完成：{OUTPUT.stat().st_size:,} 字节，约 {seconds:.2f}s 单声道 16-bit")
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        ffmpeg = find_ffmpeg()
        print("ffmpeg:", ffmpeg or "未找到")
        return 0 if ffmpeg else 1

    source = Path(argv[1]) if len(argv) > 1 and not argv[1].startswith("--") else DEFAULT_SOURCE
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        print(
            "[错误] 找不到 ffmpeg。请安装 ffmpeg 并加入 PATH，"
            "或把 ffmpeg.exe 放到本机常见路径。",
            file=sys.stderr,
        )
        return 1
    return convert(source, ffmpeg)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

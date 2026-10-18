"""GitHub Releases 拉取式自动升级（公开库、无令牌）。

设计要点：

- **公开库、无需令牌**：直接轮询 GitHub Releases ``latest`` 接口；
- **严格大于才升级**：仅当发布版本严格高于当前 ``__version__`` 才继续（拒绝降级）；
- **防篡改校验**：若发布包含 ``checksums.txt``，强制校验下载产物的 sha256，
  不符即 HOLD；若未提供校验和，则跳过校验但记录警告（公开库 + TLS 已提供传输层保护）；
- **换包与重启**由 :mod:`recyclebingguard.__main__` 负责（释放单实例锁 → 启动新 EXE
  并传 ``--replace-target`` → 旧实例退出 → 新实例覆盖自身过去）。

所有网络 / 解析逻辑都拆成可单测的纯函数；网络调用集中在 ``fetch_latest_release``、
``_download_text``、``download_file``，便于测试时 mock。
"""

from __future__ import annotations

import hashlib
import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError

from . import __version__, paths

#: GitHub API 基址。
GITHUB_API = "https://api.github.com"
#: 默认仓库（OliCheung/RecycleBinGuard 为公开库）。
DEFAULT_REPO = "OliCheung/RecycleBinGuard"
#: 下载 / 请求时使用的 User-Agent（GitHub API 要求）。
USER_AGENT = "RecycleBinGuard-Updater"
#: 发布物中作为主程序的扩展名。
EXE_SUFFIX = ".exe"
#: 校验和文件名（sha256sum 格式：``<sha256>  <name>`` 或 ``<sha256> *<name>``）。
CHECKSUMS_NAME = "checksums.txt"
#: 自动检查的最小间隔（秒）：启动后约 30s 首查，之后每 6 小时一次。
UPDATE_CHECK_INTERVAL_SECONDS = 6 * 3600
#: 首查延迟（秒），避免拖慢启动。
UPDATE_FIRST_CHECK_DELAY_SECONDS = 30


class UpdateError(Exception):
    """升级过程中出现的可恢复错误（网络、解析、校验）。"""


@dataclass
class ReleaseInfo:
    """一个可安装的 GitHub 发布。"""

    version: str  # 规范化后的版本，如 "0.2.0"
    tag_name: str  # 原始 tag，如 "v0.2.0"
    exe_url: str  # 主程序（EXE）下载地址
    exe_name: str  # 主程序文件名
    expected_sha256: str | None  # 来自 checksums.txt；未提供则为 ``None``


@dataclass
class LocalUpdate:
    """一个来自本地信号文件的更新（离线兜底）。"""

    new_exe: Path
    expected_sha256: str | None


@dataclass
class UpdateCheckResult:
    """一次更新检查的结论。"""

    status: str  # "update_available" | "up_to_date" | "error"
    release: ReleaseInfo | None = None
    local: LocalUpdate | None = None
    error: str | None = None


# --------------------------------------------------------------------------
# 版本比较
# --------------------------------------------------------------------------


def _parse_version(tag: str) -> tuple[int, ...]:
    """把 ``v0.2.0`` / ``0.2.0`` 之类解析为数字元组；非数字后缀忽略。"""
    cleaned = tag.strip().lstrip("vV")
    parts: list[int] = []
    for chunk in cleaned.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            break
        parts.append(int(num))
    return tuple(parts)


def is_newer(current: str, candidate: str) -> bool:
    """``candidate`` 是否严格高于 ``current``。

    任一版本解析失败（得到空元组）都保守返回 ``False``，绝不误升级。
    """
    current_v = _parse_version(current)
    candidate_v = _parse_version(candidate)
    if not current_v or not candidate_v:
        return False
    return candidate_v > current_v


def _normalize(tag: str) -> str:
    return tag.strip().lstrip("vV")


# --------------------------------------------------------------------------
# GitHub 拉取
# --------------------------------------------------------------------------


def fetch_latest_release(repo: str = DEFAULT_REPO, *, timeout: float = 10.0) -> dict[str, Any]:
    """拉取 ``releases/latest`` 的原始 JSON；任何网络 / 解析失败抛 :class:`UpdateError`。"""
    url = f"{GITHUB_API}/repos/{repo}/releases/latest"
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read().decode("utf-8")
    except URLError as exc:
        raise UpdateError(f"无法访问 GitHub Releases：{exc}") from exc
    except TimeoutError as exc:  # pragma: no cover - 取决于 urllib 实现
        raise UpdateError(f"访问 GitHub Releases 超时：{exc}") from exc
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise UpdateError(f"Releases 响应不是合法 JSON：{exc}") from exc


def _pick_exe_asset(release: dict[str, Any]) -> dict[str, Any] | None:
    for asset in release.get("assets", []):
        if asset.get("name", "").lower().endswith(EXE_SUFFIX):
            return asset
    return None


def _pick_checksums_asset(release: dict[str, Any]) -> dict[str, Any] | None:
    for asset in release.get("assets", []):
        if asset.get("name", "").lower() == CHECKSUMS_NAME:
            return asset
    return None


def _expected_sha256_from_checksums(text: str, exe_name: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].strip().lower()
        filename = parts[-1].lstrip("*")
        if filename != exe_name:
            continue
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest
    return None


def _download_text(url: str, *, timeout: float = 10.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except URLError as exc:
        raise UpdateError(f"下载失败：{url}（{exc}）") from exc


def parse_release(
    release: dict[str, Any], *, current_version: str, repo: str = DEFAULT_REPO
) -> ReleaseInfo | None:
    """从 Releases JSON 解析出可安装的 :class:`ReleaseInfo`；无新版本或无可安装 EXE 返回 ``None``。"""
    tag = release.get("tag_name") or release.get("name") or ""
    if not tag:
        return None
    exe_asset = _pick_exe_asset(release)
    if exe_asset is None:
        return None
    if not is_newer(current_version, tag):
        return None

    expected_sha256: str | None = None
    checksums = _pick_checksums_asset(release)
    if checksums is not None:
        try:
            expected_sha256 = _expected_sha256_from_checksums(
                _download_text(checksums["browser_download_url"]), exe_asset["name"]
            )
        except UpdateError as exc:
            raise UpdateError(f"读取校验和失败：{exc}") from exc

    return ReleaseInfo(
        version=_normalize(tag),
        tag_name=tag,
        exe_url=exe_asset["browser_download_url"],
        exe_name=exe_asset["name"],
        expected_sha256=expected_sha256,
    )


def check_for_update(
    *, repo: str = DEFAULT_REPO, current_version: str | None = None, timeout: float = 10.0
) -> UpdateCheckResult:
    """检查 GitHub 是否有新版本；任何异常都转化为 ``status="error"``，不抛出。"""
    current_version = current_version or __version__
    # 本地信号优先（离线兜底）：存在且校验通过则直接返回，不再联网。
    local = read_local_update_signal(paths.update_signal_path())
    if local is not None:
        return UpdateCheckResult(status="update_available", local=local)
    try:
        release = fetch_latest_release(repo, timeout=timeout)
        info = parse_release(release, current_version=current_version, repo=repo)
    except UpdateError as exc:
        return UpdateCheckResult(status="error", error=str(exc))
    if info is None:
        return UpdateCheckResult(status="up_to_date")
    return UpdateCheckResult(status="update_available", release=info)


# --------------------------------------------------------------------------
# 下载与校验
# --------------------------------------------------------------------------


def download_file(url: str, dest: Path, *, timeout: float = 30.0) -> None:
    """把 ``url`` 下载到 ``dest``（已存在则覆盖）。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            dest.write_bytes(response.read())
    except URLError as exc:
        raise UpdateError(f"下载失败：{url}（{exc}）") from exc


def compute_sha256(path: Path) -> str:
    """计算文件 sha256（流式，避免大文件占内存）。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release(info: ReleaseInfo, path: Path) -> tuple[bool, str | None]:
    """校验下载产物；返回 ``(通过?, 实际 sha256)``。

    若发布未提供校验和（``expected_sha256 is None``），视为通过但提示需警惕。
    """
    actual = compute_sha256(path)
    if info.expected_sha256 is None:
        return True, actual
    return actual == info.expected_sha256, actual


def prepare_release(
    info: ReleaseInfo, dest: Path, *, timeout: float = 30.0
) -> tuple[Path, str | None]:
    """下载并校验发布 EXE；成功返回 ``(本地路径, 实际 sha256)``，失败抛 :class:`UpdateError`。"""
    download_file(info.exe_url, dest, timeout=timeout)
    ok, actual = verify_release(info, dest)
    if not ok:
        raise UpdateError(f"校验失败：期望 {info.expected_sha256}，实际 {actual}")
    return dest, actual


# --------------------------------------------------------------------------
# 本地更新信号（离线兜底）
# --------------------------------------------------------------------------


def read_local_update_signal(path: Path) -> LocalUpdate | None:
    """读取本地 ``update.signal`` 文件；格式 ``{"new_exe": "...", "sha256": "..."}``。

    任一条件不满足（文件不存在 / 解析失败 / 指向文件不存在 / sha256 不符）返回 ``None``。
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    raw = data.get("new_exe")
    if not raw:
        return None
    exe = Path(raw)
    if not exe.exists():
        return None
    expected = data.get("sha256")
    if expected:
        if compute_sha256(exe) != expected:
            return None
    return LocalUpdate(new_exe=exe, expected_sha256=expected)

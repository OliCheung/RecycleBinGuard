"""自动升级（updater）测试。

全部网络调用集中在 ``fetch_latest_release`` / ``_download_text`` / ``download_file``，
这里统一用 monkeypatch 替换 ``urllib.request.urlopen``，无需真实联网。

覆盖：

- 版本解析与“严格大于才升级”；
- GitHub 发布解析：无 EXE / 非新版本 / 新版本且校验和正确；
- 校验失败 → HOLD（抛 UpdateError，绝不换包）；
- 无校验和发布 → 视为通过（仅记录）；
- 本地 ``update.signal`` 读取：有效 / sha 不符 / 文件缺失 / 指向文件缺失；
- ``check_for_update`` 编排：本地信号优先、联网 up_to_date、联网有新版本、网络失败转 error。
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from recyclebingguard import paths
from recyclebingguard.updater import (
    CHECKSUMS_NAME,
    ReleaseInfo,
    UpdateError,
    _expected_sha256_from_checksums,
    _parse_version,
    check_for_update,
    compute_sha256,
    is_newer,
    parse_release,
    prepare_release,
    read_local_update_signal,
    verify_release,
)

#: 一个合法的 64 位十六进制校验和（全 0），用于构造发布 / 信号。
DUMMY_SHA256 = "0" * 64


class _FakeResponse:
    """模拟 ``urllib.request.urlopen`` 返回的上下文管理器。"""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _make_urlopen(responses: bytes | dict[str, bytes]):
    """构造 fake ``urlopen``：

    - ``responses`` 为 ``bytes``：任意 URL 都返回它；
    - 为 ``dict``：按 URL 精确匹配（缺失即抛 ``KeyError``，便于发现意外请求）。
    """

    def fake(target, timeout: float = 10.0):  # noqa: ARG001 - 签名兼容
        # 真实 urlopen 既接收字符串也接收 Request 对象，这里统一取 URL 字符串
        url = target.full_url if hasattr(target, "full_url") else target
        if isinstance(responses, bytes):
            return _FakeResponse(responses)
        return _FakeResponse(responses[url])

    return fake


# --------------------------------------------------------------------------
# 版本解析
# --------------------------------------------------------------------------


def test_parse_version_handles_v_prefix_and_split() -> None:
    assert _parse_version("v0.2.0") == (0, 2, 0)
    assert _parse_version("0.2.0") == (0, 2, 0)
    assert _parse_version("V1.2.3") == (1, 2, 3)
    assert _parse_version("v1.2.3.4") == (1, 2, 3, 4)


def test_parse_version_ignores_non_numeric_suffix() -> None:
    # 形如 "v0.2.0-beta" 应解析为 (0, 2, 0)
    assert _parse_version("v0.2.0-beta") == (0, 2, 0)


def test_is_newer_is_strict() -> None:
    assert is_newer("0.1.0", "v0.2.0") is True
    # 相等不算“更新”，拒绝无变化换包
    assert is_newer("0.2.0", "v0.2.0") is False
    # 降级必须拒绝
    assert is_newer("0.2.0", "v0.1.0") is False
    assert is_newer("1.0.0", "v0.9.9") is False


def test_is_newer_handles_unparseable() -> None:
    # 解析失败时保守返回 False，绝不误升级
    assert is_newer("not-a-version", "v0.2.0") is False
    assert is_newer("0.1.0", "garbage") is False


# --------------------------------------------------------------------------
# 校验和解析
# --------------------------------------------------------------------------


def test_expected_sha256_from_checksums_matches_by_name() -> None:
    text = (
        f"{DUMMY_SHA256}  other.txt\n"
        f"{DUMMY_SHA256} *RecycleBinGuard.exe\n"
        f"{DUMMY_SHA256}  RecycleBinGuard.exe\n"
    )
    assert _expected_sha256_from_checksums(text, "RecycleBinGuard.exe") == DUMMY_SHA256


def test_expected_sha256_from_checksums_returns_none_when_absent() -> None:
    text = f"{DUMMY_SHA256}  other.txt\n"
    assert _expected_sha256_from_checksums(text, "RecycleBinGuard.exe") is None


def test_expected_sha256_from_checksums_rejects_bad_length() -> None:
    # 长度不足 64 或含非 hex 字符时不应被当作有效校验和
    text = "abc  RecycleBinGuard.exe\n"
    assert _expected_sha256_from_checksums(text, "RecycleBinGuard.exe") is None


# --------------------------------------------------------------------------
# 发布解析
# --------------------------------------------------------------------------


def _release_payload(
    tag: str, *, with_checksums: bool = False, checksum_text: str | None = None
) -> dict:
    assets = [
        {"name": "RecycleBinGuard.exe", "browser_download_url": "http://x/RecycleBinGuard.exe"}
    ]
    if with_checksums:
        assets.append(
            {
                "name": CHECKSUMS_NAME,
                "browser_download_url": "http://cs/checksums.txt",
            }
        )
    return {"tag_name": tag, "assets": assets}


def test_parse_release_none_when_no_exe_asset() -> None:
    release = {"tag_name": "v0.2.0", "assets": []}
    assert parse_release(release, current_version="0.1.0") is None


def test_parse_release_none_when_version_not_newer() -> None:
    release = _release_payload("v0.1.0")
    assert parse_release(release, current_version="0.1.0") is None


def test_parse_release_none_when_downgrade() -> None:
    release = _release_payload("v0.0.9")
    assert parse_release(release, current_version="0.1.0") is None


def test_parse_release_picks_exe_and_reads_checksums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksum_text = f"{DUMMY_SHA256}  RecycleBinGuard.exe\n"
    release = _release_payload("v0.2.0", with_checksums=True)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _make_urlopen(
            {
                "http://cs/checksums.txt": checksum_text.encode("utf-8"),
            }
        ),
    )

    info = parse_release(release, current_version="0.1.0")

    assert info is not None
    assert info.version == "0.2.0"
    assert info.tag_name == "v0.2.0"
    assert info.exe_name == "RecycleBinGuard.exe"
    assert info.exe_url == "http://x/RecycleBinGuard.exe"
    assert info.expected_sha256 == DUMMY_SHA256


def test_parse_release_no_checksums_asset_yields_none_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 没有 checksums.txt 资产：expected_sha256 为 None（跳过校验）
    release = _release_payload("v0.2.0", with_checksums=False)
    # 不应有任何网络请求；放一个会抛错的实现以防回归
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: pytest.fail("不应请求校验和"),
    )

    info = parse_release(release, current_version="0.1.0")

    assert info is not None
    assert info.expected_sha256 is None


# --------------------------------------------------------------------------
# 校验（verify / prepare）
# --------------------------------------------------------------------------


def test_compute_sha256_is_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello world")
    assert compute_sha256(p) == compute_sha256(p)


def test_verify_release_passes_when_sha_matches(tmp_path: Path) -> None:
    p = tmp_path / "f.exe"
    p.write_bytes(b"payload")
    actual = compute_sha256(p)
    info = ReleaseInfo(
        version="0.2.0",
        tag_name="v0.2.0",
        exe_url="u",
        exe_name="f.exe",
        expected_sha256=actual,
    )
    ok, got = verify_release(info, p)
    assert ok is True
    assert got == actual


def test_verify_release_fails_when_sha_differs(tmp_path: Path) -> None:
    p = tmp_path / "f.exe"
    p.write_bytes(b"payload")
    info = ReleaseInfo(
        version="0.2.0",
        tag_name="v0.2.0",
        exe_url="u",
        exe_name="f.exe",
        expected_sha256=DUMMY_SHA256,
    )
    ok, got = verify_release(info, p)
    assert ok is False
    assert got == compute_sha256(p)


def test_verify_release_passes_when_no_expected_sha(tmp_path: Path) -> None:
    p = tmp_path / "f.exe"
    p.write_bytes(b"payload")
    info = ReleaseInfo(
        version="0.2.0",
        tag_name="v0.2.0",
        exe_url="u",
        exe_name="f.exe",
        expected_sha256=None,
    )
    ok, got = verify_release(info, p)
    assert ok is True


def test_prepare_release_writes_and_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"real-binary-content"
    monkeypatch.setattr("urllib.request.urlopen", _make_urlopen(payload))
    info = ReleaseInfo(
        version="0.2.0",
        tag_name="v0.2.0",
        exe_url="http://x/RecycleBinGuard.exe",
        exe_name="RecycleBinGuard.exe",
        expected_sha256=compute_sha256_bytes(payload),
    )
    dest = tmp_path / "out.exe"
    result_path, actual = prepare_release(info, dest, timeout=5.0)
    assert result_path == dest
    assert dest.read_bytes() == payload
    assert actual == compute_sha256(dest)


def test_prepare_release_rejects_checksum_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关键 HOLD 测试：下载成功但校验失败 → 抛 UpdateError，绝不换包。"""
    payload = b"real-binary-content"
    monkeypatch.setattr("urllib.request.urlopen", _make_urlopen(payload))
    info = ReleaseInfo(
        version="0.2.0",
        tag_name="v0.2.0",
        exe_url="http://x/RecycleBinGuard.exe",
        exe_name="RecycleBinGuard.exe",
        expected_sha256=DUMMY_SHA256,  # 故意错误
    )
    dest = tmp_path / "out.exe"
    with pytest.raises(UpdateError):
        prepare_release(info, dest, timeout=5.0)


# --------------------------------------------------------------------------
# 本地更新信号
# --------------------------------------------------------------------------


def test_read_local_signal_returns_update_when_valid(tmp_path: Path) -> None:
    new_exe = tmp_path / "new.exe"
    new_exe.write_bytes(b"new-binary")
    sha = compute_sha256(new_exe)
    sig = tmp_path / "update.signal"
    sig.write_text(json.dumps({"new_exe": str(new_exe), "sha256": sha}), encoding="utf-8")
    result = read_local_update_signal(sig)
    assert result is not None
    assert result.new_exe == new_exe
    assert result.expected_sha256 == sha


def test_read_local_signal_optional_sha_when_absent(tmp_path: Path) -> None:
    new_exe = tmp_path / "new.exe"
    new_exe.write_bytes(b"new-binary")
    sig = tmp_path / "update.signal"
    sig.write_text(json.dumps({"new_exe": str(new_exe)}), encoding="utf-8")
    result = read_local_update_signal(sig)
    assert result is not None
    assert result.expected_sha256 is None


def test_read_local_signal_sha_mismatch_returns_none(tmp_path: Path) -> None:
    new_exe = tmp_path / "new.exe"
    new_exe.write_bytes(b"new-binary")
    sig = tmp_path / "update.signal"
    sig.write_text(json.dumps({"new_exe": str(new_exe), "sha256": DUMMY_SHA256}), encoding="utf-8")
    assert read_local_update_signal(sig) is None


def test_read_local_signal_missing_file_returns_none() -> None:
    assert read_local_update_signal(Path("nonexistent-dir/update.signal")) is None


def test_read_local_signal_missing_exe_returns_none(tmp_path: Path) -> None:
    sig = tmp_path / "update.signal"
    sig.write_text(json.dumps({"new_exe": str(tmp_path / "nope.exe")}), encoding="utf-8")
    assert read_local_update_signal(sig) is None


def test_read_local_signal_malformed_json_returns_none(tmp_path: Path) -> None:
    sig = tmp_path / "update.signal"
    sig.write_text("{ broken", encoding="utf-8")
    assert read_local_update_signal(sig) is None


# --------------------------------------------------------------------------
# 编排：check_for_update
# --------------------------------------------------------------------------


def test_check_for_update_up_to_date(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _make_urlopen(json.dumps(_release_payload("v0.1.0")).encode("utf-8")),
    )
    result = check_for_update(current_version="0.1.0")
    assert result.status == "up_to_date"
    assert result.release is None
    assert result.local is None


def test_check_for_update_update_available(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checksum_text = f"{DUMMY_SHA256}  RecycleBinGuard.exe\n"
    release = _release_payload("v0.2.0", with_checksums=True)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        _make_urlopen(
            {
                "https://api.github.com/repos/OliCheung/RecycleBinGuard/releases/latest": json.dumps(
                    release
                ).encode("utf-8"),
                "http://cs/checksums.txt": checksum_text.encode("utf-8"),
            }
        ),
    )
    result = check_for_update(current_version="0.1.0")
    assert result.status == "update_available"
    assert result.release is not None
    assert result.release.version == "0.2.0"
    assert result.local is None


def test_check_for_update_local_signal_wins_over_github(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地信号存在时直接返回，绝不联网（离线兜底优先）。"""
    new_exe = tmp_path / "new.exe"
    new_exe.write_bytes(b"x")
    isolated_home.mkdir(parents=True, exist_ok=True)
    sig = paths.update_signal_path()
    sig.write_text(json.dumps({"new_exe": str(new_exe)}), encoding="utf-8")

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: pytest.fail("本地信号存在时不应联网"),
    )
    result = check_for_update(current_version="0.1.0")
    assert result.status == "update_available"
    assert result.local is not None
    assert result.local.new_exe == new_exe
    assert result.release is None


def test_check_for_update_error_on_network_failure(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    result = check_for_update(current_version="0.1.0")
    assert result.status == "error"
    assert result.error is not None
    assert result.release is None
    assert result.local is None


def compute_sha256_bytes(data: bytes) -> str:
    """测试辅助：直接对字节算 sha256（避免先落盘）。"""
    import hashlib

    return hashlib.sha256(data).hexdigest()

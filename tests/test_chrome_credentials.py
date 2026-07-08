"""Chrome Login Data (v10 DPAPI + AES-256-GCM) 復号のテスト。

DPAPI 部分はテスト決定性のため monkeypatch でスタブする。GCM 部分は
実 pycryptodome で往復させる。SQLite fixture は sqlite3 で組む。
末尾には `load_credentials` の end-to-end テスト (Local State + Login Data +
DPAPI モックを組み合わせた統合検証) も含む。
"""

from __future__ import annotations

import base64
import json
import sqlite3
from typing import TYPE_CHECKING

from Crypto.Cipher import AES

import canvasser
from canvasser import (
    _decrypt_v10_password,
    _load_chrome_master_key,
    _read_bnid_login_row,
    load_credentials,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _encrypt_v10(master_key: bytes, plaintext: str) -> bytes:
    """テスト用: v10 プレフィックス + GCM 暗号化 blob を作る。"""
    nonce = b"\x00" * 12
    cipher = AES.new(  # pyright: ignore[reportUnknownMemberType]
        master_key, AES.MODE_GCM, nonce=nonce
    )
    ct, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return b"v10" + nonce + ct + tag


def test_decrypt_v10_password_roundtrip() -> None:
    """正しい master_key + v10 blob → 平文取得。"""
    key = b"k" * 32
    plaintext = "correct_password"
    blob = _encrypt_v10(key, plaintext)
    assert _decrypt_v10_password(key, blob) == plaintext


def test_decrypt_v10_password_wrong_key_returns_none() -> None:
    """32 bytes だが誤 master_key → GCM MAC 検証失敗で None。"""
    right = b"k" * 32
    wrong = b"x" * 32
    blob = _encrypt_v10(right, "secret")
    assert _decrypt_v10_password(wrong, blob) is None


def test_decrypt_v10_password_wrong_length_key_returns_none() -> None:
    """誤長 master_key → AES.new が ValueError → None。"""
    key_valid = b"k" * 32
    blob = _encrypt_v10(key_valid, "secret")
    assert _decrypt_v10_password(b"short", blob) is None


def test_decrypt_v10_password_v20_prefix_returns_none() -> None:
    """v20 (App-Bound) プレフィックスは復号せず None。"""
    key = b"k" * 32
    blob = b"v20" + b"\x00" * 60
    assert _decrypt_v10_password(key, blob) is None


def test_decrypt_v10_password_unknown_prefix_returns_none() -> None:
    """不明プレフィックスは復号せず None。"""
    key = b"k" * 32
    blob = b"abc" + b"\x00" * 60
    assert _decrypt_v10_password(key, blob) is None


def test_decrypt_v10_password_non_utf8_returns_none() -> None:
    """GCM 復号結果が UTF-8 でない → None。"""
    key = b"k" * 32
    nonce = b"\x00" * 12
    cipher = AES.new(  # pyright: ignore[reportUnknownMemberType]
        key, AES.MODE_GCM, nonce=nonce
    )
    ct, tag = cipher.encrypt_and_digest(b"\xff\xfe\xfd")  # 無効 UTF-8
    blob = b"v10" + nonce + ct + tag
    assert _decrypt_v10_password(key, blob) is None


def test_load_chrome_master_key_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local State → DPAPI 復号 (monkeypatch) → 32 bytes キー。"""
    fake_key = b"k" * 32

    def _fake_dpapi_unprotect(blob: bytes) -> bytes | None:
        del blob
        return fake_key

    monkeypatch.setattr("canvasser._dpapi_unprotect", _fake_dpapi_unprotect)
    local_state = tmp_path / "Local State"
    encrypted_b64 = base64.b64encode(b"DPAPI" + b"garbage").decode()
    local_state.write_text(
        json.dumps({"os_crypt": {"encrypted_key": encrypted_b64}}),
        encoding="utf-8",
    )
    assert _load_chrome_master_key(tmp_path) == fake_key


def test_load_chrome_master_key_missing_local_state(tmp_path: Path) -> None:
    """Local State が無ければ None。"""
    assert _load_chrome_master_key(tmp_path) is None


def test_load_chrome_master_key_dpapi_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DPAPI 復号が失敗すれば None。"""

    def _fake_dpapi_unprotect(blob: bytes) -> bytes | None:
        del blob
        return None

    monkeypatch.setattr("canvasser._dpapi_unprotect", _fake_dpapi_unprotect)
    local_state = tmp_path / "Local State"
    encrypted_b64 = base64.b64encode(b"DPAPI" + b"garbage").decode()
    local_state.write_text(
        json.dumps({"os_crypt": {"encrypted_key": encrypted_b64}}),
        encoding="utf-8",
    )
    assert _load_chrome_master_key(tmp_path) is None


def _make_login_data_db(path: Path, rows: list[tuple[str, str, bytes]]) -> None:
    """テスト用 Login Data DB を作る。rows: [(origin_url, username, password_blob)]。"""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE logins ("
        " origin_url TEXT, username_value TEXT, password_value BLOB,"
        " date_last_used INTEGER, blacklisted_by_user INTEGER,"
        " signon_realm TEXT)"
    )
    for i, (url, uname, pw) in enumerate(rows):
        con.execute(
            "INSERT INTO logins VALUES (?, ?, ?, ?, ?, ?)",
            (url, uname, pw, 1000 + i, 0, url),
        )
    con.commit()
    con.close()


def test_read_bnid_login_row_returns_latest(tmp_path: Path) -> None:
    """複数行あれば date_last_used が最新の 1 行を返す。"""
    default = tmp_path / "Default"
    default.mkdir()
    bnid_url = "https://account.bandainamcoid.com/login.html"
    _make_login_data_db(
        default / "Login Data",
        [
            (bnid_url, "old@example.com", b"v10OLD"),
            (bnid_url, "new@example.com", b"v10NEW"),
        ],
    )
    result = _read_bnid_login_row(tmp_path)
    assert result is not None
    assert result[0] == "new@example.com"
    assert result[1] == b"v10NEW"


def test_read_bnid_login_row_no_bnid_row_returns_none(tmp_path: Path) -> None:
    """bandainamcoid 以外の origin_url しか無ければ None。"""
    default = tmp_path / "Default"
    default.mkdir()
    _make_login_data_db(
        default / "Login Data",
        [("https://example.com/login", "someone@example.com", b"v10XX")],
    )
    assert _read_bnid_login_row(tmp_path) is None


def test_read_bnid_login_row_no_login_data_returns_none(tmp_path: Path) -> None:
    """Login Data ファイル自体が無ければ None。"""
    assert _read_bnid_login_row(tmp_path) is None


def test_read_bnid_login_row_empty_username_returns_none(tmp_path: Path) -> None:
    """LATEST 行の username が空文字なら None (fallback しない)。"""
    default = tmp_path / "Default"
    default.mkdir()
    _make_login_data_db(
        default / "Login Data",
        [("https://account.bandainamcoid.com/login.html", "", b"v10XX")],
    )
    assert _read_bnid_login_row(tmp_path) is None


def test_load_credentials_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local State + Login Data + DPAPI モックで Credentials を組み立てる (正常系)。"""
    # 非 Windows テストランナーでも load_credentials の Windows path を経由させる
    monkeypatch.setattr(canvasser.os, "name", "nt")
    fake_key = b"k" * 32

    def _fake_dpapi_unprotect(blob: bytes) -> bytes | None:
        del blob
        return fake_key

    monkeypatch.setattr("canvasser._dpapi_unprotect", _fake_dpapi_unprotect)
    local_state = tmp_path / "Local State"
    encrypted_b64 = base64.b64encode(b"DPAPI" + b"garbage").decode()
    local_state.write_text(
        json.dumps({"os_crypt": {"encrypted_key": encrypted_b64}}),
        encoding="utf-8",
    )
    default = tmp_path / "Default"
    default.mkdir()
    _make_login_data_db(
        default / "Login Data",
        [
            (
                "https://account.bandainamcoid.com/login.html",
                "user@example.com",
                _encrypt_v10(fake_key, "correct_pw"),
            )
        ],
    )

    creds = load_credentials(tmp_path)

    assert creds is not None
    assert creds.bnid_email == "user@example.com"
    assert creds.bnid_password == "correct_pw"


def test_load_credentials_no_login_data_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """master_key の復号に成功しても Login Data が無ければ None。"""
    # 非 Windows テストランナーでも load_credentials の Windows path を経由させる
    monkeypatch.setattr(canvasser.os, "name", "nt")

    def _fake_dpapi_unprotect(blob: bytes) -> bytes | None:
        del blob
        return b"k" * 32

    monkeypatch.setattr("canvasser._dpapi_unprotect", _fake_dpapi_unprotect)
    local_state = tmp_path / "Local State"
    encrypted_b64 = base64.b64encode(b"DPAPI" + b"x").decode()
    local_state.write_text(
        json.dumps({"os_crypt": {"encrypted_key": encrypted_b64}}),
        encoding="utf-8",
    )

    assert load_credentials(tmp_path) is None


def test_load_credentials_non_windows_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """非 Windows では master_key 取得を試みる前に None を返す。"""
    monkeypatch.setattr(canvasser.os, "name", "posix")

    assert load_credentials(tmp_path) is None

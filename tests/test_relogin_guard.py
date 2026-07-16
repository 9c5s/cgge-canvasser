"""relogin_guard.json (失敗ガード state) の CRUD テスト。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from canvasser import (
    CREDENTIALS_DISABLE_WINDOW_SEC,
    CREDENTIALS_MAX_FAILURES,
    JST,
    ReloginGuard,
    _record_relogin_failure,
    _relogin_disabled,
    _reset_relogin_failure,
    load_relogin_guard,
    save_relogin_guard,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_load_missing_returns_default(tmp_path: Path) -> None:
    """profile_dir に relogin_guard.json が無ければ既定値を返す。"""
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard(failure_count=0, disabled_until=None)


def test_load_broken_json_returns_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """壊れた JSON なら既定値 + 警告ログ。"""
    (tmp_path / "relogin_guard.json").write_text("{not json", encoding="utf-8")
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard()
    assert "relogin_guard.json" in caplog.text


def test_load_wrong_shape_returns_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """トップレベルが dict でない場合、既定値 + 警告ログ。"""
    (tmp_path / "relogin_guard.json").write_text("[]", encoding="utf-8")
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard()
    assert "relogin_guard.json" in caplog.text


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """save → load 往復で state が保存される。"""
    original = ReloginGuard(failure_count=2, disabled_until="2026-07-08T18:00:00+09:00")
    save_relogin_guard(tmp_path, original)
    assert load_relogin_guard(tmp_path) == original


def test_save_atomic_writes_json(tmp_path: Path) -> None:
    """save 後の JSON 内容が dataclass と一致する。"""
    guard = ReloginGuard(failure_count=1, disabled_until=None)
    save_relogin_guard(tmp_path, guard)
    data = json.loads((tmp_path / "relogin_guard.json").read_text(encoding="utf-8"))
    assert data == {"failure_count": 1, "disabled_until": None}


def test_relogin_disabled_none() -> None:
    """`disabled_until` が None ならガード無効 (False) を返す。"""
    guard = ReloginGuard(disabled_until=None)
    assert _relogin_disabled(guard, "test") is False


def test_relogin_disabled_past() -> None:
    """過去時刻の `disabled_until` はガード無効 (False) として扱う。"""
    past = (datetime.now(JST) - timedelta(hours=1)).isoformat()
    guard = ReloginGuard(disabled_until=past)
    assert _relogin_disabled(guard, "test") is False


def test_relogin_disabled_future(caplog: pytest.LogCaptureFixture) -> None:
    """未来時刻の `disabled_until` はガード有効 (True) にし警告ログを残す。"""
    future = (datetime.now(JST) + timedelta(hours=1)).isoformat()
    guard = ReloginGuard(disabled_until=future)
    assert _relogin_disabled(guard, "test") is True
    assert "test" in caplog.text


def test_relogin_disabled_invalid_string() -> None:
    """パース不能な disabled_until は False にフォールバック (fail-safe)。"""
    guard = ReloginGuard(disabled_until="not-a-date")
    assert _relogin_disabled(guard, "test") is False


def test_reset_relogin_failure_writes(tmp_path: Path) -> None:
    """failure_count が残っていれば reset で既定値 (0/None) に書き戻す。"""
    save_relogin_guard(tmp_path, ReloginGuard(failure_count=2))
    guard = load_relogin_guard(tmp_path)
    _reset_relogin_failure(tmp_path, guard)
    assert load_relogin_guard(tmp_path) == ReloginGuard()


def test_reset_relogin_failure_noop_when_clean(tmp_path: Path) -> None:
    """既に 0/None なら書き込みしない (I/O 節約)。"""
    guard = ReloginGuard()
    _reset_relogin_failure(tmp_path, guard)
    assert not (tmp_path / "relogin_guard.json").exists()


def test_record_relogin_failure_increments(tmp_path: Path) -> None:
    """failure_count に submissions を加算する。"""
    guard = ReloginGuard(failure_count=0)
    _record_relogin_failure(tmp_path, guard, submissions=1)
    assert load_relogin_guard(tmp_path).failure_count == 1


def test_record_relogin_failure_reaches_max_sets_disabled_until(tmp_path: Path) -> None:
    """MAX_FAILURES 到達で disabled_until を設定する。"""
    guard = ReloginGuard(failure_count=CREDENTIALS_MAX_FAILURES - 1)
    _record_relogin_failure(tmp_path, guard, submissions=1)
    result = load_relogin_guard(tmp_path)
    assert result.failure_count == CREDENTIALS_MAX_FAILURES
    assert result.disabled_until is not None
    # disabled_until は 6 時間後 ± 若干のずれ
    deadline = datetime.fromisoformat(result.disabled_until)
    expected = datetime.now(JST) + timedelta(seconds=CREDENTIALS_DISABLE_WINDOW_SEC)
    assert abs((deadline - expected).total_seconds()) < 10


def test_record_relogin_failure_two_submissions(tmp_path: Path) -> None:
    """submissions=2 で 1 → 3 (MAX 到達)。"""
    guard = ReloginGuard(failure_count=1)
    _record_relogin_failure(tmp_path, guard, submissions=2)
    assert load_relogin_guard(tmp_path).failure_count == 3

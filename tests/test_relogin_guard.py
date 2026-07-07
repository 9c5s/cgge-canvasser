"""relogin_guard.json (失敗ガード state) の CRUD テスト。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from canvasser import ReloginGuard, load_relogin_guard, save_relogin_guard

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_load_missing_returns_default(tmp_path: Path) -> None:
    """profile_dir に relogin_guard.json が無ければ既定値を返す。"""
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard(failure_count=0, disabled_until=None)


def test_load_broken_json_returns_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """壊れた JSON なら既定値 + stderr 警告。"""
    (tmp_path / "relogin_guard.json").write_text("{not json", encoding="utf-8")
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard()
    assert "relogin_guard.json" in capsys.readouterr().err


def test_load_wrong_shape_returns_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """トップレベルが dict でない場合、既定値 + stderr 警告。"""
    (tmp_path / "relogin_guard.json").write_text("[]", encoding="utf-8")
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard()
    assert "relogin_guard.json" in capsys.readouterr().err


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

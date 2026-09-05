"""ミッション回収フロー (collect_missions / _complete / _receive) のテスト。

FakePage で API 応答を差し替え、dry-run と本番実行の分岐・ecode 別の
ハンドリング・獲得票数の集計を検証する。E1926 (ASOBI 連携トークン切れ) 検知時に
`_complete`/`_receive`/`_process_one_mission` が `MissionOutcome` で
`linkage_expired_id` を伝搬することも検証する。`collect_missions` が E1926 検知後に
`_run_asobi_linkage_recovery` を起動して再走する end-to-end フローも検証する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import canvasser
from tests._fakes import (
    FakePage,
    as_page as _as_page,
    error_response as _error_response,
    success_response,
)

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page


def _mission(
    mid: int,
    pts: int,
    *,
    flag: bool = True,
    completed: bool = False,
    received: bool = False,
    remaining: int = 1,
) -> dict[str, Any]:
    """テスト用のミッション 1 件分の dict を組み立てる。"""
    return {
        "mission_id": mid,
        "mission_name": f"ミッション{mid}",
        "mission_point": pts,
        "action": {"mission_complete_api_call_flag": flag},
        "is_mission_completed": completed,
        "is_mission_received": received,
        "remaining_completable_count": remaining,
    }


def _listing(missions: list[dict[str, Any]], point: int = 0) -> dict[str, Any]:
    """ミッション一覧 GET の成功応答を組み立てる。"""
    return success_response({"current_point": point, "missions": missions})


def _empty_listing() -> dict[str, Any]:
    """空の ASOBI STORE 一覧など、対象なし応答を組み立てる。"""
    return _listing([])


_OK = success_response()


class TestCollectMissions:
    """collect_missions の全体フロー。"""

    def test_dryrunは達成可能ミッションの見込み票数を返す(self, tmp_path: Path) -> None:
        """dry-run では GET 2 回 (通常 + ASOBI STORE) のみで、POST/PUT を送らない。"""
        fake = FakePage([_listing([_mission(1, 30)]), _empty_listing()])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=True, auto_relogin=True
        )

        assert gained == 30
        assert len(fake.calls) == 2

    def test_本番実行は達成と受取を実行して票数を返す(self, tmp_path: Path) -> None:
        """本番実行では両一覧 GET を先に済ませてから POST → PUT を送る。"""
        receive_ok = success_response({"received_point": 30})
        fake = FakePage([
            _listing([_mission(1, 30)]),
            _empty_listing(),
            _OK,
            receive_ok,
        ])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
        )

        assert gained == 30
        assert len(fake.calls) == 4

    def test_達成済み未受取は受取のみ行う(self, tmp_path: Path) -> None:
        """is_mission_completed=True かつ未受取なら受取 PUT だけを送る。"""
        receive_ok = success_response({"received_point": 10})
        fake = FakePage([
            _listing([_mission(2, 10, completed=True, received=False)]),
            _empty_listing(),
            receive_ok,
        ])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
        )

        assert gained == 10
        assert len(fake.calls) == 3

    def test_APIフラグなしミッションは対象外(self, tmp_path: Path) -> None:
        """flag=False かつ未達成なら達成も受取も送らない。"""
        fake = FakePage([
            _listing([_mission(3, 50, flag=False)]),
            _empty_listing(),
        ])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
        )

        assert gained == 0
        assert len(fake.calls) == 2

    def test_APIフラグなしでも達成済み未受取は受取のみ行う(
        self, tmp_path: Path
    ) -> None:
        """flag=False でも completed かつ未受取なら受取 PUT だけを送る。

        チェックインボーナス (`#99`) のように外部トリガーで達成扱いになる分の
        取りこぼしを防ぐ。
        """
        receive_ok = success_response({"received_point": 15})
        fake = FakePage([
            _listing([_mission(6, 15, flag=False, completed=True, received=False)]),
            _empty_listing(),
            receive_ok,
        ])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
        )

        assert gained == 15
        assert len(fake.calls) == 3

    def test_APIフラグなしで達成済み受取済みは何もしない(self, tmp_path: Path) -> None:
        """flag=False かつ既に受取済みなら PUT も送らない (重複受取回避)。"""
        fake = FakePage([
            _listing([_mission(7, 15, flag=False, completed=True, received=True)]),
            _empty_listing(),
        ])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
        )

        assert gained == 0
        assert len(fake.calls) == 2

    def test_達成済みE1906でも受取を試みる(self, tmp_path: Path) -> None:
        """達成 POST が E1906 (既達成) を返しても受取 PUT は送る。"""
        receive_ok = success_response({"received_point": 20})
        fake = FakePage([
            _listing([_mission(4, 20)]),
            _empty_listing(),
            _error_response("E1906"),
            receive_ok,
        ])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
        )

        assert gained == 20
        assert len(fake.calls) == 4

    def test_条件未達E1924は受取を送らない(self, tmp_path: Path) -> None:
        """達成 POST が E1924 (条件未達) なら受取 PUT を送らずスキップする。"""
        fake = FakePage([
            _listing([_mission(5, 20)]),
            _empty_listing(),
            _error_response("E1924"),
        ])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
        )

        assert gained == 0
        assert len(fake.calls) == 3

    def test_一覧取得失敗はRuntimeError(self, tmp_path: Path) -> None:
        """通常一覧 GET が失敗すれば ASOBI STORE 到達前に RuntimeError で止める。"""
        fake = FakePage([{"status": 500, "body": None, "error": "boom"}])

        with pytest.raises(RuntimeError, match="通常"):
            canvasser.collect_missions(
                _as_page(fake), tmp_path, "test", dry_run=True, auto_relogin=True
            )

    def test_ASOBI_STORE一覧の取得失敗もRuntimeError(self, tmp_path: Path) -> None:
        """通常一覧が取れても ASOBI STORE 一覧が失敗すれば RuntimeError で止める。"""
        fake = FakePage([
            _listing([]),
            {"status": 500, "body": None, "error": "boom"},
        ])

        with pytest.raises(RuntimeError, match="ASOBI STORE"):
            canvasser.collect_missions(
                _as_page(fake), tmp_path, "test", dry_run=True, auto_relogin=True
            )

    def test_通常とASOBI_STOREの両方から受取を集計する(self, tmp_path: Path) -> None:
        """mission_type=0 と mission_type=1 の両方で受取を実行し合算する。

        ASOBI STORE 系のプレミアム会員ログインボーナス (flag=True で達成済み) を
        単発 fetch では取りこぼす件の回帰テスト。
        """
        receive_normal = success_response({"received_point": 5})
        receive_asobi = success_response({"received_point": 2})
        fake = FakePage([
            _listing([_mission(96, 5, completed=True, received=False)]),
            _listing([_mission(21, 2, completed=True, received=False)]),
            receive_normal,
            receive_asobi,
        ])

        gained = canvasser.collect_missions(
            _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
        )

        assert gained == 5 + 2
        assert len(fake.calls) == 4

    def test_ASOBI_STORE_fetch失敗時は通常mission_typeのPUTも送らない(
        self, tmp_path: Path
    ) -> None:
        """後段 fetch エラーで前段 PUT が消えて 0 gained と誤記録される事故を防ぐ。

        両 listing の取得を先に済ませてから POST/PUT を送る設計により、後段
        fetch 失敗時は 1 件も送っていない状態で RuntimeError を上げる。既に成功
        した PUT が run summary から消える (集計 0 gained) 事故を回避する。
        """
        fake = FakePage([
            _listing([_mission(96, 5, completed=True, received=False)]),
            {"status": 500, "body": None, "error": "boom"},
        ])

        with pytest.raises(RuntimeError, match="ASOBI STORE"):
            canvasser.collect_missions(
                _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
            )

        # 2 GET のみで、通常一覧の PUT は 1 件も送られていない
        assert len(fake.calls) == 2


def test_collect_missions_e1926_recovery_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1926 検知で driver 起動 → 再走して成功すると gained が累積される。

    `_fetch_mission_listings` は 1 回だけ呼ばれる想定。driver 成功後は
    `_filter_listings` で E1926 だった mission_id (21) に絞って再走するため、
    応答キューは fetch 2 回 (mt=0/mt=1) + attempt 1 の POST/PUT + attempt 2 の
    POST/PUT のみでよく、2 回目の fetch 応答は積まない。
    """
    call_log: list[str] = []

    def fake_recovery(page: Page, profile_dir: Path, name: str) -> bool:
        """復旧 driver のモック。呼ばれたことだけ記録して成功を返す。"""
        call_log.append("driver")
        return True

    monkeypatch.setattr(canvasser, "_run_asobi_linkage_recovery", fake_recovery)
    fake = FakePage([
        # fetch (1 回のみ): mt=0 に通常ミッション 1 件、mt=1 に ASOBI 1 件
        _listing([_mission(10, 5)]),
        success_response({"missions": [_mission(21, 2)]}),
        # attempt 1: normal (#10) は POST/PUT とも成功、asobi (#21) は POST が E1926
        _OK,
        success_response({"received_point": 5}),
        _error_response("E1926"),
        # driver 成功 (mock) 後の attempt 2: #21 だけ再走、POST/PUT とも成功
        _OK,
        success_response({"received_point": 2}),
    ])

    gained = canvasser.collect_missions(
        _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
    )

    # gained = 5 (normal, attempt 1) + 2 (asobi, attempt 2) = 7、driver は 1 回のみ
    assert gained == 7
    assert call_log == ["driver"]


def test_collect_missions_e1926_driver_failure_returns_partial_gained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """driver が復旧に失敗したら、E1926 だったミッション分は諦め部分獲得を返す。"""

    def fake_recovery_fail(page: Page, profile_dir: Path, name: str) -> bool:
        return False

    monkeypatch.setattr(canvasser, "_run_asobi_linkage_recovery", fake_recovery_fail)
    fake = FakePage([
        _listing([_mission(10, 5)]),
        success_response({"missions": [_mission(21, 2)]}),
        _OK,
        success_response({"received_point": 5}),
        _error_response("E1926"),
    ])

    gained = canvasser.collect_missions(
        _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=True
    )

    assert gained == 5  # normal (#10) だけ回収、asobi (#21) は復旧失敗でスキップ


def test_collect_missions_dry_run_does_not_trigger_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run=True では E1926 が起きても driver を起動しない (副作用ゼロを維持)。"""
    call_log: list[str] = []

    def fake_recovery(page: Page, profile_dir: Path, name: str) -> bool:
        call_log.append("driver")
        return True

    monkeypatch.setattr(canvasser, "_run_asobi_linkage_recovery", fake_recovery)
    # dry-run では POST/PUT を送らないため実際には E1926 は発生しない想定だが、
    # 万が一混入しても driver を起動しないことを回帰的に確認する。
    fake = FakePage([_empty_listing(), _empty_listing()])

    canvasser.collect_missions(
        _as_page(fake), tmp_path, "test", dry_run=True, auto_relogin=True
    )

    assert call_log == []


def test_collect_missions_no_auto_relogin_skips_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto_relogin=False (--no-auto-relogin) では E1926 でも driver を起動しない。

    ユーザーが明示的に自動再ログインを opt-out している場合、driver 経由で
    BNID 保存パスワードが submit される経路も遮断する必要がある。
    E1926 だったミッションはスキップし、翌日再試行へ回す。
    """
    call_log: list[str] = []

    def fake_recovery(page: Page, profile_dir: Path, name: str) -> bool:
        call_log.append("driver")
        return True

    monkeypatch.setattr(canvasser, "_run_asobi_linkage_recovery", fake_recovery)
    fake = FakePage([
        _listing([_mission(10, 5)]),
        success_response({"missions": [_mission(21, 2)]}),
        _OK,
        success_response({"received_point": 5}),
        _error_response("E1926"),
    ])

    gained = canvasser.collect_missions(
        _as_page(fake), tmp_path, "test", dry_run=False, auto_relogin=False
    )

    assert gained == 5  # normal (#10) だけ回収、asobi (#21) は driver 起動せずスキップ
    assert call_log == []  # driver 起動されない


class TestComplete:
    """_complete の ecode 別の戻り値。"""

    def test_dryrunはPOSTせずokを返す(self) -> None:
        """dry_run=True では evaluate を呼ばず "ok" を返す。"""
        fake = FakePage([])

        assert canvasser._complete(_as_page(fake), 1, "m", dry_run=True) == "ok"
        assert fake.calls == []

    @pytest.mark.parametrize(
        "response, expected",
        [
            (_OK, "ok"),
            (_error_response("E1906"), "already_done"),
            (_error_response("E1924"), "condition_unmet"),
            (_error_response("E9999"), "error"),
            ({"status": 0, "body": None, "error": "fetch failed"}, "error"),
        ],
    )
    def test_応答別の判定(self, response: dict[str, Any], expected: str) -> None:
        """成功・既達成・条件未達・未知エラーをそれぞれの文字列に写す。"""
        fake = FakePage([response])

        assert canvasser._complete(_as_page(fake), 1, "m", dry_run=False) == expected


class TestReceive:
    """_receive の票数集計。"""

    def test_dryrunはPUTせずptsを返す(self) -> None:
        """dry_run=True では evaluate を呼ばず pts をそのまま返す。"""
        fake = FakePage([])

        got = canvasser._receive(_as_page(fake), 1, "m", pts=30, dry_run=True)

        assert got == canvasser.MissionOutcome(gained=30)
        assert fake.calls == []

    def test_成功時はptsを返す(self) -> None:
        """PUT 成功で受取分の pts を返す。"""
        response = success_response({"received_point": 30})
        fake = FakePage([response])

        got = canvasser._receive(_as_page(fake), 1, "m", pts=30, dry_run=False)

        assert got == canvasser.MissionOutcome(gained=30)

    def test_失敗時は0を返す(self) -> None:
        """PUT 失敗では票数に計上しない。"""
        fake = FakePage([_error_response("E9999")])

        got = canvasser._receive(_as_page(fake), 1, "m", pts=30, dry_run=False)

        assert got == canvasser.MissionOutcome()


def test_receive_returns_linkage_expired_on_e1926() -> None:
    """`_receive` は受取 PUT が E1926 を返したら linkage_expired_id を伝搬する。"""
    fake = FakePage([_error_response("E1926")])

    result = canvasser._receive(_as_page(fake), 21, "ASOBI", pts=2, dry_run=False)

    assert result == canvasser.MissionOutcome(linkage_expired_id=21)


def test_complete_returns_linkage_expired_on_e1926() -> None:
    """`_complete` は達成 POST が E1926 を返したら "linkage_expired" を返す。"""
    fake = FakePage([_error_response("E1926")])

    got = canvasser._complete(_as_page(fake), 21, "ASOBI", dry_run=False)

    assert got == "linkage_expired"


def test_process_one_mission_propagates_linkage_expired_from_complete() -> None:
    """達成 POST (`_complete`) が E1926 を返す経路で linkage_expired_id を伝搬する。"""
    fake = FakePage([_error_response("E1926")])
    m = {
        "mission_id": 21,
        "mission_name": "ASOBI",
        "mission_point": 2,
        "action": {"mission_complete_api_call_flag": True},
        "is_mission_completed": False,
        "is_mission_received": False,
        "remaining_completable_count": 1,
    }

    result = canvasser._process_one_mission(_as_page(fake), m, dry_run=False)

    assert result == canvasser.MissionOutcome(linkage_expired_id=21)


def test_process_one_mission_propagates_linkage_expired_from_receive() -> None:
    """達成済み + 未受取のミッションで受取 PUT (`_receive`) が E1926 を返す経路。"""
    fake = FakePage([_error_response("E1926")])
    m = {
        "mission_id": 21,
        "mission_name": "ASOBI",
        "mission_point": 2,
        "action": {"mission_complete_api_call_flag": False},
        "is_mission_completed": True,
        "is_mission_received": False,
        "remaining_completable_count": 0,
    }

    result = canvasser._process_one_mission(_as_page(fake), m, dry_run=False)

    assert result == canvasser.MissionOutcome(linkage_expired_id=21)

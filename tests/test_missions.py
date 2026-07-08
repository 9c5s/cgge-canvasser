"""ミッション回収フロー (collect_missions / _complete / _receive) のテスト。

FakePage で API 応答を差し替え、dry-run と本番実行の分岐・ecode 別の
ハンドリング・獲得票数の集計を検証する。E1926 (ASOBI 連携トークン切れ) 検知時に
`_complete`/`_receive`/`_process_one_mission` が `MissionOutcome` で
`linkage_expired_id` を伝搬することも検証する。
"""

from typing import Any

import pytest

import canvasser
from tests._fakes import (
    FakePage,
    as_page as _as_page,
    error_response as _error_response,
    success_response,
)


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

    def test_dryrunは達成可能ミッションの見込み票数を返す(self) -> None:
        """dry-run では GET 2 回 (通常 + ASOBI STORE) のみで、POST/PUT を送らない。"""
        fake = FakePage([_listing([_mission(1, 30)]), _empty_listing()])

        gained = canvasser.collect_missions(_as_page(fake), dry_run=True)

        assert gained == 30
        assert len(fake.calls) == 2

    def test_本番実行は達成と受取を実行して票数を返す(self) -> None:
        """本番実行では両一覧 GET を先に済ませてから POST → PUT を送る。"""
        receive_ok = success_response({"received_point": 30})
        fake = FakePage([
            _listing([_mission(1, 30)]),
            _empty_listing(),
            _OK,
            receive_ok,
        ])

        gained = canvasser.collect_missions(_as_page(fake), dry_run=False)

        assert gained == 30
        assert len(fake.calls) == 4

    def test_達成済み未受取は受取のみ行う(self) -> None:
        """is_mission_completed=True かつ未受取なら受取 PUT だけを送る。"""
        receive_ok = success_response({"received_point": 10})
        fake = FakePage([
            _listing([_mission(2, 10, completed=True, received=False)]),
            _empty_listing(),
            receive_ok,
        ])

        gained = canvasser.collect_missions(_as_page(fake), dry_run=False)

        assert gained == 10
        assert len(fake.calls) == 3

    def test_APIフラグなしミッションは対象外(self) -> None:
        """flag=False かつ未達成なら達成も受取も送らない。"""
        fake = FakePage([
            _listing([_mission(3, 50, flag=False)]),
            _empty_listing(),
        ])

        gained = canvasser.collect_missions(_as_page(fake), dry_run=False)

        assert gained == 0
        assert len(fake.calls) == 2

    def test_APIフラグなしでも達成済み未受取は受取のみ行う(self) -> None:
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

        gained = canvasser.collect_missions(_as_page(fake), dry_run=False)

        assert gained == 15
        assert len(fake.calls) == 3

    def test_APIフラグなしで達成済み受取済みは何もしない(self) -> None:
        """flag=False かつ既に受取済みなら PUT も送らない (重複受取回避)。"""
        fake = FakePage([
            _listing([_mission(7, 15, flag=False, completed=True, received=True)]),
            _empty_listing(),
        ])

        gained = canvasser.collect_missions(_as_page(fake), dry_run=False)

        assert gained == 0
        assert len(fake.calls) == 2

    def test_達成済みE1906でも受取を試みる(self) -> None:
        """達成 POST が E1906 (既達成) を返しても受取 PUT は送る。"""
        receive_ok = success_response({"received_point": 20})
        fake = FakePage([
            _listing([_mission(4, 20)]),
            _empty_listing(),
            _error_response("E1906"),
            receive_ok,
        ])

        gained = canvasser.collect_missions(_as_page(fake), dry_run=False)

        assert gained == 20
        assert len(fake.calls) == 4

    def test_条件未達E1924は受取を送らない(self) -> None:
        """達成 POST が E1924 (条件未達) なら受取 PUT を送らずスキップする。"""
        fake = FakePage([
            _listing([_mission(5, 20)]),
            _empty_listing(),
            _error_response("E1924"),
        ])

        gained = canvasser.collect_missions(_as_page(fake), dry_run=False)

        assert gained == 0
        assert len(fake.calls) == 3

    def test_一覧取得失敗はRuntimeError(self) -> None:
        """通常一覧 GET が失敗すれば ASOBI STORE 到達前に RuntimeError で止める。"""
        fake = FakePage([{"status": 500, "body": None, "error": "boom"}])

        with pytest.raises(RuntimeError, match="通常"):
            canvasser.collect_missions(_as_page(fake), dry_run=True)

    def test_ASOBI_STORE一覧の取得失敗もRuntimeError(self) -> None:
        """通常一覧が取れても ASOBI STORE 一覧が失敗すれば RuntimeError で止める。"""
        fake = FakePage([
            _listing([]),
            {"status": 500, "body": None, "error": "boom"},
        ])

        with pytest.raises(RuntimeError, match="ASOBI STORE"):
            canvasser.collect_missions(_as_page(fake), dry_run=True)

    def test_通常とASOBI_STOREの両方から受取を集計する(self) -> None:
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

        gained = canvasser.collect_missions(_as_page(fake), dry_run=False)

        assert gained == 5 + 2
        assert len(fake.calls) == 4

    def test_ASOBI_STORE_fetch失敗時は通常mission_typeのPUTも送らない(self) -> None:
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
            canvasser.collect_missions(_as_page(fake), dry_run=False)

        # 2 GET のみで、通常一覧の PUT は 1 件も送られていない
        assert len(fake.calls) == 2


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

        got = canvasser._receive(_as_page(fake), 1, "m", 30, dry_run=True)

        assert got == canvasser.MissionOutcome(gained=30)
        assert fake.calls == []

    def test_成功時はptsを返す(self) -> None:
        """PUT 成功で受取分の pts を返す。"""
        response = success_response({"received_point": 30})
        fake = FakePage([response])

        got = canvasser._receive(_as_page(fake), 1, "m", 30, dry_run=False)

        assert got == canvasser.MissionOutcome(gained=30)

    def test_失敗時は0を返す(self) -> None:
        """PUT 失敗では票数に計上しない。"""
        fake = FakePage([_error_response("E9999")])

        got = canvasser._receive(_as_page(fake), 1, "m", 30, dry_run=False)

        assert got == canvasser.MissionOutcome()


def test_receive_returns_gained_on_success() -> None:
    """`_receive` は PUT 成功時、獲得票数入りの MissionOutcome を返す。"""
    fake = FakePage([success_response({"received_point": 5})])

    result = canvasser._receive(_as_page(fake), 1, "テスト", 5, dry_run=False)

    assert result == canvasser.MissionOutcome(gained=5)


def test_receive_returns_linkage_expired_on_e1926() -> None:
    """`_receive` は受取 PUT が E1926 を返したら linkage_expired_id を伝搬する。"""
    fake = FakePage([_error_response("E1926")])

    result = canvasser._receive(_as_page(fake), 21, "ASOBI", 2, dry_run=False)

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

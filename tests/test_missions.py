"""ミッション回収フロー (collect_missions / _complete / _receive) のテスト。

FakePage で API 応答を差し替え、dry-run と execute の分岐・ecode 別の
ハンドリング・獲得票数の集計を検証する。
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


_OK = success_response()


class TestCollectMissions:
    """collect_missions の全体フロー。"""

    def test_dryrunは達成可能ミッションの見込み票数を返す(self) -> None:
        """dry-run では GET 1 回のみで、POST/PUT を送らず見込みを集計する。"""
        fake = FakePage([_listing([_mission(1, 30)])])

        gained = canvasser.collect_missions(_as_page(fake), execute=False)

        assert gained == 30
        assert len(fake.calls) == 1

    def test_executeは達成と受取を実行して票数を返す(self) -> None:
        """execute では POST → PUT の順で送信し、成功分の pts を集計する。"""
        receive_ok = success_response({"received_point": 30})
        fake = FakePage([_listing([_mission(1, 30)]), _OK, receive_ok])

        gained = canvasser.collect_missions(_as_page(fake), execute=True)

        assert gained == 30
        assert len(fake.calls) == 3

    def test_達成済み未受取は受取のみ行う(self) -> None:
        """is_mission_completed=True かつ未受取なら受取 PUT だけを送る。"""
        receive_ok = success_response({"received_point": 10})
        fake = FakePage([
            _listing([_mission(2, 10, completed=True, received=False)]),
            receive_ok,
        ])

        gained = canvasser.collect_missions(_as_page(fake), execute=True)

        assert gained == 10
        assert len(fake.calls) == 2

    def test_APIフラグなしミッションは対象外(self) -> None:
        """mission_complete_api_call_flag=False は達成も受取も送らない。"""
        fake = FakePage([_listing([_mission(3, 50, flag=False)])])

        gained = canvasser.collect_missions(_as_page(fake), execute=True)

        assert gained == 0
        assert len(fake.calls) == 1

    def test_達成済みE1906でも受取を試みる(self) -> None:
        """達成 POST が E1906 (既達成) を返しても受取 PUT は送る。"""
        receive_ok = success_response({"received_point": 20})
        fake = FakePage([
            _listing([_mission(4, 20)]),
            _error_response("E1906"),
            receive_ok,
        ])

        gained = canvasser.collect_missions(_as_page(fake), execute=True)

        assert gained == 20
        assert len(fake.calls) == 3

    def test_条件未達E1924は受取を送らない(self) -> None:
        """達成 POST が E1924 (条件未達) なら受取 PUT を送らずスキップする。"""
        fake = FakePage([_listing([_mission(5, 20)]), _error_response("E1924")])

        gained = canvasser.collect_missions(_as_page(fake), execute=True)

        assert gained == 0
        assert len(fake.calls) == 2

    def test_一覧取得失敗はRuntimeError(self) -> None:
        """一覧 GET が失敗したら RuntimeError で全体を止める。"""
        fake = FakePage([{"status": 500, "body": None, "error": "boom"}])

        with pytest.raises(RuntimeError, match="ミッション一覧の取得に失敗"):
            canvasser.collect_missions(_as_page(fake), execute=False)


class TestComplete:
    """_complete の ecode 別の戻り値。"""

    def test_dryrunはPOSTせずokを返す(self) -> None:
        """execute=False では evaluate を呼ばず "ok" を返す。"""
        fake = FakePage([])

        assert canvasser._complete(_as_page(fake), 1, "m", execute=False) == "ok"
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

        assert canvasser._complete(_as_page(fake), 1, "m", execute=True) == expected


class TestReceive:
    """_receive の票数集計。"""

    def test_dryrunはPUTせずptsを返す(self) -> None:
        """execute=False では evaluate を呼ばず pts をそのまま返す。"""
        fake = FakePage([])

        got = canvasser._receive(_as_page(fake), 1, "m", 30, execute=False)

        assert got == 30
        assert fake.calls == []

    def test_成功時はptsを返す(self) -> None:
        """PUT 成功で受取分の pts を返す。"""
        response = success_response({"received_point": 30})
        fake = FakePage([response])

        assert canvasser._receive(_as_page(fake), 1, "m", 30, execute=True) == 30

    def test_失敗時は0を返す(self) -> None:
        """PUT 失敗では票数に計上しない。"""
        fake = FakePage([_error_response("E9999")])

        assert canvasser._receive(_as_page(fake), 1, "m", 30, execute=True) == 0

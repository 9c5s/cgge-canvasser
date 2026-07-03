"""チェックイン走行 (collect_checkins / _CheckinRunner) のテスト。

FakePage と now_fn 注入で外部依存を断ち、dry-run 経路のフロー全体と
安全装置 (fail closed・budget・期限 skip) を検証する。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest

import canvasser
from canvasser import JST, CheckinSettings, FailClosedError
from tests._fakes import (
    FakePage,
    as_page as _as_page,
    error_response,
    success_response,
)

if TYPE_CHECKING:
    from pathlib import Path

_FIXED_NOW = datetime(2026, 7, 3, 10, 0, tzinfo=JST)


def _spot(
    num: int,
    lat: float,
    lng: float,
    deadline: str = "2026-12-31 23:59:59",
) -> dict[str, Any]:
    """チェックインスポット 1 件分の dict を組み立てる。"""
    return {
        "slug": f"cg_vote2026_{num}",
        "name": f"スポット{num}",
        "location_latitude": lat,
        "location_longitude": lng,
        "checkin_radius": 500,
        "checkin_end_datetime": deadline,
    }


def _typed(
    num: int,
    lat: float,
    lng: float,
    deadline: str = "2026-12-31 23:59:59",
) -> canvasser.Spot:
    """正規化済み Spot を組み立てる (runner 単体メソッドのテスト用)。"""
    return canvasser.Spot.from_api(_spot(num, lat, lng, deadline))


def _listing(spots: list[dict[str, Any]]) -> dict[str, Any]:
    """スポット一覧 GET の成功応答を組み立てる。"""
    return success_response({"spots": spots})


def _no_sleep(_seconds: float) -> None:
    """実待機を無効化する sleep_fn 代替。"""


def _settings(
    *,
    execute: bool = False,
    daily_budget: int = 0,
    consecutive_failure_limit: int = 1,
    out_of_range_limit: int = 3,
    profile_dir: Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> CheckinSettings:
    """固定時刻・実待機なしのテスト用設定を組み立てる。"""
    return CheckinSettings(
        execute=execute,
        daily_budget=daily_budget,
        consecutive_failure_limit=consecutive_failure_limit,
        out_of_range_limit=out_of_range_limit,
        profile_dir=profile_dir,
        now_fn=now_fn or (lambda: _FIXED_NOW),
        sleep_fn=sleep_fn or _no_sleep,
    )


def _runner(
    spots: list[canvasser.Spot] | None = None,
    settings: CheckinSettings | None = None,
) -> canvasser._CheckinRunner:
    """単体テスト用の _CheckinRunner を組み立てる。"""
    return canvasser._CheckinRunner(
        page=_as_page(FakePage([])),
        settings=settings or _settings(),
        spots=spots or [],
        virtual_now=_FIXED_NOW,
    )


class TestSpotFromApi:
    """Spot.from_api の正規化。"""

    def test_文字列の座標と半径もfloatへ正規化する(self) -> None:
        """API が文字列で返しても境界で float に確定する。"""
        raw = _spot(1, 35.0, 135.0)
        raw["location_latitude"] = "35.5"
        raw["checkin_radius"] = "300"

        spot = canvasser.Spot.from_api(raw)

        assert spot.lat == 35.5
        assert spot.radius == 300.0

    def test_radius欠落は500mを既定にする(self) -> None:
        """checkin_radius が無いスポットは UI 既定と同じ 500m になる。"""
        raw = _spot(1, 35.0, 135.0)
        raw["checkin_radius"] = None

        assert canvasser.Spot.from_api(raw).radius == 500.0

    def test_deadlineはJSTのawareへパースし原文を保持する(self) -> None:
        """deadline は境界で一度だけパースされ、原文は表示用に残る。"""
        spot = canvasser.Spot.from_api(_spot(1, 35.0, 135.0))

        assert spot.deadline == datetime(2026, 12, 31, 23, 59, 59, tzinfo=JST)
        assert spot.deadline_raw == "2026-12-31 23:59:59"

    def test_パース不能なdeadlineはNoneで原文を保持する(self) -> None:
        """未知形式は deadline=None になり、エラー表示用の原文だけ残る。"""
        spot = canvasser.Spot.from_api(_spot(1, 35.0, 135.0, deadline="31/07/2026"))

        assert spot.deadline is None
        assert spot.deadline_raw == "31/07/2026"


class TestFetchCheckinSpots:
    """_fetch_checkin_spots の応答ハンドリング。"""

    def test_成功応答からspotsをSpotへ正規化して返す(self) -> None:
        """payload.spots の各 dict が Spot に変換されて返る。"""
        spots = [_spot(1, 35.0, 135.0)]
        fake = FakePage([_listing(spots)])

        got = canvasser._fetch_checkin_spots(_as_page(fake))

        assert got == [canvasser.Spot.from_api(s) for s in spots]

    def test_失敗応答はRuntimeError(self) -> None:
        """HTTP エラーは RuntimeError で全体を止める。"""
        fake = FakePage([{"status": 503, "body": None, "error": "down"}])

        with pytest.raises(RuntimeError, match="チェックインイベント取得に失敗"):
            canvasser._fetch_checkin_spots(_as_page(fake))


class TestPartitionSpots:
    """_partition_spots の完了済み除外。"""

    def test_完了済みslugを除外して件数を返す(self) -> None:
        """completed_spots にある slug は除外され skip 件数に計上される。"""
        spots = [_typed(1, 35.0, 135.0), _typed(2, 35.1, 135.0)]

        remaining, skipped = canvasser._partition_spots(spots, {"cg_vote2026_1"})

        assert [s.slug for s in remaining] == ["cg_vote2026_2"]
        assert skipped == 1

    def test_完了なしなら全件そのまま(self) -> None:
        """completed_spots が空なら除外は発生しない。"""
        spots = [_typed(1, 35.0, 135.0)]

        remaining, skipped = canvasser._partition_spots(spots, set())

        assert remaining == spots
        assert skipped == 0


class TestInitialVirtualNow:
    """_initial_virtual_now の再開時刻決定。"""

    def test_resumeが未来なら再開時刻を使う(self) -> None:
        """前回の仮想終了時刻が現在より先なら連続扱いで引き継ぐ。"""
        resume_at = _FIXED_NOW + timedelta(hours=5)

        got = canvasser._initial_virtual_now(_settings(), resume_at)

        assert got == resume_at

    def test_resumeが過去なら現在時刻を使う(self) -> None:
        """再開時刻が過去なら now_fn の現在時刻を採用する。"""
        resume_at = _FIXED_NOW - timedelta(hours=5)

        got = canvasser._initial_virtual_now(_settings(), resume_at)

        assert got == _FIXED_NOW

    def test_naiveな現在時刻はJSTを付与する(self) -> None:
        """now_fn が naive を返しても JST の aware に揃える。"""
        naive = datetime(2026, 7, 3, 10, 0)  # noqa: DTZ001 -- naive 入力の検証が目的

        got = canvasser._initial_virtual_now(_settings(now_fn=lambda: naive), None)

        assert got == _FIXED_NOW
        assert got.tzinfo is not None


class TestBudgetReached:
    """_CheckinRunner._budget_reached の上限判定。"""

    def test_budget0は無制限(self) -> None:
        """daily_budget=0 ではどれだけ進んでも上限に達しない。"""
        runner = _runner(settings=_settings(daily_budget=0))
        runner.successful = 1000

        assert runner._budget_reached() is False

    def test_executeはattemptedで判定する(self) -> None:
        """execute=True では実 POST 試行回数がカウンタになる。"""
        runner = _runner(settings=_settings(execute=True, daily_budget=2))
        runner.attempted = 2
        runner.successful = 0

        assert runner._budget_reached() is True

    def test_dryrunはsuccessfulで判定する(self) -> None:
        """dry-run では成功見込み件数がカウンタになる。"""
        runner = _runner(settings=_settings(daily_budget=2))
        runner.attempted = 0
        runner.successful = 1

        assert runner._budget_reached() is False


class TestWithinDeadline:
    """_CheckinRunner._within_deadline の期限判定。"""

    def test_期限内はTrue(self) -> None:
        """仮想時刻が期限より前なら処理を続行する。"""
        runner = _runner()
        spot = _typed(1, 35.0, 135.0, deadline="2026-12-31 23:59:59")

        assert runner._within_deadline(spot) is True

    def test_期限切れはskipして移動起点を進める(self) -> None:
        """期限超過スポットは skip しつつ prev 座標を現地点へ更新する。"""
        runner = _runner()
        spot = _typed(1, 35.0, 135.0, deadline="2026-01-01 00:00:00")

        assert runner._within_deadline(spot) is False
        assert (runner.prev_lat, runner.prev_lng) == (35.0, 135.0)

    def test_パース不能はdryrunでskipする(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """dry-run では期限パース失敗を警告付き skip に丸める。"""
        runner = _runner()
        spot = _typed(1, 35.0, 135.0, deadline="31/07/2026")

        assert runner._within_deadline(spot) is False
        assert "パースできません" in capsys.readouterr().err

    def test_パース不能はexecuteでfail_closed(self) -> None:
        """execute では期限パース失敗を FailClosedError で即停止する。"""
        runner = _runner(settings=_settings(execute=True))
        runner.gained = 30
        spot = _typed(1, 35.0, 135.0, deadline="31/07/2026")

        with pytest.raises(FailClosedError, match="パースできません") as ei:
            runner._within_deadline(spot)

        assert ei.value.partial_gained == 30


class TestOutOfRangeHandling:
    """_CheckinRunner._on_out_of_range の累積停止。"""

    def test_閾値未満はskipで継続する(self) -> None:
        """E5005 が上限未満なら移動起点だけ進めて継続する。"""
        runner = _runner(settings=_settings(out_of_range_limit=3))
        spot = _typed(1, 35.0, 135.0)

        runner._on_out_of_range(spot, "E5005")
        runner._on_out_of_range(spot, "E5005")

        assert runner.out_of_range_count == 2
        assert (runner.prev_lat, runner.prev_lng) == (35.0, 135.0)

    def test_閾値到達でfail_closed(self) -> None:
        """E5005 が累積上限に達したら FailClosedError で全体を止める。"""
        runner = _runner(settings=_settings(out_of_range_limit=2))
        runner.gained = 10
        spot = _typed(1, 35.0, 135.0)
        runner._on_out_of_range(spot, "E5005")

        with pytest.raises(FailClosedError, match="E5005") as ei:
            runner._on_out_of_range(spot, "E5005")

        assert ei.value.partial_gained == 10


class TestUnknownEcodeHandling:
    """_CheckinRunner._on_unknown_ecode の連続失敗停止。"""

    def test_既定は1件目で即fail_closed(self) -> None:
        """consecutive_failure_limit=1 (既定) では未知 ecode 1 件で停止する。"""
        runner = _runner(settings=_settings(consecutive_failure_limit=1))
        runner.gained = 20
        res: dict[str, Any] = {
            "status": 400,
            "body": {"status": "ERROR", "payload": {"ecode": "E9999"}},
        }

        with pytest.raises(FailClosedError, match="連続失敗") as ei:
            runner._on_unknown_ecode(_typed(1, 35.0, 135.0), res)

        assert ei.value.partial_gained == 20

    def test_閾値未満は移動起点を進めて継続する(self) -> None:
        """上限 2 なら 1 件目は skip 扱いで prev を現地点へ進める。"""
        runner = _runner(settings=_settings(consecutive_failure_limit=2))
        res: dict[str, Any] = {"status": 400, "body": None}

        runner._on_unknown_ecode(_typed(1, 35.0, 135.0), res)

        assert runner.consecutive_failures == 1
        assert (runner.prev_lat, runner.prev_lng) == (35.0, 135.0)


class TestFailClosedError:
    """FailClosedError の付帯情報。"""

    def test_partial_gainedの既定は0(self) -> None:
        """獲得票数を指定しなければ 0 で初期化される。"""
        assert FailClosedError("msg").partial_gained == 0

    def test_partial_gainedを保持する(self) -> None:
        """中断前の獲得票数をそのまま保持する。"""
        assert FailClosedError("msg", partial_gained=40).partial_gained == 40


class TestCollectCheckinsDryRun:
    """collect_checkins の dry-run 経路。"""

    def test_全スポットの見込み票数を返す(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """2 スポット dry-run は 1 件 10 票の 2 件分 = 20 票の見込みを返す。"""
        random.seed(0)
        spots = [_spot(1, 35.00, 135.0), _spot(2, 35.01, 135.0)]
        fake = FakePage([_listing(spots)])

        gained = canvasser.collect_checkins(_as_page(fake), _settings())

        assert gained == 20
        assert len(fake.calls) == 1
        assert "DRY-RUN" in capsys.readouterr().out

    def test_daily_budgetで見込み件数を打ち切る(self) -> None:
        """daily_budget=1 の dry-run は 1 件分の見込みで停止する。"""
        random.seed(0)
        spots = [_spot(1, 35.00, 135.0), _spot(2, 35.01, 135.0)]
        fake = FakePage([_listing(spots)])

        gained = canvasser.collect_checkins(_as_page(fake), _settings(daily_budget=1))

        assert gained == 10

    def test_スポットが空なら0(self, capsys: pytest.CaptureFixture[str]) -> None:
        """イベントにスポットが無ければ何もせず 0 を返す。"""
        fake = FakePage([_listing([])])

        gained = canvasser.collect_checkins(_as_page(fake), _settings())

        assert gained == 0
        assert "空でした" in capsys.readouterr().out

    def test_全件完了済みなら0(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """completed_spots に全 slug があれば走行せず 0 を返す。"""
        spots = [_spot(1, 35.00, 135.0), _spot(2, 35.01, 135.0)]
        fake = FakePage([_listing(spots)])
        canvasser.save_account_state(
            tmp_path, {"completed_spots": ["cg_vote2026_1", "cg_vote2026_2"]}
        )

        gained = canvasser.collect_checkins(
            _as_page(fake), _settings(profile_dir=tmp_path)
        )

        assert gained == 0
        assert "全スポット完了済み" in capsys.readouterr().out

    def test_期限切れスポットはskipされ票に計上しない(self) -> None:
        """全スポット期限切れなら見込み 0 票で走行を終える。"""
        random.seed(0)
        spots = [_spot(1, 35.00, 135.0, deadline="2026-01-01 00:00:00")]
        fake = FakePage([_listing(spots)])

        gained = canvasser.collect_checkins(_as_page(fake), _settings())

        assert gained == 0

    def test_破損stateはdryrunなら空stateで継続する(self, tmp_path: Path) -> None:
        """dry-run では state 破損を空 state に丸めて走行を続ける。

        非 strict の load が破損を空 dict に吸収するため、警告なしで resume 情報
        ゼロの初回相当として走る。
        """
        random.seed(0)
        (tmp_path / "canvasser_state.json").write_text("{{{", encoding="utf-8")
        spots = [_spot(1, 35.00, 135.0)]
        fake = FakePage([_listing(spots)])

        gained = canvasser.collect_checkins(
            _as_page(fake), _settings(profile_dir=tmp_path)
        )

        assert gained == 10

    def test_破損stateはexecuteならfail_closed(self, tmp_path: Path) -> None:
        """execute では state 破損を FailClosedError で即停止する。"""
        (tmp_path / "canvasser_state.json").write_text("{{{", encoding="utf-8")
        spots = [_spot(1, 35.00, 135.0)]
        fake = FakePage([_listing(spots)])

        with pytest.raises(FailClosedError, match="破損しています"):
            canvasser.collect_checkins(
                _as_page(fake), _settings(execute=True, profile_dir=tmp_path)
            )


_POST_OK: dict[str, Any] = success_response()


class TestCollectCheckinsExecute:
    """collect_checkins の execute (実 POST) 成功経路。

    実待機は sleep_fn 注入で無効化し、待機の発生自体は呼び出し記録で検証する。
    """

    def test_成功POSTで票と状態を確定する(self, tmp_path: Path) -> None:
        """2 スポット成功で 20 票獲得し completed_spots に両 slug が入る。"""
        random.seed(0)
        sleeps: list[float] = []
        spots = [_spot(1, 35.00, 135.0), _spot(2, 35.01, 135.0)]
        fake = FakePage([_listing(spots), _POST_OK, _POST_OK])

        gained = canvasser.collect_checkins(
            _as_page(fake),
            _settings(execute=True, profile_dir=tmp_path, sleep_fn=sleeps.append),
        )

        assert gained == 20
        assert len(fake.calls) == 3
        state = canvasser.load_account_state(tmp_path)
        assert state["completed_spots"] == ["cg_vote2026_1", "cg_vote2026_2"]
        assert state["last_checkin"]["schema_version"] == 2
        # 1 件目の滞在と 2 件目への移動で実待機が発生している
        assert len(sleeps) == 2
        assert all(s > 0 for s in sleeps)

    def test_daily_budgetが実POST試行を打ち切る(self, tmp_path: Path) -> None:
        """daily_budget=1 では実 POST を 1 件だけ送って停止する。"""
        random.seed(0)
        spots = [_spot(1, 35.00, 135.0), _spot(2, 35.01, 135.0)]
        fake = FakePage([_listing(spots), _POST_OK])

        gained = canvasser.collect_checkins(
            _as_page(fake),
            _settings(execute=True, daily_budget=1, profile_dir=tmp_path),
        )

        assert gained == 10
        # listing GET + POST 1 件のみで、2 件目の POST は送られない
        assert len(fake.calls) == 2
        state = canvasser.load_account_state(tmp_path)
        assert len(state["completed_spots"]) == 1

    def test_未知ecodeは1件目で中断し獲得分を保持する(self, tmp_path: Path) -> None:
        """成功 1 件の後の未知 ecode で partial_gained=10 の fail closed になる。"""
        random.seed(0)
        spots = [_spot(1, 35.00, 135.0), _spot(2, 35.01, 135.0)]
        fake = FakePage([_listing(spots), _POST_OK, error_response("E9999")])

        with pytest.raises(FailClosedError, match="連続失敗") as ei:
            canvasser.collect_checkins(
                _as_page(fake), _settings(execute=True, profile_dir=tmp_path)
            )

        assert ei.value.partial_gained == 10
        # 成功済み 1 件分の state は中断後も残っている
        state = canvasser.load_account_state(tmp_path)
        assert len(state["completed_spots"]) == 1

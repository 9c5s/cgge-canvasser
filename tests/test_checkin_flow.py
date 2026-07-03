"""チェックイン走行 (collect_checkins / _CheckinRunner) のテスト。

FakePage と now_fn 注入で外部依存を断ち、dry-run 経路のフロー全体と
安全装置 (fail closed・budget・期限 skip) を検証する。
"""

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

    def test_NaNの緯度は境界で拒否する(self) -> None:
        """JSON 由来の NaN 座標は距離計算を壊すため境界で ValueError。"""
        raw = _spot(1, 35.0, 135.0)
        raw["location_latitude"] = float("nan")

        with pytest.raises(ValueError, match="非有限値"):
            canvasser.Spot.from_api(raw)

    def test_Infinityの経度は境界で拒否する(self) -> None:
        """Infinity 座標も距離計算を壊すため境界で ValueError。"""
        raw = _spot(1, 35.0, 135.0)
        raw["location_longitude"] = float("inf")

        with pytest.raises(ValueError, match="非有限値"):
            canvasser.Spot.from_api(raw)

    def test_bool座標は数値扱いしない(self) -> None:
        """bool は int の subclass だが座標としては通さない (TypeError)。"""
        raw = _spot(1, 35.0, 135.0)
        raw["location_latitude"] = True

        with pytest.raises(TypeError, match="数値ではない"):
            canvasser.Spot.from_api(raw)

    def test_緯度の範囲外は拒否する(self) -> None:
        """地球の緯度範囲 [-90, 90] を超える値は境界で ValueError。"""
        with pytest.raises(ValueError, match="緯度の範囲外"):
            canvasser.Spot.from_api(_spot(1, 91.0, 135.0))

    def test_経度の範囲外は拒否する(self) -> None:
        """地球の経度範囲 [-180, 180] を超える値は境界で ValueError。"""
        with pytest.raises(ValueError, match="経度の範囲外"):
            canvasser.Spot.from_api(_spot(1, 35.0, 181.0))

    def test_非正のradiusは拒否する(self) -> None:
        """radius が 0 以下は「巡回範囲なし」となり spot として不整合。"""
        raw = _spot(1, 35.0, 135.0)
        raw["checkin_radius"] = -1

        with pytest.raises(ValueError, match="正の値でない"):
            canvasser.Spot.from_api(raw)


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

    def test_到着予定が期限超過なら移動前にskip(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """virtual_now が期限内でも、planned_arrival が期限超過なら skip する。"""
        runner = _runner()
        # virtual_now=10:00 (fixture)、deadline=10:30、planned_arrival=12:00
        spot = _typed(1, 35.0, 135.0, deadline="2026-07-03 10:30:00")
        planned = datetime(2026, 7, 3, 12, 0, tzinfo=JST)

        assert runner._within_deadline(spot, planned) is False
        assert (runner.prev_lat, runner.prev_lng) == (35.0, 135.0)
        assert "到着予定" in capsys.readouterr().out

    def test_到着予定が期限内なら継続(self) -> None:
        """planned_arrival が期限内なら継続 (skip しない)。"""
        runner = _runner()
        spot = _typed(1, 35.0, 135.0, deadline="2026-07-03 12:00:00")
        planned = datetime(2026, 7, 3, 11, 30, tzinfo=JST)

        assert runner._within_deadline(spot, planned) is True

    def test_planned_arrival省略時は従来のvirtual_now判定(self) -> None:
        """planned_arrival=None なら virtual_now vs deadline の従来判定に戻る。"""
        runner = _runner()
        # virtual_now=10:00、deadline=10:30 -> planned 省略なら period 内で True
        spot = _typed(1, 35.0, 135.0, deadline="2026-07-03 10:30:00")

        assert runner._within_deadline(spot) is True


class TestPlanTravelTo:
    """_plan_travel_to の計算専用性: sleep も virtual_now 進行も起こさない。"""

    def test_first_spotではNoneを返す(self) -> None:
        """prev がまだ無い最初のスポットへは移動計画が立たない。"""
        runner = _runner()

        assert runner._plan_travel_to(_typed(1, 35.0, 135.0)) is None

    def test_planは仮想時刻を進めない(self) -> None:
        """計画時点では sleep も virtual_now 進行も起こさない (実行は apply 側)。"""
        runner = _runner()
        runner.prev_lat, runner.prev_lng = 35.0, 135.0
        before = runner.virtual_now

        plan = runner._plan_travel_to(_typed(2, 35.01, 135.0))

        assert plan is not None
        assert plan.arrival > before  # 到着時刻は先だが
        assert runner.virtual_now == before  # 実際の時刻は進んでいない


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

    def _prime_resume_at(self, profile_dir: Path, lat: float) -> None:
        """既知位置 (lat, 135.0) を起点にする state を書き込む。

        collect_checkins は state が無ければ開始スポットを乱択するため、
        起点固定のテストでは前回位置を state に流し込んでおく。
        """
        canvasser.save_account_state(
            profile_dir,
            {
                "last_checkin": {
                    "schema_version": 2,
                    "spot_slug": "cg_vote2026_0",
                    "spot_name": "prev",
                    "location_latitude": lat,
                    "location_longitude": 135.0,
                    "virtual_completed_at": "2026-07-03T09:59:00+09:00",
                },
            },
        )

    def test_期限切れスポットへの移動sleepは発生しない(self, tmp_path: Path) -> None:
        """期限切れスポットは移動 sleep 前に skip する (実待機を無駄にしない)。

        起点 (34.99) → 1件目 (35.00, 期限内) → 2件目 (35.01, 期限切れ)
        → 3件目 (35.02, 期限内)。修正前は 1→2 の移動 sleep が発生してから
        skip していた。修正後は 1→2 の deadline check が sleep より先に走る
        ため、この移動 sleep は発生しない。
        """
        self._prime_resume_at(tmp_path, 34.99)
        random.seed(0)
        sleeps: list[float] = []
        spots = [
            _spot(1, 35.00, 135.0, deadline="2026-12-31 23:59:59"),
            _spot(2, 35.01, 135.0, deadline="2026-01-01 00:00:00"),
            _spot(3, 35.02, 135.0, deadline="2026-12-31 23:59:59"),
        ]
        fake = FakePage([_listing(spots), _POST_OK, _POST_OK])

        gained = canvasser.collect_checkins(
            _as_page(fake),
            _settings(execute=True, profile_dir=tmp_path, sleep_fn=sleeps.append),
        )

        # 期限切れ 1 件を skip、2 件成功で 20 票
        assert gained == 20
        # sleeps 構成: 起点→1 移動 / 1 滞在 / 2 (skip)→3 移動 の 3 件。
        # 修正前は 1→2 の移動 sleep も入り 4 件になっていた。
        assert len(sleeps) == 3
        assert all(s > 0 for s in sleeps)

    def test_deadlineパース不能はexecuteで移動sleep前にfail_closed(
        self, tmp_path: Path
    ) -> None:
        """deadline パース不能は fail_closed の前に移動 sleep を走らせない。

        起点 (34.99) → 1件目 (35.00, 成功) → 2件目 (35.01, パース不能) で
        fail_closed。修正前は 1→2 の移動 sleep が発生してから fail_closed
        だった。修正後は deadline check が sleep より先で、この移動は起きない。
        """
        self._prime_resume_at(tmp_path, 34.99)
        random.seed(0)
        sleeps: list[float] = []
        spots = [
            _spot(1, 35.00, 135.0, deadline="2026-12-31 23:59:59"),
            _spot(2, 35.01, 135.0, deadline="invalid-date"),
        ]
        fake = FakePage([_listing(spots), _POST_OK])

        with pytest.raises(FailClosedError, match="パースできません") as ei:
            canvasser.collect_checkins(
                _as_page(fake),
                _settings(execute=True, profile_dir=tmp_path, sleep_fn=sleeps.append),
            )

        assert ei.value.partial_gained == 10
        # sleeps: 起点→1 移動 / 1 滞在 の 2 件。1→2 の移動 sleep は発生しない
        # (もし発生していれば 3 件目のエントリになる)。
        assert len(sleeps) == 2

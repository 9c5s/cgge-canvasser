"""移動時間推定・時刻計算・期限パースのテスト。

GMAPS_KEY は conftest の autouse fixture で除去されるため、estimate_travel_seconds
は常に Haversine フォールバック経路を通る (外部通信なしの Small テスト)。
"""

import random
from datetime import datetime, timedelta
from typing import Any

import pytest

import canvasser
from canvasser import JST
from tests._fakes import FakeGmapsClient


class TestHumanizeDuration:
    """humanize_duration の表記変換。"""

    @pytest.mark.parametrize(
        "seconds, expected",
        [
            (0, "0s"),
            (59, "59s"),
            (60, "1m00s"),
            (90, "1m30s"),
            (3600, "1h00m"),
            (3661, "1h01m"),
            (5520, "1h32m"),
            (86400, "24h00m"),
        ],
    )
    def test_秒数を人間可読な表記へ変換する(
        self, seconds: float, expected: str
    ) -> None:
        """時間・分・秒の 3 段階で桁に応じた表記を選ぶ。"""
        assert canvasser.humanize_duration(seconds) == expected

    def test_小数秒は切り捨てる(self) -> None:
        """59.9 秒は 59s に切り捨てられる。"""
        assert canvasser.humanize_duration(59.9) == "59s"


class TestNaturalStaySeconds:
    """natural_stay_seconds の値域。"""

    def test_滞在時間は10分から30分の範囲(self) -> None:
        """定義済み下限 600 秒と上限 1800 秒に収まる。"""
        random.seed(0)
        values = [canvasser.natural_stay_seconds() for _ in range(100)]
        assert min(values) >= 600.0
        assert max(values) <= 1800.0


class TestEstimateTravelSecondsHaversine:
    """_estimate_travel_seconds_haversine の距離レンジ別推定。"""

    def test_同一点は徒歩0秒(self) -> None:
        """距離 0 は walk で 0 秒になる。"""
        got = canvasser._estimate_travel_seconds_haversine((35.0, 135.0), (35.0, 135.0))
        assert got == (0.0, "walk")

    def test_近距離は徒歩(self) -> None:
        """直線 334m (道路換算 450m) は徒歩 5km/h で約 324 秒。"""
        secs, mode = canvasser._estimate_travel_seconds_haversine(
            (35.0, 135.0), (35.003, 135.0)
        )
        assert mode == "walk"
        assert secs == pytest.approx(324.2, rel=1e-3)

    def test_中距離は車または在来線(self) -> None:
        """直線 1112m (道路換算 1501m) は 40km/h + 5 分で約 435 秒。"""
        secs, mode = canvasser._estimate_travel_seconds_haversine(
            (35.0, 135.0), (35.01, 135.0)
        )
        assert mode == "car/local"
        assert secs == pytest.approx(435.1, rel=1e-3)

    def test_遠距離は新幹線(self) -> None:
        """直線 55.6km (道路換算 75.1km) は 200km/h + 30 分で約 3151 秒。"""
        secs, mode = canvasser._estimate_travel_seconds_haversine(
            (35.0, 135.0), (35.5, 135.0)
        )
        assert mode == "shinkansen"
        assert secs == pytest.approx(3151.0, rel=1e-3)

    def test_超遠距離は飛行機(self) -> None:
        """直線 556km (道路換算 751km) は 500km/h + 90 分で約 10804 秒。"""
        secs, mode = canvasser._estimate_travel_seconds_haversine(
            (35.0, 135.0), (40.0, 135.0)
        )
        assert mode == "flight"
        assert secs == pytest.approx(10804.1, rel=1e-3)


class TestEstimateTravelSeconds:
    """estimate_travel_seconds のフォールバック合流。"""

    def test_GMAPSキー未設定ならHaversineへフォールバックする(self) -> None:
        """キー無しでは gmaps を使わず Haversine の結果をそのまま返す。"""
        got = canvasser.estimate_travel_seconds((35.0, 135.0), (35.0, 135.0))
        assert got == (0.0, "walk")

    def test_キー未設定ではクライアントはNone(self) -> None:
        """GMAPS_KEY が無い環境では _get_gmaps_client は None を返す。"""
        assert canvasser._get_gmaps_client() is None


def _leg(seconds: float, traffic_seconds: float | None = None) -> list[dict[str, Any]]:
    """duration.value と (任意で) duration_in_traffic.value を持つ応答を組み立てる。"""
    leg: dict[str, Any] = {"duration": {"value": seconds}}
    if traffic_seconds is not None:
        leg["duration_in_traffic"] = {"value": traffic_seconds}
    return [{"legs": [leg]}]


class TestEstimateTravelSecondsGmaps:
    """_estimate_travel_seconds_gmaps の transit 取得・キャッシュ・fallback。

    GMAPS_KEY を設定して googlemaps.Client のコンストラクタだけを差し替える。
    _get_gmaps_client 以降のロジック (バケット丸め・キャッシュ・fallback) は
    実物を通す。
    """

    def _install(
        self, monkeypatch: pytest.MonkeyPatch, client: FakeGmapsClient
    ) -> None:
        """GMAPS_KEY を設定し、Client 生成をフェイクへ差し替える。"""
        monkeypatch.setenv("GMAPS_KEY", "test-key")

        def fake_ctor(
            key: str | None = None, timeout: int | None = None
        ) -> FakeGmapsClient:
            return client

        monkeypatch.setattr(canvasser.googlemaps, "Client", fake_ctor)

    def _future_at(self, hour: int, minute: int) -> datetime:
        """翌日の指定時刻 (確実に未来の JST aware) を返す。"""
        return (datetime.now(JST) + timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    def test_transit経路の所要時間を返す(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """transit 応答の legs[0].duration.value が秒数として返る。"""
        client = FakeGmapsClient([_leg(1234.0)])
        self._install(monkeypatch, client)

        got = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 0)
        )

        assert got == (1234.0, "gmaps-transit")
        assert client.calls[0]["mode"] == "transit"

    def test_同一30分バケットはキャッシュを返しAPIを再呼び出ししない(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """12:31 発と 12:47 発は同じ 12:30 バケットに丸められキャッシュヒットする。"""
        client = FakeGmapsClient([_leg(1000.0)])
        self._install(monkeypatch, client)

        got1 = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 31)
        )
        got2 = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 47)
        )

        assert got1 == got2 == (1000.0, "gmaps-transit")
        assert len(client.calls) == 1

    def test_バケットが異なればAPIを再度呼ぶ(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """12:29 発と 12:31 発はバケットが違うためそれぞれ API を呼ぶ。"""
        client = FakeGmapsClient([_leg(1000.0), _leg(2000.0)])
        self._install(monkeypatch, client)

        got1 = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 29)
        )
        got2 = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 31)
        )

        assert got1 == (1000.0, "gmaps-transit")
        assert got2 == (2000.0, "gmaps-transit")
        assert len(client.calls) == 2

    def test_transitが空ならdrivingで再試行する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """transit 経路なし (深夜帯等) は driving の所要時間へフォールバックする。"""
        client = FakeGmapsClient([[], _leg(500.0)])
        self._install(monkeypatch, client)

        got = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 0)
        )

        assert got == (500.0, "gmaps-driving")
        assert client.calls[0]["mode"] == "transit"
        assert client.calls[1]["mode"] == "driving"

    def test_driving_fallbackはduration_in_trafficを優先する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """driving は traffic 反映値の duration_in_traffic があればそちらを返す。

        Directions API は mode=driving + 未来 departure_time + 経由地なしの条件下で
        duration_in_traffic を返す仕様
        (https://developers.google.com/maps/documentation/directions/get-directions)。
        この呼び出しは全条件を満たしているので、実運行に近い所要時間として優先する。
        """
        client = FakeGmapsClient([[], _leg(500.0, traffic_seconds=800.0)])
        self._install(monkeypatch, client)

        got = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 0)
        )

        assert got == (800.0, "gmaps-driving")

    def test_driving_fallbackはduration_in_traffic欠落時にdurationへフォールバックする(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """duration_in_traffic 欠落時は既存の duration にフォールバックする。"""
        client = FakeGmapsClient([[], _leg(500.0)])  # duration のみ
        self._install(monkeypatch, client)

        got = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 0)
        )

        assert got == (500.0, "gmaps-driving")

    def test_drivingも空ならNone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """transit・driving とも経路なしは None (Haversine 合流) を返す。"""
        client = FakeGmapsClient([[], []])
        self._install(monkeypatch, client)

        got = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 0)
        )

        assert got is None

    def test_transitの例外はNoneに丸めて警告する(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """API 例外は伝播させず None を返し、警告ログを残す。"""
        client = FakeGmapsClient([RuntimeError("api down")])
        self._install(monkeypatch, client)

        got = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 0)
        )

        assert got is None
        assert "gmaps directions 失敗" in caplog.text

    def test_driving再試行の例外もNoneに丸めて警告する(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """driving 側の例外も伝播させず None を返し、警告ログを残す。"""
        client = FakeGmapsClient([[], RuntimeError("api down")])
        self._install(monkeypatch, client)

        got = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 0)
        )

        assert got is None
        assert "driving 再試行失敗" in caplog.text

    def test_過去のdeparture_timeはnowに丸めて渡す(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """過去時刻を渡すと 400 になるため、API へは文字列 "now" を渡す。"""
        client = FakeGmapsClient([_leg(100.0)])
        self._install(monkeypatch, client)

        canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0),
            (36.0, 136.0),
            departure_time=datetime(2020, 1, 1, 12, 0, tzinfo=JST),
        )

        assert client.calls[0]["departure_time"] == "now"

    def test_未来のdeparture_timeはそのまま渡す(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未来時刻は始発待ち込み duration を得るためそのまま API へ渡す。"""
        client = FakeGmapsClient([_leg(100.0)])
        self._install(monkeypatch, client)
        dep = self._future_at(12, 0)

        canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=dep
        )

        assert client.calls[0]["departure_time"] == dep

    def test_naiveなdeparture_timeはJSTとして扱う(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tz なしの時刻は JST とみなして aware 化してから過去判定する。"""
        client = FakeGmapsClient([_leg(100.0)])
        self._install(monkeypatch, client)
        naive_dep = (datetime.now() + timedelta(days=1)).replace(  # noqa: DTZ005
            hour=12, minute=0, second=0, microsecond=0
        )

        got = canvasser._estimate_travel_seconds_gmaps(
            (35.0, 135.0), (36.0, 136.0), departure_time=naive_dep
        )

        assert got == (100.0, "gmaps-transit")
        passed = client.calls[0]["departure_time"]
        assert isinstance(passed, datetime)
        assert passed.tzinfo is not None

    def test_estimate_travel_secondsはgmaps結果を優先する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """クライアントが結果を返す場合は Haversine ではなく gmaps 値を使う。"""
        client = FakeGmapsClient([_leg(1234.0)])
        self._install(monkeypatch, client)

        got = canvasser.estimate_travel_seconds(
            (35.0, 135.0), (36.0, 136.0), departure_time=self._future_at(12, 0)
        )

        assert got == (1234.0, "gmaps-transit")


class TestNextArrivalTime:
    """next_arrival_time の稼働時間帯 (06:00-24:00) 補正。"""

    def test_移動0秒は現在時刻のまま(self) -> None:
        """travel_seconds=0 は補正なしで now を返す。"""
        now = datetime(2026, 7, 3, 10, 0, tzinfo=JST)
        assert canvasser.next_arrival_time(now, 0) == now

    def test_日中の移動はそのまま加算(self) -> None:
        """10:00 発 2 時間移動は同日 12:00 着になる。"""
        now = datetime(2026, 7, 3, 10, 0, tzinfo=JST)
        got = canvasser.next_arrival_time(now, 7200)
        assert got == datetime(2026, 7, 3, 12, 0, tzinfo=JST)

    def test_深夜発は朝6時発に繰り下げる(self) -> None:
        """03:00 発は 06:00 発扱いになり 1 時間移動で 07:00 着になる。"""
        now = datetime(2026, 7, 3, 3, 0, tzinfo=JST)
        got = canvasser.next_arrival_time(now, 3600)
        assert got == datetime(2026, 7, 3, 7, 0, tzinfo=JST)

    def test_日中に収まらない旅は翌朝発へ押し戻す(self) -> None:
        """23:00 発 2 時間移動は当日中に着けず翌朝 06:00 発 08:00 着になる。"""
        now = datetime(2026, 7, 3, 23, 0, tzinfo=JST)
        got = canvasser.next_arrival_time(now, 7200)
        assert got == datetime(2026, 7, 4, 8, 0, tzinfo=JST)

    def test_深夜0時ちょうど着は当日扱い(self) -> None:
        """22:00 発 2 時間移動はちょうど 24:00 着として許容される。"""
        now = datetime(2026, 7, 3, 22, 0, tzinfo=JST)
        got = canvasser.next_arrival_time(now, 7200)
        assert got == datetime(2026, 7, 4, 0, 0, tzinfo=JST)

    def test_稼働枠18時間ちょうどの移動は稼働枠に収まる(self) -> None:
        """06:00 発 18 時間移動はちょうど 24:00 着として当日中に収まる。"""
        now = datetime(2026, 7, 3, 6, 0, tzinfo=JST)
        got = canvasser.next_arrival_time(now, 18 * 3600)
        assert got == datetime(2026, 7, 4, 0, 0, tzinfo=JST)

    def test_稼働枠18時間を超える長旅は夜間も連続移動して加算する(self) -> None:
        """10:00 発 25 時間移動は押し戻しなしの連続移動で翌日 11:00 着になる。"""
        now = datetime(2026, 7, 3, 10, 0, tzinfo=JST)
        got = canvasser.next_arrival_time(now, 25 * 3600)
        assert got == datetime(2026, 7, 4, 11, 0, tzinfo=JST)

    def test_深夜発の長旅は朝6時発扱いで連続移動する(self) -> None:
        """03:00 発 20 時間移動は 06:00 発扱いになり翌日 02:00 着になる。"""
        now = datetime(2026, 7, 3, 3, 0, tzinfo=JST)
        got = canvasser.next_arrival_time(now, 20 * 3600)
        assert got == datetime(2026, 7, 4, 2, 0, tzinfo=JST)


class TestParseCheckinDeadline:
    """parse_checkin_deadline の形式別パース。"""

    def _spot(self, raw: object) -> dict[str, Any]:
        """checkin_end_datetime だけ持つスポット dict を組み立てる。"""
        return {"checkin_end_datetime": raw}

    def test_スペース区切り形式はJSTとして読む(self) -> None:
        """現行サーバ形式 "YYYY-MM-DD HH:MM:SS" は JST の aware になる。"""
        got = canvasser.parse_checkin_deadline(self._spot("2026-07-31 23:59:59"))
        assert got == datetime(2026, 7, 31, 23, 59, 59, tzinfo=JST)

    def test_T区切り形式もJSTとして読む(self) -> None:
        """ISO 風の T 区切りも同じ JST 解釈でパースされる。"""
        got = canvasser.parse_checkin_deadline(self._spot("2026-07-31T23:59:59"))
        assert got == datetime(2026, 7, 31, 23, 59, 59, tzinfo=JST)

    def test_Z付きISOはUTCからJSTへ変換する(self) -> None:
        """UTC 14:59:59Z は JST 23:59:59 に変換される。"""
        got = canvasser.parse_checkin_deadline(self._spot("2026-07-31T14:59:59Z"))
        assert got == datetime(2026, 7, 31, 23, 59, 59, tzinfo=JST)

    def test_秒なしISOはfromisoformatで救済する(self) -> None:
        """秒を省いた YYYY-MM-DDTHH:MM は strptime 失敗後に isoformat で読める。"""
        got = canvasser.parse_checkin_deadline(self._spot("2026-07-31T23:59"))
        assert got == datetime(2026, 7, 31, 23, 59, tzinfo=JST)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            20260731,
            "31/07/2026",
            "invalid",
        ],
    )
    def test_パース不能はNone(self, raw: object) -> None:
        """欠落・非文字列・未知形式はすべて None を返す。"""
        assert canvasser.parse_checkin_deadline(self._spot(raw)) is None

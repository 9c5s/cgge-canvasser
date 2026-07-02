"""移動時間推定・時刻計算・期限パースのテスト。

GMAPS_KEY は conftest の autouse fixture で除去されるため、estimate_travel_seconds
は常に Haversine フォールバック経路を通る (外部通信なしの Small テスト)。
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

import pytest

import canvasser
from canvasser import JST


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
        got = canvasser._estimate_travel_seconds_haversine(35.0, 135.0, 35.0, 135.0)
        assert got == (0.0, "walk")

    def test_近距離は徒歩(self) -> None:
        """直線 334m (道路換算 450m) は徒歩 5km/h で約 324 秒。"""
        secs, mode = canvasser._estimate_travel_seconds_haversine(
            35.0, 135.0, 35.003, 135.0
        )
        assert mode == "walk"
        assert secs == pytest.approx(324.2, rel=1e-3)

    def test_中距離は車または在来線(self) -> None:
        """直線 1112m (道路換算 1501m) は 40km/h + 5 分で約 435 秒。"""
        secs, mode = canvasser._estimate_travel_seconds_haversine(
            35.0, 135.0, 35.01, 135.0
        )
        assert mode == "car/local"
        assert secs == pytest.approx(435.1, rel=1e-3)

    def test_遠距離は新幹線(self) -> None:
        """直線 55.6km (道路換算 75.1km) は 200km/h + 30 分で約 3151 秒。"""
        secs, mode = canvasser._estimate_travel_seconds_haversine(
            35.0, 135.0, 35.5, 135.0
        )
        assert mode == "shinkansen"
        assert secs == pytest.approx(3151.0, rel=1e-3)

    def test_超遠距離は飛行機(self) -> None:
        """直線 556km (道路換算 751km) は 500km/h + 90 分で約 10804 秒。"""
        secs, mode = canvasser._estimate_travel_seconds_haversine(
            35.0, 135.0, 40.0, 135.0
        )
        assert mode == "flight"
        assert secs == pytest.approx(10804.1, rel=1e-3)


class TestEstimateTravelSeconds:
    """estimate_travel_seconds のフォールバック合流。"""

    def test_GMAPSキー未設定ならHaversineに落ちる(self) -> None:
        """キー無しでは gmaps を使わず Haversine の結果をそのまま返す。"""
        got = canvasser.estimate_travel_seconds(35.0, 135.0, 35.0, 135.0)
        assert got == (0.0, "walk")

    def test_キー未設定ではクライアントはNone(self) -> None:
        """GMAPS_KEY が無い環境では _get_gmaps_client は None を返す。"""
        assert canvasser._get_gmaps_client() is None


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

"""座標・暗号化・スポット順序決定のテスト。

乱数依存の関数は random.seed で固定し、性質 (範囲・分布の型) をリテラル境界値で
検証する。encrypt_coords は逆演算 (復号) によるラウンドトリップで確認する。
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
from typing import Any, ClassVar

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

import canvasser


def _decrypt_coords(payload: str, password: str) -> dict[str, Any]:
    """encrypt_coords のペイロードを crypto-js プロトコル仕様に従って復号する。

    暗号化側の再実装ではなく、仕様書 (salt,iv,ct 形式 + PBKDF2-SHA1 500 回) に
    基づく逆演算であるため、自作自演にはあたらない。
    """
    salt_hex, iv_hex, ct_b64 = payload.split(",")
    key = hashlib.pbkdf2_hmac(
        "sha1", password.encode(), bytes.fromhex(salt_hex), 500, dklen=32
    )
    cipher = AES.new(  # pyright: ignore[reportUnknownMemberType]
        key, AES.MODE_CBC, bytes.fromhex(iv_hex)
    )
    plaintext = unpad(cipher.decrypt(base64.b64decode(ct_b64)), 16)
    data: dict[str, Any] = json.loads(plaintext)
    return data


class TestDistanceM:
    """_distance_m の Haversine 計算。"""

    def test_同一点は距離0(self) -> None:
        """同一座標の距離は 0m である。"""
        assert canvasser._distance_m(35.0, 135.0, 35.0, 135.0) == 0.0

    def test_緯度1度は約111195m(self) -> None:
        """緯度 1 度 = 2πR/360 ≒ 111194.93m (R=6371km) に一致する。"""
        got = canvasser._distance_m(0.0, 0.0, 1.0, 0.0)
        assert got == pytest.approx(111194.93, abs=0.5)

    def test_赤道上の経度1度は緯度1度と等距離(self) -> None:
        """赤道上では経度 1 度も約 111194.93m になる。"""
        got = canvasser._distance_m(0.0, 0.0, 0.0, 1.0)
        assert got == pytest.approx(111194.93, abs=0.5)

    def test_引数の順序を入れ替えても対称(self) -> None:
        """距離は始点終点を入れ替えても同じ値になる。"""
        d1 = canvasser._distance_m(35.68, 139.77, 34.70, 135.50)
        d2 = canvasser._distance_m(34.70, 135.50, 35.68, 139.77)
        assert d1 == pytest.approx(d2)


class TestRandomPointInCircle:
    """random_point_in_circle の生成点の性質。"""

    @pytest.mark.parametrize("seed", [(0,), (1,), (42,), (1234,)])
    def test_生成点は半径内に収まる(self, seed: tuple[int]) -> None:
        """どの乱数系列でも生成点は指定半径 (+近似誤差 1%) に収まる。"""
        random.seed(seed[0])
        for _ in range(100):
            lat, lng = canvasser.random_point_in_circle(35.0, 135.0, 500.0)
            d = canvasser._distance_m(35.0, 135.0, lat, lng)
            assert d <= 500.0 * 1.01

    def test_高緯度でも半径内に収まる(self) -> None:
        """経度スケール補正 cos(lat) が効く高緯度でも半径を超えない。"""
        random.seed(7)
        for _ in range(100):
            lat, lng = canvasser.random_point_in_circle(60.0, 25.0, 300.0)
            d = canvasser._distance_m(60.0, 25.0, lat, lng)
            assert d <= 300.0 * 1.01

    def test_半径0は中心そのもの(self) -> None:
        """半径 0 なら中心座標がそのまま返る。"""
        random.seed(0)
        assert canvasser.random_point_in_circle(35.0, 135.0, 0.0) == (35.0, 135.0)


class TestNaturalAccuracy:
    """_natural_accuracy の分布範囲。"""

    def test_値域は5から80mに収まる(self) -> None:
        """クランプ下限 5m と外れ値上限 80m の範囲を出ない。"""
        random.seed(0)
        values = [canvasser._natural_accuracy() for _ in range(300)]
        assert min(values) >= 5.0
        assert max(values) <= 80.0

    def test_小数3桁に丸められる(self) -> None:
        """返り値は round(x, 3) 済みである。"""
        random.seed(1)
        values = [canvasser._natural_accuracy() for _ in range(50)]
        assert all(v == round(v, 3) for v in values)


class TestNaturalAltitude:
    """_natural_altitude の分布と None の組み合わせ。"""

    def test_Noneか値域内ペアのどちらかを返す(self) -> None:
        """(None, None) または (5-80m, 20-50m) のペアのみ返る。"""
        random.seed(3)
        none_count = 0
        value_count = 0
        for _ in range(200):
            alt, acc = canvasser._natural_altitude()
            if alt is None:
                assert acc is None
                none_count += 1
            else:
                assert acc is not None
                assert 5.0 <= alt <= 80.0
                assert 20.0 <= acc <= 50.0
                value_count += 1
        assert none_count > 0
        assert value_count > 0


class TestMakeCheckinCoords:
    """make_checkin_coords の座標生成。"""

    def _spot(self, radius: object = 500) -> dict[str, Any]:
        """テスト用スポット dict を組み立てる。"""
        return {
            "location_latitude": 35.0,
            "location_longitude": 135.0,
            "checkin_radius": radius,
        }

    def test_geolocation互換のキーを持つ(self) -> None:
        """Geolocation API 互換の 7 キーで構成される。"""
        random.seed(0)
        coords = canvasser.make_checkin_coords(self._spot())
        assert set(coords) == {
            "accuracy",
            "latitude",
            "longitude",
            "altitude",
            "altitudeAccuracy",
            "heading",
            "speed",
        }

    def test_headingとspeedは常にNone(self) -> None:
        """静止端末想定のため heading と speed は null 固定である。"""
        random.seed(0)
        coords = canvasser.make_checkin_coords(self._spot())
        assert coords["heading"] is None
        assert coords["speed"] is None

    def test_生成点は内寄せ半径に収まる(self) -> None:
        """radius * 0.85 (CHECKIN_RADIUS_MARGIN) の内側に生成される。"""
        random.seed(0)
        for _ in range(50):
            coords = canvasser.make_checkin_coords(self._spot(500))
            d = canvasser._distance_m(
                35.0, 135.0, coords["latitude"], coords["longitude"]
            )
            assert d <= 500 * canvasser.CHECKIN_RADIUS_MARGIN * 1.01

    def test_radius欠落時は500mを既定にする(self) -> None:
        """checkin_radius が無いスポットでは 500m * 0.85 に収まる。"""
        random.seed(0)
        spot = self._spot(None)
        for _ in range(50):
            coords = canvasser.make_checkin_coords(spot)
            d = canvasser._distance_m(
                35.0, 135.0, coords["latitude"], coords["longitude"]
            )
            assert d <= 500 * canvasser.CHECKIN_RADIUS_MARGIN * 1.01

    def test_文字列radiusも数値として扱う(self) -> None:
        """API が radius を文字列で返しても float に変換して使う。"""
        random.seed(0)
        coords = canvasser.make_checkin_coords(self._spot("100"))
        d = canvasser._distance_m(35.0, 135.0, coords["latitude"], coords["longitude"])
        assert d <= 100 * canvasser.CHECKIN_RADIUS_MARGIN * 1.01


class TestEncryptCoords:
    """encrypt_coords のペイロード形式とラウンドトリップ。"""

    _COORDS: ClassVar[dict[str, Any]] = {
        "accuracy": 12.345,
        "latitude": 35.681236,
        "longitude": 139.767125,
        "altitude": None,
        "altitudeAccuracy": None,
        "heading": None,
        "speed": None,
    }

    def test_ペイロードはsaltとivとciphertextの3部構成(self) -> None:
        """salt_hex,iv_hex,ct_base64 の 3 部形式で salt と iv は 16 バイトである。"""
        payload = canvasser.encrypt_coords(self._COORDS)
        salt_hex, iv_hex, ct_b64 = payload.split(",")
        assert len(bytes.fromhex(salt_hex)) == 16
        assert len(bytes.fromhex(iv_hex)) == 16
        assert len(base64.b64decode(ct_b64)) % 16 == 0

    def test_復号すると元のcoordsに戻る(self) -> None:
        """既定パスワード (API_KEY) で暗号化 → 復号のラウンドトリップが成立する。"""
        payload = canvasser.encrypt_coords(self._COORDS)
        assert _decrypt_coords(payload, canvasser.API_KEY) == self._COORDS

    def test_パスワード指定でも復号できる(self) -> None:
        """password 引数を変えると鍵導出もその値に従う。"""
        payload = canvasser.encrypt_coords(self._COORDS, password="secret")
        assert _decrypt_coords(payload, "secret") == self._COORDS

    def test_呼び出しごとにペイロードが変わる(self) -> None:
        """salt と iv が毎回ランダムなため同一入力でも暗号文が異なる。"""
        p1 = canvasser.encrypt_coords(self._COORDS)
        p2 = canvasser.encrypt_coords(self._COORDS)
        assert p1 != p2

    def test_固定乱数での出力を回帰固定する(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """salt と iv を固定したときのペイロード全体をリテラルで固定する。

        CryptoJS 互換性そのものの独立検証ではない (互換性は実 UI 経由の POST
        観測で確定済み)。PBKDF2 パラメータや連結形式が意図せず変わった場合に
        検出するための回帰テストである。
        """

        def fixed_urandom(n: int) -> bytes:
            """連番バイト列を返す決定的な os.urandom 代替。"""
            return bytes(range(n))

        monkeypatch.setattr(canvasser.os, "urandom", fixed_urandom)

        payload = canvasser.encrypt_coords(self._COORDS)

        fixed_hex = "000102030405060708090a0b0c0d0e0f"
        expected_ct = (
            "S+fKzRddxmmh0zybpJzsGEZf64HcJIJ80fA56bGIj1EUuveeoZePLkStEzCDrxJn"
            "ugQdyyCqAAYYNl8LYq5lTF+5/MWOBFAvUWIRYwVy3cMfIqR0DsfvIT6wPTW4rB5d"
            "TVrjWLUG68uE4c1bmxBd8wvW25NSvqj3gwXQvtPn+RjPrMCNQKShFSmndI7zLxW/"
        )
        assert payload == f"{fixed_hex},{fixed_hex},{expected_ct}"


def _spot_at(slug: str, lat: float, lng: float) -> dict[str, Any]:
    """順序決定テスト用の最小スポット dict を組み立てる。"""
    return {"slug": slug, "location_latitude": lat, "location_longitude": lng}


class TestOrderSpotsByProximity:
    """order_spots_by_proximity の最近傍順序。"""

    def test_空リストは空を返す(self) -> None:
        """スポットが無ければ空リストを返す。"""
        assert canvasser.order_spots_by_proximity([]) == []

    def test_start_locationに最も近いスポットから始まる(self) -> None:
        """開始位置 (35.09, 135.0) に最近の b から a → c と辿る。"""
        spots = [
            _spot_at("a", 35.00, 135.0),
            _spot_at("b", 35.10, 135.0),
            _spot_at("c", 35.30, 135.0),
        ]

        ordered = canvasser.order_spots_by_proximity(
            spots, start_location=(35.09, 135.0)
        )

        assert [s["slug"] for s in ordered] == ["b", "a", "c"]

    def test_start_index指定で開始スポットが固定される(self) -> None:
        """start_index=2 (c) からは近い順に b → a と辿る。"""
        spots = [
            _spot_at("a", 35.00, 135.0),
            _spot_at("b", 35.10, 135.0),
            _spot_at("c", 35.30, 135.0),
        ]

        ordered = canvasser.order_spots_by_proximity(spots, start_index=2)

        assert [s["slug"] for s in ordered] == ["c", "b", "a"]

    def test_全スポットが欠落なく並ぶ(self) -> None:
        """開始乱択でも結果は入力の並べ替え (欠落・重複なし) である。"""
        random.seed(5)
        spots = [
            _spot_at("a", 35.00, 135.0),
            _spot_at("b", 35.10, 135.1),
            _spot_at("c", 35.30, 135.2),
            _spot_at("d", 34.90, 134.9),
        ]

        ordered = canvasser.order_spots_by_proximity(spots)

        assert sorted(s["slug"] for s in ordered) == ["a", "b", "c", "d"]

    def test_1件だけならそのまま返す(self) -> None:
        """単一スポットは順序決定の余地なくそのまま返る。"""
        spots = [_spot_at("a", 35.0, 135.0)]
        assert canvasser.order_spots_by_proximity(spots, start_index=0) == spots

"""API 応答ヘルパーと fetch ラッパーのテスト。

ネットワーク境界は FakePage で差し替え、URL・メソッド・ヘッダ引数の組み立てと
応答の構造化ロジックを検証する。
"""

from typing import Any

import pytest

import canvasser
from tests._fakes import (
    FakePage,
    as_page as _as_page,
)


class TestAsStrDict:
    """_as_str_dict の絞り込み挙動。"""

    def test_dictはそのまま返す(self) -> None:
        """dict は同一オブジェクトとして返る。"""
        src = {"a": 1}
        assert canvasser._as_str_dict(src) is src

    @pytest.mark.parametrize(
        "value",
        [
            "[1, 2]",
            None,
            123,
            "text",
        ],
    )
    def test_dict以外はNoneを返す(self, value: object) -> None:
        """list・None・数値・文字列は None に丸められる。"""
        assert canvasser._as_str_dict(value) is None


class TestExtractEcode:
    """_extract_ecode の取り出しロジック。"""

    def test_payload内のecodeを返す(self) -> None:
        """body.payload.ecode が存在すればその値を返す。"""
        body = {"status": "ERROR", "payload": {"ecode": "E1906"}}
        assert canvasser._extract_ecode(body) == "E1906"

    @pytest.mark.parametrize(
        "body",
        [
            None,
            "non-json-text",
            {},
            {"payload": {}},
            {"payload": None},
            {"payload": "text"},
            {"payload": [1, 2]},
        ],
    )
    def test_ecodeが無ければNone(self, body: object) -> None:
        """body・payload が dict でない、または ecode 欠落はすべて None。"""
        assert canvasser._extract_ecode(body) is None


class TestIsSuccessResponse:
    """_is_success_response の判定基準。"""

    def test_200かつSUCCESSでTrue(self) -> None:
        """HTTP 200 かつ body.status=SUCCESS のときだけ True。"""
        res: dict[str, Any] = {"status": 200, "body": {"status": "SUCCESS"}}
        assert canvasser._is_success_response(res) is True

    @pytest.mark.parametrize(
        "status, body",
        [
            (500, {"status": "SUCCESS"}),
            (200, {"status": "ERROR"}),
            (200, "non-json"),
            (200, None),
            (0, None),
        ],
    )
    def test_条件を満たさなければFalse(self, status: int, body: object) -> None:
        """HTTP エラー・status 不一致・非 dict body は False。"""
        res: dict[str, Any] = {"status": status, "body": body}
        assert canvasser._is_success_response(res) is False


class TestCallApi:
    """call_api の URL 組み立てと応答パススルー。"""

    def test_URLとメソッドとAPIキーを渡す(self) -> None:
        """API_HOST + API_BASE + path の URL で evaluate に引数を渡す。"""
        fake = FakePage([{"status": 200, "body": {"status": "SUCCESS"}}])

        canvasser.call_api(_as_page(fake), "GET", "/missions")

        expected_url = f"{canvasser.API_HOST}{canvasser.API_BASE}/missions"
        assert len(fake.calls) == 1
        assert fake.calls[0][1] == [expected_url, "GET", canvasser.API_KEY, None]

    def test_応答をそのまま返す(self) -> None:
        """evaluate の戻り値を加工せず返す。"""
        response = {"status": 404, "body": "not found", "error": "non-json"}
        fake = FakePage([response])

        result = canvasser.call_api(_as_page(fake), "POST", "/mission/1")

        assert result == {"status": 404, "body": "not found", "error": "non-json"}


class TestCallCheckinApi:
    """call_checkin_api の URL 組み立てと body 伝搬。"""

    def test_bodyなしはNoneを渡す(self) -> None:
        """GET 系では body=None が evaluate 引数に渡る。"""
        fake = FakePage([{"status": 200, "body": {"status": "SUCCESS"}}])

        canvasser.call_checkin_api(_as_page(fake), "GET", "/event/cg_vote2026")

        expected_url = f"{canvasser.API_V1}/checkins/event/cg_vote2026"
        assert fake.calls[0][1] == [expected_url, "GET", canvasser.API_KEY, None]

    def test_bodyありは文字列のまま渡す(self) -> None:
        """暗号化ペイロード文字列を URL エンコードせずそのまま渡す。"""
        fake = FakePage([{"status": 200, "body": {"status": "SUCCESS"}}])
        body = "aa,bb,cc=="

        canvasser.call_checkin_api(_as_page(fake), "POST", "/spot/x/checkin", body)

        expected_url = f"{canvasser.API_V1}/checkins/spot/x/checkin"
        assert fake.calls[0][1] == [expected_url, "POST", canvasser.API_KEY, body]


class TestCheckLogin:
    """check_login の is_login 判定。"""

    def test_is_loginがTrueならTrue(self) -> None:
        """payload.is_login=True でログイン済みと判定する。"""
        fake = FakePage([{"status": "SUCCESS", "payload": {"is_login": True}}])
        assert canvasser.check_login(_as_page(fake)) is True

    @pytest.mark.parametrize(
        "response",
        [
            {"status": "SUCCESS", "payload": {"is_login": False}},
            {"status": "SUCCESS", "payload": {}},
            {"status": "ERROR", "payload": {}},
            {"status": "ERROR", "payload": None},
        ],
    )
    def test_未ログインや異常応答はFalse(self, response: object) -> None:
        """is_login 欠落・False・エラー応答はすべて未ログイン扱い。"""
        fake = FakePage([response])
        assert canvasser.check_login(_as_page(fake)) is False

    @pytest.mark.parametrize(
        "response",
        [
            None,
            [1, 2],
            "text",
        ],
    )
    def test_dict以外の応答もFalseに丸める(self, response: object) -> None:
        """サーバが配列等の想定外形式を返しても例外にせず未ログイン扱いにする。"""
        fake = FakePage([response])
        assert canvasser.check_login(_as_page(fake)) is False

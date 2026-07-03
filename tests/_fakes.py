"""テスト用の最小フェイク実装。

ネットワーク境界 (page.evaluate 経由の fetch と googlemaps.Client) だけを
差し替える。それ以外の内部実装はモックせず、実物のロジックを通す方針である。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from datetime import datetime


class FakePage:
    """`page.evaluate` をキュー応答で置き換えるテストダブル。

    呼び出しを `calls` に記録するため、送信された URL やメソッドの検証にも
    使える。キューが尽きた状態で呼ばれたら想定外のリクエストとして失敗させる。
    """

    def __init__(self, responses: list[object]) -> None:
        """先頭から順に返す応答のキューを受け取る。"""
        self._responses = list(responses)
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, expression: str, arg: object = None) -> object:
        """呼び出しを記録し、キュー先頭の応答を返す。"""
        self.calls.append((expression, arg))
        if not self._responses:
            msg = "FakePage: 想定外の evaluate 呼び出しが発生した"
            raise AssertionError(msg)
        return self._responses.pop(0)


class FakeGmapsClient:
    """`googlemaps.Client` の directions だけを再現するテストダブル。

    応答キューには list (成功応答) または Exception (directions が送出する
    例外) を積む。呼び出し内容は `calls` に記録され、mode や departure_time
    の検証に使える。
    """

    def __init__(self, responses: list[object]) -> None:
        """先頭から順に返す応答または送出する例外のキューを受け取る。"""
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def directions(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: str | None = None,
        departure_time: datetime | str | None = None,
        language: str | None = None,
        alternatives: bool = False,
    ) -> list[dict[str, Any]]:
        """呼び出しを記録し、キュー先頭の応答を返すか例外を送出する。"""
        self.calls.append({
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "departure_time": departure_time,
            "language": language,
            "alternatives": alternatives,
        })
        if not self._responses:
            msg = "FakeGmapsClient: 想定外の directions 呼び出しが発生した"
            raise AssertionError(msg)
        res = self._responses.pop(0)
        if isinstance(res, Exception):
            raise res
        return cast("list[dict[str, Any]]", res)

"""テスト用の最小フェイク実装。

ネットワーク境界 (page.evaluate 経由の fetch) だけを差し替える。それ以外の
内部実装はモックせず、実物のロジックを通す方針である。
"""

from __future__ import annotations


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

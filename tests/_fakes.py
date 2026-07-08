"""テスト用の最小フェイク実装。

ネットワーク境界 (page.evaluate 経由の fetch と googlemaps.Client) だけを
差し替える。それ以外の内部実装はモックせず、実物のロジックを通す方針である。
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from datetime import datetime

    from playwright.sync_api import Page


def as_page(fake: FakePage) -> Page:
    """FakePage を Page として渡すための cast ヘルパー。"""
    return cast("Page", fake)


class FakeLocator:
    """`page.locator(sel)` の戻り値を模したテストダブル。

    press_sequentially / click / wait_for は呼び出しを FakePage.calls に記録する。
    is_visible / count は FakePage 側の可視性・件数マップを参照する。
    """

    def __init__(self, page: FakePage, selector: str) -> None:
        """親 FakePage と selector を保持する。"""
        self._page = page
        self._selector = selector

    def fill(self, text: str) -> None:
        """既存テキストを消して text で埋める。空文字は入力欄のクリア。"""
        self._page.calls.append(("fill", (self._selector, text)))

    def press_sequentially(self, text: str) -> None:
        """キー入力相当の入力。selector と text を記録する。"""
        self._page.calls.append(("press_sequentially", (self._selector, text)))

    def click(self, **kwargs: object) -> None:
        """クリック。selector と kwargs (no_wait_after 等) を記録する。

        click_errors[selector] に非 None が入っていればそれを raise (submit 中の
        PlaywrightError を再現する用途)。
        """
        self._page.calls.append(("click", (self._selector, kwargs)))
        errors = self._page.click_errors.get(self._selector)
        if errors:
            err = errors.pop(0)
            if err is not None:
                raise err

    def wait_for(self, **kwargs: object) -> None:
        """要素の状態変化を待機する。

        キュー `wait_for_errors[selector]` に非 None が入っていればそれを送出する
        (タイムアウト等の再現用)。
        """
        self._page.calls.append(("wait_for", (self._selector, kwargs)))
        errors = self._page.wait_for_errors.get(self._selector)
        if errors:
            err = errors.pop(0)
            if err is not None:
                raise err

    def is_visible(self) -> bool:
        """selector の可視性を FakePage.visibility から取得する (既定 False)。"""
        return self._page.visibility.get(self._selector, False)

    def count(self) -> int:
        """selector の要素数を返す。

        counts_sequence[selector] があれば呼び出し順に先頭を pop して返す (最後の
        値は保持し続ける)。動的な CAPTCHA 挿入 (pre-submit=0 → post-submit=1) を
        再現する用途。設定がなければ counts[selector] を返す (既定 0)。
        """
        seq = self._page.counts_sequence.get(self._selector)
        if seq:
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return self._page.counts.get(self._selector, 0)


def success_response(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """HTTP 200 + status=SUCCESS の API 応答を組み立てる。

    payload を省略すると body は status のみになる (POST 成功等の最小応答)。
    """
    body: dict[str, Any] = {"status": "SUCCESS"}
    if payload is not None:
        body["payload"] = payload
    return {"status": 200, "body": body}


def error_response(ecode: str) -> dict[str, Any]:
    """HTTP 400 + ecode 付きのエラー応答を組み立てる。"""
    return {
        "status": 400,
        "body": {"status": "ERROR", "payload": {"ecode": ecode}},
    }


class FakePage:
    """`page.evaluate` をキュー応答で置き換えるテストダブル。

    呼び出しを `calls` に記録するため、送信された URL やメソッドの検証にも
    使える。キューが尽きた状態で呼ばれたら想定外のリクエストとして失敗させる。

    `locator()` を使うテストのために、selector ごとの可視性・件数・wait_for
    挙動を注入できる。省略時は「見えない・件数 0・wait_for は即帰る」既定。
    """

    def __init__(
        self,
        responses: Sequence[object] | None = None,
        *,
        visibility: dict[str, bool] | None = None,
        counts: dict[str, int] | None = None,
        counts_sequence: dict[str, list[int]] | None = None,
        wait_for_errors: dict[str, list[Exception | None]] | None = None,
        click_errors: dict[str, list[Exception | None]] | None = None,
        goto_errors: Sequence[Exception | None] | None = None,
    ) -> None:
        """応答キュー・selector マップ・goto/click エラーキューを受け取る。"""
        self._responses: list[object] = list(responses or [])
        self.calls: list[tuple[str, object]] = []
        self.visibility: dict[str, bool] = dict(visibility or {})
        self.counts: dict[str, int] = dict(counts or {})
        # count() の値を呼び出し順に切り替える (動的挿入シナリオ用)。
        self.counts_sequence: dict[str, list[int]] = {
            k: list(v) for k, v in (counts_sequence or {}).items()
        }
        self.wait_for_errors: dict[str, list[Exception | None]] = {
            k: list(v) for k, v in (wait_for_errors or {}).items()
        }
        self.click_errors: dict[str, list[Exception | None]] = {
            k: list(v) for k, v in (click_errors or {}).items()
        }
        self.goto_errors: list[Exception | None] = list(goto_errors or [])
        # ASOBI 連携復旧ドライバ (linkages/as/login) のポーリングが参照する現在 URL。
        # 通常のテストではアクセスされないため空文字のままでよい。
        self.url: str = ""

    def evaluate(self, expression: str, arg: object = None) -> object:
        """呼び出しを記録し、キュー先頭の応答を返す。Exception なら raise する。"""
        self.calls.append((expression, arg))
        if not self._responses:
            msg = "FakePage: 想定外の evaluate 呼び出しが発生した"
            raise AssertionError(msg)
        resp = self._responses.pop(0)
        # 応答キューに Exception を積むと raise する (check_login の PlaywrightError
        # を再現する用途)。
        if isinstance(resp, Exception):
            raise resp
        return resp

    def locator(self, selector: str) -> FakeLocator:
        """FakeLocator を返す。呼び出しは calls に記録する。"""
        self.calls.append(("locator", selector))
        return FakeLocator(self, selector)

    def goto(self, url: str, **kwargs: object) -> None:
        """遷移を記録するだけの noop。goto_errors に例外が入っていれば送出する。"""
        self.calls.append(("goto", (url, kwargs)))
        errors = self.goto_errors
        if errors:
            err = errors.pop(0)
            if err is not None:
                raise err


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

"""BNID 自動再ログイン (`auto_login`) のテスト。

ネットワーク境界は FakePage で差し替え、時計とスリープは monkeypatch で制御して
決定的に回す。BNID 側の DOM 契約 (Phase 1 で調査済) の呼び出しシーケンスと
成功/失敗/タイムアウト/CAPTCHA 各パスを検証する。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError

import canvasser
from canvasser import Credentials
from tests._fakes import FakePage, as_page

if TYPE_CHECKING:
    import pytest


def _sample_creds() -> Credentials:
    """テスト用の Credentials インスタンス。"""
    return Credentials(
        bnid_email="user@example.com",
        bnid_password="hunter2",
        saved_at="2026-07-05T00:00:00+09:00",
    )


def _is_login_response(*, is_login: bool) -> dict[str, object]:
    """check_login の evaluate 応答の組み立てヘルパー。

    check_login は fetch の JSON.parse 結果 (サーバ生のペイロード) を直接
    受け取るため、`{payload: {is_login}}` の形にする (call_api の
    `{status, body}` 構造ではない)。
    """
    return {"payload": {"is_login": is_login}}


class _FakeClock:
    """time.monotonic を差し替えるための単調増加クロック。

    step ずつ進める。deadline との比較で確実に脱出できるよう、後半では上限を
    そのまま返し続ける。
    """

    def __init__(self, start: float = 0.0, step: float = 0.5) -> None:
        """開始時刻と 1 コール毎の進み幅を指定する。"""
        self.now = start
        self.step = step

    def __call__(self) -> float:
        """呼ばれるたびに step ぶん進めて現在時刻を返す。"""
        self.now += self.step
        return self.now


def _noop_sleep(_sec: float) -> None:
    """time.sleep 差し替え用の noop (テストで時間を経過させないため)。"""
    return


def _install_fake_time(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """canvasser.time.monotonic / sleep を差し替えて時計を渡す。"""
    clock = _FakeClock()
    monkeypatch.setattr(canvasser.time, "monotonic", clock)
    monkeypatch.setattr(canvasser.time, "sleep", _noop_sleep)
    return clock


class TestAutoLogin:
    """auto_login の成功・失敗・タイムアウト・CAPTCHA 各分岐。"""

    def test_成功パスで即Trueを返す(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """フォーム操作後の 1 回目の check_login が is_login=True なら True。"""
        _install_fake_time(monkeypatch)
        fake = FakePage(responses=[_is_login_response(is_login=True)])

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is True
        # フォーム入力シーケンスの検証 (press_sequentially → click)
        interactions = {"press_sequentially", "click"}
        locator_calls = [c for c in fake.calls if c[0] in interactions]
        assert ("press_sequentially", ("#mail", "user@example.com")) in locator_calls
        assert ("press_sequentially", ("#pass", "hunter2")) in locator_calls
        assert ("click", "#btn-idpw-login") in locator_calls
        assert "ログイン成功を検知" in capsys.readouterr().err

    def test_エラーDOM可視化でFalseを返す(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """パスワード誤り相当のエラーメッセージが表示されたら False。"""
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[_is_login_response(is_login=False)],
            visibility={"#error-input-area .c-message--warning": True},
        )

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is False
        assert "認証エラー" in capsys.readouterr().err

    def test_CAPTCHA挿入でFalseを返す(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CAPTCHA を示唆する iframe が挿入されたら False + 手動ログイン誘導。"""
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[_is_login_response(is_login=False)],
            counts={'iframe[src*="recaptcha"]': 1},
        )

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is False
        err = capsys.readouterr().err
        assert "CAPTCHA/2FA" in err
        assert "手動" in err

    def test_タイムアウトでFalseを返す(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """成功も失敗も検知できなければ deadline 経過で False。"""
        _install_fake_time(monkeypatch)
        # ポーリング分だけ「未ログイン」を返し続けても deadline で抜ける
        fake = FakePage(responses=[_is_login_response(is_login=False)] * 20)

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is False
        assert "タイムアウト" in capsys.readouterr().err

    def test_フォーム操作エラーで即False(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """メール入力で PlaywrightError なら poll に入らず False を返す。"""
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[],
            wait_for_errors={
                "#btn-idpw-login:not([disabled])": [
                    PlaywrightError("timeout: disabled が外れない")
                ]
            },
        )

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is False
        assert "フォーム操作でエラー" in capsys.readouterr().err

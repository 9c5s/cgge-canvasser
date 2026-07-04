"""BNID 自動再ログイン (`auto_login` と自動再ログインゲート) のテスト。

ネットワーク境界は FakePage で差し替え、時計とスリープは monkeypatch で制御して
決定的に回す。BNID 側の DOM 契約 (Phase 1 で調査済) の呼び出しシーケンスと
成功/失敗/タイムアウト/CAPTCHA 各パスを検証する。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from playwright.sync_api import Error as PlaywrightError

import canvasser
from canvasser import JST, AutoLoginOutcome, Credentials
from tests._fakes import FakePage, as_page

if TYPE_CHECKING:
    from pathlib import Path

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


def _install_fake_time(
    monkeypatch: pytest.MonkeyPatch, *, step: float = 0.5
) -> _FakeClock:
    """canvasser.time.monotonic / sleep を差し替えて時計を渡す。

    step を大きくすると 1 コールでデッドラインへの距離をより速く消化するため、
    ポーリング回数 (と evaluate 応答キュー消費) を減らせる。
    """
    clock = _FakeClock(step=step)
    monkeypatch.setattr(canvasser.time, "monotonic", clock)
    monkeypatch.setattr(canvasser.time, "sleep", _noop_sleep)
    return clock


class TestAutoLogin:
    """auto_login の成功・失敗・タイムアウト・CAPTCHA 各分岐。"""

    def test_成功パスでSUCCESSを返す(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """フォーム操作後の 1 回目の check_login が is_login=True なら SUCCESS。"""
        _install_fake_time(monkeypatch)
        fake = FakePage(responses=[_is_login_response(is_login=True)])

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is AutoLoginOutcome.SUCCESS
        # フォーム入力シーケンスの検証 (press_sequentially → click)
        interactions = {"press_sequentially", "click"}
        locator_calls = [c for c in fake.calls if c[0] in interactions]
        assert ("press_sequentially", ("#mail", "user@example.com")) in locator_calls
        assert ("press_sequentially", ("#pass", "hunter2")) in locator_calls
        assert ("click", "#btn-idpw-login") in locator_calls
        assert "ログイン成功を検知" in capsys.readouterr().err

    def test_エラーDOM可視化でPASSWORD_ERROR(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """パスワード誤り相当のエラーメッセージが表示されたら PASSWORD_ERROR。"""
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[_is_login_response(is_login=False)],
            visibility={"#error-input-area .c-message--warning": True},
        )

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is AutoLoginOutcome.PASSWORD_ERROR
        assert "認証エラー" in capsys.readouterr().err

    def test_CAPTCHA挿入でCAPTCHA_DETECTED(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """CAPTCHA を示唆する iframe が挿入されたら CAPTCHA_DETECTED + 誘導。"""
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[_is_login_response(is_login=False)],
            counts={'iframe[src*="recaptcha"]': 1},
        )

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is AutoLoginOutcome.CAPTCHA_DETECTED
        err = capsys.readouterr().err
        assert "CAPTCHA/2FA" in err
        assert "手動" in err

    def test_タイムアウトでTIMEOUT(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """成功も失敗も検知できなければ deadline 経過で TIMEOUT。"""
        _install_fake_time(monkeypatch)
        # ポーリング分だけ「未ログイン」を返し続けても deadline で抜ける
        fake = FakePage(responses=[_is_login_response(is_login=False)] * 20)

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is AutoLoginOutcome.TIMEOUT
        assert "タイムアウト" in capsys.readouterr().err

    def test_フォーム操作エラーで即FORM_ERROR(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """メール入力で PlaywrightError なら poll に入らず FORM_ERROR。"""
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

        assert result is AutoLoginOutcome.FORM_ERROR
        assert "フォーム操作でエラー" in capsys.readouterr().err


class TestCredentialsDisabled:
    """`_credentials_disabled` の disabled_until 判定。"""

    def test_disabled_untilが未来なら停止扱い(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """未来 ISO なら True と共に stderr へ案内を出す。"""
        future = (datetime.now(JST) + timedelta(hours=1)).isoformat()
        creds = Credentials(
            bnid_email="u@e",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
            disabled_until=future,
        )

        assert canvasser._credentials_disabled(creds, "haruo") is True
        assert "一時停止中" in capsys.readouterr().err

    def test_disabled_untilが過去なら継続可能(self) -> None:
        """過去時刻は fail-safe に「有効」として扱う。"""
        past = (datetime.now(JST) - timedelta(hours=1)).isoformat()
        creds = Credentials(
            bnid_email="u@e",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
            disabled_until=past,
        )

        assert canvasser._credentials_disabled(creds, "haruo") is False

    def test_disabled_untilがNoneなら継続可能(self) -> None:
        """初期状態 (None) では停止しない。"""
        creds = Credentials(
            bnid_email="u@e",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
        )

        assert canvasser._credentials_disabled(creds, "haruo") is False

    def test_パース不能なdisabled_untilは継続可能(self) -> None:
        """手改変で不正な文字列でも fail-safe に扱う。"""
        creds = Credentials(
            bnid_email="u@e",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
            disabled_until="not-a-date",
        )

        assert canvasser._credentials_disabled(creds, "haruo") is False


class TestRecordCredentialsFailure:
    """連続失敗ガードの書き戻し。"""

    def test_失敗するとfailure_countが増える(self, tmp_path: Path) -> None:
        """1 回失敗で failure_count が +1 されて保存される。"""
        creds = Credentials(
            bnid_email="u@e",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
        )
        canvasser.save_credentials(tmp_path, creds)

        canvasser._record_credentials_failure(tmp_path, creds)

        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 1
        assert got.disabled_until is None

    def test_上限到達でdisabled_untilが設定される(self, tmp_path: Path) -> None:
        """CREDENTIALS_MAX_FAILURES に達すると disabled_until が未来時刻になる。"""
        creds = Credentials(
            bnid_email="u@e",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
            failure_count=canvasser.CREDENTIALS_MAX_FAILURES - 1,
        )
        canvasser.save_credentials(tmp_path, creds)

        canvasser._record_credentials_failure(tmp_path, creds)

        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == canvasser.CREDENTIALS_MAX_FAILURES
        assert got.disabled_until is not None
        # ISO パースできる未来時刻
        deadline = datetime.fromisoformat(got.disabled_until)
        assert deadline > datetime.now(JST)

    def test_成功リセットはfailure_countをゼロにする(self, tmp_path: Path) -> None:
        """成功時 failure_count と disabled_until がクリアされる。"""
        creds = Credentials(
            bnid_email="u@e",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
            failure_count=2,
            disabled_until="2026-07-05T06:00:00+09:00",
        )
        canvasser.save_credentials(tmp_path, creds)

        canvasser._reset_credentials_failure(tmp_path, creds)

        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 0
        assert got.disabled_until is None

    def test_成功リセットは変更なしなら書き込まない(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """初期状態 (failure_count=0, disabled_until=None) は無駄な I/O を避ける。"""
        creds = Credentials(
            bnid_email="u@e",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
        )
        calls: list[tuple[Path, Credentials]] = []

        def _spy_save(profile_dir: Path, credentials: Credentials) -> None:
            calls.append((profile_dir, credentials))

        monkeypatch.setattr(canvasser, "save_credentials", _spy_save)

        canvasser._reset_credentials_failure(tmp_path, creds)

        assert calls == []


def _install_creds(profile_dir: Path, **overrides: object) -> Credentials:
    """テスト用に credentials.json を保存し、書いた Credentials を返す。"""
    fields: dict[str, object] = {
        "bnid_email": "u@e",
        "bnid_password": "pw",
        "saved_at": "2026-07-05T00:00:00+09:00",
        "failure_count": 0,
        "disabled_until": None,
    }
    fields.update(overrides)
    creds = Credentials(**fields)  # pyright: ignore[reportArgumentType]
    canvasser.save_credentials(profile_dir, creds)
    return creds


class TestAttemptAutoRelogin:
    """attempt_auto_relogin のゲート・リトライ・失敗ガードの合流点。"""

    def test_credentialsが無ければFalseでgotoしない(self, tmp_path: Path) -> None:
        """credentials.json 非存在ではブラウザ側にも触らず False。"""
        fake = FakePage()

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        assert not any(c[0] == "goto" for c in fake.calls)

    def test_disabled_until有効ならFalseでgotoしない(self, tmp_path: Path) -> None:
        """disabled_until が未来ならブラウザ側にも触らない。"""
        future = (datetime.now(JST) + timedelta(hours=1)).isoformat()
        _install_creds(tmp_path, disabled_until=future)
        fake = FakePage()

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        assert not any(c[0] == "goto" for c in fake.calls)

    def test_成功でTrueとfailure_countリセット(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auto_login SUCCESS で failure_count がクリアされる。"""
        _install_creds(tmp_path, failure_count=2)
        _install_fake_time(monkeypatch)
        fake = FakePage(responses=[_is_login_response(is_login=True)])

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is True
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 0

    def test_タイムアウトはリトライして最終的にTrueで成功(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 回目 TIMEOUT、2 回目 SUCCESS で True + failure_count リセット。

        step=10.0 で auto_login 1 回あたり ~5 回ポーリングされるため、6 個目に
        SUCCESS 応答を置くと 2 回目 auto_login の 1 iter 目で成功する。
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch, step=10.0)
        fake = FakePage(
            responses=[
                *[_is_login_response(is_login=False)] * 5,
                _is_login_response(is_login=True),
            ]
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is True

    def test_リトライしても失敗すればFalseと失敗カウント加算(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """TIMEOUT → TIMEOUT で False かつ failure_count = 1。"""
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch, step=10.0)
        # 2 回の auto_login 分のポーリング応答をたっぷり用意する
        fake = FakePage(responses=[_is_login_response(is_login=False)] * 40)

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 1
        assert "リトライします" in capsys.readouterr().err

    def test_パスワード誤りは即Falseでリトライしない(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PASSWORD_ERROR は即 False + failure_count 加算、evaluate は 1 回だけ。"""
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[_is_login_response(is_login=False)],
            visibility={"#error-input-area .c-message--warning": True},
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        # evaluate は 1 回のみ (リトライしていない)
        evaluate_calls = [c for c in fake.calls if "async" in c[0]]
        assert len(evaluate_calls) == 1
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 1

    def test_goto失敗はFalseと失敗カウント加算(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """BNID login への遷移で PlaywrightError なら False + failure_count 加算。"""
        _install_creds(tmp_path)
        fake = FakePage(goto_errors=[PlaywrightError("navigation failed")])

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        assert "BNID ログイン画面への遷移で失敗" in capsys.readouterr().err
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 1

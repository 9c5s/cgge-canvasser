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
from tests._fakes import FakeBrowserContext, FakePage, as_context, as_page

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
        """フォーム操作後の 1 回目の check_login が is_login=True なら SUCCESS。

        入力欄には fill("") で既存テキストをクリアしてから press_sequentially で
        実キー入力を装う (press_sequentially が append 挙動なため二重入力を防止)。
        """
        _install_fake_time(monkeypatch)
        fake = FakePage(responses=[_is_login_response(is_login=True)])

        result, submitted = canvasser.auto_login(
            as_page(fake), _sample_creds(), timeout_sec=1
        )

        assert result is AutoLoginOutcome.SUCCESS
        # 実 submit は 1 回 (click 経由の POST 相当)
        assert submitted == 1
        # フォーム入力シーケンスの検証 (fill("") → press_sequentially → click)
        interactions = {"fill", "press_sequentially", "click"}
        locator_calls = [c for c in fake.calls if c[0] in interactions]
        assert ("fill", ("#mail", "")) in locator_calls
        assert ("fill", ("#pass", "")) in locator_calls
        assert ("press_sequentially", ("#mail", "user@example.com")) in locator_calls
        assert ("press_sequentially", ("#pass", "hunter2")) in locator_calls
        # click は no_wait_after=True で呼ばれる (navigation 待ちで raise しないため)
        assert (
            "click",
            ("#btn-idpw-login", {"no_wait_after": True}),
        ) in locator_calls
        # fill は press_sequentially より前に来る
        assert locator_calls.index(("fill", ("#mail", ""))) < locator_calls.index((
            "press_sequentially",
            ("#mail", "user@example.com"),
        ))
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

        result, submitted = canvasser.auto_login(
            as_page(fake), _sample_creds(), timeout_sec=1
        )

        assert result is AutoLoginOutcome.PASSWORD_ERROR
        # click 済みなので submit=1
        assert submitted == 1
        assert "認証エラー" in capsys.readouterr().err

    def test_submit前のCAPTCHAで即CAPTCHA_DETECTED(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """フォーム入力前に CAPTCHA を検知したら submit せずに CAPTCHA_DETECTED。

        BNID がフォーム表示時点で CAPTCHA を出しているケース。パスワードを送信して
        しまうと failure_count を無駄に消費するため、submit 前に検知して abort する。
        """
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[],
            counts={'iframe[src*="recaptcha"]': 1},
        )

        result, submitted = canvasser.auto_login(
            as_page(fake), _sample_creds(), timeout_sec=1
        )

        assert result is AutoLoginOutcome.CAPTCHA_DETECTED
        # BNID にパスワード送信していないので submit=0
        assert submitted == 0
        # フォーム入力操作は 1 つも走っていない (パスワード送信なし)
        assert not any(c[0] == "fill" for c in fake.calls)
        assert not any(c[0] == "press_sequentially" for c in fake.calls)
        assert not any(c[0] == "click" for c in fake.calls)
        err = capsys.readouterr().err
        assert "submit 前に CAPTCHA/2FA" in err

    def test_submit後のCAPTCHA動的挿入でCAPTCHA_DETECTED(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """submit 後 (ポーリング中) に CAPTCHA が動的挿入されたら CAPTCHA_DETECTED。

        BNID が連続失敗で CAPTCHA を動的に差し込むケースの検証。
        `counts_sequence` で「pre-submit 時=0 → poll 中=1」と切り替え、
        pre-check ではすり抜け、_poll_login_outcome の CAPTCHA 分岐で検知する
        パスを踏む。submit は既に発生しているので `submitted=1`。
        """
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[_is_login_response(is_login=False)],
            # 1 回目 (pre-submit pre-check): 0、2 回目以降 (poll 中): 1
            counts_sequence={'iframe[src*="turnstile"]': [0, 1]},
        )

        result, submitted = canvasser.auto_login(
            as_page(fake), _sample_creds(), timeout_sec=1
        )

        assert result is AutoLoginOutcome.CAPTCHA_DETECTED
        # 動的挿入なので click 済み → submit=1
        assert submitted == 1
        # フォーム入力・click が実行されている (pre-submit 分岐と区別)
        assert any(c[0] == "press_sequentially" for c in fake.calls)
        err = capsys.readouterr().err
        assert "CAPTCHA/2FA" in err

    def test_タイムアウトでTIMEOUT(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """成功も失敗も検知できなければ deadline 経過で TIMEOUT + submit=1。"""
        _install_fake_time(monkeypatch)
        # ポーリング分だけ「未ログイン」を返し続けても deadline で抜ける
        fake = FakePage(responses=[_is_login_response(is_login=False)] * 20)

        result, submitted = canvasser.auto_login(
            as_page(fake), _sample_creds(), timeout_sec=1
        )

        assert result is AutoLoginOutcome.TIMEOUT
        # click 済みなので submit=1
        assert submitted == 1
        assert "タイムアウト" in capsys.readouterr().err

    def test_click時のPlaywrightErrorはpollingに進みsubmitted1(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """click() が PlaywrightError を raise した場合の扱い。

        BNID の redirect race で click が raise しても submit は既に発生している
        可能性があるため、FORM_ERROR で切らずに polling に進んで実結果を判定する。
        polling で is_login=True を検知したら SUCCESS + submit=1。
        """
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[_is_login_response(is_login=True)],
            click_errors={
                "#btn-idpw-login": [PlaywrightError("navigation raced with click")]
            },
        )

        result, submitted = canvasser.auto_login(
            as_page(fake), _sample_creds(), timeout_sec=1
        )

        assert result is AutoLoginOutcome.SUCCESS
        # click は raise したが submit は起きている前提で計上する
        assert submitted == 1

    def test_フォーム操作エラーで即FORM_ERROR(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """メール入力で PlaywrightError なら poll に入らず FORM_ERROR + submit=0。"""
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[],
            wait_for_errors={
                "#btn-idpw-login:not([disabled])": [
                    PlaywrightError("timeout: disabled が外れない")
                ]
            },
        )

        result, submitted = canvasser.auto_login(
            as_page(fake), _sample_creds(), timeout_sec=1
        )

        assert result is AutoLoginOutcome.FORM_ERROR
        # click に到達していないので BNID に届いていない
        assert submitted == 0
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
    """テスト用に active credentials.json を保存し、書いた Credentials を返す。"""
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


def _install_pending_creds(profile_dir: Path, **overrides: object) -> Credentials:
    """テスト用に pending credentials.json.pending を保存する。"""
    fields: dict[str, object] = {
        "bnid_email": "u@e",
        "bnid_password": "pw",
        "saved_at": "2026-07-05T00:00:00+09:00",
        "failure_count": 0,
        "disabled_until": None,
    }
    fields.update(overrides)
    creds = Credentials(**fields)  # pyright: ignore[reportArgumentType]
    canvasser.save_pending_credentials(profile_dir, creds)
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
        """auto_login SUCCESS で failure_count がクリアされる。

        post-init check_login=False (auto_login まで進む) → auto_login iter 1 で True。
        """
        _install_creds(tmp_path, failure_count=2)
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[
                _is_login_response(is_login=False),  # post-init check
                _is_login_response(is_login=True),  # auto_login iter 1
            ]
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is True
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 0
        # auto_login のフォーム操作が実際に走った (post-init 短絡ではない)
        assert any(c[0] == "press_sequentially" for c in fake.calls)

    def test_遷移後既にログイン済みならauto_loginを短絡してTrue(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """初回 check_login が false negative だった場合の救済。

        LOGIN_ENTRY_URL 遷移後の check_login が True なら、cookie は実は有効。
        auto_login (mission page への redirect で #mail 無し → FORM_ERROR) を回避
        して即 SUCCESS で抜け、failure_count も減らす。
        """
        _install_creds(tmp_path, failure_count=1)
        _install_fake_time(monkeypatch)
        fake = FakePage(responses=[_is_login_response(is_login=True)])

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is True
        # auto_login のフォーム操作は一切走らない
        assert not any(c[0] == "press_sequentially" for c in fake.calls)
        # failure_count は 0 にリセット
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 0
        assert "セッション有効を確認" in capsys.readouterr().err

    def test_タイムアウトはリトライして最終的にTrueで成功(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 回目 TIMEOUT → リトライ → 2 回目 SUCCESS で True + failure_count リセット。

        responses = [False (post-init), False x5 (auto_login 1 TIMEOUT),
        False (post-retry), True (auto_login 2 iter 1 SUCCESS)] = 8 応答。
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch, step=10.0)
        fake = FakePage(
            responses=[
                *[_is_login_response(is_login=False)] * 7,
                _is_login_response(is_login=True),
            ]
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is True
        # 初回 + リトライで goto は 2 回呼ばれる
        goto_calls = [c for c in fake.calls if c[0] == "goto"]
        assert len(goto_calls) == 2

    def test_タイムアウト後の遅延成功をSUCCESSとして拾う(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """1 回目 TIMEOUT 後にリトライ用 goto で check_login=True (遅延成功) の場合。

        1 回目 submit が遅延で成功して session が有効化したケース。retry auto_login
        を回すと valid cookie で mission page へ redirect → #mail 無し → FORM_ERROR に
        誤判定するのを避けるため、post-retry check_login で SUCCESS として拾う。
        """
        _install_creds(tmp_path, failure_count=1)
        _install_fake_time(monkeypatch, step=10.0)
        # post-init: False, auto_login 1: 5 False → TIMEOUT, post-retry: True → SUCCESS
        fake = FakePage(
            responses=[
                *[_is_login_response(is_login=False)] * 6,
                _is_login_response(is_login=True),
            ]
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is True
        # 2 回目 auto_login は走っていない (press_sequentially の実行回数で判別)
        press_calls = [c for c in fake.calls if c[0] == "press_sequentially"]
        # 1 回目 auto_login のみ: fill 2 回 + press_sequentially 2 回
        assert len(press_calls) == 2
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 0
        assert "遅延成功を検知" in capsys.readouterr().err

    def test_リトライしても失敗すればFalseとfailure_count2回分加算(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """TIMEOUT → TIMEOUT で False、failure_count は 2 回分加算、goto 2 回。

        BNID には 2 回パスワードを送っているので failure_count に 2 加算する。
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch, step=10.0)
        # 2 回の auto_login + post-init/retry check 分のポーリング応答
        fake = FakePage(responses=[_is_login_response(is_login=False)] * 40)

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        # BNID に 2 回 submit されているので 2 回分計上する
        assert got.failure_count == 2
        assert "リトライします" in capsys.readouterr().err
        assert len([c for c in fake.calls if c[0] == "goto"]) == 2

    def test_パスワード誤りは即Falseでリトライしない(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PASSWORD_ERROR は即 False + failure_count 加算、goto は 1 回のみ。

        responses = [False (post-init), False (auto_login iter 1)] で
        visibility=True にすることで iter 1 内でエラー DOM 検知 → PASSWORD_ERROR。
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[
                _is_login_response(is_login=False),
                _is_login_response(is_login=False),
            ],
            visibility={"#error-input-area .c-message--warning": True},
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        # goto は 1 回のみ (リトライしていない)
        assert len([c for c in fake.calls if c[0] == "goto"]) == 1
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 1

    def test_初回goto失敗はFalseだがfailure_countは加算しない(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """認証情報を BNID に送る前の一時的なネットワーク不調は据え置く。

        BNID にパスワードを送っていないので BNID ロックの原因にならず、
        failure_count は加算せず disabled_until への進行も止める。
        """
        _install_creds(tmp_path, failure_count=2)
        fake = FakePage(goto_errors=[PlaywrightError("navigation failed")])

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        assert "BNID ログイン画面への遷移で失敗" in capsys.readouterr().err
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        # 初期値 2 のまま + disabled_until も未設定 (自動再ログインは次回も試せる)
        assert got.failure_count == 2
        assert got.disabled_until is None

    def test_リトライgoto失敗は認証試行済みなのでfailure_countを加算(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 回目 auto_login TIMEOUT の後にリトライ用 goto が失敗するケース。

        1 回目の auto_login は既に BNID に入力を送信しているため、リトライ用
        goto が失敗しても認証試行 1 回分は計上する (BNID 側から見て入力送信済)。
        responses は post-init 1 + auto_login 1 で 5 iter = 計 6 個必要。
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch, step=10.0)
        fake = FakePage(
            responses=[_is_login_response(is_login=False)] * 8,
            # 1 回目 goto 成功、2 回目 goto (リトライ) 失敗
            goto_errors=[None, PlaywrightError("retry navigation failed")],
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 1

    def test_初回auto_login_FORM_ERRORはfailure_count加算しない(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """初回 auto_login が pre-submit の FORM_ERROR (フォームが見当たらない等)。

        BNID にはパスワードを送っていないので、failure_count を消費しない。
        """
        _install_creds(tmp_path, failure_count=1)
        _install_fake_time(monkeypatch)
        # post-init check False → auto_login: wait_for が失敗 → FORM_ERROR
        fake = FakePage(
            responses=[_is_login_response(is_login=False)],
            wait_for_errors={
                "#btn-idpw-login:not([disabled])": [PlaywrightError("no form")]
            },
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        # 初期値 1 のまま (加算されない)
        assert got.failure_count == 1

    def test_リトライauto_loginがFORM_ERRORなら1回分だけ加算(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 回目 TIMEOUT → リトライ goto 成功 → 2 回目 auto_login が FORM_ERROR。

        2 回目は pre-submit で失敗しているため実 submit は 1 回のみ、failure_count は
        1 だけ加算される。
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch, step=10.0)
        # 5 個 False で auto_login 1 TIMEOUT、その次に post-retry check False、
        # そのあと 2 回目 auto_login の wait_for が失敗して FORM_ERROR
        fake = FakePage(
            responses=[_is_login_response(is_login=False)] * 8,
            # 2 回目 goto (リトライ) 用の wait_for エラー: 最初の呼び出しは 1 回目
            # auto_login の click 前で消費されるのでダミー None、2 個目が 2 回目分
            wait_for_errors={
                "#btn-idpw-login:not([disabled])": [
                    None,  # 1 回目 auto_login の wait_for は通過
                    PlaywrightError("no form on retry"),
                ]
            },
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        # 1 回目 TIMEOUT ぶんだけ計上
        assert got.failure_count == 1

    def test_リトライ後のcheck_login_PlaywrightErrorは2回目auto_loginに進む(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """リトライ用 goto 後の check_login が PlaywrightError の場合の扱い。

        遅延成功チェックの check_login が redirect 中に raise しても例外が escape
        しないようにする (escape すると failure_count 更新前に process_account へ
        飛んで無限に BNID にパスワードを投げる事故が起きる)。
        raise 後は 2 回目 auto_login に進み、TIMEOUT なら submit を計上する。
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch, step=10.0)
        # 1 個目 (post-init check): False → auto_login 1 へ
        # 2-6 個目 (auto_login 1 の 5 iter): False → TIMEOUT
        # 7 個目 (post-retry check): PlaywrightError → suppress + 2 回目 auto_login へ
        # 8-12 個目 (auto_login 2 の 5 iter): False → TIMEOUT
        # 13 個目 (post-2nd check): False (retry TIMEOUT に留まる)
        # submit 回数は 1 + 1 = 2
        fake = FakePage(
            responses=[
                *[_is_login_response(is_login=False)] * 6,
                PlaywrightError("check_login raised during redirect"),
                *[_is_login_response(is_login=False)] * 20,
            ]
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        # 2 回の submit が計上される (1 回目 TIMEOUT + 2 回目 TIMEOUT)
        assert got.failure_count == 2

    def test_初回auto_loginの_pre_submit_CAPTCHAはfailure_count加算しない(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auto_login が submit 前に CAPTCHA を検知した場合の失敗ガード。

        BNID にパスワードを送信していないので、CAPTCHA_DETECTED であっても
        failure_count は加算しない (無駄な失敗計上で disabled_until を早期発動
        させない)。
        """
        _install_creds(tmp_path, failure_count=1)
        _install_fake_time(monkeypatch)
        # post-init check_login False → auto_login pre-check で CAPTCHA 検知
        fake = FakePage(
            responses=[_is_login_response(is_login=False)],
            counts={'iframe[src*="hcaptcha"]': 1},
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        # 初期値 1 のまま (BNID に届いていないので加算しない)
        assert got.failure_count == 1

    def test_failure_count上限近くのTIMEOUTはリトライを控える(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """failure_count が MAX-1 の状態で 1 回目 TIMEOUT ならリトライしない。

        1 回目 submit で failure_count が MAX に到達する見込みなので、リトライで
        更にもう 1 submit を追加すると BNID アカウントロック閾値を超える。予算超過を
        避けるため retry_after_timeout で判定して控える。
        """
        max_fail = canvasser.CREDENTIALS_MAX_FAILURES
        _install_creds(tmp_path, failure_count=max_fail - 1)
        _install_fake_time(monkeypatch, step=10.0)
        # 1 回目 auto_login TIMEOUT のポーリング 5 回分だけ用意する
        # (2 回目 auto_login が走らないことも同時に検証)
        fake = FakePage(responses=[_is_login_response(is_login=False)] * 6)

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        # goto は 1 回のみ (リトライしていない)
        assert len([c for c in fake.calls if c[0] == "goto"]) == 1
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        # 1 回分だけ加算されて MAX に達し disabled_until が設定される
        assert got.failure_count == max_fail
        assert got.disabled_until is not None

    def test_リトライ後TIMEOUT_late_successをSUCCESSとして拾う(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """2 回目 auto_login TIMEOUT 直後の check_login=True で SUCCESS 拾い。

        1 回目 TIMEOUT → retry goto success → post-retry check False →
        2 回目 auto_login TIMEOUT → 直後の check_login True (2 回目 submit の遅延成功)
        → SUCCESS。responses は post-init 1 + auto_login 1 = 5 + post-retry 1 +
        auto_login 2 = 5 + 遅延成功 check 1 = 13。
        """
        _install_creds(tmp_path, failure_count=1)
        _install_fake_time(monkeypatch, step=10.0)
        fake = FakePage(
            responses=[
                *[_is_login_response(is_login=False)] * 12,
                _is_login_response(is_login=True),
            ]
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is True
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        # 遅延成功で failure_count はリセット
        assert got.failure_count == 0
        assert "リトライ後にも遅延成功" in capsys.readouterr().err


def _run_options(*, execute: bool, auto_relogin: bool = True) -> canvasser.RunOptions:
    """_ensure_authenticated テスト用の RunOptions を組み立てる。"""
    return canvasser.RunOptions(
        run_mission=True, execute=execute, auto_relogin=auto_relogin
    )


class TestEnsureAuthenticated:
    """_ensure_authenticated のログイン判定・dry-run ゲート。"""

    def test_ログイン済みならTrue(self) -> None:
        """check_login が true なら auto_relogin を試さずに True。"""
        fake = FakePage(responses=[_is_login_response(is_login=True)])
        opts = _run_options(execute=True)

        assert (
            canvasser._ensure_authenticated(
                as_page(fake), "haruo", canvasser.Path("."), opts
            )
            is True
        )

    def test_dry_runでは自動再ログインを走らせない(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """execute=False では credentials が保存されていても auto_login を送らない。"""
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch)
        # check_login: 未ログイン、以降 auto_login への呼び出しは想定しない
        fake = FakePage(responses=[_is_login_response(is_login=False)])
        opts = _run_options(execute=False)

        result = canvasser._ensure_authenticated(as_page(fake), "haruo", tmp_path, opts)

        assert result is False
        # dry-run では auto_login のフォーム操作 (fill/press_sequentially) が起きない
        assert not any(c[0] == "fill" for c in fake.calls)
        assert not any(c[0] == "press_sequentially" for c in fake.calls)
        # 未ログインメッセージだけ出る
        assert "未ログイン" in capsys.readouterr().err

    def test_execute_かつauto_relogin無効なら自動再ログインを走らせない(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-auto-relogin は execute でも auto_login を封じる (opt-out 尊重)。"""
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch)
        fake = FakePage(responses=[_is_login_response(is_login=False)])
        opts = _run_options(execute=True, auto_relogin=False)

        result = canvasser._ensure_authenticated(as_page(fake), "haruo", tmp_path, opts)

        assert result is False
        assert not any(c[0] == "press_sequentially" for c in fake.calls)

    def test_execute_かつauto_relogin有効なら自動再ログインを試す(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """execute + auto_relogin 有効かつ credentials 保存で auto_login が走る。

        流れ: _ensure_authenticated の check_login=false →
        attempt_auto_relogin(goto + post-init check_login=false + auto_login SUCCESS)。
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch)
        fake = FakePage(
            responses=[
                _is_login_response(is_login=False),  # _ensure_authenticated 側
                _is_login_response(is_login=False),  # attempt_auto_relogin post-init
                _is_login_response(is_login=True),  # auto_login iter 1 SUCCESS
            ]
        )
        opts = _run_options(execute=True)

        assert (
            canvasser._ensure_authenticated(as_page(fake), "haruo", tmp_path, opts)
            is True
        )
        # auto_login のフォーム操作が実際に呼ばれた
        assert any(c[0] == "press_sequentially" for c in fake.calls)


class TestRunLoginInitFlow:
    """run_login_init_flow の pending 検証 + 昇格フロー。

    login-init は pending credentials (credentials.json.pending) を実ログインで
    検証し、SUCCESS のときだけ active (credentials.json) に昇格させる。
    非 SUCCESS 時は pending を破棄して既存 active を温存する。
    """

    def test_成功時はpendingをactiveに昇格して0を返す(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auto_login SUCCESS で pending → active に昇格、終了コード 0。"""
        _install_pending_creds(tmp_path, bnid_password="new-password")
        _install_fake_time(monkeypatch)
        fake_page = FakePage(responses=[_is_login_response(is_login=True)])
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 0
        assert fake_ctx.calls == ["clear_cookies"]
        # pending は消えて active が新パスワードで作られている
        assert not (tmp_path / "credentials.json.pending").exists()
        active = canvasser.load_credentials(tmp_path)
        assert active is not None
        assert active.bnid_password == "new-password"
        assert any(c[0] == "press_sequentially" for c in fake_page.calls)

    def test_pending無しでは手動フローにフォールバック(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """pending が読めない場合は run_login_flow に委譲する。"""
        _install_fake_time(monkeypatch)
        # run_login_flow は check_login を is_login=true で 1 回返せば即 return 0
        fake_page = FakePage(responses=[_is_login_response(is_login=True)])
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 0
        # Cookie 破棄は pending が無いのでスキップされる
        assert fake_ctx.calls == []
        assert "pending credentials を読めません" in capsys.readouterr().err

    def test_PASSWORD_ERRORはpendingを破棄しactiveを温存(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """pending PASSWORD_ERROR で pending を破棄。既存 active は温存される。

        (パスワード変更時のタイプミス相当。旧 active credentials で auto-relogin
        が引き続き機能する。)
        """
        _install_creds(tmp_path, bnid_password="old-verified-password")
        _install_pending_creds(tmp_path, bnid_password="mistyped")
        _install_fake_time(monkeypatch)
        fake_page = FakePage(
            responses=[_is_login_response(is_login=False)],
            visibility={"#error-input-area .c-message--warning": True},
        )
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 1
        # pending は破棄
        assert not (tmp_path / "credentials.json.pending").exists()
        # 既存 active は温存 (旧パスワード)
        active = canvasser.load_credentials(tmp_path)
        assert active is not None
        assert active.bnid_password == "old-verified-password"
        assert "認証エラー" in capsys.readouterr().err

    def test_CAPTCHA検知時もpendingを破棄しactiveを温存(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """CAPTCHA_DETECTED は pending 破棄 + active 温存 + exit 1。

        pending は「実ログイン成功」の証明が取れていないため、`_ensure_authenticated`
        から未検証パスワードが unattended に送信される事故を防ぐために破棄する。
        旧 active credentials は残るので既存アカウントの自動再ログイン能力は保つ。
        """
        _install_creds(tmp_path, bnid_password="old-verified-password")
        _install_pending_creds(tmp_path, bnid_password="new-untested")
        _install_fake_time(monkeypatch)
        # CAPTCHA が submit 前 pre-check で検知される
        fake_page = FakePage(
            responses=[],
            counts={'iframe[src*="recaptcha"]': 1},
        )
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 1
        # pending は破棄
        assert not (tmp_path / "credentials.json.pending").exists()
        # 旧 active は温存
        active = canvasser.load_credentials(tmp_path)
        assert active is not None
        assert active.bnid_password == "old-verified-password"
        assert "自動検証に失敗" in capsys.readouterr().err

    def test_TIMEOUTもpendingを破棄しactiveを温存(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """TIMEOUT でも pending 破棄 + active 温存 + exit 1。"""
        _install_creds(tmp_path, bnid_password="old-verified-password")
        _install_pending_creds(tmp_path, bnid_password="new-untested")
        # step=10.0 で auto_login のポーリングを 5 回に圧縮する
        _install_fake_time(monkeypatch, step=10.0)
        fake_page = FakePage(responses=[_is_login_response(is_login=False)] * 10)
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 1
        assert not (tmp_path / "credentials.json.pending").exists()
        active = canvasser.load_credentials(tmp_path)
        assert active is not None
        assert active.bnid_password == "old-verified-password"
        assert "自動検証に失敗" in capsys.readouterr().err

    def test_goto失敗もpendingを破棄しactiveを温存(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """goto PlaywrightError でも pending を破棄して active を温存する。"""
        _install_creds(tmp_path, bnid_password="old-verified-password")
        _install_pending_creds(tmp_path, bnid_password="new-untested")
        _install_fake_time(monkeypatch)
        fake_page = FakePage(goto_errors=[PlaywrightError("navigation failed")])
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 1
        assert not (tmp_path / "credentials.json.pending").exists()
        active = canvasser.load_credentials(tmp_path)
        assert active is not None
        assert active.bnid_password == "old-verified-password"
        assert "BNID ログイン画面への遷移で失敗" in capsys.readouterr().err

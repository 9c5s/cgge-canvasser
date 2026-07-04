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

        result = canvasser.auto_login(as_page(fake), _sample_creds(), timeout_sec=1)

        assert result is AutoLoginOutcome.SUCCESS
        # フォーム入力シーケンスの検証 (fill("") → press_sequentially → click)
        interactions = {"fill", "press_sequentially", "click"}
        locator_calls = [c for c in fake.calls if c[0] in interactions]
        assert ("fill", ("#mail", "")) in locator_calls
        assert ("fill", ("#pass", "")) in locator_calls
        assert ("press_sequentially", ("#mail", "user@example.com")) in locator_calls
        assert ("press_sequentially", ("#pass", "hunter2")) in locator_calls
        assert ("click", "#btn-idpw-login") in locator_calls
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
        リトライ時はフォームリセットのため LOGIN_ENTRY_URL に再遷移する。
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
        # 初回 + リトライで goto は 2 回呼ばれる
        goto_calls = [c for c in fake.calls if c[0] == "goto"]
        assert len(goto_calls) == 2

    def test_リトライしても失敗すればFalseと失敗カウント加算(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """TIMEOUT → TIMEOUT で False かつ failure_count = 1、goto 2 回。"""
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
        assert len([c for c in fake.calls if c[0] == "goto"]) == 2

    def test_パスワード誤りは即Falseでリトライしない(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PASSWORD_ERROR は即 False + failure_count 加算、goto は 1 回のみ。"""
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
        # goto も 1 回のみ (リトライしていない)
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
        """
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch, step=10.0)
        fake = FakePage(
            responses=[_is_login_response(is_login=False)] * 6,
            # 1 回目 goto 成功、2 回目 goto (リトライ) 失敗
            goto_errors=[None, PlaywrightError("retry navigation failed")],
        )

        result = canvasser.attempt_auto_relogin(as_page(fake), tmp_path, "haruo")

        assert result is False
        got = canvasser.load_credentials(tmp_path)
        assert got is not None
        assert got.failure_count == 1


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
        """execute + auto_relogin 有効かつ credentials 保存で auto_login が走る。"""
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch)
        # 1 回目 check_login=false → auto_login 走行 → is_login=true で SUCCESS
        fake = FakePage(
            responses=[
                _is_login_response(is_login=False),
                _is_login_response(is_login=True),
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
    """run_login_init_flow の Cookie クリア + auto_login 検証フロー。"""

    def test_成功時は0を返しCookieを破棄する(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """auto_login SUCCESS で終了コード 0、context.clear_cookies が 1 回。"""
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch)
        fake_page = FakePage(responses=[_is_login_response(is_login=True)])
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 0
        assert fake_ctx.calls == ["clear_cookies"]
        assert any(c[0] == "goto" for c in fake_page.calls)
        # auto_login の入力操作が実行された
        assert any(c[0] == "press_sequentially" for c in fake_page.calls)

    def test_credentials無しでは手動フローにフォールバック(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """credentials.json が読めない場合は run_login_flow に委譲する。"""
        _install_fake_time(monkeypatch)
        # run_login_flow は check_login を is_login=true で 1 回返せば即 return 0
        fake_page = FakePage(responses=[_is_login_response(is_login=True)])
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 0
        # Cookie 破棄は creds が無いのでスキップされる
        assert fake_ctx.calls == []
        assert "credentials.json を読めません" in capsys.readouterr().err

    def test_auto_login失敗時は手動フローにフォールバック(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """PASSWORD_ERROR 検知後は run_login_flow に落ちる。手動操作を委ねる形。"""
        _install_creds(tmp_path)
        _install_fake_time(monkeypatch)
        fake_page = FakePage(
            # auto_login 用: is_login=false + エラー DOM 可視化 → PASSWORD_ERROR
            # run_login_flow フォールバック: is_login=true で即成功
            responses=[
                _is_login_response(is_login=False),
                _is_login_response(is_login=True),
            ],
            visibility={"#error-input-area .c-message--warning": True},
        )
        fake_ctx = FakeBrowserContext()

        code = canvasser.run_login_init_flow(
            as_context(fake_ctx), as_page(fake_page), tmp_path
        )

        assert code == 0
        assert "自動検証に失敗" in capsys.readouterr().err

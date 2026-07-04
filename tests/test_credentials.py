"""credentials.json 永続化 (保存・読込・権限縮小) のテスト。

state.json 同様にファイル I/O を伴う Medium テストだが、ネットワークや外部
サービスには依存しない。
"""

from __future__ import annotations

import json
import os
import stat
from typing import TYPE_CHECKING

import pytest

import canvasser
from canvasser import Credentials

if TYPE_CHECKING:
    from pathlib import Path


def _cred_file(profile_dir: Path) -> Path:
    """profile_dir 配下の credentials ファイルパスを返す。"""
    return profile_dir / "credentials.json"


def _sample() -> Credentials:
    """テスト用の Credentials インスタンスを組み立てる。"""
    return Credentials(
        bnid_email="user@example.com",
        bnid_password="hunter2",
        saved_at="2026-07-05T12:34:56+09:00",
    )


class TestSaveAndLoadCredentials:
    """save_credentials → load_credentials のラウンドトリップと壊れ入力の許容。"""

    def test_保存した内容をそのまま読み戻せる(self, tmp_path: Path) -> None:
        """save → load で dataclass の中身が完全一致する。"""
        canvasser.save_credentials(tmp_path, _sample())

        got = canvasser.load_credentials(tmp_path)

        assert got == _sample()

    def test_ファイルが無ければNone(self, tmp_path: Path) -> None:
        """credentials.json 未作成では機能無効扱いで None を返す。"""
        assert canvasser.load_credentials(tmp_path) is None

    def test_失敗カウンタとdisabled_untilも往復する(self, tmp_path: Path) -> None:
        """auto_login 用に失敗カウンタ / disabled_until が書き戻せる。"""
        cred = Credentials(
            bnid_email="user@example.com",
            bnid_password="pw",
            saved_at="2026-07-05T00:00:00+09:00",
            failure_count=2,
            disabled_until="2026-07-05T06:00:00+09:00",
        )

        canvasser.save_credentials(tmp_path, cred)

        assert canvasser.load_credentials(tmp_path) == cred

    def test_ディレクトリが無ければ作成する(self, tmp_path: Path) -> None:
        """profile_dir が未作成でも mkdir して保存する。"""
        target = tmp_path / "new" / "profile"

        canvasser.save_credentials(target, _sample())

        assert _cred_file(target).exists()

    def test_一時ファイルを残さない(self, tmp_path: Path) -> None:
        """書き込み成功後は credentials.json 以外の残骸が無い。"""
        canvasser.save_credentials(tmp_path, _sample())

        names = [p.name for p in tmp_path.iterdir()]
        assert names == ["credentials.json"]

    def test_壊れたJSONは警告してNoneを返す(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """JSON パース不能でも認証情報を無視して None を返す (fail-safe)。"""
        _cred_file(tmp_path).write_text("{{{", encoding="utf-8")

        assert canvasser.load_credentials(tmp_path) is None
        assert "認証情報を無視" in capsys.readouterr().err

    def test_トップレベル非dictはNoneを返す(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """list など dict 以外のトップレベルも認証情報として受け付けない。"""
        _cred_file(tmp_path).write_text("[]", encoding="utf-8")

        assert canvasser.load_credentials(tmp_path) is None
        assert "認証情報を無視" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "payload",
        [
            {"bnid_password": "pw", "saved_at": "2026-07-05T00:00:00+09:00"},
            {"bnid_email": "u@e", "saved_at": "2026-07-05T00:00:00+09:00"},
            {"bnid_email": "", "bnid_password": "pw", "saved_at": ""},
            {"bnid_email": "u@e", "bnid_password": "", "saved_at": ""},
            {
                "bnid_email": 123,
                "bnid_password": "pw",
                "saved_at": "2026-07-05T00:00:00+09:00",
            },
        ],
    )
    def test_必須フィールド欠落や不正型はNoneを返す(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        payload: dict[str, object],
    ) -> None:
        """email / password 欠落・空文字・型違いは認証情報として使えない。"""
        _cred_file(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

        assert canvasser.load_credentials(tmp_path) is None
        assert "認証情報を無視" in capsys.readouterr().err

    def test_不正なfailure_countはゼロに丸める(self, tmp_path: Path) -> None:
        """手改変で failure_count が文字列でも 0 として読み込む。"""
        _cred_file(tmp_path).write_text(
            json.dumps({
                "bnid_email": "u@e",
                "bnid_password": "pw",
                "saved_at": "2026-07-05T00:00:00+09:00",
                "failure_count": "many",
                "disabled_until": None,
            }),
            encoding="utf-8",
        )

        got = canvasser.load_credentials(tmp_path)

        assert got is not None
        assert got.failure_count == 0
        assert got.disabled_until is None

    def test_bool_failure_countはゼロに丸める(self, tmp_path: Path) -> None:
        """bool は int の subclass だが failure_count としては拒否する。"""
        _cred_file(tmp_path).write_text(
            json.dumps({
                "bnid_email": "u@e",
                "bnid_password": "pw",
                "saved_at": "2026-07-05T00:00:00+09:00",
                "failure_count": True,
                "disabled_until": None,
            }),
            encoding="utf-8",
        )

        got = canvasser.load_credentials(tmp_path)

        assert got is not None
        assert got.failure_count == 0

    def test_disabled_untilの非文字列はNoneに丸める(self, tmp_path: Path) -> None:
        """disabled_until が数値等でも None に丸めて読み込む。"""
        _cred_file(tmp_path).write_text(
            json.dumps({
                "bnid_email": "u@e",
                "bnid_password": "pw",
                "saved_at": "2026-07-05T00:00:00+09:00",
                "failure_count": 0,
                "disabled_until": 12345,
            }),
            encoding="utf-8",
        )

        got = canvasser.load_credentials(tmp_path)

        assert got is not None
        assert got.disabled_until is None


class TestCredentialsPermissions:
    """POSIX でファイル権限が 0o600 に絞られるか (Windows は skip)。"""

    @pytest.mark.skipif(os.name == "nt", reason="POSIX 専用")
    def test_保存後のファイル権限は0o600(self, tmp_path: Path) -> None:
        """save_credentials 後に chmod 600 が効いていること。"""
        canvasser.save_credentials(tmp_path, _sample())

        mode = stat.S_IMODE(_cred_file(tmp_path).stat().st_mode)
        assert mode == 0o600


class TestPersistLoginInitCredentials:
    """persist_login_init_credentials の対話フローを stdin モックで検証する。"""

    def test_対話入力を受けてpending_credentialsを保存する(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """input と getpass の戻り値が credentials.json.pending に一時保存される。

        active credentials.json は上書きされず、pending として保存される。実ログイン
        検証成功時に `run_login_init_flow` が active に昇格させる。
        """
        monkeypatch.setattr("builtins.input", lambda _prompt="": "user@example.com")
        monkeypatch.setattr(canvasser.getpass, "getpass", lambda _prompt="": "hunter2")

        canvasser.persist_login_init_credentials(tmp_path)

        # active は作られていない (pending のみ)
        assert canvasser.load_credentials(tmp_path) is None
        pending = canvasser.load_pending_credentials(tmp_path)
        assert pending is not None
        assert pending.bnid_email == "user@example.com"
        assert pending.bnid_password == "hunter2"
        assert pending.failure_count == 0
        assert pending.disabled_until is None
        assert "認証情報を" in capsys.readouterr().err

    def test_既存activeは_persist_login_init_で温存される(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """login-init の対話保存では既存 active は書き換わらない (pending のみ)。"""
        monkeypatch.setattr("builtins.input", lambda _prompt="": "new@example.com")
        monkeypatch.setattr(canvasser.getpass, "getpass", lambda _prompt="": "new-pw")
        canvasser.save_credentials(
            tmp_path,
            Credentials(
                bnid_email="old@example.com",
                bnid_password="old-pw",
                saved_at="2026-07-04T00:00:00+09:00",
            ),
        )

        canvasser.persist_login_init_credentials(tmp_path)

        active = canvasser.load_credentials(tmp_path)
        assert active is not None
        assert active.bnid_email == "old@example.com"  # 既存 active 温存
        pending = canvasser.load_pending_credentials(tmp_path)
        assert pending is not None
        assert pending.bnid_email == "new@example.com"

    def test_メール空文字はUserInputError(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空文字メールは保存前に UserInputError で拒否する。"""
        monkeypatch.setattr("builtins.input", lambda _prompt="": "")
        monkeypatch.setattr(canvasser.getpass, "getpass", lambda _prompt="": "hunter2")

        with pytest.raises(canvasser.UserInputError, match="メールアドレスが空"):
            canvasser.persist_login_init_credentials(tmp_path)

        assert not _cred_file(tmp_path).exists()
        assert not (tmp_path / "credentials.json.pending").exists()

    def test_パスワード空文字はUserInputError(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """空文字パスワードも保存前に UserInputError で拒否する。"""
        monkeypatch.setattr("builtins.input", lambda _prompt="": "user@example.com")
        monkeypatch.setattr(canvasser.getpass, "getpass", lambda _prompt="": "")

        with pytest.raises(canvasser.UserInputError, match="パスワードが空"):
            canvasser.persist_login_init_credentials(tmp_path)

        assert not _cred_file(tmp_path).exists()
        assert not (tmp_path / "credentials.json.pending").exists()

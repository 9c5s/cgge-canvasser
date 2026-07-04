"""CLI 入力検証 (アカウント名・パス封じ込め・引数組み合わせ) のテスト。"""

import argparse
import subprocess
from typing import TYPE_CHECKING

import pytest

import canvasser
from canvasser import UserInputError

if TYPE_CHECKING:
    from pathlib import Path


class TestValidateAccountName:
    """_validate_account_name の許可・拒否条件。"""

    @pytest.mark.parametrize(
        "name",
        [
            "main",
            "sub-01",
            "user.name",
            "a",
            "A" * 64,
        ],
    )
    def test_安全な名前は通る(self, name: str) -> None:
        """英数字と '_' '-' '.' のみ 64 文字以内は許可される。"""
        canvasser._validate_account_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "A" * 65,
            "日本語",
            "a b",
            "a/b",
            "a\\b",
            "../x",
        ],
    )
    def test_危険な名前はUserInputError(self, name: str) -> None:
        """空・長すぎ・非 ASCII・空白・パス区切りは拒否される。"""
        with pytest.raises(UserInputError):
            canvasser._validate_account_name(name)

    @pytest.mark.parametrize("name", [".", ".."])
    def test_ドット単体はパスとして拒否する(self, name: str) -> None:
        """正規表現を通過する '.' と '..' も追加防御で弾く。"""
        with pytest.raises(UserInputError, match="パスとして危険"):
            canvasser._validate_account_name(name)


class TestEnsureWithin:
    """_ensure_within のパス封じ込め。"""

    def test_子孫パスは通る(self, tmp_path: Path) -> None:
        """base 配下のパスは検証を通過する。"""
        canvasser._ensure_within(tmp_path, tmp_path / "child" / "grand")

    def test_外に逃げるパスはUserInputError(self, tmp_path: Path) -> None:
        """base の親を指すパスは拒否される。"""
        with pytest.raises(UserInputError, match="外に逃げて"):
            canvasser._ensure_within(tmp_path, tmp_path.parent)


class TestResolveProfiles:
    """resolve_profiles の一覧決定。"""

    def test_account指定は単一プロファイルを返す(self, tmp_path: Path) -> None:
        """--account 指定時はそのサブディレクトリ 1 件に固定される。"""
        got = canvasser.resolve_profiles(tmp_path, "alice")

        assert got == [("alice", (tmp_path / "alice").resolve())]

    def test_未指定は配下のディレクトリを列挙する(self, tmp_path: Path) -> None:
        """profiles_dir 配下のサブディレクトリをソート順で全列挙する。"""
        (tmp_path / "bob").mkdir()
        (tmp_path / "alice").mkdir()
        (tmp_path / "not_a_dir.txt").write_text("x", encoding="utf-8")

        got = canvasser.resolve_profiles(tmp_path, None)

        assert [name for name, _ in got] == ["alice", "bob"]

    def test_命名規則違反のディレクトリはskipする(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """空白入りなど規則外の名前は警告してリストから除外する。"""
        (tmp_path / "good").mkdir()
        (tmp_path / "bad name").mkdir()

        got = canvasser.resolve_profiles(tmp_path, None)

        assert [name for name, _ in got] == ["good"]
        assert "命名規則に合致しない" in capsys.readouterr().err

    def test_profiles_dirが無ければ空リスト(self, tmp_path: Path) -> None:
        """未作成の profiles_dir では空リストを返す。"""
        assert canvasser.resolve_profiles(tmp_path / "missing", None) == []

    def test_不正なaccount名はUserInputError(self, tmp_path: Path) -> None:
        """--account の値も同じ命名規則で検証される。"""
        with pytest.raises(UserInputError):
            canvasser.resolve_profiles(tmp_path, "../escape")


def _which_git(_cmd: str) -> str:
    """shutil.which の差し替え。常に固定の git パスを返す。"""
    return "/fake/git"


def _which_none(_cmd: str) -> None:
    """shutil.which の差し替え。git 不在を表す None を返す。"""
    return


class TestProfilesDirIsGitignored:
    """_profiles_dir_is_gitignored の判定分岐。

    Cookie 誤コミット防止の安全弁なので、git の exit code 解釈・git 不在時の
    デフォルト拒否・パス正規化を subprocess をモックして固定する。
    """

    def _patch_git(
        self,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        captured: dict[str, list[str]] | None = None,
    ) -> None:
        """git 実体の解決と check-ignore の exit code を差し替える。"""
        monkeypatch.setattr(canvasser.shutil, "which", _which_git)

        def fake_run(
            cmd: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            if captured is not None:
                captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode)

        monkeypatch.setattr(canvasser.subprocess, "run", fake_run)

    def test_gitが見つからなければFalse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """git 不在では誤コミット経路を判定できないため拒否側に倒す。"""
        monkeypatch.setattr(canvasser.shutil, "which", _which_none)

        assert canvasser._profiles_dir_is_gitignored(tmp_path / "profiles") is False

    @pytest.mark.parametrize(
        "returncode, expected",
        [
            (0, True),
            (1, False),
            (128, False),
        ],
    )
    def test_check_ignoreのexit_codeを解釈する(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        expected: bool,
    ) -> None:
        """0=ignored のみ True、1=not ignored と 128=repo 外は False になる。"""
        self._patch_git(monkeypatch, returncode)

        assert canvasser._profiles_dir_is_gitignored(tmp_path / "profiles") is expected

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("git 消失"),
            subprocess.TimeoutExpired(cmd="git", timeout=10),
            OSError("実行不能"),
        ],
    )
    def test_subprocess例外はFalseに丸める(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
    ) -> None:
        """check-ignore の実行自体が失敗しても例外にせず拒否側に倒す。"""
        monkeypatch.setattr(canvasser.shutil, "which", _which_git)

        def raise_exc(_cmd: list[str], **_kwargs: object) -> object:
            raise exc

        monkeypatch.setattr(canvasser.subprocess, "run", raise_exc)

        assert canvasser._profiles_dir_is_gitignored(tmp_path / "profiles") is False

    def test_パス引数はスラッシュ区切りの末尾スラッシュ付きになる(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows パス区切りを正規化し、ディレクトリ限定パターンに合う形で渡す。"""
        captured: dict[str, list[str]] = {}
        self._patch_git(monkeypatch, 0, captured)

        canvasser._profiles_dir_is_gitignored(tmp_path / "profiles")

        cmd = captured["cmd"]
        path_arg = cmd[-1]
        assert cmd[1:3] == ["check-ignore", "--quiet"]
        assert "--" in cmd
        assert path_arg.endswith("/")
        assert not path_arg.endswith("//")
        assert "\\" not in path_arg


def _thresholds(
    daily_budget: int = 0,
    consecutive_failure_limit: int = 1,
    out_of_range_limit: int = 3,
) -> argparse.Namespace:
    """_validate_thresholds 用の Namespace を組み立てる。"""
    return argparse.Namespace(
        daily_budget=daily_budget,
        consecutive_failure_limit=consecutive_failure_limit,
        out_of_range_limit=out_of_range_limit,
    )


class TestValidateThresholds:
    """_validate_thresholds の下限チェック。"""

    def test_既定値は通る(self) -> None:
        """デフォルト相当 (0, 1, 3) は検証を通過する。"""
        canvasser._validate_thresholds(_thresholds())

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"daily_budget": -1}, "--daily-budget"),
            ({"consecutive_failure_limit": 0}, "--consecutive-failure-limit"),
            ({"out_of_range_limit": 0}, "--out-of-range-limit"),
        ],
    )
    def test_下限未満はUserInputError(
        self, kwargs: dict[str, int], message: str
    ) -> None:
        """負の budget と 1 未満の閾値はフラグ名入りのメッセージで拒否する。"""
        with pytest.raises(UserInputError, match=message):
            canvasser._validate_thresholds(_thresholds(**kwargs))


class TestBuildParser:
    """_build_parser のサブコマンド構成。"""

    def test_missionの既定値はドライラン(self) -> None:
        """mission 単独では実 POST ゲートが閉じている。"""
        args = canvasser._build_parser().parse_args(["mission"])

        assert args.command == "mission"
        assert args.account is None
        assert args.execute is False
        assert args.profiles_dir == "./profiles"

    def test_missionにチェックイン用の安全弁は無い(self) -> None:
        """--daily-budget はチェックイン専用オプションで mission には載せない。"""
        with pytest.raises(SystemExit):
            canvasser._build_parser().parse_args(["mission", "--daily-budget", "3"])

    def test_checkinの既定値はドライラン(self) -> None:
        """checkin 単独では実 POST ゲートが閉じ、安全弁は既定値になる。"""
        args = canvasser._build_parser().parse_args(["checkin"])

        assert args.command == "checkin"
        assert args.account is None
        assert args.execute is False
        assert args.daily_budget == 0
        assert args.consecutive_failure_limit == 1
        assert args.out_of_range_limit == 3
        assert args.profiles_dir == "./profiles"

    def test_checkinのexecuteと安全弁を指定できる(self) -> None:
        """checkin は --execute とチェックイン専用の安全弁を受け取る。"""
        args = canvasser._build_parser().parse_args([
            "checkin",
            "--execute",
            "--daily-budget",
            "3",
        ])

        assert args.execute is True
        assert args.daily_budget == 3

    def test_missionは既定でauto_relogin有効(self) -> None:
        """--no-auto-relogin 未指定時は auto_relogin フラグが False。"""
        args = canvasser._build_parser().parse_args(["mission"])

        assert args.no_auto_relogin is False

    def test_missionに_no_auto_relogin_フラグを指定できる(self) -> None:
        """--no-auto-relogin で auto-relogin をオプトアウトできる。"""
        args = canvasser._build_parser().parse_args(["mission", "--no-auto-relogin"])

        assert args.no_auto_relogin is True

    def test_checkinに_no_auto_relogin_フラグを指定できる(self) -> None:
        """checkin にも --no-auto-relogin フラグが載る (collect 親パーサ経由)。"""
        args = canvasser._build_parser().parse_args(["checkin", "--no-auto-relogin"])

        assert args.no_auto_relogin is True

    def test_loginにno_auto_reloginフラグは無い(self) -> None:
        """login サブコマンドは collect 親パーサを持たないためフラグを拒否する。"""
        with pytest.raises(SystemExit):
            canvasser._build_parser().parse_args([
                "login",
                "--account",
                "main",
                "--no-auto-relogin",
            ])

    def test_loginはaccount必須(self) -> None:
        """login サブコマンドは --account なしでは通らない。"""
        with pytest.raises(SystemExit):
            canvasser._build_parser().parse_args(["login"])

    def test_loginコマンドを解釈する(self) -> None:
        """login --account NAME で command と account が入る。"""
        args = canvasser._build_parser().parse_args(["login", "--account", "main"])

        assert args.command == "login"
        assert args.account == "main"

    def test_login_initはaccount必須(self) -> None:
        """login-init サブコマンドも --account なしでは通らない。"""
        with pytest.raises(SystemExit):
            canvasser._build_parser().parse_args(["login-init"])

    def test_login_initコマンドを解釈する(self) -> None:
        """login-init --account NAME で command と account が入る。"""
        args = canvasser._build_parser().parse_args([
            "login-init",
            "--account",
            "main",
        ])

        assert args.command == "login-init"
        assert args.account == "main"

    def test_mark_completedはslug位置引数を受け取る(self) -> None:
        """slug は複数の位置引数として受け取る (カンマ区切りではない)。"""
        args = canvasser._build_parser().parse_args([
            "mark-completed",
            "--account",
            "syota",
            "cg_vote2026_17",
            "cg_vote2026_19",
        ])

        assert args.command == "mark-completed"
        assert args.slugs == ["cg_vote2026_17", "cg_vote2026_19"]

    @pytest.mark.parametrize(
        "argv",
        [
            ["mark-completed", "cg_vote2026_17"],
            ["mark-completed", "--account", "syota"],
        ],
    )
    def test_mark_completedはaccountとslugが必須(self, argv: list[str]) -> None:
        """--account と slug のどちらが欠けても usage エラーになる。"""
        with pytest.raises(SystemExit):
            canvasser._build_parser().parse_args(argv)

    def test_サブコマンド無しはSystemExit(self) -> None:
        """サブコマンド必須のため引数なしは usage エラーになる。"""
        with pytest.raises(SystemExit):
            canvasser._build_parser().parse_args([])


class TestBuildRunOptions:
    """_build_run_options の RunOptions への写像。"""

    def test_loginコマンドはlogin_modeのみ有効(self) -> None:
        """login ではタスクと実行ゲートがすべて閉じる。"""
        args = canvasser._build_parser().parse_args(["login", "--account", "main"])

        options = canvasser._build_run_options(args)

        assert options.login_mode is True
        assert options.login_init_mode is False
        assert options.run_mission is False
        assert options.run_checkin is False
        assert options.execute is False

    def test_login_initコマンドはlogin_init_modeのみ有効(self) -> None:
        """login-init ではタスクと実行ゲートがすべて閉じ、login_init_mode だけ立つ。"""
        args = canvasser._build_parser().parse_args([
            "login-init",
            "--account",
            "main",
        ])

        options = canvasser._build_run_options(args)

        assert options.login_mode is False
        assert options.login_init_mode is True
        assert options.run_mission is False
        assert options.run_checkin is False
        assert options.execute is False

    def test_missionドライラン(self) -> None:
        """mission 単独では mission のみ選択され、ゲートは閉じたまま。"""
        args = canvasser._build_parser().parse_args(["mission"])

        options = canvasser._build_run_options(args)

        assert options.login_mode is False
        assert options.run_mission is True
        assert options.run_checkin is False
        assert options.execute is False
        assert options.auto_relogin is True

    def test_no_auto_reloginでauto_reloginが無効化される(self) -> None:
        """--no-auto-relogin で RunOptions.auto_relogin が False になる。"""
        args = canvasser._build_parser().parse_args(["mission", "--no-auto-relogin"])

        options = canvasser._build_run_options(args)

        assert options.auto_relogin is False

    def test_checkin_no_auto_reloginでauto_reloginが無効化される(self) -> None:
        """checkin でも --no-auto-relogin が RunOptions に伝搬する。"""
        args = canvasser._build_parser().parse_args(["checkin", "--no-auto-relogin"])

        options = canvasser._build_run_options(args)

        assert options.auto_relogin is False

    def test_mission本番(self) -> None:
        """mission --execute で実行ゲートが開く。"""
        args = canvasser._build_parser().parse_args(["mission", "--execute"])

        options = canvasser._build_run_options(args)

        assert options.run_mission is True
        assert options.run_checkin is False
        assert options.execute is True

    def test_checkin本番はmissionを含まない(self) -> None:
        """checkin --execute では mission 側は動かない。"""
        args = canvasser._build_parser().parse_args(["checkin", "--execute"])

        options = canvasser._build_run_options(args)

        assert options.run_mission is False
        assert options.run_checkin is True
        assert options.execute is True

    def test_checkinの閾値が引き継がれる(self) -> None:
        """checkin の安全弁引数が RunOptions へそのまま渡る。"""
        args = canvasser._build_parser().parse_args([
            "checkin",
            "--daily-budget",
            "3",
            "--consecutive-failure-limit",
            "2",
            "--out-of-range-limit",
            "5",
        ])

        options = canvasser._build_run_options(args)

        assert options.daily_budget == 3
        assert options.consecutive_failure_limit == 2
        assert options.out_of_range_limit == 5

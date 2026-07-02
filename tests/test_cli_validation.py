"""CLI 入力検証 (アカウント名・パス封じ込め・引数組み合わせ) のテスト。"""

from __future__ import annotations

import argparse
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


def _thresholds(
    daily_budget: int = 0,
    consecutive_failure_limit: int = 1,
    max_out_of_range: int = 3,
) -> argparse.Namespace:
    """_validate_thresholds 用の Namespace を組み立てる。"""
    return argparse.Namespace(
        daily_budget=daily_budget,
        consecutive_failure_limit=consecutive_failure_limit,
        max_out_of_range=max_out_of_range,
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
            ({"max_out_of_range": 0}, "--max-out-of-range"),
        ],
    )
    def test_下限未満はUserInputError(
        self, kwargs: dict[str, int], message: str
    ) -> None:
        """負の budget と 1 未満の閾値はフラグ名入りのメッセージで拒否する。"""
        with pytest.raises(UserInputError, match=message):
            canvasser._validate_thresholds(_thresholds(**kwargs))


def _mode_flags(
    *,
    login: bool = False,
    account: str | None = None,
    no_mission: bool = False,
    checkin: bool = False,
    execute_mission: bool = False,
    execute_checkin: bool = False,
) -> argparse.Namespace:
    """_validate_mode_flags 用の Namespace を組み立てる。"""
    return argparse.Namespace(
        login=login,
        account=account,
        no_mission=no_mission,
        checkin=checkin,
        execute_mission=execute_mission,
        execute_checkin=execute_checkin,
    )


class TestValidateModeFlags:
    """_validate_mode_flags の組み合わせチェック。"""

    def test_既定の組み合わせは通る(self) -> None:
        """フラグ未指定 (完全ドライラン) は検証を通過する。"""
        canvasser._validate_mode_flags(_mode_flags())

    def test_本番フルセットも通る(self) -> None:
        """--checkin --execute-mission --execute-checkin は妥当な組み合わせ。"""
        canvasser._validate_mode_flags(
            _mode_flags(checkin=True, execute_mission=True, execute_checkin=True)
        )

    @pytest.mark.parametrize(
        "flags, message",
        [
            (_mode_flags(login=True), "--login"),
            (_mode_flags(no_mission=True), "--no-mission"),
            (
                _mode_flags(no_mission=True, checkin=True, execute_mission=True),
                "--execute-mission",
            ),
            (_mode_flags(execute_checkin=True), "--execute-checkin"),
        ],
    )
    def test_不整合な組み合わせはUserInputError(
        self, flags: argparse.Namespace, message: str
    ) -> None:
        """単独指定できないゲート系フラグは対象フラグ名入りで拒否する。"""
        with pytest.raises(UserInputError, match=message):
            canvasser._validate_mode_flags(flags)


class TestBuildParser:
    """_build_parser の既定値。"""

    def test_引数なしの既定値は完全ドライラン(self) -> None:
        """フラグ未指定では実 POST ゲートがすべて閉じている。"""
        args = canvasser._build_parser().parse_args([])

        assert args.login is False
        assert args.checkin is False
        assert args.execute_mission is False
        assert args.execute_checkin is False
        assert args.daily_budget == 0
        assert args.consecutive_failure_limit == 1
        assert args.max_out_of_range == 3
        assert args.profiles_dir == "./profiles"

"""state.json 永続化 (保存・読込・resume・手動登録) のテスト。

ファイル I/O を伴うため tmp_path を使う Medium テストだが、ネットワークや
外部サービスには依存しない。
"""

from datetime import datetime
from typing import TYPE_CHECKING

import pytest

import canvasser
from canvasser import JST, StateFileCorruptedError, UserInputError

if TYPE_CHECKING:
    from pathlib import Path


def _state_file(profile_dir: Path) -> Path:
    """profile_dir 配下の state ファイルパスを返す。"""
    return profile_dir / "canvasser_state.json"


def _spot(slug: str = "cg_vote2026_7") -> canvasser.Spot:
    """state 更新テスト用のスポットを組み立てる。"""
    return canvasser.Spot(slug=slug, name="テストスポット", lat=35.0, lng=135.0)


class TestLoadAccountState:
    """load_account_state の読込と破損ハンドリング。"""

    def test_ファイルが無ければ空dict(self, tmp_path: Path) -> None:
        """state 未作成のプロファイルは strict でも空 dict を返す。"""
        assert canvasser.load_account_state(tmp_path, strict=True) == {}

    def test_保存した内容をそのまま読み戻せる(self, tmp_path: Path) -> None:
        """save → load のラウンドトリップが成立する。"""
        state = {"completed_spots": ["cg_vote2026_1"]}

        canvasser.save_account_state(tmp_path, state)

        assert canvasser.load_account_state(tmp_path) == state

    def test_壊れたJSONはstrictで例外(self, tmp_path: Path) -> None:
        """JSON パース不能は strict で対象ファイル名入りの例外になる。"""
        _state_file(tmp_path).write_text("{{{", encoding="utf-8")

        with pytest.raises(StateFileCorruptedError, match="canvasser_state"):
            canvasser.load_account_state(tmp_path, strict=True)

    def test_壊れたJSONは非strictで空dict(self, tmp_path: Path) -> None:
        """dry-run 相当の非 strict では破損を空 dict に丸める。"""
        _state_file(tmp_path).write_text("{{{", encoding="utf-8")

        assert canvasser.load_account_state(tmp_path, strict=False) == {}

    def test_トップレベル非dictは非strictで空dict(self, tmp_path: Path) -> None:
        """list などのトップレベルも非 strict では空 dict に丸める。"""
        _state_file(tmp_path).write_text("[]", encoding="utf-8")

        assert canvasser.load_account_state(tmp_path, strict=False) == {}

    def test_トップレベル非dictはstrictで例外(self, tmp_path: Path) -> None:
        """list など dict 以外のトップレベルは strict で拒否する。"""
        _state_file(tmp_path).write_text("[]", encoding="utf-8")

        with pytest.raises(StateFileCorruptedError, match="dict でない"):
            canvasser.load_account_state(tmp_path, strict=True)

    @pytest.mark.parametrize(
        "content, message",
        [
            ('{"completed_spots": "x"}', "list ではなく"),
            ('{"completed_spots": ["evil/../path"]}', "不正な slug"),
            ('{"last_checkin": []}', "dict でない"),
            ('{"last_checkin": {"spot_slug": 1}}', "型が不正"),
            ('{"last_checkin": {"spot_slug": "bad_slug"}}', "形式でない"),
            (
                '{"last_checkin": {"virtual_completed_at": "not-a-date"}}',
                "ISO8601 として不正",
            ),
            # bool は int の subclass なので、素の isinstance では通ってしまう
            ('{"last_checkin": {"schema_version": true}}', "型が不正"),
            # JSON 標準外の NaN/Infinity は parse_constant 経路で拒否する
            ('{"last_checkin": {"location_latitude": NaN}}', "非有限値"),
            ('{"last_checkin": {"location_longitude": Infinity}}', "非有限値"),
            # Spot.from_api と同じ緯度経度範囲を state 側にも適用する
            ('{"last_checkin": {"location_latitude": 91}}', "latitude 期待"),
            ('{"last_checkin": {"location_latitude": -91}}', "latitude 期待"),
            ('{"last_checkin": {"location_longitude": 181}}', "longitude 期待"),
            ('{"last_checkin": {"location_longitude": -181}}', "longitude 期待"),
        ],
    )
    def test_スキーマ違反はstrictで例外(
        self, tmp_path: Path, content: str, message: str
    ) -> None:
        """型・slug 形式・時刻形式の違反は理由付きの例外になる。"""
        _state_file(tmp_path).write_text(content, encoding="utf-8")

        with pytest.raises(StateFileCorruptedError, match=message):
            canvasser.load_account_state(tmp_path, strict=True)

    def test_NaNリテラルは非strictでも空dictに丸める(self, tmp_path: Path) -> None:
        """parse_constant による NaN 拒否は非 strict でも作用し空 dict になる。"""
        _state_file(tmp_path).write_text(
            '{"last_checkin": {"location_latitude": NaN}}', encoding="utf-8"
        )

        assert canvasser.load_account_state(tmp_path, strict=False) == {}


class TestSaveAccountState:
    """save_account_state の atomic 書き込み。"""

    def test_一時ファイルを残さない(self, tmp_path: Path) -> None:
        """書き込み成功後は state ファイル以外の残骸が無い。"""
        canvasser.save_account_state(tmp_path, {"completed_spots": []})

        names = [p.name for p in tmp_path.iterdir()]
        assert names == ["canvasser_state.json"]

    def test_ディレクトリが無ければ作成する(self, tmp_path: Path) -> None:
        """profile_dir が未作成でも mkdir して保存する。"""
        target = tmp_path / "new" / "profile"

        canvasser.save_account_state(target, {})

        assert _state_file(target).exists()


class TestUpdateCheckinState:
    """update_checkin_state の記録内容。"""

    def test_成功記録はschema_version2で書かれる(self, tmp_path: Path) -> None:
        """last_checkin に位置・時刻・schema_version=2 が記録される。"""
        vnow = datetime(2026, 7, 3, 12, 30, tzinfo=JST)

        canvasser.update_checkin_state(tmp_path, _spot(), vnow)

        state = canvasser.load_account_state(tmp_path)
        last = state["last_checkin"]
        assert last["schema_version"] == 2
        assert last["spot_slug"] == "cg_vote2026_7"
        assert last["location_latitude"] == 35.0
        assert last["location_longitude"] == 135.0
        assert last["virtual_completed_at"] == vnow.isoformat()

    def test_mark_completedで完了リストに追加する(self, tmp_path: Path) -> None:
        """既定 (mark_completed=True) では completed_spots に slug が入る。"""
        vnow = datetime(2026, 7, 3, 12, 30, tzinfo=JST)

        canvasser.update_checkin_state(tmp_path, _spot(), vnow)

        state = canvasser.load_account_state(tmp_path)
        assert state["completed_spots"] == ["cg_vote2026_7"]

    def test_mark_completed_Falseは完了リストに触れない(self, tmp_path: Path) -> None:
        """mark_completed=False では completed_spots を追加しない。"""
        vnow = datetime(2026, 7, 3, 12, 30, tzinfo=JST)

        canvasser.update_checkin_state(tmp_path, _spot(), vnow, mark_completed=False)

        state = canvasser.load_account_state(tmp_path)
        assert "completed_spots" not in state


class TestResumeContext:
    """resume_context の復元条件。"""

    def test_schema_version2の記録から位置と時刻を復元する(
        self, tmp_path: Path
    ) -> None:
        """update_checkin_state 直後の state は resume に使える。"""
        vnow = datetime(2026, 7, 3, 12, 30, tzinfo=JST)
        canvasser.update_checkin_state(tmp_path, _spot(), vnow)

        lat, lng, resume_at, completed = canvasser.resume_context(tmp_path)

        assert (lat, lng) == (35.0, 135.0)
        assert resume_at == vnow
        assert completed == {"cg_vote2026_7"}

    def test_schema_version不一致の記録はresumeに使わない(self, tmp_path: Path) -> None:
        """旧 schema (dry-run 由来の疑い) は位置・時刻とも無視する。"""
        canvasser.save_account_state(
            tmp_path,
            {
                "last_checkin": {
                    "schema_version": 1,
                    "spot_slug": "cg_vote2026_1",
                    "location_latitude": 35.0,
                    "location_longitude": 135.0,
                    "virtual_completed_at": "2026-07-03T12:30:00+09:00",
                },
                "completed_spots": ["cg_vote2026_2"],
            },
        )

        lat, lng, resume_at, completed = canvasser.resume_context(tmp_path)

        assert (lat, lng, resume_at) == (None, None, None)
        assert completed == {"cg_vote2026_2"}

    def test_不正な時刻文字列はresume_atだけ落とす(self, tmp_path: Path) -> None:
        """virtual_completed_at が読めなくても位置情報は復元する。"""
        canvasser.save_account_state(
            tmp_path,
            {
                "last_checkin": {
                    "schema_version": 2,
                    "location_latitude": 35.0,
                    "location_longitude": 135.0,
                    "virtual_completed_at": "not-a-date",
                }
            },
        )

        lat, lng, resume_at, completed = canvasser.resume_context(tmp_path)

        assert (lat, lng) == (35.0, 135.0)
        assert resume_at is None
        assert completed == set()

    def test_naiveな時刻文字列はJSTとして復元する(self, tmp_path: Path) -> None:
        """virtual_completed_at に tz が無ければ JST を付与して読み込む。"""
        canvasser.save_account_state(
            tmp_path,
            {
                "last_checkin": {
                    "schema_version": 2,
                    "location_latitude": 35.0,
                    "location_longitude": 135.0,
                    "virtual_completed_at": "2026-07-03T12:30:00",
                }
            },
        )

        _lat, _lng, resume_at, _completed = canvasser.resume_context(tmp_path)

        assert resume_at == datetime(2026, 7, 3, 12, 30, tzinfo=JST)

    def test_数値でない座標は非strictでNoneに丸める(self, tmp_path: Path) -> None:
        """手改変で座標が文字列でも ValueError にせず resume 情報なしに落とす。"""
        canvasser.save_account_state(
            tmp_path,
            {
                "last_checkin": {
                    "schema_version": 2,
                    "location_latitude": "三五度",
                    "location_longitude": 135.0,
                    "virtual_completed_at": "2026-07-03T12:30:00+09:00",
                }
            },
        )

        lat, lng, _resume_at, _completed = canvasser.resume_context(tmp_path)

        assert lat is None
        assert lng == 135.0

    def test_範囲外座標は非strictでNoneに丸める(self, tmp_path: Path) -> None:
        """緯度 91 や経度 181 は範囲外で resume 起点として採用しない。"""
        canvasser.save_account_state(
            tmp_path,
            {
                "last_checkin": {
                    "schema_version": 2,
                    "location_latitude": 91.0,
                    "location_longitude": 181.0,
                    "virtual_completed_at": "2026-07-03T12:30:00+09:00",
                }
            },
        )

        lat, lng, _resume_at, _completed = canvasser.resume_context(tmp_path)

        assert lat is None
        assert lng is None

    def test_stateが無ければ全て空(self, tmp_path: Path) -> None:
        """初回実行相当では位置・時刻・完了集合すべて空になる。"""
        got = canvasser.resume_context(tmp_path)
        assert got == (None, None, None, set())

    def test_strictでは破損stateの例外が伝播する(self, tmp_path: Path) -> None:
        """本番経路相当の strict=True では破損を丸めず例外を上げる。"""
        (tmp_path / "canvasser_state.json").write_text("{{{", encoding="utf-8")

        with pytest.raises(StateFileCorruptedError, match="canvasser_state"):
            canvasser.resume_context(tmp_path, strict=True)


class TestMarkSpotsCompleted:
    """mark_spots_completed の手動登録。"""

    def test_slugを辞書順で追記する(self, tmp_path: Path) -> None:
        """既存の completed_spots とマージして辞書順 (文字列順) で保存する。

        ソートは差分の読みやすさのための決定的順序であり、数値としての昇順
        (3 → 5 → 10) は仕様ではない。
        """
        canvasser.save_account_state(tmp_path, {"completed_spots": ["cg_vote2026_5"]})

        canvasser.mark_spots_completed(tmp_path, ["cg_vote2026_3", "cg_vote2026_10"])

        state = canvasser.load_account_state(tmp_path)
        assert state["completed_spots"] == [
            "cg_vote2026_10",
            "cg_vote2026_3",
            "cg_vote2026_5",
        ]

    def test_不正なslugはUserInputError(self, tmp_path: Path) -> None:
        """slug 形式違反は state を書かずに UserInputError で拒否する。"""
        with pytest.raises(UserInputError, match="不正な spot_slug"):
            canvasser.mark_spots_completed(tmp_path, ["../../etc/passwd"])

        assert not _state_file(tmp_path).exists()

    def test_破損stateには追記しない(self, tmp_path: Path) -> None:
        """既存 state が破損していれば上書きせず例外を伝播する。"""
        _state_file(tmp_path).write_text("{{{", encoding="utf-8")

        with pytest.raises(StateFileCorruptedError, match="canvasser_state"):
            canvasser.mark_spots_completed(tmp_path, ["cg_vote2026_1"])

        assert _state_file(tmp_path).read_text(encoding="utf-8") == "{{{"


class TestSyncCompletedSpots:
    """sync_completed_spots のサーバ完了状態マージ。"""

    def test_サーバ側新規は追加され差分が返る(self, tmp_path: Path) -> None:
        """サーバ済み・ローカル未登録の slug が completed_spots に足される。"""
        canvasser.save_account_state(tmp_path, {"completed_spots": ["cg_vote2026_1"]})

        added, local_only = canvasser.sync_completed_spots(
            tmp_path, {"cg_vote2026_1", "cg_vote2026_2"}
        )

        assert added == ["cg_vote2026_2"]
        assert local_only == []
        state = canvasser.load_account_state(tmp_path)
        assert state["completed_spots"] == ["cg_vote2026_1", "cg_vote2026_2"]

    def test_ローカル済みサーバ未確認は削除せず警告として返す(
        self, tmp_path: Path
    ) -> None:
        """乖離は削除ではなく警告に留める (再 POST → 未観測 ecode 停止を避ける)。"""
        canvasser.save_account_state(
            tmp_path, {"completed_spots": ["cg_vote2026_1", "cg_vote2026_5"]}
        )

        added, local_only = canvasser.sync_completed_spots(tmp_path, {"cg_vote2026_1"})

        assert added == []
        assert local_only == ["cg_vote2026_5"]
        state = canvasser.load_account_state(tmp_path)
        assert state["completed_spots"] == ["cg_vote2026_1", "cg_vote2026_5"]

    def test_一致していれば書き込みしない(self, tmp_path: Path) -> None:
        """サーバとローカルが完全一致なら state ファイルを触らない。"""
        canvasser.save_account_state(tmp_path, {"completed_spots": ["cg_vote2026_1"]})
        before = _state_file(tmp_path).stat().st_mtime_ns

        added, local_only = canvasser.sync_completed_spots(tmp_path, {"cg_vote2026_1"})

        assert added == []
        assert local_only == []
        assert _state_file(tmp_path).stat().st_mtime_ns == before

    def test_state未作成でも新規に取り込める(self, tmp_path: Path) -> None:
        """初回同期でも空 state からサーバ済み集合を書き起こせる。"""
        added, local_only = canvasser.sync_completed_spots(
            tmp_path, {"cg_vote2026_1", "cg_vote2026_3"}
        )

        assert added == ["cg_vote2026_1", "cg_vote2026_3"]
        assert local_only == []
        state = canvasser.load_account_state(tmp_path)
        assert state["completed_spots"] == ["cg_vote2026_1", "cg_vote2026_3"]

    def test_サーバ空集合はローカルのみ警告になる(self, tmp_path: Path) -> None:
        """サーバから何も帰らない状態でもローカル済みは削除しない。"""
        canvasser.save_account_state(tmp_path, {"completed_spots": ["cg_vote2026_2"]})

        added, local_only = canvasser.sync_completed_spots(tmp_path, set())

        assert added == []
        assert local_only == ["cg_vote2026_2"]

    def test_破損stateには書き込まず例外を伝播する(self, tmp_path: Path) -> None:
        """破損 state を空 dict で上書きしない (mark_spots_completed と同じ扱い)。"""
        _state_file(tmp_path).write_text("{{{", encoding="utf-8")

        with pytest.raises(StateFileCorruptedError, match="canvasser_state"):
            canvasser.sync_completed_spots(tmp_path, {"cg_vote2026_1"})

        assert _state_file(tmp_path).read_text(encoding="utf-8") == "{{{"

    def test_不正な形式のslugは黙って除外する(self, tmp_path: Path) -> None:
        """サーバ由来の malformed slug は state に持ち込まない (境界での防御)。

        持ち込んでしまうと次回の strict load で `StateFileCorruptedError` に
        なり、以降 run 全体が状態破損扱いで止まる (fail closed)。
        """
        added, local_only = canvasser.sync_completed_spots(
            tmp_path,
            {
                "cg_vote2026_1",
                "cg_vote2026_9999999",
                "evil/../path",
                "future_format_2027_1",
            },
        )

        assert added == ["cg_vote2026_1"]
        assert local_only == []
        # strict load でも読み戻せる形で保存されている
        state = canvasser.load_account_state(tmp_path, strict=True)
        assert state["completed_spots"] == ["cg_vote2026_1"]


class TestPrintSyncSummary:
    """_print_sync_summary の表示分岐。"""

    def test_追加ありlocal_onlyなしは取り込みメッセージのみ(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """通常の同期成功時は stdout に取り込み件数、stderr は空。"""
        canvasser._print_sync_summary("main", ["cg_vote2026_1"], [])

        captured = capsys.readouterr()
        assert "サーバ済みを取り込みました (1件)" in captured.out
        assert "cg_vote2026_1" in captured.out
        assert captured.err == ""

    def test_差分なしは一致メッセージを付ける(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """added も local_only も空なら state はサーバと完全一致。"""
        canvasser._print_sync_summary("main", [], [])

        captured = capsys.readouterr()
        assert "追加なし (state はサーバと一致)" in captured.out
        assert captured.err == ""

    def test_追加なしlocal_onlyありは一致メッセージを付けない(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """乖離があるので「一致」とは表示せず、警告を stderr に出す。"""
        canvasser._print_sync_summary("main", [], ["cg_vote2026_5"])

        captured = capsys.readouterr()
        assert "追加なし" in captured.out
        assert "(state はサーバと一致)" not in captured.out
        assert "ローカル済みだがサーバ未確認 (1件)" in captured.err
        assert "cg_vote2026_5" in captured.err

    def test_追加ありlocal_onlyありは両方出力する(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """取り込み成功と警告を独立に出せる (どちらも起こりうる)。"""
        canvasser._print_sync_summary("main", ["cg_vote2026_2"], ["cg_vote2026_5"])

        captured = capsys.readouterr()
        assert "サーバ済みを取り込みました (1件)" in captured.out
        assert "cg_vote2026_2" in captured.out
        assert "ローカル済みだがサーバ未確認 (1件)" in captured.err
        assert "cg_vote2026_5" in captured.err

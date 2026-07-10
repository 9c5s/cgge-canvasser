"""state.json 永続化 (保存・読込・resume・手動登録) のテスト。

ファイル I/O を伴うため tmp_path を使う Medium テストだが、ネットワークや
外部サービスには依存しない。
"""

import json
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

import canvasser
from canvasser import JST, StateFileCorruptedError

if TYPE_CHECKING:
    from pathlib import Path


def _state_file(profile_dir: Path) -> Path:
    """profile_dir 配下の state ファイルパスを返す。"""
    return profile_dir / canvasser._STATE_FILENAME


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
        state = {
            "last_checkin": {
                "schema_version": 2,
                "spot_slug": "cg_vote2026_1",
                "spot_name": "テスト",
                "location_latitude": 35.0,
                "location_longitude": 135.0,
                "virtual_completed_at": "2026-07-03T12:30:00+09:00",
                "real_completed_at": "2026-07-03T03:30:00+00:00",
            }
        }

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
        canvasser.save_account_state(tmp_path, {})

        names = [p.name for p in tmp_path.iterdir()]
        assert names == [canvasser._STATE_FILENAME]

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

    def test_成功記録はcompleted_spotsを書き出さない(self, tmp_path: Path) -> None:
        """save 後の state に legacy な completed_spots キーが現れないこと。"""
        vnow = datetime(2026, 7, 3, 12, 30, tzinfo=JST)

        canvasser.update_checkin_state(tmp_path, _spot(), vnow)

        state = canvasser.load_account_state(tmp_path)
        assert "completed_spots" not in state

    def test_既存のcompleted_spotsキーは成功保存で消える(
        self, tmp_path: Path
    ) -> None:
        """legacy キー付きの state から成功 POST しても、保存結果から消える。

        save 側で pop していないと、load → 変更なし → save で残り続けてしまう。
        silent 無視の契約 (load 側では素通し、save 側で除外する) を保存経路でも
        担保する。
        """
        canvasser.save_account_state(
            tmp_path, {"completed_spots": ["cg_vote2026_1"]}
        )
        vnow = datetime(2026, 7, 3, 12, 30, tzinfo=JST)

        canvasser.update_checkin_state(tmp_path, _spot(), vnow)

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

        lat, lng, resume_at = canvasser.resume_context(tmp_path)

        assert (lat, lng) == (35.0, 135.0)
        assert resume_at == vnow

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
            },
        )

        got = canvasser.resume_context(tmp_path)

        assert got == (None, None, None)

    def test_不正な時刻文字列はresume_atだけ捨てる(self, tmp_path: Path) -> None:
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

        lat, lng, resume_at = canvasser.resume_context(tmp_path)

        assert (lat, lng) == (35.0, 135.0)
        assert resume_at is None

    def test_文字列でない時刻は非strictでNoneに丸める(self, tmp_path: Path) -> None:
        """手改変で int が入っても TypeError を送出せず resume_at=None に丸める。

        `resume_context` の non-strict 経路は緩い契約 (不正値 → None) を持つが、
        `contextlib.suppress(ValueError)` は `TypeError` を拾わないため、`if raw:`
        だけでは int 値が truthy 側の分岐に入り、`datetime.fromisoformat(int)` が
        送出する `TypeError` が escape する。`isinstance(str)` ガードで str 以外
        を先に除外することを担保する。
        """
        _state_file(tmp_path).write_text(
            json.dumps(
                {
                    "last_checkin": {
                        "schema_version": 2,
                        "location_latitude": 35.0,
                        "location_longitude": 135.0,
                        "virtual_completed_at": 12345,
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        lat, lng, resume_at = canvasser.resume_context(tmp_path)

        assert (lat, lng) == (35.0, 135.0)
        assert resume_at is None

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

        _lat, _lng, resume_at = canvasser.resume_context(tmp_path)

        assert resume_at == datetime(2026, 7, 3, 12, 30, tzinfo=JST)

    def test_数値でない座標は非strictでNoneに丸める(self, tmp_path: Path) -> None:
        """手改変で座標が文字列でも ValueError にせず resume 情報なしとして扱う。"""
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

        lat, lng, _resume_at = canvasser.resume_context(tmp_path)

        assert lat is None
        assert lng == 135.0

    def test_巨大intは非strictでNoneに丸める(self, tmp_path: Path) -> None:
        """`10**400` 相当の巨大 int は `float(v)` で OverflowError を起こす。

        resume 経路の緩い契約 (不正値 → None) から見て OverflowError の crash は
        契約違反にあたるため、except で拾って None に丸めることを担保する。
        手改変で state.json に巨大 int が入るシナリオを再現するため、
        `save_account_state` 経由ではなくファイルに直接書き込む。
        """
        _state_file(tmp_path).write_text(
            json.dumps(
                {
                    "last_checkin": {
                        "schema_version": 2,
                        "location_latitude": 10**400,
                        "location_longitude": 135.0,
                        "virtual_completed_at": "2026-07-03T12:30:00+09:00",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        lat, lng, _resume_at = canvasser.resume_context(tmp_path)

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

        lat, lng, _resume_at = canvasser.resume_context(tmp_path)

        assert lat is None
        assert lng is None

    def test_stateが無ければ全て空(self, tmp_path: Path) -> None:
        """初回実行相当では位置・時刻ともに空になる。"""
        got = canvasser.resume_context(tmp_path)
        assert got == (None, None, None)

    def test_strictでは破損stateの例外が伝播する(self, tmp_path: Path) -> None:
        """本番経路相当の strict=True では破損を丸めず例外を上げる。"""
        _state_file(tmp_path).write_text("{{{", encoding="utf-8")

        with pytest.raises(StateFileCorruptedError, match="canvasser_state"):
            canvasser.resume_context(tmp_path, strict=True)

    def test_legacy_completed_spotsはstrict_loadを通す(self, tmp_path: Path) -> None:
        """旧スキーマの completed_spots (不正 slug 含む) を silent 無視して load する。

        `_validate_completed_spots` を廃止しても strict load が通ることを担保する。
        値の内容は resume には使わないので、後続の走行対象にも影響しない。
        """
        canvasser.save_account_state(
            tmp_path,
            {
                "last_checkin": {
                    "schema_version": 2,
                    "spot_slug": "cg_vote2026_1",
                    "spot_name": "テスト",
                    "location_latitude": 35.0,
                    "location_longitude": 135.0,
                    "virtual_completed_at": "2026-07-03T12:30:00+09:00",
                    "real_completed_at": "2026-07-03T03:30:00+00:00",
                },
                "completed_spots": ["evil/../path", "cg_vote2026_1"],
            },
        )

        lat, lng, resume_at = canvasser.resume_context(tmp_path, strict=True)

        assert (lat, lng) == (35.0, 135.0)
        assert resume_at == datetime(2026, 7, 3, 12, 30, tzinfo=JST)



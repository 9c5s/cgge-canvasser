"""_setup_logging の契約検証 (handler の配置、file 出力、多重装着回避)。"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

import canvasser


@pytest.fixture(autouse=True)
def isolate_logger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """LOG_DIR を tmp に差し替え、テスト前後で logger を初期状態に戻す。"""
    monkeypatch.setattr(canvasser, "LOG_DIR", tmp_path / "logs")
    saved_handlers = list(canvasser.logger.handlers)
    saved_level = canvasser.logger.level
    saved_propagate = canvasser.logger.propagate
    yield
    # 装着した FileHandler を close してファイルロックを外す
    for h in list(canvasser.logger.handlers):
        canvasser.logger.removeHandler(h)
        h.close()
    for h in saved_handlers:
        canvasser.logger.addHandler(h)
    canvasser.logger.setLevel(saved_level)
    canvasser.logger.propagate = saved_propagate


def _file_handlers() -> list[logging.FileHandler]:
    return [h for h in canvasser.logger.handlers if isinstance(h, logging.FileHandler)]


def _console_handlers() -> list[logging.Handler]:
    """FileHandler を除いた StreamHandler (= console)。"""
    return [
        h
        for h in canvasser.logger.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]


class TestSetupLogging:
    """`_setup_logging` は起動時に 1 度だけ呼ばれ、handler 構成を決定する契約。"""

    def test_missionはfile_handlerとconsoleの2枚を装着する(
        self, tmp_path: Path
    ) -> None:
        """mission は永続ログ対象 → file handler + console の 2 枚。"""
        canvasser._setup_logging("mission")

        file_handlers = _file_handlers()
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == tmp_path / "logs" / "mission.log"
        assert len(_console_handlers()) == 1

    def test_checkinはfile_handlerとconsoleの2枚を装着する(
        self, tmp_path: Path
    ) -> None:
        """checkin も永続ログ対象 → mission と同じ構成 (ファイル名だけ異なる)。"""
        canvasser._setup_logging("checkin")

        file_handlers = _file_handlers()
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == tmp_path / "logs" / "checkin.log"

    def test_loginはconsoleだけを装着しfile_handlerは付けない(self) -> None:
        """login は対話 1 回きりなのでファイル永続化しない (要件)。"""
        canvasser._setup_logging("login")

        assert _file_handlers() == []
        assert len(_console_handlers()) == 1

    def test_二回呼んでもhandlerは重複しない(self) -> None:
        """テストや再入で複数回呼ばれても handler は 2 枚を超えない。"""
        canvasser._setup_logging("mission")
        canvasser._setup_logging("mission")

        assert len(_file_handlers()) == 1
        assert len(_console_handlers()) == 1

    def test_サブコマンドを切り替えるとfile_handlerも切り替わる(
        self, tmp_path: Path
    ) -> None:
        """mission → login では file handler が外れる (残らない)。"""
        canvasser._setup_logging("mission")
        canvasser._setup_logging("login")

        assert _file_handlers() == []

    def test_levelはINFO_propagateはTrue(self) -> None:
        """INFO 以上を捕捉し、caplog が root 経由で拾えるよう propagate は既定 True。"""
        canvasser._setup_logging("mission")

        assert canvasser.logger.level == logging.INFO
        assert canvasser.logger.propagate is True

    def test_logs_dirは自動作成される(self, tmp_path: Path) -> None:
        """mission/checkin 初回呼び出しで logs/ が無くても mkdir で作られる。"""
        assert not (tmp_path / "logs").exists()

        canvasser._setup_logging("mission")

        assert (tmp_path / "logs").is_dir()

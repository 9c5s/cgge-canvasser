"""_setup_logging の契約検証 (handler の配置、file 出力、多重装着回避)。"""

import logging
import sys
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
    # テスト中に _setup_logging が logger.handlers を iterate して close() するため、
    # 事前に外しておかないと saved_handlers 内の handler まで close されてしまう。
    # teardown で restore する契約を守るため、close はテストで装着したもの限定。
    for h in saved_handlers:
        canvasser.logger.removeHandler(h)
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

    def test_missionはfile_handlerと2枚のconsoleを装着する(
        self, tmp_path: Path
    ) -> None:
        """mission → file handler 1 枚 + console 2 枚 (stdout/stderr split)。"""
        canvasser._setup_logging("mission")

        file_handlers = _file_handlers()
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == tmp_path / "logs" / "mission.log"
        assert len(_console_handlers()) == 2

    def test_checkinはfile_handlerと2枚のconsoleを装着する(
        self, tmp_path: Path
    ) -> None:
        """checkin も永続ログ対象 → mission と同じ構成 (ファイル名だけ異なる)。"""
        canvasser._setup_logging("checkin")

        file_handlers = _file_handlers()
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == tmp_path / "logs" / "checkin.log"

    def test_loginは2枚のconsoleだけを装着しfile_handlerは付けない(self) -> None:
        """login は対話 1 回きりなのでファイル永続化しない (要件)。console は 2 枚。"""
        canvasser._setup_logging("login")

        assert _file_handlers() == []
        assert len(_console_handlers()) == 2

    def test_二回呼んでもhandlerは重複しない(self) -> None:
        """テストや再入で複数回呼ばれても handler の枚数は増えない。"""
        canvasser._setup_logging("mission")
        canvasser._setup_logging("mission")

        assert len(_file_handlers()) == 1
        assert len(_console_handlers()) == 2

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

    def test_console_formatterはmessageのみ(self) -> None:
        """コンソール 2 枚とも従来 print 互換の message だけを流す。"""
        canvasser._setup_logging("mission")

        for console in _console_handlers():
            assert console.formatter is not None
            assert console.formatter._fmt == "%(message)s"

    def test_console_infoはstdoutに向く(self) -> None:
        """INFO 以下の進捗は従来の print と同じく stdout に向ける。"""
        canvasser._setup_logging("mission")

        stdout_consoles = [
            h for h in _console_handlers() if getattr(h, "stream", None) is sys.stdout
        ]
        assert len(stdout_consoles) == 1
        h = stdout_consoles[0]
        # filter が WARNING 未満のみ通す。Handler.filter は通過時に truthy を返す。
        info = logging.LogRecord("canvasser", logging.INFO, "", 0, "msg", None, None)
        warn = logging.LogRecord("canvasser", logging.WARNING, "", 0, "msg", None, None)
        assert h.filter(info)
        assert not h.filter(warn)

    def test_console_warningはstderrに向く(self) -> None:
        """WARNING 以上のみ stderr の handler に流す。"""
        canvasser._setup_logging("mission")

        stderr_consoles = [
            h for h in _console_handlers() if getattr(h, "stream", None) is sys.stderr
        ]
        assert len(stderr_consoles) == 1
        assert stderr_consoles[0].level == logging.WARNING

    def test_file_formatterはtimestampとlevelnameを持つ(self) -> None:
        """永続ログは時刻とレベルを付ける (診断時の時系列追跡のため)。"""
        canvasser._setup_logging("mission")

        fh = _file_handlers()[0]
        assert fh.formatter is not None
        assert fh.formatter._fmt == "%(asctime)s %(levelname)s: %(message)s"

    def test_file_handlerはutf8で開く(self) -> None:
        """日本語メッセージが Windows 既定 cp932 で mojibake しないよう utf-8 固定。"""
        canvasser._setup_logging("mission")

        fh = _file_handlers()[0]
        assert fh.encoding is not None
        assert fh.encoding.lower().replace("-", "") == "utf8"

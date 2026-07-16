"""ロギング補助関数 (redact / body allowlist / path sanitize) の直接検証。"""

import pytest

from canvasser import (
    _log_body,
    _redact_secrets,
    _sanitize_exception,
    _sanitize_paths,
)


class TestRedactSecrets:
    """`_redact_secrets` はログ出力前に URL 内の機密クエリ値を伏せる。"""

    @pytest.mark.parametrize(
        "url",
        [
            "https://maps.googleapis.com/foo?key=SECRET",
            "https://x/?a=1&key=SECRET&b=2",
            "https://x/?a=1&signature=SIG",
            "https://x/?api_key=SECRET",
            "https://x/?api-key=SECRET",
            "https://x/?KEY=SECRET",
            "https://x/?key=SECRET&signature=SIG&api_key=A",
        ],
    )
    def test_機密パラメータの値は伏せられる(self, url: str) -> None:
        """key / signature / api_key / api-key を大文字小文字問わず伏せる。"""
        redacted = _redact_secrets(url)

        for secret in ("SECRET", "SIG", "=A"):
            assert secret not in redacted, redacted
        assert "***" in redacted

    def test_複数の機密パラメータをすべて伏せる(self) -> None:
        """同一 URL に複数個あればすべて置換する (最初の 1 個だけではない)。"""
        redacted = _redact_secrets("https://x/?key=AAA&signature=BBB&api_key=CCC")

        assert "AAA" not in redacted
        assert "BBB" not in redacted
        assert "CCC" not in redacted
        assert redacted.count("***") == 3

    def test_機密パラメータでなければ変更しない(self) -> None:
        """`token` / `password` は現状 allowlist 外だが、value は保持される。"""
        text = "https://x/?token=abc&password=xyz&normal=1"

        assert _redact_secrets(text) == text

    def test_クエリ文字列を含まなければそのまま返す(self) -> None:
        """通常メッセージや hostname だけの text は変更されない。"""
        text = "connection reset by peer"

        assert _redact_secrets(text) == text

    @pytest.mark.parametrize(
        "text, expected",
        [
            (
                "{'url': 'https://x/?key=SECRET'}",
                "{'url': 'https://x/?key=***'}",
            ),
            (
                '"https://x/?key=SECRET"',
                '"https://x/?key=***"',
            ),
            (
                "(https://x/?key=SECRET)",
                "(https://x/?key=***)",
            ),
            (
                "url=https://x/?key=SECRET, next",
                "url=https://x/?key=***, next",
            ),
        ],
    )
    def test_デリミタは巻き込んで伏せない(self, text: str, expected: str) -> None:
        """引用符 / 括弧 / カンマなどのデリミタは redact に巻き込まれず保持される。"""
        assert _redact_secrets(text) == expected


class TestLogBody:
    """`_log_body` は API 応答 body から診断に必要な既知キーだけ抽出する。"""

    def test_dict応答からstatusとpayload_ecodeを抽出する(self) -> None:
        """status と payload.ecode を allowlist で取り出す。"""
        got = _log_body({
            "status": "FAIL",
            "payload": {"ecode": "E1234", "detail": "secret"},
        })

        assert "status='FAIL'" in got
        assert "payload.ecode='E1234'" in got
        assert "secret" not in got

    def test_payload_messageも抽出する(self) -> None:
        """message は allowlist に含まれるので抽出される。"""
        got = _log_body({
            "status": "FAIL",
            "payload": {"message": "something went wrong"},
        })

        assert "payload.message='something went wrong'" in got

    def test_未知キーのみのdictはraw値を残さない(self) -> None:
        """status も ecode も無い dict は `<dict, len=N>` に丸める。"""
        got = _log_body({"unknown_field": "secret_value"})

        assert "secret_value" not in got
        assert "unknown_field" not in got
        assert got.startswith("<dict, len=")

    def test_dict以外の応答は型名とlenだけ残す(self) -> None:
        """文字列 / bytes / None など未知形状は raw を出さず型名と長さだけ。"""
        got = _log_body("very long raw response body")

        assert "response body" not in got
        assert got.startswith("<str, len=")

    def test_payloadがdictでなければpayloadキーを出さない(self) -> None:
        """payload が None / str などの想定外形式のときは payload.* を出さない。"""
        got = _log_body({"status": "OK", "payload": None})

        assert "status='OK'" in got
        assert "payload." not in got

    def test_payload_ecodeがなければstatusだけ出す(self) -> None:
        """成功応答 (status=SUCCESS, payload に ecode 無し) では status のみ。"""
        got = _log_body({"status": "SUCCESS", "payload": {"other": 1}})

        assert got == "{status='SUCCESS'}"


class TestSanitizePaths:
    """`_sanitize_paths` は例外メッセージ内の Windows 絶対パスを basename に丸める。"""

    def test_絶対パスは_path_プレフィックスとbasenameに置換される(self) -> None:
        """Windows ユーザー名を含むフルパスは basename だけ残す。"""
        text = r"C:\Users\shun\projects\cgge-canvasser\profiles\shun\Default\Login Data"

        got = _sanitize_paths(text)

        assert "shun" not in got
        assert got.endswith("Login Data")
        assert "<path>/" in got

    def test_forward_slash区切りにも対応する(self) -> None:
        """Path.as_posix() 由来の forward slash パスも丸められる。"""
        text = "C:/Users/shun/projects/cgge-canvasser/profiles/shun/state.json"

        got = _sanitize_paths(text)

        assert "shun" not in got
        assert got.endswith("state.json")

    def test_文の中に紛れた絶対パスも置換する(self) -> None:
        """例外メッセージ内に埋め込まれたパスも対象。"""
        text = r"OSError: cannot open C:\Users\shun\project\file.txt for reading"

        got = _sanitize_paths(text)

        assert "shun" not in got
        assert "cannot open" in got
        assert "for reading" in got

    def test_絶対パスを含まないメッセージは変更しない(self) -> None:
        """相対パスやただの文字列はそのまま返す。"""
        text = "connection reset by peer"

        assert _sanitize_paths(text) == text

    def test_複数のパスをすべて置換する(self) -> None:
        """メッセージ内に複数の Windows パスが並んでも全部丸める。"""
        text = r"src=C:\Users\shun\a.txt dst=C:\Users\shun\b.txt"

        got = _sanitize_paths(text)

        assert "shun" not in got
        assert "a.txt" in got
        assert "b.txt" in got

    def test_スペース入りユーザー名も丸める(self) -> None:
        r"""`C:\Users\Jane Doe\...` のようなスペース入りユーザー名も漏れない。"""
        text = r"C:\Users\Jane Doe\projects\main\canvasser_state.json"

        got = _sanitize_paths(text)

        assert "Jane" not in got
        assert "Doe" not in got
        assert got.endswith("canvasser_state.json")
        assert "<path>/" in got

    def test_Users以外のドライブも_path_に丸まる(self) -> None:
        r"""`D:\projects\...` も step2 で basename に丸められる。"""
        text = r"D:\projects\cgge-canvasser\profiles\shun\file.txt"

        got = _sanitize_paths(text)

        # `<user>` は入らない (Users パターンではないため step1 は動かない)
        assert "<user>" not in got
        assert got.endswith("file.txt")
        assert "<path>/" in got


class TestSanitizeException:
    """`_sanitize_exception` は URL secret redact と絶対パス redact を両方適用する。"""

    def test_urlのkeyとパスの両方が処理される(self) -> None:
        """GMAPS_KEY を含む URL とローカル絶対パスを同時に redact する。"""
        e = RuntimeError(
            r"failed to fetch https://x/?key=SECRET (opened C:\Users\shun\a.txt)"
        )

        got = _sanitize_exception(e)

        assert "SECRET" not in got
        assert "shun" not in got

    def test_通常メッセージはそのまま返す(self) -> None:
        """redact 対象を含まない例外 message はそのまま返す。"""
        e = RuntimeError("connection reset by peer")

        assert _sanitize_exception(e) == "connection reset by peer"

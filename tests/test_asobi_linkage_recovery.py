"""ASOBI 連携復旧ドライバのテスト。

Playwright モックで page.url / page.locator / page.goto を差し込む。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import canvasser
from tests._fakes import FakePage, as_page

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import pytest


class _FakeTime:
    """`canvasser.time` を丸ごと差し替える決定的フェイク。

    `monotonic()` は `monotonic_values` を順に返し、尽きたら最後の値を返し続ける
    (`FakeLocator.count` の `counts_sequence` と同じ「最後の値は保持」規約)。
    `sleep()` はテストを待たせない no-op。
    """

    def __init__(self, monotonic_values: Sequence[float] = (0.0,)) -> None:
        """ポーリングで順に返す monotonic 値の列を受け取る。"""
        self._values = list(monotonic_values)

    def sleep(self, seconds: float) -> None:
        """待たない no-op。"""
        return

    def monotonic(self) -> float:
        """次の monotonic 値を返す (最後の 1 件は消費しきらない)。"""
        return self._values.pop(0) if len(self._values) > 1 else self._values[0]


def _install_fake_time(
    monkeypatch: pytest.MonkeyPatch, monotonic_values: Sequence[float] = (0.0,)
) -> None:
    """`canvasser.time` を `_FakeTime` に差し替え、ポーリングを決定的にする。"""
    monkeypatch.setattr(canvasser, "time", _FakeTime(monotonic_values))


class MutablePage(FakePage):
    """polling で url / visibility を書き換えるための拡張フェイク。"""

    def __init__(
        self,
        url_sequence: list[str],
        *,
        responses: Sequence[object] | None = None,
        visibility: dict[str, bool] | None = None,
        counts: dict[str, int] | None = None,
        counts_sequence: dict[str, list[int]] | None = None,
        wait_for_errors: dict[str, list[Exception | None]] | None = None,
        click_errors: dict[str, list[Exception | None]] | None = None,
        goto_errors: Sequence[Exception | None] | None = None,
    ) -> None:
        """backto 到達までに辿る URL 列と、FakePage 向けの各種設定を受け取る。"""
        super().__init__(
            responses,
            visibility=visibility,
            counts=counts,
            counts_sequence=counts_sequence,
            wait_for_errors=wait_for_errors,
            click_errors=click_errors,
            goto_errors=goto_errors,
        )
        self._url_seq = list(url_sequence)
        self.url = self._url_seq[0] if self._url_seq else ""

    def goto(self, url: str, **kwargs: object) -> None:
        """遷移を記録し、url_sequence の次の URL に進める。"""
        self.calls.append(("goto", (url, kwargs)))
        # 次の URL を進める
        if len(self._url_seq) > 1:
            self._url_seq.pop(0)
            self.url = self._url_seq[0]


def test_recovery_backto_direct_landing_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BNID セッション生存: backto に直着地して True を返す。"""
    _install_fake_time(monkeypatch)
    page = MutablePage(url_sequence=[canvasser.MISSION_PAGE_URL])
    result = canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test")
    assert result is True


def test_recovery_bnid_form_appears_calls_guarded_auto_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BNID フォーム可視化時は _run_guarded_auto_login を呼び、成功なら継続する。"""
    _install_fake_time(monkeypatch)
    call_log: list[str] = []

    def fake_guarded(page: MutablePage, profile_dir: Path, name: str) -> bool:
        call_log.append("guarded")
        # ログイン成功後、backto に到達したと仮定する
        page.url = canvasser.MISSION_PAGE_URL
        return True

    monkeypatch.setattr(canvasser, "_run_guarded_auto_login", fake_guarded)
    page = MutablePage(
        url_sequence=["https://account.bandainamcoid.com/login.html"],
        visibility={canvasser._LOGIN_MAIL_SEL: True},
    )
    result = canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test")
    assert result is True
    assert call_log == ["guarded"]


def test_recovery_bnid_form_guarded_login_fails_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_run_guarded_auto_login が失敗したら復旧全体も False を返す。"""
    _install_fake_time(monkeypatch)

    def fake_guarded_fail(page: MutablePage, profile_dir: Path, name: str) -> bool:
        return False

    monkeypatch.setattr(canvasser, "_run_guarded_auto_login", fake_guarded_fail)
    page = MutablePage(
        url_sequence=["https://account.bandainamcoid.com/login.html"],
        visibility={canvasser._LOGIN_MAIL_SEL: True},
    )
    result = canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test")
    assert result is False


def test_recovery_captcha_detected_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAPTCHA/2FA を検知したら False を返す。"""
    _install_fake_time(monkeypatch)
    page = MutablePage(
        url_sequence=["https://asobistore.jp/some-page"],
        counts=dict.fromkeys(canvasser._LOGIN_CAPTCHA_SELECTORS, 1),
    )
    result = canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test")
    assert result is False


def test_recovery_timeout_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """URL が backto にも BNID form にも中間ページにも行かない → timeout で False。"""
    _install_fake_time(monkeypatch, monotonic_values=(0.0, 100.0))
    page = MutablePage(url_sequence=["https://unrelated.example.com/"])
    result = canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test")
    assert result is False


def test_recovery_passkey_prompt_dismissed_then_backto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BNID のパスキー案内画面が出たら「あとで」を押し、backto 到達で True を返す。"""
    _install_fake_time(monkeypatch)
    page = MutablePage(
        url_sequence=[
            "https://account.bandainamcoid.com/passkeyInfo.html?client_id=imasofficial"
        ],
        visibility={canvasser._PASSKEY_SKIP_BTN_SEL: True},
    )
    page.click_navigations[canvasser._PASSKEY_SKIP_BTN_SEL] = canvasser.MISSION_PAGE_URL
    result = canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test")
    assert result is True
    assert (
        "click",
        (canvasser._PASSKEY_SKIP_BTN_SEL, {"no_wait_after": True}),
    ) in page.calls

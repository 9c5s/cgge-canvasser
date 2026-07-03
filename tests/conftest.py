"""テスト全体で共有する fixture 定義。"""

from collections.abc import Iterator

import pytest

import canvasser


@pytest.fixture(autouse=True)
def isolate_gmaps(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """GMAPS_KEY と gmaps キャッシュを隔離してテストを決定的に保つ。

    canvasser は import 時に .env を読むため、開発環境の GMAPS_KEY が
    そのまま残るとテストが Google Maps API へ実通信してしまう。全テストで
    キーを外し、`functools.cache` とモジュールレベルのキャッシュも毎回
    リセットする。
    """
    monkeypatch.delenv("GMAPS_KEY", raising=False)
    canvasser._get_gmaps_client.cache_clear()
    canvasser._GMAPS_CACHE.clear()
    yield
    canvasser._get_gmaps_client.cache_clear()
    canvasser._GMAPS_CACHE.clear()

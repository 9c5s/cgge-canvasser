# googlemaps の最小型スタブ。
# googlemaps は API メソッドを実行時に Client へ動的登録するため、静的解析からは
# `Client.directions` が見えない。本プロジェクトで使う範囲だけを宣言する。

from datetime import datetime
from typing import Any

class Client:
    def __init__(self, key: str | None = None, timeout: int | None = None) -> None: ...
    def directions(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        mode: str | None = None,
        departure_time: datetime | str | None = None,
        language: str | None = None,
        alternatives: bool = False,
    ) -> list[dict[str, Any]]: ...

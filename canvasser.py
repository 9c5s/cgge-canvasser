# /// script
# requires-python = ">=3.14"
# dependencies = [
#     "playwright>=1.40.0",
#     "pycryptodome>=3.19",
#     "googlemaps>=4.10",
#     "python-dotenv>=1.0",
#     "tzdata>=2024.1; sys_platform == 'win32'",
# ]
# ///
"""シンデレラガール総選挙2026 デイリー自動化スクリプト。

Playwright の persistent context でブラウザセッションを保持し、フロントが叩く
内部 API をそのまま呼び出してミッション回収とチェックインを自動化する。
複数アカウントは ./profiles/{account}/ 配下に分けて運用する。

  uv run canvasser.py login --account main         # 初回ログイン
  uv run canvasser.py mission                      # ミッション本番
  uv run canvasser.py mission --dry-run            # ミッション ドライラン
  uv run canvasser.py checkin                      # チェックイン本番
  uv run canvasser.py checkin --dry-run            # チェックイン ドライラン
  uv run canvasser.py mark-completed --account main cg_vote2026_17  # 手動完了登録

mission と checkin は独立したサブコマンドで、同時実行はしない。無指定なら本番、
`--dry-run` を付けた場合のみ GET のみのドライランとなり、POST/PUT は一切送らない。
"""

# print はこの CLI の仕様そのもの (標準出力が UI) のため、T201 はファイル全体で
# 抑制する。
# ruff: noqa: T201

import argparse
import base64
import contextlib
import functools
import getpass
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self, cast
from zoneinfo import ZoneInfo

import googlemaps
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from dotenv import load_dotenv
from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    sync_playwright,
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Playwright

# cwd が違ってもタスクスケジューラから読めるよう、スクリプト隣の .env を明示ロードする。
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

# サーバも JST で返してくるため、内部時刻は JST に統一する。
JST = ZoneInfo("Asia/Tokyo")

# Next.js のチャンク解析で抽出した総選挙2026 用の名前空間。
API_HOST = "https://api.idolmaster-official.jp"
API_V1 = f"{API_HOST}/api/v1_1_0"
API_BASE = "/api/v1_1_0/mileage_vote/cinderellagirls_vote_2026"
API_KEY = "MEW6XfDHZVtpUxuERAGTaP6AfipAe53kCEFWEMAJ"
CHECKIN_EVENT_SLUG = "cg_vote2026"
MISSION_PAGE_URL = (
    "https://idolmaster-official.jp/cinderellagirls/vote2026/vote/mission"
)
CHECKIN_PAGE_URL = f"https://idolmaster-official.jp/mydesk/spot/{CHECKIN_EVENT_SLUG}"
LOGIN_CHECK_URL = f"{API_HOST}/api/v1_1_0/auths/login/check"
# BNID ログイン画面への遷移入口。ここから OAuth リダイレクトチェーンで
# `account.bandainamcoid.com/login.html` に着地する (Phase 1 で確認)。
LOGIN_ENTRY_URL = (
    f"{API_HOST}/api/v1_1_0/auths/login?backurl=/cinderellagirls/vote2026/vote/mission"
)

# 地球の 1 度緯度に対応する距離 [m]。WGS84 近似。
METERS_PER_DEG_LAT = 111_320.0
# 許容半径ぎりぎりの座標を避けて境界事故を防ぐ内寄せ係数。
CHECKIN_RADIUS_MARGIN = 0.85


def _default_now() -> datetime:
    """collect_checkins の now_fn 既定値。テスト時は差し替える。"""
    return datetime.now(JST)


def _as_jst_aware(dt: datetime) -> datetime:
    """時刻を JST の aware に揃える (naive は JST とみなして付与する)。

    サーバ応答も内部時刻も JST 前提のため、naive はすべて JST として扱う。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=JST)
    return dt.astimezone(JST)


# call_api と call_checkin_api が共有する fetch ラッパー JS。
# - 送信元と API は別ホストだが、サーバ側で CORS allow-credentials が有効なので
#   credentials=include で通る。x-api-key はフロント公開の固定値。
# - fetch reject (ネットワーク・CORS・DNS 失敗) や非 JSON 応答も
#   {status, body, error} で構造化して返し、呼び出し側で status==0 や
#   エラーコードで分岐できるようにする。
# - body があれば application/x-www-form-urlencoded で送る (axios が data:string
#   を送るときの既定 Content-Type を実 UI キャプチャで確定した)。
_FETCH_JSON_JS = """
async ([url, method, apiKey, body]) => {
  try {
    const res = await fetch(url, {
      method,
      credentials: 'include',
      headers: {
        'x-api-key': apiKey,
        'accept': 'application/json, text/plain, */*',
        ...(body ? {'content-type': 'application/x-www-form-urlencoded'} : {})
      },
      body: body ?? undefined,
    });
    const raw = await res.text();
    try { return {status: res.status, body: JSON.parse(raw)}; }
    catch { return {status: res.status, body: raw, error: 'non-json'}; }
  } catch (e) {
    return {status: 0, body: null, error: String(e)};
  }
}
"""


def call_api(page: Page, method: str, path: str) -> dict[str, Any]:
    """ページ内で fetch を実行し、Cookie 付きで API を叩く。

    応答は `_FETCH_JSON_JS` が {status, body, error} に構造化して返す。
    """
    url = f"{API_HOST}{API_BASE}{path}"
    return page.evaluate(_FETCH_JSON_JS, [url, method, API_KEY, None])


def _as_str_dict(value: object) -> dict[str, Any] | None:
    """与えられた値が dict なら dict[str, Any] とみなして返す。それ以外は None。

    API 応答の body や payload は isinstance で dict に絞ると型引数が Unknown に
    落ちて pyright strict を通らないため、この関数で絞り込みと cast を束ねる。
    """
    if isinstance(value, dict):
        return cast("dict[str, Any]", value)
    return None


def _extract_ecode(body: object) -> str | None:
    """API 応答の body から ecode を安全に取り出す。

    body・payload のどちらが dict 以外でも None に丸め、想定外形式の応答で
    AttributeError にならないようにする。
    """
    body_dict = _as_str_dict(body)
    if body_dict is None:
        return None
    payload = _as_str_dict(body_dict.get("payload"))
    if payload is None:
        return None
    return payload.get("ecode")


def _is_success_response(res: dict[str, Any]) -> bool:
    """API 応答が HTTP 200 かつ status=SUCCESS かを判定する。"""
    body = _as_str_dict(res.get("body"))
    return res["status"] == 200 and body is not None and body.get("status") == "SUCCESS"


def _success_payload_or_raise(res: dict[str, Any], err_prefix: str) -> dict[str, Any]:
    """成功応答の payload を取り出す。失敗応答なら RuntimeError を送出する。

    一覧取得系 (ミッション・チェックインスポット) の「成功判定 → 失敗なら
    応答全体入りのメッセージで raise → payload を cast」の定型を束ねる。
    """
    if not _is_success_response(res):
        msg = f"{err_prefix}: {res}"
        raise RuntimeError(msg)
    body = cast("dict[str, Any]", res["body"])
    return cast("dict[str, Any]", body.get("payload") or {})


# BNID ログイン画面 (Phase 1 で id ベースの安定 selector を確認済み) の DOM 契約。
# `#mail` / `#pass` は input、`#btn-idpw-login` は submit ボタン
# (初期 disabled、両方入力で enable)。エラー表示は `#error-input-area
# .c-message--warning`。
# 制約: BNID はキー入力イベントで disabled を外すため、`fill()` ではなく
# `press_sequentially()` を必ず使う (Phase 1 で判明)。
_LOGIN_MAIL_SEL = "#mail"
_LOGIN_PASS_SEL = "#pass"  # noqa: S105 (CSS selector, not a credential)
_LOGIN_BTN_SEL = "#btn-idpw-login"
_LOGIN_BTN_ENABLED_SEL = "#btn-idpw-login:not([disabled])"
_LOGIN_ERROR_SEL = "#error-input-area .c-message--warning"

# CAPTCHA / 2FA が (将来的に) 混入した時に検知するための selector。
# Phase 1 時点では BNID は初期表示・1回失敗のいずれでも痕跡なしだが、動的挿入に
# 備えて防御的に見張る。
_LOGIN_CAPTCHA_SELECTORS: tuple[str, ...] = (
    'iframe[src*="captcha"]',
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="turnstile"]',
)


def check_login(page: Page) -> bool:
    """auths/login/check を叩いて認証状態を確認する。

    fetch 例外や非 JSON 応答は未ログインと同義に丸める。Cookie 期限切れや
    ネットワーク不通も「未ログインなら login を促す」ルートに合流させるため。
    """
    result = page.evaluate(
        """
        async ([url, apiKey]) => {
          try {
            const res = await fetch(url, {
              credentials: 'include',
              headers: {'x-api-key': apiKey}
            });
            const raw = await res.text();
            try { return JSON.parse(raw); }
            catch { return {status: 'ERROR', payload: {}}; }
          } catch (e) {
            return {status: 'ERROR', payload: {}, __error: String(e)};
          }
        }
        """,
        [LOGIN_CHECK_URL, API_KEY],
    )
    result_dict = _as_str_dict(result)
    if result_dict is None:
        return False
    payload = _as_str_dict(result_dict.get("payload")) or {}
    return bool(payload.get("is_login", False))


# サーバが受け付ける `mission_type` の全集合。0=通常ミッション、1=ASOBI STORE 系。
# 2 以上は現状 E4200 (mission_type は 0 または 1) で拒否される。
_MISSION_TYPES: tuple[tuple[int, str], ...] = ((0, "通常"), (1, "ASOBI STORE"))


def collect_missions(page: Page, *, dry_run: bool) -> int:
    """API 経由で完了可能なミッションと、外部トリガー達成分の受取をまとめて消化する。

    通常 (`mission_type=0`) と ASOBI STORE 系 (`mission_type=1`) の両方を fetch する。
    ASOBI STORE 系にはプレミアム会員のログインボーナス (`#21`) など日次で回収したい
    ミッションが含まれるため、片方だけ fetch すると取りこぼす。

    - `mission_complete_api_call_flag=True` は達成 POST と受取 PUT の両方を送る対象。
      #100-104 のような累計達成数系や、動画視聴などフロントが達成 POST を出すもの。
    - `mission_complete_api_call_flag=False` でも「達成済みかつ未受取」なら受取 PUT
      だけは送る。チェックインボーナス (`#99`) などの外部トリガー達成分の取りこぼし
      を防ぐため。
    - あいことばなど、達成条件が UI 経由のみのミッションは flag=False かつ未達成の
      ままなので、達成 POST も受取 PUT も送らない。
    - `dry_run=True` は完全ドライラン。GET のみ実行し、POST/PUT は送らない。

    戻り値は今回獲得した投票券数の合計 (dry-run 時は実行した場合の見込み)。
    """
    mode_label = "本番" if not dry_run else "DRY-RUN (POST/PUT送信なし)"

    # 両 listing を先に fetch してから POST/PUT を送る。前段で PUT (受取) を送った
    # 後に後段 fetch が失敗すると、run summary で「そのアカウント 0 gained」と誤記録
    # になり、サーバ側に反映済みの投票券が集計から欠落する。fetch 失敗はここで
    # まとめて fail-closed する。
    listings: list[tuple[int, str, dict[str, Any]]] = []
    for mt, label in _MISSION_TYPES:
        listing = call_api(page, "GET", f"/missions?mission_type={mt}&limit=300")
        payload = _success_payload_or_raise(
            listing, f"ミッション一覧 ({label}) の取得に失敗"
        )
        listings.append((mt, label, payload))

    gained = 0
    for mt, label, payload in listings:
        if mt == 0:
            print(f"現在の保有投票券: {payload.get('current_point', 0)}枚")
        print(f"ミッションモード ({label}): {mode_label}")
        for m in cast("list[dict[str, Any]]", payload["missions"]):
            gained += _process_one_mission(page, m, dry_run=dry_run)

    result_label = "獲得見込み" if dry_run else "獲得"
    print(f"ミッション {result_label}: {gained}枚")
    return gained


def _process_one_mission(page: Page, m: dict[str, Any], *, dry_run: bool) -> int:
    """1 ミッションの達成 / 受取を行い、獲得票数を返す。

    分岐:
      - `completed and not received` → 受取 PUT を送る (flag に関わらず)
      - `flag=True and not completed and remaining>0` → 達成 POST → 受取 PUT
      - `flag=False and not completed` → 何もしない (UI 経由のあいことば等)
    """
    mid: int = m["mission_id"]
    name: str = m["mission_name"]
    pts: int = m["mission_point"]

    action = cast("dict[str, Any]", m.get("action") or {})
    api_completable = bool(action.get("mission_complete_api_call_flag"))
    completed = bool(m.get("is_mission_completed"))
    received = bool(m.get("is_mission_received"))
    remaining = m.get("remaining_completable_count") or 0

    if completed and not received:
        return _receive(page, mid, name, pts, dry_run=dry_run)

    if not api_completable:
        return 0

    if not completed and remaining > 0:
        outcome = _complete(page, mid, name, dry_run=dry_run)
        # 累計達成数系ミッション (#100-104) はサーバ側で自動達成されるが、
        # 一覧の completed フラグ更新が遅延する。達成 POST が「既に達成済み」を
        # 返した場合も受取 PUT は通る。
        if outcome in ("ok", "already_done"):
            return _receive(page, mid, name, pts, dry_run=dry_run)

    return 0


def _complete(page: Page, mid: int, name: str, *, dry_run: bool) -> str:
    """ミッション達成の POST を送る。

    一覧取得は `/missions` (複数形) だが個別操作は `/mission` (単数形)。
    フロントの `b.eq` 定数が単数形であることをチャンク解析で確認済み。

    戻り値:
      - "ok"             : 達成成功
      - "already_done"   : E1906 既に達成済み (受取 PUT は試すべき)
      - "condition_unmet": E1924 達成条件未満 (静かにスキップ)
      - "error"          : その他失敗
    """
    print(f"[達成] #{mid} {name}")
    if dry_run:
        print("  -> DRY-RUN (POST送信なし)")
        return "ok"
    res = call_api(page, "POST", f"/mission/{mid}")
    if _is_success_response(res):
        print("  -> 成功")
        return "ok"

    ecode = _extract_ecode(res.get("body"))
    if ecode == "E1906":
        print("  -> 既に達成済み (受取を試す)")
        return "already_done"
    if ecode == "E1924":
        print("  -> 条件未達、スキップ")
        return "condition_unmet"

    err_note = f" err={res.get('error')}" if res.get("error") else ""
    print(f"  -> 失敗: HTTP {res['status']}{err_note} body={res.get('body')}")
    return "error"


def _receive(page: Page, mid: int, name: str, pts: int, *, dry_run: bool) -> int:
    """投票券受取の PUT を送る。

    成功時は加算票数、dry-run 時は「実行していれば得た pts」を返す。
    """
    print(f"[受取] #{mid} {name} (+{pts})")
    if dry_run:
        print("  -> DRY-RUN (PUT送信なし)")
        return pts
    res = call_api(page, "PUT", f"/mission/{mid}/receive")
    if _is_success_response(res):
        body = cast("dict[str, Any]", res["body"])
        payload = cast("dict[str, Any]", body.get("payload") or {})
        received = payload.get("received_point")
        print(f"  -> 成功 (received_point={received})")
        return pts
    err_note = f" err={res.get('error')}" if res.get("error") else ""
    print(f"  -> 失敗: HTTP {res['status']}{err_note} body={res.get('body')}")
    return 0


# -------------------- チェックイン (#99) 関連 --------------------


def encrypt_coords(coords: dict[str, Any], password: str = API_KEY) -> str:
    """位置情報 JSON を crypto-js プロトコル互換で AES-CBC 暗号化する。

    フロントエンドが crypto-js で組み立てるペイロード形式を Python 側で再現する。
      - key = PBKDF2(password, salt=random16, iterations=500,
                     keySize=8 words=32B, hasher=SHA1)
      - iv = random16
      - ciphertext = AES-CBC(key, iv, PKCS7(JSON.stringify(coords)))
      - payload = f"{salt.hex()},{iv.hex()},{ct_base64}"

    パスワードはフロント公開値の X-API-KEY を流用しているため、この暗号化に機密性はない
    (サーバが受理する形式を再現するだけのラッパー)。salt と iv は hex、ciphertext は
    Base64 で連結する形式は、実 UI 経由の POST 観測で確定した。
    """
    salt = os.urandom(16)
    iv = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha1", password.encode(), salt, 500, dklen=32)
    plaintext = json.dumps(coords, separators=(",", ":")).encode()
    # pycryptodome 同梱スタブの AES.new は型が部分的に Unknown のため抑制する
    cipher = AES.new(key, AES.MODE_CBC, iv)  # pyright: ignore[reportUnknownMemberType]
    ciphertext = cipher.encrypt(pad(plaintext, 16))
    ct_b64 = base64.b64encode(ciphertext).decode()
    return f"{salt.hex()},{iv.hex()},{ct_b64}"


def random_point_in_circle(
    center_lat: float, center_lng: float, radius_m: float
) -> tuple[float, float]:
    """半径 radius_m の円内から面積一様分布でランダム点を返す。

    d = r*sqrt(u) で中心密集を避けた面積一様分布を作る。経度スケールは緯度で変わるため
    cos(lat) で補正する。
    """
    u = random.random()  # noqa: S311 -- 座標の自然化用で暗号用途ではない
    theta = random.random() * 2 * math.pi  # noqa: S311 -- 同上
    d = radius_m * math.sqrt(u)
    d_lat = (d * math.cos(theta)) / METERS_PER_DEG_LAT
    d_lng = (d * math.sin(theta)) / (
        METERS_PER_DEG_LAT * math.cos(math.radians(center_lat))
    )
    return center_lat + d_lat, center_lng + d_lng


def _natural_accuracy() -> float:
    """モバイル GPS 屋外の実測分布に近い精度値 [m] を返す。

    実機の accuracy は 8〜30m に強く集中し、稀に 30〜80m の外れ値が出る。一様乱数だと
    分布がフラットで統計的に不自然なので、正規分布と外れ値の混合で再現する。
    """
    if random.random() < 0.15:  # noqa: S311 -- 統計分布の再現用で暗号用途ではない
        # 屋内寄り or マルチパスの影響を想定した外れ値
        val = random.uniform(30.0, 80.0)  # noqa: S311 -- 同上
    else:
        # 5m 未満は現実的でないためクランプする
        val = max(5.0, random.gauss(18.0, 6.0))
    return round(val, 3)


def _natural_altitude() -> tuple[float | None, float | None]:
    """実機挙動に近い分布で altitude と altitudeAccuracy を返す。

    多くの環境 (80%) は取得不可の None、20% は日本の都市部平地相当の 5〜80m を返す。
    """
    if random.random() < 0.20:  # noqa: S311 -- 統計分布の再現用で暗号用途ではない
        alt = round(random.uniform(5.0, 80.0), 1)  # noqa: S311 -- 同上
        alt_acc = round(random.uniform(20.0, 50.0), 1)  # noqa: S311 -- 同上
        return alt, alt_acc
    return None, None


def make_checkin_coords(spot: Spot) -> dict[str, Any]:
    """スポット情報から、円内ランダム点と自然化した coords を組む。

    サーバ側や将来の異常検知で分布統計が取られた場合に BOT らしい特徴が出ないよう、
    accuracy と altitude を実機挙動に近い分布で乱択する。
    """
    lat, lng = random_point_in_circle(
        spot.lat,
        spot.lng,
        spot.radius * CHECKIN_RADIUS_MARGIN,
    )
    alt, alt_acc = _natural_altitude()
    return {
        "accuracy": _natural_accuracy(),
        "latitude": lat,
        "longitude": lng,
        "altitude": alt,
        "altitudeAccuracy": alt_acc,
        # 「静止して端末を見ている」想定なので heading と speed は null に置く
        "heading": None,
        "speed": None,
    }


def call_checkin_api(
    page: Page,
    method: str,
    path: str,
    body: str | None = None,
) -> dict[str, Any]:
    """API の checkins 系エンドポイントを叩く。

    body は "salt_hex,iv_hex,ct_base64" の文字列をそのまま (URL エンコード
    せずに) 載せる。Content-Type の付与は `_FETCH_JSON_JS` 側で行う。
    """
    url = f"{API_V1}/checkins{path}"
    return page.evaluate(_FETCH_JSON_JS, [url, method, API_KEY, body])


# チェックイン API の既知エラーコード。UI 側チャンクから "E5005" (範囲外) は把握済み。
# それ以外 (既達成含む) は未観測 = unknown として即停止で扱い、実観測で意味が確定した
# ecode だけを随時 ECODES_ALREADY_DONE に追加する。
ECODE_OUT_OF_RANGE = "E5005"
ECODES_ALREADY_DONE: tuple[str, ...] = ()


# 座標の有効範囲。Spot / state のいずれの経路でも境界で範囲外を弾く。
_LAT_MIN, _LAT_MAX = -90.0, 90.0
_LNG_MIN, _LNG_MAX = -180.0, 180.0


def _coerce_finite_float(v: object, *, name: str) -> float:
    """渡された値を有限な float に強制する。

    - `bool` は `int` の subclass だが数値扱いしない → `TypeError`
    - `int` / `float` / 数値文字列は `float` に変換 (不能なら `TypeError`)
    - `NaN` / `Infinity` は非有限値として `ValueError`

    Python の `json` は既定で `NaN` / `Infinity` を float として読み書きする
    (https://docs.python.org/3.14/library/json.html) ため、境界で潰さないと
    座標の距離計算や JSON 書き出しが黙って壊れる。
    """
    if isinstance(v, bool):
        msg = f"{name} が bool ({v}) で数値ではない"
        raise TypeError(msg)
    try:
        f = float(cast("Any", v))
    except (TypeError, ValueError) as e:
        msg = f"{name} を float に変換できない: {v!r}"
        raise TypeError(msg) from e
    if not math.isfinite(f):
        msg = f"{name} が非有限値: {f}"
        raise ValueError(msg)
    return f


def _finite_float_or_none(v: object) -> float | None:
    """有限な数値なら float に変換する。それ以外 (bool 含む) は None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float) and math.isfinite(v):
        return float(v)
    return None


def _finite_float_in_range_or_none(v: object, lo: float, hi: float) -> float | None:
    """有限かつ [lo, hi] 内なら float、それ以外は None。resume 用の緩い経路。"""
    f = _finite_float_or_none(v)
    if f is None or not (lo <= f <= hi):
        return None
    return f


@dataclass(frozen=True, slots=True, kw_only=True)
class Spot:
    """チェックインスポットの型付き値オブジェクト。

    API 応答の生 dict は境界 (`_fetch_checkin_spots`) で一度だけ本クラスへ
    正規化し、以降の走行ロジックは型と不変条件の確定した値として受け渡す。
    座標・半径の数値変換、radius 既定値、deadline のパースをここへ集約する。
    """

    slug: str
    name: str
    lat: float
    lng: float
    radius: float = 500.0
    deadline: datetime | None = None
    # deadline パース不能時のエラーメッセージ表示用に原文を保持する
    deadline_raw: object = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Self:
        """API 応答の spot dict から正規化済みの Spot を構築する。

        checkin_radius の欠落は UI 既定と同じ 500m に丸める。座標や半径が
        有限な数値でない・座標が地球の緯度経度範囲外・半径が非正の場合は
        `ValueError` で境界検知する (走行ロジックが黙って壊れないため)。
        """
        lat = _coerce_finite_float(raw["location_latitude"], name="location_latitude")
        lng = _coerce_finite_float(raw["location_longitude"], name="location_longitude")
        if not (_LAT_MIN <= lat <= _LAT_MAX):
            msg = f"location_latitude が緯度の範囲外: {lat}"
            raise ValueError(msg)
        if not (_LNG_MIN <= lng <= _LNG_MAX):
            msg = f"location_longitude が経度の範囲外: {lng}"
            raise ValueError(msg)
        # 既定 500m は `checkin_radius` キー欠落 / null のときだけ適用する。
        # `0` / `False` / `""` などの falsy 値は「値はあるが不正」なので
        # _coerce_finite_float に通して境界検知に流す (bool → TypeError、
        # 変換不能 → TypeError、非有限 → ValueError、以下で 0 以下 → ValueError)。
        raw_radius = raw.get("checkin_radius")
        radius = (
            500.0
            if raw_radius is None
            else _coerce_finite_float(raw_radius, name="checkin_radius")
        )
        if radius <= 0:
            msg = f"checkin_radius が正の値でない: {radius}"
            raise ValueError(msg)
        return cls(
            slug=str(raw["slug"]),
            name=str(raw.get("name") or ""),
            lat=lat,
            lng=lng,
            radius=radius,
            deadline=parse_checkin_deadline(raw),
            deadline_raw=raw.get("checkin_end_datetime"),
        )


def order_spots_by_proximity(
    spots: list[Spot],
    start_index: int | None = None,
    start_location: tuple[float, float] | None = None,
) -> list[Spot]:
    """最近傍法でスポット順序を決める。

    - `start_location` を指定すれば、そこに最も近いスポットを 1 件目にする
      (state.json の前回位置を渡す想定)。
    - `start_location` と `start_index` が両方 None なら開始スポットを乱択する。
    - 単純な greedy TSP 近似で、最適ではないが「近場をまとめて回る」自然な挙動になる。
    """
    if not spots:
        return []
    unvisited = list(spots)

    if start_location is not None:
        current = _pop_nearest(unvisited, *start_location)
    else:
        if start_index is None:
            # 開始スポットの乱択で暗号用途ではない
            start_index = random.randrange(len(unvisited))  # noqa: S311
        current = unvisited.pop(start_index)

    ordered = [current]
    while unvisited:
        current = _pop_nearest(unvisited, current.lat, current.lng)
        ordered.append(current)
    return ordered


def _pop_nearest(unvisited: list[Spot], lat: float, lng: float) -> Spot:
    """未訪問リストから (lat, lng) に最も近いスポットを取り除いて返す。

    同距離の場合はリスト前方を優先する (min の最初一致)。
    """
    nearest_idx = min(
        range(len(unvisited)),
        key=lambda i: _distance_m(lat, lng, unvisited[i].lat, unvisited[i].lng),
    )
    return unvisited.pop(nearest_idx)


@dataclass(kw_only=True)
class CheckinSettings:
    """collect_checkins の動作設定。

    `dry_run=True` は完全ドライラン (POST を送らず、sleep もなく、state も
    書き換えない)。デフォルト (`dry_run=False`) は本番実行。`now_fn` と
    `sleep_fn` はテスト時に差し替えるための現在時刻取得・実待機関数で、runner は
    モジュール global の time.sleep を直接呼ばない。
    """

    dry_run: bool = False
    daily_budget: int = 0
    consecutive_failure_limit: int = 1
    out_of_range_limit: int = 3
    profile_dir: Path | None = None
    now_fn: Callable[[], datetime] = _default_now
    sleep_fn: Callable[[float], None] = time.sleep


def _fetch_checkin_spots(page: Page) -> list[Spot]:
    """チェックインイベントの spot 一覧を取得し `Spot` へ正規化する。

    応答が不正なら RuntimeError。以降の走行ロジックには生 dict を流さない。
    """
    listing = call_checkin_api(page, "GET", f"/event/{CHECKIN_EVENT_SLUG}")
    payload = _success_payload_or_raise(listing, "チェックインイベント取得に失敗")
    raw_spots = cast("list[dict[str, Any]]", payload.get("spots", []))
    return [Spot.from_api(raw) for raw in raw_spots]


def _load_resume_context(
    settings: CheckinSettings,
) -> tuple[float | None, float | None, datetime | None, set[str]]:
    """state.json から resume 情報を読む。破損時は本番実行なら fail closed する。"""
    if settings.profile_dir is None:
        return None, None, None, set()
    try:
        return resume_context(settings.profile_dir, strict=not settings.dry_run)
    except StateFileCorruptedError as e:
        msg = (
            f"state.json が破損しています: {e}。手動で確認してから再実行してください。"
        )
        if not settings.dry_run:
            raise FailClosedError(msg) from e
        # dry-run は空 state のまま継続してスポット取得後の検証を回す
        print(msg, file=sys.stderr)
        return None, None, None, set()


def _partition_spots(
    all_spots: list[Spot], completed_spots: set[str]
) -> tuple[list[Spot], int]:
    """完了済みを除外した spot リストと、事前 skip した件数を返す。"""
    skipped = [s for s in all_spots if s.slug in completed_spots]
    if skipped:
        print(f"事前 skip (完了済み): {len(skipped)}件")
    return [s for s in all_spots if s.slug not in completed_spots], len(skipped)


def _announce_checkin_plan(
    settings: CheckinSettings,
    spots: list[Spot],
    total_spots: int,
    skipped: int,
) -> None:
    """実行前のヘッダ情報を出力する。"""
    budget_label = (
        "無制限" if settings.daily_budget <= 0 else f"{settings.daily_budget}件"
    )
    travel_backend = (
        "gmaps (公共交通)"
        if _get_gmaps_client() is not None
        else "haversine (自前計算)"
    )
    mode_label = "本番" if not settings.dry_run else "DRY-RUN (POST送信なし)"
    print(
        f"チェックイン対象スポット: {len(spots)}件"
        f" (全 {total_spots}, 完了済み {skipped})"
    )
    print(f"モード: {mode_label}")
    print(f"移動時間バックエンド: {travel_backend}")
    print(
        f"実POST試行 上限: {budget_label} / 連続失敗中断: "
        f"{settings.consecutive_failure_limit}件"
    )
    print(f"開始スポット: {spots[0].slug} {spots[0].name}")


def _initial_virtual_now(
    settings: CheckinSettings, resume_at: datetime | None
) -> datetime:
    """開始時の仮想時刻を決める。

    「前回 23:50 に終わって翌日 12:00 に再開」を自然な連続扱いにするため、
    resume_at が現在時刻より進んでいれば resume_at を採用する。
    """
    virtual_now = _as_jst_aware(settings.now_fn())
    if resume_at is not None and resume_at > virtual_now:
        virtual_now = resume_at
    return virtual_now


@dataclass(frozen=True, slots=True, kw_only=True)
class _TravelPlan:
    """1 スポット分の移動計画。

    実 sleep 前に deadline 判定へ渡せるよう、計算結果だけを保持する不変値。
    `_apply_travel_plan` で消化するまで `virtual_now` は進めない。
    """

    wait_seconds: float
    arrival: datetime
    mode: str
    straight_km: float
    deferred_seconds: float


@dataclass(kw_only=True)
class _CheckinRunner:
    """チェックイン走行の可変状態と 1 スポットずつの手続きを束ねる。

    `collect_checkins` から 1 実行につき 1 個生成して使い捨てる。カウンタの意味:
      - attempted: dry_run=False (本番) でのみ加算する実 POST 試行回数
      - successful: チェックイン成功 (dry-run では見込み) の件数
    """

    page: Page
    settings: CheckinSettings
    spots: list[Spot]
    virtual_now: datetime
    prev_lat: float | None = None
    prev_lng: float | None = None
    gained: int = 0
    successful: int = 0
    attempted: int = 0
    consecutive_failures: int = 0
    out_of_range_count: int = 0
    # 走行中の未処理スポット。skip 後は origin から動的に最近傍を選び直すため
    # `self.spots` (初期の静的順序、表示上の total) とは別で保持する。
    _remaining: list[Spot] = field(init=False, default_factory=list[Spot])

    def run(self) -> int:
        """全スポットを順に処理し、獲得票数 (dry-run は見込み) を返す。

        各 iteration で残り spot から `prev` に最も近いものを動的に pick する。
        deadline や到着不能で skip した spot は origin を進めないため、次スポットは
        「実際に行った場所」からの最近傍で再選択される。静的順序が持ってしまう
        「行かなかった遠方 spot の隣を無駄に訪れる」問題を回避する目的。
        """
        self._remaining = list(self.spots)
        index = 0
        while self._remaining:
            index += 1
            if self._budget_reached():
                print(
                    f"  日次上限 {self.settings.daily_budget}件に到達。"
                    f"残り {len(self._remaining)}件は次回以降。"
                )
                break
            # first spot (prev 未確定) は事前計算の順序 (`order_spots_by_proximity`
            # で開始位置に近い順) の先頭を採用。以降は現在地点からの最近傍を選ぶ。
            if self.prev_lat is None or self.prev_lng is None:
                spot = self._remaining.pop(0)
            else:
                spot = _pop_nearest(self._remaining, self.prev_lat, self.prev_lng)
            # 段 1: travel estimation を発生させる前の deadline 判定。
            # パース不能・現在時点で期限切れの spot は、gmaps API 呼び出しも
            # 実 sleep も起こさずに skip / fail-closed する。
            if not self._deadline_ok_before_travel(spot):
                continue
            plan = self._plan_travel_to(spot)
            # 段 2: 到着予定時刻が期限を超える spot は、travel estimation 済みだが
            # 実 sleep 前に skip する (sleep も origin 更新も発生させない)。
            if plan is not None and not self._deadline_ok_after_travel(
                spot, plan.arrival
            ):
                continue
            if plan is not None:
                self._apply_travel_plan(plan)
            self._attempt(spot, index)
        self._print_footer()
        return self.gained

    def _budget_reached(self) -> bool:
        """日次上限に達したかを判定する。

        上限判定のカウンタを dry_run の状態で切替える。本番 (dry_run=False) の
        attempted は「実 POST を厳密に daily_budget 回だけ送る」ためのゲートで、
        既達成・範囲外・失敗のいずれも 1 リクエスト = 1 消費として扱う。
        """
        if self.settings.daily_budget <= 0:
            return False
        counter = self.attempted if not self.settings.dry_run else self.successful
        return counter >= self.settings.daily_budget

    def _move_origin_to(self, spot: Spot) -> None:
        """次スポットの移動起点を spot の座標へ進める。"""
        self.prev_lat = spot.lat
        self.prev_lng = spot.lng

    def _plan_travel_to(self, spot: Spot) -> _TravelPlan | None:
        """前スポットからの移動計画を組み立てる。

        sleep はせず `virtual_now` も進めない。first spot (prev がまだ無い)
        なら None を返し、呼び出し側で「移動なし = 到着予定は現在時刻」として
        deadline 判定へ渡す。
        """
        if self.prev_lat is None or self.prev_lng is None:
            return None
        secs, mode = estimate_travel_seconds(
            self.prev_lat,
            self.prev_lng,
            spot.lat,
            spot.lng,
            departure_time=self.virtual_now,
        )
        if mode == "gmaps-transit":
            # transit の duration には始発待ち等が織り込まれているため、翌朝発への
            # 押し戻しをかけると二重加算になる。
            arrival = self.virtual_now + timedelta(seconds=secs)
        else:
            # haversine 系と gmaps-driving は運行時刻を含まない所要時間なので、
            # 「人間は深夜に移動しない」想定へ寄せる押し戻しを適用する
            # (driving の実測 duration に対しても意図的な自然化)。
            arrival = next_arrival_time(self.virtual_now, secs)
        wait_seconds = (arrival - self.virtual_now).total_seconds()
        straight_km = (
            _distance_m(self.prev_lat, self.prev_lng, spot.lat, spot.lng) / 1000
        )
        return _TravelPlan(
            wait_seconds=wait_seconds,
            arrival=arrival,
            mode=mode,
            straight_km=straight_km,
            deferred_seconds=wait_seconds - secs,
        )

    def _apply_travel_plan(self, plan: _TravelPlan) -> None:
        """移動計画に沿って実 sleep して仮想時刻を進める。

        deadline OK 判定を経て「この spot へ実際に行く」と決めてから呼ぶ。
        dry_run=True (dry-run) では実 sleep せず、仮想時刻だけ進める。
        """
        deferred_note = (
            f", 翌朝発に押戻し +{humanize_duration(plan.deferred_seconds)}"
            if plan.deferred_seconds > 60
            else ""
        )
        print(
            f"  移動待機: {humanize_duration(plan.wait_seconds)} ({plan.mode},"
            f" 直線 {plan.straight_km:.1f}km{deferred_note})"
            f" -> 到着 {plan.arrival:%m/%d %H:%M}"
        )
        if not self.settings.dry_run:
            self.settings.sleep_fn(plan.wait_seconds)
        self.virtual_now = plan.arrival

    def _deadline_ok_before_travel(self, spot: Spot) -> bool:
        """移動計画を組む前の deadline 判定 (パース不能・現在時点期限切れ)。

        ここで skip / fail-closed に落とせば、travel estimation (gmaps API
        呼び出し含む) も実 sleep も避けられる。純粋判定に近い副作用 (print) のみ。

        パース不能 (deadline=None) は本番 (dry_run=False) では fail closed に落として、
        サーバ形式変更を早期検知する (個別 skip では気付きにくい)。
        """
        slug = spot.slug
        deadline = spot.deadline
        if deadline is None:
            msg = (
                f"[{slug}] checkin_end_datetime = {spot.deadline_raw!r} "
                "がパースできません。サーバ側の日付形式が変わった可能性があります。"
            )
            if not self.settings.dry_run:
                raise FailClosedError(msg, partial_gained=self.gained)
            print(f"  {msg} (dry-run: skip)", file=sys.stderr)
            return False
        if self.virtual_now > deadline:
            print(f"  [{slug}] スポット期限 ({deadline:%m/%d %H:%M %Z}) 経過、skip。")
            return False
        return True

    def _deadline_ok_after_travel(self, spot: Spot, planned_arrival: datetime) -> bool:
        """移動計画を組んだ後の deadline 判定 (到着予定時刻の超過)。

        呼び出し側で `_deadline_ok_before_travel` を通過している前提。deadline は
        非 None かつ現在時点で期限内であることが保証されているため、ここでは
        到着予定超過だけ判定する。

        skip 時に prev_lat/lng は更新しない: 実 sleep を走らせていない spot に
        origin を進めると、次スポットの travel wait を過小評価する。origin は
        `_on_success` など「実際に到達したうえで結果として skip」の経路でのみ更新する。
        """
        slug = spot.slug
        # 呼び出し側で _deadline_ok_before_travel を通過している契約なので
        # deadline は None ではない。narrowing は cast で明示する。
        deadline = cast("datetime", spot.deadline)
        if planned_arrival > deadline:
            print(
                f"  [{slug}] 到着予定 {planned_arrival:%m/%d %H:%M}"
                f" が期限 ({deadline:%m/%d %H:%M %Z}) 超過、skip。"
            )
            return False
        return True

    def _will_continue_after(self) -> bool:
        """このスポットの後にループを継続するかを判定する。

        呼び出し時点で attempted・successful は加算済み、`_remaining` からは
        当該 spot が pop 済みという前提。dry_run の True と False で参照する
        カウンタが違う点だけ注意する。
        """
        if not self._remaining:
            return False
        if self.settings.daily_budget <= 0:
            return True
        counter = self.attempted if not self.settings.dry_run else self.successful
        return counter < self.settings.daily_budget

    def _attempt(self, spot: Spot, index: int) -> None:
        """1 スポット分のチェックインを dry-run または実 POST で処理する。"""
        slug = spot.slug
        coords = make_checkin_coords(spot)
        distance_m = _distance_m(
            spot.lat, spot.lng, coords["latitude"], coords["longitude"]
        )
        body = encrypt_coords(coords)

        print(
            f"[{index:3}/{len(self.spots)}] {slug} {spot.name}"
            f" (offset {distance_m:.1f}m, acc={coords['accuracy']}m,"
            f" alt={coords['altitude']})"
        )

        stay_secs = natural_stay_seconds()
        if self.settings.dry_run:
            self._simulate(spot, body, stay_secs)
            return

        # POST 直前に attempted を加算する。これで既達成・範囲外・失敗のいずれも
        # 1 リクエスト = 1 消費になり、daily_budget が実 POST 試行回数として厳密に働く。
        self.attempted += 1
        res = call_checkin_api(
            self.page,
            "POST",
            f"/event/{CHECKIN_EVENT_SLUG}/spot/{slug}/checkin",
            body=body,
        )
        ecode = _extract_ecode(res.get("body"))
        if _is_success_response(res):
            self._on_success(spot, stay_secs)
        elif ecode == ECODE_OUT_OF_RANGE:
            self._on_out_of_range(spot, ecode)
        elif ecode in ECODES_ALREADY_DONE:
            self._on_already_done(spot, ecode)
        else:
            self._on_unknown_ecode(spot, res)

    def _simulate(self, spot: Spot, body: str, stay_secs: float) -> None:
        """dry-run の 1 スポット分。state.json は書かない。

        実 POST 実行時の resume 起点をドライラン由来の値で汚染しないため。
        """
        print(f"       body={body[:60]}...(len={len(body)})  [DRY-RUN]")
        self.gained += 10
        self.successful += 1
        self._move_origin_to(spot)
        if self._will_continue_after():
            self.virtual_now = self.virtual_now + timedelta(seconds=stay_secs)
            print(
                f"       滞在 {humanize_duration(stay_secs)}"
                f" -> 出発 {self.virtual_now:%m/%d %H:%M}"
            )

    def _on_success(self, spot: Spot, stay_secs: float) -> None:
        """実 POST 成功の後処理。state 保存と滞在 sleep を行う。"""
        print("       -> 成功")
        self.gained += 10
        self.successful += 1
        self.consecutive_failures = 0
        self._move_origin_to(spot)
        self.virtual_now = _as_jst_aware(self.settings.now_fn())
        # sleep 中に中断されても completed_spots に slug が残るよう、成功直後に
        # 一次 state を保存する。
        if self.settings.profile_dir is not None:
            update_checkin_state(self.settings.profile_dir, spot, self.virtual_now)
        if self._will_continue_after():
            self.settings.sleep_fn(stay_secs)
            self.virtual_now = self.virtual_now + timedelta(seconds=stay_secs)
            print(
                f"       滞在 {humanize_duration(stay_secs)}"
                f" -> 出発 {self.virtual_now:%m/%d %H:%M %Z}"
            )
            # 滞在後の virtual_now を state に反映する (同じ spot への上書き相当)
            if self.settings.profile_dir is not None:
                update_checkin_state(self.settings.profile_dir, spot, self.virtual_now)

    def _on_out_of_range(self, spot: Spot, ecode: str) -> None:
        """E5005 (範囲外) の後処理。累積が閾値に達したら fail closed で停止する。"""
        self.out_of_range_count += 1
        limit = self.settings.out_of_range_limit
        print(
            f"       -> 範囲外 ({ecode})、スキップ "
            f"(累積 {self.out_of_range_count}/{limit})"
        )
        self.consecutive_failures = 0
        if self.out_of_range_count >= limit:
            # E5005 の多発は crypto・body・radius の実装不一致を強く示唆する。
            # 未指定なら 51 件全部を撃ってしまうため、ここで全体を止める。
            msg = (
                f"範囲外 (E5005) が {self.out_of_range_count} 件。"
                "crypto・body・radius の実装不一致の疑いがあるため停止する。"
                "座標計算とペイロードの整合性を確認してから"
                " --out-of-range-limit を上げて再実行する。"
            )
            raise FailClosedError(msg, partial_gained=self.gained)
        self._move_origin_to(spot)

    def _on_already_done(self, spot: Spot, ecode: str | None) -> None:
        """既達成 ecode の後処理。サーバ側成功済みなので完了扱いで state に反映する。

        ECODES_ALREADY_DONE が空 tuple の間は到達しない拡張点。実観測で意味が
        確定した ecode を追加した時点から有効になる。
        """
        print(f"       -> 既達成 ({ecode})、スキップ")
        self.consecutive_failures = 0
        self._move_origin_to(spot)
        # サーバ側で成功済みなので state に反映しておくと次回以降 skip される
        if self.settings.profile_dir is not None:
            update_checkin_state(
                self.settings.profile_dir, spot, self.virtual_now, mark_completed=True
            )

    def _on_unknown_ecode(self, spot: Spot, res: dict[str, Any]) -> None:
        """未観測 ecode = unknown の後処理。閾値到達で fail closed に中断する。

        BAN シグナル・認証切れ・予期せぬ状態のいずれかとして扱う。意味が確定したら
        ECODE_* に追加してから再実行する。
        """
        self.consecutive_failures += 1
        err_note = f" err={res.get('error')}" if res.get("error") else ""
        print(
            f"       -> 未観測ecode (連続{self.consecutive_failures}件目): "
            f"HTTP {res['status']}{err_note} body={res.get('body')}",
            file=sys.stderr,
        )
        if self.consecutive_failures >= self.settings.consecutive_failure_limit:
            msg = (
                f"連続失敗が {self.settings.consecutive_failure_limit}件に"
                "達したため中断する。"
                "body の ecode を確認して意味を確定してから再実行する。"
            )
            raise FailClosedError(msg, partial_gained=self.gained)
        # skip 時も移動時間は消費済み。prev を更新しないと clock は失敗スポット
        # 到着時刻・origin は前スポットという物理的にあり得ない状態になる。
        self._move_origin_to(spot)

    def _print_footer(self) -> None:
        """獲得サマリ行を出力する。"""
        label = "獲得見込み" if self.settings.dry_run else "獲得"
        footer = f"{self.successful}スポット成功"
        if not self.settings.dry_run:
            footer += f", 実POST試行 {self.attempted}件"
        footer += f", 仮想終了時刻 {self.virtual_now:%m/%d %H:%M}"
        print(f"{label}: 約{self.gained}票 ({footer})")


def collect_checkins(page: Page, settings: CheckinSettings) -> int:
    """全スポットに対してチェックインを試みる。戻り値は獲得票数の見込み。

    セーフティ:
      - `settings.dry_run=True` は完全ドライラン (POST を送らず、sleep もなく、
        state も書き換えない)。デフォルト (`dry_run=False`) は本番実行。
      - `state.completed_spots` にある slug は事前に skip する。
      - 未観測 ecode (SUCCESS と E5005 以外) は fail closed で即停止する。
        BAN シグナル・認証切れ・予期せぬ状態のいずれかとして扱う。
      - `consecutive_failure_limit` の未知エラーが連続したら全体中断する
        (デフォルト 1)。
      - `daily_budget > 0` で件数上限を設ける (0 は無制限)。
      - スポット間には交通機関稼働時間帯 (06:00-24:00) を考慮した移動待機を挟む。
      - `checkin_end_datetime` を過ぎたスポットは skip する (イベント全体期限とは別)。
      - deadline がパースできないスポットは本番 (`dry_run=False`) の場合に
        fail closed で扱う。
      - 各スポットで 10〜30 分の滞在時間を挟む (最終スポットや budget 到達時は skip)。
      - state.json は実 POST 成功時に限り atomic に更新する。
      - state.json 破損時は本番 (`dry_run=False`) で FailClosedError を送出し、
        dry-run では空 state で継続する。
      - `out_of_range_limit` 件以上 E5005 が続いたら停止する (crypto・body・radius
        の不一致が疑われるため、実 POST を 51 件全部撃たせない)。
    """
    all_spots = _fetch_checkin_spots(page)
    if not all_spots:
        print("チェックイン対象スポットが空でした。")
        return 0

    resume_lat, resume_lng, resume_at, completed_spots = _load_resume_context(settings)

    spots, skipped = _partition_spots(all_spots, completed_spots)
    if not spots:
        print("全スポット完了済みです。")
        return 0

    start_loc = (
        (resume_lat, resume_lng)
        if resume_lat is not None and resume_lng is not None
        else None
    )
    spots = order_spots_by_proximity(spots, start_location=start_loc)
    _announce_checkin_plan(settings, spots, len(all_spots), skipped)

    virtual_now = _initial_virtual_now(settings, resume_at)
    resumed = start_loc is not None
    print(
        f"開始時刻(仮想): {virtual_now:%Y-%m-%d %H:%M %Z}"
        + (f" (前回位置から再開: {resume_lat:.4f},{resume_lng:.4f})" if resumed else "")
    )

    runner = _CheckinRunner(
        page=page,
        settings=settings,
        spots=spots,
        virtual_now=virtual_now,
        prev_lat=resume_lat if resumed else None,
        prev_lng=resume_lng if resumed else None,
    )
    return runner.run()


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """2 点間の距離 [m] を Haversine 近似で計算する。"""
    earth_radius_m = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * earth_radius_m * math.asin(math.sqrt(a))


# 直線距離を実道路距離へ補正する係数
ROAD_DISTANCE_FACTOR = 1.35

# 距離レンジ別の移動手段テーブル。(道路距離の上限 [m], 平均速度 [m/h],
# 乗換・搭乗などの固定オーバーヘッド [秒], 手段名) の順で、上から順に最初に
# 該当したバンドを使う。最終バンドの上限は無限大で全距離を受ける。
_TRAVEL_SPEED_BANDS: tuple[tuple[float, float, float, str], ...] = (
    (500, 5_000, 0, "walk"),
    (30_000, 40_000, 5 * 60, "car/local"),
    (500_000, 200_000, 30 * 60, "shinkansen"),
    (float("inf"), 500_000, 90 * 60, "flight"),
)

# gmaps キャッシュ。キーの departure_time は 30 分単位に丸めてヒット率を確保する。
_GMAPS_CACHE: dict[tuple[float, float, float, float, str], tuple[float, str]] = {}
_GMAPS_TIME_BUCKET_MINUTES = 30


@functools.cache
def _get_gmaps_client() -> googlemaps.Client | None:
    """初回呼び出しで生成し、以後はキャッシュを返す。GMAPS_KEY 未設定なら None。"""
    key = os.environ.get("GMAPS_KEY")
    if not key:
        return None
    try:
        return googlemaps.Client(key=key, timeout=15)
    # ライブラリが送出しうる例外を列挙できないため、初期化失敗は広く握って
    # Haversine フォールバックへ丸める
    except Exception as e:  # noqa: BLE001
        print(
            f"Google Maps クライアント初期化失敗: {e}。"
            "Haversine にフォールバックする。",
            file=sys.stderr,
        )
        return None


def _directions_driving_fallback(
    client: googlemaps.Client,
    origin: tuple[float, float],
    dest: tuple[float, float],
    depart_arg: datetime | str,
) -> tuple[float, str] | None:
    """経路が transit で見つからない場合の driving 再試行。失敗時は None を返す。"""
    try:
        result = client.directions(
            origin,
            dest,
            mode="driving",
            departure_time=depart_arg,
            language="ja",
        )
    # API 失敗は呼び出し側の Haversine フォールバックへ丸めるため広く握る
    except Exception as e:  # noqa: BLE001
        print(f"  gmaps driving 再試行失敗: {e}", file=sys.stderr)
        return None
    if not result:
        return None
    leg = result[0]["legs"][0]
    # driving + 未来 departure_time + 経由地なしの前提を満たしているので、
    # 提供されれば traffic 反映値の duration_in_traffic を優先する。
    # https://developers.google.com/maps/documentation/directions/get-directions
    duration_field = leg.get("duration_in_traffic") or leg["duration"]
    return float(duration_field["value"]), "gmaps-driving"


def _estimate_travel_seconds_gmaps(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
    departure_time: datetime,
) -> tuple[float, str] | None:
    """Google Maps Directions API で公共交通機関の実移動時間を取得する。

    `departure_time` を渡すと、深夜出発時の始発待ち込み duration が Google 側から返る
    (例: 22 時に東京→札幌の transit なら「翌朝始発 + 移動」)。キー未設定や
    API エラー時は None を返し、呼び出し側で Haversine にフォールバックする。
    """
    client = _get_gmaps_client()
    if client is None:
        return None
    departure_time = _as_jst_aware(departure_time)
    bucket_minute = (
        departure_time.minute // _GMAPS_TIME_BUCKET_MINUTES
    ) * _GMAPS_TIME_BUCKET_MINUTES
    bucketed = departure_time.replace(minute=bucket_minute, second=0, microsecond=0)
    cache_key = (
        round(lat1, 4),
        round(lng1, 4),
        round(lat2, 4),
        round(lng2, 4),
        bucketed.isoformat(),
    )
    if cache_key in _GMAPS_CACHE:
        return _GMAPS_CACHE[cache_key]
    try:
        # departure_time を過去時刻にすると 400 になるため、過去なら "now" に
        # フォールバックする。
        real_now = datetime.now(departure_time.tzinfo)
        depart_arg: datetime | str = (
            departure_time if departure_time > real_now else "now"
        )
        result = client.directions(
            (lat1, lng1),
            (lat2, lng2),
            mode="transit",
            departure_time=depart_arg,
            language="ja",
            alternatives=False,
        )
    # API 失敗は呼び出し側の Haversine フォールバックへ丸めるため広く握る
    except Exception as e:  # noqa: BLE001
        print(f"  gmaps directions 失敗: {e}", file=sys.stderr)
        return None

    if result:
        leg = result[0]["legs"][0]
        pair: tuple[float, str] | None = (
            float(leg["duration"]["value"]),
            "gmaps-transit",
        )
    else:
        # transit で経路がない (深夜帯や公共交通が届かない場所) 場合は driving で再試行
        pair = _directions_driving_fallback(
            client, (lat1, lng1), (lat2, lng2), depart_arg
        )
    if pair is not None:
        _GMAPS_CACHE[cache_key] = pair
    return pair


def _estimate_travel_seconds_haversine(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> tuple[float, str]:
    """フォールバックの実装。Haversine と距離レンジ別平均速度で下限を推定する。

    Haversine 直線距離に `ROAD_DISTANCE_FACTOR` を掛けた値を実道路距離とみなし、
    `_TRAVEL_SPEED_BANDS` で手段を自動選択して、手段固有のオーバーヘッドを加える。
    """
    straight_m = _distance_m(lat1, lng1, lat2, lng2)
    road_m = straight_m * ROAD_DISTANCE_FACTOR
    for max_road_m, speed_m_per_h, overhead_sec, mode in _TRAVEL_SPEED_BANDS:
        if road_m < max_road_m:
            return road_m / (speed_m_per_h / 3600) + overhead_sec, mode
    msg = f"道路距離 {road_m}m がどの速度バンドにも該当しない"
    raise AssertionError(msg)


def estimate_travel_seconds(
    lat1: float,
    lng1: float,
    lat2: float,
    lng2: float,
    departure_time: datetime | None = None,
) -> tuple[float, str]:
    """2 点間の常識的な最短移動時間 [秒] と使用手段名を返す。

    `GMAPS_KEY` があれば Google Maps Directions API で実移動時間を取得し、
    `departure_time` を渡せば始発待ちや終電の運行時刻も加味された duration が
    返る。キー未設定や API 失敗時は Haversine + 距離レンジ別平均速度に
    フォールバックする。
    """
    if departure_time is None:
        departure_time = datetime.now(JST)
    gmaps_result = _estimate_travel_seconds_gmaps(
        lat1, lng1, lat2, lng2, departure_time=departure_time
    )
    if gmaps_result is not None:
        return gmaps_result
    return _estimate_travel_seconds_haversine(lat1, lng1, lat2, lng2)


def humanize_duration(seconds: float) -> str:
    """秒を人間が読める文字列 (1h32m 等) に変換する。"""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# 交通機関の稼働時間帯。深夜帯 (24:00-06:00) は JR も私鉄も動かず、車移動でも
# 睡眠時間帯にあたって不自然になるため移動不可扱いにする。
TRAVEL_ACTIVE_START_HOUR = 6
TRAVEL_ACTIVE_END_HOUR = 24  # 翌 0 時ちょうどを非稼働の開始として扱う

# 店に入り、チェックインし、買い物や食事をしてから次スポットへ、という自然な
# 滞在を模擬する
STAY_DURATION_MIN_SEC = 10 * 60
STAY_DURATION_MAX_SEC = 30 * 60


def natural_stay_seconds() -> float:
    """1 スポットあたりの滞在時間を [10 分, 30 分] の一様分布で返す。"""
    # 滞在時間の自然化用で暗号用途ではない
    return random.uniform(STAY_DURATION_MIN_SEC, STAY_DURATION_MAX_SEC)  # noqa: S311


def next_arrival_time(now: datetime, travel_seconds: float) -> datetime:
    """`now` から `travel_seconds` 移動した場合の現実的な到着時刻を返す。

    深夜帯 (24:00-06:00) は交通機関が動かず「駅で寝てから朝に再開」もできないため、
    今日中に到着できない旅は旅程ごと翌朝 06:00 発へ押し戻す。
    稼働枠 (06:00-24:00 の 18 時間) を超える長旅はどの日にも収まらないため、
    夜行便等で夜間も移動が続く連続移動として押し戻しなしで加算する。
    """
    if travel_seconds <= 0:
        return now

    cursor = now
    if cursor.hour < TRAVEL_ACTIVE_START_HOUR:
        cursor = cursor.replace(
            hour=TRAVEL_ACTIVE_START_HOUR, minute=0, second=0, microsecond=0
        )

    max_daily_travel_seconds = (
        TRAVEL_ACTIVE_END_HOUR - TRAVEL_ACTIVE_START_HOUR
    ) * 3600
    if travel_seconds > max_daily_travel_seconds:
        return cursor + timedelta(seconds=travel_seconds)

    while True:
        day_end = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        arrival = cursor + timedelta(seconds=travel_seconds)
        if arrival <= day_end:
            return arrival
        cursor = day_end.replace(hour=TRAVEL_ACTIVE_START_HOUR)


def parse_checkin_deadline(spot: dict[str, Any]) -> datetime | None:
    """`spot["checkin_end_datetime"]` を JST の aware datetime にパースする。

    失敗時は None。現行の `YYYY-MM-DD HH:MM:SS` (JST) から
    ISO8601 `Z`・offset 付きまで `datetime.fromisoformat` で一括受理する。
    naive は JST として扱う。パース失敗は呼び出し側で「fail closed = 期限判定
    できないなら実 POST を止める」扱いにする前提。
    """
    raw = spot.get("checkin_end_datetime")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return _as_jst_aware(parsed)


# --- state 永続化 ---
#
# アカウントごとに `profile_dir/canvasser_state.json` に前回終了状態を保存する。
#
# スキーマ:
#   {
#     "last_checkin": {
#       schema_version: 2,                          # 実 POST 成功時のみ書かれるマーカー
#       spot_slug, spot_name, location_latitude, location_longitude,
#       virtual_completed_at (ISO8601 JST-aware), real_completed_at (ISO8601 UTC)
#     },
#     "completed_spots": ["cg_vote2026_XX", ...]
#   }
#
# 旧版の dry-run 経路も state を書いていたため、last_checkin の中身だけでは実 POST 由来
# かどうか判別できない。LAST_CHECKIN_SCHEMA_VERSION は「実 POST 成功でだけ
# 書かれた」ことを保証するためのマーカーで、値が一致しないレコードは
# lat/lng/resume_at を無視する。
LAST_CHECKIN_SCHEMA_VERSION = 2
_STATE_FILENAME = "canvasser_state.json"
_CREDENTIALS_FILENAME = "credentials.json"
# login-init で「対話入力を保存 → 実ログインで検証」する間、既存の active
# credentials.json を上書きせずに退避する pending ファイル。SUCCESS を確認できた
# ときだけ active に昇格させる。
_CREDENTIALS_PENDING_FILENAME = "credentials.json.pending"

# BNID の連続ログイン失敗をこの回数まで許容し、超えたら disabled_until を書き込んで
# 一時停止する。BNID 側のアカウントロック閾値を刺激しないための緩めの上限。
CREDENTIALS_MAX_FAILURES = 3
# disabled_until が有効になる時間 (秒)。デフォルト 6 時間。
CREDENTIALS_DISABLE_WINDOW_SEC = 6 * 60 * 60


class StateFileCorruptedError(Exception):
    """state.json は読めるが JSON として壊れているケース。

    実行モードでは fail closed する。
    """


class FailClosedError(Exception):
    """実行モードで安全に継続できない状況で送出する。

    `process_account` でキャッチして exit_code=1 に反映することで、タスクスケジューラや
    運用ログ上でも「正常終了」に見えないようにする。

    partial_gained は、fail-closed 前にサーバ側で成功済みだった POST の reward
    を集計から落とさないために持たせる。通常経路の gained と合流させる責務は
    呼び出し側にある。
    """

    def __init__(self, msg: str, partial_gained: int = 0) -> None:
        """メッセージと、中断前に確定していた獲得票数を保持する。"""
        super().__init__(msg)
        self.partial_gained = partial_gained


class UserInputError(Exception):
    """CLI 引数などユーザー入力に起因するエラー。

    `main()` が短いメッセージと exit 1 に丸めるための専用クラス。実装バグとしての
    `ValueError` を丸め込まないよう、入力検証系はこの例外を使う。
    """


_SPOT_SLUG_RE = re.compile(r"^cg_vote2026_[0-9]{1,6}$")


def _validate_completed_spots(state: dict[str, Any], source: Path) -> None:
    """completed_spots の型と slug 形式を検証する。"""
    completed = state.get("completed_spots")
    if completed is None:
        return
    if not isinstance(completed, list):
        msg = f"{source}: completed_spots が list ではなく {type(completed).__name__}"
        raise StateFileCorruptedError(msg)
    for slug in cast("list[Any]", completed):
        if not isinstance(slug, str) or not _SPOT_SLUG_RE.fullmatch(slug):
            msg = f"{source}: completed_spots に不正な slug {slug!r}"
            raise StateFileCorruptedError(msg)


def _matches_last_checkin_kind(v: object, kind: str) -> bool:
    """last_checkin フィールドの期待型に v が合致するかを返す。

    bool は int の subclass のため素の `isinstance(v, int)` を通ってしまう。
    座標は `NaN` / `Infinity` を境界で潰したいので `math.isfinite` まで見る。
    `latitude` / `longitude` は加えて地球の緯度経度範囲まで検証する
    (`Spot.from_api` と同じ境界を state にも適用する)。
    """
    is_finite_num = (
        not isinstance(v, bool) and isinstance(v, int | float) and math.isfinite(v)
    )
    matches_by_kind: dict[str, bool] = {
        "int": not isinstance(v, bool) and isinstance(v, int),
        "str": isinstance(v, str),
        "finite_number": is_finite_num,
        "latitude": is_finite_num and _LAT_MIN <= cast("float", v) <= _LAT_MAX,
        "longitude": is_finite_num and _LNG_MIN <= cast("float", v) <= _LNG_MAX,
    }
    return matches_by_kind.get(kind, False)


def _validate_last_checkin(state: dict[str, Any], source: Path) -> None:
    """last_checkin の型・slug・時刻形式・座標の有限性と範囲を検証する。"""
    last = state.get("last_checkin")
    if last is None:
        return
    if not isinstance(last, dict):
        msg = f"{source}: last_checkin が dict でない"
        raise StateFileCorruptedError(msg)
    last_dict = cast("dict[str, Any]", last)
    for k, kind in (
        ("schema_version", "int"),
        ("spot_slug", "str"),
        ("spot_name", "str"),
        ("location_latitude", "latitude"),
        ("location_longitude", "longitude"),
        ("virtual_completed_at", "str"),
    ):
        v = last_dict.get(k)
        if v is None:
            continue  # 旧 schema 互換のため部分 dict は許容する
        if not _matches_last_checkin_kind(v, kind):
            msg = f"{source}: last_checkin.{k} の型が不正 ({kind} 期待): {v!r}"
            raise StateFileCorruptedError(msg)
    slug = last_dict.get("spot_slug")
    if isinstance(slug, str) and not _SPOT_SLUG_RE.fullmatch(slug):
        msg = (
            f"{source}: last_checkin.spot_slug {slug!r} が cg_vote2026_NNNN 形式でない"
        )
        raise StateFileCorruptedError(msg)
    vca = last_dict.get("virtual_completed_at")
    if isinstance(vca, str):
        try:
            datetime.fromisoformat(vca)
        except ValueError as e:
            msg = (
                f"{source}: last_checkin.virtual_completed_at {vca!r} が"
                f" ISO8601 として不正: {e}"
            )
            raise StateFileCorruptedError(msg) from e


def _validate_state_schema(state: dict[str, Any], source: Path) -> None:
    """読み込み時にスキーマを検証する。壊れていれば StateFileCorruptedError を送出する。

    strict モード専用のガード。dry-run では緩めに扱うため呼ばれない。
    """
    _validate_completed_spots(state, source)
    _validate_last_checkin(state, source)


def _reject_json_constant(name: str) -> float:
    """JSON の `-Infinity` / `Infinity` / `NaN` を拒否する `parse_constant`。

    Python の `json` は既定でこの 3 定数を float に変換する
    (https://docs.python.org/3.14/library/json.html) が、座標として意味を
    成さないため境界で `ValueError` に落として保存側と併せて閉じる。
    """
    msg = f"JSON に非有限値 {name} が含まれる"
    raise ValueError(msg)


def load_account_state(profile_dir: Path, *, strict: bool = False) -> dict[str, Any]:
    """`profile_dir/canvasser_state.json` を読み込む。

    strict=False は dry-run 用の緩め扱いで、パース失敗や型不一致でも空 dict を返す。
    strict=True は本番実行用で、破損時に `StateFileCorruptedError` を送出し、
    追加でスキーマ検証も行う。
    """
    state_file = profile_dir / _STATE_FILENAME
    if not state_file.exists():
        return {}
    try:
        data = json.loads(
            state_file.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, OSError, ValueError) as e:
        if strict:
            msg = f"{state_file}: {e}"
            raise StateFileCorruptedError(msg) from e
        return {}
    if not isinstance(data, dict):
        if strict:
            msg = f"{state_file}: トップレベルが dict でない"
            raise StateFileCorruptedError(msg)
        return {}
    state = cast("dict[str, Any]", data)
    if strict:
        _validate_state_schema(state, state_file)
    return state


def save_account_state(profile_dir: Path, state: dict[str, Any]) -> None:
    """`profile_dir/canvasser_state.json` に atomic に書き出す。

    一時ファイルへ書いて fsync してから `os.replace` で置換する。書き込み中に
    クラッシュしても既存ファイルは壊れない。
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_file = profile_dir / _STATE_FILENAME
    fd, tmp_path = tempfile.mkstemp(
        prefix=".canvasser_state-", suffix=".tmp", dir=str(profile_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # allow_nan=False で NaN/Infinity の書き出しを ValueError にする
            # (JSON 標準外の拡張出力を state に載せない)
            json.dump(state, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_path).replace(state_file)
    except Exception:
        # 失敗時は一時ファイルだけ掃除して例外を伝播する (state_file はそのまま残す)
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink()
        raise


@dataclass(kw_only=True)
class Credentials:
    """BNID の自動再ログインで使う資格情報。

    平文 JSON (`profiles/<account>/credentials.json`) に保存する。ファイル権限は
    POSIX で 0o600、Windows は icacls でカレントユーザー限定に絞る (best-effort)。

    - `saved_at`: 対話入力で保存した時刻 (JST ISO8601)。デバッグ・監査用で、
      自動再ログイン成否の書き戻しでは更新しない (資格情報が変わった時のみ更新)。
    - `failure_count`: 連続失敗回数。成功で 0 にリセットする。
    - `disabled_until`: `CREDENTIALS_MAX_FAILURES` に達した時に設定する JST
      ISO8601。この時刻までは自動再ログインをスキップし、BNID アカウントロックを
      刺激しない。
    """

    bnid_email: str
    bnid_password: str
    saved_at: str
    failure_count: int = 0
    disabled_until: str | None = None


def _credentials_file(profile_dir: Path) -> Path:
    """profile_dir 配下の active credentials.json パスを返す。"""
    return profile_dir / _CREDENTIALS_FILENAME


def _pending_credentials_file(profile_dir: Path) -> Path:
    """profile_dir 配下の pending (未検証) credentials.json.pending パスを返す。"""
    return profile_dir / _CREDENTIALS_PENDING_FILENAME


def _apply_credentials_permissions(path: Path) -> None:
    """credentials.json のファイル権限を最小限に絞る (best-effort)。

    POSIX は `os.chmod(path, 0o600)`。Windows は `icacls` で継承削除 +
    カレントユーザー限定に絞る。どちらも失敗しても致命的にはしない
    (資格情報の平文保存自体が第一の防壁ではないため)。
    """
    with contextlib.suppress(OSError):
        path.chmod(0o600)
    if os.name != "nt":
        return
    icacls = shutil.which("icacls")
    username = os.environ.get("USERNAME")
    if icacls is None or not username:
        return
    # icacls の失敗は非致命。git check-ignore と同じ例外集合で握り潰す。
    with contextlib.suppress(*_GIT_CHECK_IGNORE_EXCEPTIONS):
        subprocess.run(  # noqa: S603
            [
                icacls,
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{username}:F",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )


def _load_credentials_from(path: Path) -> Credentials | None:
    """指定パスから Credentials を fail-safe に読み込む共通実装。

    ファイル非存在は `None`。JSON パース失敗・型不一致は認証情報を無視するために
    `None` に丸め、stderr に警告を出す (認証情報そのものはログに出さない)。
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"[warn] {path} を読めません: {e}。認証情報を無視します。",
            file=sys.stderr,
        )
        return None
    if not isinstance(raw, dict):
        print(
            f"[warn] {path} のトップレベルが dict でありません。認証情報を無視します。",
            file=sys.stderr,
        )
        return None
    data = cast("dict[str, Any]", raw)
    email = data.get("bnid_email")
    password = data.get("bnid_password")
    if (
        not isinstance(email, str)
        or not isinstance(password, str)
        or not email
        or not password
    ):
        print(
            f"[warn] {path} に bnid_email / bnid_password が正しく入っていません。"
            "認証情報を無視します。",
            file=sys.stderr,
        )
        return None
    saved_at_raw = data.get("saved_at", "")
    saved_at = saved_at_raw if isinstance(saved_at_raw, str) else ""
    failure_count_raw = data.get("failure_count", 0)
    is_valid_int = isinstance(failure_count_raw, int) and not isinstance(
        failure_count_raw, bool
    )
    failure_count = failure_count_raw if is_valid_int else 0
    disabled_until_raw = data.get("disabled_until")
    disabled_until = disabled_until_raw if isinstance(disabled_until_raw, str) else None
    return Credentials(
        bnid_email=email,
        bnid_password=password,
        saved_at=saved_at,
        failure_count=failure_count,
        disabled_until=disabled_until,
    )


def _save_credentials_to(path: Path, credentials: Credentials) -> None:
    """指定パスに Credentials を atomic 書き出し + 権限縮小する共通実装。

    `save_account_state` と同じ「tempfile → fsync → replace」パターン。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".credentials-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                asdict(credentials),
                f,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_path).replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink()
        raise
    _apply_credentials_permissions(path)


def load_credentials(profile_dir: Path) -> Credentials | None:
    """`profile_dir/credentials.json` (active) を読み込む。"""
    return _load_credentials_from(_credentials_file(profile_dir))


def save_credentials(profile_dir: Path, credentials: Credentials) -> None:
    """`profile_dir/credentials.json` (active) に atomic に書き出す。"""
    _save_credentials_to(_credentials_file(profile_dir), credentials)


def load_pending_credentials(profile_dir: Path) -> Credentials | None:
    """`profile_dir/credentials.json.pending` (未検証) を読み込む。"""
    return _load_credentials_from(_pending_credentials_file(profile_dir))


def save_pending_credentials(profile_dir: Path, credentials: Credentials) -> None:
    """`profile_dir/credentials.json.pending` (未検証) に atomic に書き出す。

    実ログインで検証成功 (SUCCESS) するまで active を上書きしないための退避場所。
    ここに書いた時点では既存 active credentials は温存される。
    """
    _save_credentials_to(_pending_credentials_file(profile_dir), credentials)


def _activate_pending_credentials(profile_dir: Path) -> None:
    """Pending を active に atomic 置換して有効化する。

    login-init で auto_login が SUCCESS に達したときにのみ呼ぶ。既存 active は
    ここで pending に置き換わる。
    """
    pending = _pending_credentials_file(profile_dir)
    active = _credentials_file(profile_dir)
    pending.replace(active)
    _apply_credentials_permissions(active)


def _discard_pending_credentials(profile_dir: Path) -> None:
    """Pending credentials を破棄する (active は温存)。検証失敗時に呼ぶ。"""
    with contextlib.suppress(OSError):
        _pending_credentials_file(profile_dir).unlink()


def update_checkin_state(
    profile_dir: Path,
    spot: Spot,
    virtual_now: datetime,
    *,
    mark_completed: bool = True,
) -> None:
    """1 件チェックイン成功時に state を更新する。

    `mark_completed=True` の場合、`spot.slug` を `completed_spots` へ追加して
    次回起動時の事前 skip 対象にする。実行中の破損 state を空 dict で上書き
    してしまわないよう、読み込みは strict にする (本番経路からのみ呼ばれる)。
    """
    state = load_account_state(profile_dir, strict=True)
    state["last_checkin"] = {
        "schema_version": LAST_CHECKIN_SCHEMA_VERSION,
        "spot_slug": spot.slug,
        "spot_name": spot.name,
        "location_latitude": spot.lat,
        "location_longitude": spot.lng,
        "virtual_completed_at": virtual_now.isoformat(),
        "real_completed_at": datetime.now(UTC).isoformat(),
    }
    if mark_completed:
        completed = set(state.get("completed_spots") or [])
        completed.add(spot.slug)
        state["completed_spots"] = sorted(completed)
    save_account_state(profile_dir, state)


def resume_context(
    profile_dir: Path, *, strict: bool = False
) -> tuple[float | None, float | None, datetime | None, set[str]]:
    """state.json から前回位置・仮想終了時刻・完了済みスポット集合を復元する。

    旧版の dry-run 経路も `update_checkin_state` を叩いていたため、`last_checkin` から
    「実 POST 成功済み」を後方推定する手段がない。誤って dry-run 由来の slug を完了扱い
    すると次回のチェックイン実 POST で reward を落とすので、自動移行はしない。
    旧 state の補完が必要なら `mark-completed` を明示的に使う。
    """
    state = load_account_state(profile_dir, strict=strict)
    last = cast("dict[str, Any]", state.get("last_checkin") or {})
    # schema_version が一致しない last_checkin は「実 POST 成功由来か」を保証
    # できないため resume には使わない。旧 dry-run の simulated route を
    # 本番 run の起点にすると、偽の位置と時刻から始まって有効スポットを
    # skip する事故になる。
    schema_ok = last.get("schema_version") == LAST_CHECKIN_SCHEMA_VERSION
    lat = last.get("location_latitude") if schema_ok else None
    lng = last.get("location_longitude") if schema_ok else None
    raw = last.get("virtual_completed_at") if schema_ok else None
    resume_at: datetime | None = None
    if raw:
        with contextlib.suppress(ValueError):
            resume_at = _as_jst_aware(datetime.fromisoformat(raw))
    completed = set(state.get("completed_spots") or [])
    # 非 strict (dry-run) はスキーマ検証を通らないため、手改変で数値以外や
    # NaN/Infinity/範囲外の座標が入っていても ValueError にせず None
    # (resume 情報なし) に丸める。strict 経路とは違い、破損 dry-run 起動を
    # 単に「resume 位置なしの初回相当」として続行させる。
    return (
        _finite_float_in_range_or_none(lat, _LAT_MIN, _LAT_MAX),
        _finite_float_in_range_or_none(lng, _LNG_MIN, _LNG_MAX),
        resume_at,
        completed,
    )


def mark_spots_completed(profile_dir: Path, slugs: list[str]) -> None:
    """外部から手動で成功済みスポットを state に登録する CLI 用ヘルパー。

    別デバイスや UI キャプチャで既に checkin 済みの分を流し込むために使う。
    既存 state.json が破損している場合は `StateFileCorruptedError` を上げて、
    破損 state を空 dict で上書きしてしまうのを防ぐ。
    """
    invalid = [s for s in slugs if not _SPOT_SLUG_RE.fullmatch(s)]
    if invalid:
        msg = f"不正な spot_slug: {invalid}"
        raise UserInputError(msg)
    state = load_account_state(profile_dir, strict=True)
    completed = set(state.get("completed_spots") or [])
    completed.update(slugs)
    state["completed_spots"] = sorted(completed)
    save_account_state(profile_dir, state)
    print(f"[{profile_dir.name}] completed_spots に追加: {sorted(slugs)}")


def _install_chromium() -> None:
    """`playwright install chromium` を現在の Python 環境で実行する。"""
    subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def ensure_chromium_installed() -> None:
    """Chromium バイナリが未取得なら `playwright install chromium` を走らせる。

    `uv run` の初回起動から冪等に呼べるようにしておく。
    """
    with sync_playwright() as p:
        exe = p.chromium.executable_path
        if exe and Path(exe).exists():
            return

    print("Chromium バイナリを取得します (初回のみ)...", file=sys.stderr)
    _install_chromium()


def _prompt_credentials() -> tuple[str, str]:
    """対話で BNID メールアドレス・パスワードを取得する。

    パスワードは `getpass.getpass()` で echo 無効化する。空文字は
    `UserInputError` で拒否し、認証情報として使えない状態で保存へ進ませない。
    """
    print("BNID の認証情報を入力してください。", file=sys.stderr)
    email = input("メールアドレス: ").strip()
    if not email:
        msg = "メールアドレスが空です。login-init を中止します。"
        raise UserInputError(msg)
    password = getpass.getpass("パスワード: ")
    if not password:
        msg = "パスワードが空です。login-init を中止します。"
        raise UserInputError(msg)
    return email, password


def persist_login_init_credentials(profile_dir: Path) -> None:
    """login-init サブコマンド用: 対話入力 → pending credentials に保存する。

    active credentials.json は上書きしない。実ログインで検証成功 (SUCCESS) した
    ときにだけ `_activate_pending_credentials` で active に昇格させる。これで
    新パスワードのタイプミスによって旧 active credentials を失う事故を防ぐ。

    profile_dir は事前に mkdir 済みを前提とする。resume 状態や連続失敗ガードは
    「認証情報を入れ直した」ことになるため 0 / None にリセットして pending に書く。
    """
    email, password = _prompt_credentials()
    save_pending_credentials(
        profile_dir,
        Credentials(
            bnid_email=email,
            bnid_password=password,
            saved_at=datetime.now(JST).isoformat(),
        ),
    )
    print(
        f"認証情報を {_pending_credentials_file(profile_dir)} に一時保存しました。"
        "続けてブラウザで検証します (検証成功時のみ active に昇格します)。",
        file=sys.stderr,
    )


def _login_error_visible(page: Page) -> bool:
    """パスワード誤り等のエラー DOM が可視化されているか。

    Playwright の一時失敗は捕捉して False に丸める (未マウント要素の可視性判定は
    エラーになりうる)。
    """
    with contextlib.suppress(PlaywrightError):
        return page.locator(_LOGIN_ERROR_SEL).is_visible()
    return False


def _detect_login_captcha(page: Page) -> bool:
    """CAPTCHA / 2FA を示唆する iframe が挿入されているか。

    Phase 1 時点では検知なしだが、将来的な動的挿入に備えて監視する。
    どのセレクタでも 1 個以上マッチしたら True。
    """
    for sel in _LOGIN_CAPTCHA_SELECTORS:
        with contextlib.suppress(PlaywrightError):
            if page.locator(sel).count() > 0:
                return True
    return False


class AutoLoginOutcome(Enum):
    """auto_login の結果種別。

    タイムアウトのみリトライ対象になるため、呼び出し側で失敗理由を区別する。
    パスワード誤り / CAPTCHA は連続失敗ガードで failure_count には加算するが、
    再試行しても無駄なため即 abort する。
    """

    SUCCESS = "success"
    PASSWORD_ERROR = "password_error"  # noqa: S105 (state label, not credential)
    CAPTCHA_DETECTED = "captcha_detected"
    TIMEOUT = "timeout"
    FORM_ERROR = "form_error"


def auto_login(
    page: Page,
    credentials: Credentials,
    *,
    timeout_sec: int = 60,
    interval_sec: float = 1.0,
) -> tuple[AutoLoginOutcome, int]:
    """BNID ログイン画面でメール/パスワードを自動入力し (outcome, submit 回数) を返す。

    Phase 1 で判明した DOM 制約に準拠する:
    - `fill()` は disabled を外せないため `press_sequentially()` で実キー入力を装う。
    - `<form>` submit ではなく `#btn-idpw-login` の click で発火する
      (SPA 独自スクリプト側で送信するため Enter 送信は動作保証なし)。
    - HTTP ステータスは常に 200 なので、成功/失敗は「is_login フラグ vs エラー DOM」の
      race で判定する。

    submit 回数は failure_count の会計に使う。pre-submit で abort したケース
    (CAPTCHA 事前検知 / フォーム操作エラー) は BNID に届いていないので 0、
    click が成功して以降の結果 (SUCCESS / PASSWORD_ERROR / CAPTCHA_DETECTED /
    TIMEOUT) は 1 を返す。

    失敗パス:
    - PASSWORD_ERROR: `#error-input-area .c-message--warning` の可視化を検知。
      Username enumeration 対策のためメアド違いと PW 違いは区別できず、両方まとめて
      「認証情報のいずれかが不正」で abort する。submit=1。
    - CAPTCHA_DETECTED: 監視 selector にマッチ。pre-submit で検知した場合 submit=0、
      post-submit で挿入されたケースは submit=1。
    - TIMEOUT: `timeout_sec` を超えても is_login にならなければ TIMEOUT。submit=1。
      呼び出し側で 1 回だけリトライしてよい (一時的なネットワーク遅延の可能性)。
    - FORM_ERROR: フォーム操作中の PlaywrightError。DOM 変更やページ未ロードの可能性が
      あるためリトライしない。submit=0。
    """
    # submit 前に CAPTCHA / 2FA を検知する。BNID が最初から CAPTCHA を出している
    # ケース (連続失敗による動的挿入等) では、フォームに入力してから submit しても
    # 認証エラーで失敗するだけなので、パスワードを送信しないうちに abort する。
    if _detect_login_captcha(page):
        print(
            "[auto_login] submit 前に CAPTCHA/2FA を検知しました。"
            "`uv run canvasser.py login --account NAME` で手動ログインしてください。",
            file=sys.stderr,
        )
        return AutoLoginOutcome.CAPTCHA_DETECTED, 0

    # フォーム入力 (submit の 1 手前まで)。ここで失敗したら BNID に届いていないので
    # FORM_ERROR/submit=0。
    # `press_sequentially` は既存テキストに追記する仕様のため、Playwright の永続
    # コンテキスト側で残っている自動入力や前回の残骸を `fill("")` で明示クリアしてから
    # キー入力する。fill 自体は disabled 状態を解除しないので、Phase 1 制約の
    # 「実キー入力 (press_sequentially) が必要」は press_sequentially 側で満たす。
    try:
        page.locator(_LOGIN_MAIL_SEL).fill("")
        page.locator(_LOGIN_MAIL_SEL).press_sequentially(credentials.bnid_email)
        page.locator(_LOGIN_PASS_SEL).fill("")
        page.locator(_LOGIN_PASS_SEL).press_sequentially(credentials.bnid_password)
        # disabled が外れるまで待ってからクリックする (Phase 1 の必須手順)
        page.locator(_LOGIN_BTN_ENABLED_SEL).wait_for(timeout=5000)
    except PlaywrightError as e:
        print(f"[auto_login] フォーム操作でエラー: {e}", file=sys.stderr)
        return AutoLoginOutcome.FORM_ERROR, 0

    # click は別 try に分離する。click が起こす navigation を Playwright が待って
    # timeout した場合、パスワードは既に送信済みの可能性が高い。ここで FORM_ERROR に
    # 落とすと submit=0 で failure_count が加算されず、無限に BNID にパスワードを
    # 投げ続ける事故になる。`no_wait_after=True` で navigation 待機をそもそも切り、
    # それでも raise した場合は polling に進んで実結果 (SUCCESS/PASSWORD_ERROR/
    # CAPTCHA_DETECTED/TIMEOUT) で分類する (submit=1 で計上)。
    with contextlib.suppress(PlaywrightError):
        page.locator(_LOGIN_BTN_SEL).click(no_wait_after=True)

    outcome = _poll_login_outcome(
        page, timeout_sec=timeout_sec, interval_sec=interval_sec
    )
    return outcome, 1


def _poll_login_outcome(
    page: Page, *, timeout_sec: int, interval_sec: float
) -> AutoLoginOutcome:
    """Submit 済みログイン画面をポーリングし、SUCCESS / エラー / TIMEOUT を判定する。

    ページ内の状態変化 (is_login / エラー DOM / CAPTCHA) を interval_sec ごとに
    確認する。ログイン画面リダイレクト中の fetch は一時的に失敗するため
    PlaywrightError は握って次のポーリングに委ねる。
    """
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        with contextlib.suppress(PlaywrightError):
            if check_login(page):
                print("[auto_login] ログイン成功を検知しました。", file=sys.stderr)
                return AutoLoginOutcome.SUCCESS
            if _login_error_visible(page):
                print(
                    "[auto_login] BNID から認証エラーが返されました。"
                    "メールアドレスかパスワードが誤っている可能性があります。",
                    file=sys.stderr,
                )
                return AutoLoginOutcome.PASSWORD_ERROR
            if _detect_login_captcha(page):
                print(
                    "[auto_login] CAPTCHA/2FA を検知しました。"
                    "`uv run canvasser.py login --account NAME` で"
                    "手動ログインしてください。",
                    file=sys.stderr,
                )
                return AutoLoginOutcome.CAPTCHA_DETECTED
        time.sleep(interval_sec)

    print(
        "[auto_login] タイムアウト。ログイン結果を検知できませんでした。",
        file=sys.stderr,
    )
    return AutoLoginOutcome.TIMEOUT


def _credentials_disabled(credentials: Credentials, name: str) -> bool:
    """`disabled_until` が未来なら True (自動再ログインをスキップすべき)。

    非パース文字列や過去時刻は False (=有効) として扱い、fail-safe に倒す。
    未来ならユーザ向けに残時間を stderr に案内する。
    """
    if credentials.disabled_until is None:
        return False
    try:
        deadline = datetime.fromisoformat(credentials.disabled_until)
    except ValueError:
        return False
    deadline = _as_jst_aware(deadline)
    if datetime.now(JST) >= deadline:
        return False
    print(
        f"[{name}] 自動再ログインは {credentials.disabled_until} まで"
        "一時停止中です (連続失敗ガード)。",
        file=sys.stderr,
    )
    return True


def _reset_credentials_failure(profile_dir: Path, credentials: Credentials) -> None:
    """成功時の failure_count / disabled_until クリア。

    変更が無ければ書き込みしない (無駄な I/O と saved_at 保護)。
    """
    if credentials.failure_count == 0 and credentials.disabled_until is None:
        return
    save_credentials(
        profile_dir,
        Credentials(
            bnid_email=credentials.bnid_email,
            bnid_password=credentials.bnid_password,
            saved_at=credentials.saved_at,
            failure_count=0,
            disabled_until=None,
        ),
    )


def _record_credentials_failure(
    profile_dir: Path, credentials: Credentials, *, submissions: int = 1
) -> None:
    """失敗時に failure_count へ submissions を加算し、必要なら disabled_until を設定。

    `CREDENTIALS_MAX_FAILURES` に達したら BNID アカウントロックを避けるため
    `CREDENTIALS_DISABLE_WINDOW_SEC` 秒後まで自動再ログインを停止する。

    submissions は「実際に BNID にパスワードを送信した回数」。TIMEOUT リトライで
    2 回連続 submit が起きたケースでは 2 を渡し、pre-submit の FORM_ERROR (送信なし)
    ではそもそもこの関数を呼ばない (呼び出し側で判断する)。
    """
    new_count = credentials.failure_count + submissions
    new_disabled = credentials.disabled_until
    if new_count >= CREDENTIALS_MAX_FAILURES:
        window = timedelta(seconds=CREDENTIALS_DISABLE_WINDOW_SEC)
        new_disabled = (datetime.now(JST) + window).isoformat()
    save_credentials(
        profile_dir,
        Credentials(
            bnid_email=credentials.bnid_email,
            bnid_password=credentials.bnid_password,
            saved_at=credentials.saved_at,
            failure_count=new_count,
            disabled_until=new_disabled,
        ),
    )


def _retry_after_timeout(
    page: Page, name: str, credentials: Credentials
) -> tuple[AutoLoginOutcome, int]:
    """1 回目 TIMEOUT を受けてリトライを実行し、(outcome, 追加 submit 回数) を返す。

    リトライ用 goto の失敗、post-retry check_login の遅延成功、2 回目 auto_login
    の FORM_ERROR/TIMEOUT-late-success を扱う。呼び出し側で 1 回目 submit の 1
    を足して最終的な submit 回数にする。
    """
    # 1 回目 submit は既に消化済み (呼び出し元で failure_count += 1 予定)。リトライ
    # を投げると失敗ガード上限を超えて BNID にパスワードを送ってしまう可能性がある
    # ため、リトライ用 submit が予算内に収まるかを事前チェックする。
    # 目標: 「CREDENTIALS_MAX_FAILURES 回を超えて BNID にパスワードを送らない」。
    # 1 回目 submit 直後の見込み failure_count は credentials.failure_count + 1。
    # リトライで更に +1 されると MAX を超える場合は、リトライを控える。
    if credentials.failure_count + 1 >= CREDENTIALS_MAX_FAILURES:
        print(
            f"[{name}] failure_count が上限のため、タイムアウト後のリトライは"
            "控えます (BNID アカウントロック防止)。",
            file=sys.stderr,
        )
        return AutoLoginOutcome.TIMEOUT, 0

    print(f"[{name}] タイムアウトのため 1 回リトライします。", file=sys.stderr)
    try:
        page.goto(LOGIN_ENTRY_URL, wait_until="domcontentloaded")
    except PlaywrightError as e:
        print(
            f"[{name}] リトライ時の BNID ログイン画面への遷移で失敗: {e}",
            file=sys.stderr,
        )
        return AutoLoginOutcome.TIMEOUT, 0
    # check_login は redirect 進行中に PlaywrightError を投げうる。ここで例外が
    # 上流に escape すると、1 回目 submit の failure_count 加算が飛んで無限ループ
    # 気味に BNID にパスワードを投げ続ける事故になる。polling と同じく suppress する。
    with contextlib.suppress(PlaywrightError):
        if check_login(page):
            print(
                f"[{name}] タイムアウト後に遅延成功を検知しました。",
                file=sys.stderr,
            )
            return AutoLoginOutcome.SUCCESS, 0

    return _resolve_retry_outcome(page, name, credentials)


def _resolve_retry_outcome(
    page: Page, name: str, credentials: Credentials
) -> tuple[AutoLoginOutcome, int]:
    """リトライで 2 回目 auto_login を呼び、遅延成功も含めて outcome を確定する。"""
    outcome, submitted = auto_login(page, credentials)
    if outcome is AutoLoginOutcome.FORM_ERROR:
        # 2 回目は pre-submit で失敗 → submit は 1 回目のみ
        return AutoLoginOutcome.TIMEOUT, 0
    if outcome is AutoLoginOutcome.TIMEOUT:
        # check_login は redirect 中に PlaywrightError を投げうるので suppress する。
        # 失敗時は outcome=TIMEOUT のまま抜ける (失敗ガードは caller が計上する)。
        with contextlib.suppress(PlaywrightError):
            if check_login(page):
                print(
                    f"[{name}] リトライ後にも遅延成功を検知しました。",
                    file=sys.stderr,
                )
                return AutoLoginOutcome.SUCCESS, submitted
    return outcome, submitted


def _run_auto_login_sequence(
    page: Page, name: str, credentials: Credentials
) -> tuple[AutoLoginOutcome, int]:
    """auto_login を最大 2 回試して (最終 outcome, 実 submit 回数) を返す。

    TIMEOUT は 1 回だけリトライする (仕様。一時的なネットワーク遅延に備えて)。
    リトライは `LOGIN_ENTRY_URL` に再遷移してフォームをリセットしてから行う
    (`press_sequentially` が既存値に追記されるため)。

    submit 回数 (BNID にパスワードを送った回数) は、呼び出し側で failure_count
    に加算する量を決めるのに使う。BNID 側のアカウントロック閾値を刺激しないよう
    submit 回数を正確に計上する:

    - CAPTCHA/2FA 事前検知 (pre-submit): submit=0。
    - FORM_ERROR (pre-submit フォーム操作失敗): submit=0。BNID に届いていない。
    - SUCCESS/PASSWORD_ERROR/CAPTCHA_DETECTED (post-submit)/TIMEOUT: submit >= 1。
    - リトライ経路の詳細は `_retry_after_timeout` を参照。
    """
    outcome, submitted = auto_login(page, credentials)
    if outcome is not AutoLoginOutcome.TIMEOUT:
        return outcome, submitted
    retry_outcome, retry_submitted = _retry_after_timeout(page, name, credentials)
    return retry_outcome, submitted + retry_submitted


def attempt_auto_relogin(page: Page, profile_dir: Path, name: str) -> bool:
    """process_account の未ログインルートで呼ぶ自動再ログインゲート。

    credentials.json 非存在・disabled_until 有効なら即 False。BNID ログイン画面
    への初回遷移で失敗した場合は「認証情報を BNID に送っていない」ため
    failure_count には計上せず False を返す (ネットワーク不調で BNID 側のロックを
    刺激するのを避ける)。

    goto 直後には `check_login()` で「初回 check_login の false negative」を
    確認する。cookie が実は有効で mission page へリダイレクトされている場合、
    LOGIN_ENTRY_URL 遷移後も #mail が無く FORM_ERROR に落ちてしまうため、
    ここで検知して短絡する。

    auto_login の呼び出しは `_run_auto_login_sequence` に委譲し、実 submit 回数
    ぶんだけ failure_count を加算する。pre-submit の FORM_ERROR (BNID に届いて
    いない) はそもそも加算しない。
    """
    credentials = load_credentials(profile_dir)
    if credentials is None or _credentials_disabled(credentials, name):
        return False

    # BNID ログイン画面へ遷移 (OAuth リダイレクトチェーンは Chromium が追従する)。
    # goto 失敗は認証試行に至る前の一時的なネットワーク不調なので、failure_count は
    # 計上しない (BNID アカウントロックを刺激しないため)。
    try:
        page.goto(LOGIN_ENTRY_URL, wait_until="domcontentloaded")
    except PlaywrightError as e:
        print(
            f"[{name}] BNID ログイン画面への遷移で失敗: {e}",
            file=sys.stderr,
        )
        return False

    # 初回 check_login が false negative だった場合の救済。cookie が実は有効なら
    # LOGIN_ENTRY_URL は mission page へ戻され、そのまま auto_login すると
    # #mail が無く FORM_ERROR に落ちて有効 session を潰す。ここで検知して短絡する。
    # check_login は redirect 中に PlaywrightError を投げうるので suppress する
    # (例外が escape すると failure_count 更新前に process_account に飛んでしまう)。
    session_valid = False
    with contextlib.suppress(PlaywrightError):
        session_valid = check_login(page)
    if session_valid:
        print(
            f"[{name}] BNID ログイン画面遷移後にセッション有効を確認しました。",
            file=sys.stderr,
        )
        _reset_credentials_failure(profile_dir, credentials)
        return True

    outcome, submissions = _run_auto_login_sequence(page, name, credentials)
    if outcome is AutoLoginOutcome.SUCCESS:
        _reset_credentials_failure(profile_dir, credentials)
        return True
    if submissions > 0:
        _record_credentials_failure(profile_dir, credentials, submissions=submissions)
    return False


def _ensure_authenticated(
    page: Page, name: str, profile_dir: Path, options: RunOptions
) -> bool:
    """check_login → auto-relogin ゲートで最終的にログイン済みかを返す。

    False のときは呼び出し側で従来と同じ未ログイン扱い (exit_code=1) にする。
    ここでユーザ向けの案内メッセージも 1 か所に集約する。

    dry-run (dry_run=True) では、たとえ credentials が保存されていても
    自動再ログインは走らせない。auto_login は BNID にパスワード POST を送るため
    「dry-run = GET のみ、副作用ゼロ」の契約を壊してしまう。dry-run でも
    check_login のみでログイン済みかは判定する。
    """
    if check_login(page):
        return True
    if (
        options.auto_relogin
        and not options.dry_run
        and attempt_auto_relogin(page, profile_dir, name)
    ):
        return True
    print(
        f"[{name}] 未ログイン。"
        f"`uv run canvasser.py login --account {name}` を実行してください。",
        file=sys.stderr,
    )
    return False


def run_login_flow(
    page: Page, timeout_sec: int = 600, interval_sec: float = 3.0
) -> int:
    """ブラウザを headed で起動し、ログイン成功を is_login フラグでポーリング検知する。

    対話入力に頼らないので、bash-input のような非対話環境でも動作する。中断はブラウザを
    閉じるか Ctrl+C で行える。
    """
    print("ブラウザが立ち上がりました。", file=sys.stderr)
    print(
        "BNID でログインしてください。ログイン成功を検知したら自動で終了します。",
        file=sys.stderr,
    )
    print(
        f"(最大 {timeout_sec // 60} 分待機、Ctrl+C で中断可能)",
        file=sys.stderr,
    )

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        # ログイン画面へのリダイレクト中などに fetch は失敗する。次のポーリングを待つ。
        with contextlib.suppress(PlaywrightError):
            if check_login(page):
                print(
                    "ログイン状態を確認しました。次回から mission / checkin を"
                    "実行できます。",
                    file=sys.stderr,
                )
                return 0
        time.sleep(interval_sec)

    print(
        "タイムアウト。ログインを検出できませんでした。再度お試しください。",
        file=sys.stderr,
    )
    return 1


def run_login_init_flow(ctx: BrowserContext, page: Page, profile_dir: Path) -> int:
    """login-init 用: pending credentials を実ログインで検証してから active 化する。

    `persist_login_init_credentials` は pending ファイル (credentials.json.pending)
    に保存する。ここでは pending を読んで auto_login を回し、**SUCCESS のときにだけ**
    `_activate_pending_credentials` で active credentials.json を置き換える。

    設計理由:
    - **active を上書きしない**: 新パスワードのタイプミスで PASSWORD_ERROR になっても
      旧 active credentials は温存されるので、既存アカウントの自動再ログイン能力を
      失わない。
    - **未検証 creds を active に置かない**: CAPTCHA / TIMEOUT / FORM_ERROR の
      いずれも「保存パスワードで実ログインできる」証明になっていないので、
      `_ensure_authenticated` からの unattended auto-relogin で「間違ったパスワード
      が BNID にどんどん送信される」事故を防ぐため active に昇格させない。

    - SUCCESS: `_activate_pending_credentials` で置換 → exit 0。
    - PASSWORD_ERROR: pending 破棄 → exit 1 (active は温存)。
    - CAPTCHA_DETECTED / TIMEOUT / FORM_ERROR: pending 破棄 → exit 1
      (active 温存、ユーザに再実行を促す)。
    - `run_login_flow` フォールバックは **行わない**: ブラウザで別 creds を使った
      手動ログイン成功を「pending が検証成功」と誤解する事故を防ぐ。
    """
    pending = load_pending_credentials(profile_dir)
    if pending is None:
        # persist_login_init_credentials 直後なので通常は入っているはずだが、
        # 手改変等で読めない場合は手動ログインへ回す (検証すべき pending が
        # 無いので active credentials を汚染するリスクもない)。
        print("[login-init] pending credentials を読めません。", file=sys.stderr)
        return run_login_flow(page)

    # pending creds を確実に試すため既存 cookie を破棄する。login-init は
    # 「認証情報の再登録」で session invalidate はアラインしている。
    with contextlib.suppress(PlaywrightError):
        ctx.clear_cookies()

    try:
        page.goto(LOGIN_ENTRY_URL, wait_until="domcontentloaded")
    except PlaywrightError as e:
        _discard_pending_credentials(profile_dir)
        print(
            f"[login-init] BNID ログイン画面への遷移で失敗: {e}。"
            "pending は破棄しました。もう一度 login-init を実行してください。",
            file=sys.stderr,
        )
        return 1

    # login-init は failure_count 会計と関係しない (pending は SUCCESS のときだけ
    # 昇格するので、submit 回数はそのまま捨てる)
    outcome, _submitted = auto_login(page, pending)
    if outcome is AutoLoginOutcome.SUCCESS:
        _activate_pending_credentials(profile_dir)
        print(
            "[login-init] pending credentials での実ログインを確認、active に"
            "昇格しました。",
            file=sys.stderr,
        )
        return 0

    # 以下、いずれの失敗ケースでも pending を破棄して active は温存する。
    _discard_pending_credentials(profile_dir)

    if outcome is AutoLoginOutcome.PASSWORD_ERROR:
        print(
            "[login-init] pending credentials は BNID から認証エラーで拒否"
            "されました。pending を破棄しました (active は温存)。正しい認証情報で"
            " `login-init` を再実行してください。",
            file=sys.stderr,
        )
        return 1

    print(
        f"[login-init] 自動検証に失敗しました (outcome={outcome.value})。"
        "pending を破棄し active credentials は温存しました。"
        "`login-init` を再実行するか、"
        "`uv run canvasser.py login --account NAME` で手動ログインしてください。",
        file=sys.stderr,
    )
    return 1


# パストラバーサル (../) や絶対パス指定を排除するため、basename として安全な
# 文字集合に限定する
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _validate_account_name(account: str) -> None:
    """`--account` の値が安全な文字集合と長さに収まることを検証する。"""
    if not _ACCOUNT_NAME_RE.fullmatch(account):
        msg = (
            f"--account の値 {account!r} は許可されていません。"
            "使える文字は英数字・'_'・'-'・'.' のみで、長さは 1〜64 文字です。"
        )
        raise UserInputError(msg)
    # 正規表現は '.' や '..' 単体を通してしまうので、パス区切り含めここで追加防御する
    if account in (".", "..") or any(sep in account for sep in ("/", "\\")):
        msg = f"--account の値 {account!r} はパスとして危険なため許可されません。"
        raise UserInputError(msg)


def _ensure_within(base: Path, candidate: Path) -> None:
    """`candidate` が `base` の子孫であることを保証する。

    逸脱時は UserInputError を送出する。
    """
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError as e:
        msg = (
            f"プロファイル保存先 {candidate} が"
            f" profiles-dir {base} の外に逃げています。"
        )
        raise UserInputError(msg) from e


# ruff preview format が `except (A, B, C):` の外側カッコを削除するため、
# tuple はモジュールレベルの定数に切り出す (Python 3.14 はカッコなし表記も許容するが、
# 可搬性と可読性のため tuple 形式で catch する)。
_GIT_CHECK_IGNORE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    subprocess.TimeoutExpired,
    OSError,
)


def _profiles_dir_is_gitignored(profiles_dir: Path) -> bool:
    """`profiles_dir` が git ignore 対象なら True。

    profiles_dir がまだ存在しない (初回 `login` 前) ケースでも判定できるよう、
    path 末尾に `/` を付けてディレクトリと明示する。`.gitignore` の `profiles/` の
    ようなディレクトリ限定パターンは、path 側もディレクトリと分かる形でないと
    match しない。

    git repo 外や git 自体が使えない環境では False を返す (誤コミット経路を判定
    できないため拒否側に倒す)。`git check-ignore --quiet` の exit は 0=ignored、
    1=not ignored、128=error (repo 外)。
    """
    # PATH から git 実体を解決する。S607 対策で partial path を渡さない。
    git_bin = shutil.which("git")
    if git_bin is None:
        return False
    # git に渡す前に Windows の path 区切りを正規化し、末尾 / でディレクトリを明示する
    path_arg = str(profiles_dir).replace("\\", "/").rstrip("/") + "/"
    parent = profiles_dir.parent
    cwd = parent if parent.is_dir() else Path.cwd()
    try:
        # `--` で option 終端を明示することで、`-` から始まるユーザー指定パスを
        # option 扱いされないようにする。shell=False なので shell injection は
        # 起こらない。
        result = subprocess.run(  # noqa: S603
            [git_bin, "check-ignore", "--quiet", "--", path_arg],
            cwd=cwd,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except _GIT_CHECK_IGNORE_EXCEPTIONS:
        return False
    return result.returncode == 0


def resolve_profiles(
    profiles_dir: Path,
    account: str | None,
) -> list[tuple[str, Path]]:
    """処理対象のプロファイル一覧 `[(表示名, ディレクトリ), ...]` を決定する。

    `account` を指定すれば 1 アカウント固定、未指定なら `profiles_dir` 配下の
    サブディレクトリを全列挙する。
    """
    if account:
        _validate_account_name(account)
        target = (profiles_dir / account).resolve()
        _ensure_within(profiles_dir, target)
        return [(account, target)]
    if not profiles_dir.is_dir():
        return []
    result: list[tuple[str, Path]] = []
    for entry in sorted(profiles_dir.iterdir()):
        if not entry.is_dir():
            continue
        # 手動作成された悪性ディレクトリを排除するため、既存名も同じ規則で検証する
        if not _ACCOUNT_NAME_RE.fullmatch(entry.name):
            print(
                f"[warn] プロファイル名 {entry.name!r} が命名規則に合致しないため"
                " skip します。",
                file=sys.stderr,
            )
            continue
        target = entry.resolve()
        _ensure_within(profiles_dir, target)
        result.append((entry.name, target))
    return result


def open_persistent_context(
    p: Playwright, profile_dir: Path, *, headless: bool
) -> BrowserContext:
    """persistent_context を開く。

    Chromium 未取得のエラーが出たら install してリトライする。
    """
    kwargs: dict[str, Any] = {
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
    }
    try:
        return p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    except PlaywrightError as e:
        if "playwright install" in str(e).lower():
            _install_chromium()
            return p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        raise


@dataclass(kw_only=True)
class RunOptions:
    """CLI 引数から組み立てる 1 実行分の動作設定。

    mission と checkin は排他のサブコマンドなので、`run_mission` と `run_checkin`
    が同時に True になることはない。デフォルトは「何も実行しない完全ドライラン」で、
    login / login-init サブコマンドはそれぞれ `login_mode` / `login_init_mode`
    のみを立てて使う。
    """

    login_mode: bool = False
    login_init_mode: bool = False
    run_mission: bool = False
    run_checkin: bool = False
    dry_run: bool = False
    daily_budget: int = 0
    consecutive_failure_limit: int = 1
    out_of_range_limit: int = 3
    # credentials.json が保存されていれば `check_login()` false 時に auto_login を試す。
    # `--no-auto-relogin` で明示的に無効化 (手動運用に戻すとき) できる。
    auto_relogin: bool = True


def process_account(
    p: Playwright,
    name: str,
    profile_dir: Path,
    options: RunOptions,
) -> tuple[int, int]:
    """1 アカウント分の処理を行う。戻り値は `(獲得票数, exit_code)`。

    `dry_run` はドライランゲートで、True なら完全ドライラン (GET のみで
    POST/PUT は送らない)。
    未ログイン検知時は exit_code=1 を返し、呼び出し側で他アカウントへ進む。
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    headed = options.login_mode or options.login_init_mode
    ctx = open_persistent_context(p, profile_dir, headless=not headed)
    try:
        page = ctx.new_page()
        page.goto(MISSION_PAGE_URL, wait_until="domcontentloaded")

        # login は headed ブラウザ + is_login ポーリングで手動ログイン成功を検知する。
        if options.login_mode:
            return 0, run_login_flow(page)
        # login-init は事前 (_main_impl) で credentials を保存済み。ここで cookie を
        # 破棄してから auto_login で実ログインを検証する (擬似成功抜け防止)。
        if options.login_init_mode:
            return 0, run_login_init_flow(ctx, page, profile_dir)

        if not _ensure_authenticated(page, name, profile_dir, options):
            return 0, 1

        gained = 0
        exit_code = 0
        if options.run_mission:
            # dry-run の見込み枚数は集計に混ぜず、アカウント総計を汚さない
            mission_gain = collect_missions(page, dry_run=options.dry_run)
            if not options.dry_run:
                gained += mission_gain
        if options.run_checkin:
            # Referer を合わせるためチェックインページに一度 navigate しておく
            page.goto(CHECKIN_PAGE_URL, wait_until="domcontentloaded")
            settings = CheckinSettings(
                dry_run=options.dry_run,
                daily_budget=options.daily_budget,
                consecutive_failure_limit=options.consecutive_failure_limit,
                out_of_range_limit=options.out_of_range_limit,
                profile_dir=profile_dir,
            )
            try:
                checkin_gain = collect_checkins(page, settings)
                if not options.dry_run:
                    gained += checkin_gain
            except FailClosedError as e:
                # fail-closed 前に成功していた POST の reward は
                # e.partial_gained に入っているので、集計から落とさないよう合流させる。
                print(f"[{name}] fail closed: {e}", file=sys.stderr)
                if not options.dry_run:
                    gained += e.partial_gained
                exit_code = 1
        return gained, exit_code
    finally:
        ctx.close()


def main() -> int:
    """CLI エントリポイント。

    入力検証由来の `UserInputError` だけを短いメッセージに変換する。
    実装バグ由来の他の例外 (`ValueError` を含む) はそのまま通し、traceback で
    表示する。
    """
    try:
        return _main_impl()
    except UserInputError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    """CLI 引数パーサを構築する。

    login / login-init / mission / checkin / mark-completed の 5 サブコマンド。
    サブコマンドで必須引数と排他 (mission と checkin は同時実行しない) を
    構造的に表現し、フラグの組み合わせ検証を不要にする。
    """
    parser = argparse.ArgumentParser(
        description=(
            "シンデレラガール総選挙2026 デイリーミッション自動回収 (複数アカウント対応)"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 全サブコマンド共通の親パーサ
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--profiles-dir",
        default="./profiles",
        help="複数アカウントの親ディレクトリ (デフォルト: ./profiles)",
    )

    # ブラウザを起動するサブコマンド (login / mission / checkin) 共通の親パーサ
    browser = argparse.ArgumentParser(add_help=False)
    browser.add_argument(
        "--allow-unignored-profiles-dir",
        action="store_true",
        help="--profiles-dir が git ignore 対象でない場合の警告を無視する。"
        "デフォルトはモードに関係なく未 ignore の profiles-dir を拒否する "
        "(Cookie 誤コミット防止)。",
    )

    # ミッション・チェックイン実行系サブコマンド共通の親パーサ
    collect = argparse.ArgumentParser(add_help=False)
    collect.add_argument(
        "--account",
        help="対象アカウント名。未指定なら profiles-dir 内のすべてのアカウントを"
        "順次処理する",
    )
    collect.add_argument(
        "--dry-run",
        action="store_true",
        help="POST/PUT を送らない完全ドライラン (GET のみ)。state 更新や checkin の"
        "滞在 sleep も行わない。無指定なら本番実行。",
    )
    collect.add_argument(
        "--no-auto-relogin",
        action="store_true",
        help=(
            "credentials.json が保存されていても自動再ログインを行わない。"
            "手動運用に戻したいときや、資格情報を一時的に無効化したいときに使う。"
        ),
    )

    login = subparsers.add_parser(
        "login",
        parents=[common, browser],
        help="初回ログイン。Chromium を可視状態で起動する",
    )
    login.add_argument(
        "--account",
        required=True,
        help="対象アカウント名。profiles-dir 配下のサブディレクトリ名として扱う",
    )

    login_init = subparsers.add_parser(
        "login-init",
        parents=[common, browser],
        help=(
            "BNID メール/パスワードを対話入力し credentials.json に保存後、"
            "既存の対話ログインフローで実ログインを検証する"
        ),
    )
    login_init.add_argument(
        "--account",
        required=True,
        help="対象アカウント名。profiles-dir 配下のサブディレクトリ名として扱う",
    )

    subparsers.add_parser(
        "mission",
        parents=[common, browser, collect],
        help="ミッションを回収する (デフォルトはドライラン)",
    )

    checkin = subparsers.add_parser(
        "checkin",
        parents=[common, browser, collect],
        help="チェックインを処理する (デフォルトはドライラン)",
    )
    checkin.add_argument(
        "--daily-budget",
        type=int,
        default=0,
        help="1 回の実行あたりの実 POST 試行回数の上限 "
        "(デフォルト: 0 = 無制限)。未観測 ecode・失敗・成功のいずれも"
        " 1 リクエスト = 1 消費。"
        "時間帯制約 (深夜移動不可) で自然に上限がかかるため、通常は指定不要。"
        "緊急停止したいときにだけ小さな値を指定する。",
    )
    checkin.add_argument(
        "--consecutive-failure-limit",
        type=int,
        default=1,
        help=(
            "未観測 ecode が連続で何件出たら全体を中断するか"
            " (デフォルト: 1 = 1 件目で即停止)。"
        ),
    )
    checkin.add_argument(
        "--out-of-range-limit",
        type=int,
        default=3,
        help=(
            "E5005 (範囲外) の累積が何件で停止するか (デフォルト: 3)。"
            "crypto や座標の実装不一致で 51 件全部を撃たないための安全弁。"
        ),
    )

    mark = subparsers.add_parser(
        "mark-completed",
        parents=[common],
        help="実 POST 済みスポットを state.completed_spots に手動追加して終了する",
    )
    mark.add_argument(
        "--account",
        required=True,
        help="対象アカウント名。profiles-dir 配下のサブディレクトリ名として扱う",
    )
    mark.add_argument(
        "slugs",
        nargs="+",
        metavar="SLUG",
        help="登録する spot_slug (例: cg_vote2026_17)",
    )
    return parser


def _validate_thresholds(args: argparse.Namespace) -> None:
    """数値引数の下限を検証する。

    --daily-budget=0 は無制限として扱うが、負数を許すと limit_counter 判定が常時
    truthy になって実 POST 上限が壊れる。他の閾値も 1 未満だと本来の役割を
    果たせないので弾く。
    """
    if args.daily_budget < 0:
        msg = "--daily-budget は 0 以上を指定してください。"
        raise UserInputError(msg)
    if args.consecutive_failure_limit < 1:
        msg = "--consecutive-failure-limit は 1 以上を指定してください。"
        raise UserInputError(msg)
    if args.out_of_range_limit < 1:
        msg = "--out-of-range-limit は 1 以上を指定してください。"
        raise UserInputError(msg)


def _build_run_options(args: argparse.Namespace) -> RunOptions:
    """パース済み引数から RunOptions を組み立てる。

    サブコマンドがそのまま動作モードになる。チェックイン用の安全弁は
    checkin サブコマンドにしか存在しないため、mission では既定値のままにする。
    """
    if args.command == "login":
        return RunOptions(login_mode=True)
    if args.command == "login-init":
        return RunOptions(login_init_mode=True)
    if args.command == "mission":
        return RunOptions(
            run_mission=True,
            dry_run=args.dry_run,
            auto_relogin=not args.no_auto_relogin,
        )
    return RunOptions(
        run_checkin=True,
        dry_run=args.dry_run,
        daily_budget=args.daily_budget,
        consecutive_failure_limit=args.consecutive_failure_limit,
        out_of_range_limit=args.out_of_range_limit,
        auto_relogin=not args.no_auto_relogin,
    )


def _run_mark_completed(args: argparse.Namespace, profiles_dir: Path) -> int:
    """mark-completed の処理。state を編集して即終了する (ブラウザ起動なし)。"""
    _validate_account_name(args.account)
    target_dir = (profiles_dir / args.account).resolve()
    _ensure_within(profiles_dir, target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        mark_spots_completed(target_dir, args.slugs)
    except StateFileCorruptedError as e:
        print(
            f"state.json が破損しているため mark-completed で上書きできません: {e}",
            file=sys.stderr,
        )
        return 1
    return 0


def _ensure_profiles_dir_ignored(args: argparse.Namespace, profiles_dir: Path) -> None:
    """profiles_dir が git ignore されていることを確認する。

    Playwright は GET のみのドライラン中でも persistent context で cookie・cache・
    metadata を同期する。Cookie 誤コミットを防ぐため、実行モードに関係なく gitignore
    未対応の profiles_dir は拒否する。
    """
    if args.allow_unignored_profiles_dir:
        return
    if _profiles_dir_is_gitignored(profiles_dir):
        return
    msg = (
        f"{profiles_dir} が git ignore 対象になっていません。"
        "Cookie 入り persistent profile がコミットされる恐れがあります。"
        "既定の ./profiles を使うか、.gitignore に追加してから再実行してください。"
        "自己責任で続行するなら --allow-unignored-profiles-dir を付けてください。"
    )
    raise UserInputError(msg)


def _print_summary(results: list[tuple[str, int]]) -> None:
    """複数アカウント実行時の獲得サマリを出力する。"""
    print("\n=== サマリ ===")
    total = 0
    for name, gained in results:
        print(f"  {name}: +{gained}枚")
        total += gained
    print(f"  合計: +{total}枚")


def _main_impl() -> int:
    args = _build_parser().parse_args()

    profiles_dir = Path(args.profiles_dir).resolve()

    # mark-completed は state を編集して即終了する (ブラウザ起動なし)
    if args.command == "mark-completed":
        return _run_mark_completed(args, profiles_dir)

    login_mode = args.command == "login"
    login_init_mode = args.command == "login-init"
    if args.command == "checkin":
        _validate_thresholds(args)

    profiles = resolve_profiles(profiles_dir, args.account)
    if not profiles:
        msg = (
            f"プロファイルが見つかりません ({profiles_dir})。\n"
            "初回は `uv run canvasser.py login --account NAME` で"
            "アカウントを追加してください。"
        )
        raise UserInputError(msg)

    _ensure_profiles_dir_ignored(args, profiles_dir)

    # login-init はブラウザ起動前に対話入力 → credentials.json 保存を行う。
    # Chromium 取得待ちや playwright 起動より先に対話が済む方がユーザ体験が良い。
    if login_init_mode:
        _, target_profile = profiles[0]
        target_profile.mkdir(parents=True, exist_ok=True)
        persist_login_init_credentials(target_profile)

    options = _build_run_options(args)

    ensure_chromium_installed()

    exit_code = 0
    results: list[tuple[str, int]] = []
    with sync_playwright() as p:
        for name, profile_dir in profiles:
            print(f"\n=== アカウント: {name} ({profile_dir}) ===")
            try:
                gained, code = process_account(p, name, profile_dir, options)
            # 1 アカウントの失敗で全体を止めないため、意図して広く握る
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] 実行中に例外: {e}", file=sys.stderr)
                exit_code = 1
                results.append((name, 0))
                continue
            results.append((name, gained))
            if code != 0:
                exit_code = code
            if login_mode or login_init_mode:
                # login / login-init は 1 アカウント (--account 必須) のみ処理して抜ける
                return code

    if len(profiles) > 1:
        _print_summary(results)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

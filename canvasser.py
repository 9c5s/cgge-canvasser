# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "playwright>=1.40.0",
#     "pycryptodome>=3.19",
#     "googlemaps>=4.10",
#     "python-dotenv>=1.0",
#     "tzdata>=2024.1; sys_platform == 'win32'",
# ]
# ///
"""シンデレラガール総選挙2026 デイリー自動化スクリプト。

Playwright の persistent context でブラウザセッションを保持し、フロントが叩いている
内部 API をそのまま呼び出してミッション回収と (--checkin ならば) チェックインを自動化する。
複数アカウントを ./profiles/{account}/ に分けて運用する構成が前提となる。

初回のみ `--login --account NAME` で手動ログインし、以降は `uv run canvasser.py` で
全アカウントを headless に順次処理する。

  uv run canvasser.py --login --account main               # 初回ログイン
  uv run canvasser.py                                      # 全アカウント、完全ドライラン
  uv run canvasser.py --execute-mission                    # ミッションだけ本番
  uv run canvasser.py --checkin --execute-checkin          # チェックインだけ本番
  uv run canvasser.py --checkin --execute-mission --execute-checkin  # 両方本番

`--execute-mission` と `--execute-checkin` は独立したゲート。どちらも未指定であれば
GET のみのドライランとなり、POST/PUT は一切送らない。
"""

from __future__ import annotations

import argparse
import base64
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

# .env をスクリプト自身の隣から明示的にロードする。cwd が違ってもタスクスケジューラから読める。
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

# キャンペーンの時刻はすべて日本時間で扱う (サーバ側も JST で返してくる)。
JST = ZoneInfo("Asia/Tokyo")

# Next.js のチャンクから抽出した API 定数。総選挙2026 用の名前空間となる。
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

# 地球の 1 度緯度に対応する距離 (メートル)。WGS84 近似。
METERS_PER_DEG_LAT = 111_320.0
# チェックイン許容半径を少し内側へ絞って境界事故を避けるための係数。
CHECKIN_RADIUS_MARGIN = 0.85


def _default_now() -> datetime:
    """collect_checkins の now_fn の既定値。現在 JST 時刻を返す。"""
    return datetime.now(JST)


def call_api(page: Page, method: str, path: str) -> dict[str, Any]:
    """ページ内で fetch を実行し、Cookie 付きで API を叩く。

    - 送信元は idolmaster-official.jp、API は api.idolmaster-official.jp と別ホストだが、
      サーバ側で CORS allow-credentials が有効なので credentials=include で通る。
    - x-api-key はフロント公開の固定値を使う。
    - fetch reject (ネットワーク・CORS・DNS 失敗) や非 JSON 応答も {status, body, error} で
      構造化して返す。呼び出し側で status==0 やエラーコードで分岐できる。
    """
    url = f"{API_HOST}{API_BASE}{path}"
    return page.evaluate(
        """
        async ([url, method, apiKey]) => {
          try {
            const res = await fetch(url, {
              method,
              credentials: 'include',
              headers: {
                'x-api-key': apiKey,
                'accept': 'application/json, text/plain, */*'
              }
            });
            const raw = await res.text();
            try { return {status: res.status, body: JSON.parse(raw)}; }
            catch { return {status: res.status, body: raw, error: 'non-json'}; }
          } catch (e) {
            return {status: 0, body: null, error: String(e)};
          }
        }
        """,
        [url, method, API_KEY],
    )


def check_login(page: Page) -> bool:
    """auths/login/check を叩いて認証状態を確認する。

    fetch 例外や非 JSON 応答は「未ログイン」と同義扱いにする。これで Cookie
    期限切れ・ネットワーク不通も呼び出し側の「未ログインならば --login を促す」ルート
    へ合流させられる。
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
    return bool((result.get("payload") or {}).get("is_login", False))


def collect_missions(page: Page, execute: bool = False) -> int:
    """API 経由で完了可能なミッションをまとめて消化する。

    - `mission_complete_api_call_flag=True` のみを対象にする。
      あいことばやチェックインは外部トリガー起因なのでスキップする。
    - 未達成で残挑戦回数がある場合は達成 POST の後に受取 PUT を送る。
    - 達成済みで未受取の場合は受取 PUT のみ送る。
    - `execute=False` (デフォルト) は完全ドライラン。GET のみ実行し、POST/PUT は送らない。

    戻り値は今回獲得した投票券数の合計 (dry-run 時は「実行した場合に得るはずの見込み」)。
    """
    listing = call_api(page, "GET", "/missions?mission_type=0&limit=300")
    body = listing.get("body")
    if listing["status"] != 200 or not isinstance(body, dict) or body.get("status") != "SUCCESS":
        raise RuntimeError(f"ミッション一覧の取得に失敗: {listing}")

    payload = body["payload"]
    print(f"現在の保有投票券: {payload.get('current_point', 0)}枚")
    print(f"ミッションモード: {'EXECUTE (本番)' if execute else 'DRY-RUN (POST/PUT送信なし)'}")

    gained = 0
    for m in payload["missions"]:
        mid: int = m["mission_id"]
        name: str = m["mission_name"]
        pts: int = m["mission_point"]

        action = m.get("action") or {}
        if not action.get("mission_complete_api_call_flag"):
            continue

        completed = bool(m.get("is_mission_completed"))
        received = bool(m.get("is_mission_received"))
        remaining = m.get("remaining_completable_count") or 0

        if completed and not received:
            gained += _receive(page, mid, name, pts, execute=execute)
            continue

        if not completed and remaining > 0:
            outcome = _complete(page, mid, name, execute=execute)
            # 累計達成数系ミッション (#100-104) は他ミッション達成の副作用で
            # サーバ側では達成扱いになる。一方で一覧の completed フラグ更新は遅延する。
            # 達成 POST が「既に達成済み」を返した場合でも受取 PUT は通るので試す。
            if outcome in ("ok", "already_done"):
                gained += _receive(page, mid, name, pts, execute=execute)

    label = "獲得見込み" if not execute else "獲得"
    print(f"ミッション {label}: {gained}枚")
    return gained


def _complete(page: Page, mid: int, name: str, execute: bool = False) -> str:
    """ミッション達成の POST を送る。

    補足として、一覧取得は `/missions` (複数形) だが、個別操作は `/mission` (単数形) となる。
    フロントの `b.eq` 定数が単数形であることをチャンク解析で確認済み。

    execute=False の場合は POST を送らず "ok" 相当 (次の受取も dry-run で進める) を返す。

    戻り値:
      - "ok"             : 達成成功
      - "already_done"   : E1906 既に達成済み (受取 PUT は試すべき)
      - "condition_unmet": E1924 達成条件未満 (静かにスキップ)
      - "error"          : その他失敗
    """
    print(f"[達成] #{mid} {name}")
    if not execute:
        print("  -> DRY-RUN (POST送信なし)")
        return "ok"
    res = call_api(page, "POST", f"/mission/{mid}")
    body = res.get("body")
    if res["status"] == 200 and isinstance(body, dict) and body.get("status") == "SUCCESS":
        print("  -> 成功")
        return "ok"

    ecode = ((body.get("payload") or {}).get("ecode")) if isinstance(body, dict) else None
    if ecode == "E1906":
        print("  -> 既に達成済み (受取を試す)")
        return "already_done"
    if ecode == "E1924":
        print("  -> 条件未達、スキップ")
        return "condition_unmet"

    err_note = f" err={res.get('error')}" if res.get("error") else ""
    print(f"  -> 失敗: HTTP {res['status']}{err_note} body={body}")
    return "error"


def _receive(page: Page, mid: int, name: str, pts: int, execute: bool = False) -> int:
    """投票券受取の PUT を送る。成功時は加算票数を返す。

    execute=False の場合は PUT を送らず、「実行していれば得られたであろう pts」を返す。
    """
    print(f"[受取] #{mid} {name} (+{pts})")
    if not execute:
        print("  -> DRY-RUN (PUT送信なし)")
        return pts
    res = call_api(page, "PUT", f"/mission/{mid}/receive")
    body = res.get("body")
    ok = res["status"] == 200 and isinstance(body, dict) and body.get("status") == "SUCCESS"
    if ok:
        received = (body.get("payload") or {}).get("received_point")
        print(f"  -> 成功 (received_point={received})")
        return pts
    err_note = f" err={res.get('error')}" if res.get("error") else ""
    print(f"  -> 失敗: HTTP {res['status']}{err_note} body={body}")
    return 0


# -------------------- チェックイン (#99) 関連 --------------------

def encrypt_coords(coords: dict[str, Any], password: str = API_KEY) -> str:
    """位置情報 JSON を crypto-js プロトコル互換で AES-CBC 暗号化する。

    フロントエンドが crypto-js で組み立てるペイロード形式を Python 側で再現する。
      - key = PBKDF2(password, salt=random16, iterations=500, keySize=8 words=32B, hasher=SHA1)
      - iv = random16
      - ciphertext = AES-CBC(key, iv, PKCS7(JSON.stringify(coords)))
      - payload = f"{salt.hex()},{iv.hex()},{ct_base64}"

    パスワードはフロント公開値の X-API-KEY を流用しているため、この暗号化に機密性は
    ない (プロトコル互換のためのラッパーにあたる)。サーバが受理する形式を再現している。
    salt と iv は hex、ciphertext は Base64 で連結する (crypto-js の Hex と Base64
    混在フォーマットを実 UI 経由の POST 観測で確定した)。
    """
    salt = os.urandom(16)
    iv = os.urandom(16)
    key = hashlib.pbkdf2_hmac("sha1", password.encode(), salt, 500, dklen=32)
    plaintext = json.dumps(coords, separators=(",", ":")).encode()
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, 16))
    ct_b64 = base64.b64encode(ciphertext).decode()
    return f"{salt.hex()},{iv.hex()},{ct_b64}"


def random_point_in_circle(
    center_lat: float, center_lng: float, radius_m: float
) -> tuple[float, float]:
    """半径 radius_m [m] の円内から面積一様分布でランダム点を返す。

    d = r*sqrt(u)、theta = 2*pi*v で面積一様となる (中心密集を避ける)。
    経度スケールは緯度によって変わるため cos(lat) で補正する。
    """
    u = random.random()
    theta = random.random() * 2 * math.pi
    d = radius_m * math.sqrt(u)
    d_lat = (d * math.cos(theta)) / METERS_PER_DEG_LAT
    d_lng = (d * math.sin(theta)) / (
        METERS_PER_DEG_LAT * math.cos(math.radians(center_lat))
    )
    return center_lat + d_lat, center_lng + d_lng


def _natural_accuracy() -> float:
    """モバイル GPS 屋外の実測分布に近い精度値をランダムに返す [m]。

    実機の accuracy は 8〜30m あたりに強く集中し、稀に 30〜80m の外れ値を出す。
    一様乱数だと分布がフラットで統計的に不自然になるので、正規分布と外れ値の混合で再現する。
    """
    if random.random() < 0.15:
        # 外れ値: 屋内寄り、あるいはマルチパスの影響
        val = random.uniform(30.0, 80.0)
    else:
        # 中央値 18m、標準偏差 6m の正規分布。5m 未満は非現実的なのでクランプする
        val = max(5.0, random.gauss(18.0, 6.0))
    return round(val, 3)


def _natural_altitude() -> tuple[float | None, float | None]:
    """altitude と altitudeAccuracy をリアルに乱択して返す。

    - 大多数のケース (80%) は取得不可として None を返す。
    - 20% は GPS 側で取れた想定にする。日本の都市部平地の 5〜80m 程度に散らばらせる。
    """
    if random.random() < 0.20:
        alt = round(random.uniform(5.0, 80.0), 1)
        alt_acc = round(random.uniform(20.0, 50.0), 1)
        return alt, alt_acc
    return None, None


def make_checkin_coords(spot: dict[str, Any]) -> dict[str, Any]:
    """スポット情報から、円内ランダム点と自然化した coords を組んで返す。

    サーバ側 (あるいは将来の異常検知) で分布統計が取られた場合に BOT らしい特徴が出ない
    ように、accuracy と altitude を実機挙動に近い分布で乱択する。
    """
    radius = float(spot.get("checkin_radius") or 500)
    lat, lng = random_point_in_circle(
        float(spot["location_latitude"]),
        float(spot["location_longitude"]),
        radius * CHECKIN_RADIUS_MARGIN,
    )
    alt, alt_acc = _natural_altitude()
    return {
        "accuracy": _natural_accuracy(),
        "latitude": lat,
        "longitude": lng,
        "altitude": alt,
        "altitudeAccuracy": alt_acc,
        # チェックイン時は「静止して端末を見ている」想定なので heading と speed は null に置く
        "heading": None,
        "speed": None,
    }


def call_checkin_api(
    page: Page,
    method: str,
    path: str,
    body: str | None = None,
) -> dict[str, Any]:
    """checkins 系エンドポイントを叩く。

    body があれば application/x-www-form-urlencoded で送る。これは実 UI の POST を
    キャプチャして確定した仕様となる (axios が data:string を送るときの既定 Content-Type)。
    body は "salt_hex,iv_hex,ct_base64" の文字列をそのまま (URL エンコードせずに) 載せる。
    """
    url = f"{API_V1}/checkins{path}"
    return page.evaluate(
        """
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
        """,
        [url, method, API_KEY, body],
    )


# チェックイン API の既知エラーコード。UI 側から抽出したもの。
# チャンクの表示コードから "E5005" (チェックイン範囲外) を把握している。それ以外 (既達成含む) は、
# 実 POST 前の段階では「未観測 ecode = unknown = 即停止」として扱う。
# 実観測で意味が確定した ecode だけを随時ここへ追加する。
ECODE_OUT_OF_RANGE = "E5005"
ECODES_ALREADY_DONE: tuple[str, ...] = ()  # 実観測が済むまで空。未観測は unknown で停止する。


def order_spots_by_proximity(
    spots: list[dict[str, Any]],
    start_index: int | None = None,
    start_location: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    """最近傍法でスポット順序を決める。現実の人間の移動に近いルートを生成する。

    - `start_location` を指定した場合は、現在地に最も近いスポットを 1 件目に据える
      (state.json から前回位置を渡す想定)。
    - `start_location` と `start_index` の両方が未指定であれば、開始スポットを乱択する。
    - 以降は現在地から Haversine 直線距離が最小のスポットを次に選ぶ。
    - 単純な greedy TSP 近似。最適ではないが「近場をまとめて回る」自然な挙動になる。
    """
    if not spots:
        return []
    unvisited = list(spots)

    if start_location is not None:
        # 前回位置に最も近いスポットを 1 件目に据える
        cur_lat, cur_lng = start_location
        nearest_idx = 0
        nearest_d = float("inf")
        for i, s in enumerate(unvisited):
            d = _distance_m(
                cur_lat, cur_lng,
                float(s["location_latitude"]), float(s["location_longitude"]),
            )
            if d < nearest_d:
                nearest_d = d
                nearest_idx = i
        current = unvisited.pop(nearest_idx)
    else:
        if start_index is None:
            start_index = random.randrange(len(unvisited))
        current = unvisited.pop(start_index)

    ordered = [current]
    cur_lat = float(current["location_latitude"])
    cur_lng = float(current["location_longitude"])

    while unvisited:
        nearest_idx = 0
        nearest_d = float("inf")
        for i, s in enumerate(unvisited):
            d = _distance_m(
                cur_lat, cur_lng,
                float(s["location_latitude"]), float(s["location_longitude"]),
            )
            if d < nearest_d:
                nearest_d = d
                nearest_idx = i
        current = unvisited.pop(nearest_idx)
        ordered.append(current)
        cur_lat = float(current["location_latitude"])
        cur_lng = float(current["location_longitude"])

    return ordered


def collect_checkins(
    page: Page,
    execute: bool = False,
    daily_budget: int = 0,
    consecutive_failure_limit: int = 1,
    out_of_range_limit: int = 3,
    profile_dir: Path | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> int:
    """全スポットに対してチェックインを試みる。戻り値は獲得票数の見込み。

    セーフティ:
      - `execute=False` (デフォルト) は完全ドライランとなる (POST を送らず、sleep もなく、state も書き換えない)。
      - `state.completed_spots` にある slug は事前に skip する (実 POST を無駄に打たない)。
      - 未観測 ecode (SUCCESS と E5005 以外) は fail closed で即停止する。意味が判るまで
        「BAN シグナル・認証切れ・予期せぬ状態」のいずれかとして扱う。
      - `consecutive_failure_limit` の未知エラーが連続した場合、全体を中断する (デフォルト 1 = 即停止)。
      - `daily_budget > 0` で件数上限を設ける。`daily_budget=0` は無制限として扱う。
      - スポット間には交通機関稼働時間帯 (06:00-24:00) を考慮した移動時間の待機を挟む。
      - 到着時刻がスポットの `checkin_end_datetime` を過ぎたスポットは skip する (イベント全体
        期限ではないため、後続の valid スポットは処理を継続する)。
      - deadline がパースできないスポットは `execute=True` の場合に fail closed で扱う (skip 相当)。
      - 各スポットで 10〜30 分の滞在時間を挟む (最終スポットや budget 到達時は skip する)。
      - state.json は実 POST 成功時に限り atomic に更新する。
      - state.json が壊れている場合、`execute=True` では FailClosedError を送出する (exit_code=1)。
        dry-run では空 state で継続する。
      - `out_of_range_limit` 件以上 E5005 (範囲外) が続いた場合は停止する (crypto・body・radius の
        不一致が疑われるため、実 POST を 51 件全部撃たせない)。
    """
    if now_fn is None:
        now_fn = _default_now
    listing = call_checkin_api(page, "GET", f"/event/{CHECKIN_EVENT_SLUG}")
    body = listing.get("body")
    if listing["status"] != 200 or not isinstance(body, dict) or body.get("status") != "SUCCESS":
        raise RuntimeError(f"チェックインイベント取得に失敗: {listing}")

    all_spots = body.get("payload", {}).get("spots", [])
    if not all_spots:
        print("チェックイン対象スポットが空でした。")
        return 0

    # state.json から前回の最終チェックイン地点・仮想時刻・完了済み集合を復元する。
    # execute=True の場合は、state 破損時に FailClosedError を送出する。
    resume_lat: float | None = None
    resume_lng: float | None = None
    resume_at: datetime | None = None
    completed_spots: set[str] = set()
    if profile_dir is not None:
        try:
            resume_lat, resume_lng, resume_at, completed_spots = resume_context(
                profile_dir, strict=execute
            )
        except StateFileCorruptedError as e:
            msg = f"state.json が破損しています: {e}。手動で確認してから再実行してください。"
            if execute:
                raise FailClosedError(msg) from e
            # dry-run は初期値 (空 state) のまま継続する。スポット取得後の検証を回すため。
            print(msg, file=sys.stderr)

    # 完了済みスポットを事前にフィルタする。これでサーバ側で「既達成」となっている
    # ものへ無駄に POST を送らない。
    skipped_completed = [s for s in all_spots if s["slug"] in completed_spots]
    spots = [s for s in all_spots if s["slug"] not in completed_spots]
    if skipped_completed:
        print(f"事前 skip (完了済み): {len(skipped_completed)}件")
    if not spots:
        print("全スポット完了済みです。")
        return 0

    # 開始地点は state に前回位置があれば「そこに最も近いスポット」を起点にする。
    # なければランダム開始とし、以降は最近傍で辿る。
    start_loc = (
        (resume_lat, resume_lng) if resume_lat is not None and resume_lng is not None else None
    )
    spots = order_spots_by_proximity(spots, start_location=start_loc)

    budget_label = "無制限" if daily_budget <= 0 else f"{daily_budget}件"
    travel_backend = (
        "gmaps (公共交通)" if _get_gmaps_client() is not None else "haversine (自前計算)"
    )
    print(f"チェックイン対象スポット: {len(spots)}件 (全 {len(all_spots)}, 完了済み {len(skipped_completed)})")
    print(f"モード: {'EXECUTE (本番)' if execute else 'DRY-RUN (POST送信なし)'}")
    print(f"移動時間バックエンド: {travel_backend}")
    print(f"実POST試行 上限: {budget_label} / 連続失敗中断: {consecutive_failure_limit}件")
    print(f"開始スポット: {spots[0]['slug']} {spots[0]['name']}")

    gained = 0
    successful = 0
    attempted = 0  # 実 POST を送った回数。execute=True のときのみ加算する。
    consecutive_failures = 0
    out_of_range_count = 0  # E5005 (範囲外) の累積カウンタ。実装バグ検知に使う。

    # 現在時刻を追跡する。state に前回終了時刻があれば now と比較して大きい方を採用する。
    # これで「前回 23:50 に終わって翌日 12:00 に再開」も自然に連続扱いになる。
    virtual_now: datetime = now_fn()
    if virtual_now.tzinfo is None:
        virtual_now = virtual_now.replace(tzinfo=JST)
    if resume_at is not None and resume_at > virtual_now:
        virtual_now = resume_at
    resumed = start_loc is not None
    print(f"開始時刻(仮想): {virtual_now:%Y-%m-%d %H:%M %Z}"
          + (f" (前回位置から再開: {resume_lat:.4f},{resume_lng:.4f})" if resumed else ""))

    # 「1 件目に前回位置がある」場合、直前スポットは前回位置扱いになるため、
    # 1 件目の前にも移動時間を計算する必要がある。resumed で分岐を制御する。
    prev_lat: float | None = resume_lat if resumed else None
    prev_lng: float | None = resume_lng if resumed else None

    for i, spot in enumerate(spots, 1):
        # 上限判定は「実行モードで意味のあるカウンタ」で行う。
        # - execute=True: 実 POST 試行回数 (成功・既達成・範囲外・失敗のいずれでも 1 リクエスト = 1 消費)
        # - execute=False: 仮想成功数 (ドライラン獲得見込みの目安)
        # これで `--execute --daily-budget 1` は「実 POST を 1 回だけ送る」を厳密に保証する。
        limit_counter = attempted if execute else successful
        if daily_budget > 0 and limit_counter >= daily_budget:
            print(f"  日次上限 {daily_budget}件に到達。残り {len(spots) - i + 1}件は次回以降。")
            break

        slug = spot["slug"]
        name = spot["name"]
        s_lat = float(spot["location_latitude"])
        s_lng = float(spot["location_longitude"])

        # 前回位置 (直前スポット、あるいは state 復元位置) からの移動時間を計算する。
        if prev_lat is not None:
            secs, mode = estimate_travel_seconds(
                prev_lat, prev_lng, s_lat, s_lng,
                departure_time=virtual_now,
            )
            if mode == "gmaps-transit":
                # transit の duration は Google が始発待ち等を含めて計算済み。
                # 追加で翌朝発に押し戻すと二重加算になるので、そのまま加算する。
                arrival = virtual_now + timedelta(seconds=secs)
            else:
                # driving や haversine は 24 時間稼働前提の計算のため、深夜跨ぎを翌朝に押し戻す。
                arrival = next_arrival_time(virtual_now, secs)
            wait_seconds = (arrival - virtual_now).total_seconds()
            straight_km = _distance_m(prev_lat, prev_lng, s_lat, s_lng) / 1000
            deferred_seconds = wait_seconds - secs
            deferred_note = (
                f", 翌朝発に押戻し +{humanize_duration(deferred_seconds)}"
                if deferred_seconds > 60
                else ""
            )
            print(
                f"  移動待機: {humanize_duration(wait_seconds)} ({mode}, 直線 {straight_km:.1f}km"
                f"{deferred_note}) -> 到着 {arrival:%m/%d %H:%M}"
            )
            if execute:
                time.sleep(wait_seconds)
            virtual_now = arrival

        # スポットのチェックイン期間を過ぎていないか確認する。個別スポットだけを skip し、
        # 他の有効スポットは続けて処理する (イベント全体の期限とは別)。
        # 期限をパースできない場合、execute では fail closed で全体を中断する (サーバ形式変更の
        # 疑い)。個別スポットの skip では気付きにくいため、ここで例外を上げる。
        deadline = parse_checkin_deadline(spot)
        if deadline is None:
            msg = (
                f"[{slug}] checkin_end_datetime = {spot.get('checkin_end_datetime')!r} "
                "がパースできません。サーバ側の日付形式が変わった可能性があります。"
            )
            if execute:
                raise FailClosedError(msg, partial_gained=gained)
            print(f"  {msg} (dry-run: skip)", file=sys.stderr)
            # 移動時間は消費済み。次スポットの travel は現地点から計算する。
            prev_lat, prev_lng = s_lat, s_lng
            continue
        if virtual_now > deadline:
            print(f"  [{slug}] スポット期限 ({deadline:%m/%d %H:%M %Z}) 経過、skip。")
            # 到着はしているので、次スポットの移動計算は現地点起点にする。
            prev_lat, prev_lng = s_lat, s_lng
            continue

        coords = make_checkin_coords(spot)
        distance_m = _distance_m(s_lat, s_lng, coords["latitude"], coords["longitude"])
        body = encrypt_coords(coords)

        print(
            f"[{i:3}/{len(spots)}] {slug} {name}"
            f" (offset {distance_m:.1f}m, acc={coords['accuracy']}m, alt={coords['altitude']})"
        )

        stay_secs = natural_stay_seconds()

        def will_continue_after(next_attempted: int, next_successful: int) -> bool:
            """このスポット処理後に次のループへ進むかを判定する。

            - 最終スポットなら False (もう次はない)。
            - daily_budget を使い切ったら False (次のループ冒頭で break される)。
            - execute の True と False では使うカウンタが異なる (attempted と successful)。
            """
            if i >= len(spots):
                return False
            if daily_budget <= 0:
                return True
            counter = next_attempted if execute else next_successful
            return counter < daily_budget

        if not execute:
            # ドライランは POST を送らず、state.json も汚染しない。
            # 実 POST 実行時の resume を狂わせないためにも、ここで state を書き換えないのは要点になる。
            print(f"       body={body[:60]}...(len={len(body)})  [DRY-RUN]")
            gained += 10
            successful += 1
            prev_lat, prev_lng = s_lat, s_lng
            if will_continue_after(attempted, successful):
                virtual_now = virtual_now + timedelta(seconds=stay_secs)
                print(f"       滞在 {humanize_duration(stay_secs)} -> 出発 {virtual_now:%m/%d %H:%M}")
            continue

        # --- 実 POST 分岐 ---
        # POST 送信の直前に attempted を加算する。これで daily_budget が「実 POST 試行回数」で
        # 厳密に働き、既達成・範囲外・失敗のいずれも 1 リクエスト = 1 消費として扱える。
        attempted += 1
        res = call_checkin_api(
            page,
            "POST",
            f"/event/{CHECKIN_EVENT_SLUG}/spot/{slug}/checkin",
            body=body,
        )
        body_resp = res.get("body")
        ecode = ((body_resp.get("payload") or {}).get("ecode")) if isinstance(body_resp, dict) else None

        if res["status"] == 200 and isinstance(body_resp, dict) and body_resp.get("status") == "SUCCESS":
            print(f"       -> 成功")
            gained += 10
            successful += 1
            consecutive_failures = 0
            prev_lat, prev_lng = s_lat, s_lng
            virtual_now = now_fn()
            if virtual_now.tzinfo is None:
                virtual_now = virtual_now.replace(tzinfo=JST)
            # 成功直後に一次 state を保存する。sleep 中に中断されても completed_spots に slug が残り、
            # 次回起動時の事前フィルタで無駄な POST を防げる。
            if profile_dir is not None:
                update_checkin_state(profile_dir, spot, virtual_now)
            # 次のスポットに進む場合に限り滞在時間を消化する。
            # 最終スポットや budget 到達時は無駄な sleep を避ける。
            if will_continue_after(attempted, successful):
                time.sleep(stay_secs)
                virtual_now = virtual_now + timedelta(seconds=stay_secs)
                print(f"       滞在 {humanize_duration(stay_secs)} -> 出発 {virtual_now:%m/%d %H:%M %Z}")
                # 滞在で仮想時刻を進めた分を state に反映する。spot 情報は同じなので上書き相当となる。
                if profile_dir is not None:
                    update_checkin_state(profile_dir, spot, virtual_now)
            continue

        if ecode == ECODE_OUT_OF_RANGE:
            out_of_range_count += 1
            print(
                f"       -> 範囲外 ({ecode})、スキップ "
                f"(累積 {out_of_range_count}/{out_of_range_limit})"
            )
            consecutive_failures = 0
            if out_of_range_count >= out_of_range_limit:
                # E5005 が想定より多発する場合は crypto・body・radius の実装不一致が疑われる。
                # 未指定なら 51 件全部を撃ってしまうため、FailClosedError で全体を中断する
                # (process_account が exit_code=1 に反映する)。
                raise FailClosedError(
                    f"範囲外 (E5005) が {out_of_range_count} 件。crypto・body・radius の "
                    "実装不一致の疑いがあるため停止する。座標計算とペイロードの整合性を確認して "
                    "から --max-out-of-range を上げて再実行する。",
                    partial_gained=gained,
                )
            # 移動時間は消費済みなので、次スポットの travel 計算は現地点を起点にする。
            prev_lat, prev_lng = s_lat, s_lng
            continue

        if ecode in ECODES_ALREADY_DONE:
            # 実観測で意味が確定した「既達成」ecode のみここに入る (初期は空 tuple)。
            print(f"       -> 既達成 ({ecode})、スキップ")
            consecutive_failures = 0
            prev_lat, prev_lng = s_lat, s_lng
            # 「サーバ側で成功済み」の意味なので、state に反映しておけば次回以降 skip される。
            if profile_dir is not None:
                update_checkin_state(profile_dir, spot, virtual_now, mark_completed=True)
            continue

        # 未観測 ecode = unknown となる。BAN シグナル・認証切れ・予期せぬ状態のいずれかなので
        # fail closed で即中断する。意味が確定したら ECODE_* に追加してから再実行する。
        consecutive_failures += 1
        err_note = f" err={res.get('error')}" if res.get("error") else ""
        print(
            f"       -> 未観測ecode (連続{consecutive_failures}件目): "
            f"HTTP {res['status']}{err_note} body={body_resp}",
            file=sys.stderr,
        )
        if consecutive_failures >= consecutive_failure_limit:
            # unknown ecode が limit を超えた場合は想定外の状況にあたる。exit_code=1 で明示する。
            raise FailClosedError(
                f"連続失敗が {consecutive_failure_limit}件に達したため中断する。"
                "body の ecode を確認して意味を確定してから再実行する。",
                partial_gained=gained,
            )
        # 未観測 ecode を許容範囲内で流す場合でも、移動時間は消費済みなので、次スポットの
        # travel 計算を現地点起点に更新する。そうしないと clock は失敗スポット到着時刻、
        # origin は前スポットという物理的にありえない状態になる。
        prev_lat, prev_lng = s_lat, s_lng

    label = "獲得見込み" if not execute else "獲得"
    footer = f"{successful}スポット成功"
    if execute:
        footer += f", 実POST試行 {attempted}件"
    footer += f", 仮想終了時刻 {virtual_now:%m/%d %H:%M}"
    print(f"{label}: 約{gained}票 ({footer})")
    return gained


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """2 点間の距離 [m] を Haversine 近似で計算する。"""
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# 実移動時間の推定用パラメータ。直線距離を実道路距離に補正するための係数となる。
ROAD_DISTANCE_FACTOR = 1.35

# Google Maps Directions API 統合。GMAPS_KEY 環境変数があれば有効化する。
# キャッシュキーは (lat1, lng1, lat2, lng2, departure_bucket_iso) で構成する。
# departure_time は 30 分単位で丸めてキャッシュヒット率を確保する。
_GMAPS_CACHE: dict[tuple[float, float, float, float, str], tuple[float, str]] = {}
_gmaps_client: Any = None
_gmaps_probed: bool = False
_GMAPS_TIME_BUCKET_MINUTES = 30


def _get_gmaps_client() -> Any:
    """遅延初期化する。GMAPS_KEY が未設定なら None を返す。"""
    global _gmaps_client, _gmaps_probed
    if _gmaps_probed:
        return _gmaps_client
    _gmaps_probed = True
    key = os.environ.get("GMAPS_KEY")
    if not key:
        return None
    try:
        import googlemaps
        _gmaps_client = googlemaps.Client(key=key, timeout=15)
    except Exception as e:
        print(
            f"Google Maps クライアント初期化失敗: {e}。Haversine にフォールバックする。",
            file=sys.stderr,
        )
        _gmaps_client = None
    return _gmaps_client


def _estimate_travel_seconds_gmaps(
    lat1: float, lng1: float, lat2: float, lng2: float,
    departure_time: datetime,
) -> tuple[float, str] | None:
    """Google Maps Directions API で公共交通機関の実移動時間を取得する。

    `departure_time` を渡すことで、深夜出発時の「始発待ち込み」の実運行 duration が
    Google 側から返る (例: 22 時に東京→札幌の transit 検索なら「翌朝始発 + 移動」の時間)。
    キーが無い場合や API エラー時は None を返し、呼び出し側で Haversine にフォールバックする。
    """
    client = _get_gmaps_client()
    if client is None:
        return None
    # departure_time を 30 分単位で丸めてキャッシュ粒度を揃える。秒と微秒は落とす。
    bucket_minute = (departure_time.minute // _GMAPS_TIME_BUCKET_MINUTES) * _GMAPS_TIME_BUCKET_MINUTES
    bucketed = departure_time.replace(minute=bucket_minute, second=0, microsecond=0)
    cache_key = (
        round(lat1, 4), round(lng1, 4), round(lat2, 4), round(lng2, 4),
        bucketed.isoformat(),
    )
    if cache_key in _GMAPS_CACHE:
        return _GMAPS_CACHE[cache_key]
    try:
        # transit モードで日本の JR・私鉄・バス・地下鉄を含む経路検索を行う。
        # `departure_time` を過去時刻にすると 400 になるため、過去なら "now" にフォールバックする。
        # `departure_time` が aware なら real-now も aware に合わせる (naive と aware の比較エラー回避)。
        real_now = datetime.now(departure_time.tzinfo) if departure_time.tzinfo else datetime.now()
        depart_arg: Any = departure_time if departure_time > real_now else "now"
        result = client.directions(
            (lat1, lng1), (lat2, lng2),
            mode="transit",
            departure_time=depart_arg,
            language="ja",
            alternatives=False,
        )
    except Exception as e:
        print(f"  gmaps directions 失敗: {e}", file=sys.stderr)
        return None

    if not result:
        # transit で経路が見つからない場合 (深夜帯や公共交通が届かない場所) は driving で再試行する。
        try:
            result = client.directions(
                (lat1, lng1), (lat2, lng2),
                mode="driving",
                departure_time=depart_arg,
                language="ja",
            )
        except Exception:
            return None
        if not result:
            return None
        leg = result[0]["legs"][0]
        seconds = float(leg["duration"]["value"])
        pair = (seconds, "gmaps-driving")
        _GMAPS_CACHE[cache_key] = pair
        return pair

    leg = result[0]["legs"][0]
    seconds = float(leg["duration"]["value"])
    pair = (seconds, "gmaps-transit")
    _GMAPS_CACHE[cache_key] = pair
    return pair


def _estimate_travel_seconds_haversine(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> tuple[float, str]:
    """フォールバックの実装。Haversine と距離レンジ別平均速度で下限を推定する。

    Haversine 直線距離に `ROAD_DISTANCE_FACTOR` を掛けて実道路距離を推定し、距離レンジで手段を
    自動選択したうえで、手段固有の乗換・準備オーバーヘッドを加える。
    """
    straight_m = _distance_m(lat1, lng1, lat2, lng2)
    road_m = straight_m * ROAD_DISTANCE_FACTOR

    if road_m < 500:
        seconds = road_m / (5000 / 3600)
        return seconds, "walk"
    if road_m < 30_000:
        seconds = road_m / (40_000 / 3600) + 5 * 60
        return seconds, "car/local"
    if road_m < 500_000:
        seconds = road_m / (200_000 / 3600) + 30 * 60
        return seconds, "shinkansen"
    seconds = road_m / (500_000 / 3600) + 90 * 60
    return seconds, "flight"


def estimate_travel_seconds(
    lat1: float, lng1: float, lat2: float, lng2: float,
    departure_time: datetime | None = None,
) -> tuple[float, str]:
    """2 点間の常識的な最短移動時間 [秒] と使用手段名を返す。

    `GMAPS_KEY` があれば Google Maps Directions API で実移動時間を取得する。
    `departure_time` を渡せば、始発待ちや終電の運行時刻も加味された duration が返る。
    キーが無い場合や API 失敗時は Haversine と距離レンジ別平均速度にフォールバックする。
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


# 交通機関の稼働時間帯。深夜帯 (24:00-06:00) は移動不可とする。
# 早朝始発以前の長距離移動は JR も私鉄も動いておらず、車移動でも人間の睡眠時間帯にあたるため不自然になる。
TRAVEL_ACTIVE_START_HOUR = 6
TRAVEL_ACTIVE_END_HOUR = 24  # 24 = 翌 0 時。24 時ちょうどを非稼働の開始として扱う。

# 1 スポットあたりの滞在時間の想定範囲 [秒]。店に入り、チェックインし、買い物や食事をしてから
# 次のスポットへ移動する自然な滞在を模擬する。
STAY_DURATION_MIN_SEC = 10 * 60
STAY_DURATION_MAX_SEC = 30 * 60


def natural_stay_seconds() -> float:
    """1 スポットあたりの滞在時間を [10 分, 30 分] の一様分布で返す。"""
    return random.uniform(STAY_DURATION_MIN_SEC, STAY_DURATION_MAX_SEC)


def next_arrival_time(now: datetime, travel_seconds: float) -> datetime:
    """`now` から `travel_seconds` 移動した場合の現実的な到着時刻を返す。

    現実の交通機関は深夜帯 (24:00-06:00) に動かない。「移動の途中で駅に泊まって朝に再開」は
    不可能なので、出発時点で「今日中に完了できない旅」は旅そのものを翌朝 06:00 発へ
    押し戻す。翌日も収まらないケース (24 時間を超える移動) はさらに翌日へ押される。
    """
    if travel_seconds <= 0:
        return now

    cursor = now
    # 早朝すぎる場合は始発想定の 06:00 まで待機する
    if cursor.hour < TRAVEL_ACTIVE_START_HOUR:
        cursor = cursor.replace(
            hour=TRAVEL_ACTIVE_START_HOUR, minute=0, second=0, microsecond=0
        )

    while True:
        # 今日の稼働終了時刻 (24:00 = 翌日 00:00)
        day_end = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        arrival = cursor + timedelta(seconds=travel_seconds)
        if arrival <= day_end:
            return arrival
        # 今日中に着かない場合は、旅程ごと翌朝 06:00 発へ押し戻す
        cursor = day_end.replace(hour=TRAVEL_ACTIVE_START_HOUR)


def parse_checkin_deadline(spot: dict[str, Any]) -> datetime | None:
    """`spot["checkin_end_datetime"]` を JST の aware datetime にパースする。失敗時は None を返す。

    サーバは `YYYY-MM-DD HH:MM:SS` 形式 (JST) で返してくるが、ISO8601 `Z` 付きの
    レスポンスに切り替わった場合にも扱えるよう isoformat も試す。パース失敗時は
    呼び出し側で「fail closed = 期限判定できないなら実 POST を止める」扱いにする。
    """
    raw = spot.get("checkin_end_datetime")
    if not raw:
        return None
    if not isinstance(raw, str):
        return None
    # 1) 現行仕様の "YYYY-MM-DD HH:MM:SS" (JST 前提)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=JST)
        except ValueError:
            pass
    # 2) ISO 8601 (Z 付き、あるいは offset 付き)
    iso = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


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
#     "completed_spots": ["cg_vote2026_XX", ...]   # 過去にチェックイン成功したスポット slug の集合
#   }
#
# LAST_CHECKIN_SCHEMA_VERSION は last_checkin レコードのバージョン。過去に dry-run 経路が
# state を書いていた版があり、last_checkin の中身だけでは実 POST 由来かどうか判別できない。
# 現行版 (このコード) は実 POST 成功時のみこのフィールドを書くので、値が最新版と一致する
# レコードだけを resume に使う。値が異なる場合や存在しない場合は「信頼できない旧 state」として
# lat/lng/resume_at を無視する。
LAST_CHECKIN_SCHEMA_VERSION = 2
_STATE_FILENAME = "canvasser_state.json"


class StateFileCorruptedError(Exception):
    """state.json は読めるが JSON として壊れているケース。実行モードでは fail closed する。"""


class FailClosedError(Exception):
    """実行モードで安全に継続できない状況で送出する。

    `process_account` でキャッチして exit_code=1 に反映する。タスクスケジューラや
    運用ログ上でも「正常終了」に見えないよう、確実に nonzero で抜けるための例外となる。

    partial_gained は、この例外を投げるまでに集計済みの獲得見込み (実 POST 成功済み分) を持つ。
    `collect_checkins` が途中で fail-closed する場合でも、サーバ側で成功した POST の
    reward を集計から落とさないために保持する。例外を投げない通常経路の gained と
    合流させる責務は呼び出し側にある。
    """

    def __init__(self, msg: str, partial_gained: int = 0) -> None:
        super().__init__(msg)
        self.partial_gained = partial_gained


class UserInputError(Exception):
    """CLI 引数などユーザー入力に起因するエラー。

    `main()` が短いメッセージと exit 1 に丸めるための専用クラス。実装バグとしての
    `ValueError` を丸め込まないよう、入力検証系はこの例外を使う。
    """


# state.json の期待スキーマ。strict モードではこれをチェックし、通らないものは
# 破損扱いで fail closed とする。
_SPOT_SLUG_RE = re.compile(r"^cg_vote2026_[0-9]{1,6}$")


def _validate_state_schema(state: dict[str, Any], source: Path) -> None:
    """load 時にスキーマを検証する。壊れていれば StateFileCorruptedError を送出する。

    strict モード専用のガードとなる。dry-run では緩めに扱うため、この関数は呼ばれない。
    """
    completed = state.get("completed_spots")
    if completed is not None:
        if not isinstance(completed, list):
            raise StateFileCorruptedError(
                f"{source}: completed_spots が list ではなく {type(completed).__name__}"
            )
        for slug in completed:
            if not isinstance(slug, str) or not _SPOT_SLUG_RE.fullmatch(slug):
                raise StateFileCorruptedError(
                    f"{source}: completed_spots に不正な slug {slug!r}"
                )
    last = state.get("last_checkin")
    if last is not None:
        if not isinstance(last, dict):
            raise StateFileCorruptedError(f"{source}: last_checkin が dict でない")
        for k, typ in (
            ("schema_version", int),
            ("spot_slug", str),
            ("spot_name", str),
            ("location_latitude", (int, float)),
            ("location_longitude", (int, float)),
            ("virtual_completed_at", str),
        ):
            v = last.get(k)
            if v is None:
                continue  # 部分的な dict は許容する (旧 schema 互換のため)
            if not isinstance(v, typ):
                raise StateFileCorruptedError(
                    f"{source}: last_checkin.{k} の型が不正 (期待 {typ})"
                )
        # spot_slug がある場合は正規表現マッチも要求する
        slug = last.get("spot_slug")
        if isinstance(slug, str) and not _SPOT_SLUG_RE.fullmatch(slug):
            raise StateFileCorruptedError(
                f"{source}: last_checkin.spot_slug {slug!r} が cg_vote2026_NNNN 形式でない"
            )
        # virtual_completed_at がある場合は datetime としてパース可能か検証する
        vca = last.get("virtual_completed_at")
        if isinstance(vca, str):
            try:
                datetime.fromisoformat(vca)
            except ValueError as e:
                raise StateFileCorruptedError(
                    f"{source}: last_checkin.virtual_completed_at {vca!r} が"
                    f" ISO8601 として不正: {e}"
                ) from e


def load_account_state(profile_dir: Path, strict: bool = False) -> dict[str, Any]:
    """`profile_dir/canvasser_state.json` を読み込む。

    - 存在しない場合は空 dict を返す。
    - JSON パース失敗時は、`strict=False` なら空 dict を返す (dry-run 用に緩め扱い)。
      `strict=True` の場合は `StateFileCorruptedError` を送出する (`--execute` の fail closed)。
    - `strict=True` の場合は追加で schema 検証も行う。
    """
    state_file = profile_dir / _STATE_FILENAME
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        if strict:
            raise StateFileCorruptedError(f"{state_file}: {e}") from e
        return {}
    if not isinstance(data, dict):
        if strict:
            raise StateFileCorruptedError(f"{state_file}: トップレベルが dict でない")
        return {}
    if strict:
        _validate_state_schema(data, state_file)
    return data


def save_account_state(profile_dir: Path, state: dict[str, Any]) -> None:
    """`profile_dir/canvasser_state.json` に atomic に書き出す。

    同じディレクトリに一時ファイルを作り、fsync の後に `os.replace` で置換する。
    書き込み中にプロセスがクラッシュしても既存ファイルは壊れない。
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    state_file = profile_dir / _STATE_FILENAME
    fd, tmp_path = tempfile.mkstemp(
        prefix=".canvasser_state-", suffix=".tmp", dir=str(profile_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_file)
    except Exception:
        # 失敗時は一時ファイルを掃除して例外を伝播する (state_file は変更しない)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def update_checkin_state(
    profile_dir: Path,
    spot: dict[str, Any],
    virtual_now: datetime,
    mark_completed: bool = True,
) -> None:
    """1 件チェックインに成功したら state を更新する。

    - `last_checkin`：最終チェックイン地点と仮想時刻・実時刻。
    - `completed_spots`：`mark_completed=True` の場合、`spot.slug` を集合に追加する。
      これで次回起動時に同スポットへの実 POST を事前に skip できる。
    """
    state = load_account_state(profile_dir)
    state["last_checkin"] = {
        "schema_version": LAST_CHECKIN_SCHEMA_VERSION,
        "spot_slug": spot["slug"],
        "spot_name": spot["name"],
        "location_latitude": float(spot["location_latitude"]),
        "location_longitude": float(spot["location_longitude"]),
        "virtual_completed_at": virtual_now.isoformat(),
        "real_completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if mark_completed:
        completed = set(state.get("completed_spots") or [])
        completed.add(spot["slug"])
        state["completed_spots"] = sorted(completed)
    save_account_state(profile_dir, state)


def resume_context(
    profile_dir: Path, strict: bool = False
) -> tuple[float | None, float | None, datetime | None, set[str]]:
    """state.json から前回位置・仮想終了時刻・完了済みスポット集合を復元する。

    戻り値は `(last_lat, last_lng, resume_at, completed_spots)`。
      - `last_lat` と `last_lng`：前回最終チェックイン地点。state が空なら None。
      - `resume_at`：前回終了時刻 (仮想の JST aware)。
                     スキーマ上は「滞在時間を含めない出発直前の仮想時刻」を保存する。
      - `completed_spots`：実 POST 成功済みスポット slug の集合。起動時の事前フィルタに使う。
        旧版の dry-run 経路も `update_checkin_state` を叩いていたため、`last_checkin`
        フィールドから「実 POST 成功済み」を後方から推定する手段がない。誤って dry-run 由来の
        slug を完了扱いすると次回 `--execute-checkin` でそのスポットの reward を落とすため、
        自動移行はしない。旧 state の補完が必要な場合は `--mark-completed` を明示的に使う。
    """
    state = load_account_state(profile_dir, strict=strict)
    last = state.get("last_checkin") or {}
    # schema_version が一致しない last_checkin は「実 POST 成功由来か」を保証できないため
    # resume には使わない。旧 dry-run が simulated route を書いた state を execute run で
    # 起点にしてしまうと、偽の位置や時刻から始まり有効スポットを skip する事故になる。
    schema_ok = last.get("schema_version") == LAST_CHECKIN_SCHEMA_VERSION
    lat = last.get("location_latitude") if schema_ok else None
    lng = last.get("location_longitude") if schema_ok else None
    raw = last.get("virtual_completed_at") if schema_ok else None
    resume_at: datetime | None = None
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=JST)
            resume_at = parsed.astimezone(JST)
        except ValueError:
            resume_at = None
    completed = set(state.get("completed_spots") or [])
    return (
        float(lat) if lat is not None else None,
        float(lng) if lng is not None else None,
        resume_at,
        completed,
    )


def mark_spots_completed(profile_dir: Path, slugs: list[str]) -> None:
    """外部から手動で成功済みスポットを state に登録するための CLI 用ヘルパー。

    実観測せずに「サーバ側では既に成功済み」と分かっているケース (別デバイスや
    UI キャプチャ経由で checkin した分) を state に流し込むために使う。
    `strict=True` で読み込むため、既存の state.json が破損している場合は
    `StateFileCorruptedError` で失敗する (破損 state を空 dict で上書きするのを防ぐ)。
    """
    invalid = [s for s in slugs if not _SPOT_SLUG_RE.fullmatch(s)]
    if invalid:
        raise UserInputError(f"不正な spot_slug: {invalid}")
    state = load_account_state(profile_dir, strict=True)
    completed = set(state.get("completed_spots") or [])
    completed.update(slugs)
    state["completed_spots"] = sorted(completed)
    save_account_state(profile_dir, state)
    print(f"[{profile_dir.name}] completed_spots に追加: {sorted(slugs)}")


def ensure_chromium_installed() -> None:
    """Playwright の Chromium バイナリが未取得なら `playwright install chromium` を走らせる。

    `uv run` の初回起動でも自動で解決したいので、冪等に呼べるようにしておく。
    """
    with sync_playwright() as p:
        exe = p.chromium.executable_path
        if exe and Path(exe).exists():
            return

    print("Chromium バイナリを取得します (初回のみ)...", file=sys.stderr)
    subprocess.check_call(
        [sys.executable, "-m", "playwright", "install", "chromium"]
    )


def run_login_flow(page: Page, timeout_sec: int = 600, interval_sec: float = 3.0) -> int:
    """headed で起動し、ログイン成功を is_login フラグでポーリング検知する。

    対話入力に頼らないので、bash-input のような非対話環境でも動作する。
    ブラウザを閉じるか Ctrl+C で中断できる。
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
        try:
            if check_login(page):
                print(
                    "ログイン状態を確認しました。次回から --login なしで実行できます。",
                    file=sys.stderr,
                )
                return 0
        except PlaywrightError:
            # ページ遷移中や、ログイン画面へのリダイレクト中は fetch が失敗する。
            # 次のポーリングを待つ。
            pass
        time.sleep(interval_sec)

    print(
        "タイムアウト。ログインを検出できませんでした。再度お試しください。",
        file=sys.stderr,
    )
    return 1


# アカウント名の許容文字集合。パストラバーサル (../) や絶対パス指定を排除するため、
# basename として安全な文字だけを許可する。
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _validate_account_name(account: str) -> None:
    if not _ACCOUNT_NAME_RE.fullmatch(account):
        raise UserInputError(
            f"--account の値 {account!r} は許可されていません。"
            "使える文字は英数字・'_'・'-'・'.' のみで、長さは 1〜64 文字です。"
        )
    # 追加の防御として、'.' や '..' 単体、パス区切り文字を含むケースを弾く
    if account in (".", "..") or any(sep in account for sep in ("/", "\\")):
        raise UserInputError(
            f"--account の値 {account!r} はパスとして危険なため許可されません。"
        )


def _ensure_within(base: Path, candidate: Path) -> None:
    """`candidate` が `base` の子孫ディレクトリであることを検証する。そうでなければ UserInputError を送出する。"""
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError as e:
        raise UserInputError(
            f"プロファイル保存先 {candidate} が profiles-dir {base} の外に逃げています。"
        ) from e


def _profiles_dir_is_gitignored(profiles_dir: Path) -> bool:
    """`profiles_dir` が git ignore 対象であれば True を返す。

    `profiles_dir` がまだ存在しない (初回 `--login` 前) ケースでも判定したいので、path 末尾
    に `/` を付けてディレクトリ扱いを git に明示する。`.gitignore` の `profiles/` のような
    ディレクトリ限定パターンは、path 側が「ディレクトリと分かる」形でないと match しないため。

    git repo 外の場合は False を返す (誤コミット経路がないので判定不能扱いとし、拒否側にする)。
    git 自体が使えない環境も False とする。`git check-ignore --quiet` の exit code は
    0=ignored、1=not ignored、128=error (repo 外) を返す。
    """
    # git の実体を PATH から解決する。S607 対策で partial path を渡さない。
    git_bin = shutil.which("git")
    if git_bin is None:
        return False
    # Windows の path 区切りは git に渡す前に正規化する。末尾 / でディレクトリと明示する。
    path_arg = str(profiles_dir).replace("\\", "/").rstrip("/") + "/"
    parent = profiles_dir.parent
    cwd = parent if parent.is_dir() else Path.cwd()
    try:
        # `--` を挟むことで、`-` で始まるユーザー指定パスが option 扱いされないようにする。
        # 引数リストは shell=False で渡すため、shell injection は起こらない。
        result = subprocess.run(  # noqa: S603
            [git_bin, "check-ignore", "--quiet", "--", path_arg],
            cwd=cwd,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def resolve_profiles(
    profiles_dir: Path,
    account: str | None,
) -> list[tuple[str, Path]]:
    """CLI 引数から処理対象のプロファイル一覧 `[(表示名, ディレクトリ), ...]` を決定する。

    - `--account NAME` を指定すれば 1 アカウント固定となる (basename 文字種を検証する)。
    - 未指定なら `--profiles-dir` 配下のサブディレクトリを全列挙する (複数アカウント運用)。
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
        # 既存ディレクトリ名も同じ規則で検証する (手動作成された悪性ディレクトリ対策)。
        if not _ACCOUNT_NAME_RE.fullmatch(entry.name):
            print(
                f"[warn] プロファイル名 {entry.name!r} が命名規則に合致しないため skip します。",
                file=sys.stderr,
            )
            continue
        target = entry.resolve()
        _ensure_within(profiles_dir, target)
        result.append((entry.name, target))
    return result


def open_persistent_context(p, profile_dir: Path, headless: bool):
    """`persistent_context` を開く。Chromium が未取得の場合は install してからリトライする。"""
    kwargs: dict[str, Any] = {
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
    }
    try:
        return p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    except PlaywrightError as e:
        if "playwright install" in str(e).lower():
            subprocess.check_call(
                [sys.executable, "-m", "playwright", "install", "chromium"]
            )
            return p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        raise


def process_account(
    p: Any,
    name: str,
    profile_dir: Path,
    login_mode: bool,
    run_mission: bool,
    run_checkin: bool,
    execute_mission: bool,
    execute_checkin: bool,
    daily_budget: int,
    consecutive_failure_limit: int,
    out_of_range_limit: int,
) -> tuple[int, int]:
    """1 アカウント分の処理を行う。戻り値は `(獲得票数, exit_code)`。

    - `login_mode=True` のときは獲得票数を集計しない (戻り値は獲得 0)。
    - 未ログイン検知時は `exit_code=1` を返し、呼び出し側で他アカウントに進む。
    - `run_mission=True` でミッション回収、`run_checkin=True` でチェックインを実施する。
    - `execute_mission` と `execute_checkin` はそれぞれ独立した実 POST ゲート。
      両方 False なら完全ドライランとなる (GET のみで、POST/PUT は送らない)。
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    ctx = open_persistent_context(p, profile_dir, headless=not login_mode)
    try:
        page = ctx.new_page()
        page.goto(MISSION_PAGE_URL, wait_until="domcontentloaded")

        if login_mode:
            return 0, run_login_flow(page)

        if not check_login(page):
            print(
                f"[{name}] 未ログイン。"
                f"`uv run canvasser.py --login --account {name}` を実行してください。",
                file=sys.stderr,
            )
            return 0, 1

        gained = 0
        exit_code = 0
        if run_mission:
            # 実 POST を送らないドライランの見込み枚数は集計に混ぜない (アカウント総計を汚さない)。
            mission_gain = collect_missions(page, execute=execute_mission)
            if execute_mission:
                gained += mission_gain
        if run_checkin:
            # チェックインページにも navigate しておく (Referer を合わせる意図)
            page.goto(CHECKIN_PAGE_URL, wait_until="domcontentloaded")
            try:
                checkin_gain = collect_checkins(
                    page,
                    execute=execute_checkin,
                    daily_budget=daily_budget,
                    consecutive_failure_limit=consecutive_failure_limit,
                    out_of_range_limit=out_of_range_limit,
                    profile_dir=profile_dir,
                )
                if execute_checkin:
                    gained += checkin_gain
            except FailClosedError as e:
                # state 破損や deadline パース不能など、「安全に継続できない」ケース。
                # ログを stderr に流し、exit_code を nonzero に切り替える。
                # fail-closed 前にサーバ側で成功済みの POST 分は `e.partial_gained` に
                # 入っているので、集計から落とさない。
                print(f"[{name}] fail closed: {e}", file=sys.stderr)
                if execute_checkin:
                    gained += e.partial_gained
                exit_code = 1
        return gained, exit_code
    finally:
        ctx.close()


def main() -> int:
    """CLI エントリポイント。入力検証由来の `UserInputError` だけを短いメッセージに変換する。

    実装バグ由来の他の例外 (`ValueError` を含む) はそのまま通し、traceback で表示する。
    """
    try:
        return _main_impl()
    except UserInputError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1


def _main_impl() -> int:
    parser = argparse.ArgumentParser(
        description="シンデレラガール総選挙2026 デイリーミッション自動回収 (複数アカウント対応)"
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="初回ログイン用。Chromium を可視状態で起動する (--account とセットで指定する)",
    )
    parser.add_argument(
        "--account",
        help="対象アカウント名。profiles-dir 配下のサブディレクトリ名として扱う。"
             "未指定なら profiles-dir 内のすべてのアカウントを順次処理する",
    )
    parser.add_argument(
        "--profiles-dir",
        default="./profiles",
        help="複数アカウントの親ディレクトリ (デフォルト: ./profiles)",
    )
    parser.add_argument(
        "--checkin",
        action="store_true",
        help="チェックイン (#99) も処理する。未指定時はミッションのみ処理する (dry-run または本番)。",
    )
    parser.add_argument(
        "--no-mission",
        action="store_true",
        help="ミッション回収をスキップする。--checkin と組み合わせて使う",
    )
    parser.add_argument(
        "--execute-mission",
        action="store_true",
        help="ミッションを実 POST/PUT する。未指定はドライラン (GET のみ)。",
    )
    parser.add_argument(
        "--execute-checkin",
        action="store_true",
        help="チェックインを実 POST する。未指定はドライラン (GET のみで、sleep も skip)。",
    )
    parser.add_argument(
        "--daily-budget",
        type=int,
        default=0,
        help="1 回の実行あたりのチェックイン実 POST 試行回数の上限 "
             "(デフォルト: 0 = 無制限)。未観測 ecode・失敗・成功のいずれも 1 リクエスト = 1 消費。"
             "時間帯制約 (深夜移動不可) で自然に上限がかかるため、通常は指定不要。"
             "緊急停止したいときにだけ小さな値を指定する。",
    )
    parser.add_argument(
        "--consecutive-failure-limit",
        type=int,
        default=1,
        help="未観測 ecode が連続で何件出たら全体を中断するか (デフォルト: 1 = 1 件目で即停止)。",
    )
    parser.add_argument(
        "--max-out-of-range",
        type=int,
        default=3,
        help="E5005 (範囲外) の累積が何件で停止するか (デフォルト: 3)。crypto や座標の"
             "実装不一致で 51 件全部を撃たないための安全弁。",
    )
    parser.add_argument(
        "--mark-completed",
        metavar="SLUG1,SLUG2,...",
        help="実 POST 済みスポットを state.completed_spots に手動追加して終了する。"
             "--account NAME と組み合わせて使う。例: "
             "--account syota --mark-completed cg_vote2026_17,cg_vote2026_19",
    )
    parser.add_argument(
        "--allow-unignored-profiles-dir",
        action="store_true",
        help="--profiles-dir が git ignore 対象でない場合の警告を無視する。"
             "デフォルトはモードに関係なく未 ignore の profiles-dir を拒否する "
             "(Cookie 誤コミット防止)。",
    )
    args = parser.parse_args()

    # 上限系 CLI 引数の範囲検証。--daily-budget は 0=無制限扱いだが、負数を許すと
    # limit_counter 判定が常時 truthy になり、実 POST 上限が壊れる。その他の閾値も
    # 1 未満だと本来の役割 (連続失敗打ち切り、範囲外累積打ち切り) を果たせない。
    if args.daily_budget < 0:
        print("--daily-budget は 0 以上を指定してください。", file=sys.stderr)
        return 1
    if args.consecutive_failure_limit < 1:
        print(
            "--consecutive-failure-limit は 1 以上を指定してください。",
            file=sys.stderr,
        )
        return 1
    if args.max_out_of_range < 1:
        print("--max-out-of-range は 1 以上を指定してください。", file=sys.stderr)
        return 1

    profiles_dir = Path(args.profiles_dir).resolve()

    # --mark-completed は state を編集して即終了する (ブラウザ起動なし)。
    if args.mark_completed is not None:
        if args.account is None:
            print(
                "--mark-completed は --account NAME と組み合わせて使ってください。",
                file=sys.stderr,
            )
            return 1
        _validate_account_name(args.account)
        target_dir = (profiles_dir / args.account).resolve()
        _ensure_within(profiles_dir, target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        slugs = [s.strip() for s in args.mark_completed.split(",") if s.strip()]
        try:
            mark_spots_completed(target_dir, slugs)
        except StateFileCorruptedError as e:
            print(
                f"state.json が破損しているため --mark-completed で上書きできません: {e}",
                file=sys.stderr,
            )
            return 1
        # UserInputError は main() のトップレベルで捕捉される。
        return 0

    profiles = resolve_profiles(profiles_dir, args.account)

    if not profiles:
        print(
            f"プロファイルが見つかりません ({profiles_dir})。\n"
            "初回は `uv run canvasser.py --login --account NAME` でアカウントを追加してください。",
            file=sys.stderr,
        )
        return 1

    if args.login and args.account is None:
        print(
            "--login は --account で 1 アカウントを指定してください。",
            file=sys.stderr,
        )
        return 1

    # 実行対象を判定する。`--checkin` と `--no-mission` の組み合わせで挙動を切り替える。
    run_mission = not args.no_mission
    run_checkin = args.checkin
    if args.no_mission and not args.checkin:
        print(
            "--no-mission は --checkin と組み合わせて使ってください。",
            file=sys.stderr,
        )
        return 1
    # 実行ゲート単独指定を拒否する。対応する対象フラグが無いと黙って dry-run で終わり、
    # 「実 POST を送ったつもりが送られていない」誤運用に繋がるため。
    if args.execute_mission and not run_mission:
        print(
            "--execute-mission は --no-mission と併用できません。",
            file=sys.stderr,
        )
        return 1
    if args.execute_checkin and not run_checkin:
        print(
            "--execute-checkin は --checkin と組み合わせて使ってください。",
            file=sys.stderr,
        )
        return 1

    # `profiles_dir` が git ignore 対象か検証する。Cookie 書き込みは login や実 POST 経路
    # だけでなく、GET のみのドライラン中の persistent context 経由でも起こりうる
    # (Playwright は cookie・cache・metadata を随時同期する)。そのため実行モードに関係なく、
    # gitignore 未対応なら停止させる。
    if not args.allow_unignored_profiles_dir:
        if not _profiles_dir_is_gitignored(profiles_dir):
            print(
                f"{profiles_dir} が git ignore 対象になっていません。"
                "Cookie 入り persistent profile がコミットされる恐れがあります。"
                "既定の ./profiles を使うか、.gitignore に追加してから再実行してください。"
                "自己責任で続行するなら --allow-unignored-profiles-dir を付けてください。",
                file=sys.stderr,
            )
            return 1

    ensure_chromium_installed()

    exit_code = 0
    results: list[tuple[str, int]] = []
    with sync_playwright() as p:
        for name, profile_dir in profiles:
            print(f"\n=== アカウント: {name} ({profile_dir}) ===")
            try:
                gained, code = process_account(
                    p,
                    name,
                    profile_dir,
                    args.login,
                    run_mission=run_mission,
                    run_checkin=run_checkin,
                    execute_mission=args.execute_mission,
                    execute_checkin=args.execute_checkin,
                    daily_budget=args.daily_budget,
                    consecutive_failure_limit=args.consecutive_failure_limit,
                    out_of_range_limit=args.max_out_of_range,
                )
            except Exception as e:  # 1 アカウントの失敗で全体を止めない
                print(f"[{name}] 実行中に例外: {e}", file=sys.stderr)
                exit_code = 1
                results.append((name, 0))
                continue
            results.append((name, gained))
            if code != 0:
                exit_code = code
            if args.login:
                # ログインは 1 アカウントのみ。process_account の戻り値をそのまま返す。
                return code

    if len(profiles) > 1:
        print("\n=== サマリ ===")
        total = 0
        for name, gained in results:
            print(f"  {name}: +{gained}枚")
            total += gained
        print(f"  合計: +{total}枚")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

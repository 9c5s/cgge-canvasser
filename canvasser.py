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
内部 API をそのまま呼び出してミッション回収と (--checkin ならば) チェックインを自動化する。
複数アカウントは ./profiles/{account}/ 配下に分けて運用する。

  uv run canvasser.py --login --account main               # 初回ログイン
  uv run canvasser.py                                      # 全アカウント、完全ドライラン
  uv run canvasser.py --execute-mission                    # ミッションだけ本番
  uv run canvasser.py --checkin --execute-checkin          # チェックインだけ本番
  uv run canvasser.py --checkin --execute-mission --execute-checkin  # 両方本番

`--execute-mission` と `--execute-checkin` は独立したゲート。どちらも未指定なら
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

# 地球の 1 度緯度に対応する距離 [m]。WGS84 近似。
METERS_PER_DEG_LAT = 111_320.0
# 許容半径ぎりぎりの座標を避けて境界事故を防ぐ内寄せ係数。
CHECKIN_RADIUS_MARGIN = 0.85


def _default_now() -> datetime:
    """collect_checkins の now_fn 既定値。テスト時は差し替える。"""
    return datetime.now(JST)


def call_api(page: Page, method: str, path: str) -> dict[str, Any]:
    """ページ内で fetch を実行し、Cookie 付きで API を叩く。

    - 送信元と API は別ホストだが、サーバ側で CORS allow-credentials が有効なので
      credentials=include で通る。x-api-key はフロント公開の固定値。
    - fetch reject (ネットワーク・CORS・DNS 失敗) や非 JSON 応答も {status, body, error} で
      構造化して返し、呼び出し側で status==0 やエラーコードで分岐できるようにする。
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

    fetch 例外や非 JSON 応答は未ログインと同義に丸める。Cookie 期限切れやネットワーク不通も
    「未ログインなら --login を促す」ルートに合流させるため。
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

    - `mission_complete_api_call_flag=True` のみを対象にする。あいことばやチェックインは
      外部トリガー起因のため、この関数からは触らない。
    - `execute=False` (デフォルト) は完全ドライラン。GET のみ実行し、POST/PUT は送らない。

    戻り値は今回獲得した投票券数の合計 (dry-run 時は実行した場合の見込み)。
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
            # 累計達成数系ミッション (#100-104) はサーバ側で自動達成されるが、一覧の completed
            # フラグ更新が遅延する。達成 POST が「既に達成済み」を返した場合も受取 PUT は通る。
            if outcome in ("ok", "already_done"):
                gained += _receive(page, mid, name, pts, execute=execute)

    label = "獲得見込み" if not execute else "獲得"
    print(f"ミッション {label}: {gained}枚")
    return gained


def _complete(page: Page, mid: int, name: str, execute: bool = False) -> str:
    """ミッション達成の POST を送る。

    一覧取得は `/missions` (複数形) だが個別操作は `/mission` (単数形)。フロントの `b.eq`
    定数が単数形であることをチャンク解析で確認済み。

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
    """投票券受取の PUT を送る。成功時は加算票数、dry-run 時は「実行していれば得た pts」を返す。"""
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

    パスワードはフロント公開値の X-API-KEY を流用しているため、この暗号化に機密性はない
    (サーバが受理する形式を再現するだけのラッパー)。salt と iv は hex、ciphertext は
    Base64 で連結する形式は、実 UI 経由の POST 観測で確定した。
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
    """半径 radius_m の円内から面積一様分布でランダム点を返す。

    d = r*sqrt(u) で中心密集を避けた面積一様分布を作る。経度スケールは緯度で変わるため
    cos(lat) で補正する。
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
    """モバイル GPS 屋外の実測分布に近い精度値 [m] を返す。

    実機の accuracy は 8〜30m に強く集中し、稀に 30〜80m の外れ値が出る。一様乱数だと
    分布がフラットで統計的に不自然なので、正規分布と外れ値の混合で再現する。
    """
    if random.random() < 0.15:
        # 屋内寄り or マルチパスの影響を想定した外れ値
        val = random.uniform(30.0, 80.0)
    else:
        # 5m 未満は現実的でないためクランプする
        val = max(5.0, random.gauss(18.0, 6.0))
    return round(val, 3)


def _natural_altitude() -> tuple[float | None, float | None]:
    """altitude と altitudeAccuracy を実機挙動に近い分布で返す。

    多くの環境 (80%) は取得不可の None、20% は日本の都市部平地相当の 5〜80m を返す。
    """
    if random.random() < 0.20:
        alt = round(random.uniform(5.0, 80.0), 1)
        alt_acc = round(random.uniform(20.0, 50.0), 1)
        return alt, alt_acc
    return None, None


def make_checkin_coords(spot: dict[str, Any]) -> dict[str, Any]:
    """スポット情報から、円内ランダム点と自然化した coords を組む。

    サーバ側や将来の異常検知で分布統計が取られた場合に BOT らしい特徴が出ないよう、
    accuracy と altitude を実機挙動に近い分布で乱択する。
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
    """checkins 系エンドポイントを叩く。

    body があれば application/x-www-form-urlencoded で送る (axios が data:string を
    送るときの既定 Content-Type を実 UI キャプチャで確定した)。body は
    "salt_hex,iv_hex,ct_base64" の文字列をそのまま (URL エンコードせずに) 載せる。
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


# チェックイン API の既知エラーコード。UI 側チャンクから "E5005" (範囲外) は把握済み。
# それ以外 (既達成含む) は未観測 = unknown として即停止で扱い、実観測で意味が確定した
# ecode だけを随時 ECODES_ALREADY_DONE に追加する。
ECODE_OUT_OF_RANGE = "E5005"
ECODES_ALREADY_DONE: tuple[str, ...] = ()


def order_spots_by_proximity(
    spots: list[dict[str, Any]],
    start_index: int | None = None,
    start_location: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
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
      - `execute=False` (デフォルト) は完全ドライラン (POST を送らず、sleep もなく、
        state も書き換えない)。
      - `state.completed_spots` にある slug は事前に skip する。
      - 未観測 ecode (SUCCESS と E5005 以外) は fail closed で即停止する。BAN シグナル・
        認証切れ・予期せぬ状態のいずれかとして扱う。
      - `consecutive_failure_limit` の未知エラーが連続したら全体中断する (デフォルト 1)。
      - `daily_budget > 0` で件数上限を設ける (0 は無制限)。
      - スポット間には交通機関稼働時間帯 (06:00-24:00) を考慮した移動待機を挟む。
      - `checkin_end_datetime` を過ぎたスポットは skip する (イベント全体期限とは別)。
      - deadline がパースできないスポットは `execute=True` の場合に fail closed で扱う。
      - 各スポットで 10〜30 分の滞在時間を挟む (最終スポットや budget 到達時は skip)。
      - state.json は実 POST 成功時に限り atomic に更新する。
      - state.json 破損時は `execute=True` で FailClosedError を送出し、dry-run では
        空 state で継続する。
      - `out_of_range_limit` 件以上 E5005 が続いたら停止する (crypto・body・radius の
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
            # dry-run は空 state のまま継続してスポット取得後の検証を回す
            print(msg, file=sys.stderr)

    skipped_completed = [s for s in all_spots if s["slug"] in completed_spots]
    spots = [s for s in all_spots if s["slug"] not in completed_spots]
    if skipped_completed:
        print(f"事前 skip (完了済み): {len(skipped_completed)}件")
    if not spots:
        print("全スポット完了済みです。")
        return 0

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
    attempted = 0  # execute=True でのみ加算する実 POST 試行回数
    consecutive_failures = 0
    out_of_range_count = 0

    # 「前回 23:50 に終わって翌日 12:00 に再開」を自然な連続扱いにするため、
    # resume_at が現在時刻より進んでいれば resume_at を採用する。
    virtual_now: datetime = now_fn()
    if virtual_now.tzinfo is None:
        virtual_now = virtual_now.replace(tzinfo=JST)
    if resume_at is not None and resume_at > virtual_now:
        virtual_now = resume_at
    resumed = start_loc is not None
    print(f"開始時刻(仮想): {virtual_now:%Y-%m-%d %H:%M %Z}"
          + (f" (前回位置から再開: {resume_lat:.4f},{resume_lng:.4f})" if resumed else ""))

    prev_lat: float | None = resume_lat if resumed else None
    prev_lng: float | None = resume_lng if resumed else None

    for i, spot in enumerate(spots, 1):
        # 上限判定のカウンタを execute の状態で切替える。execute=True の attempted は
        # 「実 POST を厳密に daily_budget 回だけ送る」ためのゲートで、既達成・範囲外・
        # 失敗のいずれも 1 リクエスト = 1 消費として扱う。
        limit_counter = attempted if execute else successful
        if daily_budget > 0 and limit_counter >= daily_budget:
            print(f"  日次上限 {daily_budget}件に到達。残り {len(spots) - i + 1}件は次回以降。")
            break

        slug = spot["slug"]
        name = spot["name"]
        s_lat = float(spot["location_latitude"])
        s_lng = float(spot["location_longitude"])

        if prev_lat is not None:
            secs, mode = estimate_travel_seconds(
                prev_lat, prev_lng, s_lat, s_lng,
                departure_time=virtual_now,
            )
            if mode == "gmaps-transit":
                # transit の duration には始発待ち等が織り込まれているため、翌朝発への
                # 押し戻しをかけると二重加算になる。
                arrival = virtual_now + timedelta(seconds=secs)
            else:
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

        # 個別スポットの期限。イベント全体期限とは別で、超過したものだけ skip して
        # 後続の有効スポットは処理を続ける。パース不能は execute では fail closed に
        # 落として、サーバ形式変更を早期検知する (個別 skip では気付きにくい)。
        deadline = parse_checkin_deadline(spot)
        if deadline is None:
            msg = (
                f"[{slug}] checkin_end_datetime = {spot.get('checkin_end_datetime')!r} "
                "がパースできません。サーバ側の日付形式が変わった可能性があります。"
            )
            if execute:
                raise FailClosedError(msg, partial_gained=gained)
            print(f"  {msg} (dry-run: skip)", file=sys.stderr)
            # skip でも到着はしたので、次スポットの travel 起点を現地点に更新する
            prev_lat, prev_lng = s_lat, s_lng
            continue
        if virtual_now > deadline:
            print(f"  [{slug}] スポット期限 ({deadline:%m/%d %H:%M %Z}) 経過、skip。")
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
            """このスポットの後にループを継続するかを判定する。

            execute の True と False で使うカウンタが違う点だけ注意する
            (attempted と successful)。
            """
            if i >= len(spots):
                return False
            if daily_budget <= 0:
                return True
            counter = next_attempted if execute else next_successful
            return counter < daily_budget

        if not execute:
            # dry-run では state.json を書かない。実 POST 実行時の resume 起点を
            # ドライラン由来の値で汚染しないため。
            print(f"       body={body[:60]}...(len={len(body)})  [DRY-RUN]")
            gained += 10
            successful += 1
            prev_lat, prev_lng = s_lat, s_lng
            if will_continue_after(attempted, successful):
                virtual_now = virtual_now + timedelta(seconds=stay_secs)
                print(f"       滞在 {humanize_duration(stay_secs)} -> 出発 {virtual_now:%m/%d %H:%M}")
            continue

        # POST 直前に attempted を加算する。これで既達成・範囲外・失敗のいずれも
        # 1 リクエスト = 1 消費になり、daily_budget が実 POST 試行回数として厳密に働く。
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
            # sleep 中に中断されても completed_spots に slug が残るよう、成功直後に
            # 一次 state を保存する。
            if profile_dir is not None:
                update_checkin_state(profile_dir, spot, virtual_now)
            if will_continue_after(attempted, successful):
                time.sleep(stay_secs)
                virtual_now = virtual_now + timedelta(seconds=stay_secs)
                print(f"       滞在 {humanize_duration(stay_secs)} -> 出発 {virtual_now:%m/%d %H:%M %Z}")
                # 滞在後の virtual_now を state に反映する (同じ spot への上書き相当)
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
                # E5005 の多発は crypto・body・radius の実装不一致を強く示唆する。
                # 未指定なら 51 件全部を撃ってしまうため、ここで全体を止める。
                raise FailClosedError(
                    f"範囲外 (E5005) が {out_of_range_count} 件。crypto・body・radius の "
                    "実装不一致の疑いがあるため停止する。座標計算とペイロードの整合性を確認して "
                    "から --max-out-of-range を上げて再実行する。",
                    partial_gained=gained,
                )
            prev_lat, prev_lng = s_lat, s_lng
            continue

        if ecode in ECODES_ALREADY_DONE:
            print(f"       -> 既達成 ({ecode})、スキップ")
            consecutive_failures = 0
            prev_lat, prev_lng = s_lat, s_lng
            # サーバ側で成功済みなので state に反映しておくと次回以降 skip される
            if profile_dir is not None:
                update_checkin_state(profile_dir, spot, virtual_now, mark_completed=True)
            continue

        # 未観測 ecode = unknown。BAN シグナル・認証切れ・予期せぬ状態のいずれかとして
        # 扱い、fail closed で即中断する。意味が確定したら ECODE_* に追加してから再実行。
        consecutive_failures += 1
        err_note = f" err={res.get('error')}" if res.get("error") else ""
        print(
            f"       -> 未観測ecode (連続{consecutive_failures}件目): "
            f"HTTP {res['status']}{err_note} body={body_resp}",
            file=sys.stderr,
        )
        if consecutive_failures >= consecutive_failure_limit:
            raise FailClosedError(
                f"連続失敗が {consecutive_failure_limit}件に達したため中断する。"
                "body の ecode を確認して意味を確定してから再実行する。",
                partial_gained=gained,
            )
        # skip 時も移動時間は消費済み。prev を更新しないと clock は失敗スポット到着時刻・
        # origin は前スポットという物理的にあり得ない状態になる。
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


# 直線距離を実道路距離へ補正する係数
ROAD_DISTANCE_FACTOR = 1.35

# gmaps キャッシュ。キーの departure_time は 30 分単位に丸めてヒット率を確保する。
_GMAPS_CACHE: dict[tuple[float, float, float, float, str], tuple[float, str]] = {}
_gmaps_client: Any = None
_gmaps_probed: bool = False
_GMAPS_TIME_BUCKET_MINUTES = 30


def _get_gmaps_client() -> Any:
    """遅延初期化。GMAPS_KEY 未設定なら None を返す。"""
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

    `departure_time` を渡すと、深夜出発時の始発待ち込み duration が Google 側から返る
    (例: 22 時に東京→札幌の transit なら「翌朝始発 + 移動」)。キー未設定や API エラー時は
    None を返し、呼び出し側で Haversine にフォールバックする。
    """
    client = _get_gmaps_client()
    if client is None:
        return None
    bucket_minute = (departure_time.minute // _GMAPS_TIME_BUCKET_MINUTES) * _GMAPS_TIME_BUCKET_MINUTES
    bucketed = departure_time.replace(minute=bucket_minute, second=0, microsecond=0)
    cache_key = (
        round(lat1, 4), round(lng1, 4), round(lat2, 4), round(lng2, 4),
        bucketed.isoformat(),
    )
    if cache_key in _GMAPS_CACHE:
        return _GMAPS_CACHE[cache_key]
    try:
        # departure_time を過去時刻にすると 400 になるため、過去なら "now" にフォールバック
        # する。naive と aware の比較エラー回避のため real-now も tz を合わせる。
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
        # transit で経路がない (深夜帯や公共交通が届かない場所) 場合は driving で再試行
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

    Haversine 直線距離に `ROAD_DISTANCE_FACTOR` を掛けた値を実道路距離とみなし、距離レンジで
    手段を自動選択して、手段固有の乗換・準備オーバーヘッドを加える。
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

    `GMAPS_KEY` があれば Google Maps Directions API で実移動時間を取得し、`departure_time`
    を渡せば始発待ちや終電の運行時刻も加味された duration が返る。キー未設定や API 失敗時は
    Haversine + 距離レンジ別平均速度にフォールバックする。
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


# 交通機関の稼働時間帯。深夜帯 (24:00-06:00) は JR も私鉄も動かず、車移動でも睡眠時間帯に
# あたって不自然になるため移動不可扱いにする。
TRAVEL_ACTIVE_START_HOUR = 6
TRAVEL_ACTIVE_END_HOUR = 24  # 翌 0 時ちょうどを非稼働の開始として扱う

# 店に入り、チェックインし、買い物や食事をしてから次スポットへ、という自然な滞在を模擬する
STAY_DURATION_MIN_SEC = 10 * 60
STAY_DURATION_MAX_SEC = 30 * 60


def natural_stay_seconds() -> float:
    """1 スポットあたりの滞在時間を [10 分, 30 分] の一様分布で返す。"""
    return random.uniform(STAY_DURATION_MIN_SEC, STAY_DURATION_MAX_SEC)


def next_arrival_time(now: datetime, travel_seconds: float) -> datetime:
    """`now` から `travel_seconds` 移動した場合の現実的な到着時刻を返す。

    深夜帯 (24:00-06:00) は交通機関が動かず「駅で寝てから朝に再開」もできないため、
    今日中に到着できない旅は旅程ごと翌朝 06:00 発へ押し戻す。24 時間超の旅はさらに翌日へ。
    """
    if travel_seconds <= 0:
        return now

    cursor = now
    if cursor.hour < TRAVEL_ACTIVE_START_HOUR:
        cursor = cursor.replace(
            hour=TRAVEL_ACTIVE_START_HOUR, minute=0, second=0, microsecond=0
        )

    while True:
        day_end = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        arrival = cursor + timedelta(seconds=travel_seconds)
        if arrival <= day_end:
            return arrival
        cursor = day_end.replace(hour=TRAVEL_ACTIVE_START_HOUR)


def parse_checkin_deadline(spot: dict[str, Any]) -> datetime | None:
    """`spot["checkin_end_datetime"]` を JST の aware datetime にパースする。失敗時は None。

    現行は `YYYY-MM-DD HH:MM:SS` (JST) 形式だが、ISO8601 `Z` 付きへ切り替わっても扱える
    ように isoformat も試す。パース失敗は呼び出し側で「fail closed = 期限判定できないなら
    実 POST を止める」扱いにする前提。
    """
    raw = spot.get("checkin_end_datetime")
    if not raw:
        return None
    if not isinstance(raw, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=JST)
        except ValueError:
            pass
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
#     "completed_spots": ["cg_vote2026_XX", ...]
#   }
#
# 旧版の dry-run 経路も state を書いていたため、last_checkin の中身だけでは実 POST 由来
# かどうか判別できない。LAST_CHECKIN_SCHEMA_VERSION は「実 POST 成功でだけ書かれた」ことを
# 保証するためのマーカーで、値が一致しないレコードは lat/lng/resume_at を無視する。
LAST_CHECKIN_SCHEMA_VERSION = 2
_STATE_FILENAME = "canvasser_state.json"


class StateFileCorruptedError(Exception):
    """state.json は読めるが JSON として壊れているケース。実行モードでは fail closed する。"""


class FailClosedError(Exception):
    """実行モードで安全に継続できない状況で送出する。

    `process_account` でキャッチして exit_code=1 に反映することで、タスクスケジューラや
    運用ログ上でも「正常終了」に見えないようにする。

    partial_gained は、fail-closed 前にサーバ側で成功済みだった POST の reward を集計から
    落とさないために持たせる。通常経路の gained と合流させる責務は呼び出し側にある。
    """

    def __init__(self, msg: str, partial_gained: int = 0) -> None:
        super().__init__(msg)
        self.partial_gained = partial_gained


class UserInputError(Exception):
    """CLI 引数などユーザー入力に起因するエラー。

    `main()` が短いメッセージと exit 1 に丸めるための専用クラス。実装バグとしての
    `ValueError` を丸め込まないよう、入力検証系はこの例外を使う。
    """


_SPOT_SLUG_RE = re.compile(r"^cg_vote2026_[0-9]{1,6}$")


def _validate_state_schema(state: dict[str, Any], source: Path) -> None:
    """load 時にスキーマを検証する。壊れていれば StateFileCorruptedError を送出する。

    strict モード専用のガード。dry-run では緩めに扱うため呼ばれない。
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
                continue  # 旧 schema 互換のため部分 dict は許容する
            if not isinstance(v, typ):
                raise StateFileCorruptedError(
                    f"{source}: last_checkin.{k} の型が不正 (期待 {typ})"
                )
        slug = last.get("spot_slug")
        if isinstance(slug, str) and not _SPOT_SLUG_RE.fullmatch(slug):
            raise StateFileCorruptedError(
                f"{source}: last_checkin.spot_slug {slug!r} が cg_vote2026_NNNN 形式でない"
            )
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

    strict=False は dry-run 用の緩め扱いで、パース失敗や型不一致でも空 dict を返す。
    strict=True は `--execute` 用で、破損時に `StateFileCorruptedError` を送出し、
    追加でスキーマ検証も行う。
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
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, state_file)
    except Exception:
        # 失敗時は一時ファイルだけ掃除して例外を伝播する (state_file はそのまま残す)
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
    """1 件チェックイン成功時に state を更新する。

    `mark_completed=True` の場合、`spot.slug` を `completed_spots` へ追加して次回起動時の
    事前 skip 対象にする。
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

    旧版の dry-run 経路も `update_checkin_state` を叩いていたため、`last_checkin` から
    「実 POST 成功済み」を後方推定する手段がない。誤って dry-run 由来の slug を完了扱い
    すると次回 `--execute-checkin` で reward を落とすので、自動移行はしない。旧 state の
    補完が必要なら `--mark-completed` を明示的に使う。
    """
    state = load_account_state(profile_dir, strict=strict)
    last = state.get("last_checkin") or {}
    # schema_version が一致しない last_checkin は「実 POST 成功由来か」を保証できないため
    # resume には使わない。旧 dry-run の simulated route を execute run の起点にすると、
    # 偽の位置と時刻から始まって有効スポットを skip する事故になる。
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
    """外部から手動で成功済みスポットを state に登録する CLI 用ヘルパー。

    別デバイスや UI キャプチャで既に checkin 済みの分を流し込むために使う。既存 state.json
    が破損している場合は `StateFileCorruptedError` を上げて、破損 state を空 dict で
    上書きしてしまうのを防ぐ。
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
    """Chromium バイナリが未取得なら `playwright install chromium` を走らせる。

    `uv run` の初回起動から冪等に呼べるようにしておく。
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
        try:
            if check_login(page):
                print(
                    "ログイン状態を確認しました。次回から --login なしで実行できます。",
                    file=sys.stderr,
                )
                return 0
        except PlaywrightError:
            # ログイン画面へのリダイレクト中などに fetch は失敗する。次のポーリングを待つ。
            pass
        time.sleep(interval_sec)

    print(
        "タイムアウト。ログインを検出できませんでした。再度お試しください。",
        file=sys.stderr,
    )
    return 1


# パストラバーサル (../) や絶対パス指定を排除するため、basename として安全な文字集合に限定
_ACCOUNT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _validate_account_name(account: str) -> None:
    if not _ACCOUNT_NAME_RE.fullmatch(account):
        raise UserInputError(
            f"--account の値 {account!r} は許可されていません。"
            "使える文字は英数字・'_'・'-'・'.' のみで、長さは 1〜64 文字です。"
        )
    # 正規表現は '.' や '..' 単体を通してしまうので、パス区切り含めここで追加防御する
    if account in (".", "..") or any(sep in account for sep in ("/", "\\")):
        raise UserInputError(
            f"--account の値 {account!r} はパスとして危険なため許可されません。"
        )


def _ensure_within(base: Path, candidate: Path) -> None:
    """`candidate` が `base` の子孫であることを保証する。逸脱時は UserInputError を送出する。"""
    try:
        candidate.resolve().relative_to(base.resolve())
    except ValueError as e:
        raise UserInputError(
            f"プロファイル保存先 {candidate} が profiles-dir {base} の外に逃げています。"
        ) from e


def _profiles_dir_is_gitignored(profiles_dir: Path) -> bool:
    """`profiles_dir` が git ignore 対象なら True。

    profiles_dir がまだ存在しない (初回 `--login` 前) ケースでも判定できるよう、path 末尾
    に `/` を付けてディレクトリと明示する。`.gitignore` の `profiles/` のようなディレクトリ
    限定パターンは、path 側もディレクトリと分かる形でないと match しない。

    git repo 外や git 自体が使えない環境では False を返す (誤コミット経路を判定できないため
    拒否側に倒す)。`git check-ignore --quiet` の exit は 0=ignored、1=not ignored、
    128=error (repo 外)。
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
        # `--` で option 終端を明示することで、`-` から始まるユーザー指定パスを option 扱い
        # されないようにする。shell=False なので shell injection は起こらない。
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
    """処理対象のプロファイル一覧 `[(表示名, ディレクトリ), ...]` を決定する。

    `account` を指定すれば 1 アカウント固定、未指定なら `profiles_dir` 配下のサブディレクトリを
    全列挙する。
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
                f"[warn] プロファイル名 {entry.name!r} が命名規則に合致しないため skip します。",
                file=sys.stderr,
            )
            continue
        target = entry.resolve()
        _ensure_within(profiles_dir, target)
        result.append((entry.name, target))
    return result


def open_persistent_context(p, profile_dir: Path, headless: bool):
    """persistent_context を開く。Chromium 未取得のエラーが出たら install してリトライする。"""
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

    `execute_mission` と `execute_checkin` はそれぞれ独立した実 POST ゲートで、両方 False
    なら完全ドライラン (GET のみで POST/PUT は送らない)。未ログイン検知時は exit_code=1 を
    返し、呼び出し側で他アカウントへ進む。
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
            # dry-run の見込み枚数は集計に混ぜず、アカウント総計を汚さない
            mission_gain = collect_missions(page, execute=execute_mission)
            if execute_mission:
                gained += mission_gain
        if run_checkin:
            # Referer を合わせるためチェックインページに一度 navigate しておく
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
                # fail-closed 前に成功していた POST の reward は e.partial_gained に入って
                # いるので、集計から落とさないよう合流させる。
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

    # --daily-budget=0 は無制限として扱うが、負数を許すと limit_counter 判定が常時 truthy
    # になって実 POST 上限が壊れる。他の閾値も 1 未満だと本来の役割を果たせないので弾く。
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

    # --mark-completed は state を編集して即終了する (ブラウザ起動なし)
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

    run_mission = not args.no_mission
    run_checkin = args.checkin
    if args.no_mission and not args.checkin:
        print(
            "--no-mission は --checkin と組み合わせて使ってください。",
            file=sys.stderr,
        )
        return 1
    # 実行ゲート単独指定を弾く。対応する対象フラグが無いと黙って dry-run になり、
    # 「実 POST を送ったつもりが送られていない」誤運用に繋がる。
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

    # Playwright は GET のみのドライラン中でも persistent context で cookie・cache・
    # metadata を同期する。Cookie 誤コミットを防ぐため、実行モードに関係なく gitignore
    # 未対応の profiles_dir は拒否する。
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
                # --login は 1 アカウントのみ処理して抜ける
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

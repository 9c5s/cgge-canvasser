# cgge-canvasser

シンデレラガール総選挙2026のデイリーミッションとチェックインを自動で回収するスクリプト。

Playwright の persistent context でブラウザセッションを保持し、フロントが叩いている内部 API (`api.idolmaster-official.jp`) をそのまま呼び出す。複数アカウントを `./profiles/{アカウント名}/` に分けて運用する構成。

## 前提

- Windows / macOS / Linux, Python 3.10 以上
- [uv](https://docs.astral.sh/uv/) 必須 (依存関係は PEP 723 の inline script metadata で管理)
- Google Cloud Console で Directions API を有効化した API キー (任意, `.env` の `GMAPS_KEY` に設定するとチェックインの移動時間計算が公共交通機関の実運行情報ベースになる)
- BNID (バンダイナムコID) の着信認証設定済み
- アイドルマスター ポータルにログイン可能

`canvasser.py` は PEP 723 の inline script metadata を持っているので、事前セットアップなしで `uv run` から直接実行できる。

## セットアップ

`.env` に Google Maps API キーを保存 (任意, 未設定なら Haversine 距離ベースの自前計算にフォールバック):

```
GMAPS_KEY=AIzaSy...
```

初回起動時に依存関係 (playwright, pycryptodome, googlemaps, python-dotenv) と Chromium バイナリを自動取得する。

## 使い方

### 初回ログイン (アカウントごとに1回)

```powershell
uv run canvasser.py --login --account main
uv run canvasser.py --login --account sub
```

- `--account NAME` は必須。`./profiles/NAME/` にプロファイルが作られる
- Chromium が可視状態で立ち上がるので、BNID でログイン → ミッションページが表示されると自動的にログイン検知して終了する

### 日次実行 (ミッション回収, 全アカウント)

```powershell
uv run canvasser.py --execute-mission
```

`./profiles/` 配下の全アカウントを順次処理。ミッション回収 (ログインボーナス、動画視聴、公式Xフォロー、達成回数など) を実行する。

`--execute-mission` を付けない場合は **完全ドライラン** (GETのみ, POST/PUT なし)。

### 特定アカウントのみ実行

```powershell
uv run canvasser.py --account main
```

### ドライランで動作確認 (POST/PUT は一切送らない)

```powershell
uv run canvasser.py --checkin
```

`--execute-mission` / `--execute-checkin` を **どちらも付けないと完全ドライラン** (GET のみ、POST/PUT は一切送らず sleep も skip)。ペイロード生成、経路シミュレーション、state を触らないダミー ループのみ。

### ミッションだけ本番

```powershell
uv run canvasser.py --execute-mission
```

### チェックインも本番

```powershell
uv run canvasser.py --checkin --execute-mission --execute-checkin
```

- `--execute-mission`: ミッション POST/PUT のゲート
- `--execute-checkin`: チェックイン POST のゲート
- 両者は独立。片方だけドライラン、片方だけ本番も可能

### 慎重に少数件から試す

```powershell
uv run canvasser.py --account main --checkin --no-mission --execute-checkin --daily-budget 3
```

- `--daily-budget N`: 1回の実行で **N 件だけ実POST試行** して終了 (未指定なら無制限)。成功件数ではなく試行回数を数えるので、既達成 / 範囲外 / 未観測ecodeも1件消費する。
- `--no-mission`: ミッション回収をスキップしてチェックインだけ実行
- `--consecutive-failure-limit N`: **未観測 ecode が連続 N 件発生したら全体中断** (デフォルト 1 = 1件目で即停止 / fail closed)
- `--max-out-of-range N`: **E5005 (範囲外) の累積が N 件で停止** (デフォルト 3)。crypto/座標の実装不一致で 51 件全部を撃たないための安全弁。
- `--allow-unignored-profiles-dir`: `--profiles-dir` が `.gitignore` 対象でない場合の警告を無視 (デフォルトは実POST 拒否, Cookie 誤コミット防止)

### 既に消化済みスポットを state に手動登録

state.json の `completed_spots` に外部で成功済みのスポットを追加できる。実POST 前に既知の消化分を入れておくと、初回起動で未観測ecodeに突入して停止する事故を防げる。

```powershell
uv run canvasser.py --account syota --mark-completed cg_vote2026_17,cg_vote2026_19
```

## Windows タスクスケジューラ登録例

毎日 12:05 に全アカウントを実行:

```powershell
$dir = "D:\projects\cgge-canvasser"
$action  = New-ScheduledTaskAction -Execute "uv" `
             -Argument "run canvasser.py --checkin --execute-mission --execute-checkin --profiles-dir $dir\profiles" `
             -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 12:05
Register-ScheduledTask -TaskName "cgge-canvasser" -Action $action -Trigger $trigger `
             -Description "シンデレラガール総選挙2026 デイリー自動回収"
```

## 挙動の要点

### ミッション回収 (`collect_missions`)

`GET /mileage_vote/cinderellagirls_vote_2026/missions` を叩いて、`action.mission_complete_api_call_flag: true` のミッションだけを処理対象にする。未達成なら達成 POST → 受取 PUT。達成済み未受取なら受取 PUT のみ。

### チェックイン (`collect_checkins`)

`GET /checkins/event/cg_vote2026` で全51スポットの座標を取得し、以下の順で処理する:

1. **前回位置から再開**: `state.json` に保存された位置に最も近い未達成スポットを起点にする。state がなければランダム。
2. **最近傍法で巡回順を決定**: 現在地から Haversine 距離が最小の未達成スポットへ順次移動する (greedy TSP 近似)。
3. **移動時間を反映**:
   - `GMAPS_KEY` があれば Google Maps Directions API の `mode=transit` (フォールバック `driving`) を呼び、`departure_time` に仮想現在時刻を渡す。実運行時刻を含む duration が返る。
   - なければ Haversine + 距離レンジ別平均速度で下限を推定。
4. **深夜帯(24:00-06:00)は移動不可**: `next_arrival_time` で「今日中に到着できない旅は翌朝06:00発に押戻し」を強制。ただし `gmaps-transit` の結果は既に運行時刻が含まれているので押戻しなし。
5. **スポットで滞在**: 10〜30分ランダムに滞在してから次スポットへ (最終スポット or `daily-budget` 到達時はスキップ)。
6. **座標を自然化**: スポット中心から `checkin_radius * 0.85` 内の円内ランダム点、accuracy は正規分布 μ=18m σ=6m + 15%外れ値、altitude は 20% 発生で 5〜80m。
7. **AES 暗号化して POST**: `AES-CBC(PBKDF2(API_KEY, salt=r16, iter=500, sha1, keySize=32B), iv=r16)` で座標 JSON を暗号化、`Content-Type: application/x-www-form-urlencoded` で `{salt_hex},{iv_hex},{ct_base64}` 形式送信。

### エラー分類 (チェックイン POST)

| ecode | 挙動 |
|---|---|
| `SUCCESS` | 成功、投票券獲得、state 更新 (成功直後 + 滞在後の2回)、滞在時間消費 |
| `E5005` (範囲外) | スキップ、`out_of_range_count` を +1。**`--max-out-of-range` (デフォルト 3) 到達で FailClosedError で全体中断** (crypto/座標の実装不一致検知) |
| `ECODES_ALREADY_DONE` (初期値は空 tuple) | 実観測で意味を確定した ecode のみを追加する。追加後は既達成扱いで skip |
| 未観測ecode | `consecutive_failures` を加算。**`--consecutive-failure-limit` (デフォルト 1) 到達で FailClosedError で全体中断** (fail closed) |

初回実行時は既達成に相当する ecode がまだ観測されていないため、既達成スポットを踏むと **未観測ecode 扱いで即停止する**。停止時のログで ecode / body / UI 表示を確認し、意味が「サーバ側では成功済み (=次回以降 skip してよい)」と判断できたら:
1. `--mark-completed cg_vote2026_19` (実 slug 形式) で state に流し込んで再実行 (単発対応)
2. または `ECODES_ALREADY_DONE` にその ecode を追加してコミット (恒常対応)

FailClosedError は `process_account` で捕捉され、`exit_code=1` として返るのでタスクスケジューラや運用ログで正常終了に見えない。

### 状態永続化

`./profiles/{account}/canvasser_state.json` に **実POST 成功時のみ** atomic (`os.replace`) に書き出す。ドライランでは state を書き換えない。スキーマ:

```json
{
  "last_checkin": {
    "spot_slug": "cg_vote2026_19",
    "spot_name": "アイドルマスター オフィシャルショップ 315!!!SHOP",
    "location_latitude": 35.729124082591,
    "location_longitude": 139.7204013411,
    "virtual_completed_at": "2026-07-02T16:48:00+09:00",
    "real_completed_at": "2026-07-02T07:48:00+00:00"
  },
  "completed_spots": ["cg_vote2026_17", "cg_vote2026_19"]
}
```

- `completed_spots` は次回起動時の **事前 filter** に使う。既に成功したスポットへの実POSTは行わない。
- `spot_slug` は正規表現 `^cg_vote2026_[0-9]{1,6}$` にマッチする形式が必須 (schema 検証で strict チェック)。手動編集する場合は `cg_vote2026_19` のような実 slug 形式を守ること。
- state.json が JSON パース失敗 or schema 不整合になると **`--execute-checkin` 時は fail closed で即停止**。手動で確認・修復してから再実行。
- ドライランでは破損 state を空 dict として扱って続行。

## API 仕様 (参考)

Next.js チャンク解析で判明した仕様。

### 認証

- `x-api-key: <FRONTEND_PUBLIC_API_KEY>` (フロント公開の固定値。実際の値は `canvasser.py` の `API_KEY` を参照)
- `Cookie` (BNID セッション, `credentials: 'include'` で自動付与)
- `Referer: https://idolmaster-official.jp/`

### エンドポイント

| 操作 | メソッド | パス |
|---|---|---|
| ログイン確認 | GET | `/api/v1_1_0/auths/login/check` |
| ミッション一覧 | GET | `/api/v1_1_0/mileage_vote/cinderellagirls_vote_2026/missions?mission_type=0&limit=300` |
| ミッション達成 | POST | `/api/v1_1_0/mileage_vote/cinderellagirls_vote_2026/mission/{mission_id}` |
| ミッション受取 | PUT | `/api/v1_1_0/mileage_vote/cinderellagirls_vote_2026/mission/{mission_id}/receive` |
| チェックインイベント情報 | GET | `/api/v1_1_0/checkins/event/cg_vote2026` |
| チェックイン実行 | POST | `/api/v1_1_0/checkins/event/cg_vote2026/spot/{spot_slug}/checkin` |

注意: 一覧取得は複数形の `missions`, 個別操作は単数形の `mission`。

### AES 暗号化

チェックイン POST の body は位置情報 JSON を crypto-js 互換で暗号化した文字列。

- password: `x-api-key` の値を流用
- key derivation: `PBKDF2(password, salt=random16, iterations=500, keySize=8 words=32B, hasher=SHA1)`
- iv: 16 バイト random
- padding: PKCS7
- 形式: `salt_hex,iv_hex,ciphertext_base64` (salt と iv は hex、ciphertext だけ Base64)
- Content-Type: `application/x-www-form-urlencoded`

## 注意

- `profiles/{name}/` にはセッション Cookie が保存される。**Git 管理外** にし、他人に共有しない
- `.env` も Git 管理外 (`.gitignore` 設定済み)
- 実POST または `--login` 時に `--profiles-dir` が `.gitignore` 対象になっているかを `git check-ignore` で自動検証する。未 ignore なら実行拒否 (Cookie 誤コミット防止)。回避したい場合は `--allow-unignored-profiles-dir` を明示すること
- キャンペーン規約に自動化禁止条項がある場合は自己責任で判断すること
- Cookie の有効期限が切れた場合は `--login --account NAME` で再ログインが必要
- **未観測 ecode が 1 件出たら fail closed で即停止** (デフォルト)。BAN シグナル / 認証切れ / 予期せぬ状態のどれかとして扱う

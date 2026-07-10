# cgge-canvasser

シンデレラガール総選挙2026のデイリーミッションとチェックインを自動で回収するスクリプト。

Playwright の persistent context でブラウザセッションを保持し、フロントが叩いている内部 API (`api.idolmaster-official.jp`) をそのまま呼び出す。複数アカウントを `./profiles/{アカウント名}/` に分けて運用する構成。

## 前提

- Windows / macOS / Linux、Python 3.14 以上。
- [uv](https://docs.astral.sh/uv/) が必須。
- Google Cloud Console で Directions API を有効化した API キー (任意。`.env` の `GMAPS_KEY` に設定するとチェックインの移動時間計算が公共交通機関の実運行情報ベースになる)。
- BNID (バンダイナムコID) の着信認証を設定済みであること。
- アイドルマスター ポータルにログイン可能であること。

依存関係は `pyproject.toml` (開発・リポジトリ運用向け) と `canvasser.py` の PEP 723 inline script metadata (スクリプト単体を任意のディレクトリで `uv run` する用) の両方で管理する。両者は同じ依存を宣言する。事前セットアップなしで `uv run canvasser.py` を実行できる。

## セットアップ

`.env` に Google Maps API キーを保存する (任意。未設定なら Haversine 距離ベースの自前計算にフォールバック):

```
GMAPS_KEY=AIzaSy...
```

初回起動時に依存関係 (playwright, pycryptodome, googlemaps, python-dotenv) と Chromium バイナリを自動取得する。

## 使い方

### 初回ログイン (アカウントごとに1回)

```powershell
uv run canvasser.py login --account main
uv run canvasser.py login --account sub
```

- `--account NAME` は必須。`./profiles/NAME/` にプロファイルが作られる。
- Chromium が可視状態で立ち上がる。BNID でログインしてミッションページが表示されると、自動でログインを検知して終了する。

初回ログイン時に BNID フォームでパスワードを入力する際、Chrome の「パスワードを保存しますか?」プロンプトで **保存を選択** すること。この保存された資格情報は Playwright プロファイル内 (`profiles/{account}/Default/Login Data`) に Windows DPAPI + AES-256-GCM で暗号化されて格納され、以降 BNID セッション切れが検知されたときに自動再ログインの供給源として使われる。

### 日次実行 (ミッション回収、全アカウント)

```powershell
uv run canvasser.py mission
```

`./profiles/` 配下の全アカウントを順次処理する。ミッション回収 (ログインボーナス、動画視聴、公式 X フォロー、達成回数など) を実行する。

`--dry-run` を付けた場合は完全ドライラン (GET のみ、POST/PUT なし)。

### 特定アカウントのみ実行

```powershell
uv run canvasser.py mission --account main
```

### チェックイン

mission と checkin は独立したサブコマンドで、同時実行はしない。

```powershell
uv run canvasser.py checkin            # 本番
uv run canvasser.py checkin --dry-run  # ドライラン (POST は送らない)
```

`--dry-run` を付けた場合は完全ドライランとして扱う (GET のみ、POST は送らず、sleep も skip)。ペイロード生成、経路シミュレーション、state を触らないダミーループだけを回す。

### 慎重に少数件から試す

```powershell
uv run canvasser.py checkin --account main --daily-budget 3
```

チェックイン専用の安全弁 (checkin サブコマンドのみ):

- `--daily-budget N`：1 回の実行あたり N 件の実 POST 試行で終了する (未指定なら無制限)。成功件数ではなく試行回数を数えるので、既達成・範囲外・未観測 ecode も 1 件消費する。
- `--consecutive-failure-limit N`：未観測 ecode が連続 N 件で全体を中断する (デフォルト 1 = 1 件目で即停止 / fail closed)。
- `--out-of-range-limit N`：E5005 (範囲外) の累積が N 件で停止する (デフォルト 3)。crypto と座標の実装不一致で 51 件を撃ち切らないための安全弁。

共通の安全策 (login / mission / checkin すべてに適用):

- `--allow-unignored-profiles-dir`：`--profiles-dir` が `.gitignore` 対象でない場合の警告を無視する。デフォルトでは login / mission / checkin のいずれの実行モード (ドライラン含む) でも拒否する (Cookie 誤コミット防止)。

mission と checkin にのみ効く自動再ログイン制御:

- `--no-auto-relogin`：Chrome 自動保存の BNID 資格情報が読める場合でも自動再ログインを行わない。手動運用に戻したいときや、疑わしいログイン失敗を追跡したいときにだけ使う。

### サーバ側チェックイン状況を state に取り込む (sync)

別デバイスや手動 UI 操作で先にチェックインした分をローカル `state.json` に自動反映する。既達成スポットへの無駄な再 POST → 未観測 ecode → fail closed 停止を防ぐ。

```powershell
uv run canvasser.py sync                    # 全アカウント
uv run canvasser.py sync --account main
```

- チェックイン API への副作用は GET のみ (POST/PUT を送らない、ドライラン概念なし)。ただし BNID セッション切れ検知時は自動再ログイン (`--no-auto-relogin` で無効化可能) が動くため、ログイン用 POST は発生し得る。
- サーバ側「済み」かつローカル未登録の slug を `completed_spots` に**追加のみ**する。ローカル済み・サーバ未確認の slug は削除せず警告表示にとどめる (削除すると再 POST 停止のリスクがあるため)。
- `checkin` サブコマンドは実行前に同じ判定を自動で行うため、通常は `sync` を単独で叩く必要はない。別デバイス済みの state 反映を先行して確認したいとき、または全アカウント一括で state を揃えたいときに使う。

### 既に消化済みスポットを state に手動登録 (mark-completed)

サーバから読み取れないケース (`sync` の読み取り実装が壊れた、API スキーマ変更で判定が効かないなど) の手動エスケープハッチとして残している。通常運用では `sync` (または `checkin` の自動同期) を使う。

```powershell
uv run canvasser.py mark-completed --account syota cg_vote2026_17 cg_vote2026_19
```

## Windows タスクスケジューラ登録例

毎日 12:05 に全アカウントのミッションを回収する:

```powershell
$dir = "D:\projects\cgge-canvasser"
$action  = New-ScheduledTaskAction -Execute "uv" `
             -Argument "run canvasser.py mission --profiles-dir $dir\profiles" `
             -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 12:05
Register-ScheduledTask -TaskName "cgge-canvasser" -Action $action -Trigger $trigger `
             -Description "シンデレラガール総選挙2026 デイリー自動回収"
```

登録済みのタスクを更新する手順、および rollback 時の手順は `docs/superpowers/specs/2026-07-08-chrome-login-data-auto-relogin-design.md` の Rollout 節を参照。

## 挙動の要点

### ASOBI STORE 連携の自動復旧

`mission_type=1` のミッション (ASOBI STORE 系、プレミアム会員ログボ #21 など) で `E1926` (ASOBISTORE への再ログインが必要) が返ったら、`linkages/as/login` を踏んで自動復旧する。途中の中間ページ (legacy-login.asobistore.jp) は「バンダイナムコIDでログイン」を自動クリック、BNID フォームが出た場合は Chrome 自動保存の資格情報で自動突破する。復旧に失敗した場合はミッションはスキップされ、翌日に再試行される。

### ミッション回収 (`collect_missions`)

`GET /mileage_vote/cinderellagirls_vote_2026/missions` を叩いて、`action.mission_complete_api_call_flag: true` のミッションだけを処理対象にする。未達成なら達成 POST → 受取 PUT。達成済み未受取なら受取 PUT のみ。

### チェックイン (`collect_checkins`)

`GET /checkins/event/cg_vote2026` で全 51 スポットの座標を取得し、次の順で処理する。

1. **サーバ済みを事前 skip**：応答中の `checkin_status.is_checkedin == 1` を持つスポットは既達成として skip する。本番実行時は `state.completed_spots` にも自動反映される (`sync` サブコマンドと同じ経路)。
2. **前回位置から再開**：`state.json` に保存された位置に最も近い未達成スポットを起点にする。state がなければランダム。
3. **最近傍法で巡回順を決定**：現在地から Haversine 距離が最小の未達成スポットへ順次移動する (greedy TSP 近似)。
4. **移動時間を反映**：
   - `GMAPS_KEY` があれば Google Maps Directions API を呼び、`departure_time` に仮想現在時刻を渡す。`mode=transit` は始発待ちを含む実運行時刻ベースの duration が返る。経路が無い場合 (深夜帯や公共交通が届かない場所) は `mode=driving` にフォールバックし、`duration_in_traffic` が取れれば交通量反映の所要時間、無ければ `duration` の距離ベース所要時間を採用する。
   - なければ Haversine と距離レンジ別平均速度で下限を推定する。
5. **深夜帯 (24:00-06:00) は移動不可**：`next_arrival_time` で「今日中に到着できない旅は翌朝 06:00 発へ押し戻す」を強制する。ただし `gmaps-transit` の結果には運行時刻が含まれているので押し戻しは行わない。稼働枠 (06:00-24:00 の 18 時間) を超える長旅も夜行便等の連続移動として押し戻さずそのまま加算する。
6. **スポットで滞在**：10〜30 分をランダムに滞在してから次スポットへ移る (最終スポットや `daily-budget` 到達時は滞在を省く)。
7. **座標を自然化**：スポット中心から `checkin_radius * 0.85` 内の円内ランダム点を選ぶ。accuracy は正規分布 μ=18m σ=6m に 15% の外れ値を混ぜ、altitude は 20% の確率で 5〜80m を割り当てる。
8. **AES 暗号化して POST**：`AES-CBC(PBKDF2(API_KEY, salt=r16, iter=500, sha1, keySize=32B), iv=r16)` で座標 JSON を暗号化し、`Content-Type: application/x-www-form-urlencoded` で `{salt_hex},{iv_hex},{ct_base64}` 形式を送信する。

### エラー分類 (チェックイン POST)

| ecode | 挙動 |
|---|---|
| `SUCCESS` | 成功、投票券獲得、state 更新 (成功直後と滞在後の 2 回)、滞在時間を消費 |
| `E5005` (範囲外) | スキップし `out_of_range_count` を +1。`--out-of-range-limit` (デフォルト 3) 到達で FailClosedError を送出し全体中断 (crypto や座標の実装不一致検知) |
| `ECODES_ALREADY_DONE` (初期値は空 tuple) | 実観測で意味を確定した ecode のみを追加する。追加後は既達成扱いで skip |
| 未観測 ecode | `consecutive_failures` を加算。`--consecutive-failure-limit` (デフォルト 1) 到達で FailClosedError を送出し全体中断 (fail closed) |

既達成スポットは `checkin` サブコマンドの実行前に `checkin_status.is_checkedin == 1` で検出して事前 skip するため、通常運用では未観測 ecode に突入しない。もし別デバイスや UI 経由での消化が state に反映されずに未観測 ecode で止まった場合の対応は次のとおり。

1. `sync --account NAME` を走らせてサーバの実状況を state に取り込んでから再実行する (自動同期、通常はこれで解決)。
2. `mark-completed --account NAME cg_vote2026_19` (実 slug 形式) で手動流し込みして再実行する (`sync` の読み取り実装が壊れているときのエスケープハッチ)。
3. `ECODES_ALREADY_DONE` にその ecode を追加してコミットする (恒常対応、`sync` を経由せずに走らせたい場面向け)。

FailClosedError は `process_account` で捕捉され、`exit_code=1` として返る。タスクスケジューラや運用ログ上でも正常終了に見えないよう nonzero で抜ける仕様になっている。

### 状態永続化

`./profiles/{account}/canvasser_state.json` に、実 POST が成功した場合に限り atomic (`os.replace`) で書き出す。ドライランでは state を書き換えない。スキーマは次のとおり。

```json
{
  "last_checkin": {
    "schema_version": 2,
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

`schema_version` は resume で位置と時刻を引き継ぐ際の互換性チェックに使う。実 POST 由来と保証できる version のみを resume 起点として採用する (現状は `2`)。

- `completed_spots` は次回起動時の事前フィルタに使う。既に成功したスポットへは実 POST を送らない。
- `spot_slug` は正規表現 `^cg_vote2026_[0-9]{1,6}$` にマッチする形式が必須 (schema 検証で strict チェック)。手動編集する場合は `cg_vote2026_19` のような実 slug 形式を守る。
- state.json の JSON パース失敗や schema 不整合は、チェックイン実 POST (`checkin`、`--dry-run` 未指定) 時に fail closed として即停止する。手動で確認・修復してから再実行する。
- ドライランでは破損した state を空 dict として扱い、そのまま続行する。

## API 仕様 (参考)

Next.js チャンク解析で判明した仕様。

### 認証

- `x-api-key: <FRONTEND_PUBLIC_API_KEY>` (フロント公開の固定値。実際の値は `canvasser.py` の `API_KEY` を参照)
- `Cookie` (BNID セッション。`credentials: 'include'` で自動付与)
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

補足として、一覧取得は複数形の `missions`、個別操作は単数形の `mission` を使う。

### AES 暗号化

チェックイン POST の body は、位置情報 JSON を crypto-js 互換で暗号化した文字列を送る。

- password：`x-api-key` の値を流用する。
- key derivation：`PBKDF2(password, salt=random16, iterations=500, keySize=8 words=32B, hasher=SHA1)`。
- iv：16 バイトのランダム値。
- padding：PKCS7。
- 形式：`salt_hex,iv_hex,ciphertext_base64` (salt と iv は hex、ciphertext だけ Base64)。
- Content-Type：`application/x-www-form-urlencoded`。

## 運用上の注意点

- `profiles/{name}/` にはセッション Cookie が保存されるため、Git 管理外に置き、他人へ共有しない。
- `.env` も Git 管理外に置く (`.gitignore` 設定済み)。
- `login` / `mission` / `checkin` 実行時に `--profiles-dir` が `.gitignore` 対象になっているかを `git check-ignore` で自動検証する。未 ignore の場合は実行を拒否する (Cookie 誤コミット防止)。回避したい場合は `--allow-unignored-profiles-dir` を明示する。
- キャンペーン規約に自動化禁止条項がある場合は、自己責任で判断する。
- Cookie の有効期限が切れた場合は、`mission` / `checkin` 実行時に自動再ログインが試みられる。失敗した場合や手動で再登録する場合は `login --account NAME` を使う (Chrome に BNID の資格情報が自動保存されていれば再ログインに使われる)。
- 未観測 ecode が 1 件出たら fail closed で即停止する (デフォルト)。BAN シグナル・認証切れ・予期せぬ状態のいずれかとして扱う。
- BNID の資格情報は Chrome の Login Data (`profiles/{account}/Default/Login Data`) に Windows DPAPI + AES-256-GCM で暗号化されて保存される。復号には同一 Windows ユーザーアカウントでのログインが必要なため、Chrome 本体のパスワード保護と同水準のリスクに留まる。パスワードを変更した場合は、次回 `login` 実行時に Chrome の「パスワードを更新しますか?」プロンプトで更新を選択する。
- BNID 側が CAPTCHA / 2FA / パスキー強制を導入すると、自動再ログインは失敗するようになり手動 `login` に退化する。`auto_login` は CAPTCHA / 2FA を検知すると即 abort する (詳細は `canvasser.py` の `_LOGIN_CAPTCHA_SELECTORS` を参照)。

# cgge-canvasser

シンデレラガール総選挙2026 のデイリーミッションを自動で回収するスクリプト。

Playwright の persistent context でブラウザセッションを保持し、フロントが叩いている内部 API (`api.idolmaster-official.jp`) をそのまま呼び出す。

## 対象と非対象

`GET /missions` レスポンスの `action.mission_complete_api_call_flag: true` のみを対象とする。

| 自動化対象 | 内容 | 報酬 |
|---|---|---|
| ID 96 | ポータルログイン | 2票/日 |
| ID 97 | 公式Xフォロー | 5票 (初回のみ) |
| ID 98 | ASOBI STORE会員登録 | 10票 (初回のみ, 登録済みが前提) |
| ID 100〜104 | 基本ミッション達成 10/50/100/150/200回 | 15/30/50/75/100票 |
| ID 105 | アイドルコミュ視聴 | 1票/日 |
| ID 106 | シンデレラNo.1 MV視聴 | 1票/日 |
| ID 107 | ROSE MV視聴 | 1票/日 |

**非対象** (`api_call_flag: false`):
- あいことば入力系 (ID 108〜117)
- チェックイン (ID 99)
- CG総選挙ST@TION 生配信ミッション (ID 114)

デイリーだけで **5票/日**、キャンペーン期間 76 日完走で **約380票 ≒ 1,900円相当**。

## 前提

- Windows / macOS / Linux, Python 3.10 以上
- [uv](https://docs.astral.sh/uv/) 推奨 (依存関係管理と Chromium 取得を自動化)
- BNID (バンダイナムコID) の着信認証設定済み
- アイドルマスター ポータルにログイン可能

`canvasser.py` は PEP 723 の inline script metadata を持っているので、事前セットアップなしで `uv run` から直接実行できる。

## 使い方 (uv 版, 推奨)

### 初回ログイン (アカウントごとに1回)

```powershell
uv run canvasser.py --login --account main
uv run canvasser.py --login --account sub   # 2つ目のアカウントも登録可能
```

- `--account NAME` で `./profiles/NAME/` にプロファイルが作られる
- Chromium が可視状態で立ち上がるので、BNID でログイン → ミッションページが表示されると自動的に検知して終了する
- 依存関係 (playwright) と Chromium バイナリは初回起動時に自動取得

### 日次実行 (全アカウント自動処理)

```powershell
uv run canvasser.py
```

`./profiles/` 配下の全アカウントを順次処理する。1アカウントが失敗しても他は続行し、最後にサマリを表示。

### 特定アカウントのみ実行

```powershell
uv run canvasser.py --account main
```

### Windows タスクスケジューラ登録例 (全アカウント自動)

毎日 12:05 に実行:

```powershell
$dir = "D:\projects\cgge-canvasser"
$action  = New-ScheduledTaskAction -Execute "uv" `
             -Argument "run canvasser.py --profiles-dir $dir\profiles" `
             -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Daily -At 12:05
Register-ScheduledTask -TaskName "cgge-canvasser" -Action $action -Trigger $trigger `
             -Description "シンデレラガール総選挙2026 デイリー自動回収 (全アカウント)"
```

## 使い方 (pip / venv 版)

uv を使わない場合はこちら。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium

python canvasser.py --login --account main   # 初回
python canvasser.py                          # 日次 (全アカウント)
```

## 旧版からの移行

以前 `--profile ./imas_profile` で単一アカウント運用していた場合、以下のどちらかで移行する:

```powershell
# A) ディレクトリを新レイアウトに移動 (推奨)
mv .\imas_profile .\profiles\main

# B) 明示指定で使い続ける (後方互換)
uv run canvasser.py --profile .\imas_profile
```

## API 仕様 (参考)

Next.js チャンク `_next/static/chunks/4892-*.js` の `onMissionAchievement` / `onMissionReceive` から抽出:

| 操作 | メソッド | パス |
|---|---|---|
| ログイン確認 | GET | `/api/v1_1_0/auths/login/check` |
| ミッション一覧 | GET | `/api/v1_1_0/mileage_vote/cinderellagirls_vote_2026/missions?mission_type=0&limit=300` |
| 達成報告 | POST | `/api/v1_1_0/mileage_vote/cinderellagirls_vote_2026/mission/{mission_id}` |
| 投票券受取 | PUT | `/api/v1_1_0/mileage_vote/cinderellagirls_vote_2026/mission/{mission_id}/receive` |

注意: 一覧取得は複数形の `missions`, 個別操作は単数形の `mission`。

必須ヘッダ:
- `x-api-key: MEW6XfDHZVtpUxuERAGTaP6AfipAe53kCEFWEMAJ` (フロント公開の固定値)
- `Cookie` (BNID セッション, `credentials: 'include'` で自動付与)
- `Referer: https://idolmaster-official.jp/`

## 注意

- `imas_profile/` にはセッション Cookie が保存される。**Git 管理外** にし、他人に共有しない。
- キャンペーン規約に自動化禁止条項がある場合は自己責任で判断すること。
- Cookie の有効期限が切れた場合は `--login` で再ログインが必要。
- 動画視聴系ミッション (ID 105-107) が「POST だけで達成扱いになるか」はサーバ実装依存。まず 1 日回して受取ログを確認すること。

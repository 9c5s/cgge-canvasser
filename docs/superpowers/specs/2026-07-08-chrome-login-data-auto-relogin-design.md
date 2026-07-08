# 設計: Chrome Login Data による BNID 自動再ログイン + ASOBI 連携自動復旧 + `--dry-run` 一本化

- 作成日: 2026-07-08
- 著者: shun (@9c5s)
- ブランチ: `feat/auto-relogin`
- Status: Ready (Codex レビュー PASS、ユーザー承認 → 実装計画へ)
- Review: round 1 ISSUES → round 2 ISSUES → round 3 ISSUES → round 4 ISSUES → **round 5 PASS** (codex-review-loop、2026-07-08)

### Review 適用の方針 (2026-07-08 ユーザー指示)

「Codex の重箱の隅をつつくような指摘に踊らされず DRY/YAGNI/KISS/SOLID で判断して弾くものはしっかり弾く」

以下、Round 1・2・3 の指摘に対する採否をこの原則で明記する。

### Round 1・2・3 で採用した変更

- **E1926 復旧の gained 集計** (R1 major): 疑似コードを `total_gained += result.gained` に修正し、1 回目 attempt の通常 mission gained と 2 回目 attempt の再走 gained を累積する
- **guard 共有ヘルパー** (R1 major): `_run_guarded_auto_login(page, profile_dir, name)` を新設し、`attempt_auto_relogin` と `_run_asobi_linkage_recovery` の両方から呼ぶ。DRY で明確な重複を排除
- **`_receive()` の E1926 経路** (R1 major): `int` 返却を `MissionOutcome(gained, linkage_expired_id)` に変更、`_process_one_mission()` と `_receive()` の戻り値型を統一 (dataclass は 1 種類に集約)
- **タスクスケジューラ更新の必須化** (R1 major): `schtasks` の具体コマンドを Rollout に明記
- **rollback matrix** (R2 major): 新コード vs 旧コードで `--execute` の要否が反転するため、rollback 時の `schtasks /Change` 手順を追加
- **doc 内 `execute` 語の扱い** (R2 major): 最終 `rg` 検査対象から `docs/superpowers/specs/` を除外 (設計 doc は歴史的アーカイブ)
- **成功判定の PUT 反映** (R2 minor): 「POST が E1926 を返さない」→「POST/PUT が E1926 を返さない」
- **PR 直後の `mission --dry-run` 確認** (R2 minor): 翌日 12:05 実行前に parse/profile を確認 (Login Data 読みは別ステップで独立確認、R3 major 2 反映)
- **driver signature 整合** (R1 minor): `_run_asobi_linkage_recovery(page, profile_dir, name)` に統一
- **変更マップの網羅** (R1 minor): `tests/test_state.py` と module docstring 更新を追加
- **POST-able 関数の `dry_run` 必須化** (R3 major 1、ハイブリッド反映): 内部 API のうち dataclass はデフォルト維持だが、`collect_missions` / `_complete` / `_receive` / `_process_one_mission` / `_process_all_missions` は必須引数化。ongoing hazard を型で潰す
- **Login Data 読みの独立 verify ステップ** (R3 major 2 反映): dry_run では auto_relogin ゲートが走らないため、`uv run python -c "from canvasser import load_credentials..."` を Rollout smoke test に追加
- **doc の誤り訂正** (R3 minor 2/3/4): 「誤 master_key → AES.new ValueError」は誤長キーのみに限定 / grep 対象を `canvasser.py tests/ README.md` に絞る (docs/ 除外) / `ReceiveOutcome` leftover を `MissionOutcome` に統一

### Round 1・2 で押し返した指摘 (YAGNI/KISS で弾く)

- **内部 API 全体の `dry_run` デフォルト削除** (R1 major、R3 major 1 で一部撤回): R3 で「pyright は semantic default を検知できない」と再指摘され、妥協案 (dataclass はデフォルト維持、POST-able 関数は必須化) を採用した。全撤去は KISS 過剰だが、POST-able 関数の必須化は ongoing hazard 対策として妥当
- **Backup API + tempfile snapshot** (R1 major, R2 major 1, R2 minor 3): `mode=ro` + `timeout=5.0` で Chrome の Login Data (idle-locked、実測 `journal_mode = delete`) は十分読める。**Backup API は over-engineering、対象環境で `mode=ro` が破綻する実証を得るまで採用しない**。`immutable=1` は SQLite 公式仕様の指摘に基づき採用しない (この判断だけは R1 のまま維持)
- **SELECT WHERE 条件強化** (R1 minor 2): SQL には持ち込まない (KISS)。ただし R3 minor 1 指摘: 「Python 側 None フォールバック」だけでは古い有効行への探索まではしない。`_read_bnid_login_row` で `username_value` 非空・`password_value` が `bytes` であることを明示検証し、無効なら `None` を返して呼び出し側で手動 login へ誘導する挙動を明記 (「LATEST 行を 1 つ選び、無効なら諦める、古い有効行までは探さない」)
- **master key 32-byte 明示検証** (R1 minor 4): `AES.new` が誤長キーで `ValueError` を投げ、既存 None フォールバックで捕捉される。明示チェックは同等の防御を二重に書くだけで **YAGNI**
- **retry URL の ASOBI 経路対応** (R2 major 2, [HYPOTHESIS]): BNID form が ASOBI flow 中に出るケースは稀。auto_login timeout が retry で LOGIN_ENTRY_URL に飛ぶと linkage 状態は失われるが、driver の polling は timeout で False を返し、翌日再試行に落ちる (graceful degradation)。データロスなし。**リカバリ経路パラメータ化は over-engineering、YAGNI で弾く**

### HYPOTHESIS 検証結果

- Login Data の WAL 運用 (R1 HYPOTHESIS) → **棄却**: `PRAGMA journal_mode = delete` を実測、`-wal` ファイル不在
- 複数 BNID レコード (R1 HYPOTHESIS) → **棄却 (予防対応も撤回)**: 現在 3 アカウント各 1 行、YAGNI で SQL 条件強化しない
- SQLite immutable=1 の live DB 適用性 → **公式 docs で verify (https://www.sqlite.org/uri.html)**、immutable=1 は不採用 (`mode=ro` + timeout)
- retry URL による OAuth state 破壊 (R2 HYPOTHESIS) → **graceful degradation で許容、修正しない**
- 関連メモリ: `handoff-2026-07-07-chrome-login-data`, `project-asobi-linkage`, `project-task-scheduler`

---

## 目的

Playwright の persistent context (Chromium user_data_dir) が既に BNID の自動保存パスワードを保持している事実を利用して、リポジトリ配下から「平文 `credentials.json`」と「対話入力 CLI (`login-init`)」を完全撤去する。同じ資格情報で復旧できる ASOBI STORE 連携切れ (E1926) の自動復旧も同一 PR に含める。合わせて、日次運用の実態 (デフォルト本番) に一致するよう `--execute` を廃して `--dry-run` に反転する。

## スコープ (この PR で完了させる範囲)

1. **BNID 自動再ログイン**: `profiles/<acc>/Default/Login Data` (SQLite, v10 = DPAPI + AES-256-GCM) から BNID 資格情報を復号し、既存 `auto_login()` に渡して自動ログインする。
2. **ASOBI 連携自動復旧**: mission 実行中に E1926 が返ったら `linkages/as/login` を叩いて連携を復旧する。途中で BNID フォームに落ちたら (1) と同一の資格情報・同一の `auto_login()` で突破する。復旧後に E1926 だった mission の POST/PUT を再試行する。
3. **`--execute` 撤去 → `--dry-run` 一本化**: デフォルト本番、確認モードは `--dry-run`。CLI だけでなく内部シグネチャ (`RunOptions.execute` / `CheckinSettings.execute` / `collect_missions(execute=)` 等) と `execute` という語をコードから完全に消す。

## 非スコープ (別 PR / 対応しない)

- CAPTCHA / 2FA の自動突破 (現行同様、検知したら abort)
- v20 (App-Bound Encryption) 対応 (v10 のみ受理し、非 v10 は「手動 login へ」既存誘導に合流)
- macOS / Linux での DPAPI 相当対応 (運用は Windows のみ)
- `.env` / `credentials.json` 等の外部永続化による資格情報保存
- shop.asobistore.jp への直接ログイン画面対応 (BNID SSO 経由でのみ使う前提)
- 全 mission_type=1 の pre-emptive linkage refresh (E1926 発生時のみ反応する reactive 方式)

---

## 現状 (2026-07-08 時点、`canvasser.py` L 付き)

### 資格情報の永続化 (今回削除する)

- `_CREDENTIALS_FILENAME = "credentials.json"` (L1474), `_CREDENTIALS_PENDING_FILENAME = "credentials.json.pending"` (L1478)
- pending → active の状態遷移 (L1836-1860, L1986-2010, L2453-2531)
- 平文 JSON のファイル権限縮小 (`_apply_credentials_permissions`, POSIX 0o600 / Windows icacls, L1707-1735)
- `login-init` サブコマンド一式 (parser L2833-2845, `RunOptions.login_init_mode` L2679, `_main_impl` 分岐 L3024-3027)
- `tests/test_credentials.py` (272 行)

**実測 (2026-07-08)**: 3 アカウントとも `credentials.json` / `credentials.json.pending` は存在せず。既存プロファイルへの移行実装は不要。

### Chrome 自動保存パスワード (今回利用する)

- `profiles/<acc>/Default/Login Data` (SQLite): `origin_url = "https://account.bandainamcoid.com/login.html"` のレコードが 3 アカウント全てに存在、`password_value` は `v10` プリフィックス (前セッションで復号確認済み)
- `profiles/<acc>/Local State` (JSON): `os_crypt.encrypted_key` (DPAPI 暗号化された AES-256 マスタキー)
- `profile_dir` 自体が Chromium user_data_dir (`open_persistent_context(p, profile_dir, ...)` L2648-2665)

### 無改変で流用する部分

- `auto_login(page, credentials, ...) -> (AutoLoginOutcome, submissions)` (L2051-2123)
- `AutoLoginOutcome` (L2036-2048), `_poll_login_outcome` (L2126-2162)
- `_run_auto_login_sequence` (L2303-2325), `_retry_after_timeout` (L2236-2279), `_resolve_retry_outcome` (L2282-2300)
- `_detect_login_captcha`, `_login_error_visible` (L2012-2033)
- `check_login()` 短絡救済 (L2366-2375)
- 連続失敗ガードのロジック本体 (L2165-2233) — state 分離だけ行う

### E1926 (現状: 手動運用)

- `mission_type=1` の listing (`#21` プレミアム会員ログボ等) が、`_complete()` (L339-370) で E1906/E1924 以外なら `"error"` 返却、静かに +0 でスキップ
- 復旧手順は `project-asobi-linkage` メモリを参照 (`linkages/as/login?backto=...` を手動で開く)

### `--execute` (現状デフォルト = dry-run)

- CLI: `mission --execute` / `checkin --execute` (L2807-2812)
- 内部: `RunOptions.execute: bool = False` (L2682), `CheckinSettings.execute: bool = False` (L673), `collect_missions(page, *, execute: bool = False)` (L256), `_complete`, `_receive`, `_CheckinRunner._simulate` 他

---

## 新設計

### 1. Chrome Login Data からの資格情報取得

#### 1.1 復号手順

1. `profiles/<acc>/Local State` をロードし、`os_crypt.encrypted_key` (base64) を取得
2. base64 デコード → 先頭 `b"DPAPI"` (5 バイト) を剥がす
3. `ctypes.windll.crypt32.CryptUnprotectData` で DPAPI 復号 → AES-256 マスタキー (誤長キーは後段 `AES.new` が `ValueError` で拒否するので明示長チェックは不要)
4. `Default/Login Data` を `sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)` で開く (Windows path は `Path.as_uri()` で URI 化)
5. `SELECT username_value, password_value FROM logins WHERE origin_url LIKE 'https://%.bandainamcoid.com/%' ORDER BY date_last_used DESC LIMIT 1`
6. `password_value` (bytes) の先頭 3 バイトが `b"v10"` **完全一致** であることを確認。`v20` (App-Bound) や未知プレフィックスは全て `None` に倒す
7. `nonce = password_value[3:15]`, `ct_and_tag = password_value[15:]` に分割し、`AES.new(master_key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct_and_tag[:-16], ct_and_tag[-16:])`
8. UTF-8 デコード

**`immutable=1` を採用しない理由**: SQLite 公式 (https://www.sqlite.org/uri.html) は immutable=1 を「read-only media 前提。変更があると誤結果や `SQLITE_CORRUPT` を返しうる」と明言しており、live DB に対して使うのは仕様外。

**Backup API を採用しない理由 (YAGNI)**: Chrome の Login Data は idle 時にはロックを保持しない (実測: `journal_mode = delete`、`-wal`/`-shm` ファイル不在)。auto_relogin が呼ばれるのは check_login=false のときで、その瞬間 Chromium はパスワード保存を能動的に行っていない。`mode=ro` + `timeout=5.0` で足りる。もし将来 `mode=ro` が SQLITE_BUSY を返すケースが実運用で観測されたら、その時点で Backup API + tempfile 方式に移行する。それまでは KISS。

#### 1.2 追加関数 (Windows 限定)

```python
def _dpapi_unprotect(blob: bytes) -> bytes | None:
    """CryptUnprotectData の薄いラッパー。失敗は None。"""

def _load_chrome_master_key(profile_dir: Path) -> bytes | None:
    """Local State から DPAPI 経由で AES-256 マスタキーを取得。"""

def _decrypt_v10_password(master_key: bytes, blob: bytes) -> str | None:
    """v10 プリフィックス完全一致確認 → GCM 復号 → UTF-8。不一致は None。

    誤長 master_key は AES.new が ValueError を投げ、上位 except で None に落ちる。
    """

def _read_bnid_login_row(profile_dir: Path) -> tuple[str, bytes] | None:
    """Login Data を mode=ro + timeout=5.0 で開き、bandainamcoid の最新 1 行を返す。

    Chrome は idle 時に Login Data のロックを保持しないため通常は即読める。
    SQLITE_BUSY は timeout 内に解消しない場合のみ発生し、その時は None に倒す。

    R3 minor 1 反映: SELECT は 1 行だけ拾う (LIMIT 1 + date_last_used DESC)。
    その 1 行の username_value が空文字、または password_value が None/bytes 以外なら
    None を返す (Python 側で最終防御)。より古い有効行への fallback はしない
    (KISS: 実測で 3 アカウント全て 1 行のみ、fallback ロジックの複雑化を避ける)。
    """

def load_credentials(profile_dir: Path) -> Credentials | None:
    """Chrome Login Data から Credentials を組み立てて返す (公開 I/F)。"""
```

非 Windows は `os.name != "nt"` で早期 `None` + stderr 警告。

#### 1.3 例外方針

ファイル非存在 / SQLite エラー / JSON パース失敗 / DPAPI 失敗 / 非 v10 / GCM MAC 不一致 / UTF-8 デコード失敗 — いずれも stderr に理由を書いて `None` を返す。`None` は既存の「手動 login へ」誘導に合流する。認証情報値 (email / password_value 生値含む) はログに出さない。

### 2. `Credentials` の縮小と `ReloginGuard` 分離

#### 2.1 `Credentials`

```python
@dataclass(kw_only=True)
class Credentials:
    """BNID の自動再ログインで使う資格情報 (メモリ内のみ)。

    Chrome Login Data から復号した平文を保持する。ファイル永続化はしない。
    """
    bnid_email: str
    bnid_password: str
```

`saved_at` / `failure_count` / `disabled_until` を削除。前者は不要 (Chrome 側の保存時刻は参照しない)、後 2 者は `ReloginGuard` に分離。

#### 2.2 `ReloginGuard` (パスワードを含まない別 state)

```python
_RELOGIN_GUARD_FILENAME = "relogin_guard.json"

@dataclass(kw_only=True)
class ReloginGuard:
    failure_count: int = 0
    disabled_until: str | None = None
```

- `load_relogin_guard(profile_dir) -> ReloginGuard`: 欠損 / 壊れは既定値 (0/None) + stderr 警告
- `save_relogin_guard(profile_dir, guard) -> None`: `_save_credentials_to` 同型 atomic write
- `_relogin_disabled(guard, name) -> bool`: `_credentials_disabled` からロジック移設
- `_reset_relogin_failure(profile_dir, guard) -> None`: 成功時
- `_record_relogin_failure(profile_dir, guard, *, submissions: int) -> None`: 失敗時

#### 2.3 共有ヘルパー `_run_guarded_auto_login` (round 1 major 反映)

`attempt_auto_relogin` と ASOBI recovery driver の両方が「同じ資格情報 + 同じ guard で auto_login を呼ぶ」ため、ヘルパーに集約する:

```python
def _run_guarded_auto_login(page: Page, profile_dir: Path, name: str) -> bool:
    """credentials + guard を読み、guarded に auto_login して True/False を返す。

    - credentials 取得失敗 (Chrome から復号できない) → False
    - guard.disabled_until が未来 → False (silently skip、既存挙動と同じ)
    - `_run_auto_login_sequence(page, name, credentials, guard)` を呼び、
      retry budget チェックを `guard.failure_count` に対して行う
    - SUCCESS → `_reset_relogin_failure(profile_dir, guard)` して True
    - それ以外で submissions > 0 → `_record_relogin_failure(profile_dir, guard, submissions=submissions)`
    - 返り値は SUCCESS のときだけ True
    """
```

`_run_auto_login_sequence` / `_retry_after_timeout` の signature を `credentials` に加えて `guard: ReloginGuard` を取るように変更する (現状の `credentials.failure_count` 参照を `guard.failure_count` に置換)。

`attempt_auto_relogin` は薄くなり、check_login 短絡救済と goto 失敗ハンドリング、そして `_run_guarded_auto_login` の呼び出しに集約される。`_run_asobi_linkage_recovery` からも同じヘルパーを呼ぶことで、BNID フォーム経路の失敗記録・成功リセット・retry budget が共有される。

#### 2.3 定数のリネーム (任意)

`CREDENTIALS_MAX_FAILURES` / `CREDENTIALS_DISABLE_WINDOW_SEC` は「credentials」が Chrome 側を指すよう再解釈できるので命名は保つ。実装時に一貫性を見て `RELOGIN_MAX_FAILURES` / `RELOGIN_DISABLE_WINDOW_SEC` に改名する余地はあるが、意味は同一。

### 3. E1926 (ASOBI 連携) の自動復旧

#### 3.1 検知 (POST / PUT の両経路)

**POST 経路 (`_complete()`)**: ecode 分岐に E1926 を追加:

```python
if ecode == "E1926":
    print("  -> ASOBI 連携トークン切れ (完了 POST)", file=sys.stderr)
    return "linkage_expired"
```

**戻り値の共通化 (DRY で 1 種類の dataclass に集約)**: `_receive()` (現状 `int`) と `_process_one_mission()` (現状 `int`) の戻り値を **1 つの dataclass `MissionOutcome`** で統一する。ReceiveOutcome を別途作らず、同じ型を使い回す:

```python
@dataclass(kw_only=True)
class MissionOutcome:
    gained: int = 0
    linkage_expired_id: int | None = None  # 検知した mission_id (POST/PUT どちらでも)

def _receive(page, mid, name, pts, *, dry_run: bool) -> MissionOutcome:
    ...
    if ecode == "E1926":
        print("  -> ASOBI 連携トークン切れ (受取 PUT)", file=sys.stderr)
        return MissionOutcome(linkage_expired_id=mid)
    # 成功: return MissionOutcome(gained=pts)
    # 失敗: return MissionOutcome(gained=0)

def _process_one_mission(page, m, *, dry_run: bool) -> MissionOutcome:
    ...
    # 内部で _complete() の "linkage_expired" と _receive() の linkage_expired_id を
    # 束ねて返す
```

`_receive` / `_process_one_mission` はいずれも POST/PUT を送りうる関数なので、4.2 のハイブリッド方針に従い `dry_run: bool` を **必須引数** (デフォルト無し) にする。

`collect_missions` の集約層は `MissionOutcome.linkage_expired_id` を set に集めて次 attempt の対象にする。他ミッションの処理は継続する (E1926 が続く mission_type=1 群は全て `linkage_expired` として集約される)。

#### 3.2 集約と復旧起動 — `collect_missions()` の再構成

```python
def collect_missions(
    page: Page, profile_dir: Path, name: str, *, dry_run: bool, auto_relogin: bool
) -> int:
    listings = _fetch_mission_listings(page)  # 現状の 2 listing GET を関数化
    total_gained = 0
    for attempt in (1, 2):
        result = _process_all_missions(page, listings, dry_run=dry_run)
        total_gained += result.gained  # attempt ごとの獲得を累積
        if not result.linkage_expired_ids or dry_run:
            return total_gained
        if not auto_relogin:
            return total_gained  # --no-auto-relogin の opt-out を尊重
        if not _run_asobi_linkage_recovery(page, profile_dir, name):
            return total_gained  # 復旧失敗 → 現行と同じスキップ扱い
        # 復旧成功、次の attempt で linkage_expired だった id だけを対象に再走
        listings = _filter_listings(listings, keep_ids=result.linkage_expired_ids)
    return total_gained
```

`collect_missions` の signature 拡張 (`profile_dir`, `name` を追加、`dry_run` は 4.2 のハイブリッド方針に従い必須引数、`auto_relogin` は `--no-auto-relogin` opt-out を driver 起動でも尊重するため必須引数): 復旧ドライバが BNID フォームに落ちた際に guard 記録するために必要、かつ保存パスワードの submit 経路をユーザー意志で全遮断できるようにするため。`process_account` からは `collect_missions(page, profile_dir, name, dry_run=options.dry_run, auto_relogin=options.auto_relogin)` を渡す。

- E1926 が起きた `mission_id` を `result.linkage_expired_ids: set[int]` に集約
- 復旧ドライバは 1 実行につき最大 1 回
- `dry_run=True` では復旧を起動しない (POST/PUT を送らない契約と一致)
- 復旧成功後は該当 id だけを再走 (idempotent、他ミッションは触らない)
- 1 回目 attempt の gained (通常 mission 分) は `total_gained` に加算済み、2 回目 attempt の gained は E1926 の再走分だけを加算するので、二重計上は起きない

#### 3.2.1 `_process_all_missions` の返却契約

```python
@dataclass(kw_only=True)
class MissionRunResult:
    gained: int = 0
    linkage_expired_ids: set[int] = field(default_factory=set)  # POST/PUT どちらで検知したかは問わない
```

集約は `_process_one_mission()` が返す `MissionOutcome.linkage_expired_id` を set に足し、`gained` を合算する。

#### 3.3 復旧ドライバ

```python
LINKAGE_ENTRY_URL = (
    f"{API_HOST}/api/v1_1_0/linkages/as/login"
    f"?backto={urllib.parse.quote(MISSION_PAGE_URL, safe='')}"
)

def _run_asobi_linkage_recovery(
    page: Page, profile_dir: Path, name: str
) -> bool:
    """linkages/as/login を踏んで ASOBI 連携トークンを再取得する。成功なら True。

    - BNID セッション生存: 自動通過して backto (mission page) に着地
    - BNID セッション切れ: 途中の BNID フォームを `_run_guarded_auto_login` で
      突破 (attempt_auto_relogin と共有の guard で retry budget も一元管理)
    - 中間ページ (legacy-login.asobistore.jp) の「バンダイナムコIDでログイン」は
      可視化されたらクリック (BNID セッション生存時は自動通過するのが観測済み)
    """
```

処理骨子:

1. `page.goto(LINKAGE_ENTRY_URL, wait_until="domcontentloaded")`
2. `deadline` 内で以下のいずれかを polling:
   - **(a) backto 到達** (`page.url` が `MISSION_PAGE_URL` の path プレフィックスを含む): 成功 → step 3
   - **(b) 中間ページの「バンダイナムコIDでログイン」が可視**: `click()` → polling 継続
   - **(c) BNID フォーム `#mail` が可視**: `_run_guarded_auto_login(page, profile_dir, name)` を呼ぶ。True なら polling 継続、False なら失敗として `False` を返す (`_run_guarded_auto_login` 内部で guard の failure_count を更新済み)
   - **(d) CAPTCHA/2FA 検知** (BNID フォーム外の路線でも): 失敗
   - **(e) タイムアウト** (60 秒): 失敗
3. `time.sleep(10)` (cookie 保存事故対策、`project-asobi-linkage` 記録)
4. `page.goto(MISSION_PAGE_URL, wait_until="domcontentloaded")` で mission page に戻して `True` を返す

**guard 共有**: BNID フォーム経路の失敗記録・成功リセット・retry budget は `_run_guarded_auto_login` が集約するため、driver と `attempt_auto_relogin` の間で二重ガード / 二重加算は起きない。

#### 3.4 「バンダイナムコIDでログイン」ボタンのセレクタ

現状コードに参考実装が無いので、実装時に headless で `page.content()` を dump するか実プロファイルで DevTools 確認 → 1 本に絞る。暫定は複数候補で:

- `a[href*="idsvc"]`
- `a:has-text("バンダイナムコID")`
- `button:has-text("バンダイナムコID")`

#### 3.5 成功の確定

`backto` 到達は暫定成功。真の成功は「E1926 だった mission の POST/PUT が今度は E1926 を返さないこと」で二段確認する (再走で自動的に検証される)。

### 4. `--execute` 撤去 → `--dry-run` 一本化

#### 4.1 CLI パーサ

- `--execute` を削除、`--dry-run` を追加 (`action="store_true"`)
- `args.execute` → `args.dry_run` に置換
- `mission` / `checkin` サブコマンド両方に共通引数として付与 (現行 `collect` 親パーサ経由と同じ)

#### 4.2 内部シグネチャの全置換 (ハイブリッド: dataclass はデフォルト、POST-able 関数は必須)

デフォルト値の扱いを 2 種類に分ける (R3 major 1 反映):

**dataclass (`kw_only=True`)**: デフォルト `False` を維持する。これは caller が数箇所しかなく (CLI パーサ経由の `_build_run_options` と login/login-init 系)、目視 audit しやすい:

- `RunOptions` の `dry_run: bool = False`
- `CheckinSettings` の `dry_run: bool = False`
- 副作用: `RunOptions(login_mode=True)` は `dry_run=False` (本番) のまま動く

**POST-able 関数 (実 POST/PUT を送りうる)**: デフォルト無しの必須引数にする。関数の caller は多く、将来 helper が増える度に「引数忘れ = 本番 POST」ハザードを持ち込むため、型/実行時で失敗させる:

- `collect_missions(page, profile_dir, name, *, dry_run: bool, auto_relogin: bool)` — デフォルト無し
- `_complete(page, mid, name, *, dry_run: bool)` — デフォルト無し
- `_receive(page, mid, name, pts, *, dry_run: bool) -> MissionOutcome` — デフォルト無し
- `_process_one_mission(page, m, *, dry_run: bool)` — デフォルト無し
- `_process_all_missions(page, listings, *, dry_run: bool)` — デフォルト無し
- `_CheckinRunner` 内部の `self.settings.execute` → `not self.settings.dry_run` (dataclass 経由なので必須化の対象外)

判定の反転:

- `if not execute:` → `if dry_run:`
- `if execute:` → `if not dry_run:`

`_ensure_authenticated` (L2386-2412) の auto_relogin ゲートも `options.execute` を `not options.dry_run` に置換 (dry_run では BNID にパスワード POST を送らない、という現行契約を維持)。

`_build_run_options` は CLI から `RunOptions(dry_run=args.dry_run, ...)` を明示的に組み立てる。テストは `collect_missions(fake_page, tmp, "test")` のような引数忘れが `TypeError` で即座に失敗する (POST-able 関数の必須化の効果)。

**Migration audit 手順 (実装完了時に必ず実行)**:

1. `rg -n "\bexecute\b" canvasser.py tests/ README.md` で `execute` 語の残存を確認 (`docs/superpowers/specs/` は歴史的アーカイブとして検査対象から除外)
2. tests/ 全てで `RunOptions(...)` / `CheckinSettings(...)` の呼び出しを目視し、意図する dry_run の値が明示されているかを確認 (POST-able 関数側は必須化で自動チェック)
3. `uv run pytest tests/` で全テスト通過を確認

#### 4.3 デフォルト意味変更のリスクと緩和

デフォルト値の意味が反転する (`execute=False`=dry-run → `dry_run=False`=本番) ため、全 caller を明示化して意図せぬ本番化を防ぐ:

- CLI の `_main_impl` は `RunOptions(dry_run=args.dry_run, ...)` を明示的に渡す
- テストの `RunOptions(...)` / `CheckinSettings(...)` は全て `dry_run=True` / `dry_run=False` を明示
- 全置換完了後、`rg -n "\bexecute\b" canvasser.py tests/ README.md` で残存が無いことを確認 (`docs/superpowers/specs/` は歴史的アーカイブとして検査対象から除外)
- 最終出力の実行モード表示は `"本番" if not dry_run else "DRY-RUN (POST送信なし)"` に反転

#### 4.4 タスクスケジューラのコマンド更新 (round 1 major: 必須手順化)

`--execute` を CLI から削除するため、既存の日次タスク (`mission --execute --profiles-dir D:\...\profiles`) は **CLI 変更後は unknown argument で失敗する**。デプロイ前後で以下の手順を **必須**として Rollout に組み込む:

1. **デプロイ前** (PR マージ前) の確認:
   ```powershell
   schtasks /Query /TN "cgge-canvasser" /V /FO LIST | Select-String "Task To Run"
   ```
   現行の TR が `... mission --execute ...` を含むことを確認。

2. **デプロイ直後** (PR マージ後、初回スケジュール実行前) に更新:
   ```powershell
   schtasks /Change /TN "cgge-canvasser" /TR "\"C:\Users\shun\.local\bin\uv.exe\" run canvasser.py mission --profiles-dir D:\projects\cgge-canvasser\profiles"
   ```
   uv.exe のパスは `where.exe uv` で事前確認する。

3. **更新後の確認**:
   ```powershell
   schtasks /Query /TN "cgge-canvasser" /V /FO LIST | Select-String "Task To Run"
   ```
   TR に `--execute` が含まれないこと、`mission` が単独で入っていることを確認。

4. **PR 直後の smoke test** (翌日 12:05 を待たず、即日確認):

   4-a. まず parse / profile 認識 / mission GET の確認:
   ```powershell
   cd D:\projects\cgge-canvasser
   uv run canvasser.py mission --dry-run
   ```
   3 アカウントとも exit 0 で終わることを確認。

   4-b. **Login Data 読みの独立確認** (dry_run は auto_relogin を走らせないので上記だけでは検証できない):
   ```powershell
   uv run python -c "from canvasser import load_credentials; from pathlib import Path; [print(a, load_credentials(Path(f'profiles/{a}')) is not None) for a in ('haruo','shun','syota')]"
   ```
   3 アカウントとも `True` が出ることを確認 (パスワード値は表示しない、Credentials が非 None であることだけを確認)。失敗したら即座に切り戻し (下記の rollback 手順)。

5. **初回本番実行結果の確認**: 翌日 12:05 の実行後に `Get-ScheduledTaskInfo -TaskName cgge-canvasser` の `LastTaskResult=0` と、獲得票数 3 アカウント合計 +21 枚 (デイリー) を Discord で報告する。

#### 4.4.1 rollback 手順 (旧コードに戻す場合)

新コードで問題が出て旧コード (`main` の直前 commit) に戻したくなった場合、**タスクスケジューラの TR にも `--execute` を戻す必要がある** (旧コードは `--execute` 未指定だと dry-run で無回収終了する):

```powershell
git checkout main  # or 対象の revert commit

schtasks /Change /TN "cgge-canvasser" /TR "\"C:\Users\shun\.local\bin\uv.exe\" run canvasser.py mission --execute --profiles-dir D:\projects\cgge-canvasser\profiles"

# 確認
schtasks /Query /TN "cgge-canvasser" /V /FO LIST | Select-String "Task To Run"
```

TR に `--execute` が含まれることを確認する。含まれない状態で旧コードが走ると成功扱いで無回収 (見かけ上の成功) になる、これが最悪シナリオ。

#### 4.5 コミット分割

同一ブランチ内で意味単位に分ける。ロールバックしやすい順序:

1. `refactor: --execute を廃し --dry-run に一本化 (デフォルト本番)`
2. `feat: Chrome Login Data から BNID 資格情報を復号 (credentials.json/login-init を撤去)`
3. `feat: E1926 で linkages/as/login を踏み ASOBI 連携を自動復旧`
4. `docs: README を新方式に更新`

---

## ファイル変更マップ

### `canvasser.py` (概算 -600 / +200 行)

**追加**:
- `_dpapi_unprotect`, `_load_chrome_master_key`, `_decrypt_v10_password`, `_read_bnid_login_row` (Chrome Login Data 復号)
- `_RELOGIN_GUARD_FILENAME`, `ReloginGuard`, `_load_relogin_guard_from`, `_save_relogin_guard_to`, `load_relogin_guard`, `save_relogin_guard`
- `_relogin_disabled`, `_reset_relogin_failure`, `_record_relogin_failure` (旧 `_credentials_*` からロジック移設)
- `_run_guarded_auto_login` (attempt_auto_relogin と ASOBI driver で共有)
- `LINKAGE_ENTRY_URL`, `_run_asobi_linkage_recovery`, `_fetch_mission_listings`, `_process_all_missions`, `_filter_listings`, `MissionOutcome`, `MissionRunResult` (dataclass, kw_only=True。ReceiveOutcome は作らず MissionOutcome を使い回す)
- `import sqlite3`, `import ctypes`, `from urllib.parse import quote`

**削除**:
- `_CREDENTIALS_FILENAME`, `_CREDENTIALS_PENDING_FILENAME`
- `_credentials_file`, `_pending_credentials_file`, `_apply_credentials_permissions`
- `_load_credentials_from` (中身入替), `_save_credentials_to`, `save_credentials`, `load_pending_credentials`, `save_pending_credentials`, `_activate_pending_credentials`, `_discard_pending_credentials`
- `_prompt_credentials`, `persist_login_init_credentials`, `run_login_init_flow`
- `RunOptions.login_init_mode`, `_main_impl` の `login_init_mode` 分岐, `process_account` の `login_init_mode` 分岐
- argparse の `login-init` subparser
- `import getpass` (grep で `_prompt_credentials` 以外の使用がないため撤去)

**opportunistic DRY**:
- `save_account_state` (L1649-1672) と guard 保存の atomic write が同型 → `_atomic_write_json(path, data: dict)` に共通化する余地あり。実装時に判断 (無理に共通化しない、明らかな重複なら抽出する)。

**改変**:
- `Credentials` (フィールド縮小)
- `load_credentials` (中身差替)
- `attempt_auto_relogin` (`ReloginGuard` に追随)
- `collect_missions` (再構成), `_complete` (E1926 分岐追加), `_process_one_mission` (linkage_expired 伝搬)
- `_build_parser`, `_build_run_options`, `_main_impl` (`--execute` → `--dry-run`)
- `RunOptions`, `CheckinSettings`, `_CheckinRunner` の各 execute 参照

### `tests/` (概算 -272 + 400 行、既存改変多数)

**新規**:
- `tests/test_chrome_credentials.py` (~250 行) — 復号ロジックのユニット + SQLite fixture
- `tests/test_relogin_guard.py` (~120 行) — guard の CRUD と会計
- `tests/test_asobi_linkage_recovery.py` (~200 行) — driver の分岐 (backto 直着地 / 中間ページ click / BNID フォーム auto_login / タイムアウト / dry_run)

**削除**:
- `tests/test_credentials.py` (272 行、全撤去)

**改変**:
- `tests/test_login_flow.py` — `Credentials(...)` 構築の引数削減、ガード会計テストを `ReloginGuard` に切替、`_run_guarded_auto_login` の呼び出し経路確認を追加
- `tests/test_missions.py` — E1926 linkage_expired 分岐 (POST/PUT 両方)、`MissionOutcome` 化 (`_receive` と `_process_one_mission` 双方の戻り値変更)、execute → dry_run 反転
- `tests/test_checkin_flow.py` — execute → dry_run 反転
- `tests/test_state.py` — execute → dry_run 反転 (`load_account_state(strict=)` 直接扱いなので影響は小さいはず、要確認)
- `tests/test_cli_validation.py` — `--execute` → `--dry-run`
- `tests/_fakes.py` — execute → dry_run 反転

**module docstring 更新** (canvasser.py L11-25):
- `uv run canvasser.py mission --execute` の例文を `uv run canvasser.py mission` に
- `uv run canvasser.py checkin --execute` の例文を `uv run canvasser.py checkin` に
- 「`--execute` 未指定なら GET のみのドライラン」→ 「`--dry-run` 指定で GET のみのドライラン、無指定なら本番」

**最終検査**: 実装完了時に `rg -n "\bexecute\b" canvasser.py tests/ README.md` で `execute` 語の残存を確認する (許容: `--dry-run` 説明文中の「本番実行」等の日本語出現のみ)。**`docs/superpowers/specs/` は歴史的アーカイブとして検査対象から除外** (この設計 doc 自体が `--execute` 廃止の経緯を大量に記述しており、language 上の記述として残す)。

### `README.md`

- 「自動再ログイン用の資格情報登録 (`login-init`)」章 (L45-66) を削除
- 「初回ログイン」章 (L47-56) に「Chrome の自動保存パスワードに保存する運用」を統合
- タスクスケジューラ例 (L129) から `--execute` を外す
- `mission --execute` / `checkin --execute` の記述を `mission` / `checkin` (本番) と `--dry-run` (確認モード) に置換
- ASOBI 連携復旧 (E1926) が自動化された旨を追記

---

## テスト戦略 (t-wada 式)

### 観点別

- **偽陽性リスク**: `_run_asobi_linkage_recovery` は Playwright モックで組むため、実プロファイル差異でモックが実挙動と乖離する可能性 → 実装時に少なくとも 1 回、実 asobistore を通した通過確認をする (要 E1926 誘発、後述)
- **偽陰性リスク**: `dry_run` 反転で、明示せず default に頼るテストが実 POST 経路を通ってしまう → 全 caller の引数を明示化
- **状態非共有**: `relogin_guard.json` は `tmp_path` で独立
- **時刻**: `datetime.now(JST)` を触るコードは既存パターンに合わせて凍結

### 主要テストケース (最低ライン)

**Chrome Login Data 復号**:
1. 正しい master_key + v10 blob → 平文取得
2. 誤長 (32 bytes 以外) の master_key → AES.new が ValueError → 上位 except で None
3. 32 bytes だが誤 master_key → GCM MAC 検証失敗 → None
4. GCM MAC 検証失敗 (nonce/tag 改ざん) → None
5. v20 blob → 明示的に None
6. Login Data 非存在 / bandainamcoid レコード非存在 → None
7. Local State 非存在 → None
8. DPAPI 復号失敗 (blob 破損) → None
9. GCM 復号結果が UTF-8 でない → None
10. SQLite の SQLITE_BUSY (timeout 内解消せず) → None
11. `username_value` が空文字 / `password_value` が None → None (LATEST 行が空データの場合の防御、fallback なし)

**ReloginGuard**:
1. 欠損 → 既定値 (0/None)
2. 壊れ JSON → 既定値 + stderr 警告
3. save → load 往復で state 維持
4. `_record_relogin_failure(submissions=2)` で 0 → 2、MAX (3) 到達で disabled_until 設定
5. disabled_until 未来 → `_relogin_disabled` True、過去 → False

**E1926 復旧**:
1. 1 mission の POST が E1926 → driver 起動 → 復旧成功 → 再走で通る
2. 1 mission の PUT (受取) が E1926 → driver 起動 → 復旧成功 → 再走で通る (round 1 反映)
3. 複数 mission が E1926 → driver 1 回で全部再走
4. 1 回目 attempt の通常 mission 分 gained が 2 回目 attempt 後も返却値に含まれる (gained 累積確認、round 1 反映)
5. driver: backto 直着地 (BNID セッション生存) で成功
6. driver: BNID フォームに落ちる → `_run_guarded_auto_login` SUCCESS → 復旧成功 (guard の failure_count が 0 リセット)
7. driver: BNID フォームで PASSWORD_ERROR → 復旧失敗 (guard の failure_count が +1)
8. driver: CAPTCHA 検知 → 復旧失敗
9. driver: タイムアウト → 復旧失敗
10. `dry_run=True` の時 driver を起動しない
11. `_run_guarded_auto_login` は attempt_auto_relogin と ASOBI driver の両方で呼ばれても guard state を二重加算しない (round 1 反映)

**`--dry-run` 反転**:
1. 引数無しで `mission` 実行 → 本番 POST/PUT が送られる (mock 期待呼び出し)
2. `--dry-run` で `mission` 実行 → POST/PUT が送られない
3. 引数無しで `checkin` 実行 → 本番 POST + state 更新 + 滞在 sleep
4. `--dry-run` で `checkin` 実行 → GET のみ、state 未更新

---

## 実装時に確認する項目 (Investigation)

現時点で E1926 が実発生していない (3 アカウントとも連携生存) ため、以下は実装フェーズで実プロファイルを触って確認する:

- [ ] `legacy-login.asobistore.jp` 中間ページの「バンダイナムコIDでログイン」の正確なセレクタと text
- [ ] BNID セッション生存時、中間ページが自動通過されるか (可視化されないか)
- [ ] 復旧成功の確定検知点として最も安定な signal (`page.url` / 特定 DOM / API レスポンス)
- [ ] headless で asobistore 側 JS が期待通り動くか (BNID 側は既存 auto_login で headless 動作確認済み)
- [ ] Playwright sync API の headless で `page.url` polling が freeze しないか (`project-asobi-linkage` の headed poll freeze 記録との対応)

**E1926 誘発手段** (実実装で必要な場合の最終手段): haruo プロファイルの `Default/Cookies` を Playwright 停止中に SQLite で開き、`asobistore.jp` 系 cookie の `expires_utc` を過去に書き換える (再現後は再連携で復旧できる)。

---

## Rollout / 移行

1. `credentials.json` は 3 アカウントとも不在 → **削除ロジック不要**
2. `login-init` サブコマンド撤去 → 過去に叩いていた運用は消えるが、次回 BNID セッション切れ時に自動で復旧するはず (Chrome 側に既に保存済みのため追加操作なし)
3. **タスクスケジューラのコマンド更新は必須** (4.4 の手順を PR マージ直後に実施)。放置すると翌 12:05 実行が unknown argument で失敗する
4. README 更新 (login-init 章削除、`--dry-run` 説明、タスクスケジューラ例更新)

## リスクと緩和

1. **v20 (App-Bound Encryption) 化**: 将来 Chromium が v20 に切り替わったら復号不能 → 「v10 でなければ手動 login へ」既存誘導に合流。監視は不要 (壊れたら手動 login すればよい)
2. **`--dry-run` 反転による誤本番化**: 移行時ハザードは一度きり + POST-able 関数の必須化 (dataclass はデフォルト維持) で ongoing hazard も型で潰す。実装完了時に `rg` grep + 全 caller 明示化 audit
3. **E1926 driver の頑健性不足**: 現状 3 アカウントで再現しにくい → セレクタ候補複数、失敗時は現状通りスキップに退行 (デグレなし)
4. **Login Data 読み込みの整合性**: `immutable=1` は不採用 (SQLite 公式が live DB 適用を否定)。`mode=ro` + `timeout=5.0` を採用。Chrome の idle-locked 特性 (実測: `journal_mode = delete`) で通常は即読める。SQLITE_BUSY が実運用で観測されたら Backup API に移行 (YAGNI)
5. **DPAPI は同ユーザーコンテキストで復号可能**: これは Chrome 本体のパスワード保護と同レベル。credentials.json 平文 (現状) より改善こそすれ、退化はない
6. **タスクスケジューラ更新漏れ**: Rollout 手順を必須化し、確認コマンドと rollback matrix を併記
7. **retry URL の ASOBI 経路対応 (受容)**: `_run_guarded_auto_login` 内部の retry が `LOGIN_ENTRY_URL` に飛ぶことで、ASOBI flow 中の BNID timeout 時に linkage 状態を失う可能性がある。ただし発生確率が低く graceful degradation (翌日再試行) するため、パラメータ化はしない (YAGNI)

---

## 参考

- 前セッションのハンドオフ (memory: `handoff-2026-07-07-chrome-login-data`)
- ASOBI 連携復旧の手動手順 (memory: `project-asobi-linkage`)
- タスクスケジューラ登録済み (memory: `project-task-scheduler`)
- 開発用プロファイル (memory: `project-dev-profile`)

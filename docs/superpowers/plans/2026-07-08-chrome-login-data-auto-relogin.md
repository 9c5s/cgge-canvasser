# Chrome Login Data 自動再ログイン + ASOBI 連携自動復旧 + `--dry-run` 一本化 Implementation Plan

- Review: round 1 ISSUES → round 2 ISSUES → round 3 ISSUES → **round 4 PASS** (codex-review-loop、2026-07-08)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Playwright プロファイル内の Chrome 自動保存パスワードから BNID 資格情報を復号して自動再ログインを完結させ、E1926 (ASOBI STORE 連携切れ) を検知したら自動復旧させ、あわせて日次運用の実態 (無指定=本番実行) に合わせて `--execute` を `--dry-run` に反転する。

**Architecture:** 既存 `auto_login()` (Playwright 経由の BNID ログインフォーム自動入力) の資格情報供給元だけを `credentials.json` 平文永続化から Chrome Login Data (Windows DPAPI + AES-256-GCM v10 形式) 復号に差し替える。失敗ガード (`failure_count` / `disabled_until`) はパスワードを含まない `relogin_guard.json` に分離。ASOBI 連携切れは `_run_asobi_linkage_recovery` ドライバが `linkages/as/login` を踏み、途中で BNID フォームが出たら `_run_guarded_auto_login` 共有ヘルパー経由で突破する。CLI/内部 API から `execute` 語を完全撤去し、POST-able 関数は `dry_run` を必須引数化する。

**Tech Stack:** Python 3.14 / Playwright (sync API, headless) / pycryptodome (AES-GCM) / ctypes (Windows CryptUnprotectData) / sqlite3 (Chrome Login Data 読み) / uv (パッケージ/実行) / pytest (テスト) / Conventional Commits

## Global Constraints

- 対象 OS: Windows のみ (非 Windows は `os.name != "nt"` で早期 `None` + stderr 警告)
- 認証情報 (email/password 平文/`password_value` 生値) はログに出さない
- POST-able 関数 (`collect_missions` / `_complete` / `_receive` / `_process_one_mission` / `_process_all_missions`) は `dry_run: bool` を **必須引数** (デフォルト無し) にする。dataclass (`RunOptions` / `CheckinSettings`) のみデフォルト `dry_run: bool = False` を許容する
- `dry_run=True` では POST/PUT を送らず、`auto_relogin` も走らせない (BNID にパスワードを送らない)
- `credentials.json` / `credentials.json.pending` を新規に一切作らない
- `login-init` サブコマンドは撤去し、代替は既存 `login` サブコマンド (手動ログイン → Chrome が自動保存)
- v10 プリフィックス完全一致のみ受理、v20 (App-Bound) や未知プレフィックスは復号せず `None`
- SQLite Login Data は `sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)` で開く (`immutable=1` は使わない)
- コミットは Conventional Commits v1.0.0 に準拠、1 コミット 1 目的
- 日本語コメント (だ・である調)、句読点は「、」「。」
- Python コメントは既存スタイルに合わせる
- 詳細な設計判断・押し返し履歴は `docs/superpowers/specs/2026-07-08-chrome-login-data-auto-relogin-design.md` を必ず参照

---

## Phase 1: `--execute` → `--dry-run` 反転

### Task 1: `execute` を `dry_run` に完全置換しデフォルトを本番に反転

**Files:**
- Modify: `canvasser.py:19-24` (module docstring 例文)
- Modify: `canvasser.py:256-393` (collect_missions / _process_one_mission / _complete / _receive の signature)
- Modify: `canvasser.py:664-1128` (CheckinSettings / _CheckinRunner の execute → dry_run)
- Modify: `canvasser.py:2386-2412` (_ensure_authenticated の execute ゲート)
- Modify: `canvasser.py:2669-2947` (RunOptions / process_account / _build_run_options / _build_parser)
- Modify: `canvasser.py:2997-3056` (_main_impl の args.execute 参照)
- Modify: `tests/_fakes.py` (execute キーワード引数の使用箇所)
- Modify: `tests/test_missions.py` (collect_missions / _complete / _receive の呼び出し)
- Modify: `tests/test_checkin_flow.py` (execute 参照)
- Modify: `tests/test_cli_validation.py` (--execute 引数の parse テスト)
- Modify: `tests/test_state.py` (execute 参照があれば)
- Modify: `tests/test_login_flow.py` (execute 参照があれば)

**Interfaces:**
- Produces:
  - `RunOptions.dry_run: bool = False` (dataclass field、デフォルト有り)
  - `CheckinSettings.dry_run: bool = False` (dataclass field、デフォルト有り)
  - `collect_missions(page: Page, profile_dir: Path, name: str, *, dry_run: bool) -> int` (必須引数、`profile_dir`/`name` は Phase 3 で追加するが本タスクでは呼び出し互換だけ担保しシグネチャは変更しない — つまり本タスクは `collect_missions(page: Page, *, dry_run: bool) -> int` に留める)
  - `_complete(page: Page, mid: int, name: str, *, dry_run: bool) -> str` (必須引数)
  - `_receive(page: Page, mid: int, name: str, pts: int, *, dry_run: bool) -> int` (必須引数、戻り値は Phase 3 で `MissionOutcome` に変更)
  - `_process_one_mission(page: Page, m: dict[str, Any], *, dry_run: bool) -> int` (必須引数、戻り値は Phase 3 で `MissionOutcome` に変更)
  - CLI: `mission` / `checkin` に `--dry-run` フラグ (無指定は本番)、`--execute` は削除
  - Module docstring の例文: `uv run canvasser.py mission` (本番) / `uv run canvasser.py mission --dry-run` (確認)

- [ ] **Step 1: `canvasser.py` の関数 signature を反転**

以下の関数の keyword-only 引数を `execute: bool = False` から `dry_run: bool` に変更する。POST-able 関数はデフォルト無し。判定は `if not execute:` → `if dry_run:`、`if execute:` → `if not dry_run:` に反転する。

対象: `collect_missions` (L256), `_process_one_mission` (L302), `_complete` (L339), `_receive` (L373), `_ensure_authenticated` (L2386, 内部で `options.execute` → `not options.dry_run`)。

例 (`_complete`):

```python
def _complete(page: Page, mid: int, name: str, *, dry_run: bool) -> str:
    """ミッション達成の POST を送る。

    dry_run=True の場合、`_complete` は POST を送らずに "ok" を返す。
    """
    print(f"[達成] #{mid} {name}")
    if dry_run:
        print("  -> DRY-RUN (POST送信なし)")
        return "ok"
    res = call_api(page, "POST", f"/mission/{mid}")
    ...
```

- [ ] **Step 1.5: 表示ラベル "EXECUTE (本番)" を "本番" に置換**

`collect_missions` (L275) と `_CheckinRunner` 側の同種ラベルを検索して置換する:

Before: `mode_label = "EXECUTE (本番)" if execute else "DRY-RUN (POST/PUT送信なし)"`
After: `mode_label = "本番" if not dry_run else "DRY-RUN (POST/PUT送信なし)"`

`rg -n "EXECUTE" canvasser.py` でヒットする全箇所 (通常 2 箇所: `collect_missions` と `_CheckinRunner._print_footer` の周辺) を対象にする。「EXECUTE」語を canvasser.py から完全撤去する (`execute` の完全撤去要件と揃える)。

- [ ] **Step 2: `CheckinSettings` と `_CheckinRunner` の execute を dry_run に反転**

`CheckinSettings.execute: bool = False` (L673) を `CheckinSettings.dry_run: bool = False` に変更。dataclass はデフォルト維持。`_CheckinRunner` (L781-1128) 内部の全 `self.settings.execute` を `not self.settings.dry_run` に置換する (真偽が反転するので条件式も反転)。

該当箇所の探し方: `rg -n "settings\.execute" canvasser.py` で 20 件程度ヒットする、全て `not settings.dry_run` に置換。

- [ ] **Step 3: `RunOptions` と process_account / _build_run_options を反転**

- `RunOptions.execute: bool = False` (L2682) → `RunOptions.dry_run: bool = False`
- `_build_run_options` (L2924) の分岐で `execute=args.execute` → `dry_run=args.dry_run` に、mission/checkin 側は `RunOptions(run_mission=True, dry_run=args.dry_run, auto_relogin=not args.no_auto_relogin)` のように書き直す
- `process_account` (L2691) の `collect_missions(page, execute=options.execute)` → `collect_missions(page, dry_run=options.dry_run)`
- `collect_checkins` へ渡す `CheckinSettings` も同様

- [ ] **Step 4: CLI パーサから `--execute` を削除して `--dry-run` を追加**

`_build_parser` の `collect` 親パーサ (L2807-2812) を差し替える:

```python
collect.add_argument(
    "--dry-run",
    action="store_true",
    help="POST/PUT を送らない完全ドライラン (GET のみ)。state 更新や checkin の"
    "滞在 sleep も行わない。無指定なら本番実行。",
)
```

`--execute` の add_argument 呼び出しを削除する。`args.execute` を参照している箇所は Step 3 で消えているはず。

- [ ] **Step 5: `_ensure_authenticated` の auto_relogin ゲートを反転**

`canvasser.py:2386-2412` の以下を書き換える:

```python
if (
    options.auto_relogin
    and options.execute  # ← ここを反転
    and attempt_auto_relogin(page, profile_dir, name)
):
    return True
```

を:

```python
if (
    options.auto_relogin
    and not options.dry_run
    and attempt_auto_relogin(page, profile_dir, name)
):
    return True
```

docstring の「dry-run (execute=False) では〜」も「dry_run では〜」に書き換える。

- [ ] **Step 6: module docstring の例文と usage 節を書き換え**

`canvasser.py:11-25` の docstring:

```python
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
```

- [ ] **Step 7: 全テストファイルの `execute` キーワードを反転**

以下のパターンを全ファイルで一括置換する:

- `execute=True` → `dry_run=False` (本番実行)
- `execute=False` → `dry_run=True` (ドライラン)
- 引数無し (`RunOptions()` や `CheckinSettings()` のように execute を渡していない箇所) → dataclass デフォルトが変わる (旧: execute=False=dry-run → 新: dry_run=False=本番)。この場合は **期待値を反転** し、テストの意図を明示的に `dry_run=True` (dry-run 継続) にするか `dry_run=False` (本番動作、テストが本番挙動を検証) にするか個別に判断する

対象ファイル: `tests/_fakes.py`, `tests/test_missions.py`, `tests/test_checkin_flow.py`, `tests/test_cli_validation.py`, `tests/test_state.py`, `tests/test_login_flow.py`。

`tests/test_cli_validation.py` の `--execute` 引数 parse テストは `--dry-run` に置き換え、無指定時の挙動 (本番を意味する `RunOptions.dry_run == False`) の assertion に反転する。

- [ ] **Step 8: `rg -n "\bexecute\b" canvasser.py tests/ README.md` を実行して残存を目視確認**

Run: `rg -ni "\bexecute\b" canvasser.py tests/ README.md`
Expected: `execute` 語 (大文字小文字問わず) がヒットしないこと (README は Task 10 で扱うためこの時点では一部残存は許容、ただし canvasser.py と tests/ からは 0 件)

`docs/superpowers/specs/` は歴史的アーカイブなので検査対象外。

- [ ] **Step 9: 全テストを実行**

Run: `uv run pytest tests/ -v`
Expected: 全テスト PASS

- [ ] **Step 10: コミット**

```bash
git add canvasser.py tests/
git commit -m "refactor: --execute を廃止し --dry-run に一本化"
```

---

## Phase 2: 失敗ガード分離と Chrome Login Data 復号

### Task 2: `ReloginGuard` dataclass と CRUD を追加

**Files:**
- Modify: `canvasser.py` (`_RELOGIN_GUARD_FILENAME`, `ReloginGuard`, `load_relogin_guard`, `save_relogin_guard` を追加)
- Create: `tests/test_relogin_guard.py`

**Interfaces:**
- Produces:
  - `_RELOGIN_GUARD_FILENAME: str = "relogin_guard.json"` (module-level 定数)
  - `class ReloginGuard` — `@dataclass(kw_only=True)`, fields: `failure_count: int = 0`, `disabled_until: str | None = None`
  - `load_relogin_guard(profile_dir: Path) -> ReloginGuard` — 欠損・破損時は既定 `ReloginGuard()` (0/None) を返し stderr に警告
  - `save_relogin_guard(profile_dir: Path, guard: ReloginGuard) -> None` — atomic write (`save_account_state` と同型: tempfile → fsync → replace)

- [ ] **Step 1: 失敗ケースのテストを書く**

`tests/test_relogin_guard.py` を新規作成:

```python
"""relogin_guard.json (失敗ガード state) の CRUD テスト。"""

import json
from pathlib import Path

import pytest

from canvasser import ReloginGuard, load_relogin_guard, save_relogin_guard


def test_load_missing_returns_default(tmp_path: Path) -> None:
    """profile_dir に relogin_guard.json が無ければ既定値を返す。"""
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard(failure_count=0, disabled_until=None)


def test_load_broken_json_returns_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """壊れた JSON なら既定値 + stderr 警告。"""
    (tmp_path / "relogin_guard.json").write_text("{not json", encoding="utf-8")
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard()
    assert "relogin_guard.json" in capsys.readouterr().err


def test_load_wrong_shape_returns_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """トップレベルが dict でない場合、既定値 + stderr 警告。"""
    (tmp_path / "relogin_guard.json").write_text("[]", encoding="utf-8")
    guard = load_relogin_guard(tmp_path)
    assert guard == ReloginGuard()
    assert "relogin_guard.json" in capsys.readouterr().err


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """save → load 往復で state が保存される。"""
    original = ReloginGuard(failure_count=2, disabled_until="2026-07-08T18:00:00+09:00")
    save_relogin_guard(tmp_path, original)
    assert load_relogin_guard(tmp_path) == original


def test_save_atomic_writes_json(tmp_path: Path) -> None:
    """save 後の JSON 内容が dataclass と一致する。"""
    guard = ReloginGuard(failure_count=1, disabled_until=None)
    save_relogin_guard(tmp_path, guard)
    data = json.loads((tmp_path / "relogin_guard.json").read_text(encoding="utf-8"))
    assert data == {"failure_count": 1, "disabled_until": None}
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_relogin_guard.py -v`
Expected: `ImportError: cannot import name 'ReloginGuard' from canvasser` などで FAIL

- [ ] **Step 3: `canvasser.py` に ReloginGuard と CRUD を実装**

適切な位置 (`_credentials_file` (L1697) の少し下、または `Credentials` (L1676) の隣) に以下を追加する。既存 `save_account_state` (L1649-1672) と `_load_credentials_from` (L1738) の atomic write / defensive JSON パースパターンを参考にする:

```python
_RELOGIN_GUARD_FILENAME = "relogin_guard.json"


@dataclass(kw_only=True)
class ReloginGuard:
    """自動再ログインの連続失敗ガード state。パスワードを含まない。

    - `failure_count`: 連続失敗回数。成功で 0 にリセットする。
    - `disabled_until`: MAX_FAILURES に達したときに設定する JST ISO8601。
    """

    failure_count: int = 0
    disabled_until: str | None = None


def _relogin_guard_file(profile_dir: Path) -> Path:
    return profile_dir / _RELOGIN_GUARD_FILENAME


def load_relogin_guard(profile_dir: Path) -> ReloginGuard:
    """profile_dir/relogin_guard.json を読み込む。欠損・破損は既定値。"""
    path = _relogin_guard_file(profile_dir)
    if not path.exists():
        return ReloginGuard()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(
            f"[warn] {path} を読めません: {e}。ガードを既定値にリセットします。",
            file=sys.stderr,
        )
        return ReloginGuard()
    if not isinstance(raw, dict):
        print(
            f"[warn] {path} のトップレベルが dict でありません。既定値を使います。",
            file=sys.stderr,
        )
        return ReloginGuard()
    data = cast("dict[str, Any]", raw)
    fc_raw = data.get("failure_count", 0)
    failure_count = fc_raw if isinstance(fc_raw, int) and not isinstance(fc_raw, bool) else 0
    du_raw = data.get("disabled_until")
    disabled_until = du_raw if isinstance(du_raw, str) else None
    return ReloginGuard(failure_count=failure_count, disabled_until=disabled_until)


def save_relogin_guard(profile_dir: Path, guard: ReloginGuard) -> None:
    """profile_dir/relogin_guard.json に atomic に書き出す。"""
    profile_dir.mkdir(parents=True, exist_ok=True)
    path = _relogin_guard_file(profile_dir)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".relogin_guard-", suffix=".tmp", dir=str(profile_dir)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(guard), f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        Path(tmp_path).replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink()
        raise
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_relogin_guard.py -v`
Expected: 5 テスト全て PASS

- [ ] **Step 5: コミット**

```bash
git add canvasser.py tests/test_relogin_guard.py
git commit -m "feat: relogin_guard.json 用の ReloginGuard dataclass と CRUD を追加"
```

---

### Task 3: `_relogin_*` 系ヘルパーを追加し `_credentials_*` から移行

**Files:**
- Modify: `canvasser.py` (`_relogin_disabled`, `_reset_relogin_failure`, `_record_relogin_failure` を追加、既存 `_credentials_*` は残す)
- Modify: `tests/test_relogin_guard.py` (Task 2 の追加テストとして書く)

**Interfaces:**
- Consumes: `ReloginGuard`, `load_relogin_guard`, `save_relogin_guard` (Task 2)
- Produces:
  - `_relogin_disabled(guard: ReloginGuard, name: str) -> bool` — `guard.disabled_until` が未来なら True、stderr に残時間を表示
  - `_reset_relogin_failure(profile_dir: Path, guard: ReloginGuard) -> None` — failure_count/disabled_until を 0/None にリセット。既に 0/None なら書き込まない
  - `_record_relogin_failure(profile_dir: Path, guard: ReloginGuard, *, submissions: int) -> None` — failure_count に submissions を加算、MAX 到達時に disabled_until を設定

- [ ] **Step 1: テストを書く**

`tests/test_relogin_guard.py` に以下を追加:

```python
from datetime import datetime, timedelta

from canvasser import (
    CREDENTIALS_DISABLE_WINDOW_SEC,
    CREDENTIALS_MAX_FAILURES,
    JST,
    _record_relogin_failure,
    _relogin_disabled,
    _reset_relogin_failure,
)


def test_relogin_disabled_none(capsys: pytest.CaptureFixture[str]) -> None:
    guard = ReloginGuard(disabled_until=None)
    assert _relogin_disabled(guard, "test") is False


def test_relogin_disabled_past() -> None:
    past = (datetime.now(JST) - timedelta(hours=1)).isoformat()
    guard = ReloginGuard(disabled_until=past)
    assert _relogin_disabled(guard, "test") is False


def test_relogin_disabled_future(capsys: pytest.CaptureFixture[str]) -> None:
    future = (datetime.now(JST) + timedelta(hours=1)).isoformat()
    guard = ReloginGuard(disabled_until=future)
    assert _relogin_disabled(guard, "test") is True
    assert "test" in capsys.readouterr().err


def test_relogin_disabled_invalid_string() -> None:
    """パース不能な disabled_until は False にフォールバック (fail-safe)。"""
    guard = ReloginGuard(disabled_until="not-a-date")
    assert _relogin_disabled(guard, "test") is False


def test_reset_relogin_failure_writes(tmp_path: Path) -> None:
    save_relogin_guard(tmp_path, ReloginGuard(failure_count=2))
    guard = load_relogin_guard(tmp_path)
    _reset_relogin_failure(tmp_path, guard)
    assert load_relogin_guard(tmp_path) == ReloginGuard()


def test_reset_relogin_failure_noop_when_clean(tmp_path: Path) -> None:
    """既に 0/None なら書き込みしない (I/O 節約)。"""
    guard = ReloginGuard()
    _reset_relogin_failure(tmp_path, guard)
    assert not (tmp_path / "relogin_guard.json").exists()


def test_record_relogin_failure_increments(tmp_path: Path) -> None:
    guard = ReloginGuard(failure_count=0)
    _record_relogin_failure(tmp_path, guard, submissions=1)
    assert load_relogin_guard(tmp_path).failure_count == 1


def test_record_relogin_failure_reaches_max_sets_disabled_until(tmp_path: Path) -> None:
    guard = ReloginGuard(failure_count=CREDENTIALS_MAX_FAILURES - 1)
    _record_relogin_failure(tmp_path, guard, submissions=1)
    result = load_relogin_guard(tmp_path)
    assert result.failure_count == CREDENTIALS_MAX_FAILURES
    assert result.disabled_until is not None
    # disabled_until は 6 時間後 ± 若干のずれ
    deadline = datetime.fromisoformat(result.disabled_until)
    expected = datetime.now(JST) + timedelta(seconds=CREDENTIALS_DISABLE_WINDOW_SEC)
    assert abs((deadline - expected).total_seconds()) < 10


def test_record_relogin_failure_two_submissions(tmp_path: Path) -> None:
    """submissions=2 で 1 → 3 (MAX 到達)。"""
    guard = ReloginGuard(failure_count=1)
    _record_relogin_failure(tmp_path, guard, submissions=2)
    assert load_relogin_guard(tmp_path).failure_count == 3
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_relogin_guard.py -v`
Expected: ImportError で FAIL

- [ ] **Step 3: ヘルパー関数を実装**

既存の `_credentials_disabled` (L2165), `_reset_credentials_failure` (L2188), `_record_credentials_failure` (L2207) をロジックとして参考にし、ReloginGuard を受け取る形にリライトする。既存 `_credentials_*` は Task 4 で削除するが、本タスクでは並存させる (Task 4 の attempt_auto_relogin リファクタで一気に切り替える):

```python
def _relogin_disabled(guard: ReloginGuard, name: str) -> bool:
    """`guard.disabled_until` が未来なら True (自動再ログインをスキップすべき)。

    パース不能や過去時刻は False (=有効) として扱い、fail-safe に倒す。
    未来なら残時間を stderr に案内する。
    """
    if guard.disabled_until is None:
        return False
    try:
        deadline = datetime.fromisoformat(guard.disabled_until)
    except ValueError:
        return False
    deadline = _as_jst_aware(deadline)
    if datetime.now(JST) >= deadline:
        return False
    print(
        f"[{name}] 自動再ログインは {guard.disabled_until} まで"
        "一時停止中です (連続失敗ガード)。",
        file=sys.stderr,
    )
    return True


def _reset_relogin_failure(profile_dir: Path, guard: ReloginGuard) -> None:
    """成功時の failure_count / disabled_until クリア (無変化なら書き込まない)。"""
    if guard.failure_count == 0 and guard.disabled_until is None:
        return
    save_relogin_guard(profile_dir, ReloginGuard())


def _record_relogin_failure(
    profile_dir: Path, guard: ReloginGuard, *, submissions: int
) -> None:
    """失敗時に failure_count へ submissions を加算し、必要なら disabled_until を設定。"""
    new_count = guard.failure_count + submissions
    new_disabled = guard.disabled_until
    if new_count >= CREDENTIALS_MAX_FAILURES:
        window = timedelta(seconds=CREDENTIALS_DISABLE_WINDOW_SEC)
        new_disabled = (datetime.now(JST) + window).isoformat()
    save_relogin_guard(
        profile_dir,
        ReloginGuard(failure_count=new_count, disabled_until=new_disabled),
    )
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_relogin_guard.py -v`
Expected: 全テスト PASS

- [ ] **Step 5: コミット**

```bash
git add canvasser.py tests/test_relogin_guard.py
git commit -m "feat: ReloginGuard を使う _relogin_* ヘルパーを追加"
```

---

### Task 4: `_run_guarded_auto_login` ヘルパーを追加し `attempt_auto_relogin` を移行

**Files:**
- Modify: `canvasser.py` (`_run_guarded_auto_login` 追加、`_run_auto_login_sequence` / `_retry_after_timeout` の signature 変更、`attempt_auto_relogin` リファクタ)
- Modify: `tests/test_login_flow.py` (ReloginGuard を使う形にテスト書き換え)

**Interfaces:**
- Consumes: `Credentials` (既存だが本タスクでは中身は変更しない、フィールドは既存のまま)、`ReloginGuard`, `load_relogin_guard`, `save_relogin_guard`, `_relogin_disabled`, `_reset_relogin_failure`, `_record_relogin_failure` (Task 2/3)、`_run_auto_login_sequence` (既存)
- Produces:
  - `_run_guarded_auto_login(page: Page, profile_dir: Path, name: str) -> bool` — credentials + guard を読み、guarded に auto_login し True/False を返す
  - `_run_auto_login_sequence(page: Page, name: str, credentials: Credentials, guard: ReloginGuard) -> tuple[AutoLoginOutcome, int]` — signature 変更: 新引数 `guard`
  - `_retry_after_timeout(page: Page, name: str, credentials: Credentials, guard: ReloginGuard) -> tuple[AutoLoginOutcome, int]` — signature 変更: 新引数 `guard` (retry budget を `guard.failure_count` で判定)
  - `attempt_auto_relogin(page: Page, profile_dir: Path, name: str) -> bool` — 内部で `_run_guarded_auto_login` を呼ぶよう変更

- [ ] **Step 0: `_run_guarded_auto_login` の直接テストを書く**

`tests/test_login_flow.py` に以下を追加 (`_run_guarded_auto_login` の共有ヘルパー契約を直接検証する。attempt_auto_relogin 経由だけだと ASOBI driver からの呼び出しがカバーされない):

```python
def test_run_guarded_auto_login_no_credentials_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canvasser, "load_credentials", lambda pdir: None)
    fake = FakePage(responses=[])
    assert canvasser._run_guarded_auto_login(as_page(fake), tmp_path, "test") is False


def test_run_guarded_auto_login_disabled_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        canvasser, "load_credentials",
        lambda pdir: canvasser.Credentials(
            bnid_email="a@b", bnid_password="p", saved_at="2026-07-08T00:00:00+09:00"
        ),
    )
    from datetime import datetime, timedelta
    future = (datetime.now(canvasser.JST) + timedelta(hours=1)).isoformat()
    canvasser.save_relogin_guard(
        tmp_path,
        canvasser.ReloginGuard(failure_count=3, disabled_until=future),
    )
    fake = FakePage(responses=[])
    assert canvasser._run_guarded_auto_login(as_page(fake), tmp_path, "test") is False


def test_run_guarded_auto_login_success_resets_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        canvasser, "load_credentials",
        lambda pdir: canvasser.Credentials(
            bnid_email="a@b", bnid_password="p", saved_at="2026-07-08T00:00:00+09:00"
        ),
    )
    canvasser.save_relogin_guard(tmp_path, canvasser.ReloginGuard(failure_count=2))
    monkeypatch.setattr(
        canvasser, "_run_auto_login_sequence",
        lambda page, name, creds, guard: (canvasser.AutoLoginOutcome.SUCCESS, 1),
    )
    fake = FakePage(responses=[])
    assert canvasser._run_guarded_auto_login(as_page(fake), tmp_path, "test") is True
    assert canvasser.load_relogin_guard(tmp_path).failure_count == 0


def test_run_guarded_auto_login_failure_records_submissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        canvasser, "load_credentials",
        lambda pdir: canvasser.Credentials(
            bnid_email="a@b", bnid_password="p", saved_at="2026-07-08T00:00:00+09:00"
        ),
    )
    monkeypatch.setattr(
        canvasser, "_run_auto_login_sequence",
        lambda page, name, creds, guard: (canvasser.AutoLoginOutcome.PASSWORD_ERROR, 1),
    )
    fake = FakePage(responses=[])
    assert canvasser._run_guarded_auto_login(as_page(fake), tmp_path, "test") is False
    assert canvasser.load_relogin_guard(tmp_path).failure_count == 1


def test_run_guarded_auto_login_no_submission_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORM_ERROR (submissions=0) の場合、failure_count を加算しない (BNID にパスワード送っていない)。"""
    monkeypatch.setattr(
        canvasser, "load_credentials",
        lambda pdir: canvasser.Credentials(
            bnid_email="a@b", bnid_password="p", saved_at="2026-07-08T00:00:00+09:00"
        ),
    )
    monkeypatch.setattr(
        canvasser, "_run_auto_login_sequence",
        lambda page, name, creds, guard: (canvasser.AutoLoginOutcome.FORM_ERROR, 0),
    )
    fake = FakePage(responses=[])
    assert canvasser._run_guarded_auto_login(as_page(fake), tmp_path, "test") is False
    assert not (tmp_path / "relogin_guard.json").exists()  # 書き込みしない
```

これらはこの時点ではまだ `_run_guarded_auto_login` が未実装なので FAIL する。Step 1-3 で実装 → PASS。

- [ ] **Step 1: `_run_auto_login_sequence` / `_retry_after_timeout` の signature を変更**

`canvasser.py:2236-2325` を書き換え、`credentials: Credentials` の隣に `guard: ReloginGuard` を追加する。`_retry_after_timeout` 内の retry budget チェックを `credentials.failure_count` から `guard.failure_count` に変える (L2251):

```python
def _retry_after_timeout(
    page: Page, name: str, credentials: Credentials, guard: ReloginGuard
) -> tuple[AutoLoginOutcome, int]:
    ...
    if guard.failure_count + 1 >= CREDENTIALS_MAX_FAILURES:
        ...


def _resolve_retry_outcome(
    page: Page, name: str, credentials: Credentials, guard: ReloginGuard
) -> tuple[AutoLoginOutcome, int]:
    ...


def _run_auto_login_sequence(
    page: Page, name: str, credentials: Credentials, guard: ReloginGuard
) -> tuple[AutoLoginOutcome, int]:
    outcome, submitted = auto_login(page, credentials)
    if outcome is not AutoLoginOutcome.TIMEOUT:
        return outcome, submitted
    retry_outcome, retry_submitted = _retry_after_timeout(page, name, credentials, guard)
    return retry_outcome, submitted + retry_submitted
```

`_resolve_retry_outcome` の内部 `_retry_after_timeout` の呼び出しも `guard` を渡すよう追随。

- [ ] **Step 2: `_run_guarded_auto_login` を実装**

`canvasser.py` の `attempt_auto_relogin` の直前あたりに追加:

```python
def _run_guarded_auto_login(page: Page, profile_dir: Path, name: str) -> bool:
    """credentials + guard を読み、guarded に auto_login して成功なら True。

    - credentials 復号失敗 → False
    - guard.disabled_until が未来 → False (silently skip)
    - `_run_auto_login_sequence` を呼び、SUCCESS → guard を reset、失敗 → guard に失敗記録
    - `attempt_auto_relogin` と ASOBI recovery driver の両方から呼ぶ共有ヘルパー
    """
    credentials = load_credentials(profile_dir)
    if credentials is None:
        return False
    guard = load_relogin_guard(profile_dir)
    if _relogin_disabled(guard, name):
        return False
    outcome, submissions = _run_auto_login_sequence(page, name, credentials, guard)
    if outcome is AutoLoginOutcome.SUCCESS:
        _reset_relogin_failure(profile_dir, guard)
        return True
    if submissions > 0:
        _record_relogin_failure(profile_dir, guard, submissions=submissions)
    return False
```

- [ ] **Step 3: `attempt_auto_relogin` を `_run_guarded_auto_login` 経由にリファクタ**

`canvasser.py:2328-2383` を書き換える。goto 失敗ハンドリングと check_login false-negative 救済 (session_valid 短絡) は残し、auto_login sequence の呼び出しを `_run_guarded_auto_login` に委譲する。session_valid 短絡時の guard reset は `guard.failure_count > 0` の場合のみ書き込む:

```python
def attempt_auto_relogin(page: Page, profile_dir: Path, name: str) -> bool:
    """process_account の未ログインルートで呼ぶ自動再ログインゲート。

    - credentials 復号非成功・disabled_until 有効 → False (`_run_guarded_auto_login` 内で判定)
    - LOGIN_ENTRY_URL 遷移失敗 → False、failure_count には計上しない (BNID にパスワードを送っていないため)
    - goto 直後の check_login 短絡救済 (session が実は有効) は残す
    - それ以外の失敗記録・成功リセットは `_run_guarded_auto_login` に集約
    """
    credentials = load_credentials(profile_dir)
    if credentials is None:
        return False
    guard = load_relogin_guard(profile_dir)
    if _relogin_disabled(guard, name):
        return False

    try:
        page.goto(LOGIN_ENTRY_URL, wait_until="domcontentloaded")
    except PlaywrightError as e:
        print(
            f"[{name}] BNID ログイン画面への遷移で失敗: {e}",
            file=sys.stderr,
        )
        return False

    # 初回 check_login が false negative だった場合の救済 (既存挙動を維持)
    session_valid = False
    with contextlib.suppress(PlaywrightError):
        session_valid = check_login(page)
    if session_valid:
        print(
            f"[{name}] BNID ログイン画面遷移後にセッション有効を確認しました。",
            file=sys.stderr,
        )
        _reset_relogin_failure(profile_dir, guard)
        return True

    return _run_guarded_auto_login(page, profile_dir, name)
```

**注意**: 上記実装では credentials/guard を先読みして gate、そのあと `_run_guarded_auto_login` が再度読み込む形になる (キャッシュしない、KISS)。呼び出しは軽量なので許容する。

- [ ] **Step 4: `tests/test_login_flow.py` の `Credentials(...)` 構築と `_credentials_*` 参照を書き換え**

既存テストで `Credentials(bnid_email=..., bnid_password=..., saved_at=..., failure_count=..., disabled_until=...)` としている箇所の failure_count / disabled_until 部分を `ReloginGuard(failure_count=..., disabled_until=...)` の save として書き換える。`Credentials(...)` は saved_at のみ残った状態にする (Task 6 でさらに簡略化する。本タスクでは saved_at 残置で可)。

パターン例:

Before:
```python
save_credentials(profile_dir, Credentials(
    bnid_email="a@b", bnid_password="p", saved_at="2026-07-08T00:00:00+09:00",
    failure_count=2, disabled_until=None
))
```

After (本タスクの中間状態):
```python
save_credentials(profile_dir, Credentials(
    bnid_email="a@b", bnid_password="p", saved_at="2026-07-08T00:00:00+09:00"
))
save_relogin_guard(profile_dir, ReloginGuard(failure_count=2, disabled_until=None))
```

**ただし**: `credentials.json` 永続化そのものが Task 6 で撤去されるため、本タスクでのテスト書き換えは「まず ReloginGuard の分離を反映する」に留め、`save_credentials` の呼び出しは残す。Task 6 で `load_credentials` の実装が Chrome 経由に変わったタイミングで、これらのテストは `monkeypatch` で `load_credentials` を返り値注入する形に置き換える。

- [ ] **Step 5: 全テストを実行**

Run: `uv run pytest tests/test_login_flow.py tests/test_relogin_guard.py -v`
Expected: 全 PASS

- [ ] **Step 6: コミット**

```bash
git add canvasser.py tests/test_login_flow.py
git commit -m "feat: _run_guarded_auto_login ヘルパーを追加し attempt_auto_relogin を移行"
```

---

### Task 5: Chrome Login Data 復号関数群を追加

**Files:**
- Modify: `canvasser.py` (`_dpapi_unprotect`, `_load_chrome_master_key`, `_decrypt_v10_password`, `_read_bnid_login_row` を追加、`import sqlite3` `import ctypes` を追加。`from urllib.parse import quote` は Task 8 で使うため本タスクでは追加しない)
- Create: `tests/test_chrome_credentials.py`

**Interfaces:**
- Produces:
  - `_dpapi_unprotect(blob: bytes) -> bytes | None` — `CryptUnprotectData` の薄いラッパー、失敗は None
  - `_load_chrome_master_key(profile_dir: Path) -> bytes | None` — Local State から AES-256 マスタキーを取得
  - `_decrypt_v10_password(master_key: bytes, blob: bytes) -> str | None` — v10 blob → GCM 復号 → UTF-8
  - `_read_bnid_login_row(profile_dir: Path) -> tuple[str, bytes] | None` — Login Data SQLite から (username, password_blob) を返す

- [ ] **Step 1: `_decrypt_v10_password` のテストを書く**

`tests/test_chrome_credentials.py` を新規作成:

```python
"""Chrome Login Data (v10 DPAPI + AES-256-GCM) 復号のテスト。

DPAPI 部分はテスト決定性のため monkeypatch でスタブする。GCM 部分は
実 pycryptodome で往復させる。SQLite fixture は sqlite3 で組む。
"""

import base64
import json
import sqlite3
from pathlib import Path

import pytest
from Crypto.Cipher import AES

from canvasser import (
    _decrypt_v10_password,
    _load_chrome_master_key,
    _read_bnid_login_row,
    load_credentials,
)


def _encrypt_v10(master_key: bytes, plaintext: str) -> bytes:
    """テスト用: v10 プレフィックス + GCM 暗号化 blob を作る。"""
    nonce = b"\x00" * 12
    cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))
    return b"v10" + nonce + ct + tag


def test_decrypt_v10_password_roundtrip() -> None:
    """正しい master_key + v10 blob → 平文取得。"""
    key = b"k" * 32
    plaintext = "correct_password"
    blob = _encrypt_v10(key, plaintext)
    assert _decrypt_v10_password(key, blob) == plaintext


def test_decrypt_v10_password_wrong_key_returns_none() -> None:
    """32 bytes だが誤 master_key → GCM MAC 検証失敗で None。"""
    right = b"k" * 32
    wrong = b"x" * 32
    blob = _encrypt_v10(right, "secret")
    assert _decrypt_v10_password(wrong, blob) is None


def test_decrypt_v10_password_wrong_length_key_returns_none() -> None:
    """誤長 master_key → AES.new が ValueError → None。"""
    key_valid = b"k" * 32
    blob = _encrypt_v10(key_valid, "secret")
    assert _decrypt_v10_password(b"short", blob) is None


def test_decrypt_v10_password_v20_prefix_returns_none() -> None:
    """v20 (App-Bound) プレフィックスは復号せず None。"""
    key = b"k" * 32
    blob = b"v20" + b"\x00" * 60
    assert _decrypt_v10_password(key, blob) is None


def test_decrypt_v10_password_unknown_prefix_returns_none() -> None:
    key = b"k" * 32
    blob = b"abc" + b"\x00" * 60
    assert _decrypt_v10_password(key, blob) is None


def test_decrypt_v10_password_non_utf8_returns_none() -> None:
    """GCM 復号結果が UTF-8 でない → None。"""
    key = b"k" * 32
    nonce = b"\x00" * 12
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(b"\xff\xfe\xfd")  # 無効 UTF-8
    blob = b"v10" + nonce + ct + tag
    assert _decrypt_v10_password(key, blob) is None


def test_load_chrome_master_key_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local State → DPAPI 復号 (monkeypatch) → 32 bytes キー。"""
    fake_key = b"k" * 32
    monkeypatch.setattr("canvasser._dpapi_unprotect", lambda blob: fake_key)
    local_state = tmp_path / "Local State"
    local_state.write_text(
        json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + b"garbage").decode()}}),
        encoding="utf-8",
    )
    assert _load_chrome_master_key(tmp_path) == fake_key


def test_load_chrome_master_key_missing_local_state(tmp_path: Path) -> None:
    assert _load_chrome_master_key(tmp_path) is None


def test_load_chrome_master_key_dpapi_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("canvasser._dpapi_unprotect", lambda blob: None)
    local_state = tmp_path / "Local State"
    local_state.write_text(
        json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + b"garbage").decode()}}),
        encoding="utf-8",
    )
    assert _load_chrome_master_key(tmp_path) is None


def _make_login_data_db(path: Path, rows: list[tuple[str, str, bytes]]) -> None:
    """テスト用 Login Data DB を作る。rows: [(origin_url, username, password_blob)]"""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE logins ("
        " origin_url TEXT, username_value TEXT, password_value BLOB,"
        " date_last_used INTEGER, blacklisted_by_user INTEGER,"
        " signon_realm TEXT)"
    )
    for i, (url, uname, pw) in enumerate(rows):
        con.execute(
            "INSERT INTO logins VALUES (?, ?, ?, ?, ?, ?)",
            (url, uname, pw, 1000 + i, 0, url),
        )
    con.commit()
    con.close()


def test_read_bnid_login_row_returns_latest(tmp_path: Path) -> None:
    default = tmp_path / "Default"
    default.mkdir()
    _make_login_data_db(
        default / "Login Data",
        [
            ("https://account.bandainamcoid.com/login.html", "old@example.com", b"v10OLD"),
            ("https://account.bandainamcoid.com/login.html", "new@example.com", b"v10NEW"),
        ],
    )
    result = _read_bnid_login_row(tmp_path)
    assert result is not None
    assert result[0] == "new@example.com"
    assert result[1] == b"v10NEW"


def test_read_bnid_login_row_no_bnid_row_returns_none(tmp_path: Path) -> None:
    default = tmp_path / "Default"
    default.mkdir()
    _make_login_data_db(
        default / "Login Data",
        [("https://example.com/login", "someone@example.com", b"v10XX")],
    )
    assert _read_bnid_login_row(tmp_path) is None


def test_read_bnid_login_row_no_login_data_returns_none(tmp_path: Path) -> None:
    assert _read_bnid_login_row(tmp_path) is None


def test_read_bnid_login_row_empty_username_returns_none(tmp_path: Path) -> None:
    """LATEST 行の username が空文字なら None (fallback しない)。"""
    default = tmp_path / "Default"
    default.mkdir()
    _make_login_data_db(
        default / "Login Data",
        [("https://account.bandainamcoid.com/login.html", "", b"v10XX")],
    )
    assert _read_bnid_login_row(tmp_path) is None
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_chrome_credentials.py -v`
Expected: `ImportError` などで FAIL

- [ ] **Step 3: `canvasser.py` に復号関数群を実装**

必要な import を先頭 (L31 付近) に追加する:

```python
import ctypes
import sqlite3
```

既存の credentials セクション (L1697 付近) に以下を追加する:

```python
def _dpapi_unprotect(blob: bytes) -> bytes | None:
    """Windows CryptUnprotectData で blob を復号する。失敗は None。

    非 Windows は使用不可 (呼び出し側で os.name をチェックすること)。
    """
    if os.name != "nt":
        return None

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]

    in_blob = _DATA_BLOB(len(blob), ctypes.cast(ctypes.c_char_p(blob), ctypes.c_void_p))
    out_blob = _DATA_BLOB()
    try:
        ok = ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[attr-defined]
            ctypes.byref(in_blob),
            None, None, None, None, 0,
            ctypes.byref(out_blob),
        )
    except OSError as e:
        print(f"[warn] DPAPI 呼び出し失敗: {e}", file=sys.stderr)
        return None
    if not ok:
        print("[warn] DPAPI 復号失敗", file=sys.stderr)
        return None
    try:
        buf = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)  # type: ignore[attr-defined]
    return buf


def _load_chrome_master_key(profile_dir: Path) -> bytes | None:
    """Local State から DPAPI 経由で AES-256 マスタキーを取得する。

    失敗 (ファイル非存在 / JSON パース失敗 / DPAPI 失敗 / 誤フォーマット) は
    stderr に警告を出しつつ None を返す。
    """
    local_state = profile_dir / "Local State"
    if not local_state.exists():
        return None
    try:
        raw = json.loads(local_state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[warn] {local_state} を読めません: {e}", file=sys.stderr)
        return None
    os_crypt = raw.get("os_crypt") if isinstance(raw, dict) else None
    encrypted_b64 = os_crypt.get("encrypted_key") if isinstance(os_crypt, dict) else None
    if not isinstance(encrypted_b64, str):
        print(f"[warn] {local_state} に os_crypt.encrypted_key が無い", file=sys.stderr)
        return None
    try:
        encrypted = base64.b64decode(encrypted_b64)
    except (ValueError, TypeError) as e:
        print(f"[warn] encrypted_key の base64 デコード失敗: {e}", file=sys.stderr)
        return None
    if not encrypted.startswith(b"DPAPI"):
        print("[warn] encrypted_key の DPAPI プレフィックスが無い", file=sys.stderr)
        return None
    return _dpapi_unprotect(encrypted[5:])


def _decrypt_v10_password(master_key: bytes, blob: bytes) -> str | None:
    """v10 プレフィックスを剥がして GCM 復号 → UTF-8。不一致・失敗は None。"""
    if not blob.startswith(b"v10"):
        return None
    nonce = blob[3:15]
    ct_and_tag = blob[15:]
    if len(ct_and_tag) < 16:
        return None
    ct = ct_and_tag[:-16]
    tag = ct_and_tag[-16:]
    try:
        cipher = AES.new(master_key, AES.MODE_GCM, nonce=nonce)
        plain = cipher.decrypt_and_verify(ct, tag)
    except (ValueError, KeyError):
        return None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _read_bnid_login_row(profile_dir: Path) -> tuple[str, bytes] | None:
    """Login Data (SQLite) を mode=ro + timeout=5.0 で開き、bandainamcoid の最新 1 行を返す。

    LATEST 行 (date_last_used DESC の先頭) の username_value が空文字、
    または password_value が bytes 以外なら None を返す (古い有効行への fallback はしない)。
    Chrome は idle 時に Login Data のロックを保持しないため通常は即読める。
    SQLITE_BUSY は timeout 内に解消しない場合のみ発生し、その時も None に倒す。
    """
    login_data = profile_dir / "Default" / "Login Data"
    if not login_data.exists():
        return None
    try:
        con = sqlite3.connect(
            f"{login_data.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.OperationalError as e:
        print(f"[warn] {login_data} を開けません: {e}", file=sys.stderr)
        return None
    try:
        row = con.execute(
            "SELECT username_value, password_value FROM logins"
            " WHERE origin_url LIKE 'https://%.bandainamcoid.com/%'"
            " ORDER BY date_last_used DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as e:
        print(f"[warn] {login_data} の SELECT で失敗: {e}", file=sys.stderr)
        return None
    finally:
        con.close()
    if row is None:
        return None
    username, password_blob = row
    if not isinstance(username, str) or not username:
        return None
    if not isinstance(password_blob, bytes):
        return None
    return username, password_blob
```

- [ ] **Step 4: テストが通ることを確認**

Run: `uv run pytest tests/test_chrome_credentials.py -v`
Expected: 全 PASS

- [ ] **Step 5: コミット**

```bash
git add canvasser.py tests/test_chrome_credentials.py
git commit -m "feat: Chrome Login Data (v10 DPAPI+AES-GCM) 復号関数を追加"
```

---

### Task 6: `Credentials` 縮小・`load_credentials` 中身差替・`credentials.json` / `login-init` を撤去

**Files:**
- Modify: `canvasser.py` (大量削除 + `load_credentials` 差替 + `Credentials` 縮小)
- Modify: `tests/test_login_flow.py` (monkeypatch で load_credentials を注入する形に置換)
- Delete: `tests/test_credentials.py` (272 行)
- Modify: `tests/test_chrome_credentials.py` (load_credentials の end-to-end テスト追加)

**Interfaces:**
- Consumes: `_load_chrome_master_key`, `_decrypt_v10_password`, `_read_bnid_login_row` (Task 5)、`_run_guarded_auto_login` (Task 4)
- Produces:
  - `class Credentials` — `@dataclass(kw_only=True)`, fields: `bnid_email: str`, `bnid_password: str` (saved_at/failure_count/disabled_until を削除)
  - `load_credentials(profile_dir: Path) -> Credentials | None` — Chrome Login Data から復号して Credentials を組み立てる (公開 I/F は不変、実装だけ差替)
  - CLI から `login-init` サブコマンドが削除される
  - `RunOptions.login_init_mode` が削除される

- [ ] **Step 1: 削除対象の全リストを確認**

以下を削除する (Task 4 で `attempt_auto_relogin` が既に `_run_guarded_auto_login` 経由になっているので、これらは caller が居ない):

- `canvasser.py`:
  - `_CREDENTIALS_FILENAME` (L1474), `_CREDENTIALS_PENDING_FILENAME` (L1478)
  - `_credentials_file` (L1697), `_pending_credentials_file` (L1702)
  - `_apply_credentials_permissions` (L1707-1735)
  - `_load_credentials_from` (L1738-1790) ← 中身を Chrome 版に差し替え、名前は `load_credentials` に統合
  - `_save_credentials_to` (L1793-1818)
  - `save_credentials` (L1826-1828)
  - `load_pending_credentials` (L1831-1833)
  - `save_pending_credentials` (L1836-1842)
  - `_activate_pending_credentials` (L1845-1854)
  - `_discard_pending_credentials` (L1857-1860)
  - `_credentials_disabled` (L2165-2185) — Task 3 で `_relogin_disabled` に置換済み
  - `_reset_credentials_failure` (L2188-2204)
  - `_record_credentials_failure` (L2207-2233)
  - `_prompt_credentials` (L1968-1983)
  - `persist_login_init_credentials` (L1986-2009)
  - `run_login_init_flow` (L2453-2531)
  - `RunOptions.login_init_mode` field (L2679)
  - `_main_impl` の `login_init_mode` 分岐 (L3007, L3022-3027, L3049-3051)
  - `process_account` の `login_init_mode` 分岐 (L2704, L2713-2716)
  - argparse の `login-init` subparser (L2833-2845)
  - `_build_run_options` の `login-init` 分岐 (L2932-2933)
  - `import getpass` (L35) — grep で他に使用箇所無しを確認してから削除

- `tests/test_credentials.py` (全 272 行)

- [ ] **Step 2: `Credentials` を縮小**

`canvasser.py:1675-1694` を書き換え:

```python
@dataclass(kw_only=True)
class Credentials:
    """BNID の自動再ログインで使う資格情報 (メモリ内のみ)。

    Chrome Login Data から復号した平文を保持する。ファイル永続化はしない。
    """

    bnid_email: str
    bnid_password: str
```

- [ ] **Step 3: `load_credentials` を Chrome 版に差替**

`canvasser.py:1821-1828` (旧 `load_credentials` / `save_credentials`) を以下に置き換える:

```python
def load_credentials(profile_dir: Path) -> Credentials | None:
    """Chrome Login Data から BNID 資格情報を復号して Credentials を返す。

    - Windows 以外 → None + stderr 警告
    - Local State / Login Data 非存在、DPAPI/AES/UTF-8 失敗、非 v10、bandainamcoid
      レコード非存在、SQLITE_BUSY → 全て None (stderr に理由を出す)
    - 認証情報値 (email/password) はログに出さない
    """
    if os.name != "nt":
        print(
            "[warn] load_credentials は Windows でのみ動作します。",
            file=sys.stderr,
        )
        return None
    master_key = _load_chrome_master_key(profile_dir)
    if master_key is None:
        return None
    row = _read_bnid_login_row(profile_dir)
    if row is None:
        return None
    username, password_blob = row
    password = _decrypt_v10_password(master_key, password_blob)
    if password is None:
        return None
    return Credentials(bnid_email=username, bnid_password=password)
```

- [ ] **Step 4: 上記「削除対象」を全て削除**

Step 1 の全リストを canvasser.py から削除する。`import getpass` は先に `rg -n "getpass" canvasser.py` で `_prompt_credentials` 以外の使用が無いことを確認してから削除。

`_main_impl` (L2997-3056) は login-init 分岐を消して以下のように整理:

```python
def _main_impl() -> int:
    args = _build_parser().parse_args()
    profiles_dir = Path(args.profiles_dir).resolve()

    if args.command == "mark-completed":
        return _run_mark_completed(args, profiles_dir)

    login_mode = args.command == "login"
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
    options = _build_run_options(args)
    ensure_chromium_installed()

    exit_code = 0
    results: list[tuple[str, int]] = []
    with sync_playwright() as p:
        for name, profile_dir in profiles:
            print(f"\n=== アカウント: {name} ({profile_dir}) ===")
            try:
                gained, code = process_account(p, name, profile_dir, options)
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] 実行中に例外: {e}", file=sys.stderr)
                exit_code = 1
                results.append((name, 0))
                continue
            results.append((name, gained))
            if code != 0:
                exit_code = code
            if login_mode:
                # login は 1 アカウント (--account 必須) のみ処理して抜ける
                return code

    if len(profiles) > 1:
        _print_summary(results)

    return exit_code
```

`process_account` (L2691-2751) も login_init_mode の分岐を消す:

```python
def process_account(
    p: Playwright,
    name: str,
    profile_dir: Path,
    options: RunOptions,
) -> tuple[int, int]:
    profile_dir.mkdir(parents=True, exist_ok=True)
    ctx = open_persistent_context(p, profile_dir, headless=not options.login_mode)
    try:
        page = ctx.new_page()
        page.goto(MISSION_PAGE_URL, wait_until="domcontentloaded")

        if options.login_mode:
            return 0, run_login_flow(page)

        if not _ensure_authenticated(page, name, profile_dir, options):
            return 0, 1

        # ... 以下 mission/checkin ロジック
```

`RunOptions` からも `login_init_mode` を削除:

```python
@dataclass(kw_only=True)
class RunOptions:
    login_mode: bool = False
    run_mission: bool = False
    run_checkin: bool = False
    dry_run: bool = False
    daily_budget: int = 0
    consecutive_failure_limit: int = 1
    out_of_range_limit: int = 3
    auto_relogin: bool = True
```

argparse の `login-init` subparser (L2833-2845) を削除、`_build_run_options` (L2924-2947) の login-init 分岐 (L2932-2933) を削除。

- [ ] **Step 5: `tests/test_credentials.py` を削除**

```bash
git rm tests/test_credentials.py
```

- [ ] **Step 6: `tests/test_login_flow.py` を monkeypatch 形式に書き換え**

既存テストが `save_credentials(profile_dir, Credentials(...))` で seed していた箇所を、以下のように書き換える:

```python
def test_attempt_auto_relogin_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        canvasser,
        "load_credentials",
        lambda pdir: canvasser.Credentials(bnid_email="a@b", bnid_password="p"),
    )
    # ...
```

`_prompt_credentials`、`persist_login_init_credentials`、`run_login_init_flow` を触るテストは全て削除する (`login-init` 撤去済みのため)。

- [ ] **Step 7: `tests/test_chrome_credentials.py` に end-to-end テストを追加**

```python
def test_load_credentials_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local State + Login Data + DPAPI モックで Credentials を組み立てる。"""
    fake_key = b"k" * 32
    monkeypatch.setattr("canvasser._dpapi_unprotect", lambda blob: fake_key)
    local_state = tmp_path / "Local State"
    local_state.write_text(
        json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + b"garbage").decode()}}),
        encoding="utf-8",
    )
    default = tmp_path / "Default"
    default.mkdir()
    _make_login_data_db(
        default / "Login Data",
        [(
            "https://account.bandainamcoid.com/login.html",
            "user@example.com",
            _encrypt_v10(fake_key, "correct_pw"),
        )],
    )
    creds = load_credentials(tmp_path)
    assert creds is not None
    assert creds.bnid_email == "user@example.com"
    assert creds.bnid_password == "correct_pw"


def test_load_credentials_no_login_data_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("canvasser._dpapi_unprotect", lambda blob: b"k" * 32)
    local_state = tmp_path / "Local State"
    local_state.write_text(
        json.dumps({"os_crypt": {"encrypted_key": base64.b64encode(b"DPAPI" + b"x").decode()}}),
        encoding="utf-8",
    )
    assert load_credentials(tmp_path) is None


def test_load_credentials_non_windows_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canvasser.os, "name", "posix")
    assert load_credentials(tmp_path) is None
```

- [ ] **Step 8: 全テストを実行**

Run: `uv run pytest tests/ -v`
Expected: 全 PASS (test_credentials.py 削除、test_chrome_credentials.py 追加、他は monkeypatch 差替で通る)

- [ ] **Step 9: `rg` で `getpass` / `credentials.json` / `login-init` の残存確認**

Run:
```bash
rg -n "\bgetpass\b|credentials\.json|login-init|login_init" canvasser.py tests/ README.md
```
Expected: `canvasser.py` と `tests/` からはヒット無し (README は Task 11 で扱うためこの時点では残存 OK)

- [ ] **Step 10: コミット**

```bash
git add canvasser.py tests/test_login_flow.py tests/test_chrome_credentials.py
# tests/test_credentials.py は Step 5 で git rm 済み、ここでは再削除しない
git commit -m "feat: Chrome Login Data 経由に切替え credentials.json/login-init を撤去"
```

---

## Phase 3: E1926 (ASOBI 連携) 自動復旧

### Task 7: `MissionOutcome` / `MissionRunResult` dataclass 追加と `_receive`/`_process_one_mission` 戻り値変更

**Files:**
- Modify: `canvasser.py:256-393` (`_receive`, `_process_one_mission`, `_complete`, `collect_missions`)
- Modify: `tests/test_missions.py` (戻り値変更に追随)

**Interfaces:**
- Consumes: 既存 `_complete` の "ok"/"already_done"/"condition_unmet"/"error" 分岐
- Produces:
  - `class MissionOutcome` — `@dataclass(kw_only=True)`, fields: `gained: int = 0`, `linkage_expired_id: int | None = None`
  - `class MissionRunResult` — `@dataclass(kw_only=True)`, fields: `gained: int = 0`, `linkage_expired_ids: set[int] = field(default_factory=set)`
  - `_receive(page, mid, name, pts, *, dry_run) -> MissionOutcome` — E1926 検知で `linkage_expired_id=mid`
  - `_process_one_mission(page, m, *, dry_run) -> MissionOutcome` — 内部で `_complete` の linkage_expired と `_receive` の linkage_expired_id を束ねて返す
  - `_complete(page, mid, name, *, dry_run) -> str` — 戻り値に `"linkage_expired"` を追加

- [ ] **Step 1: dataclass 追加のテスト書き**

`tests/test_missions.py` に以下を追加 (既存 `_receive` テストの隣):

```python
def test_receive_returns_gained_on_success() -> None:
    fake = FakePage(responses=[success_response({"received_point": 5})])
    result = canvasser._receive(as_page(fake), 1, "テスト", 5, dry_run=False)
    assert result == canvasser.MissionOutcome(gained=5)


def test_receive_returns_linkage_expired_on_e1926() -> None:
    fake = FakePage(responses=[error_response("E1926")])
    result = canvasser._receive(as_page(fake), 21, "ASOBI", 2, dry_run=False)
    assert result == canvasser.MissionOutcome(linkage_expired_id=21)


def test_complete_returns_linkage_expired_on_e1926() -> None:
    fake = FakePage(responses=[error_response("E1926")])
    assert canvasser._complete(as_page(fake), 21, "ASOBI", dry_run=False) == "linkage_expired"


def test_process_one_mission_propagates_linkage_expired_from_complete() -> None:
    fake = FakePage(responses=[error_response("E1926")])
    m = {
        "mission_id": 21, "mission_name": "ASOBI", "mission_point": 2,
        "action": {"mission_complete_api_call_flag": True},
        "is_mission_completed": False, "is_mission_received": False,
        "remaining_completable_count": 1,
    }
    result = canvasser._process_one_mission(as_page(fake), m, dry_run=False)
    assert result == canvasser.MissionOutcome(linkage_expired_id=21)


def test_process_one_mission_propagates_linkage_expired_from_receive() -> None:
    """達成済み + 未受取のミッションで PUT が E1926 を返す経路。"""
    fake = FakePage(responses=[error_response("E1926")])
    m = {
        "mission_id": 21, "mission_name": "ASOBI", "mission_point": 2,
        "action": {"mission_complete_api_call_flag": False},
        "is_mission_completed": True, "is_mission_received": False,
        "remaining_completable_count": 0,
    }
    result = canvasser._process_one_mission(as_page(fake), m, dry_run=False)
    assert result == canvasser.MissionOutcome(linkage_expired_id=21)
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_missions.py::test_receive_returns_linkage_expired_on_e1926 -v`
Expected: FAIL (MissionOutcome / linkage_expired 分岐が未実装)

- [ ] **Step 3: dataclass と関数の戻り値変更を実装**

`canvasser.py` の `_MISSION_TYPES` (L253) の直下あたりに dataclass を追加:

```python
@dataclass(kw_only=True)
class MissionOutcome:
    """1 ミッションの処理結果。`_receive` と `_process_one_mission` で共有する。

    - `gained`: 実獲得または dry-run 見込みの投票券
    - `linkage_expired_id`: E1926 を検知したときの mission_id (POST/PUT どちらでも)
    """

    gained: int = 0
    linkage_expired_id: int | None = None


@dataclass(kw_only=True)
class MissionRunResult:
    """`_process_all_missions` の返却型。"""

    gained: int = 0
    linkage_expired_ids: set[int] = field(default_factory=set)
```

`_complete` に E1926 分岐を追加 (L361 の E1906 分岐の隣):

```python
if ecode == "E1926":
    print("  -> ASOBI 連携トークン切れ (完了 POST)", file=sys.stderr)
    return "linkage_expired"
```

`_receive` を書き換え (L373-393):

```python
def _receive(
    page: Page, mid: int, name: str, pts: int, *, dry_run: bool
) -> MissionOutcome:
    """投票券受取の PUT を送る。E1926 検知で linkage_expired_id を伝搬する。"""
    print(f"[受取] #{mid} {name} (+{pts})")
    if dry_run:
        print("  -> DRY-RUN (PUT送信なし)")
        return MissionOutcome(gained=pts)
    res = call_api(page, "PUT", f"/mission/{mid}/receive")
    if _is_success_response(res):
        body = cast("dict[str, Any]", res["body"])
        payload = cast("dict[str, Any]", body.get("payload") or {})
        received = payload.get("received_point")
        print(f"  -> 成功 (received_point={received})")
        return MissionOutcome(gained=pts)
    ecode = _extract_ecode(res.get("body"))
    if ecode == "E1926":
        print("  -> ASOBI 連携トークン切れ (受取 PUT)", file=sys.stderr)
        return MissionOutcome(linkage_expired_id=mid)
    err_note = f" err={res.get('error')}" if res.get("error") else ""
    print(f"  -> 失敗: HTTP {res['status']}{err_note} body={res.get('body')}")
    return MissionOutcome()
```

`_process_one_mission` を書き換え (L302-336):

```python
def _process_one_mission(
    page: Page, m: dict[str, Any], *, dry_run: bool
) -> MissionOutcome:
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
        return MissionOutcome()

    if not completed and remaining > 0:
        outcome = _complete(page, mid, name, dry_run=dry_run)
        if outcome == "linkage_expired":
            return MissionOutcome(linkage_expired_id=mid)
        if outcome in ("ok", "already_done"):
            return _receive(page, mid, name, pts, dry_run=dry_run)

    return MissionOutcome()
```

`collect_missions` の合算処理 (L289-298) を MissionOutcome 対応に:

```python
gained = 0
for mt, label, payload in listings:
    if mt == 0:
        print(f"現在の保有投票券: {payload.get('current_point', 0)}枚")
    print(f"ミッションモード ({label}): {mode_label}")
    for m in cast("list[dict[str, Any]]", payload["missions"]):
        outcome = _process_one_mission(page, m, dry_run=dry_run)
        gained += outcome.gained
```

**注意**: 本タスクでは E1926 復旧を組み込まない (Task 9 で組む)。ここでは linkage_expired_id を集約するのみ。集約後の使い方は Task 9。

- [ ] **Step 4: 既存テストを新しい戻り値型に追随させる**

`tests/test_missions.py` の既存 `_receive` テストで `assert result == 5` のようになっている箇所を `assert result == canvasser.MissionOutcome(gained=5)` に更新。既存 `_process_one_mission` テストも同様に。

- [ ] **Step 5: 全テスト実行**

Run: `uv run pytest tests/test_missions.py -v`
Expected: 全 PASS

- [ ] **Step 6: コミット**

```bash
git add canvasser.py tests/test_missions.py
git commit -m "feat: MissionOutcome dataclass で _receive/_process_one_mission の戻り値を統一しE1926 検知を伝搬"
```

---

### Task 8: `_run_asobi_linkage_recovery` ドライバを追加

**Files:**
- Modify: `canvasser.py` (`LINKAGE_ENTRY_URL` 定数と `_run_asobi_linkage_recovery` を追加、`from urllib.parse import quote` を追加)
- Create: `tests/test_asobi_linkage_recovery.py`

**Interfaces:**
- Consumes: `_run_guarded_auto_login` (Task 4)、`_detect_login_captcha` (既存)、`check_login` (既存)、`_LOGIN_MAIL_SEL` (既存)
- Produces:
  - `LINKAGE_ENTRY_URL: str` = `f"{API_HOST}/api/v1_1_0/linkages/as/login?backto={quote(MISSION_PAGE_URL, safe='')}"`
  - `_run_asobi_linkage_recovery(page: Page, profile_dir: Path, name: str) -> bool` — 成功なら True、失敗なら False

- [ ] **Step 1: ドライバのテストを書く**

`tests/test_asobi_linkage_recovery.py` を新規作成:

```python
"""ASOBI 連携復旧ドライバのテスト。

Playwright モックで page.url / page.locator / page.goto を差し込む。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

import canvasser
from tests._fakes import FakePage, as_page, success_response

if TYPE_CHECKING:
    from collections.abc import Iterator


class MutablePage(FakePage):
    """polling で url / visibility を書き換えるための拡張フェイク。"""

    def __init__(self, url_sequence: list[str], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._url_seq = list(url_sequence)
        self.url = self._url_seq[0] if self._url_seq else ""

    def goto(self, url: str, **kwargs: object) -> None:
        self.calls.append(("goto", (url, kwargs)))
        # 次の URL を進める
        if len(self._url_seq) > 1:
            self._url_seq.pop(0)
            self.url = self._url_seq[0]


def test_recovery_backto_direct_landing_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BNID セッション生存: backto に直着地して True を返す。"""
    monkeypatch.setattr(canvasser, "time", type("T", (), {"sleep": lambda s: None, "monotonic": lambda: 0.0}))
    page = MutablePage(url_sequence=[canvasser.MISSION_PAGE_URL])
    assert canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test") is True


def test_recovery_bnid_form_appears_calls_guarded_auto_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BNID フォームが可視化されたら _run_guarded_auto_login を呼び、成功なら継続。"""
    monkeypatch.setattr(canvasser, "time", type("T", (), {"sleep": lambda s: None, "monotonic": lambda: 0.0}))
    call_log: list[str] = []
    def fake_guarded(page: object, pdir: Path, name: str) -> bool:
        call_log.append("guarded")
        # ログイン成功後、backto に到達したと仮定
        page.url = canvasser.MISSION_PAGE_URL  # type: ignore[attr-defined]
        return True
    monkeypatch.setattr(canvasser, "_run_guarded_auto_login", fake_guarded)
    page = MutablePage(
        url_sequence=["https://account.bandainamcoid.com/login.html"],
        visibility={canvasser._LOGIN_MAIL_SEL: True},
    )
    assert canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test") is True
    assert call_log == ["guarded"]


def test_recovery_bnid_form_guarded_login_fails_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canvasser, "time", type("T", (), {"sleep": lambda s: None, "monotonic": lambda: 0.0}))
    monkeypatch.setattr(canvasser, "_run_guarded_auto_login", lambda *a, **k: False)
    page = MutablePage(
        url_sequence=["https://account.bandainamcoid.com/login.html"],
        visibility={canvasser._LOGIN_MAIL_SEL: True},
    )
    assert canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test") is False


def test_recovery_captcha_detected_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(canvasser, "time", type("T", (), {"sleep": lambda s: None, "monotonic": lambda: 0.0}))
    page = MutablePage(
        url_sequence=["https://asobistore.jp/some-page"],
        counts={sel: 1 for sel in canvasser._LOGIN_CAPTCHA_SELECTORS},
    )
    assert canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test") is False


def test_recovery_timeout_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """URL が backto にも BNID form にも中間ページにも行かない → timeout で False。"""
    # time.monotonic を制御して即 timeout させる
    counter = iter([0.0, 100.0])
    monkeypatch.setattr(canvasser, "time", type("T", (), {"sleep": lambda s: None, "monotonic": lambda: next(counter)}))
    page = MutablePage(url_sequence=["https://unrelated.example.com/"])
    assert canvasser._run_asobi_linkage_recovery(as_page(page), tmp_path, "test") is False
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_asobi_linkage_recovery.py -v`
Expected: ImportError で FAIL

- [ ] **Step 3: ドライバを実装**

`canvasser.py` に `from urllib.parse import quote` を先頭に追加。`attempt_auto_relogin` の直下あたりに以下を追加:

```python
LINKAGE_ENTRY_URL = (
    f"{API_HOST}/api/v1_1_0/linkages/as/login"
    f"?backto={quote(MISSION_PAGE_URL, safe='')}"
)

# 中間ページ (legacy-login.asobistore.jp) の「バンダイナムコIDでログイン」候補
# セレクタ。実装後に headless で確認して 1 本に絞る (現時点は複数候補で試す)。
_ASOBI_BRIDGE_SELECTORS: tuple[str, ...] = (
    'a[href*="idsvc"]',
    'a:has-text("バンダイナムコID")',
    'button:has-text("バンダイナムコID")',
)


def _run_asobi_linkage_recovery(
    page: Page, profile_dir: Path, name: str
) -> bool:
    """linkages/as/login を踏んで ASOBI 連携を再確立する。成功なら True。

    - BNID セッション生存: 自動通過して backto (mission page) に着地 → True
    - 中間ページの「バンダイナムコIDでログイン」が可視 → click して継続
    - BNID フォームが可視 → `_run_guarded_auto_login` で突破
    - CAPTCHA / タイムアウト → False (呼び出し側は現状同様スキップに退行)

    成功後は cookie 保存事故対策で 10 秒 wait してから mission page に戻す。
    """
    try:
        page.goto(LINKAGE_ENTRY_URL, wait_until="domcontentloaded")
    except PlaywrightError as e:
        print(
            f"[{name}] ASOBI 連携 URL への遷移で失敗: {e}",
            file=sys.stderr,
        )
        return False

    timeout_sec = 60
    interval_sec = 1.0
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            current_url = page.url
            # (a) backto (mission page) 到達
            if current_url.startswith(MISSION_PAGE_URL):
                print(
                    f"[{name}] ASOBI 連携復旧: backto 到達を確認しました。",
                    file=sys.stderr,
                )
                # cookie 保存事故対策の wait (project-asobi-linkage メモリ参照)
                time.sleep(10)
                # spec 3.3 に従い明示的に mission page に戻す (idempotent、既に着地済み
                # ならリロード相当、SSO 途中 URL で観測された場合は確実に mission に戻す)
                with contextlib.suppress(PlaywrightError):
                    page.goto(MISSION_PAGE_URL, wait_until="domcontentloaded")
                return True
            # (b) 中間ページの「バンダイナムコIDでログイン」ボタン
            for sel in _ASOBI_BRIDGE_SELECTORS:
                with contextlib.suppress(PlaywrightError):
                    if page.locator(sel).count() > 0:
                        print(
                            f"[{name}] 中間ページのブリッジボタン ({sel}) をクリック",
                            file=sys.stderr,
                        )
                        with contextlib.suppress(PlaywrightError):
                            page.locator(sel).click()
                        break
            # (c) BNID フォームが可視 → guarded auto_login
            with contextlib.suppress(PlaywrightError):
                if page.locator(_LOGIN_MAIL_SEL).is_visible():
                    if not _run_guarded_auto_login(page, profile_dir, name):
                        return False
                    # 成功後の続きは次ポーリングで backto 到達を待つ
                    continue
            # (d) CAPTCHA/2FA 検知 → 復旧失敗
            if _detect_login_captcha(page):
                print(
                    f"[{name}] ASOBI 連携復旧中に CAPTCHA/2FA を検知、abort",
                    file=sys.stderr,
                )
                return False
        except PlaywrightError as e:
            print(
                f"[{name}] ASOBI 連携復旧ポーリング中に PlaywrightError: {e}",
                file=sys.stderr,
            )
            # 次ポーリングに委ねる
        time.sleep(interval_sec)

    print(
        f"[{name}] ASOBI 連携復旧タイムアウト",
        file=sys.stderr,
    )
    return False
```

- [ ] **Step 4: `FakePage` に `url` 属性が無いので `tests/_fakes.py` を軽く拡張**

`tests/_fakes.py` の `FakePage.__init__` に `self.url: str = ""` を追加する。既存テストへの影響は無い (アクセスされない限り初期値のまま)。

- [ ] **Step 5: 全テスト実行**

Run: `uv run pytest tests/ -v`
Expected: 全 PASS

- [ ] **Step 6: コミット**

```bash
git add canvasser.py tests/_fakes.py tests/test_asobi_linkage_recovery.py
git commit -m "feat: _run_asobi_linkage_recovery ドライバを追加"
```

---

### Task 9: `collect_missions` を E1926 再走ループ構造にリファクタし復旧を組み込む

**Files:**
- Modify: `canvasser.py:256-299` (`collect_missions`, 内部で `_fetch_mission_listings` / `_process_all_missions` / `_filter_listings` を切り出す)
- Modify: `canvasser.py:2691-2751` (`process_account` から `collect_missions` への呼び出しに `profile_dir`, `name` を追加)
- Modify: `tests/test_missions.py` (E1926 復旧のエンドツーエンドテスト追加)

**Interfaces:**
- Consumes: `MissionOutcome`, `MissionRunResult` (Task 7)、`_run_asobi_linkage_recovery` (Task 8)、`_process_one_mission` (Task 7)
- Produces:
  - `collect_missions(page: Page, profile_dir: Path, name: str, *, dry_run: bool) -> int` — signature 変更: `profile_dir`, `name` 追加
  - `_fetch_mission_listings(page: Page) -> list[tuple[int, str, dict[str, Any]]]` — 2 listing の GET を集約する内部関数
  - `_process_all_missions(page: Page, listings: list, *, dry_run: bool) -> MissionRunResult` — 全 listing を回して MissionRunResult を返す
  - `_filter_listings(listings: list, keep_ids: set[int]) -> list` — listing 内の missions を keep_ids に絞る

- [ ] **Step 1: E1926 復旧の end-to-end テストを書く**

`tests/test_missions.py` に追加:

```python
def test_collect_missions_e1926_recovery_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1926 が返ったら driver 起動 → 再走で成功、gained は累積される。

    実装は _fetch_mission_listings を **1 回だけ** 呼び、driver 成功後は
    _filter_listings で E1926 だった mission_id に絞って再走する。したがって
    レスポンスキューは fetch 2 (mt=0/mt=1) + attempt 1 の POST/PUT + attempt 2 の
    POST/PUT のみで、2 回目 fetch レスポンスは積まない。
    """
    call_log: list[str] = []
    monkeypatch.setattr(
        canvasser,
        "_run_asobi_linkage_recovery",
        lambda page, pdir, name: call_log.append("driver") or True,
    )
    fake = FakePage(responses=[
        # fetch (1 回のみ): mt=0 に 1 件、mt=1 に 1 件
        success_response({"current_point": 100, "missions": [
            {"mission_id": 10, "mission_name": "normal", "mission_point": 5,
             "action": {"mission_complete_api_call_flag": True},
             "is_mission_completed": False, "is_mission_received": False,
             "remaining_completable_count": 1},
        ]}),
        success_response({"missions": [
            {"mission_id": 21, "mission_name": "asobi", "mission_point": 2,
             "action": {"mission_complete_api_call_flag": True},
             "is_mission_completed": False, "is_mission_received": False,
             "remaining_completable_count": 1},
        ]}),
        # attempt 1: normal POST 成功、PUT 成功、asobi POST E1926
        success_response(),
        success_response({"received_point": 5}),
        error_response("E1926"),
        # driver 成功 (mock)、attempt 2: mission_id=21 だけ再走 POST 成功、PUT 成功
        success_response(),
        success_response({"received_point": 2}),
    ])
    gained = canvasser.collect_missions(as_page(fake), tmp_path, "test", dry_run=False)
    # gained = 5 (normal, attempt 1) + 2 (asobi, attempt 2) = 7、driver 1 回のみ
    assert gained == 7
    assert call_log == ["driver"]


def test_collect_missions_e1926_driver_failure_returns_partial_gained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        canvasser, "_run_asobi_linkage_recovery",
        lambda page, pdir, name: False,
    )
    fake = FakePage(responses=[
        success_response({"current_point": 100, "missions": [
            {"mission_id": 10, "mission_name": "normal", "mission_point": 5,
             "action": {"mission_complete_api_call_flag": True},
             "is_mission_completed": False, "is_mission_received": False,
             "remaining_completable_count": 1},
        ]}),
        success_response({"missions": [
            {"mission_id": 21, "mission_name": "asobi", "mission_point": 2,
             "action": {"mission_complete_api_call_flag": True},
             "is_mission_completed": False, "is_mission_received": False,
             "remaining_completable_count": 1},
        ]}),
        success_response(),
        success_response({"received_point": 5}),
        error_response("E1926"),
    ])
    gained = canvasser.collect_missions(as_page(fake), tmp_path, "test", dry_run=False)
    assert gained == 5  # normal だけ回収、asobi は復旧失敗でスキップ


def test_collect_missions_dry_run_does_not_trigger_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dry_run=True では E1926 でも driver を起動しない。"""
    call_log: list[str] = []
    monkeypatch.setattr(
        canvasser, "_run_asobi_linkage_recovery",
        lambda *a, **k: call_log.append("driver") or True,
    )
    # dry_run では POST が送られない → linkage_expired は起きない前提だが、
    # 万が一のためにも driver 起動しないことを確認
    fake = FakePage(responses=[
        success_response({"current_point": 100, "missions": []}),
        success_response({"missions": []}),
    ])
    canvasser.collect_missions(as_page(fake), tmp_path, "test", dry_run=True)
    assert call_log == []
```

- [ ] **Step 2: テストが失敗することを確認**

Run: `uv run pytest tests/test_missions.py::test_collect_missions_e1926_recovery_flow -v`
Expected: FAIL

- [ ] **Step 3: `collect_missions` を再構成**

`canvasser.py:256-299` を以下に置き換える:

```python
def _fetch_mission_listings(
    page: Page,
) -> list[tuple[int, str, dict[str, Any]]]:
    """通常 (0) と ASOBI (1) の 2 listing GET を集約する。"""
    listings: list[tuple[int, str, dict[str, Any]]] = []
    for mt, label in _MISSION_TYPES:
        listing = call_api(page, "GET", f"/missions?mission_type={mt}&limit=300")
        payload = _success_payload_or_raise(
            listing, f"ミッション一覧 ({label}) の取得に失敗"
        )
        listings.append((mt, label, payload))
    return listings


def _process_all_missions(
    page: Page,
    listings: list[tuple[int, str, dict[str, Any]]],
    *,
    dry_run: bool,
) -> MissionRunResult:
    """全 listing を回し、gained 累積と linkage_expired_id 集合を返す。"""
    result = MissionRunResult()
    for mt, label, payload in listings:
        if mt == 0:
            print(f"現在の保有投票券: {payload.get('current_point', 0)}枚")
        mode_label = "本番" if not dry_run else "DRY-RUN (POST/PUT送信なし)"
        print(f"ミッションモード ({label}): {mode_label}")
        for m in cast("list[dict[str, Any]]", payload["missions"]):
            outcome = _process_one_mission(page, m, dry_run=dry_run)
            result.gained += outcome.gained
            if outcome.linkage_expired_id is not None:
                result.linkage_expired_ids.add(outcome.linkage_expired_id)
    return result


def _filter_listings(
    listings: list[tuple[int, str, dict[str, Any]]],
    keep_ids: set[int],
) -> list[tuple[int, str, dict[str, Any]]]:
    """listing 内の missions を keep_ids に絞る (payload は copy して mutate)。"""
    filtered: list[tuple[int, str, dict[str, Any]]] = []
    for mt, label, payload in listings:
        new_payload = dict(payload)
        new_payload["missions"] = [
            m for m in cast("list[dict[str, Any]]", payload.get("missions", []))
            if m.get("mission_id") in keep_ids
        ]
        filtered.append((mt, label, new_payload))
    return filtered


def collect_missions(
    page: Page, profile_dir: Path, name: str, *, dry_run: bool
) -> int:
    """API 経由でミッションを消化する。E1926 検知時は連携復旧を試み再走する。"""
    listings = _fetch_mission_listings(page)
    total_gained = 0
    for attempt in (1, 2):
        result = _process_all_missions(page, listings, dry_run=dry_run)
        total_gained += result.gained
        if not result.linkage_expired_ids or dry_run:
            break
        # dry_run では復旧を起動しない (契約: dry_run は副作用ゼロ)
        if attempt == 2:
            # 2 回目でも linkage_expired が残っていたら諦める (driver は 1 回のみ)
            break
        if not _run_asobi_linkage_recovery(page, profile_dir, name):
            break  # 復旧失敗 → 現行同様スキップ扱いで終了
        # 復旧成功、次 attempt で該当 id だけを再走
        listings = _filter_listings(listings, keep_ids=result.linkage_expired_ids)
    result_label = "獲得見込み" if dry_run else "獲得"
    print(f"ミッション {result_label}: {total_gained}枚")
    return total_gained
```

- [ ] **Step 4: `process_account` の呼び出しを追随**

`canvasser.py:2691-2751` の該当箇所を書き換え:

```python
if options.run_mission:
    mission_gain = collect_missions(page, profile_dir, name, dry_run=options.dry_run)
    if not options.dry_run:
        gained += mission_gain
```

- [ ] **Step 5: 既存 `collect_missions` テストの signature を追随**

`tests/test_missions.py` の既存 `collect_missions(as_page(fake), dry_run=...)` 呼び出しを `collect_missions(as_page(fake), tmp_path, "test", dry_run=...)` に更新する。tmp_path fixture を追加。

- [ ] **Step 6: 全テスト実行**

Run: `uv run pytest tests/ -v`
Expected: 全 PASS

- [ ] **Step 7: コミット**

```bash
git add canvasser.py tests/test_missions.py
git commit -m "feat: E1926 検知時に ASOBI 連携復旧を起動し該当 mission を再走"
```

---

## Phase 4: ドキュメント更新

### Task 10: README を新方式に書き換え

**Files:**
- Modify: `README.md`

**Interfaces:** (公開ドキュメント、コード I/F 変更なし)

- [ ] **Step 1: README の login-init 章と `--execute` 記述を削除・書き換え**

以下の変更を `README.md` に適用する:

- L26-45 (旧「自動再ログインを使う場合、`./profiles/{account}/credentials.json` に BNID の資格情報が保存される〜」から `disabled_until` 説明まで) を削除
- L45-66 (「自動再ログイン用の資格情報登録 (`login-init`)」章全体) を削除
- L47-56 (「初回ログイン」節) に以下を追記:

```markdown
初回ログイン時に BNID フォームでパスワードを入力する際、Chrome の
「パスワードを保存しますか?」プロンプトで **保存を選択** すること。
この保存された資格情報は Playwright プロファイル内 (`profiles/{account}/Default/Login Data`) に
Windows DPAPI + AES-256-GCM で暗号化されて格納され、以降 BNID セッション切れが
検知されたときに自動再ログインの供給源として使われる。
```

- L68-91 (「日次実行」「特定アカウントのみ実行」「チェックイン」節) の `--execute` を削除、代わりに:

```markdown
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
```

- タスクスケジューラ登録例 (L127-135) を以下に更新 (README 内では `--execute` を出さない、旧仕様からの移行手順と rollback は設計 doc `docs/superpowers/specs/2026-07-08-chrome-login-data-auto-relogin-design.md` の Rollout 節を参照):

```markdown
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
```

- 「ASOBI 連携復旧」に関する記述を追加 (`## 挙動の要点` の直下あたり):

```markdown
### ASOBI STORE 連携の自動復旧

`mission_type=1` のミッション (ASOBI STORE 系、プレミアム会員ログボ #21 など) で
`E1926` (ASOBISTORE への再ログインが必要) が返ったら、`linkages/as/login` を踏んで
自動復旧する。途中の中間ページ (legacy-login.asobistore.jp) は「バンダイナムコIDで
ログイン」を自動クリック、BNID フォームが出た場合は Chrome 自動保存の資格情報で
自動突破する。復旧に失敗した場合はミッションはスキップされ、翌日に再試行される。
```

- L113 の `--no-auto-relogin` 説明を更新: `credentials.json が保存されていても` → `Chrome 自動保存の BNID 資格情報が読める場合でも`

- [ ] **Step 2: 最終 grep で `execute` 語の残存を canvasser.py / tests / README で確認 (大文字小文字問わず)**

Run: `rg -ni "\bexecute\b" canvasser.py tests/ README.md`
Expected: ヒット無し (`docs/superpowers/` は歴史アーカイブなので検査対象外)

- [ ] **Step 3: 全テスト実行**

Run: `uv run pytest tests/ -v`
Expected: 全 PASS

- [ ] **Step 4: コミット**

```bash
git add README.md
git commit -m "docs: README を Chrome Login Data 方式 + --dry-run 一本化に更新"
```

---

## Phase 5: Rollout の smoke test 実施と PR 作成

### Task 11: 実プロファイルでの smoke test と PR 作成

**Files:** (コード変更なし、確認と PR 提出のみ)

- [ ] **Step 1: `mission --dry-run` で 3 アカウントとも exit 0 になることを確認**

Run: `uv run canvasser.py mission --dry-run`
Expected: 3 アカウント (haruo / shun / syota) それぞれ実行され、最後に exit code 0 で終了

エラーが出たら該当箇所を調査、必要ならフィードバック用スレッドに投稿。

- [ ] **Step 2: Login Data 読みの独立確認**

Run:
```powershell
uv run python -c "from canvasser import load_credentials; from pathlib import Path; [print(a, load_credentials(Path(f'profiles/{a}')) is not None) for a in ('haruo','shun','syota')]"
```
Expected: 3 アカウントとも `True`

**もし False が出た場合**: 対象アカウントで手動 `uv run canvasser.py login --account NAME` を実行し、Chrome の「パスワード保存しますか?」で **保存を選択** してから再確認。

- [ ] **Step 3: `feat/auto-relogin` を push**

Run: `git push -u origin feat/auto-relogin`
Expected: push 成功

- [ ] **Step 4: PR body を tmp file に書き出し (`--body-file` 方式で quote 事故を回避)**

Windows 環境なので **PowerShell を primary** として提示する。Claude Code の Bash tool を使う場合の代替も併記する。どちらも同じ内容の `pr-body.md` を書き出せば OK。

**PowerShell 版 (primary)**:

```powershell
$body = @'
## Summary
- Chrome 自動保存パスワード (v10 DPAPI+AES-256-GCM) から BNID 資格情報を復号し、`credentials.json` / `login-init` を撤去
- mission 実行中の E1926 で `linkages/as/login` を踏み ASOBI STORE 連携を自動復旧
- `--execute` を廃止、`--dry-run` に一本化 (無指定 = 本番実行)

## Test plan
- [ ] 全テスト PASS (`uv run pytest tests/ -v`)
- [ ] `rg -ni "\bexecute\b" canvasser.py tests/ README.md` でヒット無し (`docs/superpowers/` は歴史アーカイブとして検査対象外)
- [ ] 実プロファイル 3 アカウントで `uv run canvasser.py mission --dry-run` が exit 0 で終わる
- [ ] `load_credentials(Path("profiles/{acc}"))` が 3 アカウントとも非 None
- [ ] PR マージ直後に Task 12 (デプロイ後手順) を実行
- [ ] 翌日 12:05 の自動実行後、`Get-ScheduledTaskInfo -TaskName cgge-canvasser` の LastTaskResult=0 と Discord へ完了報告

## 設計
`docs/superpowers/specs/2026-07-08-chrome-login-data-auto-relogin-design.md` (codex-review-loop 5 round で PASS)
'@
$body | Set-Content -Encoding UTF8 -LiteralPath "$env:TEMP\cgge-pr-body.md"
```

**Bash 版 (Claude Code Bash tool = Git Bash on Windows で実行する場合)**:

```bash
cat > /tmp/cgge-pr-body.md <<'EOF'
## Summary
- Chrome 自動保存パスワード (v10 DPAPI+AES-256-GCM) から BNID 資格情報を復号し、`credentials.json` / `login-init` を撤去
- mission 実行中の E1926 で `linkages/as/login` を踏み ASOBI STORE 連携を自動復旧
- `--execute` を廃止、`--dry-run` に一本化 (無指定 = 本番実行)

## Test plan
- [ ] 全テスト PASS (`uv run pytest tests/ -v`)
- [ ] `rg -ni "\bexecute\b" canvasser.py tests/ README.md` でヒット無し (`docs/superpowers/` は歴史アーカイブとして検査対象外)
- [ ] 実プロファイル 3 アカウントで `uv run canvasser.py mission --dry-run` が exit 0 で終わる
- [ ] `load_credentials(Path("profiles/{acc}"))` が 3 アカウントとも非 None
- [ ] PR マージ直後に Task 12 (デプロイ後手順) を実行
- [ ] 翌日 12:05 の自動実行後、`Get-ScheduledTaskInfo -TaskName cgge-canvasser` の LastTaskResult=0 と Discord へ完了報告

## 設計
`docs/superpowers/specs/2026-07-08-chrome-login-data-auto-relogin-design.md` (codex-review-loop 5 round で PASS)
EOF
```

- [ ] **Step 5: `gh pr create` を実行 (どちらの環境でも `gh` の path は問題にならない)**

**PowerShell 版**:
```powershell
gh pr create --title "Chrome Login Data 自動再ログイン + ASOBI 連携復旧 + --dry-run 一本化" --body-file "$env:TEMP\cgge-pr-body.md"
```

**Bash 版**:
```bash
gh pr create --title "Chrome Login Data 自動再ログイン + ASOBI 連携復旧 + --dry-run 一本化" --body-file /tmp/cgge-pr-body.md
```
Expected: PR URL が出力される

- [ ] **Step 6: PR URL を Discord に投稿してレビュー依頼**

`gh pr view --json url --jq .url` で PR URL を取得し、Discord へ:

「Chrome Login Data 方式の PR を作成しました。レビューお願いします: <URL>。マージ後に Task 12 (デプロイ後手順) を必ず実行してください」

---

### Task 12: デプロイ後の必須手順 (PR マージ後、タスクスケジューラ更新と smoke test 実施)

**Files:** (コード変更なし、Windows タスクスケジューラの更新のみ)

**このタスクは PR マージ後に手動で実行する必要がある**。checkbox 消化型 agent がある場合、agent は「PR がマージ済みか」を `gh pr view --json state` で確認してから進む。マージ前ならこのタスクを skip する。

- [ ] **Step 1: PR がマージ済みかを確認 (未マージなら skip)**

Run: `gh pr view feat/auto-relogin --json state --jq .state`
Expected: `MERGED` (それ以外なら本タスクを skip、マージ後に再実行)

`feat/auto-relogin` が既に削除されている場合は `gh pr list --state merged --search "head:feat/auto-relogin"` で PR 番号を特定してから `gh pr view <number> --json state --jq .state` に切り替える。

- [ ] **Step 2: `main` を pull してローカルを最新化**

Run: `git checkout main && git pull`

- [ ] **Step 3: デプロイ前のスケジューラ状態を控える**

Run: `schtasks /Query /TN "cgge-canvasser" /V /FO LIST | Select-String "Task To Run"`
Expected: 現行の TR を目視で確認しメモする (rollback が必要になった場合の復元用。`--execute` の有無は状態記録のみ、判定には使わない)

- [ ] **Step 4: タスクスケジューラの TR を更新**

`where.exe uv` で uv.exe のパスを確認し、以下のコマンドで TR を更新する (uv パスは環境ごとに変わるので都度確認):

Run:
```powershell
schtasks /Change /TN "cgge-canvasser" /TR "`"C:\Users\shun\.local\bin\uv.exe`" run canvasser.py mission --profiles-dir D:\projects\cgge-canvasser\profiles"
```
Expected: `SUCCESS: The parameters of scheduled task "cgge-canvasser" have been changed.`

- [ ] **Step 5: 更新後の TR を確認**

Run: `schtasks /Query /TN "cgge-canvasser" /V /FO LIST | Select-String "Task To Run"`
Expected: TR に `mission` が含まれ、`--execute` が含まれていないこと

- [ ] **Step 6: PR 直後 smoke test — dry-run と Login Data 読みを確認**

Run:
```powershell
cd D:\projects\cgge-canvasser
uv run canvasser.py mission --dry-run
```
Expected: 3 アカウント (haruo / shun / syota) がそれぞれ実行され exit 0 で終了

Run:
```powershell
uv run python -c "from canvasser import load_credentials; from pathlib import Path; [print(a, load_credentials(Path(f'profiles/{a}')) is not None) for a in ('haruo','shun','syota')]"
```
Expected: 3 アカウントとも `True` (パスワード値は表示しない)

**もし False が出た場合**: 対象アカウントで手動 `uv run canvasser.py login --account NAME` を実行し、Chrome の「パスワードを保存しますか?」で **保存を選択** してから再確認。

- [ ] **Step 7: 翌日 12:05 の自動実行結果を確認**

Run: `Get-ScheduledTaskInfo -TaskName cgge-canvasser | Select-Object LastRunTime,LastTaskResult`
Expected: LastTaskResult=0、獲得票数 3 アカウント合計 +21 枚 (デイリー)

Discord に完了報告を投稿する。

- [ ] **Step 8 (rollback が必要になった場合のみ): 旧コードへ切り戻し**

**このステップは通常実行しない**。Step 7 で問題があり、旧コードへ戻す判断をした場合のみ実行する:

```powershell
git checkout <rollback-target-commit>
schtasks /Change /TN "cgge-canvasser" /TR "`"C:\Users\shun\.local\bin\uv.exe`" run canvasser.py mission --execute --profiles-dir D:\projects\cgge-canvasser\profiles"
schtasks /Query /TN "cgge-canvasser" /V /FO LIST | Select-String "Task To Run"
```

**重要**: 旧コードは `--execute` 未指定だと dry-run で無回収終了 (見かけ上は成功) するので、rollback 時は TR に `--execute` を必ず戻すこと。TR に `--execute` が含まれることを Step の Query で確認する。詳細は `docs/superpowers/specs/2026-07-08-chrome-login-data-auto-relogin-design.md` の Rollout 節参照。

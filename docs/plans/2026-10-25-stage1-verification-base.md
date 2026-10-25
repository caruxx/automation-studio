# 段階 1: 検証基盤（CI・テスト隔離・終了コードの単一正本）

仕様: docs/SPEC_REVISION_DRAFT_2026-10-25.md §3.2。base: main 2df75c3。統括: Claude Fable 5.1（本人指定）。実装: 統括セッション直接、タスクごとに Opus 5.5 サブエージェントがレビュー。
段階 1a で済んだもの: Python 3.12 venv と requirements.lock、pytest testpaths、Pillow/numpy の本体依存。

## Global Constraints
1. 絵文字を新規に追加しない。個人情報（実ユーザー名、メールアドレス、Drive アカウント名）を追加しない。
2. 制作経路は ffmpeg_only のみ。Adobe 関連を復活させない。
3. テストは実設定（~/.config/orzz）、Drive、外部サービスに書き込まない。一時ディレクトリに .migration_completed を置く。
4. サーバー起動・VPS・外部 API を呼ばない。commit は日本語で「何を・なぜ」、末尾に Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>。push と PR は本人許可。
5. 終了コードの意味を変えない: 0 成功 / 1 失敗 / 2 使い方 / 75 unattended_login / 76 retryable / 77 quota_exhausted / 78 preflight_fail / 79 selection_pending。79 は「待機（blocked）」であり失敗に数えない。

## Task 1: 終了コードの単一正本
- 新設 `Python/workflow.json`（schema_version 1、workflow_version "2026-10-25.1"、`exit_reasons[]` のみ。stage/resource は段階 2 で追加）。
  exit_reason: {reason, exit_code, status, retryable}。status は done / failed / blocked / usage。
- 新設 `Python/exit_codes.py`: workflow.json を読み `ExitCode`（IntEnum）、`reason_for(code)`、`status_for(code)`、`is_blocked(code)`、`is_retryable(code)` を提供。読めない場合は組み込みの既定表で動く（起動を止めない）。
- 置換: app_pipeline.py の EXIT_* 定数、app_orchestrator.py の `Exit`（79 を追加し、`_normalize_result` が "selection_pending" を返す経路と on_fail で NOTIFY ではなく「待機」扱い）、batch_orchestrator.py の EXIT_PREFLIGHT、error_humanizer.py の SENTINELS のキー（79 の説明を追加）、app.py の `== 79` 直書き 3 箇所、studio.py の emit に `reason` と `status` を追加。
- 受入 `exit_contract`: 4 入口（pipeline の sys.exit、orchestrator の Exit、batch の戻り値、studio.py の emit）が同じ表を参照し、テストで workflow.json と各モジュールの定数が一致することを検証。`consecutive_failures` が blocked を数えないこと。
- テスト: `Python/tests/test_exit_contract.py`。

## Task 2: テストの自己隔離と skip の可視化
- `Python/tests/__init__.py` で、環境変数未設定なら一時ディレクトリを作り APP_CONFIG_DIR / STUDIO_CONFIG_DIR / APP_SHARED_BASE / HOME を向け、.migration_completed を置く（discover でも pytest でも最初に import される）。終了時に削除。
- ApiBoundaryTest の skip 条件（一時ディレクトリが $TMPDIR 外）を解消するか、skip 理由を明示。`python3 Python/scripts/run_tests.py` を新設し、unittest の結果を集計して skipped > 0 なら exit 1（CI 用。`--allow-skip` で緩和）。
- 受入: 環境変数なしで `cd Python && python -m unittest discover tests` が通り、実設定への書き込み 0（テスト前後で ~/.config/orzz の mtime 不変をテスト内で確認はしない。run_tests.py が HOME を一時化することで担保）。

## Task 3: Mac repo の CI
- `.github/workflows/ci.yml`: push と pull_request。ubuntu-latest、Python 3.12、`pip install -r requirements.lock`、`apt-get install ffmpeg`、`python Python/scripts/run_tests.py`、`node Python/scripts/check_onclick_refs.js web/static/index.html`、`python -m compileall -q Python`。concurrency で同 branch の古い run を取消。
- 受入: workflow の構文が `actionlint`（あれば）または YAML パースで通る。CI 実行結果の確認は push 後（本人許可後）。

## Task 4: VPS repo（caruxx/automation-studio-vps）の worker と CI
- 別 worktree・別 branch `feature/stage1-exit-contract`。`worker/client.py` の `_pipeline_exit_error` に 79 を追加（message "pipeline selection_pending"、retryable=False、blocked=True 相当のフラグを job 結果に載せる。既存の API 契約を壊さない範囲で `error` JSON に `status: "blocked"` を含める）。終了コード表は `worker/exit_codes.json` として Mac の workflow.json の exit_reasons と同じ内容を置き、テストで表の整合（コード→reason）を検証。
- `.github/workflows/ci.yml`: pytest（tests/conftest.py は既に in-memory SQLite で隔離済み）。
- 受入: `pytest tests` が通る。75/78/79 の判定が Mac 側と一致。

## 検証の記録
各タスクの report に、コマンド・環境・SHA・結果を残す。CI の実行は push 後に head SHA で確認し、skipped/neutral は合格としない。

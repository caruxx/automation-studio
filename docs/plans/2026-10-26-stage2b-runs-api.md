# 段階 2b: VPS の run API・AI 用長期トークン・CLI（run / status / resume / cancel）

仕様: docs/SPEC_REVISION_DRAFT_2026-10-25.md §3.3 前半（起動経路を VPS 台帳の run に統一、認証、CLI）。
base: Mac main f86689a / VPS main 0ff0484。統括: Claude Fable 5.1（本人指定。2026-10-26 に「確認せずに進める」包括許可あり。force push・履歴書換え・VPS 本番 deploy は対象外）。実装: 統括セッション直接、タスクごとに Opus 5.5 サブエージェントがレビュー。
段階 2c（後続）: lease_epoch と旧 lease の報告拒否、input_hash と done_validator による完了失効、コードの工程名切替（stage_aliases）。

## 設計の要点
- **run** = 「channel_key × vol × workflow_version × request_id」で起票される 1 回の実行要求。VPS の `runs` テーブルが状態の正本。Job は run が発行する実行単位（既存の jobs テーブルを流用。payload に run_id を載せる）。
- **冪等**: 同じ request_id の `POST /runs` は同じ run を返す（201 ではなく 200）。request_id は呼び出し側（CLI）が UUID を生成し `--request-id` で固定もできる。
- **版の照合**: `workflow_version` が VPS 側の `worker/exit_codes.json` の値と違えば 409（`workflow_version_mismatch`）。
- **認証**: 既存のユーザーセッション（JWT）に加え、管理者が発行する **AI 用長期トークン**（`ai_tokens`: scopes `["ai:runs"]`、channel_keys による制限、expires_at、revoked_at、last_used_at、監査ログ `ai_token_audits`）。runs API は「ユーザー または ai:runs トークン」で呼べる。トークンの発行・一覧・失効は admin のみ（`/api/ai-tokens`）。平文は発行時に 1 回だけ返す（worker_tokens と同じ prefix + bcrypt 方式）。
- **状態**: run.state = queued / running / succeeded / failed / cancelled / blocked。Job の遷移（lease / complete / fail）に合わせて run を更新。fail の error JSON に status: blocked があれば run も blocked（失敗に数えない）。
- **CLI**: `studio.py run --vol N [--channel K] [--intent pipeline] [--from STEP] [--request-id ID] --json`、`status --run-id`、`resume --run-id [--from STEP]`、`cancel --run-id`。設定は Mac の `vps_ledger_sync` と同じ JSON（`base_url` + 新キー `ai_token`）。stdout は JSON 1 行、人向けは stderr。

## Global Constraints
1. 絵文字を新規に追加しない。個人情報・秘密情報（トークン平文）をコード・テスト・ログに書かない。
2. 既存の worker API / jobs API / vols API の契約を壊さない（追加のみ）。alembic はダウングレード可能にする。
3. テストは in-memory SQLite（conftest）。外部通信・本番 DB・VPS 実機を触らない。
4. commit は日本語、末尾に Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>。
5. 終了コードの意味を変えない。待機（blocked）は失敗に数えない（段階 1 の契約）。

## Task 1（VPS）: AI 用長期トークンと監査
- models: `AiToken`（user_id, label, token_hash, token_prefix, scopes JSON, channel_keys JSON|null, expires_at|null, revoked_at|null, last_used_at, created_at）、`AiTokenAudit`（ai_token_id, at, action, method, path, request_id|null, remote_addr|null, status_code）。alembic `20261026_0014_ai_tokens`。
- dependencies: `get_current_ai_token(required_scope)`（Bearer → prefix 照合 → bcrypt → 失効・期限・scope を検査 → last_used_at 更新 → 監査行を追加）。`get_run_principal`（ユーザー JWT か ai:runs トークンのどちらか。両方無ければ 401）。
- API `/api/ai-tokens`（admin）: POST 作成（label, scopes, channel_keys, expires_in_days）→ 平文を 1 回返す、GET 一覧（平文なし）、POST `{id}/revoke`。GET `{id}/audits`（直近 N 件）。
- 受入: 期限切れ・失効・scope 不足・channel 制限外は 401/403。監査行が残る。既存テスト全件合格。

## Task 2（VPS）: runs API
- models: `Run`（id, request_id unique, channel_id, vol_id, workflow_version, input_revision|null, intent, from_step|null, state, current_job_id|null, attempts, created_by_user_id|null, created_by_ai_token_id|null, created_at, updated_at, finished_at|null, last_error|null）。alembic `20261026_0015_runs`。
- API `/api/runs`: POST（{request_id, channel_key, vol, workflow_version, input_revision?, intent?, from_step?, priority?}）→ 既存 request_id なら 200 で同じ run、無ければ vol を channel_key + vol で解決（無ければ 404。採番はしない）、workflow_version 不一致は 409、Job（job_type pipeline、payload に run_id / intent / channel_key / vol_id / vol / from_step）を発行して 201。GET `{id}`（run + 現在の job の要約）。POST `{id}/resume`（state が failed / blocked / cancelled のとき、from_step を付けて新 Job を発行。attempts+1）。POST `{id}/cancel`（queued / leased の job を cancel、run を cancelled）。
- 連動: worker の complete / fail / lease で、payload.run_id があれば run.state を更新（lease→running、complete→succeeded、fail→failed、fail で error JSON の status=blocked → blocked、retryable で queued に戻るなら running のまま）。
- channel 制限: ai トークンは channel_keys に含まれる channel のみ。ユーザーは accessible_channel_id_set。
- 受入 `run_contract`（本段階の範囲）: 同一 request_id の重複 run 0、workflow_version 不一致で 409、blocked は失敗に数えない、resume で新 Job が発行される、cancel 後の lease で取れない。
- `GET /api/runs?channel_key=&state=` 一覧（上限 100）。

## Task 3（Mac）: CLI
- `Python/vps_runs.py`: 設定読込（`vps_ledger_sync` の CONFIG_PATH と同じファイル。`ai_token` 優先、無ければ `worker_token` は使わない＝ai:runs 専用）、`create_run / get_run / resume_run / cancel_run`、workflow_version は `exit_codes.WORKFLOW_VERSION`。
- `studio.py`: intents `run` / `status` / `resume` / `cancel`（routes.json に追加、`requires_vol` は run のみ）。引数 `--run-id`、`--request-id`、`--intent`、`--from`。stdout は JSON 1 行。終了コード: 0 成功 / 1 失敗 / 2 使い方 / 79 blocked（status で run が blocked のとき）。
- 受入: VPS 未設定時は明確なエラー（`vps_not_configured`）。request_id 固定で 2 回 run しても同じ run_id が返る（API をモックして検証）。

## 検証の記録
VPS: pytest（in-memory SQLite）。Mac: run_tests.py。最終レビューは両 repo の branch を Opus サブエージェント → 独立最終レビュー（別セッションの Opus 5.5 中 + Codex Sol 6.1 medium）。

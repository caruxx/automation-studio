# 段階 2c: lease_epoch・完了失効（input_hash / done_validator）・工程名切替

仕様: docs/SPEC_REVISION_DRAFT_2026-10-25.md §2（run_id/attempt/lease_epoch、完了判定）、§2.1（工程順と stage_aliases）、§3.3 受入 run_contract（旧 lease の報告拒否、入力変更で依存工程の完了が失効）。
base: Mac main 90673dc / VPS main ead8517。統括: Claude Fable 5.1（本人指定、2026-10-26「確認せずに進める」包括許可。force push・履歴書換え・本番 deploy は対象外）。実装: 統括セッション直接、タスクごとに Opus 5.5 サブエージェントがレビュー。
本番 deploy は Hetzner 解約・Xserver 移行のため中断中（本人指示 2026-10-26）。本段階は repo 統合まで。

## 設計の要点
- **lease_epoch（VPS）**: Job に `lease_epoch`（int、既定 0）を持ち、lease を付与するたびに +1。lease 応答に含め、worker は heartbeat / complete / fail に `lease_epoch` を添える。送られた epoch が現在値と違えば 409 `lease_epoch_mismatch`（旧 lease からの報告）。未送信（旧 worker）は従来どおり worker_id と期限だけで判定（互換）。Run への同期は現状維持。
- **完了失効（Mac）**: 新設 `Python/stage_state.py`。vol フォルダの `.stage_state.json` に `{stage: {input_hash, completed_at, workflow_version}}` を記録。`input_hash` = その工程の `depends_on`（workflow.json）の出力指紋（done_validator が見る成果物の path / size / mtime_ns、小さい JSON/TXT は sha256）を連結した sha256。完了判定 `stage_done(stage, folder, vol)` = `done_validator` 合格 かつ（記録が無い＝旧 vol なら合格のみで done、記録があれば input_hash 一致）。記録があり不一致なら **失効**（done ではない）。pipeline は各工程成功時に `mark_done` を呼ぶ。orchestrator の `_stage_artifact_done` は `stage_done` を使う。
- **工程名切替（Mac）**: `STEPS` を `suno, select, rename, bgimage, composite, render, qa, meta, localization, thumbnail, upload` に。premiere は廃止（自動通過を削除。`--from premiere` は `render` に写す）。旧名は `exit_codes.canonical_stage` で入力境界（CLI --from/--only、APP_PIPELINE_STEPS、routes.json の intent、API from_step、台帳の stage 照合、resource_lock / parallel_guard のキー）で受け付ける。routes.json の旧 intent キー（psd_composite / export）は残し、新キー（composite / render）を追加。UI の表示ラベルは据え置き（段階 7）。
- **版**: 工程名は Mac / VPS 間の契約なので `workflow_version` を `2026-10-26.1` に上げ、VPS `worker/exit_codes.json` と `PIPELINE_STEPS` を同時に更新（VPS は from_step の旧名も alias で受ける）。
- 「select」工程: 段階 2a で intent は追加済みだが STEPS には無い。本段階で STEPS に入れ、`music_selection_mode` に従う既存 `app_music_select` を step 関数として接続する（human は awaiting_selection=79 で停止）。

## Global Constraints
1. 絵文字を新規に追加しない。個人情報・秘密情報をコード・テスト・ログに書かない。
2. 既存 worker API / jobs API / runs API は追加のみ。alembic はダウングレード可能。旧 worker（lease_epoch を送らない）を拒否しない。
3. テストは Python/tests（unittest、scripts/run_tests.py で skip=fail）と VPS tests（in-memory SQLite）。外部通信・本番・実フォルダを触らない。
4. commit は日本語、末尾に Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>。
5. 終了コードの意味を変えない。待機は失敗に数えない。
6. 旧 vol（.stage_state.json 無し）の完了判定を変えない（成果物だけで done）。

## Task 1（VPS）: lease_epoch
- alembic `20261026_0016_job_lease_epoch`: jobs.lease_epoch Integer NOT NULL default 0。
- worker API: lease で +1、`WorkerJobResponse.lease_epoch`、`HeartbeatRequest / CompleteRequest / FailRequest` に `lease_epoch: int | None`。`_owned_active_lease(db, worker_id, job_id, lease_epoch=None)` で不一致は 409 detail `lease_epoch_mismatch`。
- worker/client.py: lease 応答の `lease_epoch` を job dict に保持し、heartbeat / complete / fail に添える（無ければ送らない）。heartbeat の 409 は従来どおり lease_lost。
- 受入: 期限切れ→再 lease（同 worker）後に旧 epoch の complete が 409 で job が succeeded にならない。旧 worker（epoch 無し）は従来どおり通る。既存テスト全件合格。

## Task 2（Mac）: stage_state（input_hash / done_validator）
- `Python/stage_state.py`: `fingerprint_outputs(stage, folder, vol)`、`compute_input_hash(stage, folder, vol)`、`mark_done(stage, folder, vol)`、`stage_done(stage, folder, vol)`、`invalidate(stage, folder, vol)`、`load/save`（原子的置換、壊れた JSON は警告して空扱い）。done_validator 名 → 関数の表は app_orchestrator の `_ARTIFACT_DONE` を新名に改名して流用（`composite_done` / `export_done` など workflow.json の `done_validator` 名と一致させる）。
- app_pipeline: 各 step 成功後に `mark_done`。失敗時は記録を消さない（成果物が残っていても input_hash で判定）。
- app_orchestrator: `_stage_artifact_done` → `stage_state.stage_done`。
- 受入: 記録あり・入力不変 → done。前段の出力（例: music/ の mp3）を差し替える → 後段 done が失効。記録なし → 成果物だけで done。

## Task 3（Mac + VPS）: 工程名切替
- Mac: STEPS / STEPS_WITH_PLAN / STEP_LABELS / STEP_FUNCS / _RESUME_OVERRIDE / 自動再開表 / needs_render / app_orchestrator の WORKERS・AUTOPILOT_DEFAULT_STAGES・_ARTIFACT_DONE、resource_lock / parallel_guard のキー、batch_orchestrator と app.py のオーケストレーション定数（_ORCHESTRATE_PIPELINE_STEPS 等）を新名に。入力境界で `canonical_stage`。routes.json に composite / render intent を追加（旧キーは alias として残す）。premiere step 関数と `_premiere_done` を削除。select を STEPS に追加。
- VPS: `PIPELINE_STEPS` を新名に、from_step は alias 表で旧名を受けて新名に正規化して保存。`worker/exit_codes.json` と Mac `workflow.json` の workflow_version を `2026-10-26.1` に。
- 受入: `--from psd_composite` が composite から始まる。`--from premiere` が render から。API `from_step: "export"` が `render` で保存される。両 repo の workflow_version 一致テスト。既存テスト全件合格。

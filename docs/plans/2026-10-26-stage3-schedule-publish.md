# 段階 3: スケジュール正本化と投稿

仕様: docs/SPEC_REVISION_DRAFT_2026-10-25.md §3.4（受入 schedule_recovery）、§4 切替手順、§9 の合意事項（description マーカーは使わない。主系は台帳の video_id と session_ref。reconcile の直近 50 件は不在証明にしない）。
base: Mac main 2a222a7 / VPS main e5fb995。統括: Claude Fable 5.1（本人指定、包括許可）。実装: 統括セッション直接、タスクごとに Opus 5.5 サブエージェントがレビュー。
本番反映は Xserver の接続先確認まで保留。本段階は repo 統合まで。段階 3a（Chatwork）は別 branch で続けて行い、本段階では通知の接点（`notify_event`）だけ用意する。

## 前提と裁定（§8 未回答の扱い）
- §8-3（チャンネル別の公開時刻・制作開始時刻・same_day_15min）は未回答。既定値（Asia/Tokyo、12:00、先行 2 日、late_policy=next_free_slot）で実装し、チャンネル別の値は PUT API で後から設定する。same_day_15min は明示設定時のみ。
- §8-6（description の照合 ID）は §9 のとおり不採用。結果不明は保留（unknown）とし、session_ref の照会と videos.list で解決できない vol は人に渡す。
- Mac の APScheduler ジョブ（vol_create / autopilot tick / 公開ゲート / auto_resume）は本段階で **停止**（既定 OFF・公開ゲートと auto_resume は撤去）し、コードの完全除去は段階 7。
- 「meta の VPS 逆流」は「投稿記録のある vol の meta / localizations を Mac の再生成で上書きしない」と定義する（投稿後の変更は YouTube 更新経路でのみ行う）。
- upload の共通パッケージは Mac repo の `Python/publish/`（`__version__` 固定）を正本にする。VPS worker の独立した upload job（`run_upload`）は非推奨とし、投稿は pipeline の upload 工程（Mac）に一本化する。

## Global Constraints
1. 絵文字を新規に追加しない。個人情報・秘密情報（トークン、session_ref の平文）をコード・テスト・ログに書かない。session_ref は VPS で暗号化保存し、API 応答にも平文を返さない。
2. 既存 API は追加のみ（`/vols/{id}/upload-result` は video_id 必須化のみ変更）。alembic はダウングレード可能。旧 worker / 旧 Mac を 5xx にしない。
3. 過去の publishAt を送らない。フォルダ名の日付で再予約しない。結果不明のまま投稿記録を「成功」にしない。
4. テストは Mac unittest（run_tests.py、skip=fail）と VPS pytest（SQLite）。YouTube API・VPS 実機・本番 DB を触らない（すべてモック）。
5. commit は日本語、末尾に Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>。
6. 待機（時刻待ち・結果不明）は失敗に数えない。

## Task 1（VPS）: 公開スケジュール設定と 1 日 1 枠の割当
- model `PublishSchedule`（channel_id unique, timezone="Asia/Tokyo", publish_time="12:00", produce_lead_days=2, late_policy in {next_free_slot, same_day_15min}, enabled=True, revision=1, updated_at）。alembic `20261026_0017_publish_schedules`。
- service `app/services/publish_slots.py`: `get_or_default(db, channel)`、`local_datetime(date, schedule)`→aware、`to_naive_utc`、`next_free_date(db, channel, earliest)`（同チャンネルで publish_date が未使用の最初の日。archived は除外しない）、`assign_publish_slot(db, vol, schedule, *, now, earliest=None)`（vol.publish_date / scheduled_publish_at を設定。当日で時刻超過なら late_policy: next_free_slot=翌日以降の空き、same_day_15min=now+15 分）。チャンネル行を `with_for_update` で直列化。
- API（admin）: `GET /api/schedules/publish/{channel_key}`、`PUT /api/schedules/publish/{channel_key}`（全置換。body の revision が現在値と違えば 409 `revision_conflict`。応答に revision+1）。publish_time は HH:MM、timezone は zoneinfo で検証、lead_days 0〜30。
- `run_vol_create`: earliest = fire_date + produce_lead_days（schedule が無ければ従来の params.lead_days）→ `assign_publish_slot`。
- 受入: 同日 2 本起票で 2 本目が翌日の空き枠になる。時刻超過の当日は next_free_slot で翌日、same_day_15min 明示時のみ +15 分。PUT の revision 競合 409。既存テスト全件合格。

## Task 2（VPS）: 投稿記録と publish_verify
- model `Upload`（vol_id, input_hash, state in {pending, uploading, uploaded, verified, published, unknown, failed}, session_ref_encrypted|null, offset int, video_id|null, publish_at|null, reservation_verified_at|null, published_at|null, attempts, last_error|null, job_id|null, created_at, updated_at）。unique (vol_id, input_hash)。alembic `20261026_0018_uploads`。session_ref は `app/security` の既存暗号化（token_crypto）で保存。
- worker API: `POST /vols/{id}/upload/begin` {input_hash, publish_at?} → 既存記録があり video_id があれば `{mode: "update", video_id}`、同 vol に未確定（pending/uploading/unknown）の別 input_hash があれば 409 `upload_in_progress`、無ければ pending を作成し `{mode: "insert", publish_at}`（publish_at は vol.scheduled_publish_at。過去なら 409 `publish_at_in_past`）。`POST /vols/{id}/upload/session` {input_hash, session_ref, offset} → uploading。`POST /vols/{id}/upload/result` {input_hash, video_id, privacy, publish_at} → uploaded、vol.video_id / scheduled_publish_at、step upload done。`POST /vols/{id}/upload/unknown` {input_hash, error} → unknown（人待ち）。既存 `/upload-result` は video_id 必須（無ければ 422）。
- `publish_verify`（VPS 実行。scheduler の tick 内）: state uploaded の記録について YouTube `videos.list(part=status,snippet,processingDetails)` をチャンネルの access_token で読み戻し、`privacyStatus=private` かつ `publishAt` が未来かつ `channelId` 一致なら reservation_verified_at を記録し state verified、step publish_verify done。publish_at 到来後（+猶予 `PUBLISH_VERIFY_GRACE_MINUTES` 既定 30）に `privacyStatus=public` なら published_at を記録し state published、vol.published_at 更新。到来後に public でなければ `notify_event(kind="alert", ...)`（段階 3a で Chatwork 配送。本段階はログと vol.step_states の detail）。API 呼び出しは `app/services/youtube_verify.py` の関数に閉じ、テストでモック。
- meta 逆流停止: `/vols/{id}/meta` と `/localizations` は、その vol に state uploaded 以降の Upload 記録があれば 409 `meta_locked_after_upload`（`force=true` で admin のみ上書き可、監査ログ）。
- scheduler: `next_autopilot_stage` の表に `publish_verify: ("upload",)` を加えるが job は発行しない（VPS 内で実行）。
- 受入 `schedule_recovery` の VPS 分: 同 (vol, input_hash) の begin を 2 回呼んでも記録は 1 つ、video_id ありは update モード、未確定の別 hash は 409、過去 publish_at は拒否、verify のモックで verified→published の遷移と期限超過 alert。

## Task 3（Mac）: 共通 upload パッケージ・VPS 正本の予約・Mac ゲート撤去
- `Python/publish/__init__.py`（`__version__ = "2026-10-26.1"`）、`Python/publish/upload_video.py`: app_youtube から `upload_video` の本体を純関数化（`UploadRequest`/`UploadResult` dataclass、print / sys.exit / marker 書き込みを外す、resumable の session URI と offset を `on_session(session_uri, offset)` コールバックで通知、`resume_session(session_uri, offset)` で再開）、`verify_video(video_id)`、`publish_video_to_public`。app_youtube.py の CLI と既存関数はこのパッケージを呼ぶ薄い層にする（既存の戻り値・終了コードは維持）。
- `Python/publish/input_hash.py`: 動画（サイズ + mtime 秒。sha256 は上限 64 MiB まで）、サムネ（内容）、公開メタ（title / description / tags / localizations の正規化 JSON）から sha256。予約時刻は含めない。
- step_upload: VPS 設定があれば `vps_ledger_sync.upload_begin(vol, input_hash)` → update モードなら `update_video_snippet` のみ（再投稿しない）、insert モードなら VPS の publish_at で予約投稿（フォルダ名の日付は使わない。VPS 未設定時だけ従来の resolve_publish_at_iso）。セッション URI は `upload_session` で VPS に送り、途中失敗時は次回 begin の応答にある session_ref/offset で再開する（VPS が返す）。結果は `upload_result`（video_id 必須）、結果不明（insert 応答を失った等）は `upload_unknown` を送って `awaiting_human` 相当（exit 79 ではなく 1 + 通知）。youtube_upload.json はローカル互換のため従来どおり書く。
- app.py: 公開ゲート（`_job_publish_now` / `_schedule_publish` / `_recover_scheduled_publishes` / `POST /api/youtube/schedule-publish`）を撤去（API は 410 と案内文）。auto_resume（`_schedule_resume` / `_job_auto_resume`）は撤去し、失敗時の案内は `studio.py run-resume` に。`_scheduler_reload` は schedule_jobs.json のジョブを登録しない（vol_create は VPS）。`orchestrator_tick` は `APP_ORCH_TICK_ENABLED` の既定を 0 に。全ジョブ消去の不具合は、残る固定ジョブ（digest 等）を消さないように修正。
- `publish_schedule.py`: 復旧専用（plan_scheduled_publish_recovery / resolve_recover_max_stale_days / scheduler_run_at）を削除、テストも削除。`resolve_publish_at_iso` は VPS 未設定時のフォールバックとして残す。
- `vps_ledger_sync.py`: `upload_begin / upload_session / upload_result / upload_unknown / fetch_schedule`。`app_orchestrator.UploadWorker.run` も同じ経路（report_upload 未送信の修正）。
- `studio.py`: intents `schedule-put`（`--channel --publish-time --timezone --lead-days --late-policy --enabled --revision`、admin のユーザー JWT ではなく ai:runs では不可 → `schedule-put` は管理者トークン必須のため `--admin-token` 引数ではなく環境変数 `APP_ADMIN_TOKEN`）と `schedule-status --channel [--vol]`（公開予定・投稿記録の表示、ai:runs トークンで可。VPS 側に `GET /api/runs/schedule-status` を追加）。
- 受入: VPS をモックした unittest で、(a) 同 input_hash の 2 回目は update モードで insert しない、(b) 過去 publish_at は送らない、(c) 結果不明時に youtube_upload.json を「成功」として書かない、(d) 公開ゲート API が 410、(e) `_scheduler_reload` が固定ジョブを消さない、(f) run_tests.py 全件合格。

## Task 4（両 repo）: 文書と反映手順
- SPEC/OVERVIEW/runbook に段階 3 の構成（正本の所在、Mac で止めたジョブ、切替手順 §4）を反映。VPS `DEPLOY_*` に alembic 0017/0018 と `PUBLISH_VERIFY_*` 環境変数、Chatwork は 3a で追加と明記。

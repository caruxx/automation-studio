# Automation Studio 大幅修正 仕様書草案 v0.1

- 作成日: 2026-10-25
- 作成: Claude Fable 5.1（司令塔）と Codex（gpt-6.1-sol、reasoning high、read-only sandbox）の 2 ラウンド往復で策定。Codex の原案は scratchpad に保存し、本書は Claude が統合・編集した版。
- 前提: 分析レポート `docs/ANALYSIS_YOUTUBE_AUTOMATION_2026-10-25.md`。未実装・未検証。§8 はオーナー確認待ち、§9 は Claude と Codex で未合意または要検証。
- 既に実施済み: 10 チャンネル全ての `production_mode=ffmpeg_only` と `export_engine=ffmpeg`（2026-10-25、`studio.py config set --channel all`、監査ログあり）。
- 注意: 本書は秘密情報の値を含まない。`automation-studio` は public リポジトリのため、commit 前に再確認する。

## 1. 目的と確定要件（オーナー確定、2026-10-25）

1. GUI ではなく Claude Code / Codex CLI から指示して、制作から公開確認まで完了させる動線を正とする。
2. 複数 vol・複数チャンネルを並行処理する。
3. 予定どおり制作・投稿が完走し、障害後も再開できる。
4. 画像は Codex の image 2.5 で生成し、サムネ選定は Claude / Codex が行う（人のチャット往復をなくす）。
5. 投稿は YouTube Data API の予約投稿。
6. VidIQ MCP の分析をタイトル・概要欄の生成に反映する。
7. 通知は Chatwork に集約し、Discord は廃止する。
8. 楽曲の保存先・命名・履歴を統制する。
9. PSD・Premiere・AME は使わず将来も使わない。Adobe 系コードは削除し、ffmpeg 専用にする。

## 2. 用語と単一正本

- `channel_key`: チャンネルの共通 ID（Mac の `channels.json` の id と VPS の channel_key を一致させる）。
- `vol`: チャンネル内の通し番号。採番は VPS のみ。
- `run_id` / `attempt` / `lease_epoch`: 1 回の実行、その試行番号、実行権の世代。旧 lease からの報告は拒否する。
- **`workflow.json`（定義の正本）**: 工程・依存・資源・終了理由を 1 か所で定義する。`STEPS`、`routes.json` の工程部分、VPS の依存表、`_ARTIFACT_DONE` はここから生成または参照する。
- **VPS DB（状態の正本）**: run、vol、schedule、outbox。Mac は実行とフォルダ確保のみ。
- 版の照合: `workflow_version` と repo の SHA が Mac / VPS で不一致なら lease を拒否する。

### 2.1 `workflow.json` の契約

```
root:        {schema_version, workflow_version, stage_order[], resources[], stages[], exit_reasons[]}
resource:    {id, scope: machine|channel|global, capacity, rate_limit}
stage:       {id, depends_on[], executor: mac|vps, resources[], input_schema, output_schema,
              timeout_seconds, retry: {max_attempts, backoff_seconds}, done_validator}
exit_reason: {reason, exit_code, status, retryable}
```

工程順（ffmpeg 経路の実体に合わせて改名）:

```
suno → select → rename → bgimage → composite → render → qa → meta → localization → thumbnail → upload → publish_verify
```

`meta` は確定した曲順を入力にする。`select` は新設（§3.3）。`publish_verify` は新設（§3.4）。

### 2.2 共通の実行結果

```
{workflow_version, run_id, channel_key, vol, stage, attempt, lease_epoch,
 status, reason, retry_at, artifacts[]}
artifact: {path, sha256, input_hash}
```

- `status`: queued / running / succeeded / failed / cancelled / blocked。
- `blocked` の内訳（`reason`）: 資源待ち、再試行待ち、人待ち（`awaiting_human`）、AI 判断待ち（`awaiting_ai_decision`）、照合待ち、時刻待ち。**待機は失敗に数えない**（ブレーカー対象外）。
- 完了判定は「ファイルがある」ではなく「`input_hash` と `done_validator` の検証結果」で行う。入力が変わった工程の完了は失効する。
- CLI の stdout は JSON Lines、人向けログは stderr。

## 3. 段階仕様

受入条件は、隔離 venv で `python -m pytest -q -k '<受入名>'` を実行して確認する。0 件・skipped は不合格。外部 API はスタブにし、実機確認は許可済みの対象（テストチャンネル・private 投稿）だけで行う。

### 3.1 段階 1a: ffmpeg 専用化と Adobe 削除

- scope: `_export_engine()` と合成工程の既定を ffmpeg / Pillow に反転。`production_mode` は `ffmpeg_only` のみ許可（`export_engine` は読み替えて廃止）。Adobe 系モジュール一式を削除（削除直前の SHA に tag `pre-adobe-removal`）。preflight を Premiere Link 確認から ffmpeg / ffprobe / Pillow の存在確認に差し替え。
- 変更ファイル: `Python/app_pipeline.py`、`routes.json`、`settings_catalog.json`（Adobe 関連キー）、`pyproject.toml`（pymiere）、`app_premiere.py`、`app_photoshop.py`、`jsx_bundle.py`、`cep_extension/`、`uxp_plugin/`、`Script/*.jsx`、`*.epr`、`app_render_queue.py`、`routers/premiere_photoshop.py`、`web/static/js|css/assembly-premiere-layout.*`、README / OVERVIEW / skills（app-premiere、app-psd-composite、app-export）。
- CLI: `studio.py preflight --channel KEY --json`。
- 受入 `ffmpeg_only`: 10 チャンネル全てと「設定欠落」のケースで Adobe 呼び出し 0、実行環境検査合格、短尺 1 本の render と qa が合格。
- やらないこと: 既存 vol の Adobe 成果物（.prproj、PSD）の削除や再制作。

### 3.2 段階 1: 検証基盤

- scope: 依存固定（Python 3.10+ venv と lock、Pillow / numpy / playwright を本体依存へ）、テストの設定隔離（`APP_CONFIG_DIR` 等を一時ディレクトリへ）、`testpaths` 修正、両 repo に CI、終了理由の統一（`exit_reasons` を `workflow.json` から生成し、75 / 78 / 79 の衝突を発生元で正規化）。
- 変更ファイル: `pyproject.toml`、lock、`Python/tests/`、`.github/workflows/`（両 repo）、`workflow.json`、`worker/client.py`。
- 受入 `exit_contract`: pipeline / orchestrator / batch / VPS worker の 4 入口で 75 / 78 / 79 の判定が一致、待機が failed に数えられない、テスト実行で実設定・Drive・外部への書き込み 0。
- やらないこと: `print` の全面置換（制作・投稿経路の警告化だけ先行）。

### 3.3 段階 2: 状態・並列・選曲・命名

- scope: 全ての起動経路（UI、studio.py、batch、VPS worker）を VPS 台帳の run に統一。資源と lease を `workflow.json` で管理。楽曲の生成と選曲を分離。成果物の命名を単一モジュールに集約。
- 変更ファイル: `Python/studio.py`、`app_pipeline.py`、`app_orchestrator.py`、`app_music_catalog.py`、新設 `music_scoring.py`、新設 `naming.py`、`schemas/`（JSON Schema）、VPS `app/api/jobs.py`、`app/api/worker.py`、`app/models/`、alembic。
- API（VPS `/api` 配下）: `POST /runs`、`GET /runs/{id}`、`POST /runs/{id}/resume`、`POST /runs/{id}/cancel`。入力 `{request_id, channel_key, vol, workflow_version, input_revision}`。同じ `request_id` は同じ run を返す。
- 認証: 管理者が発行する長期トークン（スコープ `ai:runs`、チャンネル制限・期限・失効・監査付き）。
- CLI: `studio.py run | status | resume | cancel --json`。
- 選曲（新工程 `select`）: チャンネル設定 `music_selection_mode: ai | human`（既定 ai、§8-1）。ai は音声測定（尺、無音率、LUFS）と LLM のプロンプト適合で採点し、結果 `{clip_id, title_group_id, duration_sec, silence_ratio, lufs, prompt_fit, score, selected, reason}` を台帳に保存して自動ハート。human のみ `awaiting_human`。全曲不合格は品質待機（`blocked`）。
- 命名と保存先: vol フォルダ必須（未指定はエラー）。原曲 `original_music/{clip_id}.mp3`、採用 `music/{clip_id}.mp3`、曲名は台帳と ID3 タグに持つ。SUNO Workspace は `{channel_key}_vol{vol}` に統一し、SUNO 側の実 ID も保存。履歴は VPS の台帳を正本にし、`.suno_history.json` 2 か所は読み取りキャッシュへ。画像 `vol{N}.png`、動画 `{channel_key}_vol{N}.mp4` も `naming.py` から生成・解析する。
- 受入 `run_contract`: 2 チャンネル × 2 vol の同時実行で成果物とチャンネル指定の混線 0、SUNO 同時 1、render の並列上限遵守、同一 `request_id` の重複 run 0、旧 lease の報告拒否、入力変更で依存工程の完了が失効。
- やらないこと: 既存の原曲削除、旧音源の一括移動（§4 の移行表で扱う）。

### 3.4 段階 3: スケジュール正本化と投稿

- scope: VPS が採番・公開枠・再開・投稿状態を管理し、Mac はフォルダ確保と実行だけ。Mac の APScheduler（vol_create / autopilot / publish ゲート / auto_resume）と `_scheduler_reload` は停止・撤去。upload を Mac / VPS 共通の 1 パッケージ（`publish/upload_video.py`、版固定）にする。
- 変更ファイル: VPS `app/services/scheduler.py`、DB、`worker/client.py`、Mac `app.py`、`publish_schedule.py`、`app_reconcile.py`、`app_youtube.py`、`app_pipeline.py`（meta の VPS 逆流を停止）。
- API: `PUT /schedules/{id}`（管理者限定）。CLI: `studio.py schedule put | status --json`。
- 設定: `{channel_key, timezone, publish_time, produce_lead_days, late_policy, enabled, revision}`。既定は Asia/Tokyo、先行 2 日、`late_policy=next_free_slot`。**1 日 1 枠 / チャンネル**で、空き枠に順番に割り当てる（これが「1 本即時・残り予約」の実装）。`same_day_15min` は明示指定時のみ。
- 投稿の冪等性: 冪等キー = (`channel_key`, `vol`, `input_hash`)。`input_hash` は動画・サムネ・公開メタから計算し、予約時刻は除外。同 vol の未確定送信は排他。既存 `video_id` があれば更新のみ。
- 投稿記録: `{input_hash, state, session_ref, offset, video_id, publish_at, reservation_verified_at, published_at}`。`session_ref`（resumable session URI）は保護して保存し、結果不明時はセッション照会で再開する。
- `publish_verify`（新工程）: upload 後に `private` と未来の `publishAt`、投稿先チャンネル、処理状態を API で読み戻す。`publish_at` 到来後に `privacyStatus=public` を別途照合して記録。時刻待ちは lease を解放する。
- 受入 `schedule_recovery`: 再起動・応答喪失・日跨ぎで欠落 0 と二重投稿 0、meta 逆流 0、期限超過の検知と Chatwork alert。
- やらないこと: 過去 `publishAt` の送信、フォルダ名の日付による再予約、Mac 側の公開ゲート、結果不明のままの INSERT。

### 3.5 段階 3a: Chatwork 通知

- scope: 通知を VPS DB の outbox に集約し、Discord を廃止。Mac 単独時はローカル SQLite に退避し、同じ `event_id` で VPS へ移管（自動昇格はしない）。
- 変更ファイル: 新設 `notify.py`（Mac / VPS 共通）、outbox テーブル、既存の Discord 呼び出し元すべて（`app_notify.sh` を含む）。
- 契約: `{event_id, run_id, kind, room_id, body, attempt, next_retry_at, sent_at}`。`kind` は info / action_required / alert。action_required と alert だけ [To] 付き。
- 受入 `notify_recovery`: 429 と再起動後の再送、重複抑制、通知障害でも制作が継続、結果不明の重複可能性を記録。
- やらないこと: Chatwork の返信による選曲・選定、通知障害を理由にした再投稿、VPS の SMTP（パスワードリセット等）の変更。

### 3.6 段階 5: サムネ AI 選定

- scope: 生成 → 採点 → Claude / Codex 選定 → 採用 を必須工程にする。既存の `app_thumbnail_scoring` / `judge_auto_approval` / `app_image_eval_loop` を再利用し、既定 ON・再生成最大 3 回。
- 変更ファイル: `codex_imagegen.py`（モデル設定化、run 別出力ディレクトリ）、`app_thumbnail_state.py`、`app_image_eval_loop.py`、新設 `app_image_compositor` のコンタクトシート出力、`routes.json`。
- CLI: `studio.py thumbnail-select --channel KEY --vol N --json`。
- 契約: `{candidate_ids, candidate_hashes, selected_id, rejected_reasons, model, backend, selection_revision}`。run / attempt 別に保存。全不合格は品質待機。
- 画像モデル: チャンネル設定 `image_model`。実利用確認が済むまで `gpt-image-2` のまま。Codex が提示した image 2.5 の公式 ID は本書では未検証（§9）。
- 受入 `thumbnail_selection`: 他 run や候補外の画像採用 0、「画像が存在するだけ」での skip 0、投稿されたサムネと採用画像のハッシュ一致。
- やらないこと: 黙ってのモデル代替、予測 CTR を実測として扱うこと。

### 3.7 段階 6: VidIQ 連携

- scope: VidIQ MCP から取得した分析を VPS の `insights` に `source=vidiq` で正規化して保存し、`claude_proposer` と VPS proposer のメタ生成に注入。鮮度 24h。取得不可でも制作・投稿は止めない。
- 変更ファイル: 新設 `vidiq_adapter.py`（MCP クライアント）、VPS `app/api/insights.py` と service、`claude_proposer.py`、`worker/proposer.py`、schemas。
- CLI: `studio.py insights-refresh --channel KEY --json`。
- 契約: insights `{source, video_ids, fetched_at, expires_at, metrics, evidence_ids}`、meta `{title, description, tags, evidence_ids, insights_degraded, degraded_reason}`。
- 受入 `vidiq_degraded`: 期限切れ・取得失敗で `insights_degraded=true` と info 通知を出して完走。分析不足時はチャンネル方針（persona・言語）だけで生成。
- やらないこと: VidIQ の認証・無人接続・費用の推定実装、根拠のない evidence の生成。

## 4. 既存データの移行

- 棚卸しから移行表 `{channel_key, vol, old_path, canonical_path, workspace_id, clip_id, sha256, decision}` を作る。所属が曖昧なファイルは隔離フォルダに移し、名前だけで所属を決めない。
- 既存 vol フォルダは移行表で参照し、公開日で改名しない。Adobe 成果物は完了判定に使わない。
- 既存の投稿は `video_id` で照合して台帳に取り込み、再投稿・選曲の変更はしない。
- 正本の切り替え手順: Mac / VPS の起票と送信を停止 → 実行中 lease の完了または明示停止 → DB バックアップ → 未完了 run・予約・outbox を VPS へ移管 → repo / DB / worker の版照合 → 1 チャンネルで試行 → VPS を有効化。復旧時も先に送信停止と台帳移管を行い、起票元は常に 1 つにする。

## 5. リスクと対策

- 認証失効（SUNO ログイン、YouTube OAuth）: 該当チャンネルだけ `awaiting_human` にし、他チャンネルは継続。
- 保存先不在・Drive 同期遅延: 実体（sha256）を検証してから再開。
- 品質待機・quota 待機: 永続化し、再起動で消えないようにする。
- 外部送信の exactly-once は保証できない: 冪等キー、セッション照会、照合で「二重より保留」を選ぶ。
- 実装の統制: 共通規則（AI_WORKFLOW.md local-v1.7）に従い、原本の照合値不一致を解消してから、単一 writer・1 branch・独立両レビュー（Opus 5.5 中、Sol 6.1 medium）・許可済みの本番確認を行う。

## 6. 実装順序（段階案との対応）

| 順 | 段階 | 並行可否 |
|---|---|---|
| 1 | 0: 保全（Drive 監査資料の private 退避、未統合 branch の判断、public repo の P0 と個人情報除去） | 先行。10/26 期限 |
| 2 | 1a: ffmpeg 専用化 | 0 の後、単独 |
| 3 | 1: 検証基盤 | 1a と並行可 |
| 4 | 2: 状態・並列・選曲・命名 | 1 の後 |
| 5 | 3 / 3a: スケジュール正本化 / Chatwork | 2 の後。3a は 2 と並行可 |
| 6 | 5 / 6: サムネ AI 選定 / VidIQ | 2 の後。互いに並行可 |
| 7 | 7: 整理（app.py・index.html 分割、未使用コードの archive） | 随時 |

段階 2〜3 は同じファイル群に集中するため直列・単一 writer。

## 7. 検証の原則

- 各受入名を pytest で実行し、コマンド・環境・SHA・結果を PR / Issue に記録する。
- 実機確認（SUNO、YouTube）はテストチャンネルと private 投稿に限定し、実施者・環境・SHA・結果を記録する。
- CI は両 repo で head SHA に対して実行し、skipped / neutral は合格としない。

## 8. オーナー確認事項（決めてほしいこと）

1. **AI 選曲を既定**にしてよいか（人のハート選定はチャンネル別の任意設定にする）。
   **回答済み（2026-10-25）**: AI 選曲にする。ハートが付いていない場合は全曲を AI 側で選曲する。ハートが付いている曲は人の採用として尊重する。`(1)` などの連番サフィックス付きタイトルはリネームを実施する（段階 2 の `select` と `naming.py` に反映）。
2. 選曲の単位は「各タイトル 1 テイク」か「タイトルに関係なく上位 N 曲」か。N はいくつか。
3. チャンネル別の公開時刻・制作開始時刻と、`same_day_15min` を許可するチャンネルはあるか。
4. image 2.5 の利用経路（Codex CLI か OpenAI API か）と正式モデル ID、1 vol あたりの生成費用上限。
5. 楽曲・画像が全て不合格のとき、何回再試行してから人に渡すか（既定案: 3 回）。
6. YouTube の description 末尾に照合用の ID を記載してよいか。不許可なら、結果不明の vol は保留扱いでよいか。

## 9. Claude と Codex の未合意・要検証事項

- **上位 N 曲選定と既存の ready 契約の矛盾**（Codex 指摘、`app_music_catalog.py:229-232`）: 現状は「全タイトル各 1 テイク」が ready 条件。`selected` / `excluded` の状態を台帳に追加して契約を変える必要がある。Claude は採用方向。
- **description の不可視マーカー**（Claude 提案 → Codex 反論）: 保存往復と表示が未検証。主系は台帳の `video_id` と `session_ref` とし、マーカーはオーナー許可と往復検証後の補助とする。Claude は Codex 案を採用。
- **reconcile の直近 50 件は不在証明にならない**（Codex 指摘、`app_reconcile.py:178`）: `video_id` → セッション照会 → 送信時刻を覆う走査、の順で照合し、タイトルは候補抽出にだけ使う。不明なら送信保留。Claude は採用。
- **Mac SQLite 通知退避の VPS への昇格**: 自動昇格は不採用、明示的な移管のみ。Claude は採用。
- **image 2.5 の正式モデル ID**: Codex は `gpt-image-2.5-sunburst` / `gpt-image-2.5-flare` を挙げたが、本書では未検証。実利用確認（§8-4）後に確定。
- **音声評価の実装**: 画像用 Vision 採点は楽曲の代替にならない（Codex 指摘）。`music_scoring.py` は ffprobe / ffmpeg の loudnorm・silencedetect と LLM のプロンプト適合を組み合わせる方向で、詳細は段階 2 の設計時に決める。

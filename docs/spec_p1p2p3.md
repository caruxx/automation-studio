# 仕様書 — P1〜P3 改善計画（ノータッチ・複数チャンネル並列・自走運用）

最終更新: 2026-05-03
対応コミット: roaming-crane（[`compiled-roaming-crane.md`](../../.claude/plans/compiled-roaming-crane.md)）

## 1. 設計の前提

| 軸 | 値 | 影響 |
|---|---|---|
| チャンネル数 | 6+ | render queue + スロット配分が必須 |
| 自動化レベル | 完全自走 | QA / plan 自動採択 / token health 監視を組み込み |
| Mac/Premiere 依存 | 受け入れる | 1 worker 固定。脱却プロジェクトは未着手 |

主目的:
> APScheduler に登録するだけで、各チャンネルが独立して
> `plan → suno → rename → premiere → export → qa → meta → thumbnail → upload → 公開ゲート`
> まで完走し、例外時のみ Discord で運営者に通知する状態を保証する。

---

## 2. パイプライン全体像

```
[scheduler] vol_create
    ↓
[ledger]  start_run('vol_create', channel_id, vol)
    ↓
app_pipeline.py (subprocess, 子プロセス)
    APP_CHANNEL_ID / APP_CHANNEL_FOLDER / APP_NO_INTERACTIVE / APP_USE_RENDER_QUEUE=1 の env を継承
    ↓
    [STEPS]  plan  →  suno  →  rename  →  premiere*  →  export*  →  qa  →  meta  →  thumbnail  →  upload  →  publish-gate
                                ※* render queue 経由（serialized 1 worker）
    ↓
    成功なら ledger.finish_run('done', exit_code=0)
    失敗なら _parse_failed_stage() → auto_resume 判定 → DateTrigger で N 分後に再投入
    ↓
[publish gate]  upload 直後 /api/youtube/schedule-publish に登録 → publish_delay_hours 経過後 public 化
```

### 2.1 ステージ一覧

| 順 | stage | 主な処理 | 失敗時の `--from` 候補 |
|---|---|---|---|
| 0 | plan | `app_competitor.propose_suno_prompt` + benchmark 3 軸 → `plan.json` + 品質スコア | plan |
| 1 | suno | `suno_auto_create.py`（Playwright） | suno |
| 2 | rename | `app_process_tracks.py`（ffmpeg リネーム + ラウドネス調整） | rename |
| 3 | premiere | render queue 経由で `app_premiere.py` | premiere |
| 4 | export | render queue 経由で AME 書き出し | export |
| 5 | qa | `ffprobe` で aspect / 解像度 / 尺 / コーデック検証（オプションでラウドネス） | premiere |
| 6 | meta | `claude_proposer.propose_titles/description/tags` | meta |
| 7 | thumbnail | Flow / Codex 並列で `<vol>/thumbnail.png` 生成（既存があればスキップ） | thumbnail |
| 8 | upload | `app_youtube.upload_video` + 公開ゲート登録 | upload |

`STEPS` / `STEP_LABELS` / `STEP_FUNCS` / `RETRY_POLICY` の 4 箇所を変更する場合は **必ず一貫して**更新（前例: P2-5 の step_thumbnail）。

---

## 3. Sentinel exit code 表

子プロセス（`app_pipeline.py` / `suno_auto_create.py` / `app_youtube.py`）がパイプライン全体で共有する終了コード。

| code | 意味 | 上位の処理 | Discord 通知絵文字 |
|---|---|---|---|
| `0` | 成功 | 次の stage へ | （なし） |
| `1` | 一般失敗 | RETRY_POLICY に従い必要なら retry。auto_resume 対象 | ❌ |
| `75` | unattended_login | 即座に通知して停止（auto_resume 対象外） | 🔐 |
| `76` | retryable | 指数バックオフで retry（plan 2x、meta 2x、upload 3x） | 🔁 |
| `77` | quota_exhausted | 即座に通知して停止（auto_resume 対象外） | 📊 |
| `78` | preflight_fail | pipeline 開始前に失敗。step は実行しない | ⚠️ |

すべてのコードは [`Python/app_pipeline.py`](../Python/app_pipeline.py) 冒頭で定義され、子モジュールと一致させる。

---

## 4. 環境変数（実行時オーバーライド）

| 変数 | 用途 | 既定 |
|---|---|---|
| `APP_NO_INTERACTIVE` | 無人モード（`input()` / `sleep(3600)` 禁止） | unset |
| `APP_CHANNEL_ID` | 子プロセスに registry 経由のチャンネル指定 | unset |
| `APP_CHANNEL_FOLDER` | 子プロセスにチャンネルフォルダパス指定 | unset |
| `APP_CHANNEL_NAME` | 表示用 channel name | unset |
| `APP_USE_RENDER_QUEUE` | premiere/export を render queue 経由でシリアライズ | `0` |
| `APP_RETRY_DISABLE` | stage retry を全停止（開発用） | unset |
| `APP_PREFLIGHT_DISABLE` | preflight チェック skip | unset |
| `APP_RENDER_QUEUE_DISABLE` | worker thread 起動抑止 | unset |
| `APP_THUMBNAIL_PROVIDERS` | サムネ生成 provider | `flow`（`flow,codex` で並列） |
| `APP_THUMBNAIL_DISABLE` | step_thumbnail をスキップ | unset |
| `APP_QA_DISABLE` | step_qa をスキップ | unset |
| `APP_QA_LOUDNESS` | ラウドネス測定を有効化（重い） | unset |
| `APP_PLAN_AUTO_ADOPT_THRESHOLD` | 採択スコア閾値 | `7.0` |
| `APP_PLAN_MAX_ATTEMPTS` | plan 再生成上限 | `3` |
| `APP_YT_DAILY_QUOTA_CAP` | YouTube 日次クオータ上限 | `9600` |
| `APP_YT_QUOTA_PER_UPLOAD` | upload 1 件あたりコスト | `1600` |
| `APP_YT_QUOTA_WINDOW_HOURS` | quota 集計ウィンドウ | `24` |
| `APP_TOKEN_HEALTH_WARN_DAYS` | token 警告閾値 | `7` |
| `APP_RUN_LEDGER_STALE_SEC` | ledger stale 判定 | `21600`（6h） |
| `APP_RENDER_QUEUE_STALE_SEC` | render queue stale 判定 | `7200`（2h） |

---

## 5. データストア

| 名前 | パス | 用途 | 初期化 |
|---|---|---|---|
| **dashboard_config** | `~/.config/{app_id}/dashboard_config.json` | UI のアクティブビュー（**job 実行時の正典ではない**） | UI から PUT |
| **channels.json** | `~/.config/{app_id}/channels.json` | 全チャンネルの正典 registry | UI > チャンネル管理 |
| **per-channel** | `<channel_folder>/.app_channel_config.json` | persona / publish_delay_hours / template* など PER_CHANNEL_KEYS | UI 自チャンネル分析軸 |
| **schedule_jobs** | `~/.config/{app_id}/schedule_jobs.json` | APScheduler 登録ジョブ | UI > 自動実行スケジュール |
| **runs.db** (P3-1) | `~/.config/{app_id}/runs.db` | **中央 ledger**（履歴 + auto_resume チェーン） | 起動時 init |
| **render_queue.db** (P2-1) | `~/.config/{app_id}/render_queue.db` | premiere/export ジョブキュー | 起動時 init |
| **YouTube quota** (P1-4) | `<channel_folder>/.youtube_quota.json` | per-channel 直近 24h 消費記録 | upload 時に追記 |
| **YouTube token** | `<channel_folder>/.youtube_token.json` | per-channel OAuth | 初回認証 |
| **benchmark concept** | `~/.config/{app_id}/benchmark/concept.json` | コンセプト軸 aggregate | UI > ベンチマーク > コンセプト |
| **benchmark title** | `~/.config/{app_id}/benchmark/title.json` | タイトル軸 aggregate | UI > ベンチマーク > タイトル |
| **benchmark thumbnail** | `~/.config/{app_id}/benchmark/thumbnail.json` | サムネ軸 aggregate + picked | UI > ベンチマーク > サムネ分析 |
| **upload marker** | `<vol_folder>/youtube_upload.json` | upload 後の video_id, scheduled_publish_at, published_at | upload 完了時 |

---

## 6. 主要 API

### 6.1 中央 ledger（P3-1）

| エンドポイント | 用途 |
|---|---|
| `GET /api/runs/active` | 全チャンネル × 直近 5 vol の状態（artifact 推論） |
| `GET /api/runs/ledger?channel_id=&status=&kind=&vol=&limit=` | ledger 履歴 |
| `GET /api/runs/ledger/stats?days=7` | 集計（成功率 / 平均所要 / auto_resume チェーン数） |
| `GET /api/runs/ledger/{run_id}` | 単一 run 詳細 |
| `GET /api/runs/ledger/{run_id}/chain` | auto_resume チェーン全体 |
| `POST /api/runs/ledger/migrate` | 既存 vol を reconstructed として取込（既定 dry-run） |
| `POST /api/runs/ledger/reap` | stale in_progress を failed に降格 |

### 6.2 render queue（P2-1）

| エンドポイント | 用途 |
|---|---|
| `GET /api/render-queue?status=&limit=` | ジョブ一覧 |
| `GET /api/render-queue/throughput?days=7` | スループット統計（理論 capacity 含む） |
| `POST /api/render-queue/enqueue` | 内部投入 |
| `POST /api/render-queue/{id}/cancel` | pending を取消 |
| `POST /api/render-queue/reap` | stale running を error に降格 |

### 6.3 公開ゲート（P2-7）

| エンドポイント | 用途 |
|---|---|
| `POST /api/youtube/schedule-publish` | private upload 後 `delay_hours` で public 化を予約 |
| `POST /api/youtube/publish-now/{video_name}` | 手動で即時 public |

### 6.4 token health（P3-5）

| エンドポイント | 用途 |
|---|---|
| `GET /api/token-health` | 全チャンネル + Playwright を観測 |
| `POST /api/token-health/notify` | 即時に Discord 通知（cron 動作確認用） |

### 6.5 schedule（P2-3 / P2-4 / P2-6）

| エンドポイント | 用途 |
|---|---|
| `GET /api/schedule/jobs?channel_id=` | チャンネル別フィルタ。`channel_id=__none__` で未指定のみ |
| `POST /api/schedule/jobs` | upsert。`balance_slots`（既定 true）で 30 分単位自動分散、`auto_resume` で失敗時再投入 |
| `DELETE /api/schedule/jobs/{id}` | 削除 |
| `POST /api/schedule/run-now/{id}` | 即時実行 |
| `GET /api/schedule/history` | 直近 50 件履歴（ledger と独立） |

### 6.6 ベンチマーク 6 軸

| 軸 | エンドポイント | 出力 |
|---|---|---|
| concept | `POST /api/benchmark/concept/run` / `GET /api/benchmark/concept` | themes / emotional_jobs / scene_anchors |
| title | `POST /api/benchmark/title/run` / `GET /api/benchmark/title` | patterns / formulas / keywords / hooks |
| thumbnail | `POST /api/benchmark/thumbnail/run` / `GET /api/benchmark/thumbnail` | composition / palette / picked refs |
| audience | （未実装） | demographics / psychographics / use_cases |
| needs | （未実装） | viewer_needs / underserved / emerging |
| music | （未実装） | genres / mood / bpm / instrumentation |

---

## 7. JOB_HANDLERS（APScheduler）

| type | ハンドラ | 説明 |
|---|---|---|
| `vol_create` | `_job_vol_create` | 次の vol 番号を解決して fully-auto pipeline を起動 |
| `spot_create` | `_job_spot_create` | 指定 vol を fully-auto で再起動 |
| `benchmark_refresh` | `_job_benchmark_refresh` | competitor analysis 再取得 |
| `export_window` | `_job_export_window` | AME watcher の ON/OFF 切替 |
| `publish_now` | `_job_publish_now` | 公開ゲートの DateTrigger（動的登録） |
| `token_health` | `_job_token_health` | トークン期限チェック + 警告通知 |

`vol_create` / `spot_create` は registry 経由で `--channel-id` を子プロセスに渡し、グローバル設定を**書き換えない**（P1-3）。

---

## 8. 中央 ledger スキーマ

```sql
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL UNIQUE,    -- ULID 風（時刻順ソート可）
    kind            TEXT    NOT NULL,           -- vol_create | spot_create | auto_resume | manual | reconstructed
    channel_id      TEXT,
    channel_folder  TEXT    NOT NULL,
    channel_name    TEXT,
    vol             INTEGER NOT NULL,
    video_name      TEXT,
    parent_run_id   TEXT,                       -- auto_resume の元 run
    parent_job_id   TEXT,                       -- scheduler ジョブ id
    from_stage      TEXT,                       -- pipeline `--from <stage>` で開始した場合
    status          TEXT    NOT NULL,           -- in_progress | done | failed | cancelled | reconstructed
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    duration_sec    INTEGER,
    exit_code       INTEGER,
    failed_stage    TEXT,
    summary         TEXT,
    artifact_video_id TEXT,                     -- 完了時の YouTube video_id
    meta_json       TEXT
);
```

`get_run_chain(run_id)` で auto_resume の祖先 → 子孫を時系列で取得可能。

---

## 9. 失敗復旧フロー

```
pipeline 失敗 (exit != 0)
    ↓
_parse_failed_stage(stdout) で `--from <stage>` を抽出
    ↓
_should_auto_resume(returncode, stage, attempt, job)
    - exit 75/77/78 → 拒否（手動介入）
    - max_attempts 到達 → 拒否
    - auto_resume=False → 拒否
    - else → 許可
    ↓
_schedule_resume(job, stage, attempt+1)
    APScheduler DateTrigger(now + delay_min) で _job_auto_resume を登録
    ledger に parent_run_id を持つ新 run を記録
    ↓
[delay_min 経過]
    ↓
_job_auto_resume(payload)
    app_pipeline.py --auto --from <stage> --channel-id <id>
    成功なら ledger.finish_run('done')
    再失敗なら ↑ をループ（max_attempts まで）
```

Discord 通知:
- 1 回目失敗: `❌→🔄 [Channel] vol.78 失敗（suno）、30 分後に自動再投入`
- 上限到達: `⛔ [Channel] vol.78 再投入打ち切り: 再投入上限到達 (3/3)`
- 手動介入要: `🔐 [Channel] vol.78 の SUNO 楽曲生成 が中断しました\n原因: ブラウザの手動ログインが必要…`

---

## 10. UI（変更点）

### ダッシュボードページ（`#p-dashboard`）
- **全チャンネル稼働状況**カード（P1-7）— チャンネル別グリッド + vol 進捗 + quota バー + 公開予定（`⏰ N時間後`）+ ▶ 即時公開ボタン
- **Render Queue** 折りたたみカード（P2-1）— pending / running / throughput / 理論 capacity / 直近ジョブ表

### マスタ設定ページ
- **自動実行スケジュール**（P2-3 / 2-4 / 2-6） — チャンネル別フィルタ select、衝突回避トースト、`🔄 auto-resume` バッジ、新規追加フォームに resume チェック + 遅延 + 試行回数

### 自チャンネル設定（ベンチマークページ内）
- **公開ゲート**フィールド（P2-7） — `publish_delay_hours`（時間単位、0=即時）

### ベンチマークページ
- 「コンセプト」「タイトル」「サムネ分析」の 3 タブ（P2-2 で並列実装済）

---

## 11. 既知の制約

| 項目 | 制約 | 緩和策 |
|---|---|---|
| Premiere 1 セッション | Mac 1 台で並列起動不可 | render queue で serialize（P2-1） |
| Playwright プロファイル | Mac ローカル、Drive 同期されない | 各 PC で初回ログイン必要、token health で先回り通知（P3-5） |
| YouTube quota | デフォルト 10,000 unit/day（≈6 upload） | local 推定 + EXIT_QUOTA_EXHAUSTED で 24h 待ち（P1-4） |
| 公開ゲート | app.py 再起動で DateTrigger 消失 | 起動時に `youtube_upload.json` を scan して再登録（P2-7） |
| ledger と外部世界の整合 | YouTube 側で削除されても ledger は残る | 手動 `POST /api/runs/ledger/reap` で stale 回収 |

---

## 12. 関連ドキュメント

- [運用テスト & スモークテスト](operation_test.md)
- [トラブル対応 runbook](runbook.md)
- [ロードマップ計画書](../../.claude/plans/compiled-roaming-crane.md)

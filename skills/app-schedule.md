# app-schedule: スケジュール自動実行（APScheduler）

> **段階 3（2026-10-26）**: 採番・公開枠・投稿状態の正本は VPS に移った。Mac の `vol_create` は
> `POST /api/worker/vols/allocate` で番号と公開日を受け取ってフォルダを作り、pipeline を実行する（VPS 未設定なら従来の採番）。
> VPS 側の `scheduled_tasks` は Mac のフォルダを作らないので、同じチャンネルで両方を有効にしない。Mac 側の公開ゲート
> （`/api/youtube/schedule-publish`、起動時の予約復旧）と auto_resume（N 分後の自動再投入）は撤去した。
> 公開予定は `studio.py schedule-status` / `schedule-put`、再開は `studio.py run --vol N --from <stage>` /
> `run-resume --run-id`。`orchestrator_tick` は既定 OFF（`APP_ORCH_TICK_ENABLED=1` で従来どおり）。
> 詳細: `docs/SPEC_REVISION_DRAFT_2026-10-25.md` §3.4、`docs/plans/2026-10-26-stage3-schedule-publish.md`。

cron 風のスケジュール管理で、PC を起動しておくだけで定期ジョブが走る。
`AsyncIOScheduler` がサーバー起動時に立ち上がり、`~/.config/{app_id}/schedule_jobs.json` を読んで登録する。

## 使い方

### UI 経由
「⚙ マスター設定」→「⏱ 自動化スケジュール」セクション → 「+ ジョブを追加」で登録。
一覧には次回実行時刻 + 最終実行ステータスが表示される。

### 実行履歴
直近 50 件の start/done/error を `/api/schedule/history` で返却。UI の「📜 実行履歴」アコーディオンから参照可能。

### 通知（段階 3a）
ジョブ完了・失敗は [Python/notify.py](../Python/notify.py) が VPS の outbox に送り、VPS が Chatwork に配送する。Discord Webhook は廃止。VPS に届かないときは `~/.config/automation-studio/notify_queue.sqlite` に退避し、`studio.py notify-flush` で移管する。

## 4 つのジョブ種別

| type | 用途 | 追加パラメータ |
|------|------|--------------|
| `vol_create` | 次の vol を **フル自動**で作る（plan → suno → rename → bgimage → psd_composite → export → qa → meta → localization → thumbnail → upload の全工程） | `channel_id`（複数チャンネル運営時、省略可 = アクティブを使う） |
| `benchmark_refresh` | ベンチマーク分析を実行 → キャッシュ更新 | なし |
| `export_window` | （廃止・互換のため残置）旧 AME ウォッチャーの切替。2026-10-25 段階 1a 以降は効果なし | `action`: `"on"` or `"off"` |
| `spot_create` | 指定 vol を **1 回限り**で作る | `vol`（必須）, `channel_id`（省略可） |

## トリガー形式

### cron（定期）
```json
{
  "kind": "cron",
  "day_of_week": "mon,fri",    // * or カンマ区切り
  "hour": 9,
  "minute": 0
}
```

または crontab 式:
```json
{"kind": "cron", "expr": "0 7 * * *"}
```

### date（1 回限り）
```json
{
  "kind": "date",
  "run_date": "2026-05-01T15:00:00"
}
```

主に `spot_create` で使う。

## ジョブ例

### 毎週月・金 9:00 に次の vol をフル自動生成

```bash
curl -X POST http://localhost:8888/api/schedule/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "vol_create",
    "name": "週2ペース自動生成",
    "trigger": {"kind":"cron","day_of_week":"mon,fri","hour":9,"minute":0}
  }'
```

実行時の流れ:
1. `channel_folder` を scan → 最大 vol 番号 +1
2. `python3 app_pipeline.py {next_vol} --from-benchmark --auto` を起動
3. plan.json 生成 → SUNO → リネーム → 画像 → FFmpeg 書き出し → メタ → アップロード
4. 成功/失敗を Discord 通知 + `/api/schedule/history` に記録

### 毎朝 7:00 にベンチマーク分析リフレッシュ

```bash
curl -X POST http://localhost:8888/api/schedule/jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "benchmark_refresh",
    "name": "朝7時分析",
    "trigger": {"kind":"cron","hour":7,"minute":0}
  }'
```

v3: ライバル優先 → スプシフォールバックで `run_full_analysis()` を実行 → `analyze_with_claude` で日本語/英語ハイブリッド JSON を生成 → `~/.config/{app_id}/competitor_analysis_cache.json` 更新（`language: ja-en-mix`、`prompt_version: 4`）。

### （廃止）深夜のみ AME 書き出しを ON にする

> 2026-10-25 段階 1a で AME ウォッチャーは削除済み。以下は履歴として残す。

```bash
# 23:00 に ON
curl -X POST http://localhost:8888/api/schedule/jobs \
  -d '{"type":"export_window","name":"深夜ON","action":"on","trigger":{"kind":"cron","hour":23,"minute":0}}'

# 7:00 に OFF
curl -X POST http://localhost:8888/api/schedule/jobs \
  -d '{"type":"export_window","name":"朝OFF","action":"off","trigger":{"kind":"cron","hour":7,"minute":0}}'
```

演算リソースを夜間に集中させられる。

### スポットで vol.100 を明日 15:00 に作る

```bash
curl -X POST http://localhost:8888/api/schedule/jobs \
  -d '{
    "type":"spot_create",
    "name":"vol.100 記念回",
    "vol":100,
    "channel_id":"your_channel_id",
    "trigger":{"kind":"date","run_date":"2026-05-01T15:00:00"}
  }'
```

### 複数チャンネル対応

`channel_id` を指定すると、ジョブ実行直前にアクティブチャンネルを切り替えてから pipeline を起動する。

```json
{
  "type": "vol_create",
  "name": "ch-A 月曜",
  "channel_id": "ch_a",
  "trigger": {"kind":"cron","day_of_week":"mon","hour":9,"minute":0}
}
```

```json
{
  "type": "vol_create",
  "name": "ch-B 金曜",
  "channel_id": "ch_b",
  "trigger": {"kind":"cron","day_of_week":"fri","hour":9,"minute":0}
}
```

SUNO ブラウザはシングルインスタンスなので、複数チャンネルの楽曲生成を**同時刻**にしない運用が必須（書き出しは FFmpeg で CPU 律速）。

## API リファレンス

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/schedule/jobs` | 全ジョブ + 次回実行時刻 + `scheduler_active` |
| POST | `/api/schedule/jobs` | 追加 / 更新（id 指定で upsert） |
| DELETE | `/api/schedule/jobs/{id}` | 削除 |
| POST | `/api/schedule/run-now/{id}` | 今すぐ実行（トリガーを待たない） |
| GET | `/api/schedule/history` | 直近 50 件の実行ログ |

## 実装

| ファイル | 役割 |
|---------|------|
| [Python/app.py](../Python/app.py) | `_scheduler` (AsyncIOScheduler), `_scheduler_reload()`, 4 種ハンドラ, 5 つの API |
| `~/.config/{app_id}/schedule_jobs.json` | ジョブ永続化（サーバー再起動時に自動復元） |

### 依存

```bash
pip3 install --user apscheduler
```

未インストールだと `[scheduler] apscheduler 未インストール` と起動時に表示され、
スケジュールは無効化される（基本機能には影響なし）。

### タイムゾーン

`AsyncIOScheduler(timezone="Asia/Tokyo")` で JST 固定。cron の `hour` もそのまま JST。

### `@app.on_event("startup")` フック

起動時に:
1. `AsyncIOScheduler` 起動
2. `schedule_jobs.json` を読み込み
3. `enabled: true` のジョブを全て登録
4. 登録数をログ出力: `[scheduler] started (N jobs registered)`

### 追加・削除時の自動リロード

`POST /api/schedule/jobs` や `DELETE` のたびに `_scheduler_reload()` が走り、
APScheduler に再登録する（古いジョブを remove → 新規 add）。

## 実行履歴

メモリ上に最新 50 件保持（`_scheduler_history`）。永続化はしていない（次期改善点）。

| フィールド | 説明 |
|-----------|------|
| `job_id` | ジョブ ID |
| `status` | `started` / `done` / `error` |
| `detail` | メッセージ（200 文字で truncate） |
| `at` | UTC ISO8601 |

## 設計原則

- **ジョブ重複起動防止**: `misfire_grace_time=300` で 5 分以内の重複を抑制
- **失敗時リカバリー**: Discord 通知 + 履歴に error 記録 → 次回実行は通常通り
- **サーバー再起動で復元**: `schedule_jobs.json` に永続化しているため、PC 再起動後も同じジョブが走る
- **認証ありのリモート起動**: Cloudflare Tunnel + 認証ミドルウェアと組み合わせて、スマホから出先で緊急スポット実行可能（[app-remote-access.md](./app-remote-access.md)）

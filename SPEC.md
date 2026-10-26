# Automation Studio 仕様書

クリエイター向け動画制作自動化システム。
SUNO 楽曲生成 → 画像合成 → FFmpeg 書き出し → YouTube アップロードまでを一つの Web UI と CLI から連動実行する。
**単一の Web ダッシュボード (localhost:8888) で 11 工程管理**する。

---

## 1. システム概要

### 目的
AI が生成した BGM を編集・投稿する YouTube チャンネル運営を、以下の原則で最大限自動化:

- **API を使わない** - Claude CLI を subprocess で呼び出し、JSON 単一オブジェクトで往復
- **ファイル中心の契約** - 動画フォルダ内の決まったファイル名でステート管理（DB 不要）
- **1 動画 = 1 フォルダ** - 命名規則 `{連番}_{prefix}_{YYMMDD}` で進捗もフォルダを見れば分かる

### 技術スタック

| 層 | 技術 |
|---|---|
| バックエンド | FastAPI (Python 3) + Uvicorn |
| フロントエンド | 静的 HTML + Vanilla JS（ビルド不要） |
| ブラウザ自動操作 | Playwright (sync API, persistent profile) |
| 書き出し | FFmpeg（`app_ffrender.py`。静止画ループ + 音声 + SRT + タイムコード） |
| サムネ合成 | Pillow（`app_image_composite.py`） |
| YouTube アップロード | YouTube Data API v3 + OAuth |
| AI | Claude CLI（第一候補、API 未使用）/ Gemini / ChatGPT |
| 通知 | Chatwork（VPS の outbox 経由。段階 3a。Mac は `notify.py` が VPS に送り、届かなければローカル退避） |

---

## 2. アーキテクチャ

```
┌──────────────────────────────────────────────────────────────┐
│                    Web Dashboard (localhost:8888)             │
│  ┌──────────────┐  ┌──────────────────────────────────────┐  │
│  │   Sidebar    │  │ Pages: ダッシュボード / コンテンツ     │  │
│  │ 📊 🎬 📅 🎵    │  │        / SUNO / YouTube / ライブ        │  │
│  │  📤 🔔 ⚙️    │  │        / 通知 / 設定                   │  │
│  └──────────────┘  └──────────────────────────────────────┘  │
└────────────────────┬─────────────────────────────────────────┘
                     │ HTTP / WebSocket
          ┌──────────▼──────────────────────┐
          │  FastAPI: app.py (組み立て+runtime core)│
          │  + app_core.py (共通土台)             │
          │  + routers/ (benchmark/images/        │
          │     scene_text/youtube/analytics/live) │
          └────┬───────┬───────────────────────┘
               │       │
       ┌───────▼──┐  ┌─▼──────────────────┐
       │ 子プロセス │  │ファイル IO           │
       │ (Popen)  │  │ (~/.config/{app_id}/ や │
       └─┬──┬──┬──┘  │  動画フォルダ)       │
         │  │  │    └─────────────────────┘
    ┌────▼┐┌▼┐ └─────────┐
    │SUNO ││ffrender    │YouTube
    │gen  ││(ffmpeg)    │upload
    └──┬──┘└─────┬──────┘└────┬─────┘
       │         │             │
 ┌─────▼─┐  ┌────▼─────┐  ┌────▼──────┐
 │Playwright  │ffmpeg/ffprobe │ google-api │
 │→suno.com   │+ Pillow       │-python-client│
 └─────┬─┘  └──────────┘  └─────┬─────┘
       │                         │
 ┌─────▼──────┐               ┌──▼──────────┐
 │ Claude CLI │               │ YouTube API │
 │ (API未使用) │               │     v3      │
 └────────────┘               └─────────────┘
```

---

## 3. ファイル配置

### リポジトリ構造

```
_claude/                         # プロジェクトルート
├── SPEC.md                      # 本文書
├── CLAUDE.md                    # Claude Code 向け共通指示
├── Python/                      # サーバー・CLI スクリプト
│   ├── app.py                   # FastAPI 組み立て役 + runtime core（middleware / startup hooks / worker 基盤[export・render queue / APScheduler / 公開ゲート]）※D9 で 12,649→7,783行
│   ├── app_core.py              # 共通土台（パス/設定/config ローダ/共有可変グローバル/タスク・subprocess・youtube queue ヘルパ/共有定数）※D9 抽出
│   ├── routers/                 # ドメイン別 APIRouter（D9 段階分割）
│   │   ├── benchmark.py         #   ベンチ分析軸（thumbnail/concept/title/description）
│   │   ├── images.py            #   codex 画像/背景/channel-thumbnail/サムネ承認/series
│   │   └── youtube.py           #   YouTube API/upload/履歴/説明文/通知
│   ├── start.sh                 # 起動スクリプト
│   ├── setup.sh                 # 依存パッケージ導入
│   ├── suno_auto_create.py      # SUNO 自動生成 + workspace + DL（fetch インターセプタ）
│   ├── app_process_tracks.py   # 楽曲リネーム + ffmpeg 後処理（サムネ/ペルソナ）
│   ├── app_sheets.py  # Google スプシ CSV 取得 + パース + マッチング
│   ├── claude_proposer.py       # Claude CLI 提案ヘルパー（タイトル/説明/タグ）
│   ├── app_ffrender.py         # FFmpeg 書き出し（唯一の書き出し経路。静止画ループ + 音声 + SRT + TC）
│   ├── app_youtube.py   # YouTube 投稿
│   ├── app_notify.sh      # 通知の互換ラッパー（notify.py を呼ぶ。Discord は廃止）
├── web/static/
│   └── index.html               # ダッシュボード UI（全 JS 同梱）
└── skills/                      # 個別スキル解説 (Claude Code 用)
    ├── app-workflow.md
    ├── app-web-dashboard.md
    ├── app-create-folder.md
    ├── app-rename-audio.md
    ├── app-ai-propose.md
    ├── app-image-select.md
    ├── app-export.md
    ├── app-youtube-desc.md
    ├── app-youtube-upload.md
    └── app-notify.md
```

### 設定ファイル (`~/.config/{app_id}/`)

```
dashboard_config.json    # チャンネル名、フォルダパス、ペルソナ、ライバル URL
suno_config.json         # LLM プロバイダー、モデル、プロンプト、Claude CLI パス
channels.json            # 複数チャンネル切替用（v2: icon_cache 付き）
benchmark_config.json    # ベンチマーク設定（v2: チャンネル横断で共通利用）
master_prompts.json      # v2: プロンプト上書き（8 種。空でハードコードにフォールバック）
schedule_jobs.json       # v2: APScheduler ジョブ定義（cron / date トリガー）
export_rules.json        # 書き出し先ルール（互換のため保持。AME ウォッチャーは 2026-10-25 段階 1a で廃止）
auth_token.txt           # v2: リモートアクセス用トークン（600 perms）
prompts.json             # SUNO プロンプトライブラリ
youtube_client_secret.json   # Google OAuth クライアントシークレット
youtube_token.json       # OAuth 認証トークン（自動更新）
discord_config.json         # 旧 Discord Webhook（段階 3a で送信経路は廃止。設定 UI の残骸は段階 7 で整理）
task_history.json        # タスクログ永続化（サーバー再起動で復元）
competitor_analysis_cache.json  # Claude 分析結果キャッシュ
chromium_profile/        # Playwright 永続プロファイル（SUNO ログイン情報）
```

### 動画フォルダの中身（1 動画 = 1 フォルダ）

```
{num}_{prefix}_{YYMMDD}/               # 例: 77_vol_260415
├── {prefix}_vol{num}.mp4         # 書き出し済 MP4
├── .ffrender_manifest.json       # 書き出し構成の正本
├── vol{num}.jpg                    # YouTube サムネイル
├── サムネイル.jpg                  # サムネ別名
├── *.png / *.jpg                   # 背景画像候補
├── selected_images.json           # 画像選択結果
├── music/                          # 処理済 MP3
├── original_music/                 # SUNO DL 直後のオリジナル MP3
├── subtitles_{num}.srt             # 実測 SRT 字幕
├── music_time_code_info_{num}.txt  # タイムコード情報
├── youtube_title.txt               # YouTube タイトル（Claude 提案 or 手動）
├── youtube_description.txt         # YouTube 説明文
├── youtube_tags.txt                # YouTube タグ（改行 or カンマ区切り）
└── youtube_upload.json             # アップロード完了マーカー
```

---

## 4. ワークフロー（11 工程）

```
[plan] → SUNO生成 → リネーム → 背景画像 → 画像合成 → 配置(自動通過) → FFmpeg書き出し → QA → 動画メタ → 多言語化 → AIサムネ → アップロード
```

工程の真実源は `app_pipeline.py` の `STEPS`。`--from-benchmark`（無人運用）時は先頭に `plan` が付き 12 工程（`STEPS_WITH_PLAN`）。

| # | step | UI 起点 | 完了判定ファイル | 担当スクリプト |
|---|------|---------|----------------|--------------|
| 0 | `plan` | （無人運用時のみ） | `plan.json` | app_orchestrator.py / app_pipeline.py `step_plan` |
| 1 | `suno` | SUNO タブ / 楽曲タブ | `music/*.mp3` が 1 本以上 | suno_auto_create.py |
| 2 | `rename` | 楽曲タブ | `music/*.mp3`（処理済み） | app_process_tracks.py |
| 3 | `bgimage` | 画像タブ | `vol{N}.png` or `vol{N}.jpg` | app_image_prompt.py / codex_imagegen.py |
| 4 | `psd_composite` | 画像タブ（Image2 Layout Studio） | `vol{N}.jpg` + `サムネイル.jpg` | app_image_composite.py（Pillow） |
| 5 | `premiere` | （自動通過） | — | app_pipeline.py `step_premiere`（何もしない） |
| 6 | `export` | 編集タブ | `{prefix}_vol*.mp4` + `subtitles_*.srt` | app_ffrender.py |
| 7 | `qa` | （書き出し直後・自動） | QA 全 4 軸 pass | app_pipeline.py `step_qa` |

> **書き出しエンジン** — 2026-10-25 段階 1a 以降は FFmpeg（`app_ffrender.py`）のみ。`production_mode` は `ffmpeg_only` だけで、旧値 `standard` / `export_engine=ame` は警告付きで読み替える。
>
> **レンダーマニフェスト `.ffrender_manifest.json`（構成の正本）** — vol フォルダに保存。曲順・使用画像・尺・音声設定・焼き込み設定を固定する。`--swap '旧=新.mp3'` / `--remove '曲名'` で構成を差し替え、`--show-manifest` で表示、`--no-render` で manifest 操作だけ行う。
>
> **曲名焼き込み（`--burn-titles always`）** — 各曲の区間に曲名を**常時焼き込み**（drawtext、画像境界∪曲境界で分割→GOP→TS 連結）。書式は per-channel `.app_channel_config.json` の `burn_titles` ブロック（`font`/`fontsize`/`color`/`prefix`/`x`/`y`/`border`）で定義。mov_text ソフト字幕も併存。
| 8 | `meta` | 詳細タブ | `youtube_description.txt` + `youtube_tags.txt` | claude_proposer.py |
| 9 | `localization` | （自動） | 多言語メタ（非 fatal） | app_pipeline.py `step_localization` |
| 10 | `thumbnail` | 画像タブ | `thumbnail.png` / `vol*.jpg` / `サムネイル.jpg` | app_channel_thumbnail.py（画像合成が使えない時のフォールバック） |
| 11 | `upload` | アップロードタブ | `youtube_upload.json` | app_youtube.py |

判定ロジックは [index.html](web/static/index.html) の `WORKFLOW_STEPS` 定数、自動実行順は `app_pipeline.py` の `STEPS` / `STEP_LABELS` / `STEP_FUNCS` / `RETRY_POLICY`（4箇所一貫更新）で定義。

---

## 5. API リファレンス

### 動画フォルダ管理

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/videos` | 一覧（title, has_*, publish_date 等を vol 降順で返却） |
| POST | `/api/videos/create` | `{publish_date}` → フォルダ自動生成 |
| GET | `/api/videos/{name}/detail` | 詳細（readiness, upload_info 等） |
| GET/PUT | `/api/videos/{name}/title` | `youtube_title.txt` |
| GET/PUT | `/api/videos/{name}/tags` | `youtube_tags.txt` |

### AI 提案（Claude CLI, API 未使用）

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/videos/{name}/suggest` | body: `{mode: "titles"\|"description"\|"tags", count?, reference?}` |

### 画像選択（書き出し連動）

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/videos/{name}/images` | `.jpg/.png` 一覧 + 選択状態 |
| GET | `/api/videos/{name}/image-file/{fn}` | プレビュー配信 |
| PUT/DELETE | `/api/videos/{name}/selected-images` | `selected_images.json` 保存/削除 |

### SUNO

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/suno/start` | `{prompt, count, interval, provider, video_name?, workspace?, batch?}` |
| POST | `/api/suno/stop` | 停止 |
| GET | `/api/suno/status` | `{running, progress, logs, meta}` |
| POST | `/api/suno/download` | `{video_name or workspace}` → Workspace 楽曲を動画フォルダ直下に DL |

### 楽曲（メディアプレイヤー / いいね / 削除 / 後処理）

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/videos/{name}/tracks` | MP3 一覧（root/music/original_music + z プレフィックス解析） |
| GET | `/api/videos/{name}/track-file/{rel_path}` | audio/mpeg 配信 |
| POST | `/api/videos/{name}/track-like` | `{rel_path, delta}` でリネーム（いいね数 = z の数） |
| DELETE | `/api/videos/{name}/track?rel_path=...` | 物理削除 |
| POST | `/api/videos/{name}/process-tracks[?rename_only=true]` | リネーム（サムネ or ペルソナ）+ ffmpeg 後処理 |
| GET | `/api/process/status` | 後処理ジョブのログ |

### 書き出し（FFmpeg）

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/videos/{name}/run-pipeline` | `{steps:["export"]}` で `app_ffrender.py` 書き出し（Premiere / AME の API は 2026-10-25 段階 1a で廃止） |
| GET/PUT | `/api/export/rules` | 書き出し先ルール（互換） |

### YouTube

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/youtube/upload` | `{video_name, title?, privacy, schedule?, tags?}`（未指定はファイル自動読込） |
| GET | `/api/youtube-desc/references` | 過去説明文の参考リスト |
| POST | `/api/youtube-desc/save` | 説明文保存 |

### YouTube Analytics

YouTube Analytics API v2 (`reports.query`) をチャンネル別 OAuth トークン（`<channel_folder>/.youtube_token.json`）で呼び出す。必要 scope は `https://www.googleapis.com/auth/yt-analytics.readonly`。既存トークンに scope が無い場合は OAuth フローを起動せず、`auth.needs_reauth=true` で返す。取得結果は `<channel_folder>/.youtube_analytics.json` に 12 時間キャッシュし、`refresh=true` で強制再取得する。API 失敗時も古いキャッシュがあれば `auth.error` と `stale=true` を付けて返す。

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/analytics/channel/{channel_id}?days=28&refresh=false` | 日別 `views/estimatedMinutesWatched/averageViewDuration/subscribersGained/subscribersLost/likes` と 7 日比較 summary |
| GET | `/api/analytics/overview?days=28&refresh=false` | 全チャンネルの summary + auth 状態 |
| GET | `/api/analytics/signals` | `views_drop` / `no_recent_upload` / `needs_reauth` / `analytics_error` の要対応シグナル |
| GET | `/api/analytics/setup` | 全チャンネルの Analytics 認証状態一覧と、ブラウザ OAuth 再認証ボタンを表示する軽量 HTML |
| POST | `/api/analytics/reauth/{channel_id}` | 対象チャンネルの OAuth 再同意フローを開き、`app_youtube.py` の `SCOPES` で `<channel_folder>/.youtube_token.json` を再発行 |

### ライブ配信（VPS / ffmpeg 24-7 マルチストリーム）

データモデル: **group**（= YouTube チャンネル / プレフィックス例 orzz, sk）の下に複数の配信ストリーム（`orzz_1, orzz_2, …`）。動画プールは VPS の `videos/<group>/` を共有。1配信 = 1 ストリームキー = 1 screen セッション `ytlive_<id>`。

| メソッド | パス | 用途 |
|---------|------|------|
| GET/PUT | `/api/live/config` | VPS 接続情報 + streams 設定（key マスク・`live_config.json` は**共有ドライブ config/**＝PC 間共有 600・旧 `~/.config/<app_id>/` ローカル置き場と旧 channels 形式は自動マイグレーション・削除分は VPS 後始末） |
| POST | `/api/live/setup` | 初期設定一括: SSH 鍵生成→（パスワード時）鍵登録→接続テスト→ffmpeg/screen 導入→`vps_live/` スクリプト配置→env 同期 |
| GET | `/api/live/status` | VPS 負荷（load/mem/disk/送信Mbps）+ グループ別容量(group_disk) + 配信別稼働（uptime/残り時間/playlist_idx/再起動回数） |
| POST | `/api/live/streams/{id}/start・stop・restart` | 配信制御 |
| POST | `/api/live/streams/{id}/swap?next=1` | **無停止差し替え**: ffmpeg のみ再起動→同一ライブ継続。next=1 でプレイリスト次の動画へ |
| GET | `/api/live/streams/{id}/log` | ffmpeg ログ tail |
| GET | `/api/live/viewers?force=1` | 同時視聴者数（boundStreamId でキー⇔broadcast 紐付け。≈3 quota/グループ・5 分キャッシュ） |
| GET | `/api/live/local-videos` / `/api/live/local-thumbnails` | ローカル候補（チャンネルフォルダの mp4 / jpg・png） |
| POST/GET | `/api/live/upload` / `/api/live/upload/{job_id}` | scp 転送（グループのプールへ）/ 進捗 |
| GET/DELETE | `/api/live/remote-videos` | プール一覧（used_by 付き）/ 削除（使用中ガード） |
| GET/PUT | `/api/live/broadcasts` | YouTube ライブのタイトル/説明（stream_id 指定・該当 broadcast を自動マーク） |
| POST | `/api/live/thumbnail` | ライブのサムネイル設定（thumbnails.set・50 quota） |

> 詳細: [skills/app-live-streaming.md](skills/app-live-streaming.md)。実体は [Python/app_live.py](Python/app_live.py)（SSH 制御）+ [Python/routers/live.py](Python/routers/live.py)。VPS 側 `stream.sh` は PLAYLIST 巡回（`ROTATE_SECONDS` 毎に timeout → 次の動画）・`MAX_SECONDS` 自動打切り・クラッシュ自動再起動を担う。

### サムネ / 背景画像 / 文字入れ（画像合成）

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/videos/{name}/run-pipeline` | `{steps:[...]}` で工程選択実行。**サムネ制作 = `["bgimage","psd_composite"]`** |
| POST | `/api/bgimage/run` | `{video_name, ref_count, force}` → ベンチ参照で背景 `vol{N}.png` 生成 |
| GET | `/api/bgimage/reference-dir/list` / POST `/dry-run` | 参照画像フォルダのプレビュー / 選択ソース確認 |
| POST | `/api/image-compositor/render-dual-thumbnail` | AI 背景 + シーンテキストで `vol{N}.jpg` + `サムネイル.jpg` を 2 枚出し（Pillow） |
| POST | `/api/image-compositor/save-canvas` / GET,POST `/template` | 画像エディタの保存・テンプレート |
| POST | `/api/scene-text/generate` | `{video_name}` → AI 画像を Vision 分析しシーンテキスト 1 件生成（チャンネル設定 `scene_text_*` 準拠） |
| POST | `/api/scene-text/suggest-from-benchmark` | `{mode:"titles"\|"vision", count}` → ベンチ（ライバル）からトーン/例/禁止語を提案 |
| POST | `/api/channel-thumbnail/start` | （フォールバック）Vision 駆動の AI サムネを `{vol}/Image/` に一括生成 + スコアリング |

> サムネの**正規フロー** = `bgimage`(背景・ベンチ参照) → `psd_composite`(画像合成・Pillow)。`channel-thumbnail`（AI 直接生成）は合成が使えない時のフォールバック。

### システム

| メソッド | パス | 用途 |
|---------|------|------|
| GET/PUT | `/api/config` / `/api/config/dashboard` / `/api/config/suno` | 設定読み書き |
| POST | `/api/config/init` / `/api/config/auto-detect` | 初回セットアップ支援 |
| GET | `/api/schedule` | カレンダー用イベント |
| GET | `/api/channels` / PUT `/api/channels/active/{id}` | チャンネル切替 |
| POST | `/api/finder/open` / `/create-folder` / `/delete-folder` | OS 連携 |
| POST | `/api/notify/discord` | 通知送信（名前は互換。送信先は VPS outbox → Chatwork） |
| POST | `/api/notify/line` | 同上（旧互換 alias） |
| WS | `/ws/logs/{task_id}` | ライブログ（suno / youtube / setup） |

---

## 6. データ契約

### 6.1 Claude CLI 出力（単一 JSON）

| 用途 | スキーマ |
|------|---------|
| SUNO (styles_title_only) | `{"title":str, "styles":str}` — styles に BPM・楽器・構造ヒント含む |
| SUNO (lyrics_styles) | `{"title":str, "styles":str, "lyrics":str}` — lyrics に [Intro][Verse] 等 |
| SUNO (lyrics) | `{"title":str, "lyrics":str}` |
| SUNO (batch) | `{"songs":[{title,styles,lyrics?},...]}` — N 曲分を 1 回で取得 |
| タイトル候補 | `{"titles":[str,...]}` |
| 説明文 | `{"description":str}` |
| タグ | `{"tags":[str,...]}` |
| 楽曲リネーム | `{"titles":[str,...]}` |

パース: コードフェンス除去 → `{...}` 抽出 → `json.loads()` → 失敗時は末尾カンマ除去リトライ。
実装は [claude_proposer._extract_json_object](Python/claude_proposer.py) / [suno_auto_create._extract_json_object](Python/suno_auto_create.py)。

### 6.2 `selected_images.json` (画像選択 → 書き出し)

```json
{
  "main": "bg_golden_hour.jpg",
  "sub": ["bg_city.png", "bg_night.png", "bg_rain.png"]
}
```

- `main` → 0-5s に配置
- `sub[0]` → 5-30s に配置（なければ main 再利用）
- `sub[0..N-1]` → 30-End を N 等分
- 実ファイル存在チェックを通ったもののみ採用
- どちらも見つからなければ `vol{N}.png` / `vol{N}-1.png` にフォールバック

### 6.3 `youtube_upload.json` (アップロード完了マーカー)

```json
{
  "video_id": "dQw4w9WgXcQ",
  "url": "https://youtu.be/dQw4w9WgXcQ",
  "title": "Elegant Lounge Music ...",
  "privacy": "unlisted",
  "schedule": "2026-04-15T09:00:00Z",
  "uploaded_at": "2026-04-15T10:32:11.123456"
}
```

ダッシュボードはこのファイルの存在で「アップロード済」を判定。

**段階 3（2026-10-26）**: このマーカーはローカル互換用の写しで、投稿状態の正本は VPS の投稿記録（`uploads`: 冪等キー
`vol × input_hash`、state、video_id、publish_at、reservation_verified_at、published_at）。`schedule` は VPS の予約時刻
（naive UTC に `Z`）で、フォルダ名の日付からは再予約しない。VPS に投稿記録がある vol は再投稿せず snippet 更新のみ。

### 6.4 `youtube_tags.txt`

改行 or カンマ区切りテキスト。空白はトリム、空要素は除外。
未保存時は既定タグ（BGM, Lounge, Chill, Relax, Study, Work, AI Music, SUNO）にフォールバック。

---

## 7. 主要コンポーネント

### 7.1 SUNO 自動生成 ([Python/suno_auto_create.py](Python/suno_auto_create.py))

- **Workspace 管理**: `/me/workspaces` で `{channel}_vol{N}` を確保（`New Workspace` → `placeholder="Untitled Workspace"` → `Create Workspace`）
- **生成モード**: `styles_title_only` / `lyrics_styles` / `lyrics`
- **プロバイダー**: `claude` (CLI) / `gemini` / `chatgpt`
- **一括生成モード** (`--batch`): Claude CLI 1 回で N 曲分取得 → LLM 往復時間を圧縮
- **楽曲ダウンロード** (`--download-workspace NAME --download-dir DIR`): 生成済 MP3 を一括 DL
- **進捗トラッキング**: ログから `曲 N/M` / `次の生成まで X 秒` を抽出 → `/api/suno/status` で Web 側に可視化

### 7.2 Claude CLI 提案 ([Python/claude_proposer.py](Python/claude_proposer.py))

`claude -p "<prompt>"` を subprocess 起動、単一 JSON を返させる。
- `propose_titles(count=5)` → タイトル候補
- `propose_description(reference=...)` → 既存説明文を参考に新規作成
- `propose_tags()` → タグ配列
- `gather_context(folder)` → 動画フォルダから楽曲名・公開日・現タイトル等を自動抽出

### 7.3 FFmpeg 書き出し ([Python/app_ffrender.py](Python/app_ffrender.py))

1. `.ffrender_manifest.json`（曲順・画像・尺・焼き込み設定）を作る、または既存を読む
2. 音声: `music/*.mp3` を concat → フェード → リミッター（音声キャッシュあり）
3. 映像: 背景画像を 1 GOP だけエンコードし `-stream_loop` + `-c copy` で目標尺まで連結（任意で曲名焼き込み）
4. SRT とタイムコードを [srt_timecode.py](Python/srt_timecode.py) の純関数で生成
5. `--swap` / `--remove` で構成の差し替え（著作権対応）、`--no-render` で manifest 操作だけ

### 7.4 YouTube アップロード ([Python/app_youtube.py](Python/app_youtube.py))

OAuth 認証 → MP4/サムネ/説明/タグ/タイトルを**保存ファイルから自動読込** → `videos().insert()`。
成功後 `youtube_upload.json` を書き出し → Web 側で即ステータス反映。

---

### 7.5 投稿の正本と公開スケジュール（段階 3、[Python/publish/](Python/publish/)）

- 投稿本体は `Python/publish/upload_video.py`（純関数。resumable upload のセッション URI と offset を VPS に保存し、
  途中失敗は次回 `resume`、再開セッションの失効だけを「結果不明」として人に渡す）。`app_youtube.py` はその薄い層。
- 冪等キーは `(channel_key, vol, input_hash)`。`input_hash` は動画（サイズ + 先頭/末尾 4 MiB）、サムネ、公開メタから作り、
  予約時刻は含めない（`Python/publish/input_hash.py`）。
- VPS の API: `POST /api/worker/vols/{id}/upload/begin | session | result | unknown`、
  `GET /api/runs/schedule-status`、`GET/PUT /api/schedules/publish/{channel_key}`（管理者）。
- 公開枠は **1 日 1 枠 / チャンネル**。VPS の `publish_schedules`（timezone / publish_time / produce_lead_days /
  late_policy / enabled / revision）で決め、`publish_verify`（VPS の tick）が `private + 未来の publishAt` と、
  到来後の `public` を YouTube から読み戻して記録する。期限超過は alert（配送は段階 3a の Chatwork）。
- Mac の APScheduler の `vol_create` は VPS の `allocate`（採番 + 公開枠）を受けてフォルダを作り pipeline を実行する。
  公開ゲートと auto_resume は撤去。CLI: `studio.py schedule-status [--vol N]`、`schedule-put`（管理者 JWT を
  環境変数 `APP_VPS_ADMIN_TOKEN` で）。

## 8. ワークフロー例: vol.77 の新規制作

```
1. コンテンツ → 「+ 新規動画」→ 公開日入力
   → 77_vol_260416 フォルダ 自動生成

2. vol.77 をクリック → 楽曲タブ
   → Workspace: vol_vol77 バッジ表示
   → プロンプト確認（SUNO 設定のプリセット自動入力）
   → [✓] 一括生成モード → ▶ 生成開始
   → Playwright が /me/workspaces で vol_vol77 作成 → /create?wid=* → 20 曲生成

3. 数分～数十分後、⬇ Workspaceの楽曲をDL
   → original_music/ に MP3 集約

4. rename_music.sh 経由で app-rename-audio 実行
   → サムネ画像から Claude CLI が英語タイトル提案 → MP3 リネーム
   → FFmpeg で無音トリム + 8 秒フェードアウト → music/ に配置

5. 画像タブで背景画像をメイン/サブ選択 → 保存

6. 編集タブ → 尺を確認 → `python3 app_pipeline.py 77 --only export`（または run-pipeline で export）
   → app_ffrender.py が音声/字幕/画像を一括配置して MP4・SRT・タイムコードを生成

7. 詳細タブ → ✨ AI提案 ×5 → タイトル選択 → 保存
   → ✨ AI提案 （説明文） → 採用
   → ✨ AI提案 （タグ） → 全部採用 → タグ保存

8. アップロードタブ → 公開予約日時設定 → ▶ アップロード実行
   → youtube_upload.json マーカー書出 → ステッパー 6/6 ✓
```

---

## 9. セットアップ

```bash
# 1. 依存パッケージ
cd Python && bash setup.sh

# 2. 設定初期化
curl -X POST http://localhost:8888/api/config/init   # 起動後

# 3. OAuth 認証
python3 app_youtube.py --auth-only

# 4. 手動配置が必要なもの
~/.config/{app_id}/youtube_client_secret.json    # Google Cloud Console から取得
~/.config/{app_id}/discord_config.json              # Discord Webhook（通知用）

# 5. 起動
bash Python/start.sh                          # http://localhost:8888/
```

---

## 10. 設計原則 / 制約

- **API 未使用** (Claude に関して): PATH 上の `claude -p` を subprocess 起動。APIキー管理不要で認証ローカル完結
- **DOM に依存しない**: Playwright は `get_by_role` / `get_by_placeholder` / `locator().filter(has_text=)` を優先。CSS ハッシュ（emotion `css-XXXXXX`）は最終フォールバック
- **ファイル = 真実**: DB を持たずフォルダ内ファイルの存在だけでステートを表現 → Google ドライブで同期すれば複数端末でそのまま共有可能
- **Claude CLI エラー時フォールスルー**: Workspace 作成失敗や DOM 不一致は警告ログのみでクラッシュさせない。楽曲生成自体は続行
- **進捗はログパース**: SUNO/YouTube のサブプロセス stdout を `task_logs` に蓄積し、正規表現で `current/total/phase` を抽出
- **タスク履歴永続化**: 完了ログを `~/.config/{app_id}/task_history.json` に保存 → サーバー再起動後も復元
- **SPA ナビゲーション**: History API (`pushState`/`popstate`) で URL hash を管理 → ブラウザの戻る/進む/リロード/直アクセスに対応
- **コンテンツフィルター**: 全て / 進行中 / 完了済み の 3 モード + テキスト検索 → 大量動画（74+件）の管理に対応

---

## 11. 外部依存

| 対象 | 連携方法 | 失敗時挙動 |
|------|---------|----------|
| SUNO (suno.com) | Playwright で DOM 操作 | DOM 変更時は警告ログ。手動フォールバック可 |
| Claude CLI | subprocess + JSON | JSON 抽出失敗時はリトライ（30秒後） |
| Gemini / ChatGPT | urllib HTTP JSON | API キー必須 |
| ffmpeg / ffprobe | 子プロセス | 不在は preflight（exit 78）で停止 |
| Pillow | Python ライブラリ | 不在は preflight（exit 78）で停止 |
| YouTube Data API v3 | google-api-python-client + OAuth | token 失効時は自動 refresh |
| Chatwork（VPS outbox） | HTTP POST（VPS → Chatwork API） | VPS 未設定時はローカル退避（`studio.py notify-flush` で移管） |

---

## 12. スキル索引（詳細は各 md 参照）

| スキル | 役割 |
|--------|------|
| [app-workflow.md](skills/app-workflow.md) | 全体フロー |
| [app-web-dashboard.md](skills/app-web-dashboard.md) | Web UI / API 全体像 / ステータスオーバーレイ |
| [app-create-folder.md](skills/app-create-folder.md) | フォルダ生成 |
| [app-rename-audio.md](skills/app-rename-audio.md) | 楽曲リネーム + 音声処理（サムネ/ペルソナ・rename-only モード） |
| [app-suno-download.md](skills/app-suno-download.md) | SUNO Workspace 一括 DL（fetch インターセプタ方式） |
| [app-track-player.md](skills/app-track-player.md) | 楽曲メディアプレイヤー + いいね + 削除 |
| [app-ai-propose.md](skills/app-ai-propose.md) | Claude CLI JSON 提案パターン |
| [app-image-select.md](skills/app-image-select.md) | 画像選択 |
| [app-export.md](skills/app-export.md) | FFmpeg 書き出し（app_ffrender） |
| [app-youtube-desc.md](skills/app-youtube-desc.md) | 説明文生成 |
| [app-youtube-upload.md](skills/app-youtube-upload.md) | YouTube 投稿 |
| [app-competitor-spreadsheet.md](skills/app-competitor-spreadsheet.md) | スプシ競合分析（195ch + 成長 → Claude → 提案）|
| [app-notify.md](skills/app-notify.md) | 通知（Chatwork、VPS outbox） |
| [app-master-config.md](skills/app-master-config.md) | マスター設定タブ（プロンプト上書き / SUNO / ベンチマーク / 書き出し / リモート / 入出力。Flow・メタ・master_settings は D8/D12 で撤去） |
| [app-schedule.md](skills/app-schedule.md) | **v2** APScheduler 統合（vol_create / benchmark_refresh / export_window / spot_create） |
| [app-remote-access.md](skills/app-remote-access.md) | **v2** 外出先スマホ操作（Cloudflare Tunnel + 認証ミドルウェア + PWA） |
| [app-imitate-evolve.md](skills/app-imitate-evolve.md) | **v2** 徹底パクリ進化分析 + ベンチマーク参照サイドパネル（投稿時刻ヒートマップ / タグ頻出） |

---

## 13. 変更履歴

- **2026-10-26 段階 3**: スケジュール正本化と投稿。投稿記録（VPS `uploads`）と `publish_verify`、1 日 1 枠の公開枠、
  共通 upload パッケージ `Python/publish/`、Mac の公開ゲート / auto_resume / 起票の停止。

- **2026-10-25（段階 1a）**: Adobe（Premiere / Media Encoder / Photoshop）経路を削除し、`production_mode` を `ffmpeg_only` のみに統一。`/api/premiere/*`、`/api/photoshop/*`、`/api/render-queue*`、`/api/export/queue*`、`/api/templates/list` を廃止。scene-text API は `/api/scene-text/generate` へ移動。`studio.py preflight` を追加。

- **2026-04-15**: 仕様書初版作成
- **2026-04**: Phase A/B/C + D 完了（YouTube Studio 風 UI、AI 提案、画像選択、アップロード統合）
- **2026-04**: SUNO Workspace 自動作成 + 楽曲 DL + 一括生成モード追加
- **2026-04**: JSX 画像なし alert 廃止 + `--images-only` モード追加
- **2026-04**: SUNO DL を fetch インターセプタ方式に刷新（userscript 準拠）
- **2026-04**: 楽曲タブにメディアプレイヤー + ♥いいね（z プレフィックス）+ 🗑削除を追加
- **2026-04**: 後処理スクリプト `app_process_tracks.py` 新設。サムネ → ペルソナ のフォールバック対応、`--rename-only` モード追加
- **2026-04**: Playwright 自動操作ブラウザに処理内容のステータスオーバーレイを表示
- **2026-04-17**: History API 導入（ブラウザ戻る/進む/URL 直アクセス対応）
- **2026-04-17**: コンテンツ一覧にフィルター（全て/進行中/完了済み）+ テキスト検索
- **2026-04-17**: グローバル進捗バー（トップバー、SUNO 実行中のみ表示）
- **2026-04-17**: ダッシュボードからの動画詳細直接遷移
- **2026-04-17**: タスク履歴永続化（`task_history.json`）
- **2026-04-17**: SUNO Workspace セレクタ修正（DOM 診断 → exact=False + 部分一致）
- **2026-04-17**: Google スプレッドシート競合分析統合（195ch + 54ch 成長データ → API quota ゼロ）
- **2026-04-24 v2 リリース**: 4 領域を刷新
  - **設定の一元化** — `/api/config` 統合スキーマ、`benchmark_config.json` 分離、運営チャンネル管理 UI（YouTube URL → icon 自動取得）、ベンチマーク StatCard + ホットチャンネル行（最新動画サムネ）、`app_pipeline.py` に `--from-benchmark` + `step_plan`、AME 書き出しキュー（ウォッチャー + ファイルサイズ安定化検知）
  - **ベンチマーク詳細化** — 動画詳細メタタブの「ベンチマーク参照」サイドパネル（適用中ベンチマーク / 投稿時刻ヒートマップ / タグ頻出）、「徹底パクリ進化」3 軸分析（✓パクる/✗避ける/+進化）
  - **日本語化** — `analyze_with_claude` / `propose_suno_prompt` / `propose_flow_prompt` / `claude_proposer` のプロンプトに日本語出力指示、SUNO/Flow プロンプト本文は英語仕様を維持、rationale のみ日本語化、キャッシュ削除 API + 「日本語で再生成」ボタン
  - **リモート + スケジュール** — 認証ミドルウェア（`ORZZ_AUTH_REQUIRED=1`、127.0.0.1 はスキップ）、APScheduler 統合（cron / date トリガー、Discord 通知連動）、Cloudflare Tunnel セットアップスクリプト、PWA（manifest.json + sw.js + login.html）、600px モバイルブレークポイント
  - **マスター設定タブ新設** — サイドバーに「⚙ マスター設定」追加、9 セクション（プロンプト管理 / SUNO 詳細 / Flow 生成 / メタ生成 / ベンチマーク / スケジュール / 書き出し / リモートアクセス / インポート・エクスポート）、`master_prompts.json` でハードコードプロンプト上書き可、`master_settings.json` で Flow `DEFAULT_COUNT` 等を上書き
- **2026-06-03**: サムネ制作フロー1本化 + 文字入れのチャンネル別設定化
  - **UI 統合** — 背景生成の入口（6 経路）を正規フロー1本に集約。各動画「画像」タブに一気通貫 CTA「🎯 サムネを制作（背景→文字入れ）」、一覧ツールバーに「🎯 サムネ一括制作」。Vision AI 一括（`channel-thumbnail`）を「PSD 合成のフォールバック」へ降格、独立「Photoshop」ナビ/ページ/孤立 JS を撤去
  - **文字入れのチャンネル別化** — `scene_text` 生成（`_generate_scene_copy_en` / `scene_text_generator.py`）の Harbor Notes 流用を撤去。per-channel 設定 `scene_text_enabled/tone/examples/forbidden/structure`（空なら persona 中立）。`POST /api/scene-text/suggest-from-benchmark`（titles=タイトル語彙 / vision=ライバルサムネの実焼込文字 Vision 抽出）でベンチから提案、基本設定に UI カード追加
  - **PSD 解決の堅牢化** — `step_psd_composite` の vol 固有 PSD 名をゼロ埋め耐性化（`{prefix}_vol{N}.psd` → `_vol{NN}.psd` → glob）。背景レイヤーはスマートオブジェクト必須

---

## 14. ライセンス / プライバシー

- Claude CLI / YouTube / SUNO / Google API はそれぞれの利用規約に従う
- 個人運営のチャンネル用。社内 / 第三者への共有は想定外
- 認証情報は `~/.config/{app_id}/` 配下にローカル保存、リポジトリには含めない

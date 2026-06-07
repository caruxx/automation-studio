# Automation Studio 仕様書

クリエイター向け動画制作自動化システム。
SUNO 楽曲生成 → Premiere タイムライン自動配置 → YouTube アップロードまでを
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
| 動画編集 | Adobe Premiere Pro + Pymiere (CDP) + ExtendScript JSX |
| 書き出し | Adobe Media Encoder (JSX 経由キュー) |
| YouTube アップロード | YouTube Data API v3 + OAuth |
| AI | Claude CLI（第一候補、API 未使用）/ Gemini / ChatGPT |
| 通知 | Discord Webhook |

---

## 2. アーキテクチャ

```
┌──────────────────────────────────────────────────────────────┐
│                    Web Dashboard (localhost:8888)             │
│  ┌──────────────┐  ┌──────────────────────────────────────┐  │
│  │   Sidebar    │  │ Pages: ダッシュボード / コンテンツ     │  │
│  │ 📊 🎬 📅 🎵 🎨 │  │        / SUNO / Premiere / YouTube      │  │
│  │  📤 🔔 ⚙️    │  │        / 通知 / 設定                   │  │
│  └──────────────┘  └──────────────────────────────────────┘  │
└────────────────────┬─────────────────────────────────────────┘
                     │ HTTP / WebSocket
          ┌──────────▼──────────────────────┐
          │  FastAPI: app.py (組み立て+runtime core)│
          │  + app_core.py (共通土台)             │
          │  + routers/ (benchmark/images/        │
          │     premiere_photoshop/youtube)        │
          └────┬───────┬───────────────────────┘
               │       │
       ┌───────▼──┐  ┌─▼──────────────────┐
       │ 子プロセス │  │ファイル IO           │
       │ (Popen)  │  │ (~/.config/{app_id}/ や │
       └─┬──┬──┬──┘  │  動画フォルダ)       │
         │  │  │    └─────────────────────┘
    ┌────▼┐┌▼┐ └─────────┐
    │SUNO ││Premiere    │YouTube
    │gen  ││(Pymiere+JSX)│upload
    └──┬──┘└─────┬──────┘└────┬─────┘
       │         │             │
 ┌─────▼─┐  ┌────▼─────┐  ┌────▼──────┐
 │Playwright  │Adobe Premiere │ google-api │
 │→suno.com   │+ Media Encoder│-python-client│
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
│   │   ├── premiere_photoshop.py #  Premiere JSX / Photoshop(UXP) / scene-text
│   │   └── youtube.py           #   YouTube API/upload/履歴/説明文/通知
│   ├── start.sh                 # 起動スクリプト
│   ├── setup.sh                 # 依存パッケージ導入
│   ├── suno_auto_create.py      # SUNO 自動生成 + workspace + DL（fetch インターセプタ）
│   ├── app_process_tracks.py   # 楽曲リネーム + ffmpeg 後処理（サムネ/ペルソナ）
│   ├── app_sheets.py  # Google スプシ CSV 取得 + パース + マッチング
│   ├── claude_proposer.py       # Claude CLI 提案ヘルパー（タイトル/説明/タグ）
│   ├── app_premiere.py         # Premiere JSX 送信 + SRT 生成
│   ├── app_youtube.py   # YouTube 投稿
│   ├── app_notify.sh      # Discord 通知
│   └── YouTube_1080p_Optimized.epr   # Media Encoder プリセット
├── web/static/
│   └── index.html               # ダッシュボード UI（全 JS 同梱）
└── skills/                      # 個別スキル解説 (Claude Code 用)
    ├── app-workflow.md
    ├── app-web-dashboard.md
    ├── app-create-folder.md
    ├── app-rename-audio.md
    ├── app-ai-propose.md
    ├── app-image-select.md
    ├── app-premiere.md
    ├── app-export.md
    ├── app-youtube-desc.md
    ├── app-youtube-upload.md
    └── app-notify.md
```

### 関連スクリプト

```
_claude/Script/
├── _[自動配置くん]premiere_long.jsx    # メイン JSX（音声/字幕/画像配置）
├── _place_images_only.jsx               # 画像のみ後から配置 JSX
├── [タイムスタンプ]create_timestamp.jsx # タイムコード生成（単体利用）
└── app_export_ame.jsx        # Media Encoder 書き出し
```

### 設定ファイル (`~/.config/{app_id}/`)

```
dashboard_config.json    # チャンネル名、フォルダパス、ペルソナ、ライバル URL
suno_config.json         # LLM プロバイダー、モデル、プロンプト、Claude CLI パス
channels.json            # 複数チャンネル切替用（v2: icon_cache 付き）
benchmark_config.json    # ベンチマーク設定（v2: チャンネル横断で共通利用）
master_prompts.json      # v2: プロンプト上書き（8 種。空でハードコードにフォールバック）
schedule_jobs.json       # v2: APScheduler ジョブ定義（cron / date トリガー）
export_rules.json        # v2: AME ウォッチャー有効化 / 完成判定ルール
export_queue.json        # v2: 書き出しキューの実行状態
auth_token.txt           # v2: リモートアクセス用トークン（600 perms）
prompts.json             # SUNO プロンプトライブラリ
youtube_client_secret.json   # Google OAuth クライアントシークレット
youtube_token.json       # OAuth 認証トークン（自動更新）
discord_config.json         # Discord Webhook
task_history.json        # タスクログ永続化（サーバー再起動で復元）
competitor_analysis_cache.json  # Claude 分析結果キャッシュ
chromium_profile/        # Playwright 永続プロファイル（SUNO ログイン情報）
```

### 動画フォルダの中身（1 動画 = 1 フォルダ）

```
{num}_{prefix}_{YYMMDD}/               # 例: 77_vol_260415
├── vol_vol{num}.prproj           # Premiere プロジェクト
├── vol_vol{num}.psd              # サムネイル PSD
├── vol_vol{num}.mp4              # 書き出し済 MP4
├── vol{num}.jpg                    # YouTube サムネイル
├── サムネイル.jpg                  # サムネ別名
├── *.png / *.jpg                   # 背景画像候補
├── selected_images.json           # 画像選択結果（JSX 連動）
├── music/                          # 処理済 MP3
├── original_music/                 # SUNO DL 直後のオリジナル MP3
├── subtitles_{num}.srt             # 実測 SRT 字幕
├── music_time_code_info_{num}.txt  # タイムコード情報
├── youtube_title.txt               # YouTube タイトル（Claude 提案 or 手動）
├── youtube_description.txt         # YouTube 説明文
├── youtube_tags.txt                # YouTube タグ（改行 or カンマ区切り）
├── youtube_upload.json             # アップロード完了マーカー
└── Adobe Premiere Pro */           # Premiere キャッシュ
```

---

## 4. ワークフロー（11 工程）

```
[plan] → SUNO生成 → リネーム → 背景画像 → PSD合成 → Premiere配置 → 書き出し → QA → 動画メタ → 多言語化 → AIサムネ → アップロード
```

工程の真実源は `app_pipeline.py` の `STEPS`。`--from-benchmark`（無人運用）時は先頭に `plan` が付き 12 工程（`STEPS_WITH_PLAN`）。

| # | step | UI 起点 | 完了判定ファイル | 担当スクリプト |
|---|------|---------|----------------|--------------|
| 0 | `plan` | （無人運用時のみ） | `plan.json` | app_orchestrator.py / app_pipeline.py `step_plan` |
| 1 | `suno` | SUNO タブ / 楽曲タブ | `music/*.mp3` が 1 本以上 | suno_auto_create.py |
| 2 | `rename` | 楽曲タブ | `music/*.mp3`（処理済み） | app_process_tracks.py |
| 3 | `bgimage` | 画像タブ | `vol{N}.png` or `vol{N}.jpg` | app_image_prompt.py / codex_imagegen.py |
| 4 | `psd_composite` | 画像タブ | `vol{N}.jpg` + `サムネイル.jpg` | app_photoshop.py |
| 5 | `premiere` | 配置タブ | `subtitles_*.srt` | app_premiere.py |
| 6 | `export` | 書き出しタブ | `vol_vol*.mp4` | app_premiere.py `--export` |
| 7 | `qa` | （書き出し直後・自動） | QA 全 4 軸 pass | app_pipeline.py `step_qa` |
| 8 | `meta` | 詳細タブ | `youtube_description.txt` + `youtube_tags.txt` | claude_proposer.py |
| 9 | `localization` | （自動） | 多言語メタ（非 fatal） | app_pipeline.py `step_localization` |
| 10 | `thumbnail` | 画像タブ | `thumbnail.png` / `vol*.jpg` / `サムネイル.jpg` | app_channel_thumbnail.py（PSD 失敗時フォールバック） |
| 11 | `upload` | アップロードタブ | `youtube_upload.json` | app_youtube.py |

判定ロジックは [index.html](web/static/index.html) の `WORKFLOW_STEPS` 定数、自動実行順は `app_pipeline.py` の `STEPS` / `STEP_LABELS` / `STEP_FUNCS` / `RETRY_POLICY`（4箇所一貫更新）で定義。

---

## 5. API リファレンス

### 動画フォルダ管理

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/videos` | 一覧（title, has_*, publish_date 等を vol 降順で返却） |
| POST | `/api/videos/create` | `{publish_date}` → フォルダ + .prproj + .psd 自動生成 |
| GET | `/api/videos/{name}/detail` | 詳細（readiness, upload_info 等） |
| GET/PUT | `/api/videos/{name}/title` | `youtube_title.txt` |
| GET/PUT | `/api/videos/{name}/tags` | `youtube_tags.txt` |

### AI 提案（Claude CLI, API 未使用）

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/videos/{name}/suggest` | body: `{mode: "titles"\|"description"\|"tags", count?, reference?}` |

### 画像選択（JSX 連動）

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

### Premiere

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/premiere/check` | 接続確認（Pymiere 経由） |
| POST | `/api/premiere/run` | `{duration_h/m/s, auto_export, video_name?}` → .prproj 自動オープン + JSX |
| POST | `/api/premiere/export` | 書き出しのみ |
| POST | `/api/premiere/regenerate-srt` | 字幕 + TC だけ再生成 |
| POST | `/api/premiere/place-images` | `{video_name}` → 既存 TL に画像のみ配置 |

### YouTube

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/youtube/upload` | `{video_name, title?, privacy, schedule?, tags?}`（未指定はファイル自動読込） |
| GET | `/api/youtube-desc/references` | 過去説明文の参考リスト |
| POST | `/api/youtube-desc/save` | 説明文保存 |

### サムネ / 背景画像 / 文字入れ（PSD 合成）

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/videos/{name}/run-pipeline` | `{steps:[...]}` で工程選択実行。**サムネ制作 = `["bgimage","psd_composite"]`** |
| POST | `/api/bgimage/run` | `{video_name, ref_count, force}` → ベンチ参照で背景 `vol{N}.png` 生成 |
| GET | `/api/bgimage/reference-dir/list` / POST `/dry-run` | 参照画像フォルダのプレビュー / 選択ソース確認 |
| POST | `/api/photoshop/render-dual-thumbnail` | AI 背景 + シーンテキストで `vol{N}.jpg` + `サムネイル.jpg` を2枚出し |
| POST | `/api/photoshop/render-for-video` | 動画フォルダ指定で PSD 合成（単一トグル方式） |
| POST | `/api/photoshop/generate-scene-text` | `{video_name}` → AI 画像を Vision 分析しシーンテキスト1件生成（チャンネル設定 `scene_text_*` 準拠） |
| POST | `/api/scene-text/suggest-from-benchmark` | `{mode:"titles"\|"vision", count}` → ベンチ（ライバル）からトーン/例/禁止語を提案 |
| GET | `/api/photoshop/check` / `layers` | Photoshop 接続確認 / アクティブ PSD のレイヤー名一覧 |
| POST | `/api/channel-thumbnail/start` | （フォールバック）Vision 駆動の AI サムネを `{vol}/Image/` に一括生成 + スコアリング |

> サムネの**正規フロー** = `bgimage`(背景・ベンチ参照) → `psd_composite`(Photoshop 文字入れ)。`channel-thumbnail`（AI 直接生成）は PSD 合成が使えない時の**フォールバック**。文字（シーンテキスト）のトーン/例/禁止語は per-channel `scene_text_*` 設定で制御（空なら persona 中立、旧 Harbor Notes 固定は撤去済み）。

### システム

| メソッド | パス | 用途 |
|---------|------|------|
| GET/PUT | `/api/config` / `/api/config/dashboard` / `/api/config/suno` | 設定読み書き |
| POST | `/api/config/init` / `/api/config/auto-detect` | 初回セットアップ支援 |
| GET | `/api/schedule` | カレンダー用イベント |
| GET | `/api/channels` / PUT `/api/channels/active/{id}` | チャンネル切替 |
| POST | `/api/finder/open` / `/create-folder` / `/delete-folder` | OS 連携 |
| POST | `/api/notify/discord` | Discord 送信 |
| POST | `/api/notify/line` | Discord 送信（旧互換 alias） |
| WS | `/ws/logs/{task_id}` | ライブログ（suno / premiere / youtube / setup） |

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

### 6.2 `selected_images.json` (画像選択 → JSX)

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

### 7.3 Premiere 自動配置 ([Python/app_premiere.py](Python/app_premiere.py))

1. `open_project(.prproj)` → Pymiere で読込完了を検知
2. `run_jsx(duration)` → [_[自動配置くん]premiere_long.jsx](Script/_[自動配置くん]premiere_long.jsx) を一時ファイル化して送信（ダイアログ置換 + SRT/TC 無効化）
3. `get_timeline_clips()` → 実測 start/end 取得
4. SRT + タイムコードを Python で生成（JSX 内蔵より正確）
5. `--images-only` モード → 専用 JSX [_place_images_only.jsx](Script/_place_images_only.jsx) で画像だけ再配置

### 7.4 YouTube アップロード ([Python/app_youtube.py](Python/app_youtube.py))

OAuth 認証 → MP4/サムネ/説明/タグ/タイトルを**保存ファイルから自動読込** → `videos().insert()`。
成功後 `youtube_upload.json` を書き出し → Web 側で即ステータス反映。

---

## 8. ワークフロー例: vol.77 の新規制作

```
1. コンテンツ → 「+ 新規動画」→ 公開日入力
   → 77_vol_260416 フォルダ + vol_vol77.prproj + vol_vol77.psd 自動生成

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

6. 書き出しタブ → 3:00:00 指定 → ▶ この動画で Premiere 自動配置を実行
   → vol_vol77.prproj 自動オープン → JSX 送信 → 音声/字幕/画像配置
   → [✓] 完了後に書き出し → Media Encoder で MP4 生成

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
- **進捗はログパース**: SUNO/Premiere/YouTube のサブプロセス stdout を `task_logs` に蓄積し、正規表現で `current/total/phase` を抽出
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
| Adobe Premiere Pro | Pymiere (CDP) + JSX | 未起動時は検知 → エラーメッセージ |
| Adobe Media Encoder | Premiere 側 JSX から encodeSequence | プリセット不在時はスキップ |
| YouTube Data API v3 | google-api-python-client + OAuth | token 失効時は自動 refresh |
| Discord Webhook | HTTP POST | 未設定時はボタン無効 |

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
| [app-image-select.md](skills/app-image-select.md) | 画像選択 + JSX 連動 |
| [app-premiere.md](skills/app-premiere.md) | Pymiere + JSX |
| [app-export.md](skills/app-export.md) | Media Encoder 書き出し |
| [app-youtube-desc.md](skills/app-youtube-desc.md) | 説明文生成 |
| [app-youtube-upload.md](skills/app-youtube-upload.md) | YouTube 投稿 |
| [app-competitor-spreadsheet.md](skills/app-competitor-spreadsheet.md) | スプシ競合分析（195ch + 成長 → Claude → 提案）|
| [app-notify.md](skills/app-notify.md) | Discord 通知 |
| [app-master-config.md](skills/app-master-config.md) | マスター設定タブ（プロンプト上書き / SUNO / ベンチマーク / 書き出し / リモート / 入出力。Flow・メタ・master_settings は D8/D12 で撤去） |
| [app-schedule.md](skills/app-schedule.md) | **v2** APScheduler 統合（vol_create / benchmark_refresh / export_window / spot_create） |
| [app-remote-access.md](skills/app-remote-access.md) | **v2** 外出先スマホ操作（Cloudflare Tunnel + 認証ミドルウェア + PWA） |
| [app-imitate-evolve.md](skills/app-imitate-evolve.md) | **v2** 徹底パクリ進化分析 + ベンチマーク参照サイドパネル（投稿時刻ヒートマップ / タグ頻出） |

---

## 13. 変更履歴

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

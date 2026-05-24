# app-web-dashboard: Web Studio の構造と API 全体像

YouTube Studio 風のローカル Web ダッシュボード。動画制作の 9 工程を単一 UI で管理する。

## 起動

```bash
bash Python/start.sh
# → http://localhost:8888/
```

[Python/start.sh](../Python/start.sh) が FastAPI (`Python/app.py`) を起動、
静的 UI は [web/static/index.html](../web/static/index.html) を配信する。

## ナビゲーション（History API / ブラウザ戻る対応）

SPA 内のページ遷移は `history.pushState` で URL ハッシュを更新:

| 操作 | URL | 戻るで何が起きるか |
|------|-----|-----------------|
| サイドバー「コンテンツ」 | `#videos` | 前のページに戻る |
| vol.XX 詳細を開く | `#videos/77_vol_260416` | 一覧に戻る |
| 「← 一覧に戻る」 | `#videos` | 前のページ |
| SUNO タブ | `#suno` | 前のページ |

- `popstate` イベントで戻る / 進む に対応
- URL 直アクセス (`localhost:8888/#videos/NAME`) でそのページを直接開ける
- リロード時も hash から状態復元（`_restoreFromHash()`）

## コンテンツ一覧のフィルター / 検索

コンテンツページのヘッダーに 3 つのフィルターボタン + テキスト検索:

| ボタン | 表示対象 |
|--------|---------|
| `全て (N)` | 全動画 |
| `進行中` | 6 工程が未完了の動画のみ |
| `完了済み` | 全 6 工程完了（アップロード済み含む）|

- テキスト検索: フォルダ名 or YouTube タイトルで絞り込み
- CSS `filter-hidden` クラスで非表示（DOM 再構築なし = 高速）
- フィルター選択中はボタンに件数 `(N)` を表示

## グローバル進捗バー

トップバー最下部に 3px の細いプログレスバーを表示:
- SUNO 実行中のみ表示（青→赤グラデーション）
- どのページにいても「SUNO が何 % か」が一目で分かる
- 停止時は非表示

## ダッシュボードからの直接遷移

「次にやること」「最近の動画」「ミニカレンダー」のカードクリックで:
`goToVideoDetail(name)` → `go('videos')` + `showVideoDetail(name)` を連続実行。
ページ切替 + 詳細展開が 1 タッチで完結。

## ページ構成（サイドバー左）

| ページ | 役割 |
|--------|------|
| 📊 ダッシュボード | KPI カード4枚 + 「次にやること」 + 最近の動画 + ベンチマーク駆動 |
| 🎬 コンテンツ | 動画一覧（テーブル、進捗ステッパー、タイトル表示） |
| 🎯 ベンチマーク | 取り込み（StatCard + ホットチャンネル）/ プロファイル / 制作ドライブ |
| ⚙ 自動化 | チャンネル別の自動化設定 + 📤 AME 書き出しキュー（v2） |
| 🎵 SUNO 生成 | 楽曲自動生成（v2 で設定は「基本設定」に集約、このタブは実行のみ） |
| 🎨 Premiere | JSX 自動配置 + Media Encoder 書き出し |
| 📤 YouTube | 説明文エディタ + アップロード |
| 🔔 通知 | Discord メッセージ送信 |
| ⚙ マスター設定（v2） | プロンプト管理 / SUNO/Flow/メタ詳細 / スケジュール / リモート / 入出力 |
| 基本設定 | チャンネル / 運営チャンネル管理 / ペルソナ / ベンチマーク / API キー |

v2 で **「⚙ マスター設定」を新設**（旧「設定」は「基本設定」に改名）。詳細は [app-master-config.md](./app-master-config.md)。

## 動画詳細（タブ構成）

コンテンツ → vol.XX クリック → タブ切替:

| タブ | 内容 | 保存ファイル |
|------|------|-------------|
| **詳細** | タイトル / 説明 / タグ + AI 提案（ベンチマーク連動・v3 で 1 パネル統合） | `youtube_title.txt` / `youtube_description.txt` / `youtube_tags.txt` |
| **画像** | グリッド選択、メイン/サブ指定 + 🖼後から配置 | `selected_images.json` |
| **楽曲** | メディアプレイヤー + ♥ + 🗑 + SUNO生成 + DL + リネーム + 後処理 | 楽曲ファイル自体（z プレフィックスでステート表現） |
| **書き出し** | 時間指定 + prproj 自動オープン + JSX 実行 | — |
| **アップロード** | サムネ/タイトル/説明/タグ/予約 → YouTube | `youtube_upload.json`（完了マーカー） |

右サイドには **ワークフロー進捗ステッパー**（6工程）が常時表示。

## ワークフロー 6 工程

```
SUNO生成 → 画像選択 → JSX配置 → 書き出し → 動画メタ → アップロード
```

判定条件（[web/static/index.html](../web/static/index.html) の `WORKFLOW_STEPS`）:

| 工程 | 判定 |
|------|------|
| SUNO生成 | `music/*.mp3` 存在 |
| 画像選択 | `selected_images.json` or `vol{N}.jpg` 存在 |
| JSX配置 | `subtitles_*.srt` 存在 |
| 書き出し | `vol_vol*.mp4` 存在（外部SSDパス対応） |
| 動画メタ | `youtube_description.txt` + `youtube_tags.txt` |
| アップロード | `youtube_upload.json` 存在 |

## v2 新規機能（サマリ）

| 機能 | 場所 | 詳細 |
|------|------|------|
| 統合設定スキーマ `/api/config` | 基本設定タブ | dashboard/suno/benchmark/channels/meta を一括返却、`PUT {section, patch}` で透過更新 |
| 運営チャンネル管理 | 基本設定 → 📺 運営チャンネル管理 | YouTube URL 入力 → アイコン自動取得（`snippet.thumbnails.high.url`）、24h TTL キャッシュ |
| ベンチマーク StatCard | ベンチマーク → 取り込み | 総チャンネル数 / 週次成長 TOP3 / 急上昇 TOP3 + ホットチャンネル行（アイコン + 最新動画サムネ） |
| マスター設定タブ | 左下サイドバー | 9 セクション（[app-master-config.md](./app-master-config.md)） |
| 徹底パクリ進化分析 | 動画詳細 → メタ → 🧬 ボタン | ベンチマーク先 + ペルソナ → ✓/✗/+ 3 軸（[app-imitate-evolve.md](./app-imitate-evolve.md)） |
| ベンチマーク参照サイドパネル | 動画詳細 → メタ → 📊 アコーディオン | 適用中ベンチマーク / 投稿時刻ヒートマップ / タグ頻出 |
| AME 書き出しキュー | 自動化タブ | 完成条件マッチを自動 enqueue → AME 投入 → ファイルサイズ安定化で完了検知 |
| スケジュール自動実行 | マスター設定 → ⏱ | APScheduler（[app-schedule.md](./app-schedule.md)） |
| 外出先リモートアクセス | マスター設定 → 🌐 | Cloudflare Tunnel + 認証（[app-remote-access.md](./app-remote-access.md)） |
| 分析プロンプトの言語ハイブリッド（v3） | ベンチマーク分析パネル | `analyze_with_claude` 出力を「descriptive=日本語 / シード=英語 / 数値=numeric」に再設計（cache `language: ja-en-mix` / `prompt_version: 4`） |
| ライバル優先のデータソース（v3） | 自動 | `rival_channels` 登録時は常に YouTube API で rivals を分析対象に。スプシは fallback。詳細は [app-competitor-spreadsheet.md](./app-competitor-spreadsheet.md) |
| AI 提案パネル統合（v3） | 動画詳細 → メタタブ | 旧「AI アシスト（英語）」+「ベンチマーク分析」の 2 パネルを 1 パネル「AI 提案（ベンチマーク連動）」に統合。「英語タイトル候補/英語説明文/英語タグ」が分析を自動参照するため「刺さる英語メタ提案」ボタンは廃止（API は残存） |
| サムネ Vision 入力（v3） | 動画詳細 → メタタブ | サムネ画像（`vol*.jpg` / `サムネイル.jpg`）があれば自動で Read ツール経由で読み取り、英語タイトル/説明文に視覚情景を反映。詳細は [app-ai-propose.md](./app-ai-propose.md) |
| 競合分析の自動表示（v3） | 動画詳細 → メタタブ表示時 | `showVideoDetail` 内で `showCachedAnalysisIfAny()` が `/api/analysis/cache` を取得し、保存済み分析結果（バズパターン/ホットキーワード/トレンド変化/推奨）を AI 提案パネル冒頭に自動描画。再分析ボタンを押さなくても前回結果が常時可視 |
| シリーズ画像案 + 一括生成（v3） | コンテンツページ上部 → アコーディオン | ベンチマーク分析駆動で「次に作るべき画像」を Claude が日本語で N 件提案 → チェック → Flow / Codex で直列一括生成 → `_series_drafts/<slug>/Image/` に格納。詳細は [app-series-proposals.md](./app-series-proposals.md) |
| PWA | manifest.json + sw.js | スマホ「ホーム画面に追加」で全画面起動 |

## 主要 API

### コンテンツ・動画
- `GET /api/videos` — 動画フォルダ一覧（vol番号で降順ソート）、全工程のbool + title
- `POST /api/videos/create` — `{num}_{prefix}_{YYMMDD}` フォルダと .prproj / .psd を自動生成（`app-create-folder` 参照）
- `GET /api/videos/{name}/detail` — 詳細情報、`readiness`、`upload_info`

### メタデータ
- `GET|PUT /api/videos/{name}/title` — `youtube_title.txt`
- `PUT /api/videos/{name}/tags` — `youtube_tags.txt`
- `GET|PUT /api/videos/{name}/tags`

### AI 提案（Claude CLI）
- `POST /api/videos/{name}/suggest` — body: `{mode: "titles"|"description"|"tags", count?, reference?}`
  - v3 から `competitor_analysis_cache.json` を自動参照し、視聴者文脈をプロンプトに注入（無ければ persona のみで従来挙動）
- `POST /api/videos/{name}/suggest-with-analysis` — 分析必須・タイトル/説明/タグを 1 ショット返却（UI ボタンは v3 で廃止、API は互換のため残存）
- `POST /api/videos/{name}/suggest-all` — 一気通貫提案（楽曲・Flow・メタ）
- `POST /api/videos/{name}/suggest-imitate-evolve` — 徹底パクリ進化分析（[app-imitate-evolve.md](./app-imitate-evolve.md)）
- 詳細は [app-ai-propose.md](./app-ai-propose.md)

### 画像選択
- `GET /api/videos/{name}/images`
- `PUT|DELETE /api/videos/{name}/selected-images`
- 詳細は [app-image-select.md](./app-image-select.md)

### 楽曲（プレイヤー / いいね / 削除）
- `GET /api/videos/{name}/tracks` — MP3 一覧（`root` / `music` / `original_music`）+ いいね数
- `GET /api/videos/{name}/track-file/{rel_path}` — HTML audio 配信
- `POST /api/videos/{name}/track-like` — `{rel_path, delta}` でリネーム（z プレフィックス更新）
- `DELETE /api/videos/{name}/track?rel_path=...` — 物理削除
- `POST /api/videos/{name}/process-tracks[?rename_only=true]` — 後処理（リネーム + ffmpeg）
- 詳細は [app-track-player.md](./app-track-player.md) / [app-rename-audio.md](./app-rename-audio.md)

### Premiere（フォルダ指定対応）
- `POST /api/premiere/run` — body: `{duration_h, duration_m, duration_s, auto_export, video_name?}`
- `video_name` 指定時は `vol_vol*.prproj` を自動オープンしてから JSX 送信
- 詳細は [app-premiere.md](./app-premiere.md)

### YouTube（保存ファイル統合）
- `POST /api/youtube/upload` — body: `{video_name, title?, privacy, schedule?, tags?}`
- `title` / `tags` 未指定時は保存ファイルから自動読み込み
- アップロード成功後 `youtube_upload.json` マーカーを書き出し（ステッパーに反映）
- 詳細は [app-youtube-upload.md](./app-youtube-upload.md)

## プロバイダー設定（SUNO）

- Gemini / ChatGPT → API キー必要（`~/.config/{app_id}/suno_config.json` の `api_key`）
- **Claude (CLI)** → `claude -p` を subprocess 呼び出し、JSON 出力で逐次生成（API未使用）
- 設定画面「Claude CLI コマンド」欄でコマンド名/絶対パスを指定可能

## Workspace 連動（SUNO 生成）

SUNO タブ「対象動画」セレクタで vol を指定すると:
1. `{channel_name}_vol{N}` を自動計算（例 `vol_vol74`、チャンネル名は英数_- 以外を `_` に置換）
2. `https://suno.com/me/workspaces` に遷移 →
   - 同名があればクリックして選択
   - 無ければ `role="button" name="New Workspace"` → `placeholder="Untitled Workspace"` に入力 → `role="button" name="Create Workspace"` クリック
3. `/create?wid=*` へリダイレクト後、通常の楽曲生成ループを実行

**一括ダウンロード**: 動画詳細「楽曲」タブの「⬇ Workspaceの楽曲をDL」で
`POST /api/suno/download {video_name}` → Playwright が `/me/workspaces` から該当ワークスペース内の
全トラックを MP3 ダウンロード → `<動画フォルダ>/original_music/` に保存。

CLI: `python3 suno_auto_create.py --download-workspace vol_vol74 --download-dir /path/to/folder`

**一括生成モード**: SUNO タブ or 楽曲タブの「一括生成モード」チェックで Claude CLI を 1 回だけ呼び、
N 曲分のメタデータを事前生成 → ループ内の LLM 待ちを削減。構造ヒント入りスタイル記述も同時に生成。
CLI: `python3 suno_auto_create.py --batch --count 5 --prompt "..."`

API パラメータ:
```
POST /api/suno/start
{
  "workspace": "vol_vol74",       // 直接指定
  "video_name": "74_vol_260415",  // または vol.XX 名から自動計算
  "prompt": "...", "count": 5, "provider": "claude"
}
```

CLI: `python3 suno_auto_create.py --workspace vol_vol74 --prompt "..." --count 5`

実装は `suno_auto_create.ensure_workspace(page, name)`。
`get_by_role("button", name="New Workspace")` など role ベースで対応。

## Playwright ブラウザ内ステータスオーバーレイ

自動操作中の SUNO / Premiere 等のブラウザに、現在の処理を可視化する黒い固定パネルを表示:

- `context.add_init_script(_STATUS_OVERLAY_SCRIPT)` で全ページの document-start に注入
- `window.__appStatus(msg, variant)` を Python 側から `page.evaluate` で呼ぶ
- 右下固定・タイトル「🤖 Automation Studio 自動操作中」・本文 + 時刻
- variant で border 色を変える: `info`（青）/ `ok`（緑）/ `warn`（橙）/ `err`（赤）

**主な表示タイミング**（`_set_status(page, ...)` で呼ぶ）:
- `SUNO /create にアクセス中...`
- `Workspace 'vol_vol77' を確保中...` / `作成完了`
- `楽曲 3/20 を生成中...`
- `楽曲一覧をスクロール取得中...` / `楽曲検出: 80曲 / audio_url 収集中...`
- `DL 12/80: track.mp3 (3.4MB)` / `📥 完了: 成功 80 / 失敗 0`

## 一括ダウンロード（/api/suno/download）

[app-suno-download.md](./app-suno-download.md) 参照。

SUNO SPA の fetch/XHR をインターセプトして `audio_url` をキャッシュし、
`context.request.get()` で Cookie 共有しつつ MP3 を取得、動画フォルダ直下に `<title>.mp3` で保存。

UI: 動画詳細「楽曲」タブの「⬇ Workspace DL」ボタン。

## タスク履歴の永続化

`~/.config/{app_id}/task_history.json` にタスクログとメタデータを保存:

- **保存タイミング**: suno / premiere / youtube / process / setup の `[完了]` 到達時に自動書き出し
- **読み込みタイミング**: サーバー起動時に `_load_task_history()` で復元
- **保持量**: タスクごと最大 500 行
- **効果**: サーバー再起動しても前回のログが残る。SUNO ページを開いたら前回の生成ログがそのまま見られる

## 設定ファイル

| ファイル | 役割 |
|---------|------|
| `~/.config/{app_id}/dashboard_config.json` | チャンネル名、フォルダパス、ペルソナ、ライバル URL |
| `~/.config/{app_id}/suno_config.json` | プロバイダー、モデル、プロンプト、Claude CLI パス |
| `~/.config/{app_id}/channels.json` | 複数チャンネル管理 |
| `~/.config/{app_id}/youtube_client_secret.json` | OAuth クライアントシークレット |
| `~/.config/{app_id}/youtube_token.json` | OAuth トークン |
| `~/.config/{app_id}/discord_config.json` | Discord Webhook |
| `~/.config/{app_id}/task_history.json` | タスクログ永続化（サーバー再起動で復元） |

## 関連スキル

- [app-workflow.md](./app-workflow.md) — 全体フロー概観
- [app-create-folder.md](./app-create-folder.md) — フォルダ作成
- [app-rename-audio.md](./app-rename-audio.md) — 楽曲リネーム+音声処理（ペルソナフォールバック / rename-only）
- [app-suno-download.md](./app-suno-download.md) — SUNO 一括 DL（fetch インターセプタ）
- [app-track-player.md](./app-track-player.md) — メディアプレイヤー + いいね + 削除
- [app-premiere.md](./app-premiere.md) — Premiere JSX
- [app-export.md](./app-export.md) — Media Encoder 書き出し
- [app-youtube-desc.md](./app-youtube-desc.md) — 説明文生成
- [app-youtube-upload.md](./app-youtube-upload.md) — アップロード
- [app-notify.md](./app-notify.md) — Discord 通知
- [app-ai-propose.md](./app-ai-propose.md) — Claude CLI 提案パターン
- [app-image-select.md](./app-image-select.md) — 画像選択
- [app-competitor-spreadsheet.md](./app-competitor-spreadsheet.md) — スプシ競合分析
- [app-series-proposals.md](./app-series-proposals.md) — シリーズ画像案の提案 → 一括生成

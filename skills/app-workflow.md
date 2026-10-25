# Channel 自動化ワークフロー

動画制作 11 工程を、**Web ダッシュボード 1 画面**で完結させるマスターワークフロー。
（旧 6 工程版から、qa / bgimage / psd_composite / localization / thumbnail を独立 step に分離した結果 11 工程に拡張。
詳細パイプラインは `python3 app_pipeline.py --help` 参照。）

## チャンネル情報
- チャンネル名: 任意（基本設定で指定）
- ジャンル: AI x BGM (SUNO)
- 作業フォルダ: `$AUTOMATION_CHANNEL_DIR`（設定画面 → 基本設定で変更可）
- スクリプト格納: `_claude/Script/`

## 起点: Web Studio

```bash
bash Python/start.sh       # http://localhost:8888/
```

左サイドバー: `ダッシュボード / コンテンツ / ベンチマーク / 設定 / 履歴 / SUNO / YouTube / ライブ`（Premiere と書き出しのページは 2026-10-25 段階 1a で廃止）

詳細は [app-web-dashboard.md](./app-web-dashboard.md)。

## ナビゲーション

- **SPA + History API**: URL hash（`#videos`, `#videos/77_vol_260416`, `#suno` 等）でブラウザの戻る/進む/リロード/直アクセスに対応
- **ダッシュボード直接遷移**: 「次にやること」「最近の動画」「ミニカレンダー」の vol クリックで 1 タッチで動画詳細へ
- **コンテンツフィルター**: `全て` / `進行中` / `完了済み` ボタン + テキスト検索（74件以上でも快適）
- **グローバル進捗バー**: SUNO 実行中は全ページのトップバー最下部に青バーで % 表示
- **タスク履歴永続化**: `~/.config/{app_id}/task_history.json` → サーバー再起動後も前回ログ復元

## フォルダ作成 → 即コンテンツ一覧に追加

「+ 新規動画」から公開日を指定 → `{num}_{prefix}_{YYMMDD}` フォルダを自動生成（テンプレートのコピーは無い）。
**この時点でコンテンツ一覧に追加され、以降 11 工程の進捗がステッパーで可視化される。**

## ワークフロー 11 工程

各工程は「コンテンツ → vol.XX → 詳細画面」のタブで完結する。
（パイプライン CLI 上の step キー: `suno / rename / bgimage / psd_composite / premiere / export / qa / meta / localization / thumbnail / upload`。`--from-benchmark` 時は先頭に `plan` が付き 12 工程）

### STEP 1: SUNO 生成（🎵 SUNOタブ / 動画詳細「楽曲」タブ）
- **Web**: `SUNO 生成` で プロバイダー (Gemini / ChatGPT / **Claude CLI**) / プロンプト / 回数 を指定 → 自動生成ループ
- **対象動画セレクタ**: vol を選ぶと `{channel_name}_vol{N}` の Workspace を
  `/me/workspaces` で自動確保（既存あれば選択、無ければ `New Workspace` → `Create Workspace` → `/create?wid=*`）
- **一括DL**: 「楽曲」タブ「⬇ Workspace DL」で生成済み MP3 を**動画フォルダ直下**に一括保存（[app-suno-download.md](./app-suno-download.md)）
- **メディアプレイヤー + いいね + 削除**: [app-track-player.md](./app-track-player.md)
  - `<audio controls>` でブラウザ再生（単一再生ロジック）
  - ♥ ボタンでファイル名先頭に `z` 追加（`z_song.mp3` → `zz_song.mp3`）
  - 🗑 ボタンでローカル物理削除
- **リネームのみ**: 「✏️ タイトルのみリネーム」で ffmpeg スキップの軽量リネーム。サムネ無しなら**チャンネルペルソナ**から提案
- **後処理**: 「🎛 後処理を実行」で Claude CLI タイトル提案 + FFmpeg（無音トリム + 8 秒フェード + -16 LUFS 正規化）→ `music/` に出力、オリジナルは `original_music/` にバックアップ
- **ブラウザ内ステータス**: Playwright 操作中のブラウザ右下に現在処理を可視化（[app-web-dashboard.md](./app-web-dashboard.md) 参照）
- **Data API ベンチマーク分析**: チャンネル URL を `/api/benchmark/channels` に登録 → 直近動画をキャッシュ → Claude で分析 → 提案。Sheets ベンチ取込は廃止（[app-competitor-spreadsheet.md](./app-competitor-spreadsheet.md) 参照）
- **Claude CLI 経由**: API未使用、`claude -p "..."` が JSON 単一オブジェクトを逐次返す（[app-ai-propose.md](./app-ai-propose.md)）
- **Tampermonkey**: 従来の「Ghost Writer by LLM」も併用可
- 完了条件: `music/*.mp3` が 1 本以上存在 → ステッパー 1/11 ✓

### STEP 2: 楽曲リネーム + 音声処理（動画詳細「楽曲」タブ）
- Claude CLI が**サムネ / ペルソナ**から「らしい」タイトルを提案 → リネーム
- ffmpeg で無音トリム + 8 秒フェードアウト + -16 LUFS 正規化 → `music/` に出力
- オリジナルは `original_music/` にバックアップ
- 詳細: [app-rename-audio.md](./app-rename-audio.md)
- 完了条件: `music/*.mp3` が処理済み（rename 完了マーカーまたは music 配下に処理後ファイル）→ 2/11 ✓

### STEP 3: 背景画像生成（動画詳細「画像」タブ）
- ベンチマーク（picked or rival_channels の thumbs プール）から N 枚ランダム選択 →
  チャンネル persona を載せたプロンプトで `codex_imagegen.py` を起動 → `vol{N}.png` を 1 枚生成
- 背景画像は `selected_images.json` があればそれを優先。無ければ `vol{N}.jpg` → `vol{N}.png` → `vol{N}-1.*` の順で app_ffrender が読む
- 既存 `vol{N}.png/.jpg` あれば既定でスキップ（`APP_BGIMAGE_FORCE=1` で上書き）
- 「上書き再生成」UI ボタン / `POST /api/bgimage/run` でも単発実行可能
- 詳細: [app-bgimage.md](./app-bgimage.md)
- 完了条件: `vol{N}.png` または `vol{N}.jpg` がフォルダ直下に存在 → 3/11 ✓

### STEP 4: 画像合成（動画詳細「画像」タブ / 工程名 psd_composite）
- 背景画像（STEP 3）+ シーンテキストを Pillow で合成し、`vol{N}.jpg`（動画背景）+ `サムネイル.jpg`（YouTube 用）を 2 枚出し（Image2 Layout Studio）
- 文字（シーンテキスト）のトーン / 例 / 禁止語は per-channel `scene_text_*` 設定で制御。「シーンテキスト自動生成」ボタン = `POST /api/scene-text/generate`
- 正規サムネフロー = `bgimage`(背景) → `psd_composite`(画像合成)。AI 直接生成（STEP 10）は合成が使えない時のフォールバック
- 詳細: [image.md](./image.md)
- 完了条件: `vol{N}.jpg` または `サムネイル.jpg` が存在 → 4/11 ✓

### STEP 5: 配置（工程名 premiere。自動で通過）
- 2026-10-25 段階 1a 以降、配置は STEP 6 の FFmpeg 書き出しに統合されている。この工程は何もせず成功する
- 完了条件: なし（STEP 6 で `subtitles_*.srt` が生成される）→ 5/11 ✓

### STEP 6: 書き出し（FFmpeg）
- `app_ffrender.py` が音声・映像（静止画ループ）・字幕(SRT)・タイムコード(TC) を一括生成。構成の正本は `.ffrender_manifest.json`
- 外部 SSD パスにも対応（チャンネル設定 `export_path`）
- 詳細: [app-export.md](./app-export.md)
- 完了条件: `{prefix}_vol{N}.mp4` が存在 → 6/11 ✓

### STEP 7: QA チェック（書き出し直後・自動）
- 解像度（1920x1080） / アスペクト（16:9） / 尺（≧ 動画予定時間） / コーデック（H.264）を ffprobe で検証
- 失敗時は Discord に通知して停止（手動で修正 → 「書き出し」から再開）
- 詳細: 検査ロジックは [Python/app_pipeline.py](../Python/app_pipeline.py) `step_qa()`
- 完了条件: QA 全 4 軸 pass → 7/11 ✓

### STEP 8: 動画メタ（動画詳細「公開準備」タブ）
- **タイトル ×5 提案** / **説明文提案** / **タグ提案** すべて Claude CLI（JSON出力・API未使用）
- ペルソナ・楽曲リスト・公開日を自動で投入 → クリック1つで採用＆保存
- 保存ファイル: `youtube_title.txt` / `youtube_description.txt` / `youtube_tags.txt`
- 詳細: [app-ai-propose.md](./app-ai-propose.md) / [app-youtube-desc.md](./app-youtube-desc.md)
- 完了条件: title / description / tags 3 ファイルが揃う → 8/11 ✓

### STEP 9: 多言語メタデータ（自動）
- メイン言語（per-channel `youtube_upload_defaults.default_language`、例: orzz=en・SUKIMA=ja）のメタを各国語へ翻訳
- メイン言語自体は翻訳対象から除外。`localization` step が title / description を多言語化し、upload 時に YouTube `localizations` へ反映
- 詳細: [Python/app_pipeline.py](../Python/app_pipeline.py) `step_localization()`
- 完了条件: 多言語メタが生成される（非 fatal・失敗しても upload は継続）→ 9/11 ✓

### STEP 10: サムネイル自動生成（フォールバック・動画詳細「画像」タブ）
- codex でサムネ候補を生成 → 候補 1 枚を `thumbnail.png` に昇格（自動サムネは codex 一本化済み。Flow 経路は廃止）
- 既存サムネ（手動配置 `vol*.jpg` / `サムネイル.jpg`）があればスキップ
- env: `APP_THUMBNAIL_PROVIDERS=codex` / `APP_THUMBNAIL_DISABLE=1`
- 失敗しても upload は止めない（手動でサムネを当てれば公開可能）
- 完了条件: `thumbnail.png` または `vol*.jpg` または `サムネイル.jpg` のいずれか → 10/11 ✓

### STEP 11: アップロード（動画詳細「アップロード」タブ）
- サムネ・タイトル・説明・タグを**保存ファイルから自動ロード**してプレビュー
- 公開設定 + 予約日時 → YouTube Data API v3 で送信
- 完了時に `youtube_upload.json` マーカー書出 → ステッパーに即反映
- 詳細: [app-youtube-upload.md](./app-youtube-upload.md)
- 完了条件: `youtube_upload.json` が存在 → 11/11 ✓

### 任意: Discord 通知
- スケジュール通知、作業完了通知など
- 詳細: [app-notify.md](./app-notify.md)

## v2 一括パイプライン `app_pipeline.py`

```bash
# 手動: vol.78 を全工程
python3 app_pipeline.py 78

# v2: ベンチマーク分析 → plan.json → 全工程（無人）
python3 app_pipeline.py 78 --from-benchmark --auto
```

`--from-benchmark` フラグで先頭に **`step_plan`** が挿入される:

1. `~/.config/{app_id}/competitor_analysis_cache.json` を読み込み
2. `propose_suno_prompt(analysis, ...)` で SUNO プロンプト + rationale を生成
3. `{folder}/plan.json` に保存
4. 後続の `step_suno` が plan.json を優先利用（環境変数 `ORZZ_SUNO_PROMPT` より優先順位低い）

失敗時は既定プロンプトで続行（non-fatal）。

### 全工程（--from-benchmark 時）

```
plan → suno → rename → bgimage → psd_composite → premiere → export → qa → meta → localization → thumbnail → upload
0/11   1/11   2/11    3/11      4/11           5/11      6/11    7/11  8/11  9/11          10/11      11/11
```

詳細は `app_pipeline.py --help` と [Python/app_pipeline.py](../Python/app_pipeline.py) `STEPS` / `STEP_LABELS`。

## v2 自動化レイヤー（スケジュール + リモート）

Web UI から手動で実行する代わりに、以下を組み合わせれば**完全無人運用**可能:

| レイヤー | 役割 | 詳細 |
|---------|------|------|
| APScheduler | 毎週月・金 9:00 に vol.X+1 をフル自動 / 毎朝 7:00 に分析リフレッシュ / スポット実行 | [app-schedule.md](./app-schedule.md) |
| 認証 + Cloudflare Tunnel + PWA | 外出先スマホから操作。Discord 通知で実行確認、緊急時は QR 経由でログイン | [app-remote-access.md](./app-remote-access.md) |
| マスター設定 | 上記すべてを 1 画面で管理（プロンプト / SUNO パラメータ / スケジュール / リモート / インポート・エクスポート） | [app-master-config.md](./app-master-config.md) |
| 徹底パクリ進化 | 動画詳細メタタブで ✓パクる/✗避ける/+進化 を Claude が提案、ベンチマーク参照サイドパネル付き | [app-imitate-evolve.md](./app-imitate-evolve.md) |

## フォルダ構成（1動画単位）

```
{num}_{prefix}_{YYMMDD}/
├── {prefix}_vol{num}.mp4         # 書き出し済み（外部SSDにも対応）
├── .ffrender_manifest.json      # 書き出し構成の正本
├── vol{num}.jpg / サムネイル.jpg  # サムネイル
├── *.png / *.jpg                  # 背景画像（ロケーション）
├── selected_images.json          # 画像選択結果
├── music/                        # 処理済みMP3（フェードアウト済み）
├── original_music/               # オリジナルMP3バックアップ
├── subtitles_{num}.srt           # 実測SRT
├── music_time_code_info_{num}.txt # タイムコード
├── youtube_title.txt             # タイトル
├── youtube_description.txt       # 説明文
├── youtube_tags.txt              # タグ
└── youtube_upload.json           # アップロード完了マーカー
```

## 命名規則
- フォルダ: `{連番}_{prefix}_{YYMMDD}` 例: `67_vol_260405`
- 動画: `{prefix}_vol{連番}.mp4` 例: `vol_vol67.mp4`
- いいね楽曲: `z_` プレフィックス（タイムライン先頭配置）

## 関連スキル索引

| スキル | 役割 |
|--------|------|
| [app-web-dashboard.md](./app-web-dashboard.md) | Web Studio 全体構造・API 一覧 |
| [app-create-folder.md](./app-create-folder.md) | フォルダ作成 |
| [app-rename-audio.md](./app-rename-audio.md) | SUNO DL 後のリネーム + 音声処理（サムネ/ペルソナ、rename-only モード） |
| [app-suno-download.md](./app-suno-download.md) | SUNO Workspace 楽曲の一括 DL（fetch インターセプタ） |
| [app-track-player.md](./app-track-player.md) | 楽曲メディアプレイヤー + いいね + 削除 |
| [app-ai-propose.md](./app-ai-propose.md) | Claude CLI による JSON 提案パターン |
| [app-bgimage.md](./app-bgimage.md) | 背景画像の自動生成（パイプライン STEP 3） |
| [app-image-select.md](./app-image-select.md) | 画像選択（`selected_images.json`） |
| [app-export.md](./app-export.md) | FFmpeg 書き出し（app_ffrender） |
| [app-youtube-desc.md](./app-youtube-desc.md) | 説明文生成 |
| [app-youtube-upload.md](./app-youtube-upload.md) | YouTube アップロード |
| [app-competitor-spreadsheet.md](./app-competitor-spreadsheet.md) | J-0後のData APIベンチマーク分析（旧スプシ経路の廃止メモ）|
| [app-notify.md](./app-notify.md) | Discord 通知 |

# Automation Studio — Claude Code 向け運用ガイド

## クイックリファレンス

スクリプトフォルダ:
```
cd <_claudeのルートパス>/Python
```

## 一括パイプライン（推奨）

```bash
# vol.78 を全工程実行（SUNO → リネーム → Premiere → 書き出し → メタ → アップロード）
python3 app_pipeline.py 78

# Web API 経由で実行（localhost:8888 起動中）
python3 app_pipeline.py 78 --via-api

# Premiere 以降だけ再実行
python3 app_pipeline.py 78 --from premiere

# メタデータだけ生成
python3 app_pipeline.py 78 --only meta

# 確認だけ
python3 app_pipeline.py 78 --dry-run
```

## 個別操作ワンライナー

### フォルダ作成
```bash
curl -s -X POST http://localhost:8888/api/videos/create \
  -H 'Content-Type: application/json' \
  -d '{"publish_date":"2026-04-20"}'
```

### SUNO 楽曲生成（Claude CLI、一括モード、20曲）
```bash
python3 suno_auto_create.py \
  --prompt "lounge jazz BGM, elegant cafe atmosphere" \
  --count 20 --interval 40 --provider claude --batch \
  --workspace vol_vol78
```

### SUNO 楽曲ダウンロード
```bash
python3 suno_auto_create.py \
  --download-workspace vol_vol78 \
  --download-dir "/path/to/78_vol_260420"
```

### 楽曲リネーム（タイトルのみ、ffmpeg なし）
```bash
python3 app_process_tracks.py /path/to/78_vol_260420 --rename-only
```

### 楽曲後処理（リネーム + ffmpeg フェードアウト + ゲイン正規化）
```bash
python3 app_process_tracks.py /path/to/78_vol_260420
```

### AI メタデータ生成（タイトル・説明・タグ）
```bash
# タイトル 5 候補
curl -s -X POST http://localhost:8888/api/videos/78_vol_260420/suggest \
  -H 'Content-Type: application/json' \
  -d '{"mode":"titles","count":5}'

# 説明文
curl -s -X POST http://localhost:8888/api/videos/78_vol_260420/suggest \
  -H 'Content-Type: application/json' \
  -d '{"mode":"description"}'

# タグ
curl -s -X POST http://localhost:8888/api/videos/78_vol_260420/suggest \
  -H 'Content-Type: application/json' \
  -d '{"mode":"tags"}'
```

### タイトル保存
```bash
curl -s -X PUT http://localhost:8888/api/videos/78_vol_260420/title \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","new_title":"Golden Hour Reverie"}'
```

### タグ保存
```bash
curl -s -X PUT http://localhost:8888/api/videos/78_vol_260420/tags \
  -H 'Content-Type: application/json' \
  -d '{"tags":["BGM","Lounge","Chill","Jazz","AI Music"]}'
```

### 背景画像生成（ベンチマーク参照 + チャンネルコンセプト）
```bash
# Web API 経由
curl -s -X POST http://localhost:8888/api/bgimage/run \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","ref_count":3,"force":false}'

# CLI 直接（パイプライン step だけ実行）
python3 app_pipeline.py 78 --only bgimage

# 強制再生成（既存 vol{N}.png/.jpg を上書き）
APP_BGIMAGE_FORCE=1 python3 app_pipeline.py 78 --only bgimage
```

詳細: [skills/app-bgimage.md](skills/app-bgimage.md)

### Premiere 自動配置（3 時間、プロジェクト自動オープン）
```bash
curl -s -X POST http://localhost:8888/api/premiere/run \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","duration_h":3,"duration_m":0,"duration_s":0}'
```

### 画像のみ後から配置
```bash
curl -s -X POST http://localhost:8888/api/premiere/place-images \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420"}'
```

### YouTube アップロード（限定公開）
```bash
curl -s -X POST http://localhost:8888/api/youtube/upload \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","privacy":"unlisted"}'
```

### 競合分析（スプレッドシート経由、API quota ゼロ）
```bash
# 分析のみ
python3 app_competitor.py --analyze

# 分析 + vol.78 向け提案
python3 app_competitor.py --propose 78

# ホットチャンネル TOP10 を API で取得
curl -s http://localhost:8888/api/analysis/hot-channels?top_n=10
```

### Web サーバー起動 / 停止
```bash
# 起動
bash start.sh

# 停止
lsof -ti:8888 | xargs kill -9
```

## ユーザーからの自然言語指示 → 実行マッピング

| ユーザーの言い方 | 実行すべきこと |
|----------------|-------------|
| 「vol.78 を作って」 | `curl POST /api/videos/create {"publish_date":"..."}` |
| 「vol.78 の楽曲を作って」 | `python3 suno_auto_create.py --workspace vol_vol78 ...` |
| 「楽曲をダウンロードして」 | `python3 suno_auto_create.py --download-workspace vol_vol78 ...` |
| 「リネームして」 | `python3 app_process_tracks.py <folder> --rename-only` |
| 「後処理して」 | `python3 app_process_tracks.py <folder>` |
| 「背景画像を作って」 | `curl POST /api/bgimage/run {"video_name":"..."}` または `python3 app_pipeline.py <vol> --only bgimage` |
| 「サムネを Photoshop で作って」「PSD を合成して」 | `python3 app_pipeline.py <vol> --only psd_composite`（bgimage 後・premiere 前。`<vol_folder>/vol{N}.jpg` + `サムネイル.jpg` を 2 枚出し） |
| 「サムネを AI で作って」「サムネを自動生成して」 | `python3 app_pipeline.py <vol> --only thumbnail`（ベンチマーク分析 concept/visual_direction から**プロンプトを動的構築** → Flow/Codex で生成 → `thumbnail.png` に昇格。既定は Flow のみ、両方使うなら `APP_THUMBNAIL_PROVIDERS=flow,codex`。詳細 [skills/app-thumbnail.md](skills/app-thumbnail.md)。⚠ 既に `サムネイル.jpg`(PSD合成)があるとスキップ） |
| 「参照画像フォルダを変更して」 | Web UI → 設定タブ → **参照画像フォルダ（背景画像生成）** → フォルダパス欄を編集 → 保存（`.app_channel_config.json` の `reference_image_dir` に per-channel 保存。空欄なら Picked → rival thumbs にフォールバック） |
| 「タイトルを提案して」 | `curl POST /api/videos/.../suggest {"mode":"titles"}` |
| 「Premiere で配置して」 | `curl POST /api/premiere/run {"video_name":"..."}` |
| 「書き出して」 | `curl POST /api/premiere/export` |
| 「アップロードして」 | `curl POST /api/youtube/upload {"video_name":"..."}` |
| 「全部やって」 | `python3 app_pipeline.py <vol>` |
| 「Premiere からやり直して」 | `python3 app_pipeline.py <vol> --from premiere` |
| 「競合を分析して」 | `python3 app_competitor.py --analyze` |
| 「競合分析から提案して」 | `curl POST /api/videos/.../suggest-with-analysis` |
| 「今伸びてるチャンネルは？」 | `curl GET /api/analysis/hot-channels?top_n=10` |
| 「WEB を起動して」 | `bash start.sh` |
| 「WEB を再起動して」 | `lsof -ti:8888 | xargs kill -9; bash start.sh` |

## vol 番号からフォルダ名の解決

フォルダ名は `{vol}_{prefix}_{YYMMDD}` 形式。API は `video_name` で受け付ける。
vol 番号だけ言われた場合は `/api/videos` をクエリして該当フォルダ名を確認:

```bash
curl -s http://localhost:8888/api/videos | python3 -c "
import json,sys
vs = json.load(sys.stdin).get('videos',[])
for v in vs:
    if v['num'] == '78':
        print(v['name'])
        break
"
```

## エラーリカバリ手順

### SUNO 関連

| エラー | 対処 |
|--------|------|
| ブラウザが起動しない | `python3 -m playwright install chromium` |
| SUNO にログインできない | ブラウザが開いたら手動でログイン → 待機後に続行 |
| Workspace 作成失敗 | SUNO のUIが変わった可能性。手動で `/me/workspaces` から作成 |
| DL で audio_url 取得失敗 (cache miss) | コンテキスト再起動: Playwright を閉じて `--download-workspace` を再実行 |
| 全曲同じタイトル | `--batch` を付けて一括生成。プロンプトに diversity 指示を追加 |

### リネーム関連

| エラー | 対処 |
|--------|------|
| 「既に一致」で何もリネームされない | サムネなし + ペルソナ空。設定画面でペルソナを入力、または vol*.jpg を配置 |
| タイトル取得数が少ない | チャンク分割 (10件ずつ) で再試行される。ログで `⚠️ チャンク N 取得失敗` 確認 |
| ffmpeg エラー | `brew install ffmpeg` でインストール確認 |

### Premiere 関連

| エラー | 対処 |
|--------|------|
| 「Premiere Pro 上で実行してね」 | `video_name` 付きで API を叩く（.prproj 自動オープン）。Premiere が起動していることを確認 |
| Pymiere 接続失敗 | Premiere Link パネルがインストールされているか確認: `bash cep_extension/install.sh` |
| 画像が見つからない | 新仕様は alert せず音声のみ配置。後から `place-images` で追加可 |

### YouTube 関連

| エラー | 対処 |
|--------|------|
| OAuth 認証エラー | `python3 app_youtube.py --auth-only` で再認証 |
| タグがハードコード | `youtube_tags.txt` が存在すればそちらを使用。なければ既定タグにフォールバック |
| MP4 が見つからない | Premiere → 書き出しを先に完了させる |

### 一般

| エラー | 対処 |
|--------|------|
| Web サーバーが応答しない | `lsof -ti:8888 | xargs kill -9; bash start.sh` |
| パイプライン途中で止まった | `python3 app_pipeline.py <vol> --from <止まった工程>` で再開 |
| Claude CLI がない | `which claude` で確認。無ければ Claude Code CLI をインストール |

## 仕様書・スキル参照

- [SPEC.md](SPEC.md) — 全体仕様（API 一覧・データ契約・アーキテクチャ）
- [skills/app-workflow.md](skills/app-workflow.md) — 9 工程の全体フロー（背景画像生成を STEP 3 として挿入）
- [skills/app-bgimage.md](skills/app-bgimage.md) — 背景画像生成（参照画像で寄せる固定テンプレ寄り）
- [skills/app-thumbnail.md](skills/app-thumbnail.md) — AI サムネ生成（ベンチ分析から動的プロンプト構築 → Flow/Codex 並列）
- [skills/app-web-dashboard.md](skills/app-web-dashboard.md) — Web UI / API / History API
- 個別スキルは [skills/](skills/) ディレクトリ内の各 `.md` を参照

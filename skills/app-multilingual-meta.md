# 多言語メタ生成 + YouTube メタ更新スキル

## 役割

YouTube 動画の **多言語化メタデータ**（タイトル/説明文を 10 言語翻訳）を Claude CLI で自動生成し、`videos.update` API で既存動画にも反映する一連のフロー。動画ファイル再アップロード不要で、quota 50 unit / vol のコストでメタだけ差し替え可能。

## 対応言語（既定 10 言語）

```
ja / zh-Hans / zh-Hant / ko / es / es-419 / pt-BR / fr / de / it
```

`POST` 時に `languages` を渡すと上書き可能。

## API エンドポイント

### `POST /api/videos/{video_name}/generate-localizations`
Claude CLI で 10 言語翻訳 → `<vol_folder>/youtube_localizations.json` を生成。

- **入力**: `<vol_folder>/youtube_title.txt` + `youtube_description.txt`（事前に meta step / suggest API で準備）
- **オプション** (`body`): `languages: [...]`（未指定なら既定 10 言語）, `force: true`（既存ファイル上書き）
- **出力**: `{"status": "ok", "languages": [...], "output_path": "..."}`

### `GET /api/videos/{video_name}/meta-status`
タイトル/説明/タグ/localizations/mp4 の充足状況を返す（Web UI チェックリスト用）。

```json
{
  "video_name": "11_HN_260523",
  "title": {"present": true, "size": 46, "ok": true, "preview": "Endless Blue Horizon | ..."},
  "description": {"present": true, "size": 1155, "ok": true, "preview": "..."},
  "tags": {"present": true, "size": 378, "ok": true},
  "localizations": {"present": true, "count": 10, "languages": [...], "ok": true},
  "mp4": {"present": true, "path": "/Volumes/SSD/.../HN_vol11.mp4", "size": ...},
  "upload": {"present": true, "video_id": "vC-jQztcX6c", "url": "https://youtu.be/...", "localizations_applied": []},
  "ready_for_upload": true
}
```

### `POST /api/youtube/update-snippet/{video_name}`
既存動画 (video_id) のタイトル/説明/タグ/言語/localizations を **YouTube videos.update API** で更新（動画ファイルは再アップロードしない、quota ~50 unit）。

- **前提**: `<vol_folder>/youtube_upload.json` に `video_id` がキャッシュ済（過去にアップロードしたもの）
- **入力**: `<vol_folder>` の `.txt` + `youtube_localizations.json`
- **body**: `{"apply_localizations": true/false}`
- **出力**: `{"status": "ok", "video_id": "...", "applied_parts": ["snippet","localizations"], "localizations_applied": [...], "previous_title": "...", "new_title": "..."}`

### `POST /api/youtube/batch-upload`
複数 vol を一括 enqueue（順次処理）。bash + curl polling の脆さを排除。

- **body**: `{"video_names": [...], "privacy": "unlisted"}`
- **出力**: `{"status": "ok", "enqueued": [...], "skipped": [...]}`
- quota 枯渇時は `YT_QUOTA_EXHAUSTED` で残りスキップ、翌日 scheduler 再開

### `GET /api/videos/{video_name}/mp4-info`
mp4 をどこから検出したか + 解像度・尺・コーデック・サイズを返す（誤判定防止）。

```json
{
  "present": true,
  "path": "/Volumes/SSD/Media_output/HN_vol06.mp4",
  "source": "manual_exported_video",  // or "vol_folder" / "external_ssd_per_video" / "external_ssd_flat"
  "size_bytes": 2711731609,
  "width": 1920, "height": 1080, "resolution": "1920x1080",
  "duration_sec": 5399.99,
  "video_codec": "h264"
}
```

## 運用フロー

```
1. step_meta で youtube_title.txt / description.txt / tags.txt 生成 (pipeline)
2. POST /api/videos/{name}/generate-localizations  ← Claude CLI で 10 言語翻訳
3. POST /api/youtube/upload (新規) or update-snippet/{name} (既存動画)
4. GET /api/videos/{name}/meta-status で充足チェック
```

## per-channel 設定

`.app_channel_config.json` の `youtube_upload_defaults.localization_languages` で対応言語をオーバーライド可能。空なら API 既定の 10 言語。

```json
{
  "youtube_upload_defaults": {
    "default_language": "en",
    "default_audio_language": "en",
    "localization_languages": ["ja", "zh-Hans", "zh-Hant", "ko", "es", "es-419", "pt-BR", "fr", "de", "it", "ru"]
  }
}
```

## quota の制約

YouTube Data API v3:

| 操作 | quota | 1日上限 (9600/24h) |
|---|---|---|
| **videos.insert** (新規アップロード) | 1600 unit | ≈ 6 回/日 |
| **videos.update** (メタ更新) | 50 unit | ≈ 192 回/日 |

つまり「既存動画のメタを多言語化」は **新規アップロードより 32 倍安い**。誤投稿の修正・運用上のメタ整備にはこちらを使うのが筋。

## 関連ファイル

- `_claude/Python/app.py:api_generate_localizations()` — `/api/videos/{name}/generate-localizations`
- `_claude/Python/app.py:api_meta_status()` — `/api/videos/{name}/meta-status`
- `_claude/Python/app.py:api_youtube_update_snippet()` — `/api/youtube/update-snippet/{name}`
- `_claude/Python/app.py:api_youtube_batch_upload()` — `/api/youtube/batch-upload`
- `_claude/Python/app.py:api_mp4_info()` — `/api/videos/{name}/mp4-info`
- `_claude/Python/app_youtube.py:update_video_snippet()` — videos.update 本体（snippet + localizations 反映）
- `_claude/Python/app_youtube.py:load_localizations()` — `youtube_localizations.json` 読み込み

## 関連スキル

- [app-psd-composite.md](app-psd-composite.md) — サムネ/背景画像合成
- [app-youtube-upload.md](app-youtube-upload.md) — 既存のアップロード（snippet 投入）
- [app-workflow.md](app-workflow.md) — pipeline 全体

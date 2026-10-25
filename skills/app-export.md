# app-export: FFmpeg 書き出し（app_ffrender）

`export` 工程は `Python/app_ffrender.py` が担う。Premiere / Media Encoder は使わない（2026-10-25 段階 1a で廃止）。

## 概要
- 入力: `music/*.mp3`（処理済み楽曲）、背景画像 `vol{N}.jpg`（画像合成の出力。無ければ `vol{N}.png` / `vol{N}_source.jpg`）、チャンネル設定（`ffrender`、`default_duration_sec`、`export_path`）
- 出力: `{prefix}_vol{N}.mp4`（`export_path` 指定時は外部ストレージ直下に flat 配置）、`subtitles_{N}.srt`、`music_time_code_info_{N}.txt`、構成の正本 `.ffrender_manifest.json`
- 方式: 静止画を 1 GOP だけエンコードし `-stream_loop` と `-c copy` で目標尺まで連結。音声は concat → フェード → リミッター。字幕とタイムコードは `srt_timecode.py` の純関数で生成する。

## 入口
```bash
python3 Python/studio.py export --vol <N> --dry-run      # 解決結果の確認
python3 Python/app_pipeline.py <N> --only export         # 実行（premiere 工程は自動で通過する）
python3 Python/app_ffrender.py --vol-folder <folder> --duration <sec> --output-path <mp4>   # 直接実行
```

## 主なオプション（app_ffrender.py）
- `--duration` 目標尺（秒）。未指定ならチャンネル設定 `default_duration_sec`、最後は 10800
- `--burn-titles always|none` 曲名の焼き込み
- `--swap '旧タイトル=新.mp3'` / `--remove 'タイトル'` 構成の差し替え（著作権対応）。`--no-render` で manifest 操作だけ
- `--show-manifest` 現在の構成を表示

## 前提
- `ffmpeg` / `ffprobe`（`brew install ffmpeg`）と Pillow。`python3 Python/studio.py preflight --json` で確認できる
- 並列: CPU 律速。`routes.json` の `export` は per-machine 1 だが、Adobe のような物理的な単一制約は無い

## 失敗時
- preflight exit 78: ffmpeg / ffprobe / Pillow が無い
- 背景画像が無い: `bgimage` と `psd_composite`（画像合成）を先に実行する
- MP4 が見つからない: `export_path` のマウント（`/Volumes/...`）と `_find_exported_mp4` の探索先を確認する

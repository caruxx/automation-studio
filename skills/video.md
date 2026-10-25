# video: FFmpeg 書き出しドメイン

Adobe（Premiere / Media Encoder）は使わない（2026-10-25 段階 1a で廃止、`production_mode` は `ffmpeg_only` のみ）。動画・字幕・タイムコードは `app_ffrender.py` が一括生成する。

## 目的
処理済み楽曲と背景画像から MP4 を書き出し、SRT 字幕とタイムコードを揃える。

## 入口コマンド
- 書き出し: `python3 Python/studio.py export --vol <N> --dry-run`
- 再開: `python3 Python/app_pipeline.py <N> --from export`
- 実行環境の検査: `python3 Python/studio.py preflight --json`

## 前提リソース
- ffmpeg / ffprobe（書き出しと QA）、Pillow（画像合成）

## 並列可否
- CPU 律速。`premiere` 工程は何もせず成功し、`export` が `app_ffrender.py` を使う。
- 単一リソースのロックは不要（SUNO ブラウザだけが単一）。

## 典型手順
1. `export` 前に `music/*.mp3` と `vol{N}.jpg`（画像合成の出力）があるか確認。
2. `app_pipeline.py` の preflight は ffmpeg / ffprobe / Pillow の存在だけを見る（不足は exit 78）。
3. QA は ffprobe で解像度・尺・コーデックを確認し `qa_report.json` を書く。

## 失敗時の対処
- preflight exit 78: `brew install ffmpeg`、`pip install pillow`。
- MP4 が見つからない: 外部 export path と `_find_exported_mp4` 対象を確認。
- 詳細は [app-export.md](./app-export.md)。

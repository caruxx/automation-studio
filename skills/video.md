# video: Premiere / AME / ffmpeg 書き出しドメイン

## 目的
Premiere プロジェクトを自動オープンし、音声・背景・字幕を配置し、AME または ffmpeg engine で MP4 を書き出す。

## 入口コマンド
- 配置: `python3 Python/studio.py premiere --vol <N> --dry-run`
- 書き出し: `python3 Python/studio.py export --vol <N> --dry-run`
- 再開: `python3 Python/app_pipeline.py <N> --from premiere`

## 前提リソース
- Premiere Pro + Premiere Link / JSX Launcher
- Adobe Media Encoder（`export_engine=ame`）
- ffmpeg / ffprobe（`export_engine=ffmpeg` または QA）

## 並列可否
- Premiere/AME は単一リソース。
- `export_engine=ffmpeg` のチャンネルでは `premiere` step はスキップされ、`export` が `app_ffrender.py` を使う。
- opt-in ロック: `python3 Python/parallel_guard.py premiere -- python3 Python/app_pipeline.py <N> --only premiere`
- 書き出しも同じ物理資源なので `export` lock を使う。

## 典型手順
1. `premiere` 前に `music/*.mp3` と背景画像があるか確認。
2. `app_pipeline.py` は preflight を行い、`export_engine=ffmpeg` なら Premiere preflight を不要化する。
3. QA は ffprobe で解像度・尺・コーデックを確認し `qa_report.json` を書く。

## 失敗時の対処
- preflight exit 78: Premiere/CEP パネルを起動・再導入。
- MP4 が見つからない: 外部 export path と `_find_exported_mp4` 対象を確認。
- ffprobe 不在: `brew install ffmpeg`。

# music: SUNO / 楽曲生成・DL・後処理ドメイン

## 目的
SUNO で楽曲を生成し、Workspace から動画フォルダへダウンロードし、リネーム・フェード・ゲイン正規化までつなぐ。

## 入口コマンド
- 正規確認: `python3 Python/studio.py suno-auto --vol <N> --dry-run`
- 実行: `python3 Python/studio.py suno-auto --vol <N> --prompt "<prompt>" --count <count>`
- 直接: `python3 Python/app_pipeline.py <N> --only suno`

## 前提リソース
- SUNO ログイン済み Playwright 永続ブラウザ
- Claude/Codex CLI（GhostWriter / batch 起草用）
- ffmpeg / ffprobe（後処理・QA）

## 並列可否
- SUNO ブラウザは単一リソース。vol 跨ぎも順次。
- opt-in ロック: `python3 Python/parallel_guard.py suno -- python3 Python/app_pipeline.py <N> --only suno`
- ダウンロードだけでも SUNO ブラウザを使うため `suno-download` ロックを使う。

## 典型手順
1. `studio.py <intent> --dry-run` で解決結果とコマンドを確認。
2. `plan.json` または `APP_SUNO_PROMPT` / channel config の `suno.prompt` があるか確認。
3. `suno_auto_create.py --batch` で一括起草し、Workspace 名を vol 別にする。
4. DL 後に `app_process_tracks.py <folder>` で `music/` へ整備。

## 失敗時の対処
- ログイン要求: ブラウザで手動ログインして再実行。
- cache miss: Playwright コンテキストを閉じ、`--download-workspace` を再実行。
- Bot 判定: `APP_KEEP_BROWSER=1`、interval を長めにする。
- ffmpeg 不在: `brew install ffmpeg`。

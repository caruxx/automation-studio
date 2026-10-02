# music: SUNO / 楽曲生成・DL・後処理ドメイン

## 目的
SUNO で楽曲を生成し、Workspace から動画フォルダへダウンロードし、リネーム・フェード・ゲイン正規化までつなぐ。

## Wobble Dayの場合

[Wobble専用入口](wobble-day-music.md)へ分岐する。新制作はインストジャズ（jazzhopを含む）。最新共通briefと選択済み画像を確認し、指定数・尺・設定を読み戻して正式Chrome CUAで生成・保存検証する。以下の旧Playwright前提、beat-soul歌入り条件、曲数の既定をWobbleへ流用しない。

## 承認済みビート系ソウルを再制作する場合

先に [制作レシピ](../docs/music/beat-soul-recipe.md) を読む。ユーザーの修正指示から採用された音楽性へ至った過程、Stylesの判断根拠、歌詞構成、SUNO設定、検証・修正、他ツールへ渡す共通指示を含む。正確な入力は [原稿集](../docs/music/beat-soul-inputs.md)、機械可読データは [プリセット](../config/music_presets/beat-soul-4min.json)。Fable・Claude・Codexで同じ記録を基準にする。「ラグジュアリーBGM」を静かなジャズやバラードへ読み替えない。

## 入口コマンド

新規SUNOセットは[セミオート選曲の契約](../docs/music/semi-auto-selection.md)に従う。２テイクを両方取得し、Automation Studioのハートで１テイクを採用。短い方の自動採用・不採用原本の削除をしない。未選択は終了コード79で停止し、SUNOを再生成せず選択後にrenameから再開する。タイトルは`app_music_catalog`の永久台帳で全経路のCreate前に予約する。
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

### 長さをCustomで指定する場合

`python3 Python/suno_auto_create.py --duration-seconds 240 ...` で、各曲のフォーム投入時にDurationをCustom・4:00へ設定する。設定JSONでは `duration_seconds: 240`、事前原稿の各曲でも `duration_seconds` を指定できる。入力欄とDurationスライダーの秒数を両方読み戻し、不一致ならその曲を送信しない。Stylesへの「4 minutes」記述だけで代用せず、ダウンロード後もffprobeで実際の尺を確認する。

## 失敗時の対処
- ログイン要求: ブラウザで手動ログインして再実行。
- cache miss: Playwright コンテキストを閉じ、`--download-workspace` を再実行。
- Bot 判定: `APP_KEEP_BROWSER=1`、interval を長めにする。
- ffmpeg 不在: `brew install ffmpeg`。

# 段階 2a: 選曲（AI 既定）・命名の単一モジュール・工程定義（Mac 側）

仕様: docs/SPEC_REVISION_DRAFT_2026-10-25.md §3.3 のうち、VPS の台帳 API・lease・長期トークンに依存しない部分。base: main a387678。統括: Claude Fable 5.1（本人指定）。実装: 統括セッション直接、タスクごとに Opus 5.5 サブエージェントがレビュー。
VPS 側の run API / 認証 / lease（§3.3 前半）は段階 2b として別計画にする（DB 設計と alembic を伴う）。

## §8 未回答事項の仮置き（本人回答で上書きする）
- §8-2 選曲の単位: 各タイトル 1 テイク（現行の ready 契約「各タイトル群から 1 つ」と一致。上位 N 曲は不採用）。
- §8-5 全曲不合格時: 再生成の再試行は段階 3 の run 契約で扱う。本段階では `blocked`（reason=quality）で止め、試行回数を台帳に残す。
- §8-1 回答済み: 既定は AI 選曲。ハートが付いているタイトル群は人の採用として尊重し、付いていない群だけ AI が選ぶ。`(1)` などの連番サフィックス付きタイトルは採用時にリネームする。

## Global Constraints
1. 絵文字を新規に追加しない。個人情報を追加しない。制作経路は ffmpeg_only のみ。
2. 既存の原曲（original_music/ と DL 直後の MP3）を削除・移動しない。既存 vol の music/ を勝手に作り直さない（process_selected の保護を維持）。
3. 外部 LLM 呼び出しはテストで実行しない（差し替え可能にし、既定で測定値だけで採点できる）。テストは隔離（tests/_isolation）。
4. サーバー起動・外部 API・本番 Drive への書き込みをしない。commit は日本語、末尾に Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>。push と PR は本人許可。
5. 終了コードの意味を変えない。選定待ち・品質待機は 79（blocked）。

## Task 1: naming.py と workflow.json の工程・資源定義
- 新設 `Python/naming.py`: vol フォルダ名 `{vol}_{prefix}_{yymmdd}` の parse / build、画像 `vol{N}.png|jpg`、動画 `{channel_key}_vol{N}.mp4`、楽曲ファイル名（安全化 + 連番サフィックス除去 + 重複時の採番）、公開用曲名（`(1)` 等を隠す。app_track_title.public_track_title を委譲）。全て純粋関数。
- 置換: app_pipeline.py の `^\d+_([^_]+)` 2 箇所、publish_schedule.parse_publish_date_from_folder、app_ffrender._title_from、app_timeline.display_title の各解析を naming 経由に（挙動は同じ。既存テスト test_music_review の期待値を維持）。
- `Python/workflow.json` に `stage_order`、`stages[]`（id、depends_on、executor、resources、timeout_seconds、retry、done_validator 名）、`resources[]`（suno: global 1、render: machine N、upload: channel 1 など現行の並列設定に合わせる）を追加。現行の STEPS は互換のため据え置き、`stage_aliases`（psd_composite→composite、premiere→（廃止・自動通過）、export→render）を定義して exit_codes.load_workflow で読めるようにする。コードの工程名切替は段階 2b。
- テスト: `tests/test_naming.py`（parse/build の往復、サフィックス除去、安全化、重複採番）、`tests/test_workflow_definition.py`（依存の非循環、alias の整合、資源 id の存在）。
- 受入: 既存 93+ テストが通り、解析結果が置換前と一致（置換箇所ごとに同値テスト）。

## Task 2: music_scoring.py と AI 選曲（select）
- 新設 `Python/music_scoring.py`: `measure(path)` → {duration_sec, silence_ratio, lufs, true_peak}（ffprobe と ffmpeg の silencedetect / ebur128。ffmpeg 不在は None）。`prompt_fit(title, prompt, llm=None)` → 0..1（llm 省略時は None）。`score(measure, fit, policy)` → {score, reasons[], passed}。policy はチャンネル設定 `music_selection.{min_sec, max_sec, max_silence_ratio, lufs_range, weights}`（既定: 120〜900 秒、無音率 ≤ 0.15、LUFS -20〜-8）。
- app_music_catalog: `take_scores` テーブル（batch_id, clip_id, duration_sec, silence_ratio, lufs, prompt_fit, score, passed, reason, scored_at）と `select_takes_ai(folder, *, policy, measurer, llm)`。ハートが無いタイトル群だけ対象。群内の passed から最高 score を choose_take。群に passed が無ければその群は未選定のまま残し、全群が不合格なら SelectionPending(reason="quality") を上げる。結果は `.music_review.json` にも出す。
- チャンネル設定 `music_selection_mode: ai | human`（既定 ai。未設定は ai）。app_pipeline._music_selection_gate: mode が ai で未選定なら select_takes_ai を 1 回実行してから再判定。human は従来どおり。
- studio.py / routes.json に `select` intent（`studio.py select <vol> --json`、dry-run 対応）。
- テスト: 測定を差し替えた採点（境界、重み、不合格理由）、ハート群の尊重、全不合格の blocked、gate の ai/human 分岐（ffmpeg 不要）。
- 受入: 2 群（1 群ハートあり・1 群なし）で AI が 1 群だけ選び、ready になる。全不合格で 79 相当の待機。

## Task 3: 採用時のリネーム規則と保存先の統制
- process_selected の出力名を naming.music_filename（連番サフィックス除去 + 安全化 + 群内重複の採番）に統一し、`_state` の processed 判定も同じ関数で比較。ID3 の title タグは公開用曲名（app_process_tracks のタグ付与経路があれば流用、無ければ mutagen 等を足さず見送り、report に明記）。
- 原曲は `original_music/{元ファイル名}` に残す現行を維持（clip_id 名への移行は §4 の移行表で扱う）。vol フォルダ未指定で呼ばれた場合はエラー（暗黙の既定フォルダを使わない）。
- テスト: `Silver Avenue(1)` 採用 → `music/Silver Avenue.mp3`、`(1)` と `(2)` が同群で共存しない、既存 music/ の保護が維持される。

## 検証の記録
各タスクの report にコマンド・環境・SHA・結果を残す。最終レビューはブランチ全体を Opus サブエージェント → 独立最終レビュー（別セッションの Opus 5.5 中 + Codex Sol 6.1 medium）。

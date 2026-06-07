# Automation Studio — Claude Code プロジェクト指示

orzz. ダッシュボード = YouTube BGM チャンネルの動画制作を全自動化するツール。

## 必読
- [AGENTS.md](AGENTS.md) — 運用コマンド＋「自然言語→実行」マッピング
- [SPEC.md](SPEC.md) — API一覧・データ契約・アーキテクチャ
- [AGENTS_DESIGN.md](AGENTS_DESIGN.md) / [AGENTS_WORKPLAN.md](AGENTS_WORKPLAN.md) — エージェント化の設計と計画
- [skills/](skills/) — 機能別スキル

## 開発時の鉄則
- ⚠ サーバーは uvicorn **reload無し**。Python変更後は `bash Python/start.sh` で再起動しないと反映されない。
- ⚠ Premiere JSX は **JSX Launcher 拡張経由でのみ**実行（AppleScript不可）。
- ⚠ pipeline stage 追加時は `STEPS` / `STEP_LABELS` / `STEP_FUNCS` / `RETRY_POLICY` の**4箇所を一貫更新**。
- sentinel exit: 0成功 / 1失敗 / 75 unattended_login / 76 retryable / 77 quota_exhausted / 78 preflight_fail
- 分析情報は日本語。YouTube出力メタ(title/description/tags)の**ソース言語はチャンネル別**（per-channel `youtube_upload_defaults.default_language`＝メイン言語、設定タブで選択。既定 en／例: orzz=en・SUKIMA=ja）。`localization` step がメイン言語→他言語へ翻訳（メイン言語自体は除外）。

## 並列処理ルール（複数 vol 同時制作）
**別リソースは並列・同一リソースは順次**。リソース競合マップ:

| リソース | 使う処理 | 並列性 |
|---|---|---|
| SUNO ブラウザ | 楽曲生成・DL | **単一**（vol 跨ぎは順次。workspace 名を vol 別 `sk_vol3`/`sk_vol4` にして混在 DL 回避） |
| Premiere/AME | premiere・export | **単一** |
| Photoshop | psd_composite | **単一** |
| Claude/Codex (LLM) | meta・localization・scene_text・SUNO GhostWriter・bgimage | competition 注意 |
| ffmpeg | 楽曲後処理 | 並列可（CPU） |
| YouTube | upload | チャンネル別 token |

- **並列 OK（別リソース）**: `export(Premiere)` ∥ `別vol SUNO生成(ブラウザ+codex)` ／ `DL(SUNO)` ∥ `サムネ(bgimage=codex画像 + psd_composite=Photoshop)` ／ `後処理(ffmpeg)` ∥ `別vol サムネ`
- **並列 NG（同一リソース→順次）**: premiere 同士・SUNO 同士・Photoshop 同士
- ⚠ LLM step（meta/localization）を `--via-api` で実行すると `_api_post` の **10 秒 timeout** で必ず失敗 → **via-api 無し CLI**（`python3 app_pipeline.py <vol> --only meta`、claude_proposer 直接）で実行
- ⚠ premiere/export の「✅完了」ログは `_api_poll` の **早期誤判定**あり → mp4 サイズ安定 + ffprobe で実完了を裏取り
- ⚠ 複数 vol の SUNO は **workspace を vol 別**にして混在 DL を防ぐ。生成リクエスト送信 ≠ レンダリング完了（DL 前に待機 or cache miss 再試行）

## サブエージェント（.claude/agents/）
ドメイン別に委譲: music / image / video / publish / analysis / pipeline / web（詳細 AGENTS_DESIGN §2）。
全エージェント model=opus・tools 無制限（完全権限委譲。Bash でサーバー再起動して検証まで自走）。

## 言語
ユーザーとのやり取り・コメントは日本語。

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
- 分析情報は日本語、YouTube出力メタ(title/description/tags)は英語。多言語は英語ソースから翻訳。

## サブエージェント（.claude/agents/）
ドメイン別に委譲: music / image / video / publish / analysis / pipeline / web（詳細 AGENTS_DESIGN §2）。
全エージェント model=opus・tools 無制限（完全権限委譲。Bash でサーバー再起動して検証まで自走）。

## 言語
ユーザーとのやり取り・コメントは日本語。

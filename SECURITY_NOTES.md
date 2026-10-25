# セキュリティノート

このプロジェクトで利用する **第三者中継 API** に関する監査記録と緩和策。

## 1. AceDataCloud Midjourney API

撤去済み（2026-06-07 D8）。実装・設定キー・UI は残っていない。監査記録は git 履歴（2026-05-17 時点）を参照。

---

## 2. その他の第三者連携 (既存)

### 2.1 Codex CLI (OpenAI ChatGPT サブスクリプション)

- 認証: `~/.codex/` 配下の token、ChatGPT.com ログイン経由
- リスク: 使用量上限到達でサイレント失敗 → 検出ロジック追加済 ([codex_imagegen.py](Python/codex_imagegen.py))
- 状態: 2026-05-20 まで使用量上限到達中

### 2.2 Claude CLI (Anthropic Max サブスクリプション)

- 認証: claude.ai ログイン、`subscriptionType: max`
- リスク: 同上 (CLI サブスクリプションの使用量制限)
- 状態: 通常運用中

### 2.3 Google Flow / Nano Banana 2

撤去済み（2026-06-07 D8）。画像生成は OpenAI Image API（`OPENAI_API_KEY`）または Codex CLI のみ。

---

## 3. Token の取り扱いポリシー

1. **保存先**: `~/.config/orzz/dashboard_config.json` (chmod 600 推奨)
2. **UI 入力**: `type="password"` で値が画面に出ない
3. **ログ出力禁止**: API リクエスト時に Authorization ヘッダはログに含めない
4. **画面マスク**: 設定画面の表示時は `sk-...***` 形式で末尾のみ表示
5. **送信先固定**: HTTPS のみ、ホスト名は `api.acedata.cloud` で固定 (DNS リバインディング対策)

## 4. 監査の継続

- このノートは API 提供者の変更・サービス停止・規約変更を都度反映する
- ユーザーの判断で「リスクを承知で組み込む」と決定済 (2026-05-17 ユーザー指示)

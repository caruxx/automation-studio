# セキュリティノート

このプロジェクトで利用する **第三者中継 API** に関する監査記録と緩和策。

## 1. AceDataCloud Midjourney API

**監査日**: 2026-05-17

### 1.1 概要

- 提供元: AceDataCloud (中華系 AI API 中継業者、法人実体不透明)
- 用途: Midjourney 画像生成への非公式 API 中継
- 通信先: `https://api.acedata.cloud`
- 認証: Bearer token (`ACEDATACLOUD_API_TOKEN`)
- クライアント実装: [github.com/AceDataCloud/MidjourneyCli](https://github.com/AceDataCloud/MidjourneyCli) (MIT)

### 1.2 既知のリスク

| カテゴリ | リスク | 緩和策 |
|---------|--------|--------|
| **プロジェクト信頼性** | GitHub Star/Fork が 0/0 (CLI 版)、運営履歴不透明 | コードは git clone でローカル監査済み (POST httpx のみ、eval/exec なし) |
| **データ漏洩** | プロンプト・参照画像・生成画像が AceDataCloud 中継サーバーを通過。プライバシーポリシー不明確 | 機密情報をプロンプトに含めない (チャンネル戦略文書等は別系統で扱う) |
| **Midjourney 規約違反** | Midjourney 公式 API は未公開。第三者中継は TOS グレー〜違反 | 業務上クリティカルな案件ではこの経路を避ける |
| **持続性** | Midjourney 側がブロックすれば即停止、AceDataCloud 自体の突然終了リスク | Flow / OpenAI gpt-image-2 をフォールバックとして併用 |
| **Token 漏洩** | token が漏れると不正請求 | `dashboard_config.json` に保存 (600 権限)、git ignore、UI でマスク入力 |
| **レート制限不明** | 上限・課金体系が README に未記載 | リクエスト前に dry-run プラン表示、最大同時 1 リクエストに制限 |

### 1.3 採用した緩和策 (本実装)

1. **プロバイダー切替式**: 既定は Codex / Flow。Midjourney は明示選択時のみ呼ばれる
2. **Token は dashboard_config.json に分離保存** (環境変数より UI で管理しやすい)
3. **ログマスク**: `task_logs` に token 本体は出さない (Authorization ヘッダはログ出力対象外)
4. **タイムアウト 300 秒**: ハングアップ防止
5. **エラーパターン明示化**: 401/403 を「認証失敗」として UI に明示
6. **同時実行 1 件まで**: バッチ内では 1 動画ずつ直列で呼ぶ (レート制限とコスト暴走防止)
7. **既存サムネ保護**: `start_index` で v 番号オフセット、上書き禁止 (既存仕様継承)

### 1.4 採用しなかった選択肢

| 選択肢 | 採用しなかった理由 |
|--------|------------------|
| AceDataCloud MCP サーバー | 自前バッチ処理に組み込むには stdio/SSE 経由の MCP クライアント実装が必要で複雑度↑ |
| midjourney-pro-cli (subprocess) | PyPI 未公開 → git clone 必要、Python 依存追加。REST 直叩きの方が監査範囲が小さい |
| 公式 Midjourney Discord 自動操作 | TOS 違反明確、Discord アカウント BAN リスク高 |

### 1.5 推奨される代替案

セキュリティ最重視の用途には：
- **Replicate.com** (SOC 2 認証、公式 Flux/Imagen3 API)
- **fal.ai** (公式 API、高速)
- **ComfyUI ローカル実行** (データが一切外部に出ない)
- **OpenAI gpt-image-2** (API key、公式)

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

- 認証: Chromium に保存された Google アカウント session
- リスク: Chromium 自動操作なので Google アカウント保護機能がアラート出すケースあり
- 状態: 利用可能

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

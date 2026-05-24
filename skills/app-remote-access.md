# app-remote-access: 外出先スマホ操作（Cloudflare Tunnel + 認証 + PWA）

PC を立ち上げておくだけで、**外出先のスマホ**からダッシュボードを操作できる構成。
Cloudflare Tunnel で外部公開 + トークン認証 + PWA でホーム画面追加。

## 全体構成

```
[スマホ] ───HTTPS───▶ [Cloudflare Tunnel (trycloudflare.com)]
                           │
                           ▼
                     [PC: uvicorn localhost:8888]
                           │
                           ├─ 認証ミドルウェア (Bearer + Cookie)
                           ├─ APScheduler (定期ジョブ)
                           └─ WebSocket (リアルタイムログ)
```

## 3 つのコンポーネント

| # | コンポーネント | ファイル |
|---|-------------|---------|
| 1 | 認証ミドルウェア | [Python/app.py](../Python/app.py) `auth_middleware` |
| 2 | Cloudflare Tunnel | [Python/setup_tunnel.sh](../Python/setup_tunnel.sh) |
| 3 | PWA + ログイン画面 | `web/static/manifest.json` / `sw.js` / `login.html` |

## 1. 認証ミドルウェア

### 有効化

環境変数 `ORZZ_AUTH_REQUIRED=1` で起動した場合**のみ**有効。既定は OFF（ローカル運用を壊さない）:

```bash
# 認証 OFF（ローカルのみ）
bash Python/start.sh

# 認証 ON（外部公開用）
ORZZ_AUTH_REQUIRED=1 bash Python/start.sh
```

### ルール

- `127.0.0.1` / `::1` / `localhost` からのアクセスは **常に認証スキップ**（PC 自身からの操作）
- `/login.html` / `/api/auth/*` / `/manifest.json` / `/sw.js` / `/static/*` / `/ws/*` は公開
- それ以外は `Authorization: Bearer <token>` または Cookie `orzz_token` が必要
- トークン不一致 HTML リクエスト → `/login.html?next=...` へ 302 リダイレクト
- トークン不一致 API リクエスト → `401 {"detail":"認証が必要です"}`

### トークン

`~/.config/{app_id}/auth_token.txt` に保存（600 パーミッション）。未存在なら `secrets.token_urlsafe(24)` で自動生成。

### フロント fetch ラッパー

[web/static/index.html](../web/static/index.html) の先頭スクリプトで、
全 fetch に `localStorage['orzz_token']` を自動で Bearer ヘッダー付与:

```javascript
window.fetch = function(input, init){
  const tok = localStorage.getItem('orzz_token');
  if(tok){
    init = init || {};
    const headers = new Headers(init.headers || {});
    if(!headers.has('Authorization')) headers.set('Authorization', 'Bearer '+tok);
    init.headers = headers;
  }
  return _origFetch(input, init);
};
```

### 認証 API

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/auth/login` | トークン検証 → Cookie セット + 200 `{status:"ok"}` |
| GET | `/api/auth/check` | 認証の必要性と現在の状態を返す（ログイン画面判断用） |
| POST | `/api/auth/logout` | Cookie 削除 |
| POST | `/api/auth/regenerate-token` | トークン再生成（既存セッション全無効化） |

### マスター設定 UI

「⚙ マスター設定」→「🌐 リモートアクセス」で:
- トークン表示（readonly）
- 📋 コピー
- ♻ 再生成（確認モーダル経由）
- QR コード表示（Tunnel URL/login.html に直行）

## 2. Cloudflare Tunnel

### 初回セットアップ

```bash
brew install cloudflared
```

### 起動

**別ターミナル**で:

```bash
cd "/Users/abe_kota/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude"
bash Python/setup_tunnel.sh
```

数秒で `https://xxxxx-yyyyy-zzzzz.trycloudflare.com` が表示される。
**このターミナルは起動しっぱなし**にする（閉じると URL 無効）。

### URL 保存

- PC ブラウザ → マスター設定 → リモートアクセス → Tunnel URL 欄に貼り付け → 「URL を保存」
- 「📱 QR コード表示」で QR 生成（`{tunnel_url}/login.html` を指す）
- スマホで QR 読み取り → ログイン画面 → トークン入力

### Quick Tunnel の制約

- **URL は起動ごとに変わる**（random サブドメイン）
- Cloudflare アカウント不要
- 永続 URL が欲しい場合は名前付き Tunnel の設定が別途必要（`cloudflared tunnel create`）

### スクリプト内容

[Python/setup_tunnel.sh](../Python/setup_tunnel.sh):
- `cloudflared` 有無チェック → 無ければ brew install コマンド表示
- `--check` で存在確認のみ
- 通常起動時は `cloudflared tunnel --url http://localhost:${ORZZ_PORT:-8888}`

## 3. PWA（Progressive Web App）

### manifest.json

[web/static/manifest.json](../web/static/manifest.json):
- `display: "standalone"` — ホーム画面追加時は全画面アプリ風
- アイコンは inline SVG で自己完結（外部画像ファイル不要）
- `theme_color: "#0a0a0a"` — Design.md の `--bg-base`

### Service Worker

[web/static/sw.js](../web/static/sw.js):
- 静的ファイルのみキャッシュ（index.html / manifest.json）
- `/api/*` と `/ws/*` は常にネットワーク経由（キャッシュしない）
- オフライン時の最低限の UI 表示のみサポート

### index.html メタタグ

```html
<meta name="viewport" content="width=device-width,initial-scale=1.0,viewport-fit=cover">
<meta name="theme-color" content="#0a0a0a">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
```

### モバイル最適化 CSS

600px ブレークポイント（[web/static/index.html](../web/static/index.html) 内 CSS）:
- ボタンは最小 44px（iOS ガイドライン）
- タブは横スクロール（フレックスラップ解除）
- StatCard グリッドは縦積み（1fr）
- 入力フィールドは 16px フォント（iOS で Zoom されないサイズ）

### ホーム画面追加

- iOS Safari: 共有ボタン → 「ホーム画面に追加」
- Android Chrome: メニュー → 「アプリをインストール」
→ アイコンタップで全画面起動。ブラウザ UI 非表示で**ネイティブアプリっぽく**使える。

## ログイン画面

[web/static/login.html](../web/static/login.html) はスタンドアロン HTML:
- Design.md のトークン（`--bg-base`, `--accent-primary` 等）をインライン CSS で複製
- トークン入力 → `/api/auth/login` → 成功時 localStorage にも保存（fetch ラッパー用）→ `next` パラメータのページに遷移
- SW 登録コードも含む（初回でも PWA 対応）

## 運用フロー（実例）

### 外出先からの緊急動画生成

1. **自宅 PC**で `ORZZ_AUTH_REQUIRED=1 bash Python/start.sh` + `bash Python/setup_tunnel.sh` を起動しっぱなし
2. **外出先**でスマホから PWA 起動（ホーム画面のアイコン）
3. ログイン（初回のみ、以降は Cookie で自動）
4. マスター設定 → 自動化スケジュール → 「+ ジョブを追加」→ `spot_create` でスポット指定
5. 実行後 Discord 通知で完了確認

### 出先で分析結果を確認

1. PWA 起動 → ベンチマークタブ
2. ホットチャンネル一覧 + 投稿時刻ヒートマップを閲覧
3. 動画詳細 → メタタブ → 「🧬 徹底パクリ進化を提案」で Claude 分析を実行

## セキュリティ注意

- **Tunnel URL は推測困難**だが、公開すると誰でもトップページにはアクセスできる（認証前）。`ORZZ_AUTH_REQUIRED=1` で起動しないと API は素通し
- **トークン漏洩時は再生成**: マスター設定 → リモートアクセス → 「♻ 再生成」
- **cookie は 30 日**: `samesite=lax`, `httponly` 付き。HTTPS 前提（Cloudflare Tunnel は自動 HTTPS）
- **Cloudflare Terms**: Quick Tunnel は商用・本番用途は禁止。長期運用には名前付き Tunnel 推奨

## 代替選択肢（参考）

| 方式 | 難度 | 固定 URL | 備考 |
|------|------|---------|------|
| Cloudflare Quick Tunnel（現在） | 低 | ✗ | アカウント不要、URL は毎回変わる |
| Cloudflare 名前付き Tunnel | 中 | ✓ | ドメイン必要、無料枠あり |
| Tailscale | 低 | ✓（内部 IP） | VPN、デバイス間のみ |
| ngrok | 低 | ✗（無料枠） | 無料は URL 変動、有料で固定可 |

## 実装チェックリスト

- [x] `auth_middleware` （FastAPI）
- [x] `/api/auth/login` / `check` / `logout` / `regenerate-token`
- [x] `auth_token.txt` 自動生成（600 perms）
- [x] fetch ラッパー（Bearer 自動付与）
- [x] `setup_tunnel.sh`
- [x] `manifest.json` / `sw.js` / `login.html`
- [x] Viewport + theme-color + apple-mobile-web-app メタ
- [x] 600px ブレークポイント（タッチ最適化）
- [x] マスター設定 UI（トークン表示・コピー・再生成・QR）

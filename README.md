# Automation Studio

クリエイター向け制作パイプライン自動化ダッシュボード。SUNO・Adobe Premiere Pro・Adobe Media Encoder・YouTube・Google Flow を 1 つの Web UI から連動実行できます。

| 自動化対象 | 主な機能 |
|---|---|
| 🎵 SUNO | プロンプト生成（Claude/Gemini/ChatGPT）→ ループ生成 → ワンクリック DL → フェード/ゲイン正規化 |
| 🖼 Google Flow | サムネ自動生成（Nano Banana 2、参照画像対応、x4 連続出力 + 2K DL） |
| 🎬 Premiere Pro | JSX 経由での音源・画像自動配置 + シーケンス組み立て |
| 📤 Adobe Media Encoder | キュー監視、書き出し進捗のリアルタイム表示、外部 SSD への自動移動 |
| 📝 メタ生成 | タイトル候補・説明文・タグの AI 生成 |
| 🚀 YouTube | 限定/公開アップロード、説明文の差分編集 |
| 📊 競合分析 | スプシ取り込み + Claude による戦略提案、自動プロンプト反映 |
| ⏱ スケジュール | APScheduler でフルパイプラインを定期駆動、LINE 通知 |

## 動作要件

- **macOS** 13 以降（Premiere Pro / Adobe Media Encoder 連携が前提）
- Python 3.10+
- Adobe Premiere Pro 2024 以降（インストール済み）
- Adobe Media Encoder（同上、Premiere とセットでインストール）

Windows 対応は将来検討中です。

## クイックスタート

### 1. インストール

```bash
git clone https://github.com/yourname/automation-studio.git
cd automation-studio
pipx install ".[all]"   # SUNO/Flow/Premiere 全部入り
# または最小:
pipx install .          # Web ダッシュボードのみ
```

ブラウザ自動化を使う場合は Playwright のブラウザもインストール:

```bash
playwright install chromium
```

### 2. 初回セットアップ

```bash
bash scripts/setup.sh   # Homebrew / Python 依存 / プリセット配置
```

### 3. 起動

```bash
automation-studio       # サーバー起動 → http://localhost:8888
```

ブラウザで開いたら **「⚙ 基本設定」** タブで以下を設定:
1. チャンネル名・チャンネルフォルダ
2. API キー（Gemini / OpenAI / YouTube Data API）
3. ブランド表示名（ヘッダや PWA に表示する任意の名前）

## 主要 UI

| タブ | 用途 |
|---|---|
| ダッシュボード | 全体の実行状況・最近の vol・1 クリック新規動画作成 |
| コンテンツ | 動画フォルダ一覧 + 各 vol の自動実行バー（楽曲→加工→配置→書出→メタ→アップロード） |
| ベンチマーク分析 | 競合チャンネルのプロファイル取得・統合プロンプト生成 |
| 自動化 | パイプライン全自動実行、AME 書き出しキュー監視、スケジューラ |
| 基本設定 | API キー・チャンネル・SUNO 設定・ベンチマーク条件 |
| 詳細設定 | プロンプト本文・SUNO/Flow 詳細パラメータ・スケジュール・リモートアクセス |

## 配布 / 配置のセキュリティ

- **設定ファイルは配布物に含まれません。** すべて `~/.config/{app_id}/` に保存（`app_id` は基本設定で変更可、既定 `orzz`）
- **API キーは UI から個別に保存。** 環境変数依存はありません
- ブランド表示名・新規ファイル prefix を **基本設定タブで自由に変更** 可能（公開用に名前を伏せたい時など）

## ロードマップ

- [x] v0.1: ブランド独立化 / 配布可能化（**現在ここ**）
- [ ] v0.2: Windows 対応 / Docker 化（API ベース機能限定）
- [ ] v0.3: テスト基盤 / CI / PyPI 公開

## ライセンス

MIT。詳細は [LICENSE](LICENSE) 参照。

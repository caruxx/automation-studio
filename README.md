# Automation Studio

クリエイター向け制作パイプライン自動化ダッシュボード。SUNO・Adobe Premiere Pro・Adobe Media Encoder・YouTube・OpenAI 画像生成 を 1 つの Web UI から連動実行できます。

| 自動化対象 | 主な機能 |
|---|---|
| 🎵 SUNO | プロンプト生成（Claude/Gemini/ChatGPT）→ ループ生成 → ワンクリック DL → フェード/ゲイン正規化 |
| 🖼 サムネ制作 | ベンチマーク参照 → AI 背景画像生成 → **Photoshop で文字入れ**（`vol{N}.jpg`＋`サムネイル.jpg` を2枚出し）。一気通貫の正規フロー |
| ✍️ 文字入れ設定 | サムネの英大文字フレーズ（シーンテキスト）を**チャンネル別**にトーン/例/禁止語で設定、ベンチマークから提案も可 |
| 🎨 AI 画像生成 | OpenAI gpt-image-2（API・並列生成・2K）を背景・AI サムネ生成に利用（codex 一本化。Flow / Midjourney は D8 で撤去） |
| 🎬 Premiere Pro | JSX 経由での音源・画像自動配置 + シーケンス組み立て |
| 📤 Adobe Media Encoder | キュー監視、書き出し進捗のリアルタイム表示、外部 SSD への自動移動 |
| 📝 メタ生成 | タイトル候補・説明文・タグの AI 生成 |
| 🚀 YouTube | 限定/公開アップロード、説明文の差分編集 |
| 📊 競合分析 | スプシ取り込み + Claude による戦略提案、自動プロンプト反映 |
| ⏱ スケジュール | APScheduler でフルパイプラインを定期駆動、LINE 通知 |

## ドキュメント

リポジトリ同梱の HTML 仕様書をブラウザで開くと、全体像を視覚的に把握できます。GitHub 上ではソース表示になるため、クローンまたはダウンロードしてからローカルで開いてください。

| ファイル | 内容 |
|---|---|
| [automation_studio_overview.html](automation_studio_overview.html) | 現状の仕様とできること。前半はどなたでも読めるやさしい解説、後半は技術仕様・プログラム解説・skills 解説・用語集。**まずはこれ** |
| [automation_studio_spec.html](automation_studio_spec.html) | 仕様 & 統合計画 |
| [automation_studio_decisions.html](automation_studio_decisions.html) | 設計上の決定事項（D1-D14） |

開き方（macOS の例）:

```bash
git clone https://github.com/caruxx/automation-studio.git
cd automation-studio
open automation_studio_overview.html   # 既定ブラウザで表示
```

テキストで素早く把握したい場合は [SPEC.md](SPEC.md)（全体仕様）/ [AGENTS.md](AGENTS.md)（操作と自然言語マッピング）も参照してください。

## 動作要件

- **macOS** 13 以降（Premiere Pro / Adobe Media Encoder 連携が前提）
- Python 3.10+
- Adobe Premiere Pro 2024 以降（インストール済み）
- Adobe Media Encoder（同上、Premiere とセットでインストール）

Windows 対応は将来検討中です。

## クイックスタート

### 1. インストール

```bash
git clone https://github.com/caruxx/automation-studio.git
cd automation-studio
pipx install ".[all]"   # SUNO/Premiere 全部入り
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
| 詳細設定 | プロンプト本文・SUNO 詳細パラメータ・スケジュール・リモートアクセス |

## サムネ制作（背景 → 文字入れ）

サムネは **正規パイプライン** `背景画像生成(bgimage) → PSD 合成(psd_composite)` の一気通貫で作ります。

1. 対象動画を開く（**コンテンツ → 動画クリック → 「画像」タブ**）
2. **「🎯 サムネを制作（背景→文字入れ）」** を押す（Photoshop を起動しておく）
   - ベンチマーク参照で背景 `vol{N}.png` を生成 → PSD テンプレに差し込み、文字を入れて
     `vol{N}.jpg`（Premiere 背景用・PLAY LIST 表示）と `サムネイル.jpg`（YouTube 用・文字表示）を出力
   - 「背景だけ再生成」「文字だけ再合成」は同じタブの個別ボタンから
3. まとめて作る場合は一覧で複数選択 → ツールバーの **「🎯 サムネ一括制作」**

> AI が直接サムネを描く「AI サムネ一括生成」は、PSD テンプレが使えない時の**フォールバック**です。

### PSD テンプレの要件（チャンネル別）

`{チャンネルフォルダ}/プロジェクト/{template_psd}` に配置し、基本設定の **PSD レイヤー名**と一致させます（vol 作成時に各 vol フォルダへ `{prefix}_vol{N}.psd` として自動コピー）。

- **背景レイヤー**（スマートオブジェクト必須）= `psd_base_layer`（既定 `base`） … AI 背景がここに差し込まれる
- **トグルレイヤー** = `psd_toggle_layer`（既定 `PLAY LIST `、**末尾スペースに注意**） … 表示/非表示で2枚出し
- **文字レイヤー** = `psd_text_layer`（既定 `都市名_テキスト`） … シーンテキストを中央配置（任意）

## 文字入れ（シーンテキスト）設定

サムネの英大文字フレーズ（例: `QUIET HOURS`）を**チャンネルごと**に制御できます（基本設定 →「文字入れ（シーンテキスト）設定」）。

| 項目 | 説明 |
|---|---|
| 有効/無効 | OFF で文字なし（単一トグル方式） |
| トーン | 例: `chill, lo-fi, study, cozy`（空なら persona 準拠の中立生成） |
| 語感の参考フレーズ | 似た register の例（完全コピーは禁止） |
| 禁止フレーズ | 完全一致を避ける語（ライバルの実際の焼込文字など） |
| 構文ヒント | 空なら `verb+noun / adjective+noun` |

- 空欄の項目は **persona 準拠の中立生成**（特定チャンネルのトーンは混入しません）
- **「ベンチマークから提案」**: ライバルの動画タイトル語彙（軽量）／サムネ画像の実焼込文字を Vision 抽出（精緻）から、トーン・例・禁止語を提案 → 編集して保存

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

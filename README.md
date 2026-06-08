# Automation Studio

クリエイター向け制作パイプライン自動化ダッシュボード。SUNO・Adobe Premiere Pro / Media Encoder / Photoshop・YouTube・OpenAI 画像生成 を 1 つの Web UI から連動実行できます。

> **重要：動画の配置・書き出し・サムネ合成には Adobe Creative Cloud（Premiere Pro / Media Encoder / Photoshop）が必須**で、いずれも**有料サブスク（月額契約）が別途必要**です。楽曲生成・メタ生成・YouTube 投稿は Adobe なしでも動きます。詳細は[必要なもの（動作要件）](#動作要件)を参照。

> **はじめての方・全体像を知りたい方は [OVERVIEW.md](OVERVIEW.md) をご覧ください。**
> 導入方法から仕様（しくみ・AI の役割・11 工程・各プログラム解説）まで、やさしい解説つきでまとめています。

| 自動化対象 | 主な機能 |
|---|---|
| SUNO | プロンプト生成（Claude/Gemini/ChatGPT）→ ループ生成 → ワンクリック DL → フェード/ゲイン正規化 |
| サムネ制作 | ベンチマーク参照 → AI 背景画像生成 → Photoshop で文字入れ（`vol{N}.jpg`＋`サムネイル.jpg` を 2 枚出し）。一気通貫の正規フロー |
| 文字入れ設定 | サムネの英大文字フレーズ（シーンテキスト）をチャンネル別にトーン/例/禁止語で設定、ベンチマークから提案も可 |
| AI 画像生成 | OpenAI gpt-image-2（API・並列生成・2K）を背景・AI サムネ生成に利用（codex 一本化） |
| Premiere Pro | JSX 経由での音源・画像自動配置 + シーケンス組み立て |
| Media Encoder | キュー監視、書き出し進捗のリアルタイム表示、外部 SSD への自動移動 |
| メタ生成 | タイトル候補・説明文・タグの AI 生成・多言語化 |
| YouTube | 限定/公開/予約アップロード、説明文の差分編集 |
| 競合分析 | スプシ取り込み + Claude による戦略提案、自動プロンプト反映 |
| スケジュール | APScheduler でフルパイプラインを定期駆動、Discord 通知 |

## ドキュメント

| ファイル | 内容 |
|---|---|
| [OVERVIEW.md](OVERVIEW.md) | 導入方法と仕様の全体像（やさしい解説 + 技術仕様 + プログラム解説 + skills 解説 + 用語集）。**まずはこれ** |
| [SPEC.md](SPEC.md) | 全体仕様（API 一覧・データ契約・アーキテクチャ） |
| [AGENTS.md](AGENTS.md) | 運用コマンド・自然言語マッピング・エラーリカバリ |
| `automation_studio_overview.html` | OVERVIEW のブラウザ閲覧版（GitHub 上はソース表示。クローン後 `open automation_studio_overview.html` で表示） |
| `skills/` | 機能別の手順書（AI アシスタント用ナレッジ） |

## 動作要件

### 共通・必須
- macOS 13 以降（Windows 対応は将来検討中）
- Python 3.10 以降
- Claude CLI（AI の第一候補・ローカル認証・API キー不要）

### Adobe Creative Cloud（動画工程に必須・有料サブスク）

> Premiere Pro / Media Encoder / Photoshop はいずれも **Adobe Creative Cloud の有料サブスク（月額契約）が別途必要**です。本ツールに Adobe ライセンスは含まれません（Adobe との契約・支払いは各自で行ってください）。

- **Adobe Premiere Pro 2024 以降** … 音源・字幕・画像の自動配置／シーケンス組み立て
- **Adobe Media Encoder** … MP4 書き出し（Premiere とセットで導入）
- **Adobe Photoshop** … サムネ合成・2 枚出し（新版は UXP 連携／旧版は AppleScript）

### API キー・認証（UI から登録）
- OpenAI（背景・AI サムネ画像生成）
- YouTube Data API v3 + OAuth（投稿）
- 任意：Gemini（プロンプト生成の選択肢）、Codex CLI（AI の控え・要 `codex login`）

### Adobe が無くても使える範囲
- 楽曲生成・後処理（SUNO + ffmpeg）／メタ生成・多言語化／競合・ベンチマーク分析
- **Adobe が必須なのは「配置・書き出し・サムネ合成」の 3 工程のみ**

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

ブラウザで開いたら「基本設定」タブで以下を設定:

1. チャンネル名・チャンネルフォルダ
2. API キー（Gemini / OpenAI / YouTube Data API）
3. ブランド表示名（ヘッダや PWA に表示する任意の名前）

AI は Claude CLI を第一候補に使います（API キー不要・ローカル認証）。控えの Codex を使う場合は事前に `codex login` が必要です。

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

サムネは正規パイプライン `背景画像生成(bgimage) → PSD 合成(psd_composite)` の一気通貫で作ります。

1. 対象動画を開く（コンテンツ → 動画クリック → 「画像」タブ）
2. 「サムネを制作（背景→文字入れ）」を押す（Photoshop を起動しておく）
   - ベンチマーク参照で背景 `vol{N}.png` を生成 → PSD テンプレに差し込み、文字を入れて
     `vol{N}.jpg`（Premiere 背景用・PLAY LIST 表示）と `サムネイル.jpg`（YouTube 用・文字表示）を出力
   - 「背景だけ再生成」「文字だけ再合成」は同じタブの個別ボタンから
3. まとめて作る場合は一覧で複数選択 → ツールバーの「サムネ一括制作」

> AI が直接サムネを描く「AI サムネ一括生成」は、PSD テンプレが使えない時のフォールバックです。

### PSD テンプレの要件（チャンネル別）

`{チャンネルフォルダ}/プロジェクト/{template_psd}` に配置し、基本設定の PSD レイヤー名と一致させます（vol 作成時に各 vol フォルダへ `{prefix}_vol{N}.psd` として自動コピー）。

- 背景レイヤー（スマートオブジェクト必須）= `psd_base_layer`（既定 `base`） … AI 背景がここに差し込まれる
- トグルレイヤー = `psd_toggle_layer`（既定 `PLAY LIST `、末尾スペースに注意） … 表示/非表示で 2 枚出し
- 文字レイヤー = `psd_text_layer`（既定 `都市名_テキスト`） … シーンテキストを中央配置（任意）

## 文字入れ（シーンテキスト）設定

サムネの英大文字フレーズ（例: `QUIET HOURS`）をチャンネルごとに制御できます（基本設定 →「文字入れ（シーンテキスト）設定」）。

| 項目 | 説明 |
|---|---|
| 有効/無効 | OFF で文字なし（単一トグル方式） |
| トーン | 例: `chill, lo-fi, study, cozy`（空なら persona 準拠の中立生成） |
| 語感の参考フレーズ | 似た register の例（完全コピーは禁止） |
| 禁止フレーズ | 完全一致を避ける語（ライバルの実際の焼込文字など） |
| 構文ヒント | 空なら `verb+noun / adjective+noun` |

- 空欄の項目は persona 準拠の中立生成（特定チャンネルのトーンは混入しません）
- 「ベンチマークから提案」: ライバルの動画タイトル語彙（軽量）／サムネ画像の実焼込文字を Vision 抽出（精緻）から、トーン・例・禁止語を提案 → 編集して保存

## 配布 / 配置のセキュリティ

- 設定ファイルは配布物に含まれません。すべて `~/.config/{app_id}/` に保存（`app_id` は基本設定で変更可、既定 `orzz`）
- API キーは UI から個別に保存。環境変数依存はありません
- ブランド表示名・新規ファイル prefix を基本設定タブで自由に変更可能（公開用に名前を伏せたい時など）
- 作業ツリー内に生成される `config/`・`competitor_analysis/`・`*.bak.*` は `.gitignore` で除外済み

## ロードマップ

- [x] v0.1: ブランド独立化 / 配布可能化（現在ここ）
- [ ] v0.2: Windows 対応 / Docker 化（API ベース機能限定）
- [ ] v0.3: テスト基盤 / CI / PyPI 公開

## ライセンス

MIT。詳細は [LICENSE](LICENSE) 参照。

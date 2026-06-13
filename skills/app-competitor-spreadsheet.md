# app-competitor-spreadsheet: スプレッドシート競合分析

YouTube Data API を使わず、Google スプレッドシートの公開 CSV から
195 チャンネルの動画データ + 54 チャンネルの日次成長データを取得して
Claude CLI で分析 → タイトル・説明・タグの最適化提案を行う。**API quota ゼロ**。

## データソース

### Sheet 1: チャンネル詳細（195 チャンネル）
- CSV: `https://docs.google.com/spreadsheets/d/YOUR_SHEET1_ID/gviz/tq?tqx=out:csv&gid=YOUR_GID1`
- 編集: `https://docs.google.com/spreadsheets/d/YOUR_SHEET1_ID/edit?gid=YOUR_GID1`
- 更新: **手動**（定期更新ではない）
- 89 列（多数が非表示）

| 列グループ | 表示列 | 非表示列（重要） |
|-----------|--------|----------------|
| 基本情報 | URL, チャンネル名, 地域, 説明文 | **動画数, 総再生, 登録者, 開設日** |
| TOP 動画 ×5 | サムネ, タイトル, URL | **公開日, 公開日時, 再生数, いいね, コメント** |
| 新着動画 ×5 | サムネ, タイトル, URL | **公開日, 公開日時, 再生数, いいね** |

### Sheet 2: 成長トラッキング（54 チャンネル）
- CSV: `https://docs.google.com/spreadsheets/d/YOUR_SHEET2_ID/gviz/tq?tqx=out:csv&gid=YOUR_GID2`
- 編集: `https://docs.google.com/spreadsheets/d/YOUR_SHEET2_ID/edit?gid=YOUR_GID2`
- 更新: **Channel Tracker が自動更新**（日次）

| 列 | 内容 |
|----|------|
| チャンネル名 | 54 チャンネル |
| 総再生回数 / 登録者数 | 規模 |
| 前日比再生数 / 登録者数 | 直近の伸び |
| **直近伸び率 (%)** | 成長率 |

## フロー

```
Sheet 2 (自動更新) → ホットチャンネル TOP15 を複合スコアで特定
  ↓
Sheet 1 でそのチャンネルの TOP5/新着5 動画データをマッチング
  ↓
既存の competitor_data スキーマに変換（YouTube API パスと同一形式）
  ↓
Claude CLI に渡して分析（成長データ込みプロンプト）
  ↓
タイトル / 説明 / タグ 提案
```

## ホットチャンネル特定ロジック

`identify_hot_channels()` の複合スコア:

```
score = (growth_rate / max_rate) × 0.4
      + (daily_view_change / max_views) × 0.4
      + (daily_sub_change / max_subs) × 0.2
```

- 伸び率が高くても規模が小さいチャンネルを過度に優先しない
- 絶対的な日次再生増分も 40% の重みで加味
- 結果として「今勢いがあり、かつ実質的な視聴者を獲得しているチャンネル」が上位に

## チャンネルマッチング

Sheet 2（54ch）→ Sheet 1（195ch）の名前マッチング:

1. 完全一致（45/54 マッチ）
2. 大小無視
3. fuzzy（`SequenceMatcher` ratio > 0.8）
4. マッチ不可（9ch）→ 成長データのみ（動画データなし）で分析に含める

## 実装

| ファイル | 役割 |
|---------|------|
| [Python/app_sheets.py](../Python/app_sheets.py) | CSV取得 / パース / マッチング / スキーマ変換 |
| [Python/app_competitor.py](../Python/app_competitor.py) | **ライバル優先 → スプシフォールバック**（v3 で反転）/ 分析実行 / キャッシュ管理 |

## API

| メソッド | パス | 用途 |
|---------|------|------|
| GET | `/api/analysis/spreadsheet-preview` | 接続テスト（チャンネル数 + ホット上位 5） |
| GET | `/api/analysis/hot-channels?top_n=10` | ホットチャンネル + Sheet1 結合で最新動画サムネ付き（v2 拡張） |
| GET | `/api/analysis/overview` | **StatCard 用サマリ**（総チャンネル数 / 週次成長 TOP3 / 急上昇 TOP3 / 更新日時）（v2） |
| GET | `/api/analysis/benchmark-videos` | ピン留め対象の TOP/最新動画一覧（動画詳細サイドパネル用）（v2） |
| GET | `/api/analysis/posting-times` | 曜日×時刻 7×24 ヒートマップ集計（v2） |
| GET | `/api/analysis/tag-frequency?top_n=15` | タイトル単語頻度（v2） |
| POST | `/api/analysis/competitors` | 全分析実行（**ライバル優先 → スプシフォールバック**、v3 で反転） |
| GET | `/api/analysis/cache` | キャッシュ取得（`source: "youtube_api_rivals"` / `"spreadsheet"`） |
| DELETE | `/api/analysis/cache` | キャッシュ削除（v4 ja-en-mix 再生成用）（v2/v3） |

## v2 変更点サマリ

- **ベンチマーク設定の分離**: 旧 `dashboard_config.benchmark_*` → `~/.config/{app_id}/benchmark_config.json` に移動（チャンネル横断で共通利用）。起動時に自動マイグレーション
- **設定タブ → キャッシュカード**に「♻ 日本語で再生成」ボタン。既存キャッシュが旧版の場合は自動検出してヒント表示
- **徹底パクリ進化**: `propose_with_analysis` とは別に `/api/videos/{name}/suggest-imitate-evolve` を新設（[app-imitate-evolve.md](./app-imitate-evolve.md)）

## v3 変更点サマリ

- **データソース優先順位を反転**: スプシ優先 → **ライバル優先**。`rival_channels` が登録されていれば常に最優先で YouTube API 取得。スプシは fallback。`benchmark_config.json` のスプシ URL がデフォルト設定済みでも、ユーザーがライバル登録したら無視されないように。
- **growth_summary は補助シグナル**: ライバル優先時もスプシが設定済みなら hot_channels だけを文脈として注入。
- **分析プロンプトの言語ハイブリッド化**: `analyze_with_claude` の出力フィールドを「人間が読む descriptive = 日本語」「下流 SUNO/タグ/画像のシード = 英語」「数値 = numeric」に分離。詳細は[ファイル冒頭の Output Language Rules ブロック](../Python/app_competitor.py)。
- **キャッシュメタ更新**: `language: "ja-en-mix"` / `prompt_version: 4`（旧 v3 = 英語固定）。
- **/api/videos/{name}/suggest が分析を自動参照**: 旧版は persona のみ。v3 から `competitor_analysis_cache.json` を optional に読んで viewer_needs / keywords / tag_suggestions をプロンプトに自動注入（無ければ従来挙動）。これにより「英語タイトル候補」「英語説明文」「英語タグ」ボタンも分析連動になった。
- **UI 統合**: 「AI アシスト（英語）」と「ベンチマーク分析」の 2 パネルを 1 パネル「AI 提案（ベンチマーク連動）」に統合。`/api/videos/{name}/suggest-with-analysis`（旧「刺さる英語メタ提案」ボタン）は UI 削除に続き **D13 で API も廃止**（競合分析を反映したメタ提案は `suggest-all` が `propose_with_analysis` を内部利用）。
- **シリーズ画像案（[app-series-proposals.md](./app-series-proposals.md)）**: 同じ analysis を `visual_direction` 軸で消費して「次に作るべき画像」を提案 → codex（gpt-image-2）一括生成 → `_series_drafts/` に格納する新機能を追加。

## 設定

設定画面 → 「競合分析データソース」:
- **チャンネル詳細シート URL** — Sheet 1 の CSV エクスポート URL
- **成長トラッキングシート URL** — Sheet 2 の CSV エクスポート URL
- **接続テスト** ボタンで即時確認

Config: `~/.config/{app_id}/benchmark_config.json`（v2 で分離、旧 `dashboard_config.json` からは自動マイグレーション）
```json
{
  "pinned_names": ["channel_a", "channel_b"],
  "filter": {"top_n": 15, "min_subs": 0, "max_subs": null, "exclude_names": []},
  "spreadsheet_channel_detail_url": "https://docs.google.com/spreadsheets/d/.../gviz/tq?tqx=out:csv&gid=...",
  "spreadsheet_growth_tracking_url": "https://docs.google.com/spreadsheets/d/.../gviz/tq?tqx=out:csv&gid=..."
}
```
チャンネル切り替え時も共通で読まれるため、**複数チャンネル運用時に同じベンチマーク設定を再利用**できる。

## 優先順位とフォールバック（v3）

```
1) rival_channels 設定あり → YouTube API で取得 (source: youtube_api_rivals)
   └ スプシ URL もあれば growth_summary だけ補助取得（hot_channels 文脈）
2) rival_channels 空 + スプシ URL あり → スプシで取得 (source: spreadsheet)
3) どちらも未設定 → エラー
```

ユーザーが「ライバルチャンネル」UI で1件でも登録したら、常にそれが分析対象になる。

## Claude 分析プロンプトへの成長データ注入

`analyze_with_claude()` に `growth_summary` が渡されるとプロンプトに以下が追加される（ライバル優先時もスプシ設定があれば付与）:

```
=== Growth Signals (auto-tracked daily) ===
Hot channels by composite score (ACTIVELY GROWING):
  1. grgr_playlist: +365,479 views/day, +500 subs/day, 3.56% growth
  2. Room.: +138,340 views/day, ...
```

これにより Claude は「今伸びているチャンネルの戦略」を重点的に分析する。

## CLI

```bash
# 分析のみ（スプシ URL が config にあれば自動使用）
python3 app_competitor.py --analyze

# 分析 + vol.78 向けに提案
python3 app_competitor.py --propose 78
```

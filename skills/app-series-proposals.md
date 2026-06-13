# app-series-proposals: シリーズ画像案の提案 → 一括生成

ベンチマーク分析の `visual_direction` + `buzz_patterns` + 既存動画一覧を入力に、Claude CLI が「次に作るべき画像」をシリーズとして提案し、codex（gpt-image-2、codex_imagegen.py）で一括生成 → ステージングフォルダに格納する機能。コンテンツページ上部のアコーディオンから操作。生成プロバイダーは D8 で Flow 撤去により codex 一本化。

## 目的

- **重複回避**: 既存 vol フォルダのタイトル一覧を読み、まだ作っていない都市/時間帯/天候の組み合わせを優先
- **シリーズ一貫性**: visual_direction（color_palette / time_of_day / atmosphere）を共通 DNA として保ちつつ city/time/weather でバリエーション
- **視聴者ジョブ駆動**: buzz_patterns.viewer_needs / underserved_niches を直接プロンプトに射影

## データソース

| 入力 | 場所 |
|------|------|
| ベンチマーク分析 | `~/.config/{app_id}/competitor_analysis_cache.json` の `analysis.visual_direction` + `buzz_patterns` + `trend_shift` + `recommendations` |
| 既存動画一覧 | `<channel_folder>/{vol}_{prefix}_{YYMMDD}/youtube_title.txt` |
| ペルソナ | `dashboard_config.json` の `persona` |

ベンチマーク分析が無い場合はエラー（先に「競合データ取得 + 分析」を実行する必要あり）。

## キャッシュ

`~/.config/{app_id}/series_proposals.json`:

```json
{
  "proposals": [
    {
      "id": "tokyo_rainy_dusk_office",
      "scene_jp": "雨の夕暮れ、東京のオフィスから見える滲んだネオン",
      "scene_en": "Tokyo rainy dusk office with neon bleeding through wet glass",
      "image_prompt_en": "<codex に直接渡す英語プロンプト>",
      "rationale_jp": "<なぜ次に効くかの説明>",
      "tags_jp": ["都市", "雨", "夕暮れ"],
      "filename_slug": "tokyo_rainy_dusk_office",
      "generated": false,
      "output_dir": ""
    }
  ],
  "generated_at": "2026-05-03T02:30:00",
  "channel_name": "W WORKSPACE",
  "based_on": "competitor_analysis_cache"
}
```

## API

| メソッド | パス | body | 用途 |
|---------|------|------|------|
| POST | `/api/series/propose` | `{count?: 8}` | Claude CLI で N 件の提案を生成 → キャッシュ |
| GET  | `/api/series/proposals` | — | キャッシュ取得（パネル表示用） |
| DELETE | `/api/series/proposals` | — | キャッシュ全クリア |
| DELETE | `/api/series/proposals/{id}` | — | 提案 1 件削除 |
| POST | `/api/series/generate` | `{ids:[...], count_per_proposal?:4, use_benchmark_picked?, ...}` | 選択された提案を codex で直列生成（body の `provider` は互換のため受理されるが無視＝codex 固定） |
| GET  | `/api/series/status` | — | 直列バッチの進捗（done/total/current/errors） |

## 画像保存先

```
<channel_folder>/_series_drafts/<filename_slug>/Image/
  └─ codex_*.png          (codex 出力)
```

`_series_drafts/` はステージング領域。生成後にユーザーが手動で `{vol}_{prefix}_{YYMMDD}/Image/` に移動するか、新規 vol として採用する。

## 実装

| ファイル | 役割 |
|---------|------|
| [Python/app_series.py](../Python/app_series.py) | `propose_series()` / キャッシュ読み書き / `staging_dir()` |
| [Python/routers/images.py](../Python/routers/images.py) | API endpoints (`/api/series/*`)、codex を直列で起動するバックグラウンドタスク |
| [web/static/index.html](../web/static/index.html) | コンテンツページ上部の `<details id="seriesPanel">` カード + JS（`loadSeriesProposals` / `generateSeriesProposals` / `generateSeriesImages`） |

## 直列実行の理由

- Codex は内部で 4 並列で動くので、プロンプト 1 件 = プロセス 1 起動が単純。
- 1 件ごとに `output_dir` を切り替えるため、同時並走させると保存先が混ざる。

実装は `_run_series()` 内の async ループで `Popen → 完了 await → 次へ` を回し、進捗を `series_generate` ログに残す。

## UI フロー

1. コンテンツページ → 「次に作るべき画像 — シリーズ提案」アコーディオン展開
2. 「件数」入力（3〜16）→ 「提案を生成 / 再生成」クリック
3. Claude CLI が分析+既存動画を読んで N 件返す → カード一覧で表示
4. カード:
   - 日本語シーン名（タイトル）
   - 英語サブタイトル
   - 日本語の根拠説明
   - 日本語タグ
   - 「英語プロンプトを表示」アコーディオン（codex に渡る本文）
   - 単独生成 / × 削除 ボタン
5. チェック → 枚/案 → 「一括生成」（プロバイダーは codex 固定。セレクトは Codex のみ）
6. ポーリングで `done/total/current` を表示、完了後に `generated: true` で緑枠

## なぜ Vision 生成 (DALL-E 等) ではなく codex なのか

- 既存の認証・出力先・解像度設定をそのまま流用できる
- ユーザーが手動で動かすときと同じパイプラインを通る → 仕上がりが一貫
- API キー追加・課金管理が不要

## CLI / curl 例

```bash
# 提案を 8 件生成
curl -s -X POST http://localhost:8888/api/series/propose \
  -H 'Content-Type: application/json' -d '{"count":8}'

# キャッシュ確認
curl -s http://localhost:8888/api/series/proposals | jq '.proposals[].scene_jp'

# 選択した 3 件を codex で生成
curl -s -X POST http://localhost:8888/api/series/generate \
  -H 'Content-Type: application/json' \
  -d '{"ids":["tokyo_rainy_dusk_office","london_foggy_morning","sf_blue_hour"],"count_per_proposal":4}'

# 進捗確認
curl -s http://localhost:8888/api/series/status | jq '.meta'
```

## 関連

- [app-competitor-spreadsheet.md](./app-competitor-spreadsheet.md) — 入力となるベンチマーク分析の生成
- [app-ai-propose.md](./app-ai-propose.md) — 同じ Claude CLI JSON 提案パターン
- [app-image-select.md](./app-image-select.md) — 生成後にどの画像を最終 vol に採用するか

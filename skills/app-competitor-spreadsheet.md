# app-competitor-spreadsheet: 廃止済み（J-0）

J-0 で Google Sheets ベンチマーク取込は廃止。ベンチマーク指定はチャンネル URL の個別登録に一本化した。

## 正規フロー

```
POST /api/benchmark/channels {"url":"https://www.youtube.com/@...","limit":30}
  ↓
channels.list（/@handle は forHandle、/channel/UC は id、カスタムURLは channelId 抽出後 id）
  ↓
uploads playlist → playlistItems.list
  ↓
videos.list + batchGetStats 論理記録
  ↓
config/benchmark/channel_cache.json
  ↓
既存 competitor_data 形式へ変換 → concept/title/thumbnail/description 分析
```

`search.list` は使わない。旧 `/api/analysis/sheets-import` は 410 を返す。

## 主な API

| メソッド | パス | 用途 |
|---------|------|------|
| POST | `/api/benchmark/channels` | チャンネル URL 登録 + 直近 N 本取得 |
| GET | `/api/benchmark/channels` | 登録済みチャンネル一覧、KPI、アイコン |
| POST | `/api/benchmark/migrate` | 既存 benchmark_profiles から channel_id 既知分を移行 |
| POST | `/api/analysis/competitors` | 新キャッシュから競合分析を生成 |
| POST | `/api/ttp/generate` | TTP 勝ちフォーマット生成 |
| GET | `/api/ttp/profiles` | TTP プロファイル一覧 |

## 実装

| ファイル | 役割 |
|---------|------|
| [Python/app_benchmark_channels.py](../Python/app_benchmark_channels.py) | URL解決、Data API 取込、チャンネル別キャッシュ、移行 |
| [Python/app_ttp.py](../Python/app_ttp.py) | TTP 集計 + 勝ちフォーマット生成 |
| [Python/app_competitor.py](../Python/app_competitor.py) | 既存 competitor_data 形式への互換接続 |
| [Python/app.py](../Python/app.py) | API エンドポイント |

`app_sheets.py` は分析シートや旧追跡シート用途が残る場合だけ維持する。ベンチマーク取込の正規経路としては使わない。

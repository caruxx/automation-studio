# app-bgimage: 背景画像の自動生成（パイプライン STEP 3）

パイプラインの **STEP 3** として走る、Premiere 自動配置の直前工程。ベンチマーク参照画像 + チャンネル
ペルソナ（コンセプト）を OpenAI gpt-image-2（codex_imagegen.py）に渡し、動画フォルダ直下に
`vol{N}.png` を 1 枚生成する。

## 目的

- **Premiere JSX のフォールバック規約に従う**: JSX は `selected_images.json` が無い場合 `vol{N}.png` / `vol{N}-1.png`
  を背景画像として読む（[app-image-select.md](./app-image-select.md) 参照）。STEP 3 ではこの **フォールバック先**
  を必ず満たすように 1 枚生成し、ユーザーが画像を選ばなくても破綻しないようにする。
- **チャンネルらしさを担保**: per-channel `persona` を embed したプロンプトで生成 → 同チャンネルの 1 巻ごとに
  ばらつかず、ブランドガイドを保つ。
- **ベンチマークの色温度・雰囲気を継承**: 競合チャンネルのサムネが集まる `~/.config/{app_id}/benchmark/thumbs/`
  からランダム N 枚を `--reference-image` で渡す → 色設計・ライティングが市場に追従する。

## 関連スキルとの位置づけ

| スキル | スコープ | 出力 |
|--------|----------|------|
| **app-bgimage（本ファイル）** | パイプライン STEP 3。**自動・無人**で背景 1 枚 | `<vol_folder>/vol{N}.png` |
| [app-image-select.md](./app-image-select.md) | ユーザーが UI で **メイン + サブ** を手動選択 | `<vol_folder>/selected_images.json` |
| [app-series-proposals.md](./app-series-proposals.md) | コンテンツページ上部で **複数 vol まとめて** 提案 → Flow/Codex で一括生成 | ステージングフォルダ |

- app-bgimage は **「無人パイプラインの最低限の保険」**。常時 1 枚は揃う。
- ユーザーが Flow / Codex で複数枚作って手動で main/sub を組みたい場合は app-image-select / app-series-proposals。
- 3 つは併存可能。bgimage が生成した `vol{N}.png` は app-image-select で `main` に明示採用すれば JSX が優先する。

## データソース

### 入力
| 項目 | 出所 |
|------|------|
| チャンネル persona | `<channel_folder>/.app_channel_config.json` の `persona` |
| チャンネル名 | per-channel config の `channel_name` または folder 名 |
| 参照画像（最優先） | per-channel config の `reference_image_dir`（UI 設定タブで指定したフォルダ） |
| 参照画像（picked） | `~/.config/{app_id}/benchmark/thumbnail.json` の `picked[]` → 各 video のローカルサムネ |
| 参照画像（最終 fallback） | `<channel_folder>/.app_channel_config.json` の `rival_channels[]` から `channel/UC...` を抽出 → `~/.config/{app_id}/benchmark/thumbs/{ch_id}/*.{jpg,jpeg,png}` |

### 参照画像の優先順位（step_bgimage 実装）

> ⚠ **前提ゲート**: `persona` が空のときは **生成を一切行わず non-fatal で return True**（codex を呼ばない）。背景画像を作るには per-channel の `persona` 設定が必須。

1. **`reference_image_dir`**（per-channel UI 設定で指定したフォルダ。最優先）
   - 設定タブ → 「参照画像フォルダ（背景画像生成）」で指定
   - 指定フォルダ内の `.jpg/.jpeg/.png/.webp` をランダムシャッフル → N 枚採用
2. **Picked**（人間が UI で✓を入れた canonical 参照） — `app_benchmark_thumbnail.get_picked_paths(limit=ref_count)`
3. **rival_channels プール**（per-channel config の `rival_channels` URL から `UC...` 抽出 → benchmark/thumbs 配下を全部集めてシャッフル）
4. **どれも無し** → 参照無しでプロンプトのみで生成（warn のみ。non-fatal）

> ⚠ **プロンプトの実態**（誤解しやすい点）: プロンプトは「ベンチマーク画像から作る」のではなく、**`persona` と `channel_name` を埋め込んだ半固定の英語テンプレ**。固定のネガティブ列挙（`no on-screen text, no logos, no human faces, no pottery, no vases, no urns, no planters, no still life, no decorative ornaments`）と「参照画像の色/光/ムードは統合してよいが構図はオリジナル・要素のコピー禁止（do not copy any element verbatim）」を含む。**ベンチマーク画像は `--reference-image` で別途渡され、生成側（gpt-image-2 edits）が色/光/ムードのみ寄せる**。プロンプト本文に参照画像パスは含まれない。

### 出力
- `<vol_folder>/vol{N}.png` — gpt-image-2 で生成された 1 枚（既定 16:9 / 高品質）
- `<vol_folder>/vol{N}_source.jpg` — 生成成功後、png を `sips` で JPEG 変換した**副生成物**（PSD 合成のフォールバック用＝PLAY LIST 焼き付き無しの素 AI 画像コピー）。既存があれば再変換しない。
- 既存の `vol{N}.png` / `vol{N}.jpg` / `vol{N}_source.jpg` のいずれかがあれば **既定でスキップ**（再生成は `APP_BGIMAGE_FORCE=1`）。⚠ `source.jpg` だけ残っている状態でもスキップ対象。

## 環境変数

| 変数 | 既定 | 意味 |
|------|------|------|
| `APP_BGIMAGE_DISABLE` | 未設定 | `1`/`true`/`yes` で step 全体をスキップ |
| `APP_BGIMAGE_REFCOUNT` | `3` | 参照画像の最大枚数 |
| `APP_BGIMAGE_FORCE` | 未設定 | `1`/`true`/`yes` で既存 vol{N}.png/.jpg を無視して再生成 |

## CLI 例

```bash
# 単発で background image だけ生成（CLI 直叩き、subprocess で codex_imagegen.py を実行）
python3 app_pipeline.py 78 --only bgimage

# 強制再生成（既存 vol{N}.png/.jpg を上書き）
APP_BGIMAGE_FORCE=1 python3 app_pipeline.py 78 --only bgimage

# 参照枚数を 5 枚に
APP_BGIMAGE_REFCOUNT=5 python3 app_pipeline.py 78 --only bgimage

# 完全に無効化（パイプライン全体を回しつつ bgimage だけ抜く）
APP_BGIMAGE_DISABLE=1 python3 app_pipeline.py 78

# チャンネル指定（並列実行時の取り違え防止）
python3 app_pipeline.py 78 --only bgimage --channel-id harbor_notes

# Web API 経由（localhost:8888 起動中）
python3 app_pipeline.py 78 --only bgimage --via-api
```

## Web API

### `POST /api/bgimage/run`

リクエスト:
```json
{
  "video_name": "78_vol_260420",
  "ref_count": 3,
  "force": false
}
```

レスポンス（生成開始時）:
```json
{
  "status": "started",
  "output": "vol78.png",
  "refs": [],
  "skipped": false
}
```

レスポンス（既存スキップ時）:
```json
{
  "status": "ok",
  "output": "vol78.png",
  "refs": [],
  "skipped": true
}
```

排他制御: `_ensure_not_running("bgimage", ...)` で 1 ジョブ同時。

### `GET /api/bgimage/status`

```json
{
  "running": true,
  "logs": ["[起動] output=vol78.png ...", "  📌 Picked 参照画像 3/3 枚 ...", ...],
  "meta": {
    "started_at": "2026-05-24T03:12:00",
    "video_name": "78_vol_260420",
    "vol": "78",
    "ref_count": 3,
    "force": false,
    "skipped": false
  }
}
```

### `POST /api/bgimage/stop`

実行中の bgimage 子プロセスを `terminate()`。

```bash
curl -s -X POST http://localhost:8888/api/bgimage/run \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"78_vol_260420","ref_count":3,"force":false}'

# 進捗 + ログ取得
curl -s http://localhost:8888/api/bgimage/status | python3 -m json.tool
```

## codex_imagegen.py との関係

step_bgimage は `codex_imagegen.py` を **subprocess で 1 回だけ起動**する。

```bash
python3 codex_imagegen.py \
  --output-dir <vol_folder> \
  --prompt "Generate a horizontal 16:9 cinematic background ... ::vol{N}.png" \
  --quality high \
  --output-format png \
  --n 1 \
  --reference-image <ref1> --reference-image <ref2> --reference-image <ref3>
```

- `--reference-image` を複数渡せる。`--model` 未指定＝既定 `gpt-image-2`、`--size` 未指定＝既定 `1536x1024`(16:9)。
- プロンプト末尾の `::vol{N}.png` は出力ファイル名指定（codex_imagegen 側のコンベンション）
- `--n 1` で 1 枚のみ生成

### 生成バックエンドの分岐（codex_imagegen.py 内部・誤解しやすい点）
`backend=auto`（既定）で、**`OPENAI_API_KEY` の有無**により経路が変わる：

| 条件 | 経路 |
|------|------|
| `OPENAI_API_KEY` あり ＋ 参照画像あり | **OpenAI Image API `/v1/images/edits`**（multipart で `image[]` 送信、`input_fidelity=high`）|
| `OPENAI_API_KEY` あり ＋ 参照画像なし | OpenAI Image API `/v1/images/generations`（JSON POST）|
| `OPENAI_API_KEY` なし | **Codex CLI フォールバック**（`codex exec` に「参照画像を分析して再構成せよ」の指示文を渡す）|

> ⚠ つまり既定経路は「Codex で処理」ではなく **OpenAI Image API（gpt-image-2）**。`--reference-image` は API の `image[]` として送られる。Codex CLI は API キー未設定時のフォールバックのみ。

参考: [Python/codex_imagegen.py](../Python/codex_imagegen.py)

## ベンチマーク thumbs 更新タイミング

`~/.config/{app_id}/benchmark/thumbs/{ch_id}/*.jpg` を更新する方法:

### 自動（推奨）
- **競合分析パイプライン経由**: Web UI 設定タブ → ベンチマーク対象を選定 → 「サムネイル軸 分析実行」
  - 内部で `app_benchmark_thumbnail.download_thumbnails()` が走り、`competitor_analysis_cache.json` の
    `topByViews` / `recentUploads` から各チャンネルの新しいサムネを DL
- **APScheduler 経由（無人）**: スケジュール → 「ベンチマーク・サムネイル更新」を週 1 回などに登録
  - 詳細: [app-schedule.md](./app-schedule.md)

### 手動（CLI）
```bash
# サムネだけ DL（分析はしない）
python3 app_benchmark_thumbnail.py --dl-only
```

### 手動（Web API）
```bash
curl -s -X POST http://localhost:8888/api/benchmark/thumbnail/run \
  -H 'Content-Type: application/json' \
  -d '{}'
```

更新頻度のおすすめ: **週 1 回**。市場のサムネトレンドは数日では大きく動かないため、頻繁すぎる更新は不要。

## rival_channels 空のチャンネルでのフォールバック方針

per-channel `.app_channel_config.json` の `rival_channels[]` が空、または URL がすべて `@handle` 形式
（現在の実装は `channel/UC...` の正規表現でのみ抽出するため `@handle` だと参照プールに入らない）の場合:

### 採用ポリシー（実装と本ドキュメントで一致）
1. **`reference_image_dir` がフォルダ指定済みかつ画像入り** → 最優先で採用
2. **Picked が 1 枚でもあれば** → Picked を採用（rival_channels 関係なし）
3. **どちらも無し かつ rival プールも空** → **参照無しでプロンプトのみで生成**（warn ログを出すが non-fatal）

「rival_channels が `@handle` 形式しか持っていなくて benchmark/thumbs が空」というケースは
**UI で `reference_image_dir` を指定すれば回避**できる（推奨運用）。
本実装は `@handle` の YouTube API による channel_id 解決はせず、運用側で `reference_image_dir` か
Picked のどちらかを最低 1 つ用意する設計を想定している。

具体的に step_bgimage のログには:
```
⚠ reference_image_dir / Picked / rival thumbs どれも無し。参照無しで生成
```

と表示される。**エラーで止めない**理由:
- パイプライン全体を止めてしまうと、楽曲生成済みなのに公開できないという最悪のケースになる
- 参照無しでもチャンネルらしいプロンプト（persona）から「最低限見れる」背景は出る
- ユーザーが品質に満足しなければ手動で UI から再生成 / 上書きできる

### 既定アセット流用は不採用
「既定の汎用背景画像 (`default_bg.png`) を流用する」案も検討したが、不採用。理由:
- チャンネルごとにブランドが違うため、共通の汎用画像は浮く
- Premiere JSX 側ですでに「画像が無ければ音声のみ配置」フォールバックが効くため、ここで無理に画像を当てる必要がない

## トラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| `⚠ persona 未設定` で step スキップ | per-channel config の `persona` が空 | 設定タブ → チャンネル → ペルソナを入力 |
| `⚠ Picked / rival thumbs どちらも無し` | Picked 未設定 + rival_channels URL が `@handle` のみ | サムネ分析タブで Picked を設定する、または rival_channels に `channel/UC...` 形式 URL を追加 |
| 既存 vol{N}.png をどうしても再生成したい | `APP_BGIMAGE_FORCE` 未設定 | `APP_BGIMAGE_FORCE=1` で起動 or UI から「上書き再生成」ON で実行 |
| codex_imagegen.py が timeout | OpenAI API 側の遅延 | step は 900s で timeout。それでも失敗するなら API 障害の可能性、`/api/codex-imagegen/status` で別の Codex ジョブが詰まっていないか確認 |
| 9 工程の途中で bgimage だけ止めたい | API quota 節約 / 既に手動で画像配置済み | `APP_BGIMAGE_DISABLE=1` で起動 |

## 関連ファイル

- [Python/app_pipeline.py](../Python/app_pipeline.py) `step_bgimage()` — STEP 3 実装本体
- [Python/app.py](../Python/app.py) `/api/bgimage/run` / `/api/bgimage/status` / `/api/bgimage/stop`
- [Python/codex_imagegen.py](../Python/codex_imagegen.py) — gpt-image-2 ラッパー
- [Python/app_benchmark_thumbnail.py](../Python/app_benchmark_thumbnail.py) `get_picked_paths()` / `download_thumbnails()`

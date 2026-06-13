# PSD 合成 (step_psd_composite) スキル

## 役割

bgimage で AI 生成した `vol{N}.png` を、per-channel テンプレ PSD の `base` スマートオブジェクトに流し込み、`都市名_テキスト` 層に LLM 生成の英語シーンコピーを `set_text` で挿入、`PLAY LIST ` 層の表示/非表示で 2 枚出力する pipeline step。

- `<vol_folder>/vol{N}.jpg` — Premiere 背景画像用（**PLAY LIST 入り**、1920×1080）
- `<vol_folder>/サムネイル.jpg` — YouTube サムネ用（**都市名_テキスト 入り**、1920×1080）
- `<vol_folder>/scene_en.txt` — LLM 生成の英語シーンコピーキャッシュ
- `<vol_folder>/<prefix>_vol{N}.psd` — 編集状態保存済み vol 固有 PSD

## pipeline 内の位置

```
1/10 suno → 2/10 rename → 3/10 bgimage → 4/10 psd_composite ← ここ
       → 5/10 premiere → 6/10 export → 7/10 qa → 8/10 meta
       → 9/10 thumbnail (PSD失敗時のフォールバック) → 10/10 upload
```

`step_thumbnail` (codex 直接生成) は `サムネイル.jpg` が既にあるとスキップする仕様なので、PSD 合成が成功すれば AI サムネは走らない（フォールバック共存）。

## 入力素材のフォールバック順

`vol{N}.png` → `vol{N}_source.jpg` → `image/` サブディレクトリ

- `vol{N}.png` (AI 生成、step_bgimage の出力)
- `vol{N}_source.jpg` (step_bgimage 完了後に sips で生成される PLAY LIST 入りでない素材コピー、PNG 削除運用と両立)
- **`vol{N}.jpg` は除外** — Photoshop 合成出力なので PLAY LIST が焼き付いており、再合成に使うと二重表示になる

## 起動方法

### CLI（フル pipeline）
```bash
APP_CHANNEL_FOLDER="<channel_folder>" \
  python3 _claude/Python/app_pipeline.py <vol>
```

### CLI（psd_composite だけ単発）
```bash
APP_CHANNEL_FOLDER="<channel_folder>" \
  python3 _claude/Python/app_pipeline.py <vol> --only psd_composite

# 既存出力があってもやり直す
APP_PSD_COMPOSITE_FORCE=1 python3 _claude/Python/app_pipeline.py <vol> --only psd_composite

# 一時的に step 全体スキップ
APP_PSD_COMPOSITE_DISABLE=1 python3 _claude/Python/app_pipeline.py <vol>
```

### Web UI（pipeline 経由）
- 動画一覧の「⚡ 選択した工程を実行」 → **PSD合成** チェックを on
- または Photoshop タブの **⚡ 自動 PSD 合成** ボタン

### Web API 直叩き
```bash
# pipeline 単発実行
curl -X POST http://localhost:8888/api/videos/12_HN_260524/run-pipeline \
  -H 'Content-Type: application/json' \
  -d '{"steps":["psd_composite"]}'

# Photoshop API 直叩き (CLI と同等の自動補完あり)
curl -X POST http://localhost:8888/api/photoshop/render-dual-thumbnail \
  -H 'Content-Type: application/json' \
  -d '{"video_name":"12_HN_260524"}'   # scene_text 空でも vision で自動生成される
```

## per-channel 設定（`.app_channel_config.json`）

| キー | 既定値 | 用途 |
|---|---|---|
| `template_psd` | `hn_base.psd` 等 | `<channel_folder>/プロジェクト/` 配下のベース PSD |
| `psd_base_layer` | `"base"` | スマートオブジェクトの差し替え対象レイヤー名 |
| `psd_toggle_layer` | `"PLAY LIST "` | 表示/非表示で 2 枚出しを切り替えるレイヤー（**末尾スペース必須のケースあり**） |
| `psd_text_layer` | `"都市名_テキスト"` | シーン名を入れるテキストレイヤー名 |
| `psd_text_font` | `"HelveticaNeue-UltraLight"` 等 | PostScript 名。明示セットでフォントリセットを防ぐ |
| `psd_export_width` | `1920` | 書き出し解像度 (BICUBICSMOOTHER でアップサンプル) |
| `psd_export_height` | `1080` | 同上 |
| `psd_image_subdir` | `"image"` | フォールバック入力画像のサブディレクトリ |

## vol 固有 PSD の運用

- vol_folder 作成時（`POST /api/videos/create`）にテンプレ PSD が自動コピーされ `HN_vol{N}.psd` 等として配置される前提
- step_psd_composite は **テンプレ本体を絶対に開かない** — vol 固有 PSD のみ open / save / close する
- vol 固有 PSD が無ければ **エラー停止 + 復旧手順を案内**（黙ってコピーするとフォルダ作成プロセスの不具合に気付けないため）

## 内部実装の特徴

1. **元 PSD を保護**: `export_image()` で `duplicate("__export_resize__", true)` → resize → export → close。元 document は手付かずなので連続書き出しが可能
2. **フォント明示セット**: `textItem.contents` 書き換え時のフォントリセット対策として `t.textItem.font = wantFont` を再設定
3. **save + close**: 書き出し成功後に PSD を save → close（連続 vol 処理でドキュメントが Photoshop メモリに溜まらない）
4. **scene_en LLM 生成**: Harbor Notes ベンチマーク分析の規則（全大文字 2-3 語、verb+noun or adjective+noun）に従ったプロンプトで Claude CLI に生成させ `scene_en.txt` にキャッシュ

## 関連ファイル

- `_claude/Python/app_pipeline.py:step_psd_composite()` — pipeline step 本体
- `_claude/Python/app_pipeline.py:_generate_scene_copy_en()` — LLM シーン生成
- `_claude/Python/app_photoshop.py:render_dual_thumbnail()` — Photoshop 合成本体（target_width/height/save_psd/scene_text_font 対応）
- `_claude/Python/app_photoshop.py:export_image()` — duplicate-based リサイズ書き出し
- `_claude/Python/app.py:api_photoshop_render_dual_thumbnail()` — Web API、CLI と同等の自動補完あり

## 関連スキル

- [app-bgimage.md](app-bgimage.md) — 前段の AI 背景画像生成
- [app-premiere.md](app-premiere.md) — 後段の自動配置（vol{N}.jpg を 1920×1080 で背景として配置）
- [app-workflow.md](app-workflow.md) — pipeline 全体フロー

## 注意事項（[[feedback_dont_overedit_jsx]] 関連）

- `_claude/Script/_[自動配置くん]premiere_long.jsx` が canonical、`_claude/Python/jsx_bundle.py` は bundle 版
- 両ファイル同期必須（Edit 時は両方修正）
- JSX 改修前に git commit を必ず打つ
- Premiere JSX の `findImageFile()` は **`vol{N}.jpg` 優先・`vol{N}.png` フォールバック** に変更済み（黒帯対策、1920×1080 を優先配置）
- Premiere JSX の音声・動画クリップは **毎回全削除してから再配置** する仕様（再実行=リセット）。キャプショントラックは QE API 制約で削除できない可能性があり、`createCaptionTrack` で積み上がるケースは手動削除が必要

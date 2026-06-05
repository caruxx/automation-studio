# app-thumbnail: サムネイル自動生成（パイプライン STEP）

> 実装: `Python/app_pipeline.py` `step_thumbnail`（描画）/ `_build_thumbnail_prompt`（プロンプト構築）/ `Python/app_image_prompt.py`（visual brief 正規化）/ `Python/codex_imagegen.py`（Codex/gpt-image）
> STEP キー: `thumbnail`（`STEPS` 内）。出力: `<vol_folder>/thumbnail.png`

> ⚠ **自動サムネ生成は codex 一本化済み（Flow 経路は削除）**。Flow は手動 UI（`app.py` の `/api/flow/*`）でのみ利用。このパイプライン step から Flow は呼ばれない。

## 目的

YouTube サムネイルを AI で自動生成する。**ベンチマーク分析（concept / visual_direction）からプロンプト文を動的構築**し、Codex（gpt-image-2）で生成、候補から 1 枚を `thumbnail.png` に昇格する。

> ⚠ 背景画像（`step_bgimage` → [app-bgimage.md](./app-bgimage.md)）とは**別 step・別ロジック**。背景画像は「参照画像を渡して色/光を寄せる」が固定テンプレ寄り、サムネは「ベンチ分析からプロンプト文を組み立てる」動的構築。混同しない。
> ⚠ PSD 合成（`step_psd_composite` → [app-psd-composite.md](./app-psd-composite.md)）が先に `サムネイル.jpg` を出していれば、この AI サムネ生成は**スキップ**される（フォールバックとして共存）。

## プロンプトの作り方（動的構築・固定テンプレではない）

`_build_thumbnail_prompt(folder)` が以下の**5段優先**で `parts[]` を積み上げ、`". ".join(parts)` を concept 本文にする：

1. `<vol_folder>/concept.txt`（per-vol 日本語コンセプト・最優先）
2. `app_benchmark_concept.get_aggregate()` の `recommendation_for_self.vibe_one_line`
3. `~/.config/{app_id}/benchmark/thumbnail.json` の `analysis.aggregate.recommendation_for_self.vibe_one_line`（= thumbnail_axis）
4. `~/.config/{app_id}/competitor_analysis_cache.json` の `analysis.visual_direction`（time_of_day / atmosphere / composition / color_palette[:3] / subjects[:3] を ` | ` 連結 + avoid[:3]）
5. どれも空なら dashboard config の `persona[:200]` を fallback

→ さらに `app_image_prompt.normalize_visual_direction(analysis, thumbnail_axis)` で competitor analysis と thumbnail aggregate から **subject / background / lighting / style / camera / atmosphere / viewer_hooks / avoid / transform** の構造化 visual brief を抽出し、`build_gpt_image2_prompt(concept=body, visual_direction=visual, for_flow=False, include_text_overlay=False)` でラベル付き多行プロンプト（`Subject: … / Background/context: … / Lighting: … / Constraints: 16:9 …; no text overlay`）を生成。

- `app_image_prompt` の import に失敗した場合は `body + "Cinematic photorealistic 16:9 thumbnail … no text overlay, no logo, no watermark."` の簡易 fallback。
- **要点**: ベンチマークの concept / visual_direction を参照してプロンプト文を動的に組み立てる ＝「ベンチ参照 → プロンプト作成 → 生成」の認識どおり。

## 参照画像（picked、limit=1）

Codex に `app_benchmark_thumbnail.get_picked_paths(limit=1)` の先頭 1 枚を `--reference-image` として渡す（`benchmark/thumbnail.json` の `picked[]` に対応するローカル画像）。

> ⚠ 背景画像は `get_picked_paths(limit=ref_count)`（複数）かつ reference_image_dir / rival_channels フォールバックを持つが、**サムネは picked のみ・limit=1 固定**。reference_image_dir / rival プールのフォールバックは無い。

## 生成プロバイダ（Codex を subprocess 起動）

プロバイダは env `APP_THUMBNAIL_PROVIDERS`（**既定 `codex`**。自動サムネ生成は codex 一本化済み）。`flow` を指定すると「⚠ Flow は削除済み → codex で生成します」と警告のうえ codex にフォールバックする。値が完全に空・無効なときだけ step をスキップ。

> ⚠ Flow（`flow_automation.py`）はこの step から呼ばれない。Flow は `app.py` の手動 UI（`/api/flow/login` `/api/flow/generate` `/api/flow/login-status`）専用に温存。

### Codex（`codex_imagegen.py`）
```
--output-dir <vol_folder>/thumbnail_candidates
--max-parallel <APP_THUMBNAIL_CODEX_MAX_PARALLEL 既定1>
--model <APP_THUMBNAIL_IMAGE_MODEL 既定 gpt-image-2>
--size <既定 1536x1024>  --quality <既定 medium>
--prompt "<prompt>::vol{N}_thumb"   # ::vol{N}_thumb は codex_imagegen の出力ファイル名コンベンション
```

`Popen` を `communicate(timeout=600)` で待機。

## 出力・候補昇格

- 候補は `<vol_folder>/thumbnail_candidates/` に `*.png/*.jpg/*.jpeg/*.webp`。
- **mtime 昇順**でソートし、**先頭 1 枚を `<vol_folder>/thumbnail.png` に昇格**（`shutil.copy2`）。codex 一本化のため `_rank` のようなプロバイダ優先順位は廃止。
- 候補ゼロでも警告のみで `return True`（**upload を止めない**）。

## スキップ条件

- `APP_THUMBNAIL_DISABLE=1`
- `<vol_folder>/thumbnail.png` 既存、または `vol*.jpg` 既存、または `サムネイル.jpg` 既存（= PSD 合成が先に出力済み）

## 環境変数まとめ

| 変数 | 既定 | 役割 |
|------|------|------|
| `APP_THUMBNAIL_PROVIDERS` | `codex` | 実質 `codex` のみ。`flow` 指定は警告のうえ codex にフォールバック（Flow 経路は削除済み） |
| `APP_THUMBNAIL_DISABLE` | (未設定) | `1` で step 全体スキップ |
| `APP_THUMBNAIL_CODEX_MAX_PARALLEL` | `1` | codex の並列数 |
| `APP_THUMBNAIL_IMAGE_MODEL` | `gpt-image-2` | codex のモデル |
| `APP_THUMBNAIL_IMAGE_SIZE` | `1536x1024` | codex の出力サイズ（16:9） |
| `APP_THUMBNAIL_IMAGE_QUALITY` | `medium` | codex の品質 |

## 関連

- [app-bgimage.md](./app-bgimage.md) — 背景画像（別 step。参照画像で寄せる固定テンプレ寄り）
- [app-psd-composite.md](./app-psd-composite.md) — PSD 合成（先に `サムネイル.jpg` を出すと AI サムネはスキップ）
- [app-image-select.md](./app-image-select.md) — `vol{N}.jpg` リネームで YouTube サムネ採用
- [app-workflow.md](./app-workflow.md) — 全工程フロー（STEP 8 にサムネ生成の概要）
- [Python/app_image_prompt.py](../Python/app_image_prompt.py) — `normalize_visual_direction` / `build_gpt_image2_prompt`

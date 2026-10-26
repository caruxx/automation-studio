# image: 背景画像・画像合成・AI サムネドメイン

## 目的
ベンチマーク分析と参照画像から背景画像を作り、画像エディタ（Pillow 合成）で `vol{N}.jpg` と `サムネイル.jpg` を出す。投稿用サムネは別工程 `thumbnail`（AI 選定、[app-thumbnail.md](./app-thumbnail.md)）が `thumbnail_selected.jpg` に確定する。

Photoshop は使わない（2026-10-25 段階 1a で廃止）。`psd_composite`（工程名は互換のため据え置き）は常に `app_image_composite.py` → Pillow 合成を実行する。

## 入口コマンド
- 背景: `python3 Python/studio.py bgimage --vol <N> --dry-run`
- 画像合成: `python3 Python/studio.py psd --vol <N> --dry-run`（工程 psd_composite = Pillow 合成）
- サムネ AI 選定: `python3 Python/studio.py thumbnail-select --vol <N> --dry-run`

## 前提リソース
- `codex_imagegen.py` が使う OpenAI Image API または Codex CLI
- Pillow（画像合成）
- `concept.txt` / benchmark concept / thumbnail aggregate / competitor `visual_direction`

## 並列可否
- bgimage / thumbnail は画像生成 quota に注意しつつ小並列可。
- 画像合成は CPU 処理で単一リソース制約は無い（`routes.json` の `psd` は per-machine 1）。

## 典型手順
1. `bgimage` で `vol{N}.png` と `vol{N}_source.jpg` を生成。
2. `psd_composite` で背景と scene text（チャンネル設定 `thumbnail_composition`）を合成して 2 枚出し。
3. `thumbnail` は新しい背景で候補を作って採点し、`thumbnail_selected.jpg` に確定する。`サムネイル.jpg` / `vol{N}.jpg` は書き換えない。

## 失敗時の対処
- persona 未設定: channel config に persona を入れる。
- 既存画像が邪魔: `APP_BGIMAGE_FORCE=1`。
- 合成失敗: Pillow の導入と `vol{N}.png` の有無を確認。
- Flow 指定: 現コードでは codex にフォールバックする。Flow 前提で作業しない。

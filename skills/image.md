# image: 背景画像・PSD 合成・AI サムネドメイン

## 目的
ベンチマーク分析と参照画像から背景画像を作り、Photoshop PSD 合成で `vol{N}.jpg` と `サムネイル.jpg` を出す。PSD が使えない場合は AI サムネをフォールバック生成する。

## 入口コマンド
- 背景: `python3 Python/studio.py bgimage --vol <N> --dry-run`
- PSD: `python3 Python/studio.py psd --vol <N> --dry-run`
- AI サムネ: `python3 Python/studio.py thumbnail --vol <N> --dry-run`

## 前提リソース
- `codex_imagegen.py` が使う OpenAI Image API または Codex CLI
- Photoshop + UXP/CEP パネル
- `concept.txt` / benchmark concept / thumbnail aggregate / competitor `visual_direction`

## 並列可否
- bgimage / thumbnail は画像生成 quota に注意しつつ小並列可。
- Photoshop は単一リソース。`psd` は必ず順次。
- opt-in ロック: `python3 Python/parallel_guard.py psd -- python3 Python/app_pipeline.py <N> --only psd_composite`

## 典型手順
1. `bgimage` で `vol{N}.png` と `vol{N}_source.jpg` を生成。
2. `psd_composite` で vol 固有 PSD を開き、背景と scene text を差し替えて 2 枚出し。
3. `thumbnail` は `thumbnail.png` / `vol*.jpg` / `サムネイル.jpg` があるとスキップされる。

## 失敗時の対処
- persona 未設定: channel config に persona を入れる。
- 既存画像が邪魔: `APP_BGIMAGE_FORCE=1`。
- Photoshop 接続不可: パネル導入と Photoshop 起動を確認。
- Flow 指定: 現コードでは codex にフォールバックする。Flow 前提で作業しない。

# pipeline: 全工程オーケストレーションドメイン

## 目的
`plan → suno → rename → bgimage → psd_composite → premiere → export → qa → meta → localization → thumbnail → upload` を順に実行し、途中再開・一部実行・自動運用を扱う。

## 入口コマンド
- 正規確認: `python3 Python/studio.py pipeline --vol <N> --dry-run`
- 実行: `python3 Python/app_pipeline.py <N>`
- 途中再開: `python3 Python/app_pipeline.py <N> --from <step>`
- 単発: `python3 Python/app_pipeline.py <N> --only <step>`

## 前提リソース
- routes.json の intent / parallelism
- app_pipeline.py の `STEPS` / `STEPS_WITH_PLAN` / `STEP_LABELS`
- 各 domain の外部資源

## 並列可否
- routes.json を基準にする。
- SUNO / Premiere+AME / Photoshop は単一ロック。
- opt-in ラッパ: `python3 Python/parallel_guard.py <intent> -- <cmd...>`

## 典型手順
1. まず `studio.py <intent> --dry-run` でコマンドと channel guard を確認。
2. `--from-benchmark` のときだけ先頭に `plan` が入る。
3. `APP_PIPELINE_STEPS` で絞る場合も `STEPS_WITH_PLAN` に存在する step だけが採用される。
4. retryable exit 76 / quota exit 77 / preflight exit 78 の契約を壊さない。

## 失敗時の対処
- 途中停止: `--from <止まった工程>`。
- Premiere preflight: `export_engine=ffmpeg` なら Premiere 不要、それ以外は実機起動。
- 並列事故が怖い操作: `parallel_guard.py` で包む。

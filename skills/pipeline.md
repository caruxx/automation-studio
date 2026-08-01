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

## 複数 vol のリレー式 batch

複数 vol は `batch_orchestrator.py` を正規入口にする。Claude、Codex、人間のいずれから呼んでも同じ CLI を使う。

```bash
python3 Python/batch_orchestrator.py \
  --vols 147-151 \
  --duration-sec 3600 \
  --channel orzz \
  --max-post 2

# カンマ区切りも可
python3 Python/batch_orchestrator.py --vols 147,149,150 --duration-sec 3600

# 画像先行を無効化し、従来のphase2内で実行
python3 Python/batch_orchestrator.py --vols 147-151 --no-prefetch-image

# SUNO曲間フロアを明示するA/B検証用
python3 Python/batch_orchestrator.py --vols 147 --min-wait-sec 20

# routing の確認
python3 Python/studio.py batch --vols 147-151 --duration-sec 3600 --channel orzz --dry-run
```

対象 vol フォルダは事前に全て作成する。自然言語で「最新動画を5本作って」と来た場合は、既存最大 vol の次から連番にし、既存最新 vol の公開日の翌日から日次の公開日で `POST /api/videos/create` を必要本数ぶん実行してから batch を開始する。

### リレー構成

```text
time --->
SUNO lock:    vol147 phase1 ----> vol148 phase1 ----> vol149 phase1 ---->
image lane:   vol147 bg->psd ----> vol148 bg->psd ----> vol149 bg->psd ->
post slot1:                    vol147 phase2 --------------------------->
post slot2:                                        vol148 phase2 ------>
```

- phase1: `app_pipeline.py N --only suno --channel-id <id> --auto`。orchestrator の逐次ループで同時に2本起動しない。SUNO lock は `suno_auto_create.py` 内部に任せ、外側の `parallel_guard.py` では包まない。
- 画像先行: phase1開始と同時に `bgimage → psd_composite` を別スレッドで開始する。PSD合成はスレッドロックでvol間直列にし、各step内部のPhotoshop lockも維持する。失敗時だけphase2冒頭で対象stepを再実行する。`--no-prefetch-image` で従来配置へ戻せる。
- phase2: 画像先行有効時は `premiere` から `upload` まで順に実行する。画像先行無効時は従来どおり `bgimage` から始める。
- `step_suno()` が生成、Workspace DL、`app_process_tracks.py` まで担当するため、phase1 後に `rename` step は重ねない。
- phase1 は SUNO browser lock により全 vol 直列。phase1 完了直後にその vol の phase2 を投入し、次 vol の phase1 を開始する。
- phase2 は既定2並列。`--max-post N` で変更できる。
- phase1 後に `music/*.mp3` を MD5 で比較し、同一内容は名前順の先頭だけ残す。

### batch 用フラグ

| フラグ | 設定元 | 意味 |
|---|---|---|
| `APP_DURATION_SEC` | `--duration-sec` | export / QA の目標尺を秒で指定する。 |
| `APP_SUNO_NO_HOLD=1` | orchestrator 固定 | `APP_KEEP_BROWSER=1` でも送信、DL、後処理後の無限 hold を行わず正常終了する。 |
| `APP_SUNO_SKIP_SECOND_DL=1` | orchestrator 固定 | 子プロセス完了時に `music/*.mp3` があれば親の2回目DLと後処理を省く。 |
| `APP_SUNO_READY_POLL=1` | orchestrator 固定 | 固定300秒待ちを使わず、20秒間隔で `audio_ready` が送信成功数の2倍に達するまで確認する。タイムアウト時はready分だけ回収する。 |
| `APP_SUNO_ONESHOT=1` | orchestrator 固定 | 送信とready poll、DL、後処理を同じPlaywrightセッションで完結し、親側の2回目DLを行わない。 |
| `APP_SUNO_SKIP_OPTIONAL_TITLE=1` | orchestrator 固定 | SUNOの任意タイトル入力とMore options探索を省く。未設定タイトルは後続の `app_process_tracks.py` が再生成する。 |
| `APP_SUNO_FORM_DIAGNOSTICS=1` | 手動調査時のみ | フォーム失敗時のDOM診断ダンプを有効にする。既定はoffで、警告ログは引き続き出力する。 |
| `APP_SUNO_PARALLEL_DRAFT=1` | 手動A/B | Claude/Codexのプロンプト一括生成をブラウザ起動・Workspace確保と並行し、送信開始前にjoinする。未指定時は従来の直列実行。 |
| `APP_SUNO_PARALLEL_DL=N` | 手動A/B | `N>1` でCookie/UAを引き継いだHTTP並列DL。HTTP 403/429を1件でも検出したらstageを破棄し、ブラウザ内逐次DLへ戻る。既定1。 |
| `APP_PROCESS_PARALLEL=4` | orchestrator固定 | `app_process_tracks.py` のffmpeg処理を4並列にする。未指定時は1で従来の逐次処理。 |
| `APP_EXPORT_HWENC=1` | orchestrator 既定 | ffrenderの `libx264` を `h264_videotoolbox` へ切り替える。映像1.2Mbps CBR、AAC 320kbpsで現行容量帯を目標にする。2026-08-01/02のvol152-156でQA4本連続合格・サイズ従来レンジを確認し既定昇格。`APP_EXPORT_HWENC=0` でlibx264へ戻す。 |
| `APP_SUNO_MIN_WAIT_SEC=N` | orchestrator 既定 20（`--min-wait-sec N` で上書き） | 曲間フロア。2026-08-01/02のvol152-156(5本)でbot判定0・スキップ0を確認し20秒を既定昇格。suno_auto_create単体実行の既定は30秒のまま。20秒未満へ下げる場合は `FORM_METRICS` 監視付きで1本ずつ検証すること。 |

### meta並行化の依存判定

`meta` は `claude_proposer.gather_context()` から `music_time_code_info_*.txt` を読み、説明文のTracklistへ挿入する。ffrender経路では曲順をmanifestで確定した後、export内でこのタイムコードを生成する。そのため `export ∥ (meta → localization)` は行わず、`export → qa → meta → localization` の順を維持する。

### preflight

実処理の前に全項目を検査し、1つでも失敗すれば exit 78 で全 vol を開始しない。

1. 指定した全 vol フォルダが存在する。
2. `localhost:8888/api/config/migration-status` が応答する。停止中は `bash Python/start.sh` を1回起動して再確認する。
3. active channel を `--channel` と一致させ、切替後も再検証する。
4. チャンネル直下の `.youtube_token.json` を実際に refresh し、失敗時は `python3 app_youtube.py --auth-only <folder>` による再認証を案内して中断する。
5. チャンネル保存先の空き容量が20 GiB以上ある。

`--dry-run` はフォルダ存在確認と計画表示だけを行い、サーバー起動、channel切替、token refresh、重複削除、pipeline subprocess 起動をしない。

### 失敗と再開

- phase1 失敗: その vol の phase2 を投入せず、次 vol の phase1 へ進む。
- phase1 subprocess が exit 0 でも `music/*.mp3` が0件なら `no tracks` として失敗にし、phase2へ進めない。
- phase2 失敗: 他 vol を止めず完走させる。
- 全 vol 成功または完了済みスキップなら exit 0。1本でも失敗なら exit 1。
- 再開時は同じ範囲に `--skip-completed` を付ける。`app_pipeline.py` の upload marker 判定と同じく、`youtube_upload.json` の video id に加え、現タイトル一致または72時間以内の upload を完了扱いにする。
- ログは `logs/batch/YYYYMMDD_HHMMSS/volN.log`、終了時サマリは vol、phase1時間、phase2時間、結果、YouTube URL を表示する。

### 2026-08-01 の事故と恒久対策

| 事故 | 対策 |
|---|---|
| `APP_KEEP_BROWSER=1` で送信後に無限 hold し、step timeout まで待った | batch が `APP_SUNO_NO_HOLD=1` を設定し、セッション中の browser 維持は残したまま完了時だけ閉じる。 |
| hold 解除後に子の auto-download と親の2回目DLが重なり、同一20テイクが40ファイルになった | `APP_SUNO_SKIP_SECOND_DL=1` で処理済み `music/` を検出し、親の再DLを止める。さらに phase1 後のMD5除去を安全網にする。 |
| upload step まで token 失効が分からず、`invalid_grant` が未捕捉で crash した | batch preflight で実 refresh する。`app_youtube.get_credentials()` も refresh 失敗を捕捉して新規 OAuth へ進む。 |

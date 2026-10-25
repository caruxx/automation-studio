# YouTube 自動化ツール（Automation Studio）現状分析レポート

- 作成日: 2026-10-25
- 目的: 大幅修正の計画に使う読み取り専用の分析。設計・実装は含まない。
- 作成: Claude Fable 5.1（分析担当セッション。統括ではない）。Explore サブエージェント 7 本の結果を統合。
- 共通規則: AI_WORKFLOW.md local-v1.7（SHA-256 `89d50eb0…`）を読了。`~/.claude/CLAUDE.md` の照合値は local-v1.6 / `299d14e5…` のままで不一致。読み取り専用分析のみ実施し、編集を伴う作業は本人の照合値更新まで行っていない。
- 注意: 本レポートは秘密情報の値、メールアドレス、IP、実ユーザー名を含めていない。ただし `automation-studio` は **public リポジトリ**なので、commit する場合はこの前提を再確認すること。
- 運用前提（本人確認 2026-10-25）: **PSD（Photoshop）と Premiere Pro / Media Encoder は使っておらず、将来も使わない。書き出しは ffmpeg 経路のみ。** Adobe 系の記述は「削除対象」として読むこと。ただし §7.2 のとおり、コードと設定はまだ Adobe を既定にしている。

## 0. 対象と基準点

| リポジトリ | 役割 | branch | HEAD (base SHA) | 状態 | 公開範囲 |
|---|---|---|---|---|---|
| `caruxx/automation-studio`（`~/dev/automation-studio`） | Mac 側本体（FastAPI + Web UI + パイプライン） | main | `1e7ae9fe5fdc58fa3cc393b530226d745949e699`（2026-09-29） | clean、origin と一致、stash 0 | **public** |
| `caruxx/automation-studio-vps`（`~/dev/automation-studio-vps`） | VPS 側（制御プレーン: 認証・ジョブキュー・スケジューラ・分析） | main | `80411fdda9b7373b3306355bc5d7287065bee0f6`（2026-09-19） | dirty は `.DS_Store` のみ、origin と一致 | private |
| `~/dev/automation-studio-mirror` | 2026-08-09 時点の古い clone（19 commit 遅れ） | main | `e73e494…` | 固有の内容なし | 対象外 |

### 0.1 すぐに判断が必要な事項（時間制約あり）

1. **Drive 退避フォルダの削除予定が 2026-10-26 以降**（`DEV/_移行済み_20261012/README_削除前に確認.md`）。その中の `_claude/docs/review/2026-09-30/`（コード・設定監査、セキュリティ監査、UI/UX 監査、Mac/VPS 構成図、runtime-readback、screenshots）は **git にも `~/dev` にも無い**。秘密を含むため public の automation-studio には入れられない。private の保管先（dev-workspace 等）を決めて退避する必要がある。同フォルダの `config_backups/`（6.5GB）も同様。
2. **main 未統合の修正 branch**: `origin/feature/suno-chrome-direct-hook`（main +23、最終 2026-10-05）に SUNO 未ログイン誤判定・自動保存による API キー／チャンネル設定破壊・日本語 UI DL・Cookie バナー対応の修正が入っている。`origin/drive-snapshot-20261012`（main +24）はその上に Drive 未コミット分（fal_video、layer_motion、suno_browser 等 約2,900行）を含む。大幅修正の起点をどちらにするか先に決める必要がある。

## 1. アーキテクチャ図（テキスト）

```
┌──────────────── VPS (automation-studio-vps, Docker: postgres16 + FastAPI:8000) ────────────────┐
│ auth(JWT+TOTP) / users / channels / vols / jobs / schedules / benchmark / insights / live      │
│ APScheduler(lifespan): autopilot_tick(5分) → jobs(queued)   benchmark 06:00   scheduled_tasks  │
│ YouTube OAuth(Webフロー, refresh_token は AES-GCM で DB 保存, 既定 scope=upload のみ)           │
│ live-runner(root systemd) ← /api/live/ops                                                    │
└───────────────▲──────────────────────────────┬───────────────────────────────────────────────┘
                │ HTTPS pull のみ (worker token)  │ lease/heartbeat/complete/fail
                │ report_step / push_context /    │
                │ report_upload (vps_ledger_sync) ▼
┌──────────────── Mac (automation-studio, python3 Python/app.py :8888, launchd は廃止・手動起動) ──┐
│ worker/client.py (VPS ワーカー, 30秒ポーリング)                                                │
│    pipeline job → studio.py <intent> --vol N --channel KEY → routes.json → app_pipeline.py     │
│    upload job   → 自前 resumable upload (Mac の app_youtube を経由しない)                       │
│    meta/localization → worker/proposer.py (claude CLI 直)                                      │
│                                                                                                │
│ app.py (9,952行: 227 route + 7 startup hook + APScheduler)                                     │
│    APScheduler: orchestrator_tick(5分) / vol_create / auto_resume / publish_* / digest / backup │
│    routers/ (8本, 149 route)  app_core.py(共有グローバル, from app_core import *)              │
│                                                                                                │
│ app_pipeline.py STEPS (正本, 11工程):                                                          │
│   suno ─▶ rename ─▶ bgimage ─▶ psd_composite ─▶ premiere ─▶ export ─▶ qa ─▶ meta ─▶ localization ─▶ thumbnail ─▶ upload │
│   │flock      ffmpeg   codex/   AppleScript    CEP+/tmp    AME|ffrender  claude  claude/codex   vision   app_youtube │
│   │suno-browser         OpenAI  Photoshop      Premiere                   -p                    scoring  (3段ガード) │
│   ▼                                                                                            │
│ Playwright Chromium (永続プロファイル, 手動ログイン)  ── suno.com (非公式, DOM 依存, 復号 DL)      │
│                                                                                                │
│ 状態: vol フォルダ成果物(共有 Drive /YT/<ch>/<N>_<prefix>_<YYMMDD>/) + ~/.config/<app_id>/runs.db │
│       + render_queue.db + schedule_jobs.json + dashboard_config.json(平文キー) + channels.json  │
│ Web UI: web/static/index.html (15,976行, インラインJS 12,550行, onclick 484)                     │
│ 通知: Discord webhook のみ (Chatwork/Slack 実装なし)                                            │
│ ライブ: app_live.py → SSH(鍵) → VPS /opt/ytlive (copy モード, screen 上 ffmpeg)  ※VPS Job 経路と二重 │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

起動経路は4つ並立している: (a) app.py 内 APScheduler、(b) `batch_orchestrator.py`、(c) 手動 `studio.py` / `app_pipeline.py`、(d) VPS ワーカー。相互の調停はない。

## 2. 主要モジュール一覧と責務

### Mac 側（`Python/`）

| 区分 | モジュール | 責務 | 行数 |
|---|---|---|---|
| 入口 | `app.py` | FastAPI 本体。227 route 直書き、startup hook 7、APScheduler 登録、publish ゲート、auto_resume | 9,952 |
| 入口 | `studio.py` + `routes.json` | CLI の正規入口。61 intent を API(localhost:8888) か CLI subprocess に振り分け。`--dry-run`、チャンネルガード | 725 / 1,443 |
| 入口 | `start.sh` / `setup_launchd.sh` | 依存の pip install、8888 の kill -9、起動。launchd は 2026-07-04 廃止（AGENTS.md）だが CLAUDE.md は launchd 前提のまま | |
| 土台 | `_app_config.py` / `app_core.py` | 設定ディレクトリ解決（`~/.config/<app_id>`）、Drive 検出、共有可変グローバル（task_logs、active_tasks、upload queue）、認証ミドルウェア | 307 / 1,817 |
| 制作 | `app_pipeline.py` | **工程の正本 `STEPS`（11工程）**、STEP_FUNCS、RETRY_POLICY、終了コード、`--from/--only/--dry-run` | 3,679 |
| 制作 | `app_orchestrator.py` | runs.db を黒板にした依存解決ワーカー（tick から呼ばれる）。`_ARTIFACT_DONE` で成果物の有無から完了推論 | 893 |
| 制作 | `batch_orchestrator.py` | 複数 vol のリレー並列（phase1=suno 直列、以降 `--max-post` 並列） | 536 |
| 制作 | `app_run_ledger.py` / `app_render_queue.py` | SQLite 台帳（6h stale、連続3失敗ブレーカー）/ Premiere・AME 直列キュー（既定 OFF） | 529 / 401 |
| 音楽 | `suno_auto_create.py` / `suno_fast_dl.py` / `suno_queue.py` | Playwright で SUNO 操作 / ページ内復号 DL / 複数 Workspace キュー（**未参照**） | 4,329 / 1,087 / 524 |
| 音楽 | `app_process_tracks.py` / `app_music_catalog.py` | ffmpeg 後処理、タイトル台帳・ハート採用 | 1,148 |
| 画像 | `codex_imagegen.py` / `routers/images.py` | OpenAI Images API（gpt-image-2）か `codex exec` で画像生成。34 route | 916 / 2,525 |
| 画像 | `app_thumbnail_scoring.py` / `app_thumbnail_state.py` / `app_image_eval_loop.py` | Claude Vision 100点採点、`judge_auto_approval`、`thumbnail_state.json`、反復生成ループ（既定 OFF） | |
| 画像 | `app_image_compositor.py` / `app_photoshop.py` | Adobe なし合成 / AppleScript `do javascript` で Photoshop | 1,132 |
| 動画 | `app_premiere.py` / `jsx_bundle.py` / `cep_extension/premiere_link` | CEP パネルと `/tmp/pymiere_*.json` でファイル IPC、AppleScript でメニュー操作 | 920 / 1,284 |
| 動画 | `app_ffrender.py` / `app_vlogrender.py` | ffmpeg でループ連結書き出し（`export_engine=ffmpeg`）/ vlog（未参照の独立 CLI） | 3,151 / 800 |
| 投稿 | `app_youtube.py` / `routers/youtube.py` / `publish_schedule.py` / `app_reconcile.py` | OAuth、resumable upload、private+publishAt 予約、二重投稿 3 段ガード、YouTube 実態照合 | 1,371 |
| 分析 | `app_benchmark_*.py` / `app_competitor.py` / `app_video_intel.py` / `app_comment_mining.py` / `app_genre_radar.py` / `routers/analytics.py` | 競合・ベンチマーク・Analytics API | |
| LLM | `app_llm_runner.py` / `claude_proposer.py` | `claude -p` → 失敗時 `codex exec` フォールバック。タイトル・説明文・タグ・翻訳 | 910 |
| ライブ | `app_live.py` / `routers/live.py` / `vps_live/` | SSH で VPS 上の ffmpeg（copy モード）を操作 | 659 / 796 |
| 運用 | `app_digest.py` / `app_learning.py` / `app_updater.py` / `app_token_health.py` | Discord 朝ダイジェスト、48h レビュー、自動更新（05:15、署名検証なし）、トークン期限点検 | |
| 連携 | `vps_ledger_sync.py` | Mac → VPS 片方向の工程報告 | |
| UI | `web/static/index.html` + js 6本 + css 7本 | 単一 HTML の SPA。ビルドなし、`?v=` と sw.js でキャッシュ管理 | 15,976 |

### VPS 側（`app/`, `worker/`）

| モジュール | 責務 |
|---|---|
| `app/main.py` | lifespan で scheduler 起動、18 router 登録、`web/static` 配信 |
| `app/api/{auth,users,google,oauth}.py` | JWT+TOTP ログイン、Google OIDC（PKCE）、YouTube OAuth Web フロー（旧経路 oauth.py は PKCE なし） |
| `app/api/{jobs,worker,vols,channels,schedules,connections}.py` | ジョブ投入（job_type 未検証）、lease（`FOR UPDATE SKIP LOCKED`）、vol 台帳、Studio Connect |
| `app/services/scheduler.py` | autopilot_tick: qa→meta→localization→thumbnail→upload の**独自依存表**（Mac の STEPS と別定義）、1ch 1件・全体5件上限、3連続失敗停止 |
| `app/services/token_crypto.py` | AES-256-GCM、鍵は KMS / keyring ファイル / 開発鍵 |
| `worker/client.py` | Mac で動く pull 型ワーカー（7 job_type）。`upload` は自前 resumable upload | 2,166 行 |
| `worker/proposer.py` | claude CLI 直呼び（Codex フォールバックなし） |
| `tests/` | 28 ファイル、211 件、全合格（3.3秒、ネットワーク不要） |

## 3. データフロー（1 vol の一生）

```
[起票] Mac: app.py vol_create(フォルダ最大+1) / VPS: run_vol_create(DB 最大+1)  ← 採番が別系統
   │  VPS 起票はフォルダを作らない → Mac 側 exit 2 (vol_not_found)
   ▼
[suno] flock suno-browser → Playwright → 生成 → 復号 DL → original_music/
   │  exit 75 = ログイン切れ ＝ ロック取得失敗（同じ値）
   ▼
[選曲待ち] .music_review.json のハート（人が決める方針） → exit 79 awaiting_selection
   │  orchestrator / batch / VPS worker は 79 を FAIL 扱い（ブレーカー加算・再試行）
   ▼
[rename→bgimage→psd/compositor→premiere/ffrender→export] 成果物: music/, Image/vol{N}.png, サムネイル.jpg, mp4, .ffrender_manifest.json
   │  Adobe 経路は /tmp IPC で並行不可。ffmpeg 経路は export 1 工程で完結
   ▼
[qa] qa_report.json (passed)   失敗時は premiere まで差し戻し
   ▼
[meta] report_step(meta, running) → VPS updated_at=now → step_meta が「VPS が新しい」と判定して VPS の meta を採用（逆流）
   ▼
[localization→thumbnail] youtube_localizations.json / Vision 採点 → thumbnail_state.json
   ▼
[upload] marker(youtube_upload.json 72h) → O_EXCL lock → reconcile(直近50件) → private + publishAt(フォルダ名日付 + publish_time_jst)
   │  VPS worker の upload job はこの 3 段を全て迂回
   ▼
[publish ゲート] app.py DateTrigger（再起動で marker から復旧）
   │  _scheduler_reload() がスケジュール保存のたびに tick / publish / auto_resume を全消去（再起動まで戻らない）
   ▼
[reconcile / digest / 48h レビュー]
```

## 4. 外部依存と認証の所在（値は記載しない）

| 連携先 | 方式 | 認証情報の所在 | 脆い点 |
|---|---|---|---|
| YouTube Data API v3（Mac） | googleapiclient、resumable | `~/.config/<app_id>/youtube_client_secret.json`、チャンネル別 `<ch>/.youtube_token.json`（**共有 Drive 上**）、API キーは `dashboard_config.json`（**実測 0644**）/ env / txt | Drive 権限＝YouTube 権限。quota は自前帳簿で実残量とずれる |
| YouTube Data API v3（VPS） | urllib 自前 resumable | refresh_token を DB に AES-GCM、鍵は `/etc/automation-studio/keyring.json` | 既定 scope は upload のみ。`videos.update`（説明文編集・公開切替）は通らない可能性。Mac と権限非対称 |
| YouTube Analytics | youtubeAnalytics v2、`channel==MINE` | Mac の OAuth トークン | scope 不足で再認証 |
| SUNO | Playwright ブラウザ自動化（非公式） | `~/.config/orzz/chromium_profile`（**`orzz` 直書き**、手動ログイン Cookie） | DOM ボタン名・`?wid=`・ページ内復号フックに依存。UI 変更のたび修正集中（7/28, 8/1, 9/6, 9/29, 未統合 23 commit） |
| Adobe Premiere/AME | CEP パネル + `/tmp/pymiere_*.json`、AppleScript メニュー操作 | TCC: アクセシビリティ・自動化。`PlayerDebugMode=1` | アプリ名「2026」固定、/tmp 固定で並行不可 |
| Adobe Photoshop | `osascript do javascript`（UXP は実態として未使用） | 同上 | README の「新版 UXP」は実装と逆 |
| 画像生成 | `OPENAI_API_KEY`(env) があれば Images API gpt-image-2、無ければ `codex exec --dangerously-bypass-approvals-and-sandbox -i` | Codex CLI ログイン | 生成物を `~/.codex/generated_images/` の最新ファイルとして拾う。**モデルは gpt-image-2 固定（本人が使う image 2.5 とずれ）** |
| LLM | `claude -p` → `codex exec` フォールバック | 各 CLI のサブスク認証 | VPS 側 proposer はフォールバックなし |
| Google Drive | CloudStorage glob 検出、`/YT/` マーカーで他ホスト吸収 | – | channels.json 10 件が絶対パス。同期遅延を `file_stability` で待つ |
| 外部 SSD | `export_path` の `/Volumes/...`（4 ch、現在未マウント） | – | 未マウントで停止 |
| Discord | webhook | 共有 `config/discord_config.json`（0600） | **Chatwork は実装なし**（LINE は Discord への別名） |
| Mac⇔VPS | Mac→VPS HTTPS pull のみ | `~/.config/automation-studio/worker.json`（0600、**studio_root が Drive の旧 checkout を指す**） | VPS→Mac 着信なし |
| ライブ | Mac→VPS SSH 鍵 `~/.ssh/ytlive_ed25519`、`config/live_config.json`（stream_key 平文 0600） | – | Mac SSH 経路と VPS Job 経路の二重。copy モードのキーフレーム要件は入力任せ |
| VidIQ / MCP | **実装なし**（SECURITY_NOTES に AceDataCloud MCP の検討記述のみ） | – | – |

## 5. 変更しにくい箇所と理由

1. **`app.py` + `from app_core import *`**: 227 route、7 startup hook、scheduler ジョブ本体が同居し、共有可変グローバルを app.py と router 4 本が star import。シンボル移動の影響範囲を静的に追えない。D9 の分割は途中で停止。
2. **工程表と完了判定の 4〜5 重定義**: `app_pipeline.STEPS`（11）、`app.py:_PIPELINE_STAGES`（7）、`app_orchestrator._ARTIFACT_DONE`、`routes.json`、VPS `scheduler.py` の依存表。工程の追加・改名で黙ってずれる。
3. **終了コードの衝突**: 75 = ログイン切れ ＝ ロック取得失敗、78 = CHANNEL_MISMATCH ＝ PREFLIGHT_FAIL、79 = 選曲待ち（orchestrator では FAIL）。pipeline・youtube・orchestrator・VPS worker の 4 か所にハードコード。
4. **`index.html` 12,550 行のインライン JS**: onclick 484、後読み JS が既存関数を上書き（`wrapExisting`）、ビルドなし、sw.js キャッシュ。削除済み DOM への初期化で null 例外（監査 U04）。
5. **`routes.json` / `studio.py` / VPS `client.py` の暗黙契約**: intent 名、`--vol/--channel/--from`、終了コード、日本語ログ文（「再開: …」）の regex 解析に 2 リポジトリがまたがって依存。
6. **設定読み込みの 4 系統**: `app_core`、`app_pipeline`、`app_ffrender`、`settings_service`。`production_mode` と `export_engine` の二重判定（監査 C03）。`~/.config/orzz` 直書き 48 箇所。
7. **グローバルなアクティブチャンネル**: `dashboard_config.json` と `active_app_id` ポインタ、`os.environ` の実行時書き換え（`app_orchestrator.dispatch`）。並列実行時にチャンネル指定が混ざる。

## 6. テストの有無

| 項目 | Mac | VPS |
|---|---|---|
| テスト | `Python/tests/` 2 ファイル（unittest 30 件）。`pyproject` の `testpaths=["tests"]` は実体と不一致。`import app` 時に本物の config dir に書く恐れ | `tests/` 28 ファイル、**211 件全合格**（今回実行、3.26 秒、warning 1,111 件は `utcnow` 非推奨） |
| CI | なし（`.github/` 不在） | なし |
| 実行環境 | system python 3.9.6、`requires-python>=3.10`、venv なし、lock なし、Pillow/numpy/playwright が依存未記載 | `.venv` 3.12、requirements 固定 |
| dry-run | `app_pipeline --dry-run`、`studio.py --dry-run`、`app_premiere --check`、`/api/health`、`scripts/p1p2p3_smoke.py`（22 ケース、要サーバ起動） | `/api/health`、worker `test` job |
| 未カバー | 実機 Adobe、SUNO 実サイト、YouTube 実投稿（テストチャンネル設定・投稿しない dry-run なし） | `worker/client.py` の `upload_video`、`run()` ループ、heartbeat、APScheduler 実発火、Postgres+Alembic |
| ログ | `print` 1,258 / `logging` 0、握りつぶし except 約 300、タスクログはメモリ 500 行・6h | logging あり |

今回の構文確認: Mac 側 90 ファイルを `ast.parse` で解析し全件成功。

## 7. 大幅修正で壊れやすいポイント（「制作・投稿で止まる」原因候補を含む）

本人の申告「動画制作や投稿でよく止まる」に直結すると考えられる箇所を優先して挙げる。

1. **選曲待ち（exit 79）が失敗扱い**: orchestrator・batch・VPS worker が 79 を FAIL にし、ブレーカー（連続 3 失敗で停止）に加算される。ハート採用を待つだけで自動運転が止まる構造。
2. **ロック取得失敗がログイン切れに化ける（exit 75 衝突）**: suno-browser / premiere-ame の flock が取れないと「手動ログイン要」と通知され、リトライされない。並列実行を増やすほど発生する。
3. **`_scheduler_reload()` がゲートを消す**: スケジュール API で保存・削除するたびに orchestrator tick、定時ジョブ、**予約公開ゲート**、auto_resume がメモリから消え、再起動まで復旧しない。「予約したのに公開されない」「tick が回らない」の直接原因になりうる。
4. **「複数本は 1 本即時・残り予約」ルールが未実装**: `publish_schedule.resolve_publish_at_iso` はフォルダ名の日付だけで決める。同日付フォルダが複数なら全部即時（過去時刻は 15 分後）。過去日付フォルダの防御は `UploadWorker` だけで、手動 `step_upload` / batch にはない。
5. **VPS worker の upload が二重投稿ガードを迂回**: marker・O_EXCL・reconcile を通らず、ネットワークエラーで再リースされると二重投稿の恐れ。
6. **Mac と VPS の制御二重化（カットオーバー未実施）**: 採番・autopilot フラグ・publishAt の決め方が別系統で排他なし。VPS 起票はフォルダを作らず Mac で exit 2。meta は VPS 側が「新しい」と判定されて逆流。
7. **VPS worker → studio.py のチャンネルガード**: `--channel` のみで `--switch` を付けないため、アクティブチャンネル以外は exit 3（VPS 側は再試行なしの failed）。
8. **auto_resume の再投入はメモリのみ**: 再起動で消える。再開コマンドは日本語文字列の regex 解析。
9. **Adobe 経路の `/tmp` IPC と「2026」固定**: 並行不可、版上げで即死。本人は未使用（§7.2）だが、設定未指定のチャンネルは今もこの経路に落ちる。
10. **SUNO の DOM 依存**: UI 変更のたびに修正が集中。main 未統合の修正 23 commit がある。
11. **run-pipeline API は全体 1 枠・ジョブ ID なし**: `_ensure_not_running("pipeline")`。並行処理を API から指示できない。
12. **公開リポジトリの未修正 P0（9/30 監査 S01〜S05）**: 認証免除、任意ファイル配信、任意フォルダ、SSRF、Codex sandbox 解除。トンネル経由（loopback に見える）で実質無認証。
13. **楽曲生成の保存先・命名が統制されていない**（本人申告の不具合。詳細は §7.1）。
14. **Adobe 未使用なのにコードと設定の既定が Adobe のまま**（詳細は §7.2）。

### 7.1 楽曲生成（SUNO）の保存先・フォルダ命名の統制不足

本人の申告「AI で楽曲生成するとフォルダ命名や保存場所の統制が取れない」に対応する実装上の原因。

**命名ルールが 1 か所に定義されていない**

| 対象 | 定義箇所 | 規約 | ずれ |
|---|---|---|---|
| vol フォルダ名 | `app_core.VIDEO_FOLDER_RE` / `parse_video_folder_name`（`app_core.py:501`） | `{vol}_{prefix}_{YYMMDD}`（「 のコピー」付きも許容） | `publish_schedule.py:25` は別 regex `^\d+_[^_]+_(\d{6})$`、`app_pipeline._resolve_folder`（`:287`）は `{vol}_(prefix|orzz)` で先頭一致し、最後は「vol 番号が一致する既存フォルダなら何でも拾う」保険つき |
| SUNO Workspace 名 | `routes.json` の `suno` / `suno-download` intent | `vol_vol{vol}`（固定） | `suno-auto` intent は `{channel_prefix}_vol{vol}`、`app_pipeline.step_suno`（`:721`）は `channel_name` を正規化した `{ch_name}_vol{vol}`。3 通りあり、同じ vol でも起動経路によって別 Workspace になる |
| ダウンロード先 | `suno_auto_create.py:4272` | `--download-dir` 省略時は **`Path.cwd()/suno_downloads`**（カレントディレクトリ依存） | 起動場所によって保存先が変わる。`--auto-download {folder}` 指定時のみ vol フォルダへ |
| ダウンロード時のファイル名 | `suno_auto_create.py:1780`、`suno_fast_dl.allocate_filename`（`:989`） | SUNO のタイトルを記号置換した `{title}.mp3`、重複は `_2`,`_3` | 2 経路で sanitize の文字集合が違う。タイトルは SUNO 側の生成物なので、vol・チャンネル・テイク番号を含まない |
| 後処理後の正式名 | `app_process_tracks.process_folder`（`:797〜985`）、`_apply_likes`、`_next_unique_title`（`:230`） | `music/{正式タイトル}.mp3`、採用は `z_` 前置（`:62`）、重複は「Reprise / Second Take / Take N」 | `original_music/` 側は `_normalize_original_names` で `_数字` を 1 回だけ除去して揃える。`music_tagged/`、`music_raw_pre_tag/` の中間フォルダが残る |
| タイトル履歴 | `.suno_history.json` | 共有 `config/.suno_history.json` と `~/.config/orzz/.suno_history.json` の **2 か所**（`app_process_tracks.py:176-179`、`suno_auto_create.py:658-918`、`app_music_catalog.py:102`） | どちらが正本か未定義。片方にしか無いタイトルは重複検知をすり抜ける |
| チャンネルフォルダ作成 | `app.py:1870`（`POST /api/channels`） | `プロジェクト/`、`素材/` を作る | orchestrator と pipeline が期待する `original_music/`、`music/`、`Image/` は作らない。工程側が都度 mkdir |

**結果として起きること**

- 起動経路（UI → API、`studio.py suno`、`studio.py suno-auto`、batch、手動 CLI）ごとに Workspace 名と保存先が変わり、同じ vol の楽曲が複数 Workspace や `suno_downloads/` に散る。
- Workspace 名が合わないと「2 回目の Workspace DL」（`app_pipeline.py:820〜`）が別タイトルの重複テイクを作る（コード内コメントにも同じ懸念の記述）。
- `vol_vol{vol}` 固定は channel_prefix を含まないため、複数チャンネルで同じ vol 番号があると SUNO 側で Workspace が衝突する。
- ファイル名が SUNO のタイトル由来のため、vol フォルダ外に出た mp3 はどの vol・どのチャンネルのものか判別できない。
- 2 か所の履歴ファイルと `_clean_title_key` の正規化（`z_` 除去、小文字化、NFC）の差で、重複タイトルの検知が経路によって変わる。

**改善案（§8.7 に再掲）**

1. 命名規約を 1 モジュール（例: `naming.py`）に集約し、vol フォルダ・Workspace・mp3・画像・mp4 の名前を `{channel_prefix}` `{vol}` `{YYMMDD}` `{take}` から生成する関数と、逆に解析する関数を 1 組だけ持つ。`routes.json` のテンプレートもそこから生成する。
2. Workspace 名を `{channel_prefix}_vol{vol}` に統一し、`vol_vol{vol}` と `channel_name` 由来の経路を廃止する。
3. ダウンロード先の既定を `Path.cwd()` から「vol フォルダ必須（未指定ならエラー）」に変える。
4. mp3 のファイル名に `{channel_prefix}_vol{vol}_{take:02d}_{title}` のように所属情報を含め、タイトルは ID3 タグ側に持たせる（既存の `_apply_metadata_tags` を拡張）。
5. `.suno_history.json` を 1 か所（VPS 正本化なら VPS の vols / music catalog）にし、Mac 側は読み取りキャッシュにする。
6. チャンネル作成時に `original_music/`、`music/`、`Image/` を含む標準レイアウトを作り、`qa` 工程でレイアウト逸脱（中間フォルダ残留、vol フォルダ外の mp3）を検知する。

## 8. 修正案・改善案（本人の要望に対応）

要望: (A) Claude/Codex から確実に動かせる動線、(B) 並行処理、(C) 制作・投稿で止まらない、(D) スケジュール設定で制作と投稿が漏れなく完走、(E) サムネ候補の提示→選択のボトルネック解消（Codex image 2.5。選定は Discord ではなく Claude/Codex に渡して決める）、(F) API 予約投稿、(G) VidIQ MCP と連携した分析→タイトル・概要欄生成、(H) 通知は Chatwork に集約、(I) 楽曲の保存先・命名の統制。

### 8.1 AI 主導の動線（A）

現状、業務操作はほぼ API/CLI で届く（`studio.py` 61 intent、API 約 376 本、`config set` 151 キー）。自走を止めているのは GUI ではなく「人の判断待ちが正式な状態でない」「ジョブ ID のない文字ログ」「文書と実装のずれ」の 3 点。

- **ジョブ API の新設**: `POST /api/runs` が `run_id` を返し、`GET /api/runs/{id}` で `{status, failed_stage, resume_from, exit_code, artifacts, awaiting}` を返す。runs.db への記録を全起動経路（studio.py 直実行を含む）で必須にする。
- **構造化出力**: `studio.py` / `app_pipeline.py` に `--json` を追加し、工程イベントと最終結果を JSON Lines で出す。「再開: …」文字列の regex 解析を廃止。
- **`awaiting_human` を正式状態に**: `{kind: login | selection | preflight | oauth}` を状態として持ち、Chatwork 通知と解除 API（例: `POST /api/runs/{id}/resume`）を用意。AI は待って再開するだけにする。exit 75/78/79 の衝突を解消し、定義を 1 モジュールに集約。選曲（ハート）とサムネ選定のうち AI に任せられるものは `awaiting_human` にせず `awaiting_ai_decision` として `app_llm_runner` に回し、人が残すのはログイン・OAuth・最終公開の承認だけにする。
- **データ契約の JSON Schema 化**: `.music_review.json`、`plan.json`、`vol_timeline.json`、`.ffrender_manifest.json`、`qa_report.json`、`thumbnail_state.json`、`/api/videos` の progress。OpenAPI を正本にして SPEC §5 の乖離（60 本 vs 376 本）を解消。
- **文書の矛盾解消**: launchd（CLAUDE.md vs AGENTS.md）、Drive `_claude` を指す Codex テンプレート、`routes.json` の `suno` intent の workspace 固定、count/interval の既定値、README の UXP 記述。AI が古い文書に従って誤作業する温床。
- **VPS に AI 用スコープ付き長期トークン**: 現状は 60 分 JWT+TOTP のみで無人運用不可。`jobs:create` / `vols:read` 限定のトークンを追加し、worker の intent / from_step を routes.json・STEPS と照合する。

### 8.2 並行処理（B）

- **資源モデルを明示**: SUNO ブラウザ（1）、Adobe（1、/tmp IPC）、Photoshop（1）、ffmpeg（CPU コア数）、LLM/画像 API（レート）、YouTube upload（quota）。`routes.json` の parallelism に `requires_mac` / `exclusive_resource` を持たせ、`resource_lock` と orchestrator が同じ表を読む。
- **ロック待ちと失敗を分離**: ロック取得失敗は `waiting_resource` として待機・再スケジュール（現状は exit 75 で「ログイン切れ」扱い）。
- **Adobe 経路は退役**（本人申告: 未使用）: 制作の既定を `ffmpeg_only` にし、Adobe の `/tmp` 固定 IPC と直列制約は並行処理の設計から外す（§7.2、§8.8）。
- **run-pipeline API の 1 枠制限を撤廃**: ジョブ API で vol 別・資源別に並列度を管理。batch_orchestrator のリレー方式（phase1 直列、以降並列）を API 化する。
- **チャンネル指定を環境変数・グローバル状態から引数へ**: `os.environ` 書き換えと `active_app_id` 依存を廃止し、台帳も app_id 分断をやめる。

### 8.3 止まらない制作・投稿（C）と スケジュール完走（D）

- **正本を 1 つに決める（最重要）**: Mac 内 APScheduler と VPS autopilot の二重化を解消。推奨は「VPS がスケジュールと台帳の正本、Mac は純粋な実行ワーカー」（VPS_SPLIT_PLAN フェーズ 5 の完遂）。その際、VPS 起票でフォルダ作成まで行う、meta 逆流（`_vps_result_is_newer`）を止める、worker upload を Mac の 3 段ガードと同じ関数に統一する。
- **`_scheduler_reload()` の修正**: 全消去ではなく `schedule_jobs.json` 由来のジョブだけ入れ替える。publish ゲートと auto_resume は永続化（runs.db か VPS）して再起動・reload で消えないようにする。
- **「1 本即時・残り予約」をコードで保証**: チャンネル別に `publish_time_jst` と投稿間隔（例: 1 日 1 本、同日複数なら翌日以降へ繰り越し）を設定し、`resolve_publish_at_iso` が過去日付・同日重複を拒否または繰り越す。過去日付の防御を `step_upload` 共通にする。
- **ブレーカーの誤作動を止める**: 79（選曲待ち）とロック待ちを失敗回数から除外。ブレーカー解除の記録を永続化。
- **日次の「漏れ検知」ジョブ**: 予定日に対して `youtube_upload.json` / VPS vols の `step_states` を突合し、未完了 vol を Chatwork に列挙（既存 `app_reconcile` と `app_digest` の延長）。これが「漏れなく投稿」の監視線になる。
- **観測性**: `print` → `logging`（JSON Lines、vol/run_id 付き）。握りつぶし except 約 300 のうち制作・投稿経路を優先して警告化。

### 8.3.1 通知の Chatwork 集約（本人の意向）

現状は Discord webhook のみ（`app_notify.sh`、`config/discord_config.json`、VPS は SMTP）。Chatwork は未実装。

- **通知層を 1 つに**: `app_notify.sh` の curl 直叩きと、app.py / routers / digest / learning / suno の各所から呼ばれる Discord 送信を `notify.py`（Python）に集約し、backend を `chatwork` に設定で切り替える。Discord は移行期間だけ併送し、その後廃止。
- **Chatwork 実装**: Messages API（`POST /v2/rooms/{room_id}/messages`）。API トークンは Mac 側なら `~/.config/<app_id>/` の 0600 ファイル、VPS 正本化なら VPS の暗号化設定に置き、ルーム ID は用途別（運用通知・要判断・日次ダイジェスト）に設定化する。既存の EC 側で Chatwork 通知を使っている実績（買い場通知）があれば、その実装を流用する。
- **通知の種別を分ける**: `info`（完了・ダイジェスト）、`action_required`（`awaiting_human`: ログイン・OAuth・最終承認）、`alert`（ブレーカー停止・漏れ検知・二重投稿疑い）。`action_required` と `alert` だけを Chatwork の [To] 付きにする。
- **画像の扱い**: Chatwork は画像のインライン表示が弱いので、コンタクトシートはファイル添付（`POST /v2/rooms/{room_id}/files`）か Drive の共有リンクにする。選定自体は §8.4 のとおり AI が行い、人には結果だけ届ける。
- **VPS 側**: SMTP（パスワードリセット・招待）はそのまま残し、運用通知だけ Chatwork へ。

### 8.4 サムネ候補の提示→選択（E）

現状、Vision 100 点採点（`app_thumbnail_scoring`）、`judge_auto_approval(conditional, score≥80, ctr≥7, similarity≤25)`、候補管理（`thumbnail_state.json`）、反復生成ループ（`app_image_eval_loop`、既定 OFF）が揃っている。欠けているのは「チャットで並べて人が選ぶ」工程をコードに置き換える外側だけ。

- **選択を「採点付き自動 + 判断は Codex / Claude」に**（本人の意向: Discord ではなく AI に渡して決める）: 既定を `APP_THUMB_EVAL_LOOP=1`・`max_attempts=3` にし、合格 1 枚があれば自動採用。合格なし、または複数合格で差が小さいときは、候補グリッドと採点 JSON を `app_llm_runner`（`claude -p` → `codex exec`）に渡し、「採用 1 枚 + 理由 + 不採用理由」を JSON Schema で返させて `thumbnail_state.json` に確定を書く。人は結果を Chatwork で受け取るだけにし、チャットでの候補提示と選択の往復をなくす。
- **候補グリッドの生成**: 既存の `app_image_compositor` で N 枚を 1 枚のコンタクトシート（番号・点数付き）にし、AI が見る入力も 1 枚にする。Claude Code / Codex のセッションから手動で判断する場合も同じファイルと JSON を読めばよい。
- **選定の手続きを `routes.json` の intent に**: `thumbnail-select --vol N`（採点 → AI 判断 → 確定）を追加し、`studio.py` から 1 コマンドで走らせる。確定は `POST /api/thumbnail/state/approve`（既存 `routers/images.py:2117`）を経由して台帳に残す。
- **Codex image 2.5 への追従**: `codex_imagegen.py` のモデル名 `gpt-image-2` を設定化（チャンネル別 `image_model`）。`codex exec` 経路は `~/.codex/generated_images/` の最新ファイル拾いなので、並列生成時に取り違える。出力ディレクトリを vol 別に指定するか API 経路（`OPENAI_API_KEY`）へ寄せる。
- **実績の還流**: `ab_log.json` の 7 日後 views を `thumbnail_state` に戻し、閾値を実績で調整（IMAGE_EVAL_LOOP S6）。

### 8.5 API 予約投稿（F）

- 既に `private + publishAt` で実装済み（Mac `app_youtube.py`）。問題は 8.3 のゲート消失・ルール未実装・VPS 経路のガード迂回。
- VPS 側 OAuth scope が upload のみなので、VPS 正本化なら `youtube`（または `youtube.force-ssl`）を追加し、説明文更新・公開切替・Analytics を VPS トークンで通す。Mac 側トークン（Drive 上）は廃止候補。
- quota は Google 側の実残量を照合する手段がないため、`APP_YT_DAILY_QUOTA_CAP` の帳簿を VPS に一本化し、upload（1,600）と `videos.update`（50）を予約時点で引き当てる。

### 8.6 VidIQ MCP 連携と タイトル・概要欄生成（G）

現状 VidIQ 連携・MCP クライアントは存在しない。既存の分析資産は YouTube Data/Analytics API ベース（benchmark、video_intel、comment_mining、genre_radar、TTP、insights）で、`claude_proposer` がタイトル・説明文・タグを生成し、VPS `insights` がヒントをメタ生成に注入している。

- **連携の置き場**: Claude Code / Codex のセッションから MCP を叩くのが最短だが、無人運用には向かない。推奨は「VPS の `insights` に `source=vidiq` の取り込みジョブを足し、キーワード・競合タイトル・スコアを `insights` テーブルへ正規化して保存」。`claude_proposer` と VPS proposer は既に insights を読むので、プロンプト側は小変更で済む。
- **MCP クライアント実装**: Python で stdio/SSE の MCP クライアントが要る（SECURITY_NOTES にも同じ指摘）。VidIQ の認証情報は VPS の暗号化設定（`app_settings`）に置き、Mac 側には出さない。
- **出力契約**: タイトル候補 N 件・説明文・タグを JSON Schema で固定し、VidIQ のスコアと Vision 採点を同じ `plan.json` に記録して後から実績と突合できるようにする。
- **未確認**: VidIQ MCP の提供形態・レート・利用規約。実装前に本人側で確認が必要。

### 7.2 Adobe 未使用と、コード・設定の既定のずれ

本人の運用は「PSD と Premiere は使わない、ffmpeg 書き出しのみ」だが、コードと設定はそうなっていない。

**判定ロジック**（`app_pipeline.py`）

- `_export_engine()`（`:1827`）: `production_mode == "ffmpeg_only"` なら ffmpeg。それ以外は `export_engine` を見て、**未設定なら `"ame"`（Adobe）**。
- `step_psd_composite`（`:1451`）: `production_mode == "ffmpeg_only"` のときだけ Pillow 合成。**`export_engine=ffmpeg` だけでは Photoshop（AppleScript）を呼ぶ。**
- preflight（`:3569`）: `export_engine != "ffmpeg"` なら Premiere Link パネルの起動確認を要求し、失敗で exit 78。

**現在の設定（`config/channels.json` 10 チャンネル + 各 `.app_channel_config.json`、読み取りのみ）**

| 状態 | チャンネル | パイプラインを回した場合の挙動 |
|---|---|---|
| `production_mode=ffmpeg_only` + `export_engine=ffmpeg` | wobble_day | 全工程 Adobe なし（意図どおり） |
| `export_engine=ffmpeg` のみ | violin_ambient、orzz、ww、sukima、ragtime_whiskers | premiere/export は ffmpeg だが、**psd_composite で Photoshop を呼ぶ**。Photoshop が無いか TCC 未許可なら psd 工程で停止 |
| どちらも未設定 | edm、chicago_jazz、harbor_notes、orbs | **既定 `ame`**。preflight で Premiere Link を要求し exit 78（PREFLIGHT_FAIL）で停止。`render_queue` も Premiere/AME 前提 |

`autopilot_enabled` は全チャンネル未設定なので Mac 側の自動運転からは起動しないが、手動・VPS 経由で pipeline を回すと上記のとおり止まる。「制作でよく止まる」の一因になりうる。

**関連する負債（退役対象に変わるもの）**

- `app_premiere.py`（920 行）、`jsx_bundle.py`（1,284 行）、`cep_extension/`（premiere_link、photoshop_link）、`uxp_plugin/`、`Script/*.jsx`、`YouTube_1080p_Optimized.epr`、`app_photoshop.py`（1,132 行）、`routers/premiere_photoshop.py`（735 行、22 route）、`app_render_queue.py`（Premiere/AME 直列キュー）、`app_timeline.py` のうち Premiere 配置向け部分、`web/static/js/assembly-premiere-layout.js`、`css/assembly-premiere-layout.css`。
- 9/30 監査 C03（`production_mode` と `export_engine` の二重判定）がそのまま該当。
- README / OVERVIEW / skills（app-premiere.md、app-psd-composite.md、app-export.md）は Adobe 必須と書かれており、AI が読むと Adobe 前提で作業する。
- `STEPS` の 11 工程のうち `psd_composite`、`premiere` は ffmpeg 経路では実質「素通り」か別実装への分岐で、工程名が実体と合っていない。

**改善案（§8.8 に再掲）**

1. 既定を反転する: `_export_engine()` の既定を `ffmpeg`、`step_psd_composite` の既定を Pillow 合成にし、Adobe は `production_mode=adobe` の明示オプトインだけで動くようにする。二重判定（C03）を `production_mode` 1 本に統一し、`export_engine` は読み替えて廃止。
2. 10 チャンネル全てを `production_mode=ffmpeg_only` に揃える（本人確認済み: 4 チャンネルは過去 Adobe 制作だが ffmpeg で代替可能）。設定変更なので実施は本人の許可のもとで行う。切り替え後の最初の vol は `--dry-run` と `qa` で ffmpeg 成果物を確認する。既存 vol の Adobe 成果物（.prproj、PSD）は完了判定に使われないが、`vol{N}.jpg`（Photoshop 出力）を背景として再利用しないよう `vol{N}.png` / `vol{N}_source.jpg` の有無を確認する。
3. `STEPS` を ffmpeg 経路の実体に合わせて改名（例: `psd_composite` → `composite`、`premiere` + `export` → `render`）。§7 の工程表多重定義の解消と同時に行う。
4. Adobe 系モジュールは削除する（本人確認済み: 将来も使わない）。手順は、削除直前の commit に tag（例: `pre-adobe-removal`）を打って履歴で戻せるようにし、routes.json・skills・README・OVERVIEW・settings_catalog（Adobe 関連キー）・pyproject の pymiere 依存から参照を外す。`STEPS` の psd_composite / premiere は改名ではなく ffmpeg 経路の実体（composite / render）に置き換える。
5. preflight から Premiere Link 確認を外し、代わりに ffmpeg / ffprobe / Pillow の存在確認にする。
6. §7 の「大幅修正で壊れやすいポイント」9（Adobe の /tmp IPC）と §8.2 の Adobe 並行不可は、退役により解消する。並行処理の制約は SUNO ブラウザ（1）と CPU・API レートだけになる。

### 8.7 楽曲の保存先・命名の統制（本人申告の不具合）

§7.1 の改善案 1〜6 を段階 2（状態と契約）に含める。命名モジュールの集約は他工程（画像 `vol{N}.png`、PSD `{prefix}_vol{N}.psd`、mp4 `{prefix}_vol{N}.mp4`）も同じ問題を抱えているため、楽曲だけでなく全成果物を対象にする。

### 8.8 Adobe 経路の退役と ffmpeg 既定化（本人の運用に合わせる）

§7.2 の改善案 1〜6。既定の反転と設定の統一は小さな変更で効果が大きく（4 チャンネルの exit 78 停止と 5 チャンネルの Photoshop 呼び出しを止める）、段階 1 の直後に単独で実施できる。モジュールの隔離は段階 7。

## 9. 推奨する分割・段階案

| 段階 | 内容 | 根拠 | 依存 |
|---|---|---|---|
| 0. 保全 | Drive 監査資料と config_backups の private 退避。未統合 branch の統合判断。public repo の P0（S01〜S05）と docs 内の個人情報・IP の除去。dashboard_config.json の 0600 化 | §0.1、§7-12 | なし。**10/26 期限** |
| 1. 検証基盤 | Mac の venv/lock、`testpaths` 修正、テストの config dir 隔離、両 repo に CI、`logging` 導入、終了コード定義の一本化 | §6 | 0 |
| 1a. ffmpeg 既定化 | `_export_engine` と psd 合成の既定反転、`production_mode` 一本化、10 チャンネルの設定統一、preflight の差し替え | §8.8 | 1（単独で先行可） |
| 2. 状態と契約 | ジョブ API + run_id、`--json`、`awaiting_human`、JSON Schema、工程表の単一定義（STEPS を正本に VPS 依存表を生成、ffmpeg 経路に合わせて工程名を改名）、**成果物の命名・保存先の単一定義**（§7.1） | §8.1、§8.7 | 1 |
| 3. スケジュール正本化 | VPS 正本化（起票でフォルダ作成、upload ガード統一、meta 逆流停止、`_scheduler_reload` 修正、1 本即時・残り予約ルール、漏れ検知ジョブ） | §8.3、§8.5 | 2 |
| 4. 並行処理 | 資源モデル（SUNO ブラウザ・CPU・API レート）、ロック待ちの状態化、run-pipeline の 1 枠撤廃。Adobe の /tmp IPC は退役で対象外 | §8.2 | 1a、2 |
| 3a. 通知の Chatwork 集約 | `notify.py` 新設、Chatwork backend、種別分け、Discord 廃止 | §8.3.1 | 1（2 と並行可） |
| 5. サムネ自動化 | eval loop 既定 ON、コンタクトシート、image 2.5 設定化、AI（Claude/Codex）による選定 intent | §8.4 | 2 |
| 6. 分析強化 | VidIQ 取り込み → insights → メタ生成 | §8.6 | 3 |
| 7. 整理 | app.py 分割の再開、index.html の分割、**Adobe 系一式の削除**（app_premiere / app_photoshop / jsx_bundle / cep_extension / uxp_plugin / Script/*.jsx / *.epr / render_queue / routers/premiere_photoshop / assembly-premiere-layout、削除前に tag）、未使用コード（flow_userscript、save_auth、suno_queue、vlog、photos）の archive | §5、§7、§7.2 | 1a |

段階 2〜4 は同じファイル群（`app_pipeline.py`、`app_orchestrator.py`、`app.py`、`worker/client.py`）に集中するため、単一 writer・1 branch で直列に進める。段階 5・6 は別ファイル群なので 2 と並行可能。

## 10. 未確認事項

1. **Drive の監査資料 `docs/review/2026-09-30/` の全文**: 存在と目次は確認したが中身は未読。P0 の正確な件数・再現手順はそこにある。
2. **未統合 branch の品質**: `feature/suno-chrome-direct-hook`（+23）と `drive-snapshot-20261012`（+24）のテスト状況・main との競合は未確認。
3. **本番 VPS の実態**: 手順書は Hetzner、記録は Xserver（`docs/XSERVER_RESTORE_2026-09-06.md`、backend :8001）。実稼働先・デプロイ済み SHA・alembic 適用状況は未確認（本番操作は禁止のため）。
4. **VPS フェーズ 5 カットオーバー条件**（全 ch 認証、ワーカー完走、予約反映、ロールバック、SKIP LOCKED 検証）の達成記録なし。
5. **「ライブ配信 copy モード化」の記録**: live_config は全 5 本 `mode=copy` だが、コード・文書に方向転換の記述が見つからなかった（メモリの記録と一致しない点）。
6. **Chatwork 通知**: 依頼文に挙がっていたが実装なし。Discord のみ。
7. **VidIQ MCP** の提供形態・認証・レート制限。
8. **`docs/*.png` スクリーンショット**の中身（public repo での個人情報混入の可能性）。
9. **Mac 側テストの実行**: 依存（3.10+、fastapi、PIL）と config dir 汚染の恐れがあるため今回は未実行。構文解析のみ。
10. **route の重複**: app.py と routers で `/api/analysis`・`/api/benchmark` が両方にあるが、同一 path の重複は「なし」と判定（登録順依存の挙動は未検証）。
12. **楽曲の散逸の実態**: §7.1 はコード上の原因分析で、実際の Drive 上に `suno_downloads/` や vol 外 mp3 がどれだけあるかは未集計（Drive は参照のみのため走査していない）。
13. **Adobe 退役の確定範囲**: 本人確認済み（2026-10-25）。**将来も Adobe は使わない。** 設定未指定の 4 チャンネル（edm、chicago_jazz、harbor_notes、orbs）は過去に Adobe で制作したが、本人確認により **ffmpeg で代替可能**。したがって 10 チャンネル全てを `ffmpeg_only` に統一してよい。残る確認は、これら 4 チャンネルの既存 vol フォルダに Adobe 経路の成果物（.prproj、PSD、AME 書き出し mp4）が残っていて、ffmpeg 経路の完了判定（`.ffrender_manifest.json` の有無）とずれないか、の 1 点。
11. **`worker.json` の `studio_root` が Drive 旧 checkout を指す件**: 現在 VPS ワーカーがどの checkout を実行しているかは未確認（設定ファイルの値は読んだが実行は確認していない）。

## 付録: 調査で実行したコマンドと結果

- `git fetch` 後の状態記録: §0 の表のとおり。
- VPS テスト: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider -rs` → 211 passed, 0 failed, 0 skipped, 3.26s。
- Mac 構文確認: python3 3.9.6 の `ast.parse` で 90 ファイル全件成功。
- ファイル変更: なし（本レポートの新規作成のみ、未 commit）。

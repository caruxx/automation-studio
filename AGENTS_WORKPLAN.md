# Automation Studio — エージェント化 実行計画書（WORKPLAN）

> **用途**: 別PCで本ファイルを起点に作業を継続するためのハンドオフ。
> **背景・設計の根拠**: [AGENTS_DESIGN.md](AGENTS_DESIGN.md)。本書は「何を・どの順で・具体的にどう変更するか」の手順。
> **起案**: 2026-05-31 / ブランチ: `main` / 基点コミット: `2817196` (snapshot before JSX edit)

---

## 0. 別PCで始める前に（必読）

1. **作業ディレクトリ**: `<共有ドライブ>/DEV/_claude`（Google Drive 共有。別PCでも同パスで同期）
2. ⚠ **`.git` 同期の競合リスク**: 共有ドライブ上の `.git` は同期遅延・競合で壊れることがある。**別PCで作業を始める前に、このPCで一度コミットして作業ツリーを確定させることを強く推奨**（コミットはユーザー判断。未実施なら未コミット変更がファイル実体として同期されている前提で進める）。
3. **サーバー起動/再起動**: `bash Python/start.sh`（ポート8888。既存プロセスを kill して起動）。⚠ **uvicorn は reload 無し**＝Python を変更したら必ず再起動しないと反映されない（今回の `meta-status` 不具合の真因がこれ）。
4. **動作確認の基本**: `curl -s http://127.0.0.1:8888/openapi.json` でルート数（正常時 220）、各 API は実際に叩いて確認。

---

## 1. 現在の状態スナップショット

### 1.1 今回セッションで完了済み（検証済み・未コミット）
| 対象 | 変更 | 状態 |
|------|------|------|
| `Python/app.py` `api_suggest_imitate_evolve` (~4256) | 英語プロンプト→**日本語出力**に書換（imitate/avoid/evolve/summary 全項目）。JSONキーは維持 | py_compile OK・マスター上書き無し確認済 |
| `web/static/index.html` (旧 5121-5124) | 英語 `strategy-note`（English-first metadata...）を**削除** | 完了 |
| 稼働サーバー | 旧コードのまま動いていた→**再起動**。openapi 194→**220**、`meta-status` 等28APIが復活 | 実API 200 確認済 |

### 1.2 未コミットの変更（`git status`）
- 今回分: `Python/app.py`, `web/static/index.html`
- **今回と無関係な既存変更**（別作業由来。混ぜてコミットしないこと）: `app_pipeline.py`, `app_youtube.py`, `jsx_bundle.py`, `suno_auto_create.py`, `Script/…premiere_long.jsx`, `*.sh`, `YouTube_1080p_Optimized.epr` ほか
- 新規(untracked): `AGENTS.md`, `AGENTS_DESIGN.md`, 本書, `.claude/commands`, `.claude/skills`, `skills/app-multilingual-meta.md`, `skills/app-psd-composite.md`
- ⚠ `CLAUDE.md` は type-change(T)＝**壊れたシンボリックリンク**（ターゲット空）。→ Phase 0 で修復。

---

## 2. 実装の前提（設計書 §6 を推奨で仮確定。別PCで変更可）

| 項目 | 決定 | 理由 / 変更余地 |
|------|------|----------------|
| ドメイン粒度 | **7分割** (music/image/video/publish/analysis/pipeline/web) | 技術スタックが完全に異なる境界。統合するなら5分割案（AGENTS_DESIGN §1）に差替可 |
| 命名 | **短名**（`music` 等） | プロジェクト内名前空間で衝突しない。接頭辞 `as-` を付けるなら全 name と本書を一括置換 |
| model | **video/pipeline/web=opus、他=sonnet** | 複雑度・改変リスクの高い領域に opus。コスト優先なら全 sonnet も可 |
| (B) upload 自律度 | **人間承認ゲートを維持**（完全自動にしない） | 誤投稿リスク回避。Phase 3 で公開ゲート接続時に再判断 |
| tools | Read, Edit, Bash, Grep, Glob | Bash許可＝**サーバー再起動して検証まで自走**できる。ローカルのみでリスク低 |
| 着手順 | Phase 0 → 1 → (B)は qa-worker から | 既存基盤への最小追加で効果検証 |

---

## 3. 作業手順

### Phase 0 — CLAUDE.md 修復（最優先・5分）

**現状**: `CLAUDE.md` はシンボリックリンクだがターゲットが空（`CLAUDE.md -> ` 矢印の後が空白）。Claude Code はプロジェクト指示として CLAUDE.md を読むため、**今プロジェクト固有ルールが読み込まれていない**。

**手順**:
```bash
cd "<共有ドライブ>/DEV/_claude"
rm CLAUDE.md          # 壊れたリンクを削除
# 下記内容で実体ファイルを作成（次のコードブロック）
```

`CLAUDE.md`（実体）に書く内容:
````markdown
# Automation Studio — Claude Code プロジェクト指示

orzz. ダッシュボード = YouTube BGM チャンネルの動画制作を全自動化するツール。

## 必読
- [AGENTS.md](AGENTS.md) — 運用コマンド＋「自然言語→実行」マッピング
- [SPEC.md](SPEC.md) — API一覧・データ契約・アーキテクチャ
- [AGENTS_DESIGN.md](AGENTS_DESIGN.md) / [AGENTS_WORKPLAN.md](AGENTS_WORKPLAN.md) — エージェント化の設計と計画
- [skills/](skills/) — 機能別スキル23個

## 開発時の鉄則
- ⚠ サーバーは uvicorn **reload無し**。Python変更後は `bash Python/start.sh` で再起動しないと反映されない。
- ⚠ Premiere JSX は **JSX Launcher 拡張経由でのみ**実行（AppleScript不可）。
- ⚠ pipeline stage 追加時は `STEPS` / `STEP_LABELS` / `STEP_FUNCS` / `RETRY_POLICY` の**4箇所を一貫更新**。
- sentinel exit: 0成功 / 1失敗 / 75 unattended_login / 76 retryable / 77 quota_exhausted / 78 preflight_fail
- 分析情報は日本語、YouTube出力メタ(title/description/tags)は英語。多言語は英語ソースから翻訳。

## サブエージェント（.claude/agents/）
ドメイン別に委譲: music / image / video / publish / analysis / pipeline / web（詳細 AGENTS_DESIGN §2）

## 言語
ユーザーとのやり取り・コメントは日本語。
````

---

### Phase 1 — 開発サブエージェント7個（低コスト・即効）

`.claude/agents/` を作成し、以下7ファイルを配置。各ファイルはそのまま貼り付け可。

**`.claude/agents/music.md`**
```markdown
---
name: music
description: SUNO楽曲生成・ダウンロード・リネーム・ffmpeg後処理の修正/デバッグ。「楽曲」「SUNO」「リネーム」「フェード」等で起動。
tools: Read, Edit, Bash, Grep, Glob
model: sonnet
---
あなたは Automation Studio の楽曲ドメイン専門エンジニア。
## 担当
suno_auto_create.py, flow_automation.py, app_process_tracks.py / app.py の /api/suno/*。工程: suno, rename。
## 勘所
- Playwright(SUNO)はUI変化に脆い。無人時ログイン不可は exit 75。--batch で多様性、cache miss は Playwright 再起動。
- ffmpeg 必須(brew install ffmpeg)。リネームのみは --rename-only、後処理はフェード+ゲイン正規化。
- 無人モードは APP_NO_INTERACTIVE=1（input/sleep ハング禁止）。
## 関連skill
skills/app-suno-download.md, skills/app-rename-audio.md
## 作業後
変更が pipeline/API に効くか確認してから報告。
```

**`.claude/agents/image.md`**
```markdown
---
name: image
description: 背景画像生成・PSD合成・サムネ生成/スコアリングの修正/デバッグ。「背景画像」「サムネ」「PSD」「Photoshop」等で起動。
tools: Read, Edit, Bash, Grep, Glob
model: sonnet
---
あなたは Automation Studio の画像・サムネドメイン専門エンジニア。
## 担当
app_photoshop.py, codex_imagegen.py, app_midjourney.py, app_image_prompt.py, app_thumbnail_scoring.py, app_channel_thumbnail.py, app_thumbnail_state.py, app_benchmark_thumbnail.py, scene_text_generator.py / app.py の /api/bgimage/*, /api/photoshop/*, /api/channel-thumbnail/*, /api/thumbnail-state/*, /api/midjourney/*, /api/codex-imagegen/*。工程: bgimage, psd_composite, thumbnail。
## 勘所
- provider は flow,codex 並列(APP_THUMBNAIL_PROVIDERS)。APP_THUMBNAIL_DISABLE=1 で無効。
- サムネ/背景は vol{N}.jpg / vol{N}.png を優先。bgimage 強制再生成は APP_BGIMAGE_FORCE=1。
- PSD合成はレイヤ名が設定駆動(psd_base_layer / psd_toggle_layer / psd_text_layer / psd_text_font)。base + サムネイル.jpg の2枚出し。
- Photoshop は UXP/CEP パネル前提。
## 関連skill
skills/app-bgimage.md, skills/app-psd-composite.md, skills/app-image-select.md
## 作業後
変更が pipeline/API に効くか確認してから報告。
```

**`.claude/agents/video.md`**
```markdown
---
name: video
description: Premiere自動配置・書き出し・render queue・JSXの修正/デバッグ。「Premiere」「配置」「書き出し」「レンダー」「JSX」等で起動。
tools: Read, Edit, Bash, Grep, Glob
model: opus
---
あなたは Automation Studio の Premiere/動画化ドメイン専門エンジニア。
## 担当
app_premiere.py, jsx_bundle.py, app_render_queue.py, Script/*.jsx / app.py の /api/premiere/*, /api/render-queue/*, /api/ame/*, /api/export/*。工程: premiere, export。
## 勘所
- ⚠ JSX は **JSX Launcher 拡張経由でのみ**実行（AppleScript不可）。
- pymiere 接続 + CEP パネル起動が前提。preflight 失敗は exit 78。
- render queue でシリアライズ(APP_USE_RENDER_QUEUE=1, 1 worker)。APP_RENDER_QUEUE_DISABLE=1 で抑止。
- timeout 目安: premiere 3600s / export 7200s。MP4 検出は _find_exported_mp4。
## 関連skill
skills/app-premiere.md, skills/app-export.md
## 作業後
実機(Premiere/AME)依存はモック確認の範囲を明示。pipeline 経由の整合を確認してから報告。
```

**`.claude/agents/publish.md`**
```markdown
---
name: publish
description: YouTubeメタ生成・多言語化・アップロード・videos.update(snippet)の修正/デバッグ。「YouTube」「メタ」「アップロード」「多言語」「quota」等で起動。
tools: Read, Edit, Bash, Grep, Glob
model: sonnet
---
あなたは Automation Studio の YouTube 公開ドメイン専門エンジニア。
## 担当
app_youtube.py, claude_proposer.py / app.py の /api/youtube/*, /api/videos/{name}/(meta-status|generate-localizations|mp4-info|suggest|title|tags), /api/youtube-desc/*。工程: meta, upload。
## 勘所
- quota: insert=1600 / update=50 unit（9600/日）。誤投稿修正は update-snippet で32倍安い。枯渇は exit 77。
- OAuth 再認証: python3 app_youtube.py --auth-only。
- ⚠ サーバーは reload無し。app.py 変更後は bash Python/start.sh 再起動して反映確認。
- メタ正本は <vol_folder>/youtube_{title,description,tags}.txt。多言語は英語ソースから翻訳（既定10言語）。
- 分析情報は日本語、出力メタは英語。
## 関連skill
skills/app-youtube-upload.md, skills/app-youtube-desc.md, skills/app-multilingual-meta.md
## 作業後
稼働サーバー(:8888)を再起動し openapi/実APIで反映確認してから報告。
```

**`.claude/agents/analysis.md`**
```markdown
---
name: analysis
description: 競合・ベンチマーク分析、徹底パクリ進化、シリーズ提案の修正/デバッグ。「競合」「ベンチマーク」「パクリ進化」「提案」等で起動。
tools: Read, Edit, Bash, Grep, Glob
model: sonnet
---
あなたは Automation Studio の分析・提案ドメイン専門エンジニア。
## 担当
app_competitor.py, app_sheets.py, app_benchmark_concept.py, app_benchmark_title.py, app_benchmark_thumbnail.py, app_series.py, claude_proposer.py(提案部) / app.py の /api/analysis/*, /api/benchmark/*, /api/series/*。
## 勘所
- ⚠ 分析情報は日本語・出力メタ(title/description/tags)は英語。混同しない。
- Sheets 経由で API quota ゼロ。ベンチ対象はピン留め優先→hot 上位5フォールバック。
- imitate_evolve プロンプトは**日本語固定**（2026-05-31 に英語→日本語修正済。マスター上書き無し）。結果が英語なら旧キャッシュを疑う。
## 関連skill
skills/app-competitor-spreadsheet.md, skills/app-imitate-evolve.md, skills/app-series-proposals.md, skills/app-ai-propose.md
## 作業後
Claude CLI 呼び出しは課金。プロンプト変更時は1回だけ実走確認。
```

**`.claude/agents/pipeline.md`**
```markdown
---
name: pipeline
description: パイプライン統括・中央台帳・retry/auto_resume・scheduler・QAの修正/デバッグ。「パイプライン」「台帳」「自動再開」「スケジューラ」「工程」等で起動。
tools: Read, Edit, Bash, Grep, Glob
model: opus
---
あなたは Automation Studio のオーケストレーション・ドメイン専門エンジニア。
## 担当
app_pipeline.py, app_run_ledger.py(SQLite runs.db), app_token_health.py / app.py の /api/runs/*, /api/pipeline/*, /api/process/*, scheduler・auto_resume 関連。工程: qa + 全体統括。
## 勘所
- ⚠ stage 追加/変更は STEPS / STEP_LABELS / STEP_FUNCS / RETRY_POLICY の**4箇所を一貫更新**（前例: P2-5 step_thumbnail）。
- STEPS = [suno, rename, bgimage, psd_composite, premiere, export, qa, meta, thumbnail, upload]。
- sentinel exit(0/1/75/76/77/78)を壊さない。retryable=76 で _run_step_with_retry。
- 中央台帳: start_run/finish_run/cancel_run。status は in_progress/done/failed/cancelled/reconstructed。stale 6h 降格。auto_resume は parent_run_id 親子チェーン。
- render queue / preflight / scheduler(_balance_trigger_slot 30分分散) と連携。
## 関連skill
skills/app-workflow.md, skills/app-schedule.md
## 作業後
台帳スキーマ・exit code 契約を壊していないか確認してから報告。
```

**`.claude/agents/web.md`**
```markdown
---
name: web
description: FastAPIバックエンド(app.py)・フロント(index.html)・設定/認証/History APIの修正/デバッグ。「ダッシュボード」「API」「UI」「ルート」「設定画面」等で起動。
tools: Read, Edit, Bash, Grep, Glob
model: opus
---
あなたは Automation Studio の Web基盤ドメイン専門エンジニア。
## 担当
app.py(11,933行/220ルート全般・config・credentials・status), web/static/index.html, _app_config.py。
## 勘所
- ⚠ uvicorn reload無し。app.py 変更後は bash Python/start.sh 再起動必須。
- ルート未登録の切り分け: openapi.json のパス集合と @app デコレータを突合（今回の meta-status 不具合の手口）。
- フロントは esc() で XSS防止。テンプレートリテラル内HTMLのタグ対応に注意。
- ファイル参照名の整合（youtube_*.txt 等、保存側と参照側を一致させる）。
- /api/status/all で全タスク状態、/api/export/queue 等でキュー確認。
## 関連skill
skills/app-web-dashboard.md, skills/app-master-config.md, skills/app-remote-access.md
## 作業後
変更ルートを実際に叩いて 200/期待JSON を確認してから報告。
```

**Phase 1 完了確認**: `ls .claude/agents/` で7ファイル。各エージェントに簡単なタスク（例: publish に「meta-status のレスポンス項目を説明して」）を投げ、担当範囲を正しく認識するか確認。

---

### Phase 2 — (B) qa-worker（step_qa の実装）

**前提確認（別PCで現物を見る）**: `STEPS` に `"qa"` は既に登録されている（app_pipeline.py:67）が、`step_qa` の中身は未実装の可能性が高い（メモリ: P3-3 未着手）。まず `grep -n "step_qa\|def step_qa\|STEP_FUNCS" Python/app_pipeline.py` で実体を確認。

**実装内容**: export 後の MP4 を ffprobe で自動検証。
- 検査項目: 解像度(=1920x1080)、尺(pipeline 指定長 ±許容)、video codec(h264)、音声トラック有無、ファイルサイズ下限。
- 合格 → exit 0、次工程(meta)へ。
- NG → exit 76(retryable) で `--from premiere`（または export）に差し戻し、auto_resume に乗せる。致命的なら exit 1。
- 既存 `_find_exported_mp4` と `/api/videos/{name}/mp4-info`（解像度/尺/コーデック取得済）を再利用。

**4箇所更新**（pipeline 鉄則）:
1. `STEPS`/`STEPS_WITH_PLAN` — `"qa"` は登録済（確認のみ）。
2. `STEP_LABELS["qa"]` — "7/10 QA（ffprobe 検証）" を設定（未設定なら）。
3. `STEP_FUNCS["qa"]` → `step_qa(vol, folder, ...)` を実装・登録。
4. `RETRY_POLICY["qa"]` — 検証失敗時の retry/backoff を定義（差し戻し系は1回）。

**台帳連携**: step_qa の結果を run_ledger に記録（finish_run の summary に検査結果）。

---

### Phase 3 以降（別PC作業時に本書へ追記して詳細化）

- **Phase 3**: publish-worker を公開ゲート(`publish_video_to_public`)に接続。QA通過＋人間承認で本公開（§2 の自律度方針に従い承認ゲートを残す）。
- **Phase 4**: 全ドメインを StageWorker 共通I/F（can_run/run/on_fail/record, AGENTS_DESIGN §3.3）に再整理＋オーケストレーター拡張（依存解決・能動トリガー）。
- **Phase 5**: P3 残（plan自動採択 P3-2 / policy-aware配分 P3-4 / token health cron P3-5）。

---

## 4. 各 Phase の検証方法

| Phase | 検証 |
|-------|------|
| 0 | `cat CLAUDE.md` で実体表示。Claude Code 再起動でプロジェクト指示が読まれる |
| 1 | `.claude/agents/` に7個。各エージェントにドメイン質問を投げ担当認識を確認 |
| 2 | export 済み vol で step_qa を実行 → 正常MP4で合格 / 壊れたMP4で差し戻し。`/api/runs` で台帳記録確認 |
| 3+ | 限定公開→承認→本公開のフローを1 vol で通し確認 |

---

## 5. 未決事項（別PCで判断・本書を更新）

- §2 の仮確定（粒度/命名/model/自律度/tools）を実運用で見直し。
- (B) のワーカーを「常駐スレッド」にするか「scheduler ジョブ」にするか（既存 render queue は常駐1 worker、scheduler は APScheduler 系）。既存方式に合わせるのが無難。
- analysis-worker の提案を music-worker が自動採択する範囲（P3-2）。完全自動は要慎重。

---

## 付録: よく使うコマンド
```bash
bash Python/start.sh                                   # 起動/再起動（reload無し）
lsof -ti:8888 | xargs kill -9                          # 停止
curl -s http://127.0.0.1:8888/openapi.json | python3 -c "import sys,json;print(len(json.load(sys.stdin)['paths']))"  # ルート数(正常220)
python3 -m py_compile Python/app.py                    # 構文チェック
python3 app_pipeline.py <vol> --dry-run                # パイプライン確認
```

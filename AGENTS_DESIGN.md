# Automation Studio — エージェント設計書（AGENTS_DESIGN）

> **状態**: ドラフト / レビュー待ち（2026-05-31 起案）
> **対象**: (A) Claude Code 開発サブエージェント + (B) アプリ内 運用自動化エージェント
> **関連**: [AGENTS.md](AGENTS.md)（運用コマンドガイド） / [SPEC.md](SPEC.md) / [skills/](skills/) / [docs/runbook.md](docs/runbook.md)

---

## 0. 目的と方針

「各機能ごとのエージェント」を、性質の異なる **2系統** で整備する。両系統で **同じ機能ドメイン分割を共通の軸**にし、開発面と運用面で一貫させる。

| 系統 | 何のため | どこに置く | コード改変 |
|------|---------|-----------|-----------|
| **(A) 開発サブエージェント** | 巨大な app.py(11,933行)+35モジュールを、機能別の専門AIに保守させる。バグ修正・機能追加を担当領域に絞って高速化 | `.claude/agents/*.md` | なし（定義のみ） |
| **(B) 運用自動化エージェント** | 6+チャンネルのノータッチ自走運用。各工程が自律実行・自己修復・能動トリガー | `Python/` 各モジュール | 既存基盤の上に追加実装 |

---

## 1. 機能ドメイン分割（共通の軸）

制作は10工程にステージ化済み（`app_pipeline.py` `STEPS`）。これを7ドメインに集約する。

| # | ドメイン | 担当工程 (STEPS) | 主なファイル | 専門技術 | 関連skill |
|---|---------|-----------------|------------|---------|-----------|
| 1 | **music** | suno, rename | suno_auto_create.py, flow_automation.py, app_process_tracks.py | Playwright, ffmpeg, Claude CLI | app-suno-download, app-rename-audio |
| 2 | **image** | bgimage, psd_composite, thumbnail | app_photoshop.py, codex_imagegen.py, app_midjourney.py, app_image_prompt.py, app_thumbnail_*.py, app_channel_thumbnail.py, app_benchmark_thumbnail.py | Photoshop UXP/CEP, 画像生成API, PSD合成 | app-bgimage, app-psd-composite, app-image-select |
| 3 | **video** | premiere, export | app_premiere.py, jsx_bundle.py, app_render_queue.py, `Script/*.jsx` | pymiere, ExtendScript/JSX, AME | app-premiere, app-export |
| 4 | **publish** | meta, upload | app_youtube.py, claude_proposer.py | YouTube Data API, OAuth, Claude CLI | app-youtube-upload, app-youtube-desc, app-multilingual-meta |
| 5 | **analysis** | (横断) | app_competitor.py, app_sheets.py, app_benchmark_*.py | pandas, Google Sheets, Claude CLI | app-competitor-spreadsheet, app-imitate-evolve, app-series-proposals |
| 6 | **pipeline** | qa + 全体統括 | app_pipeline.py, app_run_ledger.py, app_token_health.py | オーケストレーション・SQLite台帳・scheduler | app-workflow, app-schedule |
| 7 | **web** | (基盤) | app.py, web/static/index.html | FastAPI, Vanilla JS | app-web-dashboard, app-master-config |

> **粒度メモ**: 7ドメインは「技術スタックが完全に異なる」境界で切っている（Playwright / Photoshop / pymiere / Data API / pandas は相互に流用不可）。統合するなら music+image を「素材生成」、premiere+publish を「動画化・公開」にまとめて5ドメインも可（→ §6 要レビュー）。

---

## 2. (A) Claude Code 開発サブエージェント

### 2.1 目的・呼び出し

`.claude/agents/<name>.md` に定義。ユーザーが「YouTube周り直して」と言えば私(Claude Code)が `publish` エージェントへ委譲し、**そのドメインのファイル・勘所・関連skillだけを持った状態**で作業を開始する。巨大 app.py を毎回読み直す無駄を排除し、ドメイン固有の落とし穴（quota・reload無し等）を最初から踏まえる。

### 2.2 エージェント一覧（7個）

| name | 主タスク | tools(案) | model(案) | 必須の勘所 |
|------|---------|----------|-----------|-----------|
| `music` | SUNO生成/DL/リネーム/ffmpeg後処理のデバッグ | Read,Edit,Bash,Grep,Glob | sonnet | Playwright UI変化に脆い。`--batch`で多様性。cache miss時はコンテキスト再起動 |
| `image` | 背景画像・PSD合成・サムネ生成/スコアリング | Read,Edit,Bash,Grep,Glob | sonnet | provider=flow,codex並列。vol{N}.jpg優先。PSDレイヤ名は設定駆動 |
| `video` | Premiere自動配置・書き出し・JSX | Read,Edit,Bash,Grep,Glob | **opus** | JSXは**JSX Launcher拡張経由のみ**(AppleScript不可)。pymiere接続/CEPパネル前提。render queueでシリアライズ |
| `publish` | メタ生成・多言語化・アップロード・snippet更新 | Read,Edit,Bash,Grep,Glob | sonnet | quota(insert1600/update50)。OAuthは`--auth-only`。**reload無し→変更後start.sh再起動必須**。メタは`youtube_*.txt`が正 |
| `analysis` | 競合/ベンチマーク/パクリ進化/シリーズ提案 | Read,Edit,Bash,Grep,Glob | sonnet | 分析は日本語・出力メタは英語。Sheets経由でquotaゼロ。プロンプトは日本語固定 |
| `pipeline` | 工程統括・台帳・retry・auto_resume・scheduler | Read,Edit,Bash,Grep,Glob | **opus** | 新stage追加は`STEPS`/`STEP_LABELS`/`STEP_FUNCS`/`RETRY_POLICY`の**4箇所一貫更新**。sentinel exit(75-78)を壊さない |
| `web` | FastAPIルート・index.html・History API | Read,Edit,Bash,Grep,Glob | **opus** | app.py 11,933行/220ルート。reload無し。フロントはesc()でXSS防止。ファイル参照名の整合 |

### 2.3 定義の雛形（`publish` を完全例として）

```markdown
---
name: publish
description: >
  YouTube公開ドメイン専門。メタ生成・多言語化・アップロード・videos.update(snippet)の
  修正/デバッグ/機能追加に使う。「YouTube周り」「メタが反映されない」「多言語」「quota」等で起動。
tools: Read, Edit, Bash, Grep, Glob
model: sonnet
---
あなたは Automation Studio の YouTube 公開ドメイン専門エンジニア。

## 担当範囲
- Python/app_youtube.py（upload / videos.update / OAuth / load_localizations）
- Python/claude_proposer.py（タイトル/説明/タグ/多言語の生成プロンプト）
- app.py の /api/youtube/* と /api/videos/{name}/(meta-status|generate-localizations|mp4-info)

## 必ず踏まえる勘所
- quota: videos.insert=1600 / videos.update=50 unit（1日9600）。誤投稿修正はupdateで32倍安い
- OAuth再認証: `python3 app_youtube.py --auth-only`
- メタの正本は <vol_folder>/youtube_{title,description,tags}.txt。多言語は英語ソースから翻訳
- ⚠ uvicornはreload無し。app.py変更後は必ず `bash Python/start.sh` で再起動して反映を確認すること
- 分析情報は日本語、出力メタ(title/description/tags)は英語

## 関連skill（必要時に読む）
skills/app-youtube-upload.md, skills/app-youtube-desc.md, skills/app-multilingual-meta.md

## 作業後
変更がAPIに効くか、稼働サーバー(:8888)を再起動して openapi/実APIで確認してから報告する。
```

他6エージェントも同形式（担当範囲 / 勘所 / 関連skill / 作業後チェック）で作成する。

---

## 3. (B) アプリ内 運用自動化エージェント

### 3.1 既存基盤の棚卸し（★ここが起点）

**運用自動化の土台はほぼ完成している**（P1〜P3で構築済み）。新規実装は「自律性の上乗せ」に限定できる。

| 基盤 | 実装 | 役割 |
|------|------|------|
| ステージ化 | `app_pipeline.py` `STEPS`(10工程) + STEP_LABELS/FUNCS | 工程の標準化 |
| 中央台帳 | `app_run_ledger.py`（SQLite runs.db） | run_id/status/parent_run_id 履歴。stale(6h)降格 |
| retry/backoff | `RETRY_POLICY` + `_run_step_with_retry()` | exit 76=retryable で自動再試行 |
| auto_resume | `_job_auto_resume` + DateTrigger + `_NO_AUTO_RESUME_CODES` | 失敗工程から `--from` 再開 |
| 公開ゲート | `publish_video_to_public()` + publish_now job + 起動時復旧 | 限定公開→条件達成で本公開 |
| scheduler | per-channel view + `_balance_trigger_slot()`(30分分散) | Nチャンネル並列投入 |
| render queue | `app_render_queue.py`（SQLite + 1 worker） | premiere/export の物理シリアライズ |
| preflight | Premiere+CEP起動確認（exit 78） | 実行前ガード |
| token health | `app_token_health.py`（OAuth+cookie点検） | サイレント失敗の先回り（cron化は未完） |
| sentinel | exit 0/1/75/76/77/78 で全工程一貫 | 失敗種別の機械判定 |

> file:line はメモリ記録(28日前)由来のものを含む。実装時に現物確認する。STEPS と run_ledger スキーマは本起案時に grep 確認済み。

### 3.2 現行モデルの限界 → 提案モデル

- **現行**: 「1本のパイプラインを suno→…→upload と順次実行、失敗したら auto_resume」。中央集権・直列。
- **提案**: 各ドメインを **自律ワーカー** に再整理。台帳を共有黒板(blackboard)として、**「次に進められる工程」を各ワーカーが自分で拾う**。オーケストレーターは進行判断と衝突調整に専念。

```
┌─ オーケストレーター（app_pipeline.py を拡張：進行判断・スロット調整） ─────┐
│   中央台帳(runs.db) を黒板として共有                                       │
│                                                                          │
│   music-worker   : suno → rename            （素材が無い vol を拾って生成）│
│   image-worker   : bgimage → psd → thumbnail（音源確定後に起動）          │
│   video-worker   : premiere → export        （render queue でシリアライズ）│
│   qa-worker      : qa（ffprobe検証）★P3-3   （export完了を検知して自動QA）│
│   publish-worker : meta → 多言語 → upload   （QA通過＋公開ゲートで投稿）   │
│   analysis-worker: 競合分析を定期更新        （提案を各workerへ供給）★横断 │
│                                                                          │
│   各worker共通I/F: ①台帳に状態記録 ②exit code準拠でretry/通知           │
│                    ③人手要時のみ Discord/LINE 通知（runbook参照付き）     │
└──────────────────────────────────────────────────────────┘
```

### 3.3 共通ワーカーインターフェース（新規の標準）

```
class StageWorker:
    domain: str                      # music / image / video / qa / publish
    stages: list[str]                # 担当STEPS
    def can_run(vol, ledger) -> bool # 前提工程が done か（依存解決）
    def run(vol) -> ExitCode         # 既存 step_* を呼ぶ。sentinel exit準拠
    def on_fail(code) -> Action      # retry / auto_resume / notify を code で分岐
    def record(ledger)               # start_run/finish_run で台帳更新
```

- 既存の `step_suno()` 等はそのまま `run()` の中身に再利用（**ロジックは作り直さない**）。
- 新規価値は **依存解決(`can_run`)** と **能動トリガー**（「素材が無いvolを自分で見つけて着手」）。

### 3.4 自律性の強化点（未着手P3との接続）

| 強化 | 内容 | 既存との関係 |
|------|------|------------|
| **step_qa** | export後にffprobeで解像度/尺/コーデック/音声を自動検証、NGならvideo-workerへ差し戻し | P3-3（未着手）をqa-workerとして実装 |
| **plan自動採択** | analysis-workerの提案をmusic-workerが自動で取り込み次volを起案 | P3-2（未着手） |
| **policy-aware配分** | チャンネル優先度・quota残でスロット再配分 | P3-4（P2-4の延長） |
| **token health cron** | OAuth/cookie期限を定期点検し事前通知 | P3-5（モジュール有・cron化未完） |

---

## 4. Phase 0: CLAUDE.md 修復（要対応）

現状 `CLAUDE.md` は **壊れたシンボリックリンク**（`unreadable symlink`）。Claude Code はプロジェクト指示として CLAUDE.md を読むため、**今プロジェクト固有の指示が読み込まれていない**。対応案:
- (a) `CLAUDE.md` を AGENTS.md への正しい相対シンボリックリンクに張り直す、または
- (b) CLAUDE.md を実体化し「詳細は AGENTS.md / SPEC.md / skills/ 参照」+ サブエージェント運用方針を記載。

→ **(b)推奨**（リンク切れ再発を避け、(A)エージェントの起動方針もここに書ける）。

---

## 5. ロードマップ

| Phase | 内容 | 規模 | 依存 |
|-------|------|------|------|
| **0** | CLAUDE.md 修復 | 5分 | — |
| **1** | (A) サブエージェント7個を `.claude/agents/` 作成 | 低・即効 | §2確定 |
| **2** | (B) qa-worker 試作（P3-3 step_qa）= 既存基盤への最小追加で効果検証 | 中 | §3確定 |
| **3** | (B) publish-worker を公開ゲートと接続（QA通過→自動公開） | 中 | Phase2 |
| **4** | (B) 全ワーカー展開 + オーケストレーター拡張（依存解決・能動トリガー） | 大・段階 | Phase2-3 |
| **5** | plan自動採択 / policy-aware配分 / token health cron（P3残） | 大 | Phase4 |

---

## 6. 要レビュー事項（実装前に決めたい）

1. **ドメイン粒度**: 7分割で確定か / 5分割（music+image=素材生成、video+publish=動画化公開）に統合か。
2. **サブエージェント命名**: `music`/`video` 等の短名か、衝突回避で `as-music` 等の接頭辞付きか。
3. **model割当**: video/pipeline/web=opus, 他=sonnet の案で良いか（コストと精度のバランス）。
4. **(B)の自律度**: どこまで無人化するか。特に **upload を完全自動**にするか、人間承認ゲートを残すか。
5. **tools制限**: 開発エージェントに `Bash` のサーバー再起動を許すか（許すと検証まで自走、リスクは実行系コマンド）。
6. **(B)着手順**: qa-worker(Phase2)から始める案で良いか / 別ドメイン優先か。

---

## 次のステップ

本設計書をレビューのうえ、§6を確定 → Phase 0+1（CLAUDE.md修復＋サブエージェント7個作成）から着手するのが最小リスク・最大即効。

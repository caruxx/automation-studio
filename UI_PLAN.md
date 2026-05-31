# UI 一括改修 計画書（UI_PLAN）

> **状態**: ドラフト / レビュー待ち（2026-06-01 起案。general-purpose サブエージェントで全機能を機械的に棚卸し済み）
> **背景**: Phase 0-5 のバックエンドは実装済みだが、Phase 4/5 で作った「自走運用」機能の **UI が未整備**。本書はその一括改修の計画。
> **関連**: [AGENTS_DESIGN.md](AGENTS_DESIGN.md) §7-9 / [AGENTS_WORKPLAN.md](AGENTS_WORKPLAN.md) / [project memory: orchestrator integration policy]
> **対象**: `web/static/index.html`（13,786行・Vanilla JS SPA）/ `Python/app.py`（11,935行・**248 API ルート**）

---

## 0. 方針

- ユーザー指示: **「既存WEBの全機能を確認し計画を立てる。UIの変更案・現行からの改善提案も」**。
- バックエンド先行は完了。残るは UI 一括改修のみ（Phase 4 app.py orchestrator 統合＝無人稼働 だけが別途要GO）。
- **既存UIは壊さず追加・改善**。単一13,786行ファイルなのでタブ単位で隔離して進める。
- 数値・構造は調査で機械的に確定（grep/Python集計）。動的URL箇所のオーファン判定のみ「推定」。

---

## 1. 現行UIの全体像（棚卸し結果）

単一 SPA。サイドバー `go(name)`（L3594）が `<div class="page" id="p-<name>">` を表示切替。
**サイドバー7項目＋go()直叩きの隠れページ4＋デッドページ1 = 計12ページ。**

| 画面 | id | サイドバー | 役割 | 関連API（代表） |
|------|-----|:--:|------|------|
| ダッシュボード | p-dashboard | ○ | 全体ハブ・KPI・1クリック駆動 | /api/pipeline/create-from-benchmark, /api/runs/active, /api/render-queue |
| コンテンツ | p-videos | ○ | 動画制作の中核（2ペイン+7段ステッパー+下部5タブ） | /api/videos*（40）, /api/channel-thumbnail/*, /api/series/* |
| ベンチマーク | p-benchmark | ○ | 競合分析・取り込み | /api/analysis/*, /api/benchmark/* |
| 自動化 | p-automation | ○ | チャンネル別 自動実行既定値 | /api/automation/config (GET/PUT) |
| 書き出し | p-export | ○ | AME 書き出し管理 | /api/export/*, /api/ame/queue |
| 基本設定 | p-settings | ○(footer) | 一般設定（**公開方式 publish_mode 実装済**） | /api/config*, /api/channels*, /api/credentials/* |
| 詳細設定 | p-master | ○(footer) | 高度設定・**スケジュール**・リモート | /api/master, /api/prompts, /api/schedule/jobs |
| SUNO | p-suno | ×(go) | SUNO 内部実行 | /api/suno/* |
| Premiere | p-premiere | ×(go) | Premiere 配置 | /api/premiere/* |
| Photoshop | p-photoshop | ×(go) | PSD サムネ合成 | /api/photoshop/*（一部のみ） |
| YouTube | p-youtube | ×(go) | 説明文/アップロード | /api/youtube*, /api/youtube-desc/* |
| 通知 | p-notify | ×(到達経路なし) | 通知設定 | — （**実質デッドpage**） |

API 248ルート中、UI から参照されるのは 158種。主要グループ: /api/videos(40) /benchmark(22) /analysis(16) /export(14) /youtube(13) /photoshop(13) /config(8) /premiere(8) /runs(7) /schedule(6) /bgimage(6) /series(6) …

---

## 2. 機能ギャップ（バックエンド完成・UIゼロ）★本改修の主眼

調査で **「APIまで在るのにUIが無い」** 機能が確定。ここが投資対効果の最大点。

| 機能 | バックエンド | app.py統合 | UI | 必要対応 |
|------|:--:|:--:|:--:|---------|
| **runs.db 台帳** | ✅ app_run_ledger | ✅ **`/api/runs/ledger*` 7ルート完備** | **0件** | 台帳閲覧UIを足すだけ（API追加不要）|
| **token health** | ✅ app_token_health | ✅ **`/api/token-health` 2ルート + scheduler `_job_token_health` 配線済** | **0件** | 信号機UIを足すだけ（API追加不要）|
| **orchestrator**（dispatch/tick/breaker/policy/plan worker） | ✅ app_orchestrator | **✗ import 0・API 0** | **0件** | API統合（要GO）→ UI |
| publish_mode | ✅ | ✅ | ✅ 実装済(L1801-1805) | 対応不要 |
| render queue | ✅ | ✅ | ✅ ダッシュボードカード | 対応不要 |

**核心**: runs台帳 と token health は **「API完成・配線済み・UIだけゼロ」**＝可視化レイヤを足すだけで即価値。orchestrator だけ API 統合（無人稼働＝要GO）が前段に必要。

---

## 3. 改修ロードマップ（優先度順・調査所見反映）

### U1. 🔴 runs.db 台帳ビュー新設（最優先・投資対効果トップ）
`/api/runs/ledger*` 7ルートが完成済みでUIゼロ。現状バラバラな「稼働状況(runs/active)・パイプライン進行(modal)・履歴(無し)」を一本化。
- 全チャンネル×vol×stage の実行履歴テーブル（status色分け: done/in_progress/failed/cancelled）
- 失敗 run の drill-down（exit code 75/76/77/78 表示 + 既存 auto_resume で再開ボタン）
- run チェーン表示（`/api/runs/ledger/{id}/chain`、親子=auto_resume 追跡）
- 台帳統計（`/api/runs/ledger/stats`）

### U2. 🔴 トークン/接続ヘルスの常時可視化
`/api/token-health` 配線済み・UIゼロ。⚠**実環境で既に expired 検出済み**（Playwright cookie 等）。
- トップバーに健全性インジケータ（緑/黄/赤信号機）
- 期限切れ前バナー（OAuth/cookie 失効を事前検知。無言のアップロード失敗を撲滅）
- per-channel トークン期限一覧

### U3. 🟡 自走運用コントロールパネル（orchestrator・要GO前段）
Phase 4/5 の集大成。**前提: app.py への orchestrator 統合（tick の APScheduler 登録）＝無人稼働開始＝要GO**。
- autopilot ON/OFF トグル（per-channel）+ tick対象 stage（AUTOPILOT_DEFAULT_STAGES）
- ワーカー稼働ボード（`/api/workers/status` 新規・dry_run評価の可視化）
- サーキットブレーカー状態（連続失敗trippedなch表示 + 手動リセット）
- quota残ゲージ + channel優先度スライダー（policy配分が参照する channels.json priority）
- **段階運用案**: 読み取りビュー（status/quota）を先に出し、ONトグルは GO 後に有効化。

### U4. 🟡 「コンテンツ」ページの導線整理
過密（7段ステッパー+7フィルタchip+下部5タブ+右パネル+シリーズdetails+一括バー+動画詳細が1画面 L1405-1727）。progressive disclosure で「対象選択→生成→承認」を段階表示に。

### U5. 🟢 ナビゲーション統一 + デッドコード整理
- SUNO/Premiere/Photoshop/YouTube をサイドバー正式掲載 or サブメニュー化
- `p-notify` デッドページの撤去判断
- 基本設定/詳細設定の二分（命名/ch/APIキー/スケジュール/テーマが散在）を再編
- **オーファンAPI 30件**の死活整理（旧 benchmark/config、photoshop 低レベル8件、finder/browse 3件、/api/notify/line 等）

### U6. 🟢 エラー/予約投稿UX強化 + ファイル分割（中長期）
- 失敗時リトライ導線・原因表示の標準化（現状 raw text 赤帯のみ）
- publish_mode=delayed と `/api/youtube/schedule-publish` の明示連動UI（予約一覧/キャンセル）
- index.html 13,786行のタブ単位分割

---

## 4. 既存UIの問題点（調査所見・改善対象）

1. **ページ階層が二重で発見性が低い**: サイドバー7だが実体12ページ。SUNO/Premiere/Photoshop/YouTube は go() 直叩きのみ。p-notify は到達経路なし＝デッド。
2. **「コンテンツ」が過密**: 1画面に7要素同居。初見の操作導線が不明瞭。
3. **状態可視化が3分裂**: runs/active・Render Queue・パイプライン modal に割れ、台帳APIがあるのに履歴ビュー無し。
4. **エラー表示が貧弱**: catch で raw text 赤帯のみ。リトライ/原因切り分け無し。
5. **トークン失効が無言**: token-health があるのにUI通知無し。失敗で初めて気付く。
6. **publish_mode 予約が片肺**: UIに delayed あり・API もあるが直接連動が不透明。
7. **設定が2画面に分断**: 何がどちらにあるか覚えにくい。
8. **オーファンAPI 30件**が技術的負債化。

---

## 5. 必要な新規API（U3 のみ。U1/U2 は既存APIで足りる）

| API | 用途 | リスク | 対応UI |
|-----|------|--------|--------|
| `GET /api/workers/status` | tick の dry_run 評価結果（候補/稼働/ブレーカー） | 低（dry_run） | U3 |
| `POST /api/workers/autopilot` | autopilot ON/OFF（per-channel設定キー） | **中（ONで無人稼働）** | U3 |
| `GET /api/quota/status` | per-channel quota残（app_youtube台帳） | 低 | U3 |
| `PUT /api/config/priority` | channel優先度（policy配分が参照） | 低 | U3 |

> U1（台帳）と U2（token health）は **新規API不要**（`/api/runs/ledger*`・`/api/token-health` が既に在る）。だから最優先かつ低コスト。

---

## 6. 推奨着手順

1. **U1（台帳ビュー）** — 新規APIゼロ・リスクゼロ・効果最大。最優先。
2. **U2（ヘルス信号機）** — 新規APIゼロ・読み取りのみ。expired が既に出ているので即価値。
3. **U3（自走パネル）** — 読み取りビュー（status/quota）先行 → ONトグルは app.py orchestrator 統合 GO と同期。
4. U4（導線整理）→ U5（ナビ/デッドコード）→ U6（エラー/分割）は中長期。

---

## 7. 実装前の確認事項（要レビュー）

1. 着手の重さ: U1+U2 を一気にやるか、U1→U2 と段階リリースか。
2. 台帳ビューの置き場: 新タブ "history" 新設か、既存 dashboard へ統合か。
3. U3 の ON トグル有効化タイミング = app.py orchestrator 統合（無人稼働開始）の GO と同期でよいか。
4. デッドpage(p-notify)・オーファンAPI30件は「撤去」か「保留」か。

> 調査の生成物は調査エージェントの `/private/tmp/uiscan/`（routes.txt=248ルート / api_strings.txt=158種 / orphan_out.txt / pages_out.txt）に保存（一時・再現可能）。本書のタブ行番号は調査時点の実測。

# UI 一括改修 計画書（UI_PLAN）

> **状態**: ドラフト / レビュー待ち（2026-06-01 起案）
> **背景**: Phase 0-5 のバックエンド（サブエージェント / orchestrator / token health / publish_mode / 台帳）は実装済みだが、Phase 4/5 で作った「自走運用」機能の **UI が未整備**。本書はその一括改修の計画。
> **関連**: [AGENTS_DESIGN.md](AGENTS_DESIGN.md) §7-9 / [AGENTS_WORKPLAN.md](AGENTS_WORKPLAN.md)
> **対象**: `web/static/index.html`（約13,778行・Vanilla JS）/ `Python/app.py`（約232ルート）

---

## 0. 方針

- ユーザー指示: **「既存WEBの全機能を確認し計画を立てる。UIの変更案・現行からの改善提案も」**。
- バックエンド先行は完了。残るは UI 一括改修のみ（Phase 4 app.py 統合＝無人稼働 だけが別途要GO）。
- **既存UIは壊さず追加・改善**。13,778行の単一ファイルなので、改修はタブ単位で隔離して進める。

---

## 1. 現行UIの全体像（棚卸し結果）

単一ページ + 上部タブ切替（`switchView(name)` が `data-view` セクションを表示切替）。

| タブ | data-view | 役割 | UI接続 |
|------|-----------|------|--------|
| ダッシュボード | dashboard | 全体状況・KPI・進捗 | ✅ |
| 動画制作 | production | vol単位の工程実行・一括パイプライン | ✅ |
| 楽曲 | music (推定) | SUNO生成/DL/後処理 | ✅ |
| 背景画像 | bgimage | 背景画像生成 | ✅ |
| サムネイル | thumbnail | サムネ生成/スコア/承認 | ✅ |
| メタデータ | meta (推定) | タイトル/説明/タグ/多言語 | ✅ |
| アップロード | upload | 投稿/予約/履歴/一括 | ✅ |
| 競合分析 | analysis | ベンチマーク/ホットch/シリーズ提案 | ✅ |
| 設定 | settings | 全設定（公開方式 publish_mode 実装済） | ✅ |

> ⚠ 一部タブ id（music/meta）は調査時点で**推定**。実装着手前に `data-view` 定義（index.html 700-900行付近）と `switchView` を現物確認すること。API 件数（約232ルート）も grep 概算。

API は `/api/videos`(~35) `/api/youtube`(~20) `/api/config`(~15) `/api/premiere` `/api/bgimage` `/api/analysis` `/api/benchmark` `/api/series` `/api/runs` `/api/schedule` 等に分類。大半は UI 接続済み。

---

## 2. 機能ギャップ（Phase 4/5 で作ったが UI が無いもの）★本改修の主眼

| 機能 | バックエンド | API | UI | 必要対応 |
|------|------------|-----|-----|---------|
| **orchestrator**（StageWorker/dispatch/tick/breaker/policy） | `app_orchestrator.py` ✅ | **無** (app.py未統合) | **無** | API 統合（要GO）→ UI |
| **token health**（check_all/--cron） | `app_token_health.py` ✅ | **無** (CLIのみ) | **無** | 読み取り専用 API 追加 → 信号機UI |
| **runs.db 台帳** | `app_run_ledger.py` + `/api/runs/*` ✅ | ✅ あり | ⚠ 浅い | drill-down ビュー強化 |
| **publish_mode** | per-channel config ✅ | `/api/config/dashboard` ✅ | ✅ 実装済 | （対応不要） |
| **quota 残/channel優先度** | quota台帳 + policy配分 ✅ | ⚠ 部分 | **無** | quota API + 優先度設定UI |

**結論**: 自走運用（orchestrator/token health/quota）は**ほぼUI不在**。ここが改修の中心。

---

## 3. 改修ロードマップ（優先度順）

### U1. 🔴 自走運用コントロールパネル（最優先・新タブ "autopilot"）
Phase 4/5 の集大成を運用可能にする。
- **autopilot ON/OFF トグル**（per-channel）— 設定キー追加 + `/api/workers/autopilot`
- **ワーカー稼働ボード** — 各 vol×stage の走行/待機/失敗を一覧（`/api/workers/status`）
- **サーキットブレーカー状態** — 連続失敗でtrippedなチャンネル表示 + 手動リセット
- **前提**: app.py への orchestrator 統合（tick の APScheduler 登録）。⚠**無人稼働開始＝要GO**。UIだけ先に「読み取り専用ビュー」を作り、ONトグルは GO 後に有効化、も可。

### U2. 🔴 台帳ベース俯瞰ダッシュボード（既存 dashboard 強化）
`runs.db` を活かす。
- **マトリクス可視化** — 全チャンネル × 全vol × 全stage を色付きグリッド（done/in_progress/failed/未着手）
- **失敗 drill-down** — exit code（75/76/77/78）と再開ボタン（既存 auto_resume API）
- **run チェーン表示** — `/api/runs/chain/{id}`（既存だが UI 未接続の可能性）

### U3. 🟡 ヘルスバー（token health + quota）
- **トークン信号機** — check_all を叩く読み取り専用 API（`/api/token-health`、新規・低リスク）→ 緑/黄/赤。⚠**実環境で既に expired 検出済み**（Playwright cookie 等）。これを可視化すれば即運用価値あり。
- **quota 残ゲージ**（per-channel）+ **優先度スライダー**（channels.json の priority、policy配分が参照）

### U4. 🟡 1-vol ウィザード導線（中期）
分散した工程タブ（production/music/bgimage/thumbnail/meta/upload）を「1 vol を最初から最後まで」縦導線に再編。各 stage の done/要対応を一目で、次アクションを文脈表示。

### U5. 🟢 index.html 構造分割（中長期・任意）
13,778行をタブ単位で分割（部分テンプレート/ESM）。一括改修の地ならしになるが独立リスクもあるので段階的に。

---

## 4. 既存UIの改善提案（U1-U5 に含まれない細かい点）

1. **状態の可視性**: パイプラインがどの vol のどの工程で止まっているか俯瞰しづらい → U2 で解消。
2. **エラー表示**: toast 中心（推定）で永続ログ/再試行導線が弱い → 失敗を台帳ビューに集約。
3. **token/quota が CLI でしか見えない** → U3。
4. **タブ過多**（9タブ）で 1vol 仕上げ導線が横断的 → U4。
5. **巨大単一ファイル**の保守リスク → U5。

---

## 5. 必要な新規API（UI改修に伴うバックエンド追加）

| API | 用途 | リスク | 対応UI |
|-----|------|--------|--------|
| `GET /api/token-health` | check_all をJSONで返す | 低（読み取り専用） | U3 |
| `GET /api/workers/status` | tick の評価結果（候補/稼働） | 低（dry_run） | U1 |
| `POST /api/workers/autopilot` | autopilot ON/OFF（per-channel設定） | 中（ONで無人稼働） | U1 |
| `GET /api/quota/status` | per-channel quota残 | 低 | U3 |
| `PUT /api/config/priority` | channel優先度 | 低 | U3 |

> `/api/workers/*` の autopilot ON は **無人稼働の引き金**。U1 の「ONトグル有効化」は app.py 統合 GO と同じゲート。

---

## 6. 推奨着手順

1. **U3（ヘルスバー）から** — 読み取り専用APIのみで無人化リスクゼロ。token expired が既に出ているので即価値。
2. **U2（台帳俯瞰）** — 既存 `/api/runs/*` 活用、リスク低。
3. **U1（自走パネル）** — 読み取りビュー先行 → ON トグルは app.py 統合 GO 後。
4. U4 / U5 は中長期。

---

## 7. 実装前の確認事項

1. 改修の重さ: U1-U3 を一気にやるか、U3→U2→U1 と段階リリースか。
2. 新タブ新設（autopilot）か、既存 dashboard への統合か。
3. U5（ファイル分割）を今やるか、後回しか。
4. app.py orchestrator 統合（無人稼働）のタイミング — U1 の ON トグルと同期。

> ⚠ 本書のタブ id・API 件数は調査時点の**推定を含む**。実装着手時に現物（switchView / @app デコレータ）を /tmp 経由で再確認すること。

# 自己評価付き反復画像生成ループ 実装計画（IMAGE_EVAL_LOOP_PLAN）

> **状態**: 計画確定 / 実装前（2026-06-01 起案）
> **要望**: 生成画像をベンチマーク先と比較してスコア化（遜色ない/次にベンチが出しそう/視聴者目線の優位点）し、合格ラインまで複数枚生成→評価→再生成を繰り返し、合格画像を採用。+ 弱点を次プロンプトに還流するフィードバック。完全自動（パイプライン組込）。
> **方針確定**: ループ+フィードバック還流 / 完全自動（ただし安全弁=env既定OFF・max_attempts・コスト上限 必須）

## 0. 最重要事実（監査 task w1upcfp68 で判明）

**評価・合格判定・複数生成・候補管理は既に実装済み。欠けているのは「不合格なら自動再生成する外側ループ」だけ。**

| 機能 | 既存資産 | 状態 |
|------|---------|------|
| ベンチ比較スコア | `app_thumbnail_scoring.score_thumbnails()`（Claude Vision 100点: concept_fit30/trend_fit20/competitor_diff25/past_perf25 + ctr_predict + similarity + strengths/weaknesses）| ✅ 再利用 |
| 合格判定 | `judge_auto_approval(mode="conditional", score_threshold=80, ctr_threshold=7.0, similarity_max=25)` | ✅ 再利用 |
| 複数生成 | codex `--n`/`--max-parallel`, Flow `--count xN`, `build_5element_prompts`(N件バリエーション) | ✅ 再利用 |
| 候補管理/履歴 | `app_thumbnail_state`（thumbnail_state.json: score/status/run_history）| ✅ 再利用 |
| **ループ雛形** | `step_plan`+`_score_plan_via_claude`（テキストplan版の「生成→採点→閾値未満なら再生成」完成形）| ✅ 移植元 |
| **不合格→自動再生成ループ** | **無し** | ★新規 |

> ⚠ similarity は機械的画像類似度でなく Vision の「競合固有要素コピー度（低いほど良い=パクってない）」。similarity_max=25 は上限ガード。「遜色なさ」は score_total/ctr_predict が測る。

## 1. 設計判断

| 論点 | 結論 |
|------|------|
| ループ実装 | 新規ピュア関数 `run_eval_loop()` を新規 `Python/app_image_eval_loop.py` に。生成だけ `generate_fn` コールバックで注入（UI=async Popen / pipeline=同期 _run の二重実装回避）|
| 背景画像 | 第1版は対象外（サムネ評価軸と目的が逆=背景は目立たない方が良い。別関数 run_bg_eval_loop が要る。段階S4）|
| 既定 | パイプライン側 既定OFF(`APP_THUMB_EVAL_LOOP=0`、MEMORY手動トリガ整合)。UI側は既存auto_score延長で max_attempts=1=現行同一、多周回は明示オプトイン |

## 2. ループ本体（run_eval_loop in app_image_eval_loop.py）

1周回: コスト上限ガード → scan_start_index 前進 → build_5element_prompts(concept_hint/avoid更新) → generate_fn → 0枚/infra_error なら generation_failure で即break(無限ループ防止) → pending upsert → score_thumbnails(vision_calls++) → judge_auto_approval(**conditional固定**) → state反映 → status_counts で auto_approved>0 を自前判定(judgeは全件needs_reviewでも例外投げない) → 合格(required_pass達成)でbreak / 不合格でフィードバック還流して次attempt。

終了後: best_of(不合格でも最高1枚)。合格0でmax到達→best を needs_review のまま温存(自動adoptしない=手動承認余地)。append_run はループ全体で1回(run_history肥大防止)。

戻り: {passed, attempts_used, total_generated, vision_calls, approved_files, best, abort_reason("" / generation_failure / cost_cap / stopped), all_evaluations}。

## 3. フィードバック還流（3経路）

| source | sink(build_5element_prompts引数) | 物理反映 |
|--------|------|---------|
| weaknesses(不合格) | concept_hint に ` Improve on:` 追記(base起点・累積膨張防止) | Subject行 |
| 類似度超過/コピー系 | thumbnail_axis["avoid"] | Avoid行 |
| best.strengths | concept_hint に ` Keep:` 追記 | Subject行 |
| なし(バリエーション継続) | start_index前進 | Lighting/Camera/Style巡回 |

## 4. 安全弁（env、既定値）

`APP_THUMB_EVAL_LOOP=0`(OFF既定) / `APP_THUMB_MAX_ATTEMPTS=2` / `APP_THUMB_N_PER_ATTEMPT=4` / `APP_THUMB_SCORE_THRESHOLD=80` / `APP_THUMB_CTR_THRESHOLD=7.0` / `APP_THUMB_SIMILARITY_MAX=25` / `APP_THUMB_REQUIRED_PASS=1` / `APP_THUMB_MAX_TOTAL_GEN=8` / `APP_THUMB_MAX_VISION_CALLS=3` / `APP_THUMB_EVAL_ON_FAIL_ADOPT=0`。
⚠ 閾値の新定義源を増やさない: run_eval_loop 既定引数は `app_thumbnail_state.DEFAULT_*` を import 参照。env は呼び出し層に限定。

中断/保護: 0枚生成(クォータ)=generation_failure即break(不合格と峻別) / 手動adopted・rejectedは upsert前ガード(rescore L8083流用) / コスト二重上限 / should_stop(meta.stop_requested)。

## 5. 収束リスク対策

非冪等スコア振動→初合格で即break+確定auto_approvedを守るヒステリシス / 合格0でmax到達→best温存(EVAL_ON_FAIL_ADOPT=1時のみ昇格) / 閾値厳しすぎ→env化+best が threshold-5以上ならwarnログ(自動緩和しない) / 生成基盤エラー誤再試行→generation_failure峻別 / Vision採点失敗連鎖→best更新せず次へ・max_vision_callsで頭打ち。

## 6. 検証

py_compile(新ファイルに `from __future__ import annotations` 必須・Python3.9.6) / ループ単体ドライ(generate_fn・score_thumbnails をスタブ化し5分岐: 1周合格/2周合格/max不合格best温存/infra_error/cost_cap を CLI無しで確認) / フィードバック注入assert(concept_hint・avoid 反映) / **回帰(最重要): APP_THUMB_EVAL_LOOP未設定でstep_thumbnail出力が従来と完全一致)** / 実1ch スポット(クォータ要)。

## 7. 段階実装（各段階ロールバック可能）

- **S0 安全弁先行**: app_image_eval_loop.py を判定・上限・中断ロジックだけ実装(generate_fn/score_fn注入IFのみ・配線ゼロ)。py_compile+ドライ。ロールバック=ファイル削除。
- **S1 ループ本体(UIから)**: app.py _run_channel_thumbnail の単発採点ブロックを run_eval_loop に置換。既定 max_attempts=1=現行同一、多周回は Request 新フィールドでオプトイン。
- **S2 フィードバック還流**: 3経路実装。no-op化で「ただのバリエーション再生成」に縮退可。
- **S3 パイプライン組込**: step_thumbnail に APP_THUMB_EVAL_LOOP 分岐(既定OFF)。STEPS等4箇所更新は新ステージ追加でないため非該当(既存thumbnailステージ内分岐)。env OFF既定で本番影響ゼロ。
- **S4 背景拡張(任意)**: 背景専用ループ run_bg_eval_loop(評価軸を「ループ背景適性」に読替)。サムネ版安定後。

## Critical Files
- 新規 `Python/app_image_eval_loop.py`（核）
- `Python/app.py`（_run_channel_thumbnail 7363-7772・ChannelThumbnailStartRequest 7143 拡張・rescore ガード8083流用）
- `Python/app_pipeline.py`（step_thumbnail 1682-1818 env分岐・step_plan 432 雛形参照・_run 225流用）
- `Python/app_image_prompt.py`（build_5element_prompts 216 へフィードバック注入）
- `Python/app_thumbnail_scoring.py`（score/judge/best_of/status_counts 呼ぶ・conditional固定）
- 参照のみ無改変: app_thumbnail_state.py / app_channel_thumbnail.py

> 監査: workflow task w1upcfp68（scoring/vision/multigen 3エージェント）。実装計画: Plan エージェント。

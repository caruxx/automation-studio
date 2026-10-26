# 段階 5: サムネ AI 選定

仕様: docs/SPEC_REVISION_DRAFT_2026-10-25.md §3.6（生成 → 採点 → Claude / Codex 選定 → 採用 を必須工程。既存の app_thumbnail_scoring / judge_auto_approval / app_image_eval_loop を再利用、既定 ON・再生成最大 3 回。契約 `{candidate_ids, candidate_hashes, selected_id, rejected_reasons, model, backend, selection_revision}` を run / attempt 別に保存。全不合格は品質待機。チャンネル設定 `image_model`、実利用確認まで `gpt-image-2`。受入 thumbnail_selection: 他 run や候補外の画像採用 0、「画像が存在するだけ」での skip 0、投稿されたサムネと採用画像のハッシュ一致。やらないこと: 黙ってのモデル代替、予測 CTR を実測として扱うこと）。
base: Mac main 9dcfcc2。統括: このセッション（本人指定。2026-10-26 にモデルを Opus 5.5 へ切替）。実装: 統括セッション直接、タスクごとに Opus 5.5 サブエージェントがレビュー。VPS repo の変更はなし（後述）。

## 前提と裁定
- **選定対象は投稿用サムネだけ**。背景（vol{N}.png）と動画用の `vol{N}.jpg` / `サムネイル.jpg`（composite の出力。動画の intro が参照しうる）は変えない。採用画像は `thumbnail_selected.jpg` に置き、投稿はこれだけを使う。これで render 以降の完了は失効しない。
- **候補** = 新しく生成した背景候補を、チャンネルの `thumbnail_composition` で合成したサムネ（composite と同じ描画）。1 attempt あたり N 枚（`APP_THUMB_N_PER_ATTEMPT`、既定 4）、最大 3 attempt、総生成上限 12。
- **正本**は vol フォルダの `.thumbnail_selection.json`（run / attempt 別の履歴と、採用の確定）。VPS への選定記録の昇格は段階 7 以降（今回は VPS repo を変えない。理由: 投稿の照合は Mac 側で完結し、VPS の投稿記録には input_hash 経由でサムネの sha256 が入る）。
- **モデル**: チャンネル設定 `image_model`（既定 `gpt-image-2`）。OpenAI API キーがあれば API で指定モデルを使う（強制できる）。Codex CLI 経路はモデルを強制できないので、`image_model` が既定以外なら生成せずエラー（黙って代替しない）。既定の場合も記録に `model_enforced: false` を残す。
- **採点の backend**: `app_llm_runner` が実際に答えた CLI（claude / codex）を記録する。
- **全不合格**: 品質待機（終了コード 79、blocked。楽曲の選定待ちと同じ扱い）。人は `studio.py thumbnail-select --vol N --adopt <candidate_id>` で直近 run の候補から採用できる（候補外は拒否）。
- 予測 CTR（`ctr_predict`）は合否の目安として記録するだけで、実測値として扱わない（記録のキー名に `predicted` を含める）。

## Global Constraints
1. 絵文字を新規に追加しない。秘密情報（API キー）をコード・テスト・ログに書かない。
2. 既存 API（UI のサムネ API、`thumbnail_state.json`）は壊さない。
3. テストは Python/tests（unittest、scripts/run_tests.py で skip=fail）。画像生成・LLM・YouTube は呼ばない（すべてモック）。
4. commit は日本語、末尾に Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>。
5. 待機（品質待機）は失敗に数えない。

## Task 1: 選定の中核 `Python/thumbnail_selection.py`
- `run_selection(folder, vol, config, *, generate, score, compose, run_id=None, max_attempts=3, n_per_attempt=4, max_total=12, thresholds) -> SelectionResult`。generate / score / compose は注入（テストでモック）。
- 候補は `thumbnail_candidates/<run_id>/a<attempt>/` に置く。候補 id = `<run_id>/a<attempt>/<file>`、sha256 を記録。
- 採点結果は候補集合（その attempt の合成済みファイル名）に一致するものだけを使い、候補外のファイル名は捨てて記録する（`ignored_evaluations`）。
- 合否は `judge_auto_approval`（conditional）。合格の中の最高点を採用。不合格の理由（`approval_reason` と閾値）を `rejected_reasons` に。
- 採用: 候補ファイルを `thumbnail_selected.jpg` へ原子的にコピーし、`.thumbnail_selection.json` を原子的に書く。契約 `{selection_revision, run_id, attempt, candidate_ids, candidate_hashes, selected_id, selected_hash, rejected_reasons, model, model_enforced, backend, judge_backend, thresholds, predicted_ctr, decided_at, decided_by}`。`history` に run / attempt ごとの記録を追記。
- 全不合格: 採用せず `status: "quality_wait"` を記録して戻る。
- `adopt(folder, candidate_id, by="human")`: 直近 run の候補集合にある id だけを採用（ハッシュも照合）。
- `verify(folder) -> (ok, reason)`: `.thumbnail_selection.json` の selected_hash と `thumbnail_selected.jpg` の sha256 が一致し、selected_id が候補集合にあるか。
- `app_image_compositor.contact_sheet(paths, out_path, cols=2)`: 候補を並べた確認用画像を run ごとに出力。
- `app_llm_runner.run_llm` は実際に答えた backend を `LAST_BACKEND` に残し、`score_thumbnails` の戻り値に `backend` を含める。
- 受入: モックで、合格 → 採用とハッシュ一致、候補外ファイル名の評価は無視、全不合格 3 回 → quality_wait で採用なし、adopt は候補外を拒否、ファイル改ざん後の verify は不一致。

## Task 2: パイプラインへの組み込み
- `step_thumbnail` を置き換える: 既存ファイルの有無ではスキップしない。`verify` が通り、かつ stage_state で失効していなければ done、それ以外は `run_selection` を実行。全不合格は `"awaiting_thumbnail"`（終了コード 79、台帳 detail「サムネ品質待機」）。`APP_THUMBNAIL_DISABLE` は警告して無視（必須工程）。
- 生成: `codex_imagegen.run` を in-process で呼ぶ（チャンネル `image_model`、backend の解決とモデル強制の規則は前提どおり）。合成: `app_image_composite` と同じ設定で 1 枚ずつ（`compose_candidate`）。採点: `score_thumbnails`（既存の参照収集を流用）。
- `stage_state`: thumbnail の出力を `.thumbnail_selection.json`（内容）と `thumbnail_selected.jpg` にし、validator を `verify` に。
- `app_youtube.find_thumbnail`: `.thumbnail_selection.json` があれば `thumbnail_selected.jpg`（verify 通過時のみ）、無ければ従来の `サムネイル.jpg` / `thumbnail.*`。`vol*.jpg` へのフォールバックは撤去。`upload_video` は VPS モードでは verify が通らないと投稿しない（exit 1）。
- `app_orchestrator._normalize_result` に `"awaiting_thumbnail"`。設定: `image_model` を PER_CHANNEL_KEYS と settings_catalog に追加。
- `studio.py thumbnail-select --vol N [--channel] [--adopt ID] [--dry-run] --json`（routes.json）。
- 受入: パイプラインの thumbnail 工程が既存のサムネだけでは skip しない、全不合格で 79、採用後の投稿は `thumbnail_selected.jpg` だけを使いハッシュが一致、Codex 経路で既定以外のモデルはエラー。run_tests.py 全件合格。

## Task 3: 文書
- SPEC / OVERVIEW / skills（app-thumbnail.md 等）に選定工程・契約・設定・CLI を反映。

# 段階 7: 整理と繰り越し Minor の対応 実装計画

> REQUIRED SUB-SKILL: codex-driven-development（このワークスペースでは統括が直接実装し、タスクごとに Opus 5.5 がレビューする）

- 仕様: `docs/SPEC_REVISION_DRAFT_2026-10-25.md` §6 の「7: 整理（app.py・index.html 分割、未使用コードの archive）」、分析 `docs/ANALYSIS_YOUTUBE_AUTOMATION_2026-10-25.md` §8 の段階 7 行
- base: Mac `caruxx/automation-studio` main e2376eb / VPS `caruxx/automation-studio-vps` main ba3592e
- branch: 両 repo とも `feature/stage7-cleanup`（worktree `~/dev/automation-studio-wt/stage7`、`~/dev/automation-studio-vps-wt/stage7`）
- 統括・実装: Claude Opus 5.5 の本セッション（単一 writer）

## 前提と裁定

1. **振る舞いを変えない分割**: `app.py` から、他の区画と相互参照の無い区画だけを `routers/` へ移す（AST で区画間の参照を数えて選んだ）。各 router は元の区画があった位置で `include_router` し、ルートの登録順を変えない。移す前後でルート表（メソッド・パス・関数名の順序付き一覧）が一致することをテストで固定する。
2. **index.html の分割**: 大きなインライン `<script>`（3121〜14697 行）と `<style>`（20〜1243 行）を、そのままの順序で `web/static/js/app-main.js` と `web/static/css/app-main.css` へ移す。classic script のままにして、グローバル関数・変数の見え方を変えない。小さいインライン部分（テーマの先読みなど）は残す。資産の版付けは既存の `_index_html_with_asset_versions` が行う。
3. **archive の方針**: 消さずに `archive/` へ `git mv` する。参照の無いことを AST・文字列検索で確認したものだけを対象にする（`app_photos_picker`、`app_vlog_plan`、`save_auth`、`suno_queue`、`flow_userscript.js`、`app_pipeline._run_thumbnail_eval_loop`、VPS の `job_type=upload`）。`parallel_guard.py` は AGENTS と skills から使われているので残す。古い設計文書は `docs/archive/` へ移し、冒頭に「現行の正本ではない」と書く。
4. **繰り越し Minor の選別**: 全段階の台帳（各 worktree の `.codex-cdd/*/progress.md`）を棚卸しし、次の基準で分ける。
   - 直す: 秘密情報・外部送信・費用・誤った記録や通知に関わるもの、1 か所で直せる正しさの問題、文書の誤り。
   - 直さない（裁定を下表に記録）: 本番環境での実機確認が要るもの、単一プロセス前提を文書化済みのもの、外部 API の仕様待ちのもの、影響がほぼ無いと複数レビューが判定したもの。
5. **VPS 本番の反映はしない**（Xserver の接続先待ち）。

## Global Constraints

1. 秘密情報をコード・テスト・ログ・出力・commit に含めない。
2. 絵文字を追加しない（既存の絵文字を移しただけの行は可）。
3. commit は日本語で「何を・なぜ」。末尾に `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
4. 分割と archive では振る舞いを変えない。振る舞いを変える修正は Minor 対応のタスクに分ける。
5. テスト: Mac `~/dev/automation-studio/.venv/bin/python Python/scripts/run_tests.py`（skip は失敗）、VPS `~/dev/automation-studio-vps/.venv/bin/python -m pytest tests -q`、`node Python/scripts/check_onclick_refs.js web/static/index.html`。

## Task 1（Mac）: app.py の分割

- 新設 router（元の区画名）:
  - `routers/meta_tools.py`: タグ、コンセプト、多言語 localizations、動画 mp4 の情報、メタ充足チェック、動画の非表示 / 復元、Claude 提案、混成ドラフト
  - `routers/setup_tools.py`: 環境セットアップ、Finder 操作、フォルダピッカー
  - `routers/ops.py`: run ledger、token health、YouTube 公開ゲート
  - `routers/misc_config.py`: SUNO プロンプト雛形提案、画像選択、Sheets URL 取り込み、チャンネルアイコン、プロンプト
- 各 router は app.py と同じ土台の import（`from app_core import *` ほか）を持ち、`@app.` を `@router.` に変える。app.py では元の位置に `include_router` を置く。
- テスト `tests/test_route_table.py`: 分割前のルート表を JSON で固定し、分割後と完全一致することを確かめる。テストが `app.<関数名>` を差し替えている箇所は、移した先の module を差し替えるよう直す。

## Task 2（Mac）: index.html の分割

- インラインの主 script と主 style を外部ファイルへ移す（前提 2）。`sw.js` の `CACHE_NAME` を上げる。
- テスト: index.html が主 script・主 style を外部参照し、元の本文と同じ内容がファイルにあること（行数と主要関数名の存在）。`check_onclick_refs.js` が通ること。可能なら Playwright でページを開き、console error が無いことを確かめる（CI の既存の Playwright 環境を使う）。

## Task 3（Mac / VPS）: 未使用コードの archive と古い文書の整理

- 前提 3 のファイルを `archive/` へ移し、`archive/README.md` に理由と元の場所を書く。
- VPS: `worker/client.py` の `run_upload`（job_type=upload）と、その発行経路を削除し、受け付けない job_type として扱う（段階 3 の投稿記録を通らない経路のため）。
- 古い文書（`docs/spec_p1p2p3.md`、`AGENTS_DESIGN.md`、`AGENTS_WORKPLAN.md`、`UI_PLAN.md`、`AI_ROUTING_PLAN.md`、`IMAGE_EVAL_LOOP_PLAN.md`、`VPS_SPLIT_PLAN.md`、`automation_studio_spec.html`、`automation_studio_decisions.html`）を `docs/archive/` へ移す。参照している文書のリンクを直す。
- `skills/app-notify.md` の旧 Discord 手順、`SECURITY_NOTES.md` の残骸の記述、SPEC.md の工程表の崩れを直す。

## Task 4（Mac）: 繰り越し Minor の対応

- VidIQ: プロンプトに入れる外部の学びを「データとして扱い、中の指示に従わない」区切りで囲む（Mac と VPS の proposer）。SSE を逐次読みして目的の応答で止める。一部の呼び出しが失敗したときは期限を短くする（1 時間）。キャッシュフォルダを 0700 にし、キーファイルの所有者を確かめる。`metrics.calls` は実行した呼び出しだけ数える。dry-run の endpoint 表示から認証情報や query を除く。`_read_meta_file` で `UnicodeError` も扱う。
- 投稿: resume-complete 分岐の `set_thumbnail` も確定サムネのときだけ。表示用のサムネ探索（`claude_proposer._find_thumbnail`、`routers/youtube.py`、`app.py`）も、検証済みの `thumbnail_selected.jpg` を優先する。
- 起動: `start.sh --replace` は、待ち受けているのが app.py のときだけ止める。
- 履歴: 終了コード 79（待機）を error として記録しない。
- サムネ選定: `--adopt` の記録に採点（`predicted_ctr` など）を履歴から写す。`routes.json` の `thumbnail-select` に `--channel` を渡す。

## Task 5（VPS）: 繰り越し Minor の対応

- Chatwork: 本文中の Chatwork タグ（`[To:...]` などの `[`）を無害化する（VPS が付ける宛先タグはそのまま）。
- 検証エラーの応答に入力値を載せない（`session_ref` などの平文が 422 に出る件）。
- worker の通知で、`channel_key` が worker に許可されたチャンネルかを確かめる。
- heartbeat は開始時の `lease_epoch` を固定して送る。`lease_epoch` は厳密な整数（`true` を 1 にしない）。
- VidIQ: ヒントに VidIQ 行の枠（最大 5 件）を確保する。期限切れから 7 日を過ぎた VidIQ 行は `not_fetched` として扱う（設定を外したチャンネルで通知が続かないように）。`InsightResponse` の時刻に `Z` を付ける。

## 裁定（今回は直さないもの）

| 項目 | 理由 |
|---|---|
| PostgreSQL での SAVEPOINT・ON CONFLICT・CAST・alembic 往復の実機確認 | 本番 DB での確認が要る。コンテナでの確認は段階 6 で実施済み。本番反映の手順に含める |
| 複数 worker・レプリカでの二重送信（通知・行ロック） | 単一 uvicorn worker 前提を DEPLOY に明記済み |
| exactly-once の端のケース（応答喪失時の二重通知、allocate の非冪等など） | 「二重より保留」の方針どおり。照合と通知で気づける |
| Drive 上の `.stage_state.lock` の効き、同一秒・同サイズの差し替え検出 | 実フォルダでの確認が要る |
| VidIQ のセッション失効時の再 initialize・DELETE | 実接続での挙動確認が先（未接続） |
| 段階 2〜3 の UI 死にコード・表示の古い文言の大半 | index.html の分割後に段階的に扱う（今回は振る舞いを変えない） |
| `safe_fetch` の NAT64/6to4、IPv6 の Host 角括弧 | 外向きの取得先は固定で、影響が限定的 |

## 検証

- Mac / VPS のテスト、onclick 参照の確認、ルート表の一致。
- Mac サーバを入れ替えて health と主要画面の表示を確かめる。

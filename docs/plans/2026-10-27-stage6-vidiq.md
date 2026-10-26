# 段階 6: VidIQ 連携 実装計画

> REQUIRED SUB-SKILL: codex-driven-development（このワークスペースでは統括が直接実装し、タスクごとに Opus 5.5 がレビューする）

- 仕様: `docs/SPEC_REVISION_DRAFT_2026-10-25.md` §3.7
- base: Mac `caruxx/automation-studio` main 4ec8008 / VPS `caruxx/automation-studio-vps` main 7f6906a
- branch: 両 repo とも `feature/stage6-vidiq`（worktree `~/dev/automation-studio-wt/stage6`、`~/dev/automation-studio-vps-wt/stage6`）
- 統括・実装: Claude Opus 5.5 の本セッション（単一 writer）

## 前提と裁定

1. **VidIQ MCP の提供形態（2026-10-27 に公開文書で確認）**: 遠隔 MCP サーバ `https://mcp.vidiq.com/mcp`。認証は OAuth か読み取り専用の API キー（app.vidiq.com のアカウント設定で発行）。呼び出しはクレジットを消費する（調査系はおおむね 1 回 5 クレジット）。ツール名の公開は一部のみ（`keyword_research`、`outliers` など）。API キーを送るヘッダ名は文書に無い。
2. **やらないこと（仕様どおり）**: OAuth の実装、無人の定期取得、費用の推定。取得は `studio.py insights-refresh` を人か Claude / Codex が明示的に実行したときだけ行う。定期ジョブは作らない。
3. **認証情報**: API キーは環境変数 `VIDIQ_MCP_API_KEY` か、repo 外のファイル `~/.config/automation-studio/vidiq.json`（`{"api_key": ...}`、権限 0600 を要求）から読む。値はログ・JSON 出力・例外文に出さない。送り方は MCP の標準どおり `Authorization: Bearer`（未検証。実接続で確かめる）。
4. **ツールの呼び方**: ツール名と引数を推測で固定しない。`tools/list` の結果に存在し、`inputSchema.required` を満たせる呼び出しだけを行う。チャンネル設定 `vidiq_requests`（`[{tool, arguments}]`）を優先し、無ければ `vidiq_keywords` の各語を、`tools/list` にある `keyword_research` の唯一の必須文字列引数に入れて呼ぶ。どちらも無ければ未設定（`not_configured`）。1 回の更新で呼ぶ上限は既定 3（`--max-calls` で変更）。
5. **根拠**: evidence_id は実際に受け取ったツール結果から作る（`vidiq:<tool>:<結果本文の sha256 先頭 16 桁>`）。動画 ID は結果に現れたものだけを抽出する。結果の原文は Mac のローカルキャッシュにだけ保存し、VPS には正規化した要点だけを送る。
6. **鮮度**: `expires_at = fetched_at + 24h`。期限内なら `insights-refresh` は再取得しない（`--force` で再取得）。期限切れの VidIQ 行はメタ生成のヒントから外す。
7. **保存先**: VPS の `insights` テーブル（`source='vidiq'`）が正本。VPS 連携が無効な Mac では、同じ形のローカルキャッシュをヒントに使う。
8. **劣化（`vidiq_degraded`）**: VidIQ が未設定・期限切れ・取得失敗のときもメタ生成と投稿は止めない。メタに `insights_degraded=true` と `degraded_reason` を記録する。info 通知は「チャンネルで VidIQ を設定済みなのに使えない」ときだけ出す（未設定のチャンネルで毎回通知しない）。VPS 連携時は VPS がメタ受領時に通知を積み、Mac は VPS 無効時だけ自分で通知する（二重通知しない）。
9. **メタの契約**: `{title, description, tags, evidence_ids, insights_degraded, degraded_reason}`。`evidence_ids` は「プロンプトに渡した VidIQ の根拠」の id（LLM が実際に使ったかは判定しない）。Mac は `youtube_meta.json` に保存する。このファイルは stage_state の出力に含めない（取得時刻の違いで後段を失効させない）。

## Global Constraints

1. 秘密情報（VidIQ の API キー、VPS のトークン）をコード・テスト・ログ・JSON 出力・commit に含めない。
2. 絵文字を追加しない。
3. commit は日本語で「何を・なぜ」。末尾に `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
4. VidIQ が使えなくても meta・upload は従来どおり完走する（例外を外へ出さない）。
5. 外部ライブラリを増やさない（MCP クライアントは標準ライブラリの urllib で書く）。
6. テストは実ネットワークに出ない（transport を差し替える）。

## Task 1: VPS — insights の VidIQ 取り込みとメタの契約（repo: automation-studio-vps）

- alembic 0020: `insights.source` の CHECK に `vidiq` を追加。`fetched_at`、`expires_at`（nullable DateTime）列を追加。ダウングレードも書く。
- `app/services/insights.py`: ヒントの対象から `expires_at < now` の行を外す。`vidiq_status(db, channel_id, now)` → `{fresh, fetched_at, expires_at, evidence_ids}`。
- `app/api/worker.py`:
  - `POST /api/worker/insights/vidiq`: `{channel_id, fetched_at, ttl_hours(1..72, 既定 24), items[1..30]: {category, insight(<=300), video_ids[], metrics{}, evidence_ids[1..]}}`。チャンネルの権限確認は既存の insights と同じ。同じチャンネルの既存 `source='vidiq'` 行を消して入れ替える（1 トランザクション）。evidence_ids が空の item は 422。
  - `GET /api/worker/vols/{id}`: `insights_vidiq: {fresh, fetched_at, expires_at, evidence_ids}` を追加。
  - `VolMeta`: `evidence_ids: list[str] = []`、`insights_degraded: bool = False`、`degraded_reason: str | None = None` を任意項目で追加（旧クライアント互換）。
  - `report_vol_meta`: `insights_degraded` かつ理由が `not_configured` 以外なら info 通知を積む（event_id は `vidiq-degraded:<vol_id>:<理由>`）。
- `app/api/insights.py`: 一覧の source 型に `vidiq` を足す（手動作成では選べないまま）。
- `worker/client.py` `run_meta`: `insights_vidiq` を見て、メタに `evidence_ids` / `insights_degraded` / `degraded_reason` を入れて送る。
- テスト: 取り込みの入れ替え、権限、evidence 空の拒否、期限切れのヒント除外、vol 応答の `insights_vidiq`、メタの新項目と互換、劣化時の通知 1 件、worker のメタ送信。

## Task 2: Mac — vidiq_adapter と insights-refresh（repo: automation-studio）

- 新設 `Python/vidiq_adapter.py`:
  - `McpHttpClient(endpoint, token, *, timeout=30, transport=None)`: JSON-RPC 2.0 を POST。`initialize` → `notifications/initialized` → `tools/list` / `tools/call`。応答が `application/json` でも `text/event-stream` でも読む。`Mcp-Session-Id` を引き継ぐ。
  - `load_api_key()`: 環境変数か 0600 のファイル。無ければ None。
  - `plan_calls(tools, channel_cfg, max_calls)`: 前提 4 の規則で呼び出しを決める。満たせない設定は理由付きで飛ばす。
  - `normalize(tool, result)`: 前提 5 の規則で `{category, insight, video_ids, metrics, evidence_ids}` を作る。
  - `refresh(channel_key, channel_cfg, *, client_factory, now, max_calls, force, cache_dir)` → `{ok, source:"vidiq", fetched_at, expires_at, items, video_ids, evidence_ids, calls, degraded_reason}`。キャッシュが期限内なら呼ばない。
- `Python/studio.py`: `insights-refresh --channel KEY [--force] [--max-calls N] [--dry-run] --json`。ローカルキャッシュに保存し、VPS 連携時は Task 1 の取り込み API に送る。終了コード: 0 取得済み・期限内、1 取得失敗（劣化の理由を出力）、2 使い方。`routes.json` に intent を追加（via_api_safe false）。
- `Python/vps_ledger_sync.py`: `push_vidiq_insights(channel_key, payload)`。
- 設定: `app_core.PER_CHANNEL_KEYS` と `settings_catalog.json` に `vidiq_keywords`、`vidiq_requests` を追加。
- テスト: MCP の往復（JSON / SSE、セッション ID）、キーが無いとき、ツールが無いとき、必須引数を満たせないとき、正規化の evidence と動画 ID、期限内の再取得なし、CLI の終了コードと出力にキーが出ないこと。

## Task 3: Mac — メタ生成への注入と劣化の記録

- `claude_proposer.py`: `propose_titles` / `propose_description` / `propose_tags` が `insights_hint` を受け取り、ベンチマーク節の後ろに足す（マスタープロンプトの書式は変えない）。
- `app_pipeline.step_meta`: VPS 連携時は vol 応答の `insights_hint` と `insights_vidiq`、無効時はローカルキャッシュ（期限内だけ）からヒントと evidence を作る。`youtube_meta.json` に契約どおり保存し、`push_meta` で新項目も送る。劣化の通知は前提 8 のとおり。
- テスト: 新鮮な VidIQ がプロンプトに入り evidence が記録される、期限切れ・未設定で劣化として完走する、通知の出し分け、VPS 生成メタ採用経路が壊れていない。

## Task 4: 文書

- SPEC.md（meta 行・CLI）、OVERVIEW、AGENTS（「VidIQ で分析して」）、skills に `app-vidiq.md` を新設（設定、API キーの置き場所、費用の注意、劣化の扱い、未検証事項）。

## 検証

- Mac: `~/dev/automation-studio/.venv/bin/python Python/scripts/run_tests.py`（skip は失敗扱い）
- VPS: `~/dev/automation-studio-vps/.venv/bin/python -m pytest tests -q`
- 実接続（VidIQ の実 API キー、実クレジット消費）は本人のキー発行後に別途。未確認として報告する。

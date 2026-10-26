# 段階 3a: Chatwork 通知（outbox）

仕様: docs/SPEC_REVISION_DRAFT_2026-10-25.md §3.5（契約 `{event_id, run_id, kind, room_id, body, attempt, next_retry_at, sent_at}`、kind は info / action_required / alert、[To] は action_required と alert だけ。受入 notify_recovery: 429 と再起動後の再送、重複抑制、通知障害でも制作が継続、結果不明の重複可能性を記録。やらないこと: Chatwork の返信による選定、通知障害を理由にした再投稿、VPS の SMTP 変更）。
base: Mac main 11157ab / VPS main 3970d43。統括: Claude Fable 5.1（本人指定・包括許可）。実装: 統括セッション直接、タスクごとに Opus 5.5 サブエージェントがレビュー。本番反映は保留中（段階 3 と同様）。

## 設計の要点
- **配送は VPS だけ**が行う（Chatwork API トークンは VPS の環境変数にのみ置く。Mac はトークンを持たない）。VPS DB の `notify_outbox` が正本。
- **Mac は VPS に event を送る**（worker トークン、`POST /api/worker/notify`）。VPS 未設定 / 到達不能のときはローカル SQLite（`~/.config/automation-studio/notify_queue.sqlite`）に退避し、`studio.py notify-flush` で同じ event_id のまま VPS へ移管する（自動昇格はしない）。
- **Discord は廃止**: `app_notify.sh` は Webhook を叩かず `notify.py` の CLI を呼ぶ薄い互換ラッパーにする。`_notify_discord` / `_send_discord_notify` / `notify_duplicate_skip` は `notify.notify()` に委譲（呼び出し元 25 か所は変えない）。discord_config.json の設定 UI の残骸は段階 7。
- kind の決め方: 呼び出し元が明示しない場合は本文の先頭記号で推定（❌ / ⛔ / 「失敗」「打ち切り」→ alert、⚠ / 「ログイン」「手動」「確認してください」→ action_required、それ以外 info）。[To] は VPS 側で action_required / alert にだけ付ける（`CHATWORK_TO_ACCOUNT_IDS`）。
- 配送: tick（`NOTIFY_TICK_SECONDS` 既定 60）で `next_retry_at <= now` の未送信を attempt 順に送る。失敗は指数バックオフ（1, 5, 15, 60 分、上限 6 時間、`NOTIFY_MAX_ATTEMPTS` 既定 20 を超えたら dead）。429 は Retry-After を尊重。送信したが応答を失った（タイムアウト等）ときは `possible_duplicate=True` を記録して再送する（通知は二重より欠落を避ける。投稿と逆の方針で、理由を文書に書く）。
- 重複抑制: `event_id` unique。Mac 側は送信ごとに uuid を付け、ローカル退避からの移管でも同じ id を使う。VPS 側の `notify_event`（publish_verify 等）は `event_id` を指定して 1 回だけ積む。
- 通知障害で制作を止めない: Mac の `notify()` は例外を投げない（失敗はローカル退避か stderr）。VPS の `notify_event` も例外を外に出さない。

## Global Constraints
1. 絵文字を新規に追加しない（既存メッセージの記号はそのまま）。Chatwork トークン・room_id・account_id をコード・テスト・ログ・応答に書かない。
2. 既存 API は追加のみ。alembic はダウングレード可能。
3. テストは Mac unittest（run_tests.py、skip=fail）/ VPS pytest。Chatwork API・VPS 実機・Discord を呼ばない（すべてモック）。
4. commit は日本語、末尾に Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>。
5. VPS の SMTP（mailer）は変更しない。

## Task 1（VPS）: outbox と配送
- model `NotifyOutbox`（event_id unique、run_id|null、vol_id|null、kind、room_id|null、body Text、attempt、next_retry_at、sent_at|null、last_error|null、possible_duplicate bool、source、created_at、updated_at）。alembic `20261026_0019_notify_outbox`。
- `app/services/notify.py`: `notify_event(kind, body, *, vol=None, run_id=None, event_id=None, db=None)` は outbox に積む（db が無ければ SessionLocal、event_id 重複は無視）。従来の vol.step_states["_notices"] も維持。`enqueue(db, events)`、`deliver_pending(db, *, now, sender, limit)`、`chatwork_sender(token, room_id, to_ids)`（httpx POST `/v2/rooms/{room}/messages`、`X-ChatWorkToken`、本文は `[To:id]` を kind に応じて付け、`[info][title]..[/title]..[/info]` で整形）。`backoff(attempt)`、429 の Retry-After。
- scheduler: `notify_deliver_tick`（`NOTIFY_ENABLED` 既定 1、`NOTIFY_TICK_SECONDS` 既定 60）。トークン未設定なら送らず警告だけ（outbox は溜まる）。
- API: worker `POST /api/worker/notify` `{events:[{event_id, kind, body, run_id?, vol?, channel_key?}]}` → `{accepted:[event_id], duplicate:[event_id]}`。管理者 `GET /api/notify/outbox?state=pending|sent|dead&limit=` と `POST /api/notify/outbox/{id}/retry`。
- 受入: 同 event_id の二重 enqueue が 1 行、429 で next_retry_at が Retry-After 後、失敗がバックオフで再送、再起動（新しい tick）で未送信が送られる、結果不明で possible_duplicate が記録されて再送、トークン未設定で例外なし。既存テスト全件合格。

## Task 2（Mac）: notify.py と Discord の置換
- `Python/notify.py`: `notify(body, *, kind=None, run_id=None, vol=None, event_id=None) -> dict`（VPS → 失敗時ローカル退避。例外を投げない）、`infer_kind(body)`、`flush_local(limit)`、CLI（`python3 notify.py "本文" [--kind]`、`--flush`、`--status`）。ローカル退避は SQLite（events: event_id, kind, body, run_id, vol, channel_key, created_at, migrated_at）。
- `vps_ledger_sync.post_notify(events)`（`_request_strict`、未設定なら None）。
- `app_notify.sh`: Webhook を廃止し `python3 notify.py "$1"` を呼ぶ。`app_pipeline._notify_discord` / `app.py _send_discord_notify` / `app_reconcile.notify_duplicate_skip` は `notify.notify()` を呼ぶ（error_humanizer の整形は維持）。
- `studio.py notify-flush`（intent、routes.json）。
- 受入: VPS 到達不能でも notify() が例外を出さず SQLite に残る → flush で同じ event_id で VPS に届く → 2 回目の flush は duplicate 扱いで消える。call site の grep で `webhook_url` を使う送信経路が無い。run_tests.py 全件合格。

## Task 3（両 repo）: 文書
- SPEC / OVERVIEW / skills の Discord 記述を Chatwork（VPS outbox）に更新、VPS DEPLOY に環境変数（`CHATWORK_API_TOKEN` / `CHATWORK_ROOM_ID` / `CHATWORK_TO_ACCOUNT_IDS` / `NOTIFY_*`）と alembic 0019 を追記。

# app-notify: 通知（Chatwork、VPS outbox 経由）

> 実装: Mac `Python/notify.py`（送信と退避）/ VPS `app/services/notify.py`（outbox と Chatwork 配送）
> 段階 3a（2026-10-26）で Discord Webhook は廃止した。

## 流れ

1. Mac の `notify.py` が event（`event_id`, `kind`, `body`, `vol`, `run_id`, `channel_key`）を VPS の `POST /api/worker/notify` に送る。
2. VPS は outbox（`notify_outbox`）に積み、Chatwork へ配送する。Chatwork のトークンとルームは VPS の環境変数だけに置く。
3. VPS が未設定、または届かないときは、Mac の `~/.config/automation-studio/notify_queue.sqlite` に退避する。自動では移管しない。

```bash
python3 Python/studio.py notify-flush --json
```

退避した event を、同じ `event_id` のまま VPS へ移管する。

## kind

| kind | 用途 | Chatwork の [To] |
|---|---|---|
| `info` | 完了・劣化の知らせ（VidIQ が使えなかった等） | 付けない |
| `action_required` | 人の操作待ち（品質待機など） | 付ける |
| `alert` | 失敗・異常 | 付ける |

省略すると本文から推定する（待機の終了コード 79 や「失敗 0 件」は alert にしない）。

## CLI

```bash
python3 Python/notify.py "本文" --kind info --vol 12 --json
```

`--flush` で退避分の移管、`--status` でローカルの件数を表示する。

## 互換の入口

- `Python/app_notify.sh <本文>` と `POST /api/notify/discord`・`/api/notify/line` は、名前だけ旧来のまま残した互換の入口で、中身は `notify.py` を呼ぶ。Discord には送らない。
- 画面の「Discord Webhook 設定」は使われていない（保存しても送信先にならない）。画面の整理は別途行う。

## 関連

- VPS の配送停止（429 時の一時停止など）と運用の注意は VPS repo の `DEPLOY.md`。

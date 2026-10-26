#!/usr/bin/env python3
"""通知（段階 3a）。Mac は Chatwork を直接呼ばず、VPS の outbox に event を送る。

- VPS に届かない / 未設定のときはローカル SQLite（~/.config/automation-studio/notify_queue.sqlite）に退避し、
  `python3 notify.py --flush`（studio.py notify-flush）で同じ event_id のまま VPS へ移管する（自動昇格はしない）。
- どの経路でも例外を外に出さない（通知障害で制作を止めない）。
- kind は info / action_required / alert。省略時は本文から推定する。
CLI: python3 notify.py "本文" [--kind alert] [--vol N] [--run-id ID] / --flush / --status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

KINDS = ("info", "action_required", "alert")
_ALERT_RE = re.compile(r"^\s*(❌|⛔|\[ALERT\])|失敗|打ち切り|停止中|見つかりません|二重|error", re.I)
_ACTION_RE = re.compile(r"^\s*(⚠|\[ACTION\])|ログイン|手動|確認してください|人が|要対応|quota|クオータ|awaiting", re.I)


def queue_path() -> Path:
    return Path(os.environ.get("APP_NOTIFY_QUEUE") or "~/.config/automation-studio/notify_queue.sqlite").expanduser()


def infer_kind(body: str) -> str:
    text = body or ""
    if _ALERT_RE.search(text):
        return "alert"
    if _ACTION_RE.search(text):
        return "action_required"
    return "info"


def new_event_id() -> str:
    return "mac-" + uuid.uuid4().hex


def _connect() -> sqlite3.Connection:
    p = queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    conn.execute("""CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, run_id INTEGER, vol INTEGER,
        channel_key TEXT, created_at TEXT NOT NULL, migrated_at TEXT, last_error TEXT)""")
    return conn


def _store_local(event: dict[str, Any], error: str) -> None:
    conn = _connect()
    try:
        conn.execute("INSERT OR IGNORE INTO events(event_id, kind, body, run_id, vol, channel_key, created_at, last_error) VALUES (?,?,?,?,?,?,?,?)",
                     (event["event_id"], event["kind"], event["body"], event.get("run_id"), event.get("vol"), event.get("channel_key"),
                      datetime.now(timezone.utc).isoformat(timespec="seconds"), error[:500]))
        conn.commit()
    finally:
        conn.close()


def _channel_key() -> Optional[str]:
    try:
        import vps_ledger_sync
        return vps_ledger_sync.resolve_channel_key()
    except Exception:
        return None


def _send_to_vps(events: list[dict[str, Any]]) -> dict[str, Any]:
    """VPS の outbox に積む。未設定なら {"configured": False}。到達不能 / 4xx は例外。"""
    import vps_ledger_sync
    if not vps_ledger_sync.enabled():
        return {"configured": False}
    result = vps_ledger_sync.post_notify(events)
    return {"configured": True, **(result or {})}


def notify(body: str, *, kind: Optional[str] = None, run_id: Optional[int] = None, vol: Optional[int] = None,
           event_id: Optional[str] = None, channel_key: Optional[str] = None) -> dict[str, Any]:
    """1 件通知する。戻り値: {"event_id", "kind", "delivered_to": "vps"|"local"|"none", "error"?}。例外は投げない。"""
    kind = kind if kind in KINDS else infer_kind(body)
    event = {"event_id": (event_id or new_event_id())[:128], "kind": kind, "body": (body or "")[:4000],
             "run_id": run_id, "vol": vol, "channel_key": channel_key or _channel_key()}
    try:
        res = _send_to_vps([event])
        if res.get("configured") is False:
            _store_local(event, "vps_not_configured")
            return {**event, "delivered_to": "local", "error": "vps_not_configured"}
        return {**event, "delivered_to": "vps", "accepted": res.get("accepted"), "duplicate": res.get("duplicate")}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:500]
        try:
            _store_local(event, err)
            print(f"[notify] VPS に送れないためローカルに退避: {err}", file=sys.stderr)
            return {**event, "delivered_to": "local", "error": err}
        except Exception as exc2:
            print(f"[notify] 通知を記録できません（本文はログのみ）: {exc2}: {event['body'][:200]}", file=sys.stderr)
            return {**event, "delivered_to": "none", "error": err}


def flush_local(limit: int = 200) -> dict[str, Any]:
    """ローカル退避分を同じ event_id で VPS に移管する（明示操作）。VPS 未設定なら何もしない。"""
    try:
        import vps_ledger_sync
        if not vps_ledger_sync.enabled():
            return {"ok": False, "error": "vps_not_configured", "migrated": 0}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "migrated": 0}
    conn = _connect()
    try:
        rows = conn.execute("SELECT event_id, kind, body, run_id, vol, channel_key FROM events WHERE migrated_at IS NULL ORDER BY created_at LIMIT ?",
                            (int(limit),)).fetchall()
        if not rows:
            return {"ok": True, "migrated": 0, "duplicate": 0, "remaining": 0}
        events = [{"event_id": r[0], "kind": r[1], "body": r[2], "run_id": r[3], "vol": r[4], "channel_key": r[5]} for r in rows]
        try:
            res = _send_to_vps(events)
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "migrated": 0, "remaining": len(rows)}
        done = set(res.get("accepted") or []) | set(res.get("duplicate") or [])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for eid in done:
            conn.execute("UPDATE events SET migrated_at=? WHERE event_id=?", (now, eid))
        conn.commit()
        remaining = conn.execute("SELECT COUNT(*) FROM events WHERE migrated_at IS NULL").fetchone()[0]
        return {"ok": True, "migrated": len(res.get("accepted") or []), "duplicate": len(res.get("duplicate") or []), "remaining": remaining}
    finally:
        conn.close()


def local_status() -> dict[str, Any]:
    p = queue_path()
    if not p.exists():
        return {"queue": str(p), "pending": 0, "migrated": 0}
    conn = _connect()
    try:
        pending = conn.execute("SELECT COUNT(*) FROM events WHERE migrated_at IS NULL").fetchone()[0]
        migrated = conn.execute("SELECT COUNT(*) FROM events WHERE migrated_at IS NOT NULL").fetchone()[0]
        return {"queue": str(p), "pending": pending, "migrated": migrated}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("body", nargs="?", help="通知本文")
    ap.add_argument("--kind", choices=KINDS)
    ap.add_argument("--vol", type=int)
    ap.add_argument("--run-id", type=int)
    ap.add_argument("--event-id")
    ap.add_argument("--flush", action="store_true", help="ローカル退避分を VPS へ移管")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.flush:
        out = flush_local()
    elif args.status:
        out = local_status()
    elif args.body:
        out = notify(args.body, kind=args.kind, run_id=args.run_id, vol=args.vol, event_id=args.event_id)
    else:
        ap.print_help()
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok", True) is not False else 1


if __name__ == "__main__":
    sys.exit(main())

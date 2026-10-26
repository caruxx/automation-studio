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
_NOT_ALERT_RE = re.compile(r"失敗ではありません|失敗\s*0\s*件|エラー\s*0\s*件|既存なし|=0\b")
_ALERT_RE = re.compile(r"^\s*(❌|⛔|\[ALERT\])|失敗|打ち切り|停止中|見つかりません|二重投稿(?!ガード)|エラー|例外|期限切れ|\berror\b|\bfailed\b", re.I)
_ACTION_RE = re.compile(r"^\s*(⚠|\[ACTION\]|\[待機\])|ログイン|手動|確認してください|要対応|quota|クオータ|awaiting|一時停止", re.I)
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
SEND_TIMEOUT = 5


def queue_path() -> Path:
    return Path(os.environ.get("APP_NOTIFY_QUEUE") or "~/.config/automation-studio/notify_queue.sqlite").expanduser()


def infer_kind(body: str) -> str:
    """本文から kind を推定する（呼び出し元が明示しないときの既定）。待機（79）や「失敗 0 件」は alert にしない。"""
    text = body or ""
    if _ACTION_RE.search(text) and not re.match(r"^\s*(❌|⛔)", text):
        return "action_required"
    cleaned = _NOT_ALERT_RE.sub("", text)
    if _ALERT_RE.search(cleaned):
        return "alert"
    return "info"


def validate_event(event: dict[str, Any]) -> Optional[str]:
    """VPS と同じ検証を送る前に行う。問題があれば理由を返す（退避してから拒否の印を付ける）。"""
    if not _EVENT_ID_RE.match(str(event.get("event_id") or "")):
        return "invalid_event_id"
    if not str(event.get("body") or "").strip():
        return "empty_body"
    for k in ("run_id", "vol"):
        v = event.get(k)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 1):
            return f"invalid_{k}"
    if event.get("kind") not in KINDS:
        return "invalid_kind"
    if event.get("channel_key") is not None and len(str(event["channel_key"])) > 64:
        return "invalid_channel_key"
    return None


def new_event_id() -> str:
    return "mac-" + uuid.uuid4().hex


_SCHEMA = """CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL, run_id INTEGER, vol INTEGER,
        channel_key TEXT, created_at TEXT NOT NULL, migrated_at TEXT, rejected_at TEXT, last_error TEXT)"""


def _looks_corrupt(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "not a database" in text or "malformed" in text or "corrupt" in text


def _connect() -> sqlite3.Connection:
    """ローカル退避 DB。壊れていたら .corrupt-<時刻> に退避して作り直す（以後の通知を失わない）。"""
    p = queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        try:
            conn = sqlite3.connect(str(p), timeout=5)
            conn.execute(_SCHEMA)
            try:
                conn.execute("ALTER TABLE events ADD COLUMN rejected_at TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
            return conn
        except sqlite3.OperationalError:
            raise  # ロック待ち（database is locked）等は破損ではない。退避 DB を動かさない
        except sqlite3.DatabaseError as exc:
            if attempt == 2 or not _looks_corrupt(exc):
                raise
            try:
                conn.close()
            except Exception:
                pass
            broken = p.with_name(p.name + f".corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}")
            p.rename(broken)
            print(f"[notify] 退避 DB が壊れていたため {broken.name} に移して作り直します: {exc}", file=sys.stderr)
    raise RuntimeError("unreachable")


def _store_local(event: dict[str, Any], error: str) -> None:
    conn = _connect()
    try:
        conn.execute("INSERT OR IGNORE INTO events(event_id, kind, body, run_id, vol, channel_key, created_at, last_error) VALUES (?,?,?,?,?,?,?,?)",
                     (event["event_id"], event["kind"], event["body"], event.get("run_id"), event.get("vol"), event.get("channel_key"),
                      datetime.now(timezone.utc).isoformat(timespec="seconds"), (error or "")[:500]))
        conn.commit()
    finally:
        conn.close()


def _note_error(event_ids: list[str], error: str) -> None:
    try:
        conn = _connect()
    except Exception:
        return
    try:
        for eid in event_ids:
            conn.execute("UPDATE events SET last_error=? WHERE event_id=? AND migrated_at IS NULL", (error[:500], eid))
        conn.commit()
    except Exception:
        pass  # 理由の記録に失敗しても notify() から例外を出さない
    finally:
        conn.close()


def _mark(event_ids: list[str], *, migrated: bool = False, rejected: Optional[str] = None) -> None:
    if not event_ids:
        return
    conn = _connect()
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for eid in event_ids:
            if migrated:
                conn.execute("UPDATE events SET migrated_at=?, last_error=NULL WHERE event_id=?", (now, eid))
            if rejected:
                conn.execute("UPDATE events SET rejected_at=?, last_error=? WHERE event_id=?", (now, rejected[:500], eid))
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
    problem = validate_event(event)
    # 先にローカルへ保存し、VPS に届いたら migrated の印を付ける（親プロセスの timeout で殺されても通知を失わない）
    stored = True
    try:
        _store_local(event, problem or "pending")
    except Exception as exc:
        stored = False
        print(f"[notify] 通知をローカルに記録できません: {exc}", file=sys.stderr)
    if problem:
        try:
            _mark([event["event_id"]], rejected=problem)
        except Exception:
            pass
        print(f"[notify] 通知の内容が不正のため送りません（{problem}）: {event['body'][:120]}", file=sys.stderr)
        return {**event, "delivered_to": "none", "error": problem}
    try:
        res = _send_to_vps([event])
        if res.get("configured") is False:
            return {**event, "delivered_to": "local" if stored else "none", "error": "vps_not_configured"}
        if stored:
            try:
                _mark([event["event_id"]], migrated=True)
            except Exception:
                pass
        return {**event, "delivered_to": "vps", "accepted": res.get("accepted"), "duplicate": res.get("duplicate")}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:500]
        if stored and getattr(exc, "status", None) == 422:
            _mark([event["event_id"]], rejected=err)  # VPS が内容を拒否: 再送しても直らないので対象から外す
            print(f"[notify] VPS が通知を拒否しました: {err}", file=sys.stderr)
            return {**event, "delivered_to": "none", "error": err}
        if stored:
            _note_error([event["event_id"]], err)
            print(f"[notify] VPS に送れないためローカルに退避: {err}", file=sys.stderr)
            return {**event, "delivered_to": "local", "error": err}
        print(f"[notify] 通知を記録できません（本文はログのみ）: {event['body'][:200]}", file=sys.stderr)
        return {**event, "delivered_to": "none", "error": err}


def flush_local(limit: int = 200) -> dict[str, Any]:
    """ローカル退避分を同じ event_id で VPS に移管する（明示操作）。VPS 未設定なら何もしない。"""
    try:
        import vps_ledger_sync
        if not vps_ledger_sync.enabled():
            return {"ok": False, "error": "vps_not_configured", "migrated": 0}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "migrated": 0}
    try:
        conn = _connect()
    except Exception as exc:
        return {"ok": False, "error": f"queue unreadable: {exc}"[:300], "migrated": 0}
    try:
        rows = conn.execute("SELECT event_id, kind, body, run_id, vol, channel_key FROM events WHERE migrated_at IS NULL AND rejected_at IS NULL ORDER BY created_at LIMIT ?",
                            (int(limit),)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"ok": True, "migrated": 0, "duplicate": 0, "rejected": 0, "remaining": 0}
    events = [{"event_id": r[0], "kind": r[1], "body": r[2], "run_id": r[3], "vol": r[4], "channel_key": r[5]} for r in rows]
    migrated = duplicate = rejected = 0
    # 送る前に検証し、不正な event は拒否の印を付けて対象から外す（1 件の不正で全体を止めない）
    valid = []
    for e in events:
        problem = validate_event(e)
        if problem:
            _mark([e["event_id"]], rejected=problem); rejected += 1
        else:
            valid.append(e)

    def _send_batch(batch: list[dict[str, Any]]) -> Optional[str]:
        nonlocal migrated, duplicate
        try:
            res = _send_to_vps(batch)
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:300]
            _note_error([e["event_id"] for e in batch], err)
            return err
        acc = list(res.get("accepted") or []); dup = list(res.get("duplicate") or [])
        _mark(acc + dup, migrated=True)
        migrated += len(acc); duplicate += len(dup)
        return None
    error = _send_batch(valid) if valid else None
    if error and error.startswith("LedgerHttpError: HTTP 422") and len(valid) > 1:
        # VPS が一括で拒否した: 1 件ずつ送り、拒否された event だけ除外する
        error = None
        for e in valid:
            err1 = _send_batch([e])
            if err1 and err1.startswith("LedgerHttpError: HTTP 422"):
                _mark([e["event_id"]], rejected=err1); rejected += 1
            elif err1:
                error = err1
                break
    conn = _connect()
    try:
        remaining = conn.execute("SELECT COUNT(*) FROM events WHERE migrated_at IS NULL AND rejected_at IS NULL").fetchone()[0]
    finally:
        conn.close()
    out = {"ok": error is None, "migrated": migrated, "duplicate": duplicate, "rejected": rejected, "remaining": remaining}
    if error:
        out["error"] = error
    return out


def local_status() -> dict[str, Any]:
    p = queue_path()
    if not p.exists():
        return {"queue": str(p), "pending": 0, "migrated": 0}
    try:
        conn = _connect()
    except Exception as exc:
        return {"queue": str(p), "pending": -1, "migrated": -1, "error": str(exc)[:200]}
    try:
        pending = conn.execute("SELECT COUNT(*) FROM events WHERE migrated_at IS NULL AND rejected_at IS NULL").fetchone()[0]
        migrated = conn.execute("SELECT COUNT(*) FROM events WHERE migrated_at IS NOT NULL").fetchone()[0]
        rejected = conn.execute("SELECT COUNT(*) FROM events WHERE rejected_at IS NOT NULL").fetchone()[0]
        return {"queue": str(p), "pending": pending, "migrated": migrated, "rejected": rejected}
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
    if out.get("ok", True) is False or out.get("delivered_to") == "none":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

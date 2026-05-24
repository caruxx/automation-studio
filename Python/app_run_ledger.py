#!/usr/bin/env python3
"""中央 Run Ledger（SQLite）。P3-1。

これまで「pipeline 実行履歴」は `_scheduler_history`（メモリ最大 50 件）と
動画フォルダ artifact（`youtube_upload.json` 等）の組み合わせで推論していたが、
チャンネル数が増えるにつれて履歴の検索性・横断集計が崩れる。

このモジュールは **1 row / 1 pipeline 実行** を SQLite で永続化し、
auto_resume の親子関係も追跡可能にする。folder artifact ベースの推論は
補助情報として残す（中央台帳が正典）。

スキーマ:
  CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL UNIQUE,   -- ULID 風（時刻順ソート可）
    kind            TEXT    NOT NULL,          -- 'vol_create' | 'spot_create' | 'auto_resume' | 'manual' | 'reconstructed'
    channel_id      TEXT,
    channel_folder  TEXT    NOT NULL,
    channel_name    TEXT,
    vol             INTEGER NOT NULL,
    video_name      TEXT,
    parent_run_id   TEXT,                      -- auto_resume の元
    parent_job_id   TEXT,                      -- scheduler の job id
    from_stage      TEXT,                      -- pipeline `--from <stage>` で開始した場合
    status          TEXT    NOT NULL,          -- 'in_progress' | 'done' | 'failed' | 'cancelled' | 'reconstructed'
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    duration_sec    INTEGER,
    exit_code       INTEGER,
    failed_stage    TEXT,                      -- 失敗時の停止 stage
    summary         TEXT,                      -- 短い 1 行 summary（Discord 通知文と同等）
    artifact_video_id TEXT,                    -- 完了時の YouTube video_id（非正規化キャッシュ）
    meta_json       TEXT                       -- 任意の JSON（拡張用）
  );

stale 回収:
  status='in_progress' のままで `started_at` が DEFAULT_STALE_AFTER_SEC（6h）より古いものは
  失敗扱いに降格する（worker / scheduler が落ちた場合の救済）。
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import sqlite3
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"

DB_PATH = CONFIG_DIR / "runs.db"
DEFAULT_STALE_AFTER_SEC = int(os.environ.get("APP_RUN_LEDGER_STALE_SEC", "21600"))  # 6h

VALID_KINDS = ("vol_create", "spot_create", "auto_resume", "manual", "reconstructed")
VALID_STATUSES = ("in_progress", "done", "failed", "cancelled", "reconstructed")


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _gen_run_id() -> str:
    """時刻 + ランダム接尾辞。ULID ライクで時刻順ソート可能。"""
    return f"r{int(datetime.datetime.utcnow().timestamp() * 1000)}_{secrets.token_hex(3)}"


def _parse_iso(s: str) -> Optional[datetime.datetime]:
    if not s:
        return None
    s = s.rstrip("Z")
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


# ─── DB 接続 ───────────────────────────────────────

_lock = threading.Lock()


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@contextmanager
def _txn():
    with _lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()


def init_db() -> None:
    """テーブル + インデックスを idempotent に作成。app.py 起動時に呼ぶ。"""
    _ensure_dirs()
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT    NOT NULL UNIQUE,
            kind            TEXT    NOT NULL,
            channel_id      TEXT,
            channel_folder  TEXT    NOT NULL,
            channel_name    TEXT,
            vol             INTEGER NOT NULL,
            video_name      TEXT,
            parent_run_id   TEXT,
            parent_job_id   TEXT,
            from_stage      TEXT,
            status          TEXT    NOT NULL,
            started_at      TEXT    NOT NULL,
            finished_at     TEXT,
            duration_sec    INTEGER,
            exit_code       INTEGER,
            failed_stage    TEXT,
            summary         TEXT,
            artifact_video_id TEXT,
            meta_json       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
        CREATE INDEX IF NOT EXISTS idx_runs_channel_vol ON runs(channel_id, vol);
        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
        CREATE INDEX IF NOT EXISTS idx_runs_parent ON runs(parent_run_id);
        """)


# ─── 公開 API ──────────────────────────────────────

def start_run(*,
              kind: str,
              channel_folder: str,
              vol: int,
              channel_id: str = "",
              channel_name: str = "",
              video_name: str = "",
              parent_run_id: str = "",
              parent_job_id: str = "",
              from_stage: str = "",
              meta: Optional[dict] = None) -> str:
    """新しい run を 'in_progress' で記録し、run_id を返す。"""
    if kind not in VALID_KINDS:
        raise ValueError(f"kind は {VALID_KINDS} のいずれか: {kind}")
    run_id = _gen_run_id()
    now = _now_iso()
    meta_str = json.dumps(meta, ensure_ascii=False) if meta else ""
    with _txn() as conn:
        conn.execute(
            """INSERT INTO runs
               (run_id, kind, channel_id, channel_folder, channel_name, vol, video_name,
                parent_run_id, parent_job_id, from_stage, status, started_at, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?)""",
            (run_id, kind, channel_id, channel_folder, channel_name, int(vol), video_name,
             parent_run_id, parent_job_id, from_stage, now, meta_str),
        )
    return run_id


def finish_run(run_id: str, *,
               status: str,
               exit_code: Optional[int] = None,
               failed_stage: str = "",
               summary: str = "",
               artifact_video_id: str = "") -> None:
    """run を完了状態に遷移させる。"""
    if status not in VALID_STATUSES:
        raise ValueError(f"status は {VALID_STATUSES} のいずれか: {status}")
    finished = _now_iso()
    with _txn() as conn:
        row = conn.execute("SELECT started_at FROM runs WHERE run_id=?", (run_id,)).fetchone()
        dur = None
        if row:
            sa = _parse_iso(row["started_at"] or "")
            if sa:
                dur = int((datetime.datetime.utcnow() - sa).total_seconds())
        conn.execute(
            """UPDATE runs
               SET status=?, finished_at=?, duration_sec=?, exit_code=?,
                   failed_stage=?, summary=?, artifact_video_id=?
               WHERE run_id=?""",
            (status, finished, dur, exit_code if exit_code is not None else None,
             failed_stage or None, (summary or "")[:500], artifact_video_id or "", run_id),
        )


def cancel_run(run_id: str, summary: str = "") -> bool:
    """in_progress を cancelled に降格。"""
    with _txn() as conn:
        cur = conn.execute(
            """UPDATE runs SET status='cancelled', finished_at=?, summary=?
               WHERE run_id=? AND status='in_progress'""",
            (_now_iso(), (summary or "")[:500], run_id),
        )
        return cur.rowcount > 0


def get_run(run_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None


def list_runs(*,
              channel_id: Optional[str] = None,
              status: Optional[str] = None,
              kind: Optional[str] = None,
              vol: Optional[int] = None,
              limit: int = 50,
              since_iso: Optional[str] = None) -> list:
    where = []
    params: list = []
    if channel_id:
        where.append("channel_id = ?"); params.append(channel_id)
    if status:
        where.append("status = ?"); params.append(status)
    if kind:
        where.append("kind = ?"); params.append(kind)
    if vol is not None:
        where.append("vol = ?"); params.append(int(vol))
    if since_iso:
        where.append("started_at >= ?"); params.append(since_iso)
    sql = "SELECT * FROM runs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC LIMIT ?"
    params.append(int(limit))
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_run_chain(run_id: str) -> list:
    """parent_run_id を辿って祖先 → 子孫の一連の試行を時系列で返す。

    auto_resume の連鎖を可視化する用途。"""
    with _connect() as conn:
        # 祖先 root を見つける
        cur = run_id
        for _ in range(20):  # 安全ループ
            row = conn.execute("SELECT parent_run_id FROM runs WHERE run_id=?", (cur,)).fetchone()
            if not row or not row["parent_run_id"]:
                break
            cur = row["parent_run_id"]
        root = cur
        # root から BFS で子孫を集める
        seen = {root}
        queue = [root]
        while queue:
            children = conn.execute(
                "SELECT run_id FROM runs WHERE parent_run_id=?", (queue.pop(0),),
            ).fetchall()
            for r in children:
                if r["run_id"] not in seen:
                    seen.add(r["run_id"])
                    queue.append(r["run_id"])
        if not seen:
            return []
        placeholders = ",".join("?" for _ in seen)
        rows = conn.execute(
            f"SELECT * FROM runs WHERE run_id IN ({placeholders}) ORDER BY started_at ASC",
            list(seen),
        ).fetchall()
        return [dict(r) for r in rows]


# ─── stale 回収 ────────────────────────────────────

def reap_stale(stale_after_sec: int = DEFAULT_STALE_AFTER_SEC) -> int:
    """worker / scheduler が落ちた等で in_progress 取り残しを failed に降格。"""
    cutoff = (datetime.datetime.utcnow()
              - datetime.timedelta(seconds=stale_after_sec)).isoformat() + "Z"
    with _txn() as conn:
        rows = conn.execute(
            "SELECT run_id FROM runs WHERE status='in_progress' AND started_at < ?",
            (cutoff,),
        ).fetchall()
        ids = [r["run_id"] for r in rows]
        if not ids:
            return 0
        for rid in ids:
            conn.execute(
                """UPDATE runs
                   SET status='failed', finished_at=?,
                       summary=COALESCE(summary,'') || ' [reaped as stale > ' || ? || 's]'
                   WHERE run_id=?""",
                (_now_iso(), int(stale_after_sec), rid),
            )
    return len(ids)


# ─── 集計（dashboard 用） ───────────────────────────

def stats(window_days: int = 7) -> dict:
    cutoff = (datetime.datetime.utcnow()
              - datetime.timedelta(days=window_days)).isoformat() + "Z"
    out = {
        "window_days": window_days,
        "total": 0,
        "in_progress": 0,
        "done": 0,
        "failed": 0,
        "cancelled": 0,
        "by_channel": {},     # channel_id → {done, failed, total}
        "avg_duration_sec": 0,
        "auto_resume_chains": 0,  # 親を持つ run の数（=再投入で生まれた run）
    }
    with _connect() as conn:
        out["total"] = conn.execute("SELECT COUNT(*) FROM runs WHERE started_at >= ?", (cutoff,)).fetchone()[0]
        for s in ("in_progress", "done", "failed", "cancelled"):
            n = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE status=? AND started_at >= ?",
                (s, cutoff),
            ).fetchone()[0]
            out[s] = n
        # 平均所要
        row = conn.execute(
            """SELECT AVG(duration_sec) FROM runs
               WHERE status='done' AND duration_sec IS NOT NULL AND started_at >= ?""",
            (cutoff,),
        ).fetchone()
        out["avg_duration_sec"] = int(row[0] or 0)
        # by_channel
        rows = conn.execute(
            """SELECT channel_id, status, COUNT(*) AS n FROM runs
               WHERE started_at >= ? AND channel_id != ''
               GROUP BY channel_id, status""",
            (cutoff,),
        ).fetchall()
        for r in rows:
            ch = r["channel_id"] or "(unknown)"
            d = out["by_channel"].setdefault(ch, {"done": 0, "failed": 0, "in_progress": 0, "total": 0})
            d[r["status"]] = r["n"]
            d["total"] += r["n"]
        # auto_resume chain
        out["auto_resume_chains"] = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE parent_run_id IS NOT NULL AND parent_run_id != '' AND started_at >= ?",
            (cutoff,),
        ).fetchone()[0]
    return out


# ─── マイグレーション（既存 vol → ledger） ───────────

def reconstruct_from_artifacts(channel_folder: str, channel_id: str = "",
                                channel_name: str = "", *,
                                dry_run: bool = True) -> dict:
    """既存 vol フォルダの `youtube_upload.json` を読んで ledger に reconstructed
    レコードを作る（or dry_run 時は作らずに diff だけ返す）。

    既に同じ vol+channel_folder で done なレコードがある場合はスキップ。
    """
    cf = Path(channel_folder)
    out = {"channel_folder": str(cf), "would_insert": [], "skipped": [], "missing_marker": [],
           "applied": False, "inserted": 0}
    if not cf.exists():
        return out
    import re as _re
    with _connect() as conn:
        for d in cf.iterdir():
            if not d.is_dir():
                continue
            m = _re.match(r"^(\d+)_", d.name)
            if not m:
                continue
            vol = int(m.group(1))
            marker = d / "youtube_upload.json"
            if not marker.exists():
                out["missing_marker"].append(d.name)
                continue
            try:
                upm = json.loads(marker.read_text(encoding="utf-8"))
            except Exception:
                out["missing_marker"].append(d.name)
                continue
            video_id = upm.get("video_id") or ""
            uploaded_at = upm.get("uploaded_at") or _now_iso()
            # 既に done または reconstructed で同じ vol+channel_folder の run があればスキップ
            existing = conn.execute(
                """SELECT run_id FROM runs
                   WHERE channel_folder=? AND vol=?
                     AND status IN ('done', 'reconstructed')
                     AND artifact_video_id=?
                   LIMIT 1""",
                (str(cf), vol, video_id),
            ).fetchone()
            if existing:
                out["skipped"].append({"vol": vol, "name": d.name, "existing_run_id": existing["run_id"]})
                continue
            entry = {
                "vol": vol,
                "name": d.name,
                "video_id": video_id,
                "uploaded_at": uploaded_at,
            }
            out["would_insert"].append(entry)
        if dry_run:
            return out
        # apply
        for entry in out["would_insert"]:
            run_id = _gen_run_id()
            conn.execute(
                """INSERT INTO runs
                   (run_id, kind, channel_id, channel_folder, channel_name, vol, video_name,
                    status, started_at, finished_at, summary, artifact_video_id)
                   VALUES (?, 'reconstructed', ?, ?, ?, ?, ?, 'reconstructed', ?, ?, ?, ?)""",
                (run_id, channel_id, str(cf), channel_name, entry["vol"], entry["name"],
                 entry["uploaded_at"], entry["uploaded_at"],
                 "reconstructed from youtube_upload.json", entry["video_id"]),
            )
            out["inserted"] += 1
        out["applied"] = True
    return out


# ─── CLI ───────────────────────────────────────────

def _main():
    import argparse
    p = argparse.ArgumentParser(description="run ledger 運用ツール")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="DB 初期化")
    sp_l = sub.add_parser("list", help="直近 50 件を表示")
    sp_l.add_argument("--channel-id", default=None)
    sp_l.add_argument("--status", default=None)
    sp_l.add_argument("--limit", type=int, default=50)
    sp_st = sub.add_parser("stats", help="集計")
    sp_st.add_argument("--days", type=int, default=7)
    sp_reap = sub.add_parser("reap", help="stale を failed に降格")
    sp_reap.add_argument("--sec", type=int, default=DEFAULT_STALE_AFTER_SEC)
    sp_chain = sub.add_parser("chain", help="run の祖先 + 子孫を表示")
    sp_chain.add_argument("run_id")
    sp_mig = sub.add_parser("migrate", help="既存 vol を reconstructed として取り込む")
    sp_mig.add_argument("channel_folder")
    sp_mig.add_argument("--channel-id", default="")
    sp_mig.add_argument("--channel-name", default="")
    sp_mig.add_argument("--apply", action="store_true", help="dry-run を抜けて実適用")
    args = p.parse_args()

    if args.cmd == "init":
        init_db()
        print(f"✅ DB 初期化: {DB_PATH}")
    elif args.cmd == "list":
        for r in list_runs(channel_id=args.channel_id, status=args.status, limit=args.limit):
            print(f"  [{r['run_id']}] {r['status']:<12} kind={r['kind']:<14} ch={r['channel_id'] or '?'} vol.{r['vol']} dur={r.get('duration_sec') or '-'}s")
    elif args.cmd == "stats":
        print(json.dumps(stats(args.days), ensure_ascii=False, indent=2))
    elif args.cmd == "reap":
        n = reap_stale(args.sec)
        print(f"✅ {n} 件を stale → failed に降格")
    elif args.cmd == "chain":
        for r in get_run_chain(args.run_id):
            print(f"  [{r['run_id']}] parent={r.get('parent_run_id') or '-'} status={r['status']} from={r.get('from_stage') or '-'}")
    elif args.cmd == "migrate":
        out = reconstruct_from_artifacts(
            args.channel_folder,
            channel_id=args.channel_id, channel_name=args.channel_name,
            dry_run=not args.apply,
        )
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()

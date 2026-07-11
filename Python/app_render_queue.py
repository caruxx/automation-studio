#!/usr/bin/env python3
"""Premiere / Media Encoder の物理シリアル制約を吸収する SQLite ベース queue。

P2-1（6+ チャンネル並列運用）の中核モジュール。

設計:
- Premiere Pro と Media Encoder は Mac 1 台に **1 セッションしか動かない**。
- pipeline (app_pipeline.py) を 6 チャンネル並列で走らせると、premiere/export
  だけは衝突する → このモジュールに enqueue して 1 worker が直列処理する。
- 他 stage（plan/suno/rename/meta/upload）は **API 制約のみ**なので並列実行可。

スキーマ:
  CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_folder  TEXT    NOT NULL,
    channel_name    TEXT,
    vol             INTEGER NOT NULL,
    video_name      TEXT,                    -- vol フォルダ名（解決済）
    stage           TEXT    NOT NULL,        -- 'premiere' | 'export'
    status          TEXT    NOT NULL DEFAULT 'pending',
                                             -- 'pending' | 'running' | 'done' | 'error' | 'cancelled'
    enqueued_at     TEXT    NOT NULL,        -- ISO 8601
    started_at      TEXT,
    finished_at     TEXT,
    error_message   TEXT,
    duration_sec    INTEGER,                 -- finished - started
    parent_run_id   TEXT,                    -- 上位 pipeline 実行を識別する任意キー
    channel_id      TEXT                     -- channels.json の id（旧レコードは空）
  );

stale running の自動回収:
- worker が claim 後にプロセスごと落ちた場合、`status='running'` のまま残る。
- DEFAULT_STALE_AFTER_SEC（既定 7200 = 2h）を超えた running は起動時に
  `cancelled` に降格し、enqueue した側にエラー通知（pipeline は `--from` で再開）。
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"

DB_PATH = CONFIG_DIR / "render_queue.db"

DEFAULT_STALE_AFTER_SEC = int(os.environ.get("APP_RENDER_QUEUE_STALE_SEC", "7200"))
ALLOWED_STAGES = ("premiere", "export")


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _parse_iso(s: str) -> Optional[datetime.datetime]:
    if not s:
        return None
    s = s.rstrip("Z")
    try:
        return datetime.datetime.fromisoformat(s)
    except Exception:
        return None


# ─── DB 接続 ───────────────────────────────────────

_lock = threading.Lock()  # SQLite はマルチスレッド書込で WAL でも稀に詰まるため明示


def _ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL モードで複数プロセスからの読みに強くする
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@contextmanager
def _txn():
    """シリアライズ書込用のロック付きトランザクション。"""
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
        CREATE TABLE IF NOT EXISTS jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_folder  TEXT    NOT NULL,
            channel_name    TEXT,
            vol             INTEGER NOT NULL,
            video_name      TEXT,
            stage           TEXT    NOT NULL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            enqueued_at     TEXT    NOT NULL,
            started_at      TEXT,
            finished_at     TEXT,
            error_message   TEXT,
            duration_sec    INTEGER,
            parent_run_id   TEXT,
            channel_id      TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status_enqueued
            ON jobs(status, enqueued_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_finished
            ON jobs(finished_at);
        """)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "channel_id" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN channel_id TEXT DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_channel ON jobs(channel_id)")


# ─── 公開 API ──────────────────────────────────────

def enqueue(*, channel_folder: str, channel_name: str,
            vol: int, stage: str, video_name: str = "",
            parent_run_id: str = "", channel_id: str = "") -> int:
    """ジョブを pending で投入。worker がいずれ拾う。"""
    if stage not in ALLOWED_STAGES:
        raise ValueError(f"stage は {ALLOWED_STAGES} のいずれか: {stage}")
    if not channel_folder:
        raise ValueError("channel_folder is required")
    with _txn() as conn:
        cur = conn.execute(
            """INSERT INTO jobs
               (channel_folder, channel_name, vol, video_name, stage,
                status, enqueued_at, parent_run_id, channel_id)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (channel_folder, channel_name or "", int(vol), video_name or "",
             stage, _now_iso(), parent_run_id or "", channel_id or ""),
        )
        return cur.lastrowid


def claim_next() -> Optional[dict]:
    """oldest pending を 1 件 atomic に running へ遷移して返す。なければ None。
    返す dict は **更新後** の状態（status='running', started_at が入っている）。"""
    with _txn() as conn:
        row = conn.execute(
            """SELECT * FROM jobs
               WHERE status='pending'
               ORDER BY enqueued_at ASC, id ASC
               LIMIT 1"""
        ).fetchone()
        if not row:
            return None
        started = _now_iso()
        conn.execute(
            "UPDATE jobs SET status='running', started_at=? WHERE id=?",
            (started, row["id"]),
        )
        d = dict(row)
        d["status"] = "running"
        d["started_at"] = started
        return d


def mark_done(job_id: int) -> None:
    finished = _now_iso()
    with _txn() as conn:
        row = conn.execute("SELECT started_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        dur = None
        if row:
            sa = _parse_iso(row["started_at"] or "")
            if sa:
                dur = int((datetime.datetime.utcnow() - sa).total_seconds())
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=?, duration_sec=? WHERE id=?",
            (finished, dur, job_id),
        )


def mark_error(job_id: int, message: str) -> None:
    finished = _now_iso()
    msg = (message or "")[:1000]
    with _txn() as conn:
        row = conn.execute("SELECT started_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        dur = None
        if row:
            sa = _parse_iso(row["started_at"] or "")
            if sa:
                dur = int((datetime.datetime.utcnow() - sa).total_seconds())
        conn.execute(
            "UPDATE jobs SET status='error', finished_at=?, duration_sec=?, error_message=? WHERE id=?",
            (finished, dur, msg, job_id),
        )


def cancel(job_id: int) -> bool:
    with _txn() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='pending'",
            (_now_iso(), job_id),
        )
        return cur.rowcount > 0


def get_job(job_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs(status: Optional[str] = None, limit: int = 50) -> list:
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]


def reap_stale_running(stale_after_sec: int = DEFAULT_STALE_AFTER_SEC) -> int:
    """worker が落ちた等で running 取り残しになったジョブを cancelled に。
    起動時 + 定期的に呼ぶ想定。返り値: 回収件数。"""
    cutoff = (datetime.datetime.utcnow()
              - datetime.timedelta(seconds=stale_after_sec)).isoformat() + "Z"
    with _txn() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status='running' AND started_at < ?",
            (cutoff,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        for jid in ids:
            conn.execute(
                """UPDATE jobs
                   SET status='error',
                       finished_at=?,
                       error_message=COALESCE(error_message,'')
                                     || ' [reaped as stale > ' || ? || 's]'
                   WHERE id=?""",
                (_now_iso(), int(stale_after_sec), jid),
            )
    return len(ids)


# ─── 統計（throughput API 用） ──────────────────────

def stats(window_days: int = 7) -> dict:
    """直近 window_days 日の統計を返す。throughput / 平均所要時間 / 失敗率。"""
    cutoff = (datetime.datetime.utcnow()
              - datetime.timedelta(days=window_days)).isoformat() + "Z"
    out = {
        "window_days": window_days,
        "pending": 0,
        "running": 0,
        "by_day": [],          # [{date, premiere_done, export_done, errors, total_duration_sec}]
        "by_stage_avg_sec": {},
        "queue_throughput_per_day": 0.0,
        "estimated_daily_capacity": 0,
    }
    with _connect() as conn:
        out["pending"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
        out["running"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
        # stage 別 平均所要
        for stage in ALLOWED_STAGES:
            row = conn.execute(
                """SELECT AVG(duration_sec) AS avg_sec, COUNT(*) AS n
                   FROM jobs
                   WHERE stage=? AND status='done' AND duration_sec IS NOT NULL
                   AND finished_at >= ?""",
                (stage, cutoff),
            ).fetchone()
            if row and row["n"]:
                out["by_stage_avg_sec"][stage] = int(row["avg_sec"] or 0)
        # 日別集計
        rows = conn.execute(
            """SELECT
                 substr(finished_at, 1, 10) AS day,
                 stage,
                 status,
                 COUNT(*) AS n,
                 SUM(COALESCE(duration_sec, 0)) AS total_dur
               FROM jobs
               WHERE finished_at IS NOT NULL AND finished_at >= ?
               GROUP BY day, stage, status
               ORDER BY day DESC""",
            (cutoff,),
        ).fetchall()
        per_day: dict = {}
        for r in rows:
            d = per_day.setdefault(r["day"], {
                "date": r["day"],
                "premiere_done": 0, "export_done": 0,
                "errors": 0, "total_duration_sec": 0,
            })
            if r["status"] == "done":
                if r["stage"] == "premiere":
                    d["premiere_done"] += r["n"]
                elif r["stage"] == "export":
                    d["export_done"] += r["n"]
                d["total_duration_sec"] += int(r["total_dur"] or 0)
            elif r["status"] == "error":
                d["errors"] += r["n"]
        out["by_day"] = sorted(per_day.values(), key=lambda x: x["date"], reverse=True)
        # 平均 throughput（vol/day）= 1 vol = premiere 1 + export 1 として概算
        prem = sum(d["premiere_done"] for d in out["by_day"])
        days_with_data = max(1, len(out["by_day"]))
        out["queue_throughput_per_day"] = round(prem / days_with_data, 2)
        # 1 日の理論上限（24h ÷ (premiere_avg + export_avg)）
        avg_prem = out["by_stage_avg_sec"].get("premiere", 0)
        avg_exp = out["by_stage_avg_sec"].get("export", 0)
        cycle = avg_prem + avg_exp
        if cycle > 0:
            out["estimated_daily_capacity"] = max(1, int(86400 / cycle))
    return out


# ─── 同期待機（pipeline からの利用） ───────────────

def wait_for(job_id: int, *, timeout_sec: int = 7200, poll_sec: float = 2.0) -> dict:
    """job が done/error/cancelled に遷移するまで blocking wait。"""
    start = time.time()
    while True:
        job = get_job(job_id)
        if not job:
            raise RuntimeError(f"render queue job not found: id={job_id}")
        if job["status"] in ("done", "error", "cancelled"):
            return job
        if time.time() - start > timeout_sec:
            raise TimeoutError(f"render queue wait timeout: id={job_id} (>{timeout_sec}s)")
        time.sleep(poll_sec)


# ─── CLI（運用デバッグ用） ─────────────────────────

def _main():
    import argparse
    p = argparse.ArgumentParser(description="render queue 運用ツール")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="DB 初期化（idempotent）")
    sub.add_parser("list", help="直近 50 件を表示")
    sp_st = sub.add_parser("stats", help="直近 7 日の統計を JSON で表示")
    sp_st.add_argument("--days", type=int, default=7)
    sp_reap = sub.add_parser("reap", help="stale running を回収")
    sp_reap.add_argument("--sec", type=int, default=DEFAULT_STALE_AFTER_SEC)
    sp_canc = sub.add_parser("cancel", help="pending ジョブをキャンセル")
    sp_canc.add_argument("id", type=int)
    args = p.parse_args()

    if args.cmd == "init":
        init_db()
        print(f" DB 初期化: {DB_PATH}")
    elif args.cmd == "list":
        for j in list_jobs(limit=50):
            print(f"  [{j['id']:>4}] {j['status']:<10} {j['stage']:<8} "
                  f"vol.{j['vol']:>3} ch={j['channel_name'] or '?'} "
                  f"enq={j['enqueued_at']} dur={j.get('duration_sec') or '-'}s")
    elif args.cmd == "stats":
        print(json.dumps(stats(args.days), ensure_ascii=False, indent=2))
    elif args.cmd == "reap":
        n = reap_stale_running(args.sec)
        print(f" {n} 件を stale → error に降格")
    elif args.cmd == "cancel":
        ok = cancel(args.id)
        print((" cancelled" if ok else "⚠ pending ではありません") + f" (id={args.id})")


if __name__ == "__main__":
    _main()

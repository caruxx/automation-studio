#!/usr/bin/env python3
"""run ledger（P3-1）

段階 7 で app.py から移した（振る舞いは変えていない）。土台の import は app.py と同じ。
"""
import asyncio  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import secrets  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import sys  # noqa: F401
import gzip  # noqa: F401
import platform  # noqa: F401
import unicodedata  # noqa: F401
import time  # noqa: F401
import threading  # noqa: F401
from pathlib import Path  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from collections import deque  # noqa: F401

from fastapi import APIRouter, WebSocket, HTTPException, UploadFile, File, Request, Response  # noqa: F401
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: F401
from pydantic import BaseModel, Field  # noqa: F401
from typing import Optional, List, Literal  # noqa: F401

import app_core  # noqa: F401
import exit_codes as _exit_codes  # noqa: F401
from app_core import *  # noqa: F401,F403
from pydantic import field_validator  # noqa: F401
import app_updater  # noqa: F401
from settings_service import config_get, config_set, load_catalog  # noqa: F401
import app_music_catalog as music_catalog  # noqa: F401

router = APIRouter()

# ─── API: run ledger（P3-1） ─────────────────────

@router.get("/api/runs/ledger")
def api_runs_ledger_list(
    channel_id: Optional[str] = None,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    vol: Optional[int] = None,
    limit: int = 50,
):
    """中央 ledger から直近の run を返す。

    フィルタ: channel_id / status (in_progress|done|failed|cancelled|reconstructed) /
             kind (vol_create|spot_create|auto_resume|manual|reconstructed) / vol。"""
    import app_run_ledger as _ledger
    runs = _ledger.list_runs(channel_id=channel_id, status=status, kind=kind,
                             vol=vol, limit=int(limit))
    return {"status": "ok", "runs": runs, "count": len(runs)}


@router.get("/api/runs/ledger/stats")
def api_runs_ledger_stats(days: int = 7):
    """成功率 / 平均所要 / 自動再投入率の集計。運用 SLO 確認用。"""
    import app_run_ledger as _ledger
    return _ledger.stats(window_days=int(days))


@router.get("/api/runs/ledger/{run_id}")
def api_runs_ledger_get(run_id: str):
    import app_run_ledger as _ledger
    r = _ledger.get_run(run_id)
    if not r:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"status": "ok", "run": r}


@router.get("/api/runs/ledger/{run_id}/chain")
def api_runs_ledger_chain(run_id: str):
    """指定 run の祖先 + 子孫（auto_resume チェーン）を時系列で返す。"""
    import app_run_ledger as _ledger
    chain = _ledger.get_run_chain(run_id)
    if not chain:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"status": "ok", "chain": chain, "count": len(chain)}


class LedgerMigrateRequest(BaseModel):
    channel_id: Optional[str] = None  # 未指定なら全チャンネルを順に処理
    apply: Optional[bool] = False     # False=dry-run, True=実際に挿入

@router.post("/api/runs/ledger/migrate")
def api_runs_ledger_migrate(req: LedgerMigrateRequest = LedgerMigrateRequest()):
    """既存 vol を ledger に reconstructed として取り込む。

    既定は dry-run（diff のみ返す）。`apply: true` で実際に挿入。"""
    import app_run_ledger as _ledger
    chs = get_channels()
    targets = chs if not req.channel_id else [c for c in chs if c.get("id") == req.channel_id]
    if not targets:
        raise HTTPException(404, "対象チャンネルが見つかりません")
    results = []
    total_would = 0
    total_inserted = 0
    for ch in targets:
        cf = ch.get("folder") or ""
        if not cf or not Path(cf).exists():
            continue
        out = _ledger.reconstruct_from_artifacts(
            cf, channel_id=ch.get("id", ""), channel_name=ch.get("name", ""),
            dry_run=not req.apply,
        )
        total_would += len(out.get("would_insert", []))
        total_inserted += out.get("inserted", 0)
        results.append({"channel": ch.get("name"), **out})
    return {
        "status": "ok",
        "dry_run": not req.apply,
        "total_would_insert": total_would,
        "total_inserted": total_inserted,
        "channels": results,
    }


@router.post("/api/runs/ledger/reap")
def api_runs_ledger_reap():
    """stale in_progress を failed に降格（手動メンテ用）。"""
    import app_run_ledger as _ledger
    n = _ledger.reap_stale()
    return {"status": "ok", "reaped": n}

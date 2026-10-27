#!/usr/bin/env python3
"""混成（複数 Style）ドラフト生成

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

# ─── 混成（複数 Style）ドラフト生成 ───
# SUNO は 1 prompt = 1 Style なので「ボサ14＋R&B6」のような混成は Style グループごとに
# 生成して統合する必要がある。/tmp/sukima_mix20_draft_gen.py をやめ、
# .app_channel_config.json の suno.cozy_mix を読む config 駆動（S6）。
class MixDraftRequest(BaseModel):
    video_name: Optional[str] = None     # 履歴/workspace ヒント（{channel}_vol{N}）。無くても可。
    provider: Optional[str] = None        # 未指定なら DRAFT_PROVIDER env→suno.provider→codex
    diversity_threshold: Optional[float] = None
    diversity_retry: Optional[int] = None
    history_limit: Optional[int] = None


@router.post("/api/suno/generate-mix-drafts")
async def api_suno_generate_mix_drafts(req: MixDraftRequest):
    """SUNO を起動せず、suno.cozy_mix を読んで混成ドラフト（複数 Style）を生成して返す。

    ブラウザ不要・LLM 生成のみ。返した songs[] を確認・編集 → /api/suno/start に
    songs_draft_json で投入する前段（混成版の generate-drafts 相当）。
    """
    channel_config = load_channel_config()
    cozy = ((channel_config.get("suno") or {}).get("cozy_mix") or {})
    if not cozy:
        raise HTTPException(
            400,
            "このチャンネルには suno.cozy_mix（混成 Style 定義）がありません。"
            ".app_channel_config.json に cozy_mix を設定してください。",
        )

    # workspace ヒント解決（履歴キー用。video_name から {channel}_vol{N} を作る）
    workspace = None
    if req.video_name:
        cfg = get_dashboard_config()
        channel_name = (cfg.get("channel_name") or "orzz").strip()
        channel_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", channel_name).strip("_") or "orzz"
        m = re.match(r"^(\d+)_", req.video_name)
        vol = m.group(1) if m else ""
        workspace = f"{channel_slug}_vol{vol}" if vol else channel_slug

    sc = get_suno_config()

    def _run():
        sys.path.insert(0, str(SUNO_SCRIPT.parent))
        import suno_auto_create as _suno
        return _suno.generate_mixed_drafts(
            channel_config,
            provider=req.provider,
            claude_cli=sc.get("claude_cli"),
            codex_cli=sc.get("codex_cli"),
            workspace=workspace,
            diversity_threshold=req.diversity_threshold,
            diversity_retry=req.diversity_retry,
            history_limit=req.history_limit,
        )

    try:
        songs = await asyncio.to_thread(_run)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"混成ドラフト生成に失敗しました: {e}")

    mix = cozy.get("mix") or {}
    expected = sum(v for k, v in mix.items()
                   if not str(k).startswith("_") and isinstance(v, int))
    return {
        "status": "ok",
        "count": len(songs),
        "expected": expected,
        "songs": songs,
    }

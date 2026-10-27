#!/usr/bin/env python3
"""YouTube 公開ゲート（P2-7）

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

# ─── API: YouTube 公開ゲート（P2-7） ───────────────
# private upload 後、N 時間経過してから自動 public 化する後段ジョブを動的登録。

class SchedulePublishRequest(BaseModel):
    video_name: str
    channel_folder: Optional[str] = None  # 未指定なら active channel
    channel_name: Optional[str] = None
    delay_hours: float

@router.post("/api/youtube/schedule-publish")
def api_youtube_schedule_publish(req: SchedulePublishRequest):
    """段階 3: Mac 側の公開ゲートは撤去した。予約公開は upload 時の publishAt（VPS の投稿記録が正本）で行う。"""
    raise HTTPException(410, "Mac 側の公開ゲートは廃止されました。公開予定は VPS（studio.py schedule-status / schedule-put）で管理し、"
                             "upload 時に publishAt として送られます。即時公開は /api/youtube/publish-now/{video_name} を使ってください。")


@router.post("/api/youtube/publish-now/{video_name}")
def api_youtube_publish_now(video_name: str):
    """指定 vol を**いま即座に** public 化（運営者の手動操作用）。"""
    cfg = get_dashboard_config()
    ch_folder = cfg.get("channel_folder") or ""
    if not ch_folder:
        raise HTTPException(400, "active channel が無い")
    folder = resolve_video_folder(video_name, channel_root=ch_folder)
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    from app_youtube import publish_video_to_public
    result = publish_video_to_public(folder)
    if result.get("status") in ("ok", "already_public"):
        return {"status": "ok", **result}
    raise HTTPException(500, f"publish failed: {result}")


class UpdateSnippetRequest(BaseModel):
    apply_localizations: bool = True


@router.post("/api/youtube/update-snippet/{video_name}")
def api_youtube_update_snippet(video_name: str, req: UpdateSnippetRequest = UpdateSnippetRequest()):
    """既存動画のタイトル/説明/タグ/言語/localizations を YouTube videos.update で更新。

    動画ファイルは再アップロードしない（quota ~50 unit）。`youtube_upload.json` から
    video_id を取得し、`youtube_title.txt` `youtube_description.txt` `youtube_tags.txt`
    `youtube_localizations.json` を読み取って snippet + localizations を反映する。
    """
    cfg = get_dashboard_config()
    ch_folder = cfg.get("channel_folder") or ""
    if not ch_folder:
        raise HTTPException(400, "active channel が無い")
    folder = resolve_video_folder(video_name, channel_root=ch_folder)
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    from app_youtube import update_video_snippet
    result = update_video_snippet(folder, apply_localizations=req.apply_localizations)
    status_code_map = {"ok": 200, "missing": 404, "retryable": 503, "error": 500}
    sc = status_code_map.get(result.get("status", "error"), 500)
    if sc != 200:
        raise HTTPException(sc, f"update-snippet failed: {result}")
    return {"status": "ok", **result}


# 段階 3: 起動時の公開予約復旧（_recover_scheduled_publishes）は撤去。VPS の publish_verify が予約を照合する。

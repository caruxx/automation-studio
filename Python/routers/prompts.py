#!/usr/bin/env python3
"""チャンネルアイコン・プロンプト

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

# ─── API: チャンネルアイコン ───

@router.get("/api/channel-icon")
def api_channel_icon():
    """現在のチャンネルアイコンを返す。ローカル → channels.json の icon_cache → 404。"""
    from fastapi.responses import RedirectResponse
    config = get_dashboard_config()
    icon_path = config.get("channel_icon", "")
    if icon_path and Path(icon_path).exists():
        return FileResponse(icon_path)
    channel_dir = Path(config.get("channel_folder", ""))
    if channel_dir.exists():
        for name in ["icon.png", "icon.jpg", "channel_icon.png", "channel_icon.jpg"]:
            icon = channel_dir / name
            if icon.exists():
                return FileResponse(str(icon))
    # YouTube URL から取得した remote アイコンにリダイレクト（active channel）
    chs = get_channels()
    active = next((c for c in chs if c.get("folder") == config.get("channel_folder")), None)
    if active:
        url = (active.get("icon_cache") or {}).get("url") or active.get("icon_url", "")
        if url:
            return RedirectResponse(url, status_code=302)
    raise HTTPException(404, "アイコンが設定されていません")

# ─── API: プロンプト ───

def get_prompts():
    default = [
        {"id": "lounge", "name": "Lounge BGM", "text": "Create a sophisticated lounge BGM track. Think elegant cafe, golden hour, luxury hotel lobby vibes. Instrumental only."},
        {"id": "ambient", "name": "Ambient Chill", "text": "Create a calm ambient BGM. Ethereal pads, soft piano, nature-inspired textures. Instrumental only."},
    ]
    return load_json(PROMPTS_CONFIG, default) if PROMPTS_CONFIG.exists() else default

@router.get("/api/prompts")
def api_list_prompts():
    return {"prompts": get_prompts()}

class PromptSave(BaseModel):
    id: Optional[str] = None
    name: str
    text: str

@router.post("/api/prompts")
def api_save_prompt(req: PromptSave):
    prompts = get_prompts()
    pid = req.id or re.sub(r'[^a-zA-Z0-9]', '_', req.name.lower()).strip('_') + str(len(prompts))
    existing = next((p for p in prompts if p["id"] == pid), None)
    if existing:
        existing["name"] = req.name; existing["text"] = req.text
    else:
        prompts.append({"id": pid, "name": req.name, "text": req.text})
    save_json(PROMPTS_CONFIG, prompts)
    return {"status": "ok", "prompts": prompts}

@router.delete("/api/prompts/{prompt_id}")
def api_delete_prompt(prompt_id: str):
    save_json(PROMPTS_CONFIG, [p for p in get_prompts() if p["id"] != prompt_id])
    return {"status": "ok"}

#!/usr/bin/env python3
"""画像選択・Claude 提案（タイトル・説明・タグ）

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

# ─── API: 画像選択（JSX 連動）───

IMAGE_EXTS = (".jpg", ".jpeg", ".png")

@router.get("/api/videos/{video_name}/images")
def api_video_images(video_name: str):
    """動画フォルダ内の画像一覧を返す。サムネ判定＋選択状態も返却。"""
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    # 選択状態を読み込み
    sel_file = folder / "selected_images.json"
    sel = {"main": "", "sub": []}
    if sel_file.exists():
        try:
            sel = json.loads(sel_file.read_text(encoding="utf-8"))
            sel.setdefault("main", "")
            sel.setdefault("sub", [])
        except Exception:
            pass

    m = re.match(r"^(\d+)_", video_name)
    num = m.group(1) if m else ""

    images = []
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in IMAGE_EXTS:
            continue
        if p.name.startswith("."):
            continue
        fn = p.name
        kind = "none"
        if sel["main"] == fn:
            kind = "main"
        elif fn in sel["sub"]:
            kind = "sub"
        is_thumb = (fn == f"vol{num}.jpg" or fn == f"vol{num}.png" or fn == "サムネイル.jpg")
        images.append({
            "filename": fn,
            "size": p.stat().st_size,
            "kind": kind,
            "is_thumb": is_thumb,
        })
    return {"images": images, "selected": sel}


@router.get("/api/videos/{video_name}/image-file/{filename:path}")
def api_video_image_file(video_name: str, filename: str):
    """動画フォルダ内の画像を配信（サムネ・背景プレビュー用）"""
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    # パストラバーサル防止
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "不正なファイル名")
    fp = folder / filename
    if not fp.exists() or fp.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(404, "画像が見つかりません")
    return FileResponse(fp)


class SelectedImagesUpdate(BaseModel):
    main: Optional[str] = ""
    sub: Optional[List[str]] = []

@router.put("/api/videos/{video_name}/selected-images")
def api_update_selected_images(video_name: str, req: SelectedImagesUpdate):
    """選択画像を selected_images.json に保存（JSX が読み込む）"""
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    if not folder.exists():
        raise HTTPException(404)
    data = {
        "main": (req.main or "").strip(),
        "sub": [s.strip() for s in (req.sub or []) if s and s.strip()],
    }
    (folder / "selected_images.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"status": "ok", **data}

@router.delete("/api/videos/{video_name}/selected-images")
def api_delete_selected_images(video_name: str):
    """選択状態をリセット（JSX は初期値フォールバック）"""
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    sel_file = folder / "selected_images.json"
    if sel_file.exists():
        sel_file.unlink()
    return {"status": "ok"}

# ─── API: Claude 提案（タイトル・説明・タグ）───

class SuggestRequest(BaseModel):
    mode: str                       # "titles" | "description" | "tags"
    count: Optional[int] = 5        # titles 用
    reference: Optional[str] = None # description 用（任意の参考文）

@router.post("/api/videos/{video_name}/suggest")
def api_video_suggest(video_name: str, req: SuggestRequest):
    """Claude CLI でタイトル/説明/タグを提案（JSON出力・API未使用）

    ベンチマーク分析キャッシュ（competitor_analysis_cache.json）が存在すれば、
    そこに含まれる viewer_needs / keywords / tag_suggestions 等を視聴者文脈として
    プロンプトに自動注入する（無ければ persona のみで動作）。
    """
    from claude_proposer import (
        propose_titles, propose_description, propose_tags, gather_context,
    )
    import app_competitor as _ac
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    persona = config.get("persona", "")
    channel_name = config.get("channel_name", "orzz.")
    ctx = gather_context(folder)
    # メイン言語（メタ生成のソース言語）= per-channel youtube_upload_defaults.default_language
    source_lang = (get_yt_upload_defaults().get("default_language") or "en").strip() or "en"
    # 説明文 CTA 用のチャンネルハンドル（channels.json レジストリから解決）
    handle = ""
    try:
        for _ch in get_channels():
            if _ch.get("folder") == config.get("channel_folder"):
                handle = _ch.get("handle", "") or ""
                break
    except Exception:
        handle = ""

    # ベンチマーク分析キャッシュを optional 注入
    benchmark_analysis = None
    try:
        cache = _ac.load_cache()
        if cache and isinstance(cache, dict):
            benchmark_analysis = cache.get("analysis") if isinstance(cache.get("analysis"), dict) else None
    except Exception:
        benchmark_analysis = None

    try:
        if req.mode == "titles":
            titles = propose_titles(
                cli_cmd=cli_cmd, persona=persona, channel_name=channel_name,
                source_lang=source_lang,
                count=req.count or 5, benchmark_analysis=benchmark_analysis, **ctx,
            )
            return {"status": "ok", "titles": titles}
        elif req.mode == "description":
            description = propose_description(
                cli_cmd=cli_cmd, persona=persona, channel_name=channel_name,
                source_lang=source_lang, handle=handle,
                reference=req.reference or "", benchmark_analysis=benchmark_analysis, **ctx,
            )
            return {"status": "ok", "description": description}
        elif req.mode == "tags":
            tags = propose_tags(cli_cmd=cli_cmd, persona=persona,
                                channel_name=channel_name, source_lang=source_lang,
                                benchmark_analysis=benchmark_analysis, **ctx)
            return {"status": "ok", "tags": tags}
        else:
            raise HTTPException(400, f"未知のmode: {req.mode}")
    except RuntimeError as e:
        raise HTTPException(500, str(e))

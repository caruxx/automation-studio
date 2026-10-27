#!/usr/bin/env python3
"""SUNO プロンプト雛形提案・Finder 操作・フォルダピッカー

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

# ─── API: SUNO プロンプト雛形提案（ペルソナ起点） ───

class SunoSuggestPromptRequest(BaseModel):
    persona: Optional[str] = None
    style_hint: Optional[str] = None

@router.post("/api/suno/suggest-prompt")
def api_suno_suggest_prompt(req: SunoSuggestPromptRequest):
    """ペルソナと任意のスタイルヒントから SUNO 用プロンプト雛形を Claude CLI で生成。
    競合分析を必要としない軽量版。設定タブ内の SUNO セクションから呼び出される。
    """
    persona = (req.persona or get_dashboard_config().get("persona") or "").strip()
    if not persona:
        raise HTTPException(400, "ペルソナが空です。設定タブでペルソナを入力してください")
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    style_hint = (req.style_hint or "").strip()

    prompt = f"""You are crafting a single SUNO music generation prompt for a YouTube BGM channel.

=== Channel Persona ===
{persona[:2000]}

=== Style Hint (optional, may be empty) ===
{style_hint or '(none)'}

=== Your Task ===
Output a SINGLE SUNO prompt (roughly 150-250 characters of English).
- Lead with genre + mood + key instruments that match the persona's atmosphere
- Include BPM/tempo hint only if meaningful (e.g. "slow 70bpm")
- No vocals. Instrumental BGM only.
- Anchor one sensory scene (e.g. "rainy midnight cafe") for concrete atmosphere.
- STRICTLY FORBIDDEN: do NOT reference any real artist, band, composer, producer, label, or song/album names. SUNO rejects copyrighted names.

Respond with a SINGLE JSON object:
{{
  "prompt": "<the SUNO prompt, one line, in English>",
  "rationale": "<one English sentence explaining why this fits the persona>"
}}

Output ONLY the JSON object."""

    try:
        from app_llm_runner import run_llm, LLMError
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="suno-persona")
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:300]}")

    # JSON 抽出
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        raise HTTPException(500, f"JSON 抽出失敗: {out[:300]}")
    try:
        obj = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(500, f"JSON パース失敗: {e}")
    if not obj.get("prompt"):
        raise HTTPException(500, "prompt フィールドが空です")
    return obj

# ─── API: Finder 操作 ───

class FinderRequest(BaseModel):
    path: str

@router.post("/api/finder/open")
def api_finder_open(req: FinderRequest):
    safe = _safe_user_path(req.path)
    if open_in_finder(str(safe)):
        return {"status": "ok"}
    raise HTTPException(400, f"パスが存在しません: {safe}")


# ─── API: フォルダピッカー（手打ち入力の代替） ───
@router.get("/api/finder/list")
def api_finder_list(path: str = ""):
    """ディレクトリの中身を返す。ピッカー UI 用。
    path が空 or 不正なら HOME を返す。最大 200 件、超過時は truncated:true。
    """
    # ルートリスト（クイックジャンプ用）
    roots = []
    volumes = Path("/Volumes")
    if volumes.exists():
        roots.append({"label": "外部ストレージ", "path": "/Volumes", "icon": ""})
    roots.append({"label": "ホーム", "path": str(HOME), "icon": ""})
    cloud = HOME / "Library" / "CloudStorage"
    if cloud.exists():
        roots.append({"label": "クラウド", "path": str(cloud), "icon": ""})
    # パス解決（空なら HOME）
    target_str = (path or "").strip() or str(HOME)
    try:
        target = _safe_user_path(target_str)
    except HTTPException:
        # 危険パスならホームに戻す
        target = HOME
    if not target.exists() or not target.is_dir():
        target = HOME
    # エントリー列挙
    entries = []
    truncated = False
    try:
        for e in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if e.name.startswith("."):
                continue
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            if len(entries) >= 200:
                truncated = True
                break
            child_count = 0
            try:
                child_count = sum(1 for _ in e.iterdir() if not _.name.startswith("."))
            except (PermissionError, OSError):
                pass
            entries.append({
                "name": e.name,
                "path": str(e),
                "child_count": child_count,
            })
    except PermissionError:
        raise HTTPException(403, f"このフォルダは読み取り権限がありません: {target}")
    parent = str(target.parent) if target.parent != target else None
    return {
        "path": str(target),
        "parent": parent,
        "entries": entries,
        "roots": roots,
        "truncated": truncated,
    }

# /api/finder/create-folder・delete-folder は UI/運用経路とも参照ゼロのため削除（2026-06-10 掃除）

@router.get("/api/finder/browse")
def api_finder_browse(path: str = ""):
    """指定パスの子フォルダ一覧を返す"""
    if not path:
        candidates = list((HOME / "Library/CloudStorage").glob("GoogleDrive-*/共有ドライブ"))
        path = str(candidates[0]) if candidates else str(HOME)
    p = _safe_user_path(path)
    if not p.exists():
        return {"path": str(p), "entries": [], "error": "パスが存在しません"}
    entries = []
    try:
        for child in sorted(p.iterdir()):
            if child.name.startswith('.'):
                continue
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
            })
    except PermissionError:
        return {"path": str(p), "entries": [], "error": "アクセス権限がありません"}
    return {"path": str(p), "parent": str(p.parent), "entries": entries}

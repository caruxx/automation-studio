#!/usr/bin/env python3
"""環境セットアップ

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

# ─── API: 環境セットアップ ───

def _channel_youtube_token_path() -> Optional[Path]:
    """アクティブなチャンネルフォルダ内の `.youtube_token.json` を返す。
    チャンネルフォルダ未設定なら None。app_youtube.py の resolve_token_path と整合。"""
    try:
        cfg = get_dashboard_config()
        ch = (cfg.get("channel_folder") or "").strip()
        if not ch:
            return None
        return Path(ch) / ".youtube_token.json"
    except Exception:
        return None


def _validate_active_youtube_channel(token_path: Optional[Path], *, raise_on_mismatch: bool = False) -> dict:
    """アクティブチャンネル登録情報と OAuth トークンの実チャンネルを照合する。"""
    ch = _active_channel_registry_entry()
    return _validate_youtube_channel_for_entry(
        token_path, ch, fallback_name=get_dashboard_config().get("channel_name", ""),
        raise_on_mismatch=raise_on_mismatch,
    )


def _read_youtube_token_status(token_path: Path) -> dict:
    """指定されたトークンファイルの妥当性を返す（中身は返さない）。"""
    out = {"path": str(token_path), "exists": token_path.exists(),
           "token_valid": False, "scopes": [], "expiry": "",
           "has_readonly": False, "has_refresh_token": False}
    if not token_path.exists():
        return out
    try:
        td = json.loads(token_path.read_text(encoding="utf-8"))
        out["scopes"] = td.get("scopes", [])
        out["expiry"] = td.get("expiry", "")
        out["has_readonly"] = "https://www.googleapis.com/auth/youtube.readonly" in out["scopes"]
        out["has_refresh_token"] = bool(td.get("refresh_token"))
        from datetime import datetime, timezone
        if out["expiry"]:
            exp = datetime.fromisoformat(out["expiry"].replace("Z", "+00:00"))
            out["token_valid"] = exp > datetime.now(timezone.utc)
    except Exception:
        pass
    return out


@router.get("/api/credentials/status")
def api_credentials_status():
    """各外部サービスの認証状態を返す（シークレット値は含まない）"""
    result = {}

    # YouTube OAuth
    yt_secret = CONFIG_DIR / "youtube_client_secret.json"
    legacy_token = CONFIG_DIR / "youtube_token.json"
    channel_token = _channel_youtube_token_path()
    dc = get_dashboard_config()

    # 「アクティブな」トークンはアクティブチャンネルの .youtube_token.json に固定。
    # レガシーグローバルトークンは表示だけ残し、チャンネル別運用では OK 判定に使わない。
    primary = channel_token
    primary_status = _read_youtube_token_status(primary) if primary else {
        "path": "", "exists": False, "token_valid": False,
        "scopes": [], "expiry": "", "has_readonly": False, "has_refresh_token": False
    }
    channel_check = _validate_active_youtube_channel(primary, raise_on_mismatch=False) if primary_status["exists"] else {
        "expected_channel_id": (_active_channel_registry_entry().get("youtube_channel_id") or ""),
        "expected_channel_name": dc.get("channel_name", ""),
        "actual_channel_id": "",
        "actual_channel_title": "",
        "actual_channels": [],
        "verified": False,
        "matched": None,
        "error": "token file not found",
    }

    yt = {
        "client_secret_exists": yt_secret.exists(),
        # 旧 UI 互換のためのフィールド（プライマリの値を入れる）
        "token_exists": primary_status["exists"],
        "token_valid": primary_status["token_valid"],
        "scopes": primary_status["scopes"],
        "expiry": primary_status["expiry"],
        "has_readonly": primary_status["has_readonly"],
        "has_refresh_token": primary_status["has_refresh_token"],
        # 新フィールド: チャンネル別 / レガシーそれぞれの状態
        "active_token_path": primary_status["path"],
        "active_channel_name": dc.get("channel_name", ""),
        "active_channel_folder": dc.get("channel_folder", ""),
        "uses_legacy_fallback": False,
        "channel_verification": channel_check,
        "channel_token": _read_youtube_token_status(channel_token) if channel_token else None,
        "legacy_token": _read_youtube_token_status(legacy_token),
    }
    result["youtube"] = yt

    # Claude CLI
    result["claude_cli"] = {
        "available": bool(shutil.which(get_suno_config().get("claude_cli") or "claude")),
        "command": get_suno_config().get("claude_cli") or "claude",
    }
    result["codex_cli"] = {
        "available": bool(shutil.which(get_suno_config().get("codex_cli") or "codex")),
        "command": get_suno_config().get("codex_cli") or "codex",
    }

    # Discord notification
    result["discord"] = {"configured": DISCORD_CONFIG.exists()}
    result["line"] = {"configured": False, "deprecated": True}

    # SUNO (Playwright profile)
    suno_profile = CONFIG_DIR / "chromium_profile"
    result["suno_browser"] = {"profile_exists": suno_profile.exists()}

    # ffmpeg
    result["ffmpeg"] = {"available": bool(shutil.which("ffmpeg"))}

    # Dashboard config
    result["config"] = {
        "persona_set": bool(dc.get("persona")),
        "rival_channels_count": len(dc.get("rival_channels", [])),
        "channel_folder_exists": Path(dc.get("channel_folder", "")).exists() if dc.get("channel_folder") else False,
    }

    return result


@router.post("/api/credentials/reauth-youtube")
def api_reauth_youtube():
    """YouTube OAuth を再認証。
    アクティブチャンネルのトークン（<channel_folder>/.youtube_token.json）を削除する。
    次の API 呼び出し時にブラウザで OAuth 同意画面が開くので、
    ブランドアカウントのチャンネル選択画面で正しいチャンネルを選ぶこと。"""
    deleted = []
    legacy = CONFIG_DIR / "youtube_token.json"
    channel = _channel_youtube_token_path()
    targets = [channel] if channel is not None else [legacy]
    for p in targets:
        if p is not None and p.exists():
            try:
                p.unlink()
                deleted.append(str(p))
            except Exception as e:
                print(f"[reauth] {p} 削除失敗: {e}")
    msg = "トークンを削除しました。" if deleted else "削除対象のトークンはありませんでした。"
    msg += " 次の YouTube API 呼び出し時にブラウザで再認証が求められます（ブランドアカウントの場合はチャンネル選択画面でアップロード先を選んでください）。"
    return {"status": "ok", "message": msg, "deleted": deleted}


@router.get("/api/setup/status")
def api_setup_status():
    """現在のPC の環境セットアップ状態を確認"""
    checks = {}
    # Python パッケージ
    for pkg in ["fastapi", "uvicorn", "playwright"]:
        try:
            __import__(pkg)
            checks[pkg] = True
        except ImportError:
            checks[pkg] = False
    # Playwright ブラウザ
    pw_cache = HOME / "Library/Caches/ms-playwright"
    checks["chromium"] = pw_cache.exists() and any(pw_cache.glob("chromium*"))
    # 設定ファイル
    checks["suno_config"] = SUNO_CONFIG.exists()
    _suno_cfg = get_suno_config()
    if _suno_cfg.get("provider") in ("claude", "codex"):
        # CLI プロバイダー: APIキー不要、CLIの存在で判定
        cli_key = "codex_cli" if _suno_cfg.get("provider") == "codex" else "claude_cli"
        cli_default = "codex" if _suno_cfg.get("provider") == "codex" else "claude"
        checks["suno_api_key"] = bool(shutil.which(_suno_cfg.get(cli_key) or cli_default))
    else:
        checks["suno_api_key"] = bool(_suno_cfg.get("api_key"))
    checks["youtube_secret"] = (CONFIG_DIR / "youtube_client_secret.json").exists()
    checks["discord_config"] = DISCORD_CONFIG.exists()
    checks["chromium_profile"] = (CONFIG_DIR / "chromium_profile").exists()
    return {"checks": checks}


class InitialSetupRequest(BaseModel):
    system_name: Optional[str] = "Automation Studio"
    discord_webhook_url: Optional[str] = ""
    discord_username: Optional[str] = "Automation Studio"


def _initial_setup_state() -> dict:
    channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    dashboard = load_json(DASHBOARD_CONFIG, {}) if DASHBOARD_CONFIG.exists() else {}
    discord = load_json(DISCORD_CONFIG, {}) if DISCORD_CONFIG.exists() else {}
    return {
        "needs_setup": len(channels) == 0,
        "channels_count": len(channels),
        "channels_path": str(CHANNELS_CONFIG),
        "config_dir": str(CONFIG_DIR),
        "shared_config_dir": str(SHARED_CONFIG_DIR),
        "templates_copied": globals().get("_TEMPLATE_INIT_COPIED", []),
        "system_name": dashboard.get("brand_full") or DEFAULT_DASHBOARD_CONFIG["brand_full"],
        "discord_configured": bool((discord.get("webhook_url") or "").strip()),
        "next_action": "新規チャンネル(かんたん作成) ウィザードでチャンネルを登録してください",
    }


@router.get("/api/setup/initial")
def api_initial_setup_status():
    return _initial_setup_state()


@router.post("/api/setup/initial")
def api_initial_setup_save(req: InitialSetupRequest):
    """初回セットアップの最小保存。

    チャンネル登録そのものは既存の /api/channels + onboarding ウィザードへ委譲する。
    """
    name = (req.system_name or "Automation Studio").strip() or "Automation Studio"
    cfg = load_json(DASHBOARD_CONFIG, {}) if DASHBOARD_CONFIG.exists() else {}
    cfg["brand_short"] = name
    cfg["brand_full"] = name
    DASHBOARD_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    save_json(DASHBOARD_CONFIG, cfg)

    webhook = (req.discord_webhook_url or "").strip()
    username = (req.discord_username or "Automation Studio").strip() or "Automation Studio"
    if webhook:
        if not (webhook.startswith("https://discord.com/api/webhooks/") or webhook.startswith("https://discordapp.com/api/webhooks/")):
            raise HTTPException(400, "Discord Webhook URL の形式ではありません")
        discord = load_json(DISCORD_CONFIG, {}) if DISCORD_CONFIG.exists() else {}
        discord["webhook_url"] = webhook
        discord["username"] = username
        DISCORD_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        save_json(DISCORD_CONFIG, discord)

    return {"status": "ok", **_initial_setup_state()}

@router.post("/api/setup/run")
async def api_setup_run():
    """環境セットアップスクリプトを実行"""
    setup_script = SHARED_BASE / "Python" / "setup.sh"
    if not setup_script.exists():
        raise HTTPException(404, "setup.sh が見つかりません")
    reservation = await _ensure_not_running("setup", "セットアップが既に実行中です")
    task_logs["setup"] = []
    try:
        proc = subprocess.Popen(
            ["bash", str(setup_script)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        _register_active_task("setup", proc, reservation)
    except Exception:
        _release_task_reservation("setup", reservation)
        raise
    _stream_subprocess(proc, "setup", timeout=1800)
    return {"status": "started"}

@router.get("/api/setup/logs")
def api_setup_logs():
    return {"logs": task_logs.get("setup", [])[-50:]}

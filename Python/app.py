#!/usr/bin/env python3
"""orzz. Dashboard — Web管理画面 (共有ドライブ版)"""

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import gzip
import platform
import unicodedata
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from collections import deque

from fastapi import FastAPI, WebSocket, HTTPException, UploadFile, File, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List

# ─── 土台は app_core へ分離（D9 第1段）。foundation シンボルを取り込む ───
import app_core  # noqa: F401  （app_core.X 直接参照用）
from app_core import *  # noqa: F401,F403  土台の全シンボル

# ─── FastAPI ───
app = FastAPI(title=load_json(DASHBOARD_CONFIG, {}).get("brand_full") or DEFAULT_DASHBOARD_CONFIG["brand_full"])

# 静的ファイル
static_dir = WEB_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ─── 認証ミドルウェア（Sprint 5-C） ───
# ORZZ_AUTH_REQUIRED=1 のときのみ有効。loopback アドレスはスキップ。
_AUTH_PUBLIC_PATHS = {"/login.html", "/login", "/api/auth/login", "/api/auth/check",
                       "/manifest.json", "/sw.js", "/favicon.ico"}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not AUTH_REQUIRED:
        return await call_next(request)
    path = request.url.path
    # 公開パス + 静的ファイル + WebSocket は通過
    if path in _AUTH_PUBLIC_PATHS or path.startswith("/static/") or path.startswith("/ws/"):
        return await call_next(request)
    # ローカルホスト（PC 自身からのアクセス）は通過
    client_host = (request.client.host if request.client else "") or ""
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return await call_next(request)
    # トークンチェック
    expected = _get_or_create_auth_token()
    received = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        received = auth[7:].strip()
    if not received:
        received = request.cookies.get("orzz_token", "")
    if not secrets.compare_digest(received, expected):
        # /login.html にリダイレクト（HTML リクエストの場合のみ）
        accept = request.headers.get("accept", "")
        if "text/html" in accept and not path.startswith("/api/"):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/login.html?next={path}", status_code=302)
        return JSONResponse({"detail": "認証が必要です"}, status_code=401)
    return await call_next(request)


# ─── API: 設定 ───
def _mask_secret(val, visible=4):
    """シークレット値をマスク（先頭N文字のみ表示）"""
    if not val or len(val) <= visible:
        return val
    return val[:visible] + "●" * min(8, len(val) - visible)

@app.get("/api/config/migration-status")
def api_config_migration_status():
    """v2 配布化マイグレーション結果。フロントの起動バナー表示で使用。

    Returns:
      {
        performed: bool,           # 今回起動でコピーが走ったか
        src: str,                  # 旧ディレクトリ
        dst: str,                  # 新ディレクトリ
        copied: [...],             # コピーしたファイル相対パス
        skipped: [{path, reason}], # スキップしたもの
        skipped_reason: str,       # 全体スキップ理由（同一/旧無し/既マイグレ済）
        copied_count: int,
        legacy_kept: bool,         # 旧ディレクトリが残っているか（ロールバック用）
        migration_log: str,        # 末尾 5 行の履歴
        app_id: str,               # 解決した app_id
      }
    """
    log_path = Path(_resolve_config_dir()) / "migration.log"
    log_tail = ""
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            log_tail = "\n".join(lines[-5:])
        except Exception:
            pass
    return {
        **_MIGRATION_RESULT,
        "copied_count": len(_MIGRATION_RESULT.get("copied", [])),
        "legacy_kept": _LEGACY_CONFIG_DIR.exists(),
        "migration_log": log_tail,
        "app_id": _resolve_app_id(),
    }


@app.get("/api/config")
def api_get_config():
    dc = get_dashboard_config()
    sc = get_suno_config()
    bc = get_benchmark_config()
    chs = get_channels()
    # API レスポンスではシークレットをマスク
    safe_sc = {**sc}
    if safe_sc.get("api_key"):
        safe_sc["api_key"] = _mask_secret(safe_sc["api_key"])
    safe_dc = {**dc}
    if safe_dc.get("auth_token"):
        safe_dc["auth_token"] = _mask_secret(safe_dc["auth_token"])
    # 他マシンで保存された channel_folder を現マシンへ解決（レスポンス用のみ・保存しない）。
    # get_channels() 側も解決済みを返すので、フロントの c.folder===active.channel_folder が揃う。
    if safe_dc.get("channel_folder"):
        safe_dc["channel_folder"] = _resolve_to_current_host(safe_dc["channel_folder"])
    return {
        "dashboard": safe_dc,
        "suno": safe_sc,
        "benchmark": bc,
        "channels": {
            "active_folder": safe_dc.get("channel_folder", ""),
            "list": chs,
        },
        "meta": {"schema_version": CONFIG_SCHEMA_VERSION},
    }

# ─── 統合 PUT: セクション指定で透過更新 ───
class UnifiedConfigUpdate(BaseModel):
    section: str  # "dashboard" | "suno" | "benchmark"
    patch: dict

@app.put("/api/config")
def api_put_unified_config(update: UnifiedConfigUpdate):
    section = (update.section or "").strip().lower()
    patch = update.patch or {}
    if section == "dashboard":
        cfg = get_dashboard_config()
        cfg.update(patch)
        save_dashboard_config_smart(cfg)
        return {"status": "ok", "section": section, "config": cfg}
    if section == "suno":
        save_suno_config_smart(patch)
        return {"status": "ok", "section": section, "config": get_suno_config()}
    if section == "benchmark":
        cfg = get_benchmark_config()
        # filter は浅マージ
        if "filter" in patch and isinstance(patch["filter"], dict):
            cfg["filter"] = {**cfg.get("filter", {}), **patch["filter"]}
            patch = {k: v for k, v in patch.items() if k != "filter"}
        cfg.update(patch)
        save_benchmark_config(cfg)
        return {"status": "ok", "section": section, "config": cfg}
    raise HTTPException(400, f"未知の section: {section}")

# ─── API: 自動化設定（チャンネル別） ───

def _automation_config_path() -> Path:
    """現行チャンネルの自動化設定ファイル。チャンネルフォルダ直下に保存"""
    cfg = get_dashboard_config()
    ch = Path(cfg.get("channel_folder") or "")
    if not ch or not ch.exists():
        # フォールバック: ~/.config/orzz/automation_default.json
        return CONFIG_DIR / "automation_default.json"
    return ch / "_automation.json"

@app.get("/api/automation/config")
def api_get_automation_config():
    p = _automation_config_path()
    if not p.exists():
        return {"config": {}, "path": str(p)}
    try:
        return {"config": json.loads(p.read_text(encoding="utf-8")), "path": str(p)}
    except Exception as e:
        raise HTTPException(500, f"読み込み失敗: {e}")

class AutomationConfigUpdate(BaseModel):
    suno_prompt: Optional[str] = None
    suno_provider: Optional[str] = None
    suno_mode: Optional[str] = None
    suno_count: Optional[int] = None
    suno_interval: Optional[int] = None
    suno_batch: Optional[bool] = None
    suno_headless: Optional[bool] = None
    suno_diversity_threshold: Optional[float] = None
    suno_diversity_retry: Optional[int] = None
    suno_history_limit: Optional[int] = None
    dl_wait_sec: Optional[int] = None
    premiere_duration: Optional[int] = None
    premiere_auto_export: Optional[bool] = None
    meta_title_count: Optional[int] = None
    upload_privacy: Optional[str] = None
    auto_upload: Optional[bool] = None
    thumb_provider: Optional[str] = None
    thumb_prompt: Optional[str] = None
    thumb_auto: Optional[bool] = None
    auto_approval_mode: Optional[bool] = None  # ON: YouTube upload まで無停止で実行

@app.put("/api/automation/config")
def api_put_automation_config(req: AutomationConfigUpdate):
    p = _automation_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = req.dict(exclude_none=True)
    p.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "config": body, "path": str(p)}


@app.get("/api/config/auto-detect")
def api_auto_detect():
    """現在のPCの共有ドライブからパスを自動検出"""
    detected = auto_detect_paths()
    return {
        "channel_folder": detected.get("channel_folder", ""),
        "yt_root": detected.get("yt_root", ""),
        "shared_drive": str(SHARED_BASE),
        "config_dir": str(CONFIG_DIR),
    }

@app.post("/api/config/init")
def api_config_init():
    """設定が未初期化なら自動検出値で初期化"""
    config = get_dashboard_config()
    changed = False
    detected = auto_detect_paths()

    if not config.get("channel_folder") and detected.get("channel_folder"):
        config["channel_folder"] = detected["channel_folder"]
        changed = True

    if changed:
        save_dashboard_config_smart(config)
    return {"status": "ok", "config": config, "changed": changed}

class DashboardConfigUpdate(BaseModel):
    brand_short: Optional[str] = None
    brand_full: Optional[str] = None
    app_id: Optional[str] = None
    file_prefix: Optional[str] = None
    channel_name: Optional[str] = None
    channel_folder: Optional[str] = None
    export_path: Optional[str] = None
    persona: Optional[str] = None
    rival_channels: Optional[List[str]] = None
    spreadsheet_channel_detail_url: Optional[str] = None
    spreadsheet_growth_tracking_url: Optional[str] = None
    youtube_api_key: Optional[str] = None
    sheets_api_key: Optional[str] = None
    auth_token: Optional[str] = None
    template_prproj: Optional[str] = None
    template_psd: Optional[str] = None
    psd_base_layer: Optional[str] = None
    psd_toggle_layer: Optional[str] = None
    psd_image_subdir: Optional[str] = None
    publish_mode: Optional[str] = None  # P3-3: unlisted / public / delayed
    publish_delay_hours: Optional[float] = None  # P2-7: upload 後 N 時間で public 化（0=即時）
    reference_image_dir: Optional[str] = None    # step_bgimage: 参照画像フォルダ（空文字でクリア）
    reference_image: Optional[str] = None        # step_bgimage: 固定参照画像（空文字でクリア）
    default_duration_sec: Optional[int] = None   # Premiere 自動配置の規定尺（秒）。0/空でクリア（10800 にフォールバック）

@app.put("/api/config/dashboard")
def api_update_dashboard_config(update: DashboardConfigUpdate):
    config = get_dashboard_config()
    # スプシ URL は CSV エクスポート形式へ自動正規化（/edit?gid=N → /gviz/tq?tqx=out:csv&gid=N）
    try:
        from app_sheets import normalize_sheet_url as _norm_sheet
    except Exception:
        _norm_sheet = lambda x: x
    for k, v in update.dict(exclude_none=True).items():
        if k == "file_prefix":
            config[k] = sanitize_file_prefix(v)
        elif k in ("spreadsheet_channel_detail_url", "spreadsheet_growth_tracking_url"):
            config[k] = _norm_sheet(v) if v else v
        elif k == "reference_image_dir":
            # step_bgimage: 参照画像フォルダ
            #   - 空文字 → フィールド削除（Picked → rival thumbs フォールバックに戻す）
            #   - `~` を展開して保存
            #   - 存在しなければ warning のみ（後から作るユースケースを許容）
            raw = (v or "").strip()
            if not raw:
                config["reference_image_dir"] = ""  # 空文字を保存（save_dashboard_config_smart 経由で per-channel に空が書かれる）
            else:
                expanded = str(Path(raw).expanduser())
                config["reference_image_dir"] = expanded
                try:
                    if not Path(expanded).is_dir():
                        print(f"⚠ reference_image_dir 保存: ディレクトリが存在しません ({expanded}) — 後から作成すれば step_bgimage で参照されます")
                except Exception:
                    pass
        elif k == "reference_image":
            # step_bgimage: 固定参照画像。指定時は reference_image_dir のランダム選択より優先。
            raw = (v or "").strip()
            if not raw:
                config["reference_image"] = ""
            else:
                expanded = str(Path(raw).expanduser())
                config["reference_image"] = expanded
                try:
                    if not Path(expanded).is_file():
                        print(f"⚠ reference_image 保存: ファイルが存在しません ({expanded}) — 後から作成すれば step_bgimage で参照されます")
                except Exception:
                    pass
        elif k == "default_duration_sec":
            # Premiere 自動配置の規定尺（秒）。
            #   - 正の整数 → そのまま保存（per-channel）
            #   - 0 / 負 / 数値化失敗 → per-channel から削除（read 時に 10800 へフォールバック）
            try:
                iv = int(v) if v not in (None, "") else 0
            except (TypeError, ValueError):
                iv = 0
            if iv > 0:
                config["default_duration_sec"] = iv
            else:
                config.pop("default_duration_sec", None)
                # save_dashboard_config_smart は「キーを送らない＝変更しない」なので、
                # 永続的に削除するには per-channel ファイルを直接書き換える必要がある。
                try:
                    cc = load_channel_config()
                    if "default_duration_sec" in cc:
                        cc.pop("default_duration_sec", None)
                        save_channel_config(cc)
                except Exception as _e:
                    print(f"⚠ default_duration_sec 削除に失敗: {_e}")
        else:
            config[k] = v
    save_dashboard_config_smart(config)
    if update.file_prefix is not None and config.get("channel_folder"):
        channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
        changed = False
        for ch in channels:
            if ch.get("folder") == config.get("channel_folder"):
                ch["prefix"] = config["file_prefix"]
                changed = True
        if changed:
            save_json(CHANNELS_CONFIG, channels)
    return {"status": "ok", "config": config}


@app.get("/api/config/infer-naming")
def api_infer_naming(folder: str = ""):
    config = get_dashboard_config()
    channel_dir = Path(folder or config.get("channel_folder") or "")
    if not channel_dir.exists():
        raise HTTPException(400, "チャンネルフォルダが存在しません")
    examples = []
    counts: dict[str, int] = {}
    max_num = 0
    for d in sorted(channel_dir.iterdir()):
        if not d.is_dir():
            continue
        info = parse_video_folder_name(d.name)
        if not info:
            continue
        counts[info["prefix"]] = counts.get(info["prefix"], 0) + 1
        max_num = max(max_num, info["num"])
        if len(examples) < 8:
            examples.append(d.name)
    prefix = infer_file_prefix_from_folder(channel_dir)
    return {
        "prefix": prefix,
        "counts": counts,
        "examples": examples,
        "max_num": max_num,
        "next_num": max_num + 1,
        "matched": sum(counts.values()),
    }

class SunoConfigUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    claude_cli: Optional[str] = None
    codex_cli: Optional[str] = None
    generation_mode: Optional[str] = None
    prompt: Optional[str] = None
    loop_count: Optional[int] = None
    loop_interval_sec: Optional[int] = None
    loop_batch: Optional[bool] = None

@app.put("/api/config/suno")
def api_update_suno_config(update: SunoConfigUpdate):
    patch = {k: v for k, v in update.dict(exclude_none=True).items()}
    save_suno_config_smart(patch)
    return {"status": "ok", "config": get_suno_config()}

# ─── API: ベンチマーク設定（チャンネル横断） ───

@app.get("/api/benchmark/config")
def api_get_benchmark_config():
    return {"config": get_benchmark_config()}

class BenchmarkFilterPatch(BaseModel):
    top_n: Optional[int] = None
    min_subs: Optional[int] = None
    max_subs: Optional[int] = None
    exclude_names: Optional[List[str]] = None

class BenchmarkConfigUpdate(BaseModel):
    pinned_names: Optional[List[str]] = None
    filter: Optional[BenchmarkFilterPatch] = None
    spreadsheet_channel_detail_url: Optional[str] = None
    spreadsheet_growth_tracking_url: Optional[str] = None

@app.put("/api/benchmark/config")
def api_update_benchmark_config(update: BenchmarkConfigUpdate):
    cfg = get_benchmark_config()
    body = update.dict(exclude_none=True)
    if "filter" in body:
        cfg["filter"] = {**cfg.get("filter", {}), **body.pop("filter")}
    cfg.update(body)
    save_benchmark_config(cfg)
    return {"status": "ok", "config": cfg}

# ─── API: マスター設定（プロンプト + 詳細パラメータの一元管理） ───

MASTER_PROMPTS_FILE = CONFIG_DIR / "master_prompts.json"
# プロンプトキー一覧と「上書き対象のスクリプト」のメモ
MASTER_PROMPT_KEYS = {
    "title_generation":      "claude_proposer.py の _TITLES_PROMPT を上書き",
    "description_generation": "claude_proposer.py の _DESCRIPTION_PROMPT を上書き",
    "tags_generation":       "claude_proposer.py の _TAGS_PROMPT を上書き",
    "competitor_analysis":   "app_competitor.py analyze_with_claude のシステム指示を上書き",
    "suno_from_analysis":    "app_competitor.py propose_suno_prompt の指示を上書き",
    "suno_from_persona":     "app.py /api/suno/suggest-prompt のプロンプトを上書き",
    "imitate_evolve":        "Sprint 5-A 「徹底パクリ進化」分析（imitate / avoid / evolve 3 軸）",
}

def get_master_prompts():
    """ユーザーが上書きしたプロンプト群。チャンネル別 → グローバル の順でフォールバック。
    空キーはハードコード（claude_proposer.py 内の既定）にフォールバック。"""
    glob = load_json(MASTER_PROMPTS_FILE, {}) or {}
    cc = load_channel_config()
    cc_prompts = cc.get("master_prompts") or {}
    return {**glob, **cc_prompts}

def save_master_prompts(data):
    """master_prompts はアクティブチャンネル別に保存。
    アクティブチャンネルが無いときはグローバルへフォールバック。"""
    if _channel_config_path():
        cc = load_channel_config()
        cc["master_prompts"] = data
        save_channel_config(cc)
    else:
        save_json(MASTER_PROMPTS_FILE, data)

@app.get("/api/master")
def api_get_master():
    """マスター設定 = 全部統合返却。既存の分離ファイルもそのまま含める。"""
    return {
        "prompts": {
            "values": get_master_prompts(),
            "keys": MASTER_PROMPT_KEYS,
        },
        "suno": get_suno_config(),
        "benchmark": get_benchmark_config(),
        "export": get_export_rules(),
        "channels": get_channels(),
        "dashboard": get_dashboard_config(),
        "meta": {"schema_version": CONFIG_SCHEMA_VERSION},
    }

class MasterUpdate(BaseModel):
    section: str  # "prompts" | "suno" | "benchmark" | "export"
    patch: dict

@app.put("/api/master")
def api_put_master(update: MasterUpdate):
    section = (update.section or "").strip()
    patch = update.patch or {}
    if section == "prompts":
        cur = get_master_prompts()
        cur.update(patch)
        # 空文字列は削除扱い → ハードコードフォールバック
        cur = {k: v for k, v in cur.items() if v}
        save_master_prompts(cur)
        return {"status": "ok", "section": section, "config": cur}
    if section == "suno":
        cfg = get_suno_config(); cfg.update(patch); save_json(SUNO_CONFIG, cfg)
        return {"status": "ok", "section": section, "config": cfg}
    if section == "benchmark":
        cfg = get_benchmark_config()
        if "filter" in patch and isinstance(patch["filter"], dict):
            cfg["filter"] = {**cfg.get("filter", {}), **patch.pop("filter")}
        cfg.update(patch); save_benchmark_config(cfg)
        return {"status": "ok", "section": section, "config": cfg}
    if section == "export":
        cfg = get_export_rules()
        if "rules" in patch and isinstance(patch["rules"], dict):
            cfg["rules"] = {**cfg.get("rules", {}), **patch.pop("rules")}
        cfg.update(patch); save_export_rules(cfg)
        return {"status": "ok", "section": section, "config": cfg}
    if section == "remote":
        # D12: tunnel_url は dashboard_config.json に移送（master_settings 廃止）。
        # 原文を直接 load/save（get_dashboard_config の channel_folder 解決値を保存しないため）。
        cur = load_json(DASHBOARD_CONFIG, {})
        cur["tunnel_url"] = (patch.get("tunnel_url") or "").strip()
        save_json(DASHBOARD_CONFIG, cur)
        return {"status": "ok", "section": section, "config": {"tunnel_url": cur["tunnel_url"]}}
    raise HTTPException(400, f"未知の section: {section}")

@app.get("/api/master/export")
def api_master_export():
    """全設定の JSON ダンプ。バックアップ・別 PC への移行用。"""
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "dashboard": get_dashboard_config(),
        "suno": get_suno_config(),
        "benchmark": get_benchmark_config(),
        "channels": get_channels(),
        "master_prompts": get_master_prompts(),
        "export_rules": get_export_rules(),
    }

class MasterImportRequest(BaseModel):
    data: dict
    overwrite: bool = False  # True なら既存ファイルを上書き、False なら patch マージ

@app.post("/api/master/import")
def api_master_import(req: MasterImportRequest):
    d = req.data or {}
    if "schema_version" not in d:
        raise HTTPException(400, "schema_version が無いインポートデータです")
    sections_applied = []
    if "dashboard" in d:
        cfg = d["dashboard"] if req.overwrite else {**get_dashboard_config(), **d["dashboard"]}
        save_dashboard_config_smart(cfg); sections_applied.append("dashboard")
    if "suno" in d:
        cfg = d["suno"] if req.overwrite else {**get_suno_config(), **d["suno"]}
        save_suno_config_smart(cfg); sections_applied.append("suno")
    if "benchmark" in d:
        cfg = d["benchmark"] if req.overwrite else {**get_benchmark_config(), **d["benchmark"]}
        save_benchmark_config(cfg); sections_applied.append("benchmark")
    if "master_prompts" in d:
        save_master_prompts(d["master_prompts"]); sections_applied.append("master_prompts")
    if "export_rules" in d:
        save_export_rules(d["export_rules"]); sections_applied.append("export_rules")
    return {"status": "ok", "applied": sections_applied}


# ─── API: SUNO プロンプト雛形提案（ペルソナ起点） ───

class SunoSuggestPromptRequest(BaseModel):
    persona: Optional[str] = None
    style_hint: Optional[str] = None

@app.post("/api/suno/suggest-prompt")
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

@app.post("/api/finder/open")
def api_finder_open(req: FinderRequest):
    safe = _safe_user_path(req.path)
    if open_in_finder(str(safe)):
        return {"status": "ok"}
    raise HTTPException(400, f"パスが存在しません: {safe}")


# ─── API: フォルダピッカー（手打ち入力の代替） ───
@app.get("/api/finder/list")
def api_finder_list(path: str = ""):
    """ディレクトリの中身を返す。ピッカー UI 用。
    path が空 or 不正なら HOME を返す。最大 200 件、超過時は truncated:true。
    """
    # ルートリスト（クイックジャンプ用）
    roots = []
    volumes = Path("/Volumes")
    if volumes.exists():
        roots.append({"label": "外部ストレージ", "path": "/Volumes", "icon": "💾"})
    roots.append({"label": "ホーム", "path": str(HOME), "icon": "🏠"})
    cloud = HOME / "Library" / "CloudStorage"
    if cloud.exists():
        roots.append({"label": "クラウド", "path": str(cloud), "icon": "☁"})
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

class FolderCreateRequest(BaseModel):
    path: str
    open_after: bool = True

@app.post("/api/finder/create-folder")
def api_finder_create_folder(req: FolderCreateRequest):
    p = _safe_user_path(req.path)
    p.mkdir(parents=True, exist_ok=True)
    if req.open_after:
        open_in_finder(p)
    return {"status": "ok", "path": str(p), "exists": p.exists()}

class FolderDeleteRequest(BaseModel):
    path: str
    confirm_name: str  # 安全のためフォルダ名を再入力

@app.post("/api/finder/delete-folder")
def api_finder_delete_folder(req: FolderDeleteRequest):
    p = _safe_user_path(req.path)
    if not p.exists():
        raise HTTPException(404, "フォルダが存在しません")
    if p.name != req.confirm_name:
        raise HTTPException(400, "フォルダ名が一致しません（安全確認）")
    children = list(p.iterdir()) if p.is_dir() else []
    if len(children) > 0:
        raise HTTPException(400, f"フォルダが空ではありません（{len(children)}件）。先に中身を削除してください。")
    p.rmdir()
    return {"status": "ok"}

@app.get("/api/finder/browse")
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

# ─── API: SUNO ───
class SunoRunRequest(BaseModel):
    prompt: Optional[str] = None
    count: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    interval: Optional[int] = None   # ループ間隔（秒）
    workspace: Optional[str] = None  # 直接指定
    video_name: Optional[str] = None # 指定時は {channel}_vol{N} を自動生成
    batch: Optional[bool] = False    # Claude CLI 一括生成モード
    generation_mode: Optional[str] = None  # styles_title_only / lyrics_styles / lyrics
    headless: Optional[bool] = None
    # フロント「⚡ 生成+DL+整理」用（app.py は直接消費しないが forward 互換のため受理）
    dl_wait_sec: Optional[int] = None
    dl_min_duration: Optional[int] = None
    auto_download: Optional[bool] = None
    # 多様性制御（チャンネル別に上書き可能。未指定なら suno_auto_create 側の既定/設定を使用）
    diversity_threshold: Optional[float] = None
    diversity_retry: Optional[int] = None
    history_limit: Optional[int] = None
    # 合意済みドラフト投入経路: ここに [{title,styles,lyrics,mode}, ...] を渡すと
    # LLM 再生成をスキップし、そのまま SUNO に投入する（--songs-file 起動）。
    songs_draft_json: Optional[List[dict]] = None


# ─── 設定→suno_auto_create.generate_content_batch 用 settings 組み立て ───
def _build_suno_batch_settings(prompt: str, generation_mode: Optional[str],
                               provider: Optional[str], video_name: Optional[str],
                               workspace: Optional[str],
                               diversity_threshold: Optional[float],
                               diversity_retry: Optional[int],
                               history_limit: Optional[int]) -> dict:
    """ドラフト一括生成（generate_content_batch）に渡す settings を、
    既存 suno 設定（get_suno_config / .app_channel_config.json）に倣って組み立てる。
    機密キー（api_key / claude_cli / codex_cli）はグローバル設定から引き継ぐ。
    """
    sc = get_suno_config()
    settings = {
        "provider": (provider or sc.get("provider") or "claude").strip(),
        "model": sc.get("model") or "",
        "api_key": sc.get("api_key") or "",
        "claude_cli": sc.get("claude_cli") or "claude",
        "codex_cli": sc.get("codex_cli") or "codex",
        "generation_mode": (generation_mode or sc.get("generation_mode") or "styles_title_only"),
        "prompt": prompt,
    }
    # 多様性パラメータ（明示があれば採用、なければチャンネル設定→suno_auto_create 既定）
    settings["diversity_threshold"] = (
        diversity_threshold if diversity_threshold is not None else sc.get("diversity_threshold")
    )
    settings["diversity_retry"] = (
        diversity_retry if diversity_retry is not None else sc.get("diversity_retry")
    )
    settings["history_limit"] = (
        history_limit if history_limit is not None else sc.get("history_limit")
    )
    # 履歴キー解決のヒント（workspace / video_name → channels.json と照合）
    if workspace:
        settings["workspace"] = workspace
    if video_name:
        settings["video_name"] = video_name
    return settings


class SunoDraftRequest(BaseModel):
    prompt: Optional[str] = None
    count: Optional[int] = None
    generation_mode: Optional[str] = None
    provider: Optional[str] = None
    video_name: Optional[str] = None
    workspace: Optional[str] = None
    diversity_threshold: Optional[float] = None
    diversity_retry: Optional[int] = None
    history_limit: Optional[int] = None


@app.post("/api/suno/generate-drafts")
async def api_suno_generate_drafts(req: SunoDraftRequest):
    """SUNO を起動せず LLM だけで N 曲分のドラフト（title/styles/lyrics/mode）を返す。
    人間が WEB で確認・編集 → /api/suno/start に songs_draft_json で投入する前段。
    """
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(400, "prompt が空です。ドラフト生成前にプロンプト本文を明示してください。")
    if len(prompt) < 10:
        raise HTTPException(400, f"prompt が短すぎます（{len(prompt)} chars）。フルテキストを送信してください。")
    count = max(1, min(int(req.count or 5), 50))

    settings = _build_suno_batch_settings(
        prompt=prompt,
        generation_mode=req.generation_mode,
        provider=req.provider,
        video_name=req.video_name,
        workspace=req.workspace,
        diversity_threshold=req.diversity_threshold,
        diversity_retry=req.diversity_retry,
        history_limit=req.history_limit,
    )
    # batch 生成は Claude/Codex CLI provider のみ対応（generate_content_batch の制約）
    if settings["provider"] not in ("claude", "codex"):
        raise HTTPException(
            400,
            f"ドラフト一括生成は Claude / Codex CLI のみ対応です（指定: {settings['provider']}）。"
            "プロバイダーを Claude または Codex に切り替えてください。",
        )

    def _run():
        sys.path.insert(0, str(SUNO_SCRIPT.parent))
        import suno_auto_create as _suno
        return _suno.generate_content_batch(settings, count)

    try:
        songs = await asyncio.to_thread(_run)
    except Exception as e:
        raise HTTPException(500, f"ドラフト生成に失敗しました: {e}")
    return {
        "status": "ok",
        "count": len(songs),
        "generation_mode": settings["generation_mode"],
        "provider": settings["provider"],
        "songs": songs,
    }


@app.post("/api/suno/start")
async def api_suno_start(req: SunoRunRequest):
    await _ensure_not_running("suno", "SUNO は既に実行中です")
    # ─── プロンプト確認ロジック ───
    # 暗黙のフォールバック（チャンネル設定 / prompts_library の暗黙選択 / 既定文字列）
    # でジャンル違いの曲が作られる事故を防ぐため、prompt は呼び出し側で必ず明示する。
    # vol.6→vol.8 で実際に「ジャズハウス」プロンプトが勝手に選ばれて作り直しになった。
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(
            400,
            "prompt が空です。SUNO 起動前に必ずプロンプト本文を明示してください "
            "（暗黙のチャンネル設定フォールバックは廃止しました）。"
        )
    if len(prompt) < 10:
        raise HTTPException(
            400,
            f"prompt が短すぎます（{len(prompt)} chars）。"
            "意図したプロンプトか確認のうえ、フルテキストを送信してください。"
        )
    cmd = [sys.executable, "-u", str(SUNO_SCRIPT)]  # -u: unbuffered
    cmd += ["--prompt", prompt]
    # ─── 合意済みドラフト投入経路（--songs-file）───
    # songs_draft_json があれば LLM 再生成をスキップし、確認済みドラフトをそのまま投入。
    # 一時 JSON に書き出して suno_auto_create.py を --songs-file で起動する。
    songs_file_path = None
    if req.songs_draft_json:
        if not isinstance(req.songs_draft_json, list) or not req.songs_draft_json:
            raise HTTPException(400, "songs_draft_json が空です。投入するドラフトを配列で渡してください。")
        # title / styles のどちらか欠けた行は不正。lyrics は instrumental 系で空でも可。
        cleaned = []
        for i, s in enumerate(req.songs_draft_json):
            if not isinstance(s, dict):
                raise HTTPException(400, f"songs_draft_json[{i}] が dict ではありません。")
            title = str(s.get("title", "")).strip()
            styles = str(s.get("styles", "")).strip()
            if not title and not styles:
                raise HTTPException(400, f"songs_draft_json[{i}] は title / styles の両方が空です。")
            cleaned.append({
                "title": title,
                "styles": styles,
                "lyrics": str(s.get("lyrics", "")).strip(),
                "mode": str(s.get("mode", "") or (req.generation_mode or "")).strip(),
            })
        import tempfile as _tempfile
        fd, songs_file_path = _tempfile.mkstemp(prefix="suno_draft_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
        cmd += ["--songs-file", songs_file_path]
        # --songs-file は loop_count を配列長から決めるので --count は渡さない
    elif req.count:
        cmd += ["--count", str(req.count)]
    if req.interval: cmd += ["--interval", str(req.interval)]
    if req.provider: cmd += ["--provider", req.provider]
    if req.model: cmd += ["--model", req.model]

    # Workspace 名の解決
    workspace = req.workspace
    if not workspace and req.video_name:
        config = get_dashboard_config()
        channel_name = (config.get("channel_name") or "orzz").strip()
        # チャンネル名を英数字+アンダースコアに正規化（SUNO の制約に優しく）
        channel_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", channel_name).strip("_") or "orzz"
        m = re.match(r"^(\d+)_", req.video_name)
        vol = m.group(1) if m else ""
        workspace = f"{channel_slug}_vol{vol}" if vol else channel_slug
    if workspace:
        cmd += ["--workspace", workspace]
    # 合意済みドラフト投入時は LLM 生成自体を行わないので --batch は無意味（付けない）
    if req.batch and not songs_file_path:
        cmd += ["--batch"]
    if req.generation_mode:
        cmd += ["--mode", req.generation_mode]
    if req.headless:
        cmd += ["--headless"]
    # 多様性制御（チャンネル別。フォームで指定があれば CLI に渡す）
    if req.diversity_threshold is not None:
        cmd += ["--diversity-threshold", str(req.diversity_threshold)]
    if req.diversity_retry is not None:
        cmd += ["--diversity-retry", str(req.diversity_retry)]
    if req.history_limit is not None:
        cmd += ["--history-limit", str(req.history_limit)]
    # 起動時ログ先頭に prompt 全文を出して、後追いでも何で作ったか確認できるようにする
    task_logs["suno"] = [
        "─" * 60,
        f"▶ SUNO 起動 prompt 確認（length={len(prompt)} chars）:",
        prompt,
        "─" * 60,
    ]
    if songs_file_path:
        task_logs["suno"].insert(
            1, f"🎯 合意済みドラフト {len(req.songs_draft_json)} 曲を投入（--songs-file、LLM生成スキップ）"
        )
    import datetime as _dt
    task_meta["suno"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "workspace": workspace or "",
        "count": len(req.songs_draft_json) if songs_file_path else req.count,
        "interval": req.interval,
        "video_name": req.video_name or "",
        "prompt": prompt,  # 採用 prompt を meta に保存（後追い監査用）
        "prompt_length": len(prompt),
        "songs_file": songs_file_path or "",  # ドラフト投入時のみ
    }
    # Cloudflare Bot 判定対策: 既定で APP_KEEP_BROWSER=1 を立てる（vol.7 で実証、vol.6 で再発）
    # 明示的に "0" が立っていればユーザー意図を尊重して上書きしない
    suno_env = {**os.environ}
    if suno_env.get("APP_KEEP_BROWSER", "").strip() not in ("0", "false", "no"):
        suno_env["APP_KEEP_BROWSER"] = "1"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=suno_env)
    active_tasks["suno"] = proc
    _stream_subprocess(proc, "suno")
    return {"status": "started"}

class SunoDownloadRequest(BaseModel):
    workspace: Optional[str] = None
    video_name: Optional[str] = None   # 指定時は {channel}_vol{N} を自動計算 + フォルダの original_music へ保存
    target_dir: Optional[str] = None   # 明示保存先（指定されれば優先）

@app.post("/api/suno/download")
async def api_suno_download(req: SunoDownloadRequest):
    await _ensure_not_running("suno", "SUNO 生成が実行中のためダウンロードできません")

    # workspace 名の解決
    workspace = req.workspace
    video_folder = None
    if req.video_name:
        config = get_dashboard_config()
        video_folder = Path(config["channel_folder"]) / req.video_name
        if not workspace:
            channel_name = (config.get("channel_name") or "orzz").strip()
            channel_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", channel_name).strip("_") or "orzz"
            m = re.match(r"^(\d+)_", req.video_name)
            vol = m.group(1) if m else ""
            workspace = f"{channel_slug}_vol{vol}" if vol else channel_slug
    if not workspace:
        raise HTTPException(400, "workspace または video_name が必要です")

    # 保存先: target_dir > 動画フォルダ直下 > ~/Downloads/suno_<workspace>
    if req.target_dir:
        target_dir = req.target_dir
    elif video_folder:
        target_dir = str(video_folder)  # フォルダ直下に展開
    else:
        target_dir = str(Path.home() / "Downloads" / f"suno_{workspace}")

    cmd = [sys.executable, "-u", str(SUNO_SCRIPT),
           "--download-workspace", workspace,
           "--download-dir", target_dir]
    task_logs["suno"] = []
    import datetime as _dt
    task_meta["suno"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "workspace": workspace,
        "mode": "download",
        "target_dir": target_dir,
        "video_name": req.video_name or "",
    }
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["suno"] = proc
    _stream_subprocess(proc, "suno")
    return {"status": "started", "workspace": workspace, "target_dir": target_dir}

@app.post("/api/suno/stop")
def api_suno_stop():
    proc = active_tasks.get("suno")
    if proc and proc.returncode is None:
        proc.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}

def _parse_suno_progress(logs):
    """SUNOログから進捗を抽出: {current, total, next_wait_sec, next_remaining_sec, phase}"""
    progress = {"current": 0, "total": 0, "next_wait_sec": 0,
                "next_remaining_sec": 0, "phase": ""}
    for line in logs:
        # 曲 3/5
        m = re.search(r"曲\s+(\d+)/(\d+)", line)
        if m:
            progress["current"] = int(m.group(1))
            progress["total"] = int(m.group(2))
            progress["phase"] = "generating"
        # 次の生成まで 180 秒
        m = re.search(r"次の生成まで\s+(\d+)\s+秒", line)
        if m:
            progress["next_wait_sec"] = int(m.group(1))
            progress["phase"] = "waiting"
        # 残り 150 秒
        m = re.search(r"残り\s+(\d+)\s+秒", line)
        if m:
            progress["next_remaining_sec"] = int(m.group(1))
        # LLM呼び出し / Claude CLI 呼び出し
        if "LLM呼び出し中" in line or "Claude CLI 呼び出し中" in line:
            progress["phase"] = "llm"
        if "Create ボタンをクリック" in line:
            progress["phase"] = "submitted"
        if "[完了]" in line:
            progress["phase"] = "done"
    return progress


@app.get("/api/suno/status")
def api_suno_status():
    proc = active_tasks.get("suno")
    running = proc is not None and proc.returncode is None
    logs = task_logs.get("suno", [])
    progress = _parse_suno_progress(logs)
    meta = task_meta.get("suno", {})
    return {
        "running": running,
        "logs": logs[-50:],
        "progress": progress,
        "started_at": meta.get("started_at"),
        "meta": meta,
    }

# ─── API: チャンネル ───

# YouTube アイコン取得 TTL（秒）
ICON_CACHE_TTL_SEC = 24 * 60 * 60

def _resolve_youtube_channel_meta(url_or_id: str) -> dict:
    """YouTube URL/handle/channelId から {channel_id, name, handle, icon_url, subscribers, url} を返す。
    YouTube Data API v3 の channels.list を使用。
    """
    cfg = get_dashboard_config()
    api_key = (cfg.get("youtube_api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "YouTube Data API Key が未設定です（設定タブで入力してください）")
    import urllib.request, urllib.parse
    s = (url_or_id or "").strip()
    if not s:
        raise HTTPException(400, "URL が空です")

    cid = None
    handle = ""
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", s)
    if m:
        cid = m.group(1)
    elif s.startswith("UC") and len(s) >= 20:
        cid = s
    else:
        h = re.search(r"@([\w\-.]+)", s)
        if h:
            handle = "@" + h.group(1)
            qs = urllib.parse.urlencode({"part": "id", "forHandle": handle, "key": api_key})
            try:
                with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/channels?{qs}", timeout=15) as r:
                    d = json.loads(r.read())
                if d.get("items"):
                    cid = d["items"][0]["id"]
            except Exception as e:
                raise HTTPException(502, f"YouTube API エラー (handle): {e}")
    if not cid:
        raise HTTPException(400, f"channelId を解決できません: {s}")

    qs = urllib.parse.urlencode({"part": "snippet,statistics", "id": cid, "key": api_key})
    try:
        with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/channels?{qs}", timeout=15) as r:
            d = json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"YouTube API エラー: {e}")
    items = d.get("items") or []
    if not items:
        raise HTTPException(404, f"チャンネルが見つかりません: {cid}")
    sn = items[0].get("snippet", {})
    stat = items[0].get("statistics", {})
    thumbs = sn.get("thumbnails", {})
    icon_url = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    return {
        "channel_id": cid,
        "name": sn.get("title", ""),
        "handle": sn.get("customUrl", "") or handle,
        "icon_url": icon_url,
        "subscribers": int(stat.get("subscriberCount", 0)) if stat.get("subscriberCount") else 0,
        "url": f"https://www.youtube.com/channel/{cid}",
    }


def _refresh_channel_icon_if_stale(ch: dict) -> dict:
    """channels.json の 1 要素について、icon_cache が空 or 24h 超なら refresh.
    YouTube Data API Key が未設定でも、YouTube ページからアイコンだけスクレイプして補完する。"""
    cache = ch.get("icon_cache") or {}
    fetched_at = cache.get("fetched_at") or ""
    is_stale = True
    if fetched_at:
        try:
            dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            age = (datetime.now(dt.tzinfo) - dt).total_seconds()
            is_stale = age > ICON_CACHE_TTL_SEC
        except Exception:
            is_stale = True
    if not is_stale and cache.get("url"):
        return ch
    yt_url = ch.get("youtube_url") or ch.get("youtube_channel_id") or ""
    if not yt_url:
        return ch
    try:
        meta = _resolve_youtube_channel_meta(yt_url)
        ch["icon_cache"] = {"url": meta.get("icon_url", ""), "fetched_at": datetime.utcnow().isoformat() + "Z"}
        ch["youtube_channel_id"] = meta.get("channel_id") or ch.get("youtube_channel_id", "")
        if not ch.get("handle"):
            ch["handle"] = meta.get("handle", "")
    except Exception as e:
        # API キー未設定や API 障害時は YouTube ページのスクレイプで補完
        try:
            scraped = resolve_channel_icon(yt_url)
        except Exception:
            scraped = ""
        if scraped:
            ch["icon_cache"] = {"url": scraped, "fetched_at": datetime.utcnow().isoformat() + "Z"}
        else:
            ch.setdefault("icon_cache", {})["error"] = str(e)[:120]
    return ch


@app.get("/api/channels")
def api_list_channels():
    """各チャンネルの icon_cache が古ければ refresh し、icon_url を含めて返す。"""
    chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    changed = False
    for ch in chs:
        if ch.get("youtube_url") or ch.get("youtube_channel_id"):
            before = (ch.get("icon_cache") or {}).get("url", "")
            _refresh_channel_icon_if_stale(ch)
            if (ch.get("icon_cache") or {}).get("url", "") != before:
                changed = True
    if changed:
        save_json(CHANNELS_CONFIG, chs)
    # 返却用に派生フィールドを追加
    for ch in chs:
        cache = ch.get("icon_cache") or {}
        ch["icon_url"] = cache.get("url", "")
        ch.setdefault("youtube_url", "")
        ch.setdefault("youtube_channel_id", "")
        ch.setdefault("handle", "")
        # ⚠ save_json(2221) より後。返却用にのみ現マシン解決（原文は保存済み channels.json に維持）
        if ch.get("folder"):
            ch["folder"] = _resolve_to_current_host(ch["folder"])
        if not ch.get("prefix"):
            ch["prefix"] = infer_file_prefix_from_folder(Path(ch.get("folder") or "")) or ""
    return {"channels": chs}

@app.get("/api/channels/overview")
def api_channels_overview():
    """全チャンネル一元管理ビュー用データ。各 ch の icon/url/prefix に加え、per-ch 設定
    （persona 要約 / rival_channels 件数 / priority / autopilot_enabled）をまとめて返す。
    ⚠ 読み取りのみ・アクティブ ch は切り替えない（横断管理用）。"""
    chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    out = []
    for ch in chs:
        folder = _resolve_to_current_host(ch.get("folder") or "")
        cc = {}
        if folder and Path(folder).exists():
            cc = load_json(Path(folder) / _CHANNEL_CONFIG_FILENAME, {})
        cache = ch.get("icon_cache") or {}
        persona = (cc.get("persona") or "").strip()
        rivals = cc.get("rival_channels") or []
        out.append({
            "channel_id": ch.get("id", ""),
            "name": ch.get("name", ""),
            "folder": folder,
            "prefix": ch.get("prefix") or infer_file_prefix_from_folder(Path(folder or "")) or "",
            "icon_url": cache.get("url", ""),
            "youtube_url": ch.get("youtube_url", ""),
            "handle": ch.get("handle", ""),
            "persona": persona,
            "persona_set": bool(persona),
            "rivals_count": len(rivals),
            "priority": int(cc.get("priority", 100)) if str(cc.get("priority", "")).strip() else 100,
            "autopilot_enabled": bool(cc.get("autopilot_enabled", False)),
        })
    active_folder = _resolve_to_current_host(get_dashboard_config().get("channel_folder") or "")
    return {"channels": out, "active_folder": active_folder}

class ChannelResolveRequest(BaseModel):
    url: str

@app.post("/api/channels/resolve-url")
def api_channels_resolve_url(req: ChannelResolveRequest):
    """YouTube チャンネル URL or handle or channelId からメタ情報を返す（保存しない）"""
    return _resolve_youtube_channel_meta(req.url)

class ChannelCreate(BaseModel):
    name: str
    folder: str
    prefix: str = ""
    template_prproj: str = ""
    template_psd: str = ""
    create_folder: bool = True
    open_in_finder: bool = True
    youtube_url: str = ""


def _create_empty_channel_config(folder: Path, req: ChannelCreate) -> None:
    """新規チャンネルに旧グローバル設定が流れ込まないよう初期 config を置く。"""
    if not folder.exists():
        return
    p = folder / _CHANNEL_CONFIG_FILENAME
    if p.exists():
        return
    seed = {
        "persona": "",
        "rival_channels": [],
        "reference_image_dir": "",
        "reference_image": "",
        "benchmark_pinned_names": [],
        "benchmark_filter": DEFAULT_BENCHMARK_CONFIG["filter"],
        "spreadsheet_channel_detail_url": "",
        "spreadsheet_growth_tracking_url": "",
        "template_prproj": req.template_prproj or "",
        "template_psd": req.template_psd or "",
        "_schema_version": 1,
        "_created_at": datetime.utcnow().isoformat() + "Z",
        "_created_for_channel": req.name or "",
    }
    p.write_text(json.dumps(seed, indent=2, ensure_ascii=False), encoding="utf-8")

@app.get("/api/channels/suggest-folder")
def api_suggest_channel_folder(name: str = ""):
    """YTフォルダ配下に番号付きチャンネルフォルダ名を提案"""
    # 既存の YT フォルダを探す
    yt_candidates = list((HOME / "Library/CloudStorage").glob("GoogleDrive-*/共有ドライブ/YT"))
    if not yt_candidates:
        return {"suggestion": "", "yt_root": ""}
    yt_root = yt_candidates[0]
    # 最大番号を取得
    max_num = 0
    for d in yt_root.iterdir():
        if d.is_dir():
            m = re.match(r'^(\d+)\.', d.name)
            if m: max_num = max(max_num, int(m.group(1)))
    next_num = max_num + 1
    channel_dir_name = f"{next_num}.【{name or 'NewChannel'}】"
    return {"suggestion": str(yt_root / channel_dir_name), "yt_root": str(yt_root)}

@app.post("/api/channels")
def api_create_channel(req: ChannelCreate):
    channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    channel_id = re.sub(r'[^a-zA-Z0-9]', '_', req.name.lower()).strip('_') or f"ch{len(channels)}"
    folder = Path(req.folder)
    if req.create_folder:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "プロジェクト").mkdir(exist_ok=True)
        (folder / "素材").mkdir(exist_ok=True)
    _create_empty_channel_config(folder, req)
    inferred_prefix = infer_file_prefix_from_folder(folder)
    prefix = sanitize_file_prefix(req.prefix or inferred_prefix or req.name, fallback="vol")
    new_channel = {
        "id": channel_id, "name": req.name, "folder": str(folder),
        "prefix": prefix, "template_prproj": req.template_prproj, "template_psd": req.template_psd,
        "youtube_url": (req.youtube_url or "").strip(),
        "youtube_channel_id": "",
        "handle": "",
        "icon_cache": {},
    }
    # YouTube URL 指定があればアイコンと channel_id を取得
    if new_channel["youtube_url"]:
        try:
            meta = _resolve_youtube_channel_meta(new_channel["youtube_url"])
            new_channel["youtube_channel_id"] = meta.get("channel_id", "")
            new_channel["handle"] = meta.get("handle", "")
            new_channel["icon_cache"] = {"url": meta.get("icon_url", ""), "fetched_at": datetime.utcnow().isoformat() + "Z"}
            # 名前未指定の場合は YouTube タイトルで補完
            if not req.name.strip() and meta.get("name"):
                new_channel["name"] = meta["name"]
                new_channel["id"] = re.sub(r'[^a-zA-Z0-9]', '_', meta["name"].lower()).strip('_') or new_channel["id"]
        except HTTPException as e:
            # URL 解決に失敗してもチャンネル登録は続ける（後で再試行可能）
            new_channel["icon_cache"] = {"error": e.detail}
    channels.append(new_channel)
    save_json(CHANNELS_CONFIG, channels)
    if req.open_in_finder and folder.exists():
        open_in_finder(folder)
    return {"status": "ok", "channel": new_channel}

@app.delete("/api/channels/{channel_id}")
def api_delete_channel(channel_id: str):
    # ⚠R1: get_channels() は folder を解決済みにするため、それを save すると他マシンのパスを壊す。
    #       生 load した原文ベースで削除・保存する。
    channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    channels = [c for c in channels if c.get("id") != channel_id]
    save_json(CHANNELS_CONFIG, channels)
    return {"status": "ok"}


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    youtube_url: Optional[str] = None
    prefix: Optional[str] = None


@app.put("/api/channels/{channel_id}")
def api_update_channel(channel_id: str, req: ChannelUpdate):
    """既存チャンネルの YouTube URL / 名前 / prefix を更新。
    youtube_url が変わった場合はメタ情報（channel_id / handle / icon）も再取得する。"""
    channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    ch = next((c for c in channels if c.get("id") == channel_id), None)
    if not ch:
        raise HTTPException(404, "チャンネルが見つかりません")
    if req.name is not None and req.name.strip():
        ch["name"] = req.name.strip()
    if req.prefix is not None:
        new_prefix = sanitize_file_prefix(req.prefix, fallback=ch.get("prefix") or "vol")
        if new_prefix:
            ch["prefix"] = new_prefix
    if req.youtube_url is not None:
        url = req.youtube_url.strip()
        ch["youtube_url"] = url
        if url:
            try:
                meta = _resolve_youtube_channel_meta(url)
                ch["youtube_channel_id"] = meta.get("channel_id", "")
                ch["handle"] = meta.get("handle", "")
                icon_url = meta.get("icon_url", "")
                ch["icon_cache"] = {
                    "url": icon_url,
                    "fetched_at": datetime.utcnow().isoformat() + "Z",
                }
                # 名前が未設定の場合のみ YouTube タイトルで補完
                if (not ch.get("name") or ch.get("name") == "New Channel") and meta.get("name"):
                    ch["name"] = meta["name"]
            except HTTPException as e:
                ch["icon_cache"] = {"error": e.detail}
        else:
            # URL クリア時はメタ情報もクリア
            ch["youtube_channel_id"] = ""
            ch["handle"] = ""
            ch["icon_cache"] = {}
    save_json(CHANNELS_CONFIG, channels)
    return {"status": "ok", "channel": ch}

def _schedule_self_restart(delay_sec: float = 1.5):
    """app_id 切替反映のため、delay 後に start.sh を起動して自身を置き換える。
    start.sh が :8888 を kill → python3 app.py を exec（新 app_id を resolve）。
    デタッチ起動なので現リクエストのレスポンスは先に返る。"""
    import shlex
    script = SHARED_BASE / "Python" / "start.sh"
    try:
        subprocess.Popen(
            ["bash", "-c", f"sleep {delay_sec}; bash {shlex.quote(str(script))}"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[switch] app_id 切替のため {delay_sec}s 後に自己再起動を予約")
    except Exception as e:
        print(f"[switch] 自己再起動の予約失敗: {e}")


@app.put("/api/channels/active/{channel_id}")
def api_set_active_channel(channel_id: str):
    channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    ch = next((c for c in channels if c["id"] == channel_id), None)
    if not ch: raise HTTPException(404, "チャンネルが見つかりません")
    config = get_dashboard_config()
    folder = Path(_resolve_to_current_host(ch["folder"]))  # FS アクセス用に現マシン解決
    prefix = sanitize_file_prefix(ch.get("prefix") or infer_file_prefix_from_folder(folder) or config.get("file_prefix"), fallback="vol")
    if ch.get("prefix") != prefix:
        ch["prefix"] = prefix
        save_json(CHANNELS_CONFIG, channels)  # folder は原文のまま（prefix のみ更新）
    # チャンネル切替: グローバル設定のみ更新（per-channel キーを書き戻さない）
    raw = load_json(DASHBOARD_CONFIG, {})
    raw["channel_name"] = ch["name"]
    # dashboard_config は ~/.config/{app_id} のローカル（GDrive 非同期）なので解決済み現マシンパスを保存
    raw["channel_folder"] = str(folder)
    raw["file_prefix"] = prefix
    # per-channel キーが旧グローバルに残っていれば剥がす（マイグレーション後の片付け）
    for k in PER_CHANNEL_KEYS:
        raw.pop(k, None)
    save_json(DASHBOARD_CONFIG, raw)
    # ── D4+: チャンネル → app_id 遷移 ──
    # channels.json の per-ch app_id をアクティブポインタに書き、現プロセスの app_id と
    # 異なれば profile(suno_config/benchmark 等)を切り替えるためサーバーを自己再起動する。
    import _app_config as _ac
    target_app = (ch.get("app_id") or "").strip() or ("sk" if channel_id == "sukima" else "orzz")
    current_app = _ac.resolve_app_id()
    app_id_changed = bool(target_app and target_app != current_app)
    _ac.set_active_app_id(target_app)
    if app_id_changed:
        # 再起動後にターゲット app_id がこの ch で起動するよう、その dashboard にも反映
        try:
            tdash = Path.home() / ".config" / target_app / "dashboard_config.json"
            traw = load_json(tdash, {})
            traw["channel_name"] = ch["name"]
            traw["channel_folder"] = str(folder)
            traw["file_prefix"] = prefix
            for k in PER_CHANNEL_KEYS:
                traw.pop(k, None)
            tdash.parent.mkdir(parents=True, exist_ok=True)
            save_json(tdash, traw)
        except Exception as e:
            print(f"[switch] target dashboard 反映失敗: {e}")
        _schedule_self_restart()
    return {"status": "ok", "channel": ch, "app_id": target_app,
            "app_id_changed": app_id_changed, "restart_required": app_id_changed}

# ─── API: 動画フォルダ ───

class VideoFolderCreate(BaseModel):
    publish_date: str
    open_in_finder: bool = True

def _resolve_template_candidates(channel_dir: Path, kind: str) -> List[Path]:
    """テンプレ検索ディレクトリを優先順に返す。
    kind: 'prproj' | 'psd'
    優先順: <channel_folder>/プロジェクト/ → <channel_folder>/Project/ → <channel_folder> 直下
    """
    candidates = []
    for sub in ("プロジェクト", "Project", "templates", "Templates", ""):
        d = channel_dir / sub if sub else channel_dir
        if d.exists() and d.is_dir():
            candidates.append(d)
    return candidates


def _find_template_file(channel_dir: Path, filename: str) -> Optional[Path]:
    """指定ファイル名のテンプレを検索ディレクトリから見つけて返す。見つからなければ None。"""
    if not filename:
        return None
    # 絶対パスで指定された場合はそのまま
    p = Path(filename)
    if p.is_absolute() and p.exists():
        return p
    for d in _resolve_template_candidates(channel_dir, ""):
        cand = d / filename
        if cand.exists():
            return cand
    return None


class PickFileRequest(BaseModel):
    extensions: List[str] = []          # ["prproj", "psd"] など。空なら任意ファイル
    initial: Optional[str] = None       # 初期表示ディレクトリ（POSIX path）
    prompt: Optional[str] = "ファイルを選択"


@app.post("/api/finder/pick-file")
def api_finder_pick_file(req: PickFileRequest):
    """macOS の choose file ダイアログを osascript で開いて選ばれたファイルの POSIX パスを返す。
    UI 側からテンプレ .prproj / .psd 選択に使う。"""
    if platform.system() != "Darwin":
        raise HTTPException(400, "macOS でのみ利用可能です")
    exts = [e.strip().lstrip(".") for e in (req.extensions or []) if e and e.strip()]
    type_clause = ""
    if exts:
        # AppleScript の of type 句は UTI / 拡張子の OR リスト
        type_clause = " of type {" + ", ".join([f'"{e}"' for e in exts]) + "}"
    initial_clause = ""
    if req.initial:
        ip = Path(req.initial).expanduser()
        if ip.exists():
            posix = str(ip).replace('"', '\\"')
            initial_clause = f' default location POSIX file "{posix}"'
    prompt = (req.prompt or "ファイルを選択").replace('"', '\\"')
    script = (
        f'POSIX path of (choose file with prompt "{prompt}"'
        f"{type_clause}{initial_clause})"
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "ダイアログがタイムアウトしました")
    if r.returncode != 0:
        # ユーザーがキャンセルした場合 osascript は -128 で終了
        msg = (r.stderr or "").strip()
        if "User canceled" in msg or "-128" in msg:
            return {"status": "canceled", "path": ""}
        raise HTTPException(500, f"ファイル選択失敗: {msg[:200]}")
    path = (r.stdout or "").strip()
    return {"status": "ok", "path": path, "filename": Path(path).name if path else ""}


@app.get("/api/templates/list")
def api_list_templates():
    """チャンネルフォルダ配下の Premiere / Photoshop テンプレ候補を返す。
    UI 側でセレクトボックスに表示する用。"""
    config = get_dashboard_config()
    channel_dir_str = config.get("channel_folder") or ""
    if not channel_dir_str:
        return {"channel_folder": "", "prproj": [], "psd": [], "search_dirs": []}
    channel_dir = Path(channel_dir_str)
    if not channel_dir.exists():
        return {"channel_folder": str(channel_dir), "prproj": [], "psd": [],
                "search_dirs": [], "warning": "チャンネルフォルダが存在しません"}
    search_dirs = _resolve_template_candidates(channel_dir, "")
    prproj_set: dict[str, str] = {}
    psd_set: dict[str, str] = {}
    for d in search_dirs:
        for p in d.glob("*.prproj"):
            if p.name not in prproj_set:
                prproj_set[p.name] = str(p)
        for p in d.glob("*.psd"):
            if p.name not in psd_set:
                psd_set[p.name] = str(p)
    return {
        "channel_folder": str(channel_dir),
        "search_dirs": [str(d) for d in search_dirs],
        "prproj": [{"filename": k, "path": v} for k, v in sorted(prproj_set.items())],
        "psd": [{"filename": k, "path": v} for k, v in sorted(psd_set.items())],
        "current_prproj": config.get("template_prproj", ""),
        "current_psd": config.get("template_psd", ""),
    }


@app.post("/api/videos/create")
def api_create_video_folder(req: VideoFolderCreate):
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    if not channel_dir.exists():
        raise HTTPException(400, "チャンネルフォルダが存在しません")
    max_num = 0
    for d in channel_dir.iterdir():
        if d.is_dir():
            info = parse_video_folder_name(d.name)
            if info:
                max_num = max(max_num, info["num"])
    next_num = max_num + 1
    try:
        dt = datetime.strptime(req.publish_date, "%Y-%m-%d")
        date_str = dt.strftime("%y%m%d")
    except ValueError:
        date_str = req.publish_date.replace("-", "")
    prefix = get_file_prefix()
    new_dir = channel_dir / f"{next_num}_{prefix}_{date_str}"
    new_dir.mkdir(exist_ok=True)
    vol_num = f"{next_num:02d}"
    # テンプレートコピー（未配置時は warnings に積んで UI に通知）
    tpl_prproj = config.get("template_prproj") or ""
    tpl_psd = config.get("template_psd") or ""
    warnings: List[str] = []
    created: List[str] = []

    if tpl_prproj:
        prproj_src = _find_template_file(channel_dir, tpl_prproj)
        if prproj_src:
            prproj_dst = new_dir / f"{prefix}_vol{vol_num}.prproj"
            try:
                shutil.copy2(str(prproj_src), str(prproj_dst))
                try:
                    with gzip.open(str(prproj_dst), 'rb') as f: xml = f.read()
                    # テンプレ内に残っている可能性のある旧シーケンス名 (BGMシーケンス /
                    # {prefix}_vol01 / {prefix}_vol02 等) を全部 {prefix}_vol{N} に置換。
                    # 旧コードは 'BGMシーケンス' しか置換せず、テンプレが {prefix}_vol01
                    # と命名されていた場合に置換が効かず、JSX が新シーケンスを見つけられず
                    # 「クリップが見つかりません」エラーになっていた。
                    target_name = f"{prefix}_vol{vol_num}".encode()
                    for old in (b"BGM\xe3\x82\xb7\xe3\x83\xbc\xe3\x82\xb1\xe3\x83\xb3\xe3\x82\xb9",
                                f"{prefix}_vol01".encode(),
                                f"{prefix}_vol02".encode()):
                        if old != target_name:
                            xml = xml.replace(old, target_name)
                    with gzip.open(str(prproj_dst), 'wb') as f: f.write(xml)
                except Exception:
                    pass
                created.append(prproj_dst.name)
            except Exception as e:
                warnings.append(f".prproj コピー失敗: {e}")
        else:
            warnings.append(f"Premiere テンプレが見つかりません: {tpl_prproj}（基本設定 → チャンネル → テンプレートで指定してください）")
    else:
        warnings.append("Premiere テンプレ未設定（基本設定 → チャンネル → テンプレートで指定してください）")

    if tpl_psd:
        psd_src = _find_template_file(channel_dir, tpl_psd)
        if psd_src:
            psd_dst = new_dir / f"{prefix}_vol{vol_num}.psd"
            try:
                shutil.copy2(str(psd_src), str(psd_dst))
                created.append(psd_dst.name)
            except Exception as e:
                warnings.append(f".psd コピー失敗: {e}")
        else:
            warnings.append(f"Photoshop テンプレが見つかりません: {tpl_psd}（基本設定 → チャンネル → テンプレートで指定してください）")
    else:
        warnings.append("Photoshop テンプレ未設定（基本設定 → チャンネル → テンプレートで指定してください）")

    if req.open_in_finder:
        open_in_finder(new_dir)
    return {
        "status": "ok",
        "folder": new_dir.name,
        "num": next_num,
        "path": str(new_dir),
        "created": created,
        "warnings": warnings,
    }

@app.get("/api/videos")
def api_list_videos():
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    if not channel_dir.exists():
        return {"videos": [], "error": "チャンネルフォルダが見つかりません"}
    videos = []
    # フォルダ名先頭の数値で降順ソート（文字列ソートだと "7" > "71" になる問題を回避）
    def _vol_key(p):
        info = parse_video_folder_name(p.name)
        return info["num"] if info else -1
    entries = [d for d in channel_dir.iterdir() if d.is_dir() and parse_video_folder_name(d.name)]
    entries.sort(key=_vol_key, reverse=True)
    for d in entries:
        info = parse_video_folder_name(d.name)
        if not info:
            continue
        num = info["num_text"]
        has_music = (d / "music").is_dir() and any(_is_usable_audio_file(p) for p in (d / "music").iterdir())
        has_srt = any(d.glob("subtitles_*.srt"))
        has_mp4 = _find_exported_mp4(d, d.name) is not None
        has_desc = (d / "youtube_description.txt").exists()
        has_thumb = any(d.glob("vol*.jpg")) or (d / "サムネイル.jpg").exists()
        has_upload = (d / "youtube_upload.json").exists()
        has_tags = (d / "youtube_tags.txt").exists()
        has_images_selected = (d / "selected_images.json").exists()
        # 背景画像（パイプライン STEP 3 / step_bgimage）の完了マーカー: vol{num}.png または vol{num}.jpg
        has_bg_image = (d / f"vol{num}.png").exists() or (d / f"vol{num}.jpg").exists()
        is_hidden = (d / ".hidden").exists()
        title_file = d / "youtube_title.txt"
        title = title_file.read_text(encoding="utf-8").strip() if title_file.exists() else ""
        concept_file = d / "concept.txt"
        concept = concept_file.read_text(encoding="utf-8").strip() if concept_file.exists() else ""
        publish_date = info["publish_date"]
        videos.append({
            "num": num, "name": d.name, "path": str(d),
            "prefix": info["prefix"],
            "title": title,
            "concept": concept,
            "has_music": has_music, "has_srt": has_srt, "has_mp4": has_mp4,
            "has_desc": has_desc, "has_thumb": has_thumb,
            "has_upload": has_upload, "has_tags": has_tags,
            "has_images_selected": has_images_selected,
            "has_bg_image": has_bg_image,
            "is_hidden": is_hidden,
            "publish_date": publish_date,
        })
    return {"videos": videos[:100]}


# ─── API: 全チャンネル × 全 vol の稼働ステータス（P1-7 一元ダッシュボード） ───
# UI は #p-dashboard 上部にこの集約ビューを描画する。**台帳ではなく集約**：
# 状態は依然として folder artifact から推論する（中央 ledger は P3）。

_PIPELINE_STAGES = ["plan", "suno", "rename", "premiere", "export", "meta", "upload"]

def _infer_stage_from_artifacts(folder: Path) -> dict:
    """vol フォルダのファイル群から、各 stage の done/current を推定。"""
    has_plan = (folder / "plan.json").exists()
    has_music = (folder / "music").is_dir() and any(_is_usable_audio_file(p) for p in (folder / "music").iterdir())
    has_audio_processed = (folder / "audio").is_dir() and any(_is_usable_audio_file(p) for p in (folder / "audio").iterdir())
    has_timecode = any(folder.glob("timecode*.txt"))
    has_srt = any(folder.glob("subtitles_*.srt"))
    has_mp4 = _find_exported_mp4(folder, folder.name) is not None
    has_title = (folder / "youtube_title.txt").exists()
    has_desc = (folder / "youtube_description.txt").exists()
    has_tags = (folder / "youtube_tags.txt").exists()
    has_upload = (folder / "youtube_upload.json").exists()
    done_map = {
        "plan":     has_plan,
        "suno":     has_music,
        "rename":   has_audio_processed or has_music,  # rename は music 内で完了する場合あり
        "premiere": has_timecode or has_srt,
        "export":   has_mp4,
        "meta":     has_title and has_desc and has_tags,
        "upload":   has_upload,
    }
    completed = [s for s in _PIPELINE_STAGES if done_map.get(s)]
    # 「現在 stage」= 最初の未完了 stage（plan は optional なのでスキップ）
    current = ""
    for s in _PIPELINE_STAGES:
        if s == "plan":
            continue  # plan は from-benchmark 起動時のみ
        if not done_map.get(s):
            current = s
            break
    return {
        "completed": completed,
        "current": current,
        "is_done": current == "",
    }


@app.get("/api/runs/active")
def api_runs_active():
    """全チャンネル × 直近 vol の稼働ステータスを集約。

    返却:
      channels[].channel_id / name / folder
      channels[].vols[]: {vol, name, completed[], current, is_done, publish_date,
                          is_uploaded, video_id, concept}
      channels[].quota: {used, cap, remaining}（quota.json があれば）
      history_recent[]: 直近 20 件のジョブ実行履歴
      active_jobs[]: 今 RUNNING ステータスのジョブ
    """
    import app_youtube as _ytm  # quota helpers
    out_channels = []
    for ch in get_channels():
        ch_dir = Path(ch.get("folder") or "")
        if not ch_dir.exists():
            continue
        vols = []
        # vol フォルダを vol 番号降順で最大 5 件
        try:
            entries = [d for d in ch_dir.iterdir() if d.is_dir() and parse_video_folder_name(d.name)]
        except Exception:
            entries = []
        def _vol_key(p):
            info = parse_video_folder_name(p.name)
            return info["num"] if info else -1
        entries.sort(key=_vol_key, reverse=True)
        for d in entries[:5]:
            info = parse_video_folder_name(d.name)
            if not info:
                continue
            stage_info = _infer_stage_from_artifacts(d)
            concept_file = d / "concept.txt"
            concept = concept_file.read_text(encoding="utf-8").strip() if concept_file.exists() else ""
            up_marker = d / "youtube_upload.json"
            video_id = ""
            scheduled_publish_at = ""
            published_at = ""
            privacy_now = ""
            if up_marker.exists():
                try:
                    upm = json.loads(up_marker.read_text(encoding="utf-8"))
                    video_id = upm.get("video_id", "")
                    scheduled_publish_at = upm.get("scheduled_publish_at") or ""
                    published_at = upm.get("published_at") or ""
                    privacy_now = upm.get("privacy") or ""
                except Exception:
                    pass
            vols.append({
                "vol": info["num_text"],
                "name": d.name,
                "completed": stage_info["completed"],
                "current": stage_info["current"],
                "is_done": stage_info["is_done"],
                "publish_date": info.get("publish_date", ""),
                "is_uploaded": bool(video_id),
                "video_id": video_id,
                "concept": concept,
                # P2-7: 公開ゲート状態
                "scheduled_publish_at": scheduled_publish_at,
                "published_at": published_at,
                "privacy": privacy_now,
            })
        # quota 状態
        quota_block = {}
        try:
            used = _ytm.quota_used_in_window(ch_dir)
            cap = _ytm.DEFAULT_DAILY_QUOTA_CAP
            quota_block = {
                "used": used, "cap": cap, "remaining": max(0, cap - used),
                "per_upload": _ytm.QUOTA_PER_UPLOAD,
                "window_h": _ytm.QUOTA_WINDOW_HOURS,
            }
        except Exception:
            pass
        out_channels.append({
            "channel_id": ch.get("id", ""),
            "channel_name": ch.get("name", ""),
            "channel_folder": str(ch_dir),
            "icon_url": ch.get("icon_url", ""),
            "vols": vols,
            "quota": quota_block,
        })
    # スケジューラ履歴 + 実行中ジョブ
    history_recent = list(reversed(_scheduler_history[-20:]))
    active_jobs = [h for h in history_recent if h.get("status") == "started"][:5]
    return {
        "channels": out_channels,
        "history_recent": history_recent,
        "active_jobs": active_jobs,
        "stages": _PIPELINE_STAGES,
    }

@app.get("/api/videos/{video_name}/detail")
def api_video_detail(video_name: str):
    """動画フォルダの詳細情報（書き出し状態、ファイル一覧）"""
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    folder = channel_dir / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    folder_info = parse_video_folder_name(video_name)
    m = re.match(r'^(\d+)_', video_name)
    num = folder_info["num_text"] if folder_info else (m.group(1) if m else "00")

    # 各ファイルの存在チェック
    has_music = (folder / "music").is_dir() and any(_is_usable_audio_file(p) for p in (folder / "music").iterdir())
    music_count = sum(1 for p in (folder / "music").iterdir() if _is_usable_audio_file(p)) if (folder / "music").is_dir() else 0
    has_srt = any(folder.glob("subtitles_*.srt"))
    has_prproj = any(folder.glob("*vol*.prproj"))
    has_psd = any(folder.glob("*vol*.psd"))
    has_desc = (folder / "youtube_description.txt").exists()
    has_timecode = any(folder.glob("music_time_code_info_*.txt"))

    # サムネイル
    thumb = None
    for p in [f"vol{num}.jpg", f"vol{num}.png", "サムネイル.jpg"]:
        if (folder / p).exists():
            thumb = p
            break

    # 背景画像（パイプライン STEP 3 / step_bgimage）の完了マーカー: vol{num}.png または vol{num}.jpg
    # サムネイル兼用パスと意図的に重なるが、判定軸として独立に持つ（UI ステッパー用）
    has_bg_image = (folder / f"vol{num}.png").exists() or (folder / f"vol{num}.jpg").exists()

    # MP4（書き出し済み）- チャンネルフォルダ内 / 外部パスのサブフォルダ / flat 配置を全探索
    mp4_file = None
    mp4_source = ""
    mp4_size = 0
    export_path = config.get("export_path", "")
    manual_mp4 = _read_manual_exported_mp4(folder)
    found = _find_exported_mp4(folder, video_name)
    if found is not None:
        mp4_file = str(found)
        mp4_source = "manual" if manual_mp4 is not None and found == manual_mp4 else "auto"
        try:
            mp4_size = found.stat().st_size
        except Exception:
            mp4_size = 0

    # 説明文の内容
    desc_text = ""
    if has_desc:
        desc_text = (folder / "youtube_description.txt").read_text(encoding="utf-8")

    # 公開日
    publish_date = folder_info["publish_date"] if folder_info else ""

    has_upload = (folder / "youtube_upload.json").exists()
    has_tags = (folder / "youtube_tags.txt").exists()
    upload_info = {}
    if has_upload:
        try:
            upload_info = json.loads((folder / "youtube_upload.json").read_text(encoding="utf-8"))
        except Exception:
            upload_info = {}

    # 準備状態の判定
    readiness = {
        "music": has_music,
        "thumbnail": thumb is not None,
        "bg_image": has_bg_image,
        "premiere": has_prproj,
        "srt": has_srt,
        "timecode": has_timecode,
        "mp4": mp4_file is not None,
        "description": has_desc,
        "tags": has_tags,
        "upload": has_upload,
    }
    all_ready = all([has_music, thumb is not None, has_prproj, has_srt, mp4_file is not None, has_desc])

    return {
        "num": num,
        "name": video_name,
        "path": str(folder),
        "publish_date": publish_date,
        "music_count": music_count,
        "thumbnail": thumb,
        "has_bg_image": has_bg_image,
        "has_prproj": has_prproj,
        "has_psd": has_psd,
        "readiness": readiness,
        "all_ready": all_ready,
        "mp4_file": mp4_file,
        "mp4_source": mp4_source,
        "mp4_size_mb": round(mp4_size / 1024 / 1024, 1) if mp4_size else 0,
        "description": desc_text,
        "export_path": export_path,
        "upload_info": upload_info,
    }


class VideoMp4ReferenceUpdate(BaseModel):
    path: str


@app.get("/api/videos/{video_name}/mp4-candidates")
def api_video_mp4_candidates(video_name: str, folder: Optional[str] = None):
    """動画詳細から使う MP4 候補一覧。
    folder 指定時はそのフォルダ配下も検索し、任意の書き出し済み動画を紐づけ可能にする。
    """
    config = get_dashboard_config()
    video_folder = Path(config["channel_folder"]) / video_name
    if not video_folder.exists():
        raise HTTPException(404, "動画フォルダが見つかりません")
    extra = Path(folder).expanduser() if folder else None
    current = _find_exported_mp4(video_folder, video_name)
    candidates = _collect_mp4_candidates(video_folder, video_name, extra)
    return {
        "video_name": video_name,
        "current": _mp4_candidate_payload(current, "current") if current else None,
        "candidates": candidates,
    }


@app.put("/api/videos/{video_name}/mp4-reference")
def api_put_video_mp4_reference(video_name: str, req: VideoMp4ReferenceUpdate):
    config = get_dashboard_config()
    video_folder = Path(config["channel_folder"]) / video_name
    if not video_folder.exists():
        raise HTTPException(404, "動画フォルダが見つかりません")
    payload = _write_manual_exported_mp4(video_folder, Path(req.path))
    return {"status": "ok", "video_name": video_name, "mp4": payload}


@app.delete("/api/videos/{video_name}/mp4-reference")
def api_delete_video_mp4_reference(video_name: str):
    config = get_dashboard_config()
    video_folder = Path(config["channel_folder"]) / video_name
    if not video_folder.exists():
        raise HTTPException(404, "動画フォルダが見つかりません")
    marker = video_folder / MANUAL_EXPORTED_VIDEO_FILE
    if marker.exists():
        marker.unlink()
    return {"status": "ok", "video_name": video_name}

class VideoTitleUpdate(BaseModel):
    video_name: str
    new_title: str  # YouTube用タイトル

@app.put("/api/videos/{video_name}/title")
def api_update_video_title(video_name: str, req: VideoTitleUpdate):
    """動画のYouTubeタイトルを保存"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    title_file = folder / "youtube_title.txt"
    title_file.write_text(req.new_title, encoding="utf-8")
    return {"status": "ok"}

@app.get("/api/videos/{video_name}/title")
def api_get_video_title(video_name: str):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    title_file = folder / "youtube_title.txt"
    if title_file.exists():
        return {"title": title_file.read_text(encoding="utf-8").strip()}
    m = re.match(r'^(\d+)_', video_name)
    num = m.group(1) if m else "00"
    return {"title": f"orzz. vol.{num}"}

# ─── API: タグ ───

class VideoTagsUpdate(BaseModel):
    tags: List[str]

@app.get("/api/videos/{video_name}/tags")
def api_get_video_tags(video_name: str):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    tags_file = folder / "youtube_tags.txt"
    if not tags_file.exists():
        return {"tags": []}
    lines = tags_file.read_text(encoding="utf-8").splitlines()
    return {"tags": [line.strip() for line in lines if line.strip()]}

@app.put("/api/videos/{video_name}/tags")
def api_update_video_tags(video_name: str, req: VideoTagsUpdate):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    tags_file = folder / "youtube_tags.txt"
    tags_file.write_text("\n".join([t.strip() for t in req.tags if t.strip()]), encoding="utf-8")
    return {"status": "ok", "count": len([t for t in req.tags if t.strip()])}

# ─── API: コンセプト（端的な日本語1行） ───

class VideoConceptUpdate(BaseModel):
    concept: str

@app.get("/api/videos/{video_name}/concept")
def api_get_video_concept(video_name: str):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    f = folder / "concept.txt"
    return {"concept": f.read_text(encoding="utf-8").strip() if f.exists() else ""}

@app.put("/api/videos/{video_name}/concept")
def api_update_video_concept(video_name: str, req: VideoConceptUpdate):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    text = (req.concept or "").strip()
    f = folder / "concept.txt"
    if text:
        f.write_text(text, encoding="utf-8")
    elif f.exists():
        f.unlink()
    return {"status": "ok", "concept": text}

@app.post("/api/videos/{video_name}/generate-concept")
def api_generate_video_concept(video_name: str):
    """Claude CLI でその動画の端的な日本語コンセプトを生成して concept.txt に保存。"""
    from claude_proposer import propose_concept, gather_context
    import app_competitor as _ac
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    persona = config.get("persona", "")
    channel_name = config.get("channel_name", "orzz.")
    ctx = gather_context(folder)

    benchmark_analysis = None
    try:
        cache = _ac.load_cache()
        if cache and isinstance(cache, dict):
            a = cache.get("analysis")
            benchmark_analysis = a if isinstance(a, dict) else None
    except Exception:
        benchmark_analysis = None

    try:
        concept = propose_concept(
            cli_cmd=cli_cmd, persona=persona, channel_name=channel_name,
            benchmark_analysis=benchmark_analysis, **ctx,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    if concept:
        (folder / "concept.txt").write_text(concept, encoding="utf-8")
    return {"status": "ok", "concept": concept}

# ─── API: 多言語 localizations 生成（YouTube snippet.localizations 用） ───

DEFAULT_LOCALIZATION_LANGS = ["ja", "zh-Hans", "zh-Hant", "ko", "es", "es-419", "pt-BR", "fr", "de", "it"]


class GenerateLocalizationsRequest(BaseModel):
    languages: Optional[List[str]] = None  # 未指定なら DEFAULT_LOCALIZATION_LANGS
    force: bool = False                    # True なら既存 youtube_localizations.json を無視して再生成


def _build_localizations_prompt(title: str, description: str, langs: List[str], source_lang: str = "en") -> str:
    lang_list = ", ".join(langs)
    keys_block = ",".join(f'"{l}":{{"title":"...","description":"..."}}' for l in langs)
    _src_name = {
        "en": "English", "ja": "Japanese",
        "zh-Hans": "Simplified Chinese", "zh-Hant": "Traditional Chinese",
        "ko": "Korean", "es": "Spanish", "es-419": "Latin American Spanish",
        "pt-BR": "Brazilian Portuguese", "fr": "French", "de": "German", "it": "Italian",
    }.get(source_lang, source_lang)
    return f"""Translate the following YouTube video metadata into these languages: {lang_list}. Return JSON only.

ORIGINAL ({_src_name}):
TITLE: {title}
DESCRIPTION:
{description}

RULES:
- Translate naturally; keep the BGM-channel mood.
- Preserve Tracklist with timestamps verbatim.
- Title within ~80 chars.
- zh-Hans = Simplified Chinese, zh-Hant = Traditional Chinese.
- es = Spain Spanish, es-419 = Latin America Spanish.
- pt-BR = Brazilian Portuguese.
- In description, escape newlines as \\n inside JSON strings (literal \\n, NOT raw line breaks).

OUTPUT (JSON only, no markdown fence, no explanation, all values single-line with \\n escapes):
{{{keys_block}}}"""


@app.post("/api/videos/{video_name}/generate-localizations")
def api_generate_localizations(video_name: str, req: GenerateLocalizationsRequest):
    """Claude CLI で 10 言語翻訳して youtube_localizations.json を生成。

    入力: <vol_folder>/youtube_title.txt + youtube_description.txt
    出力: <vol_folder>/youtube_localizations.json (上書き)
    使い所: app_youtube.py が upload 時に同 JSON を読んで snippet.localizations として送信
    """
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, f"vol_folder が存在しません: {folder}")

    title_path = folder / "youtube_title.txt"
    desc_path = folder / "youtube_description.txt"
    if not title_path.exists() or not desc_path.exists():
        raise HTTPException(400, "youtube_title.txt または youtube_description.txt がありません。先に meta step / suggest API を実行してください")

    out_path = folder / "youtube_localizations.json"
    if out_path.exists() and not req.force:
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            return {"status": "skipped", "reason": "既存ファイルあり (force=true で再生成可能)",
                    "video_name": video_name, "languages": list(existing.keys()),
                    "output_path": str(out_path)}
        except Exception:
            pass  # 壊れていれば再生成

    title = title_path.read_text(encoding="utf-8").strip()
    description = desc_path.read_text(encoding="utf-8").strip()
    # メイン言語（メタ生成のソース言語）は翻訳ターゲットから除外（重複ローカライズ防止）
    source_lang = (get_yt_upload_defaults().get("default_language") or "en").strip() or "en"
    langs = req.languages or DEFAULT_LOCALIZATION_LANGS
    langs = [l for l in langs if l and l != source_lang]
    if not langs:
        raise HTTPException(400, f"翻訳対象言語がありません（メイン言語 {source_lang} を除くと空）")
    prompt = _build_localizations_prompt(title, description, langs, source_lang=source_lang)

    # Claude CLI 呼び出し（pipeline の _generate_scene_copy_en と同じパターン）
    suno_cfg_path = CONFIG_DIR / "suno_config.json"
    cli = "claude"
    if suno_cfg_path.exists():
        try:
            cli = (json.loads(suno_cfg_path.read_text(encoding="utf-8")).get("claude_cli") or "claude")
        except Exception:
            pass

    try:
        from app_llm_runner import run_llm, LLMError
        raw = run_llm(prompt, cli_cmd=cli, timeout=180, label="localizations")
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:200]}")
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise HTTPException(500, f"JSON が抽出できませんでした: {raw[:200]}")
    try:
        data = json.loads(m.group(0), strict=False)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"JSON parse 失敗: {e}; raw head: {m.group(0)[:200]}")

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "video_name": video_name,
        "languages": list(data.keys()),
        "output_path": str(out_path),
    }


# ─── API: 動画 mp4 の場所・情報を統一表示 ───

@app.get("/api/videos/{video_name}/mp4-info")
def api_mp4_info(video_name: str):
    """vol の mp4 をどこから検出したか、サイズ・解像度・尺・コーデックを返す。

    検出ソース判定（_find_exported_mp4 の探索順と整合）:
      - "manual_exported_video": <vol_folder>/manual_exported_video.json で紐づけ済
      - "vol_folder": <vol_folder>/*.mp4
      - "external_ssd_per_video": <export_path>/<video_name>/*.mp4
      - "external_ssd_flat": <export_path>/<prefix>_vol<num>*.mp4 (flat 配置)
    """
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, f"vol_folder が存在しません: {folder}")

    mp4 = _find_exported_mp4(folder, video_name)
    if not mp4:
        return {"present": False, "path": None, "source": None}

    mp4_str = str(mp4)
    source = "unknown"
    # manual_exported_video.json と一致するか
    manual_path = folder / MANUAL_EXPORTED_VIDEO_FILE
    if manual_path.exists():
        try:
            md = json.loads(manual_path.read_text(encoding="utf-8"))
            if md.get("path") == mp4_str:
                source = "manual_exported_video"
        except Exception:
            pass
    if source == "unknown":
        if mp4_str.startswith(str(folder) + "/"):
            source = "vol_folder"
        elif mp4_str.startswith("/Volumes/"):
            # per_video subfolder か flat か
            try:
                ext_dir = _resolve_external_export_dir()
            except Exception:
                ext_dir = None
            if ext_dir is not None and mp4.parent == (ext_dir / video_name):
                source = "external_ssd_per_video"
            else:
                source = "external_ssd_flat"

    info: dict = {
        "present": True,
        "path": mp4_str,
        "source": source,
        "size_bytes": mp4.stat().st_size,
    }
    # ffprobe があれば解像度・尺・コーデックを取得（無くても non-fatal）
    import shutil as _sh
    ffprobe = _sh.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error",
                 "-show_entries", "format=duration",
                 "-show_entries", "stream=width,height,codec_name,codec_type",
                 "-of", "json", mp4_str],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                fd = json.loads(proc.stdout)
                for s in fd.get("streams", []):
                    if s.get("codec_type") == "video" or "width" in s:
                        info["width"] = s.get("width")
                        info["height"] = s.get("height")
                        info["resolution"] = f'{s.get("width")}x{s.get("height")}'
                        info["video_codec"] = s.get("codec_name")
                        break
                fmt = fd.get("format", {})
                if fmt.get("duration"):
                    info["duration_sec"] = float(fmt["duration"])
        except Exception:
            pass
    return info


# ─── API: メタ情報の充足状況チェック ───

@app.get("/api/videos/{video_name}/meta-status")
def api_meta_status(video_name: str):
    """動画 vol の meta データ充足状況を返す（Web UI のチェックリスト用）。
    各項目の present / ok を見て、ready_for_upload で総合判定。
    """
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, f"vol_folder が存在しません: {folder}")

    def _check_text(filename: str, min_size: int = 1) -> dict:
        p = folder / filename
        if not p.exists():
            return {"present": False, "size": 0, "ok": False}
        size = p.stat().st_size
        try:
            preview = p.read_text(encoding="utf-8").strip()[:80]
        except Exception:
            preview = ""
        return {"present": True, "size": size, "ok": size >= min_size, "preview": preview}

    title = _check_text("youtube_title.txt", 5)
    description = _check_text("youtube_description.txt", 50)
    tags = _check_text("youtube_tags.txt", 5)

    loc_path = folder / "youtube_localizations.json"
    if loc_path.exists():
        try:
            d = json.loads(loc_path.read_text(encoding="utf-8"))
            locs = {"present": True, "languages": list(d.keys()), "count": len(d), "ok": len(d) >= 5}
        except Exception as e:
            locs = {"present": True, "error": str(e), "ok": False}
    else:
        locs = {"present": False, "languages": [], "count": 0, "ok": False}

    mp4 = _find_exported_mp4(folder, folder.name)
    mp4_info: dict = {"present": mp4 is not None, "path": str(mp4) if mp4 else None}
    if mp4:
        try:
            mp4_info["size"] = mp4.stat().st_size
        except Exception:
            pass

    upload_info: dict = {"present": False, "video_id": None, "url": None}
    up_path = folder / "youtube_upload.json"
    if up_path.exists():
        try:
            ud = json.loads(up_path.read_text(encoding="utf-8"))
            upload_info = {
                "present": True,
                "video_id": ud.get("video_id"),
                "url": ud.get("url"),
                "title": ud.get("title"),
                "localizations_applied": ud.get("localizations_applied") or [],
            }
        except Exception:
            pass

    ready_for_upload = bool(title.get("ok") and description.get("ok") and tags.get("ok") and mp4_info["present"])
    return {
        "video_name": video_name,
        "title": title,
        "description": description,
        "tags": tags,
        "localizations": locs,
        "mp4": mp4_info,
        "upload": upload_info,
        "ready_for_upload": ready_for_upload,
    }


# ─── API: 動画の非表示 / 復元 ───

@app.post("/api/videos/{video_name}/hide")
def api_video_hide(video_name: str):
    """動画を一覧から非表示にする（フォルダは維持、.hidden ファイルを作成）"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    (folder / ".hidden").write_text("", encoding="utf-8")
    return {"status": "ok", "hidden": True}

@app.post("/api/videos/{video_name}/unhide")
def api_video_unhide(video_name: str):
    """非表示を解除"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    hidden_file = folder / ".hidden"
    if hidden_file.exists():
        hidden_file.unlink()
    return {"status": "ok", "hidden": False}


# ─── API: 楽曲 (メディアプレイヤー + いいね) ───

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".aac")

def _parse_track_likes(filename: str):
    """ファイル名先頭の 'z+_' / 'x_' を抜いていいね数・バッド状態と素の名前を返す"""
    stem = Path(filename).stem
    ext = Path(filename).suffix
    # バッド: x_ プレフィックス
    is_bad = False
    if stem.startswith("x_"):
        is_bad = True
        stem = stem[2:]
    # いいね: z+ プレフィックス
    m = re.match(r"^(z+)_(.+)$", stem)
    if m:
        return len(m.group(1)), is_bad, m.group(2) + ext
    return 0, is_bad, stem + ext


def _get_duration(filepath: Path) -> float:
    """ffprobe で MP3 の再生時間（秒）を取得"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(filepath)],
            capture_output=True, text=True, timeout=10,
        )
        return float(proc.stdout.strip())
    except Exception:
        return 0.0

def _apply_track_prefix(base_filename: str, likes: int, is_bad: bool) -> str:
    """base_filename に likes 数の z+_ / バッドの x_ を付与"""
    _, _, base = _parse_track_likes(base_filename)  # 既存プレフィックス除去
    prefix = ""
    if is_bad:
        prefix = "x_"
    elif likes > 0:
        prefix = f"{'z' * likes}_"
    return f"{prefix}{base}"


def _is_deleted_track_name(name: str) -> bool:
    return Path(name).stem.startswith("x_")


def _is_usable_audio_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in AUDIO_EXTS
        and not path.name.startswith(".")
        and not _is_deleted_track_name(path.name)
    )


def _schedule_track_file_delete(path: Path, delay_sec: int = 5, *, folder: Optional[Path] = None, base_name: str = "") -> None:
    """× 押下後の猶予を置いて、ローカルファイルを物理削除する。
    同じ base_name のバックアップ/処理済みファイルも消し、後工程に残さない。"""
    path = Path(path)
    folder = Path(folder) if folder else None
    base_name = (base_name or _parse_track_likes(path.name)[2]).strip()

    def _worker():
        time.sleep(delay_sec)
        targets: list[Path] = [path]
        if folder and base_name:
            for d in [folder, folder / "music", folder / "original_music"]:
                if not d.is_dir():
                    continue
                for p in d.iterdir():
                    if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                        continue
                    if _parse_track_likes(p.name)[2] == base_name:
                        targets.append(p)
        seen: set[Path] = set()
        try:
            for p in targets:
                rp = p.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                if p.exists() and p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                    p.unlink()
                    print(f"[track-delete] deleted after {delay_sec}s: {p}", flush=True)
        except Exception as e:
            print(f"[track-delete] failed: {path}: {e}", flush=True)

    threading.Thread(target=_worker, daemon=True, name="track-delayed-delete").start()


def _purge_stale_deleted_tracks(folder: Path, min_age_sec: int = 5) -> int:
    """過去に残った x_ 音声を掃除する。新規×直後の猶予は mtime で守る。"""
    folder = Path(folder)
    deleted = 0
    now = time.time()
    for d in [folder, folder / "music", folder / "original_music"]:
        if not d.is_dir():
            continue
        for p in list(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            if not _is_deleted_track_name(p.name):
                continue
            try:
                if now - p.stat().st_mtime < min_age_sec:
                    continue
                p.unlink()
                deleted += 1
                print(f"[track-delete] purged stale: {p}", flush=True)
            except Exception as e:
                print(f"[track-delete] purge failed: {p}: {e}", flush=True)
    return deleted


@app.get("/api/videos/{video_name}/tracks")
def api_video_tracks(video_name: str):
    """動画フォルダ直下 + music/ + original_music/ の音声ファイルを一覧"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    _purge_stale_deleted_tracks(folder)

    tracks = []
    # スキャン対象: フォルダ直下 → music/ → original_music/
    scan_dirs = [("root", folder)]
    for sub in ["music", "original_music"]:
        sp = folder / sub
        if sp.is_dir():
            scan_dirs.append((sub, sp))

    for location, d in scan_dirs:
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            if p.name.startswith("."):
                continue
            likes, is_bad, base_name = _parse_track_likes(p.name)
            rel = p.relative_to(folder).as_posix()
            duration = _get_duration(p)
            tracks.append({
                "filename": p.name,
                "base_name": base_name,
                "likes": likes,
                "is_bad": is_bad,
                "duration": round(duration, 1),
                "size": p.stat().st_size,
                "location": location,
                "rel_path": rel,
            })
    return {"tracks": tracks}


@app.get("/api/videos/{video_name}/track-file/{rel_path:path}")
def api_video_track_file(video_name: str, rel_path: str):
    """音声ファイル配信 (HTML audio 用)。rel_path は フォルダ相対パス"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    # 安全: "/", ".." の除去チェック
    if ".." in rel_path.split("/"):
        raise HTTPException(400, "不正なパス")
    fp = (folder / rel_path).resolve()
    try:
        fp.relative_to(folder.resolve())
    except ValueError:
        raise HTTPException(400, "フォルダ外アクセス禁止")
    if not fp.exists() or fp.suffix.lower() not in AUDIO_EXTS:
        raise HTTPException(404, "ファイルが見つかりません")
    return FileResponse(fp, media_type="audio/mpeg")


class TrackLikeRequest(BaseModel):
    rel_path: str
    delta: int = 0              # いいね: +1 / -1
    set_to: Optional[int] = None
    set_likes: Optional[int] = None  # set_to のエイリアス
    toggle_bad: Optional[bool] = None  # True で 👎 トグル

@app.post("/api/videos/{video_name}/track-like")
def api_video_track_like(video_name: str, req: TrackLikeRequest):
    """いいね/×変更 → ファイル名をリネーム。
    × は除外マークではなく削除予約として扱い、5秒後に物理削除する。"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)

    src = (folder / req.rel_path).resolve()
    try:
        src.relative_to(folder.resolve())
    except ValueError:
        raise HTTPException(400, "フォルダ外")
    if not src.exists():
        raise HTTPException(404, "ファイルが見つかりません")

    current_likes, current_bad, base_name = _parse_track_likes(src.name)
    delete_after_sec = None

    if req.toggle_bad is not None:
        # UI の × ボタンは「除外」ではなく「削除予約」。
        # すでに x_ の場合も解除せず、そのまま削除を予約する。
        new_bad = True
        new_likes = 0
        delete_after_sec = 5
    else:
        new_bad = current_bad
        if req.set_likes is not None:
            new_likes = max(0, int(req.set_likes))
        elif req.set_to is not None:
            new_likes = max(0, int(req.set_to))
        else:
            new_likes = max(0, current_likes + int(req.delta))
        if new_likes > 0:
            new_bad = False  # いいね付けたらバッド解除

    new_name = _apply_track_prefix(base_name, new_likes, new_bad)
    if new_name == src.name:
        if delete_after_sec:
            try:
                os.utime(src, None)
            except Exception:
                pass
            _schedule_track_file_delete(src, delete_after_sec, folder=folder, base_name=base_name)
        return {"status": "unchanged", "likes": new_likes, "is_bad": new_bad,
                "filename": src.name, "rel_path": req.rel_path,
                "delete_after_sec": delete_after_sec}

    dst = src.parent / new_name
    if dst.exists() and dst.resolve() != src.resolve():
        for i in range(1, 100):
            cand = src.parent / f"{dst.stem}_{i}{dst.suffix}"
            if not cand.exists():
                dst = cand
                break

    src.rename(dst)
    if delete_after_sec:
        try:
            os.utime(dst, None)
        except Exception:
            pass
        _schedule_track_file_delete(dst, delete_after_sec, folder=folder, base_name=base_name)
    rel_new = dst.relative_to(folder).as_posix()
    return {"status": "ok", "likes": new_likes, "is_bad": new_bad,
            "filename": dst.name, "rel_path": rel_new,
            "delete_after_sec": delete_after_sec}


class BulkDeleteRequest(BaseModel):
    mode: str  # "bad" / "no_likes" / "short"
    min_duration: Optional[float] = None  # "short" 時: この秒数以下を削除

@app.post("/api/videos/{video_name}/tracks-bulk-delete")
def api_video_tracks_bulk_delete(video_name: str, req: BulkDeleteRequest):
    """条件に合う楽曲を一括削除"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)

    deleted = 0
    skipped = 0
    scan_dirs = [folder]
    for sub in ["music", "original_music"]:
        sp = folder / sub
        if sp.is_dir():
            scan_dirs.append(sp)

    for d in scan_dirs:
        for p in list(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS or p.name.startswith("."):
                continue
            likes, is_bad, base = _parse_track_likes(p.name)
            should_delete = False

            if req.mode == "bad" and is_bad:
                should_delete = True
            elif req.mode == "no_likes" and likes == 0 and not is_bad:
                should_delete = True
            elif req.mode == "short" and req.min_duration:
                dur = _get_duration(p)
                if dur > 0 and dur < req.min_duration:
                    should_delete = True

            if should_delete:
                try:
                    p.unlink()
                    deleted += 1
                except Exception:
                    skipped += 1

    return {"status": "ok", "deleted": deleted, "skipped": skipped}


@app.delete("/api/videos/{video_name}/track")
def api_video_track_delete(video_name: str, rel_path: str):
    """楽曲ファイルを物理削除"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    if ".." in rel_path.split("/"):
        raise HTTPException(400, "不正なパス")
    fp = (folder / rel_path).resolve()
    try:
        fp.relative_to(folder.resolve())
    except ValueError:
        raise HTTPException(400, "フォルダ外アクセス禁止")
    if not fp.exists() or fp.suffix.lower() not in AUDIO_EXTS:
        raise HTTPException(404, "ファイルが存在しません")
    try:
        fp.unlink()
        return {"status": "ok", "deleted": rel_path}
    except Exception as e:
        raise HTTPException(500, f"削除失敗: {e}")


# ─── API: 楽曲 後処理（リネーム + FFmpeg）───

PROCESS_TRACKS_SCRIPT = SHARED_BASE / "Python" / "app_process_tracks.py"

@app.post("/api/videos/{video_name}/process-tracks")
async def api_video_process_tracks(video_name: str, rename_only: bool = False,
                                   apply_tags: bool = False,
                                   keep_names: bool = False,
                                   genre: Optional[str] = None,
                                   album: Optional[str] = None,
                                   artist: Optional[str] = None):
    """Claude CLI でタイトル提案 + ffmpeg で無音トリム+フェードアウト+ゲイン正規化 → music/ に出力

    Query param:
      - `rename_only=true` でリネームのみ（ffmpeg スキップ）
      - `apply_tags=true` で後処理後に公開前整備（透かし除去+ID3タグ+ファイル名正規化）。
        既定 false=従来挙動（整備しない）。
      - `keep_names=true` でタイトル再生成（サムネ/ペルソナ由来）をスキップし既存
        ファイル名（draft 正式名）を維持。曲別 genre を draft から効かせたいとき用。
      - `genre` / `album` / `artist` は apply_tags 時の ID3。未指定は channel config
        の suno.tag_defaults → フォルダ連番由来で補完。
    """
    await _ensure_not_running("process", "後処理が既に実行中です")
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cmd = [sys.executable, "-u", str(PROCESS_TRACKS_SCRIPT), str(folder),
           "--cli", cli_cmd]
    if rename_only:
        cmd += ["--rename-only"]
    if keep_names:
        cmd += ["--keep-names"]
    if apply_tags:
        cmd += ["--apply-tags"]
        # channel config の suno.tag_defaults を既定値に
        td = ((load_channel_config().get("suno") or {}).get("tag_defaults") or {})
        eff_artist = artist or td.get("artist") or "SUKIMA"
        cmd += ["--artist", eff_artist]
        if album or td.get("album_template"):
            # album 明示が優先。template の {N} はフォルダ連番で置換。
            eff_album = album
            if not eff_album and td.get("album_template"):
                m = re.match(r"^(\d+)_", video_name)
                if m:
                    eff_album = str(td["album_template"]).replace("{N}", m.group(1))
            if eff_album:
                cmd += ["--album", eff_album]
        eff_genre = genre or td.get("genre_default")
        if eff_genre:
            cmd += ["--genre", eff_genre]
        gbk = td.get("genre_by_kind")
        if isinstance(gbk, dict) and gbk:
            cmd += ["--genre-map", json.dumps(gbk, ensure_ascii=False)]
    task_logs["process"] = []
    import datetime as _dt
    task_meta["process"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "video_name": video_name,
    }
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    active_tasks["process"] = proc
    _stream_subprocess(proc, "process")
    return {"status": "started"}

@app.get("/api/process/status")
def api_process_status():
    proc = active_tasks.get("process")
    running = proc is not None and proc.returncode is None
    return {"running": running, "logs": task_logs.get("process", [])[-500:]}


# ─── API: 画像選択（JSX 連動）───

IMAGE_EXTS = (".jpg", ".jpeg", ".png")

@app.get("/api/videos/{video_name}/images")
def api_video_images(video_name: str):
    """動画フォルダ内の画像一覧を返す。サムネ判定＋選択状態も返却。"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
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


@app.get("/api/videos/{video_name}/image-file/{filename:path}")
def api_video_image_file(video_name: str, filename: str):
    """動画フォルダ内の画像を配信（サムネ・背景プレビュー用）"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
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

@app.put("/api/videos/{video_name}/selected-images")
def api_update_selected_images(video_name: str, req: SelectedImagesUpdate):
    """選択画像を selected_images.json に保存（JSX が読み込む）"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
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

@app.delete("/api/videos/{video_name}/selected-images")
def api_delete_selected_images(video_name: str):
    """選択状態をリセット（JSX は初期値フォールバック）"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    sel_file = folder / "selected_images.json"
    if sel_file.exists():
        sel_file.unlink()
    return {"status": "ok"}

# ─── API: Claude 提案（タイトル・説明・タグ）───

class SuggestRequest(BaseModel):
    mode: str                       # "titles" | "description" | "tags"
    count: Optional[int] = 5        # titles 用
    reference: Optional[str] = None # description 用（任意の参考文）

@app.post("/api/videos/{video_name}/suggest")
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
    folder = Path(config["channel_folder"]) / video_name
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

# ─── API: パイプライン自動実行 ───
# PIPELINE_SCRIPT は app_core へ移動（pipeline/images の両ドメインが参照する共有定数・D9）

class PipelineRunRequest(BaseModel):
    steps: List[str]  # ["suno", "rename", "premiere", "meta", "upload"]
    duration: Optional[int] = 10800
    suno_count: Optional[int] = None
    privacy: Optional[str] = "unlisted"
    # SUNO 上書き（指定があればフォーム値を pipeline に反映）
    suno_prompt: Optional[str] = None
    suno_interval: Optional[int] = None
    suno_provider: Optional[str] = None
    suno_mode: Optional[str] = None  # styles_title_only / lyrics_styles / lyrics
    suno_batch: Optional[bool] = None
    suno_headless: Optional[bool] = None
    # 多様性制御（チャンネル別。明示があれば自動化設定より優先）
    diversity_threshold: Optional[float] = None
    diversity_retry: Optional[int] = None
    history_limit: Optional[int] = None
    # DL + 整理
    dl_wait_sec: Optional[int] = None
    dl_min_duration: Optional[int] = None

@app.post("/api/videos/{video_name}/run-pipeline")
async def api_run_pipeline(video_name: str, req: PipelineRunRequest):
    """チェックした工程を自動実行"""
    await _ensure_not_running("pipeline", "パイプラインが既に実行中です")
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    # vol 番号を取得
    m = re.match(r"^(\d+)_", video_name)
    vol = m.group(1) if m else "0"

    # チャンネル別の自動化設定を既定値として取り込み（リクエストに明示があればそちらを優先）
    auto_cfg = {}
    ap = _automation_config_path()
    if ap.exists():
        try:
            auto_cfg = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:
            auto_cfg = {}
    def _pick(req_val, auto_key):
        return req_val if req_val is not None else auto_cfg.get(auto_key)
    dur = _pick(req.duration if req.duration != 10800 else None, "premiere_duration") or req.duration or 10800
    privacy = _pick(req.privacy if req.privacy != "unlisted" else None, "upload_privacy") or req.privacy or "unlisted"

    cmd = [sys.executable, "-u", str(PIPELINE_SCRIPT), vol,
           "--via-api", "--duration", str(dur),
           "--privacy", privacy]
    # --only は使えないので、pipeline.py に --steps を追加する必要がある
    # 簡易対応: 全ステップを渡す (pipeline 側で対応)
    task_logs["pipeline"] = []
    import datetime as _dt
    task_meta["pipeline"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "video_name": video_name,
        "steps": req.steps,
    }
    # steps を環境変数で渡す
    env = {**os.environ, "PYTHONUNBUFFERED": "1",
           "APP_PIPELINE_STEPS": ",".join(req.steps)}
    # SUNO 上書き（リクエスト > 自動化設定 > デフォルト の優先順位）
    def _env(key_env, req_val, auto_key, cast=str):
        v = req_val if req_val not in (None, "") else auto_cfg.get(auto_key)
        if v is not None:
            env[key_env] = cast(v) if cast is not bool else ("1" if v else "0")
    _env("APP_SUNO_PROMPT",   req.suno_prompt,   "suno_prompt")
    _env("APP_SUNO_COUNT",    req.suno_count,    "suno_count", str)
    _env("APP_SUNO_INTERVAL", req.suno_interval, "suno_interval", str)
    _env("APP_SUNO_PROVIDER", req.suno_provider, "suno_provider")
    _env("APP_SUNO_MODE",     req.suno_mode,     "suno_mode")
    _env("APP_SUNO_BATCH",    req.suno_batch,    "suno_batch", bool)
    _env("APP_SUNO_HEADLESS", req.suno_headless, "suno_headless", bool)
    _env("APP_SUNO_DIVERSITY_THRESHOLD", req.diversity_threshold, "suno_diversity_threshold", str)
    _env("APP_SUNO_DIVERSITY_RETRY",     req.diversity_retry,     "suno_diversity_retry", str)
    _env("APP_SUNO_HISTORY_LIMIT",       req.history_limit,       "suno_history_limit", str)
    _env("APP_DL_WAIT_SEC",   req.dl_wait_sec,   "dl_wait_sec", str)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=env)
    active_tasks["pipeline"] = proc
    _stream_subprocess(proc, "pipeline")
    return {"status": "started", "steps": req.steps, "vol": vol}

class CreateFromBenchmarkRequest(BaseModel):
    publish_date: str
    use_auto_approval: Optional[bool] = None  # None=設定値を採用
    duration: Optional[int] = None
    privacy: Optional[str] = None
    suno_prompt: Optional[str] = None  # 未指定なら競合分析から自動生成
    suno_count: Optional[int] = None

@app.post("/api/pipeline/create-from-benchmark")
async def api_create_from_benchmark(req: CreateFromBenchmarkRequest):
    """ベンチマーク分析から制作を単一アクションで駆動。

    1. 新 vol フォルダ作成（/api/videos/create 相当）
    2. 競合分析キャッシュから SUNO プロンプト提案（未指定時）
    3. 自動承認モードなら全工程（suno → rename → premiere → export → meta → upload）を起動
    4. OFF なら 1 の vol 情報だけ返して UI 側で個別工程を回す
    """
    await _ensure_not_running("pipeline", "パイプラインが既に実行中です")
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    if not channel_dir.exists():
        raise HTTPException(400, "チャンネルフォルダが存在しません")

    auto_cfg = {}
    ap = _automation_config_path()
    if ap.exists():
        try:
            auto_cfg = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:
            auto_cfg = {}
    auto_approval = req.use_auto_approval if req.use_auto_approval is not None else bool(auto_cfg.get("auto_approval_mode"))

    vf = api_create_video_folder(VideoFolderCreate(publish_date=req.publish_date, open_in_finder=False))
    video_name = vf["folder"]

    suno_prompt = req.suno_prompt
    suno_rationale = None
    if not suno_prompt:
        try:
            import app_competitor as _ac
            cache = _ac.load_cache() or {}
            analysis = cache.get("analysis", {})
            if analysis.get("music_direction"):
                from app_benchmark_analyze import propose_suno_prompt
                suno_cfg_local = get_suno_config()
                cli_cmd = suno_cfg_local.get("claude_cli") or "claude"
                proposal = propose_suno_prompt(analysis, cli_cmd=cli_cmd)
                suno_prompt = proposal.get("prompt")
                suno_rationale = proposal.get("rationale")
        except Exception as e:
            suno_rationale = f"（SUNO 自動提案失敗、既定プロンプト使用）: {e}"

    if not auto_approval:
        return {
            "status": "created",
            "video_name": video_name,
            "vol": vf["num"],
            "auto_approval": False,
            "suno_prompt": suno_prompt,
            "suno_rationale": suno_rationale,
            "hint": "自動承認モードが OFF のため、フォルダだけ作成しました。個別工程を手動で進めてください。",
        }

    steps = ["suno", "rename", "premiere", "export", "meta", "upload"]
    pipeline_req = PipelineRunRequest(
        steps=steps,
        duration=req.duration or auto_cfg.get("premiere_duration") or 10800,
        privacy=req.privacy or auto_cfg.get("upload_privacy") or "unlisted",
        suno_prompt=suno_prompt,
        suno_count=req.suno_count,
    )
    await api_run_pipeline(video_name, pipeline_req)
    return {
        "status": "started",
        "video_name": video_name,
        "vol": vf["num"],
        "auto_approval": True,
        "suno_prompt": suno_prompt,
        "suno_rationale": suno_rationale,
        "steps": steps,
    }


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


@app.post("/api/suno/generate-mix-drafts")
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


# ─── 一気通貫オーケストレーション（投稿手前まで） ───
# SUKIMA の一連（フォルダ作成→混成ドラフト→SUNO投入→DL→整備→bgimage→psd→
# premiere→export→qa→meta→thumbnail）を 1 アクションで駆動する入口。
# ⚠ auto_upload は厳守で false。upload step は絶対に呼ばない（投稿は手動）。
# ⚠ dry_run=true では SUNO ブラウザ投入 / Premiere / export を実行せず、
#    「どの工程がどの順序で呼ばれるか」だけをログに記録して返す（配線検証用）。

# pipeline に委譲する後半工程（SUNO/DL/整備の後）。upload は含めない（投稿手前で停止）。
_ORCHESTRATE_PIPELINE_STEPS = [
    "bgimage", "psd_composite", "premiere", "export", "qa", "meta", "thumbnail",
]


class OrchestrateRequest(BaseModel):
    publish_date: Optional[str] = None    # 発行日（vol フォルダ命名用）。未指定は今日。
    duration: Optional[int] = None        # Premiere 尺秒。未指定は channel default_duration_sec。
    privacy: Optional[str] = "unlisted"
    dry_run: bool = True                  # 既定 true（実走は明示 false ＋ ユーザー合意が必要）
    suno_interval: Optional[int] = None   # SUNO 投入間隔秒（既定は cozy 運用の 90）
    provider: Optional[str] = None        # 混成ドラフト LLM provider


@app.post("/api/orchestrate/create-and-run")
async def api_orchestrate_create_and_run(req: OrchestrateRequest):
    """SUKIMA 一連を「投稿手前まで」一気通貫で駆動する統合エンドポイント。

    工程順:
      1. フォルダ作成 + テンプレ自動コピー（/api/videos/create 流用）
      2. 混成ドラフト生成（suno.cozy_mix → generate_mixed_drafts）
      3. SUNO 投入（songs_draft_json 経路、--songs-file）  ※dry_run はスキップ
      4. DL（/api/suno/download）                         ※dry_run はスキップ
      5. 整備（process-tracks: apply_tags=true, keep_names=true）※dry_run はスキップ
      6. 後半 pipeline（bgimage→psd_composite→premiere→export→qa→meta→thumbnail）
         を APP_PIPELINE_STEPS で委譲。upload は含めない。      ※dry_run はスキップ

    auto_upload は常に false（upload step を渡さない）。dry_run=true では実走系
    （SUNO ブラウザ投入 / DL / 整備 / Premiere / export）を一切実行せず、
    呼び出す工程の順序とパラメータを plan として返す（配線検証用）。
    """
    config = get_dashboard_config()
    channel_dir = Path(config.get("channel_folder") or "")
    if not channel_dir.exists():
        raise HTTPException(400, "チャンネルフォルダが存在しません")

    channel_config = load_channel_config()
    cozy = ((channel_config.get("suno") or {}).get("cozy_mix") or {})
    if not cozy:
        raise HTTPException(
            400,
            "このチャンネルには suno.cozy_mix（混成 Style 定義）がありません。"
            "一気通貫は cozy_mix を持つチャンネル（SUKIMA 等）でのみ実行できます。",
        )

    import datetime as _dt
    publish_date = req.publish_date or _dt.date.today().strftime("%Y-%m-%d")
    interval = req.suno_interval if req.suno_interval is not None else 90
    duration = req.duration or int(channel_config.get("default_duration_sec") or 3700)
    privacy = req.privacy or "unlisted"

    # 実行する工程プラン（dry/実走 共通で記録）。auto_upload は常に false。
    plan = {
        "channel_folder": str(channel_dir),
        "publish_date": publish_date,
        "dry_run": req.dry_run,
        "auto_upload": False,
        "steps": [
            {"step": "create_folder", "detail": "vol フォルダ作成＋テンプレ(prproj/psd)自動コピー"},
            {"step": "generate_mix_drafts", "detail": f"cozy_mix 混成ドラフト生成（provider={req.provider or 'codex(既定)'}）"},
            {"step": "suno_start", "detail": f"songs_draft_json 投入（--songs-file, interval={interval}s）", "browser": True},
            {"step": "suno_download", "detail": "Workspace DL → 動画フォルダ", "browser": True},
            {"step": "process_tracks", "detail": "整備（apply_tags=true, keep_names=true）"},
            {"step": "pipeline", "detail": "後半工程委譲", "pipeline_steps": list(_ORCHESTRATE_PIPELINE_STEPS), "premiere": True},
        ],
        "pipeline_steps": list(_ORCHESTRATE_PIPELINE_STEPS),
        "upload_step_included": "upload" in _ORCHESTRATE_PIPELINE_STEPS,  # 常に False を期待
        "duration_sec": duration,
        "privacy": privacy,
    }

    # ── Step 1: フォルダ作成＋テンプレコピー（dry でも作成する＝後段の検証に使うため安全な操作）──
    vf = api_create_video_folder(VideoFolderCreate(publish_date=publish_date, open_in_finder=False))
    video_name = vf["folder"]
    plan["video_name"] = video_name
    plan["vol"] = vf["num"]
    plan["create_warnings"] = vf.get("warnings", [])
    plan["create_copied"] = vf.get("created", [])

    if req.dry_run:
        # dry_run: 実走系（SUNO投入/DL/整備/Premiere/export）は一切呼ばない。
        # フォルダだけ作って、どの工程がどの順序・どのパラメータで呼ばれるかを返す。
        plan["status"] = "dry_run"
        plan["note"] = (
            "dry_run=true のためフォルダ作成のみ実行。SUNO ブラウザ投入・DL・整備・"
            "Premiere・export・upload は呼んでいません。実走は dry_run=false ＋ "
            "ユーザー合意（SUNO 起動 / Premiere 実走）後に行ってください。"
        )
        return plan

    # ── 実走（dry_run=false）。⚠ SUNO ブラウザ・Premiere が動く。要ユーザー合意。──
    await _ensure_not_running("pipeline", "パイプラインが既に実行中です")
    await _ensure_not_running("suno", "SUNO が既に実行中です")

    # workspace 解決
    channel_name = (config.get("channel_name") or "orzz").strip()
    channel_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", channel_name).strip("_") or "orzz"
    workspace = f"{channel_slug}_vol{vf['num']}"

    # Step 2: 混成ドラフト生成
    sc = get_suno_config()

    def _gen():
        sys.path.insert(0, str(SUNO_SCRIPT.parent))
        import suno_auto_create as _suno
        return _suno.generate_mixed_drafts(
            channel_config, provider=req.provider,
            claude_cli=sc.get("claude_cli"), codex_cli=sc.get("codex_cli"),
            workspace=workspace,
        )

    try:
        songs = await asyncio.to_thread(_gen)
    except Exception as e:
        raise HTTPException(500, f"混成ドラフト生成に失敗しました: {e}")
    if not songs:
        raise HTTPException(500, "混成ドラフトが 0 曲でした。cozy_mix 設定を確認してください。")
    plan["draft_count"] = len(songs)

    # Step 3: SUNO 投入（songs_draft_json 経路）。共通の合意済みドラフト投入と同じ start。
    suno_mode = ((channel_config.get("suno") or {}).get("generation_mode") or "lyrics_styles")
    start_prompt = f"[mix] {workspace} cozy_mix {len(songs)} songs（混成ドラフト投入）"
    await api_suno_start(SunoRunRequest(
        prompt=start_prompt,
        songs_draft_json=songs,
        generation_mode=suno_mode,
        interval=interval,
        workspace=workspace,
        video_name=video_name,
    ))

    plan["status"] = "started"
    plan["note"] = (
        "SUNO 投入を開始しました。DL・整備・後半 pipeline は SUNO 完了後に "
        "各 step の status を見て順次起動してください（無人連鎖はこのエンドポイント"
        "では行いません）。upload は含めていません。"
    )
    plan["next_steps"] = {
        "download": {"endpoint": "/api/suno/download", "body": {"video_name": video_name}},
        "process_tracks": {"endpoint": f"/api/videos/{video_name}/process-tracks",
                            "query": {"apply_tags": "true", "keep_names": "true"}},
        "pipeline": {"endpoint": f"/api/videos/{video_name}/run-pipeline",
                     "body": {"steps": list(_ORCHESTRATE_PIPELINE_STEPS),
                              "duration": duration, "privacy": privacy}},
    }
    return plan


@app.get("/api/pipeline/status")
def api_pipeline_status():
    proc = active_tasks.get("pipeline")
    running = proc is not None and proc.returncode is None
    return {"running": running, "logs": task_logs.get("pipeline", [])[-200:],
            "meta": task_meta.get("pipeline", {})}


# ─── API: 競合分析 ───

@app.post("/api/analysis/competitors")
async def api_analysis_competitors():
    """ライバルチャンネルを YouTube API で取得 → Claude 分析 → キャッシュ"""
    await _ensure_not_running("analysis", "分析が既に実行中です")
    script = SHARED_BASE / "Python" / "app_competitor.py"
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cmd = [sys.executable, "-u", str(script), "--analyze", "--cli", cli_cmd]
    task_logs["analysis"] = []
    import datetime as _dt
    task_meta["analysis"] = {"started_at": _dt.datetime.now().isoformat()}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    active_tasks["analysis"] = proc
    _stream_subprocess(proc, "analysis")
    return {"status": "started"}


@app.get("/api/analysis/status")
def api_analysis_status():
    proc = active_tasks.get("analysis")
    running = proc is not None and proc.returncode is None
    return {"running": running, "logs": task_logs.get("analysis", [])[-200:]}


@app.get("/api/analysis/spreadsheet-preview")
def api_spreadsheet_preview():
    """スプシ接続テスト: チャンネル数 + ホットチャンネル上位 5"""
    config = get_dashboard_config()
    detail_url = config.get("spreadsheet_channel_detail_url", "").strip()
    growth_url = config.get("spreadsheet_growth_tracking_url", "").strip()
    result = {"detail": None, "growth": None}

    if detail_url:
        try:
            from app_sheets import fetch_csv, parse_channel_detail
            rows = fetch_csv(detail_url)
            channels = parse_channel_detail(rows)
            result["detail"] = {
                "channel_count": len(channels),
                "sample_names": [c.title for c in channels[:5]],
            }
        except Exception as e:
            result["detail"] = {"error": str(e)}

    if growth_url:
        try:
            from app_sheets import fetch_csv, parse_growth_tracking, identify_hot_channels
            rows = fetch_csv(growth_url)
            entries = parse_growth_tracking(rows)
            hot = identify_hot_channels(entries, top_n=5)
            result["growth"] = {
                "channel_count": len(entries),
                "hot_channels": [
                    {"name": e.channel_name, "growth_rate": e.growth_rate,
                     "daily_views": e.daily_view_change}
                    for e in hot
                ],
            }
        except Exception as e:
            result["growth"] = {"error": str(e)}

    return result


# ─── チャンネルアイコン取得（YouTube ページから og:image 系をスクレイプ） ───
# スプシの =IMAGE() は CSV/JSON 出力で値が消えるため、URL から直接取得してキャッシュする。
_CHANNEL_ICON_CACHE_FILE = lambda: CONFIG_DIR / "channel_icon_cache.json"
_CHANNEL_ICON_TTL_SEC = 86400 * 7  # 7 日
_AVATAR_RE = re.compile(
    r'(https?://(?:[a-z0-9-]+\.)?(?:ggpht|googleusercontent)\.com/[A-Za-z0-9_\-]+=s\d+[A-Za-z0-9_\-=.@?]*)'
)


def _load_icon_cache() -> dict:
    p = _CHANNEL_ICON_CACHE_FILE()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_icon_cache(cache: dict) -> None:
    try:
        _CHANNEL_ICON_CACHE_FILE().write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _scrape_channel_icon(channel_url: str) -> str:
    """YouTube チャンネルページから アバター画像 URL を取得。失敗時は空文字。"""
    if not channel_url or "youtube.com" not in channel_url:
        return ""
    import urllib.request
    try:
        req = urllib.request.Request(
            channel_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read(1_500_000).decode("utf-8", errors="replace")
    except Exception:
        return ""
    m = _AVATAR_RE.search(html)
    if not m:
        return ""
    url = m.group(1)
    # 大きいサイズ希望（=s900 → そのまま、=s48 等が来たら s176 に上げる）
    return re.sub(r"=s\d+", "=s176", url)


def resolve_channel_icon(channel_url: str) -> str:
    """キャッシュ込みのアイコン URL 解決。"""
    if not channel_url:
        return ""
    cache = _load_icon_cache()
    entry = cache.get(channel_url)
    now = time.time()
    if entry and isinstance(entry, dict):
        ts = entry.get("fetched_at", 0)
        if now - ts < _CHANNEL_ICON_TTL_SEC and entry.get("icon_url"):
            return entry["icon_url"]
    # 取得して保存
    icon = _scrape_channel_icon(channel_url)
    cache[channel_url] = {"icon_url": icon, "fetched_at": now}
    _save_icon_cache(cache)
    return icon


@app.get("/api/analysis/hot-channels")
def api_hot_channels(top_n: int = 10):
    """スプシからホットチャンネルだけ取得（分析なし）。Sheet1 から最新動画サムネ等を補完。"""
    bc = get_benchmark_config()
    config = get_dashboard_config()
    growth_url = (bc.get("spreadsheet_growth_tracking_url") or config.get("spreadsheet_growth_tracking_url", "")).strip()
    detail_url = (bc.get("spreadsheet_channel_detail_url") or config.get("spreadsheet_channel_detail_url", "")).strip()
    if not growth_url:
        raise HTTPException(400, "成長トラッキングシート URL が未設定です")
    try:
        from app_sheets import (
            fetch_csv, parse_growth_tracking, identify_hot_channels,
            parse_channel_detail, match_channels, SheetFetchError,
        )
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
        hot = identify_hot_channels(entries, top_n=top_n)
        # Sheet1 で詳細補完
        details = []
        if detail_url:
            try:
                d_rows = fetch_csv(detail_url)
                details = parse_channel_detail(d_rows)
            except Exception:
                details = []
        detail_by_name = {ch.title: ch for ch in details}
        # YouTube 動画 URL から video_id を抽出してサムネ URL を組み立てる
        # （スプシに =IMAGE() の thumb 列が無い時のフォールバック）
        _vid_re = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")
        def _yt_thumb(video_url: str) -> str:
            if not video_url:
                return ""
            m = _vid_re.search(video_url)
            if not m:
                return ""
            return f"https://i.ytimg.com/vi/{m.group(1)}/maxresdefault.jpg"
        out = []
        for e in hot:
            match = detail_by_name.get(e.channel_name)
            if not match and details:
                match = match_channels(e.channel_name, details)
            latest = None
            top_video = None
            channel_url = ""
            icon_url = ""
            if match:
                channel_url = match.url
                icon_url = match.icon_url or ""
                # スプシの =IMAGE() は CSV エクスポートで値が消えるため、
                # icon_url が空なら YouTube ページからスクレイプ（キャッシュ付き）
                if not icon_url and channel_url:
                    try:
                        icon_url = resolve_channel_icon(channel_url)
                    except Exception:
                        icon_url = ""
                if match.recent_videos:
                    v = match.recent_videos[0]
                    latest = {"title": v.title,
                              "thumb_url": v.thumbnail or _yt_thumb(v.url),
                              "published_at": v.publish_date, "url": v.url, "views": v.view_count}
                if match.top_videos:
                    tv = match.top_videos[0]
                    top_video = {"title": tv.title,
                                 "thumb_url": tv.thumbnail or _yt_thumb(tv.url),
                                 "url": tv.url, "views": tv.view_count}
            out.append({
                "name": e.channel_name,
                "growth_rate": e.growth_rate,
                "weekly_growth_pct": round(e.growth_rate * 7, 2),
                "recent_views_7d": e.daily_view_change * 7,
                "daily_views": e.daily_view_change,
                "daily_subs": e.daily_sub_change,
                "total_views": e.total_views,
                "subscribers": e.subscribers,
                "last_updated": e.last_updated,
                "score": round(e.score, 3),
                "channel_url": channel_url,
                "icon_url": icon_url,
                "latest_video": latest,
                "top_video": top_video,
            })
        return {"hot_channels": out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/analysis/overview")
def api_analysis_overview():
    """ベンチマーク全体の重点指標サマリ。StatCard 用。"""
    bc = get_benchmark_config()
    config = get_dashboard_config()
    growth_url = (bc.get("spreadsheet_growth_tracking_url") or config.get("spreadsheet_growth_tracking_url", "")).strip()
    if not growth_url:
        return {"total_channels": 0, "top_growth": [], "top_recent_views": [], "updated_at": "", "configured": False}
    try:
        from app_sheets import fetch_csv, parse_growth_tracking, SheetFetchError
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    if not entries:
        return {"total_channels": 0, "top_growth": [], "top_recent_views": [], "updated_at": "", "configured": True}
    top_growth = sorted(entries, key=lambda e: e.growth_rate, reverse=True)[:3]
    top_recent = sorted(entries, key=lambda e: e.daily_view_change, reverse=True)[:3]
    updated = max((e.last_updated for e in entries if e.last_updated), default="")
    return {
        "total_channels": len(entries),
        "top_growth": [{"name": e.channel_name, "growth_rate": e.growth_rate,
                        "subscribers": e.subscribers} for e in top_growth],
        "top_recent_views": [{"name": e.channel_name, "daily_views": e.daily_view_change,
                              "subscribers": e.subscribers} for e in top_recent],
        "updated_at": updated,
        "configured": True,
    }


def _resolve_growth_url() -> str:
    """成長トラッキング（CHANNEL_TRACK + 個別 TRACK_ タブ）スプシ URL を解決。"""
    bc = get_benchmark_config()
    config = get_dashboard_config()
    return (bc.get("spreadsheet_growth_tracking_url")
            or config.get("spreadsheet_growth_tracking_url", "")).strip()


@app.get("/api/analysis/channel-list")
def api_channel_list():
    """CHANNEL_TRACK マスタの全チャンネル一覧（UI ピッカー用・15列フル）。

    各 entry に新着判定（is_new/days_tracked）を付与して返す。個別タブの時系列は
    重いので含めない（必要時に channel-timeline をオンデマンドで叩く）。
    """
    growth_url = _resolve_growth_url()
    if not growth_url:
        return {"configured": False, "channels": []}
    try:
        from app_sheets import (
            fetch_csv, parse_growth_tracking, detect_new_channels, SheetFetchError,
        )
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
        detect_new_channels(entries)  # is_new / days_tracked を副作用付与
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    chans = [{
        "name": e.channel_name,
        "subscribers": e.subscribers,
        "total_views": e.total_views,
        "daily_subs": e.daily_sub_change,
        "daily_views": e.daily_view_change,
        "growth_rate": e.growth_rate,
        "video_count": e.video_count,
        "last_post_date": e.last_post_date,
        "recent5_avg_views": e.recent5_avg_views,
        "weekly_growth_rate": e.weekly_growth_rate,
        "monthly_growth_rate": e.monthly_growth_rate,
        "tracking_start": e.tracking_start,
        "last_updated": e.last_updated,
        "is_new": e.is_new,
        "days_tracked": e.days_tracked,
    } for e in entries]
    chans.sort(key=lambda c: c["subscribers"], reverse=True)
    return {"configured": True, "count": len(chans), "channels": chans}


@app.get("/api/analysis/new-channels")
def api_new_channels(within_days: int = 14):
    """追跡開始が直近 within_days 日以内の新着チャンネル（日次登録増順）。"""
    growth_url = _resolve_growth_url()
    if not growth_url:
        return {"configured": False, "new_channels": []}
    try:
        from app_sheets import (
            fetch_csv, parse_growth_tracking, detect_new_channels, SheetFetchError,
        )
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
        new = detect_new_channels(entries, new_within_days=within_days)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"configured": True, "within_days": within_days, "count": len(new),
            "new_channels": [{
                "name": e.channel_name,
                "subscribers": e.subscribers,
                "daily_subs": e.daily_sub_change,
                "daily_views": e.daily_view_change,
                "tracking_start": e.tracking_start,
                "days_tracked": e.days_tracked,
                "last_post_date": e.last_post_date,
                "video_count": e.video_count,
            } for e in new]}


@app.get("/api/analysis/channel-timeline")
def api_channel_timeline(name: str, spreadsheet: str = ""):
    """個別 TRACK_<name> タブ（1日4回取得の時系列）をオンデマンド取得して指標化。

    spreadsheet 省略時は設定中の成長トラッキング URL を使用。個別タブはこのブックに
    のみ存在するため、旧スプシ設定のままだと「タブが見つかりません」になる（切替後に有効）。
    """
    from dataclasses import asdict
    src = (spreadsheet or _resolve_growth_url()).strip()
    if not src:
        raise HTTPException(400, "成長トラッキングシート URL が未設定です")
    if not name.strip():
        raise HTTPException(400, "チャンネル名を指定してください")
    try:
        from app_sheets import fetch_channel_timeline
        tl = fetch_channel_timeline(src, name.strip())
    except Exception as e:
        raise HTTPException(500, str(e))
    data = asdict(tl)
    if tl.error:
        # 404 ではなく 200 + error フィールドで返す（UI が個別にハンドリング）
        data["ok"] = False
    else:
        data["ok"] = True
    return data


@app.get("/api/analysis/tracking-events")
def api_tracking_events(record: int = 0):
    """日次スナップショット差分（新着ch / 新作投稿 / 急伸）を返す。

    record=1 のとき今日のスナップショットを保存してから差分を計算（日次更新フック用）。
    record=0（既定）は保存済みスナップショットを使った読み取りのみ（最新2件の差分）。
    """
    growth_url = _resolve_growth_url()
    if not growth_url:
        return {"configured": False, "events": None}
    try:
        from app_sheets import (
            fetch_csv, parse_growth_tracking, record_daily_snapshot,
            snapshot_growth, load_latest_snapshot, diff_snapshots, SheetFetchError,
        )
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
        if record:
            result = record_daily_snapshot(entries)
            events = result["events"]
            first_run = result["first_run"]
        else:
            # 保存せず、現在値 vs 直近保存スナップショットで差分のみ
            curr = snapshot_growth(entries)
            prev = load_latest_snapshot(before=curr["date"]) or load_latest_snapshot()
            if prev:
                events = diff_snapshots(prev, curr)
                first_run = False
            else:
                events = {"prev_date": None, "curr_date": curr["date"],
                          "new_channels": [], "dropped_channels": [],
                          "new_videos": [], "surging": []}
                first_run = True
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"configured": True, "recorded": bool(record),
            "first_run": first_run, "events": events}


@app.get("/api/analysis/cache")
def api_analysis_cache():
    """キャッシュされた分析結果を返す"""
    try:
        import app_competitor as _ac
        data = _ac.load_cache()
        if not data:
            return {"cached": False}
        return {"cached": True, **data}
    except Exception:
        return {"cached": False}

@app.delete("/api/analysis/cache")
def api_delete_analysis_cache():
    """キャッシュ削除（再分析を強制するため。英語メタデータ生成向けの再生成等に使用）"""
    cache_file = SHARED_CONFIG_DIR / "competitor_analysis_cache.json"
    try:
        from app_channel_cache import delete_scoped_and_legacy
        return {"status": "ok", "deleted": delete_scoped_and_legacy("competitor_analysis_cache.json", cache_file)}
    except Exception:
        if cache_file.exists():
            try:
                cache_file.unlink()
                return {"status": "ok", "deleted": True}
            except Exception as e:
                raise HTTPException(500, f"削除失敗: {e}")
        return {"status": "ok", "deleted": False}


# ─── ベンチマーク分析軸は routers/benchmark.py へ分離（D9 第2段）───
from routers.benchmark import router as _benchmark_router
app.include_router(_benchmark_router)


# ─── API: ベンチマーク詳細表示用（Sprint 5-A） ───
# Sheet1 を読み込み、ピン留め or hot 上位の TOP/最新動画 + メタ集計を返す

def _benchmark_target_channels():
    """benchmark_config.pinned_names + 必要なら hot ranking から対象チャンネル名を返す。"""
    bc = get_benchmark_config()
    pinned = list(bc.get("pinned_names") or [])
    if pinned:
        return pinned[:8]
    # Pin 無ければ hot top 5 をフォールバック
    try:
        hot = api_hot_channels(top_n=5)
        return [h["name"] for h in hot.get("hot_channels", [])][:5]
    except Exception:
        return []

def _load_sheet1_channels():
    """Sheet1（channel_detail）を読み込んで {title: ChannelDetail} の dict を返す。"""
    bc = get_benchmark_config()
    config = get_dashboard_config()
    detail_url = (bc.get("spreadsheet_channel_detail_url") or config.get("spreadsheet_channel_detail_url", "")).strip()
    if not detail_url:
        return {}
    from app_sheets import fetch_csv, parse_channel_detail
    try:
        rows = fetch_csv(detail_url)
        details = parse_channel_detail(rows)
        return {ch.title: ch for ch in details}
    except Exception:
        return {}

@app.get("/api/analysis/benchmark-videos")
def api_benchmark_videos():
    """適用中のベンチマーク（pinned or hot top）の TOP 動画 + 最新動画を返す。動画詳細サイドパネル用。"""
    targets = _benchmark_target_channels()
    if not targets:
        return {"channels": [], "hint": "ピン留めも hot ランキングも空です。設定タブでベンチマーク対象を指定してください"}
    detail_by_name = _load_sheet1_channels()
    out = []
    for name in targets:
        ch = detail_by_name.get(name)
        if not ch:
            # fuzzy match
            from app_sheets import match_channels
            ch = match_channels(name, list(detail_by_name.values()))
        if not ch:
            out.append({"name": name, "found": False})
            continue
        # TOP 動画 3 件 + 最新 3 件
        top_v = [{"title": v.title, "thumb_url": v.thumbnail, "url": v.url,
                  "published_at": v.publish_date, "views": v.view_count,
                  "likes": v.like_count, "comments": v.comment_count}
                 for v in (ch.top_videos or [])[:3]]
        recent_v = [{"title": v.title, "thumb_url": v.thumbnail, "url": v.url,
                     "published_at": v.publish_date, "views": v.view_count}
                    for v in (ch.recent_videos or [])[:3]]
        out.append({
            "name": ch.title,
            "url": ch.url,
            "subscribers": ch.subscribers,
            "video_count": ch.video_count,
            "description": (ch.description or "")[:300],
            "top_videos": top_v,
            "recent_videos": recent_v,
            "found": True,
        })
    return {"channels": out, "targets": targets}

@app.get("/api/analysis/posting-times")
def api_posting_times():
    """ベンチマーク先 TOP 動画の publishedAt を曜日×時刻バケットに集計。
    返却: {heatmap: [[count_by_hour]*7], total_count, weekday_labels, hour_labels}
    publish_datetime が無い行はスキップ。
    """
    targets = _benchmark_target_channels()
    detail_by_name = _load_sheet1_channels()
    heatmap = [[0] * 24 for _ in range(7)]  # 7 weekdays × 24 hours
    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    total = 0
    for name in targets:
        ch = detail_by_name.get(name)
        if not ch:
            from app_sheets import match_channels
            ch = match_channels(name, list(detail_by_name.values()))
        if not ch:
            continue
        for v in (ch.top_videos or []) + (ch.recent_videos or []):
            dt_str = (v.publish_datetime or v.publish_date or "").strip()
            if not dt_str:
                continue
            # 想定フォーマット: "2026/04/13 20:30" or "2026-04-13T20:30" など
            try:
                dt_norm = dt_str.replace("/", "-").replace("T", " ")
                # 時刻が無い場合は 12:00 と仮定
                if " " not in dt_norm:
                    dt_norm += " 12:00"
                dt = datetime.fromisoformat(dt_norm[:16])
                wd = dt.weekday()  # 0=月
                hr = dt.hour
                heatmap[wd][hr] += 1
                total += 1
            except Exception:
                continue
    return {
        "heatmap": heatmap,
        "total_count": total,
        "weekday_labels": weekday_labels,
        "hour_labels": [f"{h:02d}" for h in range(24)],
    }

@app.get("/api/analysis/tag-frequency")
def api_tag_frequency(top_n: int = 20):
    """Sheet1 から tags 取得は無いため、TOP 動画タイトルから単語頻度を集計（簡易）。
    将来的に YouTube Data API で snippet.tags を取れば真のタグ集計に拡張可能。
    """
    import collections
    targets = _benchmark_target_channels()
    detail_by_name = _load_sheet1_channels()
    counter = collections.Counter()
    for name in targets:
        ch = detail_by_name.get(name)
        if not ch:
            from app_sheets import match_channels
            ch = match_channels(name, list(detail_by_name.values()))
        if not ch:
            continue
        for v in (ch.top_videos or []):
            words = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}|[ぁ-んァ-ン一-龥ー]{2,}", v.title or "")
            for w in words:
                w = w.lower() if w[0].isascii() else w
                # 一般的すぎる語を除外
                if w in {"the", "and", "for", "with", "music", "bgm", "vol", "playlist",
                         "vibes", "mix", "songs", "song", "edit"}: continue
                counter[w] += 1
    top = counter.most_common(top_n)
    return {"tags": [{"word": w, "count": c} for w, c in top], "source": "TOP動画タイトル単語集計（簡易）"}


# ─── API: 徹底パクリ進化（Sprint 5-A） ───

@app.post("/api/videos/{video_name}/suggest-imitate-evolve")
def api_suggest_imitate_evolve(video_name: str):
    """ベンチマーク先動画 + 自チャンネルペルソナ から「✓パクる / ✗避ける / +進化させる」3 軸を提案。
    master_prompts.imitate_evolve で上書き可能。
    """
    config = get_dashboard_config()
    persona = config.get("persona", "").strip()
    if not persona:
        raise HTTPException(400, "ペルソナ未設定（基本設定で入力してください）")
    bench = api_benchmark_videos().get("channels", [])
    if not bench:
        raise HTTPException(400, "ベンチマーク対象が空。設定タブでピン留めしてください")
    # ベンチマーク先 TOP 動画タイトル一覧
    bench_summary = []
    for ch in bench[:5]:
        if not ch.get("found"): continue
        bench_summary.append(f"\n=== {ch['name']} (登録 {ch.get('subscribers',0):,}) ===")
        for v in ch.get("top_videos", [])[:3]:
            bench_summary.append(f"  TOP: {v.get('views',0):,}回 | {v['title']} | tags=- | {v.get('published_at','')}")
        for v in ch.get("recent_videos", [])[:2]:
            bench_summary.append(f"  RECENT: {v.get('views',0):,}回 | {v['title']} | {v.get('published_at','')}")
    bench_text = "\n".join(bench_summary) or "(no data)"
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    default_prompt = f"""あなたは YouTube BGM チャンネルの戦略コンサルタントです。
オーナーは、ベンチマークチャンネルから実証済みの勝ちパターン（原則）を抽出し、それを自チャンネル独自のアイデンティティへと進化させたいと考えています。素材・タイトル・名称・ブランド固有の表現をそのまま流用してはいけません。

=== 自チャンネルのペルソナ ===
{persona[:2000]}

=== ベンチマーク動画（人気動画 + 最近の投稿）===
{bench_text}

=== タスク ===
視聴者ニーズの観点から、具体的な提案をすべて日本語で記述してください。

"imitate"（パクる要素）: 視聴者を明確に惹きつけている、実証済みの構成・タイトルパターン・サムネイルの原則・投稿時刻・感情的な訴求。
"avoid"（避ける要素）: 使い古されたパターン、自ペルソナと噛み合わないもの、視聴者の飽きを招くリスク、直接的な模倣に近すぎるもの。
"evolve"（進化させる要素）: 自ペルソナの強みを活かして、より深い未充足ニーズに応え、「まさにこれだ」と思わせる体験を生み出す方法。

次の単一の JSON オブジェクトのみで回答してください:
{{
  "imitate": ["具体的な施策1", "具体的な施策2", ...],
  "avoid": ["具体的なリスク1", "具体的なリスク2", ...],
  "evolve": ["具体的な進化案1", "具体的な進化案2", ...],
  "summary": "<戦略を1〜2文で要約した日本語>"
}}

JSON 以外は一切出力しないこと。すべての値は日本語で記述すること。"""

    prompt = (get_master_prompts().get("imitate_evolve") or "").strip() or default_prompt
    try:
        from app_llm_runner import run_llm, LLMError
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="imitate-evolve")
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:300]}")
    m = re.search(r"\{[\s\S]*\}", out or "")
    if not m:
        raise HTTPException(500, f"JSON 抽出失敗: {(out or '')[:300]}")
    try:
        obj = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(500, f"JSON パース失敗: {e}")
    return obj


# NOTE: 旧 POST /api/videos/{name}/suggest-with-analysis は D13 で廃止。
# 競合分析を反映したメタ提案は suggest-all（propose_with_analysis を内部利用）へ集約。


def _load_analysis_cache_or_409():
    """競合分析キャッシュを読み込み、未実行なら 400、music/visual_direction 不足なら 409"""
    try:
        import app_competitor as _ac
        cache = _ac.load_cache()
    except Exception:
        raise HTTPException(500, "キャッシュ読み込み失敗")
    if not cache:
        raise HTTPException(400, "先に競合分析を実行してください（📡 競合分析ボタン）")
    return cache


def _video_context(video_name: str):
    """動画フォルダから current_title / songs / persona を取り出す共通処理"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    songs = []
    music_dir = folder / "music"
    if music_dir.is_dir():
        songs = [p.stem for p in sorted(music_dir.glob("*.mp3")) if not _is_deleted_track_name(p.name)]
    title_file = folder / "youtube_title.txt"
    current_title = title_file.read_text(encoding="utf-8").strip() if title_file.exists() else ""
    persona = config.get("persona", "")
    return folder, current_title, songs, persona


@app.post("/api/videos/{video_name}/suggest-suno-prompt")
def api_suggest_suno_prompt(video_name: str):
    """競合分析の music_direction から SUNO プロンプト案を生成"""
    cache = _load_analysis_cache_or_409()
    analysis = cache.get("analysis", {})
    if not analysis.get("music_direction"):
        raise HTTPException(409, detail={"error": "analysis_outdated", "hint": "competitor analysis を再実行してください（music_direction が未生成）"})

    _, current_title, _, _ = _video_context(video_name)
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    existing_prompt = suno_cfg.get("prompt", "") or ""

    from app_benchmark_analyze import propose_suno_prompt
    try:
        result = propose_suno_prompt(
            analysis, current_title=current_title,
            existing_prompt=existing_prompt, cli_cmd=cli_cmd,
        )
        return {"status": "ok", **result}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/api/videos/{video_name}/suggest-all")
def api_suggest_all(video_name: str):
    """楽曲・メタ の 2 提案を一気通貫で生成（同期・60-90秒想定。Flow サムネは D8 撤去）"""
    cache = _load_analysis_cache_or_409()
    analysis = cache.get("analysis", {})
    competitor_data = cache.get("competitor_data", {})
    if not analysis.get("music_direction") or not analysis.get("visual_direction"):
        raise HTTPException(409, detail={"error": "analysis_outdated", "hint": "competitor analysis を再実行してください（music/visual_direction が未生成）"})

    _, current_title, songs, persona = _video_context(video_name)
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    existing_prompt = suno_cfg.get("prompt", "") or ""

    from app_benchmark_analyze import propose_with_analysis, propose_suno_prompt
    errors = {}
    result = {"meta": None, "suno": None}

    try:
        result["suno"] = propose_suno_prompt(
            analysis, current_title=current_title,
            existing_prompt=existing_prompt, cli_cmd=cli_cmd,
        )
    except RuntimeError as e:
        errors["suno"] = str(e)

    try:
        result["meta"] = propose_with_analysis(
            analysis, competitor_data,
            cli_cmd=cli_cmd, current_title=current_title,
            songs=songs, persona=persona,
            growth_summary=cache.get("growth_summary", {}),
        )
    except RuntimeError as e:
        errors["meta"] = str(e)

    if errors and not any(result.values()):
        raise HTTPException(500, detail={"errors": errors})
    return {"status": "ok" if not errors else "partial", "errors": errors, **result}


# ─── API: ベンチマーク管理（ピン留め + フィルタ） ───

class BenchmarkConfigUpdate(BaseModel):
    pinned_names: Optional[List[str]] = None
    top_n: Optional[int] = None
    min_subs: Optional[int] = None
    max_subs: Optional[int] = None
    exclude_names: Optional[List[str]] = None


@app.get("/api/analysis/benchmark-config")
def api_benchmark_config_get():
    """現在のベンチマークピン留め設定とフィルタを返す"""
    cfg = get_benchmark_config()
    flt = cfg.get("filter", {}) or {}
    return {
        "pinned_names": cfg.get("pinned_names", []) or [],
        "filter": {
            "top_n": int(flt.get("top_n", 15)),
            "min_subs": int(flt.get("min_subs", 0)),
            "max_subs": flt.get("max_subs"),
            "exclude_names": flt.get("exclude_names", []) or [],
        },
    }


@app.put("/api/analysis/benchmark-config")
def api_benchmark_config_put(req: BenchmarkConfigUpdate):
    """ベンチマークピン留め + フィルタを保存"""
    cfg = get_benchmark_config()
    if req.pinned_names is not None:
        cfg["pinned_names"] = [n.strip() for n in req.pinned_names if n and n.strip()][:20]
    flt = cfg.get("filter", {}) or {}
    if req.top_n is not None:
        flt["top_n"] = max(1, min(50, int(req.top_n)))
    if req.min_subs is not None:
        flt["min_subs"] = max(0, int(req.min_subs))
    if req.max_subs is not None:
        flt["max_subs"] = int(req.max_subs) if req.max_subs > 0 else None
    if req.exclude_names is not None:
        flt["exclude_names"] = [n.strip() for n in req.exclude_names if n and n.strip()]
    cfg["filter"] = flt
    save_benchmark_config(cfg)
    return {"status": "ok", "pinned_names": cfg.get("pinned_names", []), "filter": flt}


@app.get("/api/analysis/cache-info")
def api_analysis_cache_info():
    """キャッシュの鮮度情報（最終分析日時・経過日数・ソース）を返す"""
    try:
        import app_competitor as _ac
        data = _ac.load_cache()
        if not data:
            return {"cached": False, "age_days": None, "analyzed_at": None, "source": None}
        analyzed_at = data.get("analyzed_at")
        age_days = None
        if analyzed_at:
            import datetime as _dt
            try:
                t = _dt.datetime.fromisoformat(analyzed_at)
                age_days = (_dt.datetime.now() - t).days
            except Exception:
                pass
        analysis = data.get("analysis", {})
        return {
            "cached": True,
            "analyzed_at": analyzed_at,
            "age_days": age_days,
            "source": data.get("source"),
            "channel_count": len((data.get("competitor_data") or {}).get("channels", [])),
            "has_music_direction": bool(analysis.get("music_direction")),
            "has_visual_direction": bool(analysis.get("visual_direction")),
            "stale": (age_days is not None and age_days >= 7),
            "schema_outdated": not (analysis.get("music_direction") and analysis.get("visual_direction")),
        }
    except Exception:
        return {"cached": False, "age_days": None}


# ─── API: サムネ要素分析（Vision） ───

class ThumbnailAnalysisRequest(BaseModel):
    urls: Optional[List[str]] = None       # 画像URL 一覧
    uploaded_paths: Optional[List[str]] = None  # サーバ側に保存済みの画像パス（POST /api/analysis/thumbnails-upload 経由）
    context_hint: Optional[str] = None


@app.post("/api/analysis/thumbnails-upload")
async def api_thumbnails_upload(files: List[UploadFile] = File(...)):
    """画像をサーバ一時領域に保存し、分析関数に渡せるパス一覧を返す"""
    import tempfile
    dst_dir = Path(tempfile.mkdtemp(prefix="orzz_thumb_upload_"))
    saved = []
    for f in files:
        ext = Path(f.filename or "img").suffix.lower() or ".jpg"
        if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
            ext = ".jpg"
        dst = dst_dir / f"{len(saved):02d}{ext}"
        content = await f.read()
        dst.write_bytes(content)
        saved.append(str(dst))
    return {"paths": saved}


@app.post("/api/analysis/thumbnails")
def api_analyze_thumbnails(req: ThumbnailAnalysisRequest):
    """競合サムネ画像を Claude Vision で要素抽出。**コピー生成ではなく要素抽出→自チャンネルへの落とし込み**が目的。"""
    from app_benchmark_analyze import analyze_thumbnail_elements
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    try:
        result = analyze_thumbnail_elements(
            image_paths=req.uploaded_paths or [],
            url_list=req.urls or [],
            context_hint=req.context_hint or "",
            cli_cmd=cli_cmd,
        )
        # キャッシュに統合（上書き。手動トリガのため頻度は低い想定）
        try:
            import app_competitor as _ac
            cache = _ac.load_cache() or {}
            if cache:
                if "analysis" not in cache:
                    cache["analysis"] = {}
                cache["analysis"]["thumbnail_elements"] = result
                import datetime as _dt
                cache["thumbnail_analyzed_at"] = _dt.datetime.now().isoformat()
                from app_channel_cache import save_scoped_cache
                save_scoped_cache("competitor_analysis_cache.json", SHARED_CONFIG_DIR / "competitor_analysis_cache.json", cache)
        except Exception:
            pass
        return {"status": "ok", **result}
    except RuntimeError as e:
        msg = str(e)
        if "解析対象の画像がありません" in msg:
            raise HTTPException(400, msg)
        raise HTTPException(500, msg)


# ─── API: Sheets URL 取り込み ───

class SheetImportRequest(BaseModel):
    sheet_url: str


@app.post("/api/analysis/sheets-import")
def api_sheets_import(req: SheetImportRequest):
    """Google Sheets URL から全シート/全タブを読み取り、ベンチマーク候補として返す。"""
    from app_competitor import import_benchmark_from_sheet
    try:
        result = import_benchmark_from_sheet(req.sheet_url)
        return {"status": "ok", **result}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


# ─── API: ベンチマーク完成パイプライン（全自動） ───

BENCHMARK_PROFILES_FILE = SHARED_CONFIG_DIR / "benchmark_profiles.json"  # PC間共有

class BenchmarkRunRequest(BaseModel):
    sheet_a_url: Optional[str] = None   # 未指定時は dashboard_config から
    sheet_b_url: Optional[str] = None
    extra_urls: Optional[List[str]] = None
    channel_filter: Optional[List[str]] = None   # 指定時は選択チャンネルだけ分析
    skip_existing: Optional[bool] = True   # 既存の有効プロファイルは再生成しない
    force: Optional[bool] = False          # True で既存無視の強制再生成
    max_age_days: Optional[int] = None     # スキップの鮮度しきい値（None=常にスキップ）
    dry_run: Optional[bool] = False        # 生成せず内訳見積りのみ返す


class BenchmarkSourcesRequest(BaseModel):
    sheet_a_url: Optional[str] = None
    sheet_b_url: Optional[str] = None
    extra_urls: Optional[List[str]] = None


@app.post("/api/benchmark/preview-sources")
def api_benchmark_preview_sources(req: BenchmarkSourcesRequest):
    """Sheet A/B + extra_urls のチャンネル一覧だけを取得（Claude 呼ばず軽量）。
    ユーザーが分析対象を選択する前に表示するプレビュー用。"""
    cfg = get_dashboard_config()
    sheet_a = (req.sheet_a_url or cfg.get("spreadsheet_channel_detail_url") or "").strip()
    sheet_b = (req.sheet_b_url or cfg.get("spreadsheet_growth_tracking_url") or "").strip()
    extras = [u.strip() for u in (req.extra_urls or []) if u.strip()]
    if not sheet_a and not extras:
        raise HTTPException(400, "Sheet A URL またはリスト外 URL が最低 1 つ必要です")
    try:
        from app_competitor import list_benchmark_sources
        result = list_benchmark_sources(sheet_a_url=sheet_a, sheet_b_url=sheet_b, extra_urls=extras)
    except Exception as e:
        raise HTTPException(500, f"プレビュー失敗: {e}")
    return {
        "status": "ok",
        "sheet_a": sheet_a,
        "sheet_b": sheet_b,
        "extras": extras,
        "channels": result.get("channels") or [],
    }


@app.post("/api/benchmark/run-full")
async def api_benchmark_run_full(req: BenchmarkRunRequest):
    """ベンチマーク完成パイプラインを起動（バックグラウンド）。
    Sheet A/B + リスト外 URL → コメント取得 → Claude プロファイル生成 → 保存。
    channel_filter 指定時は選択チャンネルだけ分析。
    進捗は /api/benchmark/status で取得可能。
    """
    cfg = get_dashboard_config()
    sheet_a = (req.sheet_a_url or cfg.get("spreadsheet_channel_detail_url") or "").strip()
    sheet_b = (req.sheet_b_url or cfg.get("spreadsheet_growth_tracking_url") or "").strip()
    extras = [u.strip() for u in (req.extra_urls or []) if u.strip()]
    channel_filter = [n.strip() for n in (req.channel_filter or []) if n and n.strip()]
    if not sheet_a and not extras:
        raise HTTPException(400, "Sheet A URL またはリスト外 URL が最低 1 つ必要です")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    # dry_run（S3 コスト見積り）: 実行中でも即時に内訳だけ返す（生成しない＝排他不要）
    if req.dry_run:
        try:
            from app_competitor import run_full_benchmark
            return run_full_benchmark(
                sheet_a_url=sheet_a, sheet_b_url=sheet_b,
                extra_urls=extras, cli_cmd=cli_cmd,
                channel_filter=channel_filter or None,
                skip_existing=True if req.skip_existing is None else bool(req.skip_existing),
                force=bool(req.force), max_age_days=req.max_age_days, dry_run=True,
            )
        except Exception as e:
            raise HTTPException(500, f"見積り失敗: {e}")

    await _ensure_not_running("benchmark", "プロファイル生成が既に実行中です")

    task_logs["benchmark"] = []
    import datetime as _dt
    task_meta["benchmark"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "sheet_a": sheet_a,
        "sheet_b": sheet_b,
        "extra_urls": extras,
        "channel_filter": channel_filter,
        "status": "running",
    }

    def _log(msg: str):
        task_logs["benchmark"].append(msg)
        if len(task_logs["benchmark"]) > 500:
            task_logs["benchmark"] = task_logs["benchmark"][-500:]

    def _worker():
        try:
            from app_competitor import run_full_benchmark
            run_full_benchmark(
                sheet_a_url=sheet_a, sheet_b_url=sheet_b,
                extra_urls=extras, cli_cmd=cli_cmd, progress_cb=_log,
                channel_filter=channel_filter or None,
                skip_existing=True if req.skip_existing is None else bool(req.skip_existing),
                force=bool(req.force), max_age_days=req.max_age_days,
            )
            task_meta["benchmark"]["status"] = "done"
            task_meta["benchmark"]["completed_at"] = _dt.datetime.now().isoformat()
        except Exception as e:
            _log(f"❌ エラー: {e}")
            task_meta["benchmark"]["status"] = "failed"
            task_meta["benchmark"]["error"] = str(e)
        finally:
            active_tasks.pop("benchmark", None)

    import threading
    t = threading.Thread(target=_worker, daemon=True)
    active_tasks["benchmark"] = t
    t.start()
    return {"status": "started", "sheet_a": sheet_a, "sheet_b": sheet_b, "extras": extras}


@app.get("/api/benchmark/status")
def api_benchmark_status():
    t = active_tasks.get("benchmark")
    running = t is not None and (hasattr(t, "is_alive") and t.is_alive())
    return {
        "running": running,
        "logs": task_logs.get("benchmark", [])[-200:],
        "meta": task_meta.get("benchmark", {}),
    }


@app.get("/api/benchmark/context-status")
def api_benchmark_context_status():
    """ベンチマーク周辺の前提データを1箇所で確認するための軽量サマリ。

    - competitor_analysis_cache.json: 制作提案・タイトル/コンセプト/サムネ分析の共通入力
    - benchmark_profiles.json: チャンネル深掘り・融合プロンプト生成の入力
    """
    analysis = api_analysis_cache_info()
    profiles = {"generated_at": None, "count": 0}
    if BENCHMARK_PROFILES_FILE.exists():
        try:
            data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
            profiles = {
                "generated_at": data.get("generated_at"),
                "count": len(data.get("profiles") or []),
            }
        except Exception:
            profiles = {"generated_at": None, "count": 0, "error": "read_failed"}
    analysis_proc = active_tasks.get("analysis")
    benchmark_task = active_tasks.get("benchmark")
    benchmark_running = benchmark_task is not None and (
        (hasattr(benchmark_task, "is_alive") and benchmark_task.is_alive())
        or (hasattr(benchmark_task, "returncode") and benchmark_task.returncode is None)
    )
    return {
        "analysis": analysis,
        "profiles": profiles,
        "running": {
            "analysis": analysis_proc is not None and getattr(analysis_proc, "returncode", None) is None,
            "benchmark": benchmark_running,
        },
    }


# ─── API: 全体ステータス（共通ステータスバー向け） ───

_TASK_LABELS = {
    "suno": {"label": "SUNO 生成", "icon": "🎵"},
    "process": {"label": "楽曲後処理", "icon": "🎛"},
    "pipeline": {"label": "パイプライン", "icon": "🚀"},
    "analysis": {"label": "競合分析", "icon": "📊"},
    "benchmark": {"label": "ベンチマーク", "icon": "🎯"},
    "youtube": {"label": "YouTube アップロード", "icon": "📤"},
    "flow": {"label": "Flow 画像生成", "icon": "🖼"},
    "premiere": {"label": "Premiere 配置", "icon": "🎬"},
}


def _task_is_running(task_key: str) -> bool:
    obj = active_tasks.get(task_key)
    if obj is None:
        return False
    # subprocess.Popen
    if hasattr(obj, "returncode"):
        return obj.returncode is None
    # threading.Thread
    if hasattr(obj, "is_alive"):
        return bool(obj.is_alive())
    return False


def _task_progress_summary(task_key: str) -> dict:
    """タスクごとの進捗サマリ。SUNO は progress をパースして %表示に。"""
    logs = task_logs.get(task_key, [])
    meta = task_meta.get(task_key, {}) or {}
    summary = {
        "last_log": logs[-1] if logs else "",
        "started_at": meta.get("started_at"),
        "meta": meta,
    }
    if task_key == "suno":
        pg = _parse_suno_progress(logs)
        if pg and pg.get("total"):
            cur = pg.get("current", 0) or 0
            tot = pg.get("total", 0) or 0
            summary["progress"] = {
                "current": cur,
                "total": tot,
                "percent": min(100, int((cur / tot) * 100)) if tot else 0,
                "phase": pg.get("phase", ""),
            }
    return summary


@app.get("/api/status/all")
def api_status_all():
    """全タスクの状態を集約して返す（共通ステータスバー用）"""
    tasks = []
    for key, info in _TASK_LABELS.items():
        running = _task_is_running(key)
        if not running:
            # 直近完了も出せるよう meta の completed_at を確認
            meta = task_meta.get(key, {}) or {}
            if not meta.get("completed_at"):
                continue
        tasks.append({
            "key": key,
            "label": info["label"],
            "icon": info["icon"],
            "running": running,
            **_task_progress_summary(key),
        })
    # running を先頭に、完了タスクを後ろに
    tasks.sort(key=lambda x: (not x["running"], x["key"]))
    return {"tasks": tasks, "any_running": any(t["running"] for t in tasks)}


@app.get("/api/benchmark/profiles")
def api_benchmark_profiles(resolve_icons: bool = False):
    """保存済のベンチマーク・プロファイルを返す。
    Sheet A の ICON_IMAGE が空でアイコンが無いプロファイルは、
    既存キャッシュからフォールバック表示する。
    resolve_icons=true の時だけネットワーク取得を許可する。"""
    if not BENCHMARK_PROFILES_FILE.exists():
        return {"generated_at": None, "profiles": []}
    try:
        data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"読み込み失敗: {e}")
    icon_cache = _load_icon_cache()
    for p in (data.get("profiles") or []):
        if not (p.get("thumbnail") or "").strip():
            url = (p.get("url") or "").strip()
            if url:
                cached = icon_cache.get(url)
                if isinstance(cached, dict) and cached.get("icon_url"):
                    p["thumbnail"] = cached["icon_url"]
                elif resolve_icons:
                    try:
                        p["thumbnail"] = resolve_channel_icon(url)
                    except Exception:
                        pass
    return data


class DeleteProfilesRequest(BaseModel):
    channel_names: List[str]


@app.delete("/api/benchmark/profiles")
def api_benchmark_delete_profiles(req: DeleteProfilesRequest):
    """指定したチャンネル名のプロファイルを benchmark_profiles.json から削除する。
    リスト外チャンネル URL（dashboard_config.benchmark_extra_urls）にも該当があれば併せて除去。"""
    if not BENCHMARK_PROFILES_FILE.exists():
        raise HTTPException(404, "benchmark_profiles.json が存在しません")
    targets = {n.strip() for n in (req.channel_names or []) if n and n.strip()}
    if not targets:
        raise HTTPException(400, "channel_names が空です")
    try:
        data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"読み込み失敗: {e}")
    before = data.get("profiles") or []
    removed_urls: List[str] = []
    kept = []
    for p in before:
        if p.get("channel_name") in targets:
            url = (p.get("url") or "").strip()
            if url:
                removed_urls.append(url)
        else:
            kept.append(p)
    if len(kept) == len(before):
        return {"status": "noop", "removed": 0, "remaining": len(kept)}
    data["profiles"] = kept
    BENCHMARK_PROFILES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # benchmark_extra_urls にあるなら除去
    if removed_urls:
        try:
            cfg = get_dashboard_config()
            extras = cfg.get("benchmark_extra_urls") or []
            new_extras = [u for u in extras if u not in removed_urls]
            if len(new_extras) != len(extras):
                cfg["benchmark_extra_urls"] = new_extras
                save_dashboard_config_smart(cfg)
        except Exception:
            pass
    return {
        "status": "ok",
        "removed": len(before) - len(kept),
        "remaining": len(kept),
        "removed_urls": removed_urls,
    }


class AddChannelRequest(BaseModel):
    url: str


@app.post("/api/benchmark/add-channel")
def api_benchmark_add_channel(req: AddChannelRequest):
    """リスト外チャンネル URL を追加保存（次回 run-full で取り込まれる）"""
    cfg = get_dashboard_config()
    extras = cfg.get("benchmark_extra_urls") or []
    if req.url not in extras:
        extras.append(req.url)
        cfg["benchmark_extra_urls"] = extras
        save_dashboard_config_smart(cfg)
    return {"status": "ok", "extras": extras}


@app.delete("/api/benchmark/extra-channel")
def api_benchmark_remove_channel(url: str):
    cfg = get_dashboard_config()
    extras = [u for u in (cfg.get("benchmark_extra_urls") or []) if u != url]
    cfg["benchmark_extra_urls"] = extras
    save_dashboard_config_smart(cfg)
    return {"status": "ok", "extras": extras}


class BenchmarkFuseRequest(BaseModel):
    channel_names: list[str]


@app.post("/api/benchmark/suggest-persona")
async def api_benchmark_suggest_persona(req: BenchmarkFuseRequest):
    """指定したベンチマーク・チャンネル（複数）の良いところを抽出し、
    自チャンネル向けのペルソナ案（複数バリエーション）を Claude で生成する。
    BenchmarkFuseRequest を流用（channel_names のみ受け取り）。"""
    names = [n.strip() for n in (req.channel_names or []) if n and n.strip()]
    if not names:
        raise HTTPException(400, "1 件以上のチャンネルを選択してください")
    if not BENCHMARK_PROFILES_FILE.exists():
        raise HTTPException(409, "benchmark_profiles が未生成です。先にプロファイル生成を実行してください")
    data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
    all_profiles = data.get("profiles") or []
    selected = [p for p in all_profiles if p.get("channel_name") in names]
    if not selected:
        raise HTTPException(404, f"プロファイルが見つかりません: {', '.join(names)}")

    # 既存ペルソナを参考材料として渡す
    cfg = get_dashboard_config()
    current_persona = (cfg.get("persona") or "").strip()

    # Claude に渡すコンパクトな材料を構築
    materials = []
    for p in selected:
        prof = p.get("profile") or {}
        mp = prof.get("music_profile") or {}
        vp = prof.get("visual_profile") or {}
        pe = prof.get("persona") or {}
        ap = prof.get("appeal_points") or []
        materials.append({
            "channel_name": p.get("channel_name"),
            "music": {
                "genres": mp.get("genres"),
                "mood": mp.get("mood"),
                "imagery": mp.get("imagery"),
            },
            "visual": {
                "palette": vp.get("palette"),
                "atmosphere": vp.get("atmosphere"),
                "time_of_day": vp.get("time_of_day"),
            },
            "audience": {
                "age_range": pe.get("age_range"),
                "demographics": pe.get("demographics"),
                "viewing_scenes": pe.get("viewing_scenes"),
                "psychological_needs": pe.get("psychological_needs"),
            },
            "appeal_points": ap[:5] if isinstance(ap, list) else [],
        })

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    prompt = f"""You are a brand strategist for a YouTube BGM/instrumental music channel.
Your task: synthesize **3 distinct persona drafts** for our own channel by extracting the strongest elements from the benchmark channels below.

Current persona (reference, may be empty):
{current_persona or '(empty)'}

Benchmark channel materials (extract the GOOD points, do not copy verbatim):
{json.dumps(materials, ensure_ascii=False, indent=2)}

Rules:
- Output 3 persona drafts in JAPANESE.
- Each draft must be 2-4 sentences, 80-200 characters, written in a way that can be pasted directly into a YouTube channel "About" or used as a creative brief.
- Each draft should pick a DIFFERENT angle (e.g. mood/scene focus, audience focus, visual world focus).
- Translate insights into our own aesthetic — do NOT mention specific competitor channel names or trademarked elements.
- For each draft, include a short rationale (1 line, JA) explaining what was borrowed from the benchmarks.

Return ONLY a JSON object in this exact shape (no markdown fences, no prose):
{{
  "drafts": [
    {{ "label": "短い見出し（10-20字）", "persona": "ペルソナ本文（80-200字）", "rationale": "どの観点を借りたかの一文" }},
    ...
  ]
}}
"""
    try:
        from app_llm_runner import run_llm, LLMError
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=240, label="persona-suggest").strip()
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:300]}")
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        raise HTTPException(502, f"Claude 応答から JSON を抽出できませんでした: {out[:300]}")
    try:
        parsed = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(502, f"JSON 解析失敗: {e}; 応答: {out[:300]}")
    drafts = parsed.get("drafts") or []
    if not isinstance(drafts, list) or not drafts:
        raise HTTPException(502, "drafts フィールドが不正です")
    return {
        "status": "ok",
        "channel_names": [p.get("channel_name") for p in selected],
        "drafts": drafts,
    }


@app.post("/api/benchmark/fuse")
async def api_benchmark_fuse(req: BenchmarkFuseRequest):
    """指定したチャンネル（1 件以上）のプロファイルから、orzz. 向けの統合 direction と
    SUNO / Flow プロンプトを返す。1 件なら単一チャンネルの抽出→翻案、複数なら融合。
    バックグラウンドではなく同期実行（最大 7 分）。"""
    names = [n.strip() for n in (req.channel_names or []) if n and n.strip()]
    if len(names) < 1:
        raise HTTPException(400, "チャンネルを 1 件以上選択してください")
    if not BENCHMARK_PROFILES_FILE.exists():
        raise HTTPException(409, "benchmark_profiles が未生成です。先にプロファイル生成を実行してください")

    data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
    all_profiles = data.get("profiles") or []
    selected = [p for p in all_profiles if p.get("channel_name") in names]
    missing = [n for n in names if not any(p.get("channel_name") == n for p in all_profiles)]
    if missing:
        raise HTTPException(404, f"未登録のチャンネル: {', '.join(missing)}")
    if len(selected) < 1:
        raise HTTPException(400, "有効なプロファイルがありません")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    from app_competitor import fuse_benchmark_profiles
    try:
        result = fuse_benchmark_profiles(selected, cli_cmd=cli_cmd)
    except Exception as e:
        raise HTTPException(500, f"融合失敗: {e}")

    return {
        "status": "ok",
        "channel_names": [p.get("channel_name") for p in selected],
        "fusion": result,
    }


@app.get("/api/benchmark/extra-channels")
def api_benchmark_list_extras():
    cfg = get_dashboard_config()
    return {"extras": cfg.get("benchmark_extra_urls") or []}


# ─── API: A/B ログ（vol ごとの制作条件 + 7 日後 delta） ───

AB_LOG_FILE = CONFIG_DIR / "ab_log.json"


def _load_ab_log() -> list[dict]:
    try:
        return json.loads(AB_LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_ab_log(entries: list[dict]):
    AB_LOG_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


class ABLogCreate(BaseModel):
    video_name: str
    suno_prompt: str = ""
    flow_prompt: str = ""
    target_bpm: str = ""
    color_theme: str = ""
    benchmark_names: list[str] = []
    note: str = ""


class ABLogMeasure(BaseModel):
    views_delta_7d: int = 0
    subs_delta_7d: int = 0
    note: str = ""


@app.get("/api/ab-log")
def api_ab_log_list():
    """A/B ログ一覧（新しい順）"""
    entries = _load_ab_log()
    import datetime as _dt
    now = _dt.datetime.now()
    for e in entries:
        pub = e.get("published_at")
        if pub:
            try:
                t = _dt.datetime.fromisoformat(pub)
                e["days_since_publish"] = (now - t).days
                e["ready_to_measure"] = e["days_since_publish"] >= 7 and e.get("views_delta_7d") is None
            except Exception:
                e["days_since_publish"] = None
                e["ready_to_measure"] = False
    entries.sort(key=lambda e: e.get("published_at") or "", reverse=True)
    return {"entries": entries}


@app.post("/api/ab-log")
def api_ab_log_create(req: ABLogCreate):
    """新規エントリ追加（投稿時に制作条件を記録）"""
    entries = _load_ab_log()
    # 同一 video_name の重複は更新
    entries = [e for e in entries if e.get("video_name") != req.video_name]
    import datetime as _dt
    entries.append({
        "video_name": req.video_name,
        "published_at": _dt.datetime.now().isoformat(),
        "suno_prompt": req.suno_prompt,
        "flow_prompt": req.flow_prompt,
        "target_bpm": req.target_bpm,
        "color_theme": req.color_theme,
        "benchmark_names": req.benchmark_names,
        "note": req.note,
        "views_delta_7d": None,
        "subs_delta_7d": None,
        "measured_at": None,
    })
    _save_ab_log(entries)
    return {"status": "ok"}


@app.post("/api/ab-log/{video_name}/measure")
def api_ab_log_measure(video_name: str, req: ABLogMeasure):
    """7 日後の delta を書き込む（Channel Tracker から手動転記）"""
    entries = _load_ab_log()
    found = False
    import datetime as _dt
    for e in entries:
        if e.get("video_name") == video_name:
            e["views_delta_7d"] = int(req.views_delta_7d)
            e["subs_delta_7d"] = int(req.subs_delta_7d)
            e["measured_at"] = _dt.datetime.now().isoformat()
            if req.note:
                e["measure_note"] = req.note
            found = True
            break
    if not found:
        raise HTTPException(404, "該当エントリが A/B ログにありません")
    _save_ab_log(entries)
    return {"status": "ok"}


@app.delete("/api/ab-log/{video_name}")
def api_ab_log_delete(video_name: str):
    entries = _load_ab_log()
    before = len(entries)
    entries = [e for e in entries if e.get("video_name") != video_name]
    if len(entries) == before:
        raise HTTPException(404)
    _save_ab_log(entries)
    return {"status": "ok"}


# ─── API: チャンネルアイコン ───

@app.get("/api/channel-icon")
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

@app.get("/api/prompts")
def api_list_prompts():
    return {"prompts": get_prompts()}

class PromptSave(BaseModel):
    id: Optional[str] = None
    name: str
    text: str

@app.post("/api/prompts")
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

@app.delete("/api/prompts/{prompt_id}")
def api_delete_prompt(prompt_id: str):
    save_json(PROMPTS_CONFIG, [p for p in get_prompts() if p["id"] != prompt_id])
    return {"status": "ok"}

# ─── API: スケジュール ───

@app.get("/api/schedule")
def api_get_schedule():
    schedule = load_json(SCHEDULE_CONFIG, []) if SCHEDULE_CONFIG.exists() else []
    videos = api_list_videos().get("videos", [])
    channel_dir = Path(get_dashboard_config().get("channel_folder", ""))
    for v in videos:
        if not v.get("publish_date"):
            continue
        if not any(s.get("video_num") == v["num"] for s in schedule):
            upload_json = channel_dir / v["name"] / "youtube_upload.json"
            upload_info = load_json(upload_json, {}) if upload_json.exists() else {}
            youtube_scheduled = bool(upload_info.get("schedule"))
            schedule.append({
                "video_num": v["num"], "video_name": v["name"], "date": v["publish_date"],
                "status": "exported" if v["has_mp4"] else ("ready" if v["has_music"] else "pending"),
                "uploaded": bool(upload_info.get("video_id")),
                "youtube_scheduled": youtube_scheduled,
            })
    # 既存エントリにも youtube_scheduled を補完
    for s in schedule:
        if "youtube_scheduled" not in s:
            vname = s.get("video_name", "")
            upload_json = channel_dir / vname / "youtube_upload.json" if vname else None
            upload_info = load_json(upload_json, {}) if (upload_json and upload_json.exists()) else {}
            s["youtube_scheduled"] = bool(upload_info.get("schedule"))
    return {"schedule": schedule}

# ─── youtube ドメインは routers/youtube.py へ分離（D9）───
from routers.youtube import router as _youtube_router
app.include_router(_youtube_router)


# ─── images ドメインは routers/images.py へ分離（D9）───
from routers.images import router as _images_router
app.include_router(_images_router)


# ─── premiere_photoshop ドメインは routers/premiere_photoshop.py へ分離（D9）───
from routers.premiere_photoshop import router as _premiere_photoshop_router
app.include_router(_premiere_photoshop_router)


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


@app.get("/api/credentials/status")
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
    discord_cfg = CONFIG_DIR / "discord_config.json"
    result["discord"] = {"configured": discord_cfg.exists()}
    result["line"] = {"configured": False, "deprecated": True}

    # SUNO (Playwright profile)
    suno_profile = CONFIG_DIR / "chromium_profile"
    result["suno_browser"] = {"profile_exists": suno_profile.exists()}

    # ffmpeg
    result["ffmpeg"] = {"available": bool(shutil.which("ffmpeg"))}

    # Premiere (pymiere)
    try:
        __import__("pymiere")
        result["pymiere"] = {"installed": True}
    except ImportError:
        result["pymiere"] = {"installed": False}

    # Dashboard config
    result["config"] = {
        "persona_set": bool(dc.get("persona")),
        "rival_channels_count": len(dc.get("rival_channels", [])),
        "channel_folder_exists": Path(dc.get("channel_folder", "")).exists() if dc.get("channel_folder") else False,
    }

    return result


@app.post("/api/credentials/reauth-youtube")
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


@app.get("/api/setup/status")
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
    checks["discord_config"] = (CONFIG_DIR / "discord_config.json").exists()
    checks["chromium_profile"] = (CONFIG_DIR / "chromium_profile").exists()
    return {"checks": checks}

@app.post("/api/setup/run")
async def api_setup_run():
    """環境セットアップスクリプトを実行"""
    setup_script = SHARED_BASE / "Python" / "setup.sh"
    if not setup_script.exists():
        raise HTTPException(404, "setup.sh が見つかりません")
    task_logs["setup"] = []
    proc = subprocess.Popen(
        ["bash", str(setup_script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    active_tasks["setup"] = proc
    _stream_subprocess(proc, "setup")
    return {"status": "started"}

@app.get("/api/setup/logs")
def api_setup_logs():
    return {"logs": task_logs.get("setup", [])[-50:]}

# ─── WebSocket ───
@app.websocket("/ws/logs/{task_name}")
async def ws_logs(websocket: WebSocket, task_name: str):
    await websocket.accept()
    last_idx = 0
    try:
        while True:
            logs = task_logs.get(task_name, [])
            if len(logs) > last_idx:
                for line in logs[last_idx:]: await websocket.send_text(line)
                last_idx = len(logs)
            await asyncio.sleep(0.5)
    except Exception:
        pass

# ─── フロントエンド ───
@app.get("/")
def index():
    html = WEB_DIR / "static" / "index.html"
    if html.exists():
        return FileResponse(str(html))
    return HTMLResponse("<h1>orzz. Dashboard</h1><p>static/index.html が見つかりません</p>")

@app.get("/login.html")
def login_page():
    p = WEB_DIR / "static" / "login.html"
    if p.exists():
        return FileResponse(str(p))
    raise HTTPException(404, "login.html not found")

@app.get("/manifest.json")
def manifest():
    """設定駆動の動的 manifest.json を返す（PWA 表示名がブランド設定と連動する）"""
    cfg = get_dashboard_config()
    brand_full = cfg.get("brand_full") or DEFAULT_DASHBOARD_CONFIG["brand_full"]
    brand_short = cfg.get("brand_short") or DEFAULT_DASHBOARD_CONFIG["brand_short"]
    initial = (brand_short[:1] or "A").upper()
    icon_template = (
        "data:image/svg+xml;utf8,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {vb}'>"
        "<rect width='{w}' height='{h}' rx='{r}' fill='%230a0a0a'/>"
        "<text x='{cx}' y='{cy}' font-size='{fs}' text-anchor='middle' "
        "fill='%233b82f6' font-family='-apple-system,sans-serif' font-weight='700'>{i}</text>"
        "</svg>"
    )
    return {
        "name": brand_full,
        "short_name": brand_short[:12],
        "description": "クリエイター向け制作パイプライン自動化ダッシュボード",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0a0a0a",
        "theme_color": "#0a0a0a",
        "icons": [
            {
                "src": icon_template.format(vb="192 192", w=192, h=192, r=32, cx=96, cy=128, fs=96, i=initial),
                "sizes": "192x192", "type": "image/svg+xml",
            },
            {
                "src": icon_template.format(vb="512 512", w=512, h=512, r=80, cx=256, cy=340, fs=256, i=initial),
                "sizes": "512x512", "type": "image/svg+xml",
            },
        ],
    }

@app.get("/sw.js")
def service_worker():
    p = WEB_DIR / "static" / "sw.js"
    if p.exists():
        return FileResponse(str(p), media_type="application/javascript")
    raise HTTPException(404, "sw.js not found")

def _find_free_port(start=8888, end=8899, host="127.0.0.1"):
    import socket
    for p in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return None


# ─── 自動書き出しキュー（Sprint 4-E） ───────────────
import hashlib

EXPORT_QUEUE_FILE = CONFIG_DIR / "export_queue.json"
EXPORT_RULES_FILE = CONFIG_DIR / "export_rules.json"

DEFAULT_EXPORT_RULES = {
    "watcher_enabled": False,
    "scan_interval_sec": 30,
    # mp4 サイズが変化しなくなって完了とみなすまでの猶予秒数。
    # AME はフレーム解析やアトムフィックスで一時的に書き込みを止めるため、
    # 短すぎると未完了で done 判定されて次ジョブと AME 競合する。
    "size_stable_window_sec": 30,
    # mp4 完了検知の二段階タイムアウト（堅牢化）
    # Phase1: encodeSequence 後、mp4 が出現するまでの上限。
    #   AME 投入失敗（startBatch が空振り、AME がジョブ受け取らず等）を早期検知。
    "mp4_appear_timeout_sec": 300,   # 5 分
    # Phase2: mp4 出現後、サイズ安定で done を出すまでの上限（実エンコード時間の上限）
    "mp4_stable_timeout_sec": 1800,  # 30 分
    "auto_upload_after_export": False,   # ME 完了後に YouTube へ自動アップロード
    "auto_upload_privacy": "unlisted",   # 自動アップロード時の公開設定
    "rules": {
        "require_prproj": True,
        "require_music": True,
        "require_srt": True,
        "skip_if_mp4": True,
    },
}

def get_export_rules():
    cfg = DEFAULT_EXPORT_RULES.copy()
    cfg["rules"] = DEFAULT_EXPORT_RULES["rules"].copy()
    loaded = load_json(EXPORT_RULES_FILE, {})
    cfg.update({k: v for k, v in loaded.items() if k != "rules"})
    if loaded.get("rules"):
        cfg["rules"].update(loaded["rules"])
    return cfg

def save_export_rules(cfg):
    save_json(EXPORT_RULES_FILE, cfg)

def get_export_queue():
    data = load_json(EXPORT_QUEUE_FILE, {"items": []})
    if not isinstance(data, dict) or "items" not in data:
        return {"items": []}
    return data

def save_export_queue(data):
    save_json(EXPORT_QUEUE_FILE, data)


def get_export_ignore_list() -> list:
    """AME watcher が無視すべき video_name のリスト（per-channel）。
    既に別で書き出し済 / 手動非表示にしたフォルダがここに入る。"""
    cc = load_channel_config()
    raw = cc.get("export_ignore_list") or []
    if not isinstance(raw, list):
        return []
    # 重複・空文字除去
    return sorted({str(x).strip() for x in raw if isinstance(x, (str, int)) and str(x).strip()})


def add_to_export_ignore_list(video_name: str) -> list:
    if not video_name:
        return get_export_ignore_list()
    cc = load_channel_config()
    cur = list(cc.get("export_ignore_list") or [])
    if video_name not in cur:
        cur.append(video_name)
        cc["export_ignore_list"] = cur
        save_channel_config(cc)
    return get_export_ignore_list()


def remove_from_export_ignore_list(video_name: str) -> list:
    cc = load_channel_config()
    cur = list(cc.get("export_ignore_list") or [])
    if video_name in cur:
        cur = [x for x in cur if x != video_name]
        cc["export_ignore_list"] = cur
        save_channel_config(cc)
    return get_export_ignore_list()

def _fingerprint(prproj_path: str, mtime: float) -> str:
    return hashlib.sha256(f"{prproj_path}|{int(mtime)}".encode()).hexdigest()[:16]

def _scan_video_folder_for_export(folder: Path) -> dict:
    """完成判定: prproj / music / srt あり、mp4 なし。export_path も考慮。"""
    if not folder.is_dir():
        return {"completable": False}
    rules = get_export_rules()["rules"]
    name = folder.name
    prproj_files = list(folder.glob("*vol*.prproj")) + list(folder.glob("*.prproj"))
    has_prproj = bool(prproj_files)
    music_dir = folder / "music"
    has_music = music_dir.exists() and any(_is_usable_audio_file(p) for p in music_dir.iterdir())
    has_srt = bool(list(folder.glob("*.srt")))
    # MP4 は flat 配置（<export_path>/<prefix>_vol<num>.mp4）も含めて全配置を探索
    has_mp4 = _find_exported_mp4(folder, name) is not None
    completable = True
    if rules.get("require_prproj") and not has_prproj: completable = False
    if rules.get("require_music") and not has_music: completable = False
    if rules.get("require_srt") and not has_srt: completable = False
    if rules.get("skip_if_mp4") and has_mp4: completable = False
    return {
        "completable": completable,
        "video_name": name,
        "prproj_path": str(prproj_files[0]) if prproj_files else "",
        "has_prproj": has_prproj, "has_music": has_music, "has_srt": has_srt, "has_mp4": has_mp4,
        "folder": str(folder),
    }

def _scan_all_for_export(include_ignored: bool = False) -> list:
    """書き出し可能フォルダをスキャン。
    include_ignored=False なら ignore list（チャンネル別）に入っている video_name は除外。
    また pending/running 中の video_name もスキャン対象外（保存で mtime が変わって
    fingerprint が変化 → 別アイテム扱いで重複登録、を防ぐ）。
    さらに「現在のチャンネル prefix（例: WW）と一致しない」フォルダも除外。
    複数チャンネル並列運用時に、書き出しキューが他チャンネル分まで拾わないようにする。"""
    config = get_dashboard_config()
    channel_dir = Path(config.get("channel_folder", ""))
    if not channel_dir.exists():
        return []
    ignore_set = set() if include_ignored else set(get_export_ignore_list())
    if not include_ignored:
        try:
            q = get_export_queue()
            ignore_set |= {x.get("video_name", "") for x in q["items"]
                           if x.get("status") in ("pending", "running")}
            ignore_set.discard("")
        except Exception:
            pass
    # チャンネル prefix（例: "WW", "orzz" 等）。フォルダ名 "<num>_<prefix>_<date>" の prefix と
    # 一致するもののみスキャン対象とする。設定が空なら従来通り全件。
    channel_prefix = ""
    try:
        channel_prefix = (get_file_prefix() or "").strip()
    except Exception:
        pass
    # _<prefix>_ パターンで matching するための正規化
    prefix_pattern = re.compile(rf"^\d+_{re.escape(channel_prefix)}_", re.IGNORECASE) if channel_prefix else None

    out = []
    for d in sorted(channel_dir.iterdir()):
        if not d.is_dir() or d.name.startswith('.'):
            continue
        if d.name in ignore_set:
            continue
        # チャンネル prefix と一致しないフォルダはスキップ（他チャンネルの動画フォルダ対策）
        if prefix_pattern is not None and not prefix_pattern.match(d.name):
            continue
        info = _scan_video_folder_for_export(d)
        if info.get("completable"):
            out.append(info)
    return out

def _enqueue_export(info: dict, force: bool = False) -> dict:
    """キューに追加。ignore list に入っている動画は force=True でない限り追加しない。"""
    video_name = info.get("video_name", "")
    if not force and video_name in set(get_export_ignore_list()):
        return {"status": "skip", "reason": "ignored", "video_name": video_name}
    q = get_export_queue()
    prproj = info.get("prproj_path", "")
    if not prproj:
        return {"status": "skip", "reason": "prproj_missing"}
    try:
        mtime = Path(prproj).stat().st_mtime
    except Exception:
        return {"status": "skip", "reason": "prproj_stat_failed"}
    fp = _fingerprint(prproj, mtime)
    # 重複チェック: 同 video_name で pending/running 中ならスキップ。
    # Premiere で開いて保存すると prproj の mtime が変わって fingerprint も変わるため、
    # fingerprint 単独だと「同じ動画の二重登録」を防げない（既存重複バグの根本原因）。
    if not force:
        for item in q["items"]:
            if (item.get("video_name") == video_name
                and item.get("status") in ("pending", "running")):
                return {"status": "duplicate",
                        "fingerprint": item.get("fingerprint"),
                        "reason": "already_in_progress"}
    # 同 fingerprint かつ done → 同じ書き出しを再実行しない
    # （prproj の更新時刻が変われば fingerprint が変わるので新規追加される）
    for item in q["items"]:
        if item.get("fingerprint") == fp and item.get("status") == "done":
            return {"status": "skip", "reason": "already_done", "fingerprint": fp}
    item = {
        "fingerprint": fp,
        "video_name": info["video_name"],
        "prproj_path": prproj,
        "folder": info["folder"],
        "status": "pending",
        "added_at": datetime.utcnow().isoformat() + "Z",
        "started_at": "",
        "completed_at": "",
        "output_path": "",
        "error": "",
    }
    q["items"].append(item)
    save_export_queue(q)
    return {"status": "queued", "fingerprint": fp, "item": item}

def _resolve_output_path(folder: Path, video_name: str) -> Path:
    """書き出し先解決。
    export_path 設定があれば <export_path>/<video_name>/<file>.mp4 形式。
    無ければチャンネルフォルダ直下。
    """
    m = re.match(r"^(\d+)_", video_name)
    num = m.group(1) if m else "00"
    prefix = get_file_prefix()
    fname = f"{prefix}_vol{num}.mp4"
    ext_dir = _resolve_external_export_dir()
    if ext_dir is not None:
        target = ext_dir / video_name
        target.mkdir(parents=True, exist_ok=True)
        return target / fname
    return folder / fname


def _write_manual_exported_mp4(folder: Path, path: Path) -> dict:
    safe = _safe_user_path(str(path))
    if not _is_exported_mp4_candidate(safe):
        raise HTTPException(400, "MP4 ファイルを選択してください")
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "path": str(safe),
        "filename": safe.name,
        "size": safe.stat().st_size,
        "registered_at": datetime.now().isoformat(),
    }
    (folder / MANUAL_EXPORTED_VIDEO_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def _mp4_candidate_payload(path: Path, source: str) -> dict:
    try:
        st = path.stat()
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime).isoformat()
    except Exception:
        size = 0
        mtime = ""
    return {
        "path": str(path),
        "filename": path.name,
        "folder": str(path.parent),
        "source": source,
        "size": size,
        "size_mb": round(size / 1024 / 1024, 1) if size else 0,
        "mtime": mtime,
    }


def _collect_mp4_candidates(folder: Path, video_name: str, extra_folder: Optional[Path] = None) -> List[dict]:
    seen = set()
    out = []

    def add(path: Optional[Path], source: str):
        if path is None:
            return
        try:
            p = path.expanduser().resolve(strict=False)
        except Exception:
            return
        if str(p) in seen or not _is_exported_mp4_candidate(p):
            return
        seen.add(str(p))
        out.append(_mp4_candidate_payload(p, source))

    add(_read_manual_exported_mp4(folder), "manual")
    for f in folder.glob("*vol*.mp4"):
        add(f, "video_folder")
    for f in folder.glob("*.mp4"):
        add(f, "video_folder")

    try:
        ext_dir = _resolve_external_export_dir()
    except Exception:
        ext_dir = None
    if ext_dir is not None and ext_dir.exists():
        per_video = ext_dir / video_name
        if per_video.exists():
            for f in per_video.glob("*.mp4"):
                add(f, "export_path")
        m = re.match(r"^(\d+)_", video_name)
        if m:
            num = m.group(1)
            prefix = get_file_prefix()
            base = f"{prefix}_vol{num}"
            for f in ext_dir.glob(f"{base}*.mp4"):
                tail = f.stem[len(base):]
                if tail == "" or not tail[0].isdigit():
                    add(f, "export_path")

    if extra_folder is not None:
        safe = _safe_user_path(str(extra_folder))
        if not safe.exists() or not safe.is_dir():
            raise HTTPException(404, f"フォルダが見つかりません: {safe}")
        scanned = 0
        for f in safe.rglob("*.mp4"):
            add(f, "selected_folder")
            scanned += 1
            if scanned >= 200:
                break

    out.sort(key=lambda x: (x.get("source") == "manual", x.get("mtime") or ""), reverse=True)
    return out


async def _wait_for_mp4_complete(output_path: Path,
                                  fingerprint: Optional[str] = None) -> bool:
    """mp4 完了検知を二段階で行う。
    Phase 1: 出現待ち（mp4_appear_timeout_sec、既定 5 分）。
      AME が encodeSequence を受け取って書き込みを始めるまでを待つ。
      ここで時間切れなら「AME が投入を取りこぼした」可能性が高く、即 error で次の pending へ。
    Phase 2: 安定待ち（mp4_stable_timeout_sec、既定 30 分）。
      出現後、サイズが size_stable_window_sec（既定 30 秒）変化しなければ done。

    fingerprint があれば _ame_progress に進捗（phase / size_bytes / elapsed_sec）を更新する。
    """
    rules = get_export_rules()
    stable_window = int(rules.get("size_stable_window_sec", 30))
    appear_timeout = int(rules.get("mp4_appear_timeout_sec", 300))
    stable_timeout = int(rules.get("mp4_stable_timeout_sec", 1800))

    # ── Phase 1: mp4 が出現するまで ──
    appear_start = time.time()
    while not output_path.exists():
        elapsed = time.time() - appear_start
        if elapsed > appear_timeout:
            if fingerprint:
                _ame_progress[fingerprint] = {
                    **_ame_progress.get(fingerprint, {}),
                    "size_bytes": 0,
                    "rate_bytes_per_sec": 0,
                    "elapsed_sec": int(elapsed),
                    "stable_for_sec": 0,
                    "phase": "appear_timeout",
                }
            return False
        if fingerprint:
            _ame_progress[fingerprint] = {
                "size_bytes": 0,
                "rate_bytes_per_sec": 0,
                "elapsed_sec": int(elapsed),
                "stable_for_sec": 0,
                "phase": "waiting_appear",
            }
        await asyncio.sleep(5)

    # ── Phase 2: 出現後、サイズ安定まで ──
    stable_start = time.time()
    last_size = -1
    last_change = time.time()
    samples: list[tuple[float, int]] = []
    while time.time() - stable_start < stable_timeout:
        now = time.time()
        try:
            size = output_path.stat().st_size
        except Exception:
            size = -1
        if size != last_size:
            last_size = size
            last_change = now
            samples.append((now, size))
            if len(samples) > 30:
                samples = samples[-30:]
            if fingerprint:
                if len(samples) >= 2:
                    dt = samples[-1][0] - samples[0][0]
                    db = samples[-1][1] - samples[0][1]
                    rate = (db / dt) if dt > 0 else 0
                else:
                    rate = 0
                _ame_progress[fingerprint] = {
                    "size_bytes": size,
                    "rate_bytes_per_sec": int(rate),
                    "elapsed_sec": int(now - stable_start),
                    "stable_for_sec": 0,
                    "phase": "writing",
                }
        elif size > 0 and (now - last_change) >= stable_window:
            if fingerprint:
                _ame_progress[fingerprint] = {
                    "size_bytes": size,
                    "rate_bytes_per_sec": 0,
                    "elapsed_sec": int(now - stable_start),
                    "stable_for_sec": int(now - last_change),
                    "phase": "done",
                }
            return True
        else:
            if fingerprint:
                _ame_progress.setdefault(fingerprint, {}).update({
                    "stable_for_sec": int(now - last_change),
                    "phase": "stabilizing" if size > 0 else "writing",
                })
        await asyncio.sleep(2)
    if fingerprint:
        _ame_progress[fingerprint] = {**_ame_progress.get(fingerprint, {}), "phase": "stable_timeout"}
    return False


# AME 進捗ストア（fingerprint → {size_bytes, rate_bytes_per_sec, elapsed_sec, phase}）
_ame_progress: dict[str, dict] = {}

# 実行中の subprocess を fingerprint で参照可能にする（停止/キャンセル用）
_active_export_proc: dict[str, "asyncio.subprocess.Process"] = {}


def _kill_active_proc(fingerprint: str) -> bool:
    proc = _active_export_proc.get(fingerprint)
    if not proc:
        return False
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception as e:
        print(f"[export] kill failed fp={fingerprint}: {e}")
        return False
    return True

async def _process_export_item(item: dict) -> dict:
    """単一アイテムを処理: Premiere で開いて encodeSequence → mp4 完了待ち。"""
    item["status"] = "running"
    item["started_at"] = datetime.utcnow().isoformat() + "Z"
    q = get_export_queue()
    for x in q["items"]:
        if x["fingerprint"] == item["fingerprint"]: x.update(item)
    save_export_queue(q)

    folder = Path(item["folder"])
    # 外部 SSD 設定があればマウント確認 → 未マウントなら即エラー
    try:
        output_path = _resolve_output_path(folder, item["video_name"])
    except ValueError as e:
        item["status"] = "error"
        item["error"] = f"外部書き出し先が利用不可: {e}"
        item["completed_at"] = datetime.utcnow().isoformat() + "Z"
        q = get_export_queue()
        for x in q["items"]:
            if x["fingerprint"] == item["fingerprint"]: x.update(item)
        save_export_queue(q)
        return item
    item["output_path"] = str(output_path)

    # Premiere スクリプト経由で対象 .prproj を開いてから encodeSequence
    # 外部 export_path 設定時は AME に SSD パスを直接渡し、本体側 mp4 は作らない
    cmd = [sys.executable, "-u", str(PREMIERE_SCRIPT), "--export-only", "--project", item["prproj_path"]]
    is_external = output_path.parent != folder
    if is_external:
        cmd += ["--output-path", str(output_path)]
    # サブプロセスの出力をファイルに残す（ハング時のデバッグ用）
    log_path = CONFIG_DIR / f"export_log_{item['fingerprint']}.txt"
    try:
        log_f = open(log_path, "w", encoding="utf-8")
    except Exception:
        log_f = None
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=(log_f if log_f else asyncio.subprocess.PIPE),
        stderr=asyncio.subprocess.STDOUT,
    )
    _active_export_proc[item["fingerprint"]] = proc
    # タイムアウト: クラウド DL を考慮しても 30 分で十分。それ以上はハング扱い。
    EXPORT_SUBPROC_TIMEOUT = 30 * 60
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=EXPORT_SUBPROC_TIMEOUT)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            try: await proc.wait()
            except Exception: pass
            if log_f:
                try: log_f.close()
                except Exception: pass
            item["status"] = "error"
            item["error"] = f"Premiere 起動 / 書き出しがタイムアウト（{EXPORT_SUBPROC_TIMEOUT//60}分）。ログ: {log_path}"
            item["completed_at"] = datetime.utcnow().isoformat() + "Z"
            q = get_export_queue()
            for x in q["items"]:
                if x["fingerprint"] == item["fingerprint"]: x.update(item)
            save_export_queue(q)
            return item
        if log_f:
            try: log_f.close()
            except Exception: pass
        # 外部から kill された場合は returncode が負（SIGKILL=-9 等）になる
        if proc.returncode is not None and proc.returncode < 0:
            item["status"] = "error"
            item["error"] = f"キャンセル（signal={-proc.returncode}）"
            item["completed_at"] = datetime.utcnow().isoformat() + "Z"
            q = get_export_queue()
            for x in q["items"]:
                if x["fingerprint"] == item["fingerprint"]: x.update(item)
            save_export_queue(q)
            return item
        if proc.returncode != 0:
            # ログファイルから末尾を読んで error に格納
            tail = ""
            try:
                if log_path.exists():
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
            except Exception:
                pass
            item["status"] = "error"
            item["error"] = tail or f"premiere subprocess exit {proc.returncode}"
            item["completed_at"] = datetime.utcnow().isoformat() + "Z"
            q = get_export_queue()
            for x in q["items"]:
                if x["fingerprint"] == item["fingerprint"]: x.update(item)
            save_export_queue(q)
            return item
    finally:
        _active_export_proc.pop(item["fingerprint"], None)

    # mp4 完了待ち（appear/stable の二段階）
    completed = await _wait_for_mp4_complete(output_path, fingerprint=item.get("fingerprint"))
    if completed:
        item["status"] = "done"
    else:
        prog = _ame_progress.get(item.get("fingerprint", ""), {})
        phase = prog.get("phase", "")
        item["status"] = "error"
        if phase == "appear_timeout":
            item["error"] = (
                f"mp4 が出現せず（{prog.get('elapsed_sec', 0)}秒）。"
                "AME がジョブを取りこぼした可能性。AME の起動状態と書き出し先を確認してください。"
            )
        elif phase == "stable_timeout":
            item["error"] = (
                f"mp4 サイズが安定せず（{prog.get('elapsed_sec', 0)}秒）。"
                "AME ハング / ストレージ書き込み詰まりの可能性。"
            )
        else:
            item["error"] = f"mp4_timeout (phase={phase or 'unknown'})"
    item["completed_at"] = datetime.utcnow().isoformat() + "Z"
    q = get_export_queue()
    for x in q["items"]:
        if x["fingerprint"] == item["fingerprint"]: x.update(item)
    save_export_queue(q)

    # 完了したフォルダは ignore list に自動追加（watcher が再検知しないように）
    if item["status"] == "done":
        try:
            add_to_export_ignore_list(item.get("video_name", ""))
        except Exception as e:
            print(f"[export_processor] ignore-list add failed: {e}")

    # 書き出し完了後の YouTube 自動アップロード
    # 動画ファイルは output_path（外部 SSD の可能性あり）、メタは channel folder から
    if item["status"] == "done":
        try:
            rules = get_export_rules()
            if rules.get("auto_upload_after_export"):
                privacy = (rules.get("auto_upload_privacy") or "unlisted").strip() or "unlisted"
                _prepare_youtube_meta(folder)
                _kick_auto_upload(folder, privacy=privacy, video_path=str(output_path))
        except Exception as e:
            print(f"[export_processor] auto_upload error: {e}")

    return item


def _prepare_youtube_meta(folder: Path):
    """自動アップロード前にメタファイル（title/description/tags）が無ければ生成。
    タイムスタンプは対応する music_time_code_info_*.txt の LOOP 直前までを概要欄に流し込む。"""
    folder = Path(folder)
    m = re.match(r"^(\d+)_", folder.name)
    vol = m.group(1) if m else "00"

    # title
    title_file = folder / "youtube_title.txt"
    if not title_file.exists() or not title_file.read_text(encoding="utf-8").strip():
        title_text = ""
        meta_json = folder / "video_youtube_title.json"
        if meta_json.exists():
            try:
                d = json.loads(meta_json.read_text(encoding="utf-8"))
                title_text = (d.get("new_title") or d.get("title") or "").strip()
            except Exception:
                pass
        if not title_text:
            title_text = f"orzz. vol.{vol}"
        title_file.write_text(title_text, encoding="utf-8")

    # description（タイムスタンプ含む）
    desc_file = folder / "youtube_description.txt"
    if not desc_file.exists() or not desc_file.read_text(encoding="utf-8").strip():
        timestamps, _tc_file = _read_matching_timecodes_until_loop(folder, vol)
        body = [
            f"orzz. vol.{vol}",
            "",
            "Lounge & jazz BGM for relax / work / study.",
            "",
        ]
        if timestamps:
            body.append("Tracklist")
            body.append(timestamps)
            body.append("")
        body.append("#orzz #BGM #LoungeJazz #ChillMusic")
        desc_file.write_text("\n".join(body), encoding="utf-8")


def _kick_auto_upload(folder: Path, privacy: str = "unlisted", video_path: Optional[str] = None):
    """既存のメタファイルを使って YouTube アップロードを起動（非ブロッキング）。
    video_path が指定された場合、その mp4 を直接使う（外部 SSD 用）。
    folder は title/description/tags/サムネイル等メタ取得元として常に必要。
    トークンはアクティブチャンネルの `.youtube_token.json` を明示指定。
    """
    try:
        cmd, meta = _build_youtube_upload_command(
            UploadRequest(folder=str(folder), privacy=privacy, video_path=video_path)
        )
        result = _enqueue_youtube_upload(cmd, meta, source="auto")
        print(
            f"[auto_upload] queued #{result.get('job', {}).get('id')}: "
            f"folder={folder} video={video_path or '(folder内 mp4)'} privacy={privacy}"
        )
    except Exception as e:
        print(f"[auto_upload] queue failed: {e}")


_export_watcher_task = None
_export_processor_task = None

async def _export_watcher_loop():
    """定期的に channel_folder をスキャン → 完成条件マッチを enqueue"""
    while True:
        rules = get_export_rules()
        if rules.get("watcher_enabled"):
            try:
                for info in _scan_all_for_export():
                    _enqueue_export(info)
            except Exception as e:
                print(f"[export_watcher] scan error: {e}")
        await asyncio.sleep(int(rules.get("scan_interval_sec", 30)))

async def _export_processor_loop():
    """キュー先頭から順次処理。並列実行はしない（AME 1 ジョブずつ）。"""
    while True:
        try:
            q = get_export_queue()
            pending = [x for x in q["items"] if x.get("status") == "pending"]
            if pending:
                await _process_export_item(pending[0])
        except Exception as e:
            print(f"[export_processor] error: {e}")
        await asyncio.sleep(5)

@app.on_event("startup")
async def _start_export_workers():
    global _export_watcher_task, _export_processor_task
    # 起動時に running 残骸を error にリセット（前回プロセスが kill された場合の救済）
    try:
        q = get_export_queue()
        changed = False
        for x in q["items"]:
            if x.get("status") == "running":
                x["status"] = "error"
                x["error"] = "サーバー再起動で中断"
                x["completed_at"] = datetime.utcnow().isoformat() + "Z"
                changed = True
        if changed:
            save_export_queue(q)
    except Exception as e:
        print(f"[startup] running reset failed: {e}")
    if _export_watcher_task is None:
        _export_watcher_task = asyncio.create_task(_export_watcher_loop())
    if _export_processor_task is None:
        _export_processor_task = asyncio.create_task(_export_processor_loop())

# ─── API: 書き出しキュー ───

@app.get("/api/export/queue")
def api_get_export_queue():
    return get_export_queue()


@app.get("/api/ame/queue")
def api_get_ame_queue():
    """AME キューの進捗付き表示。トップバーチップ・自動化タブ詳細から呼ばれる。

    Returns:
      {
        "queue": [{...item, "progress": {size_bytes, rate_bytes_per_sec, elapsed_sec, phase}}],
        "current_index": int|null,    # running 中アイテムの 0-based index
        "total": int,                  # pending+running+done の合計（クリア前）
        "running_count": int,
        "pending_count": int,
        "done_count": int,
        "error_count": int,
        "watcher_enabled": bool
      }
    """
    q = get_export_queue()
    items = q.get("items", []) or []
    enriched = []
    current_index = None
    counts = {"pending": 0, "running": 0, "done": 0, "error": 0}
    for i, it in enumerate(items):
        st = it.get("status", "pending")
        counts[st] = counts.get(st, 0) + 1
        prog = _ame_progress.get(it.get("fingerprint", "")) or {}
        enriched.append({**it, "progress": prog})
        if st == "running" and current_index is None:
            current_index = i
    rules = get_export_rules()
    return {
        "queue": enriched,
        "current_index": current_index,
        "total": len(items),
        "running_count": counts.get("running", 0),
        "pending_count": counts.get("pending", 0),
        "done_count": counts.get("done", 0),
        "error_count": counts.get("error", 0),
        "watcher_enabled": bool(rules.get("watcher_enabled")),
    }

class ExportQueueAddRequest(BaseModel):
    video_name: str

@app.post("/api/export/queue")
def api_add_export_queue(req: ExportQueueAddRequest):
    config = get_dashboard_config()
    folder = Path(config.get("channel_folder", "")) / req.video_name
    if not folder.exists():
        raise HTTPException(404, f"動画フォルダが見つかりません: {req.video_name}")
    info = _scan_video_folder_for_export(folder)
    if not info.get("prproj_path"):
        raise HTTPException(400, "prproj が見つかりません")
    return _enqueue_export(info)

@app.delete("/api/export/queue/{fingerprint}")
def api_delete_export_queue(fingerprint: str, ignore: bool = False):
    """キューから削除。ignore=true なら video_name を ignore list へ追加して
    watcher が再検知しないようにする。"""
    q = get_export_queue()
    target_names = [x.get("video_name", "") for x in q["items"]
                    if x.get("fingerprint") == fingerprint and x.get("status") != "running"]
    before = len(q["items"])
    q["items"] = [x for x in q["items"] if x.get("fingerprint") != fingerprint or x.get("status") == "running"]
    save_export_queue(q)
    if ignore:
        for name in target_names:
            if name:
                add_to_export_ignore_list(name)
    return {"status": "ok", "removed": before - len(q["items"]),
            "ignored": target_names if ignore else []}


@app.post("/api/export/queue/{fingerprint}/cancel")
def api_cancel_export_item(fingerprint: str):
    """個別キャンセル: pending → 削除、running → subprocess を kill して error 化。
    履歴は残るので、必要なら別途 clear-done でまとめて掃除する。"""
    q = get_export_queue()
    target = next((x for x in q["items"] if x.get("fingerprint") == fingerprint), None)
    if target is None:
        raise HTTPException(404, "fingerprint not found")
    status = target.get("status")
    if status == "running":
        killed = _kill_active_proc(fingerprint)
        # subprocess の wait が完了して finally で status 更新するのを待つよりは、
        # ここで即座に error 化しておく（race しても最終的な状態は error のまま）。
        target["status"] = "error"
        target["error"] = "ユーザーがキャンセル"
        target["completed_at"] = datetime.utcnow().isoformat() + "Z"
        save_export_queue(q)
        return {"status": "ok", "action": "killed" if killed else "marked_error",
                "fingerprint": fingerprint}
    if status == "pending":
        # pending は subprocess がまだ無いので削除でよい
        q["items"] = [x for x in q["items"] if x.get("fingerprint") != fingerprint]
        save_export_queue(q)
        return {"status": "ok", "action": "removed", "fingerprint": fingerprint}
    # done / error は単に削除
    q["items"] = [x for x in q["items"] if x.get("fingerprint") != fingerprint]
    save_export_queue(q)
    return {"status": "ok", "action": "removed", "fingerprint": fingerprint}


@app.post("/api/export/queue/stop-all")
def api_stop_all_export_queue():
    """全停止: running を kill、pending を error 化。done/error は触らない。
    履歴は残るので、続けて completed をクリアしたい場合は clear-done を使う。"""
    q = get_export_queue()
    killed = []
    for x in q["items"]:
        st = x.get("status")
        if st == "running":
            _kill_active_proc(x.get("fingerprint", ""))
            x["status"] = "error"
            x["error"] = "ユーザー操作で停止"
            x["completed_at"] = datetime.utcnow().isoformat() + "Z"
            killed.append(x.get("video_name"))
        elif st == "pending":
            x["status"] = "error"
            x["error"] = "ユーザー操作で停止（実行前）"
            x["completed_at"] = datetime.utcnow().isoformat() + "Z"
            killed.append(x.get("video_name"))
    save_export_queue(q)
    return {"status": "ok", "stopped": killed}


@app.post("/api/export/queue/reset")
def api_reset_export_queue():
    """キュー完全リセット: running を kill して items を空にする。履歴も全消去。"""
    q = get_export_queue()
    for x in q["items"]:
        if x.get("status") == "running":
            _kill_active_proc(x.get("fingerprint", ""))
    save_export_queue({"items": []})
    return {"status": "ok", "cleared": len(q["items"])}


@app.post("/api/export/queue/clear-done")
def api_clear_done_export_queue(to_ignore: bool = True):
    """完了/エラー済みアイテムをキューから削除。to_ignore=true（既定）なら
    削除した video_name を ignore list へ追加して再検知を防ぐ。"""
    q = get_export_queue()
    cleared = [x for x in q["items"] if x.get("status") in ("done", "error")]
    q["items"] = [x for x in q["items"] if x.get("status") not in ("done", "error")]
    save_export_queue(q)
    ignored = []
    if to_ignore:
        for x in cleared:
            n = x.get("video_name", "")
            if n:
                add_to_export_ignore_list(n)
                ignored.append(n)
    return {"status": "ok", "removed": len(cleared), "ignored": ignored}


# ── AME 書き出し無視リスト ──
@app.get("/api/export/ignore-list")
def api_get_export_ignore_list():
    return {"items": get_export_ignore_list()}


class ExportIgnoreItem(BaseModel):
    video_name: str


@app.post("/api/export/ignore-list")
def api_add_export_ignore(req: ExportIgnoreItem):
    items = add_to_export_ignore_list(req.video_name.strip())
    # 該当 video_name の pending アイテムもキューから外す
    q = get_export_queue()
    before = len(q["items"])
    q["items"] = [x for x in q["items"]
                  if x.get("video_name") != req.video_name or x.get("status") == "running"]
    if len(q["items"]) != before:
        save_export_queue(q)
    return {"status": "ok", "items": items, "queue_removed": before - len(q["items"])}


@app.delete("/api/export/ignore-list/{video_name}")
def api_remove_export_ignore(video_name: str):
    items = remove_from_export_ignore_list(video_name)
    return {"status": "ok", "items": items}


class ExportIgnoreBulk(BaseModel):
    video_names: List[str]


@app.put("/api/export/ignore-list")
def api_replace_export_ignore(req: ExportIgnoreBulk):
    """無視リストを丸ごと置換（管理 UI 向け）。"""
    cc = load_channel_config()
    cleaned = sorted({n.strip() for n in (req.video_names or []) if n and n.strip()})
    cc["export_ignore_list"] = cleaned
    save_channel_config(cc)
    return {"status": "ok", "items": cleaned}

@app.get("/api/export/rules")
def api_get_export_rules():
    return get_export_rules()

class ExportRulesPatch(BaseModel):
    watcher_enabled: Optional[bool] = None
    scan_interval_sec: Optional[int] = None
    size_stable_window_sec: Optional[int] = None
    auto_upload_after_export: Optional[bool] = None
    auto_upload_privacy: Optional[str] = None
    rules: Optional[dict] = None

@app.put("/api/export/rules")
def api_put_export_rules(patch: ExportRulesPatch):
    cfg = get_export_rules()
    body = patch.dict(exclude_none=True)
    if "rules" in body and isinstance(body["rules"], dict):
        cfg["rules"] = {**cfg.get("rules", {}), **body.pop("rules")}
    cfg.update(body)
    save_export_rules(cfg)
    return {"status": "ok", "config": cfg}

@app.post("/api/export/scan")
def api_export_scan(include_ignored: bool = False):
    """即時スキャン: 完成条件マッチした vol 一覧を返す（enqueue はしない）。
    include_ignored=True にすると無視リスト内のフォルダも含めて返す（UI 管理用）。"""
    return {
        "completable": _scan_all_for_export(include_ignored=include_ignored),
        "ignore_list": get_export_ignore_list(),
    }


# ─── API: 認証（Sprint 5-C） ───

class LoginRequest(BaseModel):
    token: str

@app.post("/api/auth/login")
def api_auth_login(req: LoginRequest):
    """トークンを検証し、Cookie にセット。"""
    expected = _get_or_create_auth_token()
    if not secrets.compare_digest((req.token or "").strip(), expected):
        raise HTTPException(401, "トークンが一致しません")
    resp = JSONResponse({"status": "ok"})
    # Cookie は HTTPS 推奨だが、ローカル運用 + Cloudflare Tunnel で柔軟に
    resp.set_cookie("orzz_token", expected, httponly=True, samesite="lax", max_age=60*60*24*30)
    return resp

@app.get("/api/auth/check")
def api_auth_check(request: Request):
    """認証が必要かどうか + 現在の認証状態を返す（ログイン画面の判断用）"""
    if not AUTH_REQUIRED:
        return {"required": False, "authenticated": True}
    expected = _get_or_create_auth_token()
    received = request.cookies.get("orzz_token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        received = auth[7:].strip()
    return {"required": True, "authenticated": secrets.compare_digest(received, expected)}

@app.post("/api/auth/logout")
def api_auth_logout():
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("orzz_token")
    return resp

@app.post("/api/auth/regenerate-token")
def api_auth_regenerate():
    """トークンを再生成。既存セッションは全て無効化される。"""
    new = secrets.token_urlsafe(24)
    AUTH_TOKEN_FILE.write_text(new)
    try:
        AUTH_TOKEN_FILE.chmod(0o600)
    except Exception:
        pass
    return {"status": "ok", "token": new}


# ─── スケジュール（APScheduler 統合） ───

_scheduler = None
_scheduler_history = []  # 直近 50 件の実行履歴

def _scheduler_get_jobs():
    return load_json(SCHEDULE_JOBS_FILE, {"jobs": []}).get("jobs", [])

def _scheduler_save_jobs(jobs):
    save_json(SCHEDULE_JOBS_FILE, {"jobs": jobs})

def _record_history(job_id: str, status: str, detail: str = ""):
    _scheduler_history.append({
        "job_id": job_id, "status": status, "detail": detail[:200],
        "at": datetime.utcnow().isoformat() + "Z",
    })
    if len(_scheduler_history) > 50:
        _scheduler_history[:] = _scheduler_history[-50:]

def _send_discord_notify(message: str):
    """Discord 通知を非同期 fire-and-forget で送る。失敗は無視。"""
    try:
        notify_script = SHARED_BASE / "Python" / "app_notify.sh"
        if notify_script.exists():
            subprocess.Popen(["bash", str(notify_script), message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _send_line_notify(message: str):
    """Backward-compatible alias for older scheduler code paths."""
    _send_discord_notify(message)

def _resolve_job_channel(job: dict) -> tuple:
    """job の channel_id をチャンネル名 + フォルダパス + id に解決。

    P1-3: グローバル `dashboard_config.json` を書き換えず、解決結果を子プロセスへ
    env (APP_CHANNEL_ID / APP_CHANNEL_FOLDER / APP_CHANNEL_NAME) で渡す。
    P2-2: id を返り値に含めることで、子プロセスに `--channel-id` を渡せるように。
    channel_id が空なら UI のアクティブチャンネルにフォールバック（id は空）。

    Returns: (channel_name: Optional[str], channel_folder: Optional[Path], channel_id: Optional[str])
    """
    channel_id = job.get("channel_id", "")
    if channel_id:
        chs = get_channels()
        ch = next((c for c in chs if c.get("id") == channel_id), None)
        if ch:
            return ch["name"], Path(ch["folder"]), ch.get("id") or ""
    # フォールバック: UI のアクティブチャンネル
    cfg = get_dashboard_config()
    folder = cfg.get("channel_folder") or ""
    name = cfg.get("channel_name") or ""
    # registry を逆引きして id を補完
    fallback_id = ""
    if folder:
        try:
            target = str(Path(folder).expanduser().resolve())
            for ch in get_channels():
                p = str(Path(ch.get("folder") or "").expanduser().resolve())
                if p == target:
                    fallback_id = ch.get("id") or ""
                    break
        except Exception:
            pass
    return (name or None), (Path(folder) if folder else None), fallback_id


def _build_job_env(ch_name: Optional[str], ch_folder: Optional[Path],
                   ch_id: Optional[str] = None) -> dict:
    """子プロセス用の env。グローバル設定を介さずチャンネルを伝達。"""
    env = {**os.environ}
    if ch_folder:
        env["APP_CHANNEL_FOLDER"] = str(ch_folder)
    if ch_name:
        env["APP_CHANNEL_NAME"] = ch_name
    if ch_id:
        env["APP_CHANNEL_ID"] = ch_id
    # 無人モード（input/sleep ハングを禁止）
    env["APP_NO_INTERACTIVE"] = "1"
    # P2-1: render queue を使う（premiere/export はシリアライズキュー経由）
    env["APP_USE_RENDER_QUEUE"] = "1"
    return env


# ─── P2-7: 公開ゲート — private upload + N 時間後に public 化 ──────
# 完全自走の前提: per-channel `publish_delay_hours` で「アップロード後 N 時間
# は private のまま、その後自動で public 化」を実現する。途中で運営者が
# YouTube Studio で確認して取消したい場合は手動で削除できる猶予時間。

async def _job_publish_now(payload: dict):
    """指定 vol を private → public に切り替える非同期ハンドラ。

    payload: {
      "id": "publish_<channel_id>_<vol>",
      "video_name": "78_vol_260420",
      "channel_folder": "/path/to/channel",
      "channel_name": "...",
    }
    """
    job_id = payload.get("id", "?")
    video_name = payload.get("video_name", "")
    ch_folder = payload.get("channel_folder", "")
    ch_name = payload.get("channel_name") or "(unknown)"
    folder = Path(ch_folder) / video_name if (ch_folder and video_name) else None
    if not folder or not folder.exists():
        _record_history(job_id, "error", f"publish: folder 不在 {folder}")
        _send_line_notify(f"❌ 公開: [{ch_name}] {video_name} - フォルダ不在")
        return
    _record_history(job_id, "started", f"[{ch_name}] {video_name} を public に切替")
    try:
        # subprocess ではなくモジュール直接呼び出し（軽量）
        sys.path.insert(0, str(SHARED_BASE / "Python"))
        from app_youtube import publish_video_to_public
        result = publish_video_to_public(folder)
    except Exception as e:
        _record_history(job_id, "error", f"publish 例外: {e}")
        _send_line_notify(f"❌ 公開: [{ch_name}] {video_name} 例外: {str(e)[:80]}")
        return
    status = result.get("status")
    vid = result.get("video_id", "")
    if status == "ok":
        url = f"https://youtu.be/{vid}"
        _record_history(job_id, "done", f"[{ch_name}] {video_name} → public ({vid})")
        _send_line_notify(f"🎉 [{ch_name}] {video_name} を public に公開しました\n{url}")
    elif status == "already_public":
        _record_history(job_id, "done", f"[{ch_name}] {video_name} は既に public")
    elif status == "retryable":
        # 30 分後に再投入
        _record_history(job_id, "error", f"transient: {result.get('error','')}")
        _send_line_notify(f"⚠ 公開: [{ch_name}] {video_name} transient エラー、30 分後に再試行")
        try:
            from apscheduler.triggers.date import DateTrigger
            import datetime as _dt
            run_at = _dt.datetime.now() + _dt.timedelta(minutes=30)
            new_id = f"{job_id}_retry_{int(time.time())}"
            payload2 = {**payload, "id": new_id}
            _scheduler.add_job(_job_publish_now, trigger=DateTrigger(run_date=run_at),
                               id=new_id, args=[payload2],
                               replace_existing=True, misfire_grace_time=300)
        except Exception as e:
            print(f"[publish] retry 登録失敗: {e}")
    else:
        _record_history(job_id, "error", f"{status}: {result.get('error','')[:200]}")
        _send_line_notify(f"❌ 公開: [{ch_name}] {video_name} 失敗 ({status})")


def _schedule_publish(*, video_name: str, channel_folder: str, channel_name: str,
                       run_at) -> str:
    """指定時刻に publish ジョブを APScheduler の DateTrigger で登録。"""
    if _scheduler is None:
        raise RuntimeError("scheduler が起動していません")
    from apscheduler.triggers.date import DateTrigger
    # チャンネル + vol 単位で id を作る（同じ vol を二重登録しないため replace_existing で上書き）
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{channel_folder}_{video_name}")[-80:]
    job_id = f"publish_{safe}"
    payload = {
        "id": job_id,
        "video_name": video_name,
        "channel_folder": channel_folder,
        "channel_name": channel_name,
    }
    _scheduler.add_job(
        _job_publish_now, trigger=DateTrigger(run_date=run_at),
        id=job_id, args=[payload],
        replace_existing=True, misfire_grace_time=600,
    )
    return job_id


# ─── P2-6: auto_resume — 失敗 stage 検出 + N 分後に再投入 ────
# pipeline は失敗時に標準出力末尾に「再開: python3 app_pipeline.py <vol> --from <stage>」を
# 必ず出すので、それから stage を抽出する。

# 手動介入が必要で resume しても無意味な exit code（P1-1/P1-4/P1-5 の sentinel）
_NO_AUTO_RESUME_CODES = {75, 77, 78}


def _parse_failed_stage(stdout: str) -> str:
    """pipeline の '再開: python3 app_pipeline.py <vol> --from <stage>' から stage を抽出。"""
    if not stdout:
        return ""
    m = re.search(r"--from\s+(\w+)", stdout)
    return m.group(1) if m else ""


def _should_auto_resume(returncode: int, stage: str, attempt: int, job: dict) -> tuple:
    """auto_resume 可否。Returns (ok: bool, reason_if_not: str)."""
    if not job.get("auto_resume"):
        return False, "auto_resume が無効"
    if returncode == 0:
        return False, "成功なので resume 不要"
    if returncode in _NO_AUTO_RESUME_CODES:
        return False, f"exit code {returncode} は手動介入が必要"
    if not stage:
        return False, "失敗 stage を特定できない"
    max_attempts = int(job.get("auto_resume_max_attempts") or 3)
    if attempt >= max_attempts:
        return False, f"再投入上限到達 ({attempt}/{max_attempts})"
    return True, ""


async def _job_auto_resume(payload: dict):
    """auto_resume 専用ハンドラ。N 分後の DateTrigger で起動される。

    payload は schedule_jobs.json には保存されない（一時的なメモリ内 APScheduler ジョブ）。
    """
    job_id = payload.get("id", "?")
    vol = payload.get("vol")
    if not vol:
        _record_history(job_id, "error", "resume: vol 未指定")
        return
    ch_name, ch_dir, ch_id = _resolve_job_channel(payload)
    if not ch_dir or not ch_dir.exists():
        _record_history(job_id, "error", f"resume: channel_folder 不在: {ch_dir}")
        _send_line_notify(f"❌ auto_resume: vol.{vol} - チャンネル不在")
        return
    from_stage = (payload.get("from_stage") or "").strip() or "suno"
    attempt = int(payload.get("attempt", 1))
    # P3-1: ledger に auto_resume として記録（parent を持つ）
    run_id = ""
    parent_rid = payload.get("_parent_run_id") or payload.get("parent_run_id") or ""
    try:
        import app_run_ledger as _ledger
        run_id = _ledger.start_run(
            kind="auto_resume", channel_folder=str(ch_dir),
            channel_id=ch_id or "", channel_name=ch_name or "",
            vol=int(vol), parent_run_id=parent_rid,
            from_stage=from_stage,
            meta={"attempt": attempt},
        )
    except Exception as e:
        print(f"[ledger] start_run 失敗（無視）: {e}")
    _record_history(job_id, "started",
                    f"[{ch_name}] resume vol.{vol} from {from_stage} (attempt {attempt}, run={run_id[-12:] if run_id else '?'})")
    _send_line_notify(
        f"🔄 [{ch_name}] vol.{vol} を {from_stage} から自動再投入中（{attempt} 回目）"
    )
    cmd = [sys.executable, str(SHARED_BASE / "Python" / "app_pipeline.py"),
           str(vol), "--auto", "--from", from_stage]
    if ch_id:
        cmd += ["--channel-id", ch_id]
    else:
        cmd += ["--channel-folder", str(ch_dir)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=_build_job_env(ch_name, ch_dir, ch_id),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        stdout_str = (out or b"").decode(errors="replace")
        if proc.returncode == 0:
            _record_history(job_id, "done", f"[{ch_name}] resume vol.{vol} 完了")
            _send_line_notify(f"✅ [{ch_name}] vol.{vol} 再投入で完了")
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="done", exit_code=0,
                                       summary=f"resume vol.{vol} 完了 (attempt {attempt})")
                except Exception:
                    pass
            return
        # 再失敗 → さらに resume できるか
        next_stage = _parse_failed_stage(stdout_str) or from_stage
        ok, reason = _should_auto_resume(proc.returncode, next_stage, attempt, payload)
        if run_id:
            try:
                import app_run_ledger as _ledger
                _ledger.finish_run(run_id, status="failed",
                                   exit_code=proc.returncode,
                                   failed_stage=next_stage,
                                   summary=f"resume vol.{vol} 失敗 (attempt {attempt}, next={next_stage})")
            except Exception:
                pass
        if ok:
            payload["_parent_run_id"] = run_id
            _schedule_resume(payload, next_stage, attempt + 1)
            _send_line_notify(
                f"❌→🔄 [{ch_name}] vol.{vol} 再失敗、{int(payload.get('auto_resume_delay_min') or 30)} 分後に再々投入をスケジュール（{attempt + 1} 回目）"
            )
            _record_history(job_id, "error",
                            f"[{ch_name}] resume vol.{vol} 失敗、再々投入予約: {next_stage}")
        else:
            tail = stdout_str[-200:]
            _record_history(job_id, "error", f"resume 打ち切り ({reason}): {tail}")
            _send_line_notify(f"⛔ [{ch_name}] vol.{vol} 再投入打ち切り: {reason}")
    except Exception as e:
        _record_history(job_id, "error", f"resume 例外: {e}")
        if run_id:
            try:
                import app_run_ledger as _ledger
                _ledger.finish_run(run_id, status="failed", summary=f"例外: {str(e)[:100]}")
            except Exception:
                pass


def _schedule_resume(parent_payload: dict, from_stage: str, attempt: int) -> bool:
    """N 分後の DateTrigger で resume ジョブを APScheduler に登録（永続化なし）。"""
    if _scheduler is None:
        print("[scheduler] resume 失敗: scheduler が起動していない")
        return False
    delay_min = int(parent_payload.get("auto_resume_delay_min") or 30)
    import datetime as _dt
    run_at = _dt.datetime.now() + _dt.timedelta(minutes=delay_min)
    parent_id = parent_payload.get("id", "?")
    payload = {
        # 元のジョブ id を含めて新 id を組み立てる
        "id": f"resume_{parent_id}_{attempt}",
        "vol": parent_payload.get("vol") or parent_payload.get("_resolved_vol"),
        "channel_id": parent_payload.get("channel_id"),
        "from_stage": from_stage,
        "attempt": attempt,
        "auto_resume": True,
        "auto_resume_delay_min": delay_min,
        "auto_resume_max_attempts": int(parent_payload.get("auto_resume_max_attempts") or 3),
        "name": f"auto_resume({parent_id})",
    }
    try:
        from apscheduler.triggers.date import DateTrigger
        _scheduler.add_job(
            _job_auto_resume, trigger=DateTrigger(run_date=run_at),
            id=payload["id"], args=[payload],
            replace_existing=True, misfire_grace_time=300,
        )
        print(f"[scheduler] resume スケジュール: {payload['id']} at {run_at.isoformat()} (from={from_stage}, attempt={attempt})")
        return True
    except Exception as e:
        print(f"[scheduler] resume 登録失敗: {e}")
        return False


async def _job_vol_create(job: dict):
    """次の vol を fully-auto で作る。channel_id 指定可。

    P1-3: dashboard_config.json は読み取り専用扱い。channel_folder を直接 subprocess に渡す。
    P3-1: 実行を中央 ledger (`runs.db`) に記録。
    """
    job_id = job.get("id", "?")
    ch_name, ch_dir, ch_id = _resolve_job_channel(job)
    if not ch_dir or not ch_dir.exists():
        _record_history(job_id, "error", f"channel_folder が存在しない: {ch_dir}")
        _send_line_notify(f"❌ Automation Studio 自動: vol 作成失敗 ({job_id}) - チャンネル不在")
        return
    # 次の vol 番号を解決（グローバル config 経由ではなく、解決済み folder を直接スキャン）
    max_n = 0
    for d in ch_dir.iterdir():
        m = re.match(r"^(\d+)_", d.name)
        if m: max_n = max(max_n, int(m.group(1)))
    next_vol = max_n + 1
    # P3-1: ledger に in_progress で記録
    run_id = ""
    try:
        import app_run_ledger as _ledger
        run_id = _ledger.start_run(
            kind="vol_create", channel_folder=str(ch_dir),
            channel_id=ch_id or "", channel_name=ch_name or "",
            vol=next_vol, parent_job_id=job_id,
        )
    except Exception as e:
        print(f"[ledger] start_run 失敗（無視）: {e}")
    _record_history(job_id, "started", f"[{ch_name}] vol.{next_vol} 作成開始 (run={run_id[-12:] if run_id else '?'})")
    _send_line_notify(f"🚀 [{ch_name}] vol.{next_vol} の作成を開始 ({job.get('name', job_id)})")
    # app_pipeline.py を起動（registry 由来の id があれば --channel-id 優先）
    cmd = [sys.executable, str(SHARED_BASE / "Python" / "app_pipeline.py"),
           str(next_vol), "--from-benchmark", "--auto"]
    if ch_id:
        cmd += ["--channel-id", ch_id]
    else:
        cmd += ["--channel-folder", str(ch_dir)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=_build_job_env(ch_name, ch_dir, ch_id),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        stdout_str = (out or b"").decode(errors="replace")
        if proc.returncode == 0:
            _record_history(job_id, "done", f"[{ch_name}] vol.{next_vol} 完了")
            _send_line_notify(f"✅ [{ch_name}] vol.{next_vol} 完了")
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="done", exit_code=0,
                                       summary=f"vol.{next_vol} 完了")
                except Exception as e:
                    print(f"[ledger] finish_run 失敗: {e}")
        else:
            tail = stdout_str[-500:]
            _record_history(job_id, "error", tail)
            # P2-6: auto_resume が有効なら失敗 stage を解析して N 分後に再投入
            failed_stage = _parse_failed_stage(stdout_str)
            resume_payload = {
                **job,
                "vol": next_vol,  # 動的に解決した vol を保持
                "_resolved_vol": next_vol,
                "_parent_run_id": run_id,
            }
            ok, reason = _should_auto_resume(proc.returncode, failed_stage, 1, resume_payload)
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="failed",
                                       exit_code=proc.returncode,
                                       failed_stage=failed_stage,
                                       summary=f"vol.{next_vol} 失敗 ({failed_stage or 'unknown'})")
                except Exception as e:
                    print(f"[ledger] finish_run 失敗: {e}")
            if ok:
                _schedule_resume(resume_payload, failed_stage, attempt=2)
                _send_line_notify(
                    f"❌→🔄 [{ch_name}] vol.{next_vol} 失敗（{failed_stage}）、"
                    f"{int(job.get('auto_resume_delay_min') or 30)} 分後に自動再投入"
                )
            else:
                _send_line_notify(
                    f"❌ [{ch_name}] vol.{next_vol} 失敗 (exit={proc.returncode}) — auto_resume なし: {reason}"
                )
    except Exception as e:
        _record_history(job_id, "error", str(e)[:200])
        _send_line_notify(f"❌ [{ch_name}] vol.{next_vol} 例外: {str(e)[:80]}")
        if run_id:
            try:
                import app_run_ledger as _ledger
                _ledger.finish_run(run_id, status="failed",
                                   summary=f"例外: {str(e)[:100]}")
            except Exception:
                pass

async def _job_benchmark_refresh(job: dict):
    job_id = job.get("id", "?")
    _record_history(job_id, "started", "benchmark refresh")
    try:
        # /api/analysis/competitors を内部呼び出し
        await api_analysis_competitors()
        _record_history(job_id, "done", "")
    except Exception as e:
        _record_history(job_id, "error", str(e)[:200])

async def _job_export_window(job: dict):
    """on/off の time_of_day 指定で watcher を ON/OFF 切替"""
    job_id = job.get("id", "?")
    action = (job.get("action") or "").lower()
    cfg = get_export_rules()
    if action == "on":
        cfg["watcher_enabled"] = True
    elif action == "off":
        cfg["watcher_enabled"] = False
    else:
        _record_history(job_id, "error", f"未知の action: {action}")
        return
    save_export_rules(cfg)
    _record_history(job_id, "done", f"watcher {action}")

async def _job_spot_create(job: dict):
    """指定 vol を fully-auto で作る（複数チャンネル対応）。

    P1-3: dashboard_config.json は読み取り専用扱い。同時刻に複数チャンネルのジョブが
    走ってもグローバル状態が衝突しない。
    """
    job_id = job.get("id", "?")
    vol = job.get("vol")
    if not vol:
        _record_history(job_id, "error", "vol 未指定")
        return
    ch_name, ch_dir, ch_id = _resolve_job_channel(job)
    if not ch_dir or not ch_dir.exists():
        _record_history(job_id, "error", f"channel_folder が存在しない: {ch_dir}")
        _send_line_notify(f"❌ Automation Studio スポット: vol.{vol} 失敗 - チャンネル不在")
        return
    # P3-1: ledger 開始
    run_id = ""
    try:
        import app_run_ledger as _ledger
        run_id = _ledger.start_run(
            kind="spot_create", channel_folder=str(ch_dir),
            channel_id=ch_id or "", channel_name=ch_name or "",
            vol=int(vol), parent_job_id=job_id,
        )
    except Exception as e:
        print(f"[ledger] start_run 失敗（無視）: {e}")
    _record_history(job_id, "started", f"[{ch_name}] spot vol.{vol} (run={run_id[-12:] if run_id else '?'})")
    _send_line_notify(f"🚀 [{ch_name}] スポット vol.{vol} 開始")
    cmd = [sys.executable, str(SHARED_BASE / "Python" / "app_pipeline.py"),
           str(vol), "--from-benchmark", "--auto"]
    if ch_id:
        cmd += ["--channel-id", ch_id]
    else:
        cmd += ["--channel-folder", str(ch_dir)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=_build_job_env(ch_name, ch_dir, ch_id),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        stdout_str = (out or b"").decode(errors="replace")
        ok = proc.returncode == 0
        if ok:
            _record_history(job_id, "done", f"[{ch_name}] vol.{vol} 完了")
            _send_line_notify(f"✅ [{ch_name}] スポット vol.{vol}")
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="done", exit_code=0,
                                       summary=f"spot vol.{vol} 完了")
                except Exception as e:
                    print(f"[ledger] finish_run 失敗: {e}")
        else:
            tail = stdout_str[-300:]
            _record_history(job_id, "error", tail)
            # P2-6: auto_resume
            failed_stage = _parse_failed_stage(stdout_str)
            resume_payload = {**job, "_resolved_vol": vol, "_parent_run_id": run_id}
            should_resume, reason = _should_auto_resume(proc.returncode, failed_stage, 1, resume_payload)
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="failed",
                                       exit_code=proc.returncode,
                                       failed_stage=failed_stage,
                                       summary=f"spot vol.{vol} 失敗 ({failed_stage or 'unknown'})")
                except Exception as e:
                    print(f"[ledger] finish_run 失敗: {e}")
            if should_resume:
                _schedule_resume(resume_payload, failed_stage, attempt=2)
                _send_line_notify(
                    f"❌→🔄 [{ch_name}] スポット vol.{vol} 失敗（{failed_stage}）、"
                    f"{int(job.get('auto_resume_delay_min') or 30)} 分後に自動再投入"
                )
            else:
                _send_line_notify(
                    f"❌ [{ch_name}] スポット vol.{vol} 失敗 (exit={proc.returncode}) — auto_resume なし: {reason}"
                )
    except Exception as e:
        _record_history(job_id, "error", str(e)[:200])
        if run_id:
            try:
                import app_run_ledger as _ledger
                _ledger.finish_run(run_id, status="failed",
                                   summary=f"例外: {str(e)[:100]}")
            except Exception:
                pass

async def _job_token_health(job: dict):
    """P3-5: 全チャンネルのトークン健全性を点検し、warn 以上を Discord 通知。

    既定は日次 cron で運用想定。副作用は通知のみ（自動再認証はしない）。"""
    job_id = job.get("id", "?")
    try:
        sys.path.insert(0, str(SHARED_BASE / "Python"))
        from app_token_health import check_all
        result = check_all()
    except Exception as e:
        _record_history(job_id, "error", f"token_health 例外: {e}")
        return
    warnings = result.get("warnings", [])
    if not warnings:
        _record_history(job_id, "done", "全トークン正常")
        return
    # warn を Discord で 1 通にまとめて通知
    lines = ["⚠ トークン健全性チェック - 要対応:"]
    for w in warnings[:10]:
        lines.append(f"  • [{w.get('channel_name','?')}] {w.get('kind','?')}: {w.get('status','?')} - {w.get('message','')[:80]}")
    if len(warnings) > 10:
        lines.append(f"  ... 他 {len(warnings) - 10} 件")
    lines.append("\n対応: docs/runbook.md の「unattended-login」セクション参照")
    _send_line_notify("\n".join(lines))
    _record_history(job_id, "error" if any(w.get("status") in ("expired", "missing") for w in warnings) else "done",
                    f"{len(warnings)} 件の warn")


JOB_HANDLERS = {
    "vol_create": _job_vol_create,
    "benchmark_refresh": _job_benchmark_refresh,
    "export_window": _job_export_window,
    "spot_create": _job_spot_create,
    "publish_now": _job_publish_now,  # P2-7: 公開ゲート（DateTrigger 動的登録）
    "token_health": _job_token_health,  # P3-5: トークン期限の先回り通知
}

# ─── P2-4: 時間帯スロット配分（slot balancing） ─────
# 6+ チャンネルで同時刻に vol_create/spot_create が発火すると、render queue
# は serialized でも上流（SUNO / Claude CLI）が同時並列で過負荷になる。
# 30 分刻みで slot をずらすことで、自然と階段状の起動になり負荷を平準化する。

SLOT_GRANULARITY_MIN = 30  # 30 分単位
SLOT_BALANCED_TYPES = {"vol_create", "spot_create"}  # チャンネル重量級ジョブのみ対象
SLOT_MAX_SHIFTS = 48  # 30 分 × 48 = 24 時間分まで自動調整


# P3-4: render queue 実測から最低 gap を推定（fallback は SLOT_GRANULARITY_MIN）
def _estimate_required_gap_minutes() -> int:
    """直近 7 日の `premiere + export` 平均所要から、必要な slot gap を計算。

    Premiere は 1 セッション固定なので、上流で密集すると後段が詰まる → 平均サイクルに
    合わせて gap を動的に広げる。"""
    try:
        import app_render_queue as _rq
        s = _rq.stats(window_days=7)
        avg_p = int((s.get("by_stage_avg_sec") or {}).get("premiere") or 0)
        avg_e = int((s.get("by_stage_avg_sec") or {}).get("export") or 0)
        cycle_sec = avg_p + avg_e
        if cycle_sec > 0:
            cycle_min = (cycle_sec + 59) // 60  # 切り上げ分
            # SLOT_GRANULARITY_MIN の倍数に切り上げる
            mult = (cycle_min + SLOT_GRANULARITY_MIN - 1) // SLOT_GRANULARITY_MIN
            return max(SLOT_GRANULARITY_MIN, mult * SLOT_GRANULARITY_MIN)
    except Exception:
        pass
    return SLOT_GRANULARITY_MIN


def _channel_quota_pressure(channel_id: str) -> float:
    """channel が直近 24h で quota をどれだけ使っているか（0.0〜1.0+）。

    register 対象が date trigger の場合のみ意味がある（cron は仕様上 24h ウィンドウに
    bind しない）。"""
    if not channel_id:
        return 0.0
    try:
        chs = get_channels()
        ch = next((c for c in chs if c.get("id") == channel_id), None)
        if not ch:
            return 0.0
        cf = Path(ch.get("folder") or "")
        if not cf.exists():
            return 0.0
        import app_youtube as _yt
        used = _yt.quota_used_in_window(cf)
        cap = _yt.DEFAULT_DAILY_QUOTA_CAP
        if cap <= 0:
            return 0.0
        return used / cap
    except Exception:
        return 0.0


def _trigger_slot_key(trigger: dict) -> Optional[tuple]:
    """同一スロット判定用のキー。比較対象外（cron expr / 不正）は None。"""
    if not isinstance(trigger, dict):
        return None
    kind = trigger.get("kind", "cron")
    if kind == "cron" and not trigger.get("expr"):
        # day_of_week + hour + minute を正規化
        dow = str(trigger.get("day_of_week", "*"))
        hour = trigger.get("hour", "*")
        minute = trigger.get("minute", 0)
        try:
            hour = int(hour) if hour != "*" else hour
            minute = int(minute)
        except Exception:
            return None
        return ("cron", dow, hour, minute)
    if kind == "date":
        rd = trigger.get("run_date") or ""
        return ("date", rd[:16])  # 分単位で比較（秒以下は無視）
    return None


def _shift_trigger(trigger: dict, minutes: int) -> dict:
    """trigger を minutes 分後ろにずらした dict を返す（破壊的でない）。"""
    out = dict(trigger)
    kind = out.get("kind", "cron")
    if kind == "cron" and not out.get("expr"):
        try:
            hour = int(out.get("hour", 0))
            minute = int(out.get("minute", 0))
        except Exception:
            return out
        total = hour * 60 + minute + minutes
        new_hour = (total // 60) % 24
        new_min = total % 60
        out["hour"] = new_hour
        out["minute"] = new_min
        return out
    if kind == "date":
        try:
            rd = datetime.fromisoformat(out.get("run_date") or "")
            rd2 = rd + datetime_timedelta(minutes=minutes)
            out["run_date"] = rd2.isoformat()
        except Exception:
            pass
    return out


# datetime.timedelta を fromisoformat と区別する別名
from datetime import timedelta as datetime_timedelta


def _balance_trigger_slot(job: dict, all_jobs: list) -> tuple:
    """衝突する slot を回避するため trigger を後ろにずらす。

    P2-4: シンプルな 30 分単位の slot 探索。
    P3-4 で policy-aware 化:
      - render queue 実測 throughput から「必要 gap」を動的に推定（fallback 30 分）
      - 同チャンネルジョブが直近に占めているなら、必要 gap 以上空ける
      - quota pressure が 70% 超のチャンネルは、当面 1 日後送り（date trigger のみ）
      - 全埋まりの場合は元のまま返す（運営者の判断を待つ）

    Returns: (final_trigger: dict, shift_minutes: int, shift_count: int)
    """
    if job.get("type") not in SLOT_BALANCED_TYPES:
        return job.get("trigger") or {}, 0, 0
    base_trigger = job.get("trigger") or {}
    base_key = _trigger_slot_key(base_trigger)
    if base_key is None:
        return base_trigger, 0, 0

    # 既存ジョブの占有スロット（自分自身は除く）+ チャンネル別の占有時刻
    own_id = job.get("id")
    own_channel = job.get("channel_id") or ""
    occupied: set = set()  # 全ジョブの slot key
    same_channel_keys: list = []  # 同チャンネルジョブの slot key（gap チェック用）
    for j in all_jobs:
        if j.get("id") == own_id:
            continue
        if j.get("type") not in SLOT_BALANCED_TYPES:
            continue
        if not j.get("enabled", True):
            continue
        k = _trigger_slot_key(j.get("trigger") or {})
        if k is None:
            continue
        occupied.add(k)
        if (j.get("channel_id") or "") == own_channel and own_channel:
            same_channel_keys.append(k)

    required_gap = _estimate_required_gap_minutes()

    # P3-4: quota pressure による初期 shift
    quota_initial_shift = 0
    if base_trigger.get("kind") == "date":
        pressure = _channel_quota_pressure(own_channel)
        if pressure > 0.70:
            # 70% 超 → 24h 後送り
            quota_initial_shift = 60 * 24
            print(f"[scheduler] {own_channel} quota pressure {pressure:.0%} → +24h 自動シフト")

    def _gap_ok(shifted_key: tuple) -> bool:
        """同チャンネルの直近占有との間隔が required_gap 以上か（cron only の概算）。"""
        if shifted_key[0] != "cron" or not same_channel_keys:
            return True
        # (kind, dow, hour, minute) → 当日内分単位の比較
        try:
            _, dow, hour, minute = shifted_key
            if not isinstance(hour, int) or not isinstance(minute, int):
                return True
            target_min = hour * 60 + minute
            for k in same_channel_keys:
                if k[0] != "cron":
                    continue
                _, kdow, khour, kminute = k
                if kdow != dow or not isinstance(khour, int) or not isinstance(kminute, int):
                    continue
                kmin = khour * 60 + kminute
                if abs(target_min - kmin) < required_gap:
                    return False
        except Exception:
            return True
        return True

    # quota_initial_shift から開始して空きスロットを探す
    for n in range(quota_initial_shift // SLOT_GRANULARITY_MIN, SLOT_MAX_SHIFTS + 1):
        shift = n * SLOT_GRANULARITY_MIN
        candidate = _shift_trigger(base_trigger, shift) if shift > 0 else base_trigger
        k = _trigger_slot_key(candidate)
        if k is None:
            return base_trigger, 0, 0
        if k in occupied:
            continue
        if not _gap_ok(k):
            continue
        return candidate, shift, n

    # 上限到達 → 元のまま
    return base_trigger, 0, 0


def _make_trigger(job: dict):
    """job の trigger 定義から APScheduler トリガーを生成"""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    t = job.get("trigger") or {}
    kind = t.get("kind", "cron")
    if kind == "cron":
        # cron 式 or {day_of_week, hour, minute}
        if t.get("expr"):
            return CronTrigger.from_crontab(t["expr"])
        return CronTrigger(
            day_of_week=t.get("day_of_week", "*"),
            hour=t.get("hour", "*"),
            minute=t.get("minute", 0),
        )
    if kind == "date":
        return DateTrigger(run_date=datetime.fromisoformat(t["run_date"]))
    raise ValueError(f"未知の trigger kind: {kind}")

def _scheduler_register_job(job: dict):
    handler = JOB_HANDLERS.get(job.get("type"))
    if not handler:
        return False
    try:
        trigger = _make_trigger(job)
    except Exception as e:
        print(f"[scheduler] trigger 生成失敗 {job.get('id')}: {e}")
        return False
    _scheduler.add_job(handler, trigger=trigger, id=job["id"], args=[job],
                       replace_existing=True, misfire_grace_time=300)
    return True

def _scheduler_reload():
    """jobs.json を読み直して APScheduler に登録し直す"""
    if _scheduler is None:
        return
    # 既存ジョブ全削除
    for j in _scheduler.get_jobs():
        try: _scheduler.remove_job(j.id)
        except Exception: pass
    # 再登録
    for job in _scheduler_get_jobs():
        if not job.get("enabled", True):
            continue
        _scheduler_register_job(job)

# ─── D1: orchestrator 無人稼働 tick（autopilot ON の channel のみ・既定 dormant）───
# ⚠ 安全境界:
#   - autopilot_enabled は per-channel 既定 OFF。全 ch OFF なら tick は空リストで即 return
#     ＝dispatch ゼロ（無人稼働しない）。実際に動くのは channel の autopilot を ON にした時だけ。
#   - WORKERS は upload/plan を含まない＝自動投稿・勝手な企画起案はしない（最終 upload は手動ゲート）。
#   - 連続 BREAKER_THRESHOLD(=3) 失敗の channel は is_channel_tripped で自動停止。
#   - AsyncIOScheduler は同期関数をスレッドプールで実行＝ブロッキング dispatch がループを止めない。
#   - env: APP_ORCH_TICK_ENABLED(既定1) / APP_ORCH_TICK_MINUTES(既定5) / APP_ORCH_AUTOPILOT_STAGES(既定=AUTOPILOT_DEFAULT_STAGES)
def _orchestrator_tick():
    """APScheduler 定期ジョブ本体。autopilot ON の channel を orchestrator.tick に投入。"""
    if (os.environ.get("APP_ORCH_TICK_ENABLED") or "1").strip() not in ("1", "true", "yes"):
        return
    try:
        sys.path.insert(0, str(SHARED_BASE / "Python"))
        import app_orchestrator as _orch
    except Exception as e:
        print(f"[orchestrator] tick import 失敗: {e}")
        return
    try:
        all_chans = _build_orchestrator_channels()
    except Exception as e:
        print(f"[orchestrator] channels 構築失敗: {e}")
        return
    enabled = [c for c in all_chans if c.get("autopilot_enabled")]
    if not enabled:
        return  # dormant: autopilot ON の channel が無い（既定）→ 何もしない
    stages_env = (os.environ.get("APP_ORCH_AUTOPILOT_STAGES") or "").strip()
    stage_list = ([s.strip() for s in stages_env.split(",") if s.strip()]
                  or list(getattr(_orch, "AUTOPILOT_DEFAULT_STAGES", [])))
    workers = {k: v for k, v in _orch.WORKERS.items() if k in stage_list} or None
    try:
        res = _orch.tick(enabled, workers=workers, via_api=False, notify=_send_line_notify)
        _record_history("orchestrator_tick", "ok",
                        f"enabled={len(enabled)} eval={res.get('evaluated')} "
                        f"dispatched={res.get('dispatched')} skipped_quota={res.get('skipped_quota')}")
        if res.get("dispatched"):
            print(f"[orchestrator] tick: {res}")
    except Exception as e:
        _record_history("orchestrator_tick", "error", str(e)[:200])
        print(f"[orchestrator] tick 失敗: {e}")


@app.on_event("startup")
async def _start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        _scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")
        _scheduler.start()
        _scheduler_reload()
        # D1: orchestrator tick を interval ジョブで登録（既定 dormant＝autopilot 全 OFF）
        if (os.environ.get("APP_ORCH_TICK_ENABLED") or "1").strip() in ("1", "true", "yes"):
            try:
                from apscheduler.triggers.interval import IntervalTrigger
                _tick_min = max(1, int(os.environ.get("APP_ORCH_TICK_MINUTES") or 5))
                _scheduler.add_job(_orchestrator_tick, trigger=IntervalTrigger(minutes=_tick_min),
                                   id="orchestrator_tick", replace_existing=True,
                                   max_instances=1, coalesce=True)
                print(f"[orchestrator] tick 登録（{_tick_min}分間隔・autopilot ON channel のみ実投入＝既定 dormant）")
            except Exception as e:
                print(f"[orchestrator] tick 登録失敗: {e}")
        print(f"[scheduler] started ({len(_scheduler.get_jobs())} jobs registered)")
    except ImportError:
        print("[scheduler] apscheduler 未インストール。pip install apscheduler が必要")
    except Exception as e:
        print(f"[scheduler] 起動失敗: {e}")


# ─── Render queue worker（P2-1） ────────────────────
# Premiere / Media Encoder は Mac 1 台に 1 セッションしか動かない物理制約があるため、
# 6+ チャンネル並列ジョブは render queue (SQLite) に enqueue → 1 worker が直列処理。
# 他 stage（plan/suno/rename/meta/upload）は queue を経由せず subprocess で並列実行。

_rq_worker_started = False


def _execute_render_job(job: dict) -> None:
    """1 ジョブを subprocess で実行。終了 → mark_done / mark_error。"""
    import app_render_queue as _rq
    job_id = int(job["id"])
    stage = str(job["stage"])
    ch_folder = Path(job["channel_folder"])
    video_name = job.get("video_name") or ""
    folder = ch_folder / video_name if video_name else ch_folder
    if not folder.exists():
        _rq.mark_error(job_id, f"video folder not found: {folder}")
        _send_line_notify(
            f"❌ render queue: [{job.get('channel_name','?')}] vol.{job['vol']} - フォルダ不在"
        )
        return

    cmd_base = [sys.executable, "-u", str(SHARED_BASE / "Python" / "app_premiere.py")]
    prproj = next(iter(folder.glob("*vol*.prproj")), None) or next(iter(folder.glob("*.prproj")), None)
    if stage == "premiere":
        cmd = cmd_base + ["--duration", "10800"]
        if prproj:
            cmd += ["--project", str(prproj)]
        timeout = 3600
    elif stage == "export":
        cmd = cmd_base + ["--export-only"]
        if prproj:
            cmd += ["--project", str(prproj)]
        timeout = 7200
    else:
        _rq.mark_error(job_id, f"unknown stage: {stage}")
        return

    env = {**os.environ}
    env["APP_CHANNEL_FOLDER"] = str(ch_folder)
    env["APP_CHANNEL_NAME"] = job.get("channel_name") or ""
    env["APP_NO_INTERACTIVE"] = "1"
    # worker から起動したサブプロセスが再帰的に enqueue しないよう抑止
    env["APP_USE_RENDER_QUEUE"] = "0"

    print(f"[render-queue] ▶ job#{job_id} {stage} vol.{job['vol']} "
          f"[{job.get('channel_name','?')}]", flush=True)
    try:
        proc = subprocess.run(cmd, env=env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode == 0:
            _rq.mark_done(job_id)
            print(f"[render-queue] ✅ job#{job_id} done", flush=True)
        else:
            tail = (proc.stdout or b"").decode(errors="replace")[-300:]
            _rq.mark_error(job_id, f"exit={proc.returncode} {tail}")
            _send_line_notify(
                f"❌ render queue: [{job.get('channel_name','?')}] "
                f"vol.{job['vol']} {stage} 失敗 (exit={proc.returncode})"
            )
    except subprocess.TimeoutExpired:
        _rq.mark_error(job_id, f"timeout {timeout}s")
        _send_line_notify(
            f"⏰ render queue: [{job.get('channel_name','?')}] "
            f"vol.{job['vol']} {stage} タイムアウト ({timeout}s)"
        )
    except Exception as e:
        _rq.mark_error(job_id, f"exception: {e}")
        _send_line_notify(
            f"❌ render queue: [{job.get('channel_name','?')}] "
            f"vol.{job['vol']} {stage} 例外: {str(e)[:80]}"
        )


def _render_queue_worker_loop() -> None:
    """daemon thread。pending を 1 件ずつ pick → 実行 → mark を繰り返す。"""
    import app_render_queue as _rq
    print("[render-queue] worker started", flush=True)
    while True:
        try:
            job = _rq.claim_next()
            if not job:
                time.sleep(3)
                continue
            _execute_render_job(job)
        except Exception as e:
            print(f"[render-queue] worker exception: {e}", flush=True)
            time.sleep(5)


@app.on_event("startup")
async def _validate_channel_registry():
    """P2-2: dashboard_config.channel_folder が channels.json に登録されているか検証。

    canonical source は channels.json。dashboard_config の channel_folder/channel_name は
    あくまで「UI のアクティブビュー」のポインタなので、registry に無い folder を
    指していたら警告（自動修正はしない、運営者の判断を待つ）。"""
    try:
        # PC 間共有: 共有ドライブ上の registry を旧ローカル版＋YT フォルダスキャンで同期・自己修復
        try:
            sync_channel_registry(verbose=True)
        except Exception as e:
            print(f"[registry] sync 失敗（続行）: {e}")
        # PC 間共有: ベンチマークのプロファイル/設定/分析を共有ドライブへ移行（thumbs 画像は除く）
        try:
            migrate_benchmark_to_shared(verbose=True)
        except Exception as e:
            print(f"[benchmark] 移行呼び出し失敗（続行）: {e}")
        chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
        if not chs:
            print("[registry] channels.json が空。基本設定 > チャンネル管理から追加してください。")
            return
        cfg = load_json(DASHBOARD_CONFIG, {}) or {}
        active_folder = cfg.get("channel_folder") or ""
        active_name = cfg.get("channel_name") or ""
        if not active_folder:
            print(f"[registry] dashboard_config に channel_folder 未設定（{len(chs)} 件登録あり）")
            return
        # registry のいずれかと一致するか
        target = _norm_folder(active_folder)
        match = None
        for ch in chs:
            if _norm_folder(ch.get("folder") or "") == target:
                match = ch
                break
        if match:
            print(f"[registry] active channel: {match.get('name')!r} (id={match.get('id')!r}) ✓")
        else:
            print(f"[registry] ⚠ dashboard_config.channel_folder が registry に未登録: {active_folder}")
            print(f"[registry] ⚠ active_name={active_name!r} → 'UI 内表示は許可するが job 実行時は --channel-id 推奨'")
    except Exception as e:
        print(f"[registry] 検証失敗: {e}")


@app.on_event("startup")
async def _init_run_ledger():
    """P3-1: run ledger の DB 初期化 + stale 回収。"""
    try:
        import app_run_ledger as _ledger
        _ledger.init_db()
        reaped = _ledger.reap_stale()
        if reaped:
            print(f"[ledger] reaped {reaped} stale in_progress runs at startup")
        print(f"[ledger] initialized at {_ledger.DB_PATH}")
    except Exception as e:
        print(f"[ledger] 起動失敗: {e}")


@app.on_event("startup")
async def _start_render_queue_worker():
    """render queue の DB 初期化 + stale 回収 + worker thread 起動。"""
    global _rq_worker_started
    if _rq_worker_started:
        return
    if os.environ.get("APP_RENDER_QUEUE_DISABLE", "").strip() in ("1", "true", "yes"):
        print("[render-queue] disabled by APP_RENDER_QUEUE_DISABLE")
        return
    try:
        import app_render_queue as _rq
        _rq.init_db()
        reaped = _rq.reap_stale_running()
        if reaped:
            print(f"[render-queue] reaped {reaped} stale running jobs at startup")
        t = threading.Thread(target=_render_queue_worker_loop, daemon=True,
                             name="render-queue-worker")
        t.start()
        _rq_worker_started = True
    except Exception as e:
        print(f"[render-queue] 起動失敗: {e}")


# ─── API: render queue（P2-1） ───────────────────
# 6+ チャンネル並列運用時に Premiere/Export がシリアライズされる様子を可視化する。

class RenderQueueEnqueueRequest(BaseModel):
    channel_folder: str
    channel_name: Optional[str] = ""
    vol: int
    video_name: Optional[str] = ""
    stage: str  # "premiere" | "export"
    parent_run_id: Optional[str] = ""

@app.get("/api/render-queue")
def api_render_queue_list(status: Optional[str] = None, limit: int = 50):
    """直近のジョブを返す（status フィルタ可: pending / running / done / error / cancelled）。"""
    import app_render_queue as _rq
    jobs = _rq.list_jobs(status=status, limit=int(limit))
    return {
        "status": "ok",
        "jobs": jobs,
        "counts": {
            "pending": len(_rq.list_jobs("pending", limit=999)),
            "running": len(_rq.list_jobs("running", limit=999)),
        },
    }

@app.get("/api/render-queue/throughput")
def api_render_queue_throughput(days: int = 7):
    """直近 N 日のスループット統計（運用ガイドの「物理的に何チャンネルまで」根拠）。"""
    import app_render_queue as _rq
    return _rq.stats(window_days=int(days))

@app.post("/api/render-queue/enqueue")
def api_render_queue_enqueue(req: RenderQueueEnqueueRequest):
    """pipeline 以外（CLI / 手動）からも投入可能にする内部 API。"""
    import app_render_queue as _rq
    if req.stage not in _rq.ALLOWED_STAGES:
        raise HTTPException(400, f"stage must be one of {_rq.ALLOWED_STAGES}")
    jid = _rq.enqueue(
        channel_folder=req.channel_folder,
        channel_name=req.channel_name or "",
        vol=int(req.vol),
        video_name=req.video_name or "",
        stage=req.stage,
        parent_run_id=req.parent_run_id or "",
    )
    return {"status": "ok", "id": jid}

@app.post("/api/render-queue/{job_id}/cancel")
def api_render_queue_cancel(job_id: int):
    import app_render_queue as _rq
    ok = _rq.cancel(int(job_id))
    if not ok:
        raise HTTPException(409, "pending 状態のジョブだけがキャンセル可能です")
    return {"status": "ok"}

@app.post("/api/render-queue/reap")
def api_render_queue_reap():
    """stale running を error に降格（手動メンテ用）。"""
    import app_render_queue as _rq
    n = _rq.reap_stale_running()
    return {"status": "ok", "reaped": n}


# ─── API: run ledger（P3-1） ─────────────────────

@app.get("/api/runs/ledger")
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


@app.get("/api/runs/ledger/stats")
def api_runs_ledger_stats(days: int = 7):
    """成功率 / 平均所要 / 自動再投入率の集計。運用 SLO 確認用。"""
    import app_run_ledger as _ledger
    return _ledger.stats(window_days=int(days))


@app.get("/api/runs/ledger/{run_id}")
def api_runs_ledger_get(run_id: str):
    import app_run_ledger as _ledger
    r = _ledger.get_run(run_id)
    if not r:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"status": "ok", "run": r}


@app.get("/api/runs/ledger/{run_id}/chain")
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

@app.post("/api/runs/ledger/migrate")
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


@app.post("/api/runs/ledger/reap")
def api_runs_ledger_reap():
    """stale in_progress を failed に降格（手動メンテ用）。"""
    import app_run_ledger as _ledger
    n = _ledger.reap_stale()
    return {"status": "ok", "reaped": n}


# ─── API: token health（P3-5） ───────────────────

@app.get("/api/token-health")
def api_token_health():
    """全チャンネル + Playwright のトークン健全性を返す。

    UI ダッシュボードで表示する用。warning が無い場合 `warnings: []` で返る。"""
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    try:
        from app_token_health import check_all
        return check_all()
    except Exception as e:
        raise HTTPException(500, f"token health check failed: {e}")


@app.post("/api/token-health/notify")
async def api_token_health_notify():
    """手動でチェックを実行し、warn があれば Discord に通知（cron 動作の即時確認用）。"""
    job = {"id": f"token_health_manual_{int(time.time())}"}
    await _job_token_health(job)
    return {"status": "ok", "job_id": job["id"]}


# ─── API: YouTube 公開ゲート（P2-7） ───────────────
# private upload 後、N 時間経過してから自動 public 化する後段ジョブを動的登録。

class SchedulePublishRequest(BaseModel):
    video_name: str
    channel_folder: Optional[str] = None  # 未指定なら active channel
    channel_name: Optional[str] = None
    delay_hours: float

@app.post("/api/youtube/schedule-publish")
def api_youtube_schedule_publish(req: SchedulePublishRequest):
    """指定 vol を delay_hours 経過後に public 化するジョブを APScheduler に登録。"""
    if _scheduler is None:
        raise HTTPException(503, "scheduler 未起動")
    if req.delay_hours < 0:
        raise HTTPException(400, "delay_hours は 0 以上")
    ch_folder = req.channel_folder
    ch_name = req.channel_name
    if not ch_folder:
        cfg = get_dashboard_config()
        ch_folder = cfg.get("channel_folder") or ""
        ch_name = ch_name or cfg.get("channel_name") or ""
    if not ch_folder:
        raise HTTPException(400, "channel_folder が解決できません")
    folder = Path(ch_folder) / req.video_name
    if not folder.exists():
        raise HTTPException(404, f"video folder not found: {folder}")
    import datetime as _dt
    run_at = _dt.datetime.now() + _dt.timedelta(hours=req.delay_hours)
    try:
        job_id = _schedule_publish(
            video_name=req.video_name, channel_folder=ch_folder,
            channel_name=ch_name or "", run_at=run_at,
        )
    except Exception as e:
        raise HTTPException(500, f"register failed: {e}")
    # marker に scheduled_publish_at を追記（再起動時の復旧用）
    try:
        marker = folder / "youtube_upload.json"
        if marker.exists():
            d = json.loads(marker.read_text(encoding="utf-8"))
            d["scheduled_publish_at"] = run_at.isoformat()
            marker.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[publish] marker 更新失敗（無視）: {e}")
    return {"status": "ok", "job_id": job_id, "scheduled_at": run_at.isoformat()}


@app.post("/api/youtube/publish-now/{video_name}")
def api_youtube_publish_now(video_name: str):
    """指定 vol を**いま即座に** public 化（運営者の手動操作用）。"""
    cfg = get_dashboard_config()
    ch_folder = cfg.get("channel_folder") or ""
    if not ch_folder:
        raise HTTPException(400, "active channel が無い")
    folder = Path(ch_folder) / video_name
    if not folder.exists():
        raise HTTPException(404, f"video folder not found: {folder}")
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    from app_youtube import publish_video_to_public
    result = publish_video_to_public(folder)
    if result.get("status") in ("ok", "already_public"):
        return {"status": "ok", **result}
    raise HTTPException(500, f"publish failed: {result}")


class UpdateSnippetRequest(BaseModel):
    apply_localizations: bool = True


@app.post("/api/youtube/update-snippet/{video_name}")
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
    folder = Path(ch_folder) / video_name
    if not folder.exists():
        raise HTTPException(404, f"video folder not found: {folder}")
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    from app_youtube import update_video_snippet
    result = update_video_snippet(folder, apply_localizations=req.apply_localizations)
    status_code_map = {"ok": 200, "missing": 404, "retryable": 503, "error": 500}
    sc = status_code_map.get(result.get("status", "error"), 500)
    if sc != 200:
        raise HTTPException(sc, f"update-snippet failed: {result}")
    return {"status": "ok", **result}


@app.on_event("startup")
async def _recover_scheduled_publishes():
    """サーバ再起動時に、未公開の `scheduled_publish_at` を持つ vol を再登録。

    完全自走運用で「app.py を再起動 → 公開ジョブが消える」事故を防ぐ。
    過去日付の場合は now+1min で即時投入。"""
    try:
        chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    except Exception:
        chs = []
    if not chs or _scheduler is None:
        return
    import datetime as _dt
    n_recovered = 0
    for ch in chs:
        ch_folder_str = ch.get("folder") or ""
        if not ch_folder_str:
            continue
        ch_folder = Path(ch_folder_str)
        if not ch_folder.exists():
            continue
        for vol_dir in ch_folder.iterdir():
            if not vol_dir.is_dir():
                continue
            marker = vol_dir / "youtube_upload.json"
            if not marker.exists():
                continue
            try:
                d = json.loads(marker.read_text(encoding="utf-8"))
            except Exception:
                continue
            sched = d.get("scheduled_publish_at")
            already = d.get("published_at") or (d.get("privacy") == "public")
            if not sched or already:
                continue
            try:
                run_at = _dt.datetime.fromisoformat(sched)
            except Exception:
                continue
            # 過去日付なら 1 分後に即時投入
            if run_at < _dt.datetime.now():
                run_at = _dt.datetime.now() + _dt.timedelta(minutes=1)
            try:
                _schedule_publish(
                    video_name=vol_dir.name,
                    channel_folder=str(ch_folder),
                    channel_name=ch.get("name", ""),
                    run_at=run_at,
                )
                n_recovered += 1
            except Exception as e:
                print(f"[publish] 復旧失敗 {vol_dir.name}: {e}")
    if n_recovered:
        print(f"[publish] {n_recovered} 件の公開ゲートを再登録（再起動復旧）")


# ─── API: スケジュール ───

@app.get("/api/schedule/jobs")
def api_get_schedule_jobs(channel_id: Optional[str] = None):
    """登録ジョブを返す。

    P2-3: `?channel_id=<id>` で channel 別にフィルタ。
    `channel_id` が空文字 → アクティブチャンネル（dashboard_config）にフォールバック解釈はしない。
    特殊値 `__none__` → channel_id 未指定のジョブ（全チャンネル共通）のみを返す。

    各ジョブには channel_name（registry 由来の解決名）を補完する。"""
    jobs = _scheduler_get_jobs()
    # APScheduler から次回実行時刻を取得
    if _scheduler:
        next_runs = {j.id: (j.next_run_time.isoformat() if j.next_run_time else None)
                     for j in _scheduler.get_jobs()}
        for job in jobs:
            job["next_run"] = next_runs.get(job.get("id"), None)
    # チャンネル名を補完（registry 経由で id → name）
    chs = get_channels()
    name_by_id = {c.get("id"): c.get("name") for c in chs}
    for job in jobs:
        cid = job.get("channel_id") or ""
        job["channel_name"] = name_by_id.get(cid, "") if cid else ""
    # フィルタ
    if channel_id is not None:
        if channel_id == "__none__":
            jobs = [j for j in jobs if not j.get("channel_id")]
        else:
            jobs = [j for j in jobs if j.get("channel_id") == channel_id]
    return {
        "jobs": jobs,
        "scheduler_active": _scheduler is not None,
        "channels": [{"id": c.get("id"), "name": c.get("name")} for c in chs],
    }

class ScheduleJobUpsert(BaseModel):
    id: Optional[str] = None
    type: str  # vol_create | benchmark_refresh | export_window | spot_create
    name: str = ""
    enabled: bool = True
    trigger: dict  # {kind:"cron", day_of_week:"mon,fri", hour:9, minute:0} or {kind:"date", run_date:"..."} or {kind:"cron", expr:"0 7 * * *"}
    channel_id: Optional[str] = None
    vol: Optional[int] = None     # spot_create のみ
    action: Optional[str] = None  # export_window: "on" | "off"
    balance_slots: Optional[bool] = True  # P2-4: 同時刻の vol_create/spot_create を 30 分単位で自動分散
    # P2-6: auto_resume — 失敗 stage を検出し N 分後に同 vol を `--from <stage>` で再投入
    auto_resume: Optional[bool] = False
    auto_resume_delay_min: Optional[int] = 30
    auto_resume_max_attempts: Optional[int] = 3

@app.post("/api/schedule/jobs")
def api_upsert_schedule_job(req: ScheduleJobUpsert):
    if req.type not in JOB_HANDLERS:
        raise HTTPException(400, f"未知の type: {req.type}")
    jobs = _scheduler_get_jobs()
    job = req.dict(exclude_none=True)
    job.pop("balance_slots", None)  # 永続化はしない（リクエストごとの挙動制御フラグ）
    if not job.get("id"):
        job["id"] = f"{req.type}_{int(time.time())}_{secrets.token_hex(3)}"
    # P2-4: 衝突する slot は 30 分単位で自動的に後ろへずらす
    shift_min = 0
    requested_trigger = job.get("trigger") or {}
    if (req.balance_slots is None or req.balance_slots) and req.type in SLOT_BALANCED_TYPES:
        balanced, shift_min, shift_count = _balance_trigger_slot(job, jobs)
        if shift_min > 0:
            job["trigger"] = balanced
            print(f"[scheduler] slot conflict: {job.get('id')} を {shift_min} 分後ろにずらしました "
                  f"({shift_count} スロット分)")
    # 既存上書き or 追加
    found = False
    for i, j in enumerate(jobs):
        if j.get("id") == job["id"]:
            jobs[i] = job; found = True; break
    if not found:
        jobs.append(job)
    _scheduler_save_jobs(jobs)
    _scheduler_reload()
    return {
        "status": "ok",
        "job": job,
        "slot_shifted_minutes": shift_min,
        "slot_requested_trigger": requested_trigger if shift_min > 0 else None,
    }

@app.delete("/api/schedule/jobs/{job_id}")
def api_delete_schedule_job(job_id: str):
    jobs = [j for j in _scheduler_get_jobs() if j.get("id") != job_id]
    _scheduler_save_jobs(jobs)
    _scheduler_reload()
    return {"status": "ok"}

@app.post("/api/schedule/run-now/{job_id}")
async def api_schedule_run_now(job_id: str):
    job = next((j for j in _scheduler_get_jobs() if j.get("id") == job_id), None)
    if not job:
        raise HTTPException(404, "job が見つかりません")
    handler = JOB_HANDLERS.get(job.get("type"))
    if not handler:
        raise HTTPException(400, f"未知の type: {job.get('type')}")
    asyncio.create_task(handler(job))
    return {"status": "started", "job_id": job_id}

@app.get("/api/schedule/history")
def api_schedule_history():
    return {"history": list(reversed(_scheduler_history))[:50]}


# ─── U3: 自走運用パネル（読み取り + 設定保存のみ） ─────────────────────
# ⚠ 重要な安全境界: これらの API は orchestrator.evaluate(dry_run=True) など
#   **副作用ゼロ**の呼び出しと per-channel 設定保存だけを行う。
#   tick()/dispatch() は呼ばない。_scheduler.add_job も一切しない。
#   = 無人稼働（autopilot の実起動）は依然として未実装。autopilot_enabled は
#   設定値を保存するだけで、実際の定期実行は別途 GO 後に app.py へ統合する。

def _build_orchestrator_channels():
    """get_channels() から orchestrator.evaluate 用の channels を構築。
    各 vol dict に folder(絶対パス)を必ず付与（evaluate が Path() で使うため）。
    channels.json の "id" を "channel_id" に詰め替える（/api/runs/active と同形）。"""
    chans = []
    for ch in get_channels():
        folder = ch.get("folder") or ""
        ch_dir = Path(folder)
        if not folder or not ch_dir.exists():
            continue
        vols = []
        try:
            entries = [d for d in ch_dir.iterdir()
                       if d.is_dir() and parse_video_folder_name(d.name)]
        except Exception:
            entries = []
        def _vk(p):
            info = parse_video_folder_name(p.name)
            return info["num"] if info else -1
        entries.sort(key=_vk, reverse=True)
        for d in entries[:10]:
            info = parse_video_folder_name(d.name)
            if not info:
                continue
            vols.append({"vol": info["num"], "name": d.name, "folder": str(d)})
        # ⚠ autopilot_enabled / priority は per-ch `.app_channel_config.json` が真実源
        # （_save_channel_scalar の保存先。channels.json には無い）。ここを channels.json
        # から読むと autopilot トグルが tick に伝わらず永久 dormant になる（D1 整合性）。
        cc = load_json(ch_dir / _CHANNEL_CONFIG_FILENAME, {})
        _prio_raw = cc.get("priority", ch.get("priority", 100))
        try:
            priority = int(_prio_raw) if str(_prio_raw).strip() != "" else 100
        except (TypeError, ValueError):
            priority = 100
        chans.append({
            "channel_id": ch.get("id", ""),
            "channel_name": ch.get("name", ""),
            "folder": folder,
            "priority": priority,
            "autopilot_enabled": bool(cc.get("autopilot_enabled", ch.get("autopilot_enabled", False))),
            "vols": vols,
        })
    return chans


@app.get("/api/workers/status")
def api_workers_status():
    """自走運用の dry-run 可視化: いま各 vol で着手可能な stage 候補 + チャンネル健全性。
    ⚠ evaluate(dry_run=True) のみ。run()/dispatch()/tick() は呼ばない（副作用ゼロ）。"""
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    try:
        import app_orchestrator as _orch
    except Exception as e:
        raise HTTPException(500, f"orchestrator import 失敗: {e}")
    channels = _build_orchestrator_channels()
    try:
        cands = _orch.evaluate(channels, dry_run=True)
    except Exception as e:
        raise HTTPException(500, f"evaluate 失敗: {e}")
    cand_list = [{"domain": c.domain, "vol": c.vol, "folder": c.folder,
                  "channel_id": c.channel_id} for c in cands]
    # チャンネル別の健全性 + autopilot 設定
    ch_health = []
    for ch in channels:
        cid = ch.get("channel_id", "")
        try:
            fails = _orch.consecutive_failures(cid)
            tripped = _orch.is_channel_tripped(cid)
        except Exception:
            fails, tripped = 0, False
        ch_health.append({
            "channel_id": cid, "channel_name": ch.get("channel_name", ""),
            "priority": ch.get("priority", 100),
            "autopilot_enabled": ch.get("autopilot_enabled", False),
            "consecutive_failures": fails, "tripped": tripped,
            "candidate_count": sum(1 for c in cand_list if c["channel_id"] == cid),
        })
    return {
        "status": "ok",
        "candidates": cand_list,
        "channels": ch_health,
        "breaker_threshold": getattr(_orch, "BREAKER_THRESHOLD", 3),
        "autopilot_default_stages": getattr(_orch, "AUTOPILOT_DEFAULT_STAGES", []),
        # D1: tick 登録済みか（登録されていても autopilot ON channel が無ければ dormant）
        "scheduler_registered": bool(_scheduler is not None and any(
            getattr(j, "id", "") == "orchestrator_tick" for j in _scheduler.get_jobs())),
        "autopilot_enabled_channels": sum(1 for ch in channels if ch.get("autopilot_enabled")),
    }


@app.get("/api/quota/status")
def api_quota_status():
    """各チャンネルの YouTube quota 残（policy 配分の可視化用）。読み取りのみ。"""
    import app_youtube as _ytm
    out = []
    for ch in get_channels():
        folder = ch.get("folder") or ""
        ch_dir = Path(folder)
        if not folder or not ch_dir.exists():
            continue
        try:
            used = _ytm.quota_used_in_window(ch_dir)
            cap = _ytm.DEFAULT_DAILY_QUOTA_CAP
            out.append({
                "channel_id": ch.get("id", ""), "channel_name": ch.get("name", ""),
                "used": used, "cap": cap, "remaining": max(0, cap - used),
                "per_upload": _ytm.QUOTA_PER_UPLOAD,
            })
        except Exception:
            continue
    return {"status": "ok", "channels": out}


def _save_channel_scalar(channel_id: str, updates: dict):
    """指定 channel_id の .app_channel_config.json に updates をマージ保存。
    channels.json から folder を解決（active 限定でない per-channel 保存）。"""
    chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    target = next((c for c in chs if c.get("id") == channel_id), None)
    if not target:
        raise HTTPException(404, f"channel not found: {channel_id}")
    folder = target.get("folder") or ""
    if not folder or not Path(folder).exists():
        raise HTTPException(400, f"channel folder not found: {folder}")
    p = Path(folder) / _CHANNEL_CONFIG_FILENAME
    d = load_json(p, {})
    d.update(updates)
    save_json(p, d)
    return {"status": "ok", "channel_id": channel_id, "saved": updates}


class ChannelPriorityRequest(BaseModel):
    channel_id: str
    priority: int


@app.put("/api/config/priority")
def api_set_channel_priority(req: ChannelPriorityRequest):
    """channel 優先度を per-channel 保存（orchestrator policy 配分が参照）。"""
    return _save_channel_scalar(req.channel_id, {"priority": int(req.priority)})


class AutopilotRequest(BaseModel):
    channel_id: str
    enabled: bool


@app.get("/api/workers/autopilot")
def api_get_autopilot():
    """各チャンネルの autopilot_enabled 設定を返す（保存値の読み取りのみ）。"""
    out = []
    for ch in get_channels():
        folder = ch.get("folder") or ""
        cc = {}
        if folder:
            cc = load_json(Path(folder) / _CHANNEL_CONFIG_FILENAME, {})
        out.append({
            "channel_id": ch.get("id", ""), "channel_name": ch.get("name", ""),
            "autopilot_enabled": bool(cc.get("autopilot_enabled", False)),
            "priority": int(cc.get("priority", 100)) if str(cc.get("priority", "")).strip() else 100,
        })
    return {"status": "ok", "channels": out,
            # D1 配線済: tick 登録後は autopilot ON にした channel が実際に無人稼働する
            "scheduler_registered": bool(_scheduler is not None and any(
                getattr(j, "id", "") == "orchestrator_tick" for j in _scheduler.get_jobs()))}


@app.put("/api/workers/autopilot")
def api_set_autopilot(req: AutopilotRequest):
    """autopilot ON/OFF を per-channel 保存。
    ⚠ これは設定値の保存のみ。実際の無人稼働（tick の定期実行）は未実装で、
       別途 app.py への orchestrator 統合（GO 後）が必要。ここでは scheduler に
       一切触れない（add_job しない）。"""
    return _save_channel_scalar(req.channel_id, {"autopilot_enabled": bool(req.enabled)})


def _run_uvicorn():
    """pyproject.toml の `automation-studio` コマンドエントリポイント。
    `python3 Python/app.py` 直接起動でも同関数を呼ぶ。
    """
    import uvicorn
    print(f"共有ドライブ: {SHARED_BASE}")
    print(f"Web: {WEB_DIR}")
    print(f"Config: {CONFIG_DIR}")
    # ポートは 8888 固定（フォールバックの 8889/8890... 揺れを止める）。
    # 8888 が使用中の場合は start.sh が起動前に kill する責務。
    # APP_PORT / ORZZ_PORT env で明示指定された場合のみ上書き可能。
    port_env = (os.environ.get("APP_PORT") or os.environ.get("ORZZ_PORT"))
    port = int(port_env) if port_env else 8888
    host = (os.environ.get("APP_HOST") or os.environ.get("ORZZ_HOST") or "127.0.0.1").strip()
    public_hosts = {".".join(("0", "0", "0", "0")), ":" * 2}
    if host in public_hosts and not AUTH_REQUIRED:
        print("⚠️  外部公開ホストで認証が無効です。APP_AUTH_REQUIRED=1 の併用を推奨します。")
    print(f"起動: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)  # nosec B104


if __name__ == "__main__":
    _run_uvicorn()

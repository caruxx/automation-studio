#!/usr/bin/env python3
"""タグ・コンセプト・多言語・mp4 情報・メタ充足・非表示/復元

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

# ─── API: タグ ───

class VideoTagsUpdate(BaseModel):
    tags: List[str]

@router.get("/api/videos/{video_name}/tags")
def api_get_video_tags(video_name: str):
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    tags_file = folder / "youtube_tags.txt"
    if not tags_file.exists():
        return {"tags": []}
    lines = tags_file.read_text(encoding="utf-8").splitlines()
    return {"tags": [line.strip() for line in lines if line.strip()]}

@router.put("/api/videos/{video_name}/tags")
def api_update_video_tags(video_name: str, req: VideoTagsUpdate):
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    if not folder.exists():
        raise HTTPException(404)
    tags_file = folder / "youtube_tags.txt"
    tags_file.write_text("\n".join([t.strip() for t in req.tags if t.strip()]), encoding="utf-8")
    return {"status": "ok", "count": len([t for t in req.tags if t.strip()])}

# ─── API: コンセプト（端的な日本語1行） ───

class VideoConceptUpdate(BaseModel):
    concept: str

@router.get("/api/videos/{video_name}/concept")
def api_get_video_concept(video_name: str):
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    f = folder / "concept.txt"
    return {"concept": f.read_text(encoding="utf-8").strip() if f.exists() else ""}

@router.put("/api/videos/{video_name}/concept")
def api_update_video_concept(video_name: str, req: VideoConceptUpdate):
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    text = (req.concept or "").strip()
    f = folder / "concept.txt"
    if text:
        f.write_text(text, encoding="utf-8")
    elif f.exists():
        f.unlink()
    return {"status": "ok", "concept": text}

@router.post("/api/videos/{video_name}/generate-concept")
def api_generate_video_concept(video_name: str):
    """Claude CLI でその動画の端的な日本語コンセプトを生成して concept.txt に保存。"""
    from claude_proposer import propose_concept, gather_context
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
- Preserve the entire Tracklist section verbatim, including timestamps and English track titles. Do not translate, reorder, summarize, or rename any Tracklist line.
- Title within ~80 chars.
- zh-Hans = Simplified Chinese, zh-Hant = Traditional Chinese.
- es = Spain Spanish, es-419 = Latin America Spanish.
- pt-BR = Brazilian Portuguese.
- In description, escape newlines as \\n inside JSON strings (literal \\n, NOT raw line breaks).

OUTPUT (JSON only, no markdown fence, no explanation, all values single-line with \\n escapes):
{{{keys_block}}}"""


@router.post("/api/videos/{video_name}/generate-localizations")
def api_generate_localizations(video_name: str, req: GenerateLocalizationsRequest):
    """Claude CLI で 10 言語翻訳して youtube_localizations.json を生成。

    入力: <vol_folder>/youtube_title.txt + youtube_description.txt
    出力: <vol_folder>/youtube_localizations.json (上書き)
    使い所: app_youtube.py が upload 時に同 JSON を読んで snippet.localizations として送信
    """
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
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

@router.get("/api/videos/{video_name}/mp4-info")
def api_mp4_info(video_name: str):
    """vol の mp4 をどこから検出したか、サイズ・解像度・尺・コーデックを返す。

    検出ソース判定（_find_exported_mp4 の探索順と整合）:
      - "manual_exported_video": <vol_folder>/manual_exported_video.json で紐づけ済
      - "vol_folder": <vol_folder>/*.mp4
      - "external_ssd_per_video": <export_path>/<video_name>/*.mp4
      - "external_ssd_flat": <export_path>/<prefix>_vol<num>*.mp4 (flat 配置)
    """
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
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

@router.get("/api/videos/{video_name}/meta-status")
def api_meta_status(video_name: str):
    """動画 vol の meta データ充足状況を返す（Web UI のチェックリスト用）。
    各項目の present / ok を見て、ready_for_upload で総合判定。
    """
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
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

@router.post("/api/videos/{video_name}/hide")
def api_video_hide(video_name: str):
    """動画を一覧から非表示にする（フォルダは維持、.hidden ファイルを作成）"""
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    if not folder.exists():
        raise HTTPException(404)
    (folder / ".hidden").write_text("", encoding="utf-8")
    return {"status": "ok", "hidden": True}

@router.post("/api/videos/{video_name}/unhide")
def api_video_unhide(video_name: str):
    """非表示を解除"""
    config = get_dashboard_config()
    folder = resolve_video_folder(video_name)
    if not folder.exists():
        raise HTTPException(404)
    hidden_file = folder / ".hidden"
    if hidden_file.exists():
        hidden_file.unlink()
    return {"status": "ok", "hidden": False}

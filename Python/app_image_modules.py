#!/usr/bin/env python3
"""Structured image prompt modules per channel.

画像生成プロンプトを固定セクションに分け、チャンネル別の名前付き
モジュールとして保存・合成する。既存の長文プロンプトは初回だけ
セクション分解して `.studio_learning/image_modules.json` に退避する。
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any

SECTIONS = ("composition", "subject", "background", "color_tone", "mood", "text_overlay", "style")
SECTION_LABELS = {
    "composition": "Composition",
    "subject": "Subject",
    "background": "Background",
    "color_tone": "Color and light",
    "mood": "Mood",
    "text_overlay": "Text overlay",
    "style": "Style and quality",
}
LEARNING_DIR = ".studio_learning"
MODULES_FILE = "image_modules.json"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def module_path(channel_folder: str | Path) -> Path:
    return Path(channel_folder).expanduser() / LEARNING_DIR / MODULES_FILE


def empty_sections() -> dict[str, str]:
    return {k: "" for k in SECTIONS}


_LABEL_MAP = {
    "camera/composition": "composition",
    "camera": "composition",
    "composition": "composition",
    "subject": "subject",
    "background/context": "background",
    "background": "background",
    "context": "background",
    "lighting": "color_tone",
    "color": "color_tone",
    "color and light": "color_tone",
    "style/rendering": "style",
    "style": "style",
    "style and quality": "style",
    "viewer resonance": "mood",
    "mood": "mood",
    "atmosphere": "mood",
    "constraints": "text_overlay",
    "reference handling": "text_overlay",
    "text overlay": "text_overlay",
    "avoid": "text_overlay",
}


def heuristic_split_prompt(prompt: str) -> dict[str, str]:
    """既存のラベル付き/長文プロンプトをセクションへ機械的に分解する。"""
    sections = empty_sections()
    text = (prompt or "").strip()
    if not text:
        return sections
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z][A-Za-z /_-]{1,40}):\s*(.*)$", line)
        if m:
            key = _LABEL_MAP.get(m.group(1).strip().lower())
            if key:
                current = key
                if m.group(2).strip():
                    sections[key] = _join(sections[key], m.group(2).strip())
                continue
        if current:
            sections[current] = _join(sections[current], line)
    if any(sections.values()):
        return sections

    # 完全な自然文の場合の保守的フォールバック。
    lowered = text.lower()
    if any(x in lowered for x in ("16:9", "wide", "composition", "negative space")):
        sections["composition"] = _extract_sentence(text, ("16:9", "composition", "negative space", "wide"))
    if any(x in lowered for x in ("subject", "scene", "primary", "generate")):
        sections["subject"] = _extract_sentence(text, ("subject", "scene", "primary", "generate")) or text[:260]
    sections["background"] = _extract_sentence(text, ("background", "environment", "cafe", "interior", "window"))
    sections["color_tone"] = _extract_sentence(text, ("light", "lighting", "color", "palette", "shadow", "warm", "cool"))
    sections["mood"] = _extract_sentence(text, ("mood", "atmospheric", "calm", "relax", "cozy", "cinematic"))
    sections["text_overlay"] = _extract_sentence(text, ("no text", "caption", "subtitle", "logo", "watermark", "readable"))
    sections["style"] = _extract_sentence(text, ("style", "quality", "photorealistic", "painterly", "gouache", "rendering"))
    if not sections["subject"]:
        sections["subject"] = text[:320]
    return sections


def _join(a: str, b: str) -> str:
    return (a + " " + b).strip() if a else b.strip()


def _extract_sentence(text: str, needles: tuple[str, ...]) -> str:
    parts = re.split(r"(?<=[.!?。])\s+", text)
    for s in parts:
        low = s.lower()
        if any(n in low for n in needles):
            return s.strip()[:420]
    return ""


def split_prompt_with_llm(prompt: str, *, cli_cmd: str = "claude") -> dict[str, str]:
    """LLM で分解を試み、失敗時は heuristic に戻す。"""
    text = (prompt or "").strip()
    if not text:
        return empty_sections()
    instruction = f"""Split this image-generation prompt into JSON sections.
Return ONLY one JSON object with these exact keys:
{", ".join(SECTIONS)}

Keep the original intent. Do not invent new creative direction. If a section is absent, use "".

Prompt:
{text[:6000]}
"""
    try:
        from app_llm_runner import run_llm
        out = run_llm(instruction, cli_cmd=cli_cmd, timeout=120, label="image-modules-migrate")
        obj = _extract_json(out)
        if isinstance(obj, dict):
            return {k: str(obj.get(k) or "").strip() for k in SECTIONS}
    except Exception:
        pass
    return heuristic_split_prompt(text)


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _module_id(section: str, name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", name or section).strip("-").lower()
    return f"{section}:{base or 'default'}"


def _module(section: str, name: str, text: str, *, source: str = "migrated") -> dict[str, Any]:
    return {
        "id": _module_id(section, name),
        "section": section,
        "name": name,
        "text": text or "",
        "source": source,
        "created_at": _now_iso(),
    }


def create_payload_from_prompt(prompt: str, *, channel_id: str = "", cli_cmd: str = "claude") -> dict[str, Any]:
    sections = split_prompt_with_llm(prompt, cli_cmd=cli_cmd)
    modules: dict[str, list[dict[str, Any]]] = {}
    selection: dict[str, str] = {}
    for sec in SECTIONS:
        mod = _module(sec, "legacy_default", sections.get(sec, ""))
        modules[sec] = [mod]
        selection[sec] = mod["id"]
    return {
        "schema_version": 1,
        "channel_id": channel_id,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "migrated_from_legacy": bool((prompt or "").strip()),
        "legacy_prompt_backup": prompt or "",
        "sections": list(SECTIONS),
        "modules": modules,
        "selection": selection,
        "overrides": {},
        "winning_modules": [],
    }


def ensure_modules(channel_folder: str | Path, *, legacy_prompt: str = "", channel_id: str = "",
                   cli_cmd: str = "claude") -> dict[str, Any]:
    path = module_path(channel_folder)
    data = _read_json(path, None)
    if isinstance(data, dict) and data.get("modules"):
        _normalize_payload(data)
        has_text = any(
            str(m.get("text") or "").strip()
            for rows in (data.get("modules") or {}).values()
            for m in (rows or [])
            if isinstance(m, dict)
        )
        if legacy_prompt and not has_text and not data.get("legacy_prompt_backup"):
            data = create_payload_from_prompt(legacy_prompt, channel_id=channel_id, cli_cmd=cli_cmd)
            _write_json(path, data)
        return data
    data = create_payload_from_prompt(legacy_prompt, channel_id=channel_id, cli_cmd=cli_cmd)
    _write_json(path, data)
    return data


def load_modules(channel_folder: str | Path) -> dict[str, Any]:
    data = _read_json(module_path(channel_folder), {})
    if not isinstance(data, dict):
        data = {}
    _normalize_payload(data)
    return data


def save_modules(channel_folder: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    _normalize_payload(payload)
    payload["updated_at"] = _now_iso()
    _write_json(module_path(channel_folder), payload)
    return payload


def _normalize_payload(payload: dict[str, Any]) -> None:
    payload.setdefault("schema_version", 1)
    payload.setdefault("sections", list(SECTIONS))
    payload.setdefault("modules", {})
    payload.setdefault("selection", {})
    payload.setdefault("overrides", {})
    payload.setdefault("winning_modules", [])
    for sec in SECTIONS:
        payload["modules"].setdefault(sec, [])


def selected_sections(payload: dict[str, Any], *, base_sections: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    _normalize_payload(payload)
    sections = {k: (base_sections or {}).get(k, "") for k in SECTIONS}
    selected_modules: dict[str, str] = {}
    for sec in SECTIONS:
        sid = (payload.get("selection") or {}).get(sec) or ""
        mods = (payload.get("modules") or {}).get(sec) or []
        found = next((m for m in mods if m.get("id") == sid), None)
        if found:
            sections[sec] = found.get("text") or sections.get(sec, "")
            selected_modules[sec] = found.get("id") or ""
    for sec, text in (payload.get("overrides") or {}).items():
        if sec in sections and str(text or "").strip():
            sections[sec] = str(text).strip()
    meta = {
        "schema_version": 1,
        "sections": sections,
        "module_ids": selected_modules,
        "overrides": {k: v for k, v in (payload.get("overrides") or {}).items() if k in SECTIONS and v},
    }
    return sections, meta


def compose_prompt(sections: dict[str, Any], *, extra_constraints: list[str] | None = None) -> str:
    lines = []
    for sec in SECTIONS:
        value = str((sections or {}).get(sec) or "").strip()
        if value:
            lines.append(f"{SECTION_LABELS[sec]}: {value}")
    if extra_constraints:
        lines.append("Constraints: " + "; ".join([x for x in extra_constraints if x]) + ".")
    return "\n".join(lines).strip()


def apply_modules_to_sections(channel_folder: str | Path | None, base_sections: dict[str, str],
                              *, legacy_prompt: str = "", channel_id: str = "") -> tuple[dict[str, str], dict[str, Any] | None]:
    if not channel_folder:
        return base_sections, None
    try:
        payload = ensure_modules(channel_folder, legacy_prompt=legacy_prompt, channel_id=channel_id)
        sections, meta = selected_sections(payload, base_sections=base_sections)
        meta["channel_folder"] = str(channel_folder)
        return sections, meta
    except Exception:
        return base_sections, None


def add_candidate_modules_from_ttp(channel_folder: str | Path, ttp: dict[str, Any]) -> dict[str, Any]:
    payload = load_modules(channel_folder)
    agg = (ttp or {}).get("aggregate") or {}
    spec = (ttp or {}).get("winning_format_spec") or {}
    added: list[str] = []
    comp = spec.get("title_formula") or spec.get("series_structure") or ""
    if comp:
        mod = _module("composition", "ttp_composition", str(comp), source="ttp")
        payload["modules"]["composition"] = _upsert_module(payload["modules"]["composition"], mod)
        added.append(mod["id"])
    tags = agg.get("frequent_tags") or []
    tag_text = ", ".join([str(t.get("tag") or "") for t in tags[:8] if isinstance(t, dict)])
    if tag_text:
        mod = _module("color_tone", "ttp_color_tone", f"Use proven channel mood cues and palette hints from tags: {tag_text}", source="ttp")
        payload["modules"]["color_tone"] = _upsert_module(payload["modules"]["color_tone"], mod)
        added.append(mod["id"])
    save_modules(channel_folder, payload)
    return {"status": "ok", "added": added, "payload": payload}


def _upsert_module(rows: list[dict[str, Any]], mod: dict[str, Any]) -> list[dict[str, Any]]:
    return [r for r in rows if r.get("id") != mod.get("id")] + [mod]


def record_winning_modules(channel_folder: str | Path, generation_meta: dict[str, Any],
                           *, video_id: str = "", title: str = "", ratio: float | None = None) -> dict[str, Any]:
    payload = load_modules(channel_folder)
    module_ids = (generation_meta or {}).get("module_ids") or {}
    if not module_ids:
        return {"status": "no_modules"}
    row = {
        "learned_at": _now_iso(),
        "video_id": video_id,
        "title": title,
        "ratio": ratio,
        "module_ids": module_ids,
        "sections": (generation_meta or {}).get("sections") or {},
    }
    wins = payload.get("winning_modules") or []
    key = (video_id, json.dumps(module_ids, sort_keys=True, ensure_ascii=False))
    if not any((w.get("video_id"), json.dumps(w.get("module_ids") or {}, sort_keys=True, ensure_ascii=False)) == key for w in wins):
        wins.append(row)
    payload["winning_modules"] = wins[-100:]
    save_modules(channel_folder, payload)
    return {"status": "ok", "winning_modules": len(payload["winning_modules"])}


def load_generation_meta_for_video(channel_folder: str | Path, video_id: str) -> dict[str, Any]:
    """48hレビュー用に upload記録の video_id から近い生成メタを探す。"""
    root = Path(channel_folder)
    for upload in root.glob("*/youtube_upload.json"):
        try:
            d = json.loads(upload.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(d.get("video_id") or "") != str(video_id or ""):
            continue
        folder = upload.parent
        candidates = [
            folder / "image_generation_meta.json",
            folder / "thumbnail_candidates" / "image_generation_meta.json",
            folder / "Image" / "image_generation_meta.json",
        ]
        for p in candidates:
            data = _read_json(p, {})
            if isinstance(data, dict) and (data.get("module_ids") or data.get("items")):
                return data
            if isinstance(data, list) and data:
                return {"items": data}
    return {}

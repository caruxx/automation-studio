#!/usr/bin/env python3
"""シリーズ画像案の生成・キャッシュ。

ベンチマーク分析（competitor_analysis_cache.json の visual_direction +
buzz_patterns + trend_shift）と、既存の動画フォルダ一覧（重複回避用）を
入力に、Claude CLI に「次に作るべき画像」のシーン案を JSON で返させる。

各提案は次の構造:
{
  "id": "tokyo_rainy_dusk_office",
  "scene_jp": "雨の夕暮れ、東京のオフィスから見える滲んだネオン",
  "scene_en": "Tokyo rainy dusk office with neon bleeding through wet glass",
  "image_prompt_en": "（Flow/Codex に渡す英語プロンプト本体）",
  "rationale_jp": "なぜこの案が次に効くか（ベンチ分析の根拠）",
  "tags_jp": ["都市", "雨", "夕暮れ", "深い集中"],
  "filename_slug": "tokyo_rainy_dusk_office"
}
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"

CACHE_FILE = CONFIG_DIR / "series_proposals.json"
BENCHMARK_CACHE_FILE = CONFIG_DIR / "competitor_analysis_cache.json"
DEFAULT_CLI = "claude"
DEFAULT_TIMEOUT = 240


def _extract_json_object(text: str) -> Optional[dict]:
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = candidate[start:end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def _slugify(text: str, max_len: int = 40) -> str:
    """英数 + アンダースコアの安全なフォルダ名に変換。"""
    s = re.sub(r"[^\w\s-]", "", text or "", flags=re.ASCII).strip().lower()
    s = re.sub(r"[\s-]+", "_", s) or "scene"
    return s[:max_len]


def load_benchmark_analysis() -> Optional[dict]:
    if not BENCHMARK_CACHE_FILE.exists():
        return None
    try:
        cache = json.loads(BENCHMARK_CACHE_FILE.read_text(encoding="utf-8"))
        a = cache.get("analysis")
        return a if isinstance(a, dict) else None
    except Exception:
        return None


def load_proposals_cache() -> dict:
    if not CACHE_FILE.exists():
        return {"proposals": [], "generated_at": "", "channel_name": ""}
    try:
        d = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {"proposals": []}
    except Exception:
        return {"proposals": []}


def save_proposals_cache(payload: dict) -> None:
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _summarize_existing(videos: list[dict], limit: int = 30) -> str:
    """既存動画一覧を Claude に渡す要約に変換。タイトル + フォルダ名のみ。"""
    lines = []
    for v in videos[:limit]:
        title = (v.get("title") or "").strip() or "(untitled)"
        name = v.get("name") or ""
        lines.append(f"  - {name}: {title}")
    return "\n".join(lines) or "(no existing videos)"


def propose_series(
    *,
    cli_cmd: str = DEFAULT_CLI,
    count: int = 8,
    persona: str = "",
    channel_name: str = "",
    existing_videos: Optional[list[dict]] = None,
    analysis: Optional[dict] = None,
) -> dict:
    """Claude CLI に「次に作るべき画像案 N 件」を JSON で返させる。"""
    if analysis is None:
        analysis = load_benchmark_analysis()
    if not analysis:
        raise RuntimeError("競合分析キャッシュが見つかりません。先に「競合データ取得 + 分析」を実行してください。")

    vd = analysis.get("visual_direction") or {}
    bp = analysis.get("buzz_patterns") or {}
    ts = analysis.get("trend_shift") or {}
    rec = analysis.get("recommendations") or {}

    existing_block = _summarize_existing(existing_videos or [])

    prompt = f"""You are an art director planning the NEXT batch of YouTube thumbnails / hero images for a BGM channel.
Generate {count} fresh image scene proposals that visually extend the channel's series — not duplicating what exists, while staying anchored to viewer-validated visual direction from competitor analysis.

## Output Language Rules (highest priority)
- scene_jp / rationale_jp / tags_jp / каждый日本語フィールドは「自然な日本語」
- image_prompt_en / scene_en は ネイティブ英語（Flow/Codex に直接渡る）
- filename_slug は英数小文字 + アンダースコア（30 字以内）

---

## Channel
Name: {channel_name or '(unspecified)'}
Persona: {persona or '(unspecified)'}

## Existing videos (avoid duplicating these scenes)
{existing_block}

## Visual Direction (from benchmark analysis)
- color_palette: {json.dumps(vd.get('color_palette', []), ensure_ascii=False)}
- time_of_day: {vd.get('time_of_day', '')}
- subjects: {json.dumps(vd.get('subjects', []), ensure_ascii=False)}
- composition: {vd.get('composition', '')}
- atmosphere: {vd.get('atmosphere', '')}
- avoid: {json.dumps(vd.get('avoid', []), ensure_ascii=False)}

## Viewer Context (from buzz patterns)
- viewer_needs: {json.dumps(bp.get('viewer_needs', []), ensure_ascii=False)}
- title_patterns: {json.dumps(bp.get('title_patterns', []), ensure_ascii=False)}
- trend_shift: {ts.get('from_buzz_to_recent', '')}
- underserved_niches: {json.dumps(ts.get('underserved_niches', []), ensure_ascii=False)}
- title_tips: {json.dumps(rec.get('title_tips', []), ensure_ascii=False)}

## Your task
Propose {count} distinct, NEW image scenes — each one is a candidate for ONE future video's hero/thumbnail.

Design rules:
- Treat the {count} scenes as a SERIES with shared visual DNA (palette, atmosphere, framing) but varied (city / time-of-day / weather / vantage / subject).
- Each scene must directly serve a viewer need or underserved niche identified above.
- image_prompt_en MUST be 1〜3 sentences, photographic / cinematic style, include:
  - the city or place
  - the time of day / weather
  - composition cue (wide framing, shallow DoF, low-key lighting, etc.)
  - color palette anchor
  - what to AVOID (lift from analysis.avoid)
- rationale_jp は 1〜2 文で「なぜこのシーンが次に効くか」を分析根拠ごと簡潔に。
- 既存動画一覧と被らないシーンを選ぶこと（同じ city + time-of-day の組み合わせは避ける）。
- 同じ提案セット内でも city / time / weather のいずれかは必ず変える。

Respond with a SINGLE JSON object (no markdown fences):
{{
  "proposals": [
    {{
      "id": "tokyo_rainy_dusk_office",
      "scene_jp": "雨の夕暮れ、東京のオフィスから見える滲んだネオン",
      "scene_en": "Tokyo rainy dusk office with neon bleeding through wet glass",
      "image_prompt_en": "<Flow/Codex に直接渡す英語プロンプト>",
      "rationale_jp": "<なぜ次に効くかの説明>",
      "tags_jp": ["都市", "雨", "夕暮れ"],
      "filename_slug": "tokyo_rainy_dusk_office"
    }}
  ]
}}
"""

    print(f"🎨 Claude CLI でシリーズ案 {count} 件を生成中...")
    from app_llm_runner import run_llm
    out = run_llm(prompt, cli_cmd=cli_cmd, timeout=DEFAULT_TIMEOUT, label="series")

    obj = _extract_json_object(out)
    if not obj or "proposals" not in obj:
        raise RuntimeError(f"JSON 抽出失敗: {out[:300]}")

    proposals = obj.get("proposals") or []
    # サニタイズ + slug の重複解消 + id 補完
    seen_slugs: set[str] = set()
    cleaned: list[dict] = []
    for i, p in enumerate(proposals):
        if not isinstance(p, dict):
            continue
        scene_jp = str(p.get("scene_jp") or "").strip()
        scene_en = str(p.get("scene_en") or "").strip()
        if not scene_jp and not scene_en:
            continue
        slug_raw = str(p.get("filename_slug") or p.get("id") or scene_en or f"scene_{i+1}")
        slug = _slugify(slug_raw)
        # 重複回避
        base = slug
        n = 2
        while slug in seen_slugs:
            slug = f"{base}_{n}"
            n += 1
        seen_slugs.add(slug)
        cleaned.append({
            "id": slug,
            "scene_jp": scene_jp,
            "scene_en": scene_en,
            "image_prompt_en": str(p.get("image_prompt_en") or "").strip(),
            "rationale_jp": str(p.get("rationale_jp") or "").strip(),
            "tags_jp": [str(t).strip() for t in (p.get("tags_jp") or []) if str(t).strip()],
            "filename_slug": slug,
            "generated": False,  # 画像生成済みフラグ
            "output_dir": "",
        })
    print(f"  ✓ {len(cleaned)} 件の提案を取得")
    return {"proposals": cleaned}


def staging_dir(channel_folder: Path, slug: str) -> Path:
    """提案の画像保存先（チャンネル直下の _series_drafts/{slug}/Image/）。"""
    return Path(channel_folder) / "_series_drafts" / slug / "Image"

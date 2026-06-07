#!/usr/bin/env python3
"""Image generation prompt helpers for benchmark-driven thumbnails.

The prompt shape follows the current GPT Image guidance: keep the intent
skimmable, specify concrete visual details, and make constraints explicit.
"""

from __future__ import annotations

from typing import Any


def _clean_text(value: Any, max_len: int = 240) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = " ".join(text.split())
    return text[:max_len].rstrip()


def _list_text(value: Any, limit: int = 4) -> str:
    if isinstance(value, str):
        return _clean_text(value)
    if not isinstance(value, (list, tuple)):
        return ""
    items = [_clean_text(v, 80) for v in value if _clean_text(v, 80)]
    return ", ".join(items[:limit])


def _first_nonempty(*values: Any, max_len: int = 240) -> str:
    for value in values:
        text = _clean_text(value, max_len=max_len)
        if text:
            return text
    return ""


def normalize_visual_direction(analysis: dict | None = None,
                               thumbnail_axis: dict | None = None) -> dict:
    """Extract a stable visual brief from competitor and thumbnail analyses."""
    analysis = analysis or {}
    thumbnail_axis = thumbnail_axis or {}

    vd = analysis.get("visual_direction") or analysis.get("fused_visual_direction") or {}
    element = thumbnail_axis.get("element_extraction") or {}
    rec = thumbnail_axis.get("recommendation_for_self") or {}

    return {
        "subject": _first_nonempty(
            _list_text(vd.get("subjects")),
            _list_text(element.get("subjects")),
            _list_text(rec.get("keep")),
            "an evocative visual anchor for a BGM YouTube thumbnail",
        ),
        "background_context": _first_nonempty(
            thumbnail_axis.get("shared_composition"),
            element.get("composition"),
            rec.get("vibe_one_line"),
            vd.get("composition"),
        ),
        "lighting": _first_nonempty(
            element.get("lighting"),
            vd.get("time_of_day"),
            "soft cinematic light with natural shadows",
        ),
        "style": _first_nonempty(
            _list_text(thumbnail_axis.get("shared_palette")),
            _list_text(vd.get("color_palette")),
            _list_text(element.get("color_palette")),
            "photorealistic cinematic thumbnail",
        ),
        "camera_composition": _first_nonempty(
            vd.get("composition"),
            element.get("composition"),
            thumbnail_axis.get("shared_composition"),
            "16:9 landscape, clear focal point, thumbnail-grade readability",
        ),
        "atmosphere": _first_nonempty(
            vd.get("atmosphere"),
            element.get("atmosphere"),
            thumbnail_axis.get("common_atmosphere"),
        ),
        "viewer_hooks": _list_text(
            thumbnail_axis.get("viewer_hooks") or analysis.get("shared_hooks") or
            (analysis.get("buzz_patterns") or {}).get("viewer_needs"),
            limit=5,
        ),
        "avoid": _list_text(
            vd.get("avoid") or thumbnail_axis.get("avoid") or
            (thumbnail_axis.get("adaptation_hints") or {}).get("avoid"),
            limit=5,
        ),
        "transform": _list_text(
            rec.get("transform") or
            (thumbnail_axis.get("adaptation_hints") or {}).get("transform"),
            limit=5,
        ),
    }


def build_gpt_image2_prompt(
    *,
    concept: str = "",
    visual_direction: dict | None = None,
    context_hint: str = "",
    for_flow: bool = False,
    include_text_overlay: bool = False,
) -> str:
    """Build a production-oriented prompt for GPT Image 2 or Flow.

    The output intentionally uses labeled segments. GPT Image prompting
    guidance says structured templates are easier to maintain than clever
    syntax, and this app later sends the prompt through several command paths.
    """
    visual_direction = visual_direction or {}
    subject = _first_nonempty(
        concept,
        context_hint,
        visual_direction.get("subject"),
        "an evocative BGM thumbnail scene",
        max_len=320,
    )
    background = _first_nonempty(
        visual_direction.get("background_context"),
        visual_direction.get("atmosphere"),
        "a calm, immersive environment that promises relaxation and focus",
    )
    lighting = _first_nonempty(
        visual_direction.get("lighting"),
        "warm natural light, soft shadows, gentle contrast",
    )
    style = _first_nonempty(
        visual_direction.get("style"),
        "photorealistic cinematic image, rich materials, realistic color",
    )
    camera = _first_nonempty(
        visual_direction.get("camera_composition"),
        "16:9 landscape, eye-level cinematic framing, clear focal point",
    )

    constraints = [
        "16:9 landscape YouTube thumbnail",
        "no watermark",
        "no channel logo",
        "no copied brand assets",
    ]
    # 既定は写実を強制するが、visual_direction['style'] に独自画風(油彩/イラスト調 等)が
    # 指定された時は photorealistic を入れない（Style/rendering 行の独自画風を優先）。
    if not (visual_direction.get("style") or "").strip():
        constraints.insert(1, "photorealistic")
    if not include_text_overlay:
        constraints.append(
            "no text overlay, no captions, no subtitles, no readable text, "
            "no Japanese/Korean/Chinese characters, no lettering, no typography "
            "(zero readable text anywhere in the image)"
        )
    if for_flow:
        constraints.extend(["shallow depth of field", "thumbnail-grade clarity"])
    else:
        constraints.extend(["size target 1536x1024 or 2048x1152", "quality low for drafts, medium/high for finals"])

    hooks = _clean_text(visual_direction.get("viewer_hooks"), 220)
    avoid = _clean_text(visual_direction.get("avoid"), 220)
    transform = _clean_text(visual_direction.get("transform"), 220)

    parts = [
        f"Subject: {subject}",
        f"Background/context: {background}",
        f"Lighting: {lighting}",
        f"Style/rendering: {style}",
        f"Camera/composition: {camera}",
    ]
    if hooks:
        parts.append(f"Viewer resonance: {hooks}")
    if transform:
        parts.append(f"Benchmark translation: reinterpret these abstract elements, do not copy them: {transform}")
    if avoid:
        parts.append(f"Avoid: {avoid}")
    parts.append("Constraints: " + "; ".join(constraints) + ".")
    if not include_text_overlay:
        # 偽テキスト焼き込み対策: 参照画像内の文字を絶対に再現させない最終ダメ押し。
        # Subject/Background は _first_nonempty で 320 字切りされ directive 末尾が消えるため、
        # ここ（末尾・非トランケート）に置いて codex の参照分析経路へ確実に効かせる。
        parts.append(
            "Reference handling: IGNORE all text, captions, subtitles, letters, words and logos "
            "visible in any reference image; reuse ONLY their color palette, lighting, mood and "
            "composition. Render zero readable text in the output."
        )
    return "\n".join(parts)


# ─── 5要素ベンチマーク駆動プロンプト生成 ─────────────

_LIGHTING_VARIANTS = [
    "warm golden hour light, long soft shadows, gentle highlights",
    "blue hour twilight, cool ambient glow, faint warm rim light",
    "overcast diffused daylight, low contrast, balanced exposure",
    "dramatic chiaroscuro, single key light, deep moody shadows",
    "neon nightscape with reflective wet surfaces, magenta/cyan rim light",
    "first-light pre-dawn, soft pastel sky, low-angle ambient",
]

_CAMERA_VARIANTS = [
    "eye-level wide shot, balanced rule-of-thirds composition, clear focal anchor",
    "low-angle wide, slight tilt, cinematic foreground depth",
    "elevated three-quarter view, layered depth, leading lines",
    "close-up macro detail, shallow depth of field, soft bokeh",
    "top-down flat lay, geometric arrangement, negative space on right",
    "tracking dolly-style framing, motion implied, off-center subject",
]

_STYLE_VARIANTS = [
    "photorealistic cinematic image, rich materials, filmic grading",
    "soft painterly realism, gentle film grain, muted saturation",
    "editorial magazine still, crisp resolution, high dynamic range",
    "modern lifestyle photography, natural color science, subtle haze",
]


def _pick(seq: list[str], idx: int) -> str:
    return seq[idx % len(seq)] if seq else ""


def _slugify_simple(text: str, max_len: int = 24) -> str:
    import re as _re
    s = _re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return (s[:max_len].rstrip("-")) or "image"


def build_5element_prompts(
    *,
    thumbnail_axis: dict | None = None,
    competitor_analysis: dict | None = None,
    concept_hint: str = "",
    n: int = 4,
    include_text_overlay: bool = False,
    filename_prefix: str = "",
    start_index: int = 1,
) -> list[dict]:
    """ベンチマークの 5要素抽出から N 件のバリエーション付き構造化プロンプトを生成。

    各バリエーションは Lighting / Camera-composition / Style を巡回させて
    被写体・背景は固定したまま視覚的多様性を確保する。

    Args:
        start_index: v 番号の開始値。既存ファイルがある場合 caller 側で max+1 を指定すれば
                     上書きを避けて -v5, -v6, ... のように追記できる。

    Returns: [{"prompt": str, "filename": str, "elements": {...}}, ...]
    """
    vd = normalize_visual_direction(competitor_analysis, thumbnail_axis)

    # 5要素のうち時間帯・カメラ・スタイルだけはバリエーションさせる
    # Vision の長文応答（特に日本語）が途中で切れないよう max_len を広げる
    subject = _first_nonempty(concept_hint, vd.get("subject"), "an evocative thumbnail scene", max_len=600)
    background = _first_nonempty(vd.get("background_context"), "a calm, immersive environment that promises relaxation and focus", max_len=600)
    hooks = _clean_text(vd.get("viewer_hooks"), 500)
    avoid = _clean_text(vd.get("avoid"), 500)
    transform = _clean_text(vd.get("transform"), 500)

    base_constraints = [
        "16:9 landscape YouTube thumbnail",
        "photorealistic",
        "no watermark",
        "no channel logo",
        "no copied brand assets",
    ]
    if not include_text_overlay:
        base_constraints.append("no text overlay")

    n = max(1, min(int(n or 4), 8))
    prefix = (filename_prefix or _slugify_simple(concept_hint or subject))[:20].rstrip("-") or "image"
    start = max(1, int(start_index or 1))

    out: list[dict] = []
    for i in range(n):
        v_num = start + i
        # バリエーション巡回は v 番号ベース（start_index がズレてもパターンは連続）
        lighting = _pick(_LIGHTING_VARIANTS, v_num - 1)
        camera = _pick(_CAMERA_VARIANTS, v_num - 1)
        style = _pick(_STYLE_VARIANTS, v_num - 1)
        parts = [
            f"Subject: {subject}",
            f"Background/context: {background}",
            f"Lighting: {lighting}",
            f"Style/rendering: {style}",
            f"Camera/composition: {camera}",
        ]
        if hooks:
            parts.append(f"Viewer resonance: {hooks}")
        if transform:
            parts.append(f"Benchmark translation: reinterpret these abstract elements, do not copy them: {transform}")
        if avoid:
            parts.append(f"Avoid: {avoid}")
        parts.append("Constraints: " + "; ".join(base_constraints) + ".")
        prompt_text = "\n".join(parts)
        out.append({
            "prompt": prompt_text,
            "filename": f"{prefix}-v{v_num}",
            "elements": {
                "subject": subject,
                "background_context": background,
                "lighting": lighting,
                "style_rendering": style,
                "camera_composition": camera,
            },
        })
    return out

#!/usr/bin/env python3
"""Non-Adobe thumbnail compositor.

Photoshop の PSD 合成と同じ成果物名を、Pillow だけで生成するテスト実装。
PSD 自体は読まず、背景画像を 16:9 cover crop して文字レイヤー相当を焼き込む。
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont


DEFAULT_W = 1920
DEFAULT_H = 1080


def _clamp(n: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, n))


def _font_candidates(preferred: Optional[str] = None) -> list[Path]:
    roots = [
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path.home() / "Library/Fonts",
    ]
    names = []
    if preferred:
        names.extend([preferred, f"{preferred}.ttf", f"{preferred}.otf"])
    names.extend([
        "Helvetica.ttc",
        "HelveticaNeue.ttc",
        "Avenir.ttc",
        "Arial.ttf",
        "Hiragino Sans GB.ttc",
        "ヒラギノ角ゴシック W6.ttc",
        "NotoSansCJK-Regular.ttc",
    ])
    out: list[Path] = []
    for root in roots:
        for name in names:
            p = root / name
            if p.exists():
                out.append(p)
        try:
            for p in root.rglob("*.ttf"):
                if preferred and preferred.lower() in p.stem.lower():
                    out.append(p)
            for p in root.rglob("*.otf"):
                if preferred and preferred.lower() in p.stem.lower():
                    out.append(p)
        except Exception:
            pass
    return out


def _font(size: int, preferred: Optional[str] = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for p in _font_candidates(preferred):
        try:
            return ImageFont.truetype(str(p), size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _cover_image(path: str | Path, width: int, height: int) -> Image.Image:
    img = Image.open(path).convert("RGB")
    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w = int(math.ceil(src_w * scale))
    new_h = int(math.ceil(src_h * scale))
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max(0, (new_w - width) // 2)
    top = max(0, (new_h - height) // 2)
    return img.crop((left, top, left + width, top + height)).convert("RGBA")


def _add_vignette(img: Image.Image, strength: float = 0.28) -> Image.Image:
    strength = _clamp(strength, 0, 0.75)
    if strength <= 0:
        return img
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    pad_x = int(w * 0.08)
    pad_y = int(h * 0.04)
    draw.ellipse((-pad_x, -pad_y, w + pad_x, h + pad_y), fill=255)
    mask = ImageEnhance.Contrast(mask.filter(ImageFilter.GaussianBlur(int(w * 0.12)))).enhance(1.25)
    dark = Image.new("RGBA", (w, h), (0, 0, 0, int(255 * strength)))
    inv = Image.eval(mask, lambda p: 255 - p)
    dark.putalpha(inv.point(lambda p: int(p * strength)))
    out = img.copy()
    out.alpha_composite(dark)
    return out


def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int, int, int]:
    try:
        return draw.textbbox((0, 0), text, font=font)
    except Exception:
        w = int(draw.textlength(text, font=font))
        return (0, 0, w, font.size if hasattr(font, "size") else 32)


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_width: int, start_size: int,
              min_size: int = 30, preferred: Optional[str] = None) -> ImageFont.ImageFont:
    size = start_size
    while size > min_size:
        f = _font(size, preferred)
        b = _text_bbox(draw, text, f)
        if (b[2] - b[0]) <= max_width:
            return f
        size -= 4
    return _font(min_size, preferred)


def _draw_centered_text(
    img: Image.Image,
    text: str,
    y: int,
    max_width: int,
    start_size: int,
    fill: tuple[int, int, int, int] = (255, 255, 255, 245),
    preferred_font: Optional[str] = None,
    tracking: int = 0,
    shadow: bool = True,
) -> tuple[int, int, int, int]:
    if not text:
        return (0, y, 0, y)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    f = _fit_font(draw, text, max_width, start_size, preferred=preferred_font)
    bbox = _text_bbox(draw, text, f)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (img.width - text_w) // 2
    if tracking > 0 and len(text) > 1:
        text_w = int(sum(draw.textlength(ch, font=f) for ch in text) + tracking * (len(text) - 1))
        x = (img.width - text_w) // 2
        if shadow:
            sx = x
            for ch in text:
                draw.text((sx + 4, y + 5), ch, font=f, fill=(0, 0, 0, 125))
                sx += int(draw.textlength(ch, font=f)) + tracking
        cx = x
        for ch in text:
            draw.text((cx, y), ch, font=f, fill=fill)
            cx += int(draw.textlength(ch, font=f)) + tracking
    else:
        if shadow:
            draw.text((x + 4, y + 5), text, font=f, fill=(0, 0, 0, 140))
        draw.text((x, y), text, font=f, fill=fill)
    img.alpha_composite(layer)
    return (x, y, x + text_w, y + text_h)


def _draw_playlist(img: Image.Image, playlist_text: str, preferred_font: Optional[str] = None) -> None:
    if not playlist_text:
        return
    draw = ImageDraw.Draw(img)
    text = playlist_text.strip()
    f = _fit_font(draw, text, int(img.width * 0.72), 118, min_size=44, preferred=preferred_font)
    bbox = _text_bbox(draw, text, f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (img.width - tw) // 2
    y = int(img.height * 0.145)
    band = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(band)
    pad_x, pad_y = 38, 20
    rect = (x - pad_x, y - pad_y, x + tw + pad_x, y + th + pad_y)
    bd.rounded_rectangle(rect, radius=8, fill=(0, 0, 0, 70), outline=(255, 255, 255, 52), width=1)
    bd.text((x + 4, y + 5), text, font=f, fill=(0, 0, 0, 130))
    bd.text((x, y), text, font=f, fill=(255, 255, 255, 232))
    img.alpha_composite(band)


def _draw_scene(img: Image.Image, scene_text: str, scene_text_ja: Optional[str] = None,
                preferred_font: Optional[str] = None, ja_font: Optional[str] = None) -> None:
    if not scene_text and not scene_text_ja:
        return
    shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    y0 = int(img.height * 0.50)
    sd.rectangle((0, y0 - 170, img.width, y0 + 210), fill=(0, 0, 0, 58))
    img.alpha_composite(shade)
    if scene_text:
        _draw_centered_text(
            img,
            scene_text.strip().upper(),
            y0 - 54,
            int(img.width * 0.82),
            126,
            preferred_font=preferred_font,
            tracking=3,
        )
    if scene_text_ja:
        _draw_centered_text(
            img,
            scene_text_ja.strip(),
            y0 + 86,
            int(img.width * 0.74),
            54,
            fill=(255, 255, 255, 230),
            preferred_font=ja_font,
            tracking=0,
        )


def _save_jpg(img: Image.Image, path: Path, quality: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(path, "JPEG", quality=int(_clamp(quality, 1, 95)), optimize=True, progressive=True)


def _playlist_text_from_layer(layer_name: str) -> str:
    text = (layer_name or "PLAY LIST").strip()
    text = re.sub(r"[_-]+", " ", text)
    return text or "PLAY LIST"


def render_dual_thumbnail(
    psd_path: str,
    base_image: str,
    scene_text: str,
    out_dir: Optional[str] = None,
    vol_name: Optional[str] = None,
    base_layer: str = "base",
    scene_text_layer: str = "都市名_テキスト",
    playlist_layer: str = "PLAY LIST ",
    quality: int = 90,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    save_psd: bool = False,
    scene_text_font: Optional[str] = None,
    scene_text_ja: Optional[str] = None,
    scene_text_ja_layer: Optional[str] = None,
    scene_text_ja_font: Optional[str] = None,
    toggle_always_visible: bool = False,
    bg_base_only: bool = False,
    center_text: bool = True,
    playlist_text: Optional[str] = None,
    darken: float = 0.18,
    vignette: float = 0.28,
) -> dict:
    """Generate vol{N}.jpg and サムネイル.jpg without Photoshop.

    Signature intentionally mirrors app_photoshop.render_dual_thumbnail. PSD-related
    parameters are accepted for call compatibility but are not used to read layers.
    """
    base = Path(base_image).expanduser().resolve()
    if not base.exists():
        raise FileNotFoundError(f"背景画像が存在しません: {base}")
    out_dir_p = Path(out_dir).expanduser().resolve() if out_dir else base.parent
    if not vol_name:
        m = re.search(r"vol\d+", Path(psd_path or base.stem).stem, re.IGNORECASE)
        vol_name = m.group(0).lower() if m else base.stem
    width = int(target_width or DEFAULT_W)
    height = int(target_height or DEFAULT_H)
    headline = playlist_text if playlist_text is not None else _playlist_text_from_layer(playlist_layer)

    canvas = _cover_image(base, width, height)
    if darken:
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, int(255 * _clamp(darken, 0, 0.7))))
        canvas.alpha_composite(overlay)
    canvas = _add_vignette(canvas, vignette)

    out_bg = out_dir_p / f"{vol_name}.jpg"
    out_thumb = out_dir_p / "サムネイル.jpg"

    bg = canvas.copy()
    if not bg_base_only:
        if toggle_always_visible or headline:
            _draw_playlist(bg, headline, preferred_font=scene_text_font)
    _save_jpg(bg, out_bg, quality)

    thumb = canvas.copy()
    if toggle_always_visible:
        _draw_playlist(thumb, headline, preferred_font=scene_text_font)
    _draw_scene(
        thumb,
        scene_text if center_text else scene_text,
        scene_text_ja=scene_text_ja if scene_text_ja_layer or scene_text_ja else scene_text_ja,
        preferred_font=scene_text_font,
        ja_font=scene_text_ja_font,
    )
    _save_jpg(thumb, out_thumb, quality)

    return {
        "bg": str(out_bg),
        "thumbnail": str(out_thumb),
        "with_toggle": str(out_bg),
        "engine": "pillow",
        "base_image": str(base),
        "psd_ignored": str(psd_path or ""),
        "base_layer_ignored": base_layer,
        "scene_text_layer_ignored": scene_text_layer,
        "save_psd_ignored": bool(save_psd),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Render thumbnail pair without Photoshop")
    ap.add_argument("--base-image", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--vol-name", required=True)
    ap.add_argument("--scene-text", default="")
    ap.add_argument("--scene-text-ja", default="")
    ap.add_argument("--playlist-text", default="PLAY LIST")
    ap.add_argument("--width", type=int, default=DEFAULT_W)
    ap.add_argument("--height", type=int, default=DEFAULT_H)
    ap.add_argument("--quality", type=int, default=90)
    args = ap.parse_args()
    result = render_dual_thumbnail(
        psd_path="",
        base_image=args.base_image,
        scene_text=args.scene_text,
        scene_text_ja=args.scene_text_ja,
        out_dir=args.out_dir,
        vol_name=args.vol_name,
        playlist_text=args.playlist_text,
        target_width=args.width,
        target_height=args.height,
        quality=args.quality,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

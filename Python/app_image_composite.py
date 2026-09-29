"""既存のPillow合成をAdobeなしの制作工程へ接続する。背景原本は保持する。"""
from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from pathlib import Path

from PIL import Image
from app_image_compositor import render_dual_thumbnail


def compose_images(folder: Path, vol: int, config: dict, *, force=False) -> bool:
    folder = Path(folder)
    output = [folder / f"vol{vol}.jpg", folder / "サムネイル.jpg"]
    if not force and all(path.is_file() for path in output):
        print("Adobeなし画像合成: 背景とサムネがあるため再利用します")
        return True
    source = next((p for p in [folder / f"vol{vol}.png", folder / f"vol{vol}_source.jpg"]
                   if p.is_file()), None)
    if source is None:
        raise ValueError("画像の原本がありません。背景画像を生成・配置してください")
    settings = config.get("thumbnail_composition") or {}
    title = str(settings.get("title") or "").strip()
    scene = folder / "scene_en.txt"
    if not title and config.get("scene_text_enabled") and scene.is_file():
        title = scene.read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory(prefix="studio-image-") as raw:
        scratch = Path(raw)
        render_dual_thumbnail(
            psd_path="", base_image=str(source), scene_text=title, out_dir=str(scratch),
            vol_name=f"vol{vol}", target_width=1920, target_height=1080,
            scene_text_font=settings.get("font"), playlist_text=str(settings.get("headline") or ""),
            toggle_always_visible=bool(settings.get("headline_always_visible", False)),
            bg_base_only=True, darken=float(settings.get("darken", 0)),
            vignette=float(settings.get("vignette", 0)), quality=95,
        )
        staged = [scratch / p.name for p in output]
        for path in staged:
            with Image.open(path) as img:
                if img.size != (1920, 1080):
                    raise ValueError("画像合成の出力が1920×1080になっていません")
                img.verify()
        # 2枚とも検証してから更新。既存の画像も復元できるよう保存する。
        backup = folder / ".image_previous" / secrets.token_hex(8)
        backup.mkdir(parents=True)
        replaced = []
        try:
            for target, candidate in zip(output, staged):
                if target.exists():
                    shutil.copy2(target, backup / target.name)
                part = folder / ("." + target.name + ".part")
                shutil.copyfile(candidate, part)
                os.replace(part, target)
                replaced.append(target)
        except BaseException:
            for target in replaced:
                prior = backup / target.name
                if prior.exists():
                    shutil.copy2(prior, target)
                else:
                    target.unlink()
            raise
        finally:
            for target in output:
                (folder / ("." + target.name + ".part")).unlink(missing_ok=True)
    print("Adobeなし画像合成: 動画背景とサムネを1920×1080で保存しました")
    return True

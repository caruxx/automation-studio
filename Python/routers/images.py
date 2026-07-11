#!/usr/bin/env python3
"""画像生成ルータ（D9）。codex画像生成/背景画像/参照フォルダ/チャンネル横断サムネ一括/サムネ承認状態/シリーズ画像案。重い処理はハンドラ内ローカルimport、app_coreの土台シンボルを全取り込み。"""
from fastapi import APIRouter
from typing import Any, Dict, List, Optional
from app_core import *  # noqa: F401,F403  土台シンボル(stdlib/fastapi/foundation)を全取り込み

router = APIRouter()


# ─── 画像生成: Flow/Midjourney は D8 で撤去（codex 一本化 = codex_imagegen.py）───


class ImageCompositorRenderRequest(BaseModel):
    video_name: Optional[str] = None
    video_folder: Optional[str] = None
    scene_text: Optional[str] = None
    scene_text_ja: Optional[str] = None
    playlist_text: Optional[str] = None
    image_subdir: Optional[str] = None
    quality: int = 90
    target_width: Optional[int] = None
    target_height: Optional[int] = None
    darken: Optional[float] = 0.18
    vignette: Optional[float] = 0.28
    toggle_always_visible: Optional[bool] = None
    bg_base_only: Optional[bool] = None


class ImageCompositorCanvasSaveRequest(BaseModel):
    video_name: Optional[str] = None
    video_folder: Optional[str] = None
    filename: str = "サムネイル.jpg"
    data_url: str


class ImageCompositorTemplateRequest(BaseModel):
    video_name: Optional[str] = None
    video_folder: Optional[str] = None
    template: Dict[str, Any]


def _layout_template_path(folder: Path) -> Path:
    return folder / "thumbnail_layout_template.json"


def _channel_layout_template_path(folder: Path) -> Path:
    return folder.parent / "thumbnail_layout_template.json"


def _resolve_video_folder(video_name: Optional[str], video_folder: Optional[str]) -> Path:
    if video_folder:
        folder = Path(video_folder).expanduser()
    elif video_name:
        folder = resolve_video_folder(video_name)
    else:
        raise HTTPException(400, "video_folder か video_name のいずれかが必要")
    if not folder.exists():
        raise HTTPException(404, f"動画フォルダが存在しません: {folder}")
    return folder


def _video_vol_number(folder: Path) -> Optional[str]:
    m = re.match(r"^(\d+)_", folder.name)
    return m.group(1) if m else None


def _find_compositor_base_image(folder: Path, image_subdir: str = "image") -> Optional[Path]:
    vol_num = _video_vol_number(folder)
    if vol_num:
        for cand_name in (f"vol{vol_num}.png", f"vol{vol_num}_source.jpg"):
            cand = folder / cand_name
            if cand.exists():
                return cand
    for pat in ("vol*.png", "vol*_source.jpg"):
        for cand in sorted(folder.glob(pat)):
            if cand.name.endswith("_source.jpg") or cand.suffix.lower() == ".png":
                return cand
    for sub in (image_subdir, "Image", "image"):
        d = folder / sub
        if not d.exists():
            continue
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            found = sorted(d.glob(ext))
            if found:
                return found[0]
    return None


def _scan_local_font_families() -> List[Dict[str, Any]]:
    """Mac/PCに入っているフォントをCanvas用のfamily名として返す。"""
    try:
        from PIL import ImageFont
    except Exception:
        ImageFont = None

    roots = [
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path.home() / "Library" / "Fonts",
    ]
    exts = {".ttf", ".otf", ".ttc"}
    seen: Dict[str, Dict[str, Any]] = {}
    files = []
    for root in roots:
        if not root.exists():
            continue
        try:
            files.extend(p for p in root.rglob("*") if p.suffix.lower() in exts)
        except Exception:
            continue
    for path in sorted(files, key=lambda p: str(p).lower())[:1500]:
        family = path.stem
        style = ""
        if ImageFont is not None:
            try:
                ft = ImageFont.truetype(str(path), 14)
                names = ft.getname()
                if names and names[0]:
                    family = str(names[0]).strip() or family
                if len(names) > 1 and names[1]:
                    style = str(names[1]).strip()
            except Exception:
                pass
        if family.startswith("."):
            continue
        key = family.lower()
        if not key or key in seen:
            continue
        seen[key] = {
            "family": family,
            "label": f"{family} {style}".strip(),
            "style": style,
            "path": str(path),
        }
    fonts = sorted(seen.values(), key=lambda x: x["family"].lower())
    preferred = [
        {"family": "system", "label": "System Sans", "style": "", "path": ""},
        {"family": "serif", "label": "Serif", "style": "", "path": ""},
        {"family": "gothic", "label": "Japanese Gothic", "style": "", "path": ""},
        {"family": "mincho", "label": "Japanese Mincho", "style": "", "path": ""},
        {"family": "script", "label": "Script / Brand", "style": "", "path": ""},
        {"family": "mono", "label": "Mono", "style": "", "path": ""},
    ]
    existing = {f["family"].lower() for f in fonts}
    return preferred + [f for f in fonts if f["family"].lower() not in existing.intersection({p["family"].lower() for p in preferred})]


@router.post("/api/image-compositor/render-dual-thumbnail")
def api_image_compositor_render_dual_thumbnail(req: ImageCompositorRenderRequest):
    """Photoshop なしで vol{N}.jpg + サムネイル.jpg を生成するテスト版。"""
    cfg = get_dashboard_config()
    folder = _resolve_video_folder(req.video_name, req.video_folder)
    image_subdir = req.image_subdir or cfg.get("psd_image_subdir") or "image"
    base_image = _find_compositor_base_image(folder, image_subdir=image_subdir)
    if not base_image:
        raise HTTPException(404, f"背景画像が見つかりません: {folder}")

    scene_text = (req.scene_text or "").strip()
    cache_file = folder / "scene_en.txt"
    if not scene_text and cache_file.exists():
        try:
            scene_text = cache_file.read_text(encoding="utf-8").strip()
        except Exception:
            scene_text = ""
    if not scene_text:
        try:
            from scene_text_generator import generate_scene_text_for_image
            scene_text = generate_scene_text_for_image(
                str(base_image),
                persona=(cfg.get("persona") or "").strip(),
                tone=(cfg.get("scene_text_tone") or ""),
                examples=cfg.get("scene_text_examples") or [],
                forbidden_phrases=cfg.get("scene_text_forbidden") or [],
                structure=(cfg.get("scene_text_structure") or ""),
            )
            if scene_text:
                try:
                    cache_file.write_text(scene_text + "\n", encoding="utf-8")
                except Exception:
                    pass
        except Exception:
            scene_text = ""

    scene_text_ja = (req.scene_text_ja or "").strip()
    if not scene_text_ja:
        ja_file = folder / "scene_ja.txt"
        if ja_file.exists():
            try:
                scene_text_ja = ja_file.read_text(encoding="utf-8").strip()
            except Exception:
                scene_text_ja = ""

    vol_num = _video_vol_number(folder)
    vol_name = f"vol{vol_num}" if vol_num else "vol"
    width = req.target_width or cfg.get("psd_export_width") or 1920
    height = req.target_height or cfg.get("psd_export_height") or 1080
    toggle_always_visible = bool(cfg.get("psd_toggle_always_visible")) if req.toggle_always_visible is None else bool(req.toggle_always_visible)
    bg_base_only = bool(cfg.get("psd_bg_base_only")) if req.bg_base_only is None else bool(req.bg_base_only)

    try:
        import app_image_compositor as comp
        result = comp.render_dual_thumbnail(
            psd_path=str(folder / "_non_adobe_compositor.psd"),
            base_image=str(base_image),
            scene_text=scene_text or "",
            scene_text_ja=scene_text_ja or None,
            scene_text_ja_layer=(cfg.get("scene_text_ja_layer") or None),
            out_dir=str(folder),
            vol_name=vol_name,
            playlist_layer=cfg.get("psd_toggle_layer") or "PLAY LIST",
            playlist_text=req.playlist_text,
            quality=req.quality,
            target_width=int(width),
            target_height=int(height),
            scene_text_font=cfg.get("psd_text_font") or None,
            scene_text_ja_font=cfg.get("scene_text_ja_font") or None,
            toggle_always_visible=toggle_always_visible,
            bg_base_only=bg_base_only,
            darken=float(req.darken if req.darken is not None else 0.18),
            vignette=float(req.vignette if req.vignette is not None else 0.28),
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"image-compositor 失敗: {e}")

    return {
        "ok": True,
        "engine": "pillow",
        "video_folder": str(folder),
        "base_image": str(base_image),
        "scene_text": scene_text,
        "scene_text_ja": scene_text_ja,
        **result,
    }


@router.get("/api/image-compositor/fonts")
def api_image_compositor_fonts(refresh: bool = False):
    """ローカルPCに入っているフォントfamily名を返す。"""
    cache = getattr(api_image_compositor_fonts, "_cache", None)
    if refresh or cache is None:
        fonts = _scan_local_font_families()
        cache = {"fonts": fonts, "count": len(fonts), "updated_at": datetime.utcnow().isoformat() + "Z"}
        setattr(api_image_compositor_fonts, "_cache", cache)
    return {"ok": True, **cache}


@router.post("/api/image-compositor/save-canvas")
def api_image_compositor_save_canvas(req: ImageCompositorCanvasSaveRequest):
    """ブラウザ上の簡易レイヤーエディタから書き出した画像を動画フォルダへ保存。"""
    folder = _resolve_video_folder(req.video_name, req.video_folder)
    filename = Path(req.filename or "サムネイル.jpg").name
    if not re.search(r"\.(jpe?g|png|webp)$", filename, re.IGNORECASE):
        raise HTTPException(400, "filename は jpg/png/webp のみ対応です")
    raw = req.data_url or ""
    m = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$", raw, re.S)
    if not m:
        raise HTTPException(400, "data_url は image の base64 Data URL が必要です")
    try:
        import base64
        import io
        from PIL import Image
        data = base64.b64decode(m.group(2), validate=True)
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "画像データが大きすぎます")
        img = Image.open(io.BytesIO(data))
        img.load()
        out = folder / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        ext = out.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            img.convert("RGB").save(out, "JPEG", quality=92, optimize=True, progressive=True)
        elif ext == ".png":
            img.convert("RGBA").save(out, "PNG", optimize=True)
        else:
            img.convert("RGB").save(out, "WEBP", quality=92)
        return {"ok": True, "path": str(out), "filename": out.name, "bytes": out.stat().st_size}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"canvas 保存失敗: {e}")


@router.get("/api/image-compositor/template")
def api_image_compositor_get_template(video_name: Optional[str] = None, video_folder: Optional[str] = None):
    """動画フォルダのサムネレイアウトテンプレートを取得。"""
    folder = _resolve_video_folder(video_name, video_folder)
    path = _layout_template_path(folder)
    if not path.exists():
        channel_path = _channel_layout_template_path(folder)
        if channel_path.exists():
            path = channel_path
    if not path.exists():
        return {"ok": True, "exists": False, "path": str(path), "template": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"ok": True, "exists": True, "path": str(path), "template": data}
    except Exception as e:
        raise HTTPException(500, f"テンプレート読み込み失敗: {e}")


@router.post("/api/image-compositor/template")
def api_image_compositor_save_template(req: ImageCompositorTemplateRequest):
    """動画フォルダへサムネレイアウトテンプレートを保存。"""
    folder = _resolve_video_folder(req.video_name, req.video_folder)
    payload = req.template or {}
    layers = payload.get("layers")
    if not isinstance(layers, list):
        raise HTTPException(400, "template.layers が必要です")
    safe_layers = []
    for layer in layers[:50]:
        if not isinstance(layer, dict):
            continue
        typ = str(layer.get("type") or "")
        if typ not in ("text", "image"):
            continue
        item = {
            "type": typ,
            "name": str(layer.get("name") or typ)[:120],
            "x": float(layer.get("x") or 0),
            "y": float(layer.get("y") or 0),
            "scale": float(layer.get("scale") or 1),
            "opacity": float(layer.get("opacity") if layer.get("opacity") is not None else 1),
            "visible": bool(layer.get("visible", True)),
            "locked": bool(layer.get("locked", False)),
        }
        if typ == "text":
            item.update({
                "text": str(layer.get("text") or "")[:300],
                "size": float(layer.get("size") or 76),
                "color": str(layer.get("color") or "#ffffff")[:32],
                "font": str(layer.get("font") or "system")[:120],
            })
        else:
            item.update({
                "src": str(layer.get("src") or "")[:500000],
                "source_name": str(layer.get("source_name") or layer.get("name") or "")[:180],
            })
        safe_layers.append(item)
    out = {
        "version": 1,
        "canvas": payload.get("canvas") if isinstance(payload.get("canvas"), dict) else {"width": 1280, "height": 720},
        "background": payload.get("background") if isinstance(payload.get("background"), dict) else {},
        "layers": safe_layers,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    path = _layout_template_path(folder)
    channel_path = _channel_layout_template_path(folder)
    try:
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        channel_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "path": str(path), "channel_path": str(channel_path), "template": out}
    except Exception as e:
        raise HTTPException(500, f"テンプレート保存失敗: {e}")


class ImageModulesUpdateRequest(BaseModel):
    modules: Optional[Dict[str, List[Dict[str, Any]]]] = None
    selection: Optional[Dict[str, str]] = None
    overrides: Optional[Dict[str, str]] = None
    add_module: Optional[Dict[str, Any]] = None
    legacy_prompt: Optional[str] = None


def _channel_folder_by_id(channel_id: str) -> Path:
    cid = (channel_id or "").strip()
    for ch in get_channels():
        if cid in (ch.get("id"), ch.get("name"), ch.get("channel_name")):
            p = Path(ch.get("folder") or "").expanduser()
            if p.exists():
                return p
    cfg = get_dashboard_config()
    if not cid or cid in (cfg.get("channel_name"), "active"):
        p = Path(cfg.get("channel_folder") or "").expanduser()
        if p.exists():
            return p
    raise HTTPException(404, f"channel_id が見つかりません: {channel_id}")


@router.get("/api/image-modules/{channel_id}")
def api_image_modules_get(channel_id: str, migrate: bool = True):
    import app_image_modules as _im
    folder = _channel_folder_by_id(channel_id)
    legacy = ""
    if migrate:
        cfg = get_dashboard_config()
        legacy = (
            (cfg.get("bgimage_prompt") or cfg.get("thumbnail_prompt") or cfg.get("image_prompt") or "")
            if Path(cfg.get("channel_folder") or "").expanduser() == folder else ""
        )
        if not legacy:
            for p in (
                SHARED_CONFIG_DIR / "benchmark" / "channels" / channel_id / "thumbnail.json",
                SHARED_CONFIG_DIR / "benchmark" / "thumbnail.json",
                SHARED_BASE / "config" / "benchmark" / "channels" / channel_id / "thumbnail.json",
                SHARED_BASE / "config" / "benchmark" / "thumbnail.json",
            ):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    agg = ((d.get("analysis") or {}).get("aggregate") or {})
                    rec = agg.get("recommendation_for_self") or {}
                    legacy = (agg.get("gpt_image2_prompt_seed") or rec.get("gpt_image2_prompt_seed") or "").strip()
                    if not legacy:
                        notes = agg.get("gpt_image2_prompt_notes") or rec.get("gpt_image2_prompt_notes") or {}
                        if isinstance(notes, dict):
                            legacy = " / ".join(
                                f"{k}: {v}" for k, v in notes.items()
                                if isinstance(v, str) and v.strip()
                            )
                    if legacy:
                        break
                except Exception:
                    pass
    payload = _im.ensure_modules(folder, legacy_prompt=legacy, channel_id=channel_id) if migrate else _im.load_modules(folder)
    sections, meta = _im.selected_sections(payload)
    return {
        "status": "ok",
        "channel_id": channel_id,
        "channel_folder": str(folder),
        "path": str(_im.module_path(folder)),
        "schema": list(_im.SECTIONS),
        "modules": payload.get("modules") or {},
        "selection": payload.get("selection") or {},
        "overrides": payload.get("overrides") or {},
        "legacy_prompt_backup": payload.get("legacy_prompt_backup") or "",
        "composed_prompt": _im.compose_prompt(sections),
        "meta": meta,
        "winning_modules": payload.get("winning_modules") or [],
    }


@router.put("/api/image-modules/{channel_id}")
def api_image_modules_put(channel_id: str, req: ImageModulesUpdateRequest):
    import app_image_modules as _im
    folder = _channel_folder_by_id(channel_id)
    payload = _im.ensure_modules(folder, legacy_prompt=req.legacy_prompt or "", channel_id=channel_id)
    if req.modules is not None:
        payload["modules"] = req.modules
    if req.selection is not None:
        payload.setdefault("selection", {}).update({k: v for k, v in req.selection.items() if k in _im.SECTIONS})
    if req.overrides is not None:
        payload["overrides"] = {k: v for k, v in req.overrides.items() if k in _im.SECTIONS}
    if req.add_module:
        sec = str(req.add_module.get("section") or "")
        if sec not in _im.SECTIONS:
            raise HTTPException(400, f"invalid section: {sec}")
        name = str(req.add_module.get("name") or "custom")
        text = str(req.add_module.get("text") or "")
        mid = str(req.add_module.get("id") or f"{sec}:{re.sub(r'[^A-Za-z0-9_-]+', '-', name).strip('-').lower() or 'custom'}")
        mod = {"id": mid, "section": sec, "name": name, "text": text, "source": "ui", "created_at": datetime.utcnow().isoformat() + "Z"}
        rows = [m for m in (payload.get("modules") or {}).get(sec, []) if m.get("id") != mid]
        rows.append(mod)
        payload.setdefault("modules", {})[sec] = rows
    payload = _im.save_modules(folder, payload)
    sections, meta = _im.selected_sections(payload)
    return {
        "status": "ok",
        "channel_id": channel_id,
        "path": str(_im.module_path(folder)),
        "modules": payload.get("modules") or {},
        "selection": payload.get("selection") or {},
        "overrides": payload.get("overrides") or {},
        "composed_prompt": _im.compose_prompt(sections),
        "meta": meta,
    }

# ─── API: ChatGPT / Codex 画像生成（並列） ───

CODEX_IMAGEGEN_SCRIPT = SHARED_BASE / "Python" / "codex_imagegen.py"


class CodexImagegenRefImage(BaseModel):
    filename: Optional[str] = ""
    data_url: str                              # "data:image/png;base64,..." 形式


class CodexImagegenGenerateRequest(BaseModel):
    prompts: str                              # 1 ブロック 1 件・空行区切り、`...::filename` で個別ファイル名指定可
    video_name: Optional[str] = None          # 指定時は {channel}/{video_name}/Image に保存
    output_dir: Optional[str] = None          # 明示保存先（優先）
    max_parallel: Optional[int] = 4
    timeout_sec: Optional[int] = 900
    aspect_ratio: Optional[str] = "16:9"      # 16:9 / 1:1 / 9:16 / 4:3 / 3:4 → size に直接マップ
    use_benchmark_picked: Optional[bool] = False  # benchmark picked サムネを参照画像として渡す
    reference_images_b64: Optional[List[CodexImagegenRefImage]] = None  # 直接アップロードされた参照画像群
    # gpt-image-2 の追加パラメータ
    quality: Optional[str] = "medium"         # low / medium / high / auto
    n_per_prompt: Optional[int] = 1           # 1 リクエストあたりの生成枚数 (1-10)
    background: Optional[str] = "auto"        # auto / transparent / opaque
    moderation: Optional[str] = "auto"        # auto / low
    input_fidelity: Optional[str] = "high"    # edits 用: high / low
    output_format: Optional[str] = "png"      # png / jpeg / webp


class CodexImagegenBuild5ERequest(BaseModel):
    """参照画像（アップロード or picked）を Claude Vision でリアルタイム分析して 5要素を抽出 →
    Lighting / Camera / Style をバリエーション巡回した N 件のプロンプトを返す。
    参照画像が 1 枚もない場合のみ、ベンチマーク (thumbnail_aggregate) のキャッシュ文章にフォールバック。"""
    video_name: Optional[str] = None
    n: Optional[int] = 4
    concept_hint: Optional[str] = ""
    include_text_overlay: Optional[bool] = False
    filename_prefix: Optional[str] = ""
    use_benchmark_picked: Optional[bool] = True
    reference_images_b64: Optional[List[CodexImagegenRefImage]] = None


class CodexImagegenSuggestRequest(BaseModel):
    context: str
    count: Optional[int] = 4
    provider: Optional[str] = "codex"
    cli: Optional[str] = None


def _run_codex_text_prompt(cli_cmd: str, prompt: str, timeout: int = 300) -> str:
    """Run Codex CLI non-interactively and return the final assistant message."""
    import tempfile

    fd, out_path = tempfile.mkstemp(prefix="orzz_codex_prompt_", suffix=".txt")
    os.close(fd)
    try:
        proc = subprocess.run(
            [
                shutil.which(cli_cmd) or cli_cmd,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-last-message",
                out_path,
                "-",
            ],
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:500])
        try:
            text = Path(out_path).read_text(encoding="utf-8")
        except Exception:
            text = ""
        return text or proc.stdout or ""
    finally:
        try:
            Path(out_path).unlink()
        except Exception:
            pass


@router.post("/api/codex-imagegen/generate")
async def api_codex_imagegen_generate(req: CodexImagegenGenerateRequest):
    out_dir = req.output_dir
    if not out_dir and req.video_name:
        cfg = get_dashboard_config()
        out_dir = str(resolve_video_folder(req.video_name) / "Image")
    if not out_dir:
        raise HTTPException(400, "output_dir または video_name が必要です")

    prompts_text = (req.prompts or "").strip()
    if not prompts_text:
        raise HTTPException(400, "prompts が空です")

    # アスペクト比 → gpt-image-2 の標準 size に直接マップ (プロンプト文への前置注入は廃止)
    aspect = (req.aspect_ratio or "16:9").strip()
    if aspect not in ("16:9", "1:1", "9:16", "4:3", "3:4"):
        aspect = "16:9"

    quality = (req.quality or "medium").strip()
    if quality not in ("low", "medium", "high", "auto"):
        quality = "medium"
    background = (req.background or "auto").strip()
    if background not in ("auto", "transparent", "opaque"):
        background = "auto"
    moderation = (req.moderation or "auto").strip()
    if moderation not in ("auto", "low"):
        moderation = "auto"
    input_fidelity = (req.input_fidelity or "high").strip()
    if input_fidelity not in ("high", "low"):
        input_fidelity = "high"
    output_format = (req.output_format or "png").strip()
    if output_format not in ("png", "jpeg", "webp"):
        output_format = "png"
    n_per_prompt = max(1, min(10, int(req.n_per_prompt or 1)))

    # 1 行 1 件のプロンプトを stdin で渡す（並列は 1〜4 に制限）
    parallel = max(1, min(4, int(req.max_parallel or 4)))
    cmd = [
        sys.executable, "-u", str(CODEX_IMAGEGEN_SCRIPT),
        "--output-dir", out_dir,
        "--max-parallel", str(parallel),
        "--timeout", str(max(60, int(req.timeout_sec or 900))),
        "--aspect", aspect,
        "--quality", quality,
        "--background", background,
        "--moderation", moderation,
        "--input-fidelity", input_fidelity,
        "--output-format", output_format,
        "--n", str(n_per_prompt),
    ]
    reference_names: list[str] = []
    if req.use_benchmark_picked:
        try:
            import app_benchmark_thumbnail as _bt
            # picked は複数選択可。Codex CLI 経路でも全枚数を分析対象として渡す
            # （実用上のキャップは 4 — Codex のプロンプト長と分析時間を考慮）
            picked_paths = _bt.get_picked_paths(limit=4)
            for p in picked_paths:
                cmd += ["--reference-image", p]
                reference_names.append(Path(p).name)
        except Exception:
            pass

    # 直接アップロードされた参照画像群を一時ファイル化して --reference-image に追加（複数選択可）
    uploaded_refs_tmp: list[Path] = []
    if req.reference_images_b64:
        import base64 as _b64
        import tempfile as _tempfile
        for idx, ref in enumerate(req.reference_images_b64[:8]):  # 安全側で 8 枚まで
            try:
                du = (ref.data_url or "").strip()
                if "," in du and du.startswith("data:"):
                    header, _, b64body = du.partition(",")
                    # ex: data:image/png;base64
                    mime = "image/png"
                    if header.startswith("data:") and ";" in header:
                        mime = header.split(":", 1)[1].split(";", 1)[0] or mime
                    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                               "image/webp": ".webp", "image/gif": ".gif"}
                    ext = ext_map.get(mime, ".png")
                    raw = _b64.b64decode(b64body)
                else:
                    # 純粋な base64 のみのケース
                    raw = _b64.b64decode(du)
                    ext = Path(ref.filename or "").suffix or ".png"
                # 安全なファイル名
                safe_stem = re.sub(r"[^\w.\-]+", "_", Path(ref.filename or f"upload-{idx+1}").stem)[:48] or f"upload-{idx+1}"
                tf = _tempfile.NamedTemporaryFile(prefix=f"codex_ref_{safe_stem}_", suffix=ext, delete=False)
                tf.write(raw)
                tf.close()
                tmp_path = Path(tf.name)
                uploaded_refs_tmp.append(tmp_path)
                cmd += ["--reference-image", str(tmp_path)]
                reference_names.append(ref.filename or tmp_path.name)
            except Exception as e:
                # 1 枚壊れても他は処理続行
                continue

    task_logs["codex_imagegen"] = []
    import datetime as _dt
    line_count = sum(1 for ln in prompts_text.splitlines() if ln.strip() and not ln.strip().startswith("#"))
    task_meta["codex_imagegen"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "video_name": req.video_name or "",
        "output_dir": out_dir,
        "prompt_count": line_count,
        "max_parallel": parallel,
        "aspect_ratio": aspect,
        "quality": quality,
        "n_per_prompt": n_per_prompt,
        "background": background,
        "moderation": moderation,
        "input_fidelity": input_fidelity,
        "output_format": output_format,
        "reference_images": reference_names,
    }
    reservation = await _ensure_not_running("codex_imagegen", "Codex 画像生成が既に実行中です")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        _register_active_task("codex_imagegen", proc, reservation)
    except Exception:
        _release_task_reservation("codex_imagegen", reservation)
        raise
    # stdin に流し込んで close（codex_imagegen.py は stdin から読み取る）
    try:
        proc.stdin.write(prompts_text + "\n")
        proc.stdin.close()
    except Exception:
        pass
    deadline = max(120, int(req.timeout_sec or 900)) * max(1, line_count) + 120
    _stream_subprocess(proc, "codex_imagegen", timeout=deadline)
    return {
        "status": "started",
        "output_dir": out_dir,
        "prompt_count": line_count,
    }


@router.get("/api/codex-imagegen/status")
def api_codex_imagegen_status():
    proc = active_tasks.get("codex_imagegen")
    running = proc is not None and proc.returncode is None
    logs = task_logs.get("codex_imagegen", [])
    meta = task_meta.get("codex_imagegen", {})
    return {"running": running, "logs": logs[-120:], "meta": meta}


@router.post("/api/codex-imagegen/stop")
def api_codex_imagegen_stop():
    proc = active_tasks.get("codex_imagegen")
    if proc and proc.returncode is None:
        proc.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}


# ─── API: 背景画像生成（パイプライン STEP 3 / Premiere 自動配置の直前） ───
#
# `step_bgimage` の via_api 分岐 / UI「背景画像」カード / 単発 CLI 呼び出しが共通で叩く endpoint。
# 排他制御は task key="bgimage"。並列 Codex 画像生成（"codex_imagegen"）とは別キーなので、
# UI から手動 Codex を走らせている裏でパイプラインが bgimage を走らせると 両方走ってしまうが、
# 物理出力先（vol{N}.png は動画フォルダ直下、Codex は通常 Image/ 配下）が衝突しないため許容する。

class BgImageRunRequest(BaseModel):
    video_name: str
    ref_count: Optional[int] = 3
    force: Optional[bool] = False
    timeout_sec: Optional[int] = 1800
    reference_image: Optional[str] = None


@router.post("/api/bgimage/run")
async def api_bgimage_run(req: BgImageRunRequest):
    """背景画像 vol{N}.png を生成（ベンチマーク参照 + チャンネルコンセプト）。

    `app_pipeline.py --only bgimage --via-api` および UI の「背景画像」カードから呼ばれる。
    内部的には app_pipeline.py を `--only bgimage` で起動し、subprocess の stdout を
    task_logs["bgimage"] に流し込む。via_api を立てずに起動するので無限ループしない。
    """
    config = get_dashboard_config()
    folder = resolve_video_folder(req.video_name)

    # vol 番号抽出
    m = re.match(r"^(\d+)_", req.video_name)
    if not m:
        raise HTTPException(400, f"video_name から vol 番号が抽出できません: {req.video_name}")
    vol = m.group(1)

    # 既存ファイル検出（force=False のときの早期 ok）
    existing = list(folder.glob(f"vol{vol}.png")) + list(folder.glob(f"vol{vol}.jpg"))
    if existing and not req.force:
        # task_logs にも残しておく（UI でリプレイ可能に）
        task_logs["bgimage"] = [
            f"[skip] 既存 {existing[0].name} あり、生成スキップ（force=true で上書き再生成）",
            f"[完了] 終了コード: 0",
        ]
        import datetime as _dt
        task_meta["bgimage"] = {
            "started_at": _dt.datetime.now().isoformat(),
            "video_name": req.video_name,
            "output": existing[0].name,
            "skipped": True,
        }
        return {
            "status": "ok",
            "output": existing[0].name,
            "refs": [],
            "skipped": True,
        }

    # 子プロセス（app_pipeline.py --only bgimage） を起動。
    # via_api=False で起動するため、step_bgimage は CLI 経路を走り subprocess 内で codex_imagegen.py を叩く。
    # アクティブチャンネルの per-channel 設定（persona / rival_channels / reference_image_dir）を
    # 子プロセスに見せるため APP_CHANNEL_FOLDER を明示的に env に立てる
    # （これがないと subprocess の _load_dashboard_config は per-channel をマージしない）。
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    ch_folder = (config.get("channel_folder") or "").strip()
    if ch_folder:
        env["APP_CHANNEL_FOLDER"] = ch_folder
    ch_name = (config.get("channel_name") or "").strip()
    if ch_name:
        env["APP_CHANNEL_NAME"] = ch_name
    try:
        env["APP_BGIMAGE_REFCOUNT"] = str(max(1, int(req.ref_count or 3)))
    except (TypeError, ValueError):
        env["APP_BGIMAGE_REFCOUNT"] = "3"
    try:
        env["APP_BGIMAGE_TIMEOUT_SEC"] = str(max(60, int(req.timeout_sec or 1800)))
    except (TypeError, ValueError):
        env["APP_BGIMAGE_TIMEOUT_SEC"] = "1800"
    ref_image = (req.reference_image or config.get("reference_image") or "").strip()
    if ref_image:
        env["APP_BGIMAGE_REFERENCE_IMAGE"] = ref_image
    if req.force:
        env["APP_BGIMAGE_FORCE"] = "1"
    cmd = [
        sys.executable, "-u", str(PIPELINE_SCRIPT),
        vol, "--only", "bgimage",
    ]

    task_logs["bgimage"] = []
    import datetime as _dt
    task_meta["bgimage"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "video_name": req.video_name,
        "vol": vol,
        "ref_count": int(env["APP_BGIMAGE_REFCOUNT"]),
        "timeout_sec": int(env["APP_BGIMAGE_TIMEOUT_SEC"]),
        "reference_image": env.get("APP_BGIMAGE_REFERENCE_IMAGE", ""),
        "force": bool(req.force),
        "skipped": False,
    }
    reservation = await _ensure_not_running("bgimage", "背景画像生成が既に実行中です")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        _register_active_task("bgimage", proc, reservation)
    except Exception:
        _release_task_reservation("bgimage", reservation)
        raise
    _stream_subprocess(proc, "bgimage", timeout=int(env["APP_BGIMAGE_TIMEOUT_SEC"]) + 300)

    return {
        "status": "started",
        "output": f"vol{vol}.png",
        "refs": [],  # ref 一覧は子プロセスのログから事後に確認可能
        "skipped": False,
    }


@router.get("/api/bgimage/status")
def api_bgimage_status():
    """背景画像生成の進捗 + 末尾ログを返す。`step_bgimage` の via_api 分岐がポーリングする。"""
    proc = active_tasks.get("bgimage")
    running = proc is not None and proc.returncode is None
    logs = task_logs.get("bgimage", [])
    meta = task_meta.get("bgimage", {})
    return {"running": running, "logs": logs[-200:], "meta": meta}


@router.post("/api/bgimage/stop")
def api_bgimage_stop():
    proc = active_tasks.get("bgimage")
    if proc and proc.returncode is None:
        proc.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}


# ─── step_bgimage: 参照画像フォルダ（per-channel UI 連携） ───
_REF_IMG_EXTS = ("*.jpg", "*.jpeg", "*.png", "*.webp")
# 各チャンネルルート直下の既定参照フォルダ名（app_pipeline.DEFAULT_REFERENCE_DIRNAME と一致）
DEFAULT_REFERENCE_DIRNAME = "ベンチマーク"


def _resolve_reference_dir_portable(raw: str) -> Optional[Path]:
    """reference_image_dir(raw) を「現マシン」へ移植解決して存在する Path を返す（無ければ None）。

    Google Drive のパスはマシン毎にユーザー名が違う(/Users/user_a… と /Users/user_b…)
    ので、保存値をそのまま使うと別マシンで死ぬ。以下の順で現マシンへ解決する:
      - 空なら各チャンネルルート/「ベンチマーク」を既定採用
      - 別マシンの絶対パス → 共有ドライブ marker で現マシン root へ付け替え(_resolve_to_current_host)
      - 相対パス → アクティブチャンネルフォルダ配下
    app_pipeline.step_bgimage の _resolve_ref_path と同じ規約（両 Mac でどちらも生きる）。"""
    cfg = get_dashboard_config()
    ch_raw = (cfg.get("channel_folder") or "").strip()
    ch_dir = Path(_resolve_to_current_host(ch_raw)).expanduser() if ch_raw else None
    raw = (raw or "").strip()
    candidates: list[Path] = []
    if not raw:
        if ch_dir:
            candidates.append(ch_dir / DEFAULT_REFERENCE_DIRNAME)
    else:
        candidates.append(Path(raw).expanduser())
        swapped = _resolve_to_current_host(raw)
        if swapped and swapped != raw:
            candidates.append(Path(swapped).expanduser())
        if ch_dir and not Path(raw).is_absolute():
            candidates.append(ch_dir / raw)
    for c in candidates:
        try:
            if c.is_dir():
                return c
        except Exception:
            continue
    return None


def _bgimage_reference_dir() -> Optional[Path]:
    """アクティブチャンネルの reference_image_dir を現マシンへ移植解決して返す。未設定時は
    各チャンネルルート/「ベンチマーク」を既定採用。存在しなければ None。"""
    return _resolve_reference_dir_portable(get_dashboard_config().get("reference_image_dir") or "")


def _bgimage_reference_files(p: Path) -> list[Path]:
    files: list[Path] = []
    for pat in _REF_IMG_EXTS:
        files.extend(p.glob(pat))
    # ファイル名順で安定化（プレビュー先頭表示の再現性のため）
    files.sort(key=lambda x: x.name.lower())
    return files


@router.get("/api/bgimage/reference-dir/list")
def api_bgimage_reference_dir_list(limit: int = 6):
    """アクティブチャンネルの reference_image_dir 内の画像数とサムネ先頭 N 件のファイル名を返す。
    未設定でも各チャンネルルート/「ベンチマーク」を既定採用し、2台 Mac のユーザー名差を吸収して解決する。"""
    raw = (get_dashboard_config().get("reference_image_dir") or "").strip()
    configured = bool(raw)
    p = _resolve_reference_dir_portable(raw)
    if not p:
        # 解決できなかった（既定ベンチマークも不在）。期待パスを参考表示。
        cfg = get_dashboard_config()
        ch = (cfg.get("channel_folder") or "").strip()
        expected = str(Path(_resolve_to_current_host(ch)) / DEFAULT_REFERENCE_DIRNAME) if ch else (raw or "")
        return {"ok": True, "configured": configured, "exists": False, "count": 0, "files": [],
                "path": expected, "default_used": not configured,
                "error": "ディレクトリが存在しません"}
    files = _bgimage_reference_files(p)
    return {
        "ok": True,
        "configured": configured,
        "exists": True,
        "count": len(files),
        "files": [f.name for f in files[:max(1, min(int(limit or 6), 48))]],
        "path": str(p),
        "default_used": not configured,
    }


@router.get("/api/bgimage/reference-dir/thumb/{filename:path}")
def api_bgimage_reference_dir_thumb(filename: str):
    """reference_image_dir 内の画像を返す。フォルダ外へのパス越境は拒否。"""
    p = _bgimage_reference_dir()
    if not p:
        raise HTTPException(404, "reference_image_dir 未設定 / 不在")
    # パス越境防止: realpath で resolve したものが reference_image_dir 配下であることを確認
    try:
        target = (p / filename).resolve()
        root = p.resolve()
        target.relative_to(root)
    except Exception:
        raise HTTPException(403, "範囲外のパスです")
    if not target.is_file():
        raise HTTPException(404, "ファイルが見つかりません")
    suffix = target.suffix.lower()
    media = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return FileResponse(str(target), media_type=media)


@router.post("/api/bgimage/reference-dir/dry-run")
def api_bgimage_reference_dir_dry_run(count: int = 3):
    """app_pipeline.step_bgimage と同じロジックで「どの 3 枚が選ばれるか」を返す（ファイルは作らない）。
    アクティブチャンネルの reference_image_dir を最優先、無ければフォールバック理由を返す。"""
    import random as _r
    n = max(1, min(int(count or 3), 12))
    cfg = get_dashboard_config()
    raw = (cfg.get("reference_image_dir") or "").strip()
    # 未設定でも各チャンネルルート/「ベンチマーク」を既定採用＋現マシンへ移植解決
    p = _resolve_reference_dir_portable(raw)
    src_label = "reference_image_dir" if raw else f"既定（チャンネルルート/{DEFAULT_REFERENCE_DIRNAME}）"
    result = {"ok": True, "source": None, "selected": [], "pool_size": 0, "path": "", "note": "",
              "default_used": not bool(raw)}
    if p and p.is_dir():
        pool = _bgimage_reference_files(p)
        result["path"] = str(p)
        result["pool_size"] = len(pool)
        if pool:
            pool_shuffled = pool[:]
            _r.shuffle(pool_shuffled)
            picked = pool_shuffled[:n]
            result["source"] = "reference_image_dir"
            result["selected"] = [{"name": x.name, "path": str(x)} for x in picked]
            result["note"] = f"{src_label} から {len(picked)}/{n} 枚（プール {len(pool)} 枚）: {p}"
            return result
        result["note"] = f"{src_label} に画像が 0 枚 — フォールバックします: {p}"
    else:
        ch = (cfg.get("channel_folder") or "").strip()
        expected = str(Path(_resolve_to_current_host(ch)) / DEFAULT_REFERENCE_DIRNAME) if ch else (raw or "")
        result["note"] = f"{src_label} が存在しません — フォールバックします: {expected}"
    # フォールバック先の概要だけ返す（dry-run なので実選択はしない）
    fallback = {"picked_available": False, "rival_pool_size": 0}
    try:
        from app_benchmark_thumbnail import get_picked_paths  # type: ignore
        picked = get_picked_paths(limit=n) or []
        picked = [p for p in picked if Path(p).exists()]
        fallback["picked_available"] = bool(picked)
        fallback["picked_count"] = len(picked)
    except Exception:
        fallback["picked_available"] = False
    # rival thumbs プール（CONFIG_DIR/benchmark/thumbs/<UCxxx>/*.{jpg,jpeg,png}）
    try:
        pool: list[Path] = []
        for url in (cfg.get("rival_channels") or []):
            m = re.search(r"channel/(UC[A-Za-z0-9_-]+)", str(url))
            if not m:
                continue
            ch_id = m.group(1)
            bench_dir = CONFIG_DIR / "benchmark" / "thumbs" / ch_id
            if bench_dir.exists():
                pool.extend(list(bench_dir.glob("*.jpg")) + list(bench_dir.glob("*.jpeg")) + list(bench_dir.glob("*.png")))
        fallback["rival_pool_size"] = len(pool)
    except Exception:
        pass
    result["fallback"] = fallback
    return result


@router.post("/api/codex-imagegen/suggest-prompts")
def api_codex_imagegen_suggest_prompts(req: CodexImagegenSuggestRequest):
    """CLI provider で複数の画像プロンプト案を生成して返す（1 行 1 件）。"""
    provider = (req.provider or "codex").strip().lower()
    if provider not in ("codex", "claude"):
        raise HTTPException(400, f"未対応 provider: {provider}")
    default_cli = get_suno_config().get("codex_cli" if provider == "codex" else "claude_cli") or provider
    cli_cmd = (req.cli or default_cli).strip() or default_cli
    count = max(1, min(int(req.count or 4), 12))
    instruction = (
        f"次のコンテキストから、画像生成 AI 向けの英語プロンプト案を厳密に {count} 行（1 行 1 件）出力してください。"
        " 番号・記号・前置きを含めず、各行は完結した英語プロンプトのみ。"
        " 各案はバリエーション（構図 / 時間帯 / カメラアングル / 色調）を変えてください。\n\n"
        f"コンテキスト: {req.context}"
    )
    try:
        if provider == "codex":
            text = _run_codex_text_prompt(cli_cmd, instruction, timeout=300)
        else:
            # claude provider: 共通ランナーで Claude→Codex 自動フォールバック
            from app_llm_runner import run_llm
            text = run_llm(instruction, cli_cmd=cli_cmd, timeout=180, label="image-prompt-gen")
    except FileNotFoundError:
        raise HTTPException(500, f"{cli_cmd} CLI が見つかりません")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{provider} CLI タイムアウト")
    except RuntimeError as e:
        raise HTTPException(500, f"{provider} CLI 失敗: {str(e)[:300]}")
    text = (text or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    # よくある番号付き先頭（"1. ..." / "- ..."）を剥がす
    cleaned: list[str] = []
    import re as _re
    for ln in lines:
        ln2 = _re.sub(r"^\s*(?:\d+[\.\)]\s*|[-*•]\s*)", "", ln)
        if ln2:
            cleaned.append(ln2)
    if not cleaned:
        raise HTTPException(500, "プロンプト案が空でした")
    return {"ok": True, "prompts": cleaned[:count], "raw": text}


def _b64_ref_to_tmp(ref: "CodexImagegenRefImage", idx: int) -> Optional[Path]:
    """data_url を一時ファイルに保存して Path を返す。失敗時は None。"""
    import base64 as _b64
    import tempfile as _tempfile
    try:
        du = (ref.data_url or "").strip()
        if "," in du and du.startswith("data:"):
            header, _, b64body = du.partition(",")
            mime = "image/png"
            if header.startswith("data:") and ";" in header:
                mime = header.split(":", 1)[1].split(";", 1)[0] or mime
            ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                       "image/webp": ".webp", "image/gif": ".gif"}
            ext = ext_map.get(mime, ".png")
            raw = _b64.b64decode(b64body)
        else:
            raw = _b64.b64decode(du)
            ext = Path(ref.filename or "").suffix or ".png"
        safe_stem = re.sub(r"[^\w.\-]+", "_", Path(ref.filename or f"upload-{idx+1}").stem)[:48] or f"upload-{idx+1}"
        tf = _tempfile.NamedTemporaryFile(prefix=f"codex_5e_ref_{safe_stem}_", suffix=ext, delete=False)
        tf.write(raw)
        tf.close()
        return Path(tf.name)
    except Exception:
        return None


def _build_5e_vision_prompt(image_paths: list[Path], concept_hint: str,
                            channel_name: str = "", persona: str = "") -> str:
    image_block = "\n".join(f"  - {p}" for p in image_paths)
    hint_block = concept_hint.strip() or "(指定なし → 画像から最適な被写体カテゴリを推測してください)"

    # チャンネル文脈ブロック: persona があれば使い、無ければ Claude にジャンル推測を禁止する
    persona_clean = (persona or "").strip()
    name_clean = (channel_name or "").strip()
    if persona_clean:
        channel_block = (
            f"## ユーザーのチャンネル文脈（translate_to_self はここに引き寄せる）\n"
            f"- チャンネル名: {name_clean or '(不明)'}\n"
            f"- ペルソナ／コンセプト:\n{persona_clean}\n\n"
            f"translate_to_self はこの persona の文脈に翻訳してください。"
        )
        translate_rule = "translate_to_self はチャンネルの persona 文脈に合わせた具体的翻訳を記述する"
    else:
        channel_block = (
            f"## ユーザーのチャンネル文脈\n"
            f"- チャンネル名: {name_clean or '(不明)'}\n"
            f"- ペルソナ／コンセプト: **未設定**\n\n"
            f"**重要 — ジャンル推測の禁止**: persona が未設定のため、"
            f"具体的なジャンル名（jazz / lounge / lo-fi / chill / ambient / piano / "
            f"healing / sleep など）や音楽カテゴリを **絶対に勝手に推測しないでください**。"
            f"参照画像の競合サムネが BGM 系であっても、ユーザーのチャンネルが BGM 系とは限りません。"
        )
        translate_rule = (
            "translate_to_self はジャンル中立的な汎用翻訳指針として記述する。"
            "例: 「象徴オブジェクトに置換」「色温度を自チャンネルのコア時間帯に合わせて調整」"
            "「人物を抽象シルエット化」「固有テロップを汎用ミニマル日本語1語に置換」。"
            "「Lounge Jazz BGM」「Jazz の文脈に合わせ」のような具体ジャンル名を含む文は禁止"
        )

    return f"""あなたは YouTube サムネイル分析の専門家です。
以下の参照画像を **必ず Read ツールで開いて視覚的に分析**し、要素を抽出してください。
分析を省略・推測のみで回答することは禁止です。

## 分析対象（必ず Read で読み込むこと）
{image_block}

## ユーザーが想定する被写体／文脈
{hint_block}

{channel_block}

## 抽出ルール
- 各画像を実際に見て、視覚要素を言語化してください。
- コピー目的ではありません — 「効いている抽象要素」だけを抽出。
- 「構成要素 / ライティング / あなたが視聴者の注目を引くと判断したポイント」の 3 点は重点的に抽出。
- 固有要素（人物の顔・ロゴ・商標・キャラクター・チャンネル名・特徴的な小道具・テロップ）は再現せず、
  「訴えられない程度」「元画像と同一視されない程度」に翻訳する指針として記述してください。
- {translate_rule}
- 出力はすべて自然な日本語または短い英語フレーズで。

## 出力（単一 JSON / 余計な文章なし）
```json
{{
  "subject": "被写体・主役として効いているもの（user hint があれば最優先で使用、無ければ画像から抽出）",
  "background_context": "背景とコンテキスト・空間感",
  "lighting": "ライティング・時間帯・光源方向・色温度・コントラスト",
  "style_rendering": "画風・質感・レンダリング・色調",
  "camera_composition": "カメラ視点・構図・焦点・配置パターン",
  "attention_hooks": ["クリックを誘う注目ポイント1", "..."],
  "shared_palette": ["支配色1", "支配色2"],
  "avoid": ["コピーすべきでない固有要素（顔・ロゴ・チャンネル名・テロップ等）"],
  "translate_to_self": ["元画像の要素をどう翻訳するか（ジャンル名を含めないこと）"]
}}
```"""


def _vision_to_thumbnail_axis(vobj: dict) -> dict:
    """Vision 出力 JSON を build_5element_prompts が読める形に整形。"""
    if not isinstance(vobj, dict):
        return {}
    return {
        "shared_palette": vobj.get("shared_palette"),
        "shared_composition": vobj.get("camera_composition"),
        "shared_atmosphere": vobj.get("background_context"),
        "element_extraction": {
            "subjects": [vobj.get("subject")] if vobj.get("subject") else [],
            "composition": vobj.get("camera_composition"),
            "lighting": vobj.get("lighting"),
            "color_palette": vobj.get("shared_palette"),
            "atmosphere": vobj.get("background_context"),
        },
        "recommendation_for_self": {
            "keep": [vobj.get("subject")] if vobj.get("subject") else [],
            "transform": vobj.get("translate_to_self") or [],
            "vibe_one_line": vobj.get("background_context") or "",
        },
        "viewer_hooks": vobj.get("attention_hooks") or [],
        "avoid": vobj.get("avoid") or [],
        "adaptation_hints": {
            "transform": vobj.get("translate_to_self") or [],
            "avoid": vobj.get("avoid") or [],
        },
    }


@router.post("/api/codex-imagegen/build-5element")
def api_codex_imagegen_build_5element(req: CodexImagegenBuild5ERequest):
    """参照画像（アップロード + benchmark picked）を **Claude Vision でリアルタイム分析**し、
    その結果から 5要素プロンプトを N 件構築する。
    参照画像が 1 枚もない場合のみ thumbnail.json のキャッシュ aggregate にフォールバック。"""
    import app_image_prompt as _ip
    import app_benchmark_thumbnail as _bt

    # ─── 1) 参照画像を集める（アップロード優先 → picked 追加） ───────
    ref_paths: list[Path] = []
    uploaded_tmps: list[Path] = []
    ref_names: list[str] = []

    if req.reference_images_b64:
        for idx, ref in enumerate(req.reference_images_b64[:6]):  # Vision 分析の現実的キャップ
            tmp = _b64_ref_to_tmp(ref, idx)
            if tmp:
                ref_paths.append(tmp)
                uploaded_tmps.append(tmp)
                ref_names.append(ref.filename or tmp.name)

    if req.use_benchmark_picked:
        try:
            for p in (_bt.get_picked_paths(limit=4) or []):
                pp = Path(p)
                if pp.exists() and pp not in ref_paths:
                    ref_paths.append(pp)
                    ref_names.append(pp.name)
        except Exception:
            pass

    thumbnail_axis: dict = {}
    source: str = ""
    vision_raw: str = ""
    vision_obj: dict = {}
    vision_error: str = ""

    # ─── 2) 参照画像があれば Claude Vision でリアルタイム分析（必須実行） ───
    if ref_paths:
        suno_cfg = get_suno_config()
        cli_cmd = (suno_cfg.get("claude_cli") or "claude").strip()
        # チャンネル文脈を渡す（persona が空なら Vision にジャンル推測を禁止させる）
        dash_cfg = get_dashboard_config()
        channel_name = (dash_cfg.get("channel_name") or "").strip()
        persona = (dash_cfg.get("persona") or "").strip()
        prompt = _build_5e_vision_prompt(
            ref_paths,
            (req.concept_hint or "").strip(),
            channel_name=channel_name,
            persona=persona,
        )
        try:
            vision_raw = _bt._run_claude_vision(cli_cmd, prompt, ref_paths, timeout=300)
            vision_obj = _bt._extract_json(vision_raw) or {}
            if vision_obj:
                thumbnail_axis = _vision_to_thumbnail_axis(vision_obj)
                source = f"vision_realtime({len(ref_paths)} images)"
            else:
                vision_error = "JSON 抽出失敗"
        except FileNotFoundError:
            vision_error = f"{cli_cmd} CLI が見つかりません"
        except RuntimeError as e:
            vision_error = str(e)[:200]
        except Exception as e:
            vision_error = f"{type(e).__name__}: {str(e)[:160]}"

    # ─── 3) Vision が無理だった場合のフォールバック（キャッシュ aggregate） ───
    if not thumbnail_axis:
        try:
            thumb_cache = _bt.load_cache() or {}
        except Exception:
            thumb_cache = {}
        analysis = (thumb_cache.get("analysis") or {}) if isinstance(thumb_cache, dict) else {}
        agg = analysis.get("aggregate") if isinstance(analysis, dict) else None
        if isinstance(agg, dict) and agg:
            notes = agg.get("gpt_image2_prompt_notes") or {}
            thumbnail_axis = {
                "shared_palette": agg.get("shared_palette"),
                "shared_composition": agg.get("shared_composition"),
                "shared_atmosphere": agg.get("vibe_one_line"),
                "recommendation_for_self": agg.get("recommendation_for_self") or {},
                "element_extraction": {
                    "subjects": [notes.get("subject")] if notes.get("subject") else [],
                    "composition": notes.get("camera_composition"),
                    "lighting": notes.get("lighting"),
                    "color_palette": agg.get("shared_palette"),
                    "atmosphere": notes.get("background_context"),
                },
                "avoid": notes.get("avoid"),
            }
            source = source or "thumbnail_aggregate(cache_fallback)"
        else:
            per = analysis.get("per_channel") if isinstance(analysis, dict) else None
            if isinstance(per, dict) and per:
                first = next(iter(per.values()), None) or {}
                br = first.get("five_element_breakdown") or {}
                thumbnail_axis = {
                    "shared_palette": first.get("common_palette"),
                    "shared_composition": first.get("common_composition"),
                    "element_extraction": {
                        "subjects": first.get("common_subjects") or [],
                        "composition": br.get("camera_composition"),
                        "lighting": br.get("lighting"),
                        "color_palette": first.get("common_palette"),
                        "atmosphere": br.get("background_context"),
                    },
                    "viewer_hooks": first.get("viewer_hooks"),
                    "avoid": first.get("avoid"),
                }
                source = source or "thumbnail_per_channel(cache_fallback)"
            else:
                source = source or "default_fallback"

    # ─── 4) 競合分析キャッシュ（任意・visual_direction だけ薄く参照） ───
    competitor_analysis: dict = {}
    try:
        import app_competitor as _ac
        competitor_analysis = _ac.load_cache() or {}
    except Exception:
        competitor_analysis = {}

    # ─── 5) 出力フォルダの既存ファイルをスキャンして v 番号オフセットを決定 ───
    prefix_for_scan = (req.filename_prefix or req.video_name or "").strip()
    start_index = 1
    existing_count = 0
    scanned_dir = ""
    if prefix_for_scan and req.video_name:
        try:
            cfg = get_dashboard_config()
            image_dir = resolve_video_folder(req.video_name) / "Image"
            if image_dir.is_dir():
                # prefix-v{NUM}.{ext} または prefix-v{NUM}-{m}.{ext} (n_per_prompt > 1 で生成された連番)
                # prefix の末尾は app_image_prompt 側で 20 文字に切られるので、同じく切ったものでマッチ
                truncated = prefix_for_scan[:20].rstrip("-") or "image"
                pat = re.compile(
                    rf"^{re.escape(truncated)}-v(\d+)(?:-\d+)?\.(?:png|jpe?g|webp)$",
                    re.IGNORECASE,
                )
                max_v = 0
                for f in image_dir.iterdir():
                    if not f.is_file():
                        continue
                    m = pat.match(f.name)
                    if m:
                        existing_count += 1
                        try:
                            max_v = max(max_v, int(m.group(1)))
                        except ValueError:
                            pass
                start_index = max_v + 1 if max_v > 0 else 1
                scanned_dir = str(image_dir)
        except Exception:
            pass

    # ─── 6) 5要素プロンプト N 件をビルド ───
    n = max(1, min(int(req.n or 4), 8))
    items = _ip.build_5element_prompts(
        thumbnail_axis=thumbnail_axis,
        competitor_analysis=competitor_analysis,
        concept_hint=(req.concept_hint or "").strip(),
        n=n,
        include_text_overlay=bool(req.include_text_overlay),
        filename_prefix=prefix_for_scan,
        start_index=start_index,
        channel_folder=str(Path(get_dashboard_config().get("channel_folder", "")).expanduser()) if get_dashboard_config().get("channel_folder") else "",
        channel_id=(get_dashboard_config().get("channel_name") or ""),
    )

    # ─── 7) 一時ファイル後始末 ───
    for t in uploaded_tmps:
        try:
            t.unlink()
        except Exception:
            pass

    return {
        "ok": True,
        "items": items,
        "as_lines": "\n\n".join(f"{it['prompt']}::{it['filename']}" for it in items),
        "reference_images": ref_names,
        "reference_count": len(ref_paths),
        "source": source,
        "start_index": start_index,
        "existing_count": existing_count,
        "scanned_dir": scanned_dir,
        "vision_raw": vision_raw[:4000] if vision_raw else "",
        "vision_extracted": vision_obj,
        "vision_error": vision_error,
    }


# ─── API: チャンネル動画横断・サムネ一括生成 ─────────

class ChannelThumbnailPlanRequest(BaseModel):
    video_names: List[str]
    max_competitors_per_video: Optional[int] = 4
    use_self_stats: Optional[bool] = True       # Phase B: 自動画 YouTube Data API 数値を取得


class ChannelThumbnailStartRequest(BaseModel):
    video_names: List[str]
    provider: Optional[str] = "codex"           # "codex" | "flow"
    n_per_video: Optional[int] = 4
    max_competitors_per_video: Optional[int] = 4
    use_self_stats: Optional[bool] = True
    concept_hint_override: Optional[str] = ""
    aspect: Optional[str] = "16:9"
    quality: Optional[str] = "medium"
    include_text_overlay: Optional[bool] = False
    background: Optional[str] = "auto"
    output_format: Optional[str] = "png"
    timeout_sec: Optional[int] = 900
    max_parallel: Optional[int] = 4
    retry_failed_only: Optional[bool] = False   # Phase C: 直前 run の失敗のみ再実行
    # 自動承認設定 (.md 仕様書 §7.4, §10)
    approval_mode: Optional[str] = "conditional"   # "manual" | "conditional" | "auto"
    score_threshold: Optional[int] = 80
    ctr_threshold: Optional[float] = 7.0
    similarity_max: Optional[int] = 25
    auto_score: Optional[bool] = True              # 生成完了後に Vision スコアリングを自動実行


class ThumbnailApproveRequest(BaseModel):
    video_name: str
    filename: str
    status: str                                     # "adopted" | "rejected" | "needs_review"
    reason: Optional[str] = ""


class ThumbnailSettingsRequest(BaseModel):
    video_name: str
    approval_mode: Optional[str] = None
    score_threshold: Optional[int] = None
    ctr_threshold: Optional[float] = None
    similarity_max: Optional[int] = None


class ThumbnailRescoreRequest(BaseModel):
    video_name: str
    filenames: Optional[List[str]] = None           # 指定なら指定分のみ。None なら全件
    approval_mode: Optional[str] = None             # 同時に承認モード上書き
    score_threshold: Optional[int] = None
    ctr_threshold: Optional[float] = None
    similarity_max: Optional[int] = None


def _ct_collect_video_context(video_name: str) -> Optional[dict]:
    """video_name から動画ディレクトリと context を集める。"""
    import app_channel_thumbnail as _ct
    try:
        video_dir = resolve_video_folder(video_name)
    except HTTPException:
        return None
    return _ct.read_video_context(video_dir)


def _ct_fetch_self_stats(video_ids: list[str]) -> dict:
    """video_id → YouTube Data API statistics の dict を返す。"""
    if not video_ids:
        return {}
    try:
        import app_competitor as _comp
        api_key = _comp._resolve_youtube_api_key()
        if not api_key:
            return {}
        details = _comp._fetch_video_details_by_key(video_ids, api_key)
        return {d.get("video_id"): d for d in details if d.get("video_id")}
    except Exception:
        return {}


def _ct_build_plan(video_names: list[str], *,
                   max_competitors: int = 4,
                   use_self_stats: bool = True) -> dict:
    """plan: video_names ごとに matched competitors を計算して返却（dry-run 用）。"""
    import app_channel_thumbnail as _ct
    import app_benchmark_thumbnail as _bt
    bench_cache = _bt.load_cache() or {}

    # Phase B: 自動画の数値を一括取得
    self_stats_map: dict = {}
    if use_self_stats:
        contexts = []
        for vn in video_names:
            c = _ct_collect_video_context(vn)
            if c:
                contexts.append(c)
        vids = [c["video_id"] for c in contexts if c.get("video_id")]
        self_stats_map = _ct_fetch_self_stats(vids)

    plan: list[dict] = []
    for vn in video_names:
        ctx = _ct_collect_video_context(vn)
        if not ctx:
            plan.append({
                "video_name": vn, "ok": False, "error": "video folder not found",
                "matched_competitors": [],
            })
            continue
        matched = _ct.match_competitors_by_keyword(
            self_title=ctx.get("title_extended") or ctx["title"],
            self_concept=ctx["concept"],
            benchmark_cache=bench_cache,
            top_n=max(1, min(int(max_competitors), 8)),
        )
        self_st = self_stats_map.get(ctx["video_id"], {}) if ctx.get("video_id") else {}
        peer_avg = _ct.average_views(matched)
        plan.append({
            "video_name": vn,
            "ok": True,
            "title": ctx["title"],
            "has_title": ctx.get("has_title", False),
            "has_concept": ctx.get("has_concept", False),
            "concept_excerpt": (ctx["concept"] or "")[:200],
            "tags_excerpt": (ctx.get("tags") or "")[:200],
            "video_id": ctx.get("video_id") or "",
            "self_stats": self_st,
            "matched_competitors": matched,
            "matched_count": len(matched),
            "peer_avg_views": int(peer_avg) if peer_avg else 0,
            "fallback_to_aggregate": len(matched) == 0,
        })
    return {"plan": plan, "total": len(plan)}


@router.post("/api/channel-thumbnail/plan")
def api_channel_thumbnail_plan(req: ChannelThumbnailPlanRequest):
    """dry-run: 選択動画ごとの matched_competitors を返却。UI で目視確認用。"""
    if not req.video_names:
        raise HTTPException(400, "video_names が空です")
    # benchmark cache の健全性
    try:
        import app_benchmark_thumbnail as _bt
        bc = _bt.load_cache() or {}
        chs = bc.get("channels") or []
        any_thumb = any((ch.get("thumbnails") or []) for ch in chs)
        if not any_thumb:
            raise HTTPException(409, "benchmark/thumbnail.json が空です。先にベンチマーク取込み + サムネ DL を実行してください。")
    except HTTPException:
        raise
    except Exception:
        pass
    video_names = [validate_video_name(v) for v in req.video_names]
    return _ct_build_plan(
        video_names,
        max_competitors=req.max_competitors_per_video or 4,
        use_self_stats=bool(req.use_self_stats),
    )


@router.post("/api/channel-thumbnail/start")
async def api_channel_thumbnail_start(req: ChannelThumbnailStartRequest):
    """選択動画群を直列で Vision 分析 → 5要素プロンプト → Codex/Flow 生成。"""
    import app_channel_thumbnail as _ct
    import app_benchmark_thumbnail as _bt
    import app_image_prompt as _ip

    if not req.video_names:
        raise HTTPException(400, "video_names が空です")
    provider = "codex"  # D8: Flow/MJ 撤去により codex 一本化

    cfg = get_dashboard_config()
    ch_folder = Path(cfg.get("channel_folder", "")).expanduser()
    if not ch_folder.is_dir():
        raise HTTPException(500, f"channel_folder が無効です: {ch_folder}")

    # benchmark 健全性
    bench_cache = _bt.load_cache() or {}
    if not any((ch.get("thumbnails") or []) for ch in (bench_cache.get("channels") or [])):
        raise HTTPException(409, "benchmark/thumbnail.json が空です。先にベンチマーク取込み + サムネ DL を実行してください。")

    # 再実行モード: 直前 run のエラー動画だけに絞る
    targets = [validate_video_name(v) for v in req.video_names]
    if req.retry_failed_only:
        prev_meta = task_meta.get("channel_thumbnail") or {}
        prev_errs = [e.get("video_name") for e in (prev_meta.get("errors") or [])]
        targets = [v for v in targets if v in prev_errs]
        if not targets:
            raise HTTPException(400, "再実行対象（直前エラー）がありません")

    import datetime as _dt
    task_logs["channel_thumbnail"] = []
    task_meta["channel_thumbnail"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "provider": provider,
        "total": len(targets),
        "done": 0,
        "current": "",
        "errors": [],
        "succeeded": [],
        "stop_requested": False,
        "params": {
            "n_per_video": req.n_per_video,
            "aspect": req.aspect,
            "quality": req.quality,
            "max_competitors": req.max_competitors_per_video,
            "use_self_stats": req.use_self_stats,
        },
    }

    async def _run_channel_thumbnail():
        meta = task_meta["channel_thumbnail"]
        logs = task_logs["channel_thumbnail"]
        # Phase B: 自動画 stats を一括 fetch
        self_stats_map: dict = {}
        if req.use_self_stats:
            try:
                ctxs = []
                for vn in targets:
                    c = _ct_collect_video_context(vn)
                    if c:
                        ctxs.append(c)
                vids = [c["video_id"] for c in ctxs if c.get("video_id")]
                self_stats_map = _ct_fetch_self_stats(vids)
                if self_stats_map:
                    logs.append(f" YouTube Data API で {len(self_stats_map)} 件の自動画 stats を取得")
                elif vids:
                    logs.append(f"⚠ self_stats 取得失敗（YouTube API key 未設定？）— concept のみで継続")
            except Exception as e:
                logs.append(f"⚠ self_stats 取得例外: {e}")

        # Vision プロンプト用の persona / channel_name
        channel_name = (cfg.get("channel_name") or "").strip()
        persona = (cfg.get("persona") or "").strip()
        cli_cmd = (get_suno_config().get("claude_cli") or "claude").strip()

        for idx, vn in enumerate(targets, 1):
            if meta.get("stop_requested"):
                logs.append(f" 停止要求により中断（{idx-1}/{len(targets)} 完了）")
                break
            meta["current"] = vn
            logs.append(f"\n=== [{idx}/{len(targets)}] {vn} ===")

            ctx = _ct_collect_video_context(vn)
            if not ctx:
                msg = "video folder not found"
                logs.append(f"❌ {msg}")
                meta["errors"].append({"video_name": vn, "error": msg})
                meta["done"] = idx
                continue

            try:
                # 1) 競合マッチング (title_extended = title + tags でトークン語彙拡張)
                matched = _ct.match_competitors_by_keyword(
                    self_title=ctx.get("title_extended") or ctx["title"],
                    self_concept=ctx["concept"],
                    benchmark_cache=bench_cache,
                    top_n=max(1, min(int(req.max_competitors_per_video or 4), 8)),
                )
                logs.append(f" matched: {len(matched)} 件 " +
                            (", ".join(f"{m['channelName']}:{m['title'][:30]}" for m in matched[:3]) or "(0 件 → aggregate fallback)"))

                # 2) 参照画像 paths
                ref_paths = _ct.matched_local_paths(matched)

                # 3) concept_hint 構築（Phase B 数値統合）
                peer_avg = _ct.average_views(matched)
                self_st = self_stats_map.get(ctx.get("video_id") or "", {})
                concept_hint = _ct.build_per_video_concept_hint(
                    video_title=ctx["title"],
                    video_concept=ctx["concept"],
                    self_stats=self_st,
                    peer_avg_views=peer_avg,
                    concept_hint_override=(req.concept_hint_override or "").strip(),
                )

                # 4) Vision 分析（参照画像があるとき）
                thumbnail_axis: dict = {}
                vision_obj: dict = {}
                if ref_paths:
                    vprompt = _build_5e_vision_prompt(
                        ref_paths, concept_hint,
                        channel_name=channel_name, persona=persona,
                    )
                    try:
                        vraw = _bt._run_claude_vision(cli_cmd, vprompt, ref_paths, timeout=300)
                        vision_obj = _bt._extract_json(vraw) or {}
                        if vision_obj:
                            thumbnail_axis = _vision_to_thumbnail_axis(vision_obj)
                            logs.append(f" Vision 抽出: subject={(vision_obj.get('subject','') or '')[:70]}")
                            # Phase C: attention_hooks を Viewer resonance として強調する hint を追加
                            hooks = vision_obj.get("attention_hooks") or []
                            if isinstance(hooks, list) and hooks:
                                thumbnail_axis.setdefault("viewer_hooks", hooks)
                        else:
                            logs.append("⚠ Vision JSON 抽出失敗 → aggregate fallback")
                    except Exception as e:
                        logs.append(f"⚠ Vision 失敗 ({type(e).__name__}: {str(e)[:120]}) → aggregate fallback")
                else:
                    logs.append("⚠ 参照画像 0 → benchmark.aggregate を使用")

                # 5) aggregate fallback
                if not thumbnail_axis:
                    agg = (bench_cache.get("analysis") or {}).get("aggregate") or {}
                    if isinstance(agg, dict) and agg:
                        notes = agg.get("gpt_image2_prompt_notes") or {}
                        thumbnail_axis = {
                            "shared_palette": agg.get("shared_palette"),
                            "shared_composition": agg.get("shared_composition"),
                            "shared_atmosphere": agg.get("vibe_one_line"),
                            "recommendation_for_self": agg.get("recommendation_for_self") or {},
                            "element_extraction": {
                                "subjects": [notes.get("subject")] if notes.get("subject") else [],
                                "composition": notes.get("camera_composition"),
                                "lighting": notes.get("lighting"),
                                "color_palette": agg.get("shared_palette"),
                                "atmosphere": notes.get("background_context"),
                            },
                            "avoid": notes.get("avoid"),
                        }

                # 6) start_index 自動算出 + 5要素プロンプト N 件
                image_dir = ch_folder / vn / "Image"
                image_dir.mkdir(parents=True, exist_ok=True)
                start_idx, existing = _ct.scan_start_index(image_dir, vn)
                if existing > 0:
                    logs.append(f" 既存 {existing} 枚 → v{start_idx} から生成")

                n_per_video = max(1, min(int(req.n_per_video or 4), 8))
                items = _ip.build_5element_prompts(
                    thumbnail_axis=thumbnail_axis,
                    competitor_analysis={},
                    concept_hint=concept_hint,
                    n=n_per_video,
                    include_text_overlay=bool(req.include_text_overlay),
                    filename_prefix=vn,
                    start_index=start_idx,
                    channel_folder=str(ch_folder),
                    channel_id=channel_name,
                )

                # 7) サブプロセスキック (Codex 一本化・D8 で Flow/MJ 撤去)
                if provider == "codex":
                    prompts_text = "\n\n".join(f"{it['prompt']}::{it['filename']}" for it in items)
                    aspect = (req.aspect or "16:9").strip()
                    quality = (req.quality or "medium").strip()
                    background = (req.background or "auto").strip()
                    output_format = (req.output_format or "png").strip()
                    parallel = max(1, min(4, int(req.max_parallel or 4)))
                    cmd = [
                        sys.executable, "-u", str(CODEX_IMAGEGEN_SCRIPT),
                        "--output-dir", str(image_dir),
                        "--max-parallel", str(parallel),
                        "--timeout", str(max(60, int(req.timeout_sec or 900))),
                        "--aspect", aspect,
                        "--quality", quality,
                        "--background", background,
                        "--output-format", output_format,
                    ]
                    meta_path = image_dir / f".image_generation_meta_{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}.json"
                    meta_map = {
                        it["filename"]: {
                            **(it.get("prompt_meta") or {}),
                            "video_name": vn,
                            "image_kind": "channel_thumbnail",
                            "matched_competitors": [
                                {"channelName": m.get("channelName"),
                                 "videoId": m.get("videoId"),
                                 "match_score": m.get("match_score"),
                                 "viewCount": m.get("viewCount")}
                                for m in matched[:4]
                            ],
                        }
                        for it in items
                    }
                    try:
                        meta_path.write_text(json.dumps(meta_map, ensure_ascii=False, indent=2), encoding="utf-8")
                        cmd += ["--generation-meta-json", str(meta_path)]
                    except Exception:
                        pass
                    for rp in ref_paths[:4]:
                        cmd += ["--reference-image", str(rp)]
                    image_reservation = _reserve_task_sync(
                        "codex_imagegen", "Codex 画像生成が既に実行中です"
                    )
                    try:
                        sub = subprocess.Popen(
                            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                        )
                        _register_active_task("codex_imagegen", sub, image_reservation)
                    except Exception:
                        _release_task_reservation("codex_imagegen", image_reservation)
                        raise
                    task_meta["codex_imagegen"] = {
                        "started_at": _dt.datetime.now().isoformat(),
                        "video_name": vn,
                        "output_dir": str(image_dir),
                        "prompt_count": len(items),
                        "max_parallel": parallel,
                        "aspect_ratio": aspect,
                        "quality": quality,
                        "n_per_prompt": 1,
                        "background": background,
                        "moderation": "auto",
                        "input_fidelity": "high",
                        "output_format": output_format,
                        "reference_images": [Path(p).name for p in ref_paths[:4]],
                        "prompt_modules": meta_map,
                    }
                    task_logs["codex_imagegen"] = []
                    try:
                        sub.stdin.write(prompts_text + "\n")
                        sub.stdin.close()
                    except Exception:
                        pass
                    image_deadline = max(120, int(req.timeout_sec or 900)) * max(1, len(items)) + 120
                    _stream_subprocess(sub, "codex_imagegen", timeout=image_deadline)
                    while sub.returncode is None:
                        if meta.get("stop_requested"):
                            try: sub.terminate()
                            except Exception: pass
                            break
                        await asyncio.sleep(2)
                    logs.append(f"  ✓ Codex rc={sub.returncode}, {len(items)} prompts (v{start_idx}〜v{start_idx+n_per_video-1})")

                # 8) 生成完了直後の Vision スコアリング + state.json 保存
                import app_thumbnail_state as _tstate
                import app_thumbnail_scoring as _tscore

                # 生成されたファイルを検出（v{start}〜v{end} の範囲、未指定 n_per_prompt は 1）
                # mtime が run 開始後のもの限定（過去ファイルを誤検出しないため）
                run_started_epoch = _dt.datetime.fromisoformat(meta["started_at"]).timestamp()
                generated_files: list[Path] = []
                for i in range(n_per_video):
                    v_num = start_idx + i
                    base = f"{vn[:20].rstrip('-') or 'image'}-v{v_num}"
                    for ext in ("png", "jpg", "jpeg", "webp"):
                        candidates = list(image_dir.glob(f"{base}.{ext}")) + \
                                     list(image_dir.glob(f"{base}-*.{ext}"))
                        for c in candidates:
                            if c.exists() and c not in generated_files:
                                try:
                                    # この run で生成されたものだけ採用
                                    if c.stat().st_mtime >= run_started_epoch - 5:
                                        generated_files.append(c)
                                except Exception:
                                    pass

                # ─── 生成 0 件は失敗扱いに (run はサブプロセス成功でもファイル出来てなければ NG) ───
                if not generated_files:
                    # task_logs["codex_imagegen"] の末尾 8 行をエラー詳細に含める（原因特定の手がかり）
                    sub_logs = (task_logs.get("codex_imagegen") or task_logs.get("flow") or [])
                    tail = "\n".join(sub_logs[-8:]) if sub_logs else ""
                    detail = (
                        f"画像が 1 枚も生成されませんでした。"
                        f"サブプロセスは正常終了しましたが、出力ファイル {vn[:20]}-v{start_idx}.* が見つかりません。"
                    )
                    logs.append(f"❌ {vn}: {detail}")
                    if tail:
                        logs.append(f"    └ サブプロセス末尾ログ:\n{tail}")
                    meta["errors"].append({"video_name": vn, "error": detail, "sub_tail": tail[:600]})
                    _tstate.append_run(image_dir, {
                        "run_id": f"RUN_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{vn}",
                        "started_at": meta["started_at"],
                        "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
                        "generated": [],
                        "approved": [],
                        "errors": [detail],
                        "sub_tail": tail[:600],
                        "provider": provider,
                    }, video_name=vn)
                    meta["done"] = idx
                    continue

                # 状態 JSON に「生成ログ」だけ先に保存（スコア前の状態）
                base_entries = []
                for i, gf in enumerate(generated_files):
                    prompt_text = items[i % len(items)]["prompt"] if items else ""
                    base_entries.append({
                        "filename": gf.name,
                        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
                        "prompt": prompt_text[:1500],
                        "status": _tstate.STATUSES[0],   # "pending"
                        "matched_competitors": [
                            {"channelName": m.get("channelName"),
                             "videoId": m.get("videoId"),
                             "match_score": m.get("match_score"),
                             "viewCount": m.get("viewCount")}
                            for m in matched[:4]
                        ],
                    })
                _tstate.upsert_many(image_dir, base_entries, video_name=vn)
                # 承認設定を state に反映
                _tstate.update_settings(
                    image_dir,
                    approval_mode=req.approval_mode,
                    score_threshold=req.score_threshold,
                    ctr_threshold=req.ctr_threshold,
                    similarity_max=req.similarity_max,
                    video_name=vn,
                )

                # Vision スコアリング (auto_score=True のとき)
                eval_entries: list[dict] = []
                if req.auto_score and generated_files:
                    logs.append(f" Vision スコアリング開始 ({len(generated_files)} 枚)…")
                    try:
                        scoring = _tscore.score_thumbnails(
                            generated_paths=generated_files,
                            competitor_paths=ref_paths[:4],
                            video_title=ctx["title"],
                            video_concept=ctx["concept"],
                            channel_name=channel_name,
                            persona=persona,
                            cli_cmd=cli_cmd,
                            timeout=300,
                        )
                        evals_raw = scoring.get("evaluations") or []
                        if scoring.get("error"):
                            logs.append(f"⚠ スコアリング失敗: {scoring['error']}")
                        # 自動承認判定
                        if evals_raw:
                            judged = _tscore.judge_auto_approval(
                                evals_raw,
                                mode=(req.approval_mode or "conditional"),
                                score_threshold=int(req.score_threshold or 80),
                                ctr_threshold=float(req.ctr_threshold or 7.0),
                                similarity_max=int(req.similarity_max or 25),
                            )
                            # filename ベースで base_entries にマージ
                            judged_by_name = {j["filename"]: j for j in judged}
                            for be in base_entries:
                                j = judged_by_name.get(be["filename"])
                                if j:
                                    be.update({
                                        "score_total": j.get("score_total"),
                                        "score_breakdown": j.get("score_breakdown") or {},
                                        "ctr_predict": j.get("ctr_predict"),
                                        "similarity_to_competitors": j.get("similarity"),
                                        "status": j.get("status"),
                                        "approval_reason": j.get("approval_reason"),
                                        "evaluation_comment": j.get("comment", ""),
                                        "strengths": j.get("strengths", []),
                                        "weaknesses": j.get("weaknesses", []),
                                        "evaluated_at": j.get("evaluated_at"),
                                    })
                                    eval_entries.append(be)
                            _tstate.upsert_many(image_dir, base_entries, video_name=vn)
                            counts = _tscore.status_counts(judged)
                            best = _tscore.best_of(judged)
                            logs.append(
                                f" スコア: 自動承認 {counts.get('auto_approved',0)} / "
                                f"要確認 {counts.get('needs_review',0)} / "
                                f"最高 {best.get('score_total',0) if best else 0}/100 ({best.get('filename','') if best else '-'})"
                            )
                    except Exception as e:
                        logs.append(f"⚠ スコアリング例外: {type(e).__name__}: {str(e)[:160]}")

                # run_history 追記
                run_id = f"RUN_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{vn}"
                _tstate.append_run(image_dir, {
                    "run_id": run_id,
                    "started_at": meta["started_at"],
                    "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
                    "generated": [p.name for p in generated_files],
                    "approved": [e["filename"] for e in eval_entries if e.get("status") == "auto_approved"],
                    "errors": [],
                    "provider": provider,
                }, video_name=vn)

                meta["succeeded"].append({
                    "video_name": vn,
                    "matched_count": len(matched),
                    "start_index": start_idx,
                    "n_generated": n_per_video,
                    "n_files_detected": len(generated_files),
                    "auto_approved": sum(1 for e in eval_entries if e.get("status") == "auto_approved"),
                    "needs_review": sum(1 for e in eval_entries if e.get("status") == "needs_review"),
                })
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:200]}"
                logs.append(f"❌ {msg}")
                meta["errors"].append({"video_name": vn, "error": msg})

            meta["done"] = idx

        meta["current"] = ""
        logs.append(f"\n=== バッチ完了 (成功 {len(meta['succeeded'])} / 失敗 {len(meta['errors'])}) ===")

    # active_tasks に「実行中フラグ」専用のダミーを置く（subprocess.Popen ではないので _ct_done で判定）
    class _CTSentinel:
        _ct_done = False
        returncode = None
    sentinel = _CTSentinel()
    reservation = await _ensure_not_running("channel_thumbnail", "channel_thumbnail バッチが既に実行中です")
    _register_active_task("channel_thumbnail", sentinel, reservation)
    asyncio.create_task(_run_channel_thumbnail())

    # ワーカー完了で sentinel._ct_done を立てる薄いラッパー
    async def _mark_done_when_finished():
        while task_meta["channel_thumbnail"].get("done", 0) < task_meta["channel_thumbnail"].get("total", 0):
            if task_meta["channel_thumbnail"].get("stop_requested"):
                break
            await asyncio.sleep(1)
        sentinel._ct_done = True
    asyncio.create_task(_mark_done_when_finished())

    return {
        "status": "started",
        "total": len(targets),
        "provider": provider,
    }


@router.get("/api/channel-thumbnail/status")
def api_channel_thumbnail_status():
    """直列バッチの進捗を返す。"""
    return {
        "logs": task_logs.get("channel_thumbnail", [])[-300:],
        "meta": task_meta.get("channel_thumbnail", {}),
        "running": not getattr(active_tasks.get("channel_thumbnail"), "_ct_done", True),
    }


# ─── API: Midjourney (AceDataCloud) token 管理 ─────

@router.get("/api/channel-thumbnail/readiness")
def api_channel_thumbnail_readiness():
    """ワークフロー実行に必要なベンチマーク・分析データの準備状況を返す。

    返す内容:
      - benchmark_thumbnail: thumbnail.json が存在し channels[] が空でないか
      - benchmark_concept: concept.json が存在し analysis があるか
      - competitor_analysis: competitor_analysis_cache.json が存在するか
      - persona_set: dashboard_config.persona が設定済みか
      - youtube_api_key_set: YouTube Data API key (Phase B 用)
    """
    out: dict = {
        "benchmark_thumbnail": False, "benchmark_thumbnail_channels": 0,
        "benchmark_thumbnail_picked": 0,
        "benchmark_concept": False, "benchmark_concept_channels": 0,
        "competitor_analysis": False,
        "persona_set": False,
        "youtube_api_key_set": False,
        "claude_cli_set": False,
        "next_action": "",
        "ready_for_plan": False,
    }
    try:
        import app_benchmark_thumbnail as _bt
        bc = _bt.load_cache() or {}
        chs = bc.get("channels") or []
        any_thumb = any((ch.get("thumbnails") or []) for ch in chs)
        out["benchmark_thumbnail"] = bool(any_thumb)
        out["benchmark_thumbnail_channels"] = len([c for c in chs if c.get("thumbnails")])
        out["benchmark_thumbnail_picked"] = len(bc.get("picked") or [])
    except Exception:
        pass
    try:
        import app_benchmark_concept as _bc
        d = _bc.load_cache() or {}
        per = d.get("per_channel") or {}
        out["benchmark_concept"] = bool(per)
        out["benchmark_concept_channels"] = len(per)
    except Exception:
        pass
    try:
        import app_competitor as _comp_cache
        out["competitor_analysis"] = bool(_comp_cache.load_cache())
    except Exception:
        pass

    cfg = get_dashboard_config()
    out["persona_set"] = bool((cfg.get("persona") or "").strip())
    suno_cfg = get_suno_config()
    out["claude_cli_set"] = bool((suno_cfg.get("claude_cli") or "claude").strip())

    try:
        import app_competitor as _comp
        out["youtube_api_key_set"] = bool(_comp._resolve_youtube_api_key())
    except Exception:
        pass

    # 次に何をすべきかの推奨アクション
    if not out["benchmark_thumbnail"]:
        out["next_action"] = "ベンチマーク・サムネ取込み + Vision 分析 を実行してください (画面: ベンチマーク → 取り込み)"
    elif not out["competitor_analysis"]:
        out["next_action"] = "競合分析 (Claude による buzz_patterns 抽出) を実行してください"
    else:
        out["ready_for_plan"] = True
        out["next_action"] = "対象動画を選択して「生成プランを作成」"
    return out


@router.post("/api/channel-thumbnail/stop")
def api_channel_thumbnail_stop():
    """実行中バッチに停止要求 + サブプロセス terminate。"""
    meta = task_meta.get("channel_thumbnail")
    if not meta or meta.get("done", 0) >= meta.get("total", 0):
        return {"status": "not_running"}
    meta["stop_requested"] = True
    # 現在動いているサブプロセスを止める
    for key in ("codex_imagegen", "flow"):
        sub = active_tasks.get(key)
        if sub and sub.returncode is None:
            try:
                sub.terminate()
            except Exception:
                pass
    return {"status": "stop_requested"}


# ─── API: サムネ承認状態 (thumbnail_state.json) ─────

def _resolve_image_dir(video_name: str) -> Optional[Path]:
    if not video_name:
        return None
    video_dir = resolve_video_folder(video_name)
    d = video_dir / "Image"
    if not d.is_dir():
        # フォルダ未生成の場合は作る
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
    return d


@router.get("/api/thumbnail-state/{video_name}")
def api_thumbnail_state_get(video_name: str):
    """指定動画の thumbnail_state.json を返す。"""
    import app_thumbnail_state as _tstate
    image_dir = _resolve_image_dir(video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {video_name}")
    _tstate.cleanup_missing_files(image_dir)
    state = _tstate.load_state(image_dir, video_name=video_name)
    summary = _tstate.aggregate_summary(image_dir)
    return {"video_name": video_name, "state": state, "summary": summary}


@router.post("/api/thumbnail-state/approve")
def api_thumbnail_state_approve(req: ThumbnailApproveRequest):
    """1 件のサムネを採用 / 却下 / 要確認 に変更。"""
    import app_thumbnail_state as _tstate
    if req.status not in _tstate.STATUSES:
        raise HTTPException(400, f"未対応 status: {req.status}")
    image_dir = _resolve_image_dir(req.video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {req.video_name}")
    state = _tstate.set_status(image_dir, req.filename, req.status,
                                reason=req.reason or "",
                                video_name=req.video_name)
    return {"ok": True, "state": state.get("thumbnails", {}).get(req.filename, {})}


@router.post("/api/thumbnail-state/settings")
def api_thumbnail_state_settings(req: ThumbnailSettingsRequest):
    """承認設定（mode / 閾値）を保存。"""
    import app_thumbnail_state as _tstate
    image_dir = _resolve_image_dir(req.video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {req.video_name}")
    state = _tstate.update_settings(
        image_dir,
        approval_mode=req.approval_mode,
        score_threshold=req.score_threshold,
        ctr_threshold=req.ctr_threshold,
        similarity_max=req.similarity_max,
        video_name=req.video_name,
    )
    return {"ok": True, "state": {k: state.get(k) for k in
            ("approval_mode", "score_threshold", "ctr_threshold", "similarity_max")}}


@router.post("/api/thumbnail-state/rescore")
async def api_thumbnail_state_rescore(req: ThumbnailRescoreRequest):
    """既存生成済みサムネを再スコアリング（閾値変更後の再判定など）。"""
    import app_thumbnail_state as _tstate
    import app_thumbnail_scoring as _tscore
    import app_channel_thumbnail as _ct
    image_dir = _resolve_image_dir(req.video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {req.video_name}")

    state = _tstate.load_state(image_dir, video_name=req.video_name)
    all_thumbs = state.get("thumbnails", {}) or {}
    # 対象ファイル
    targets = (req.filenames or list(all_thumbs.keys()))
    targets = [t for t in targets if (image_dir / t).exists()]
    if not targets:
        raise HTTPException(400, "対象サムネがありません")

    # context / competitor 再構築
    ctx = _ct.read_video_context(image_dir.parent) or {}
    import app_benchmark_thumbnail as _bt
    bench_cache = _bt.load_cache() or {}
    matched = _ct.match_competitors_by_keyword(
        self_title=ctx.get("title", ""),
        self_concept=ctx.get("concept", ""),
        benchmark_cache=bench_cache,
        top_n=4,
    )
    ref_paths = _ct.matched_local_paths(matched)

    # チャンネル文脈
    cfg = get_dashboard_config()
    channel_name = (cfg.get("channel_name") or "").strip()
    persona = (cfg.get("persona") or "").strip()
    cli_cmd = (get_suno_config().get("claude_cli") or "claude").strip()

    # 設定を保存 (引数で上書きされていれば反映)
    _tstate.update_settings(
        image_dir,
        approval_mode=req.approval_mode,
        score_threshold=req.score_threshold,
        ctr_threshold=req.ctr_threshold,
        similarity_max=req.similarity_max,
        video_name=req.video_name,
    )
    state_now = _tstate.load_state(image_dir, video_name=req.video_name)

    # Vision 再スコアリング
    scoring = _tscore.score_thumbnails(
        generated_paths=[image_dir / t for t in targets],
        competitor_paths=ref_paths[:4],
        video_title=ctx.get("title", ""),
        video_concept=ctx.get("concept", ""),
        channel_name=channel_name,
        persona=persona,
        cli_cmd=cli_cmd,
        timeout=300,
    )
    if scoring.get("error"):
        return {"ok": False, "error": scoring["error"]}

    judged = _tscore.judge_auto_approval(
        scoring.get("evaluations") or [],
        mode=state_now.get("approval_mode") or "conditional",
        score_threshold=int(state_now.get("score_threshold") or 80),
        ctr_threshold=float(state_now.get("ctr_threshold") or 7.0),
        similarity_max=int(state_now.get("similarity_max") or 25),
    )

    # state 反映 (adopted は保持)
    new_entries = []
    for j in judged:
        fn = j["filename"]
        cur = all_thumbs.get(fn, {}) or {}
        # 既に手動で adopted されていたら status は変えない
        new_status = cur.get("status") if cur.get("status") in ("adopted", "rejected") else j.get("status")
        new_entries.append({
            "filename": fn,
            "score_total": j.get("score_total"),
            "score_breakdown": j.get("score_breakdown") or {},
            "ctr_predict": j.get("ctr_predict"),
            "similarity_to_competitors": j.get("similarity"),
            "evaluation_comment": j.get("comment", ""),
            "strengths": j.get("strengths", []),
            "weaknesses": j.get("weaknesses", []),
            "evaluated_at": j.get("evaluated_at"),
            "approval_reason": j.get("approval_reason"),
            "status": new_status,
        })
    _tstate.upsert_many(image_dir, new_entries, video_name=req.video_name)
    return {
        "ok": True,
        "rescored": len(new_entries),
        "evaluations": new_entries,
        "summary": _tstate.aggregate_summary(image_dir),
    }


@router.get("/api/thumbnail-state/export-csv/{video_name}")
def api_thumbnail_state_export_csv(video_name: str):
    """state を CSV で出力（採用・候補・スコア一覧）。"""
    import app_thumbnail_state as _tstate
    import csv
    import io
    image_dir = _resolve_image_dir(video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {video_name}")
    state = _tstate.load_state(image_dir, video_name=video_name)
    rows = []
    for fn, t in (state.get("thumbnails") or {}).items():
        sb = t.get("score_breakdown") or {}
        rows.append({
            "filename": fn,
            "status": t.get("status", ""),
            "score_total": t.get("score_total", ""),
            "concept_fit": sb.get("concept_fit", ""),
            "trend_fit": sb.get("trend_fit", ""),
            "competitor_diff": sb.get("competitor_diff", ""),
            "past_perf": sb.get("past_perf", ""),
            "ctr_predict": t.get("ctr_predict", ""),
            "similarity": t.get("similarity_to_competitors", ""),
            "evaluation_comment": t.get("evaluation_comment", ""),
            "approval_reason": t.get("approval_reason", ""),
            "evaluated_at": t.get("evaluated_at", ""),
            "generated_at": t.get("generated_at", ""),
        })
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    csv_text = buf.getvalue()
    from fastapi.responses import Response
    safe = re.sub(r"[^\w\-.]+", "_", video_name)[:60]
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="thumbnail_state_{safe}.csv"'},
    )


# ─── API: シリーズ画像案（ベンチマーク分析駆動） ─────

class SeriesProposeRequest(BaseModel):
    count: Optional[int] = 8


class SeriesGenerateRequest(BaseModel):
    ids: List[str]                          # 提案 id（filename_slug）の配列
    provider: str = "flow"                  # "flow" | "codex"
    count_per_proposal: Optional[int] = 4   # Flow x4 / Codex は本数の目安
    aspect: Optional[str] = "16:9"
    headless: Optional[bool] = False        # Flow 用
    timeout_sec: Optional[int] = 900        # Codex 用
    max_parallel: Optional[int] = 4         # Codex 用
    use_benchmark_picked: Optional[bool] = False  # Flow 用: picked された競合サムネを参照画像として渡す


@router.post("/api/series/propose")
def api_series_propose(req: SeriesProposeRequest):
    """ベンチマーク分析 + 既存動画一覧 → Claude CLI で次に作るべき画像案 N 件を生成 → キャッシュ。"""
    import app_series as _ser

    config = get_dashboard_config()
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    # 既存動画一覧（重複回避用）
    existing: list[dict] = []
    try:
        ch_folder = Path(config.get("channel_folder", "")).expanduser()
        if ch_folder.is_dir():
            for f in sorted(ch_folder.iterdir()):
                if not f.is_dir():
                    continue
                if not re.match(r"^\d+_", f.name):
                    continue
                title = ""
                tf = f / "youtube_title.txt"
                if tf.exists():
                    try:
                        title = tf.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass
                existing.append({"name": f.name, "title": title})
    except Exception:
        existing = []

    try:
        result = _ser.propose_series(
            cli_cmd=cli_cmd,
            count=max(3, min(int(req.count or 8), 16)),
            persona=config.get("persona", ""),
            channel_name=config.get("channel_name", ""),
            existing_videos=existing,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    import datetime as _dt
    payload = {
        "proposals": result.get("proposals", []),
        "generated_at": _dt.datetime.now().isoformat(),
        "channel_name": config.get("channel_name", ""),
        "based_on": "competitor_analysis_cache",
    }
    _ser.save_proposals_cache(payload)
    return {"status": "ok", **payload}


@router.get("/api/series/proposals")
def api_series_proposals():
    """キャッシュ済みのシリーズ画像案を返す。"""
    import app_series as _ser
    return _ser.load_proposals_cache()


@router.delete("/api/series/proposals")
def api_series_proposals_clear():
    """キャッシュをクリア。"""
    import app_series as _ser
    _ser.save_proposals_cache({"proposals": [], "generated_at": "", "channel_name": ""})
    return {"status": "ok"}


@router.delete("/api/series/proposals/{proposal_id}")
def api_series_proposal_delete(proposal_id: str):
    """提案 1 件を削除。"""
    import app_series as _ser
    cache = _ser.load_proposals_cache()
    cache["proposals"] = [p for p in cache.get("proposals", []) if p.get("id") != proposal_id]
    _ser.save_proposals_cache(cache)
    return {"status": "ok", "remaining": len(cache["proposals"])}


@router.post("/api/series/generate")
async def api_series_generate(req: SeriesGenerateRequest):
    """選択された提案を codex で順次生成（D8: Flow 撤去により codex 一本化）。

    保存先: <channel_folder>/_series_drafts/{slug}/Image/
    Codex は内部で並列なので 1 提案ずつ叩けば十分。
    """
    import app_series as _ser

    if not req.ids:
        raise HTTPException(400, "ids が空です")
    provider = "codex"  # D8: Flow 撤去により codex 一本化

    cache = _ser.load_proposals_cache()
    proposals_by_id = {p.get("id"): p for p in cache.get("proposals", [])}
    targets = [proposals_by_id[i] for i in req.ids if i in proposals_by_id]
    if not targets:
        raise HTTPException(404, "指定 id の提案が見つかりません")

    config = get_dashboard_config()
    ch_folder = Path(config.get("channel_folder", "")).expanduser()
    if not ch_folder.is_dir():
        raise HTTPException(500, f"channel_folder が無効です: {ch_folder}")

    # バックグラウンドで直列実行
    task_logs["series_generate"] = []
    import datetime as _dt
    task_meta["series_generate"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "provider": provider,
        "total": len(targets),
        "done": 0,
        "current": "",
        "errors": [],
    }

    async def _run_series():
        meta = task_meta["series_generate"]
        for idx, p in enumerate(targets, 1):
            slug = p.get("filename_slug") or p.get("id") or f"scene_{idx}"
            out_dir = _ser.staging_dir(ch_folder, slug)
            out_dir.mkdir(parents=True, exist_ok=True)
            meta["current"] = slug
            task_logs["series_generate"].append(f"[{idx}/{len(targets)}] {slug} → {out_dir}")

            prompt_en = p.get("image_prompt_en", "").strip()
            if not prompt_en:
                msg = f"⚠️ {slug}: image_prompt_en が空、スキップ"
                task_logs["series_generate"].append(msg)
                meta["errors"].append({"slug": slug, "error": "empty prompt"})
                continue

            try:
                if True:  # D8: Flow 撤去により codex 一本化
                    # Codex: 1 行 1 件、ファイル名サジェスト付き
                    fname_seed = slug
                    prompt_line = f"{prompt_en}::{fname_seed}"
                    aspect = (req.aspect or "16:9")
                    aspect_prefix = f"Aspect ratio {aspect} (web-optimized). "
                    prompts_text = f"{aspect_prefix}{prompt_line}\n"
                    parallel = max(1, min(4, int(req.max_parallel or 4)))
                    cmd = [sys.executable, "-u", str(CODEX_IMAGEGEN_SCRIPT),
                           "--output-dir", str(out_dir),
                           "--max-parallel", str(parallel),
                           "--timeout", str(max(60, int(req.timeout_sec or 900)))]
                    if req.use_benchmark_picked:
                        try:
                            import app_benchmark_thumbnail as _bt
                            picked_paths = _bt.get_picked_paths(limit=1)
                            if picked_paths:
                                cmd += ["--reference-image", picked_paths[0]]
                                task_logs["series_generate"].append(
                                    f" image2 reference(API): {Path(picked_paths[0]).name}"
                                )
                            else:
                                task_logs["series_generate"].append(
                                    "  ⚠ picked サムネ無し → Image2 参照画像なしで生成"
                                )
                        except Exception as e:
                            task_logs["series_generate"].append(f"  ⚠ image2 picked 参照失敗: {e}")
                    image_reservation = _reserve_task_sync(
                        "codex_imagegen", "Codex 画像生成が既に実行中です"
                    )
                    try:
                        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True, bufsize=1)
                        _register_active_task("codex_imagegen", proc, image_reservation)
                    except Exception:
                        _release_task_reservation("codex_imagegen", image_reservation)
                        raise
                    task_meta["codex_imagegen"] = {
                        "started_at": _dt.datetime.now().isoformat(),
                        "video_name": "",
                        "output_dir": str(out_dir),
                        "prompt_count": 1,
                        "max_parallel": parallel,
                        "aspect_ratio": aspect,
                    }
                    task_logs["codex_imagegen"] = []
                    try:
                        proc.stdin.write(prompts_text)
                        proc.stdin.close()
                    except Exception:
                        pass
                    _stream_subprocess(
                        proc, "codex_imagegen",
                        timeout=max(120, int(req.timeout_sec or 900)) + 120,
                    )
                    while proc.returncode is None:
                        await asyncio.sleep(2)
                    task_logs["series_generate"].append(f"  ✓ Codex rc={proc.returncode}")

                # キャッシュ更新（generated フラグ）
                cache_now = _ser.load_proposals_cache()
                for cp in cache_now.get("proposals", []):
                    if cp.get("id") == p.get("id"):
                        cp["generated"] = True
                        cp["output_dir"] = str(out_dir)
                _ser.save_proposals_cache(cache_now)

            except Exception as e:
                msg = f"❌ {slug}: {e}"
                task_logs["series_generate"].append(msg)
                meta["errors"].append({"slug": slug, "error": str(e)})

            meta["done"] = idx

        meta["current"] = ""
        task_logs["series_generate"].append(f"=== 完了 (成功 {meta['done']-len(meta['errors'])} / 失敗 {len(meta['errors'])}) ===")

    asyncio.create_task(_run_series())
    return {"status": "started", "total": len(targets), "provider": provider}


@router.get("/api/series/status")
def api_series_status():
    """直列バッチの進捗を返す。"""
    return {
        "logs": task_logs.get("series_generate", [])[-200:],
        "meta": task_meta.get("series_generate", {}),
    }

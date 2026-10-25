"""文字入れ（シーンテキスト）API。段階 1a で routers/premiere_photoshop.py から分離（Adobe 非依存の部分だけ）。"""
from fastapi import APIRouter
from app_core import *  # noqa: F401,F403  土台シンボル(stdlib/fastapi/foundation)を全取り込み

router = APIRouter()


class SceneTextGenerateRequest(BaseModel):
    video_name: Optional[str] = None         # アクティブチャンネル配下の vol フォルダ名
    video_folder: Optional[str] = None       # or 絶対パス
    image_path: Optional[str] = None         # 明示的な画像パス（指定時は video_name より優先）
    persona: Optional[str] = None            # 未指定時は per-channel config から取得


@router.post("/api/scene-text/generate")
def api_scene_text_generate(req: SceneTextGenerateRequest):
    """AI生成画像からチャンネル設定形式の英語シーンテキストを自動生成。"""
    try:
        from scene_text_generator import generate_scene_text_for_image
    except Exception as e:
        raise HTTPException(500, f"scene_text_generator のインポート失敗: {e}")

    # 画像パス解決
    image_path = req.image_path
    if not image_path:
        config = get_dashboard_config()
        folder_path = req.video_folder
        if not folder_path and req.video_name:
            folder_path = str(resolve_video_folder(req.video_name))
        if not folder_path:
            raise HTTPException(400, "image_path / video_folder / video_name のいずれかが必要")
        folder = Path(folder_path)
        # vol{N}.png を優先、無ければ image_subdir 配下の最初の画像
        cands = list(folder.glob("vol*.png")) + list(folder.glob("vol*.jpg"))
        if not cands:
            subdir = folder / (config.get("psd_image_subdir") or "image")
            if subdir.is_dir():
                cands = sorted(p for p in subdir.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        if not cands:
            raise HTTPException(404, f"画像が見つかりません: {folder}")
        image_path = str(cands[0])

    _cfg = get_dashboard_config()
    persona = req.persona
    if persona is None:
        persona = (_cfg.get("persona") or "").strip()

    try:
        text = generate_scene_text_for_image(
            image_path, persona=persona,
            tone=(_cfg.get("scene_text_tone") or ""),
            examples=_cfg.get("scene_text_examples") or [],
            forbidden_phrases=_cfg.get("scene_text_forbidden") or [],
            structure=(_cfg.get("scene_text_structure") or ""),
            case=(_cfg.get("scene_text_case") or "upper"),
            punctuation=(_cfg.get("scene_text_punctuation") or "none"),
        )
        return {"status": "ok", "scene_text": text, "image_path": image_path}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"generate-scene-text 失敗: {e}")


class SceneTextSuggestRequest(BaseModel):
    mode: str = "titles"   # "titles"（ライバルタイトル語彙・軽量） | "vision"（サムネ画像の実焼込文字・消費大）
    count: int = 6         # vision で読むサムネ枚数 / titles の参照件数上限


@router.post("/api/scene-text/suggest-from-benchmark")
def api_scene_text_suggest(req: SceneTextSuggestRequest):
    """アクティブチャンネルのベンチマーク（ライバル）から、文字入れ設定の提案を返す。

    mode=titles: ライバル動画タイトルの語彙からテーマ・トーンを推定（軽量・LLM テキスト）。
    mode=vision: ライバルサムネ画像の実際の焼込文字を読み取って抽出（Vision・消費大）。
    返却: {tone, examples:[], forbidden:[], structure}（UI が各欄に流し込み、ユーザーが選択/編集）。
    """
    import app_benchmark_thumbnail as _bt
    cfg = get_dashboard_config()
    persona = (cfg.get("persona") or "").strip()
    cli = (get_suno_config().get("claude_cli") or "claude").strip()
    mode = (req.mode or "titles").strip().lower()
    count = max(1, min(int(req.count or 6), 12))

    bc = _bt.load_cache() or {}
    items = []
    for ch in (bc.get("channels") or []):
        for t in (ch.get("thumbnails") or []):
            items.append({
                "title": (t.get("title") or "").strip(),
                "localPath": t.get("localPath") or "",
                "viewCount": int(t.get("viewCount", 0) or 0),
            })
    items.sort(key=lambda x: -x["viewCount"])   # 「効いてる」順
    if not items:
        raise HTTPException(409, "ベンチマークのサムネデータがありません。先にベンチマーク取込み + サムネ DL を実行してください。")

    json_shape = (
        '{\n'
        '  "tone": "comma-separated mood keywords (e.g. chill, lo-fi, study, cozy)",\n'
        '  "examples": ["3-6 ALL-CAPS 2-3 word sample phrases in the target register"],\n'
        '  "forbidden": ["phrases to avoid copying verbatim"],\n'
        '  "structure": "short syntax hint, e.g. verb+noun or adjective+noun"\n'
        '}'
    )

    try:
        from app_llm_runner import run_llm, run_llm_vision
        if mode == "vision":
            paths = [it["localPath"] for it in items
                     if it["localPath"] and Path(it["localPath"]).exists()][:count]
            if not paths:
                raise HTTPException(409, "ライバルサムネ画像がローカルにありません（benchmark/thumbs 未DL）。titles モードをお試しください。")
            prompt = (
                "You are designing the on-thumbnail TEXT style for a YouTube BGM channel by analyzing rival thumbnails.\n"
                f"Channel persona: {persona or '(unspecified)'}\n"
                f"For the {len(paths)} rival thumbnail images, READ the actual text burned into each image, "
                "then design a caption style that fits THIS channel and differentiates from the rivals.\n"
                "Output a JSON object exactly in this shape:\n"
                f"{json_shape}\n"
                "- examples: FRESH phrases in the same register (do NOT copy the rivals' exact text).\n"
                "- forbidden: the rivals' ACTUAL on-image phrases you read, verbatim.\n"
                "Output ONLY the JSON, nothing else."
            )
            raw = run_llm_vision(prompt, paths, cli_cmd=cli, timeout=240, label="scene-suggest-vision")
        else:
            titles = [it["title"] for it in items if it["title"]][:max(count, 20)]
            titles_joined = "\n".join(f"- {t}" for t in titles)
            prompt = (
                "You are designing the on-thumbnail TEXT style for a YouTube BGM channel by analyzing rival video titles.\n"
                f"Channel persona: {persona or '(unspecified)'}\n"
                f"Rival video titles ({len(titles)}):\n{titles_joined}\n\n"
                "Design a short English ALL-CAPS caption style that fits THIS channel and differentiates from rivals.\n"
                "Output a JSON object exactly in this shape:\n"
                f"{json_shape}\n"
                "Output ONLY the JSON, nothing else."
            )
            raw = run_llm(prompt, cli_cmd=cli, timeout=180, label="scene-suggest-titles")
        obj = _bt._extract_json(raw) or {}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"提案生成に失敗: {e}")

    def _as_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [s.strip() for s in re.split(r"[\n,/]", v) if s.strip()]
        return []

    return {
        "ok": True,
        "mode": mode,
        "source_count": len(items),
        "tone": (str(obj.get("tone") or "")).strip(),
        "examples": _as_list(obj.get("examples")),
        "forbidden": _as_list(obj.get("forbidden")),
        "structure": (str(obj.get("structure") or "")).strip(),
    }


class PhotoshopRenderDualRequest(BaseModel):
    video_name: Optional[str] = None        # アクティブチャンネル配下の vol フォルダ名
    video_folder: Optional[str] = None      # or 絶対パス
    scene_text: Optional[str] = None        # 空 or 未指定なら vision で自動生成 + scene_en.txt キャッシュ
    base_layer: Optional[str] = None        # 既定: per-channel `psd_base_layer`
    scene_text_layer: Optional[str] = None  # 既定: per-channel `psd_text_layer` → "都市名_テキスト"
    playlist_layer: Optional[str] = None    # 既定: per-channel `psd_toggle_layer` → "PLAY LIST " (末尾スペース)
    image_subdir: Optional[str] = None      # 既定: per-channel `psd_image_subdir`
    quality: int = 90
    target_width: Optional[int] = None      # 空 or 未指定なら per-channel `psd_export_width` → 1920
    target_height: Optional[int] = None     # 空 or 未指定なら per-channel `psd_export_height` → 1080
    save_psd: Optional[bool] = None         # 空 or 未指定なら True (vol 固有 PSD の編集状態を保存)
    scene_text_font: Optional[str] = None   # 空 or 未指定なら per-channel `psd_text_font`

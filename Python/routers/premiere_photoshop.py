#!/usr/bin/env python3
"""Premiere Pro / Photoshop(UXP) 連携ルータ（D9）。JSX配置/書き出し/SRT再生成、UXPパネル連携、scene-text。app_premiere/app_photoshop はハンドラ内ローカルimport、app_coreの土台シンボルを全取り込み。"""
from fastapi import APIRouter
from app_core import *  # noqa: F401,F403  土台シンボル(stdlib/fastapi/foundation)を全取り込み

router = APIRouter()


# ─── API: Premiere Pro ───
# PREMIERE_SCRIPT は app_core へ移動（premiere/render-queue 両ドメインが参照する共有定数・D9）

class PremiereRunRequest(BaseModel):
    duration: Optional[int] = None
    duration_h: Optional[int] = None
    duration_m: Optional[int] = None
    duration_s: Optional[int] = None
    auto_export: bool = False
    folder: Optional[str] = None   # 動画フォルダのパス（.prproj を自動オープン）
    video_name: Optional[str] = None  # または動画フォルダ名のみ

@router.post("/api/premiere/run")
async def api_premiere_run(req: PremiereRunRequest):
    await _ensure_not_running("premiere", "Premiere 処理が既に実行中です")
    # duration 優先順位: duration_h/m/s explicit > duration explicit > per-channel default_duration_sec > 10800
    if req.duration_h is not None or req.duration_m is not None or req.duration_s is not None:
        duration = (req.duration_h or 0) * 3600 + (req.duration_m or 0) * 60 + (req.duration_s or 0)
    elif req.duration is not None:
        duration = req.duration
    else:
        _cfg = get_dashboard_config()
        _ch_default = _cfg.get("default_duration_sec")
        duration = int(_ch_default) if isinstance(_ch_default, (int, float)) and _ch_default > 0 else 10800
    cmd = [sys.executable, str(PREMIERE_SCRIPT), "--duration", str(duration)]

    # 動画フォルダ指定がある場合は .prproj を自動で開く
    folder_path = None
    if req.folder:
        folder_path = Path(req.folder)
    elif req.video_name:
        config = get_dashboard_config()
        folder_path = Path(config["channel_folder"]) / req.video_name
    if folder_path:
        if not folder_path.exists():
            raise HTTPException(404, f"動画フォルダが存在しません: {folder_path}")
        prproj = next(iter(folder_path.glob("*vol*.prproj")), None)
        if not prproj:
            raise HTTPException(404, f".prproj が見つかりません: {folder_path}")
        cmd += ["--project", str(prproj)]

    if req.auto_export:
        cmd.append("--export")
    task_logs["premiere"] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["premiere"] = proc
    _stream_subprocess(proc, "premiere")
    return {"status": "started", "duration": duration}

@router.post("/api/premiere/export")
async def api_premiere_export():
    """書き出しのみ実行"""
    await _ensure_not_running("premiere", "Premiere 処理が既に実行中です")
    cmd = [sys.executable, str(PREMIERE_SCRIPT), "--export-only"]
    task_logs["premiere"] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["premiere"] = proc
    _stream_subprocess(proc, "premiere")
    return {"status": "started"}

@router.post("/api/premiere/regenerate-srt")
async def api_premiere_regenerate_srt():
    cmd = [sys.executable, str(PREMIERE_SCRIPT), "--regenerate-srt"]
    task_logs["premiere"] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["premiere"] = proc
    _stream_subprocess(proc, "premiere")
    return {"status": "started"}

@router.get("/api/premiere/status")
def api_premiere_status():
    proc = active_tasks.get("premiere")
    running = proc is not None and proc.returncode is None
    return {"running": running, "logs": task_logs.get("premiere", [])[-50:]}

@router.get("/api/premiere/check")
def api_premiere_check():
    """Premiere Pro 接続確認 (pymiere → AppleScript フォールバック)"""
    import subprocess as _sp
    # 1. pymiere 試行
    try:
        from pymiere import exe_utils
        is_running, pid = exe_utils.is_premiere_running()
        if not is_running:
            return {"connected": False, "reason": "Premiere Pro が起動していません"}
        from pymiere.core import eval_script
        name = eval_script('app.project.name')
        return {"connected": True, "project": name, "pid": pid, "method": "pymiere"}
    except Exception:
        pass
    # 2. ファイルポーリング方式（Premiere Link CEP パネル経由）
    proc = _sp.run(['pgrep', '-fi', 'Adobe Premiere Pro'], capture_output=True, text=True)
    if proc.returncode != 0:
        return {"connected": False, "reason": "Premiere Pro が起動していません"}
    import os as _os, time as _time, json as _json
    PING = '/tmp/pymiere_ping.txt'
    TRIGGER = '/tmp/pymiere_trigger.json'
    RESULT  = '/tmp/pymiere_result.json'
    # パネル生存確認
    if not _os.path.exists(PING) or (_time.time() - _os.path.getmtime(PING)) > 30:
        return {"connected": False, "reason": "Premiere Link が未応答です。「パネルを開く」または「Premiere を再起動」を試してください。"}
    # JSX 実行でプロジェクト名取得
    try:
        if _os.path.exists(RESULT): _os.unlink(RESULT)
        with open(TRIGGER, 'w') as f:
            _json.dump({'code': 'app.project.name || "no_project"', 'ts': _time.time()}, f)
        for _ in range(50):
            _time.sleep(0.2)
            if _os.path.exists(RESULT): break
        if _os.path.exists(RESULT):
            with open(RESULT, 'r', encoding='utf-8') as rf:
                d = _json.load(rf)
            _os.unlink(RESULT)
            name = d.get('result', 'unknown')
        else:
            name = 'unknown'
        return {"connected": True, "project": name, "method": "file_poll"}
    except Exception as e2:
        return {"connected": False, "reason": f"ファイルポーリング エラー: {e2}"}

@router.get("/api/premiere/panel-status")
def api_premiere_panel_status():
    """Premiere Link パネルの生存状態とアクティビティログを返す"""
    import os as _os, time as _t, json as _j
    PING = '/tmp/pymiere_ping.txt'
    ACTIVITY = '/tmp/pymiere_activity.json'
    alive = False
    ping_age = None
    if _os.path.exists(PING):
        ping_age = round(_t.time() - _os.path.getmtime(PING), 1)
        alive = ping_age < 30
    logs = []
    try:
        if _os.path.exists(ACTIVITY):
            with open(ACTIVITY, 'r', encoding='utf-8') as af:
                logs = _j.load(af)
    except Exception:
        pass
    return {"alive": alive, "ping_age": ping_age, "logs": logs[:15]}


@router.post("/api/premiere/reopen-panel")
def api_premiere_reopen_panel():
    """「ウィンドウ > 拡張機能 > Premiere Link」を AppleScript で自動クリックして
    パネルを開き直す。アクセシビリティ権限が必要。"""
    import sys as _sys, importlib as _il
    _sys.path.insert(0, str(SHARED_BASE / "Python"))
    try:
        _premiere_mod = _il.import_module("app_premiere")
    except Exception as e:
        raise HTTPException(500, f"app_premiere モジュール読み込み失敗: {e}")
    ok = False
    try:
        ok = bool(_premiere_mod.open_pymiere_panel())
    except Exception as e:
        raise HTTPException(500, f"パネル起動処理エラー: {e}")
    return {"ok": ok}


@router.post("/api/premiere/restart")
def api_premiere_restart():
    """Premiere Pro を安全に終了 → 再起動する。
    起動中のプロジェクトは保存ダイアログが出るため、あらかじめ保存済みであることを
    呼び出し側で確認すること。"""
    import subprocess as _sp, time as _time, os as _os
    # 実行中アプリを特定
    try:
        r = _sp.run(['osascript', '-e',
                     'tell application "System Events" to get name of every process'],
                    capture_output=True, text=True, timeout=10)
    except Exception as e:
        raise HTTPException(500, f"プロセス取得失敗: {e}")
    app_name = None
    for part in r.stdout.split(','):
        name = part.strip()
        if 'Adobe Premiere Pro' in name:
            app_name = name
            break
    if not app_name:
        return {"ok": False, "reason": "Premiere Pro が起動していません"}
    # 強制終了（保存確認なし: ハング復旧用）
    try:
        _sp.run(['pkill', '-9', '-f', 'Adobe Premiere Pro'], capture_output=True, timeout=10)
    except Exception as e:
        raise HTTPException(500, f"終了失敗: {e}")
    # 停止ファイルをクリーンアップ
    for p in ('/tmp/pymiere_trigger.json', '/tmp/pymiere_result.json', '/tmp/pymiere_ping.txt'):
        try:
            if _os.path.exists(p):
                _os.unlink(p)
        except Exception:
            pass
    # 再起動
    _time.sleep(2)
    app_path = f"/Applications/{app_name}/{app_name}.app"
    if not _os.path.exists(app_path):
        # バージョン表記違いに対応: /Applications 下から検索
        for entry in _os.listdir("/Applications"):
            if entry.startswith("Adobe Premiere Pro"):
                app_path = f"/Applications/{entry}/{entry}.app"
                if _os.path.exists(app_path):
                    break
    try:
        _sp.Popen(['open', app_path])
    except Exception as e:
        raise HTTPException(500, f"再起動失敗: {e}")
    return {"ok": True, "app": app_name, "app_path": app_path}


# ─── API: Photoshop Link (UXP) ─────────────────────────────────────────────

class PhotoshopOpenRequest(BaseModel):
    path: str

class PhotoshopSetTextRequest(BaseModel):
    layer: str
    text: str

class PhotoshopExportRequest(BaseModel):
    out_path: str
    fmt: str = "jpg"
    quality: int = 90

class PhotoshopEvalRequest(BaseModel):
    code: str
    timeout: float = 30.0

class PhotoshopReplaceSmartObjectRequest(BaseModel):
    layer: str
    image_path: str

class PhotoshopSetVisibleRequest(BaseModel):
    layer: str
    visible: bool

class PhotoshopRenderThumbsRequest(BaseModel):
    psd_path: str
    base_image: Optional[str] = None
    base_layer: str = "base"
    text_replacements: Optional[dict] = None
    toggle_layer: str = "PLAY LIST"
    out_dir: Optional[str] = None
    vol_name: Optional[str] = None
    quality: int = 90

class PhotoshopRenderForVideoRequest(BaseModel):
    """動画フォルダ（または video_name）を指定するだけで、
    チャンネル設定からレイヤー名を引いてサムネを 2 枚書き出す。"""
    video_folder: Optional[str] = None
    video_name: Optional[str] = None    # video_folder の代わりにこちらでも可
    text_replacements: Optional[dict] = None
    quality: int = 90
    # チャンネル設定の上書き（指定があれば優先）
    base_layer: Optional[str] = None
    toggle_layer: Optional[str] = None
    image_subdir: Optional[str] = None


def _import_photoshop():
    import importlib as _il
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    return _il.import_module("app_photoshop")


@router.get("/api/photoshop/check")
def api_photoshop_check():
    """Photoshop Link UXP パネルの接続状態を返す。"""
    try:
        ps = _import_photoshop()
        return ps.check_photoshop()
    except Exception as e:
        return {"connected": False, "reason": f"app_photoshop 読み込み失敗: {e}"}


@router.get("/api/photoshop/panel-status")
def api_photoshop_panel_status():
    """Photoshop プロセス状態を返す（AppleScript 経由なのでパネル不要）。"""
    import subprocess as _sp
    running = _sp.run(["pgrep", "-fi", "Adobe Photoshop"], capture_output=True).returncode == 0
    return {"alive": running, "method": "applescript_do_javascript"}


@router.post("/api/photoshop/open")
def api_photoshop_open(req: PhotoshopOpenRequest):
    try:
        ps = _import_photoshop()
        return {"ok": True, "active_document": ps.open_psd(req.path)}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"open 失敗: {e}")


@router.get("/api/photoshop/layers")
def api_photoshop_layers():
    try:
        ps = _import_photoshop()
        return {"layers": ps.list_layers()}
    except Exception as e:
        raise HTTPException(500, f"layers 取得失敗: {e}")


@router.post("/api/photoshop/set-text")
def api_photoshop_set_text(req: PhotoshopSetTextRequest):
    try:
        ps = _import_photoshop()
        ps.set_text(req.layer, req.text)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"set-text 失敗: {e}")


@router.post("/api/photoshop/export")
def api_photoshop_export(req: PhotoshopExportRequest):
    try:
        ps = _import_photoshop()
        out = ps.export_image(req.out_path, req.fmt, req.quality)
        return {"ok": True, "path": out}
    except Exception as e:
        raise HTTPException(500, f"export 失敗: {e}")


@router.post("/api/photoshop/replace-so")
def api_photoshop_replace_so(req: PhotoshopReplaceSmartObjectRequest):
    """指定レイヤー（スマートオブジェクト）の中身を画像で差し替え。"""
    try:
        ps = _import_photoshop()
        ps.replace_smart_object(req.layer, req.image_path)
        return {"ok": True}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"replace-so 失敗: {e}")


@router.post("/api/photoshop/set-visible")
def api_photoshop_set_visible(req: PhotoshopSetVisibleRequest):
    """レイヤーの表示/非表示を切り替え。"""
    try:
        ps = _import_photoshop()
        ps.set_layer_visible(req.layer, req.visible)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"set-visible 失敗: {e}")


@router.post("/api/photoshop/render-thumbs")
def api_photoshop_render_thumbs(req: PhotoshopRenderThumbsRequest):
    """サムネ 2 枚セット（PLAY LIST 表示版・非表示版）を書き出し。"""
    try:
        ps = _import_photoshop()
        return ps.render_thumbnail_set(
            req.psd_path,
            base_image=req.base_image,
            base_layer=req.base_layer,
            text_replacements=req.text_replacements,
            toggle_layer=req.toggle_layer,
            out_dir=req.out_dir,
            vol_name=req.vol_name,
            quality=req.quality,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"render-thumbs 失敗: {e}")


@router.post("/api/photoshop/render-for-video")
def api_photoshop_render_for_video(req: PhotoshopRenderForVideoRequest):
    """動画フォルダのみ指定でサムネ 2 枚生成。レイヤー名はチャンネル設定から取得。"""
    config = get_dashboard_config()
    folder_path = req.video_folder
    if not folder_path and req.video_name:
        folder_path = str(Path(config["channel_folder"]) / req.video_name)
    if not folder_path:
        raise HTTPException(400, "video_folder か video_name のいずれかが必要")
    try:
        ps = _import_photoshop()
        return ps.render_thumbnails_for_video(
            folder_path,
            base_layer=req.base_layer or config.get("psd_base_layer", "base"),
            toggle_layer=req.toggle_layer or config.get("psd_toggle_layer", "PLAY LIST"),
            image_subdir=req.image_subdir or config.get("psd_image_subdir", "image"),
            text_replacements=req.text_replacements,
            quality=req.quality,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"render-for-video 失敗: {e}")


class PhotoshopGenerateSceneTextRequest(BaseModel):
    video_name: Optional[str] = None         # アクティブチャンネル配下の vol フォルダ名
    video_folder: Optional[str] = None       # or 絶対パス
    image_path: Optional[str] = None         # 明示的な画像パス（指定時は video_name より優先）
    persona: Optional[str] = None            # 未指定時は per-channel config から取得


@router.post("/api/photoshop/generate-scene-text")
def api_photoshop_generate_scene_text(req: PhotoshopGenerateSceneTextRequest):
    """AI生成画像から シーンテキスト (英大文字 2-3 語) を自動生成。"""
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
            folder_path = str(Path(config["channel_folder"]) / req.video_name)
        if not folder_path:
            raise HTTPException(400, "image_path / video_folder / video_name のいずれかが必要")
        folder = Path(folder_path)
        # vol{N}.png を優先、無ければ image_subdir 配下の最初の画像
        cands = list(folder.glob("vol*.png")) + list(folder.glob("vol*.jpg"))
        if cands:
            image_path = str(cands[0])
        else:
            ps = _import_photoshop()
            swap = ps._find_swap_image(folder, config.get("psd_image_subdir", "image"))
            if not swap:
                raise HTTPException(404, f"画像が見つかりません: {folder}")
            image_path = str(swap)

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


@router.post("/api/photoshop/render-dual-thumbnail")
def api_photoshop_render_dual_thumbnail(req: PhotoshopRenderDualRequest):
    """Harbor Notes 仕様: AI生成画像 + シーンテキスト中央配置で 2 層対立式の 2 枚出力。
    vol{N}.jpg (都市名OFF/PLAY LIST ON, Premiere背景画像用) と
    サムネイル.jpg (都市名ON/PLAY LIST OFF, YouTubeサムネ用) を生成。

    CLI の step_psd_composite と同等の挙動になるよう、未指定パラメータは per-channel config
    から自動補完される（scene_text は空なら vision で生成、サイズは 1920×1080、save_psd=True 等）。
    """
    config = get_dashboard_config()
    folder_path = req.video_folder
    if not folder_path and req.video_name:
        folder_path = str(Path(config["channel_folder"]) / req.video_name)
    if not folder_path:
        raise HTTPException(400, "video_folder か video_name のいずれかが必要")
    folder = Path(folder_path)
    if not folder.exists():
        raise HTTPException(404, f"フォルダが存在しません: {folder}")

    try:
        ps = _import_photoshop()
        # PSD 検索（vol{N}.psd or vol*.psd or テンプレ）
        psd = ps._find_video_psd(folder)
        # 差し替え画像検索 — CLI step_psd_composite と同じフォールバック順:
        # vol{N}.png → vol{N}_source.jpg → image_subdir 配下の任意の画像。
        # vol{N}.jpg は Photoshop 合成出力（PLAY LIST 焼き付き）なので候補から除外する。
        swap = None
        m = re.match(r"^(\d+)_", folder.name)
        vol_num = m.group(1) if m else None
        if vol_num:
            for cand_name in (f"vol{vol_num}.png", f"vol{vol_num}_source.jpg"):
                cand = folder / cand_name
                if cand.exists():
                    swap = cand
                    break
        if not swap:
            for cand in folder.glob("vol*.png"):
                if not cand.name.endswith("_source.jpg"):
                    swap = cand
                    break
        if not swap:
            image_subdir = req.image_subdir or config.get("psd_image_subdir", "image")
            swap = ps._find_swap_image(folder, image_subdir)
        if not swap:
            raise HTTPException(404, f"差し替え画像が見つかりません: {folder}")

        # scene_text 自動補完（空 or 未指定なら vision で生成 + scene_en.txt にキャッシュ）
        scene_text = (req.scene_text or "").strip()
        if not scene_text:
            cache_file = folder / "scene_en.txt"
            if cache_file.exists():
                try:
                    scene_text = cache_file.read_text(encoding="utf-8").strip()
                except Exception:
                    scene_text = ""
            if not scene_text:
                try:
                    persona = (config.get("persona") or "").strip()
                    scene_text = generate_scene_text_for_image(
                        str(swap), persona=persona,
                        tone=(config.get("scene_text_tone") or ""),
                        examples=config.get("scene_text_examples") or [],
                        forbidden_phrases=config.get("scene_text_forbidden") or [],
                        structure=(config.get("scene_text_structure") or ""),
                    )
                    if scene_text:
                        try:
                            cache_file.write_text(scene_text + "\n", encoding="utf-8")
                        except Exception:
                            pass
                except Exception:
                    scene_text = ""

        # サイズ自動補完
        tw = req.target_width or config.get("psd_export_width") or 1920
        th = req.target_height or config.get("psd_export_height") or 1080
        # save_psd デフォルト True
        save_psd = True if req.save_psd is None else bool(req.save_psd)
        # フォント自動補完
        scene_text_font = req.scene_text_font or config.get("psd_text_font") or None
        # レイヤー名（strip しない — 末尾スペース必須なケースあり）
        base_layer = req.base_layer or config.get("psd_base_layer") or "base"
        scene_text_layer = req.scene_text_layer or config.get("psd_text_layer") or "都市名_テキスト"
        playlist_layer = req.playlist_layer or config.get("psd_toggle_layer") or "PLAY LIST "
        # competitor 踏襲: headline（toggle 層）を両出力で常時表示するか。
        # 未設定/False なら従来の toggle 切替（背景 ON / サムネ OFF）と同一。pipeline と挙動を揃える。
        toggle_always_visible = bool(config.get("psd_toggle_always_visible"))

        return ps.render_dual_thumbnail(
            psd_path=str(psd),
            base_image=str(swap),
            scene_text=scene_text,
            out_dir=str(folder),
            base_layer=base_layer,
            scene_text_layer=scene_text_layer,
            playlist_layer=playlist_layer,
            toggle_always_visible=toggle_always_visible,
            quality=req.quality,
            target_width=int(tw),
            target_height=int(th),
            save_psd=save_psd,
            scene_text_font=scene_text_font,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"render-dual-thumbnail 失敗: {e}")


@router.post("/api/photoshop/eval")
def api_photoshop_eval(req: PhotoshopEvalRequest):
    """任意の ExtendScript を実行（デバッグ用）。"""
    if (os.environ.get("APP_ENABLE_PHOTOSHOP_EVAL") or os.environ.get("ORZZ_ENABLE_PHOTOSHOP_EVAL")) != "1":
        raise HTTPException(403, "Photoshop eval はデバッグ時のみ有効です")
    try:
        ps = _import_photoshop()
        return {"ok": True, "result": ps.run_jsx(req.code, timeout=req.timeout)}
    except Exception as e:
        raise HTTPException(500, f"eval 失敗: {e}")

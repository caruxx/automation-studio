"""Background API for the non-destructive ffmpeg editor."""
from __future__ import annotations
import json, math, re, threading, time, uuid, shutil
from collections import deque
from pathlib import Path
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app_core import get_channels, resolve_video_folder, resolve_asset_in_folder, path_is_within, _find_exported_mp4, _resolve_to_current_host
from app_video_edit import run_edit, run_queue, generate_editor_assets
from resource_lock import acquire_resource

router=APIRouter(prefix="/api/video-edit", tags=["video-edit"])
HISTORY_FILE=Path.home()/".config/orzz/video_edit_history.json"
ASSET_CACHE=Path.home()/".cache/orzz/video_edit"
_queue: deque[dict[str,Any]]=deque(); _jobs: dict[str,dict[str,Any]]={}; _lock=threading.RLock(); _worker_started=False

class EditRequest(BaseModel):
    operation: str
    input_path: str
    video_name: Optional[str]=""
    params: dict[str,Any]={}

class QueueRequest(BaseModel):
    input_path: str
    video_name: Optional[str]=""
    operations: list[dict[str,Any]]=[]

class AdoptRequest(BaseModel):
    video_name: str
    output_path: str
    target_path: str
    confirmed: bool=False

class ThumbnailUseRequest(BaseModel):
    video_name: str
    frame_path: str

def _vol_folder(name: str) -> Optional[Path]:
    if not name: return None
    for ch in get_channels():
        root=ch.get("folder") or ""
        if root:
            try:
                found=resolve_video_folder(name, channel_root=_resolve_to_current_host(root))
            except HTTPException:
                continue
            if found: return Path(found).resolve()
    return None

MEDIA_EXT={".mp4",".mov",".mkv",".m4a",".mp3",".wav",".png",".jpg",".jpeg",".webp"}

def _require_folder(video_name: Optional[str]) -> Path:
    """video_name を必須にし、登録チャンネル配下の vol フォルダ（実体パス）を返す。"""
    if not (video_name or "").strip(): raise HTTPException(400, "video_name が必要です")
    folder=_vol_folder(video_name)
    if not folder: raise HTTPException(404, "video_name が見つかりません")
    return folder

def _exported_mp4_for(folder: Path, video_name: str) -> Optional[Path]:
    """この vol の書き出し済み mp4（vol フォルダ外。export_path 直下など）。名前に vol 番号を含む .mp4 だけ。"""
    try:
        exported=_find_exported_mp4(folder, video_name)
    except Exception:
        return None
    if exported is None: return None
    m=re.match(r"^(\d+)_", video_name or "")
    if not m: return None
    p=Path(exported).resolve(strict=False)
    if p.suffix.lower()!=".mp4" or not p.is_file(): return None
    if not re.search(rf"vol0*{int(m.group(1))}(?!\d)", p.stem): return None
    return p

def _resolve_media(folder: Path, video_name: str, raw: str, *, allow_exported: bool=False) -> Path:
    """vol フォルダ配下の素材だけを許可する。相対・絶対どちらも可。
    allow_exported=True（読み取り用途: preview / assets / 編集入力）のときだけ、この vol の書き出し済み mp4 も許可する。"""
    try:
        return resolve_asset_in_folder(folder, raw, allowed_ext=MEDIA_EXT)
    except HTTPException as exc:
        if exc.status_code==403 and allow_exported:
            exported=_exported_mp4_for(folder, video_name)
            try:
                wanted=Path(str(raw)).expanduser().resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                raise exc
            if exported is not None and wanted==exported:
                return exported
        raise

# 操作ごとに受け付ける params のキー（S02: 利用者が送った任意キー（_cleanup 等）を ffmpeg 層へ通さない）
_ALLOWED_PARAMS={
    "trim":{"start","end","reencode"},
    "concat":{"input_paths"},
    "loop_to_duration":{"duration","fade_out"},
    "fade":{"video_in","video_out","audio_in","audio_out"},
    "replace_audio":{"audio_path","volume_db"},
    "burn_overlay":{"overlay_type","text","overlay_path","x","y","w","h","position","size","opacity"},
    "extract_frame":{"time"},
    "convert":{"resolution","fps","format"},
}
_NUMERIC_PARAMS={"start","end","duration","fade_out","video_in","video_out","audio_in","audio_out","volume_db","x","y","w","h","size","opacity","time","fps"}
_RE_RESOLUTION=re.compile(r"^\d{2,5}x\d{2,5}$")
_RE_POSITION=re.compile(r"^[0-9A-Za-z()*+\-/.: ]{1,80}$")
_FORMATS={"mp4","mov","mkv","webm"}

def _sanitize_params(operation: str, raw: dict[str,Any]) -> dict[str,Any]:
    allowed=_ALLOWED_PARAMS.get(operation)
    if allowed is None: raise HTTPException(400, f"未知の操作です: {operation}")
    out: dict[str,Any]={}
    for key, val in (raw or {}).items():
        k=str(key)
        if k.startswith("_"): raise HTTPException(400, f"params に内部キーは指定できません: {k}")
        if k not in allowed: continue
        if k in _NUMERIC_PARAMS:
            if val in (None, ""): continue
            try: num=float(val)
            except (TypeError, ValueError): raise HTTPException(400, f"{k} は数値で指定してください")
            if not math.isfinite(num) or abs(num) > 1e7: raise HTTPException(400, f"{k} の値が範囲外です")
            out[k]=num; continue
        if k=="reencode":
            out[k]=val if isinstance(val,bool) else str(val).strip().lower() in ("1","true","yes","on"); continue
        if k=="resolution":
            v=str(val).strip()
            if v and not _RE_RESOLUTION.match(v): raise HTTPException(400, "resolution は 幅x高さ（例 1920x1080）で指定してください")
            if v: out[k]=v
            continue
        if k=="format":
            v=str(val).strip().lstrip(".").lower()
            if v and v not in _FORMATS: raise HTTPException(400, f"format は {sorted(_FORMATS)} のいずれかです")
            if v: out[k]=v
            continue
        if k=="overlay_type":
            v=str(val).strip().lower()
            if v not in ("text","image"): raise HTTPException(400, "overlay_type は text か image です")
            out[k]=v; continue
        if k=="position":
            v=str(val).strip()
            if v and not _RE_POSITION.match(v): raise HTTPException(400, "position に使えない文字が含まれています")
            if v: out[k]=v
            continue
        if k=="text":
            v=str(val)[:300]
            if "\x00" in v: raise HTTPException(400, "text が不正です")
            out[k]=v; continue
        out[k]=val  # input_paths / audio_path / overlay_path はこの後パス検証する
    return out

def _validated(req: EditRequest) -> dict[str,Any]:
    folder=_require_folder(req.video_name)
    p=_resolve_media(folder, req.video_name, req.input_path, allow_exported=True)
    params=_sanitize_params(req.operation, dict(req.params or {})); params["input_path"]=str(p)
    for key in ("input_paths","audio_path","overlay_path"):
        if key in params:
            vals=params[key] if isinstance(params[key],list) else [params[key]]
            checked=[str(_resolve_media(folder, req.video_name, str(raw))) for raw in vals if str(raw).strip()]
            if not checked: raise HTTPException(400, f"{key} が空です")
            params[key]=checked if isinstance(params[key],list) else checked[0]
    return params

def _save_history():
    HISTORY_FILE.parent.mkdir(parents=True,exist_ok=True)
    done=[j for j in _jobs.values() if j["status"] in ("completed","failed")][-50:]
    HISTORY_FILE.write_text(json.dumps(done,ensure_ascii=False,indent=2),encoding="utf-8")

def _worker():
    while True:
        with _lock:
            job=_queue.popleft() if _queue else None
        if not job: time.sleep(.2); continue
        jid=job["id"]
        def update(state):
            with _lock: _jobs[jid].update(state)
        try:
            update({"status":"running","started_at":time.time(),"queue_position":0})
            if job["operation"] == "run_queue": run_queue(job["params"]["input_path"], job["params"]["operations"], update)
            else: run_edit(job["operation"],job["params"],update)
        except Exception as e: update({"status":"failed","error":str(e),"finished_at":time.time()})
        finally:
            with _lock: _save_history()

def _ensure_worker():
    global _worker_started
    if not _worker_started:
        threading.Thread(target=_worker,name="video-edit-worker",daemon=True).start(); _worker_started=True

@router.post("/run")
def run(req: EditRequest):
    if req.operation not in {"trim","concat","loop_to_duration","fade","replace_audio","burn_overlay","extract_frame","convert"}: raise HTTPException(400, "未対応の操作です")
    params=_validated(req); jid=uuid.uuid4().hex[:12]
    job={"id":jid,"operation":req.operation,"video_name":req.video_name or "","input_path":params["input_path"],"params":params,"status":"queued","progress":0.0,"created_at":time.time()}
    with _lock:
        _jobs[jid]=job; _queue.append(job); job["queue_position"]=len(_queue)
    _ensure_worker(); return {"status":"queued","job_id":jid,"queue_position":job["queue_position"]}

@router.post("/run-queue")
def run_queue_api(req: QueueRequest):
    if not req.operations: raise HTTPException(400, "操作キューが空です")
    allowed={"trim","concat","loop_to_duration","fade","replace_audio","burn_overlay","extract_frame","convert"}
    if any(x.get("operation") not in allowed for x in req.operations): raise HTTPException(400, "未対応の操作があります")
    base=_validated(EditRequest(operation="trim",input_path=req.input_path,video_name=req.video_name,params={}))
    operations=[]
    for item in req.operations:
        validated=_validated(EditRequest(operation=item["operation"],input_path=req.input_path,video_name=req.video_name,params=item.get("params") or {}))
        validated.pop("input_path",None)
        operations.append({"operation":item["operation"],"params":validated})
    jid=uuid.uuid4().hex[:12]; params={"input_path":base["input_path"],"operations":operations}
    job={"id":jid,"operation":"run_queue","video_name":req.video_name or "","input_path":base["input_path"],"params":params,"status":"queued","progress":0.0,"created_at":time.time()}
    with _lock: _jobs[jid]=job; _queue.append(job); job["queue_position"]=len(_queue)
    _ensure_worker(); return {"status":"queued","job_id":jid,"queue_position":job["queue_position"]}

@router.get("/status")
def status(job_id: str=""):
    with _lock:
        if job_id:
            if job_id not in _jobs: raise HTTPException(404, "job が見つかりません")
            return dict(_jobs[job_id])
        active=[dict(j) for j in _jobs.values() if j["status"] in ("queued","running")]
        return {"status":"ok","running":next((j for j in active if j["status"]=="running"),None),"queued":[j for j in active if j["status"]=="queued"]}

@router.get("/history")
def history(limit: int=20):
    with _lock:
        rows=[dict(j) for j in _jobs.values() if j["status"] in ("completed","failed")]
    if not rows and HISTORY_FILE.exists():
        try: rows=json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception: rows=[]
    return {"history":sorted(rows,key=lambda j:j.get("created_at",0),reverse=True)[:max(1,min(limit,50))]}

@router.get("/preview")
def preview(video_name: str="", asset: str="", path: str=""):
    """vol フォルダ配下の素材だけを配信する。asset（相対）または path（絶対。実体が vol 配下のもの）。"""
    folder=_require_folder(video_name)
    p=_resolve_media(folder, video_name, asset or path, allow_exported=True)
    return FileResponse(str(p))

@router.get("/assets")
def assets(video_name: str="", asset: str="", path: str="", count: int=8):
    folder=_require_folder(video_name)
    p=_resolve_media(folder, video_name, asset or path, allow_exported=True)
    try:
        data=generate_editor_assets(str(p), ASSET_CACHE, count)
        key=data["cache_key"]
        return {"cache_key":key,"duration":data["duration"],"cached":data["cached"],"frames":[f"/api/video-edit/asset/{key}/{p.name}" for p in data["frames"]],"waveform":f"/api/video-edit/asset/{key}/{data['waveform'].name}"}
    except Exception as e: raise HTTPException(500, f"編集素材生成失敗: {e}")

@router.get("/asset/{cache_key}/{filename}")
def asset(cache_key: str, filename: str):
    if not all(c.isalnum() or c in "_-" for c in cache_key) or Path(filename).name != filename: raise HTTPException(400,"不正なパスです")
    p=(ASSET_CACHE/cache_key/filename).resolve()
    try: p.relative_to(ASSET_CACHE.resolve())
    except ValueError: raise HTTPException(400,"不正なパスです")
    if not p.is_file(): raise HTTPException(404,"素材がありません")
    return FileResponse(str(p))

@router.post("/adopt")
def adopt(req: AdoptRequest):
    if not req.confirmed: raise HTTPException(400,"確認が必要です")
    folder=_require_folder(req.video_name)
    output=_resolve_media(folder, req.video_name, req.output_path); target=_resolve_media(folder, req.video_name, req.target_path)
    stamp=time.strftime("%Y%m%d_%H%M%S"); backup=target.with_name(f"{target.stem}.backup_{stamp}{target.suffix}")
    for n in range(1, 1000):
        if not backup.exists(): break
        backup=target.with_name(f"{target.stem}.backup_{stamp}_{n}{target.suffix}")
    lock=acquire_resource("ffmpeg_edit", owner="video-edit:adopt", blocking=True)
    try:
        target.rename(backup)
        try: shutil.copy2(output,target)
        except Exception:
            backup.rename(target); raise
    finally: lock.release()
    return {"status":"ok","target_path":str(target),"backup_path":str(backup)}

@router.post("/use-thumbnail")
def use_thumbnail(req: ThumbnailUseRequest):
    folder=_require_folder(req.video_name)
    if "\x00" in str(req.frame_path or ""): raise HTTPException(400,"frame_path が不正です")
    try: src=Path(req.frame_path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError): raise HTTPException(400,"frame_path が不正です")
    if not (path_is_within(src, ASSET_CACHE) or path_is_within(src, folder)): raise HTTPException(403,"フレームは編集素材キャッシュか vol 配下に限ります")
    if src.suffix.lower() not in (".png",".jpg",".jpeg",".webp") or not src.is_file(): raise HTTPException(404,"フレームがありません")
    out_dir=folder/"thumbnail_materials"; out_dir.mkdir(exist_ok=True)
    out=out_dir/f"video_frame_{time.strftime('%Y%m%d_%H%M%S')}{src.suffix.lower()}"; shutil.copy2(src,out)
    return {"status":"ok","path":str(out)}

@router.get("/candidates/{video_name}")
def candidates(video_name: str):
    folder=_vol_folder(video_name)
    if not folder: raise HTTPException(404, "video_name が見つかりません")
    exts={".mp4",".mov",".mkv",".mp3",".wav",".m4a",".png",".jpg",".jpeg"}
    files=[str(p) for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts]
    return {"folder":str(folder),"files":files[:500]}

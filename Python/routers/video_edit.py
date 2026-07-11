"""Background API for the non-destructive ffmpeg editor."""
from __future__ import annotations
import json, threading, time, uuid, shutil
from collections import deque
from pathlib import Path
from typing import Any, Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app_core import get_channels, resolve_video_folder
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
                found=resolve_video_folder(name, channel_root=root)
            except HTTPException:
                continue
            if found: return Path(found).resolve()
    return None

def _validated(req: EditRequest) -> dict[str,Any]:
    p=Path(req.input_path).expanduser()
    if not p.is_absolute(): raise HTTPException(400, "input_path は絶対パスで指定してください")
    p=p.resolve()
    if not p.is_file(): raise HTTPException(404, f"入力ファイルがありません: {p}")
    if req.video_name:
        folder=_vol_folder(req.video_name)
        if not folder: raise HTTPException(404, "video_name が見つかりません")
        try: p.relative_to(folder)
        except ValueError: raise HTTPException(400, "video_name を指定した入力は対応 vol 配下に限ります")
    params=dict(req.params); params["input_path"]=str(p)
    for key in ("input_paths","audio_path","overlay_path"):
        if key in params:
            vals=params[key] if isinstance(params[key],list) else [params[key]]
            checked=[]
            for raw in vals:
                q=Path(raw).expanduser()
                if not q.is_absolute() or not q.resolve().is_file(): raise HTTPException(400, f"{key} の絶対パスが不正です: {raw}")
                if req.video_name:
                    try: q.resolve().relative_to(_vol_folder(req.video_name))
                    except ValueError: raise HTTPException(400, f"{key} は対応 vol 配下に限ります")
                checked.append(str(q.resolve()))
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
def preview(path: str):
    p=Path(path).expanduser().resolve()
    if not p.is_file(): raise HTTPException(404, "動画ファイルが見つかりません")
    return FileResponse(str(p))

@router.get("/assets")
def assets(path: str, count: int=8):
    try:
        data=generate_editor_assets(path, ASSET_CACHE, count)
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
    folder=_vol_folder(req.video_name)
    if not folder: raise HTTPException(404,"video_name が見つかりません")
    output=Path(req.output_path).expanduser().resolve(); target=Path(req.target_path).expanduser().resolve()
    try: output.relative_to(folder); target.relative_to(folder)
    except ValueError: raise HTTPException(400,"出力と差し替え先は対応 vol 配下に限ります")
    if not output.is_file() or not target.is_file(): raise HTTPException(404,"出力または差し替え先がありません")
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
    folder=_vol_folder(req.video_name)
    if not folder: raise HTTPException(404,"video_name が見つかりません")
    src=Path(req.frame_path).expanduser().resolve()
    if not src.is_file(): raise HTTPException(404,"フレームがありません")
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

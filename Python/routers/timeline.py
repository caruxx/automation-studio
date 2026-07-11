from __future__ import annotations
import hashlib, json, subprocess, threading
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app_core import get_channels, resolve_video_folder
from app_timeline import IMAGE_EXTS, load, save
from settings_service import config_set

router = APIRouter(prefix="/api/timeline", tags=["timeline"])
CACHE = Path.home()/".cache/orzz/timeline"

def _folder(name: str) -> Path:
    for ch in get_channels():
        try: return resolve_video_folder(name, channel_root=ch.get("folder") or "")
        except HTTPException: pass
    raise HTTPException(404, "動画フォルダが見つかりません")

class TimelineUpdate(BaseModel):
    model: dict

class NowPlayingTemplateUpdate(BaseModel):
    channel_id: str
    now_playing: dict

class VisualizerTemplateUpdate(BaseModel):
    channel_id: str
    visualizer: dict

@router.get("/{video_name}")
def get_timeline(video_name: str): return load(_folder(video_name))

@router.put("/{video_name}")
def put_timeline(video_name: str, req: TimelineUpdate): return save(_folder(video_name), req.model)

@router.post("/{video_name}/now-playing-template")
def save_now_playing_template(video_name: str, req: NowPlayingTemplateUpdate):
    _folder(video_name)
    results=[]
    for key, value in req.now_playing.items():
        results.append(config_set(f"channel.now_playing.{key}", json.dumps(value, ensure_ascii=False) if isinstance(value,(dict,list)) else ("true" if value is True else "false" if value is False else str(value)), channel_id=req.channel_id, actor="timeline-ui"))
    return {"status":"ok","saved":len(results)}

@router.post("/{video_name}/visualizer-template")
def save_visualizer_template(video_name: str, req: VisualizerTemplateUpdate):
    """Save the current visualizer as this channel's default template.

    config_set validates every field against settings_catalog and appends the
    standard config audit row, so this uses the same restore/audit path as the
    Now Playing template.
    """
    _folder(video_name)
    results=[]
    for key, value in req.visualizer.items():
        try:
            results.append(config_set(
                f"channel.visualizer.{key}",
                json.dumps(value, ensure_ascii=False) if isinstance(value,(dict,list)) else
                ("true" if value is True else "false" if value is False else str(value)),
                channel_id=req.channel_id, actor="timeline-ui"))
        except KeyError:
            raise HTTPException(400, f"未対応のビジュアライザー設定です: {key}")
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    return {"status":"ok","saved":len(results)}

@router.get("/{video_name}/waveform/{clip_id}")
def waveform(video_name: str, clip_id: str):
    folder=_folder(video_name); model=load(folder); clip=next((x for x in model["audio_clips"] if x["id"]==clip_id),None)
    if not clip: raise HTTPException(404,"音声クリップがありません")
    src=(folder/clip["path"]).resolve(); key=hashlib.sha1(f"{src}:{src.stat().st_mtime_ns}".encode()).hexdigest()[:20]
    out=CACHE/key/"waveform.png"; out.parent.mkdir(parents=True,exist_ok=True)
    if not out.exists():
        r=subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(src),"-filter_complex","showwavespic=s=900x100:colors=0xa777e3","-frames:v","1",str(out)],capture_output=True,text=True,timeout=90)
        if r.returncode: raise HTTPException(500,"波形生成に失敗しました")
    return FileResponse(out)

@router.get("/{video_name}/audio/{clip_id}")
def audio(video_name: str, clip_id: str):
    folder=_folder(video_name); model=load(folder); clip=next((x for x in model["audio_clips"] if x["id"]==clip_id),None)
    if not clip: raise HTTPException(404,"音声クリップがありません")
    p=(folder/clip["path"]).resolve()
    try:p.relative_to(folder.resolve())
    except ValueError: raise HTTPException(400,"不正なパスです")
    return FileResponse(p)

@router.get("/{video_name}/image")
def image(video_name: str, path: str):
    folder=_folder(video_name); raw=Path(path).expanduser(); p=(raw if raw.is_absolute() else folder/raw).resolve()
    roots=[folder.resolve()]
    try:
        cfg=json.loads((folder.parent/".app_channel_config.json").read_text(encoding="utf-8")); ref=Path(str(cfg.get("reference_image_dir") or "")).expanduser().resolve()
        if ref.is_dir(): roots.append(ref)
    except Exception: pass
    if not any(_inside(p,root) for root in roots): raise HTTPException(400,"不正なパスです")
    if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS: raise HTTPException(404,"画像がありません")
    return FileResponse(p)

@router.get("/{video_name}/image-candidates")
def image_candidates(video_name: str):
    folder=_folder(video_name); roots=[folder,folder/"Image"]
    try:
        cfg=json.loads((folder.parent/".app_channel_config.json").read_text(encoding="utf-8")); ref=Path(str(cfg.get("reference_image_dir") or "")).expanduser()
        if ref.is_dir(): roots.append(ref)
    except Exception: pass
    rows=[]
    for root in roots:
        if not root.is_dir(): continue
        for p in root.iterdir():
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                if root==folder or root==folder/"Image": path=str(p.relative_to(folder))
                else: path=str(p.resolve())
                url=f"/api/timeline/{video_name}/image?path={path}"
                rows.append({"path":path,"name":p.name,"url":url})
    return {"images":rows[:300]}

def _inside(path: Path, root: Path) -> bool:
    try:path.relative_to(root); return True
    except ValueError:return False

@router.post("/{video_name}/export")
def export(video_name: str, background_tasks: BackgroundTasks):
    folder=_folder(video_name)
    def run(): subprocess.run(["python3",str(Path(__file__).parents[1]/"app_pipeline.py"),folder.name.split("_",1)[0],"--only","export"],cwd=str(Path(__file__).parents[1]))
    background_tasks.add_task(run)
    return {"status":"started","video_name":video_name}

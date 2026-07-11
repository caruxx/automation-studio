from __future__ import annotations
import hashlib, json, subprocess, threading, time
from typing import Optional
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app_core import get_channels, resolve_video_folder
from app_timeline import IMAGE_EXTS, load, save
from settings_service import config_set

router = APIRouter(prefix="/api/timeline", tags=["timeline"])
CACHE = Path.home()/".cache/orzz/timeline"
_export_tasks: dict[str, dict] = {}
_export_lock = threading.Lock()

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _export_output_path(folder: Path) -> Path:
    """app_pipeline の export step と同じ規則で書き出し先を解決する。"""
    from app_pipeline import _resolve_external_output_path, _resolve_local_output_path
    vol = int(folder.name.split("_", 1)[0])
    return (_resolve_external_output_path(vol, folder)
            or _resolve_local_output_path(vol, folder)).expanduser().resolve()

def _collect_export_output(video_name: str, proc: subprocess.Popen) -> None:
    task = _export_tasks[video_name]
    try:
        if proc.stdout:
            for raw in proc.stdout:
                with _export_lock:
                    task["log_tail"].append(raw.rstrip("\r\n"))
                    task["log_tail"] = task["log_tail"][-200:]
        returncode = proc.wait()
    except Exception as exc:
        returncode = proc.poll()
        with _export_lock:
            task["log_tail"].append(f"[エラー] ログ収集に失敗しました: {exc}")
    with _export_lock:
        task["running"] = False
        task["returncode"] = returncode if returncode is not None else -1
        task["finished_ts"] = time.time()
        task["finished_at"] = _iso_now()

def _export_phase(task: dict | None, mp4_exists: bool) -> str:
    if not task:
        return "完了" if mp4_exists else "未実行"
    if not task.get("running"):
        return "完了" if task.get("returncode") == 0 else "失敗"
    text = "\n".join(task.get("log_tail", [])).lower()
    if any(x in text for x in ("render", "レンダ", "ffmpeg", "書き出し完了待ち", "encoding")):
        return "レンダリング中"
    return "起動中"

def _folder(name: str) -> Path:
    for ch in get_channels():
        try: return resolve_video_folder(name, channel_root=ch.get("folder") or "")
        except HTTPException: pass
    raise HTTPException(404, "動画フォルダが見つかりません")

class TimelineUpdate(BaseModel):
    model: dict
    base_updated_at: Optional[str] = None  # Python3.9互換のため PEP604 は使わない

class NowPlayingTemplateUpdate(BaseModel):
    channel_id: str
    now_playing: dict

class VisualizerTemplateUpdate(BaseModel):
    channel_id: str
    visualizer: dict

@router.get("/{video_name}")
def get_timeline(video_name: str): return load(_folder(video_name))

@router.put("/{video_name}")
def put_timeline(video_name: str, req: TimelineUpdate):
    folder = _folder(video_name)
    if req.base_updated_at is not None and req.base_updated_at != load(folder).get("updated_at"):
        raise HTTPException(409, "タイムラインが他の画面で更新されています")
    return save(folder, req.model)

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
def export(video_name: str):
    folder=_folder(video_name)
    with _export_lock:
        current = _export_tasks.get(video_name)
        if current and current.get("running"):
            raise HTTPException(409, "既に書き出し実行中です")
        started_at = _iso_now()
        proc = subprocess.Popen(
            ["python3", str(Path(__file__).parents[1]/"app_pipeline.py"), folder.name.split("_",1)[0], "--only", "export"],
            cwd=str(Path(__file__).parents[1]), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        _export_tasks[video_name] = {"started_at": started_at, "started_ts": time.time(), "pid": proc.pid,
            "running": True, "returncode": None, "log_tail": [], "finished_ts": None, "finished_at": None}
    threading.Thread(target=_collect_export_output, args=(video_name, proc), daemon=True).start()
    return {"status":"started","video_name":video_name,"pid":proc.pid,"started_at":started_at}

@router.get("/{video_name}/export/status")
def export_status(video_name: str):
    folder = _folder(video_name)
    output_path = _export_output_path(folder)
    with _export_lock:
        task = _export_tasks.get(video_name)
        snapshot = dict(task) if task else None
        if snapshot: snapshot["log_tail"] = list(snapshot.get("log_tail", []))
    exists = output_path.is_file()
    stat = output_path.stat() if exists else None
    elapsed = max(0, (snapshot.get("finished_ts") or time.time()) - snapshot["started_ts"]) if snapshot else 0
    return {"running": bool(snapshot and snapshot.get("running")),
        "started_at": snapshot.get("started_at") if snapshot else None,
        "elapsed_sec": round(elapsed, 1), "returncode": snapshot.get("returncode") if snapshot else None,
        "log_tail": snapshot.get("log_tail", []) if snapshot else [], "output_path": str(output_path),
        "mp4_exists": exists, "mp4_size_bytes": stat.st_size if stat else 0,
        "mp4_mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z") if stat else None,
        "phase": _export_phase(snapshot, exists)}

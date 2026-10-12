"""Independent layer-motion storage/export API. Never mutates pipeline artifacts."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app_core import resolve_video_folder
from app_layer_motion import (
    MAX_PROJECT_BYTES, ExportCancelled, InvalidProject, publish_unique, render_mp4, validate_project,
)

router = APIRouter(prefix="/api/layer-motion", tags=["layer-motion"])
MAX_REQUEST_BYTES = MAX_PROJECT_BYTES + 4096
MAX_HISTORY = 100
_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()
_ACTIVE = {"queued", "running", "cancelling"}


def _safe_name(value: str, suffix: str):
    if not re.fullmatch(r"motion_[0-9a-f]{32}" + re.escape(suffix), value):
        raise HTTPException(400, "ファイル名が不正です")


def _scope(folder: Path) -> str:
    return hashlib.sha256(str(folder).encode("utf-8")).hexdigest()


def _folder(video_name: str, scope: str = "") -> Path:
    # Capture the folder once: an active-channel switch must never redirect a job.
    folder = Path(resolve_video_folder(video_name)).resolve()
    if not isinstance(scope, str):
        raise HTTPException(400, "scope は文字列にしてください")
    if scope and scope != _scope(folder):
        raise HTTPException(409, "対象チャンネルが変更されています。編集内容を保全して対象動画を開き直してください")
    return folder


@router.get("/context")
def context(video_name: str):
    return {"scope": _scope(_folder(video_name))}


def _directory(folder: Path, kind: str, *, create: bool = False) -> Path:
    folder = folder.resolve()
    current = folder
    for component in ("layer_motion", kind):
        current = current / component
        if current.is_symlink():
            raise HTTPException(400, "layer_motion 保存先にシンボリックリンクは使用できません")
        if create:
            current.mkdir(exist_ok=True)
        try:
            current.resolve().relative_to(folder)
        except ValueError as exc:
            raise HTTPException(400, "保存先が対象動画フォルダ外です") from exc
        if current.exists() and not current.is_dir():
            raise HTTPException(400, "layer_motion 保存先がフォルダではありません")
    return current


def _file(folder: Path, kind: str, filename: str) -> Path:
    _safe_name(filename, ".json" if kind == "projects" else ".mp4")
    directory = _directory(folder, kind)
    path = directory / filename
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise HTTPException(400, "ファイルのシンボリックリンクは使用できません")
    if not path.is_file() or path.stat().st_size == 0:
        raise HTTPException(404, "ファイルがありません")
    return path


async def _body(request: Request) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "リクエストは 32 MiB 以下にしてください")
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, "JSON を読み込めません") from exc
    if not isinstance(value, dict) or not isinstance(value.get("video_name"), str):
        raise HTTPException(400, "video_name と project を指定してください")
    return value


def _validated(data: dict) -> dict:
    try:
        return validate_project(data.get("project"))
    except InvalidProject as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


def _save(data: dict):
    folder = _folder(data["video_name"], data.get("scope", ""))
    project = _validated(data)
    title = data.get("title", "レイヤーモーション")
    if not isinstance(title, str) or len(title) > 200:
        raise HTTPException(400, "title は 200 文字以内にしてください")
    directory = _directory(folder, "projects", create=True)
    filename = f"motion_{uuid.uuid4().hex}.json"
    target = directory / filename
    temporary = directory / f".{filename}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump({"title": title, "project": project}, stream, ensure_ascii=False, separators=(",", ":"))
        publish_unique(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {"filename": filename, "path": str(target)}


@router.post("/project")
async def save_project(request: Request):
    return await run_in_threadpool(_save, await _body(request))


@router.get("/projects")
def projects(video_name: str, scope: str = ""):
    folder = _folder(video_name, scope)
    directory = _directory(folder, "projects")
    rows = []
    if directory.is_dir():
        for path in sorted(directory.glob("motion_*.json"), key=lambda p: p.name, reverse=True):
            if path.is_symlink() or not re.fullmatch(r"motion_[0-9a-f]{32}\.json", path.name):
                continue
            try:
                # Read bounded files only; a corrupt file does not hide other projects.
                if path.stat().st_size > MAX_REQUEST_BYTES:
                    continue
                saved = json.loads(path.read_text(encoding="utf-8"))
                title = saved.get("title", path.stem)
                if isinstance(title, str):
                    rows.append({"filename": path.name, "title": title[:200]})
            except (OSError, ValueError, AttributeError, RecursionError):
                continue
    return {"projects": rows}


@router.get("/project")
def load_project(video_name: str, filename: str, scope: str = ""):
    path = _file(_folder(video_name, scope), "projects", filename)
    if path.stat().st_size > MAX_REQUEST_BYTES:
        raise HTTPException(400, "保存ファイルが上限を超えています")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("invalid project")
        project = _validated(saved)
        title = saved.get("title", "レイヤーモーション")
        if not isinstance(title, str) or len(title) > 200:
            raise ValueError("invalid title")
        return {"project": project, "title": title}
    except (OSError, ValueError, RecursionError) as exc:
        raise HTTPException(400, "保存プロジェクトを読み込めません") from exc


def _public(job):
    return {key: value for key, value in job.items() if not key.startswith("_")}


def _run(job_id: str, project: dict):
    with _lock:
        job = _jobs[job_id]
        event = job["_cancel"]
        folder, filename = job["_folder"], job["_filename"]
        if not event.is_set():
            job["status"] = "running"
    temporary = None
    outcome = None
    failure = None
    def progress(value):
        with _lock:
            job["progress"] = value
    def process(value):
        with _lock:
            job["_process"] = value
            if value is not None and event.is_set() and value.poll() is None:
                value.terminate()
    try:
        if event.is_set():
            raise ExportCancelled()
        directory = _directory(folder, "exports", create=True)
        temporary = directory / f".{filename}.partial"
        # Only this job owns this exclusive partial file; never touch unrelated output.
        with temporary.open("xb"):
            pass
        render_mp4(project, temporary, event, progress, process)
        with _lock:
            if event.is_set():
                raise ExportCancelled()
            directory = _directory(folder, "exports")  # Recheck after potentially long render.
            target = directory / filename
            publish_unique(temporary, target)
            job.update(status="completed", progress=100, output_path=str(target),
                       url=f"/api/layer-motion/file/{quote(job['video_name'], safe='')}/{filename}?job_id={job_id}")
    except ExportCancelled:
        outcome = "cancelled"
    except Exception as exc:
        outcome = "cancelled" if event.is_set() else "failed"
        if not event.is_set():
            failure = str(exc)
    finally:
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError as exc:
            outcome, failure = "failed", f"一時ファイルの削除に失敗しました: {exc}"
        with _lock:
            if outcome:
                job["status"] = outcome
            if failure:
                job["error"] = failure
            job["_process"] = None
            job["finished_at"] = time.time()


def _request_id(value):
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or len(value) > 36:
        raise HTTPException(400, "request_id は UUID にしてください")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HTTPException(400, "request_id は UUID にしてください") from exc


def _submit(data: dict):
    folder = _folder(data["video_name"], data.get("scope", ""))
    request_id = _request_id(data.get("request_id"))
    project = _validated(data)
    digest = hashlib.sha256(json.dumps(project, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    job_id = uuid.uuid4().hex
    with _lock:
        if request_id:
            existing = next((j for j in _jobs.values() if j.get("request_id") == request_id), None)
            if existing:
                if existing["_folder"] != folder or existing["_digest"] != digest:
                    raise HTTPException(409, "request_id は別の内容に使用済みです。新しい UUID を指定してください")
                return {"job_id": existing["job_id"], "status": existing["status"], "request_id": request_id}
        if any(j["status"] in _ACTIVE for j in _jobs.values()):
            raise HTTPException(409, "レイヤーモーションを書き出し中です。完了またはキャンセルを待ってください")
        _directory(folder, "exports", create=True)
        while len(_jobs) >= MAX_HISTORY:
            finished = next((key for key, value in _jobs.items() if value["status"] not in _ACTIVE), None)
            if finished is None:
                break
            del _jobs[finished]
        job = {"job_id": job_id, "video_name": data["video_name"], "status": "queued", "progress": 0,
               "request_id": request_id, "created_at": time.time(), "_folder": folder,
               "_filename": f"motion_{uuid.uuid4().hex}.mp4", "_digest": digest,
               "_cancel": threading.Event(), "_process": None}
        _jobs[job_id] = job
        thread = threading.Thread(target=_run, args=(job_id, project), name=f"layer-motion-{job_id[:8]}", daemon=True)
        try:
            thread.start()
        except Exception:
            del _jobs[job_id]
            raise
    return {"job_id": job_id, "status": "queued", "request_id": request_id}


@router.post("/export")
async def export_project(request: Request):
    return await run_in_threadpool(_submit, await _body(request))


@router.get("/status")
def status(job_id: str = "", request_id: str = ""):
    request_id = _request_id(request_id)
    if not job_id and not request_id:
        raise HTTPException(400, "job_id または request_id を指定してください")
    with _lock:
        job = _jobs.get(job_id) if job_id else next(
            (j for j in _jobs.values() if j.get("request_id") == request_id), None
        )
        if job is None or (request_id and job.get("request_id") != request_id):
            raise HTTPException(404, "job が見つかりません（再起動後は再書き出しできます）")
        return _public(job)


@router.post("/cancel")
def cancel_export(data: dict):
    job_id = data.get("job_id")
    if not isinstance(job_id, str):
        raise HTTPException(400, "job_id を指定してください")
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "job が見つかりません")
        if job["status"] in _ACTIVE:
            job["_cancel"].set()
            job["status"] = "cancelling"
            process = job.get("_process")
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
        return _public(job)


@router.get("/file/{video_name}/{filename}")
def export_file(video_name: str, filename: str, job_id: str = "", scope: str = ""):
    if job_id:
        with _lock:
            job = _jobs.get(job_id)
            if not job or job["status"] != "completed" or job["video_name"] != video_name or job["_filename"] != filename:
                raise HTTPException(404, "完成した job とファイルが一致しません")
            folder = job["_folder"]
    else:
        folder = _folder(video_name, scope)
    return FileResponse(_file(folder, "exports", filename), media_type="video/mp4", filename=filename)

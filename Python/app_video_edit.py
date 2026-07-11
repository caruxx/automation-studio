#!/usr/bin/env python3
"""Non-destructive ffmpeg helper used by Automation Studio.

Every operation writes a new ``*_edited_N`` file.  The module can run as a
CLI worker (JSON progress on stdout) or be imported by the API queue.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import hashlib
from pathlib import Path
from typing import Any, Callable

from resource_lock import acquire_resource

EDIT_TIMEOUT_SEC = int(os.environ.get("APP_VIDEO_EDIT_TIMEOUT", "21600"))
PROBE_TIMEOUT_SEC = 30


def _probe(path: Path) -> dict[str, Any]:
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,duration",
           "-of", "json", str(path)]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=PROBE_TIMEOUT_SEC, check=True).stdout)


def duration_of(path: Path) -> float:
    data = _probe(path)
    raw = (data.get("format") or {}).get("duration")
    if raw not in (None, "", "N/A"):
        return float(raw)
    durations = []
    for stream in data.get("streams") or []:
        value = stream.get("duration")
        if value not in (None, "", "N/A"):
            durations.append(float(value))
    return max(durations, default=0.0)


def new_output(input_path: Path, suffix: str | None = None) -> Path:
    ext = suffix or input_path.suffix or ".mp4"
    if not ext.startswith("."):
        ext = "." + ext
    for n in range(1, 10000):
        candidate = input_path.with_name(f"{input_path.stem}_edited_{n}{ext}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("出力ファイル番号を確保できません")


def _pos(value: str) -> str:
    values = {
        "center": "(W-w)/2:(H-h)/2", "top-left": "20:20",
        "top-right": "W-w-20:20", "bottom-left": "20:H-h-20",
        "bottom-right": "W-w-20:H-h-20",
    }
    return values.get(value, value if re.fullmatch(r"[-+*/().A-Za-z0-9 ]+:[-+*/().A-Za-z0-9 ]+", value or "") else values["center"])


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", r"\\").replace(":", r"\:").replace("'", r"\'").replace("%", r"\%")


def build_command(operation: str, params: dict[str, Any], output: Path) -> list[str]:
    src = Path(params["input_path"])
    base = ["nice", "-n", str(params.get("nice", 10)), "ffmpeg", "-hide_banner", "-y"]
    op = operation
    if op == "trim":
        start, end = float(params.get("start", 0)), params.get("end")
        cmd = base + ["-ss", str(start), "-i", str(src)]
        if end not in (None, ""):
            cmd += ["-t", str(max(0.001, float(end) - start))]
        if params.get("reencode"):
            cmd += ["-c:v", "libx264", "-preset", "medium", "-c:a", "aac"]
        else:
            cmd += ["-c", "copy"]
        return cmd + [str(output)]
    if op == "concat":
        inputs = [Path(x) for x in params.get("input_paths") or []]
        if len(inputs) < 2:
            raise ValueError("concat は2ファイル以上が必要です")
        same = len({tuple((s.get("codec_type"), s.get("codec_name")) for s in (_probe(p).get("streams") or [])) for p in inputs}) == 1
        list_file = output.with_suffix(output.suffix + ".concat.txt")
        list_file.write_text("".join(f"file '{str(p).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n" for p in inputs), encoding="utf-8")
        params["_cleanup"] = [str(list_file)]
        return base + ["-f", "concat", "-safe", "0", "-i", str(list_file)] + (["-c", "copy"] if same else ["-c:v", "libx264", "-c:a", "aac"]) + [str(output)]
    if op == "loop_to_duration":
        dur, fade = float(params["duration"]), float(params.get("fade_out", 3))
        vf = f"fade=t=out:st={max(0, dur-fade)}:d={fade}"
        af = f"afade=t=out:st={max(0, dur-fade)}:d={fade}"
        return base + ["-stream_loop", "-1", "-i", str(src), "-t", str(dur), "-vf", vf, "-af", af, "-c:v", "libx264", "-preset", "medium", "-c:a", "aac", str(output)]
    if op == "fade":
        dur = duration_of(src); vi=float(params.get("video_in", 0)); vo=float(params.get("video_out", 0)); ai=float(params.get("audio_in", vi)); ao=float(params.get("audio_out", vo))
        vf = ([f"fade=t=in:st=0:d={vi}"] if vi else []) + ([f"fade=t=out:st={max(0,dur-vo)}:d={vo}"] if vo else [])
        af = ([f"afade=t=in:st=0:d={ai}"] if ai else []) + ([f"afade=t=out:st={max(0,dur-ao)}:d={ao}"] if ao else [])
        cmd=base+["-i",str(src)];
        if vf: cmd += ["-vf", ",".join(vf)]
        if af: cmd += ["-af", ",".join(af)]
        return cmd+["-c:v","libx264","-c:a","aac",str(output)]
    if op == "replace_audio":
        audio=Path(params["audio_path"]); db=float(params.get("volume_db",0))
        return base+["-i",str(src),"-stream_loop","-1","-i",str(audio),"-map","0:v:0","-map","1:a:0","-filter:a",f"volume={db}dB","-c:v","copy","-c:a","aac","-shortest",str(output)]
    if op == "burn_overlay":
        overlay_type=params.get("overlay_type","image"); pos=_pos(str(params.get("position","center"))); opacity=float(params.get("opacity",1)); size=int(params.get("size",64))
        if overlay_type == "image":
            image=Path(params["overlay_path"]); filt=f"[1:v]scale={size}:-1,format=rgba,colorchannelmixer=aa={opacity}[ov];[0:v][ov]overlay={pos}"
            return base+["-i",str(src),"-i",str(image),"-filter_complex",filt,"-c:v","libx264","-c:a","copy",str(output)]
        text=_escape_drawtext(str(params.get("text", ""))); x,y=pos.split(":",1)
        filt=f"drawtext=text='{text}':fontsize={size}:fontcolor=white@{opacity}:x={x}:y={y}"
        return base+["-i",str(src),"-vf",filt,"-c:v","libx264","-c:a","copy",str(output)]
    if op == "extract_frame":
        return base+["-ss",str(float(params.get("time",0))),"-i",str(src),"-frames:v","1","-q:v","2",str(output)]
    if op == "convert":
        vf=[]
        if params.get("resolution"): vf.append(f"scale={str(params['resolution']).replace('x',':')}")
        if params.get("fps"): vf.append(f"fps={float(params['fps'])}")
        cmd=base+["-i",str(src)]
        if vf: cmd += ["-vf",",".join(vf)]
        return cmd+["-c:v","libx264","-c:a","aac",str(output)]
    raise ValueError(f"未知の操作: {op}")


def run_edit(operation: str, params: dict[str, Any], progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    src=Path(params["input_path"]).expanduser().resolve()
    if not src.is_file(): raise FileNotFoundError(src)
    params=dict(params); params["input_path"]=str(src)
    ext = ".jpg" if operation == "extract_frame" else ("."+str(params.get("format","mp4")).lstrip(".") if operation == "convert" else src.suffix)
    output=new_output(src, ext)
    cmd=build_command(operation, params, output)
    total = float(params.get("duration") or (1 if operation=="extract_frame" else duration_of(src)) or 1)
    started=time.time(); state={"status":"running","progress":0.0,"output_path":str(output),"command":shlex.join(cmd)}
    if progress: progress(state)
    lock=acquire_resource("ffmpeg_edit", owner=f"video-edit:{operation}", blocking=True)
    proc=None
    try:
        proc=subprocess.Popen(cmd[:-1]+["-progress","pipe:1","-nostats",cmd[-1]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        deadline=started+EDIT_TIMEOUT_SEC
        for raw in iter(proc.stdout.readline, ""):
            if time.time()>deadline:
                proc.terminate(); time.sleep(2)
                if proc.poll() is None: proc.kill()
                raise TimeoutError(f"ffmpeg watchdog timeout ({EDIT_TIMEOUT_SEC}s)")
            line=raw.strip()
            if line.startswith("out_time_ms="):
                value = line.split("=", 1)[1]
                if value in ("", "N/A"):
                    continue
                pct=min(99.0, float(value)/1_000_000/total*100)
                state.update(progress=round(pct,1), updated_at=time.time())
                if progress: progress(dict(state))
        code=proc.wait(timeout=10)
        if code != 0: raise RuntimeError(f"ffmpeg 終了コード {code}")
        state.update(status="completed", progress=100.0, finished_at=time.time(), size=output.stat().st_size)
        if progress: progress(dict(state))
        return state
    except Exception:
        if output.exists(): output.unlink()
        raise
    finally:
        lock.release()
        for p in params.get("_cleanup",[]):
            Path(p).unlink(missing_ok=True)


def run_queue(input_path: str, operations: list[dict[str, Any]], progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Apply existing operations in order, retaining only the final output."""
    if not operations:
        raise ValueError("操作キューが空です")
    original = Path(input_path).expanduser().resolve()
    current = original
    intermediates: list[Path] = []
    try:
        for index, item in enumerate(operations):
            op = str(item.get("operation") or "")
            params = dict(item.get("params") or {})
            params["input_path"] = str(current)
            def update(state: dict[str, Any], i=index, name=op):
                if progress:
                    mapped = dict(state)
                    mapped["operation"] = name
                    mapped["operation_index"] = i
                    mapped["progress"] = round((i + float(state.get("progress", 0))/100) / len(operations) * 100, 1)
                    progress(mapped)
            result = run_edit(op, params, update)
            next_path = Path(result["output_path"])
            if current != original:
                intermediates.append(current)
            current = next_path
        for path in intermediates:
            path.unlink(missing_ok=True)
        result.update(output_path=str(current), status="completed", progress=100.0, operations=len(operations))
        if progress: progress(dict(result))
        return result
    except Exception:
        for path in intermediates + ([current] if current != original else []):
            path.unlink(missing_ok=True)
        raise


def generate_editor_assets(input_path: str, cache_root: Path, count: int = 8) -> dict[str, Any]:
    """Generate a cached thumbnail strip and waveform under the app cache."""
    src = Path(input_path).expanduser().resolve()
    if not src.is_file(): raise FileNotFoundError(src)
    count = max(6, min(int(count), 10))
    stat = src.stat()
    key = hashlib.sha256(f"{src}:{stat.st_mtime_ns}:{stat.st_size}:{count}".encode()).hexdigest()[:20]
    folder = cache_root / key
    folder.mkdir(parents=True, exist_ok=True)
    duration = duration_of(src)
    frames = [folder / f"frame_{i:02d}.jpg" for i in range(count)]
    waveform = folder / "waveform.png"
    if all(p.exists() for p in frames) and waveform.exists():
        return {"cache_key": key, "duration": duration, "frames": frames, "waveform": waveform, "cached": True}
    lock = acquire_resource("ffmpeg_edit", owner="video-edit:assets", blocking=True)
    try:
        deadline = time.time() + EDIT_TIMEOUT_SEC
        for i, out in enumerate(frames):
            if out.exists(): continue
            at = duration * (i + .5) / count
            subprocess.run(["nice","-n","10","ffmpeg","-hide_banner","-loglevel","error","-y","-ss",str(at),"-i",str(src),"-frames:v","1","-vf","scale=240:-2","-q:v","4",str(out)], check=True, timeout=max(1, int(deadline-time.time())))
        if not waveform.exists():
            subprocess.run(["nice","-n","10","ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(src),"-filter_complex","showwavespic=s=1200x100:colors=0x22c7b8","-frames:v","1",str(waveform)], check=True, timeout=max(1, int(deadline-time.time())))
    finally:
        lock.release()
    return {"cache_key": key, "duration": duration, "frames": frames, "waveform": waveform, "cached": False}


def main() -> int:
    ap=argparse.ArgumentParser(description="Automation Studio かんたん動画編集")
    ap.add_argument("operation", choices=["trim","concat","loop_to_duration","fade","replace_audio","burn_overlay","extract_frame","convert"])
    ap.add_argument("--params", required=True, help="JSON オブジェクト")
    args=ap.parse_args(); params=json.loads(args.params)
    try:
        result=run_edit(args.operation, params, lambda s: print(json.dumps(s,ensure_ascii=False),flush=True))
        print(json.dumps(result,ensure_ascii=False)); return 0
    except Exception as e:
        print(json.dumps({"status":"failed","error":str(e)},ensure_ascii=False),flush=True); return 1

if __name__ == "__main__": raise SystemExit(main())

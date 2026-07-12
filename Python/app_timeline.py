"""Assembly timeline model shared by the API and ffmpeg renderer."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
import re
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TIMELINE_NAME = "vol_timeline.json"
VERSION = 2
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
NOW_PLAYING_DEFAULTS = {
    "enabled": False, "mode": "intro", "intro_seconds": 8.0,
    "first_title_delay_seconds": 0.0,
    "font_path": "", "font_name": "Hiragino Sans", "position": "bottom-center",
    "size": 48, "color": "#ffffff", "border_color": "#000000", "border_width": 2,
    "opacity": 1.0, "fade_in": 0.4, "fade_out": 0.4, "margin": 64,
}
VISUALIZER_DEFAULTS = {
    "enabled": False, "pattern": "bars", "position": "bottom-center",
    "margin": 64, "width_percent": 60.0, "height_px": 160,
    "color_mode": "single", "color1": "#ffffff", "color2": "#7c3aed",
    "opacity": 0.75,
    "loop_seconds": 20.0,
}


def display_title(filename: str) -> str:
    stem = Path(str(filename or "")).stem
    return re.sub(r"^\s*(?:track\s*)?\d{1,3}\s*[-_. )]+\s*", "", stem, flags=re.I).strip() or stem


def probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=20)
        return max(0.0, float((r.stdout or "0").strip() or 0))
    except Exception:
        return 0.0


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush(); os.fsync(f.fileno())
        os.replace(raw, path)
    finally:
        if os.path.exists(raw): os.unlink(raw)


def _channel_config(folder: Path) -> dict:
    p = folder.parent / ".app_channel_config.json"
    try: return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception: return {}


def _selected_images(folder: Path) -> tuple[Path | None, list[Path]]:
    p = folder / "selected_images.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            main = folder / d.get("main", "") if d.get("main") else None
            subs = [folder / x for x in d.get("sub", []) if (folder / x).is_file()]
            if main and not main.is_file(): main = None
            if main or subs: return main, subs
        except Exception: pass
    num = folder.name.split("_", 1)[0]
    main = next((folder / f"vol{num}{e}" for e in (".jpg", ".png") if (folder / f"vol{num}{e}").is_file()), None)
    subs = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS and p.stem.startswith(f"vol{num}-")]
    return main, sorted(subs)


def _audio_files(folder: Path) -> list[Path]:
    processed = folder / "music"
    original = folder / "original_music"
    root = processed if processed.is_dir() and any(processed.glob("*.mp3")) else original
    if not root.is_dir(): return []
    files = [p for p in root.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    cfg = _channel_config(folder)
    ffr = cfg.get("ffrender") if isinstance(cfg.get("ffrender"), dict) else {}
    if str(ffr.get("song_order") or "") == "z_desc_then_random":
        rng = random.SystemRandom()
        priority: dict[int, list[Path]] = {}
        normal = []
        for path in files:
            match = re.match(r"^(z+)_", path.name, flags=re.I)
            (priority.setdefault(len(match.group(1)), []).append(path) if match else normal.append(path))
        ordered = []
        for count in sorted(priority, reverse=True):
            rng.shuffle(priority[count])
            ordered.extend(priority[count])
        rng.shuffle(normal)
        return ordered + normal
    return sorted(files, key=lambda p: p.name.lower())


def _scene_text(folder: Path, cfg: dict) -> list[dict]:
    rows = []
    for key, label in (("scene_en.txt", "見出し"), ("scene_ja.txt", "シーンテキスト")):
        p = folder / key
        if p.exists() and p.read_text(encoding="utf-8").strip():
            rows.append({"id": key, "label": label, "text": p.read_text(encoding="utf-8").strip(), "start": 0.0, "end": None})
    if not rows and cfg.get("scene_text_enabled"):
        rows.append({"id": "scene-setting", "label": "シーンテキスト", "text": "設定済み", "start": 0.0, "end": None})
    return rows


def build_initial(folder: Path) -> dict:
    folder = Path(folder)
    cfg = _channel_config(folder)
    audio, cursor = [], 0.0
    files = _audio_files(folder)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(files)))) as pool:
        durations = list(pool.map(probe_duration, files))
    for i, (p, dur) in enumerate(zip(files, durations)):
        audio.append({"id": hashlib.sha1(str(p).encode()).hexdigest()[:12], "filename": p.name, "display_name": display_title(p.name),
                      "path": str(p.relative_to(folder)), "duration": dur, "start": cursor,
                      "end": cursor + dur, "favorite": p.name.startswith("z"), "gain_db": 0.0,
                      "crossfade_sec": 0.0, "crossfade_curve": "equal_power"})
        cursor += dur
    target = float(cfg.get("default_duration_sec") or 10800)
    total = cursor
    main, subs = _selected_images(folder)
    segs = []
    def add(p, s, e):
        if p and e > s: segs.append({"id": f"visual-{len(segs)+1}", "image_path": str(p.relative_to(folder)), "start": s, "end": e, "crossfade_sec": 0.0})
    images = [p for p in [main, *subs] if p]
    images = list(dict.fromkeys(images))
    if total and len(images) == 1:
        add(images[0], 0, total)
    elif total and images:
        for i, p in enumerate(images):
            s = total * i / len(images); e = total if i == len(images)-1 else total * (i+1) / len(images)
            add(p, s, e)
    text = _scene_text(folder, cfg)
    for x in text: x["end"] = total
    channel_np = cfg.get("now_playing") if isinstance(cfg.get("now_playing"), dict) else {}
    now_playing = dict(NOW_PLAYING_DEFAULTS); now_playing.update(channel_np)
    channel_visualizer = cfg.get("visualizer") if isinstance(cfg.get("visualizer"), dict) else {}
    visualizer = dict(VISUALIZER_DEFAULTS); visualizer.update(channel_visualizer)
    return {"version": VERSION, "video_name": folder.name, "source": "derived", "total_duration": total,
            "target_duration": target, "audio_clips": audio, "excluded": [], "visual_segments": segs,
            "video_tracks": [{"id": "V1", "segments": segs}], "text_lane": text,
            "text_tracks": [{"id": "T1", "clips": text}], "crossfade_sec": 0.0, "audio_crossfade_sec": 0.0,
            "audio_crossfade_curve": "equal_power", "track_states": {
                "A1": {"muted": False}, "V1": {"hidden": False}, "T1": {"hidden": False},
            }, "now_playing": now_playing, "visualizer": visualizer, "updated_at": None}


def normalize(model: dict) -> dict:
    cursor = 0.0
    excluded = set(model.get("excluded") or [])
    global_fade = max(0.0, float(model.get("audio_crossfade_sec") or 0))
    for index, c in enumerate(model.get("audio_clips", [])):
        try:
            gain_db = float(c.get("gain_db", 0) or 0)
        except (TypeError, ValueError):
            gain_db = 0.0
        if not math.isfinite(gain_db):
            gain_db = 0.0
        c["gain_db"] = max(-60.0, min(12.0, gain_db))
        c.setdefault("display_name", display_title(c.get("filename", "")))
        if c.get("id") in excluded: continue
        fade = 0.0 if index == 0 else max(0.0, float(c.get("crossfade_sec", global_fade) or 0))
        fade = min(fade, float(c.get("duration") or 0), cursor)
        cursor -= fade
        c["start"] = cursor; cursor += float(c.get("duration") or 0); c["end"] = cursor
    model["total_duration"] = cursor
    # Authored boundaries must survive a save even when both sides initially use
    # the same image; otherwise a newly split V1 clip is merged immediately.
    segments = [dict(seg) for seg in sorted(model.get("visual_segments") or [], key=lambda x: float(x.get("start") or 0))]
    model["visual_segments"] = segments
    tracks = model.get("video_tracks") if isinstance(model.get("video_tracks"), list) else []
    if not tracks:
        tracks = [{"id": "V1", "segments": segments}]
    tracks[0]["id"] = "V1"; tracks[0]["segments"] = segments
    model["video_tracks"] = tracks
    text_tracks = model.get("text_tracks") if isinstance(model.get("text_tracks"), list) else []
    if not text_tracks:
        text_tracks = [{"id": "T1", "clips": model.get("text_lane") or []}]
    text_tracks[0]["id"] = "T1"
    if not isinstance(text_tracks[0].get("clips"), list):
        text_tracks[0]["clips"] = model.get("text_lane") or []
    # Legacy migrations and UI tests could leave empty T2+ tracks behind. Keep
    # authored content and explicitly user-created empty tracks, but discard
    # unmarked empty residue. The default model remains A1/V1/T1 only.
    text_tracks = [text_tracks[0]] + [
        track for track in text_tracks[1:]
        if (isinstance(track.get("clips"), list) and track["clips"])
        or track.get("user_created") is True
    ]
    for track in text_tracks:
        for clip in track.get("clips") or []:
            effect = str(clip.get("effect") or "none").strip().lower()
            clip["effect"] = effect if effect in {"none", "typewriter"} else "none"
            try:
                speed = float(clip.get("effect_speed", 12) or 12)
            except (TypeError, ValueError):
                speed = 12.0
            if not math.isfinite(speed):
                speed = 12.0
            clip["effect_speed"] = max(1.0, min(60.0, speed))
            clip["type_sound"] = clip.get("type_sound") is True
            try:
                volume = float(clip.get("type_sound_volume", 0.5))
            except (TypeError, ValueError):
                volume = 0.5
            if not math.isfinite(volume):
                volume = 0.5
            clip["type_sound_volume"] = max(0.0, min(1.0, volume))
    # text_tracks is the canonical multi-track model; text_lane stays as a
    # backward-compatible alias for older UI/render consumers.
    model["text_lane"] = text_tracks[0]["clips"]
    model["text_tracks"] = text_tracks
    raw_states = model.get("track_states") if isinstance(model.get("track_states"), dict) else {}
    states = {}
    for track_id, raw in raw_states.items():
        if not re.fullmatch(r"[AVT]\d+", str(track_id)) or not isinstance(raw, dict):
            continue
        if str(track_id).startswith("A"):
            states[str(track_id)] = {"muted": raw.get("muted") is True}
        else:
            states[str(track_id)] = {"hidden": raw.get("hidden") is True}
    states.setdefault("A1", {"muted": False})
    states.setdefault("V1", {"hidden": False})
    states.setdefault("T1", {"hidden": False})
    model["track_states"] = states
    model["version"] = VERSION
    # updated_at はここでは触らない。毎回現在時刻を刻むと load() 経由の GET/PUT 検査で
    # 値が揺れ、楽観ロックが常に 409 になる。刻印は save() の書き込み直前のみ。
    return model


def load(folder: Path, *, persist_initial: bool = False) -> dict:
    p = Path(folder) / TIMELINE_NAME
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        before_version = d.get("version")
        before_segments = json.dumps(d.get("visual_segments") or [], sort_keys=True)
        before_text_tracks = json.dumps(d.get("text_tracks") or [], sort_keys=True)
        before_track_states = json.dumps(d.get("track_states") or {}, sort_keys=True)
        missing_tracks = not isinstance(d.get("video_tracks"), list) or not d.get("video_tracks") or not isinstance(d.get("text_tracks"), list) or not d.get("text_tracks")
        if "now_playing" not in d:
            cfg = _channel_config(Path(folder)); np = dict(NOW_PLAYING_DEFAULTS)
            if isinstance(cfg.get("now_playing"), dict): np.update(cfg["now_playing"])
            d["now_playing"] = np
        # Channel settings are the template; fields authored in this vol are
        # the final override. Merge partial legacy payloads instead of making
        # every client resend the complete visualizer object.
        cfg = _channel_config(Path(folder)); visualizer = dict(VISUALIZER_DEFAULTS)
        if isinstance(cfg.get("visualizer"), dict): visualizer.update(cfg["visualizer"])
        if isinstance(d.get("visualizer"), dict): visualizer.update(d["visualizer"])
        d["visualizer"] = visualizer
        d["source"] = "saved"
        normalized = normalize(d)
        if (before_version != VERSION or missing_tracks
                or before_segments != json.dumps(normalized.get("visual_segments") or [], sort_keys=True)
                or before_text_tracks != json.dumps(normalized.get("text_tracks") or [], sort_keys=True)
                or before_track_states != json.dumps(normalized.get("track_states") or {}, sort_keys=True)):
            atomic_write(p, normalized)
        return normalized
    d = build_initial(Path(folder))
    if persist_initial: atomic_write(p, d)
    return d


def save(folder: Path, model: dict) -> dict:
    current = build_initial(Path(folder))
    allowed = {"version", "video_name", "target_duration", "audio_clips", "excluded", "visual_segments", "video_tracks", "text_lane", "text_tracks", "crossfade_sec", "audio_crossfade_sec", "audio_crossfade_curve", "track_states", "now_playing", "visualizer"}
    incoming = {k: v for k, v in model.items() if k in allowed}
    # Older clients only sent text_lane.  build_initial() already has text_tracks,
    # so without this promotion normalize() would let the generated empty T1 win.
    if "text_lane" in incoming and "text_tracks" not in incoming:
        lane = incoming.get("text_lane")
        incoming["text_tracks"] = [{"id": "T1", "clips": lane if isinstance(lane, list) else []}]
    if isinstance(incoming.get("visualizer"), dict):
        visualizer = dict(current.get("visualizer") or VISUALIZER_DEFAULTS)
        visualizer.update(incoming["visualizer"])
        incoming["visualizer"] = visualizer
    current.update(incoming)
    current["source"] = "saved"
    normalize(current)
    current["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    atomic_write(Path(folder) / TIMELINE_NAME, current)
    return current

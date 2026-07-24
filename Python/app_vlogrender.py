#!/usr/bin/env python3
"""Render a folder of video clips and photos as a single vlog MP4."""

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
FFMPEG_TIMEOUT = 4 * 60 * 60
FFPROBE_TIMEOUT = 60
FPS = 30
GOP_FRAMES = 150
VIDEO_BITRATE_MBPS = None
VIDEO_ENCODER = "libx264"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}
FONT_FILES = {
    "jp": Path("/System/Library/Fonts/ヒラギノ明朝 ProN.ttc"),
    "en": Path("/System/Library/Fonts/Supplemental/SnellRoundhand.ttc"),
    "serif": Path("/System/Library/Fonts/Supplemental/Georgia.ttf"),
}
ASPECT_SIZES = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
}


def _bitrate_to_crf(mbps: float) -> int:
    points = ((40, 12), (20, 14), (16, 16), (10, 18), (8, 19), (6, 21))
    return min(points, key=lambda pair: abs(pair[0] - mbps))[1]


def _video_encode_args(crf: int) -> List[str]:
    """Keep video encoding arguments aligned with app_ffrender.py."""
    if VIDEO_BITRATE_MBPS is None:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(crf)]
    if VIDEO_ENCODER == "h264_videotoolbox":
        return [
            "-c:v",
            VIDEO_ENCODER,
            "-b:v",
            "{:g}M".format(VIDEO_BITRATE_MBPS),
            "-profile:v",
            "high",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        str(_bitrate_to_crf(VIDEO_BITRATE_MBPS)),
    ]


def _closed_gop_args(frames: int) -> List[str]:
    """Keep closed-GOP arguments aligned with app_ffrender.py."""
    if VIDEO_BITRATE_MBPS is None or VIDEO_ENCODER == "libx264":
        return [
            "-x264-params",
            "keyint={0}:min-keyint={0}:scenecut=0:bframes=0".format(frames),
        ]
    return ["-bf", "0", "-g", str(frames)]


def _run_ff(cmd: List[str], label: str) -> float:
    """Run ffmpeg and fail loudly, following app_ffrender.py semantics."""
    t0 = time.time()
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT,
    )
    elapsed = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(
            "[{0}] ffmpeg failed (rc={1})\ncmd: {2} ...\n{3}".format(
                label,
                result.returncode,
                " ".join(str(part) for part in cmd[:16]),
                (result.stderr or "")[-4000:],
            )
        )
    print("  OK {0}: {1:.1f}s".format(label, elapsed), file=sys.stderr)
    return elapsed


def _finite_float(
    value: Any,
    label: str,
    minimum: Optional[float] = None,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("{0} must be a number".format(label))
    if not math.isfinite(number):
        raise ValueError("{0} must be finite".format(label))
    if minimum is not None and number < minimum:
        raise ValueError("{0} must be at least {1}".format(label, minimum))
    return number


def _probe_json(path: Path) -> Dict[str, Any]:
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=FFPROBE_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffprobe failed for {0}\n{1}".format(path, (result.stderr or "")[-2000:])
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON for {0}: {1}".format(path, exc))


def _rotation_from_stream(stream: Dict[str, Any]) -> int:
    values = []
    tags = stream.get("tags") or {}
    if tags.get("rotate") is not None:
        values.append(tags.get("rotate"))
    for side_data in stream.get("side_data_list") or []:
        if side_data.get("rotation") is not None:
            values.append(side_data.get("rotation"))
        display_matrix = str(side_data.get("displaymatrix") or "")
        match = re.search(r"rotation\s+of\s+(-?\d+(?:\.\d+)?)", display_matrix)
        if match:
            values.append(match.group(1))
    for value in values:
        try:
            normalized = int(round(float(value))) % 360
        except (TypeError, ValueError):
            continue
        if normalized in (0, 90, 180, 270):
            return normalized
    return 0


def _stream_duration(stream: Dict[str, Any], data: Dict[str, Any]) -> float:
    candidates = [
        stream.get("duration"),
        (data.get("format") or {}).get("duration"),
    ]
    tags = stream.get("tags") or {}
    if tags.get("DURATION"):
        raw = str(tags["DURATION"])
        match = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", raw)
        if match:
            return (
                int(match.group(1)) * 3600
                + int(match.group(2)) * 60
                + float(match.group(3))
            )
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


def probe_media(path: Path, kind: str, photo_sec: float, clip_max_sec: float) -> Dict[str, Any]:
    data = _probe_json(path)
    stream = next(
        (item for item in data.get("streams") or [] if item.get("codec_type") == "video"),
        None,
    )
    if stream is None:
        raise RuntimeError("no video stream found: {0}".format(path))
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise RuntimeError("invalid dimensions for {0}: {1}x{2}".format(path, width, height))
    rotation = _rotation_from_stream(stream)
    effective_width, effective_height = width, height
    if rotation in (90, 270):
        effective_width, effective_height = height, width
    source_duration = None
    if kind == "video":
        source_duration = _stream_duration(stream, data)
        if source_duration <= 0:
            raise RuntimeError("invalid or missing duration for video: {0}".format(path))
        adopted_duration = min(source_duration, clip_max_sec)
    else:
        adopted_duration = photo_sec
    orientation = (
        "landscape"
        if effective_width > effective_height
        else "portrait"
        if effective_height > effective_width
        else "square"
    )
    return {
        "path": str(path),
        "name": path.name,
        "kind": kind,
        "width": width,
        "height": height,
        "rotation": rotation,
        "effective_width": effective_width,
        "effective_height": effective_height,
        "orientation": orientation,
        "source_duration": source_duration,
        "adopted_duration": adopted_duration,
    }


def scan_media(
    folder: Path,
    order: str,
    photo_sec: float,
    clip_max_sec: float,
    excluded_paths: Sequence[Path],
) -> List[Dict[str, Any]]:
    excluded = set()
    for path in excluded_paths:
        try:
            excluded.add(path.resolve())
        except OSError:
            pass
    candidates = []
    for path in folder.iterdir():
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in excluded:
            continue
        suffix = path.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            candidates.append((path, "video"))
        elif suffix in PHOTO_EXTENSIONS:
            candidates.append((path, "photo"))
    if order == "name":
        candidates.sort(key=lambda item: (item[0].name.casefold(), item[0].name))
    else:
        candidates.sort(key=lambda item: (item[0].stat().st_mtime_ns, item[0].name.casefold()))
    if not candidates:
        raise RuntimeError("no video clips or photos found in {0}".format(folder))
    return [
        probe_media(path, kind, photo_sec, clip_max_sec)
        for path, kind in candidates
    ]


def _probe_duration(path: Path) -> float:
    data = _probe_json(path)
    format_duration = (data.get("format") or {}).get("duration")
    try:
        duration = float(format_duration)
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        video_stream = next(
            (item for item in data.get("streams") or [] if item.get("codec_type") == "video"),
            {},
        )
        duration = _stream_duration(video_stream, data)
    if duration <= 0:
        raise RuntimeError("could not determine rendered duration: {0}".format(path))
    return duration


def _scratch_dir(folder: Path) -> Path:
    safe_name = re.sub(r"[^0-9A-Za-z._-]+", "_", folder.name).strip("._") or "media"
    digest = hashlib.sha1(str(folder.resolve()).encode("utf-8")).hexdigest()[:10]
    scratch = Path("/tmp/vlogrender") / "{0}_{1}".format(safe_name, digest)
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _base_composite_filter(width: int, height: int) -> str:
    return (
        "[base]split=2[bgsrc][fgsrc];"
        "[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,"
        "crop={w}:{h},boxblur=luma_radius=24:luma_power=2[bg];"
        "[fgsrc]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1[composite]"
    ).format(w=width, h=height)


def normalize_media(
    media: Dict[str, Any],
    index: int,
    scratch: Path,
    width: int,
    height: int,
) -> Path:
    source = Path(media["path"])
    duration = float(media["adopted_duration"])
    frames = max(1, int(round(duration * FPS)))
    output = scratch / "normalized_{0:03d}.ts".format(index)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    if media["kind"] == "photo":
        cmd += ["-loop", "1", "-framerate", str(FPS), "-i", str(source)]
        zoom_step = 0.08 / max(1, frames - 1)
        filter_graph = (
            "[0:v]setpts=PTS-STARTPTS[base];"
            + _base_composite_filter(width, height)
            + ";[composite]zoompan="
            "z='min(1.08,1+on*{step:.10f})':"
            "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            "d=1:s={w}x{h}:fps={fps},setsar=1,format=yuv420p[outv]"
        ).format(step=zoom_step, w=width, h=height, fps=FPS)
        cmd += [
            "-filter_complex",
            filter_graph,
            "-map",
            "[outv]",
            "-frames:v",
            str(frames),
        ]
    else:
        filter_graph = (
            "[0:v]trim=duration={duration:.6f},setpts=PTS-STARTPTS[base];"
            + _base_composite_filter(width, height)
            + ";[composite]fps={fps},setsar=1,format=yuv420p[outv]"
        ).format(duration=duration, fps=FPS)
        cmd += [
            "-i",
            str(source),
            "-filter_complex",
            filter_graph,
            "-map",
            "[outv]",
            "-t",
            "{0:.6f}".format(duration),
        ]
    cmd += (
        _video_encode_args(18)
        + _closed_gop_args(GOP_FRAMES)
        + [
            "-r",
            str(FPS),
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-f",
            "mpegts",
            str(output),
        ]
    )
    _run_ff(cmd, "normalize {0}".format(source.name))
    return output


def concatenate_media(
    normalized: List[Path],
    durations: List[float],
    xfade_sec: float,
    scratch: Path,
) -> Tuple[Path, float]:
    if len(normalized) != len(durations) or not normalized:
        raise RuntimeError("internal error: normalized media and durations do not match")
    if len(normalized) == 1:
        return normalized[0], durations[0]
    for index, duration in enumerate(durations):
        if xfade_sec > 0 and duration <= xfade_sec:
            raise ValueError(
                "xfade duration {0:.3f}s must be shorter than clip {1} ({2:.3f}s)".format(
                    xfade_sec, index + 1, duration
                )
            )
    output = scratch / "joined.mp4"
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    for path in normalized:
        cmd += ["-i", str(path)]
    filters = []
    for index in range(len(normalized)):
        filters.append(
            "[{0}:v]fps={1},settb=AVTB,setpts=PTS-STARTPTS,format=yuv420p[v{0}]".format(
                index, FPS
            )
        )
    if xfade_sec > 0:
        current = "v0"
        cumulative = durations[0]
        for index in range(1, len(normalized)):
            offset = cumulative - xfade_sec
            output_label = "xf{0}".format(index)
            filters.append(
                "[{left}][v{index}]xfade=transition=fade:duration={duration:.6f}:"
                "offset={offset:.6f}[{output}]".format(
                    left=current,
                    index=index,
                    duration=xfade_sec,
                    offset=offset,
                    output=output_label,
                )
            )
            current = output_label
            cumulative += durations[index] - xfade_sec
    else:
        inputs = "".join("[v{0}]".format(index) for index in range(len(normalized)))
        filters.append(
            "{0}concat=n={1}:v=1:a=0[concatv]".format(inputs, len(normalized))
        )
        current = "concatv"
        cumulative = sum(durations)
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[{0}]".format(current),
    ]
    cmd += (
        _video_encode_args(18)
        + _closed_gop_args(GOP_FRAMES)
        + [
            "-r",
            str(FPS),
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    _run_ff(cmd, "xfade join")
    return output, cumulative


def _escape_filter_path(path: Path) -> str:
    value = str(path)
    value = value.replace("\\", "\\\\")
    value = value.replace(":", "\\:")
    value = value.replace("'", "\\'")
    return value


def load_telops(path: Path, total_duration: float) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("could not read telops JSON {0}: {1}".format(path, exc))
    if not isinstance(raw, list):
        raise ValueError("telops JSON must contain an array")
    telops = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("telop {0} must be an object".format(index))
        text = item.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("telop {0} text must be a non-empty string".format(index))
        start = _finite_float(item.get("start"), "telop {0} start".format(index), 0.0)
        duration = _finite_float(item.get("dur"), "telop {0} dur".format(index), 0.0)
        if duration <= 0:
            raise ValueError("telop {0} dur must be greater than zero".format(index))
        style = str(item.get("style") or "jp")
        position = str(item.get("pos") or "center")
        if style not in FONT_FILES:
            raise ValueError("telop {0} has invalid style: {1}".format(index, style))
        if position not in ("center", "bottom", "top"):
            raise ValueError("telop {0} has invalid pos: {1}".format(index, position))
        if start >= total_duration:
            continue
        end = min(total_duration, start + duration)
        if end <= start:
            continue
        telops.append(
            {
                "text": text,
                "start": start,
                "end": end,
                "style": style,
                "pos": position,
            }
        )
    return telops


def build_drawtext_clauses(
    telops: List[Dict[str, Any]],
    scratch: Path,
    width: int,
    height: int,
) -> List[str]:
    clauses = []
    positions = {
        "top": "max(0\\,min(h-text_h\\,{0}))".format(int(height * 0.10)),
        "center": "max(0\\,min(h-text_h\\,(h-text_h)/2))",
        "bottom": "max(0\\,min(h-text_h\\,h-text_h-{0}))".format(int(height * 0.10)),
    }
    font_size = 72 if width >= 1900 else 56
    for index, telop in enumerate(telops):
        text_file = scratch / "telop_{0:03d}.txt".format(index)
        text_file.write_text(telop["text"], encoding="utf-8")
        font = FONT_FILES[telop["style"]]
        if not font.is_file():
            raise RuntimeError("font file not found: {0}".format(font))
        start = float(telop["start"])
        end = float(telop["end"])
        fade_in = min(0.4, (end - start) / 2.0)
        fade_out = min(0.4, (end - start) / 2.0)
        alpha = (
            "1*if(lt(t,{fi_end:.3f}),(t-{start:.3f})/{fi:.3f},"
            "if(gt(t,{fo_start:.3f}),({end:.3f}-t)/{fo:.3f},1))"
        ).format(
            fi_end=start + fade_in,
            start=start,
            fi=max(fade_in, 0.001),
            fo_start=end - fade_out,
            end=end,
            fo=max(fade_out, 0.001),
        )
        clauses.append(
            "drawtext=fontfile='{font}':textfile='{textfile}':fontsize={size}:"
            "fontcolor=white:alpha='{alpha}':borderw=2:bordercolor=black:"
            "x=max(0\\,min(w-text_w\\,(w-text_w)/2)):y={y}:"
            "enable='between(t,{start:.3f},{end:.3f})'".format(
                font=_escape_filter_path(font),
                textfile=_escape_filter_path(text_file),
                size=font_size,
                alpha=alpha,
                y=positions[telop["pos"]],
                start=start,
                end=end,
            )
        )
    return clauses


def render_final(
    joined: Path,
    output: Path,
    total_duration: float,
    telops: List[Dict[str, Any]],
    bgm: Optional[Path],
    scratch: Path,
    width: int,
    height: int,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined)]
    if bgm is not None:
        cmd += ["-stream_loop", "-1", "-i", str(bgm)]
    clauses = [
        "scale=in_range=auto:out_range=tv",
        "format=yuv420p",
    ] + build_drawtext_clauses(telops, scratch, width, height)
    cmd += ["-vf", ",".join(clauses)]
    cmd += ["-map", "0:v:0"]
    if bgm is not None:
        fade_duration = min(2.0, total_duration)
        fade_start = max(0.0, total_duration - fade_duration)
        audio_filter = (
            "[1:a:0]atrim=duration={total:.6f},asetpts=PTS-STARTPTS,"
            "afade=t=out:st={start:.6f}:d={duration:.6f}[bgma]"
        ).format(total=total_duration, start=fade_start, duration=fade_duration)
        cmd += [
            "-filter_complex",
            audio_filter,
            "-map",
            "[bgma]",
        ]
    cmd += _video_encode_args(18) + _closed_gop_args(GOP_FRAMES)
    cmd += [
        "-r",
        str(FPS),
        "-pix_fmt",
        "yuv420p",
    ]
    if bgm is not None:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-t",
        "{0:.6f}".format(total_duration),
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run_ff(cmd, "final render")


def analysis_payload(
    folder: Path,
    output: Path,
    aspect: str,
    media: List[Dict[str, Any]],
    xfade_sec: float,
) -> Dict[str, Any]:
    expected_duration = sum(float(item["adopted_duration"]) for item in media)
    if len(media) > 1:
        expected_duration -= xfade_sec * (len(media) - 1)
    width, height = ASPECT_SIZES[aspect]
    return {
        "folder": str(folder),
        "output": str(output),
        "aspect": aspect,
        "canvas": {"width": width, "height": height, "fps": FPS},
        "media_count": len(media),
        "expected_duration": round(expected_duration, 6),
        "xfade_sec": xfade_sec,
        "media": media,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render video clips and photos as a telop/BGM vlog MP4."
    )
    parser.add_argument("folder", help="Folder containing video clips and photos")
    parser.add_argument("--out", help="Output MP4 path (default: <folder>/vlog.mp4)")
    parser.add_argument("--aspect", choices=("16:9", "9:16"), default="16:9")
    parser.add_argument("--bgm", help="Optional BGM audio path")
    parser.add_argument("--telops", help="Telops JSON path (default: <folder>/telops.json)")
    parser.add_argument("--photo-sec", type=float, default=4.0)
    parser.add_argument("--clip-max-sec", type=float, default=8.0)
    parser.add_argument("--xfade-sec", type=float, default=0.5)
    parser.add_argument("--order", choices=("name", "mtime"), default="name")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("material folder does not exist: {0}".format(folder))
        photo_sec = _finite_float(args.photo_sec, "--photo-sec", 0.001)
        clip_max_sec = _finite_float(args.clip_max_sec, "--clip-max-sec", 0.001)
        xfade_sec = _finite_float(args.xfade_sec, "--xfade-sec", 0.0)
        output = (
            Path(args.out).expanduser().resolve()
            if args.out
            else folder / "vlog.mp4"
        )
        telops_path = (
            Path(args.telops).expanduser().resolve()
            if args.telops
            else folder / "telops.json"
        )
        bgm = Path(args.bgm).expanduser().resolve() if args.bgm else None
        if bgm is not None and not bgm.is_file():
            raise ValueError("BGM file does not exist: {0}".format(bgm))
        media = scan_media(
            folder,
            args.order,
            photo_sec,
            clip_max_sec,
            [output] + ([bgm] if bgm is not None else []),
        )
        payload = analysis_payload(folder, output, args.aspect, media, xfade_sec)
        if payload["expected_duration"] <= 0:
            raise ValueError("expected output duration must be greater than zero")
        if len(media) > 1 and xfade_sec > 0:
            shortest = min(float(item["adopted_duration"]) for item in media)
            if xfade_sec >= shortest:
                raise ValueError(
                    "--xfade-sec must be shorter than every adopted media duration"
                )
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        if not Path(FFMPEG).is_file() and shutil.which(FFMPEG) is None:
            raise RuntimeError("ffmpeg was not found")
        if not Path(FFPROBE).is_file() and shutil.which(FFPROBE) is None:
            raise RuntimeError("ffprobe was not found")
        width, height = ASPECT_SIZES[args.aspect]
        scratch = _scratch_dir(folder)
        normalized = []
        for index, item in enumerate(media):
            normalized.append(normalize_media(item, index, scratch, width, height))
        actual_durations = [_probe_duration(path) for path in normalized]
        joined, calculated_duration = concatenate_media(
            normalized, actual_durations, xfade_sec, scratch
        )
        joined_duration = _probe_duration(joined)
        if abs(joined_duration - calculated_duration) > 1.0:
            raise RuntimeError(
                "joined duration mismatch: probed={0:.3f}s calculated={1:.3f}s".format(
                    joined_duration, calculated_duration
                )
            )
        telops = load_telops(telops_path, joined_duration)
        render_final(
            joined,
            output,
            joined_duration,
            telops,
            bgm,
            scratch,
            width,
            height,
        )
        final_duration = _probe_duration(output)
        result = dict(payload)
        result.update(
            {
                "scratch": str(scratch),
                "actual_intermediate_durations": [
                    round(value, 6) for value in actual_durations
                ],
                "joined_duration": round(joined_duration, 6),
                "output_duration": round(final_duration, 6),
                "telop_count": len(telops),
                "bgm": str(bgm) if bgm is not None else None,
            }
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print("ERROR: {0}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

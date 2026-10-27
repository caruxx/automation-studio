#!/usr/bin/env python3
"""Analyze vlog materials with vision and write a validated edit plan."""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app_llm_runner import run_llm_vision
from app_vlogrender import DEFAULT_XFADE_SEC, FFMPEG, scan_media


DEFAULT_TARGET_SEC = 35.0
DEFAULT_PHOTO_SEC = 4.0
FRAME_LONG_EDGE = 768
FRAME_TIMEOUT = 180
VALID_STYLES = ("jp", "en", "serif")
VALID_POSITIONS = ("center", "bottom", "top")


def _warn(message: str) -> None:
    print("補正: {0}".format(message), file=sys.stderr)


def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _safe_stem(value: str) -> str:
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._")
    return stem or "media"


def _frame_path(
    frames_dir: Path,
    media_index: int,
    media_name: str,
    frame_index: int,
) -> Path:
    return frames_dir / "{0:03d}_{1}_{2:02d}.jpg".format(
        media_index,
        _safe_stem(Path(media_name).stem)[:80],
        frame_index,
    )


def _can_reuse(source: Path, output: Path) -> bool:
    try:
        return output.is_file() and output.stat().st_size > 0 and (
            output.stat().st_mtime_ns >= source.stat().st_mtime_ns
        )
    except OSError:
        return False


def _run_frame_ffmpeg(cmd: List[str], source: Path, output: Path) -> None:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=FRAME_TIMEOUT,
    )
    if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        try:
            output.unlink()
        except OSError:
            pass
        raise RuntimeError(
            "representative frame extraction failed for {0}: {1}".format(
                source,
                (result.stderr or "")[-2000:],
            )
        )


def _video_timestamps(duration: float, count: int) -> List[float]:
    if duration <= 0:
        raise ValueError("video duration must be greater than zero")
    if duration <= 0.6:
        return [max(0.0, duration / 2.0)] * count
    start = 0.5
    end = max(start, duration - 0.1)
    if count == 1:
        return [(start + end) / 2.0]
    return [
        start + ((end - start) * index / float(count - 1))
        for index in range(count)
    ]


def extract_representative_frames(
    folder: Path,
    media: List[Dict[str, Any]],
    frames_per_clip: int,
) -> Tuple[List[Path], List[Dict[str, Any]]]:
    """Extract or reuse 768px representative JPEG frames for every material."""
    frames_dir = folder / ".frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    all_paths = []
    frame_manifest = []
    scale_filter = (
        "scale={0}:{0}:force_original_aspect_ratio=decrease".format(FRAME_LONG_EDGE)
    )
    for media_index, item in enumerate(media):
        source = Path(item["path"])
        paths = []
        timestamps = []
        if item["kind"] == "video":
            duration = float(item["source_duration"])
            timestamps = _video_timestamps(duration, frames_per_clip)
            for frame_index, timestamp in enumerate(timestamps, 1):
                output = _frame_path(
                    frames_dir, media_index, item["name"], frame_index
                )
                if not _can_reuse(source, output):
                    _run_frame_ffmpeg(
                        [
                            FFMPEG,
                            "-y",
                            "-hide_banner",
                            "-loglevel",
                            "error",
                            "-ss",
                            "{0:.6f}".format(timestamp),
                            "-i",
                            str(source),
                            "-frames:v",
                            "1",
                            "-vf",
                            scale_filter,
                            "-q:v",
                            "3",
                            str(output),
                        ],
                        source,
                        output,
                    )
                paths.append(output)
        else:
            output = _frame_path(frames_dir, media_index, item["name"], 1)
            if not _can_reuse(source, output):
                _run_frame_ffmpeg(
                    [
                        FFMPEG,
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        scale_filter,
                        "-q:v",
                        "3",
                        str(output),
                    ],
                    source,
                    output,
                )
            paths.append(output)
        all_paths.extend(paths)
        frame_manifest.append(
            {
                "name": item["name"],
                "kind": item["kind"],
                "source_duration": item.get("source_duration"),
                "frames": [str(path) for path in paths],
                "timestamps": [round(value, 3) for value in timestamps],
            }
        )
    return all_paths, frame_manifest


def build_prompt(
    media: List[Dict[str, Any]],
    frame_manifest: List[Dict[str, Any]],
    lang: str,
    target_sec: float,
    mood: Optional[str],
) -> str:
    material_lines = []
    frame_number = 1
    for item, frames in zip(media, frame_manifest):
        labels = []
        for path in frames["frames"]:
            labels.append("image {0}: {1}".format(frame_number, path))
            frame_number += 1
        duration = (
            "{0:.3f}s".format(float(item["source_duration"]))
            if item["kind"] == "video"
            else "photo"
        )
        material_lines.append(
            "- {0} ({1}, {2}): {3}".format(
                item["name"],
                item["kind"],
                duration,
                "; ".join(labels),
            )
        )
    language_rule = {
        "jp": "テロップは日本語だけにする。",
        "en": "テロップは英語だけにする。",
        "both": (
            "テロップは日本語を主体にし、短い英語を要所だけ別テロップとして添える。"
        ),
    }[lang]
    mood_rule = mood.strip() if mood and mood.strip() else "素材から自然に判断する"
    schema = {
        "order": [
            {
                "name": "<ファイル名>",
                "sec": 4.0,
                "why": "<被写体・場所・時間帯・色調と、この位置に置く理由を一行で>",
            }
        ],
        "telops": [
            {
                "text": "...",
                "start": 0.5,
                "dur": 2.5,
                "style": "jp|en|serif",
                "pos": "center|bottom|top",
            }
        ],
        "summary": "<この動画の一言説明>",
    }
    return """あなたは短編Vlogの映像編集者です。添付画像は撮影素材の代表フレームです。
素材ごとに全フレームを見比べ、自然なストーリーになる構成プランを作ってください。

要件:
- 各素材の被写体、場所、時間帯、色調を把握し、order の why に一行で記述する。
- 空や風景の引き、移動、店や建物、手元や料理、締め、のような自然な流れに並べ替える。
- すべての素材を order に一度ずつ、下記の正確なファイル名で含める。
- 動画の sec は実尺を超えない。写真には自然な表示秒数を割り当てる。
- sec の合計を目標尺 {target:.3f} 秒前後にする。
- テロップ時刻は、クリップ間に {xfade:.3f} 秒のクロスフェードが入ることを考慮する。
- 冒頭の挨拶、場面の一言、締めの一言を telops に含める。
- {language_rule}
- 雰囲気: {mood}
- style は jp、en、serif のいずれか、pos は center、bottom、top のいずれかにする。
- JSON オブジェクトだけを返す。Markdown、コードフェンス、前置き、後書きは禁止。
- 次のスキーマとキー名に厳密に従う:
{schema}

素材と画像の対応:
{materials}
""".format(
        target=target_sec,
        xfade=DEFAULT_XFADE_SEC,
        language_rule=language_rule,
        mood=mood_rule,
        schema=json.dumps(schema, ensure_ascii=False, indent=2),
        materials="\n".join(material_lines),
    )


def parse_llm_json(response: str) -> Dict[str, Any]:
    text = (response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise ValueError("vision response did not contain a JSON object")
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise ValueError("vision response was not valid JSON: {0}".format(exc))
    if not isinstance(value, dict):
        raise ValueError("vision response JSON must be an object")
    return value


def _default_sec(
    media: Dict[str, Any],
    retained_total: float,
    missing_count: int,
    target_sec: float,
) -> float:
    remaining = max(0.0, target_sec - retained_total)
    proposed = remaining / float(missing_count) if remaining > 0 else DEFAULT_PHOTO_SEC
    proposed = max(0.5, proposed)
    if media["kind"] == "video":
        proposed = min(proposed, float(media["source_duration"]))
    return proposed


def validate_and_correct_plan(
    raw: Dict[str, Any],
    media: List[Dict[str, Any]],
    target_sec: float,
) -> Dict[str, Any]:
    """Validate untrusted LLM JSON and return a renderer-safe plan."""
    known = {item["name"]: item for item in media}
    raw_order = raw.get("order")
    if not isinstance(raw_order, list):
        _warn("order が配列ではないため、全素材を既定順で補完した")
        raw_order = []
    order = []
    seen = set()
    for index, entry in enumerate(raw_order):
        if not isinstance(entry, dict):
            _warn("order[{0}] がオブジェクトではないため除外した".format(index))
            continue
        name = entry.get("name")
        if not isinstance(name, str) or name not in known:
            _warn(
                "order[{0}] の存在しない素材 {1!r} を除外した".format(index, name)
            )
            continue
        if name in seen:
            _warn("order の重複素材 {0!r} を2件目以降から除外した".format(name))
            continue
        item = known[name]
        sec = _finite_number(entry.get("sec"))
        if sec is None or sec <= 0:
            sec = _default_sec(item, sum(row["sec"] for row in order), 1, target_sec)
            _warn("{0!r} の不正な sec を {1:.3f} 秒に補正した".format(name, sec))
        if item["kind"] == "video" and sec > float(item["source_duration"]):
            original = sec
            sec = float(item["source_duration"])
            _warn(
                "{0!r} の sec を実尺にクランプした: {1:.3f} -> {2:.3f}".format(
                    name, original, sec
                )
            )
        why = entry.get("why")
        if not isinstance(why, str):
            why = ""
            _warn("{0!r} の why を空文字に補正した".format(name))
        order.append({"name": name, "sec": round(sec, 3), "why": why.strip()})
        seen.add(name)

    missing = [item for item in media if item["name"] not in seen]
    for missing_index, item in enumerate(missing):
        retained_total = sum(row["sec"] for row in order)
        sec = _default_sec(
            item,
            retained_total,
            len(missing) - missing_index,
            target_sec,
        )
        order.append(
            {
                "name": item["name"],
                "sec": round(sec, 3),
                "why": "vision の order に無かったため末尾へ補完",
            }
        )
        _warn(
            "不足素材 {0!r} を末尾へ追加した ({1:.3f} 秒)".format(
                item["name"], sec
            )
        )

    total_duration = sum(float(item["sec"]) for item in order)
    if len(order) > 1:
        total_duration -= DEFAULT_XFADE_SEC * (len(order) - 1)
    raw_telops = raw.get("telops")
    if not isinstance(raw_telops, list):
        _warn("telops が配列ではないため空配列に補正した")
        raw_telops = []
    candidates = []
    for index, entry in enumerate(raw_telops):
        if not isinstance(entry, dict):
            _warn("telops[{0}] がオブジェクトではないため除外した".format(index))
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            _warn("telops[{0}] の text が空のため除外した".format(index))
            continue
        start = _finite_number(entry.get("start"))
        duration = _finite_number(entry.get("dur"))
        if start is None or duration is None or duration <= 0:
            _warn("telops[{0}] の時刻が不正なため除外した".format(index))
            continue
        if start < 0:
            _warn(
                "telops[{0}] の start を 0 秒にクランプした: {1:.3f}".format(
                    index, start
                )
            )
            start = 0.0
        if start >= total_duration:
            _warn("telops[{0}] は動画尺外から始まるため除外した".format(index))
            continue
        if start + duration > total_duration:
            original = duration
            duration = total_duration - start
            _warn(
                "telops[{0}] の dur を動画尺内にクランプした: "
                "{1:.3f} -> {2:.3f}".format(index, original, duration)
            )
        style = entry.get("style")
        if style not in VALID_STYLES:
            _warn(
                "telops[{0}] の style {1!r} を 'jp' に補正した".format(
                    index, style
                )
            )
            style = "jp"
        position = entry.get("pos")
        if position not in VALID_POSITIONS:
            _warn(
                "telops[{0}] の pos {1!r} を 'center' に補正した".format(
                    index, position
                )
            )
            position = "center"
        candidates.append(
            {
                "_index": index,
                "text": text.strip(),
                "start": start,
                "dur": duration,
                "style": style,
                "pos": position,
            }
        )

    candidates.sort(key=lambda item: (item["start"], item["_index"]))
    telops = []
    for item in candidates:
        if telops:
            previous_end = telops[-1]["start"] + telops[-1]["dur"]
            overlap = previous_end - item["start"]
            severe_threshold = min(telops[-1]["dur"], item["dur"]) * 0.5
            if overlap > severe_threshold:
                original = item["start"]
                item["start"] = previous_end
                _warn(
                    "telops[{0}] の激しい重なりを後ろへずらした: "
                    "{1:.3f} -> {2:.3f}".format(
                        item["_index"], original, item["start"]
                    )
                )
        if item["start"] >= total_duration:
            _warn(
                "telops[{0}] は重なり補正後に動画尺外となったため除外した".format(
                    item["_index"]
                )
            )
            continue
        if item["start"] + item["dur"] > total_duration:
            original = item["dur"]
            item["dur"] = total_duration - item["start"]
            _warn(
                "telops[{0}] の dur を重なり補正後の動画尺にクランプした: "
                "{1:.3f} -> {2:.3f}".format(
                    item["_index"], original, item["dur"]
                )
            )
        telops.append(
            {
                "text": item["text"],
                "start": round(item["start"], 3),
                "dur": round(item["dur"], 3),
                "style": item["style"],
                "pos": item["pos"],
            }
        )

    summary = raw.get("summary")
    if not isinstance(summary, str):
        _warn("summary を空文字に補正した")
        summary = ""
    return {
        "order": order,
        "telops": telops,
        "summary": summary.strip(),
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        ".{0}.{1}.tmp".format(path.name, os.getpid())
    )
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze vlog materials with vision and write an edit plan."
    )
    parser.add_argument("folder", help="Folder containing video clips and photos")
    parser.add_argument("--out", help="Plan JSON path (default: <folder>/vlog_plan.json)")
    parser.add_argument(
        "--telops-out",
        help="Telops JSON path (default: <folder>/telops.json)",
    )
    parser.add_argument("--lang", choices=("jp", "en", "both"), default="both")
    parser.add_argument("--target-sec", type=float, default=DEFAULT_TARGET_SEC)
    parser.add_argument("--mood")
    parser.add_argument("--frames-per-clip", type=int, default=2)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("material folder does not exist: {0}".format(folder))
        target_sec = _finite_number(args.target_sec)
        if target_sec is None or target_sec <= 0:
            raise ValueError("--target-sec must be a finite number greater than zero")
        if args.frames_per_clip <= 0:
            raise ValueError("--frames-per-clip must be greater than zero")
        plan_path = (
            Path(args.out).expanduser().resolve()
            if args.out
            else folder / "vlog_plan.json"
        )
        telops_path = (
            Path(args.telops_out).expanduser().resolve()
            if args.telops_out
            else folder / "telops.json"
        )
        media = scan_media(
            folder,
            "name",
            DEFAULT_PHOTO_SEC,
            sys.float_info.max,
            [],
        )
        frame_paths, frame_manifest = extract_representative_frames(
            folder,
            media,
            args.frames_per_clip,
        )
        prompt = build_prompt(
            media,
            frame_manifest,
            args.lang,
            target_sec,
            args.mood,
        )
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "folder": str(folder),
                        "media_count": len(media),
                        "frame_count": len(frame_paths),
                        "frames": frame_manifest,
                        "prompt": prompt,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        response = run_llm_vision(
            prompt,
            frame_paths,
            timeout=600,
            label="vlog-plan",
        )
        raw = parse_llm_json(response)
        plan = validate_and_correct_plan(raw, media, target_sec)
        _write_json(plan_path, plan)
        _write_json(telops_path, plan["telops"])
        print(
            json.dumps(
                {
                    "plan": str(plan_path),
                    "telops": str(telops_path),
                    "media_count": len(media),
                    "frame_count": len(frame_paths),
                    "planned_duration": round(
                        sum(float(item["sec"]) for item in plan["order"]), 3
                    ),
                    "telop_count": len(plan["telops"]),
                    "summary": plan["summary"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (
        OSError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print("ERROR: {0}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Verify explicitly listed local Suno downloads without changing source files."""

import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


INCOMPLETE_SUFFIXES = {".part", ".partial", ".crdownload", ".download", ".tmp"}


class SummaryArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"passed": False, "error": message}))
        raise SystemExit(1)


def run_media(command, timeout):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return None, "", f"Process exceeded {timeout} seconds"
    except OSError as exc:
        return None, "", str(exc)


def check_track(track, index, manifest_dir, ffprobe, ffmpeg, timeout, seen_ids, seen_paths):
    out = {"index": index, "errors": [], "container_metadata_verified": False}
    errors = out["errors"]
    if not isinstance(track, dict):
        errors.append("Track must be an object")
        return out
    clip_id, title, raw_path = (track.get(key) for key in ("clip_id", "title", "path"))
    out.update(clip_id=clip_id, title=title, requested_path=raw_path)
    if not isinstance(clip_id, str) or not clip_id.strip():
        errors.append("clip_id must be a nonempty string")
    elif clip_id in seen_ids:
        errors.append("Duplicate clip_id")
    else:
        seen_ids.add(clip_id)
    if not isinstance(title, str) or not title.strip():
        errors.append("title must be a nonempty string")
    if not isinstance(raw_path, str) or not raw_path.strip():
        errors.append("path must be a nonempty string")
        return out
    try:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = manifest_dir / path
        path = path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"Cannot resolve path: {exc}")
        return out
    out["path"] = str(path)
    if str(path) in seen_paths:
        errors.append("Duplicate resolved path")
    else:
        seen_paths.add(str(path))
    named_paths = (Path(raw_path), path)
    if any(
        p.name.lower() in INCOMPLETE_SUFFIXES
        or any(suffix.lower() in INCOMPLETE_SUFFIXES for suffix in p.suffixes)
        for p in named_paths
    ):
        errors.append("Incomplete or temporary download filename")
    if not path.is_file():
        errors.append("Audio file is missing or not a regular file")
    if errors:
        return out
    try:
        before = path.stat()
        out["bytes"] = before.st_size
        if before.st_size == 0:
            errors.append("Audio file is empty")
            return out
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        out["sha256"] = digest.hexdigest()
        if not ffprobe or not ffmpeg:
            errors.append("ffprobe and ffmpeg must both be available on PATH")
            return out
        code, stdout, stderr = run_media(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout
        )
        out["probe_ok"] = code == 0
        if code != 0:
            errors.append(f"ffprobe failed: {stderr}")
            return out
        try:
            probe = json.loads(stdout)
            duration = float(probe.get("format", {}).get("duration", "nan"))
        except (ValueError, TypeError, AttributeError):
            errors.append("ffprobe did not return valid duration metadata")
            return out
        if not (0 < duration < float("inf")):
            errors.append("Duration must be finite and positive")
        else:
            out["duration_seconds"] = duration
        streams = probe.get("streams", [])
        out["streams"] = [
            {"index": s.get("index"), "codec_type": s.get("codec_type"), "codec_name": s.get("codec_name")}
            for s in streams
        ]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        out["audio_stream_count"] = len(audio_streams)
        out["audio_codecs"] = [s.get("codec_name") for s in audio_streams]
        if not audio_streams:
            errors.append("No audio stream exists")
        elif any(not s.get("codec_name") for s in audio_streams):
            errors.append("An audio stream has no detected codec")

        tags = probe.get("format", {}).get("tags", {})
        comments = [str(v) for k, v in tags.items() if k.casefold() == "comment"]
        embedded_ids = sorted(set(re.findall(r"(?:^|;)\s*id\s*=\s*([^;\s]+)", ";".join(comments))))
        out["embedded_comment_ids"] = embedded_ids
        if embedded_ids:
            if embedded_ids == [clip_id]:
                out["container_metadata_verified"] = True
                out["identity_check"] = "embedded_id_matches_manifest"
            else:
                out["identity_check"] = "embedded_id_mismatch"
                errors.append("Embedded comment id does not match manifest clip_id")
        else:
            evidence = track.get("mapping_evidence")
            out["mapping_evidence"] = evidence
            evidence_present = (isinstance(evidence, str) and bool(evidence.strip())) or (
                isinstance(evidence, dict) and bool(evidence)
            )
            out["identity_check"] = "evidence_only_not_container_metadata_verified"
            if not evidence_present:
                errors.append("No embedded comment id; nonempty mapping_evidence is required")

        if audio_streams:
            code, _, stderr = run_media(
                [ffmpeg, "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a", "-f", "null", "-"],
                timeout,
            )
            out["decode_ok"] = code == 0 and not stderr
            out["decode_stderr"] = stderr
            if not out["decode_ok"]:
                errors.append(f"Full audio decode failed: {stderr or 'nonzero exit status'}")
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            errors.append("Source file changed during verification")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        errors.append(f"Local verification failed: {exc}")
    out["passed"] = not errors
    return out


def main():
    parser = SummaryArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON with expected_audio_count and explicit tracks")
    parser.add_argument("--output", required=True, type=Path, help="New report file; existing files are never overwritten")
    parser.add_argument("--timeout-seconds", type=int, default=120, help="Per ffprobe/ffmpeg timeout, 1–600 seconds (default: 120)")
    args = parser.parse_args()
    if not 1 <= args.timeout_seconds <= 600:
        parser.error("--timeout-seconds must be between 1 and 600")
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        print(json.dumps({"passed": False, "error": "Output already exists; refusing overwrite", "output": str(output)}))
        return 1
    manifest_path = args.manifest.expanduser().resolve()
    report = {
        "schema_version": 1,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "manifest": str(manifest_path),
        "timeout_seconds_per_process": args.timeout_seconds,
        "errors": [],
        "tracks": [],
        "limitations": [
            "Checks local files, stream decoding, and recorded identity evidence only.",
            "Does not verify musical quality, instrumental content, thumbnail or opening-audio alignment.",
            "Does not generate, download, access a network or browser, or modify source audio.",
            "expected_audio_count counts audio files, not Create clicks or requested titles.",
            "mapping_evidence is caller-supplied evidence and is not container metadata verification.",
        ],
    }
    try:
        def reject_constant(value):
            raise ValueError(f"Nonstandard JSON number: {value}")

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"), parse_constant=reject_constant)
        if not isinstance(manifest, dict):
            raise ValueError("Manifest must be an object")
        count = manifest.get("expected_audio_count")
        report["expected_audio_count"] = count
        if type(count) is not int or count <= 0:
            report["errors"].append("expected_audio_count must be a positive integer")
        tracks = manifest.get("tracks")
        if not isinstance(tracks, list):
            raise ValueError("tracks must be an array")
        if type(count) is int and len(tracks) != count:
            report["errors"].append(f"Track count {len(tracks)} does not match expected_audio_count {count}")
        ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
        report["tools"] = {"ffprobe": ffprobe, "ffmpeg": ffmpeg}
        seen_ids, seen_paths = set(), set()
        report["tracks"] = [
            check_track(track, i, manifest_path.parent, ffprobe, ffmpeg, args.timeout_seconds, seen_ids, seen_paths)
            for i, track in enumerate(tracks, start=1)
        ]
    except (OSError, ValueError, TypeError) as exc:
        report["errors"].append(str(exc))
    report["passed"] = not report["errors"] and bool(report["tracks"]) and all(
        not track["errors"] for track in report["tracks"]
    )
    report["summary"] = {
        "listed_tracks": len(report["tracks"]),
        "passed_tracks": sum(not t["errors"] for t in report["tracks"]),
        "decode_ok": sum(t.get("decode_ok", False) for t in report["tracks"]),
        "container_metadata_verified": sum(t.get("container_metadata_verified", False) for t in report["tracks"]),
        "evidence_only": sum(t.get("identity_check") == "evidence_only_not_container_metadata_verified" for t in report["tracks"]),
    }
    try:
        with output.open("x", encoding="utf-8") as sink:
            json.dump(report, sink, ensure_ascii=False, indent=2, allow_nan=False)
            sink.write("\n")
    except (OSError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": str(exc), "output": str(output)}, ensure_ascii=False))
        return 1
    print(json.dumps({"passed": report["passed"], **report["summary"], "output": str(output)}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

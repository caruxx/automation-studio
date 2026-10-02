#!/usr/bin/env python3
"""Local bridge between a Wobble Day request and official-Chrome CUA work.

This command never opens a browser, calls a network service, or clicks Create.
It exposes only two local primitives from Automation Studio:

* ``reserve`` validates an authorized request and reserves every draft title.
* ``hold-lock`` holds the shared SUNO browser flock while another tool uses CUA.

The production modules are imported from ``Python/`` (the script's grandparent),
so this file can live at ``Python/scripts/wobble_suno_bridge.py`` in the source
tree without depending on the current working directory.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PYTHON_DIR = Path(__file__).resolve().parents[1]
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

MAX_LOCK_SECONDS = 3600


class BridgeError(ValueError):
    """A request or invocation is unsafe for this bridge."""


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BridgeError(f"{field} must be a positive integer")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BridgeError(f"{field} must be a non-empty string")
    return value.strip()


def _load_json(path: str | Path, *, expected: type, label: str) -> Any:
    source = Path(path).expanduser()
    if not source.is_file():
        raise BridgeError(f"{label} does not exist or is not a file: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeError(f"cannot read {label} as UTF-8 JSON: {source}: {exc}") from exc
    if not isinstance(value, expected):
        raise BridgeError(f"{label} must contain {expected.__name__} JSON")
    return value


def _validate_request(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("project") != "Wobble Day":
        raise BridgeError("request.project must be exactly 'Wobble Day'")
    if request.get("mode") != "generate_and_download":
        raise BridgeError("request.mode must be exactly 'generate_and_download'")
    if request.get("browser_route") != "official_chrome_cua":
        raise BridgeError("request.browser_route must be exactly 'official_chrome_cua'")

    request_id = _required_text(request.get("request_id"), "request.request_id")
    authorization = request.get("authorization")
    if not isinstance(authorization, dict):
        raise BridgeError("request.authorization must be an object")
    if authorization.get("generate_authorized") is not True:
        raise BridgeError("request.authorization.generate_authorized must be true")
    user_instruction = _required_text(
        authorization.get("user_instruction"),
        "request.authorization.user_instruction",
    )
    authorized_audio_count = _positive_int(
        authorization.get("authorized_audio_count"),
        "request.authorization.authorized_audio_count",
    )

    mood = _required_text(request.get("mood"), "request.mood")
    duration_seconds = _positive_int(
        request.get("duration_seconds_per_audio"),
        "request.duration_seconds_per_audio",
    )

    visual = request.get("visual_references")
    if not isinstance(visual, dict):
        raise BridgeError("request.visual_references must be an object")
    selected_thumbnail = _required_text(
        visual.get("selected_thumbnail_reference"),
        "request.visual_references.selected_thumbnail_reference",
    )
    selection_evidence = _required_text(
        visual.get("selection_evidence"),
        "request.visual_references.selection_evidence",
    )

    count = request.get("count")
    if not isinstance(count, dict):
        raise BridgeError("request.count must be an object")
    requested_audio_count = _positive_int(
        count.get("requested_audio_count"), "request.count.requested_audio_count"
    )
    requested_title_count = _positive_int(
        count.get("requested_title_count"), "request.count.requested_title_count"
    )
    outputs_per_create = _positive_int(
        count.get("outputs_per_create_observed"),
        "request.count.outputs_per_create_observed",
    )
    ui_evidence = _required_text(
        count.get("ui_evidence"), "request.count.ui_evidence"
    )
    planned_create_count = _positive_int(
        count.get("planned_create_count"), "request.count.planned_create_count"
    )
    if authorized_audio_count != requested_audio_count:
        raise BridgeError(
            "authorized_audio_count must equal count.requested_audio_count"
        )
    if requested_title_count != planned_create_count:
        raise BridgeError(
            "count.requested_title_count must equal count.planned_create_count"
        )
    if planned_create_count * outputs_per_create != requested_audio_count:
        raise BridgeError(
            "planned_create_count * outputs_per_create_observed must equal "
            "requested_audio_count; implicit extra takes are not authorized"
        )

    settings = request.get("settings")
    if not isinstance(settings, dict):
        raise BridgeError("request.settings must be an object")
    if settings.get("instrumental") is not True:
        raise BridgeError("request.settings.instrumental must be true")
    lyrics = settings.get("lyrics")
    if lyrics is not None and (not isinstance(lyrics, str) or lyrics.strip()):
        raise BridgeError("request.settings.lyrics must be empty for Wobble Day")

    return {
        "request_id": request_id,
        "user_instruction": user_instruction,
        "authorized_audio_count": authorized_audio_count,
        "requested_audio_count": requested_audio_count,
        "requested_title_count": requested_title_count,
        "outputs_per_create_observed": outputs_per_create,
        "ui_evidence": ui_evidence,
        "planned_create_count": planned_create_count,
        "mood": mood,
        "duration_seconds_per_audio": duration_seconds,
        "selected_thumbnail_reference": selected_thumbnail,
        "selection_evidence": selection_evidence,
    }


def _validate_songs(songs: list[Any], validated: dict[str, Any]) -> list[dict[str, Any]]:
    expected = validated["planned_create_count"]
    if len(songs) != expected:
        raise BridgeError(
            f"songs length must equal planned_create_count: expected={expected} actual={len(songs)}"
        )
    checked: list[dict[str, Any]] = []
    for index, raw in enumerate(songs):
        if not isinstance(raw, dict):
            raise BridgeError(f"songs[{index}] must be an object")
        song = dict(raw)
        song["title"] = _required_text(song.get("title"), f"songs[{index}].title")
        song["styles"] = _required_text(song.get("styles"), f"songs[{index}].styles")
        if song.get("instrumental") is not True:
            raise BridgeError(f"songs[{index}].instrumental must be true")
        song_lyrics = song.get("lyrics")
        if song_lyrics is not None and (
            not isinstance(song_lyrics, str) or song_lyrics.strip()
        ):
            raise BridgeError(f"songs[{index}].lyrics must be empty")
        song["lyrics"] = ""
        if song.get("duration_seconds") is not None:
            song_duration = _positive_int(
                song.get("duration_seconds"), f"songs[{index}].duration_seconds"
            )
            if song_duration != validated["duration_seconds_per_audio"]:
                raise BridgeError(
                    f"songs[{index}].duration_seconds must equal "
                    "request.duration_seconds_per_audio"
                )
        if "_music_request_id" in song:
            raise BridgeError(
                f"songs[{index}] is already reserved; reuse the saved reservation output"
            )
        title_fixed = song.get("title_fixed", False)
        if not isinstance(title_fixed, bool):
            raise BridgeError(f"songs[{index}].title_fixed must be boolean when present")
        song["title_fixed"] = title_fixed
        checked.append(song)
    return checked


def _validate_lock_request(request: dict[str, Any]) -> dict[str, Any]:
    """Validate only facts known before CUA performs its read-only preflight."""
    if request.get("project") != "Wobble Day":
        raise BridgeError("request.project must be exactly 'Wobble Day'")
    request_id = _required_text(request.get("request_id"), "request.request_id")
    if request.get("browser_route") != "official_chrome_cua":
        raise BridgeError("request.browser_route must be exactly 'official_chrome_cua'")
    mode = request.get("mode")
    if mode not in {"generate_and_download", "download_only"}:
        raise BridgeError(
            "hold-lock request.mode must be 'generate_and_download' or 'download_only'"
        )
    authorization = request.get("authorization")
    if not isinstance(authorization, dict):
        raise BridgeError("request.authorization must be an object")
    _required_text(
        authorization.get("user_instruction"),
        "request.authorization.user_instruction",
    )

    if mode == "generate_and_download":
        if authorization.get("generate_authorized") is not True:
            raise BridgeError("request.authorization.generate_authorized must be true")
        authorized = _positive_int(
            authorization.get("authorized_audio_count"),
            "request.authorization.authorized_audio_count",
        )
        count = request.get("count")
        if not isinstance(count, dict):
            raise BridgeError("request.count must be an object")
        requested = _positive_int(
            count.get("requested_audio_count"),
            "request.count.requested_audio_count",
        )
        if authorized != requested:
            raise BridgeError(
                "authorized_audio_count must equal count.requested_audio_count"
            )
    else:
        clip_ids = request.get("existing_clip_ids")
        if not isinstance(clip_ids, list) or not clip_ids:
            raise BridgeError(
                "download_only requires a non-empty request.existing_clip_ids list"
            )
        normalized: list[str] = []
        for index, clip_id in enumerate(clip_ids):
            normalized.append(
                _required_text(clip_id, f"request.existing_clip_ids[{index}]")
            )
        if len(set(normalized)) != len(normalized):
            raise BridgeError("request.existing_clip_ids must be unique")

    return {"request_id": request_id, "mode": mode}


def _probe_atomic_output_path(path: str | Path) -> Path:
    """Fail before catalog writes when the destination cannot hard-link atomically."""
    target = Path(path).expanduser()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BridgeError(
            f"cannot prepare output directory {target.parent}; use a local run directory: {exc}"
        ) from exc
    if target.exists() or target.is_symlink():
        raise BridgeError(f"output already exists; refusing to overwrite: {target}")

    source_name: str | None = None
    link_name: str | None = None
    try:
        source_fd, source_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".link-probe",
        )
        try:
            os.write(source_fd, b"atomic-link-probe\n")
            os.fsync(source_fd)
        finally:
            os.close(source_fd)
        link_name = f"{source_name}.linked"
        os.link(source_name, link_name)
    except OSError as exc:
        raise BridgeError(
            "output directory does not support the required same-directory atomic "
            f"hard-link operation; use a local run directory: {target.parent}: {exc}"
        ) from exc
    finally:
        for probe_path in (link_name, source_name):
            if probe_path:
                try:
                    os.unlink(probe_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    if target.exists() or target.is_symlink():
        raise BridgeError(f"output already exists; refusing to overwrite: {target}")
    return target


def _atomic_write_new(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise BridgeError(f"output already exists; refusing to overwrite: {target}")

    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp_name, target)
        except FileExistsError as exc:
            raise BridgeError(f"output already exists; refusing to overwrite: {target}") from exc
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reserve(request_path: str, songs_path: str, output_path: str) -> int:
    request = _load_json(request_path, expected=dict, label="request")
    validated = _validate_request(request)
    songs = _validate_songs(
        _load_json(songs_path, expected=list, label="songs"), validated
    )

    output = _probe_atomic_output_path(output_path)

    # Import only after every local precondition and the output path have passed.
    # app_music_catalog may seed its permanent catalog on the first reservation.
    from app_music_catalog import prepare_submission

    prepared_songs: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    block_reasons: list[str] = []
    try:
        for index, song in enumerate(songs):
            requested_title = song["title"]
            prepared = prepare_submission(song)
            if not isinstance(prepared, dict):
                raise RuntimeError("app_music_catalog.prepare_submission returned non-object")
            reserved_title = _required_text(
                prepared.get("title"), f"prepared_songs[{index}].title"
            )
            request_token = _required_text(
                prepared.get("_music_request_id"),
                f"prepared_songs[{index}]._music_request_id",
            )
            prepared["title"] = reserved_title
            prepared["_music_request_id"] = request_token
            changed = reserved_title != requested_title
            fixed = bool(song.get("title_fixed"))
            change = {
                "index": index,
                "requested_title": requested_title,
                "reserved_title": reserved_title,
                "changed": changed,
                "title_fixed": fixed,
                "create_blocked": bool(changed and fixed),
            }
            changes.append(change)
            if change["create_blocked"]:
                block_reasons.append(
                    f"songs[{index}] fixed title changed: {requested_title!r} -> {reserved_title!r}; "
                    "stop before Create and obtain an explicit title decision"
                )
            prepared_songs.append(prepared)
    except BaseException as exc:
        if prepared_songs:
            partial = {
                "schema_version": 1,
                "kind": "wobble_day_suno_reservation",
                "status": "reservation_partial",
                "created_at": _utc_now(),
                "request_id": validated["request_id"],
                "project": "Wobble Day",
                "mode": "generate_and_download",
                "error": str(exc),
                "reserved_count": len(prepared_songs),
                "songs": prepared_songs,
                "title_changes": changes,
                "create_blocked": True,
                "block_reasons": [
                    "reservation stopped after partial catalog writes; use this output for recovery "
                    "and do not reserve the same drafts again"
                ],
            }
            _atomic_write_new(output, partial)
        raise

    result = {
        "schema_version": 1,
        "kind": "wobble_day_suno_reservation",
        "status": "reserved_create_blocked" if block_reasons else "reserved",
        "created_at": _utc_now(),
        "request_id": validated["request_id"],
        "project": "Wobble Day",
        "mode": "generate_and_download",
        "authorization": {
            "generate_authorized": True,
            "authorized_audio_count": validated["authorized_audio_count"],
            "user_instruction": validated["user_instruction"],
        },
        "count": {
            "requested_audio_count": validated["requested_audio_count"],
            "requested_title_count": validated["requested_title_count"],
            "outputs_per_create_observed": validated["outputs_per_create_observed"],
            "ui_evidence": validated["ui_evidence"],
            "planned_create_count": validated["planned_create_count"],
        },
        "mood": validated["mood"],
        "duration_seconds_per_audio": validated["duration_seconds_per_audio"],
        "selected_thumbnail_reference": validated["selected_thumbnail_reference"],
        "selection_evidence": validated["selection_evidence"],
        "reserved_count": len(prepared_songs),
        "songs": prepared_songs,
        "title_changes": changes,
        "create_blocked": bool(block_reasons),
        "block_reasons": block_reasons,
        "mark_submitted": {
            "status": "deferred",
            "instruction": (
                "After CUA confirms that Create was accepted, call "
                "app_music_catalog.mark_submitted with the corresponding prepared song."
            ),
        },
    }
    _atomic_write_new(output, result)
    print(
        json.dumps(
            {
                "status": result["status"],
                "request_id": result["request_id"],
                "reserved_count": result["reserved_count"],
                "create_blocked": result["create_blocked"],
                "output": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 3 if block_reasons else 0


def hold_lock(request_path: str, max_seconds: int) -> int:
    request = _load_json(request_path, expected=dict, label="request")
    validated = _validate_lock_request(request)
    if isinstance(max_seconds, bool) or max_seconds <= 0 or max_seconds > MAX_LOCK_SECONDS:
        raise BridgeError(
            f"--max-seconds must be between 1 and {MAX_LOCK_SECONDS}"
        )

    from resource_lock import ResourceBusyError, ResourceLock

    lock = ResourceLock(
        "suno-browser", owner=f"wobble-cua:{validated['request_id']}"
    )
    try:
        lock.acquire(blocking=False)
    except ResourceBusyError as exc:
        print(
            json.dumps(
                {
                    "status": "busy",
                    "request_id": validated["request_id"],
                    "resource": "suno-browser",
                    "error": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 75

    inherited = bool(getattr(lock, "inherited", False))
    lock_mode = "inherited_parent" if inherited else "acquired_here"
    reason = "stdin_eof"
    started = time.monotonic()
    print(
        json.dumps(
            {
                "status": "ready",
                "request_id": validated["request_id"],
                "resource": "suno-browser",
                "pid": os.getpid(),
                "max_seconds": max_seconds,
                "lock_mode": lock_mode,
                "lock_owned_by_process": not inherited,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        readable, _, _ = select.select([sys.stdin], [], [], max_seconds)
        if readable:
            line = sys.stdin.readline()
            reason = "stdin_newline" if line else "stdin_eof"
        else:
            reason = "timeout"
    except KeyboardInterrupt:
        reason = "interrupt"
    finally:
        lock.release()

    print(
        json.dumps(
            {
                "status": "released",
                "request_id": validated["request_id"],
                "resource": "suno-browser",
                "pid": os.getpid(),
                "reason": reason,
                "held_seconds": round(time.monotonic() - started, 3),
                "lock_mode": lock_mode,
                "lock_released_by_process": not inherited,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pure-local Wobble Day title reservation and SUNO lock bridge"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    reserve_parser = subparsers.add_parser(
        "reserve", help="validate a request and reserve every draft title"
    )
    reserve_parser.add_argument("--request", required=True, help="authorized request JSON")
    reserve_parser.add_argument("--songs", required=True, help="draft songs JSON array")
    reserve_parser.add_argument(
        "--output", required=True, help="new reservation result JSON; never overwritten"
    )

    lock_parser = subparsers.add_parser(
        "hold-lock", help="hold the shared SUNO browser lock for an external CUA session"
    )
    lock_parser.add_argument("--request", required=True, help="authorized request JSON")
    lock_parser.add_argument(
        "--max-seconds",
        type=int,
        default=1800,
        help=f"bounded lock lifetime in seconds (default 1800, maximum {MAX_LOCK_SECONDS})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "reserve":
            return reserve(args.request, args.songs, args.output)
        if args.command == "hold-lock":
            return hold_lock(args.request, args.max_seconds)
        raise BridgeError(f"unknown command: {args.command}")
    except BridgeError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

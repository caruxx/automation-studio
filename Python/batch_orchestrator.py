#!/usr/bin/env python3
"""Relay-style batch runner for multiple Automation Studio volumes."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
sys.path.insert(0, str(BASE))

from app_core import get_active_channel_info, get_channels, resolve_vol_folder  # noqa: E402
from app_pipeline import STEPS, _local_upload_marker_allows_skip  # noqa: E402
from app_youtube import SCOPES, _save_credentials_atomic, resolve_token_path  # noqa: E402
from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402

BASE_URL = "http://localhost:8888"
EXIT_PREFLIGHT = 78
MIN_FREE_BYTES = 20 * 1024**3


@dataclass
class Result:
    vol: int
    folder: Path
    phase1_sec: float = 0.0
    phase2_sec: float = 0.0
    phase1_code: int | None = None
    phase2_code: int | None = None
    status: str = "pending"
    youtube_url: str = ""


def parse_vols(value: str) -> list[int]:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("--vols is empty")
    try:
        if "," in value:
            vols = [int(part.strip()) for part in value.split(",") if part.strip()]
        elif "-" in value:
            start_text, end_text = value.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError("range start is greater than end")
            vols = list(range(start, end + 1))
        else:
            vols = [int(value)]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid --vols value: {value} ({exc})") from exc
    if not vols or any(vol <= 0 for vol in vols):
        raise argparse.ArgumentTypeError("--vols must contain positive integers")
    return list(dict.fromkeys(vols))


def http_request(method: str, path: str, timeout: float = 5.0) -> None:
    request = urllib.request.Request(BASE_URL + path, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}")
        response.read()


def server_responding() -> bool:
    try:
        http_request("GET", "/api/config/migration-status")
        return True
    except Exception:
        return False


def ensure_server() -> None:
    if server_responding():
        print("preflight server: ok")
        return
    print("preflight server: not responding; starting with bash Python/start.sh")
    preflight_log = ROOT / "logs" / "batch" / "preflight_server.log"
    preflight_log.parent.mkdir(parents=True, exist_ok=True)
    stream = preflight_log.open("ab")
    subprocess.Popen(
        ["bash", str(BASE / "start.sh")],
        cwd=str(ROOT),
        stdout=stream,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(30):
        time.sleep(1)
        if server_responding():
            print("preflight server: started")
            return
    raise RuntimeError(f"localhost:8888 did not start; see {preflight_log}")


def resolve_channel(channel_id: str) -> dict:
    if channel_id:
        for channel in get_channels():
            if channel.get("id") == channel_id:
                return channel
        raise RuntimeError(f"channel not found: {channel_id}")
    active = get_active_channel_info()
    if not active.get("id") or not active.get("folder"):
        raise RuntimeError("active channel could not be resolved; pass --channel")
    return active


def ensure_channel(channel: dict) -> None:
    requested = str(channel.get("id") or "")
    active = get_active_channel_info()
    if active.get("id") == requested:
        print(f"preflight channel: {requested} (already active)")
        return
    http_request("PUT", f"/api/channels/active/{requested}", timeout=10)
    current = get_active_channel_info()
    if current.get("id") != requested:
        raise RuntimeError(
            f"channel switch did not take effect: requested={requested}, active={current.get('id') or '-'}"
        )
    print(f"preflight channel: switched to {requested}")


def check_youtube_token(channel_folder: Path, example_video_folder: Path) -> None:
    token_path = resolve_token_path(video_folder=example_video_folder)
    if token_path != channel_folder / ".youtube_token.json":
        raise RuntimeError(f"unexpected YouTube token path: {token_path}")
    if not token_path.exists():
        raise RuntimeError(
            f"YouTube token is missing. Reauthentication required: python3 app_youtube.py --auth-only {example_video_folder}"
        )
    try:
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        if not credentials.refresh_token:
            raise RuntimeError("refresh token is missing")
        credentials.refresh(Request())
        _save_credentials_atomic(token_path, credentials)
    except Exception as exc:
        detail = str(exc)
        reason = "invalid_grant" if "invalid_grant" in detail else type(exc).__name__
        raise RuntimeError(
            "YouTube token refresh failed "
            f"({reason}). Reauthentication required: python3 app_youtube.py --auth-only {example_video_folder}"
        ) from exc
    print("preflight YouTube token: refresh ok")


def check_disk(channel_folder: Path) -> None:
    free = shutil.disk_usage(channel_folder).free
    print(f"preflight disk: {free / 1024**3:.1f} GiB free")
    if free < MIN_FREE_BYTES:
        raise RuntimeError(f"disk free space is below 20 GiB: {free / 1024**3:.1f} GiB")


def process_parallel_default() -> str:
    """ffmpeg 後処理の並列度。CPU コア数 - 2（システムと並行 phase2 用に残す）。
    下限 4 は従来値の維持。環境変数 APP_PROCESS_PARALLEL が明示されていればそちらを優先。"""
    explicit = (os.environ.get("APP_PROCESS_PARALLEL") or "").strip()
    if explicit:
        return explicit
    return str(max(4, (os.cpu_count() or 4) - 2))


def command_env(duration_sec: int, channel_id: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APP_DURATION_SEC": str(duration_sec),
            "APP_SUNO_NO_HOLD": "1",
            "APP_SUNO_SKIP_SECOND_DL": "1",
            "APP_SUNO_READY_POLL": "1",
            "APP_SUNO_ONESHOT": "1",
            "APP_SUNO_SKIP_OPTIONAL_TITLE": "1",
            "APP_PROCESS_PARALLEL": process_parallel_default(),
            "APP_CHANNEL_ID": channel_id,
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def phase1_command(vol: int, channel_id: str) -> list[str]:
    return [
        sys.executable,
        str(BASE / "app_pipeline.py"),
        str(vol),
        "--only",
        "suno",
        "--channel-id",
        channel_id,
        "--auto",
    ]


def channel_export_engine(channel_folder: Path) -> str:
    try:
        config = json.loads((channel_folder / ".app_channel_config.json").read_text(encoding="utf-8"))
    except Exception:
        return "ame"
    engine = str(config.get("export_engine") or "ame").strip().lower()
    return engine if engine in {"ame", "ffmpeg"} else "ame"


def phase2_commands(vol: int, channel_id: str) -> list[tuple[str, list[str]]]:
    """Build post commands; each pipeline implementation owns its resource lock."""
    post_steps = STEPS[STEPS.index("bgimage"):]
    commands = []
    for step in post_steps:
        pipeline = [
            sys.executable,
            str(BASE / "app_pipeline.py"),
            str(vol),
            "--only",
            step,
            "--channel-id",
            channel_id,
            "--auto",
        ]
        commands.append((step, pipeline))
    return commands


def append_log(log_path: Path, message: str) -> None:
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(message.rstrip() + "\n")


def run_logged(command: list[str], env: dict[str, str], log_path: Path, label: str) -> tuple[int, float]:
    started = time.monotonic()
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"[{label}] command: {' '.join(command)}\n")
        stream.flush()
        code = subprocess.call(command, cwd=str(BASE), env=env, stdout=stream, stderr=subprocess.STDOUT)
        elapsed = time.monotonic() - started
        stream.write(f"[{label}] exit={code} elapsed_sec={elapsed:.1f}\n")
    return code, elapsed


def remove_duplicate_tracks(folder: Path, log_path: Path) -> list[Path]:
    music = folder / "music"
    files = sorted((path for path in music.glob("*.mp3") if path.is_file()), key=lambda path: path.name)
    by_digest: dict[str, list[Path]] = {}
    for path in files:
        digest = hashlib.md5()  # nosec B324 - content identity, not security
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        by_digest.setdefault(digest.hexdigest(), []).append(path)
    removed: list[Path] = []
    for matches in by_digest.values():
        for duplicate in matches[1:]:
            duplicate.unlink()
            removed.append(duplicate)
            append_log(log_path, f"[dedupe] removed duplicate: {duplicate.name}; kept: {matches[0].name}")
    append_log(log_path, f"[dedupe] scanned={len(files)} removed={len(removed)}")
    return removed


def read_youtube_url(folder: Path) -> str:
    try:
        marker = json.loads((folder / "youtube_upload.json").read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(marker.get("url") or (f"https://youtu.be/{marker.get('video_id')}" if marker.get("video_id") else ""))


def completed(folder: Path) -> bool:
    skip, _marker = _local_upload_marker_allows_skip(folder)
    return skip


def format_seconds(value: float) -> str:
    return "-" if not value else f"{value / 60:.1f}m"


def print_summary(results: list[Result]) -> None:
    print("\nBatch summary")
    print("vol | phase1 | phase2 | result | YouTube URL")
    print("----|--------|--------|--------|------------")
    for result in results:
        print(
            f"{result.vol} | {format_seconds(result.phase1_sec)} | {format_seconds(result.phase2_sec)} "
            f"| {result.status} | {result.youtube_url or '-'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Relay-style batch pipeline orchestrator")
    parser.add_argument("--vols", required=True, type=parse_vols, help="range 147-151 or list 147,149,150")
    parser.add_argument(
        "--duration-sec", type=int, default=10800,
        help="target video duration in seconds (default: 10800)",
    )
    parser.add_argument("--channel", default="", help="channel id; defaults to active channel")
    parser.add_argument("--max-post", type=int, default=2, help="maximum concurrent phase2 jobs (default: 2)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan without preflight mutations or subprocesses")
    parser.add_argument("--skip-completed", action="store_true", help="skip vols accepted by the pipeline upload marker rule")
    args = parser.parse_args()
    if args.duration_sec <= 0:
        parser.error("--duration-sec must be positive")
    if args.max_post <= 0:
        parser.error("--max-post must be positive")

    try:
        channel = resolve_channel(args.channel)
        channel_id = str(channel.get("id") or "")
        resolved = []
        missing = []
        for vol in args.vols:
            item = resolve_vol_folder(vol, channel_id=channel_id)
            if not item.get("ok"):
                missing.append(vol)
            else:
                resolved.append((vol, Path(item["folder"])))
        if missing:
            print(
                "Batch aborted: video folders do not exist for vols: " + ", ".join(map(str, missing))
                + ". Create all vol folders before starting the batch.",
                file=sys.stderr,
            )
            return EXIT_PREFLIGHT

        env_preview = {
            "APP_DURATION_SEC": str(args.duration_sec),
            "APP_SUNO_NO_HOLD": "1",
            "APP_SUNO_SKIP_SECOND_DL": "1",
            "APP_SUNO_READY_POLL": "1",
            "APP_SUNO_ONESHOT": "1",
            "APP_SUNO_SKIP_OPTIONAL_TITLE": "1",
            "APP_PROCESS_PARALLEL": process_parallel_default(),
            "APP_CHANNEL_ID": channel_id,
        }
        export_engine = channel_export_engine(Path(channel["folder"]))
        print(f"channel: {channel_id} ({channel.get('name') or '-'})")
        print(f"export-engine: {export_engine}")
        print(f"vols: {','.join(str(vol) for vol, _ in resolved)}")
        print(f"max-post: {args.max_post}")
        print("environment: " + " ".join(f"{key}={value}" for key, value in env_preview.items()))
        for vol, folder in resolved:
            suffix = " [skip-completed candidate]" if args.skip_completed and completed(folder) else ""
            print(f"vol{vol} phase1: {' '.join(phase1_command(vol, channel_id))}{suffix}")
            for step, command in phase2_commands(vol, channel_id):
                print(f"vol{vol} phase2[{step}]: {' '.join(command)}{suffix}")
        if args.dry_run:
            print("dry-run: no server start, channel switch, token refresh, file deletion, or pipeline process was performed")
            return 0

        ensure_server()
        ensure_channel(channel)
        channel_folder = Path(channel["folder"])
        check_youtube_token(channel_folder, resolved[0][1])
        check_disk(channel_folder)
    except Exception as exc:
        print(f"Batch preflight failed: {exc}", file=sys.stderr)
        return EXIT_PREFLIGHT

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = ROOT / "logs" / "batch" / stamp
    log_dir.mkdir(parents=True, exist_ok=False)
    env = command_env(args.duration_sec, channel_id)
    results = [Result(vol=vol, folder=folder) for vol, folder in resolved]

    def run_phase2(result: Result, log_path: Path) -> Result:
        print(f"vol{result.vol} phase2 started")
        for step, command in phase2_commands(result.vol, channel_id):
            try:
                code, elapsed = run_logged(command, env, log_path, f"phase2:{step}")
            except Exception as exc:
                result.phase2_code = 1
                result.status = "phase2 failed"
                append_log(log_path, f"[phase2:{step}] orchestrator error: {type(exc).__name__}: {exc}")
                print(f"vol{result.vol} phase2[{step}] failed before normal exit")
                return result
            result.phase2_sec += elapsed
            if code != 0:
                result.phase2_code = code
                result.status = "phase2 failed"
                print(f"vol{result.vol} phase2[{step}] finished: exit={code}")
                return result
        result.phase2_code = 0
        result.youtube_url = read_youtube_url(result.folder)
        result.status = "ok"
        print(f"vol{result.vol} phase2 finished: exit=0")
        return result

    futures: list[concurrent.futures.Future[Result]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.max_post) as executor:
        for result in results:
            log_path = log_dir / f"vol{result.vol}.log"
            if args.skip_completed and completed(result.folder):
                result.status = "skipped completed"
                result.youtube_url = read_youtube_url(result.folder)
                append_log(log_path, "[batch] skipped: existing upload marker satisfies pipeline skip rule")
                print(f"vol{result.vol} skipped: completed upload marker")
                continue
            print(f"vol{result.vol} phase1 started")
            try:
                result.phase1_code, result.phase1_sec = run_logged(
                    phase1_command(result.vol, channel_id), env, log_path, "phase1"
                )
            except Exception as exc:
                result.phase1_code = 1
                result.status = "phase1 failed"
                append_log(log_path, f"[phase1] orchestrator error: {type(exc).__name__}: {exc}")
                print(f"vol{result.vol} phase1 failed before normal exit")
                continue
            print(f"vol{result.vol} phase1 finished: exit={result.phase1_code}")
            if result.phase1_code != 0:
                result.status = "phase1 failed"
                continue
            track_count = sum(1 for path in (result.folder / "music").glob("*.mp3") if path.is_file())
            if track_count == 0:
                result.status = "no tracks"
                append_log(log_path, "[phase1] validation failed: music/*.mp3 count=0")
                print(f"vol{result.vol} phase1 validation failed: no tracks")
                continue
            append_log(log_path, f"[phase1] validation ok: music/*.mp3 count={track_count}")
            try:
                remove_duplicate_tracks(result.folder, log_path)
            except Exception as exc:
                result.status = "phase1 failed"
                append_log(log_path, f"[dedupe] failed: {type(exc).__name__}: {exc}")
                print(f"vol{result.vol} phase1 dedupe failed")
                continue
            futures.append(executor.submit(run_phase2, result, log_path))
        for future in concurrent.futures.as_completed(futures):
            future.result()

    print_summary(results)
    return 0 if all(result.status in {"ok", "skipped completed"} for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

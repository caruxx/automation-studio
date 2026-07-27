#!/usr/bin/env python3
"""複数 SUNO Workspace の生成待ちを重ねるインターリーブ型キュー。"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from resource_lock import ResourceLock
import suno_auto_create as suno


TERMINAL_STATES = {"done", "failed"}
VALID_MODES = {"lyrics", "lyrics_styles", "styles_title_only", "instrumental_filler"}
WID_RE = re.compile(
    r"[?&]wid=([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
    re.I,
)


@dataclass
class Job:
    index: int
    vol: int
    channel_folder: Path
    workspace: str
    prompt: str
    count: int
    mode: str
    download_dir: Path
    settings: dict[str, Any]
    state: str = "pending"
    wid: str = ""
    submitted_at: float | None = None
    submitted_count: int = 0
    downloaded_count: int = 0
    error: str = ""
    step: str = "pending"

    @property
    def label(self) -> str:
        return self.workspace or f"vol{self.vol}"


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit(job: str, state: str, detail: Any) -> None:
    print(
        json.dumps(
            {"job": job, "state": state, "detail": detail, "timestamp": timestamp()},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def call_existing(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """既存関数の人間向け stdout を隠し、キューの stdout を JSON Lines に保つ。"""
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            return func(*args, **kwargs)
    except BaseException as exc:
        diagnostic = captured.getvalue().strip()
        if diagnostic:
            previous = str(getattr(exc, "captured_output", "") or "").strip()
            exc.captured_output = "\n".join(x for x in (previous, diagnostic) if x)
        raise


def load_channel_suno_config(channel_folder: Path) -> dict[str, Any]:
    path = channel_folder / ".app_channel_config.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"チャンネル設定を読めません: {path}: {exc}") from exc
    suno_config = data.get("suno") or {}
    if not isinstance(suno_config, dict):
        raise ValueError(f"suno 設定がオブジェクトではありません: {path}")
    return suno_config


def load_jobs(path: Path) -> list[Job]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"jobs-file を読めません: {path}: {exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("jobs-file は 1 件以上の JSON 配列にしてください")

    jobs: list[Job] = []
    workspaces: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"job {index} は JSON オブジェクトにしてください")
        try:
            vol = int(item["vol"])
            raw_channel_folder = str(item["channel_folder"]).strip()
            raw_download_dir = str(item["download_dir"]).strip()
            channel_folder = Path(raw_channel_folder).expanduser()
            workspace = str(item["workspace"]).strip()
            count = int(item.get("count", 10))
            mode = str(item.get("mode") or "instrumental_filler").strip()
            download_dir = Path(raw_download_dir).expanduser()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"job {index} の必須項目または型が不正です: {exc}") from exc
        if vol <= 0:
            raise ValueError(f"job {index}: vol は正の整数にしてください")
        if not raw_channel_folder:
            raise ValueError(f"job {index}: channel_folder は空にできません")
        if not workspace:
            raise ValueError(f"job {index}: workspace は空にできません")
        if workspace in workspaces:
            raise ValueError(f"job {index}: workspace が重複しています: {workspace}")
        if count <= 0:
            raise ValueError(f"job {index}: count は正の整数にしてください")
        if mode not in VALID_MODES:
            raise ValueError(f"job {index}: mode が不正です: {mode}")
        if not raw_download_dir:
            raise ValueError(f"job {index}: download_dir は空にできません")

        channel_config = load_channel_suno_config(channel_folder)
        prompt = str(item.get("prompt") or channel_config.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(
                f"job {index}: prompt が空で、{channel_folder / '.app_channel_config.json'} "
                "にも suno.prompt がありません"
            )

        settings = suno.load_config()
        settings.update(channel_config)
        settings.update(
            {
                "workspace": workspace,
                "prompt": prompt,
                "loop_count": count,
                "generation_mode": mode,
                "batch_mode": bool(channel_config.get("loop_batch", True)),
            }
        )
        workspaces.add(workspace)
        jobs.append(
            Job(
                index=index,
                vol=vol,
                channel_folder=channel_folder,
                workspace=workspace,
                prompt=prompt,
                count=count,
                mode=mode,
                download_dir=download_dir,
                settings=settings,
            )
        )
    return jobs


def print_plan(jobs: list[Job]) -> None:
    for order, job in enumerate(jobs, 1):
        emit(
            job.label,
            "planned",
            {
                "order": order,
                "vol": job.vol,
                "count": job.count,
                "mode": job.mode,
                "workspace": job.workspace,
                "download_dir": str(job.download_dir),
                "prompt_head": job.prompt[:80],
            },
        )
    emit("summary", "summary", {"dry_run": True, "total": len(jobs), "failed": 0})


def launch_browser(playwright: Any, headless: bool) -> Any:
    profile_dir = str(Path.home() / ".config/orzz/chromium_profile")
    launch_kwargs = {
        "user_data_dir": profile_dir,
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled", "--no-first-run"],
        "viewport": {"width": 1280, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "accept_downloads": True,
    }
    try:
        return playwright.chromium.launch_persistent_context(**launch_kwargs)
    except Exception:
        return playwright.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)


def ensure_login(page: Any) -> None:
    page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=30000)
    time.sleep(5)
    if suno.is_suno_logged_in(page):
        return
    emit("browser", "login_wait", {"timeout_sec": 300})
    for elapsed in range(300):
        time.sleep(1)
        try:
            url = page.url.lower()
            if "suno.com" in url and not any(x in url for x in ("sign", "login", "clerk")):
                if "/create" not in url:
                    page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=15000)
                    time.sleep(3)
                if suno.is_suno_logged_in(page):
                    emit("browser", "login_ready", {"elapsed_sec": elapsed + 1})
                    return
        except Exception:
            continue
    raise suno.UnattendedLoginRequired("SUNO のログインが 300 秒以内に確認できませんでした")


def workspace_wid(page: Any) -> str:
    match = WID_RE.search(str(page.url or ""))
    return match.group(1) if match else ""


def prepare_drafts(job: Job) -> list[dict[str, Any]] | None:
    provider = str(job.settings.get("provider") or "")
    if job.settings.get("batch_mode") and provider in ("claude", "codex"):
        drafts = call_existing(suno.generate_content_batch, job.settings, job.count)
        if len(drafts) != job.count:
            raise RuntimeError(f"ドラフト数が不足しています: {len(drafts)}/{job.count}")
        return drafts
    return None


def interval_seconds(settings: dict[str, Any], batch_mode: bool) -> int:
    try:
        hard_floor = max(15, int(os.environ.get("APP_SUNO_MIN_WAIT_SEC", "30")))
    except ValueError:
        hard_floor = 30
    try:
        configured = int(settings.get("loop_interval_sec") or 180)
    except (TypeError, ValueError):
        configured = 180
    if batch_mode:
        base = max(hard_floor, min(configured, max(hard_floor, configured // 3)))
    else:
        base = max(hard_floor, configured)
    return max(hard_floor, int(base * (1.0 + random.uniform(-0.3, 0.3))))


def submit_job(page: Any, job: Job) -> None:
    job.state = "submitting"
    job.step = "workspace_ensure"
    emit(job.label, job.state, {"vol": job.vol, "count": job.count})
    if not call_existing(suno.ensure_workspace, page, job.workspace):
        raise RuntimeError("Workspace の確保に失敗しました")
    job.step = "workspace_wid"
    job.wid = workspace_wid(page)
    if not job.wid:
        raise RuntimeError(f"Workspace の wid を取得できませんでした: {page.url}")
    emit(job.label, job.state, {"wid": job.wid, "detail": "workspace_ready"})

    job.step = "draft_generation"
    drafts = prepare_drafts(job)
    direct_url = f"https://suno.com/create?wid={job.wid}"

    for index in range(job.count):
        job.step = f"song_{index + 1}_content"
        content = drafts[index] if drafts is not None else call_existing(suno.generate_content, job.settings)
        # LLM 起草中や前曲送信後の SPA 状態を持ち越さず、必ず対象 wid を開き直す。
        job.step = f"song_{index + 1}_workspace_navigation"
        page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        if workspace_wid(page) != job.wid:
            raise RuntimeError(f"対象 Workspace への遷移を確認できませんでした: {page.url}")
        emit(
            job.label,
            job.state,
            {
                "wid": job.wid,
                "detail": "workspace_reopened",
                "song": index + 1,
                "page_url": page.url,
            },
        )
        job.step = f"song_{index + 1}_submission"
        call_existing(suno.submit_song_to_suno, page, content, form_retries=2)
        job.submitted_count += 1
        emit(
            job.label,
            job.state,
            {"submitted": job.submitted_count, "total": job.count, "wid": job.wid},
        )
        if index + 1 < job.count:
            wait_sec = interval_seconds(job.settings, drafts is not None)
            emit(job.label, job.state, {"wait_sec": wait_sec, "after": job.submitted_count})
            time.sleep(wait_sec)

    job.submitted_at = time.time()
    job.state = "rendering"
    job.step = "rendering"
    emit(
        job.label,
        job.state,
        {"submitted": job.submitted_count, "wid": job.wid, "submitted_at": timestamp()},
    )


def postprocess(job: Job) -> None:
    command = [sys.executable, str(Path(__file__).parent / "app_process_tracks.py"), str(job.download_dir)]
    result = subprocess.run(
        command,
        cwd=str(Path(__file__).parent),
        text=True,
        capture_output=True,
        timeout=7200,
        check=False,
    )
    if result.returncode:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
        raise RuntimeError(
            f"app_process_tracks.py が exit {result.returncode} で失敗しました"
            + (f": {tail[0]}" if tail else "")
        )


def download_job(page: Any, job: Job) -> None:
    if not job.wid:
        raise RuntimeError("wid がないためダウンロードできません")
    job.state = "downloading"
    job.step = "workspace_download"
    direct_url = f"https://suno.com/create?wid={job.wid}"
    emit(job.label, job.state, {"wid": job.wid, "download_dir": str(job.download_dir)})
    job.downloaded_count = int(
        call_existing(suno.download_workspace_tracks, page, direct_url, str(job.download_dir)) or 0
    )
    expected = job.submitted_count * 2
    if job.downloaded_count < expected:
        raise RuntimeError(f"ダウンロード数が不足しています: {job.downloaded_count}/{expected}")
    emit(job.label, job.state, {"downloaded": job.downloaded_count, "expected": expected})
    job.step = "postprocess"
    postprocess(job)
    job.state = "done"
    job.step = "completed"
    emit(
        job.label,
        job.state,
        {"downloaded": job.downloaded_count, "postprocess": "completed", "wid": job.wid},
    )


def fail_job(job: Job, exc: BaseException, page: Any = None, trace: str = "") -> None:
    job.state = "failed"
    job.error = str(exc)
    try:
        page_url = str(page.url or "") if page is not None else ""
    except Exception:
        page_url = ""
    detail = {
        "error": job.error,
        "exception_type": type(exc).__name__,
        "step": str(getattr(exc, "step", "") or job.step or "unknown"),
        "page_url": page_url,
        "wid": job.wid,
        "traceback": trace or "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    captured_output = str(getattr(exc, "captured_output", "") or "").strip()
    if captured_output:
        detail["diagnostic_output"] = captured_output
    emit(job.label, job.state, detail)


def emit_summary(jobs: list[Job]) -> None:
    failed = [job for job in jobs if job.state == "failed"]
    emit(
        "summary",
        "summary",
        {
            "total": len(jobs),
            "done": sum(job.state == "done" for job in jobs),
            "failed": len(failed),
            "failed_jobs": [job.label for job in failed],
        },
    )


def run_scheduler(jobs: list[Job], page: Any, final_wait_sec: int) -> int:
    while any(job.state not in TERMINAL_STATES for job in jobs):
        pending = next((job for job in jobs if job.state == "pending"), None)
        if pending is not None and not any(job.state == "submitting" for job in jobs):
            try:
                submit_job(page, pending)
            except Exception as exc:
                fail_job(pending, exc, page, traceback.format_exc())

        now = time.time()
        ready = sorted(
            (
                job
                for job in jobs
                if job.state == "rendering"
                and job.submitted_at is not None
                and now - job.submitted_at >= final_wait_sec
            ),
            key=lambda item: item.submitted_at or 0,
        )
        if ready:
            job = ready[0]
            try:
                download_job(page, job)
            except Exception as exc:
                fail_job(job, exc, page, traceback.format_exc())
            continue

        if pending is None:
            rendering = [job for job in jobs if job.state == "rendering" and job.submitted_at is not None]
            if rendering:
                next_ready = min((job.submitted_at or now) + final_wait_sec for job in rendering)
                wait_sec = max(1, min(30, int(next_ready - now + 0.999)))
                emit("scheduler", "waiting", {"wait_sec": wait_sec, "rendering": len(rendering)})
                time.sleep(wait_sec)

    failed = [job for job in jobs if job.state == "failed"]
    emit_summary(jobs)
    return 1 if failed else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SUNO 複数 Workspace インターリーブキュー")
    parser.add_argument("--jobs-file", required=True, type=Path, help="ジョブ JSON 配列のパス")
    parser.add_argument("--dry-run", action="store_true", help="ブラウザとロックを使わず計画だけ表示")
    parser.add_argument("--headless", action="store_true", help="ブラウザをヘッドレスで起動")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        jobs = load_jobs(args.jobs_file)
    except ValueError as exc:
        emit("queue", "failed", {"error": str(exc)})
        return 1

    if args.dry_run:
        print_plan(jobs)
        return 0

    try:
        final_wait_sec = max(0, int(os.environ.get("APP_SUNO_FINAL_WAIT_SEC", "300")))
    except ValueError:
        emit("queue", "failed", {"error": "APP_SUNO_FINAL_WAIT_SEC は整数にしてください"})
        return 1

    lock = ResourceLock("suno", owner="suno_queue").acquire(blocking=True)
    atexit.register(lock.release)
    context = None
    page = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = launch_browser(playwright, args.headless)
            context.add_init_script(suno._SUNO_AUDIO_URL_INTERCEPTOR)
            page = context.pages[0] if context.pages else context.new_page()
            ensure_login(page)
            emit("queue", "started", {"jobs": len(jobs), "final_wait_sec": final_wait_sec})
            for job in jobs:
                emit(job.label, job.state, {"order": job.index, "vol": job.vol})
            return run_scheduler(jobs, page, final_wait_sec)
    except KeyboardInterrupt:
        trace = traceback.format_exc()
        for job in jobs:
            if job.state not in TERMINAL_STATES:
                fail_job(job, RuntimeError("ユーザー操作で中断されました"), page, trace)
        emit_summary(jobs)
        return 130
    except Exception as exc:
        trace = traceback.format_exc()
        emit("queue", "failed", {"error": str(exc)})
        for job in jobs:
            if job.state not in TERMINAL_STATES:
                fail_job(job, exc, page, trace)
        emit_summary(jobs)
        return 1
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

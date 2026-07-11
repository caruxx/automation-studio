#!/usr/bin/env python3
"""Opt-in parallelism guard for Automation Studio commands.

Usage:
  python3 parallel_guard.py <intent> -- <cmd...>

The guard reads routes.json, maps the intent to a lock scope, then runs the
command while holding an flock lock under /tmp/automation_studio_locks.
It does not change existing pipeline/API behavior; callers must opt in.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


BASE = Path(__file__).resolve().parent
ROUTES_FILE = BASE / "routes.json"
from resource_lock import LOCK_DIR, ResourceBusyError, ResourceLock, canonical_resource, safe_name

RESOURCE_LOCKS = {
    "suno": "suno-browser",
    "suno-auto": "suno-browser",
    "suno-download": "suno-browser",
    "premiere": "premiere-ame",
    "place-images": "premiere-ame",
    "export": "premiere-ame",
    "pipeline-from-premiere": "premiere-ame",
    "psd": "photoshop",
    "psd_composite": "photoshop",
}


def _load_routes() -> dict:
    try:
        data = json.loads(ROUTES_FILE.read_text(encoding="utf-8"))
        intents = data.get("intents") or {}
        return intents if isinstance(intents, dict) else {}
    except Exception:
        return {}


def resolve_lock_name(intent: str, channel: str = "") -> tuple[str, dict]:
    intents = _load_routes()
    route = intents.get(intent) or {}
    if intent in RESOURCE_LOCKS:
        return RESOURCE_LOCKS[intent], route

    par = route.get("parallelism") or {}
    scope = str(par.get("scope") or "global")
    max_parallel = int(par.get("max_parallel") or 1)
    if max_parallel > 1:
        return f"{scope}-{intent}", route
    if scope == "per-channel":
        return f"per-channel-{safe_name(channel or os.environ.get('APP_CHANNEL_ID') or 'active')}", route
    if scope == "per-machine":
        return "per-machine", route
    return f"global-{intent}", route


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a command under a routes.json based flock guard.")
    parser.add_argument("intent", help="routes.json intent name, e.g. suno, premiere, psd")
    parser.add_argument("--channel", default="", help="channel id for per-channel single-resource intents")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="command after --")
    args = parser.parse_args()

    cmd = list(args.cmd or [])
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("command is required after --", file=sys.stderr)
        return 2

    lock_name, route = resolve_lock_name(args.intent, args.channel)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f"{safe_name(canonical_resource(lock_name))}.lock"
    started = time.time()
    print(
        json.dumps(
            {
                "event": "waiting",
                "intent": args.intent,
                "lock": str(lock_path),
                "parallelism": route.get("parallelism") or {},
                "cmd": cmd,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    lock = ResourceLock(lock_name, owner=f"parallel_guard:{args.intent}")
    try:
        lock.acquire(blocking=True)
        waited = round(time.time() - started, 3)
        print(
            json.dumps({"event": "acquired", "intent": args.intent, "lock": str(lock_path), "waited_sec": waited}, ensure_ascii=False),
            flush=True,
        )
        try:
            return subprocess.call(cmd)
        finally:
            print(json.dumps({"event": "released", "intent": args.intent, "lock": str(lock_path)}, ensure_ascii=False), flush=True)
            lock.release()
    except ResourceBusyError as exc:
        print(str(exc), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())

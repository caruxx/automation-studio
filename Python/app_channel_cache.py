#!/usr/bin/env python3
"""Channel-scoped cache helpers.

Benchmark outputs used to live in one global config directory, which meant a
fresh channel could accidentally reuse the previous channel's analysis.  This
module keeps the old files as compatibility mirrors, but prefers a scoped file
under ``benchmark/channels/<channel_id>/`` and only trusts legacy files that are
explicitly stamped for the current channel.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from _app_config import (
        resolve_config_dir as _resolve_config_dir,
        resolve_shared_config_dir as _resolve_shared_config_dir,
    )
    CONFIG_DIR = _resolve_config_dir()
    SHARED_CONFIG_DIR = _resolve_shared_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
    SHARED_CONFIG_DIR = CONFIG_DIR

DASHBOARD_CONFIG = CONFIG_DIR / "dashboard_config.json"
# channels.json は PC 間共有のため共有ドライブ側を読む（channel_id 解決を別 PC と一致させる）
CHANNELS_CONFIG = SHARED_CONFIG_DIR / "channels.json"
CHANNEL_CONTEXT_KEY = "_channel_context"
ALLOW_UNSCOPED_ENV = "APP_ALLOW_UNSCOPED_BENCHMARK_CACHE"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return default


def _safe_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip()).strip("_")
    return key[:80] or "active"


def _resolve_registry_entry(channel_id: str = "", channel_folder: str = "") -> dict:
    channels = _load_json(CHANNELS_CONFIG, [])
    if not isinstance(channels, list):
        return {}
    if channel_id:
        return next((c for c in channels if c.get("id") == channel_id), {}) or {}
    if not channel_folder:
        return {}
    try:
        target = str(Path(channel_folder).expanduser().resolve())
    except Exception:
        target = str(channel_folder)
    for ch in channels:
        try:
            folder = str(Path(ch.get("folder") or "").expanduser().resolve())
        except Exception:
            folder = str(ch.get("folder") or "")
        if folder == target:
            return ch
    return {}


def active_channel_context() -> dict:
    dashboard = _load_json(DASHBOARD_CONFIG, {})
    if not isinstance(dashboard, dict):
        dashboard = {}

    env_id = (os.environ.get("APP_CHANNEL_ID") or "").strip()
    env_folder = (os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    env_name = (os.environ.get("APP_CHANNEL_NAME") or "").strip()

    entry = _resolve_registry_entry(channel_id=env_id) if env_id else {}
    if not entry:
        entry = _resolve_registry_entry(channel_folder=env_folder or dashboard.get("channel_folder", ""))

    folder = env_folder or entry.get("folder") or dashboard.get("channel_folder") or ""
    name = env_name or entry.get("name") or dashboard.get("channel_name") or Path(folder).name
    channel_id = env_id or entry.get("id") or ""
    key = _safe_key(channel_id or name or folder)

    return {
        "id": channel_id,
        "key": key,
        "name": name or "",
        "folder": str(folder or ""),
    }


def scoped_cache_dir() -> Path:
    # PC 間共有: scoped 分析(concept/title/thumbnail 等)を共有ドライブに置く。
    # ※サムネ「画像」は別管理(CONFIG_DIR/benchmark/thumbs)でローカル維持。
    ctx = active_channel_context()
    return SHARED_CONFIG_DIR / "benchmark" / "channels" / ctx["key"]


def scoped_cache_path(filename: str) -> Path:
    return scoped_cache_dir() / filename


def stamp_payload(payload: dict) -> dict:
    stamped = dict(payload or {})
    stamped[CHANNEL_CONTEXT_KEY] = active_channel_context()
    return stamped


def payload_matches_current(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    saved = payload.get(CHANNEL_CONTEXT_KEY) or {}
    if not isinstance(saved, dict):
        return False
    current = active_channel_context()
    if saved.get("id") and current.get("id"):
        return saved.get("id") == current.get("id")
    if saved.get("folder") and current.get("folder"):
        try:
            return str(Path(saved["folder"]).expanduser().resolve()) == str(Path(current["folder"]).expanduser().resolve())
        except Exception:
            return str(saved.get("folder")) == str(current.get("folder"))
    return bool(saved.get("key") and saved.get("key") == current.get("key"))


def allow_unscoped_cache() -> bool:
    return (os.environ.get(ALLOW_UNSCOPED_ENV) or "").strip().lower() in ("1", "true", "yes")


def load_scoped_cache(filename: str, legacy_path: Path, default: Any) -> Any:
    scoped = scoped_cache_path(filename)
    data = _load_json(scoped, None)
    if isinstance(data, dict):
        return data

    legacy = _load_json(legacy_path, None)
    if isinstance(legacy, dict) and (payload_matches_current(legacy) or allow_unscoped_cache()):
        return legacy
    return default


def save_scoped_cache(filename: str, legacy_path: Path, payload: dict) -> dict:
    stamped = stamp_payload(payload)
    scoped = scoped_cache_path(filename)
    scoped.parent.mkdir(parents=True, exist_ok=True)
    scoped.write_text(json.dumps(stamped, ensure_ascii=False, indent=2), encoding="utf-8")

    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(json.dumps(stamped, ensure_ascii=False, indent=2), encoding="utf-8")
    return stamped


def delete_scoped_and_legacy(filename: str, legacy_path: Path) -> bool:
    deleted = False
    for path in (scoped_cache_path(filename), legacy_path):
        if path.exists():
            path.unlink()
            deleted = True
    return deleted

#!/usr/bin/env python3
"""Settings catalog + validated config read/write helpers."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _app_config import resolve_config_dir, resolve_shared_base, resolve_shared_config_dir
from atomic_json import atomic_write_json, file_lock, load_json as _atomic_load_json

CONFIG_DIR = resolve_config_dir()
SHARED_BASE = resolve_shared_base()
SHARED_CONFIG_DIR = resolve_shared_config_dir()
CATALOG_PATH = Path(__file__).resolve().parent / "settings_catalog.json"
CHANNEL_CONFIG_FILENAME = ".app_channel_config.json"
AUDIT_FILE = CONFIG_DIR / "config_audit.jsonl"


def _load_json(path: Path, default: Any = None) -> Any:
    return _atomic_load_json(path, default)


def _save_json(path: Path, data: Any) -> None:
    atomic_write_json(path, data, ensure_ascii=False, indent=2)


def load_catalog() -> dict[str, Any]:
    data = _load_json(CATALOG_PATH, {"version": 1, "settings": []})
    settings = data.get("settings") if isinstance(data, dict) else []
    data["settings"] = settings if isinstance(settings, list) else []
    return data


def _catalog_map() -> dict[str, dict[str, Any]]:
    return {str(x.get("key")): x for x in load_catalog().get("settings", []) if x.get("key")}


def find_setting(key: str) -> dict[str, Any]:
    item = _catalog_map().get(key)
    if not item:
        raise KeyError(f"unknown setting: {key}")
    return item


def search_settings(term: str) -> list[dict[str, Any]]:
    q = (term or "").strip().lower()
    out = []
    for item in load_catalog().get("settings", []):
        hay = " ".join(str(item.get(k, "")) for k in ("key", "label_ja", "description_ja")).lower()
        if not q or q in hay:
            out.append(item)
    return out


def _channels_path() -> Path:
    return SHARED_CONFIG_DIR / "channels.json"


def resolve_channel(channel_id: str) -> dict[str, Any]:
    channel_id = (channel_id or "").strip()
    if not channel_id:
        raise ValueError("--channel <id> is required for channel scoped settings")
    channels = _load_json(_channels_path(), [])
    for ch in channels if isinstance(channels, list) else []:
        if ch.get("id") == channel_id:
            out = dict(ch)
            if out.get("folder"):
                out["folder"] = str(_resolve_channel_folder_to_current_host(out["folder"]))
            return out
    raise KeyError(f"channel not found: {channel_id}")


def _resolve_channel_folder_to_current_host(folder: str) -> Path:
    p = Path(str(folder or "")).expanduser()
    if p.exists():
        return p
    text = str(folder or "")
    marker = "/YT/"
    idx = text.find(marker)
    if idx >= 0:
        rest = text[idx + len(marker):]
        drive_root = _shared_drive_root()
        cand = drive_root / "YT" / rest
        if cand.exists():
            return cand
    return p


def _shared_drive_root() -> Path:
    import os
    env_base = os.environ.get("STUDIO_DRIVE_BASE") or os.environ.get("APP_DRIVE_BASE")
    if env_base:
        text = str(Path(env_base).expanduser())
        marker = "/共有ドライブ/"
        idx = text.find(marker)
        if idx >= 0:
            return Path(text[:idx + len(marker) - 1])
    text = str(SHARED_CONFIG_DIR)
    marker = "/共有ドライブ/"
    idx = text.find(marker)
    if idx >= 0:
        return Path(text[:idx + len(marker) - 1])
    return SHARED_BASE.parent.parent


def _storage_path(item: dict[str, Any], channel_id: str = "") -> Path:
    storage = item.get("storage") or {}
    file_id = storage.get("file")
    if file_id == "dashboard_config.json":
        return CONFIG_DIR / "dashboard_config.json"
    if file_id == "suno_config.json":
        return CONFIG_DIR / "suno_config.json"
    if file_id == "youtube_upload_defaults.json":
        return CONFIG_DIR / "youtube_upload_defaults.json"
    if file_id == "benchmark_config.json":
        return SHARED_CONFIG_DIR / "benchmark_config.json"
    if file_id == "update_config.json":
        return SHARED_CONFIG_DIR / "update_config.json"
    if file_id == "prompts.json":
        return SHARED_CONFIG_DIR / "prompts.json"
    if file_id == "ops_config.json":
        return SHARED_CONFIG_DIR / "ops_config.json"
    if file_id == "discord_config.json":
        return SHARED_CONFIG_DIR / "discord_config.json"
    if file_id == "channel_config":
        ch = resolve_channel(channel_id)
        folder = Path(str(ch.get("folder") or "")).expanduser()
        return folder / CHANNEL_CONFIG_FILENAME
    raise ValueError(f"unsupported storage file: {file_id}")


def _get_nested(data: dict[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_nested(data: dict[str, Any], dotted: str, value: Any) -> None:
    cur = data
    parts = dotted.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _parse_value(raw: str, item: dict[str, Any]) -> Any:
    typ = item.get("type") or "string"
    if typ == "string":
        value: Any = raw
    elif typ == "integer":
        value = int(raw)
    elif typ == "number":
        value = float(raw)
    elif typ == "boolean":
        v = str(raw).strip().lower()
        if v in {"1", "true", "yes", "on", "y"}:
            value = True
        elif v in {"0", "false", "no", "off", "n"}:
            value = False
        else:
            raise ValueError("boolean value must be true/false")
    elif typ in {"array", "multiselect"}:
        try:
            parsed = json.loads(raw)
            value = parsed if isinstance(parsed, list) else [parsed]
        except Exception:
            value = [x.strip() for x in raw.replace("\\n", "\n").splitlines() if x.strip()]
    else:
        try:
            value = json.loads(raw)
        except Exception:
            value = raw

    choices = item.get("choices")
    if choices:
        if isinstance(value, list):
            invalid = [x for x in value if x not in choices]
            if invalid:
                raise ValueError(f"value must be one of: {', '.join(map(str, choices))}")
        elif value not in choices:
            raise ValueError(f"value must be one of: {', '.join(map(str, choices))}")
    validation = item.get("validation") or {}
    if isinstance(value, (int, float)):
        if "min" in validation and value < validation["min"]:
            raise ValueError(f"value must be >= {validation['min']}")
        if "max" in validation and value > validation["max"]:
            raise ValueError(f"value must be <= {validation['max']}")
    if isinstance(value, str):
        if validation.get("non_empty") and not value.strip():
            raise ValueError("value must not be empty")
        if "max_length" in validation and len(value) > int(validation["max_length"]):
            raise ValueError(f"value must be <= {validation['max_length']} chars")
        pattern = validation.get("pattern")
        if pattern and not re.match(str(pattern), value):
            raise ValueError(f"value must match pattern: {pattern}")
        if validation.get("time_hhmm"):
            try:
                hh, mm = map(int, value.split(":"))
            except Exception:
                raise ValueError("value must be HH:MM")
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                raise ValueError("value must be HH:MM")
    return value


def _mask_if_secret(key: str, value: Any) -> Any:
    low = key.lower()
    if any(x in low for x in ("api_key", "secret", "token", "webhook")):
        if not value:
            return value
        return "●●●●●●●●"
    return value


def _is_secret_item(item: dict[str, Any]) -> bool:
    key = str(item.get("key") or "").lower()
    if item.get("secret") is True:
        return True
    return any(x in key for x in ("api_key", "secret", "token", "webhook"))


def append_config_audit(actor: str, key: str, old: Any, new: Any,
                        *, channel_id: str = "", path: str = "") -> None:
    if old == new:
        return
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "when": datetime.now(timezone.utc).isoformat(),
            "actor": actor or "unknown",
            "key": key,
            "old": _mask_if_secret(key, old),
            "new": _mask_if_secret(key, new),
        }
        if channel_id:
            row["channel_id"] = channel_id
        if path:
            row["path"] = path
        with file_lock(AUDIT_FILE, exclusive=True, timeout=10):
            with AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                f.flush()
                import os
                os.fsync(f.fileno())
    except Exception:
        pass


def audit_dict_changes(actor: str, prefix: str, old: dict[str, Any], new: dict[str, Any],
                       *, channel_id: str = "", path: str = "") -> None:
    keys = sorted(set((old or {}).keys()) | set((new or {}).keys()))
    for k in keys:
        if str(k).startswith("_"):
            continue
        append_config_audit(actor, f"{prefix}.{k}" if prefix else str(k),
                            (old or {}).get(k), (new or {}).get(k),
                            channel_id=channel_id, path=path)


def config_get(key: str, *, channel_id: str = "") -> dict[str, Any]:
    item = find_setting(key)
    path = _storage_path(item, channel_id)
    data = _load_json(path, {})
    value = _get_nested(data if isinstance(data, dict) else {}, (item.get("storage") or {}).get("path") or key)
    if _is_secret_item(item):
        return {"key": key, "value": "", "masked_value": _mask_if_secret(key, value), "configured": bool(value), "path": str(path), "setting": item}
    return {"key": key, "value": value, "path": str(path), "setting": item}


def config_set(key: str, raw_value: str, *, channel_id: str = "", actor: str = "ai") -> dict[str, Any]:
    item = find_setting(key)
    if item.get("scope") == "channel" and not channel_id:
        raise ValueError("--channel <id> is required for this setting")
    if item.get("scope") == "channel" and channel_id == "all":
        if (item.get("storage") or {}).get("file") != "channel_config":
            raise ValueError("channel_id=all is only supported for channel_config settings")
        channels = _load_json(_channels_path(), [])
        parsed = _parse_value(raw_value, item)
        results = []
        for ch in channels if isinstance(channels, list) else []:
            cid = str(ch.get("id") or "").strip()
            if cid:
                results.append(config_set(key, raw_value, channel_id=cid, actor=actor))
        append_config_audit(actor, key, None, parsed, channel_id="all", path="all")
        return {"key": key, "old": None, "new": parsed, "path": "all", "channel_id": "all", "results": results, "count": len(results)}
    path = _storage_path(item, channel_id)
    data = _load_json(path, {})
    if not isinstance(data, dict):
        data = {}
    dotted = (item.get("storage") or {}).get("path") or key
    old = _get_nested(data, dotted)
    new = _parse_value(raw_value, item)
    _set_nested(data, dotted, new)
    _save_json(path, data)
    append_config_audit(actor, key, old, new, channel_id=channel_id, path=str(path))
    return {"key": key, "old": old, "new": new, "path": str(path), "channel_id": channel_id}

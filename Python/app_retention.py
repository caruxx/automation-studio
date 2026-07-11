#!/usr/bin/env python3
"""YouTube API data retention and disconnect helpers."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from _app_config import resolve_config_dir, resolve_shared_config_dir
    CONFIG_DIR = resolve_config_dir()
    SHARED_CONFIG_DIR = resolve_shared_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
    SHARED_CONFIG_DIR = CONFIG_DIR

RETENTION_LOG = Path(os.environ.get("APP_YT_RETENTION_LOG") or (CONFIG_DIR / "data_retention_log.jsonl"))
DISCONNECT_AUDIT_LOG = Path(os.environ.get("APP_YT_DISCONNECT_AUDIT_LOG") or (CONFIG_DIR / "youtube_disconnect_audit.jsonl"))
RETENTION_DAYS = int(os.environ.get("APP_YT_RETENTION_DAYS", "30"))
TOKEN_FILENAME = ".youtube_token.json"
API_CACHE_NAMES = {
    "competitor_analysis_cache.json",
    "benchmark_profiles.json",
    "onboarding_verify_report.json",
    "tracking_snapshots.json",
    "tracking_events.json",
}
API_CACHE_PATTERNS = [
    ".studio_learning/*.json",
    ".studio_learning/*.jsonl",
    ".studio_learning/comment_mining_raw_*.json",
    "benchmark/**/*.json",
    "benchmark/**/*.jsonl",
    "genre_radar/**/*.json",
    "genre_radar/**/*.jsonl",
    ".cache/youtube*.json",
    ".cache/youtube*.jsonl",
]
STAT_NAMES = {"monetization_snapshots.json", "onboarding_verify_report.json"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def append_log(path: Path, row: dict[str, Any]) -> None:
    row.setdefault("when", now_iso())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def ensure_fetched_at(obj: Any, fetched_at: str | None = None) -> Any:
    fetched_at = fetched_at or now_iso()
    if isinstance(obj, dict):
        obj.setdefault("fetched_at", fetched_at)
        for key in ("competitor_data", "growth_summary", "analysis", "channels", "items", "videos", "records"):
            val = obj.get(key)
            if isinstance(val, (dict, list)):
                obj[key] = ensure_fetched_at(val, fetched_at)
        return obj
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                ensure_fetched_at(item, fetched_at)
        return obj
    return obj


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def migrate_file(path: Path, *, dry_run: bool = False) -> bool:
    data = read_json(path, None)
    if data is None:
        return False
    before = json.dumps(data, ensure_ascii=False, sort_keys=True)
    ensure_fetched_at(data)
    changed = json.dumps(data, ensure_ascii=False, sort_keys=True) != before
    if changed and not dry_run:
        write_json(path, data)
    return changed


def file_fetched_at(path: Path) -> dt.datetime:
    data = read_json(path, {})
    value = ""
    if isinstance(data, dict):
        value = str(data.get("fetched_at") or data.get("analyzed_at") or data.get("generated_at") or "")
    if not value:
        value = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc).isoformat()
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)


def retention_enabled(channel_dir: Path | None = None) -> bool:
    if channel_dir:
        cfg = read_json(channel_dir / ".app_channel_config.json", {})
        ret = cfg.get("retention") if isinstance(cfg, dict) else None
        if isinstance(ret, dict) and "enabled" in ret:
            return bool(ret.get("enabled"))
    return True


def iter_api_files(channel_dir: Path) -> list[Path]:
    found: set[Path] = set()
    for name in API_CACHE_NAMES:
        p = channel_dir / name
        if p.exists():
            found.add(p)
    for pattern in API_CACHE_PATTERNS:
        for p in channel_dir.glob(pattern):
            if p.is_file():
                found.add(p)
    return sorted(found)


def run_retention(channel_dirs: list[str | Path], *, dry_run: bool = False) -> dict[str, Any]:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=RETENTION_DAYS)
    report = {"status": "ok", "dry_run": dry_run, "cutoff": cutoff.isoformat(), "channels": [], "deleted": 0, "migrated": 0, "kept_stats": 0}
    for raw in channel_dirs:
        ch_dir = Path(raw)
        if not ch_dir.exists() or not retention_enabled(ch_dir):
            continue
        entry = {"folder": str(ch_dir), "expired": [], "migrated": [], "kept_stats": []}
        for path in iter_api_files(ch_dir):
            changed = migrate_file(path, dry_run=dry_run)
            if changed:
                report["migrated"] += 1
                entry["migrated"].append(str(path))
            fetched = file_fetched_at(path)
            if fetched >= cutoff:
                continue
            is_stats = path.name in STAT_NAMES or ".studio_learning" in path.parts
            if is_stats:
                entry["kept_stats"].append({"path": str(path), "token_checked_at": now_iso()})
                report["kept_stats"] += 1
                append_log(RETENTION_LOG, {"action": "keep_stats", "path": str(path), "dry_run": dry_run, "token_checked_at": now_iso()})
            else:
                entry["expired"].append(str(path))
                if not dry_run:
                    try:
                        path.unlink()
                    except Exception:
                        pass
                report["deleted"] += 1
                append_log(RETENTION_LOG, {"action": "delete_expired", "path": str(path), "dry_run": dry_run})
        if entry["expired"] or entry["migrated"] or entry["kept_stats"]:
            report["channels"].append(entry)
    append_log(RETENTION_LOG, {"action": "retention_run", "dry_run": dry_run, "summary": {k: report[k] for k in ("deleted", "migrated", "kept_stats")}})
    return report


def revoke_token_file(token_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    data = read_json(token_path, {})
    token = str(data.get("refresh_token") or data.get("token") or "")
    if not token:
        return {"status": "no_token", "token_path": str(token_path)}
    if dry_run:
        return {"status": "dry_run", "token_path": str(token_path)}
    body = urllib.parse.urlencode({"token": token}).encode("utf-8")
    req = urllib.request.Request("https://oauth2.googleapis.com/revoke", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as res:
        return {"status": "ok", "code": res.status, "token_path": str(token_path)}


def disconnect_channel(channel_id: str, channel_dir: str | Path, *, dry_run: bool = False,
                       revoke: bool = True) -> dict[str, Any]:
    ch_dir = Path(channel_dir)
    deleted: list[str] = []
    token_path = ch_dir / TOKEN_FILENAME
    revoke_result = {"status": "skipped"}
    if token_path.exists() and revoke:
        try:
            revoke_result = revoke_token_file(token_path, dry_run=dry_run)
        except Exception as e:
            revoke_result = {"status": "error", "error": str(e), "token_path": str(token_path)}
    targets = iter_api_files(ch_dir)
    if token_path.exists():
        targets.append(token_path)
    for p in sorted(set(targets)):
        deleted.append(str(p))
        if dry_run:
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except Exception:
            pass
    row = {"action": "youtube_disconnect", "channel_id": channel_id, "channel_dir": str(ch_dir), "dry_run": dry_run, "revoke": revoke_result, "deleted": deleted}
    append_log(DISCONNECT_AUDIT_LOG, row)
    return {"status": "ok", "channel_id": channel_id, "dry_run": dry_run, "revoke": revoke_result, "deleted": deleted, "audit_log": str(DISCONNECT_AUDIT_LOG)}

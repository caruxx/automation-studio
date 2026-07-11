#!/usr/bin/env python3
"""設定ファイルの日次バックアップと復元."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _app_config import resolve_shared_base, resolve_shared_config_dir  # noqa: E402
from app_core import get_channels  # noqa: E402
from settings_service import append_config_audit  # noqa: E402

SHARED_BASE = resolve_shared_base()
CONFIG_DIR = resolve_shared_config_dir()
BACKUP_ROOT = SHARED_BASE / "config_backups"
CHANNEL_CONFIG_NAME = ".app_channel_config.json"
KEEP_GENERATIONS = 14


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    shutil.copytree(src, dst, ignore=ignore)


def list_backups() -> dict[str, Any]:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(BACKUP_ROOT.iterdir(), reverse=True):
        if not p.is_dir() or not p.name.isdigit() or len(p.name) != 8:
            continue
        manifest = p / "manifest.json"
        data = {}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            pass
        items.append({
            "date": p.name,
            "path": str(p),
            "created_at": data.get("created_at", ""),
            "channel_config_count": len(data.get("channel_configs") or []),
        })
    return {"status": "ok", "backup_root": str(BACKUP_ROOT), "backups": items}


def rotate(keep: int = KEEP_GENERATIONS) -> list[str]:
    dirs = [p for p in BACKUP_ROOT.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 8]
    dirs.sort(reverse=True)
    removed = []
    for p in dirs[keep:]:
        shutil.rmtree(p, ignore_errors=True)
        removed.append(p.name)
    return removed


def create_backup(date: str | None = None, *, actor: str = "daily") -> dict[str, Any]:
    date = date or _today()
    dest = BACKUP_ROOT / date
    dest.mkdir(parents=True, exist_ok=True)
    config_dest = dest / "config"
    _copytree(CONFIG_DIR, config_dest)
    channel_entries = []
    channel_root = dest / "channel_configs"
    channel_root.mkdir(parents=True, exist_ok=True)
    for ch in get_channels():
        folder = Path(ch.get("folder") or "")
        src = folder / CHANNEL_CONFIG_NAME
        if not src.exists():
            continue
        safe_id = ch.get("id") or folder.name
        out_dir = channel_root / safe_id
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / CHANNEL_CONFIG_NAME
        shutil.copy2(src, dst)
        channel_entries.append({
            "channel_id": ch.get("id", ""),
            "channel_name": ch.get("name", ""),
            "folder": str(folder),
            "source": str(src),
            "backup": str(dst.relative_to(dest)),
        })
    manifest = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "date": date,
        "config_dir": str(CONFIG_DIR),
        "channel_configs": channel_entries,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    removed = rotate()
    append_config_audit(actor, "config_backup.create", None, date, path=str(dest))
    return {
        "status": "ok",
        "date": date,
        "path": str(dest),
        "config_path": str(config_dest),
        "channel_config_count": len(channel_entries),
        "rotated": removed,
    }


def _snapshot_before_restore(date: str) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return create_backup(f"pre_restore_{date}_{stamp}", actor="restore-snapshot")


def restore_backup(date: str, *, actor: str = "ai") -> dict[str, Any]:
    src = BACKUP_ROOT / date
    if not src.exists() or not src.is_dir():
        raise FileNotFoundError(f"backup not found: {date}")
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    before = _snapshot_before_restore(date)
    backup_config = src / "config"
    restored_config_files = 0
    if backup_config.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        for item in backup_config.rglob("*"):
            rel = item.relative_to(backup_config)
            dst = CONFIG_DIR / rel
            if item.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dst)
                restored_config_files += 1
    restored_channel_configs = 0
    for entry in manifest.get("channel_configs") or []:
        rel = entry.get("backup") or ""
        target = Path(entry.get("source") or "")
        if not rel or not target:
            continue
        src_file = src / rel
        if not src_file.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, target)
        restored_channel_configs += 1
        append_config_audit(actor, "config_backup.restore.channel_config", None, date,
                            channel_id=entry.get("channel_id", ""), path=str(target))
    append_config_audit(actor, "config_backup.restore", None, date, path=str(src))
    return {
        "status": "ok",
        "date": date,
        "source": str(src),
        "pre_restore_backup": before,
        "restored_config_files": restored_config_files,
        "restored_channel_configs": restored_channel_configs,
    }


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--list", action="store_true")
    p.add_argument("--restore", default="")
    args = p.parse_args()
    if args.list:
        print(json.dumps(list_backups(), ensure_ascii=False, indent=2))
        return 0
    if args.restore:
        print(json.dumps(restore_backup(args.restore, actor="cli"), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(create_backup(actor="cli"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""チャンネル別の未投稿素材ストック日数を算出する."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_core import get_channels, parse_video_folder_name  # noqa: E402
from settings_service import config_get  # noqa: E402


def _warn_days() -> int:
    try:
        v = config_get("stock.warn_days").get("value")
        return max(0, int(v if v is not None else 7))
    except Exception:
        return 7


def _has_upload(folder: Path) -> bool:
    marker = folder / "youtube_upload.json"
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return bool(data.get("video_id") or data.get("uploaded_at") or data.get("url"))
    except Exception:
        return True


def _has_mp4(folder: Path) -> bool:
    try:
        candidates = list(folder.glob("*.mp4"))
        for sub in ("export", "exports", "output", "outputs", "render", "renders"):
            p = folder / sub
            if p.is_dir():
                candidates.extend(p.glob("*.mp4"))
        return any(p.is_file() for p in candidates)
    except Exception:
        return False


def _has_meta(folder: Path) -> bool:
    return all((folder / name).exists() for name in ("youtube_title.txt", "youtube_description.txt", "youtube_tags.txt"))


def _date_key(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        return datetime.max


def compute_stock(*, warn_days: int | None = None) -> dict[str, Any]:
    warn = _warn_days() if warn_days is None else int(warn_days)
    channels = []
    total_unposted = 0
    for ch in get_channels():
        folder = Path(ch.get("folder") or "")
        entries = []
        if folder.exists():
            try:
                dirs = [p for p in folder.iterdir() if p.is_dir() and parse_video_folder_name(p.name)]
            except Exception:
                dirs = []
            for d in dirs:
                info = parse_video_folder_name(d.name) or {}
                if _has_upload(d):
                    continue
                pub = info.get("publish_date") or ""
                complete = _has_mp4(d) or _has_meta(d)
                reserved = bool(pub)
                if not (reserved or complete):
                    continue
                item = {
                    "vol": info.get("num_text") or str(info.get("num") or ""),
                    "name": d.name,
                    "publish_date": pub,
                    "reserved": reserved,
                    "complete": complete,
                    "path": str(d),
                }
                entries.append(item)
        entries.sort(key=lambda x: (_date_key(x.get("publish_date") or ""), int(re.sub(r"\D", "", x.get("vol") or "0") or 0)))
        days = len(entries)
        total_unposted += days
        channels.append({
            "channel_id": ch.get("id", ""),
            "channel_name": ch.get("name", ""),
            "channel_folder": str(folder),
            "stock_days": days,
            "warn_days": warn,
            "warning": days < warn,
            "reserved_count": sum(1 for x in entries if x["reserved"]),
            "complete_unposted_count": sum(1 for x in entries if x["complete"]),
            "items": entries[:30],
        })
    return {
        "status": "ok",
        "warn_days": warn,
        "channel_count": len(channels),
        "total_unposted": total_unposted,
        "channels": channels,
        "warnings": [c for c in channels if c.get("warning")],
    }


if __name__ == "__main__":
    print(json.dumps(compute_stock(), ensure_ascii=False, indent=2))

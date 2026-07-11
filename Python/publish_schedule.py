#!/usr/bin/env python3
"""Shared YouTube publishAt resolution helpers."""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any, Optional

JST = _dt.timezone(_dt.timedelta(hours=9))
DEFAULT_PUBLISH_TIME_JST = "12:00"


def validate_publish_time_jst(value: Any) -> str:
    text = str(value or DEFAULT_PUBLISH_TIME_JST).strip()
    if not re.match(r"^\d{2}:\d{2}$", text):
        raise ValueError("publish_time_jst must be HH:MM")
    hh, mm = map(int, text.split(":"))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("publish_time_jst must be HH:MM")
    return f"{hh:02d}:{mm:02d}"


def parse_publish_date_from_folder(folder: Path) -> Optional[_dt.date]:
    m = re.match(r"^\d+_[^_]+_(\d{6})$", Path(folder).name)
    if not m:
        return None
    try:
        return _dt.datetime.strptime(m.group(1), "%y%m%d").date()
    except Exception:
        return None


def resolve_publish_at_iso(
    folder: Path,
    config: Optional[dict[str, Any]] = None,
    *,
    now: Optional[_dt.datetime] = None,
) -> Optional[str]:
    publish_date = parse_publish_date_from_folder(folder)
    if not publish_date:
        return None
    publish_time = validate_publish_time_jst((config or {}).get("publish_time_jst"))
    hh, mm = map(int, publish_time.split(":"))
    now_jst = (now or _dt.datetime.now(JST)).astimezone(JST)
    scheduled = _dt.datetime.combine(publish_date, _dt.time(hour=hh, minute=mm), tzinfo=JST)
    if scheduled <= now_jst and publish_date == now_jst.date():
        return (now_jst + _dt.timedelta(minutes=15)).isoformat()
    return scheduled.isoformat()

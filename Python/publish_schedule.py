#!/usr/bin/env python3
"""Shared YouTube publishAt resolution helpers."""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from typing import Any, Optional

from naming import folder_publish_date  # 命名規則の単一正本

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
    return folder_publish_date(Path(folder).name)


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


# ─── 再起動時の公開予約復旧 ───
# marker の scheduled_publish_at は世代差で naive（ローカル時刻）と aware（"…Z" / "+09:00"）が混在する。
# Python 3.11+ の fromisoformat は "Z" を aware として読むため、naive の now() と直接比較すると TypeError になる。
DEFAULT_RECOVER_MAX_STALE_DAYS = 7
MAX_RECOVER_STALE_DAYS = 3650  # timedelta のオーバーフロー防止と設定ミスの上限

# 段階 3: 起動時復旧専用のヘルパー（normalize_scheduled_local / scheduler_run_at / resolve_recover_max_stale_days /
# plan_scheduled_publish_recovery）は Mac の公開ゲート撤去に伴い削除した。予約の照合は VPS の publish_verify。

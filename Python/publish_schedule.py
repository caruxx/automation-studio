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


# ─── 再起動時の公開予約復旧 ───
# marker の scheduled_publish_at は世代差で naive（ローカル時刻）と aware（"…Z" / "+09:00"）が混在する。
# Python 3.11+ の fromisoformat は "Z" を aware として読むため、naive の now() と直接比較すると TypeError になる。
DEFAULT_RECOVER_MAX_STALE_DAYS = 7


def normalize_scheduled_local(value: Any) -> Optional[_dt.datetime]:
    """scheduled_publish_at をローカル時刻の naive datetime に正規化する。解釈できなければ None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def plan_scheduled_publish_recovery(
    value: Any,
    *,
    now: Optional[_dt.datetime] = None,
    max_stale_days: int = DEFAULT_RECOVER_MAX_STALE_DAYS,
) -> tuple[str, Optional[_dt.datetime]]:
    """復旧時の扱いを決める。

    戻り値 (kind, run_at):
      ("future", run_at)  予約時刻が未来 → その時刻で再登録
      ("overdue", run_at) 過去だが max_stale_days 以内 → now+1 分で即時投入
      ("stale", run_at)   max_stale_days より古い → 再登録せず警告のみ（一斉公開事故の防止）
      ("invalid", None)   解釈不能
    now は naive ローカル時刻（省略時は datetime.now()）。
    """
    run_at = normalize_scheduled_local(value)
    if run_at is None:
        return "invalid", None
    current = now if now is not None else _dt.datetime.now()
    if current.tzinfo is not None:
        current = current.astimezone().replace(tzinfo=None)
    if run_at >= current:
        return "future", run_at
    if current - run_at > _dt.timedelta(days=max(0, int(max_stale_days))):
        return "stale", run_at
    return "overdue", current + _dt.timedelta(minutes=1)

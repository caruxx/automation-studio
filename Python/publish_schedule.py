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


def normalize_scheduled_local(value: Any) -> Optional[_dt.datetime]:
    """scheduled_publish_at を JST の naive datetime に正規化する。解釈できなければ None。

    スケジューラは Asia/Tokyo 固定（app.py の AsyncIOScheduler）なので、OS の TZ に依存せず JST に揃える。
    naive の値は従来どおり JST のローカル時刻として扱う。"""
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
        try:
            parsed = parsed.astimezone(JST).replace(tzinfo=None)
        except (OverflowError, ValueError):
            return None
    return parsed


def scheduler_run_at(run_at: _dt.datetime) -> _dt.datetime:
    """復旧で求めた JST naive の run_at を、APScheduler の DateTrigger へ渡す aware 値にする。

    DateTrigger は naive を OS のローカル時間帯で解釈し、scheduler の Asia/Tokyo 設定を継承しない。
    JST 付きの aware にしておけば OS の TZ に関係なく意図した時刻に発火する。"""
    if run_at.tzinfo is None:
        return run_at.replace(tzinfo=JST)
    return run_at.astimezone(JST)


def resolve_recover_max_stale_days(raw: Any, default: int = DEFAULT_RECOVER_MAX_STALE_DAYS) -> tuple[int, Optional[str]]:
    """環境変数 APP_PUBLISH_RECOVER_MAX_STALE_DAYS を解釈する。

    戻り値 (days, warning): 空なら既定、負数は 0 に丸め、整数以外は既定に戻す。丸めた・戻した場合は warning に理由を返す。"""
    text = str(raw if raw is not None else "").strip()
    if not text:
        return default, None
    try:
        days = int(text)
    except ValueError:
        return default, f"APP_PUBLISH_RECOVER_MAX_STALE_DAYS={text!r} は整数ではないため既定値 {default} を使います"
    if days < 0:
        return 0, f"APP_PUBLISH_RECOVER_MAX_STALE_DAYS={days} は負数のため 0 として扱います"
    if days > MAX_RECOVER_STALE_DAYS:
        return MAX_RECOVER_STALE_DAYS, f"APP_PUBLISH_RECOVER_MAX_STALE_DAYS={days} は上限を超えるため {MAX_RECOVER_STALE_DAYS} として扱います"
    return days, None


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
    now は JST の naive 時刻（省略時は datetime.now(JST) を naive 化。aware を渡せば JST に変換する）。
    """
    run_at = normalize_scheduled_local(value)
    if run_at is None:
        return "invalid", None
    current = now if now is not None else _dt.datetime.now(JST)
    if current.tzinfo is not None:
        current = current.astimezone(JST).replace(tzinfo=None)
    if run_at >= current:
        return "future", run_at
    window_days = min(max(0, int(max_stale_days)), MAX_RECOVER_STALE_DAYS)
    if current - run_at > _dt.timedelta(days=window_days):
        return "stale", run_at
    return "overdue", current + _dt.timedelta(minutes=1)

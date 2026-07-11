#!/usr/bin/env python3
"""YouTube API quota metering and low-cost stats helpers."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

try:
    from _app_config import resolve_config_dir
    CONFIG_DIR = resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"

JST = dt.timezone(dt.timedelta(hours=9))
QUOTA_LOG = Path(os.environ.get("APP_YT_QUOTA_LOG") or (CONFIG_DIR / "youtube_api_quota.jsonl"))
DEFAULT_DAILY_QUOTA_CAP = int(os.environ.get("APP_YT_DAILY_QUOTA_CAP", "10000"))
BATCH_GET_STATS_DAILY_CAP = int(os.environ.get("APP_YT_BATCH_STATS_DAILY_QUOTA_CAP", "10000"))

QUOTA_COSTS: dict[str, int] = {
    "videos.insert": 100,
    "videos.list": 1,
    "videos.batchGetStats": 1,
    "videos.update": 50,
    "thumbnails.set": 50,
    "channels.list": 1,
    "playlistItems.list": 1,
    "commentThreads.list": 1,
    "search.list": 100,
    "liveBroadcasts.list": 1,
    "liveStreams.list": 1,
}

BATCH_STATS_METHOD = "videos.batchGetStats"
BATCH_STATS_POOL = "batchGetStats"


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _today_jst() -> str:
    return dt.datetime.now(JST).date().isoformat()


def cost_for(method: str, default: int = 1) -> int:
    return int(QUOTA_COSTS.get(method, default))


def record_quota(method: str, *, channel_id: str = "", cost: int | None = None,
                 when: str | None = None, pool: str | None = None,
                 feature: str | None = None,
                 detail: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "when": when or _now_utc().isoformat(timespec="seconds").replace("+00:00", "Z"),
        "method": method,
        "cost": int(cost if cost is not None else cost_for(method)),
        "channel_id": channel_id or os.environ.get("APP_CHANNEL_ID", "") or "",
        "feature": feature or os.environ.get("APP_YT_QUOTA_FEATURE", "") or "general",
    }
    if pool:
        row["pool"] = pool
    if detail:
        row["detail"] = detail
    try:
        QUOTA_LOG.parent.mkdir(parents=True, exist_ok=True)
        with QUOTA_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass
    return row


def execute_youtube(request, method: str, *, channel_id: str = "",
                    cost: int | None = None, pool: str | None = None,
                    feature: str | None = None,
                    detail: dict[str, Any] | None = None):
    resp = request.execute()
    record_quota(method, channel_id=channel_id, cost=cost, pool=pool, feature=feature, detail=detail)
    return resp


def _parse_when(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(s)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def iter_quota_events() -> list[dict[str, Any]]:
    if not QUOTA_LOG.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in QUOTA_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return rows
    return rows


def quota_summary(day: str | None = None) -> dict[str, Any]:
    day = day or _today_jst()
    methods: dict[str, dict[str, Any]] = {}
    features: dict[str, dict[str, Any]] = {}
    channels: dict[str, int] = {}
    total = 0
    batch_total = 0
    events = []
    for row in iter_quota_events():
        when = _parse_when(row.get("when"))
        if not when or when.astimezone(JST).date().isoformat() != day:
            continue
        cost = int(row.get("cost") or 0)
        method = str(row.get("method") or "unknown")
        pool = str(row.get("pool") or "")
        feature = str(row.get("feature") or "general")
        ch = str(row.get("channel_id") or "")
        if pool == BATCH_STATS_POOL or method == BATCH_STATS_METHOD:
            batch_total += cost
        else:
            total += cost
        ent = methods.setdefault(method, {"method": method, "cost": 0, "calls": 0})
        ent["cost"] += cost
        ent["calls"] += 1
        fent = features.setdefault(feature, {"feature": feature, "cost": 0, "calls": 0})
        fent["cost"] += cost
        fent["calls"] += 1
        if ch:
            channels[ch] = channels.get(ch, 0) + cost
        events.append(row)
    return {
        "status": "ok",
        "date": day,
        "log_path": str(QUOTA_LOG),
        "standard": {
            "used": total,
            "cap": DEFAULT_DAILY_QUOTA_CAP,
            "pct": round((total / DEFAULT_DAILY_QUOTA_CAP) * 100, 2) if DEFAULT_DAILY_QUOTA_CAP else 0,
        },
        "batchGetStats": {
            "used": batch_total,
            "cap": BATCH_GET_STATS_DAILY_CAP,
            "pct": round((batch_total / BATCH_GET_STATS_DAILY_CAP) * 100, 2) if BATCH_GET_STATS_DAILY_CAP else 0,
        },
        "methods": sorted(methods.values(), key=lambda x: (-int(x["cost"]), x["method"])),
        "features": sorted(features.values(), key=lambda x: (-int(x["cost"]), x["feature"])),
        "channels": channels,
        "events_today": len(events),
        "recent": events[-20:],
    }


def batch_get_video_stats(youtube, ids: list[str], *, channel_id: str = "",
                          part: str = "id,snippet,statistics,contentDetails",
                          feature: str | None = None) -> dict[str, Any]:
    clean_ids = [str(x).strip() for x in ids if str(x).strip()][:50]
    if not clean_ids:
        return {"items": [], "summary": {"requestedVideoCount": 0, "succeededVideoCount": 0, "failedVideoCount": 0}}
    req = youtube.videos().batchGetStats(part=part, id=",".join(clean_ids))
    return execute_youtube(
        req,
        BATCH_STATS_METHOD,
        channel_id=channel_id,
        pool=BATCH_STATS_POOL,
        feature=feature,
        detail={"ids": len(clean_ids)},
    )


def get_video_stats_with_fallback(youtube, ids: list[str], *, channel_id: str = "",
                                  part: str = "id,snippet,statistics,contentDetails",
                                  feature: str | None = None) -> tuple[dict[str, Any], str]:
    try:
        return batch_get_video_stats(youtube, ids, channel_id=channel_id, part=part, feature=feature), "batchGetStats"
    except Exception:
        clean_ids = [str(x).strip() for x in ids if str(x).strip()][:50]
        resp = execute_youtube(
            youtube.videos().list(part=part.replace("id,", ""), id=",".join(clean_ids)),
            "videos.list",
            channel_id=channel_id,
            feature=feature,
            detail={"fallback_from": BATCH_STATS_METHOD, "ids": len(clean_ids)},
        )
        return resp, "videos.list"

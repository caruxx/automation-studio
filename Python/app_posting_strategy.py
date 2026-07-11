#!/usr/bin/env python3
"""Phase J-4 posting strategy aggregation from cached data only."""
from __future__ import annotations

import datetime as _dt
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any

from app_core import get_channels
from app_learning import LEARNING_DIR
from publish_schedule import DEFAULT_PUBLISH_TIME_JST, validate_publish_time_jst
from settings_service import config_get

import app_benchmark_channels as _bench

JST = _dt.timezone(_dt.timedelta(hours=9))
MIN_RECOMMEND_SAMPLES = 3


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _parse_dt(value: Any) -> _dt.datetime | None:
    if not value:
        return None
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        return (d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)).astimezone(JST)
    except Exception:
        return None


def _coerce_int(v: Any) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return 0


def _median(values: list[float]) -> float:
    vals = [float(v) for v in values if float(v) > 0]
    return float(median(vals)) if vals else 0.0


def _mean(values: list[float]) -> float:
    vals = [float(v) for v in values if float(v) > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _channel_by_id(channel_id: str) -> dict[str, Any] | None:
    for ch in get_channels():
        if str(ch.get("id") or "") == channel_id:
            return ch
    return None


def _channel_config(ch: dict[str, Any] | None) -> dict[str, Any]:
    if not ch:
        return {}
    folder = Path(str(ch.get("folder") or "")).expanduser()
    return _read_json(folder / ".app_channel_config.json", {})


def _current_publish_time(channel_id: str) -> str:
    try:
        return validate_publish_time_jst(config_get("channel.publish_time_jst", channel_id=channel_id).get("value"))
    except Exception:
        return DEFAULT_PUBLISH_TIME_JST


def _current_weekly_count(channel_id: str) -> int | None:
    try:
        v = config_get("channel.weekly_publish_count", channel_id=channel_id).get("value")
        return int(v) if v not in (None, "") else None
    except Exception:
        return None


def _source_speed(row: dict[str, Any]) -> int:
    for key in ("speed", "views_48h", "first_v48", "v48", "views", "viewCount"):
        n = _coerce_int(row.get(key))
        if n > 0:
            return n
    return 0


def _own_samples(ch: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not ch:
        return []
    folder = Path(str(ch.get("folder") or "")).expanduser()
    if not folder.exists():
        return []
    samples: list[dict[str, Any]] = []
    learning = folder / LEARNING_DIR
    for row in _read_json(learning / "learned_patterns.json", []):
        if not isinstance(row, dict):
            continue
        speed = _source_speed(row)
        if speed <= 0:
            continue
        dt = _parse_dt(row.get("published_at") or row.get("publishedAt") or row.get("learned_at"))
        samples.append({
            "source": "own_48h_review",
            "channel_id": ch.get("id") or "",
            "channel_name": ch.get("name") or "",
            "published_at": dt.isoformat() if dt else "",
            "speed": speed,
            "title": row.get("title") or "",
        })
    upload_by_id: dict[str, dict[str, Any]] = {}
    for marker in folder.glob("*/youtube_upload.json"):
        data = _read_json(marker, {})
        vid = data.get("video_id") or data.get("id") or ""
        if not vid:
            continue
        upload_by_id[str(vid)] = data
    for path in sorted(learning.glob("*.json")):
        if path.name in {"learned_patterns.json", "monetization_snapshots.json"}:
            continue
        data = _read_json(path, {})
        rows = data if isinstance(data, list) else (data.get("reviews") if isinstance(data, dict) else [])
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            speed = _source_speed(row)
            if speed <= 0:
                continue
            vid = str(row.get("video_id") or "")
            up = upload_by_id.get(vid, {})
            dt = _parse_dt(row.get("published_at") or row.get("publishedAt") or up.get("published_at") or row.get("reviewed_at"))
            samples.append({
                "source": "own_48h_review",
                "channel_id": ch.get("id") or "",
                "channel_name": ch.get("name") or "",
                "published_at": dt.isoformat() if dt else "",
                "speed": speed,
                "title": row.get("title") or "",
            })
    return [s for s in samples if s.get("published_at")]


def _benchmark_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in (_bench._registry().get("channels") or []):  # cached local JSON only
        cid = c.get("channel_id") or c.get("channelId") or ""
        name = c.get("channel_name") or c.get("channelName") or ""
        for v in c.get("videos") or c.get("recentUploads") or []:
            dt = _parse_dt(v.get("publishedAt") or v.get("published_at") or v.get("published"))
            speed = _source_speed(v)
            if not dt or speed <= 0:
                continue
            rows.append({
                "source": "benchmark_cache",
                "channel_id": cid,
                "channel_name": name,
                "published_at": dt.isoformat(),
                "speed": speed,
                "title": v.get("title") or "",
            })
    return rows


def _matching_benchmark_samples(channel_id: str, ch: dict[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    all_rows = _benchmark_rows()
    if not all_rows:
        return [], "none"
    if any(r.get("channel_id") == channel_id for r in all_rows):
        return [r for r in all_rows if r.get("channel_id") == channel_id], "benchmark_channel"
    if ch is None:
        return [], "unknown_channel"
    cfg = _channel_config(ch)
    rivals = [str(x).lower() for x in (cfg.get("rival_channels") or []) if x]
    if rivals:
        matched = []
        for r in all_rows:
            hay = f"{r.get('channel_id','')} {r.get('channel_name','')}".lower()
            if any(x in hay or hay in x for x in rivals):
                matched.append(r)
        if matched:
            return matched, "channel_rivals"
    return all_rows, "genre_benchmark"


def _distribution_by_week(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_week: dict[tuple[int, int], int] = Counter()
    by_channel_week: dict[tuple[str, int, int], int] = Counter()
    for r in rows:
        dt = _parse_dt(r.get("published_at"))
        if not dt:
            continue
        iso = dt.date().isocalendar()
        by_week[(iso.year, iso.week)] += 1
        by_channel_week[(str(r.get("channel_id") or r.get("source") or "unknown"), iso.year, iso.week)] += 1
    vals = list(by_channel_week.values() or by_week.values())
    counts = Counter(vals)
    recommended = int(round(_median([float(v) for v in vals]))) if vals else 0
    return {
        "samples": len(vals),
        "distribution": {str(k): v for k, v in sorted(counts.items())},
        "median_per_week": recommended,
        "mean_per_week": round(_mean([float(v) for v in vals]), 2) if vals else 0,
    }


def _aggregate(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_weekday: dict[int, list[float]] = defaultdict(list)
    for r in samples:
        dt = _parse_dt(r.get("published_at"))
        speed = _source_speed(r)
        if not dt or speed <= 0:
            continue
        by_hour[dt.hour].append(float(speed))
        by_weekday[dt.weekday()].append(float(speed))
    hourly = {
        f"{h:02d}:00": {
            "samples": len(vals),
            "avg_speed": round(_mean(vals), 1),
            "median_speed": round(_median(vals), 1),
        }
        for h, vals in sorted(by_hour.items())
    }
    weekdays = {
        str(w): {
            "label": ["月", "火", "水", "木", "金", "土", "日"][w],
            "samples": len(vals),
            "avg_speed": round(_mean(vals), 1),
            "median_speed": round(_median(vals), 1),
        }
        for w, vals in sorted(by_weekday.items())
    }
    all_speeds = [_source_speed(r) for r in samples]
    return {
        "sample_count": len([v for v in all_speeds if v > 0]),
        "hourly_jst": hourly,
        "weekday_jst": weekdays,
        "weekly_post_count": _distribution_by_week(samples),
        "overall_median_speed": round(_median([float(v) for v in all_speeds]), 1),
        "overall_avg_speed": round(_mean([float(v) for v in all_speeds]), 1),
    }


def _best_hour(agg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    rows = agg.get("hourly_jst") or {}
    if not rows:
        return "", {}
    best = max(rows.items(), key=lambda kv: (kv[1].get("median_speed") or 0, kv[1].get("samples") or 0))
    return best[0], best[1]


def _reason(samples: list[dict[str, Any]], agg: dict[str, Any], source_scope: str) -> list[str]:
    best_label, best = _best_hour(agg)
    overall = float(agg.get("overall_median_speed") or 0)
    reasons = []
    bench_count = sum(1 for s in samples if s.get("source") == "benchmark_cache")
    own_count = sum(1 for s in samples if s.get("source") == "own_48h_review")
    if best_label:
        lift = ""
        if overall > 0 and best.get("median_speed"):
            pct = ((float(best["median_speed"]) / overall) - 1) * 100
            lift = f"、全体中央値比{pct:+.0f}%"
        source_text = "ベンチ" if bench_count >= own_count else "自チャンネル48hレビュー"
        reasons.append(f"{source_text}上位は{best_label} JST投稿の初速中央値が最も高い（{int(best.get('samples') or 0)}件{lift}）")
    freq = (agg.get("weekly_post_count") or {}).get("median_per_week") or 0
    if freq:
        reasons.append(f"週あたり投稿本数の中央値は{freq}本（蓄積済み投稿ペースから算出）")
    if source_scope == "genre_benchmark":
        reasons.append("チャンネル固有データが薄いため、登録済みベンチ全体をジャンル近似として使用")
    elif source_scope == "channel_rivals":
        reasons.append("チャンネル設定のベンチマーク先に一致する取込済み動画を使用")
    return reasons


def strategy(channel_id: str) -> dict[str, Any]:
    channel_id = (channel_id or "").strip()
    ch = _channel_by_id(channel_id)
    own = _own_samples(ch)
    bench, source_scope = _matching_benchmark_samples(channel_id, ch)
    samples = own + bench
    agg = _aggregate(samples)
    current_time = _current_publish_time(channel_id) if ch else DEFAULT_PUBLISH_TIME_JST
    current_weekly = _current_weekly_count(channel_id) if ch else None
    if agg["sample_count"] < MIN_RECOMMEND_SAMPLES:
        return {
            "status": "accumulating",
            "channel_id": channel_id,
            "channel_name": (ch or {}).get("name") or "",
            "message": "蓄積中",
            "sample_count": agg["sample_count"],
            "minimum_samples": MIN_RECOMMEND_SAMPLES,
            "current": {"publish_time_jst": current_time, "weekly_publish_count": current_weekly},
            "aggregation": agg,
            "quota_units_used": 0,
            "sources": {"own_48h_review": len(own), "benchmark_cache": len(bench), "scope": source_scope},
        }
    best_label, _ = _best_hour(agg)
    rec_time = best_label.replace(":00", ":00") if best_label else current_time
    rec_weekly = max(1, int((agg.get("weekly_post_count") or {}).get("median_per_week") or 1))
    hour_delta = abs(int((rec_time or "00:00")[:2]) - int((current_time or "00:00")[:2]))
    hour_delta = min(hour_delta, 24 - hour_delta)
    freq_delta = abs((current_weekly or rec_weekly) - rec_weekly)
    return {
        "status": "ok",
        "channel_id": channel_id,
        "channel_name": (ch or {}).get("name") or "",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "current": {"publish_time_jst": current_time, "weekly_publish_count": current_weekly},
        "recommendation": {
            "publish_time_jst": rec_time,
            "weekly_publish_count": rec_weekly,
            "confidence": "high" if agg["sample_count"] >= 20 else "medium",
            "large_gap": bool(hour_delta >= 3 or freq_delta >= 2),
        },
        "reasons": _reason(samples, agg, source_scope),
        "aggregation": agg,
        "sources": {"own_48h_review": len(own), "benchmark_cache": len(bench), "scope": source_scope},
        "quota_units_used": 0,
    }


def digest_lines(limit: int = 5) -> list[str]:
    today = _dt.datetime.now(JST).date()
    if today.weekday() != 0:
        return []
    out = []
    for ch in get_channels():
        cid = str(ch.get("id") or "")
        if not cid:
            continue
        st = strategy(cid)
        rec = st.get("recommendation") or {}
        if st.get("status") == "ok" and rec.get("large_gap"):
            out.append(f"{ch.get('name') or cid}: 投稿は {rec.get('publish_time_jst')} JST / 週{rec.get('weekly_publish_count')}本が候補（現設定との差あり）")
        if len(out) >= limit:
            break
    return out

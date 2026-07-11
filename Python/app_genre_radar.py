#!/usr/bin/env python3
"""J-1 genre radar snapshots and weekly discovery."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from _app_config import resolve_config_dir, resolve_shared_config_dir
    CONFIG_DIR = resolve_config_dir()
    SHARED_CONFIG_DIR = resolve_shared_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
    SHARED_CONFIG_DIR = CONFIG_DIR

from app_quota import record_quota
from app_retention import ensure_fetched_at

RADAR_DIR = SHARED_CONFIG_DIR / "genre_radar"
MOST_POPULAR_FILE = RADAR_DIR / "most_popular_snapshots.jsonl"
SEARCH_FILE = RADAR_DIR / "search_discovery.jsonl"
TOP_FILE = RADAR_DIR / "top5.json"
DEFAULT_REGIONS = ["JP", "US"]
DEFAULT_SEARCH_BUDGET_WEEK = 10
DEFAULT_KEYWORDS = [
    "bgm music",
    "work bgm",
    "study bgm",
    "relaxing bgm",
    "cafe music",
    "lofi bgm",
    "jazz bgm",
    "sleep music",
    "ambient music",
    "piano bgm",
]


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            continue
    return rows


def _dashboard_api_key() -> str:
    cfg = _read_json(CONFIG_DIR / "dashboard_config.json", {})
    key = (cfg.get("youtube_api_key") or "").strip() if isinstance(cfg, dict) else ""
    if key:
        return key
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"].strip()
    fp = CONFIG_DIR / "youtube_api_key.txt"
    return fp.read_text(encoding="utf-8").strip() if fp.exists() else ""


def _ops_config() -> dict[str, Any]:
    cfg = _read_json(SHARED_CONFIG_DIR / "ops_config.json", {})
    return cfg if isinstance(cfg, dict) else {}


def configured_regions() -> list[str]:
    raw = ((_ops_config().get("genre_radar") or {}).get("regions") or DEFAULT_REGIONS)
    if isinstance(raw, str):
        raw = [x.strip() for x in re.split(r"[,\s]+", raw) if x.strip()]
    regions = [str(x).strip().upper() for x in raw if str(x).strip()]
    return regions or list(DEFAULT_REGIONS)


def configured_search_budget_week() -> int:
    raw = ((_ops_config().get("research") or {}).get("search_budget_week"))
    try:
        return max(0, int(raw if raw is not None else DEFAULT_SEARCH_BUDGET_WEEK))
    except Exception:
        return DEFAULT_SEARCH_BUDGET_WEEK


def _oauth_service():
    import app_benchmark_channels as _bench
    return _bench._oauth_service()  # noqa: SLF001 - same local auth path as benchmark reads.


def _execute_oauth(endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    youtube = _oauth_service()
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if endpoint == "videos":
        return youtube.videos().list(**clean).execute()
    if endpoint == "search":
        return youtube.search().list(**clean).execute()
    if endpoint == "channels":
        return youtube.channels().list(**clean).execute()
    raise RuntimeError(f"unsupported OAuth endpoint: {endpoint}")


def _yt_get(endpoint: str, params: dict[str, Any], method: str, *, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    key = _dashboard_api_key()
    auth = "api_key" if key else "oauth"
    try:
        if key:
            q = dict(params)
            q["key"] = key
            url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(q)}"
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        else:
            data = _execute_oauth(endpoint, params)
        record_quota(method, feature="genre_radar", detail={**(detail or {}), "endpoint": endpoint, "auth": auth})
        return data
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} failed ({auth}) HTTP {e.code}: {body[:800]}") from e
    except Exception as e:
        raise RuntimeError(f"{method} failed ({auth}): {e}") from e


def _coerce_int(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return 0


def _thumb(snippet: dict[str, Any]) -> str:
    thumbs = snippet.get("thumbnails") or {}
    for key in ("maxres", "standard", "high", "medium", "default"):
        url = (thumbs.get(key) or {}).get("url")
        if url:
            return url
    return ""


def _parse_time(value: str) -> _dt.datetime | None:
    if not value:
        return None
    try:
        s = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = _dt.datetime.fromisoformat(s)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None


def run_most_popular_snapshot(regions: list[str] | None = None, *, max_results: int = 25) -> dict[str, Any]:
    fetched_at = _now_iso()
    regions = [str(x).strip().upper() for x in (regions or configured_regions()) if str(x).strip()]
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for region in regions:
        try:
            data = _yt_get(
                "videos",
                {
                    "part": "snippet,statistics,contentDetails",
                    "chart": "mostPopular",
                    "regionCode": region,
                    "videoCategoryId": "10",
                    "maxResults": max(1, min(50, int(max_results or 25))),
                },
                "videos.list",
                detail={"chart": "mostPopular", "regionCode": region, "videoCategoryId": "10"},
            )
            items = []
            for item in data.get("items") or []:
                sn = item.get("snippet") or {}
                st = item.get("statistics") or {}
                items.append(ensure_fetched_at({
                    "video_id": item.get("id") or "",
                    "title": sn.get("title") or "",
                    "channel_id": sn.get("channelId") or "",
                    "channel_title": sn.get("channelTitle") or "",
                    "published_at": sn.get("publishedAt") or "",
                    "view_count": _coerce_int(st.get("viewCount")),
                    "like_count": _coerce_int(st.get("likeCount")),
                    "comment_count": _coerce_int(st.get("commentCount")),
                    "thumbnail": _thumb(sn),
                    "fetched_at": fetched_at,
                }, fetched_at))
            row = ensure_fetched_at({
                "type": "mostPopular",
                "feature": "genre_radar",
                "region": region,
                "video_category_id": "10",
                "fetched_at": fetched_at,
                "items": items,
            }, fetched_at)
            _append_jsonl(MOST_POPULAR_FILE, row)
            rows.append(row)
        except Exception as e:
            errors.append({"region": region, "error": str(e)})
    return {"status": "ok" if rows else "error", "fetched_at": fetched_at, "regions": regions, "snapshots": rows, "errors": errors, "path": str(MOST_POPULAR_FILE)}


def _ttp_keywords(limit: int = 10) -> list[str]:
    try:
        import app_benchmark_channels as _bench
        data = _bench.competitor_data_from_cache()
        counter: Counter[str] = Counter()
        for ch in data.get("channels") or []:
            for v in (ch.get("videos") or ch.get("topByViews") or ch.get("recentUploads") or []):
                for tag in v.get("tags") or []:
                    text = str(tag).strip().lower()
                    if 2 <= len(text) <= 40 and not re.search(r"https?://", text):
                        counter[text] += 1
        return [k for k, _ in counter.most_common(limit)]
    except Exception:
        return []


def search_keywords(max_calls: int | None = None) -> list[str]:
    merged = []
    seen: set[str] = set()
    for kw in [*DEFAULT_KEYWORDS, *_ttp_keywords()]:
        text = str(kw).strip()
        low = text.lower()
        if text and low not in seen:
            merged.append(text)
            seen.add(low)
    cap = configured_search_budget_week() if max_calls is None else min(configured_search_budget_week(), max(0, int(max_calls)))
    return merged[:cap]


def _search_channels(keyword: str, *, max_results: int = 10) -> list[dict[str, Any]]:
    data = _yt_get(
        "search",
        {
            "part": "snippet",
            "q": keyword,
            "type": "video",
            "videoCategoryId": "10",
            "order": "date",
            "maxResults": max(1, min(25, int(max_results or 10))),
            "publishedAfter": (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=30)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
        "search.list",
        detail={"keyword": keyword},
    )
    out = []
    for item in data.get("items") or []:
        sn = item.get("snippet") or {}
        cid = sn.get("channelId") or ""
        vid = ((item.get("id") or {}).get("videoId") or "")
        if cid:
            out.append({"keyword": keyword, "channel_id": cid, "video_id": vid, "title": sn.get("title") or "", "channel_title": sn.get("channelTitle") or "", "published_at": sn.get("publishedAt") or ""})
    return out


def _channel_stats(channel_ids: list[str]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    ids = [x for x in dict.fromkeys(channel_ids) if x]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        if not chunk:
            continue
        data = _yt_get("channels", {"part": "snippet,statistics", "id": ",".join(chunk), "maxResults": 50}, "channels.list", detail={"ids": len(chunk)})
        for item in data.get("items") or []:
            sn = item.get("snippet") or {}
            st = item.get("statistics") or {}
            cid = item.get("id") or ""
            stats[cid] = {
                "channel_id": cid,
                "channel_name": sn.get("title") or "",
                "url": f"https://www.youtube.com/channel/{cid}",
                "thumbnail": _thumb(sn),
                "subscribers": _coerce_int(st.get("subscriberCount")),
                "total_views": _coerce_int(st.get("viewCount")),
                "video_count": _coerce_int(st.get("videoCount")),
                "hidden_subscriber_count": bool(st.get("hiddenSubscriberCount")),
            }
    return stats


def _video_stats(video_ids: list[str]) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    ids = [x for x in dict.fromkeys(video_ids) if x]
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = _yt_get("videos", {"part": "snippet,statistics", "id": ",".join(chunk)}, "videos.list", detail={"ids": len(chunk), "source": "search_candidates"})
        record_quota("videos.batchGetStats", pool="batchGetStats", feature="genre_radar", detail={"ids": len(chunk), "logical": True})
        for item in data.get("items") or []:
            sn = item.get("snippet") or {}
            st = item.get("statistics") or {}
            stats[item.get("id") or ""] = {
                "view_count": _coerce_int(st.get("viewCount")),
                "like_count": _coerce_int(st.get("likeCount")),
                "comment_count": _coerce_int(st.get("commentCount")),
                "published_at": sn.get("publishedAt") or "",
            }
    return stats


def run_weekly_discovery(*, max_search_calls: int | None = None, max_results_per_search: int = 10) -> dict[str, Any]:
    fetched_at = _now_iso()
    keywords = search_keywords(max_search_calls)
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for keyword in keywords:
        try:
            candidates.extend(_search_channels(keyword, max_results=max_results_per_search))
        except Exception as e:
            errors.append({"keyword": keyword, "error": str(e)})
    ch_stats = _channel_stats([c["channel_id"] for c in candidates]) if candidates else {}
    vid_stats = _video_stats([c["video_id"] for c in candidates]) if candidates else {}
    rows = []
    for c in candidates:
        row = ensure_fetched_at({**c, **(ch_stats.get(c["channel_id"]) or {})}, fetched_at)
        row["video_stats"] = ensure_fetched_at(vid_stats.get(c.get("video_id") or "") or {}, fetched_at)
        rows.append(row)
    payload = ensure_fetched_at({
        "type": "weekly_search",
        "feature": "genre_radar",
        "fetched_at": fetched_at,
        "search_budget_configured": configured_search_budget_week(),
        "search_calls_used": len(keywords),
        "keywords": keywords,
        "candidates": rows,
        "errors": errors,
    }, fetched_at)
    _append_jsonl(SEARCH_FILE, payload)
    top = build_top5()
    return {"status": "ok", "fetched_at": fetched_at, "search_calls_used": len(keywords), "keywords": keywords, "candidate_count": len(rows), "errors": errors, "top": top, "path": str(SEARCH_FILE)}


def _candidate_history() -> dict[str, list[dict[str, Any]]]:
    hist: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in _iter_jsonl(SEARCH_FILE):
        for c in run.get("candidates") or []:
            cid = c.get("channel_id") or ""
            if cid:
                hist[cid].append(c)
    for rows in hist.values():
        rows.sort(key=lambda x: x.get("fetched_at") or "")
    return hist


def _growth_for(rows: list[dict[str, Any]]) -> tuple[float | None, list[int]]:
    series = [_coerce_int(r.get("subscribers")) for r in rows if _coerce_int(r.get("subscribers")) > 0]
    if len(series) >= 2 and series[0] > 0:
        return round(((series[-1] - series[0]) / series[0]) * 100, 2), series
    return None, series


def _velocity_score(c: dict[str, Any]) -> float:
    stats = c.get("video_stats") or {}
    views = _coerce_int(stats.get("view_count"))
    published = _parse_time(stats.get("published_at") or c.get("published_at") or "")
    if not published:
        return float(views)
    age_h = max(1.0, (_dt.datetime.now(_dt.timezone.utc) - published).total_seconds() / 3600)
    return round(views / age_h, 2)


def build_top5() -> dict[str, Any]:
    hist = _candidate_history()
    latest_by_genre: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cid, rows in hist.items():
        latest = rows[-1]
        growth, spark = _growth_for(rows)
        latest = dict(latest)
        latest["growth_rate_pct"] = growth
        latest["sparkline"] = spark
        latest["velocity_score"] = _velocity_score(latest)
        latest_by_genre[latest.get("keyword") or "BGM"].append(latest)

    top_rows = []
    for genre, rows in latest_by_genre.items():
        rows.sort(key=lambda r: (r.get("growth_rate_pct") if r.get("growth_rate_pct") is not None else -1, r.get("velocity_score") or 0), reverse=True)
        rep = rows[0]
        score = rep.get("growth_rate_pct")
        top_rows.append({
            "genre": genre,
            "growth_rate_pct": score,
            "representative_channel": {
                "channel_id": rep.get("channel_id") or "",
                "name": rep.get("channel_name") or rep.get("channel_title") or "",
                "url": rep.get("url") or (f"https://www.youtube.com/channel/{rep.get('channel_id')}" if rep.get("channel_id") else ""),
                "thumbnail": rep.get("thumbnail") or "",
                "subscribers": rep.get("subscribers") or 0,
            },
            "sparkline": rep.get("sparkline") or [],
            "velocity_score": rep.get("velocity_score") or 0,
            "sample_video": {"video_id": rep.get("video_id") or "", "title": rep.get("title") or "", "view_count": (rep.get("video_stats") or {}).get("view_count") or 0},
            "fetched_at": rep.get("fetched_at") or "",
        })
    top_rows.sort(key=lambda r: (r.get("growth_rate_pct") if r.get("growth_rate_pct") is not None else -1, r.get("velocity_score") or 0), reverse=True)
    status = "ok" if top_rows else "insufficient_data"
    payload = ensure_fetched_at({
        "status": status,
        "generated_at": _now_iso(),
        "items": top_rows[:5],
        "message": "" if top_rows else "ジャンルレーダーの週次searchデータがまだありません。",
    })
    _write_json(TOP_FILE, payload)
    return payload


def top5() -> dict[str, Any]:
    data = _read_json(TOP_FILE, None)
    if isinstance(data, dict):
        return data
    return build_top5()


def digest_lines(limit: int = 5) -> list[str]:
    data = top5()
    rows = data.get("items") or []
    if not rows:
        return ["データ蓄積中"]
    out = []
    for r in rows[:limit]:
        ch = r.get("representative_channel") or {}
        growth = r.get("growth_rate_pct")
        growth_text = f"{growth:+.1f}%" if isinstance(growth, (int, float)) else "初回計測"
        out.append(f"{r.get('genre')}: {growth_text} / {ch.get('name') or '-'}")
    return out

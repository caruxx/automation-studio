#!/usr/bin/env python3
"""Benchmark channel registry backed by YouTube Data API.

J-0 replaces spreadsheet benchmark intake with explicit channel URL
registration.  This module owns the registry/cache and exports the legacy
``competitor_data`` shape so existing concept/title/thumbnail analysis keeps
working.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
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

from app_quota import execute_youtube, record_quota
from app_retention import ensure_fetched_at

BENCHMARK_DIR = SHARED_CONFIG_DIR / "benchmark"
CHANNELS_FILE = BENCHMARK_DIR / "channel_cache.json"
MIGRATION_FILE = BENCHMARK_DIR / "channel_migration.json"
BENCHMARK_PROFILES_FILE = SHARED_CONFIG_DIR / "benchmark_profiles.json"
COMPETITOR_CACHE_FILE = SHARED_CONFIG_DIR / "competitor_analysis_cache.json"
DEFAULT_FETCH_LIMIT = 30


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


def _api_key() -> str:
    cfg = _read_json(CONFIG_DIR / "dashboard_config.json", {})
    key = (cfg.get("youtube_api_key") or "").strip() if isinstance(cfg, dict) else ""
    if key:
        return key
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"].strip()
    fp = CONFIG_DIR / "youtube_api_key.txt"
    return fp.read_text(encoding="utf-8").strip() if fp.exists() else ""


def _yt_get(endpoint: str, params: dict[str, Any], method: str, *, channel_id: str = "") -> dict[str, Any]:
    key = _api_key()
    if key:
        q = dict(params)
        q["key"] = key
        url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(q)}"
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        record_quota(method, channel_id=channel_id, feature="benchmark", detail={"endpoint": endpoint})
        return data
    return _yt_get_oauth(endpoint, params, method, channel_id=channel_id)


def _oauth_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token_file = CONFIG_DIR / "youtube_token.json"
    if not token_file.exists():
        try:
            dashboard = _read_json(CONFIG_DIR / "dashboard_config.json", {})
            folder = Path(str((dashboard or {}).get("channel_folder") or "")).expanduser()
            candidate = folder / ".youtube_token.json"
            if candidate.exists():
                token_file = candidate
        except Exception:
            pass
    if not token_file.exists():
        raise RuntimeError("YouTube API key も OAuth token も未設定です")
    creds = Credentials.from_authorized_user_file(str(token_file))
    if creds and not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json(), encoding="utf-8")
    if not creds or not creds.valid:
        raise RuntimeError(f"OAuth token が無効です: {token_file}")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _yt_get_oauth(endpoint: str, params: dict[str, Any], method: str, *, channel_id: str = "") -> dict[str, Any]:
    youtube = _oauth_service()
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    if endpoint == "channels":
        req = youtube.channels().list(**clean)
    elif endpoint == "playlistItems":
        req = youtube.playlistItems().list(**clean)
    elif endpoint == "videos":
        req = youtube.videos().list(**clean)
    else:
        raise RuntimeError(f"未対応 YouTube endpoint: {endpoint}")
    return execute_youtube(req, method, channel_id=channel_id, feature="benchmark", detail={"endpoint": endpoint, "auth": "oauth"})


def _coerce_int(v: Any) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return 0


def _thumb(snip: dict[str, Any]) -> str:
    thumbs = snip.get("thumbnails") or {}
    for key in ("maxres", "standard", "high", "medium", "default"):
        url = (thumbs.get(key) or {}).get("url")
        if url:
            return url
    return ""


def _scrape_channel_id(url: str) -> str:
    if not url or "youtube.com" not in url:
        return ""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            html = r.read(1_500_000).decode("utf-8", errors="replace")
    except Exception:
        return ""
    for pat in (r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]+)"', r'<meta itemprop="channelId" content="(UC[A-Za-z0-9_-]+)"'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return ""


def resolve_channel_id(channel_url: str) -> dict[str, Any]:
    raw = (channel_url or "").strip()
    if not raw:
        raise RuntimeError("channel_url が空です")
    if re.fullmatch(r"UC[A-Za-z0-9_-]+", raw):
        cid = raw
    else:
        m = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", raw)
        cid = m.group(1) if m else ""
    handle = ""
    if not cid:
        m = re.search(r"(?:youtube\.com/|^)/?@([A-Za-z0-9_.-]+)", raw)
        if m:
            handle = "@" + m.group(1)
            data = _yt_get("channels", {"part": "id", "forHandle": handle}, "channels.list")
            items = data.get("items") or []
            cid = (items[0] or {}).get("id") if items else ""
    if not cid:
        m = re.search(r"/user/([A-Za-z0-9_.-]+)", raw)
        if m:
            data = _yt_get("channels", {"part": "id", "forUsername": m.group(1)}, "channels.list")
            items = data.get("items") or []
            cid = (items[0] or {}).get("id") if items else ""
    if not cid:
        cid = _scrape_channel_id(raw)
    if not cid:
        raise RuntimeError(f"channelId を解決できません（search.list は使いません）: {raw}")
    return {"channel_id": cid, "url": raw, "handle": handle}


def fetch_channel(channel_url: str, *, limit: int = DEFAULT_FETCH_LIMIT, force: bool = False) -> dict[str, Any]:
    resolved = resolve_channel_id(channel_url)
    cid = resolved["channel_id"]
    existing = get_channel(cid)
    if existing and not force:
        fetched = _parse_dt(existing.get("fetched_at"))
        if fetched and (_dt.datetime.now(_dt.timezone.utc) - fetched).days < 30:
            return existing

    ch_resp = _yt_get(
        "channels",
        {"part": "snippet,statistics,contentDetails", "id": cid, "maxResults": 1},
        "channels.list",
        channel_id=cid,
    )
    item = (ch_resp.get("items") or [None])[0]
    if not item:
        raise RuntimeError(f"チャンネル情報を取得できません: {cid}")
    sn = item.get("snippet") or {}
    st = item.get("statistics") or {}
    uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or ""

    ids: list[str] = []
    page = ""
    while uploads and len(ids) < limit:
        params = {"part": "contentDetails", "playlistId": uploads, "maxResults": min(50, limit - len(ids))}
        if page:
            params["pageToken"] = page
        pl = _yt_get("playlistItems", params, "playlistItems.list", channel_id=cid)
        for row in pl.get("items") or []:
            vid = ((row.get("contentDetails") or {}).get("videoId") or "").strip()
            if vid:
                ids.append(vid)
        page = pl.get("nextPageToken") or ""
        if not page:
            break

    videos: list[dict[str, Any]] = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        if not chunk:
            continue
        v_resp = _yt_get(
            "videos",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)},
            "videos.list",
            channel_id=cid,
        )
        record_quota("videos.batchGetStats", channel_id=cid, pool="batchGetStats", feature="benchmark", detail={"ids": len(chunk), "logical": True})
        for v in v_resp.get("items") or []:
            vsn = v.get("snippet") or {}
            vst = v.get("statistics") or {}
            vid = v.get("id") or ""
            videos.append(ensure_fetched_at({
                "videoId": vid,
                "video_id": vid,
                "title": vsn.get("title") or "",
                "description": (vsn.get("description") or "")[:2000],
                "tags": (vsn.get("tags") or [])[:30],
                "publishedAt": vsn.get("publishedAt") or vsn.get("publishTime") or "",
                "published_at": vsn.get("publishedAt") or vsn.get("publishTime") or "",
                "viewCount": _coerce_int(vst.get("viewCount")),
                "views": _coerce_int(vst.get("viewCount")),
                "likeCount": _coerce_int(vst.get("likeCount")),
                "commentCount": _coerce_int(vst.get("commentCount")),
                "duration": (v.get("contentDetails") or {}).get("duration") or "",
                "channelTitle": vsn.get("channelTitle") or sn.get("title") or "",
                "thumbnail": _thumb(vsn),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "stats_source": "videos.list+batchGetStats_logical",
            }))

    videos.sort(key=lambda x: x.get("publishedAt") or "", reverse=True)
    top = sorted(videos, key=lambda x: int(x.get("viewCount") or 0), reverse=True)[:10]
    recent = videos[:10]
    fetched_at = _now_iso()
    payload = ensure_fetched_at({
        "channel_id": cid,
        "channelId": cid,
        "url": f"https://www.youtube.com/channel/{cid}",
        "source_url": channel_url,
        "handle": resolved.get("handle") or sn.get("customUrl") or "",
        "channel_name": sn.get("title") or "",
        "channelName": sn.get("title") or "",
        "description": sn.get("description") or "",
        "thumbnail": _thumb(sn),
        "icon_url": _thumb(sn),
        "subscribers": _coerce_int(st.get("subscriberCount")),
        "total_views": _coerce_int(st.get("viewCount")),
        "video_count": _coerce_int(st.get("videoCount")),
        "hidden_subscriber_count": bool(st.get("hiddenSubscriberCount")),
        "uploads_playlist_id": uploads,
        "fetched_at": fetched_at,
        "videos": videos,
        "top_videos": _profile_videos(top),
        "recent_videos": _profile_videos(recent),
        "topByViews": top,
        "recentUploads": recent,
        "kpi": _kpi(videos, _coerce_int(st.get("subscriberCount"))),
    })
    save_channel(payload)
    return payload


def _parse_dt(value: Any) -> _dt.datetime | None:
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None


def _profile_videos(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "video_id": v.get("videoId") or v.get("video_id") or "",
        "title": v.get("title") or "",
        "description": v.get("description") or "",
        "published": v.get("publishedAt") or v.get("published_at") or "",
        "published_at": v.get("publishedAt") or v.get("published_at") or "",
        "thumbnail": v.get("thumbnail") or "",
        "views": int(v.get("viewCount") or v.get("views") or 0),
        "likes": int(v.get("likeCount") or 0),
        "comments_count": int(v.get("commentCount") or 0),
        "duration": v.get("duration") or "",
        "url": v.get("url") or "",
        "tags": v.get("tags") or [],
    } for v in videos]


def _kpi(videos: list[dict[str, Any]], subscribers: int) -> dict[str, Any]:
    if not videos:
        return {"recent_avg_views": 0, "top_avg_views": 0, "post_frequency_per_week": 0, "weekday_top": None, "subscribers": subscribers}
    recent = videos[:10]
    top = sorted(videos, key=lambda x: int(x.get("viewCount") or 0), reverse=True)[:10]
    dates = [_parse_dt(v.get("publishedAt")) for v in videos]
    dates = [d for d in dates if d]
    freq = 0.0
    weekday_top = None
    if len(dates) >= 2:
        span = max(1, (max(dates) - min(dates)).days)
        freq = round((len(dates) / span) * 7, 2)
        counts: dict[int, int] = {}
        for d in dates:
            counts[d.astimezone(_dt.timezone(_dt.timedelta(hours=9))).weekday()] = counts.get(d.weekday(), 0) + 1
        weekday_top = max(counts.items(), key=lambda x: x[1])[0] if counts else None
    return {
        "subscribers": subscribers,
        "recent_avg_views": round(sum(int(v.get("viewCount") or 0) for v in recent) / max(1, len(recent))),
        "top_avg_views": round(sum(int(v.get("viewCount") or 0) for v in top) / max(1, len(top))),
        "post_frequency_per_week": freq,
        "weekday_top": weekday_top,
        "video_count_cached": len(videos),
    }


def _registry() -> dict[str, Any]:
    data = _read_json(CHANNELS_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("channels", [])
    return data


def save_registry(data: dict[str, Any]) -> None:
    data["updated_at"] = _now_iso()
    _write_json(CHANNELS_FILE, data)


def save_channel(channel: dict[str, Any]) -> None:
    data = _registry()
    cid = channel.get("channel_id") or channel.get("channelId")
    rows = [c for c in data.get("channels", []) if (c.get("channel_id") or c.get("channelId")) != cid]
    rows.append(channel)
    rows.sort(key=lambda c: (c.get("channel_name") or c.get("channelName") or "").lower())
    data["channels"] = rows
    save_registry(data)


def delete_channel(channel_id: str) -> dict[str, Any]:
    data = _registry()
    target = (channel_id or "").strip()
    before = data.get("channels", []) or []
    kept = [
        c for c in before
        if (c.get("channel_id") or c.get("channelId") or "") != target
    ]
    if len(kept) == len(before):
        return {"status": "noop", "removed": 0, "remaining": len(kept)}
    data["channels"] = kept
    save_registry(data)
    save_competitor_cache()
    return {"status": "ok", "removed": len(before) - len(kept), "remaining": len(kept)}


def get_channel(channel_id: str) -> dict[str, Any] | None:
    for c in _registry().get("channels") or []:
        if (c.get("channel_id") or c.get("channelId")) == channel_id:
            return c
    return None


def list_channels() -> dict[str, Any]:
    rows = []
    for c in _registry().get("channels") or []:
        rows.append({
            "channel_id": c.get("channel_id") or c.get("channelId") or "",
            "name": c.get("channel_name") or c.get("channelName") or "",
            "url": c.get("url") or "",
            "source_url": c.get("source_url") or "",
            "icon_url": c.get("icon_url") or c.get("thumbnail") or "",
            "subscribers": c.get("subscribers", 0),
            "total_views": c.get("total_views", 0),
            "video_count": c.get("video_count", 0),
            "fetched_at": c.get("fetched_at", ""),
            "kpi": c.get("kpi") or {},
        })
    return {"status": "ok", "count": len(rows), "channels": rows, "cache_path": str(CHANNELS_FILE)}


def competitor_data_from_cache(channel_ids: list[str] | None = None) -> dict[str, Any]:
    wanted = set(channel_ids or [])
    chans = []
    for c in _registry().get("channels") or []:
        cid = c.get("channel_id") or c.get("channelId") or ""
        if wanted and cid not in wanted and (c.get("channel_name") or "") not in wanted:
            continue
        top = c.get("topByViews") or sorted(c.get("videos") or [], key=lambda x: int(x.get("viewCount") or 0), reverse=True)[:10]
        recent = c.get("recentUploads") or sorted(c.get("videos") or [], key=lambda x: x.get("publishedAt") or "", reverse=True)[:10]
        chans.append({
            "url": c.get("url") or "",
            "channelId": cid,
            "channelName": c.get("channel_name") or c.get("channelName") or "",
            "totalVideos": len(c.get("videos") or []),
            "topByViews": top,
            "recentUploads": recent,
            "fetched_at": c.get("fetched_at") or "",
            "icon_url": c.get("icon_url") or c.get("thumbnail") or "",
            "subscribers": c.get("subscribers", 0),
            "kpi": c.get("kpi") or {},
        })
    return {"channels": chans, "source": "benchmark_channel_cache", "generated_at": _now_iso()}


def save_competitor_cache(analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = ensure_fetched_at({
        "competitor_data": competitor_data_from_cache(),
        "analysis": analysis or {},
        "source": "benchmark_channel_cache",
        "analyzed_at": _now_iso(),
        "language": "ja",
        "prompt_version": 6,
    })
    try:
        from app_channel_cache import save_scoped_cache
        return save_scoped_cache("competitor_analysis_cache.json", COMPETITOR_CACHE_FILE, payload)
    except Exception:
        _write_json(COMPETITOR_CACHE_FILE, payload)
        return payload


def migrate_existing() -> dict[str, Any]:
    data = _registry()
    known = {(c.get("channel_id") or c.get("channelId")) for c in data.get("channels") or []}
    migrated = 0
    needs: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    prof = _read_json(BENCHMARK_PROFILES_FILE, {})
    profile_rows = (prof.get("profiles") or []) if isinstance(prof, dict) else []
    for p in profile_rows:
        cid = p.get("channel_id") or p.get("channelId") or ""
        url = p.get("url") or ""
        m = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", url)
        if not cid and m:
            cid = m.group(1)
        if cid:
            candidates.append((cid, p))
        else:
            needs.append({"channel_name": p.get("channel_name") or "", "url": url, "reason": "channel_id_unknown"})
    for cid, p in candidates:
        if cid in known:
            continue
        row = ensure_fetched_at({
            "channel_id": cid,
            "channelId": cid,
            "url": f"https://www.youtube.com/channel/{cid}",
            "source_url": p.get("url") or f"https://www.youtube.com/channel/{cid}",
            "channel_name": p.get("channel_name") or "",
            "channelName": p.get("channel_name") or "",
            "thumbnail": p.get("thumbnail") or "",
            "icon_url": p.get("thumbnail") or "",
            "subscribers": p.get("subscribers") or 0,
            "total_views": p.get("total_views") or 0,
            "top_videos": p.get("top_videos") or [],
            "recent_videos": p.get("recent_videos") or [],
            "videos": [],
            "topByViews": [],
            "recentUploads": [],
            "fetched_at": _now_iso(),
            "migrated_from": "benchmark_profiles",
            "needs_refresh": True,
        })
        data.setdefault("channels", []).append(row)
        known.add(cid)
        migrated += 1
    save_registry(data)
    result = {"status": "ok", "migrated": migrated, "needs_reregister": needs, "needs_reregister_count": len(needs), "cache_path": str(CHANNELS_FILE)}
    _write_json(MIGRATION_FILE, result)
    return result

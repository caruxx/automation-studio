#!/usr/bin/env python3
"""Demand-gap analysis from benchmark video comments."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from _app_config import resolve_config_dir, resolve_shared_config_dir
    CONFIG_DIR = resolve_config_dir()
    SHARED_CONFIG_DIR = resolve_shared_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
    SHARED_CONFIG_DIR = CONFIG_DIR

import app_benchmark_channels as _bench
from app_benchmark_common import extract_json_object
from app_quota import execute_youtube, record_quota
from app_retention import ensure_fetched_at

COMMENT_MINING_DIR = SHARED_CONFIG_DIR / "comment_mining"
COMMENT_MINING_INDEX = COMMENT_MINING_DIR / "demand_memos.json"
RAW_PREFIX = "comment_mining_raw_"
DEFAULT_TOP_N = 10
DEFAULT_MAX_COMMENTS = 100


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


def _safe_key(value: str) -> str:
    key = re.sub(r"[^A-Za-z0-9_-]+", "_", value or "").strip("_")
    return key[:120] or "unknown_channel"


def _api_key() -> str:
    cfg = _read_json(CONFIG_DIR / "dashboard_config.json", {})
    key = (cfg.get("youtube_api_key") or "").strip() if isinstance(cfg, dict) else ""
    if key:
        return key
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"].strip()
    fp = CONFIG_DIR / "youtube_api_key.txt"
    return fp.read_text(encoding="utf-8").strip() if fp.exists() else ""


def _oauth_service():
    return _bench._oauth_service()  # noqa: SLF001 - benchmark auth is the canonical local path.


def _yt_comment_threads(video_id: str, *, channel_id: str, max_comments: int) -> dict[str, Any]:
    params = {
        "part": "snippet",
        "videoId": video_id,
        "maxResults": min(100, max(1, int(max_comments))),
        "order": "relevance",
        "textFormat": "plainText",
    }
    key = _api_key()
    if key:
        q = dict(params)
        q["key"] = key
        url = f"https://www.googleapis.com/youtube/v3/commentThreads?{urllib.parse.urlencode(q)}"
        record_quota(
            "commentThreads.list",
            channel_id=channel_id,
            feature="comment_mining",
            detail={"endpoint": "commentThreads", "video_id": video_id, "auth": "api_key"},
        )
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data
    youtube = _oauth_service()
    req = youtube.commentThreads().list(**params)
    record_quota(
        "commentThreads.list",
        channel_id=channel_id,
        feature="comment_mining",
        detail={"endpoint": "commentThreads", "video_id": video_id, "auth": "oauth"},
    )
    try:
        return req.execute()
    except Exception:
        raise


def _top_videos(channel: dict[str, Any], top_n: int) -> list[dict[str, Any]]:
    rows = channel.get("topByViews") or channel.get("top_videos") or channel.get("videos") or []
    videos = []
    for v in rows:
        vid = v.get("videoId") or v.get("video_id") or ""
        if not vid:
            continue
        vv = dict(v)
        vv["video_id"] = vid
        vv["views"] = int(v.get("viewCount") or v.get("views") or 0)
        videos.append(vv)
    videos.sort(key=lambda x: int(x.get("views") or 0), reverse=True)
    return videos[: max(1, int(top_n or DEFAULT_TOP_N))]


def _extract_comments(resp: dict[str, Any], video: dict[str, Any], fetched_at: str) -> list[dict[str, Any]]:
    out = []
    for item in resp.get("items") or []:
        sn = ((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
        text = (sn.get("textOriginal") or sn.get("textDisplay") or "").strip()
        if not text:
            continue
        out.append({
            "comment_id": (((item.get("snippet") or {}).get("topLevelComment") or {}).get("id") or item.get("id") or ""),
            "video_id": video.get("video_id") or "",
            "video_title": video.get("title") or "",
            "like_count": int(sn.get("likeCount") or 0),
            "published_at": sn.get("publishedAt") or "",
            "updated_at": sn.get("updatedAt") or "",
            "text": text[:2000],
            "fetched_at": fetched_at,
        })
    return out


def _active_channel_folder() -> Path | None:
    try:
        from app_core import get_active_channel_info, _resolve_to_current_host
        folder = (get_active_channel_info() or {}).get("folder") or ""
        folder = _resolve_to_current_host(folder) if folder else ""
        return Path(folder).expanduser() if folder else None
    except Exception:
        return None


def _raw_path(channel_key: str, channel_folder: str | Path | None = None) -> Path:
    folder = Path(channel_folder).expanduser() if channel_folder else _active_channel_folder()
    if folder:
        return folder / ".studio_learning" / f"{RAW_PREFIX}{_safe_key(channel_key)}.json"
    return COMMENT_MINING_DIR / f"{RAW_PREFIX}{_safe_key(channel_key)}.json"


def _memo_path(channel_key: str) -> Path:
    return COMMENT_MINING_DIR / f"{_safe_key(channel_key)}.json"


def _fallback_keywords(comments: list[dict[str, Any]]) -> list[str]:
    stop = {"the", "and", "for", "you", "this", "that", "with", "have", "are", "is", "to", "of", "in", "it", "i", "a", "に", "で", "の", "を", "が"}
    counter: Counter[str] = Counter()
    for c in comments:
        for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}|[ぁ-んァ-ヶ一-龠]{2,}", c.get("text") or ""):
            lw = w.lower()
            if lw not in stop:
                counter[lw] += 1
    return [k for k, _ in counter.most_common(20)]


def _fallback_memo(channel: dict[str, Any], comments: list[dict[str, Any]], errors: list[dict[str, Any]]) -> dict[str, Any]:
    keywords = _fallback_keywords(comments)
    sample = " / ".join(keywords[:8])
    return {
        "viewer_requests": [],
        "complaints": [],
        "use_scenes": [],
        "repeated_keywords": keywords,
        "underserved_demands": [],
        "demand_notes": [f"コメント {len(comments)} 件から頻出語: {sample}" if sample else "コメント量が少なく、需要抽出は保留"],
        "japanese_summary": f"{channel.get('channel_name') or channel.get('channelName') or '対象チャンネル'} のコメント {len(comments)} 件を確認。頻出語は {sample or '未抽出'}。LLM詳細分析は後で再実行してください。",
        "llm_error": errors[-1].get("error") if errors else "",
    }


def _analyze_with_llm(channel: dict[str, Any], videos: list[dict[str, Any]], comments: list[dict[str, Any]], *, cli_cmd: str) -> dict[str, Any]:
    compact_comments = [{
        "video_title": c.get("video_title") or "",
        "like_count": c.get("like_count") or 0,
        "text": c.get("text") or "",
    } for c in sorted(comments, key=lambda x: int(x.get("like_count") or 0), reverse=True)[:600]]
    prompt = f"""You are a YouTube BGM demand-gap analyst. Analyze viewer comments from benchmark videos.

Channel:
{json.dumps({"id": channel.get("channel_id") or channel.get("channelId"), "name": channel.get("channel_name") or channel.get("channelName")}, ensure_ascii=False)}

Top videos:
{json.dumps([{"title": v.get("title"), "views": v.get("views"), "video_id": v.get("video_id")} for v in videos], ensure_ascii=False)[:12000]}

Comments:
{json.dumps(compact_comments, ensure_ascii=False)[:60000]}

Extract audience demand without copying raw comments. Focus on:
- viewer requests
- complaints or friction
- use scenes such as work, sleep, study, cafe, concentration, relaxation
- repeated keywords
- demands nobody seems to answer yet

Return ONLY one JSON object:
{{
  "viewer_requests": ["..."],
  "complaints": ["..."],
  "use_scenes": ["..."],
  "repeated_keywords": ["..."],
  "underserved_demands": ["..."],
  "demand_notes": ["short actionable memo", "..."],
  "japanese_summary": "日本語で300-700字。次のシリーズ企画に使える需要ギャップを具体的に要約。"
}}
"""
    from app_llm_runner import run_llm
    out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="comment-mining")
    obj = extract_json_object(out) or {}
    if not obj:
        raise RuntimeError(f"LLM JSON抽出失敗: {out[:300]}")
    return obj


def _save_memo(memo: dict[str, Any]) -> None:
    key = memo.get("channel_key") or memo.get("channel_id") or "unknown_channel"
    _write_json(_memo_path(key), memo)
    idx = _read_json(COMMENT_MINING_INDEX, {"memos": []})
    rows = [m for m in (idx.get("memos") or []) if m.get("channel_key") != key and m.get("channel_id") != memo.get("channel_id")]
    rows.append({
        "channel_key": key,
        "channel_id": memo.get("channel_id") or "",
        "channel_name": memo.get("channel_name") or "",
        "generated_at": memo.get("generated_at") or "",
        "comment_count": memo.get("comment_count") or 0,
        "video_count": memo.get("video_count") or 0,
        "japanese_summary": memo.get("japanese_summary") or "",
    })
    rows.sort(key=lambda x: x.get("generated_at") or "", reverse=True)
    _write_json(COMMENT_MINING_INDEX, {"generated_at": _now_iso(), "memos": rows[:100]})


def _write_learning(memo: dict[str, Any], channel_folder: str | Path | None = None) -> None:
    folder = Path(channel_folder).expanduser() if channel_folder else _active_channel_folder()
    if not folder:
        return
    notes = memo.get("demand_notes") or memo.get("underserved_demands") or []
    summary = memo.get("japanese_summary") or ""
    insight = "需要由来: " + (" / ".join([str(x) for x in notes[:3]]) or summary[:160])
    try:
        import app_learning
        app_learning.append_learned_pattern(folder, {
            "learned_at": _now_iso(),
            "source": "comment_mining",
            "tag": "需要由来",
            "insight": insight[:240],
            "channel_id": memo.get("channel_id") or "",
            "channel_name": memo.get("channel_name") or "",
            "comment_count": memo.get("comment_count") or 0,
        })
    except Exception:
        pass


def run(channel_key: str = "", *, top_n: int = DEFAULT_TOP_N, max_comments: int = DEFAULT_MAX_COMMENTS,
        cli_cmd: str = "claude", channel_folder: str | Path | None = None) -> dict[str, Any]:
    channel = _bench.get_channel(channel_key) if channel_key else None
    if not channel:
        data = _bench.competitor_data_from_cache([channel_key] if channel_key else None)
        chans = data.get("channels") or []
        if channel_key:
            channel = next((c for c in chans if (c.get("channelId") == channel_key or c.get("channelName") == channel_key)), None)
        else:
            channel = chans[0] if chans else None
    if not channel:
        raise RuntimeError(f"登録済みベンチチャンネルが見つかりません: {channel_key or '(first)'}")
    cid = channel.get("channel_id") or channel.get("channelId") or channel_key
    cname = channel.get("channel_name") or channel.get("channelName") or cid
    key = _safe_key(cid or cname)
    videos = _top_videos(channel, top_n)
    if not videos:
        raise RuntimeError(f"コメント取得対象の上位動画がありません: {cname}")

    fetched_at = _now_iso()
    raw_comments: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    fetched_videos: list[dict[str, Any]] = []
    for v in videos:
        vid = v.get("video_id") or ""
        if not vid:
            continue
        try:
            resp = _yt_comment_threads(vid, channel_id=cid, max_comments=max_comments)
            comments = _extract_comments(resp, v, fetched_at)
            raw_comments.extend(comments)
            fetched_videos.append({"video_id": vid, "title": v.get("title") or "", "views": v.get("views") or 0, "comments": len(comments)})
        except Exception as e:
            msg = str(e)
            human = {}
            try:
                from error_humanizer import humanize_error
                human = humanize_error(message=msg, stage="commentThreads.list")
            except Exception:
                human = {}
            errors.append({"video_id": vid, "title": v.get("title") or "", "error": msg[:500], "humanized": human})
            fetched_videos.append({"video_id": vid, "title": v.get("title") or "", "views": v.get("views") or 0, "comments": 0, "error": msg[:240], "humanized": human})

    raw_payload = ensure_fetched_at({
        "source": "commentThreads.list",
        "feature": "comment_mining",
        "channel_key": key,
        "channel_id": cid,
        "channel_name": cname,
        "fetched_at": fetched_at,
        "top_n": top_n,
        "max_comments_per_video": max_comments,
        "videos": fetched_videos,
        "comments": raw_comments,
        "errors": errors,
    }, fetched_at)
    raw_path = _raw_path(key, channel_folder)
    _write_json(raw_path, raw_payload)

    try:
        analysis = _analyze_with_llm(channel, videos, raw_comments, cli_cmd=cli_cmd)
    except Exception as e:
        errors.append({"stage": "llm", "error": str(e)[:500]})
        analysis = _fallback_memo(channel, raw_comments, errors)

    memo = {
        "channel_key": key,
        "channel_id": cid,
        "channel_name": cname,
        "generated_at": _now_iso(),
        "source": "comment_mining",
        "raw_comments_path": str(raw_path),
        "comment_count": len(raw_comments),
        "video_count": len(fetched_videos),
        "quota_feature": "comment_mining",
        "viewer_requests": analysis.get("viewer_requests") or [],
        "complaints": analysis.get("complaints") or [],
        "use_scenes": analysis.get("use_scenes") or [],
        "repeated_keywords": analysis.get("repeated_keywords") or [],
        "underserved_demands": analysis.get("underserved_demands") or [],
        "demand_notes": analysis.get("demand_notes") or [],
        "japanese_summary": analysis.get("japanese_summary") or "",
        "llm_error": analysis.get("llm_error") or "",
        "errors": errors,
        "error_humanizer": next((e.get("humanized") for e in errors if e.get("humanized")), {}),
    }
    _save_memo(memo)
    _write_learning(memo, channel_folder)
    return {"status": "ok", "memo": memo, "raw_path": str(raw_path)}


def get(channel_key: str) -> dict[str, Any]:
    key = _safe_key(channel_key)
    memo = _read_json(_memo_path(key), None)
    if memo is None:
        ch = _bench.get_channel(channel_key)
        cid = (ch or {}).get("channel_id") or (ch or {}).get("channelId") or ""
        if cid and cid != key:
            memo = _read_json(_memo_path(_safe_key(cid)), None)
    if memo is None:
        return {"status": "not_found", "channel_key": channel_key}
    return {"status": "ok", "memo": memo}


def latest_memos(limit: int = 5) -> list[dict[str, Any]]:
    idx = _read_json(COMMENT_MINING_INDEX, {"memos": []})
    return (idx.get("memos") or [])[:limit]


def prompt_hint(limit: int = 3) -> str:
    lines = []
    for m in latest_memos(limit):
        summary = (m.get("japanese_summary") or "").strip()
        if summary:
            lines.append(f"- [{m.get('channel_name') or m.get('channel_id')}] {summary[:500]}")
    return "\n".join(lines)

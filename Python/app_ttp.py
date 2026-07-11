#!/usr/bin/env python3
"""TTP profile generator for benchmark channels."""
from __future__ import annotations

import datetime as _dt
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from _app_config import resolve_shared_config_dir
    SHARED_CONFIG_DIR = resolve_shared_config_dir()
except Exception:
    SHARED_CONFIG_DIR = Path.home() / ".config" / "orzz"

import app_benchmark_channels as _bench
from app_benchmark_common import extract_json_object

TTP_DIR = SHARED_CONFIG_DIR / "ttp_profiles"
TTP_INDEX = TTP_DIR / "profiles.json"
JST = _dt.timezone(_dt.timedelta(hours=9))


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
    return key[:100] or "ttp"


def _parse_dt(value: str) -> _dt.datetime | None:
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


def _parse_duration_seconds(value: str) -> int:
    m = re.fullmatch(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def _title_pattern(title: str) -> str:
    t = re.sub(r"\d+", "N", title or "")
    t = re.sub(r"\b(vol|episode|ep|part)\.?\s*N\b", "Vol.N", t, flags=re.I)
    if "|" in t:
        return " | ".join(["phrase"] * min(4, len(t.split("|"))))
    if " - " in t:
        return " - ".join(["phrase"] * min(3, len(t.split(" - "))))
    if re.search(r"【.+?】", t):
        return "【hook】 + title"
    if len(t) >= 70:
        return "long descriptive scene title"
    return "short mood keyword title"


def _series_key(title: str) -> str:
    m = re.search(r"\b(?:vol|episode|ep|part)\.?\s*(\d+)\b", title or "", re.I)
    if m:
        return "Vol/Episode numbering"
    if re.search(r"#\d+", title or ""):
        return "hashtag numbering"
    return ""


def aggregate(channel_ids: list[str] | None = None) -> dict[str, Any]:
    data = _bench.competitor_data_from_cache(channel_ids)
    channels = data.get("channels") or []
    videos: list[dict[str, Any]] = []
    for ch in channels:
        for v in ch.get("recentUploads") or []:
            vv = dict(v)
            vv["_channel"] = ch.get("channelName")
            vv["_channel_id"] = ch.get("channelId")
            videos.append(vv)
    durations = [_parse_duration_seconds(v.get("duration") or "") for v in videos]
    durations = [d for d in durations if d > 0]
    dates = [_parse_dt(v.get("publishedAt") or v.get("published_at") or "") for v in videos]
    dates = [d.astimezone(JST) for d in dates if d]
    tag_counter: Counter[str] = Counter()
    for v in videos:
        tag_counter.update([str(t).lower() for t in (v.get("tags") or []) if t])
    title_patterns = Counter(_title_pattern(v.get("title") or "") for v in videos)
    series = Counter(_series_key(v.get("title") or "") for v in videos)
    series.pop("", None)
    weekday = Counter(d.strftime("%a") for d in dates)
    hours = Counter(d.hour for d in dates)
    views_by_age = []
    now = _dt.datetime.now(JST)
    for v in videos:
        d = _parse_dt(v.get("publishedAt") or v.get("published_at") or "")
        if not d:
            continue
        age_h = max(1, int((now - d.astimezone(JST)).total_seconds() // 3600))
        views_by_age.append({"age_hours": age_h, "views": int(v.get("viewCount") or v.get("views") or 0), "title": v.get("title") or ""})
    return {
        "generated_at": _now_iso(),
        "channel_count": len(channels),
        "video_count": len(videos),
        "channels": [{"channel_id": c.get("channelId"), "name": c.get("channelName"), "subscribers": c.get("subscribers", 0)} for c in channels],
        "title_syntax_patterns": [{"pattern": k, "count": v} for k, v in title_patterns.most_common(10)],
        "duration_distribution": {
            "min_sec": min(durations) if durations else 0,
            "max_sec": max(durations) if durations else 0,
            "avg_sec": round(sum(durations) / len(durations)) if durations else 0,
            "buckets": {
                "<30m": sum(1 for d in durations if d < 1800),
                "30-60m": sum(1 for d in durations if 1800 <= d < 3600),
                "1-3h": sum(1 for d in durations if 3600 <= d < 10800),
                "3h+": sum(1 for d in durations if d >= 10800),
            },
        },
        "posting_cadence": {
            "weekday_counts": dict(weekday.most_common()),
            "hour_counts_jst": {str(k): v for k, v in sorted(hours.items())},
            "top_weekday": weekday.most_common(1)[0][0] if weekday else "",
            "top_hour_jst": hours.most_common(1)[0][0] if hours else None,
        },
        "series_structure": [{"pattern": k, "count": v} for k, v in series.most_common(8)],
        "frequent_tags": [{"tag": k, "count": v} for k, v in tag_counter.most_common(30)],
        "growth_curve": {
            "source": "cached_public_stats_from_batchGetStats_flow",
            "samples": sorted(views_by_age, key=lambda x: x["age_hours"])[:50],
        },
    }


def _fallback_report(spec: dict[str, Any]) -> str:
    dur = spec.get("duration_distribution") or {}
    post = spec.get("posting_cadence") or {}
    pat = (spec.get("title_syntax_patterns") or [{}])[0].get("pattern", "long descriptive scene title")
    tags = ", ".join(t["tag"] for t in (spec.get("frequent_tags") or [])[:8])
    return (
        f"勝ちフォーマット要約: 対象 {spec.get('channel_count')}ch / {spec.get('video_count')}本。"
        f"タイトルは「{pat}」が中心。尺は平均 {dur.get('avg_sec', 0)//60} 分、"
        f"投稿は JST {post.get('top_hour_jst')}時・{post.get('top_weekday')} が多め。"
        f"頻出タグは {tags or '未取得'}。adopt は構文と尺の安定感、avoid は固有表現の直用、"
        "evolve は自チャンネルのペルソナに合わせた別シーン化。"
    )


def generate(channel_ids: list[str] | None = None, *, cli_cmd: str = "claude", name: str = "") -> dict[str, Any]:
    spec = aggregate(channel_ids)
    if not spec.get("video_count"):
        raise RuntimeError("TTP生成対象のベンチマーク動画がありません")
    prompt = f"""You are a YouTube BGM strategy analyst. Build a winning-format spec from the aggregated benchmark data.

Data:
{json.dumps(spec, ensure_ascii=False, indent=2)[:30000]}

Return ONLY one JSON object:
{{
  "winning_format_spec": {{
    "title_formula": "...",
    "duration": "...",
    "posting_schedule": "...",
    "series_structure": "...",
    "tag_strategy": ["..."],
    "growth_curve_read": "..."
  }},
  "imitate_evolve": {{
    "adopt": ["..."],
    "avoid": ["..."],
    "evolve": ["..."],
    "differentiation_points": ["..."]
  }},
  "japanese_report": "日本語で500-900字。完コピ回避を明記。"
}}
"""
    llm_obj: dict[str, Any] = {}
    try:
        from app_llm_runner import run_llm
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="ttp-generate")
        llm_obj = extract_json_object(out) or {}
    except Exception as e:
        llm_obj = {"llm_error": str(e)}
    report = llm_obj.get("japanese_report") or _fallback_report(spec)
    profile = {
        "id": _safe_key(name or "_".join([c.get("channel_id") or "" for c in spec.get("channels", [])]) or _now_iso()),
        "generated_at": _now_iso(),
        "input_channel_ids": channel_ids or [],
        "aggregate": spec,
        "winning_format_spec": llm_obj.get("winning_format_spec") or {},
        "imitate_evolve": llm_obj.get("imitate_evolve") or {
            "adopt": ["実績のあるタイトル構文、長尺BGMの安定尺、投稿曜日/時刻の傾向"],
            "avoid": ["チャンネル固有名、固有のビジュアル設定、タイトル文言の直コピー"],
            "evolve": ["自チャンネルのペルソナに合わせて場所・時間帯・感情ジョブを置き換える"],
            "differentiation_points": ["同じ型を使っても固有シーンと音色設計は別物にする"],
        },
        "japanese_report": report,
        "llm_error": llm_obj.get("llm_error", ""),
    }
    save_profile(profile)
    return profile


def save_profile(profile: dict[str, Any]) -> None:
    idx = _read_json(TTP_INDEX, {"profiles": []})
    rows = [p for p in idx.get("profiles", []) if p.get("id") != profile.get("id")]
    rows.append(profile)
    rows.sort(key=lambda p: p.get("generated_at") or "", reverse=True)
    idx = {"generated_at": _now_iso(), "profiles": rows[:50]}
    _write_json(TTP_INDEX, idx)
    _write_json(TTP_DIR / f"{profile.get('id')}.json", profile)
    try:
        import os
        folder = (os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
        if not folder:
            from app_core import get_dashboard_config
            folder = (get_dashboard_config().get("channel_folder") or "").strip()
        if folder and Path(folder).exists():
            import app_image_modules as _im
            _im.add_candidate_modules_from_ttp(folder, profile)
    except Exception:
        pass


def list_profiles() -> dict[str, Any]:
    idx = _read_json(TTP_INDEX, {"profiles": []})
    return {"status": "ok", "profiles": idx.get("profiles") or [], "generated_at": idx.get("generated_at")}

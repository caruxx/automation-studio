#!/usr/bin/env python3
"""Phase H-A: monetization, 48h review, and rival alert summaries."""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_core import get_channels  # noqa: E402
from app_quota import execute_youtube, get_video_stats_with_fallback  # noqa: E402
from app_retention import ensure_fetched_at  # noqa: E402
from settings_service import append_config_audit, config_get  # noqa: E402

JST = _dt.timezone(_dt.timedelta(hours=9))
TOKEN_FILENAME = ".youtube_token.json"
LEARNING_DIR = ".studio_learning"
MONETIZATION_FILE = "monetization_snapshots.json"
LEARNED_PATTERNS_FILE = "learned_patterns.json"
REVIEW_AUDIT_FILE = "review_audit.jsonl"
YT_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YPP_SUBSCRIBERS = 1000
YPP_WATCH_HOURS = 4000


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _today_jst() -> str:
    return _dt.datetime.now(JST).date().isoformat()


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


def _coerce_int(v: Any) -> int:
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return 0


def _channel_folder(ch: dict[str, Any]) -> Path:
    return Path(str(ch.get("folder") or "")).expanduser()


def _learning_dir(ch_or_folder: dict[str, Any] | str | Path) -> Path:
    folder = _channel_folder(ch_or_folder) if isinstance(ch_or_folder, dict) else Path(ch_or_folder)
    return folder / LEARNING_DIR


def _token_scopes(token_file: Path) -> list[str]:
    data = _read_json(token_file, {})
    scopes = data.get("scopes") or data.get("scope") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return [str(s) for s in scopes if s]


def _has_analytics_scope(token_file: Path) -> bool:
    scopes = _token_scopes(token_file)
    return YT_ANALYTICS_SCOPE in scopes


def _is_enabled(key: str, default: bool = True) -> bool:
    try:
        value = config_get(key).get("value")
        return default if value is None else bool(value)
    except Exception:
        return default


def review_enabled() -> bool:
    return _is_enabled("review.enabled", True)


def review_learn_writeback_enabled() -> bool:
    return _is_enabled("review.learn_writeback", True)


def rival_alert_enabled() -> bool:
    return _is_enabled("alerts.rival_enabled", True)


def _load_credentials(token_file: Path):
    import app_youtube as _yt
    return _yt.load_channel_credentials(token_file)


def _yt_service(token_file: Path):
    from googleapiclient.discovery import build
    creds = _load_credentials(token_file)
    if creds is None:
        raise RuntimeError(f"OAuth token invalid: {token_file}")
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _snapshot_path(ch: dict[str, Any]) -> Path:
    return _learning_dir(ch) / MONETIZATION_FILE


def _load_snapshots(ch: dict[str, Any]) -> list[dict[str, Any]]:
    data = _read_json(_snapshot_path(ch), [])
    return data if isinstance(data, list) else []


def _save_snapshot(ch: dict[str, Any], row: dict[str, Any]) -> None:
    rows = _load_snapshots(ch)
    today = row.get("date")
    rows = [r for r in rows if r.get("date") != today]
    rows.append(row)
    rows.sort(key=lambda r: r.get("date") or "")
    _write_json(_snapshot_path(ch), rows[-730:])


def _predict_subscriber_date(rows: list[dict[str, Any]], current_subs: int) -> dict[str, Any]:
    if current_subs >= YPP_SUBSCRIBERS:
        return {"status": "achieved", "date": None, "daily_gain": 0}
    if len(rows) < 2:
        return {"status": "insufficient_data", "date": None, "daily_gain": 0}
    latest = rows[-1]
    latest_date = _dt.date.fromisoformat(latest["date"])
    target_start = latest_date - _dt.timedelta(days=28)
    base = rows[0]
    for r in rows:
        try:
            if _dt.date.fromisoformat(r.get("date", "")) <= target_start:
                base = r
        except Exception:
            continue
    try:
        base_date = _dt.date.fromisoformat(base["date"])
    except Exception:
        return {"status": "insufficient_data", "date": None, "daily_gain": 0}
    days = max(1, (latest_date - base_date).days)
    gain = current_subs - _coerce_int(base.get("subscriber_count"))
    daily = gain / days
    if daily <= 0:
        return {"status": "no_growth", "date": None, "daily_gain": round(daily, 2)}
    remaining = max(0, YPP_SUBSCRIBERS - current_subs)
    predict = latest_date + _dt.timedelta(days=int((remaining + daily - 0.0001) // daily))
    return {"status": "predicted", "date": predict.isoformat(), "daily_gain": round(daily, 2)}


def fetch_monetization_snapshot_for_channel(ch: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    folder = _channel_folder(ch)
    token = folder / TOKEN_FILENAME
    base = {
        "channel_id": ch.get("id") or "",
        "youtube_channel_id": ch.get("youtube_channel_id") or "",
        "name": ch.get("name") or "",
        "folder": str(folder),
    }
    unavailable_analytics = {
        "available": False,
        "watch_hours_28d": None,
        "watch_hours_target": YPP_WATCH_HOURS,
        "message": "YouTube Analytics API は再認証で有効化できます",
        "needs_reauth": True,
    }
    rows = _load_snapshots(ch)
    if rows and rows[-1].get("date") == _today_jst() and not force:
        latest = rows[-1]
    else:
        if not token.exists():
            return {
                **base,
                "status": "unauthorized",
                "error": f"token not found: {token}",
                "snapshots": rows[-30:],
                "analytics": {**unavailable_analytics, "message": f"token not found: {token}"},
            }
        try:
            youtube = _yt_service(token)
            resp = execute_youtube(
                youtube.channels().list(part="snippet,statistics", mine=True, maxResults=1),
                "channels.list",
                channel_id=ch.get("id") or "",
            )
            item = (resp.get("items") or [None])[0] or {}
            stats = item.get("statistics") or {}
            sn = item.get("snippet") or {}
            latest = ensure_fetched_at({
                "date": _today_jst(),
                "fetched_at": _now_utc().isoformat(timespec="seconds"),
                "youtube_channel_id": item.get("id") or ch.get("youtube_channel_id") or "",
                "title": sn.get("title") or ch.get("name") or "",
                "subscriber_count": _coerce_int(stats.get("subscriberCount")),
                "view_count": _coerce_int(stats.get("viewCount")),
                "video_count": _coerce_int(stats.get("videoCount")),
                "hidden_subscriber_count": bool(stats.get("hiddenSubscriberCount")),
            })
            _save_snapshot(ch, latest)
            rows = _load_snapshots(ch)
        except Exception as e:
            return {
                **base,
                "status": "error",
                "error": str(e),
                "snapshots": rows[-30:],
                "analytics": {**unavailable_analytics, "message": str(e)},
            }
    subs = _coerce_int(latest.get("subscriber_count"))
    pred = _predict_subscriber_date(rows, subs)
    analytics = dict(unavailable_analytics)
    if token.exists() and _has_analytics_scope(token):
        try:
            from routers.analytics import get_channel_analytics_payload
            payload = get_channel_analytics_payload(ch, days_count=28, refresh=force)
            auth = payload.get("auth") or {}
            summary = payload.get("summary") or {}
            if auth.get("ok"):
                minutes = float(
                    summary.get("watch_minutes_total")
                    or summary.get("estimatedMinutesWatched")
                    or summary.get("watch_minutes")
                    or 0
                )
                analytics.update({
                    "available": True,
                    "watch_hours_28d": round(minutes / 60, 1),
                    "message": "",
                    "needs_reauth": False,
                })
            else:
                analytics.update({
                    "available": False,
                    "watch_hours_28d": None,
                    "message": auth.get("error") or "YouTube Analytics API は再認証で有効化できます",
                    "needs_reauth": bool(auth.get("needs_reauth")),
                })
        except Exception as e:
            analytics.update({
                "available": False,
                "watch_hours_28d": None,
                "message": f"Analytics 読み取りは後で再試行できます: {e}",
                "needs_reauth": True,
            })
    return {
        **base,
        "status": "ok",
        "latest": latest,
        "snapshots": rows[-30:],
        "subscriber_goal": YPP_SUBSCRIBERS,
        "subscriber_progress_pct": round(min(100, (subs / YPP_SUBSCRIBERS) * 100), 1),
        "subscriber_prediction": pred,
        "analytics": analytics,
    }


def monetization_report(*, force: bool = False) -> dict[str, Any]:
    channels = [c for c in get_channels() if c.get("folder")]
    items = [fetch_monetization_snapshot_for_channel(ch, force=force) for ch in channels]
    return {
        "status": "ok",
        "generated_at": _now_utc().isoformat(timespec="seconds"),
        "channels": items,
        "quota_estimate_units": len([x for x in items if x.get("status") == "ok"]),
    }


def _parse_dt(value: str) -> _dt.datetime | None:
    if not value:
        return None
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = _dt.datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=JST)
    except Exception:
        return None


def _find_uploaded_videos(ch: dict[str, Any], *, limit: int = 80) -> list[dict[str, Any]]:
    folder = _channel_folder(ch)
    out: list[dict[str, Any]] = []
    if not folder.exists():
        return out
    for marker in folder.glob("*/youtube_upload.json"):
        data = _read_json(marker, {})
        vid = data.get("video_id") or data.get("id")
        url = data.get("url") or data.get("watch_url") or ""
        if not vid:
            m = re.search(r"(?:youtu\.be/|v=)([A-Za-z0-9_-]{11})", str(url))
            vid = m.group(1) if m else ""
        if not vid:
            continue
        published = data.get("published_at") or data.get("publish_at") or data.get("scheduled_publish_time") or data.get("uploaded_at") or data.get("timestamp") or ""
        out.append({"video_id": vid, "video_name": marker.parent.name, "marker": str(marker), "published_at": published})
    out.sort(key=lambda x: x.get("published_at") or "", reverse=True)
    return out[:limit]


def _videos_stats(ch: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    if not ids:
        return []
    token = _channel_folder(ch) / TOKEN_FILENAME
    youtube = _yt_service(token)
    resp, source = get_video_stats_with_fallback(
        youtube,
        ids[:50],
        channel_id=ch.get("id") or "",
        part="id,snippet,statistics",
    )
    out = []
    for it in resp.get("items") or []:
        st = it.get("statistics") or {}
        sn = it.get("snippet") or {}
        out.append(ensure_fetched_at({
            "video_id": it.get("id") or "",
            "title": sn.get("title") or "",
            "published_at": sn.get("publishedAt") or sn.get("publishTime") or "",
            "views": _coerce_int(st.get("viewCount")),
            "likes": _coerce_int(st.get("likeCount")),
            "comments": _coerce_int(st.get("commentCount")),
            "stats_source": source,
        }))
    return out


def _one_line_ai(prompt: str, *, fallback: str) -> str:
    try:
        from app_llm_runner import run_llm
        text = run_llm(prompt, timeout=90, label="learning-one-line").strip()
        line = re.sub(r"\s+", " ", text.splitlines()[0]).strip(" -:。")
        return line[:160] or fallback
    except Exception:
        return fallback


def learned_patterns_path(channel_folder: str | Path) -> Path:
    return _learning_dir(channel_folder) / LEARNED_PATTERNS_FILE


def load_learned_patterns(channel_folder: str | Path, *, limit: int = 8) -> list[dict[str, Any]]:
    data = _read_json(learned_patterns_path(channel_folder), [])
    rows = data if isinstance(data, list) else []
    rows.sort(key=lambda r: r.get("learned_at") or "", reverse=True)
    return rows[:limit]


def learned_patterns_prompt_hint(channel_folder: str | Path, *, limit: int = 5) -> str:
    rows = load_learned_patterns(channel_folder, limit=limit)
    lines = []
    for r in rows:
        insight = (r.get("insight") or "").strip()
        if insight:
            lines.append(f"- {insight}")
    return "\n".join(lines)


def _write_learned_pattern(ch: dict[str, Any], pattern: dict[str, Any]) -> None:
    path = learned_patterns_path(_channel_folder(ch))
    rows = _read_json(path, [])
    if not isinstance(rows, list):
        rows = []
    vid = pattern.get("video_id")
    if vid and any(r.get("video_id") == vid for r in rows):
        return
    rows.append(pattern)
    rows = rows[-50:]
    old_count = len(_read_json(path, [])) if path.exists() else 0
    _write_json(path, rows)
    append_config_audit("review", "review.learned_patterns", old_count, len(rows),
                        channel_id=ch.get("id") or "", path=str(path))
    _append_jsonl(_learning_dir(ch) / REVIEW_AUDIT_FILE, {
        "when": _now_utc().isoformat(timespec="seconds"),
        "action": "learned_pattern_write",
        "channel_id": ch.get("id") or "",
        "video_id": pattern.get("video_id"),
        "ratio": pattern.get("ratio"),
        "insight": pattern.get("insight"),
    })


def append_learned_pattern(channel_folder: str | Path, pattern: dict[str, Any]) -> dict[str, Any]:
    """Append a non-review learning row while preserving the existing 50-row cap."""
    path = learned_patterns_path(channel_folder)
    rows = _read_json(path, [])
    if not isinstance(rows, list):
        rows = []
    key = (
        pattern.get("source"),
        pattern.get("tag"),
        pattern.get("channel_id"),
        pattern.get("insight"),
    )
    if any((r.get("source"), r.get("tag"), r.get("channel_id"), r.get("insight")) == key for r in rows):
        return {"status": "duplicate", "path": str(path), "count": len(rows)}
    rows.append(pattern)
    rows = rows[-50:]
    old_count = len(_read_json(path, [])) if path.exists() else 0
    _write_json(path, rows)
    append_config_audit("learning", "learned_patterns", old_count, len(rows), path=str(path))
    _append_jsonl(Path(channel_folder) / LEARNING_DIR / REVIEW_AUDIT_FILE, {
        "when": _now_utc().isoformat(timespec="seconds"),
        "action": "learned_pattern_write",
        "source": pattern.get("source"),
        "tag": pattern.get("tag"),
        "insight": pattern.get("insight"),
    })
    return {"status": "ok", "path": str(path), "count": len(rows)}


def review_video_against_average(ch: dict[str, Any], current: dict[str, Any],
                                 baseline_videos: list[dict[str, Any]], *,
                                 writeback: bool = True) -> dict[str, Any]:
    vals = [_coerce_int(v.get("views")) for v in baseline_videos if v.get("video_id") != current.get("video_id")]
    vals = [v for v in vals if v > 0]
    avg = int(sum(vals) / len(vals)) if vals else 0
    views = _coerce_int(current.get("views"))
    ratio = (views / avg) if avg else None
    diff_pct = round(((ratio - 1) * 100), 1) if ratio is not None else None
    win = bool(ratio is not None and ratio >= 1.2)
    insight = ""
    if win:
        prompt = (
            "You analyze one winning YouTube BGM video. In Japanese, output ONE short line about what likely worked "
            "(thumbnail text, title type, or concept). No bullets.\n"
            f"Channel: {ch.get('name')}\nTitle: {current.get('title')}\nViews at 48h: {views}\n"
            f"Channel moving average: {avg}\n"
        )
        insight = _one_line_ai(prompt, fallback="タイトルの場面訴求とサムネの分かりやすさが初速に効いた可能性")
        if writeback and review_learn_writeback_enabled():
            gen_meta = {}
            winning_meta = {}
            try:
                import app_image_modules as _im
                gen_meta = _im.load_generation_meta_for_video(_channel_folder(ch), current.get("video_id") or "")
                if gen_meta.get("module_ids"):
                    winning_meta = gen_meta
                else:
                    for item in gen_meta.get("items") or []:
                        if item.get("module_ids"):
                            winning_meta = item
                            break
                if winning_meta:
                    _im.record_winning_modules(
                        _channel_folder(ch),
                        winning_meta,
                        video_id=current.get("video_id") or "",
                        title=current.get("title") or "",
                        ratio=round(ratio, 3) if ratio is not None else None,
                    )
            except Exception:
                winning_meta = {}
            _write_learned_pattern(ch, {
                "learned_at": _now_utc().isoformat(timespec="seconds"),
                "video_id": current.get("video_id"),
                "title": current.get("title"),
                "views_48h": views,
                "baseline_avg": avg,
                "ratio": round(ratio, 3) if ratio is not None else None,
                "diff_pct": diff_pct,
                "insight": insight,
                "winning_image_modules": winning_meta.get("module_ids") or {},
                "winning_image_sections": winning_meta.get("sections") or {},
            })
    return {
        "channel_id": ch.get("id") or "",
        "channel_name": ch.get("name") or "",
        "video_id": current.get("video_id"),
        "title": current.get("title") or "",
        "views": views,
        "baseline_avg": avg,
        "ratio": round(ratio, 3) if ratio is not None else None,
        "diff_pct": diff_pct,
        "win": win,
        "insight": insight,
    }


def run_48h_reviews(*, writeback: bool = True, mock: dict[str, Any] | None = None) -> dict[str, Any]:
    if not review_enabled() and mock is None:
        return {"status": "skipped", "reason": "review.enabled=false", "reviews": []}
    if mock:
        ch = mock.get("channel") or {"id": "mock", "name": "Mock Channel", "folder": str(Path.cwd())}
        result = review_video_against_average(ch, mock["current"], mock.get("baseline") or [], writeback=writeback)
        return {"status": "ok", "mock": True, "reviews": [result], "skipped": []}
    reviews, skipped = [], []
    now = _now_utc()
    for ch in [c for c in get_channels() if c.get("folder")]:
        uploaded = _find_uploaded_videos(ch)
        candidates = []
        for v in uploaded:
            dt = _parse_dt(v.get("published_at") or "")
            if not dt:
                continue
            age_h = (now - dt.astimezone(_dt.timezone.utc)).total_seconds() / 3600
            if 36 <= age_h <= 60:
                candidates.append(v)
        if not candidates:
            skipped.append({"channel_id": ch.get("id"), "name": ch.get("name"), "reason": "no_48h_target"})
            continue
        ids = [v["video_id"] for v in (candidates + uploaded)[:50]]
        try:
            stats = _videos_stats(ch, ids)
        except Exception as e:
            skipped.append({"channel_id": ch.get("id"), "name": ch.get("name"), "reason": str(e)})
            continue
        by_id = {v["video_id"]: v for v in stats}
        baseline = [by_id[x["video_id"]] for x in uploaded if x["video_id"] in by_id][:12]
        for c in candidates:
            cur = by_id.get(c["video_id"])
            if cur:
                reviews.append(review_video_against_average(ch, cur, baseline, writeback=writeback))
    return {"status": "ok", "reviews": reviews, "skipped": skipped}


def detect_rival_alerts(*, dry_run: bool = True, mock_records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not rival_alert_enabled() and mock_records is None:
        return {"status": "skipped", "reason": "alerts.rival_enabled=false", "alerts": []}
    records = mock_records
    if records is None:
        try:
            import app_video_intel as _vi
            records = _vi.list_records(limit=500)
        except Exception as e:
            return {"status": "error", "error": str(e), "alerts": []}
    by_ch: dict[str, list[dict[str, Any]]] = {}
    for r in records or []:
        by_ch.setdefault(str(r.get("channel") or ""), []).append(r)
    alerts = []
    today_cutoff = _dt.date.today() - _dt.timedelta(days=14)
    for channel, rows in by_ch.items():
        vals = [_coerce_int(r.get("first_v48")) for r in rows if _coerce_int(r.get("first_v48")) > 0]
        if len(vals) < 3:
            continue
        avg = sum(vals) / len(vals)
        for r in rows:
            v48 = _coerce_int(r.get("first_v48"))
            if not v48 or avg <= 0 or v48 < avg * 1.35:
                continue
            ds = (r.get("detected_date") or r.get("last_seen") or "")[:10]
            try:
                if ds and _dt.date.fromisoformat(ds) < today_cutoff:
                    continue
            except Exception:
                pass
            fallback = "タイトルの具体的な利用シーンとサムネの第一印象が初速を押し上げた可能性"
            insight = _one_line_ai(
                "You analyze a fast-starting rival YouTube BGM video. In Japanese, output ONE short line on what worked. "
                f"Rival channel: {channel}\nTitle: {r.get('title')}\n48h views: {v48}\nRival average: {int(avg)}\n",
                fallback=fallback,
            )
            alerts.append({
                "channel": channel,
                "title": r.get("title") or "",
                "video_id": r.get("video_id") or "",
                "first_v48": v48,
                "baseline_avg": int(avg),
                "ratio": round(v48 / avg, 2),
                "insight": insight,
            })
    alerts.sort(key=lambda x: x.get("ratio") or 0, reverse=True)
    return {"status": "ok", "dry_run": dry_run, "alerts": alerts[:10], "records": len(records or [])}


def digest_sections() -> dict[str, Any]:
    monet = monetization_report(force=False)
    reviews = run_48h_reviews(writeback=True)
    rivals = detect_rival_alerts(dry_run=True)
    return {"monetization": monet, "reviews": reviews, "rivals": rivals}

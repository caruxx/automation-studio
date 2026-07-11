#!/usr/bin/env python3
"""YouTube 実態とローカル投稿記録の照合."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _app_config import resolve_config_dir, resolve_shared_base, resolve_shared_config_dir  # noqa: E402
from app_quota import execute_youtube  # noqa: E402

CONFIG_DIR = resolve_config_dir()
SHARED_BASE = resolve_shared_base()
SHARED_CONFIG_DIR = resolve_shared_config_dir()
CHANNELS_CONFIG = SHARED_CONFIG_DIR / "channels.json"
REPORT_PATH = SHARED_CONFIG_DIR / "reconcile_latest.json"
CHANNEL_TOKEN_FILENAME = ".youtube_token.json"
JST = dt.timezone(dt.timedelta(hours=9))


def _load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _shared_drive_root() -> Path:
    text = unicodedata.normalize("NFC", str(SHARED_CONFIG_DIR))
    marker = "/共有ドライブ/"
    idx = text.find(marker)
    if idx >= 0:
        return Path(text[:idx + len(marker) - 1])
    return SHARED_BASE.parent.parent


def _resolve_to_current_host(folder: str) -> str:
    if not folder:
        return folder
    text = unicodedata.normalize("NFC", str(folder))
    marker = "/共有ドライブ/"
    idx = text.find(marker)
    if idx < 0:
        return folder
    return str(_shared_drive_root() / text[idx + len(marker):])


def _channels() -> list[dict[str, Any]]:
    rows = _load_json(CHANNELS_CONFIG, [])
    out = []
    for ch in rows if isinstance(rows, list) else []:
        item = dict(ch)
        if item.get("folder"):
            item["folder"] = _resolve_to_current_host(item["folder"])
        out.append(item)
    return out


def _dashboard_api_key() -> str:
    for cfg_dir in (CONFIG_DIR, Path.home() / ".config" / "orzz"):
        cfg = _load_json(cfg_dir / "dashboard_config.json", {})
        key = (cfg.get("youtube_api_key") or "").strip() if isinstance(cfg, dict) else ""
        if key:
            return key
        p = cfg_dir / "youtube_api_key.txt"
        try:
            key = p.read_text(encoding="utf-8").strip()
        except Exception:
            key = ""
        if key:
            return key
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"].strip()
    return ""


def reconcile_enabled() -> bool:
    try:
        from settings_service import config_get
        value = config_get("reconcile.enabled").get("value")
        return True if value is None else bool(value)
    except Exception:
        return True


def _norm_title(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "").casefold()).strip()


def _vol_from_name(value: str) -> str:
    m = re.match(r"^0*(\d+)(?:_|$)", Path(value or "").name)
    return m.group(1) if m else ""


def _title_has_vol(title: str, vol: str) -> bool:
    if not vol:
        return False
    n = re.escape(str(int(vol)))
    patterns = [
        rf"\bvol\.?\s*0*{n}\b",
        rf"\bvolume\s*0*{n}\b",
        rf"\b#?\s*0*{n}\b",
    ]
    return any(re.search(p, title or "", re.IGNORECASE) for p in patterns)


def _read_local_records(channel_folder: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not channel_folder.exists():
        return rows
    for folder in sorted((p for p in channel_folder.iterdir() if p.is_dir()), key=lambda p: p.name):
        marker = folder / "youtube_upload.json"
        if not marker.exists():
            continue
        data = _load_json(marker, {})
        vid = (data.get("video_id") or data.get("id") or "").strip()
        if not vid and data.get("url"):
            m = re.search(r"(?:youtu\.be/|v=)([A-Za-z0-9_-]{11})", str(data.get("url")))
            if m:
                vid = m.group(1)
        title = (data.get("title") or "").strip()
        title_file = folder / "youtube_title.txt"
        if not title and title_file.exists():
            try:
                title = title_file.read_text(encoding="utf-8").strip()
            except Exception:
                title = ""
        rows.append({
            "video_name": folder.name,
            "folder": str(folder),
            "marker": str(marker),
            "video_id": vid,
            "title": title,
            "vol": _vol_from_name(folder.name),
            "posted": bool(vid or data.get("uploaded_at") or data.get("published_at")),
            "data": data if isinstance(data, dict) else {},
        })
    return rows


def _youtube_from_token(token_path: Path):
    import app_youtube
    creds = app_youtube.load_channel_credentials(token_path)
    if creds is None:
        raise RuntimeError(f"OAuth token が無効: {token_path}")
    return build("youtube", "v3", credentials=creds), "oauth"


def _youtube_from_key(api_key: str):
    if not api_key:
        raise RuntimeError("YouTube API key が未設定")
    return build("youtube", "v3", developerKey=api_key), "api_key"


def fetch_recent_uploads(
    *,
    channel_id: str = "",
    token_path: str | Path | None = None,
    limit: int = 50,
    prefer_api_key: bool = False,
) -> dict[str, Any]:
    """対象チャンネルの uploads プレイリスト直近投稿を取得する."""
    limit = max(1, min(int(limit or 50), 50))
    api_key = _dashboard_api_key()
    youtube = None
    auth = ""
    token = Path(token_path) if token_path else None
    if prefer_api_key and api_key and channel_id:
        youtube, auth = _youtube_from_key(api_key)
    elif token and token.exists():
        youtube, auth = _youtube_from_token(token)
    elif api_key and channel_id:
        youtube, auth = _youtube_from_key(api_key)
    else:
        raise RuntimeError("uploads取得に使える API key / OAuth token がありません")

    if auth == "oauth":
        ch_resp = execute_youtube(
            youtube.channels().list(part="id,snippet,contentDetails", mine=True, maxResults=10),
            "channels.list",
            channel_id=channel_id,
            feature="reconcile",
            detail={"endpoint": "channels", "auth": auth},
        )
        ch_items = ch_resp.get("items") or []
        if channel_id:
            ch_items = [x for x in ch_items if x.get("id") == channel_id] or ch_items
    else:
        ch_resp = execute_youtube(
            youtube.channels().list(part="id,snippet,contentDetails", id=channel_id, maxResults=1),
            "channels.list",
            channel_id=channel_id,
            feature="reconcile",
            detail={"endpoint": "channels", "auth": auth},
        )
        ch_items = ch_resp.get("items") or []
    if not ch_items:
        raise RuntimeError(f"YouTube channel が見つかりません: {channel_id or '(mine)'}")

    ch_item = ch_items[0]
    uploads_id = (((ch_item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or "")
    if not uploads_id:
        raise RuntimeError("uploads playlist id が取得できません")
    pl_resp = execute_youtube(
        youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=uploads_id,
            maxResults=limit,
        ),
        "playlistItems.list",
        channel_id=ch_item.get("id") or channel_id,
        feature="reconcile",
        detail={"endpoint": "playlistItems", "auth": auth, "limit": limit},
    )
    uploads = []
    for item in pl_resp.get("items") or []:
        sn = item.get("snippet") or {}
        cd = item.get("contentDetails") or {}
        vid = cd.get("videoId") or sn.get("resourceId", {}).get("videoId") or ""
        if not vid:
            continue
        uploads.append({
            "video_id": vid,
            "title": sn.get("title") or "",
            "published_at": cd.get("videoPublishedAt") or sn.get("publishedAt") or "",
            "url": f"https://youtu.be/{vid}",
        })
    return {
        "status": "ok",
        "channel_id": ch_item.get("id") or channel_id,
        "channel_title": (ch_item.get("snippet") or {}).get("title") or "",
        "uploads_playlist_id": uploads_id,
        "auth": auth,
        "limit": limit,
        "uploads": uploads,
        "quota_units": 2,
    }


def _find_marker_video_id_direct(
    *,
    video_id: str,
    channel_id: str = "",
    token_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """marker の video_id を videos.list で直接確認する."""
    if not video_id or not token_path:
        return None
    token = Path(token_path)
    youtube, auth = _youtube_from_token(token)
    resp = execute_youtube(
        youtube.videos().list(part="snippet,status", id=video_id),
        "videos.list",
        channel_id=channel_id,
        feature="reconcile",
        detail={"endpoint": "videos", "auth": auth, "source": "marker_video_id"},
    )
    items = resp.get("items") or []
    if not items:
        return None
    item = items[0]
    sn = item.get("snippet") or {}
    actual_channel_id = sn.get("channelId") or ""
    if channel_id and actual_channel_id and actual_channel_id != channel_id:
        return None
    title = sn.get("title") or ""
    return {
        "video_id": item.get("id") or video_id,
        "title": title,
        "url": f"https://youtu.be/{item.get('id') or video_id}",
        "published_at": sn.get("publishedAt") or "",
        "channel_id": actual_channel_id,
        "status": item.get("status") or {},
    }


def find_existing_upload(
    folder: str | Path,
    *,
    title: str = "",
    channel_id: str = "",
    token_path: str | Path | None = None,
    limit: int = 50,
    write_marker: bool = False,
) -> dict[str, Any]:
    """アップロード直前ガード用。既存投稿があれば marker を補正して返す."""
    folder = Path(folder)
    marker = folder / "youtube_upload.json"
    local = _load_json(marker, {}) if marker.exists() else {}
    video_id = (local.get("video_id") or local.get("id") or "").strip()
    target_title = (title or local.get("title") or "").strip()
    if not target_title:
        title_file = folder / "youtube_title.txt"
        if title_file.exists():
            target_title = title_file.read_text(encoding="utf-8").strip()
    vol = _vol_from_name(folder.name)

    if video_id:
        direct_match = _find_marker_video_id_direct(
            video_id=video_id,
            channel_id=channel_id,
            token_path=token_path,
        )
        if direct_match:
            result = {
                "status": "exists",
                "exists": True,
                "reason": "marker_video_id_direct",
                "video_name": folder.name,
                "vol": vol,
                "target_title": target_title,
                "match": {
                    "video_id": direct_match.get("video_id"),
                    "title": direct_match.get("title") or target_title,
                    "url": direct_match.get("url"),
                    "published_at": direct_match.get("published_at") or "",
                },
                "channel_id": direct_match.get("channel_id") or channel_id,
                "checked": {"uploads": 0, "auth": "oauth", "quota_units": 1, "direct_video_id": True},
            }
            if write_marker:
                data = dict(local)
                data.update({
                    "video_id": result["match"].get("video_id"),
                    "url": result["match"].get("url"),
                    "title": result["match"].get("title") or target_title,
                    "privacy": data.get("privacy") or "unknown",
                    "uploaded_at": data.get("uploaded_at") or result["match"].get("published_at") or dt.datetime.now().isoformat(),
                    "reconciled_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
                    "reconciled_status": "posted_verified",
                    "reconciled_reason": "marker_video_id_direct",
                    "reconciled_from": "youtube_videos_list",
                })
                _save_json(marker, data)
                result["marker_updated"] = str(marker)
            return result

    recent = fetch_recent_uploads(channel_id=channel_id, token_path=token_path, limit=limit)
    uploads = recent.get("uploads") or []
    match = None
    reason = ""
    if video_id:
        match = next((u for u in uploads if u.get("video_id") == video_id), None)
        if match:
            reason = "video_id"
    if match is None and target_title:
        nt = _norm_title(target_title)
        match = next((u for u in uploads if _norm_title(u.get("title", "")) == nt), None)
        if match:
            reason = "title_exact"
    if match is None and vol:
        match = next((u for u in uploads if _title_has_vol(u.get("title", ""), vol)), None)
        if match:
            reason = "title_vol"

    result = {
        "status": "exists" if match else "missing",
        "exists": bool(match),
        "reason": reason,
        "video_name": folder.name,
        "vol": vol,
        "target_title": target_title,
        "match": match or {},
        "channel_id": recent.get("channel_id") or channel_id,
        "checked": {"uploads": len(uploads), "auth": recent.get("auth"), "quota_units": recent.get("quota_units", 2)},
    }
    if match and write_marker:
        data = dict(local)
        data.update({
            "video_id": match.get("video_id"),
            "url": match.get("url"),
            "title": match.get("title") or target_title,
            "privacy": data.get("privacy") or "unknown",
            "uploaded_at": data.get("uploaded_at") or match.get("published_at") or dt.datetime.now().isoformat(),
            "reconciled_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
            "reconciled_status": "posted_verified",
            "reconciled_reason": reason,
            "reconciled_from": "youtube_uploads_playlist",
        })
        _save_json(marker, data)
        result["marker_updated"] = str(marker)
    return result


def notify_duplicate_skip(vol: str, channel_name: str = "") -> dict[str, Any]:
    msg = f"vol.{vol}は既にYouTube上に存在するためスキップしました"
    if channel_name:
        msg = f"[{channel_name}] {msg}"
    script = SHARED_BASE / "Python" / "app_notify.sh"
    if not script.exists():
        return {"sent": False, "message": msg, "error": "app_notify.sh not found"}
    try:
        subprocess.run(["bash", str(script), msg], capture_output=True, text=True, timeout=10)
        return {"sent": True, "message": msg}
    except Exception as e:
        return {"sent": False, "message": msg, "error": str(e)[:200]}


def reconcile_channel(channel: dict[str, Any], *, limit: int = 50) -> dict[str, Any]:
    folder = Path(channel.get("folder") or "")
    cid = (channel.get("youtube_channel_id") or "").strip()
    name = channel.get("name") or channel.get("id") or folder.name
    token = folder / CHANNEL_TOKEN_FILENAME
    out = {
        "channel_id": channel.get("id") or "",
        "youtube_channel_id": cid,
        "channel_name": name,
        "folder": str(folder),
        "status": "ok",
        "missing_on_youtube": [],
        "missing_locally": [],
        "local_posted": 0,
        "youtube_uploads_checked": 0,
        "quota_units": 0,
        "error": "",
    }
    if not folder.exists():
        out.update({"status": "error", "error": "channel folder not found"})
        return out
    local_rows = _read_local_records(folder)
    out["local_posted"] = sum(1 for r in local_rows if r.get("posted"))
    try:
        yt = fetch_recent_uploads(channel_id=cid, token_path=token if token.exists() else None, limit=limit)
    except Exception as e:
        out.update({"status": "error", "error": str(e)[:300]})
        return out
    uploads = yt.get("uploads") or []
    out["youtube_uploads_checked"] = len(uploads)
    out["quota_units"] = int(yt.get("quota_units") or 0)

    by_id = {r["video_id"]: r for r in local_rows if r.get("video_id")}
    by_vol = {r["vol"]: r for r in local_rows if r.get("vol")}
    yt_ids = {u["video_id"] for u in uploads if u.get("video_id")}
    for row in local_rows:
        if not row.get("posted"):
            continue
        vid = row.get("video_id")
        found = bool(vid and vid in yt_ids)
        if not found and row.get("vol"):
            found = any(_title_has_vol(u.get("title", ""), row["vol"]) for u in uploads)
        if not found:
            out["missing_on_youtube"].append({
                "video_name": row["video_name"],
                "vol": row["vol"],
                "video_id": vid,
                "title": row.get("title") or "",
            })
    for u in uploads:
        if u.get("video_id") in by_id:
            continue
        vol_hit = next((v for v in by_vol if _title_has_vol(u.get("title", ""), v)), "")
        if vol_hit:
            continue
        out["missing_locally"].append(u)
    return out


def run_reconcile_all(*, limit: int = 50, write_report: bool = True) -> dict[str, Any]:
    if not reconcile_enabled():
        return {"status": "disabled", "enabled": False, "channels": [], "summary": {"drift_count": 0}}
    channels = _channels()
    results = [reconcile_channel(ch, limit=limit) for ch in channels]
    drift = sum(len(r.get("missing_on_youtube") or []) + len(r.get("missing_locally") or []) for r in results)
    errors = sum(1 for r in results if r.get("status") != "ok")
    report = {
        "status": "ok",
        "enabled": True,
        "generated_at": dt.datetime.now(JST).isoformat(timespec="seconds"),
        "limit": limit,
        "summary": {
            "channels": len(results),
            "drift_count": drift,
            "error_count": errors,
            "quota_units_estimated": sum(int(r.get("quota_units") or 0) for r in results if r.get("status") == "ok"),
        },
        "channels": results,
    }
    if write_report:
        _save_json(REPORT_PATH, report)
    return report


def digest_line() -> str:
    if not reconcile_enabled():
        return "無効"
    report = _load_json(REPORT_PATH, {})
    if not report:
        report = run_reconcile_all(write_report=True)
    summary = report.get("summary") or {}
    drift = int(summary.get("drift_count") or 0)
    errors = int(summary.get("error_count") or 0)
    if drift == 0 and errors == 0:
        return f"問題なし（{summary.get('channels', 0)}ch / quota約{summary.get('quota_units_estimated', 0)}unit）"
    parts = []
    for ch in report.get("channels") or []:
        n = len(ch.get("missing_on_youtube") or []) + len(ch.get("missing_locally") or [])
        if n:
            parts.append(f"{ch.get('channel_name')}: {n}件")
        elif ch.get("status") != "ok":
            parts.append(f"{ch.get('channel_name')}: 取得失敗")
    return f"ズレ{drift}件 / エラー{errors}ch（{', '.join(parts[:5])}）"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="YouTube 実態とローカル投稿記録の照合")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = run_reconcile_all(limit=args.limit, write_report=True)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(digest_line())

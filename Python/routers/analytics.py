#!/usr/bin/env python3
"""YouTube Analytics API ルータ。

チャンネル別 OAuth トークンを使って youtubeAnalytics v2 reports.query を呼び、
外部ポーラー向けに軽量な summary/signals を返す。
"""
import datetime as _dt
import html
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app_core import CONFIG_DIR, get_channels
from app_youtube import SCOPES as YOUTUBE_SCOPES, reauth_channel_credentials
from error_humanizer import humanize_error

router = APIRouter()

ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
TOKEN_FILENAME = ".youtube_token.json"
CACHE_FILENAME = ".youtube_analytics.json"
CACHE_TTL_SECONDS = 12 * 60 * 60
YT_UPLOAD_HISTORY_FILE = CONFIG_DIR / "youtube_upload_history.jsonl"
REAUTH_TIMEOUT_SECONDS = 180
REAUTH_STALE_SECONDS = 210
_reauth_state_lock = threading.Lock()
_reauth_state: Optional[dict] = None


def _channel_lock_id(ch: dict) -> str:
    return ch.get("id") or ch.get("youtube_channel_id") or ""


def _reauth_elapsed_seconds(state: Optional[dict]) -> int:
    if not state:
        return 0
    return max(0, int(time.monotonic() - float(state.get("started_monotonic") or 0)))


def _reauth_conflict_detail(state: Optional[dict]) -> str:
    channel_id = (state or {}).get("channel_id") or "unknown"
    return f"別の再認証が進行中です ({channel_id}, {_reauth_elapsed_seconds(state)}秒経過)"


def _acquire_reauth_state(channel_id: str) -> tuple[str, Optional[str]]:
    global _reauth_state
    now = time.monotonic()
    with _reauth_state_lock:
        if _reauth_state and now - float(_reauth_state.get("started_monotonic") or 0) <= REAUTH_STALE_SECONDS:
            return "", _reauth_conflict_detail(_reauth_state)
        token = uuid.uuid4().hex
        _reauth_state = {
            "token": token,
            "channel_id": channel_id,
            "started_monotonic": now,
            "started_at": _now_iso(),
        }
        return token, None


def _release_reauth_state(token: str) -> bool:
    global _reauth_state
    with _reauth_state_lock:
        if _reauth_state and _reauth_state.get("token") == token:
            _reauth_state = None
            return True
    return False


def _cancel_reauth_state() -> None:
    global _reauth_state
    with _reauth_state_lock:
        _reauth_state = None


def _is_current_reauth_state(token: str) -> bool:
    with _reauth_state_lock:
        return bool(_reauth_state and _reauth_state.get("token") == token)


def _analytics_api_enable_url(text: str) -> str:
    low = (text or "").lower()
    if "accessnotconfigured" not in low and "has not been used in project" not in low:
        return ""
    m = re.search(r"project\s+(\d+)", text or "", re.IGNORECASE)
    if not m:
        return ""
    return f"https://console.developers.google.com/apis/api/youtubeanalytics.googleapis.com/overview?project={m.group(1)}"


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _date_from_any(value: str) -> Optional[_dt.datetime]:
    if not value:
        return None
    s = str(value).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return _dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                out.append(item)
    except Exception:
        return []
    return out


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(path)
        path.chmod(0o600)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def _channels() -> list[dict]:
    return [c for c in (get_channels() or []) if c.get("id") or c.get("youtube_channel_id")]


def _find_channel(channel_id: str) -> Optional[dict]:
    key = (channel_id or "").strip()
    for ch in _channels():
        if key in {ch.get("id"), ch.get("youtube_channel_id"), ch.get("prefix"), ch.get("handle")}:
            return ch
    return None


def _channel_payload_base(ch: dict) -> dict:
    return {
        "channel_id": ch.get("id") or ch.get("youtube_channel_id") or "",
        "youtube_channel_id": ch.get("youtube_channel_id") or "",
        "name": ch.get("name") or "",
    }


def _token_path(ch: dict) -> Path:
    return Path(ch.get("folder") or "") / TOKEN_FILENAME


def _cache_path(ch: dict) -> Path:
    return Path(ch.get("folder") or "") / CACHE_FILENAME


def _token_scopes(token_file: Path) -> list[str]:
    data = _read_json(token_file, {})
    scopes = data.get("scopes") or data.get("scope") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    return [str(s) for s in scopes if s]


def _auth_result(ok: bool, *, needs_reauth: bool = False, error: str = "") -> dict:
    return {"ok": bool(ok), "needs_reauth": bool(needs_reauth), "error": error or ""}


def _empty_payload(ch: dict, auth: dict, days_count: int) -> dict:
    return {
        **_channel_payload_base(ch),
        "fetched_at": "",
        "auth": auth,
        "days": [],
        "summary": _summarize([]),
        "requested_days": days_count,
    }


def _cache_is_fresh(cache: dict) -> bool:
    fetched = _date_from_any(cache.get("fetched_at") or "")
    if not fetched:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=_dt.timezone.utc)
    return (_now_utc() - fetched).total_seconds() < CACHE_TTL_SECONDS


def _coerce_int(value) -> int:
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def _coerce_float(value) -> float:
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _summarize(rows: list[dict]) -> dict:
    ordered = sorted(rows or [], key=lambda r: r.get("date") or "")
    last7 = ordered[-7:]
    prior7 = ordered[-14:-7]
    views_last7 = sum(_coerce_int(r.get("views")) for r in last7)
    views_prior7 = sum(_coerce_int(r.get("views")) for r in prior7)
    if views_prior7 > 0:
        views_delta_pct = round(((views_last7 - views_prior7) / views_prior7) * 100, 2)
    elif views_last7 > 0:
        views_delta_pct = 100.0
    else:
        views_delta_pct = 0.0
    subs_gained = sum(_coerce_int(r.get("subscribersGained")) for r in last7)
    subs_lost = sum(_coerce_int(r.get("subscribersLost")) for r in last7)
    return {
        "views_last7": views_last7,
        "views_prior7": views_prior7,
        "views_delta_pct": views_delta_pct,
        "watch_minutes_total": sum(_coerce_float(r.get("estimatedMinutesWatched")) for r in ordered),
        "watch_minutes_last7": sum(_coerce_float(r.get("estimatedMinutesWatched")) for r in last7),
        "subs_net_last7": subs_gained - subs_lost,
    }


def _with_error_on_cache(cache: dict, ch: dict, auth: dict, days_count: int) -> dict:
    if cache:
        payload = {**cache}
        payload.update(_channel_payload_base(ch))
        payload["auth"] = auth
        payload["requested_days"] = days_count
        payload["stale"] = True
        payload["summary"] = _summarize(payload.get("days") or [])
        return payload
    return _empty_payload(ch, auth, days_count)


def _analytics_error_message(exc: Exception) -> tuple[str, bool]:
    text = str(exc)
    needs_reauth = False
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            status = getattr(exc.resp, "status", None)
            reason = getattr(exc.resp, "reason", "")
            content = exc.content.decode("utf-8", errors="ignore") if getattr(exc, "content", None) else ""
            text = f"HTTP {status} {reason}: {content or exc}"
            low = text.lower()
            access_not_configured = "accessnotconfigured" in low or "youtubeanalytics.googleapis.com" in low and "has not been used" in low
            needs_reauth = (
                not access_not_configured
                and (
                "insufficientpermissions" in low
                or "insufficient authentication scopes" in low
                or "request had insufficient authentication scopes" in low
                or status in {401, 403}
                )
            )
    except Exception:
        pass
    enable_url = _analytics_api_enable_url(text)
    if enable_url:
        h = humanize_error(message=text, technical_detail=text, stage="youtube_analytics")
        guidance = f"{h.get('next_action') or 'Cloud ConsoleでYouTube Analytics APIを有効化してください。'} {enable_url}"
        if guidance not in text:
            text = f"{text}\n{guidance}"
    if enable_url and len(text) > 1000:
        keep = max(0, 960 - len(enable_url))
        text = f"{text[:keep]}\n... {enable_url}"
    return text[:1000], needs_reauth


def _delete_cache(ch: dict) -> bool:
    cache_file = _cache_path(ch)
    if not cache_file.exists():
        return False
    try:
        cache_file.unlink()
        return True
    except Exception:
        return False


def _reauth_channel(ch: dict, is_current=None) -> dict:
    folder = Path(ch.get("folder") or "")
    if not folder:
        raise RuntimeError("channel folder is not configured")
    if not folder.exists():
        raise RuntimeError(f"channel folder not found: {folder}")
    creds = reauth_channel_credentials(folder, should_save=is_current)
    if is_current is not None and not is_current():
        return {
            "ok": False,
            "channel_id": ch.get("id") or ch.get("youtube_channel_id") or "",
            "error": "OAuth reauth result was discarded because the flow is no longer current",
        }
    if is_current is None or is_current():
        _delete_cache(ch)
    scopes = list(getattr(creds, "scopes", None) or _token_scopes(_token_path(ch)) or YOUTUBE_SCOPES)
    return {
        "ok": True,
        "channel_id": ch.get("id") or ch.get("youtube_channel_id") or "",
        "scopes": scopes,
    }


def _status_label(auth: dict) -> tuple[str, str]:
    error = auth.get("error") or ""
    if auth.get("ok"):
        return "連携OK", "ok"
    if "token not found" in error.lower():
        return "トークンなし", "missing"
    if auth.get("needs_reauth"):
        return "要再認証", "reauth"
    return "エラー", "error"


def _linkify_error(text: str) -> str:
    escaped = html.escape(text or "")
    return re.sub(
        r"https://console\.developers\.google\.com/apis/api/youtubeanalytics\.googleapis\.com/overview\?project=\d+",
        lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noreferrer">{m.group(0)}</a>',
        escaped,
    )


def _render_setup_page() -> str:
    rows = []
    for idx, ch in enumerate(_channels()):
        cid = ch.get("id") or ch.get("youtube_channel_id") or ""
        row_id = f"row-{idx}"
        payload = get_channel_analytics_payload(ch, days_count=28, refresh=False)
        auth = payload.get("auth") or {}
        label, cls = _status_label(auth)
        name = ch.get("name") or cid or "(no name)"
        error_html = _linkify_error(auth.get("error") or "")
        rows.append(f"""
        <tr id="{row_id}">
          <td>
            <div class="name">{html.escape(name)}</div>
            <div class="id">{html.escape(cid)}</div>
          </td>
          <td><span class="badge {cls}">{html.escape(label)}</span></td>
          <td class="error">{error_html}</td>
          <td><button type="button" onclick='reauth({json.dumps(cid)}, this, {json.dumps(row_id)})'>再認証</button></td>
          <td class="result"></td>
        </tr>
        """)
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Analytics 再認証</title>
  <style>
    body {{ margin: 0; background: #111827; color: #e5e7eb; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 32px 20px; }}
    h1 {{ margin: 0 0 10px; font-size: 24px; }}
    .note {{ margin: 0 0 24px; color: #cbd5e1; }}
    table {{ width: 100%; border-collapse: collapse; background: #0f172a; border: 1px solid #334155; }}
    th, td {{ padding: 12px; border-bottom: 1px solid #1f2937; text-align: left; vertical-align: top; }}
    th {{ color: #93a4b8; font-size: 12px; letter-spacing: .04em; text-transform: uppercase; }}
    .name {{ font-weight: 650; }}
    .id {{ margin-top: 4px; color: #94a3b8; font-size: 12px; }}
    .badge {{ display: inline-block; padding: 4px 8px; border-radius: 999px; font-size: 12px; white-space: nowrap; }}
    .badge.ok {{ background: #064e3b; color: #a7f3d0; }}
    .badge.reauth, .badge.missing {{ background: #78350f; color: #fde68a; }}
    .badge.error {{ background: #7f1d1d; color: #fecaca; }}
    .error {{ color: #cbd5e1; font-size: 12px; max-width: 460px; word-break: break-word; white-space: pre-wrap; }}
    .error a {{ color: #93c5fd; }}
    button {{ border: 1px solid #475569; background: #1e293b; color: #f8fafc; border-radius: 6px; padding: 8px 12px; cursor: pointer; }}
    button:disabled {{ opacity: .55; cursor: wait; }}
    .result {{ color: #cbd5e1; font-size: 12px; min-width: 160px; }}
    .result button {{ margin-top: 8px; padding: 6px 10px; font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <h1>YouTube Analytics 再認証</h1>
    <p class="note">再認証するとブラウザが開きます。ブランドアカウントとして対象チャンネルを選択してください。</p>
    <table>
      <thead><tr><th>チャンネル</th><th>認証状態</th><th>詳細</th><th>操作</th><th>結果</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </main>
  <script>
    function setResultText(result, text) {{
      result.textContent = text;
    }}

    async function reauth(id, button, rowId) {{
      const row = document.getElementById(rowId);
      const result = row.querySelector('.result');
      button.disabled = true;
      setResultText(result, '再認証を開始しています...');
      try {{
        const res = await fetch('/api/analytics/reauth/' + encodeURIComponent(id), {{ method: 'POST' }});
        const data = await res.json();
        if (res.status === 409) {{
          const detail = data.detail || '別の再認証が進行中です';
          result.innerHTML = '';
          const msg = document.createElement('div');
          msg.textContent = detail;
          const retry = document.createElement('button');
          retry.type = 'button';
          retry.textContent = '進行中の再認証を破棄して再試行';
          retry.addEventListener('click', () => cancelAndRetry(id, button, rowId, retry));
          result.appendChild(msg);
          result.appendChild(retry);
          button.disabled = false;
        }} else if (data.ok) {{
          setResultText(result, '完了しました。再読み込みします...');
          setTimeout(() => location.reload(), 1200);
        }} else {{
          setResultText(result, data.error || '再認証に失敗しました');
          button.disabled = false;
        }}
      }} catch (e) {{
        setResultText(result, String(e));
        button.disabled = false;
      }}
    }}

    async function cancelAndRetry(id, button, rowId, retryButton) {{
      const row = document.getElementById(rowId);
      const result = row.querySelector('.result');
      retryButton.disabled = true;
      setResultText(result, '進行中の再認証を破棄しています...');
      try {{
        await fetch('/api/analytics/reauth/cancel', {{ method: 'POST' }});
      }} catch (e) {{
        setResultText(result, String(e));
        button.disabled = false;
        return;
      }}
      await reauth(id, button, rowId);
    }}
  </script>
</body>
</html>"""


def _load_credentials_without_oauth(token_file: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(token_file), scopes=_token_scopes(token_file))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _write_json(token_file, json.loads(creds.to_json()))
        else:
            raise RuntimeError("OAuth token is invalid and cannot be refreshed")
    return creds


def _verify_authenticated_channel(ch: dict, creds) -> tuple[bool, str]:
    expected = (ch.get("youtube_channel_id") or "").strip()
    if not expected:
        return True, ""
    from googleapiclient.discovery import build

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = youtube.channels().list(part="id,snippet", mine=True, maxResults=10).execute()
    items = resp.get("items") or []
    actual_ids = [str(it.get("id") or "").strip() for it in items if it.get("id")]
    if expected in actual_ids:
        return True, ""
    titles = []
    for it in items:
        sn = it.get("snippet") or {}
        title = sn.get("title") or it.get("id") or ""
        if title:
            titles.append(str(title))
    actual = ", ".join([x for x in actual_ids if x] or titles) or "unknown"
    return False, f"OAuth token channel mismatch: expected {expected}, actual {actual}. ブランドアカウントとして対象チャンネルを選択してください。"


def _fetch_analytics(ch: dict, days_count: int) -> dict:
    from googleapiclient.discovery import build

    token_file = _token_path(ch)
    if not token_file.exists():
        return _empty_payload(ch, _auth_result(False, needs_reauth=True, error=f"token not found: {token_file}"), days_count)

    scopes = _token_scopes(token_file)
    if ANALYTICS_SCOPE not in scopes:
        return _empty_payload(
            ch,
            _auth_result(False, needs_reauth=True, error=f"missing OAuth scope: {ANALYTICS_SCOPE}"),
            days_count,
        )

    creds = _load_credentials_without_oauth(token_file)
    matched, mismatch_error = _verify_authenticated_channel(ch, creds)
    if not matched:
        return _empty_payload(ch, _auth_result(False, needs_reauth=True, error=mismatch_error), days_count)

    service = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
    end = _dt.date.today() - _dt.timedelta(days=1)
    start = end - _dt.timedelta(days=days_count - 1)
    resp = service.reports().query(
        ids="channel==MINE",
        startDate=start.isoformat(),
        endDate=end.isoformat(),
        metrics="views,estimatedMinutesWatched,averageViewDuration,subscribersGained,subscribersLost,likes",
        dimensions="day",
        sort="day",
    ).execute()

    headers = [h.get("name") for h in (resp.get("columnHeaders") or [])]
    out_days = []
    for row in resp.get("rows") or []:
        item = dict(zip(headers, row))
        out_days.append({
            "date": item.get("day") or item.get("date") or "",
            "views": _coerce_int(item.get("views")),
            "estimatedMinutesWatched": _coerce_float(item.get("estimatedMinutesWatched")),
            "averageViewDuration": _coerce_float(item.get("averageViewDuration")),
            "subscribersGained": _coerce_int(item.get("subscribersGained")),
            "subscribersLost": _coerce_int(item.get("subscribersLost")),
            "likes": _coerce_int(item.get("likes")),
        })

    payload = {
        **_channel_payload_base(ch),
        "fetched_at": _now_iso(),
        "auth": _auth_result(True),
        "days": out_days,
        "summary": _summarize(out_days),
        "requested_days": days_count,
    }
    _write_json(_cache_path(ch), payload)
    return payload


def get_channel_analytics_payload(ch: dict, days_count: int = 28, refresh: bool = False) -> dict:
    days_count = max(1, min(int(days_count or 28), 365))
    cache_file = _cache_path(ch)
    cache = _read_json(cache_file, {}) if cache_file.exists() else {}
    if cache and not refresh and _cache_is_fresh(cache):
        payload = {**cache}
        payload.update(_channel_payload_base(ch))
        payload["summary"] = _summarize(payload.get("days") or [])
        payload["requested_days"] = days_count
        payload.setdefault("auth", _auth_result(True))
        return payload
    token_file = _token_path(ch)
    if not token_file.exists():
        auth = _auth_result(False, needs_reauth=True, error=f"token not found: {token_file}")
        return _with_error_on_cache(cache, ch, auth, days_count)
    if ANALYTICS_SCOPE not in _token_scopes(token_file):
        auth = _auth_result(False, needs_reauth=True, error=f"missing OAuth scope: {ANALYTICS_SCOPE}")
        return _with_error_on_cache(cache, ch, auth, days_count)
    try:
        return _fetch_analytics(ch, days_count)
    except Exception as e:
        msg, needs_reauth = _analytics_error_message(e)
        return _with_error_on_cache(cache, ch, _auth_result(False, needs_reauth=needs_reauth, error=msg), days_count)


def _history_matches_channel(entry: dict, ch: dict) -> bool:
    candidates = {
        (ch.get("id") or "").strip(),
        (ch.get("youtube_channel_id") or "").strip(),
        (ch.get("name") or "").strip(),
        (ch.get("prefix") or "").strip(),
        (ch.get("handle") or "").strip(),
    }
    candidates.discard("")
    fields = (
        "channel_id",
        "youtube_channel_id",
        "channel_name",
        "channel",
        "channel_handle",
        "handle",
        "prefix",
    )
    values = {(entry.get(f) or "").strip() for f in fields}
    values.discard("")
    return bool(candidates & values)


def _is_successful_upload_history(entry: dict) -> bool:
    status = str(entry.get("status") or "").strip().lower()
    if status in {"failed", "fail", "error", "cancelled", "canceled", "skipped"}:
        return False
    return bool(entry.get("video_id") or entry.get("youtube_video_id"))


def _last_upload_at(ch: dict) -> Optional[_dt.datetime]:
    newest = None
    for entry in _read_jsonl(YT_UPLOAD_HISTORY_FILE):
        if not _history_matches_channel(entry, ch):
            continue
        if not _is_successful_upload_history(entry):
            continue
        ts = _date_from_any(entry.get("uploaded_at") or entry.get("recorded_at") or "")
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_dt.timezone.utc)
        if newest is None or ts > newest:
            newest = ts
    return newest


def _signal(channel_id: str, typ: str, message: str, severity: str) -> dict:
    return {
        "channel_id": channel_id,
        "type": typ,
        "message": message,
        "severity": severity,
        "detected_at": _now_iso(),
    }


@router.get("/api/analytics/setup", response_class=HTMLResponse)
def api_analytics_setup():
    return HTMLResponse(_render_setup_page())


@router.post("/api/analytics/reauth/cancel")
def api_analytics_reauth_cancel():
    _cancel_reauth_state()
    return {"ok": True}


@router.post("/api/analytics/reauth/{channel_id}")
def api_analytics_reauth(channel_id: str):
    ch = _find_channel(channel_id)
    if not ch:
        raise HTTPException(404, f"channel not found: {channel_id}")
    lock_channel_id = _channel_lock_id(ch)
    reauth_token, conflict_detail = _acquire_reauth_state(lock_channel_id)
    if conflict_detail:
        raise HTTPException(409, conflict_detail)

    result = {}

    def _run():
        try:
            run_result = _reauth_channel(ch, is_current=lambda: _is_current_reauth_state(reauth_token))
            if _is_current_reauth_state(reauth_token):
                result.update(run_result)
        except Exception as e:
            if _is_current_reauth_state(reauth_token):
                result.update({
                    "ok": False,
                    "channel_id": ch.get("id") or ch.get("youtube_channel_id") or "",
                    "error": str(e),
                })
        finally:
            _release_reauth_state(reauth_token)

    t = threading.Thread(target=_run, name=f"analytics-reauth-{channel_id}", daemon=True)
    t.start()
    t.join(REAUTH_TIMEOUT_SECONDS)
    if t.is_alive():
        _release_reauth_state(reauth_token)
        return {
            "ok": False,
            "channel_id": ch.get("id") or ch.get("youtube_channel_id") or "",
            "error": f"OAuth reauth timed out after {REAUTH_TIMEOUT_SECONDS} seconds",
        }
    return result or {
        "ok": False,
        "channel_id": ch.get("id") or ch.get("youtube_channel_id") or "",
        "error": "OAuth reauth finished without a result",
    }


@router.get("/api/analytics/channel/{channel_id}")
def api_analytics_channel(channel_id: str, days: int = 28, refresh: bool = False):
    ch = _find_channel(channel_id)
    if not ch:
        raise HTTPException(404, f"channel not found: {channel_id}")
    return get_channel_analytics_payload(ch, days_count=days, refresh=refresh)


@router.get("/api/analytics/overview")
def api_analytics_overview(days: int = 28, refresh: bool = False):
    items = []
    for ch in _channels():
        payload = get_channel_analytics_payload(ch, days_count=days, refresh=refresh)
        items.append({
            **_channel_payload_base(ch),
            "fetched_at": payload.get("fetched_at", ""),
            "auth": payload.get("auth") or _auth_result(False, error="unknown analytics auth state"),
            "summary": payload.get("summary") or _summarize(payload.get("days") or []),
        })
    return {"fetched_at": _now_iso(), "days": max(14, min(int(days or 28), 365)), "channels": items}


@router.get("/api/analytics/signals")
def api_analytics_signals():
    signals = []
    for ch in _channels():
        cid = ch.get("id") or ch.get("youtube_channel_id") or ""
        payload = get_channel_analytics_payload(ch, days_count=28, refresh=False)
        auth = payload.get("auth") or {}
        if auth.get("needs_reauth"):
            signals.append(_signal(cid, "needs_reauth", auth.get("error") or "YouTube Analytics scope is missing", "high"))
        elif not auth.get("ok"):
            signals.append(_signal(cid, "analytics_error", auth.get("error") or "YouTube Analytics API error", "high"))

        summary = payload.get("summary") or {}
        delta = summary.get("views_delta_pct")
        if isinstance(delta, (int, float)) and delta <= -30:
            signals.append(_signal(
                cid,
                "views_drop",
                f"直近7日の再生数が前7日比 {delta:.1f}% 低下しています",
                "medium",
            ))

        last_upload = _last_upload_at(ch)
        if last_upload is None:
            signals.append(_signal(cid, "no_recent_upload", "成功したアップロード履歴が見つかりません", "medium"))
        else:
            age_days = int((_now_utc() - last_upload).total_seconds() // 86400)
            if age_days >= 14:
                signals.append(_signal(cid, "no_recent_upload", f"最後の成功アップロードから {age_days} 日経過しています", "medium"))

    return {"detected_at": _now_iso(), "signals": signals}

#!/usr/bin/env python3
"""VPS の runs API を Mac（Claude / Codex の CLI）から呼ぶ薄いクライアント。

設定: ~/.config/automation-studio/worker.json（vps_ledger_sync と同じファイル）の base_url と ai_token。
      APP_WORKER_CONFIG で場所を上書きできる。ai_token が無ければ vps_not_configured（worker_token は使わない）。
stdout には何も出さない（呼び出し側の studio.py が JSON 1 行を出す）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

USER_AGENT = "AutomationStudioCLI/1.0"
TIMEOUT_SECONDS = 15


class VpsRunsError(RuntimeError):
    def __init__(self, code: str, message: str, status: Optional[int] = None, detail: Any = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.detail = detail


def config_path() -> Path:
    return Path(os.environ.get("APP_WORKER_CONFIG") or "~/.config/automation-studio/worker.json").expanduser()


def load_settings() -> dict[str, str]:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise VpsRunsError("vps_not_configured", f"VPS の設定が読めません: {path}")
    base_url = str((data or {}).get("base_url") or "").strip().rstrip("/")
    ai_token = str((data or {}).get("ai_token") or "").strip()
    if not base_url or not ai_token:
        raise VpsRunsError("vps_not_configured", f"base_url と ai_token を {path} に設定してください（ai:runs スコープの AI トークン）")
    return {"base_url": base_url, "ai_token": ai_token}


def workflow_version() -> str:
    from exit_codes import WORKFLOW_VERSION
    return WORKFLOW_VERSION


def new_request_id() -> str:
    return "req-" + uuid.uuid4().hex


def request(method: str, path: str, *, payload: Optional[dict[str, Any]] = None, query: Optional[dict[str, Any]] = None,
            settings: Optional[dict[str, str]] = None, opener=None) -> dict[str, Any]:
    settings = settings or load_settings()
    url = settings["base_url"] + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {settings['ai_token']}", "Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(req, timeout=TIMEOUT_SECONDS) as res:
            raw = res.read()
            status = getattr(res, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail = json.loads(raw.decode("utf-8")).get("detail")
        except Exception:
            detail = raw.decode("utf-8", errors="replace")[:300]
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict", 422: "invalid_request"}.get(exc.code, "http_error")
        if isinstance(detail, dict) and detail.get("error"):
            code = str(detail["error"])
        raise VpsRunsError(code, f"{method} {path} -> {exc.code}", status=exc.code, detail=detail)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VpsRunsError("vps_unreachable", f"{method} {path} 失敗: {exc}")
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        raise VpsRunsError("invalid_response", f"{method} {path}: JSON ではない応答")
    if not isinstance(data, dict):
        raise VpsRunsError("invalid_response", f"{method} {path}: 想定外の応答")
    data["_status"] = status
    return data


def create_run(channel_key: str, vol: int, *, request_id: Optional[str] = None, intent: str = "pipeline",
               from_step: Optional[str] = None, input_revision: Optional[str] = None, **kw) -> dict[str, Any]:
    payload = {"request_id": request_id or new_request_id(), "channel_key": channel_key, "vol": int(vol),
               "workflow_version": workflow_version(), "intent": intent}
    if from_step:
        payload["from_step"] = from_step
    if input_revision:
        payload["input_revision"] = input_revision
    return request("POST", "/api/runs", payload=payload, **kw)


def get_run(run_id: int, **kw) -> dict[str, Any]:
    return request("GET", f"/api/runs/{int(run_id)}", **kw)


def list_runs(channel_key: Optional[str] = None, state: Optional[str] = None, limit: int = 20, **kw) -> dict[str, Any]:
    return request("GET", "/api/runs", query={"channel_key": channel_key, "state": state, "limit": limit}, **kw)


def resume_run(run_id: int, *, from_step: Optional[str] = None, **kw) -> dict[str, Any]:
    return request("POST", f"/api/runs/{int(run_id)}/resume", payload={"from_step": from_step} if from_step else {}, **kw)


def cancel_run(run_id: int, **kw) -> dict[str, Any]:
    return request("POST", f"/api/runs/{int(run_id)}/cancel", payload={}, **kw)

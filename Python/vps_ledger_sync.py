#!/usr/bin/env python3
"""Mac パイプラインと VPS の vol 台帳を同期する。"""
from __future__ import annotations

import argparse
import json
import os
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


CONFIG_PATH = Path("~/.config/automation-studio/worker.json").expanduser()
USER_AGENT = "AutomationStudioWorker/1.0"
TIMEOUT_SECONDS = 5
_VOL_CACHE: dict[tuple[str, int], int] = {}
_SHARED_MARKER = "/共有ドライブ/"


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _load_settings() -> Optional[dict[str, str]]:
    if (os.environ.get("APP_LEDGER_SYNC") or "").strip() == "0":
        return None
    data = _read_json(CONFIG_PATH, None)
    if not isinstance(data, dict) or data.get("ledger_sync") is False:
        return None
    base_url = data.get("base_url")
    worker_token = data.get("worker_token")
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    if not isinstance(worker_token, str) or not worker_token.strip():
        return None
    return {
        "base_url": base_url.rstrip("/"),
        "worker_token": worker_token.strip(),
    }


def _folder_key(value: str) -> str:
    """共有ドライブ配下は Mac のユーザー名に依存しない比較キーへ正規化する。"""
    text = unicodedata.normalize("NFC", str(value or "").strip())
    marker_index = text.find(_SHARED_MARKER)
    if marker_index >= 0:
        return "共有ドライブ/" + text[marker_index + len(_SHARED_MARKER):].rstrip("/")
    try:
        return unicodedata.normalize("NFC", str(Path(text).expanduser().resolve()))
    except Exception:
        return unicodedata.normalize("NFC", os.path.normpath(text))


def _channel_folder() -> str:
    folder = (os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    if folder:
        return folder
    try:
        from _app_config import resolve_config_dir

        dashboard = _read_json(resolve_config_dir() / "dashboard_config.json", {})
    except Exception:
        dashboard = {}
    if isinstance(dashboard, dict):
        return str(dashboard.get("channel_folder") or "").strip()
    return ""


def resolve_channel_key() -> Optional[str]:
    folder = _channel_folder()
    if not folder:
        return None
    try:
        from _app_config import resolve_shared_config_dir

        channels_path = resolve_shared_config_dir() / "channels.json"
    except Exception:
        return None
    channels = _read_json(channels_path, [])
    if not isinstance(channels, list):
        return None
    target = _folder_key(folder)
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        if _folder_key(str(channel.get("folder") or "")) == target:
            channel_key = channel.get("id")
            if isinstance(channel_key, str) and channel_key.strip():
                return channel_key.strip()
    return None


def _request(
    method: str,
    path: str,
    *,
    query: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    settings = _load_settings()
    if settings is None:
        return None
    url = settings["base_url"] + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    body = None
    headers = {
        "Authorization": f"Bearer {settings['worker_token']}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read()
        if not raw:
            return {}
        result = json.loads(raw.decode("utf-8"))
        return result if isinstance(result, dict) else None
    except Exception as exc:
        print(f"[ledger-sync] {method} {path} 失敗: {exc}")
        return None


def ensure_vol(vol: int) -> Optional[int]:
    settings = _load_settings()
    channel_key = resolve_channel_key()
    if settings is None or channel_key is None:
        return None
    cache_key = (channel_key, int(vol))
    if cache_key in _VOL_CACHE:
        return _VOL_CACHE[cache_key]
    result = _request(
        "GET",
        "/api/worker/vols/lookup",
        query={"channel_key": channel_key, "vol_number": int(vol), "create": "true"},
    )
    if not isinstance(result, dict):
        return None
    candidate = result.get("vol") if isinstance(result.get("vol"), dict) else result
    vol_id = candidate.get("id") if isinstance(candidate, dict) else None
    if isinstance(vol_id, bool) or not isinstance(vol_id, int):
        return None
    _VOL_CACHE[cache_key] = vol_id
    return vol_id


def report_step(vol: int, step: str, status: str, detail: str = "") -> None:
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return
    _request(
        "POST",
        f"/api/worker/vols/{vol_id}/steps",
        payload={"step": step, "status": status, "detail": detail, "source": "mac"},
    )


def push_context(vol: int, folder: Path) -> None:
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return
    try:
        from claude_proposer import gather_context

        context = gather_context(Path(folder))
        # VPS 側は publish_date を date 型で検証するため、空値はキーごと省く。
        # songs/tracklist_text も空なら送らない（既存 context を空で上書きしない）。
        payload: dict[str, Any] = {"folder_name": Path(folder).name}
        if context.get("publish_date"):
            payload["publish_date"] = context["publish_date"]
        if context.get("songs"):
            payload["songs"] = context["songs"]
        if context.get("tracklist_text"):
            payload["tracklist_text"] = context["tracklist_text"]
    except Exception as exc:
        print(f"[ledger-sync] context 収集失敗: {exc}")
        return
    _request("POST", f"/api/worker/vols/{vol_id}/context", payload=payload)


def push_meta(vol: int, title: str, description: str, tags: list[str]) -> None:
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return
    _request(
        "POST",
        f"/api/worker/vols/{vol_id}/meta",
        payload={
            "meta": {"title": title, "description": description, "tags": tags},
            "source": "mac",
        },
    )


def push_localizations(vol: int, localizations: dict) -> None:
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return
    _request(
        "POST",
        f"/api/worker/vols/{vol_id}/localizations",
        payload={"localizations": localizations, "source": "mac"},
    )


def report_upload(vol: int, folder: Path) -> None:
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return
    marker = _read_json(Path(folder) / "youtube_upload.json", None)
    if not isinstance(marker, dict):
        return
    _request(
        "POST",
        f"/api/worker/vols/{vol_id}/upload-result",
        payload={
            "video_id": marker.get("video_id"),
            # マーカーのキーは世代差がある: schedule(現行) / scheduled_publish_at / publishAt
            "scheduled_publish_at": (
                marker.get("schedule")
                or marker.get("scheduled_publish_at")
                or marker.get("publishAt")
            ),
            "published_at": marker.get("published_at"),
        },
    )


class LedgerHttpError(RuntimeError):
    """VPS が 4xx を返した（detail を持つ）。接続不能は LedgerUnavailable。"""
    def __init__(self, status: int, detail: Any):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class LedgerUnavailable(RuntimeError):
    """VPS に接続できない（待てば通る）。"""


class LedgerConfigError(RuntimeError):
    """設定の問題（チャンネル / vol を解決できない等）。待っても直らない。"""


def enabled() -> bool:
    """VPS 台帳同期が設定されているか（投稿の正本を VPS にするかの判定に使う）。"""
    return _load_settings() is not None


def _request_strict(method: str, path: str, *, payload: Optional[dict[str, Any]] = None,
                    timeout: int = 15) -> dict[str, Any]:
    """投稿記録用: 4xx は LedgerHttpError、接続不能は LedgerUnavailable（黙って None にしない）。"""
    settings = _load_settings()
    if settings is None:
        raise LedgerUnavailable("vps_not_configured")
    url = settings["base_url"] + path
    headers = {"Authorization": f"Bearer {settings['worker_token']}", "Accept": "application/json",
               "User-Agent": USER_AGENT, "Content-Type": "application/json"}
    body = json.dumps(payload or {}, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except Exception:
            detail = None
        raise LedgerHttpError(exc.code, detail) from None
    except Exception as exc:
        raise LedgerUnavailable(str(exc)) from None
    try:
        result = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        raise LedgerUnavailable("invalid JSON from VPS") from None
    return result if isinstance(result, dict) else {}


def allocate_vol(channel_key: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Mac の vol_create 用: VPS で次の vol 番号と公開枠を採番する。VPS 未設定なら None（従来の採番）。
    channel_key を渡さないときは環境（APP_CHANNEL_FOLDER）/ アクティブ設定から解決する。"""
    if not enabled():
        return None
    channel_key = channel_key or resolve_channel_key()
    if channel_key is None:
        raise LedgerConfigError("channels.json からチャンネル（channel_key）を解決できません")
    data = _request_strict("POST", "/api/worker/vols/allocate", payload={"channel_key": channel_key})
    if not isinstance(data.get("vol_number"), int) or isinstance(data.get("vol_number"), bool):
        raise LedgerUnavailable("allocate の応答に vol_number がありません")
    _VOL_CACHE[(channel_key, int(data["vol_number"]))] = int(data["id"]) if isinstance(data.get("id"), int) else _VOL_CACHE.get((channel_key, int(data["vol_number"])))
    return data


def upload_discard(vol: int, input_hash: str, begin_token: Optional[str]) -> Optional[dict[str, Any]]:
    """状態照会で未完了と確認したセッションを捨て、新規送信（insert）に戻す。"""
    vol_id = ensure_vol(vol)
    if vol_id is None:
        raise LedgerUnavailable("vol を VPS で解決できません")
    return _request_strict("POST", f"/api/worker/vols/{vol_id}/upload/discard",
                           payload={"input_hash": input_hash, "begin_token": begin_token})


def upload_failed(vol: int, input_hash: str, error: str) -> bool:
    """送信前 / 送信中の回復できない失敗（動画は作られていない）。failed にして再 begin できるようにする。"""
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return False
    try:
        _request_strict("POST", f"/api/worker/vols/{vol_id}/upload/failed",
                        payload={"input_hash": input_hash, "error": error[:2000]})
        return True
    except (LedgerHttpError, LedgerUnavailable) as exc:
        print(f"[ledger-sync] upload/failed 報告失敗: {exc}")
        return False


def upload_begin(vol: int, input_hash: str, begin_token: Optional[str] = None,
                 known_video_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """投稿の開始（冪等）。VPS 未設定なら None（従来経路）。4xx は LedgerHttpError、接続不能は LedgerUnavailable。
    応答の begin_token は同じ送信元の session 報告・再 begin に添える。"""
    if not enabled():
        return None
    if resolve_channel_key() is None:
        raise LedgerConfigError("channels.json からチャンネル（channel_key）を解決できません")
    vol_id = ensure_vol(vol)
    if vol_id is None:
        raise LedgerUnavailable("vol を VPS で解決できません")
    payload: dict[str, Any] = {"input_hash": input_hash}
    if begin_token:
        payload["begin_token"] = begin_token
    if known_video_id:
        payload["known_video_id"] = known_video_id
    return _request_strict("POST", f"/api/worker/vols/{vol_id}/upload/begin", payload=payload)


def upload_session(vol: int, input_hash: str, session_ref: str, offset: int, begin_token: Optional[str] = None) -> bool:
    """送信中のセッション URI と offset を報告する（15 分の引き継ぎ期限も延びる）。保存できたか返す。"""
    vol_id = _VOL_CACHE.get((resolve_channel_key() or "", int(vol))) or ensure_vol(vol)
    if vol_id is None:
        return False
    try:
        _request_strict("POST", f"/api/worker/vols/{vol_id}/upload/session",
                        payload={"input_hash": input_hash, "session_ref": session_ref, "offset": int(offset),
                                 "begin_token": begin_token})
        return True
    except (LedgerHttpError, LedgerUnavailable) as exc:
        print(f"[ledger-sync] upload/session 保存失敗: {exc}")
        return False


def upload_result(vol: int, input_hash: str, video_id: str, privacy: str, publish_at: Optional[str]) -> bool:
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return False
    try:
        _request_strict("POST", f"/api/worker/vols/{vol_id}/upload/result",
                        payload={"input_hash": input_hash, "video_id": video_id, "privacy": privacy, "publish_at": publish_at})
        return True
    except (LedgerHttpError, LedgerUnavailable) as exc:
        print(f"[ledger-sync] upload/result 報告失敗: {exc}")
        return False


def upload_unknown(vol: int, input_hash: str, error: str) -> bool:
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return False
    try:
        _request_strict("POST", f"/api/worker/vols/{vol_id}/upload/unknown",
                        payload={"input_hash": input_hash, "error": error[:2000]})
        return True
    except (LedgerHttpError, LedgerUnavailable) as exc:
        print(f"[ledger-sync] upload/unknown 報告失敗: {exc}")
        return False


def post_notify(events: list[dict[str, Any]]) -> dict[str, Any]:
    """通知 event を VPS の outbox に積む（段階 3a）。未設定なら LedgerUnavailable。到達不能 / 4xx は例外。"""
    if not enabled():
        raise LedgerUnavailable("vps_not_configured")
    payload = [{k: v for k, v in e.items() if v is not None} for e in events]
    return _request_strict("POST", "/api/worker/notify", payload={"events": payload}, timeout=5)


def fetch_vol(vol: int) -> Optional[dict]:
    vol_id = ensure_vol(vol)
    if vol_id is None:
        return None
    return _request("GET", f"/api/worker/vols/{vol_id}")


def _status() -> None:
    settings = _load_settings()
    print(f"ledger_sync: {'enabled' if settings else 'disabled'}")
    print(f"config: {CONFIG_PATH}")
    print(f"base_url: {settings['base_url'] if settings else '-'}")
    print(f"channel_folder: {_channel_folder() or '-'}")
    print(f"channel_key: {resolve_channel_key() or '-'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="VPS vol 台帳同期の設定確認")
    parser.add_argument("--status", action="store_true", help="設定とチャンネル解決結果を表示")
    args = parser.parse_args()
    if args.status:
        _status()
        return
    parser.print_help()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Google Photos Picker API から選択済みメディアをダウンロードする CLI。"""

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
SCOPES = [SCOPE]
API_BASE = "https://photospicker.googleapis.com/v1"
CONFIG_DIR = Path.home() / ".config" / "orzz"
CLIENT_SECRET = CONFIG_DIR / "youtube_client_secret.json"
TOKEN_FILE = CONFIG_DIR / "photos_picker_token.json"
REQUEST_TIMEOUT_SEC = 60


class PickerError(Exception):
    """ユーザー向けメッセージを持つ Picker CLI エラー。"""


class AuthenticationRequiredError(PickerError):
    """保存済み OAuth トークンの再認証が必要。"""


class Output:
    def __init__(self, json_mode: bool) -> None:
        self.json_mode = json_mode

    def info(self, message: str) -> None:
        print(message, file=sys.stderr if self.json_mode else sys.stdout, flush=True)

    def error(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    def result(self, payload: Dict[str, Any]) -> None:
        if self.json_mode:
            print(json.dumps(payload, ensure_ascii=False), flush=True)


def save_credentials_atomic(path: Path, credentials: Credentials) -> None:
    """OAuth トークンを対象ファイルだけに原子的に保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, str(path))
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def get_credentials(output: Output) -> Credentials:
    """Picker 専用トークンを読み込み、必要なら更新または OAuth 同意を行う。"""
    credentials = None
    if TOKEN_FILE.exists():
        try:
            credentials = Credentials.from_authorized_user_file(
                str(TOKEN_FILE), SCOPES
            )
        except Exception as exc:
            output.error(
                "Photos Picker トークンを読み込めませんでした。"
                "再認証を開始します: {}".format(exc)
            )

    has_scope = bool(credentials and credentials.has_scopes(SCOPES))
    if credentials and credentials.valid and has_scope:
        return credentials

    if (
        credentials
        and credentials.expired
        and credentials.refresh_token
        and has_scope
    ):
        try:
            credentials.refresh(Request())
            save_credentials_atomic(TOKEN_FILE, credentials)
            return credentials
        except RefreshError as exc:
            raise AuthenticationRequiredError(
                "Photos Picker のトークン更新に失敗しました。"
                "{} を削除せずに確認し、再認証してください: {}".format(
                    TOKEN_FILE, exc
                )
            )

    if not CLIENT_SECRET.exists():
        raise PickerError(
            "OAuth クライアントシークレットが見つかりません: {}".format(
                CLIENT_SECRET
            )
        )

    output.info("Google Photos Picker の OAuth 同意画面を開きます。")
    output.info("Picker 専用トークン保存先: {}".format(TOKEN_FILE))
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    redirect = (
        contextlib.redirect_stdout(sys.stderr)
        if output.json_mode
        else contextlib.nullcontext()
    )
    with redirect:
        credentials = flow.run_local_server(port=0)
    save_credentials_atomic(TOKEN_FILE, credentials)
    output.info("Photos Picker の認証が完了しました。")
    return credentials


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from iter_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_strings(child)


def extract_help_urls(payload: Any) -> List[str]:
    urls = []
    seen = set()
    for value in iter_strings(payload):
        if value.startswith(("https://", "http://")) and value not in seen:
            seen.add(value)
            urls.append(value)
    return urls


def extract_error_reason(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    details = error.get("details")
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        reason = detail.get("reason")
        if reason:
            return str(reason)
        metadata = detail.get("metadata")
        if isinstance(metadata, dict) and metadata.get("reason"):
            return str(metadata["reason"])
    return ""


def api_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = {}

    if response.status_code == 401:
        return (
            "Photos Picker API が 401 を返しました。トークンは自動削除していません。"
            "{} を確認し、必要なら手動で削除して再認証してください。".format(
                TOKEN_FILE
            )
        )

    reason = extract_error_reason(payload)
    urls = extract_help_urls(payload)
    if response.status_code == 403 and (
        reason == "SERVICE_DISABLED" or urls
    ):
        lines = [
            "Google Photos Picker API が GCP プロジェクトで有効化されていません。"
            "Google Cloud Console で API を有効化してから再実行してください。"
        ]
        if urls:
            lines.append("有効化・ヘルプ URL:")
            lines.extend(urls)
        return "\n".join(lines)

    message = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
    suffix = ": {}".format(message) if message else ""
    return "Photos Picker API エラー (HTTP {}){}".format(
        response.status_code, suffix
    )


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    **kwargs: Any
) -> Dict[str, Any]:
    try:
        response = session.request(
            method, url, timeout=REQUEST_TIMEOUT_SEC, **kwargs
        )
    except requests.RequestException as exc:
        raise PickerError("Photos Picker API への接続に失敗しました: {}".format(exc))

    if response.status_code == 401:
        raise AuthenticationRequiredError(api_error_message(response))
    if not response.ok:
        raise PickerError(api_error_message(response))
    if response.status_code == 204 or not response.content:
        return {}
    try:
        payload = response.json()
    except ValueError:
        raise PickerError("Photos Picker API が不正な JSON を返しました。")
    if not isinstance(payload, dict):
        raise PickerError("Photos Picker API のレスポンス形式が不正です。")
    return payload


def parse_duration_seconds(value: Any, fallback: float) -> float:
    if not isinstance(value, str):
        return fallback
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", value.strip())
    if not match:
        return fallback
    parsed = float(match.group(1))
    return parsed if parsed > 0 else fallback


def open_picker_uri(uri: str, output: Output) -> None:
    output.info("Google フォトで素材を選択してください:")
    output.info(uri)
    if sys.platform != "darwin":
        return
    try:
        result = subprocess.run(
            ["open", uri],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            output.info("ブラウザを自動で開けませんでした。上の URL を開いてください。")
    except OSError:
        output.info("ブラウザを自動で開けませんでした。上の URL を開いてください。")


def wait_for_selection(
    api_session: requests.Session,
    session_id: str,
    initial_payload: Dict[str, Any],
    timeout_sec: int,
    fallback_poll_sec: float,
) -> Dict[str, Any]:
    payload = initial_payload
    deadline = time.monotonic() + timeout_sec
    while not payload.get("mediaItemsSet"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PickerError(
                "写真・アルバムの選択待ちが {} 秒でタイムアウトしました。".format(
                    timeout_sec
                )
            )
        polling_config = payload.get("pollingConfig")
        recommended = (
            polling_config.get("pollInterval")
            if isinstance(polling_config, dict)
            else None
        )
        interval = parse_duration_seconds(recommended, fallback_poll_sec)
        time.sleep(min(interval, remaining))
        payload = request_json(
            api_session,
            "GET",
            "{}/sessions/{}".format(API_BASE, session_id),
        )
    return payload


def list_media_items(
    api_session: requests.Session, session_id: str
) -> List[Dict[str, Any]]:
    items = []
    page_token = None
    while True:
        params = {"sessionId": session_id}
        if page_token:
            params["pageToken"] = page_token
        payload = request_json(
            api_session, "GET", "{}/mediaItems".format(API_BASE), params=params
        )
        page_items = payload.get("mediaItems") or []
        if not isinstance(page_items, list):
            raise PickerError("mediaItems のレスポンス形式が不正です。")
        items.extend(item for item in page_items if isinstance(item, dict))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return items


def safe_filename(item: Dict[str, Any]) -> str:
    media_file = item.get("mediaFile")
    filename = media_file.get("filename") if isinstance(media_file, dict) else None
    candidate = str(filename or item.get("id") or "media")
    candidate = Path(candidate.replace("\x00", "")).name
    return candidate if candidate not in ("", ".", "..") else "media"


def available_path(destination: Path, filename: str) -> Path:
    original = Path(filename)
    stem = original.stem or "media"
    suffix = original.suffix
    candidate = destination / filename
    number = 2
    while candidate.exists() or candidate.with_name(candidate.name + ".part").exists():
        candidate = destination / "{}_{}{}".format(stem, number, suffix)
        number += 1
    return candidate


def creation_timestamp(item: Dict[str, Any]) -> Optional[float]:
    value = item.get("createTime")
    if not value:
        metadata = item.get("mediaMetadata")
        if isinstance(metadata, dict):
            value = metadata.get("creationTime")
    if not isinstance(value, str) or not value:
        return None
    normalized = value.strip()
    match = re.match(r"^(.*\.\d{6})\d+(Z|[+-]\d\d:\d\d)$", normalized)
    if match:
        normalized = match.group(1) + match.group(2)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def download_item(
    api_session: requests.Session,
    item: Dict[str, Any],
    destination: Path,
) -> Path:
    media_file = item.get("mediaFile")
    if not isinstance(media_file, dict):
        raise PickerError("メディア情報に mediaFile がありません。")
    base_url = media_file.get("baseUrl")
    if not isinstance(base_url, str) or not base_url:
        raise PickerError("メディア情報に baseUrl がありません。")

    media_type = str(item.get("type") or "").upper()
    mime_type = str(media_file.get("mimeType") or "").lower()
    parameter = "=dv" if media_type == "VIDEO" or mime_type.startswith("video/") else "=d"
    target = available_path(destination, safe_filename(item))
    partial = target.with_name(target.name + ".part")

    try:
        response = api_session.get(
            base_url + parameter,
            timeout=REQUEST_TIMEOUT_SEC,
            stream=True,
        )
        if response.status_code == 401:
            raise AuthenticationRequiredError(api_error_message(response))
        if not response.ok:
            raise PickerError(
                "ダウンロードに失敗しました (HTTP {})".format(
                    response.status_code
                )
            )
        with partial.open("xb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
        os.replace(str(partial), str(target))
        timestamp = creation_timestamp(item)
        if timestamp is not None:
            os.utime(str(target), (timestamp, timestamp))
        return target
    except requests.RequestException as exc:
        raise PickerError("メディアのダウンロード接続に失敗しました: {}".format(exc))
    finally:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Google Photos Picker API で選択した素材をダウンロードします。"
    )
    parser.add_argument(
        "--dest",
        type=Path,
        help="選択した素材のダウンロード先。--auth-only 以外では必須",
    )
    parser.add_argument(
        "--timeout-sec",
        type=int,
        default=600,
        help="ユーザーの選択待ち上限秒数 (既定: 600)",
    )
    parser.add_argument(
        "--poll-sec",
        type=float,
        default=5.0,
        help="ポーリング間隔秒数。API 推奨値があればそちらを優先 (既定: 5)",
    )
    parser.add_argument(
        "--auth-only", action="store_true", help="認証だけ行って終了"
    )
    parser.add_argument(
        "--json", action="store_true", help="結果を JSON で標準出力"
    )
    return parser


def run(args: argparse.Namespace, output: Output) -> int:
    if not args.auth_only and args.dest is None:
        raise PickerError("--dest は必須です。")
    if args.timeout_sec <= 0:
        raise PickerError("--timeout-sec は 1 以上を指定してください。")
    if args.poll_sec <= 0:
        raise PickerError("--poll-sec は 0 より大きい値を指定してください。")

    credentials = get_credentials(output)
    if args.auth_only:
        output.result(
            {
                "authenticated": True,
                "token_file": str(TOKEN_FILE),
            }
        )
        if not args.json:
            output.info("認証のみ完了しました。")
        return 0

    destination = args.dest.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    api_session = requests.Session()
    api_session.headers.update(
        {"Authorization": "Bearer {}".format(credentials.token)}
    )

    picker_session_id = None
    downloaded = []
    failures = []
    try:
        picker_session = request_json(
            api_session, "POST", "{}/sessions".format(API_BASE), json={}
        )
        picker_session_id = picker_session.get("id")
        picker_uri = picker_session.get("pickerUri")
        if not picker_session_id or not picker_uri:
            raise PickerError("Picker セッションの id または pickerUri がありません。")
        open_picker_uri(str(picker_uri), output)
        wait_for_selection(
            api_session,
            str(picker_session_id),
            picker_session,
            args.timeout_sec,
            args.poll_sec,
        )
        items = list_media_items(api_session, str(picker_session_id))
        for item in items:
            try:
                target = download_item(api_session, item, destination)
                downloaded.append(target.name)
                output.info("保存: {}".format(target))
            except AuthenticationRequiredError:
                raise
            except PickerError as exc:
                failures.append(
                    {
                        "item": safe_filename(item),
                        "error": str(exc),
                    }
                )
                output.error(
                    "取得失敗: {}: {}".format(safe_filename(item), exc)
                )
    finally:
        if picker_session_id:
            try:
                request_json(
                    api_session,
                    "DELETE",
                    "{}/sessions/{}".format(API_BASE, picker_session_id),
                )
            except PickerError as exc:
                output.error("Picker セッションの削除に失敗しました: {}".format(exc))

    result = {
        "destination": str(destination),
        "downloaded_count": len(downloaded),
        "failed_count": len(failures),
        "files": downloaded,
        "failures": failures,
    }
    output.result(result)
    if not args.json:
        output.info("保存先: {}".format(destination))
        output.info(
            "取得件数: {}、失敗件数: {}".format(
                len(downloaded), len(failures)
            )
        )
    return 0 if downloaded else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    output = Output(args.json)
    try:
        return run(args, output)
    except PickerError as exc:
        output.error("エラー: {}".format(exc))
        if args.json:
            output.result({"error": str(exc)})
        return 1
    except KeyboardInterrupt:
        output.error("中断しました。")
        if args.json:
            output.result({"error": "interrupted"})
        return 130


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Automation Studio dispatcher.

AI / 人間の正規入口を routes.json に集約し、vol 解決・チャンネルガード・
via-api ガードを通してから既存 CLI/API へ委譲する。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app_core import (  # noqa: E402
    PYTHON_DIR,
    get_active_channel_info,
    get_channels,
    load_routes_table,
    resolve_vol_folder,
    _norm_folder,
)
import app_updater  # noqa: E402
import app_youtube  # noqa: E402
import settings_service  # noqa: E402
import app_config_backup  # noqa: E402

BASE_URL = "http://localhost:8888"
STUDIO_ROOT = Path(__file__).resolve().parent.parent


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def http_json(method: str, path: str, body: Any | None = None, timeout: int = 8) -> dict[str, Any]:
    data = None
    headers = {}
    if body is not None and method.upper() not in {"GET", "DELETE"}:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        headers=headers,
        method=method.upper(),
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def fill_value(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return value.format(**ctx)
        except KeyError:
            return value
    if isinstance(value, list):
        return [fill_value(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: fill_value(v, ctx) for k, v in value.items()}
    return value


def resolve_vol(num: str, channel_id: str = "") -> dict[str, Any]:
    if not channel_id:
        try:
            return http_json("GET", f"/api/resolve-vol/{num}", timeout=5)
        except Exception:
            pass
    elif channel_id:
        try:
            return http_json("GET", f"/api/resolve-vol/{num}?channel_id={channel_id}", timeout=5)
        except Exception:
            pass
    return resolve_vol_folder(num, channel_id=channel_id)


def print_list(routes: dict[str, Any]) -> None:
    for name, route in sorted((routes.get("intents") or {}).items()):
        aliases = ", ".join(route.get("aliases") or [])
        safe = "safe" if route.get("via_api_safe", True) else "cli-only"
        par = route.get("parallelism") or {}
        print(f"{name:22} {safe:8} {par.get('scope','-')}:{par.get('max_parallel','-')}  {route.get('description','')}")
        if aliases:
            print(f"{'':22} aliases: {aliases}")
    emit({"ok": True, "intent": "list", "count": len(routes.get("intents") or {})})


def print_explain(routes: dict[str, Any], intent: str) -> int:
    route = (routes.get("intents") or {}).get(intent)
    if not route:
        print(f"未知の intent です: {intent}", file=sys.stderr)
        emit({"ok": False, "intent": intent, "error": "unknown_intent"})
        return 2
    print(json.dumps(route, ensure_ascii=False, indent=2))
    emit({"ok": True, "intent": intent, "route": route})
    return 0


def switch_channel(channel_id: str) -> bool:
    try:
        http_json("PUT", f"/api/channels/active/{channel_id}", timeout=10)
        return True
    except Exception as e:
        print(f"チャンネル切替に失敗しました: {e}", file=sys.stderr)
        return False


def ensure_channel_guard(args: argparse.Namespace, resolved: dict[str, Any] | None) -> int:
    active = get_active_channel_info()
    print(f"active channel: {active.get('name') or '-'} ({active.get('id') or '-'}) {active.get('folder') or ''}")

    if args.channel and args.channel != active.get("id"):
        if not args.switch:
            print(
                f"指定 channel={args.channel} が active={active.get('id') or '-'} と違います。"
                "実行するなら --switch を明示してください。",
                file=sys.stderr,
            )
            emit({"ok": False, "intent": args.intent, "error": "channel_mismatch", "active_channel": active, "requested_channel": args.channel})
            return 3
        if not switch_channel(args.channel):
            emit({"ok": False, "intent": args.intent, "error": "channel_switch_failed", "requested_channel": args.channel})
            return 3
        active = get_active_channel_info()
        print(f"switched active channel: {active.get('name') or '-'} ({active.get('id') or '-'}) {active.get('folder') or ''}")

    if resolved and resolved.get("folder") and active.get("folder"):
        video_parent = str(Path(resolved["folder"]).parent)
        if _norm_folder(video_parent) != _norm_folder(active["folder"]):
            print(
                f"解決した video_name が active channel 配下ではありません: {resolved.get('folder')}",
                file=sys.stderr,
            )
            emit({"ok": False, "intent": args.intent, "error": "video_channel_mismatch", "video": resolved, "active_channel": active})
            return 3
    return 0


def build_api(route: dict[str, Any], ctx: dict[str, Any]) -> tuple[str, str, Any]:
    api = route.get("api") or {}
    method = api.get("method", "GET")
    path = fill_value(api.get("path", ""), ctx)
    body = fill_value(api.get("body", {}), ctx)
    return method, path, body


def curl_preview(method: str, path: str, body: Any) -> list[str]:
    cmd = ["curl", "-s", "-X", method.upper(), BASE_URL + path]
    if method.upper() not in {"GET", "DELETE"}:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body, ensure_ascii=False)]
    return cmd


def build_cli(route: dict[str, Any], ctx: dict[str, Any], args: argparse.Namespace) -> list[str]:
    cmd = fill_value((route.get("cli") or {}).get("cmd", []), ctx)
    if args.intent == "pipeline" and args.from_step:
        cmd += ["--from", args.from_step]
    if args.via_api:
        cmd += ["--via-api"]
    return cmd


def run_api(method: str, path: str, body: Any) -> int:
    try:
        result = http_json(method, path, body=body, timeout=30)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"API error {e.code}: {detail}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"API error: {e}", file=sys.stderr)
        return 1


def channel_onboard(args: argparse.Namespace) -> int:
    if not args.channel:
        print("channel-onboard には --channel <id> が必要です", file=sys.stderr)
        emit({"ok": False, "intent": "channel-onboard", "error": "missing_channel"})
        return 2
    channel_id = args.channel
    if args.status:
        try:
            data = http_json("GET", f"/api/channels/{channel_id}/onboarding", timeout=10)
        except Exception as e:
            print(f"onboarding status 取得失敗: {e}", file=sys.stderr)
            emit({"ok": False, "intent": "channel-onboard", "error": "api_error", "detail": str(e)})
            return 1
        ch = data.get("channel") or {}
        steps = ((data.get("onboarding") or {}).get("steps") or {})
        print(f"channel: {ch.get('name') or channel_id} ({channel_id})")
        for key in data.get("order") or []:
            entry = steps.get(key) or {}
            mark = "✓" if entry.get("done") else "□"
            print(f"{mark} {key:16} {entry.get('desc','')}")
        emit({"ok": True, "intent": "channel-onboard", "channel": channel_id, "remaining": data.get("remaining", []), "next_step": data.get("next_step", "")})
        return 0

    try:
        data = http_json("GET", f"/api/channels/{channel_id}/onboarding", timeout=10)
    except Exception as e:
        print(f"onboarding 取得失敗: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "channel-onboard", "error": "api_error", "detail": str(e)})
        return 1
    step = args.step or data.get("next_step")
    if not step:
        print("オンボーディングは完了しています")
        emit({"ok": True, "intent": "channel-onboard", "channel": channel_id, "completed": True})
        return 0
    body: dict[str, Any] = {}
    if args.urls:
        body["urls"] = [u.strip() for u in args.urls.split(",") if u.strip()]
    if args.dry_run:
        body["dry_run"] = True
    print(f"実行 step: {step}")
    try:
        result = http_json("POST", f"/api/channels/{channel_id}/onboarding/{step}", body=body, timeout=10)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"onboarding API error {e.code}: {detail}", file=sys.stderr)
        emit({"ok": False, "intent": "channel-onboard", "channel": channel_id, "step": step, "error": "api_error", "detail": detail})
        return 1
    except Exception as e:
        print(f"onboarding API error: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "channel-onboard", "channel": channel_id, "step": step, "error": "api_error", "detail": str(e)})
        return 1
    action = result.get("action") or ""
    next_action = result.get("next_action") or ""
    if action == "needs_human":
        print("人間の操作が必要です")
        print(next_action)
    elif action == "started":
        print("バックグラウンドタスクを開始しました。/api/status/all または /api/benchmark/status を確認してください。")
    else:
        print(f"step 完了/処理結果: {action or 'ok'}")
    emit({
        "ok": True,
        "intent": "channel-onboard",
        "channel": channel_id,
        "step": step,
        "action": action,
        "next_action": next_action,
        "remaining": result.get("remaining", []),
        "next_step": result.get("next_step", ""),
    })
    return 0


def initial_setup(args: argparse.Namespace) -> int:
    body = {
        "system_name": args.system_name or "Automation Studio",
        "discord_webhook_url": args.discord_webhook or "",
        "discord_username": args.discord_username or "Automation Studio",
    }
    if args.dry_run:
        print("初回セットアップ確認:")
        print("curl -s http://localhost:8888/api/setup/initial")
        print("保存する場合:")
        print("curl -s -X POST http://localhost:8888/api/setup/initial -H 'Content-Type: application/json' -d " + json.dumps(body, ensure_ascii=False))
        emit({"ok": True, "intent": "setup", "dry_run": True, "body": body})
        return 0
    try:
        if body["discord_webhook_url"] or args.system_name:
            data = http_json("POST", "/api/setup/initial", body=body, timeout=10)
        else:
            data = http_json("GET", "/api/setup/initial", timeout=10)
    except Exception as e:
        print(f"初回セットアップ API に接続できません: {e}", file=sys.stderr)
        print("先に `bash Python/start.sh` で Automation Studio を起動してください。")
        emit({"ok": False, "intent": "setup", "error": "api_unavailable", "detail": str(e)})
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if data.get("needs_setup"):
        print("次の操作: Web UI の「新規チャンネル(かんたん作成)」でチャンネルを登録してください。")
    emit({"ok": True, "intent": "setup", "needs_setup": data.get("needs_setup"), "channels_count": data.get("channels_count", 0)})
    return 0


def run_update(args: argparse.Namespace) -> int:
    try:
        if args.rollback:
            data = app_updater.rollback(STUDIO_ROOT)
        elif args.from_zip:
            data = app_updater.apply_from_zip(args.from_zip, STUDIO_ROOT)
        elif args.check:
            data = app_updater.check_for_update(STUDIO_ROOT)
        else:
            data = app_updater.update(STUDIO_ROOT)
    except Exception as e:
        data = {"ok": False, "intent": "update", "message": str(e)}
    print(json.dumps(data, ensure_ascii=False, indent=2))
    if data.get("ok") and (data.get("backup") or data.get("restored_version")):
        print("次の操作: bash Python/start.sh で再起動してください。")
    emit({"ok": bool(data.get("ok")), "intent": "update", **data})
    return 0 if data.get("ok") else 1


def run_config(args: argparse.Namespace) -> int:
    sub = args.resolve_num or args.config_action
    key = args.config_action if args.resolve_num else args.config_key
    value = args.config_key if args.resolve_num else args.config_value
    try:
        if sub == "search":
            rows = settings_service.search_settings(key or "")
            for item in rows:
                scope = item.get("scope", "")
                tier = item.get("tier", "")
                print(f"{item.get('key')}\t{scope}/{tier}\t{item.get('label_ja','')}\t{item.get('description_ja','')}")
            emit({"ok": True, "intent": "config", "action": "search", "count": len(rows)})
            return 0
        if sub == "get":
            if not key:
                print("config get には key が必要です", file=sys.stderr)
                return 2
            data = settings_service.config_get(key, channel_id=args.channel or "")
            print(json.dumps({"key": data["key"], "value": data["value"], "path": data["path"]},
                             ensure_ascii=False, indent=2))
            emit({"ok": True, "intent": "config", "action": "get", "key": data["key"], "value": data["value"]})
            return 0
        if sub == "set":
            if not key or value is None:
                print("config set には key と value が必要です", file=sys.stderr)
                return 2
            data = settings_service.config_set(
                key, value,
                channel_id=args.channel or "",
                actor="ai",
            )
            print(json.dumps(data, ensure_ascii=False, indent=2))
            emit({"ok": True, "intent": "config", "action": "set", "key": data["key"], "path": data["path"]})
            return 0
    except Exception as e:
        print(f"config error: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "config", "action": sub, "error": str(e)})
        return 1
    print("config subcommand は search/get/set のいずれかです", file=sys.stderr)
    return 2


def run_autopilot(args: argparse.Namespace) -> int:
    if args.resume_all:
        try:
            data = http_json("POST", "/api/autopilot/resume-all", body={}, timeout=10)
        except Exception as e:
            print(f"autopilot resume-all API に接続できません: {e}", file=sys.stderr)
            emit({"ok": False, "intent": "autopilot", "error": "api_unavailable", "detail": str(e)})
            return 1
        print(json.dumps(data, ensure_ascii=False, indent=2))
        emit({"ok": True, "intent": "autopilot", "action": "resume-all", "count": data.get("count", 0)})
        return 0
    try:
        data = http_json("GET", "/api/workers/status", timeout=10)
    except Exception as e:
        print(f"autopilot status API に接続できません: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "autopilot", "error": "api_unavailable", "detail": str(e)})
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    emit({"ok": True, "intent": "autopilot", "action": "status"})
    return 0


def run_image_modules(args: argparse.Namespace) -> int:
    channel_id = args.channel or "active"
    body: dict[str, Any] = {}
    if args.set_module:
        selection = {}
        for item in args.set_module:
            if "=" not in item:
                print("--set は section=module_id 形式です", file=sys.stderr)
                return 2
            k, v = item.split("=", 1)
            selection[k.strip()] = v.strip()
        body["selection"] = selection
    if args.override:
        overrides = {}
        for item in args.override:
            if "=" not in item:
                print("--override は section=text 形式です", file=sys.stderr)
                return 2
            k, v = item.split("=", 1)
            overrides[k.strip()] = v.strip()
        body["overrides"] = overrides
    if args.add_module:
        if len(args.add_module) < 3:
            print("--add-module は section name text の3引数です", file=sys.stderr)
            return 2
        body["add_module"] = {"section": args.add_module[0], "name": args.add_module[1], "text": " ".join(args.add_module[2:])}
    method = "PUT" if body else "GET"
    path = f"/api/image-modules/{channel_id}"
    cmd = curl_preview(method, path, body if body else {})
    print(" ".join(cmd))
    if args.dry_run:
        emit({"ok": True, "intent": "image-modules", "channel": channel_id, "cmd": cmd, "body": body, "dry_run": True})
        return 0
    try:
        data = http_json(method, path, body=body if body else None, timeout=30)
    except Exception as e:
        print(f"image-modules API に接続できません: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "image-modules", "error": "api_unavailable", "detail": str(e)})
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    emit({"ok": True, "intent": "image-modules", "channel": channel_id, "path": data.get("path", "")})
    return 0


def run_digest(args: argparse.Namespace) -> int:
    if args.dry_run:
        print("curl -s -X POST http://localhost:8888/api/digest/send-now")
        emit({"ok": True, "intent": "digest", "dry_run": True, "cmd": ["curl", "-s", "-X", "POST", BASE_URL + "/api/digest/send-now"]})
        return 0
    try:
        data = http_json("POST", "/api/digest/send-now", body={}, timeout=30)
    except Exception as e:
        print(f"digest API に接続できません: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "digest", "error": "api_unavailable", "detail": str(e)})
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    emit({"ok": True, "intent": "digest", "sent": bool((data.get("discord") or {}).get("sent"))})
    return 0


def run_config_backup(args: argparse.Namespace) -> int:
    try:
        if args.list:
            data = app_config_backup.list_backups()
            action = "list"
        elif args.restore:
            data = app_config_backup.restore_backup(args.restore, actor="ai")
            action = "restore"
        else:
            data = app_config_backup.create_backup(actor="ai")
            action = "create"
    except Exception as e:
        print(f"config-backup error: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "config-backup", "error": str(e)})
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    emit({"ok": True, "intent": "config-backup", "action": action, **data})
    return 0


def run_youtube_disconnect(args: argparse.Namespace) -> int:
    if not args.channel:
        print("youtube-disconnect には --channel <id> が必要です", file=sys.stderr)
        emit({"ok": False, "intent": "youtube-disconnect", "error": "missing_channel"})
        return 2
    body = {"confirm": bool(args.confirm), "dry_run": bool(args.dry_run), "revoke": bool(args.revoke)}
    path = f"/api/channels/{args.channel}/youtube-disconnect"
    if args.dry_run:
        body["confirm"] = True
    if not body["confirm"]:
        print("実行には --confirm が必要です。確認だけなら --dry-run を付けてください。", file=sys.stderr)
        emit({"ok": False, "intent": "youtube-disconnect", "error": "missing_confirm"})
        return 2
    try:
        data = http_json("POST", path, body=body, timeout=30)
    except Exception as e:
        print(f"youtube-disconnect API に接続できません: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "youtube-disconnect", "error": "api_unavailable", "detail": str(e)})
        return 1
    print(json.dumps(data, ensure_ascii=False, indent=2))
    emit({"ok": True, "intent": "youtube-disconnect", "channel": args.channel, "dry_run": body["dry_run"], "deleted": len(data.get("deleted") or [])})
    return 0


def _find_channel(channel_id: str) -> dict[str, Any] | None:
    key = (channel_id or "").strip()
    for ch in get_channels() or []:
        candidates = {ch.get("id"), ch.get("youtube_channel_id"), ch.get("prefix"), ch.get("handle")}
        if key and key in candidates:
            return ch
    return None


def run_channel_auth(args: argparse.Namespace) -> int:
    if not args.channel:
        print("channel-auth には --channel <id> が必要です", file=sys.stderr)
        emit({"ok": False, "intent": "channel-auth", "error": "missing_channel"})
        return 2
    ch = _find_channel(args.channel)
    if not ch:
        print(f"チャンネルが見つかりません: {args.channel}", file=sys.stderr)
        emit({"ok": False, "intent": "channel-auth", "error": "channel_not_found", "channel": args.channel})
        return 2
    folder = Path(ch.get("folder") or "").expanduser()
    token_path = folder / ".youtube_token.json"
    scopes = list(app_youtube.SCOPES)
    print("YouTube チャンネル別再認証")
    print(f"channel: {ch.get('name') or args.channel} ({ch.get('id') or ch.get('youtube_channel_id') or args.channel})")
    print(f"token: {token_path}")
    print("scopes:")
    for scope in scopes:
        print(f"  - {scope}")
    print("認証時はブラウザで『ブランドアカウントとして対象チャンネルを選択してください』。")
    if args.dry_run or args.print_url:
        emit({
            "ok": True,
            "intent": "channel-auth",
            "channel": args.channel,
            "token_path": str(token_path),
            "scopes": scopes,
            "dry_run": bool(args.dry_run),
            "print_url": bool(args.print_url),
        })
        return 0
    try:
        app_youtube.reauth_channel_credentials(folder)
    except Exception as e:
        print(f"channel-auth error: {e}", file=sys.stderr)
        emit({"ok": False, "intent": "channel-auth", "channel": args.channel, "error": str(e)})
        return 1
    emit({"ok": True, "intent": "channel-auth", "channel": args.channel, "token_path": str(token_path)})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Automation Studio 正規ディスパッチャ")
    parser.add_argument("intent", nargs="?", help="intent 名。resolve の場合はサブコマンドとして扱う")
    parser.add_argument("resolve_num", nargs="?", help="resolve サブコマンド用 vol 番号")
    parser.add_argument("--list", action="store_true", help="intent 一覧")
    parser.add_argument("--explain", metavar="INTENT", help="intent の詳細")
    parser.add_argument("--vol", help="vol 番号")
    parser.add_argument("--channel", help="チャンネル id")
    parser.add_argument("--switch", action="store_true", help="--channel が active と違う場合に明示切替")
    parser.add_argument("--via-api", action="store_true", help="API 経由で実行")
    parser.add_argument("--dry-run", action="store_true", help="実行せず解決後のコマンドだけ表示")
    parser.add_argument("--from", dest="from_step", help="pipeline の再開 step")
    parser.add_argument("--publish-date", default="YYYY-MM-DD")
    parser.add_argument("--prompt", default="lounge jazz BGM, elegant cafe atmosphere")
    parser.add_argument("--count", default="20")
    parser.add_argument("--interval", default="40")
    parser.add_argument("--privacy", default="unlisted")
    parser.add_argument("--stream-id", default="{stream_id}")
    parser.add_argument("--host", default="{host}")
    parser.add_argument("--title", default="{title}")
    parser.add_argument("--tags", default="{tags}")
    parser.add_argument("--step", help="channel-onboard の個別 step")
    parser.add_argument("--status", action="store_true", help="channel-onboard の残タスク表示")
    parser.add_argument("--urls", help="channel-onboard benchmark_set 用URL（カンマ区切り）")
    parser.add_argument("--url", help="benchmark-add 用チャンネルURL")
    parser.add_argument("--system-name", default="", help="setup: 表示名（既定 Automation Studio）")
    parser.add_argument("--discord-webhook", default="", help="setup: Discord webhook URL（任意）")
    parser.add_argument("--discord-username", default="Automation Studio", help="setup: Discord 通知名")
    parser.add_argument("--check", action="store_true", help="update: 更新確認のみ")
    parser.add_argument("--from-zip", help="update: ローカル zip から更新")
    parser.add_argument("--rollback", action="store_true", help="update: backup から復元")
    parser.add_argument("config_action", nargs="?", help="config サブコマンド")
    parser.add_argument("config_key", nargs="?", help="config key または search 語")
    parser.add_argument("config_value", nargs="?", help="config set の値")
    parser.add_argument("--resume-all", action="store_true", help="autopilot: 更新停止フラグを全解除")
    parser.add_argument("--restore", help="config-backup: 指定日付 YYYYMMDD を復元")
    parser.add_argument("--confirm", action="store_true", help="youtube-disconnect: 実行確認")
    parser.add_argument("--revoke", action=argparse.BooleanOptionalAction, default=True, help="youtube-disconnect: Google revoke を実行")
    parser.add_argument("--print-url", action="store_true", help="channel-auth: ブラウザを開かず認証条件だけ表示")
    parser.add_argument("--set", dest="set_module", action="append", help="image-modules: section=module_id を選択")
    parser.add_argument("--override", action="append", help="image-modules: section=text の自由上書き")
    parser.add_argument("--add-module", nargs="+", help="image-modules: section name text")
    parser.add_argument("--operation", default="trim", help="video-edit: 編集操作")
    parser.add_argument("--input", dest="input_path", default="", help="video-edit: 入力ファイル絶対パス")
    parser.add_argument("--params", default="{}", help="video-edit: 追加パラメータ JSON")
    args = parser.parse_args()

    routes = load_routes_table()
    intents = routes.get("intents") or {}

    if args.list and args.intent != "config-backup":
        print_list(routes)
        return 0
    if args.explain:
        return print_explain(routes, args.explain)
    if not args.intent:
        parser.error("intent か --list/--explain を指定してください")

    if args.intent == "resolve":
        vol = args.resolve_num or args.vol
        if not vol:
            parser.error("resolve には vol 番号が必要です")
        resolved = resolve_vol(vol, args.channel or "")
        code = 0 if resolved.get("ok") else 2
        print(json.dumps(resolved, ensure_ascii=False, indent=2))
        emit({"ok": code == 0, "intent": "resolve", "video_name": resolved.get("video_name", ""), "folder": resolved.get("folder", ""), "source": "resolve"})
        return code

    if args.intent == "channel-onboard":
        return channel_onboard(args)

    if args.intent == "setup":
        return initial_setup(args)

    if args.intent == "update":
        return run_update(args)

    if args.intent == "config":
        return run_config(args)

    if args.intent == "autopilot":
        return run_autopilot(args)

    if args.intent == "image-modules":
        return run_image_modules(args)

    if args.intent == "digest":
        return run_digest(args)

    if args.intent == "config-backup":
        return run_config_backup(args)

    if args.intent == "youtube-disconnect":
        return run_youtube_disconnect(args)

    if args.intent == "channel-auth":
        return run_channel_auth(args)

    route = intents.get(args.intent)
    if not route:
        print(f"未知の intent です: {args.intent}", file=sys.stderr)
        emit({"ok": False, "intent": args.intent, "error": "unknown_intent"})
        return 2

    if args.via_api and not route.get("via_api_safe", True):
        reason = route.get("via_api_unsafe_reason") or "--via-api 非対応です"
        print(reason, file=sys.stderr)
        emit({"ok": False, "intent": args.intent, "error": "via_api_unsafe", "reason": reason})
        return 2

    resolved: dict[str, Any] | None = None
    vol = args.vol
    if route.get("requires_vol"):
        if not vol:
            print(f"{args.intent} には --vol が必要です", file=sys.stderr)
            emit({"ok": False, "intent": args.intent, "error": "missing_vol"})
            return 2
        resolved = resolve_vol(vol, args.channel or "")
        if not resolved.get("ok"):
            print(resolved.get("error") or f"vol.{vol} が見つかりません", file=sys.stderr)
            if resolved.get("candidates"):
                print(json.dumps(resolved["candidates"], ensure_ascii=False, indent=2))
            emit({"ok": False, "intent": args.intent, "error": "vol_not_found", "vol": vol, "candidates": resolved.get("candidates", [])})
            return 2

    guard_code = ensure_channel_guard(args, resolved)
    if guard_code:
        return guard_code

    ctx = {
        "vol": vol or "",
        "video_name": (resolved or {}).get("video_name", ""),
        "folder": (resolved or {}).get("folder", ""),
        # SUNO workspace 等のチャンネル別命名に使う（rw_vol 固定だと orzz 等で誤 workspace になる）
        "channel_prefix": get_active_channel_info().get("prefix") or "vol",
        "publish_date": args.publish_date,
        "prompt": args.prompt,
        "count": args.count,
        "interval": args.interval,
        "privacy": args.privacy,
        "stream_id": args.stream_id,
        "channel_id": args.channel or "",
        "host": args.host,
        "title": args.title,
        "tags": args.tags,
        "url": args.url or "",
        "operation": args.operation,
        "input_path": args.input_path,
        "params": args.params,
    }

    use_api = args.via_api or (route.get("prefer") == "api" and route.get("api"))
    if use_api:
        if not route.get("api"):
            print(f"{args.intent} には API 定義がありません", file=sys.stderr)
            emit({"ok": False, "intent": args.intent, "error": "api_not_defined"})
            return 2
        method, path, body = build_api(route, ctx)
        cmd = curl_preview(method, path, body)
        print(" ".join(cmd))
        if args.dry_run:
            emit({"ok": True, "intent": args.intent, "video_name": ctx["video_name"], "cmd": cmd, "dry_run": True})
            return 0
        code = run_api(method, path, body)
        emit({"ok": code == 0, "intent": args.intent, "video_name": ctx["video_name"], "cmd": cmd, "exit_code": code})
        return code

    if not route.get("cli"):
        print(f"{args.intent} には CLI 定義がありません", file=sys.stderr)
        emit({"ok": False, "intent": args.intent, "error": "cli_not_defined"})
        return 2
    cmd = build_cli(route, ctx, args)
    print(" ".join(cmd))
    if args.dry_run:
        emit({"ok": True, "intent": args.intent, "video_name": ctx["video_name"], "cmd": cmd, "dry_run": True})
        return 0
    code = subprocess.call(cmd, cwd=str(PYTHON_DIR))
    emit({"ok": code == 0, "intent": args.intent, "video_name": ctx["video_name"], "cmd": cmd, "exit_code": code})
    return code


if __name__ == "__main__":
    raise SystemExit(main())

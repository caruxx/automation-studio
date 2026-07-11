#!/usr/bin/env python3
"""運用者向けエラー説明の共通変換."""
from __future__ import annotations

import re
from typing import Any


ACTION_URLS = {
    "suno_login": "/#suno",
    "youtube_quota": "/#ledger",
    "premiere_preflight": "/#premiere",
    "youtube_auth": "/#youtube",
    "youtube_analytics_api": "https://console.developers.google.com/apis/api/youtubeanalytics.googleapis.com/overview",
    "settings": "/#settings",
    "ledger": "/#ledger",
}


SENTINELS = {
    75: {
        "code": "suno_login_required",
        "title": "SUNOのログインが切れました",
        "next_action": "ダッシュボードからSUNOを開き、ブラウザで再ログインしてから同じvolを再開してください。",
        "action_url": ACTION_URLS["suno_login"],
    },
    76: {
        "code": "temporary_retryable_error",
        "title": "一時的な通信エラーです",
        "next_action": "しばらく待ってから再実行してください。自動再投入の対象なら次の待機後に再開されます。",
        "action_url": ACTION_URLS["ledger"],
    },
    77: {
        "code": "youtube_quota_exhausted",
        "title": "YouTube APIの1日上限に達しました",
        "next_action": "約24時間待ってから再開してください。急ぐ場合は別チャンネルの作業へ切り替えてください。",
        "action_url": ACTION_URLS["youtube_quota"],
    },
    78: {
        "code": "premiere_preflight_failed",
        "title": "Premiere Proの準備ができていません",
        "next_action": "Premiere Proを起動し、Premiere Linkパネルを開いてから再実行してください。",
        "action_url": ACTION_URLS["premiere_preflight"],
    },
}


PATTERNS = [
    (re.compile(r"accessNotConfigured|youtubeanalytics\.googleapis\.com|YouTube Analytics API .*has not been used", re.I), {
        "code": "youtube_analytics_api_not_enabled",
        "title": "YouTube Analytics APIが有効化されていません",
        "next_action": "Cloud ConsoleでYouTube Analytics APIを有効化してください。",
        "action_url": ACTION_URLS["youtube_analytics_api"],
    }),
    (re.compile(r"api.?key|APIキー|commentThreads|channels\.list|videos\.list|playlistItems|mostPopular|search\.list|forbidden|insufficientPermissions|accessNotConfigured", re.I), {
        "code": "youtube_api_key_recommended",
        "title": "公開データ取得にYouTube Data APIキーが必要です",
        "next_action": "設定カタログの youtube.api_key にAPIキーを設定すると解決します。",
        "action_url": ACTION_URLS["settings"],
    }),
    (re.compile(r"invalid_grant|refresh_token|再認証|OAuth", re.I), {
        "code": "youtube_reauth_required",
        "title": "YouTubeの再認証が必要です",
        "next_action": "YouTube設定で対象チャンネルを再認証してから、止まった工程を再開してください。",
        "action_url": ACTION_URLS["youtube_auth"],
    }),
    (re.compile(r"quotaExceeded|quota|クオータ", re.I), SENTINELS[77]),
    (re.compile(r"Premiere|CEP|Premiere Link|preflight", re.I), SENTINELS[78]),
    (re.compile(r"SUNO|login|ログイン|cookie|Cookie|UnattendedLoginRequired", re.I), SENTINELS[75]),
    (re.compile(r"timeout|timed out|429|5\d\d|temporar|一時的|retry", re.I), SENTINELS[76]),
]


def humanize_error(
    *,
    exit_code: int | None = None,
    message: str = "",
    stage: str = "",
    channel_name: str = "",
    vol: str | int = "",
    technical_detail: str = "",
) -> dict[str, Any]:
    """技術エラーを {平易な日本語, 次にやること, action_url} へ変換する."""
    base = SENTINELS.get(int(exit_code)) if exit_code is not None and str(exit_code).lstrip("-").isdigit() else None
    text = " ".join(str(x or "") for x in (message, technical_detail, stage))
    if base is None:
        for rx, value in PATTERNS:
            if rx.search(text):
                base = value
                break
    if base is None:
        base = {
            "code": "unknown_error",
            "title": "処理が途中で止まりました",
            "next_action": "実行履歴で詳細ログを確認し、止まった工程から再実行してください。",
            "action_url": ACTION_URLS["ledger"],
        }
    title = base["title"]
    context = []
    if channel_name:
        context.append(str(channel_name))
    if vol not in ("", None):
        context.append(f"vol.{vol}")
    if stage:
        context.append(str(stage))
    plain = f"{' / '.join(context)}: {title}" if context else title
    detail = technical_detail or message or ""
    return {
        "code": base["code"],
        "plain_ja": plain,
        "next_action": base["next_action"],
        "action_url": base["action_url"],
        "exit_code": exit_code,
        "stage": stage,
        "technical_detail": detail,
    }


def format_for_discord(**kwargs: Any) -> str:
    h = humanize_error(**kwargs)
    out = f"{h['plain_ja']}\n次にやること: {h['next_action']}\n操作: {h['action_url']}"
    if h.get("technical_detail"):
        out += f"\n詳細ログ: {str(h['technical_detail'])[:500]}"
    return out


if __name__ == "__main__":
    import argparse
    import json
    p = argparse.ArgumentParser()
    p.add_argument("--exit-code", type=int, default=None)
    p.add_argument("--message", default="")
    p.add_argument("--stage", default="")
    p.add_argument("--channel-name", default="")
    p.add_argument("--vol", default="")
    args = p.parse_args()
    print(json.dumps(humanize_error(**vars(args)), ensure_ascii=False, indent=2))

#!/bin/bash
# app_notify.sh - Discord Webhook で通知送信
set -euo pipefail

# 設定は共有ドライブ <SHARED>/config/discord_config.json（PC 間共通）。
# 本スクリプトは <SHARED>/Python/ に置かれる前提で、シンボリックリンク経由の起動でも
# 実体位置から共有 config を解決する。無ければ旧ローカル置き場にフォールバック。
SCRIPT_REAL="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "${BASH_SOURCE[0]}")"
SHARED_CONFIG_FILE="$(dirname "$(dirname "$SCRIPT_REAL")")/config/discord_config.json"
LEGACY_CONFIG_FILE="${HOME}/.config/orzz/discord_config.json"

if [[ -f "$SHARED_CONFIG_FILE" ]]; then
    CONFIG_FILE="$SHARED_CONFIG_FILE"
elif [[ -f "$LEGACY_CONFIG_FILE" ]]; then
    CONFIG_FILE="$LEGACY_CONFIG_FILE"
else
    echo "エラー: ${SHARED_CONFIG_FILE} が見つかりません"
    echo "ダッシュボードの通知設定で Discord Webhook URL を保存してください"
    exit 1
fi

WEBHOOK_URL=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE'))['webhook_url'])")

MESSAGE="$1"

PAYLOAD=$(python3 -c "import json,sys; cfg=json.load(open(sys.argv[1])); msg=sys.argv[2]; suffix='\\n...'; limit=2000; content=msg if len(msg)<=limit else msg[:limit-len(suffix)]+suffix; payload={'content': content, 'username': cfg.get('username', 'Automation Studio')}; avatar=cfg.get('avatar_url'); payload.update({'avatar_url': avatar} if avatar else {}); print(json.dumps(payload, ensure_ascii=False))" "$CONFIG_FILE" "$MESSAGE")

curl -fsS -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" > /dev/null

echo "Discord通知送信完了"

#!/bin/bash
# app_notify.sh - Discord Webhook で通知送信
set -euo pipefail

CONFIG_FILE="${HOME}/.config/orzz/discord_config.json"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "エラー: ${CONFIG_FILE} が見つかりません"
    echo "セットアップ手順に従ってクレデンシャルを配置してください"
    exit 1
fi

WEBHOOK_URL=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE'))['webhook_url'])")

MESSAGE="$1"

PAYLOAD=$(python3 -c "import json,sys; cfg=json.load(open(sys.argv[1])); msg=sys.argv[2]; suffix='\\n...'; limit=2000; content=msg if len(msg)<=limit else msg[:limit-len(suffix)]+suffix; payload={'content': content, 'username': cfg.get('username', 'Automation Studio')}; avatar=cfg.get('avatar_url'); payload.update({'avatar_url': avatar} if avatar else {}); print(json.dumps(payload, ensure_ascii=False))" "$CONFIG_FILE" "$MESSAGE")

curl -fsS -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" > /dev/null

echo "Discord通知送信完了"

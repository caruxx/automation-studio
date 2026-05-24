# app-notify: Discord スケジュール通知

Discord Webhook を使用して、動画の公開スケジュールやタスク通知を送信するスキル。

## セットアップ

### 1. Discord で Webhook を作成

1. 通知を受け取りたいサーバーとチャンネルを開く
2. チャンネル設定 → 連携サービス → Webhook を作成
3. Webhook URL をコピー

### 2. クレデンシャルファイルの配置

```bash
mkdir -p ~/.config/{app_id}
```

以下のファイルを作成:

```
~/.config/{app_id}/discord_config.json
```

```json
{
  "webhook_url": "YOUR_DISCORD_WEBHOOK_URL",
  "username": "Automation Studio"
}
```

`avatar_url` は任意。

```json
{
  "webhook_url": "YOUR_DISCORD_WEBHOOK_URL",
  "username": "Automation Studio",
  "avatar_url": "https://example.com/icon.png"
}
```

## 通知スクリプト

配置先: `~/.config/{app_id}/app_notify.sh`

```bash
#!/bin/bash
# app_notify.sh - Discord Webhook で通知送信
set -euo pipefail

CONFIG_FILE="${HOME}/.config/{app_id}/discord_config.json"

WEBHOOK_URL=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE'))['webhook_url'])")
MESSAGE="$1"

PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'content': sys.argv[1]}, ensure_ascii=False))" "$MESSAGE")

curl -fsS -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" > /dev/null

echo "Discord通知送信完了"
```

```bash
chmod +x ~/.config/{app_id}/app_notify.sh
```

## 使用方法

### シンプル通知

```bash
~/.config/{app_id}/app_notify.sh "vol.67 のアップロードが完了しました"
```

### 公開スケジュール通知

```bash
~/.config/{app_id}/app_notify.sh "$(cat <<MSG
📺 公開スケジュール
━━━━━━━━━━━━━━━━
12:00 - vol.67 Chill BGM
18:00 - vol.68 Night Jazz
MSG
)"
```

## Web API

```bash
curl -X POST http://localhost:8888/api/notify/discord \
  -H "Content-Type: application/json" \
  -d '{"message":"Discord 通知テスト"}'
```

旧互換として `/api/notify/line` も残すが、送信先は Discord。

## 設定ファイル構成

```
~/.config/{app_id}/
├── discord_config.json      # Discord Webhook URL（手動配置）
├── app_notify.sh            # 通知送信スクリプト
└── ...
```

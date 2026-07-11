#!/bin/bash
# Automation Studio Cloudflare Tunnel セットアップ
# 外出先のスマホから https://xxx.trycloudflare.com 経由でアクセスするための簡易トンネル。
#
# 使い方:
#   bash setup_tunnel.sh           # cloudflared のインストール確認 → 一時 Tunnel を起動
#   bash setup_tunnel.sh --check   # cloudflared の有無だけ確認

set -euo pipefail

APP_PORT="${APP_PORT:-${ORZZ_PORT:-8888}}"

# cloudflared インストール確認
if ! command -v cloudflared >/dev/null 2>&1; then
    echo "❌ cloudflared が見つかりません"
    echo ""
    echo "macOS の場合:"
    echo "  brew install cloudflared"
    echo ""
    echo "それ以外の OS は: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    exit 1
fi

if [[ "${1:-}" == "--check" ]]; then
    echo "✓ cloudflared インストール済み: $(which cloudflared)"
    cloudflared --version
    exit 0
fi

echo "=================================="
echo "  Automation Studio Cloudflare Tunnel"
echo "=================================="
echo "  ローカル: http://localhost:${APP_PORT}"
echo "  注: Tunnel URL は起動後に表示されます"
echo "  注: 認証は APP_AUTH_REQUIRED=1 で起動した場合のみ"
echo "  注: マスター設定 → リモートアクセス に URL を貼り付けて QR コード生成"
echo "=================================="
echo ""

# 一時 Tunnel（アカウント不要、URL は trycloudflare.com サブドメイン）
echo "🌐 起動中... 数秒で URL が表示されます"
echo ""
exec cloudflared tunnel --url "http://localhost:${APP_PORT}"

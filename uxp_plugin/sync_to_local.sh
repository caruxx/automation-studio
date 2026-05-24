#!/bin/bash
# UXP プラグインを Cloud Drive → ローカル ($HOME/.uxp_plugins/) に同期する。
# UXP は Google Drive Cloud Storage 配下を読み込めないことがあるため、
# 編集したらこのスクリプトで Sync → UDT で Reload する。
#
# 使い方:
#   bash uxp_plugin/sync_to_local.sh           # photoshop-link を同期
#   bash uxp_plugin/sync_to_local.sh <name>    # 任意のプラグインを同期

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAME="${1:-photoshop_link}"
DEST_NAME="${NAME//_/-}"   # アンダースコアをハイフンに
SRC="$SCRIPT_DIR/$NAME"
DEST="$HOME/.uxp_plugins/$DEST_NAME"

if [[ ! -d "$SRC" ]]; then
    echo "❌ ソースが存在しません: $SRC"
    exit 1
fi

mkdir -p "$DEST"
rsync -a --delete --exclude '.DS_Store' "$SRC/" "$DEST/"
echo "✅ $SRC"
echo "  → $DEST"
echo ""
echo "次の手順: UDT で Reload (Load & Watch なら自動)"

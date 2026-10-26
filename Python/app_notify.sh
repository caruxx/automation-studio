#!/bin/bash
# app_notify.sh - 互換ラッパー（段階 3a）。Discord Webhook は廃止。通知は notify.py が VPS の outbox（Chatwork 配送）へ送る。
set -uo pipefail
SCRIPT_REAL="$(python3 -c "import os,sys;print(os.path.realpath(sys.argv[1]))" "${BASH_SOURCE[0]}")"
DIR="$(dirname "$SCRIPT_REAL")"
PY="${APP_NOTIFY_PYTHON:-}"
if [[ -z "$PY" ]]; then
    if [[ -x "$DIR/../.venv/bin/python" ]]; then PY="$DIR/../.venv/bin/python"; else PY="python3"; fi
fi
MESSAGE="${1:-}"
if [[ -z "$MESSAGE" ]]; then echo "usage: app_notify.sh <message>" >&2; exit 2; fi
"$PY" "$DIR/notify.py" "$MESSAGE" --json >/dev/null 2>&1 || true
echo "通知を送信しました（VPS outbox）"

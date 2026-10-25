#!/bin/bash
# Automation Studio 起動スクリプト
# リポジトリ直下の .venv（Python 3.12 以上）で app.py を起動する。

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="${APP_LOG_DIR:-$HOME/Library/Logs/AutomationStudio}"
TODAY="$(date +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/server-$TODAY.log"
VENV_DIR="${APP_VENV_DIR:-$ROOT_DIR/.venv}"
PORT="${APP_PORT:-8888}"

echo "=================================="
echo "  Automation Studio"
echo "=================================="
echo "  Server : $SCRIPT_DIR/app.py"
echo "  Web    : $ROOT_DIR/web/static/"
echo "  Venv   : $VENV_DIR"
echo "=================================="
echo ""

rotate_logs() {
    mkdir -p "$LOG_DIR"
    # 日付ローテ7世代: 新しい7ファイルだけ残す
    find "$LOG_DIR" -name 'server-*.log' -type f -print0 2>/dev/null \
      | xargs -0 ls -t 2>/dev/null \
      | awk 'NR>7' \
      | xargs rm -f 2>/dev/null || true
}

if [[ "${1:-}" == "--rotate-logs-only" ]]; then
    rotate_logs
    echo "rotated: $LOG_DIR"
    exit 0
fi

# 仮想環境: 無ければ作成し、requirements.lock から依存を入れる
find_python() {
    for cand in python3.12 python3.13 python3; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
                echo "$cand"; return 0
            fi
        fi
    done
    return 1
}

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    PY="$(find_python || true)"
    if [[ -z "$PY" ]]; then
        echo "Python 3.12 以上が見つかりません（brew install python@3.12）" >&2
        exit 1
    fi
    echo "仮想環境を作成します: $VENV_DIR ($PY)"
    "$PY" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$ROOT_DIR/requirements.lock"
fi

# 依存の確認（不足があれば requirements.lock から補う）
if ! "$VENV_DIR/bin/python" -c 'import fastapi, uvicorn, apscheduler, googleapiclient, PIL, numpy, python_multipart' 2>/dev/null; then
    echo "依存を requirements.lock から導入します"
    "$VENV_DIR/bin/pip" install --quiet -r "$ROOT_DIR/requirements.lock"
fi

# ポート確認: 使用中なら止めずに終了する（kill -9 は廃止）
if lsof -ti:"$PORT" >/dev/null 2>&1; then
    echo "ポート $PORT は使用中です。既存プロセスを確認してから再実行してください: lsof -ti:$PORT" >&2
    exit 1
fi

cd "$SCRIPT_DIR"
rotate_logs
if [[ "${APP_LOG_TO_STDOUT:-0}" == "1" ]]; then
    exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/app.py"
fi
echo "Log    : $LOG_FILE"
exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/app.py" >> "$LOG_FILE" 2>&1

#!/bin/bash
# Automation Studio 起動スクリプト
# 任意のPCから実行可能（共有ドライブ版）

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="${APP_LOG_DIR:-$HOME/Library/Logs/AutomationStudio}"
TODAY="$(date +%Y-%m-%d)"
LOG_FILE="$LOG_DIR/server-$TODAY.log"

echo "=================================="
echo "  Automation Studio"
echo "=================================="
echo "  Server : $SCRIPT_DIR/app.py"
echo "  Web    : $SCRIPT_DIR/../web/static/"
echo "  Config : ${HOME}/.config/orzz/"
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

# 依存パッケージチェック
# pip 名と import 名は一致しないことが多いので、明示的なマッピング表を持つ。
# 左: pip install で使う名前 / 右: python -c "import X" で使う名前
# （単純に hyphen→underscore で済まないため、setup.sh の旧ロジックは
#  google-auth が入っていると google-auth-oauthlib を skip するバグがあった）
DEPS=(
    "fastapi|fastapi"
    "uvicorn|uvicorn"
    "google-auth|google.auth"
    "google-auth-oauthlib|google_auth_oauthlib"
    "google-auth-httplib2|google_auth_httplib2"
    "google-api-python-client|googleapiclient"
    "playwright|playwright"
    "python-multipart|python_multipart"
    "apscheduler|apscheduler"
)

MISSING=""
for entry in "${DEPS[@]}"; do
    pip_name="${entry%%|*}"
    import_name="${entry##*|}"
    if ! python3 -c "import $import_name" 2>/dev/null; then
        MISSING="$MISSING $pip_name"
    fi
done

if [[ -n "$MISSING" ]]; then
    echo "不足パッケージをインストール中:$MISSING"
    pip3 install --user $MISSING 2>/dev/null || pip3 install $MISSING
    echo ""
fi

# ポート確認 — 既に8888が使われていたら停止
if lsof -ti:8888 >/dev/null 2>&1; then
    echo "ポート8888が使用中です。既存プロセスを停止します..."
    lsof -ti:8888 | xargs kill -9 2>/dev/null || true
    sleep 1
fi

# 起動（必ず共有ドライブの app.py を使用）
cd "$SCRIPT_DIR"
rotate_logs
if [[ "${APP_LOG_TO_STDOUT:-0}" == "1" ]]; then
    exec python3 "$SCRIPT_DIR/app.py"
fi
echo "Log    : $LOG_FILE"
exec python3 "$SCRIPT_DIR/app.py" >> "$LOG_FILE" 2>&1

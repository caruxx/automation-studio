#!/bin/bash
# orzz. Dashboard 起動スクリプト
# 任意のPCから実行可能（共有ドライブ版）

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=================================="
echo "  orzz. Dashboard"
echo "=================================="
echo "  Server : $SCRIPT_DIR/app.py"
echo "  Web    : $SCRIPT_DIR/../web/static/"
echo "  Config : ${HOME}/.config/orzz/"
echo "=================================="
echo ""

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
exec python3 "$SCRIPT_DIR/app.py"

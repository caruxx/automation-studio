#!/bin/bash
# ─── Automation Studio 環境セットアップスクリプト ───
# 新しいPCで初回実行する際に使用
# Homebrew → Python3 → pip パッケージ → Playwright → 設定ファイル初期化
#
# 環境変数で挙動カスタマイズ可:
#   APP_ID         設定ディレクトリ名（既定 "orzz" — 後方互換のため）
#   APP_CONFIG_DIR 設定ディレクトリの絶対パス（APP_ID より優先）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ID="${APP_ID:-orzz}"
CONFIG_DIR="${APP_CONFIG_DIR:-${HOME}/.config/${APP_ID}}"
OS_TYPE="$(uname -s)"

echo "=================================="
echo "  Automation Studio 環境セットアップ"
echo "=================================="
echo "  OS: $OS_TYPE"
echo "  Config: $CONFIG_DIR"
echo "=================================="
echo ""

# ─── [1/7] 設定ディレクトリ ───
echo "[1/7] 設定ディレクトリ作成..."
mkdir -p "$CONFIG_DIR"

# ─── [2/7] Homebrew（macOS のみ）───
if [[ "$OS_TYPE" == "Darwin" ]]; then
    echo "[2/7] Homebrew 確認..."
    if ! command -v brew &>/dev/null; then
        echo "  Homebrew をインストール中..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        # Apple Silicon / Intel 対応
        if [[ -x "/opt/homebrew/bin/brew" ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -x "/usr/local/bin/brew" ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
    else
        echo "  ✅ Homebrew インストール済み ($(brew --version | head -1))"
    fi
else
    echo "[2/7] Homebrew スキップ（macOS以外）"
fi

# ─── [3/7] Python3 ───
echo "[3/7] Python3 確認..."
if ! command -v python3 &>/dev/null; then
    echo "  Python3 をインストール中..."
    if [[ "$OS_TYPE" == "Darwin" ]]; then
        brew install python3
    elif command -v apt-get &>/dev/null; then
        sudo apt-get update && sudo apt-get install -y python3 python3-pip
    elif command -v yum &>/dev/null; then
        sudo yum install -y python3 python3-pip
    else
        echo "  ❌ Python3 を手動でインストールしてください"
        exit 1
    fi
fi
echo "  ✅ Python3 $(python3 --version)"

# pip3 確認
if ! command -v pip3 &>/dev/null; then
    echo "  pip3 をインストール中..."
    python3 -m ensurepip --upgrade 2>/dev/null || true
    if ! command -v pip3 &>/dev/null; then
        echo "  python3 -m pip を使用します"
        PIP_CMD="python3 -m pip"
    else
        PIP_CMD="pip3"
    fi
else
    PIP_CMD="pip3"
fi
echo "  ✅ pip3 確認済み"

# ─── [4/7] Python パッケージ ───
echo "[4/7] Python パッケージインストール..."

# pip 名と import 名のマッピング表（"pip名|import名"）。
# 旧ロジック「hyphen→underscore で頭だけ」は google-auth インストール後に
# google-auth-oauthlib の判定が「google」だけで通って skip されるバグがあった。
PACKAGES=(
    "fastapi|fastapi"                            # Web API サーバー
    "uvicorn|uvicorn"                            # ASGI サーバー
    "playwright|playwright"                      # ブラウザ自動操作
    "google-auth|google.auth"                    # YouTube API 認証
    "google-auth-oauthlib|google_auth_oauthlib"
    "google-auth-httplib2|google_auth_httplib2"
    "google-api-python-client|googleapiclient"   # YouTube Data API
)

for entry in "${PACKAGES[@]}"; do
    pkg="${entry%%|*}"
    mod="${entry##*|}"
    if python3 -c "import $mod" 2>/dev/null; then
        echo "  ✅ $pkg"
    else
        echo "  📦 $pkg インストール中..."
        $PIP_CMD install --user "$pkg" 2>/dev/null || $PIP_CMD install "$pkg"
    fi
done

# ─── [5/7] Playwright ブラウザ + 依存ライブラリ ───
echo "[5/7] Playwright Chromium インストール..."

# Playwright のブラウザがインストール済みか確認
PW_INSTALLED=false
if python3 -c "
from pathlib import Path
for base in [Path.home()/'Library/Caches/ms-playwright', Path.home()/'.cache/ms-playwright']:
    if base.exists() and any(base.glob('chromium-*')):
        exit(0)
exit(1)
" 2>/dev/null; then
    PW_INSTALLED=true
    echo "  ✅ Chromium ブラウザ インストール済み"
fi

if [[ "$PW_INSTALLED" == "false" ]]; then
    echo "  📦 Chromium をダウンロード中（約90MB）..."
    python3 -m playwright install chromium

    # Linux の場合、システム依存ライブラリもインストール
    if [[ "$OS_TYPE" == "Linux" ]]; then
        echo "  📦 システム依存ライブラリをインストール中..."
        python3 -m playwright install-deps chromium 2>/dev/null || \
            echo "  ⚠️ sudo が必要な場合: sudo python3 -m playwright install-deps chromium"
    fi
fi

# 動作テスト
echo "  ブラウザ起動テスト中..."
if python3 -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('about:blank')
    browser.close()
    print('OK')
" 2>/dev/null; then
    echo "  ✅ Playwright 動作確認OK"
else
    echo "  ❌ Playwright の起動に失敗しました"
    echo "     以下を手動実行してください:"
    echo "     python3 -m playwright install chromium"
    if [[ "$OS_TYPE" == "Linux" ]]; then
        echo "     sudo python3 -m playwright install-deps chromium"
    fi
fi

# ─── [6/7] FFmpeg（音声処理用）───
echo "[6/7] FFmpeg 確認..."
if command -v ffmpeg &>/dev/null; then
    echo "  ✅ FFmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"
else
    echo "  FFmpeg をインストール中..."
    if [[ "$OS_TYPE" == "Darwin" ]]; then
        brew install ffmpeg
    elif command -v apt-get &>/dev/null; then
        sudo apt-get install -y ffmpeg
    else
        echo "  ⚠️ FFmpeg を手動でインストールしてください"
    fi
fi

# ─── [6.5/7] Premiere Link CEP パネル ───
if [[ "$OS_TYPE" == "Darwin" ]]; then
    CEP_DIR="$HOME/Library/Application Support/Adobe/CEP/extensions/net.premiere.link"
    BRIDGE_INSTALLER="$(cd "$SCRIPT_DIR/.." && pwd)/cep_extension/install.sh"
    if [[ ! -d "$CEP_DIR" ]]; then
        echo "[6.5/7] Premiere Link パネルをインストール中..."
        if [[ -f "$BRIDGE_INSTALLER" ]]; then
            bash "$BRIDGE_INSTALLER"
        else
            echo "  ⚠️ install.sh が見つかりません: $BRIDGE_INSTALLER"
            echo "     手動で cep_extension/install.sh を実行してください"
        fi
    else
        echo "[6.5/7] ✅ Premiere Link パネル（既存）"
    fi
fi

# ─── [7/7] 設定ファイル初期化 ───
echo "[7/7] 設定ファイル初期化..."

if [[ ! -f "$CONFIG_DIR/suno_config.json" ]]; then
    cat > "$CONFIG_DIR/suno_config.json" << 'EOF'
{
  "provider": "gemini",
  "model": "gemini-3-flash-preview",
  "api_key": "",
  "generation_mode": "styles_title_only",
  "prompt": "",
  "loop_count": 5,
  "loop_interval_sec": 180,
  "headless": false
}
EOF
    echo "  📄 作成: suno_config.json（⚠️ APIキーを設定してください）"
else
    echo "  ✅ suno_config.json（既存）"
fi

if [[ ! -f "$CONFIG_DIR/youtube_client_secret.json" ]]; then
    cat > "$CONFIG_DIR/youtube_client_secret.json" << 'EOF'
{
  "installed": {
    "client_id": "YOUR_CLIENT_ID",
    "project_id": "YOUR_PROJECT_ID",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_secret": "YOUR_CLIENT_SECRET",
    "redirect_uris": ["http://localhost:8080"]
  }
}
EOF
    echo "  📄 作成: youtube_client_secret.json（⚠️ クレデンシャルを設定してください）"
else
    echo "  ✅ youtube_client_secret.json（既存）"
fi

if [[ ! -f "$CONFIG_DIR/discord_config.json" ]]; then
    cat > "$CONFIG_DIR/discord_config.json" << 'EOF'
{
  "webhook_url": "YOUR_DISCORD_WEBHOOK_URL",
  "username": "Automation Studio"
}
EOF
    echo "  📄 作成: discord_config.json（⚠️ Discord Webhook URL を設定してください）"
else
    echo "  ✅ discord_config.json（既存）"
fi

# 共有ドライブのスクリプトへのシンボリックリンク
echo ""
echo "スクリプトリンク作成..."
for script in suno_auto_create.py app_youtube.py app_notify.sh YouTube_1080p_Optimized.epr; do
    src="$SCRIPT_DIR/$script"
    dst="$CONFIG_DIR/$script"
    if [[ -f "$src" ]]; then
        ln -sf "$src" "$dst"
        echo "  🔗 $script → 共有ドライブ"
    fi
done

# ─── 完了 ───
echo ""
echo "=================================="
echo "  ✅ セットアップ完了"
echo "=================================="
echo ""
echo "インストール済み:"
echo "  - Python3  $(python3 --version 2>&1)"
echo "  - FastAPI, Uvicorn, Playwright"
echo "  - Google API Client (YouTube用)"
echo "  - FFmpeg (音声処理用)"
echo "  - Chromium (ブラウザ自動操作用)"
echo ""
echo "⚠️  要設定（ダッシュボードの設定画面から設定可能）:"

# 未設定チェック
NEEDS_SETUP=false
if grep -q "YOUR_" "$CONFIG_DIR/suno_config.json" 2>/dev/null || ! grep -q "api_key" "$CONFIG_DIR/suno_config.json" 2>/dev/null; then
    echo "  - Gemini/ChatGPT APIキー"
    NEEDS_SETUP=true
fi
if grep -q "YOUR_" "$CONFIG_DIR/youtube_client_secret.json" 2>/dev/null; then
    echo "  - YouTube OAuthクレデンシャル"
    NEEDS_SETUP=true
fi
if grep -q "YOUR_" "$CONFIG_DIR/discord_config.json" 2>/dev/null; then
    echo "  - Discord Webhook URL"
    NEEDS_SETUP=true
fi
if [[ "$NEEDS_SETUP" == "false" ]]; then
    echo "  なし（全て設定済み）"
fi

echo ""
echo "次のステップ:"
echo "  bash $SCRIPT_DIR/start.sh"
echo "  → http://localhost:8888 を開く"
echo ""

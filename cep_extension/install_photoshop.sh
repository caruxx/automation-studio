#!/bin/bash
# Photoshop Link - Adobe Photoshop CEP パネルのインストーラ
#
# 実行するとこのディレクトリの photoshop_link/ を
# ~/Library/Application Support/Adobe/CEP/extensions/ にコピーし、
# CEP デバッグモード（未署名拡張の許可）を有効化します。
#
# 使い方:
#   bash install_photoshop.sh
#
# アンインストール:
#   bash install_photoshop.sh uninstall

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/photoshop_link"
OS_TYPE="$(uname -s)"

if [[ "$OS_TYPE" == "Darwin" ]]; then
    CEP_ROOT="$HOME/Library/Application Support/Adobe/CEP/extensions"
    DEFAULTS_CMD="defaults"
elif [[ "$OS_TYPE" == "Linux" ]]; then
    echo "❌ CEP 拡張は macOS / Windows 専用です（Linux は未対応）"
    exit 1
else
    CEP_ROOT="$APPDATA/Adobe/CEP/extensions"
    DEFAULTS_CMD=""
fi

DEST_DIR="$CEP_ROOT/net.photoshop.link"

# ─── アンインストール ───
if [[ "${1:-}" == "uninstall" ]]; then
    echo "Photoshop Link をアンインストールします..."
    if [[ -d "$DEST_DIR" ]]; then
        rm -rf "$DEST_DIR"
        echo "  ✅ 削除: $DEST_DIR"
    else
        echo "  ℹ️  インストールされていません"
    fi
    exit 0
fi

echo "=================================="
echo "  Photoshop Link インストーラ"
echo "=================================="
echo "  Source:      $SRC_DIR"
echo "  Destination: $DEST_DIR"
echo "=================================="

if [[ ! -d "$SRC_DIR" ]]; then
    echo "❌ ソースディレクトリが見つかりません: $SRC_DIR"
    exit 1
fi

# ─── [1/4] CEP 拡張ディレクトリ準備 ───
echo "[1/4] CEP 拡張ディレクトリを準備..."
mkdir -p "$CEP_ROOT"

# ─── [2/4] 既存インストールの処理 ───
echo "[2/4] 既存インストールを確認..."
if [[ -d "$DEST_DIR" ]]; then
    echo "  既存の Photoshop Link を上書きします"
    rm -rf "$DEST_DIR"
fi

# ─── [3/4] コピー ───
echo "[3/4] ファイルをコピー..."
cp -R "$SRC_DIR" "$DEST_DIR"
# CSInterface.js が無い場合は Photoshop 本体から探して補完
if [[ ! -f "$DEST_DIR/js/CSInterface.js" ]]; then
    echo "  CSInterface.js を Photoshop 本体から取得..."
    PS_CSI="$(find /Applications -name 'CSInterface.js' -path '*Photoshop*CEP*' 2>/dev/null | head -1)"
    if [[ -n "$PS_CSI" ]]; then
        cp "$PS_CSI" "$DEST_DIR/js/CSInterface.js"
        echo "  ✅ CSInterface.js 取得: $PS_CSI"
    else
        echo "  ⚠️  CSInterface.js が見つかりません。手動で配置してください:"
        echo "     $DEST_DIR/js/CSInterface.js"
    fi
fi
echo "  ✅ 配置完了: $DEST_DIR"

# ─── [4/4] CEP デバッグモード有効化（未署名拡張を許可）───
echo "[4/4] CEP デバッグモードを有効化..."
if [[ -n "$DEFAULTS_CMD" ]]; then
    for v in 6 7 8 9 10 11 12; do
        "$DEFAULTS_CMD" write "com.adobe.CSXS.$v" PlayerDebugMode 1 2>/dev/null || true
    done
    echo "  ✅ PlayerDebugMode = 1 (CSXS 6–12)"
else
    echo "  ⚠️  Windows の場合は手動でレジストリを設定:"
    echo "     HKEY_CURRENT_USER\\Software\\Adobe\\CSXS.X → PlayerDebugMode = 1 (REG_SZ)"
fi

echo ""
echo "=================================="
echo "  ✅ インストール完了"
echo "=================================="
echo ""
echo "次の手順:"
echo "  1. Adobe Photoshop を（起動中なら）完全に終了して再起動"
echo "  2. メニュー: ウィンドウ → 拡張機能 → Photoshop Link を選択"
echo "  3. パネル右上「フォルダ選択」で .jsx フォルダを指定"
echo ""
echo "Python からの IPC は /tmp/photoshop_link_*.json を使用します。"

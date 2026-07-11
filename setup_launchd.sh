#!/bin/bash
# Automation Studio LaunchAgent setup.
#
# 使い方:
#   bash setup_launchd.sh             # plist 生成 + load
#   bash setup_launchd.sh --sync      # コードミラー再同期 + launchd 再起動
#   bash setup_launchd.sh --unload    # unload のみ
#   bash setup_launchd.sh --install   # 明示 install/load
#
# アンインストール:
#   bash setup_launchd.sh --unload
#   rm -f "$HOME/Library/LaunchAgents/jp.caruvistar.automation-studio.plist"

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PY_START="$ROOT_DIR/Python/start.sh"
LABEL="${APP_LAUNCHD_LABEL:-jp.caruvistar.automation-studio}"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${APP_LOG_DIR:-$HOME/Library/Logs/AutomationStudio}"
RUNNER_DIR="$HOME/Library/Application Support/AutomationStudio"
RUNNER="$RUNNER_DIR/launchd_runner.sh"
MIRROR_DIR="$RUNNER_DIR/app"
RUNTIME_CONFIG_DIR="${STUDIO_CONFIG_DIR:-$RUNNER_DIR/config}"
DRIVE_CONFIG_DIR="${STUDIO_DRIVE_CONFIG_DIR:-$ROOT_DIR/config}"
ACTION="${1:---install}"
DOMAIN_TARGET="gui/$(id -u)/${LABEL}"

if [[ ! -f "$PY_START" ]]; then
  echo "Python/start.sh が見つかりません: $PY_START" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR" "$RUNNER_DIR"

unload_agent() {
  if launchctl list "$LABEL" >/dev/null 2>&1; then
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
  fi
}

copy_config_runtime_to_drive() {
  if [[ ! -d "$RUNTIME_CONFIG_DIR" ]]; then
    return 0
  fi
  if [[ ! -f "$RUNTIME_CONFIG_DIR/channels.json" ]]; then
    return 0
  fi
  mkdir -p "$DRIVE_CONFIG_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.DS_Store' "$RUNTIME_CONFIG_DIR/" "$DRIVE_CONFIG_DIR/"
  else
    cp -R "$RUNTIME_CONFIG_DIR/." "$DRIVE_CONFIG_DIR/"
  fi
  echo "synced config runtime -> Drive: $DRIVE_CONFIG_DIR"
}

copy_config_drive_to_runtime() {
  mkdir -p "$RUNTIME_CONFIG_DIR"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.DS_Store' "$DRIVE_CONFIG_DIR/" "$RUNTIME_CONFIG_DIR/"
  else
    cp -R "$DRIVE_CONFIG_DIR/." "$RUNTIME_CONFIG_DIR/"
  fi
  echo "synced config Drive -> runtime: $RUNTIME_CONFIG_DIR"
}

sync_mirror() {
  copy_config_drive_to_runtime
  mkdir -p "$MIRROR_DIR"
  rm -rf "$MIRROR_DIR/Python" "$MIRROR_DIR/web" "$MIRROR_DIR/config"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '__pycache__' --exclude '*.pyc' "$ROOT_DIR/Python/" "$MIRROR_DIR/Python/"
    rsync -a --exclude '.DS_Store' "$ROOT_DIR/web/" "$MIRROR_DIR/web/"
  else
    mkdir -p "$MIRROR_DIR/Python" "$MIRROR_DIR/web"
    cp -R "$ROOT_DIR/Python/." "$MIRROR_DIR/Python/"
    cp -R "$ROOT_DIR/web/." "$MIRROR_DIR/web/"
  fi
  ln -s "$RUNTIME_CONFIG_DIR" "$MIRROR_DIR/config"
  cp "$ROOT_DIR/VERSION" "$MIRROR_DIR/VERSION"
  echo "synced mirror: $MIRROR_DIR"
  echo "config link  : $MIRROR_DIR/config -> $RUNTIME_CONFIG_DIR"
}

write_plist_and_runner() {
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${RUNNER}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${HOME}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>APP_LOG_DIR</key>
    <string>${LOG_DIR}</string>
    <key>APP_SHARED_BASE</key>
    <string>${MIRROR_DIR}</string>
    <key>STUDIO_CONFIG_DIR</key>
    <string>${RUNTIME_CONFIG_DIR}</string>
    <key>STUDIO_DRIVE_BASE</key>
    <string>${ROOT_DIR}</string>
  </dict>
</dict>
</plist>
EOF

  cat > "$RUNNER" <<EOF
#!/bin/bash
set -euo pipefail
ROOT_DIR="${MIRROR_DIR}"
RUNTIME_CONFIG_DIR="${RUNTIME_CONFIG_DIR}"
DRIVE_BASE="${ROOT_DIR}"
LOG_DIR="${LOG_DIR}"
TODAY="\$(date +%Y-%m-%d)"
LOG_FILE="\$LOG_DIR/server-\$TODAY.log"
mkdir -p "\$LOG_DIR"
find "\$LOG_DIR" -name 'server-*.log' -type f -print0 2>/dev/null \\
  | xargs -0 ls -t 2>/dev/null \\
  | awk 'NR>7' \\
  | xargs rm -f 2>/dev/null || true
if lsof -ti:8888 >/dev/null 2>&1; then
  lsof -ti:8888 | xargs kill -9 2>/dev/null || true
  sleep 1
fi
cd "\$ROOT_DIR/Python"
export APP_SHARED_BASE="\$ROOT_DIR"
export STUDIO_CONFIG_DIR="\$RUNTIME_CONFIG_DIR"
export STUDIO_DRIVE_BASE="\$DRIVE_BASE"
exec python3 "\$ROOT_DIR/Python/app.py" >> "\$LOG_FILE" 2>&1
EOF
  chmod 755 "$RUNNER"
  chmod 644 "$PLIST"
}

case "$ACTION" in
  --unload|unload)
    unload_agent
    echo "unloaded: $LABEL"
    echo "plist は残しています: $PLIST"
    exit 0
    ;;
  --sync|sync)
    copy_config_runtime_to_drive
    sync_mirror
    write_plist_and_runner
    if [[ ! -f "$PLIST" || ! -f "$RUNNER" ]]; then
      launchctl load "$PLIST"
    elif ! launchctl list "$LABEL" >/dev/null 2>&1; then
      launchctl load "$PLIST"
    fi
    launchctl kickstart -k "$DOMAIN_TARGET"
    echo "restarted: $LABEL"
    echo "health   : curl -s http://localhost:8888/api/health"
    exit 0
    ;;
  --install|install|"")
    ;;
  *)
    echo "unknown action: $ACTION" >&2
    echo "usage: bash setup_launchd.sh [--install|--sync|--unload]" >&2
    exit 2
    ;;
esac

sync_mirror
write_plist_and_runner
unload_agent
launchctl load "$PLIST"

echo "loaded: $LABEL"
echo "plist : $PLIST"
echo "logs  : $LOG_DIR"
echo ""
echo "同期 + 再起動:"
echo "  bash setup_launchd.sh --sync"
echo "状態確認:"
echo "  launchctl list | grep automation"
echo "停止:"
echo "  bash setup_launchd.sh --unload"
echo "アンインストール:"
echo "  bash setup_launchd.sh --unload"
echo "  rm -f \"$PLIST\""

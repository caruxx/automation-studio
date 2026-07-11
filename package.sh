#!/bin/bash
# Build a clean Automation Studio distribution zip.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(tr -d '[:space:]' < "$ROOT_DIR/VERSION")"
DIST_DIR="$ROOT_DIR/dist"
STAGE_DIR="$DIST_DIR/package_stage"
PKG_ROOT="$STAGE_DIR/automation-studio"
ZIP_PATH="$DIST_DIR/automation-studio-${VERSION}.zip"
EXCLUDES="$ROOT_DIR/package_exclude.txt"

if [[ -z "$VERSION" ]]; then
  echo "VERSION is empty" >&2
  exit 1
fi

rm -rf "$STAGE_DIR" "$ZIP_PATH"
mkdir -p "$PKG_ROOT/config" "$DIST_DIR"

copy_dir() {
  local src="$1"
  local dst="$2"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude-from="$EXCLUDES" "$ROOT_DIR/$src/" "$PKG_ROOT/$dst/"
  else
    mkdir -p "$PKG_ROOT/$dst"
    cp -R "$ROOT_DIR/$src/." "$PKG_ROOT/$dst/"
  fi
}

copy_dir "Python" "Python"
copy_dir "web" "web"
copy_dir "skills" "skills"

cp "$ROOT_DIR/README.md" "$PKG_ROOT/README.md"
cp "$ROOT_DIR/VERSION" "$PKG_ROOT/VERSION"
cp "$ROOT_DIR/start.sh" "$PKG_ROOT/start.sh"
cp "$ROOT_DIR/setup_launchd.sh" "$PKG_ROOT/setup_launchd.sh"
[[ -f "$ROOT_DIR/LICENSE" ]] && cp "$ROOT_DIR/LICENSE" "$PKG_ROOT/LICENSE"

find "$ROOT_DIR/config" -maxdepth 1 -name "*.template.json" -type f -exec cp {} "$PKG_ROOT/config/" \;

# Defense in depth: remove known personal/runtime files even if copied by fallback.
find "$PKG_ROOT" \( \
  -name "__pycache__" -o -name "*.pyc" -o -name ".DS_Store" -o \
  -name ".claude" -o -name ".codex" -o -name "generated_product_images" -o \
  -name "competitor_analysis" -o -name "AI_ROUTING_PLAN.md" -o \
  -name "IMAGE_EVAL_LOOP_PLAN.md" -o -name "AGENTS_WORKPLAN.md" -o \
  -name "*.youtube_token.json" -o -name "youtube_token.json" -o \
  -name "youtube_client_secret.json" -o -name "client_secret*" -o \
  -name "onboarding.json" \) -prune -exec rm -rf {} +

find "$PKG_ROOT/config" -maxdepth 1 -type f ! -name "*.template.json" -delete
rm -rf "$PKG_ROOT/config/benchmark"

(
  cd "$STAGE_DIR"
  zip -qr "$ZIP_PATH" "automation-studio"
)

scan_zip() {
  local zip="$1"
  local tmp_patterns
  tmp_patterns="$(mktemp)"
  {
    echo "abe_kota"
    if [[ -f "$ROOT_DIR/config/channels.json" ]]; then
      python3 - "$ROOT_DIR/config/channels.json" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    data = []
for ch in data if isinstance(data, list) else []:
    cid = str(ch.get("youtube_channel_id") or "").strip()
    if cid:
        print(cid)
PY
    fi
  } > "$tmp_patterns"

  local contents
  contents="$(mktemp)"
  unzip -Z1 "$zip" > "$contents"
  if grep -E '(^|/)(dashboard_config|discord_config|channels)\.json$|youtube_api_key\.txt$|competitor_analysis_cache\.json$|benchmark_profiles\.json$|benchmark_config\.json$|(^|/)benchmark/|youtube_token|client_secret|onboarding\.json$|AI_ROUTING_PLAN\.md$|(^|/)\.claude/|(^|/)\.codex/' "$contents"; then
    echo "個人データファイルが zip に含まれています" >&2
    rm -f "$tmp_patterns" "$contents"
    return 1
  fi

  if unzip -p "$zip" 2>/dev/null | LC_ALL=C grep -aE 'discord(app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+'; then
    echo "Discord webhook URL を検出しました" >&2
    rm -f "$tmp_patterns" "$contents"
    return 1
  fi

  if unzip -p "$zip" 2>/dev/null | LC_ALL=C grep -aF -f "$tmp_patterns"; then
    echo "個人データ混入スキャンで検出しました" >&2
    rm -f "$tmp_patterns" "$contents"
    return 1
  fi
  rm -f "$tmp_patterns" "$contents"
}

scan_zip "$ZIP_PATH"

echo "OK: $ZIP_PATH"
unzip -Z1 "$ZIP_PATH" | awk -F/ 'NF>=1{print $1}' | sort -u

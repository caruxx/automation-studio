#!/usr/bin/env bash
# Deprecated: use app_notify.sh instead.
echo "[deprecated] orzz_line_notify.sh is deprecated; use app_notify.sh" >&2
exec "$(dirname "$0")/app_notify.sh" "$@"

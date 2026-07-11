#!/bin/bash
# Automation Studio root launcher.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "$SCRIPT_DIR/Python/start.sh"

#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/wait_for_yolink_change.py" \
  --kind th \
  --device-id "${YOLINK_TH_8003_SERIAL:-}" \
  "$@"

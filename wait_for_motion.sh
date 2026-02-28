#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/wait_for_yolink_change.py" \
  --kind motion \
  --device-id "${YOLINK_MOTION_7804_SERIAL:-}" \
  "$@"

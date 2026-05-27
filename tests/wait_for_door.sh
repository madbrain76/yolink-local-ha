#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_python.sh"
exec_yolink_python "${SCRIPT_DIR}/wait_for_yolink_change.py" \
  --kind door \
  --device-id "${YOLINK_DOOR_7704_SERIAL:-}" \
  "$@"

#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_python.sh"
exec_yolink_python "${SCRIPT_DIR}/wait_for_yolink_change.py" \
  --kind leak \
  --device-id "${YOLINK_LEAK_7903_SERIAL:-}" \
  "$@"

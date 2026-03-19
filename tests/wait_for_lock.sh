#!/usr/bin/env bash
set -euo pipefail

# Lock model number is currently unknown in this environment.
# Use placeholder env var name until model is identified:
#   YOLINK_LOCK_MODEL_NUMBER_SERIAL=<deviceId>
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_ID="${YOLINK_LOCK_MODEL_NUMBER_SERIAL:-}"

if [[ -z "${LOCK_ID}" ]]; then
  echo "NOTE: no lock device is currently available to test."
  echo "Set YOLINK_LOCK_MODEL_NUMBER_SERIAL to run this test."
  echo "Example: YOLINK_LOCK_MODEL_NUMBER_SERIAL=<deviceId>"
  exit 0
fi

exec python3 "${SCRIPT_DIR}/wait_for_yolink_change.py" \
  --kind lock \
  --device-id "${LOCK_ID}" \
  "$@"

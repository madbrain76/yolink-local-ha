#!/usr/bin/env bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

exec_yolink_python() {
  if [[ -n "${YOLOCAL_PYTHON:-}" ]]; then
    exec "${YOLOCAL_PYTHON}" "$@"
  fi

  if command -v uv >/dev/null 2>&1; then
    cd "${REPO_ROOT}"
    exec uv run python "$@"
  fi

  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    exec "${REPO_ROOT}/.venv/bin/python" "$@"
  fi

  exec python3 "$@"
}

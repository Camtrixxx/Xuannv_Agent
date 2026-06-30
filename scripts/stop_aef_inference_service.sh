#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${AEF_PORT:-7862}"
PID_FILE="${PROJECT_ROOT}/agent/runtime/pids/aef_inference.pid"

if [[ -f "${PID_FILE}" ]]; then
  pid="$(cat "${PID_FILE}" || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    echo "Stopping AEF inference service: ${pid}"
    kill "${pid}"
    rm -f "${PID_FILE}"
    exit 0
  fi
  rm -f "${PID_FILE}"
fi

pids="$(pgrep -f "aef_inference.server.*--port ${PORT}" || true)"
if [[ -z "${pids}" ]]; then
  echo "AEF inference service is not running."
  exit 0
fi

echo "Stopping AEF inference service: ${pids}"
# shellcheck disable=SC2086
kill ${pids}

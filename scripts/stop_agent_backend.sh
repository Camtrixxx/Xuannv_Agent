#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PID_FILE="${PROJECT_ROOT}/agent/runtime/pids/agent_backend.pid"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "Agent backend is not running."
  exit 0
fi

pid="$(cat "${PID_FILE}" || true)"
if [[ -z "${pid}" ]] || ! kill -0 "${pid}" >/dev/null 2>&1; then
  echo "Agent backend is not running."
  rm -f "${PID_FILE}"
  exit 0
fi

echo "Stopping Agent backend: ${pid}"
kill "${pid}"
rm -f "${PID_FILE}"

#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python}"
HOST="${AGENT_HOST:-0.0.0.0}"
PORT="${AGENT_PORT:-7870}"
LOG_DIR="${PROJECT_ROOT}/agent/runtime/logs"
PID_DIR="${PROJECT_ROOT}/agent/runtime/pids"
LOG_FILE="${LOG_DIR}/agent_backend.log"
PID_FILE="${PID_DIR}/agent_backend.pid"

mkdir -p "${LOG_DIR}" "${PID_DIR}"
cd "${PROJECT_ROOT}"
export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH:-}"

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}" || true)"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" >/dev/null 2>&1; then
    echo "Agent backend is already running: ${old_pid}"
    exit 0
  fi
fi

echo "Starting Xuannv Agent backend on ${HOST}:${PORT}"
nohup setsid "${PYTHON}" -m uvicorn agent.backend.app:app \
  --host "${HOST}" \
  --port "${PORT}" \
  >"${LOG_FILE}" 2>&1 </dev/null &

echo $! > "${PID_FILE}"
echo "PID: $(cat "${PID_FILE}")"
echo "Log: ${LOG_FILE}"

#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-/home/heyuhang/miniconda3/envs/hyh-dl/bin/python}"
HOST="${AGENT_HOST:-0.0.0.0}"
PORT="${AGENT_PORT:-7870}"
LOG_DIR="${PROJECT_ROOT}/agent/runtime/logs"
PID_DIR="${PROJECT_ROOT}/agent/runtime/pids"
LOG_FILE="${LOG_DIR}/agent_backend.log"
PID_FILE="${PID_DIR}/agent_backend.pid"

mkdir -p "${LOG_DIR}" "${PID_DIR}"
cd "${PROJECT_ROOT}"
export PYTHONPATH="${PROJECT_ROOT}:${PYTHONPATH:-}"

# Load local secrets (e.g. DEEPSEEK_API_KEY) from a gitignored .env if present.
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_ROOT}/.env"
  set +a
fi

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}" || true)"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" >/dev/null 2>&1; then
    echo "Agent backend is already running: ${old_pid}"
    exit 0
  fi
fi

existing_pid="$(pgrep -f "(agent.backend.app|uvicorn agent.backend.app:app).*--port ${PORT}" || true)"
if [[ -n "${existing_pid}" ]]; then
  echo "Agent backend is already running on port ${PORT}: ${existing_pid}"
  echo "${existing_pid%%$'\n'*}" > "${PID_FILE}"
  exit 0
fi

echo "Starting Xuannv Agent backend on ${HOST}:${PORT}"
nohup setsid "${PYTHON}" -m uvicorn agent.backend.app:app \
  --host "${HOST}" \
  --port "${PORT}" \
  >"${LOG_FILE}" 2>&1 </dev/null &

echo $! > "${PID_FILE}"
echo "PID: $(cat "${PID_FILE}")"
echo "Log: ${LOG_FILE}"

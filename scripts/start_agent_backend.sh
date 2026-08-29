#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Load local config (AGENT_PORT, DEEPSEEK_API_KEY, ...) from a gitignored .env
# before deriving anything from it, so .env can set the port. Values already
# exported in the environment still win.
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_ROOT}/.env"
  set +a
fi

PYTHON="${PYTHON:-/home/zkcs/anaconda3/envs/xuannv_agent/bin/python}"
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

new_pid=$!
echo "${new_pid}" > "${PID_FILE}"
echo "PID: ${new_pid}"
echo "Log: ${LOG_FILE}"

# Don't report success for a process that died on startup (bad import, port
# already taken, missing dependency). Wait for the health endpoint instead.
for _ in $(seq 1 "${START_TIMEOUT:-40}"); do
  if curl --noproxy '*' -fsS -m 2 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    echo "Agent backend is healthy on ${PORT}."
    exit 0
  fi
  if ! kill -0 "${new_pid}" 2>/dev/null; then
    echo "ERROR: Agent backend exited during startup. Last log lines:" >&2
    tail -n 20 "${LOG_FILE}" >&2
    rm -f "${PID_FILE}"
    exit 1
  fi
  sleep 1
done

echo "ERROR: Agent backend did not become healthy in ${START_TIMEOUT:-40}s. Last log lines:" >&2
tail -n 20 "${LOG_FILE}" >&2
exit 1

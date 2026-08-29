#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${PROJECT_ROOT}/scripts/_common.sh"
load_env "${PROJECT_ROOT}"
PORT="${AGENT_PORT:-7870}"
PID_FILE="${PROJECT_ROOT}/agent/runtime/pids/agent_backend.pid"
LOG_FILE="${PROJECT_ROOT}/agent/runtime/logs/agent_backend.log"

echo "== Agent backend process =="
if [[ -f "${PID_FILE}" ]]; then
  pid="$(cat "${PID_FILE}" || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" >/dev/null 2>&1; then
    ps -o pid,ppid,stat,etime,cmd -p "${pid}"
  else
    echo "not running (stale pid: ${pid:-empty})"
  fi
else
  pgrep -af "(agent.backend.app|uvicorn agent.backend.app:app).*--port ${PORT}" || echo "not started"
fi

echo
echo "== Health =="
curl --noproxy '*' --connect-timeout 5 --max-time 10 -sS \
  "http://127.0.0.1:${PORT}/api/health" || true
echo

echo
echo "== Recent log =="
tail -60 "${LOG_FILE}" 2>/dev/null || true

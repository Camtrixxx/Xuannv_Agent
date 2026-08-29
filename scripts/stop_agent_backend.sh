#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${PROJECT_ROOT}/scripts/_common.sh"
load_env "${PROJECT_ROOT}"

PORT="${AGENT_PORT:-7870}"
PID_FILE="${PROJECT_ROOT}/agent/runtime/pids/agent_backend.pid"
PATTERN="(agent\.backend\.app|uvicorn agent\.backend\.app:app).*--port ${PORT}"

declare -a targets=()

if [[ -f "${PID_FILE}" ]]; then
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    targets+=("${pid}")
  fi
fi

# Also sweep by pattern: the PID file can be stale, or a previous run may have
# left an extra worker behind. Both must be gone before we report success.
while read -r pid; do
  [[ -z "${pid}" ]] && continue
  for known in "${targets[@]:-}"; do
    [[ "${known}" == "${pid}" ]] && continue 2
  done
  targets+=("${pid}")
done < <(service_pids "${PATTERN}")

if [[ "${#targets[@]}" -eq 0 ]]; then
  rm -f "${PID_FILE}"
  echo "Agent backend is not running."
  exit 0
fi

# Only drop the PID file once the processes are actually dead, so a failed stop
# stays visible to the status script instead of looking like a clean shutdown.
stop_pids "Agent backend" "${targets[@]}"
rm -f "${PID_FILE}"

#!/usr/bin/env bash
# Keep the Agent alive: probe /api/health, restart it when the probe fails.
#
# Designed to be driven by cron every minute (see scripts/install_watchdog_cron.sh).
# Deliberately conservative: a restart is only triggered after several
# consecutive failed probes, so a momentarily busy server is not mistaken for a
# dead service.
set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${PROJECT_ROOT}/scripts/_common.sh"
load_env "${PROJECT_ROOT}"

PORT="${AGENT_PORT:-9070}"
LOG_DIR="${PROJECT_ROOT}/agent/runtime/logs"
LOG_FILE="${LOG_DIR}/watchdog.log"
LOCK_FILE="${PROJECT_ROOT}/agent/runtime/watchdog.lock"
PROBES="${WATCHDOG_PROBES:-3}"
PROBE_GAP="${WATCHDOG_PROBE_GAP:-3}"
MAX_LOG_LINES="${WATCHDOG_MAX_LOG_LINES:-2000}"

mkdir -p "${LOG_DIR}"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG_FILE}"
}

# Only one watchdog at a time: a restart takes longer than cron's one-minute
# tick, and two overlapping restarts would fight over the port. The lock fd must
# be closed in anything we exec (`9>&-` below), otherwise the long-lived Agent
# inherits it and holds the lock forever, silently disabling every later run.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  exit 0
fi

healthy() {
  curl --noproxy '*' -fsS -m 5 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1
}

for attempt in $(seq 1 "${PROBES}"); do
  if healthy; then
    exit 0
  fi
  [[ "${attempt}" -lt "${PROBES}" ]] && sleep "${PROBE_GAP}"
done

log "health probe failed ${PROBES}x on port ${PORT}; restarting agent"

# Capture why it died before the log is overwritten by the next start.
if [[ -f "${LOG_DIR}/agent_backend.log" ]]; then
  {
    echo "--- last 20 lines of agent_backend.log before restart ---"
    tail -n 20 "${LOG_DIR}/agent_backend.log"
    echo "--- end ---"
  } >> "${LOG_FILE}"
fi

# stop first: the process may be alive but wedged (hung, port bound, not
# answering), in which case start alone would refuse to act.
"${PROJECT_ROOT}/scripts/stop_agent_backend.sh" >>"${LOG_FILE}" 2>&1 9>&- || true

if "${PROJECT_ROOT}/scripts/start_agent_backend.sh" >>"${LOG_FILE}" 2>&1 9>&-; then
  log "restart OK"
else
  log "restart FAILED (exit $?) — see the start output above"
fi

# Keep the log from growing without bound; cron runs this every minute forever.
if [[ "$(wc -l < "${LOG_FILE}")" -gt "${MAX_LOG_LINES}" ]]; then
  tail -n "${MAX_LOG_LINES}" "${LOG_FILE}" > "${LOG_FILE}.tmp" && mv "${LOG_FILE}.tmp" "${LOG_FILE}"
fi

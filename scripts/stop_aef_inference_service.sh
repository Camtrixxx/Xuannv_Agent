#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${PROJECT_ROOT}/scripts/_common.sh"
load_env "${PROJECT_ROOT}"

PORT="${AEF_PORT:-7862}"
PID_FILE="${PROJECT_ROOT}/agent/runtime/pids/aef_inference.pid"
PATTERN="aef_inference\.server.*--port ${PORT}"

# Model teardown frees GPU memory, so allow a longer graceful window than the
# Agent gets before escalating to SIGKILL.
STOP_TIMEOUT="${STOP_TIMEOUT:-30}"

declare -a targets=()

if [[ -f "${PID_FILE}" ]]; then
  pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    targets+=("${pid}")
  fi
fi

while read -r pid; do
  [[ -z "${pid}" ]] && continue
  for known in "${targets[@]:-}"; do
    [[ "${known}" == "${pid}" ]] && continue 2
  done
  targets+=("${pid}")
done < <(service_pids "${PATTERN}")

if [[ "${#targets[@]}" -eq 0 ]]; then
  rm -f "${PID_FILE}"
  echo "AEF inference service is not running."
  exit 0
fi

stop_pids "AEF inference service" "${targets[@]}"
rm -f "${PID_FILE}"

#!/usr/bin/env bash
# Shared helpers for the start/stop/status scripts. Sourced, not executed.

# Load the gitignored .env so every script agrees on AGENT_PORT/AEF_PORT and
# secrets. Already-exported environment values take precedence.
load_env() {
  local root="$1"
  [[ -f "${root}/.env" ]] || return 0
  set -a
  # shellcheck disable=SC1091
  source "${root}/.env"
  set +a
}

# Print the PIDs matching a pgrep -f pattern, excluding this script and its
# ancestors so a stop script never targets itself.
service_pids() {
  local pattern="$1" pid
  local -a out=()
  while read -r pid; do
    [[ -z "${pid}" ]] && continue
    [[ "${pid}" == "$$" || "${pid}" == "${PPID}" ]] && continue
    out+=("${pid}")
  done < <(pgrep -f "${pattern}" 2>/dev/null || true)
  printf '%s\n' "${out[@]:-}" | sed '/^$/d'
}

# Wait until every given PID is gone. Returns 0 if all exited within timeout.
wait_for_exit() {
  local timeout="$1"; shift
  local deadline=$(( $(date +%s) + timeout )) pid alive
  while :; do
    alive=0
    for pid in "$@"; do
      kill -0 "${pid}" 2>/dev/null && alive=1 && break
    done
    [[ "${alive}" -eq 0 ]] && return 0
    [[ "$(date +%s)" -ge "${deadline}" ]] && return 1
    sleep 0.3
  done
}

# SIGTERM, wait for exit, then SIGKILL as a fallback. Returns non-zero only if
# a process survives both. STOP_TIMEOUT overrides the graceful-shutdown budget.
stop_pids() {
  local label="$1"; shift
  local -a pids=("$@")
  [[ "${#pids[@]}" -eq 0 ]] && return 0

  echo "Stopping ${label}: ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null || true

  if wait_for_exit "${STOP_TIMEOUT:-15}" "${pids[@]}"; then
    echo "${label} stopped."
    return 0
  fi

  echo "${label} did not exit in ${STOP_TIMEOUT:-15}s; sending SIGKILL."
  kill -9 "${pids[@]}" 2>/dev/null || true
  if wait_for_exit 5 "${pids[@]}"; then
    echo "${label} killed."
    return 0
  fi

  echo "ERROR: ${label} still alive after SIGKILL: ${pids[*]}" >&2
  return 1
}

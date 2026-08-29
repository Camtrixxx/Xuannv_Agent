#!/usr/bin/env bash
# Install (or refresh) the cron entries that keep the Agent alive.
#
#   * * * * *  watchdog: restart the Agent if /api/health stops answering
#   @reboot    start the Agent again after the machine comes back
#
# Idempotent: re-running replaces the previous Xuannv entries instead of
# stacking duplicates. Needs no sudo — this is the current user's crontab.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MARKER="# xuannv-agent watchdog"

current="$(crontab -l 2>/dev/null || true)"
# Drop any previous block so this script can be run repeatedly.
cleaned="$(printf '%s\n' "${current}" | grep -vF "${MARKER}" | sed '/^$/d')"

new_block="${MARKER}
* * * * * ${PROJECT_ROOT}/scripts/watchdog_agent.sh >/dev/null 2>&1 ${MARKER}
@reboot sleep 60 && ${PROJECT_ROOT}/scripts/watchdog_agent.sh >/dev/null 2>&1 ${MARKER}"

printf '%s\n%s\n' "${cleaned}" "${new_block}" | sed '/^$/d' | crontab -

echo "Installed cron entries:"
crontab -l | grep -F "${MARKER}"
echo
echo "Watchdog log: ${PROJECT_ROOT}/agent/runtime/logs/watchdog.log"

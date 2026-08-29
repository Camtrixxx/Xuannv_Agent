#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/_common.sh
source "${PROJECT_ROOT}/scripts/_common.sh"
load_env "${PROJECT_ROOT}"
AEF_PORT="${AEF_PORT:-7862}"
AGENT_PORT="${AGENT_PORT:-7870}"

# AEF only serves the Yajiang region and needs model weights under AEF_CODE_ROOT.
# Where those are absent it cannot start, which must not block the Agent: Harbin
# and Haidian do not touch AEF. Set AEF_REQUIRED=1 to make its failure fatal.
aef_ok=0
if curl --noproxy '*' -fsS "http://127.0.0.1:${AEF_PORT}/api/health" >/dev/null 2>&1; then
  echo "AEF inference service is already healthy on ${AEF_PORT}."
  aef_ok=1
elif "${PROJECT_ROOT}/scripts/start_aef_inference_service.sh"; then
  aef_ok=1
fi

if [[ "${aef_ok}" -ne 1 ]]; then
  if [[ "${AEF_REQUIRED:-0}" == "1" ]]; then
    echo "ERROR: AEF inference service failed to start and AEF_REQUIRED=1." >&2
    exit 1
  fi
  echo "WARNING: AEF inference service is unavailable — Yajiang tasks will fail." >&2
  echo "         Harbin and Haidian are unaffected; continuing to start the Agent." >&2
fi

if curl --noproxy '*' -fsS "http://127.0.0.1:${AGENT_PORT}/api/health" >/dev/null 2>&1; then
  echo "Agent backend is already healthy on ${AGENT_PORT}."
else
  "${PROJECT_ROOT}/scripts/start_agent_backend.sh"
fi

echo "Agent URL: http://127.0.0.1:${AGENT_PORT}/"
echo "API docs:  http://127.0.0.1:${AGENT_PORT}/api-docs"

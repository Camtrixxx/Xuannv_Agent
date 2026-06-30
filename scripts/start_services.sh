#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AEF_PORT="${AEF_PORT:-7862}"
AGENT_PORT="${AGENT_PORT:-7870}"

if curl --noproxy '*' -fsS "http://127.0.0.1:${AEF_PORT}/api/health" >/dev/null 2>&1; then
  echo "AEF inference service is already healthy on ${AEF_PORT}."
else
  "${PROJECT_ROOT}/scripts/start_aef_inference_service.sh"
fi

for _ in $(seq 1 60); do
  if curl --noproxy '*' -fsS "http://127.0.0.1:${AEF_PORT}/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if curl --noproxy '*' -fsS "http://127.0.0.1:${AGENT_PORT}/api/health" >/dev/null 2>&1; then
  echo "Agent backend is already healthy on ${AGENT_PORT}."
else
  "${PROJECT_ROOT}/scripts/start_agent_backend.sh"
fi

echo "Agent URL: http://127.0.0.1:${AGENT_PORT}/"
echo "API docs:  http://127.0.0.1:${AGENT_PORT}/api-docs"

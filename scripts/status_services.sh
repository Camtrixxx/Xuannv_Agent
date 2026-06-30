#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

"${PROJECT_ROOT}/scripts/status_aef_inference_service.sh"
echo
"${PROJECT_ROOT}/scripts/status_agent_backend.sh"

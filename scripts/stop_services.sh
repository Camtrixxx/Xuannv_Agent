#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

"${PROJECT_ROOT}/scripts/stop_agent_backend.sh"
"${PROJECT_ROOT}/scripts/stop_aef_inference_service.sh"

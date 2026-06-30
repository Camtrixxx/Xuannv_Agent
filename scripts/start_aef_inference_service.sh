#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-/opt/miniconda3/envs/hyh-dl/bin/python}"
HOST="${AEF_HOST:-127.0.0.1}"
PORT="${AEF_PORT:-7862}"

# AEF service reuses the training/model code and data that already live on this server.
# The model weights themselves are intentionally not stored in this Agent repository.
AEF_CODE_ROOT="${AEF_CODE_ROOT:-/data/heyuhang/yajiang-aef}"
AEF_CONFIG="${AEF_CONFIG:-${AEF_CODE_ROOT}/configs/yajiang_v1_2_continue_200.yaml}"
AEF_MANIFEST="${AEF_MANIFEST:-${AEF_CODE_ROOT}/data/full_npy/train.jsonl}"
AEF_DEPLOY_MODEL="${AEF_DEPLOY_MODEL:-${AEF_CODE_ROOT}/outputs/aef_hyh_yajiang_v1_2_continue_200/exports/aef_hyh_yajiang_v1_2_continue_200_deploy.pt}"
AEF_CACHE_DIR="${AEF_CACHE_DIR:-${AEF_CODE_ROOT}/outputs/aef_inference_service_v1_2_continue_200}"
AEF_DEVICE="${AEF_DEVICE:-auto}"

LOG_DIR="${PROJECT_ROOT}/agent/runtime/logs"
PID_DIR="${PROJECT_ROOT}/agent/runtime/pids"
LOG_FILE="${LOG_DIR}/aef_inference.log"
PID_FILE="${PID_DIR}/aef_inference.pid"

mkdir -p "${LOG_DIR}" "${PID_DIR}"
cd "${PROJECT_ROOT}"
export PYTHONPATH="${PROJECT_ROOT}:${AEF_CODE_ROOT}:${PYTHONPATH:-}"

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}" || true)"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" >/dev/null 2>&1; then
    echo "AEF inference service is already running: ${old_pid}"
    exit 0
  fi
fi

echo "Starting AEF inference service on ${HOST}:${PORT}"
nohup setsid "${PYTHON}" -m aef_inference.server \
  --host "${HOST}" \
  --port "${PORT}" \
  --config "${AEF_CONFIG}" \
  --manifest "${AEF_MANIFEST}" \
  --deploy-model "${AEF_DEPLOY_MODEL}" \
  --cache-dir "${AEF_CACHE_DIR}" \
  --device "${AEF_DEVICE}" \
  >"${LOG_FILE}" 2>&1 </dev/null &

echo $! > "${PID_FILE}"
echo "PID: $(cat "${PID_FILE}")"
echo "Log: ${LOG_FILE}"

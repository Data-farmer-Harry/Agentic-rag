#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${ROOT_DIR}/.data/kg-extraction"
PID_FILE="${STATE_DIR}/background.pid"
LATEST_LOG_FILE="${STATE_DIR}/latest-log.txt"

mkdir -p "${STATE_DIR}"

if [[ -f "${PID_FILE}" ]]; then
  existing_pid="$(tr -d '[:space:]' < "${PID_FILE}")"
  if [[ "${existing_pid}" =~ ^[1-9][0-9]*$ ]] && kill -0 "${existing_pid}" 2>/dev/null; then
    printf 'KG extraction is already running (PID %s).\n' "${existing_pid}" >&2
    exit 1
  fi
  rm -f "${PID_FILE}"
fi

timestamp="$(date '+%Y%m%d-%H%M%S')"
log_file="${STATE_DIR}/kg-extraction-${timestamp}.log"

cd "${ROOT_DIR}"
nohup "${ROOT_DIR}/scripts/run_kg_extraction.sh" \
  --full \
  --yes \
  --skip-errors \
  "$@" \
  > "${log_file}" 2>&1 < /dev/null &

pid=$!
printf '%s\n' "${pid}" > "${PID_FILE}"
printf '%s\n' "${log_file}" > "${LATEST_LOG_FILE}"

printf 'KG extraction started in the background.\n'
printf '  PID: %s\n' "${pid}"
printf '  Log: %s\n' "${log_file}"
printf '\nMonitor:\n  tail -f %q\n' "${log_file}"
printf 'Status:\n  ps -p %q\n' "${pid}"
printf 'Stop:\n  kill %q\n' "${pid}"

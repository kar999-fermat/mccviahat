#!/usr/bin/env bash
# scripts/model/reset_server.sh
#
# Restart the llama.cpp container between runs to clear the KV cache.
# All timeouts are read from model_config.sh (written by model_fetch.sh).
#
# Usage: bash scripts/model/reset_server.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/model_config.sh"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: model_config.sh not found."
  echo "Run first: bash scripts/model/model_fetch.sh [7b|70b|405b]"
  exit 1
fi

# shellcheck source=model_config.sh
source "$CONFIG_FILE"

T_START=$(date +%s)
mem_avail_gb() { awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo; }
MIN_AVAIL_GB=$(mem_avail_gb)

echo ">>> Stopping llama server (model=${MODEL_SIZE})..."
bash "$SCRIPT_DIR/llama_down.sh"

echo ">>> Starting llama server (model=${MODEL_SIZE}, load=${LOAD_MODE:-mmap})..."
bash "$SCRIPT_DIR/llama_up.sh"

finish() {
  local status="$1" secs=$(( $(date +%s) - T_START ))
  # One line per reset, so load times can be compared before/after changes.
  printf '%s,%s,%s,%s,%s,%s,%s\n' "$(date -Is)" "$(hostname -s)" "$MODEL_SIZE" \
    "${LOAD_MODE:-mmap}" "\"${MCC_SERVER_ARGS:-${SERVER_ARGS:-}}\"" "$secs" "$status" >> "${HOME}/mcc_reset_times.csv"
  echo ">>> Reset ${status} after ${secs}s (~$((secs / 60)) min); lowest free RAM during load: ${MIN_AVAIL_GB} GB"
  echo "    (logged to ~/mcc_reset_times.csv)"
}

echo ">>> Waiting for server to be ready (up to ${HEALTH_RETRIES}x${HEALTH_SLEEP}s)..."
for i in $(seq 1 "${HEALTH_RETRIES}"); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    echo ">>> Server is up and healthy (attempt ${i})"
    finish ok
    exit 0
  fi
  # Stop waiting if the container died (e.g. ran out of memory).
  if [ "$(docker inspect -f '{{.State.Running}}' mccviahat-llama 2>/dev/null)" != "true" ]; then
    echo "ERROR: llama container stopped while loading. Last log lines:" >&2
    docker logs --tail 30 mccviahat-llama >&2 || true
    if [ "$(docker inspect -f '{{.State.OOMKilled}}' mccviahat-llama 2>/dev/null)" = "true" ]; then
      echo "ERROR: it was killed for using too much memory." >&2
    fi
    finish container_died
    exit 1
  fi
  AV=$(mem_avail_gb)
  [ "$AV" -lt "$MIN_AVAIL_GB" ] && MIN_AVAIL_GB=$AV
  if [ $((i % 6)) -eq 1 ]; then
    echo "    attempt ${i}/${HEALTH_RETRIES} — loading ($(( $(date +%s) - T_START ))s, ${AV} GB RAM free)"
  fi
  sleep "${HEALTH_SLEEP}"
done

echo "ERROR: Server did not become healthy within $((HEALTH_RETRIES * HEALTH_SLEEP))s"
finish timeout
exit 1

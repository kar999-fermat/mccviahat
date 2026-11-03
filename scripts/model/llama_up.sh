#!/usr/bin/env bash
set -euo pipefail

# Usage: bash scripts/model/llama_up.sh
#
# All parameters are read from model_config.sh (written by model_fetch.sh).
# To switch models: bash scripts/model/model_fetch.sh [7b|70b|405b]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/model_config.sh"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: model_config.sh not found."
  echo "Run first: bash scripts/model/model_fetch.sh [7b|70b|405b]"
  exit 1
fi

# shellcheck source=model_config.sh
source "$CONFIG_FILE"

IMAGE="mccviahat-llama:dev"
NAME="mccviahat-llama"
PORT="8000"
N_THREADS=$(nproc)

# How weights are loaded (written by model_fetch.sh; older configs = mmap).
# Uses llama.cpp's --load-mode flag (the llama.cpp version pinned in the
# Dockerfile replaced the old --no-mmap / --mlock flags with it).
#   mmap    : llama.cpp default; pages come from the file on demand
#   no-mmap : read all weights into RAM before /health reports ready
#   mlock   : mmap, but lock every page in RAM so none are paged out
#   dio     : like no-mmap, but reads with DirectIO, skipping the page cache
LOAD_MODE="${LOAD_MODE:-mmap}"
LOAD_ARGS=()
DOCKER_EXTRA=()
case "$LOAD_MODE" in
  mmap)    ;;
  no-mmap) LOAD_ARGS=(--load-mode none) ;;
  mlock)   LOAD_ARGS=(--load-mode mmap+mlock); DOCKER_EXTRA=(--ulimit memlock=-1:-1) ;;
  dio)     LOAD_ARGS=(--load-mode dio) ;;
  *) echo "ERROR: unknown LOAD_MODE '${LOAD_MODE}'" >&2; exit 1 ;;
esac
# Optional extra llama-server flags, saved in model_config.sh by
#   MCC_SERVER_ARGS="--no-repack" bash scripts/model/model_fetch.sh 405b
# (an MCC_SERVER_ARGS set in the shell overrides it for a quick test).
SERVER_ARGS="${MCC_SERVER_ARGS:-${SERVER_ARGS:-}}"
read -r -a EXTRA_ARGS <<< "${SERVER_ARGS}"


# If already running, do nothing
if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "Already running: $NAME"
  docker ps --filter "name=$NAME"
  exit 0
fi

# If an old stopped container exists, remove it
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  docker rm "$NAME" >/dev/null
fi

echo "Starting llama server: $NAME  model=${MODEL_SIZE}  ctx=${CTX_SIZE}  cpuset=${CPUSET}  load=${LOAD_MODE} ${SERVER_ARGS}"
docker run -d \
  --name "$NAME" \
  --init \
  --stop-timeout 60 \
  ${DOCKER_EXTRA[@]+"${DOCKER_EXTRA[@]}"} \
  --cpuset-cpus "${CPUSET}" \
  -p "${PORT}:8000" \
  -v "${MODEL_DIR}:/models" \
  "$IMAGE" \
  /opt/llama.cpp/build/bin/llama-server \
    --host 0.0.0.0 --port 8000 \
    --model "${MODEL_PATH_IN_CONTAINER}" \
    --ctx-size "${CTX_SIZE}" \
    --threads "${N_THREADS}" \
    --threads-batch "${N_THREADS}" \
    --parallel 1 \
    --override-kv tokenizer.ggml.eos_token_id=int:-1 \
    ${LOAD_ARGS[@]+"${LOAD_ARGS[@]}"} \
    ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} \
  >/dev/null

echo "Started."
docker ps --filter "name=$NAME"
echo "PID: $(docker inspect -f '{{.State.Pid}}' "$NAME")"

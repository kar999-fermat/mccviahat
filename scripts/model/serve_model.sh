#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   bash scripts/model/serve_model.sh          # start serving the 405B shards
#   bash scripts/model/serve_model.sh stop     # stop serving
#
# Run this on ONE node that already has the 405B shards (after
# `bash scripts/model/model_fetch.sh 405b` finished there). Other nodes in the
# same cluster can then copy the shards from this node over the cluster
# network, instead of every node downloading 246 GB from Hugging Face:
#
#   MCC_MODEL_MIRROR=http://<this-node-hostname>:8888 bash scripts/model/model_fetch.sh 405b
#
# Uses nginx in Docker because it supports resumable, multi-connection
# (HTTP Range) downloads. Files are served read-only.
#
# Stop the mirror before you start recording traces on this node, so serving
# files does not add load during an experiment (a reboot also stops it).

NAME="mcc-model-mirror"
PORT="${MCC_MIRROR_PORT:-8888}"

if [ "${1:-}" = "stop" ]; then
  docker rm -f "$NAME" >/dev/null 2>&1 && echo "Mirror stopped." || echo "Mirror was not running."
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/model_config.sh"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: run 'bash scripts/model/model_fetch.sh 405b' on this node first." >&2
  exit 1
fi
# shellcheck source=model_config.sh
source "$CONFIG_FILE"

if [ "$MODEL_SIZE" != "405b" ]; then
  echo "ERROR: model_config.sh is set to ${MODEL_SIZE}; run 'bash scripts/model/model_fetch.sh 405b' first." >&2
  exit 1
fi

N_SHARDS=$(find "$MODEL_DIR" -maxdepth 1 -name '*-of-00006.gguf' -size +0 | wc -l)
if [ "$N_SHARDS" -lt 6 ]; then
  echo "ERROR: only ${N_SHARDS}/6 shards in ${MODEL_DIR}; finish the download first." >&2
  exit 1
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p "${PORT}:80" \
  -v "${MODEL_DIR}:/usr/share/nginx/html:ro" \
  nginx:alpine >/dev/null

sleep 2
FIRST=$(basename "$MODEL_FILE")
if curl -sfI "http://localhost:${PORT}/${FIRST}" >/dev/null; then
  echo "Serving ${MODEL_DIR} on port ${PORT}."
  echo
  echo "On each OTHER node, run:"
  echo "  MCC_MODEL_MIRROR=http://$(hostname -f):${PORT} bash scripts/model/model_fetch.sh 405b"
  echo
  echo "When every node has the shards: bash scripts/model/serve_model.sh stop"
else
  echo "ERROR: mirror started but is not answering on port ${PORT}." >&2
  docker logs --tail 20 "$NAME" >&2 || true
  exit 1
fi

#!/usr/bin/env bash
set -euo pipefail

# Usage: bash scripts/model/llama_down.sh
#
# Stops the llama container regardless of which model is running.
# Model size is read from model_config.sh for display purposes only.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/model_config.sh"

MODEL_SIZE="unknown"
if [ -f "$CONFIG_FILE" ]; then
  # shellcheck source=model_config.sh
  source "$CONFIG_FILE"
fi

NAME="mccviahat-llama"

if ! docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "Not running: $NAME"
  exit 0
fi

# A server holding hundreds of GB can take minutes to exit. Killing it at once
# with `docker rm -f` could fail with "the container's main process becomes a
# zombie during shutdown" and break the reset. Ask it to stop cleanly first,
# then keep trying to remove it until it is really gone.
docker stop -t 60 "$NAME" >/dev/null 2>&1 || true
for i in $(seq 1 60); do
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  if ! docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "Stopped and removed: $NAME  (model=${MODEL_SIZE})"
    exit 0
  fi
  [ $((i % 6)) -eq 1 ] && echo "    waiting for the old server to finish exiting... (${i}0s)"
  sleep ${MCC_DOWN_SLEEP:-10}
done
echo "ERROR: could not remove $NAME after 10 min. Rebooting the node (sudo reboot) clears it." >&2
exit 1

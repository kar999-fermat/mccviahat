#!/usr/bin/env bash
set -euo pipefail

# Usage: bash scripts/model/model_fetch.sh [7b|70b|405b]
#
# Downloads the model if not already present, then writes
# scripts/model/model_config.sh which is sourced by all other scripts.
# To switch models just re-run this script with the other size.

MODEL_SIZE="${1:-7b}"

HOME_MODEL_DIR="${HOME}/model_cache/llama"
EXTRA_FS="/mydata"  # CloudLab extra disk; the home disk is only ~60 GB
MODEL_DIR="${MCC_MODEL_DIR:-${HOME_MODEL_DIR}}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/model_config.sh"

case "$MODEL_SIZE" in
  7b)
    MODEL_FILE="${MODEL_DIR}/llama-2-7b.Q4_K_M.gguf"
    MODEL_PATH_IN_CONTAINER="/models/llama-2-7b.Q4_K_M.gguf"
    URL="https://huggingface.co/TheBloke/Llama-2-7B-GGUF/resolve/main/llama-2-7b.Q4_K_M.gguf"
    CTX_SIZE=2048
    CPUSET="0-61"
    RESET_TIMEOUT=90
    HEALTH_RETRIES=30
    HEALTH_SLEEP=2
    REQUEST_TIMEOUT=600
    BASELINE_S=2
    PROC_INTERVAL_S=0.1
    ;;
  70b)
    MODEL_FILE="${MODEL_DIR}/llama-3.1-70b.Q4_K_M.gguf"
    MODEL_PATH_IN_CONTAINER="/models/llama-3.1-70b.Q4_K_M.gguf"
    URL="https://huggingface.co/bartowski/Meta-Llama-3.1-70B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-70B-Instruct-Q4_K_M.gguf"
    CTX_SIZE=2048
    CPUSET="0-61"
    RESET_TIMEOUT=300
    HEALTH_RETRIES=60
    HEALTH_SLEEP=5
    REQUEST_TIMEOUT=3600
    BASELINE_S=5
    PROC_INTERVAL_S=0.1
    ;;
  405b)
    # Original Meta Llama 3.1 Instruct weights, Q4_K_M, in six GGUF shards.
    # llama.cpp loads all shards when given the first shard's path.
    # 246 GB does not fit on the home disk: use the CloudLab extra disk,
    # creating it on first use (needs passwordless sudo, as on CloudLab).
    if [ -z "${MCC_MODEL_DIR:-}" ]; then
      if ! mountpoint -q "$EXTRA_FS" 2>/dev/null; then
        echo "Creating ${EXTRA_FS} on the unused local disk..."
        sudo mkdir -p "$EXTRA_FS"
        sudo /usr/local/etc/emulab/mkextrafs.pl -f "$EXTRA_FS"
      fi
      sudo chown "$USER" "$EXTRA_FS"
      MODEL_DIR="${EXTRA_FS}/model_cache/llama"
    fi
    MODEL_DIR="${MODEL_DIR}/405b"
    # Move shards an earlier run saved on the home disk; partial files
    # resume where they stopped.
    if [ -d "${HOME_MODEL_DIR}/405b" ] && [ "${HOME_MODEL_DIR}/405b" != "$MODEL_DIR" ]; then
      echo "Moving earlier 405B download from ${HOME_MODEL_DIR}/405b to ${MODEL_DIR}"
      mkdir -p "$MODEL_DIR"
      mv "${HOME_MODEL_DIR}/405b/"* "$MODEL_DIR/" 2>/dev/null || true
      rmdir "${HOME_MODEL_DIR}/405b" 2>/dev/null || true
    fi
    MODEL_STEM="Meta-Llama-3.1-405B-Instruct-Q4_K_M"
    MODEL_FILE="${MODEL_DIR}/${MODEL_STEM}-00001-of-00006.gguf"
    MODEL_PATH_IN_CONTAINER="/models/${MODEL_STEM}-00001-of-00006.gguf"
    MODEL_REPO="bullerwins/Meta-Llama-3.1-405B-Instruct-GGUF"
    CTX_SIZE=2048
    CPUSET="0-61"
    RESET_TIMEOUT=2400
    HEALTH_RETRIES=180
    HEALTH_SLEEP=10
    REQUEST_TIMEOUT=14400
    BASELINE_S=5
    PROC_INTERVAL_S=0.1
    ;;
  *)
    echo "Usage: $0 [7b|70b|405b]"
    exit 1
    ;;
esac

mkdir -p "$MODEL_DIR"

# Download only if not already present
if [ "$MODEL_SIZE" = "405b" ]; then
  echo "405B Q4_K_M: about 246 GB of weights across six files."
  echo "Recommended CPU node: 384 GB RAM and at least 300 GB free disk."
  echo "Model directory: $MODEL_DIR"
  # Stop early instead of failing mid-shard with curl error 23.
  HAVE_GB=$(du -s --block-size=1G "$MODEL_DIR" | cut -f1)
  FREE_GB=$(df --output=avail --block-size=1G "$MODEL_DIR" | tail -1 | tr -d ' ')
  if [ $((HAVE_GB + FREE_GB)) -lt 260 ]; then
    echo "ERROR: ${FREE_GB} GB free in $MODEL_DIR (${HAVE_GB} GB already downloaded); need ~260 GB in total." >&2
    echo "Set MCC_MODEL_DIR to a larger disk." >&2
    exit 1
  fi
  for part in 1 2 3 4 5 6; do
    printf -v SHARD_NAME '%s-%05d-of-00006.gguf' "$MODEL_STEM" "$part"
    SHARD_FILE="${MODEL_DIR}/${SHARD_NAME}"
    if [ -s "$SHARD_FILE" ]; then
      echo "Already downloaded: $SHARD_FILE"
      continue
    fi
    echo "Downloading shard ${part}/6: $SHARD_NAME"
    CURL_AUTH=()
    if [ -n "${HF_TOKEN:-}" ]; then
      CURL_AUTH=(--header "Authorization: Bearer ${HF_TOKEN}")
    fi
    curl --fail --location --retry 5 --continue-at - \
      ${CURL_AUTH[@]+"${CURL_AUTH[@]}"} \
      --output "${SHARD_FILE}.part" \
      "https://huggingface.co/${MODEL_REPO}/resolve/main/${SHARD_NAME}"
    if [ ! -s "${SHARD_FILE}.part" ]; then
      echo "ERROR: empty download for $SHARD_NAME" >&2
      exit 1
    fi
    mv "${SHARD_FILE}.part" "$SHARD_FILE"
  done
elif [ -s "$MODEL_FILE" ]; then
  echo "Model already exists: $MODEL_FILE"
  ls -lh "$MODEL_FILE"
else
  if [ -z "${HF_TOKEN:-}" ]; then
    echo "ERROR: HF_TOKEN is not set."
    echo "Set it with: export HF_TOKEN='your_token_here'"
    exit 1
  fi
  echo "Downloading $MODEL_SIZE model to: $MODEL_FILE"
  TMP_FILE="${MODEL_FILE}.part"
  rm -f "$TMP_FILE"
  wget --header="Authorization: Bearer ${HF_TOKEN}" -O "$TMP_FILE" "$URL"
  mv "$TMP_FILE" "$MODEL_FILE"
  echo "Downloaded:"
  ls -lh "$MODEL_FILE"
fi

# Always write config (even if model already existed)
cat > "$CONFIG_FILE" <<EOF
# Auto-generated by model_fetch.sh — do not edit manually.
# Re-run: bash scripts/model/model_fetch.sh [7b|70b|405b]

MODEL_SIZE="${MODEL_SIZE}"
MODEL_FILE="${MODEL_FILE}"
MODEL_PATH_IN_CONTAINER="${MODEL_PATH_IN_CONTAINER}"
MODEL_DIR="${MODEL_DIR}"
CTX_SIZE=${CTX_SIZE}
CPUSET="${CPUSET}"
RESET_TIMEOUT=${RESET_TIMEOUT}
HEALTH_RETRIES=${HEALTH_RETRIES}
HEALTH_SLEEP=${HEALTH_SLEEP}
REQUEST_TIMEOUT=${REQUEST_TIMEOUT}
BASELINE_S=${BASELINE_S}
PROC_INTERVAL_S=${PROC_INTERVAL_S}
EOF

echo ""
echo "Config written: ${CONFIG_FILE}"
echo "  MODEL_SIZE      = ${MODEL_SIZE}"
echo "  REQUEST_TIMEOUT = ${REQUEST_TIMEOUT}s"
echo "  RESET_TIMEOUT   = ${RESET_TIMEOUT}s"
echo "  HEALTH_RETRIES  = ${HEALTH_RETRIES} x ${HEALTH_SLEEP}s"
echo "  BASELINE_S      = ${BASELINE_S}s"
echo ""
echo "All scripts now use the ${MODEL_SIZE} model."
echo "To switch: bash scripts/model/model_fetch.sh [7b|70b|405b]"

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
    LOAD_MODE_DEFAULT="mmap"
    ;;
  70b)
    MODEL_FILE="${MODEL_DIR}/llama-3.1-70b.Q4_K_M.gguf"
    MODEL_PATH_IN_CONTAINER="/models/llama-3.1-70b.Q4_K_M.gguf"
    URL="https://huggingface.co/bartowski/Meta-Llama-3.1-70B-Instruct-GGUF/resolve/main/Meta-Llama-3.1-70B-Instruct-Q4_K_M.gguf"
    CTX_SIZE=2048
    CPUSET="0-61"
    # Cold load after a reboot can take over 5 min on slower disks (it timed
    # out at 300 s on a c8220), so allow up to 15 min. Only the waiting time
    # changes; how the model is loaded is the same as before.
    RESET_TIMEOUT=1000
    HEALTH_RETRIES=180
    HEALTH_SLEEP=5
    REQUEST_TIMEOUT=3600
    BASELINE_S=5
    PROC_INTERVAL_S=0.1
    LOAD_MODE_DEFAULT="mmap"   # unchanged, so new 70b data matches old 70b data
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
    # Loading 246 GB from a spinning disk can take ~30 min, longer than the
    # old 180 x 10 s health window, so the reset could give up mid-load.
    RESET_TIMEOUT=4200
    HEALTH_RETRIES=400
    HEALTH_SLEEP=10
    REQUEST_TIMEOUT=14400
    BASELINE_S=5
    PROC_INTERVAL_S=0.1
    # Read every weight into RAM before /health reports ready, so no trace
    # starts while weights are still being paged in from disk.
    LOAD_MODE_DEFAULT="no-mmap"
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
  N_DONE=$(find "$MODEL_DIR" -maxdepth 1 -name "${MODEL_STEM}-0000?-of-00006.gguf" -size +0 | wc -l)
  if [ "$N_DONE" -lt 6 ] && [ $((HAVE_GB + FREE_GB)) -lt 260 ]; then
    echo "ERROR: ${FREE_GB} GB free in $MODEL_DIR (${HAVE_GB} GB already downloaded); need ~260 GB in total." >&2
    echo "Set MCC_MODEL_DIR to a larger disk." >&2
    exit 1
  fi
  # Where the shards come from:
  #   default            -> Hugging Face
  #   MCC_MODEL_MIRROR   -> another node already holding the shards, served by
  #                         scripts/model/serve_model.sh (fast, inside the cluster)
  #   MCC_MODEL_DIR      -> a mounted CloudLab dataset that already has them
  if [ -n "${MCC_MODEL_MIRROR:-}" ]; then
    BASE_URL="${MCC_MODEL_MIRROR%/}"
    echo "Source: cluster mirror ${BASE_URL}"
  else
    BASE_URL="https://huggingface.co/${MODEL_REPO}/resolve/main"
    echo "Source: Hugging Face (${MODEL_REPO})"
  fi

  # Expected size + SHA256 of every shard, from the Hugging Face API.
  MANIFEST="${MODEL_DIR}/.manifest.tsv"
  if curl -sSf --max-time 60 "https://huggingface.co/api/models/${MODEL_REPO}/tree/main" -o "${MANIFEST}.json" 2>/dev/null \
     && python3 -c '
import json, sys
for f in json.load(open(sys.argv[1])):
    if f.get("path", "").endswith(".gguf") and f.get("lfs"):
        print(f["path"], f["lfs"]["size"], f["lfs"]["oid"], sep="\t")
' "${MANIFEST}.json" > "$MANIFEST" && [ -s "$MANIFEST" ]; then
    echo "Got expected sizes/checksums for $(wc -l < "$MANIFEST") shards."
  else
    echo "WARNING: could not get checksums from Hugging Face; only checking that files are non-empty." >&2
    : > "$MANIFEST"
  fi
  rm -f "${MANIFEST}.json"
  expected_size() { awk -F'\t' -v n="$1" '$1==n{print $2}' "$MANIFEST"; }
  expected_sha()  { awk -F'\t' -v n="$1" '$1==n{print $3}' "$MANIFEST"; }

  # Work out which shards are missing or incomplete.
  TODO=()
  for part in 1 2 3 4 5 6; do
    printf -v SHARD_NAME '%s-%05d-of-00006.gguf' "$MODEL_STEM" "$part"
    SHARD_FILE="${MODEL_DIR}/${SHARD_NAME}"
    WANT=$(expected_size "$SHARD_NAME")
    if [ -s "$SHARD_FILE" ]; then
      HAVE=$(stat -c %s "$SHARD_FILE")
      if [ -z "$WANT" ] || [ "$HAVE" = "$WANT" ]; then
        echo "Already downloaded: $SHARD_NAME"
        continue
      fi
      echo "Wrong size ($HAVE, expected $WANT); resuming: $SHARD_NAME"
      mv "$SHARD_FILE" "${SHARD_FILE}.part"
    fi
    TODO+=("$SHARD_NAME")
  done

  if [ "${#TODO[@]}" -gt 0 ]; then
    if [ ! -w "$MODEL_DIR" ]; then
      echo "ERROR: shards missing and ${MODEL_DIR} is read-only: ${TODO[*]}" >&2
      exit 1
    fi
    if ! command -v aria2c >/dev/null 2>&1; then
      echo "Installing aria2 (parallel downloader)..."
      if ! sudo apt-get install -y aria2 >/dev/null 2>&1; then
        sudo apt-get update -y >/dev/null 2>&1 || true
        sudo apt-get install -y aria2 >/dev/null 2>&1 || true
      fi
    fi
    AUTH=()
    if [ -z "${MCC_MODEL_MIRROR:-}" ] && [ -n "${HF_TOKEN:-}" ]; then
      AUTH=(--header "Authorization: Bearer ${HF_TOKEN}")
    fi
    # .part files that already have the full size only need checking.
    DL=()
    for n in "${TODO[@]}"; do
      WANT=$(expected_size "$n")
      if [ -n "$WANT" ] && [ -f "${MODEL_DIR}/${n}.part" ] && [ "$(stat -c %s "${MODEL_DIR}/${n}.part")" = "$WANT" ]; then
        continue
      fi
      DL+=("$n")
    done
    T0=$(date +%s)
    if [ "${#DL[@]}" -eq 0 ]; then
      echo "All remaining shards are fully downloaded; checking them."
    elif command -v aria2c >/dev/null 2>&1; then
      # All missing shards at once, 8 connections each, resumable.
      LIST="${MODEL_DIR}/.aria2_list"
      : > "$LIST"
      for n in "${DL[@]}"; do
        printf '%s/%s\n  out=%s.part\n' "$BASE_URL" "$n" "$n" >> "$LIST"
      done
      echo "Downloading ${#DL[@]} shard(s) in parallel with aria2c..."
      aria2c --input-file="$LIST" --dir="$MODEL_DIR" \
        --max-concurrent-downloads=6 --split=8 --max-connection-per-server=8 \
        --min-split-size=64M --continue=true --file-allocation=none \
        --auto-file-renaming=false \
        --max-tries=20 --retry-wait=10 --summary-interval=60 --console-log-level=warn \
        ${AUTH[@]+"${AUTH[@]}"} || echo "WARNING: aria2c reported errors; checking what arrived." >&2
      rm -f "$LIST"
    else
      echo "aria2c unavailable; falling back to one curl per shard, all shards in parallel."
      PIDS=()
      for n in "${DL[@]}"; do
        curl --fail --location --retry 10 --retry-delay 10 --continue-at - --silent --show-error \
          ${AUTH[@]+"${AUTH[@]}"} --output "${MODEL_DIR}/${n}.part" "${BASE_URL}/${n}" &
        PIDS+=($!)
      done
      for p in "${PIDS[@]}"; do wait "$p" || echo "WARNING: a curl download failed; checking what arrived." >&2; done
    fi
    echo "Download finished in $(( ($(date +%s) - T0) / 60 )) min."

    # Check each new shard (size, then SHA256 unless MCC_VERIFY_SHA=0) before
    # giving it its final name. Check all of them, then report.
    BAD=()
    for n in "${TODO[@]}"; do
      PART="${MODEL_DIR}/${n}.part"
      if [ ! -s "$PART" ]; then
        echo "ERROR: $n is missing or empty." >&2; BAD+=("$n"); continue
      fi
      WANT=$(expected_size "$n")
      if [ -n "$WANT" ] && [ "$(stat -c %s "$PART")" != "$WANT" ]; then
        echo "ERROR: $n is incomplete ($(stat -c %s "$PART") of $WANT bytes)." >&2; BAD+=("$n"); continue
      fi
      SHA=$(expected_sha "$n")
      if [ -n "$SHA" ] && [ "${MCC_VERIFY_SHA:-1}" != "0" ]; then
        echo "Verifying SHA256 of $n ..."
        if [ "$(sha256sum "$PART" | cut -d' ' -f1)" != "$SHA" ]; then
          echo "ERROR: checksum mismatch for $n; deleting it." >&2
          rm -f "$PART"; BAD+=("$n"); continue
        fi
      fi
      mv "$PART" "${MODEL_DIR}/${n}"
      echo "OK: $n"
    done
    if [ "${#BAD[@]}" -gt 0 ]; then
      echo "ERROR: ${#BAD[@]} shard(s) not ready. Re-run the same command; finished parts are kept and partial ones resume." >&2
      exit 1
    fi
  fi

  MEM_GB=$(awk '/MemTotal/{print int($2/1048576)}' /proc/meminfo)
  if [ "$MEM_GB" -lt 300 ]; then
    echo "WARNING: this node has ${MEM_GB} GB RAM; 405B needs ~300+ GB." >&2
  fi
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

# Nodes with fewer than 64 CPU threads (e.g. c8220: 40) can't use 0-61.
# Keep the same layout (all but the last 2 threads), so c6420 stays 0-61.
NCPU=$(nproc)
if [ "$NCPU" -lt 64 ]; then
  CPUSET="0-$((NCPU - 3))"
  echo "Note: this node has ${NCPU} CPU threads; using CPUSET=${CPUSET}."
fi

LOAD_MODE="${MCC_LOAD_MODE:-$LOAD_MODE_DEFAULT}"
case "$LOAD_MODE" in mmap|no-mmap|mlock|dio) ;; *) echo "ERROR: MCC_LOAD_MODE must be mmap, no-mmap, mlock or dio" >&2; exit 1;; esac

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
LOAD_MODE="${LOAD_MODE}"
SERVER_ARGS="${MCC_SERVER_ARGS:-}"
EOF

echo ""
echo "Config written: ${CONFIG_FILE}"
echo "  MODEL_SIZE      = ${MODEL_SIZE}"
echo "  REQUEST_TIMEOUT = ${REQUEST_TIMEOUT}s"
echo "  RESET_TIMEOUT   = ${RESET_TIMEOUT}s"
echo "  HEALTH_RETRIES  = ${HEALTH_RETRIES} x ${HEALTH_SLEEP}s"
echo "  BASELINE_S      = ${BASELINE_S}s"
echo "  LOAD_MODE       = ${LOAD_MODE}"
[ -n "${MCC_SERVER_ARGS:-}" ] && echo "  SERVER_ARGS     = ${MCC_SERVER_ARGS}"
echo ""
echo "All scripts now use the ${MODEL_SIZE} model."
echo "To switch: bash scripts/model/model_fetch.sh [7b|70b|405b]"

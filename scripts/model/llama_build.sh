#!/usr/bin/env bash
set -euo pipefail

IMAGE="mccviahat-llama:dev"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "Building image: $IMAGE"
cd "$HERE/docker/llama_server"
# Always rebuild to pick up any updates. llama.cpp version is pinned in the
# Dockerfile; override with LLAMA_CPP_REF=<tag-or-commit>.
BUILD_ARGS=()
if [ -n "${LLAMA_CPP_REF:-}" ]; then
  BUILD_ARGS=(--build-arg "LLAMA_CPP_REF=${LLAMA_CPP_REF}")
fi
docker build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -t "$IMAGE" .
echo "llama.cpp version in image: $(docker inspect -f '{{index .Config.Labels "llama_cpp_ref"}}' "$IMAGE")"

#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_SEMANTIC_IMAGE:-namma-semantic:latest}"

docker build \
  -f "$ROOT/semantic/Dockerfile" \
  -t "$IMAGE" \
  "$ROOT"

echo "Built $IMAGE"


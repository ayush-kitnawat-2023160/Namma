#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_NEXTGEN_IMAGE:-namma-nextgen:latest}"

exec docker build \
  --network host \
  -f "$ROOT/nextgen/Dockerfile" \
  -t "$IMAGE" \
  "$ROOT"

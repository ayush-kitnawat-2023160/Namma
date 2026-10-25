#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_SUGAR_IMAGE:-namma-sugar:latest}"

exec docker build \
  --network host \
  -f "$ROOT/nextgen/sugar/Dockerfile" \
  -t "$IMAGE" \
  "$ROOT"

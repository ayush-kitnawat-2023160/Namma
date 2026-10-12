#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_IMAGE:-namma-iitb:latest}"
echo "Building $IMAGE from $ROOT"
docker build --progress=plain -t "$IMAGE" "$ROOT"
echo "Build complete. Next: ./run.sh smoke ; on GPU host: ./run.sh check"


#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${NAMMA_SERVER:-}" != 1 ]]; then
  echo 'Server build only. On the college server use: NAMMA_SERVER=1 ./build_precision.sh' >&2
  exit 2
fi
MEM_KB="$(awk '/MemTotal:/ {print $2}' /proc/meminfo)"
(( MEM_KB >= 100 * 1024 * 1024 )) || { echo 'Refusing build: need >=100 GiB host RAM.' >&2; exit 2; }
AVAILABLE_KB="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
(( AVAILABLE_KB >= 32 * 1024 * 1024 )) || { echo 'Refusing build: need >=32 GiB available host RAM.' >&2; exit 2; }
FREE_KB="$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
(( FREE_KB >= 40 * 1024 * 1024 )) || { echo 'Refusing build: need >=40 GiB free disk.' >&2; exit 2; }
DOCKER_LIMITS="$(docker info --format '{{.MemoryLimit}} {{.SwapLimit}}')"
[[ "$DOCKER_LIMITS" == 'true true' ]] || { echo "Refusing build: Docker memory/swap cgroup limits are not enforced ($DOCKER_LIMITS)." >&2; exit 2; }
docker image inspect "${NAMMA_NEXTGEN_IMAGE:-namma-nextgen:latest}" >/dev/null
mkdir -p "$ROOT/.runtime"
exec 9>"$ROOT/.runtime/precision.lock"
flock -n 9 || { echo 'Another Precision job/build holds the lock.' >&2; exit 2; }
# Use Docker's classic builder deliberately: it resolves the existing local
# base image and enforces these build-container limits. BuildKit ignores the
# equivalent `docker build --memory` flag. Fail if classic build is unavailable;
# never silently retry an unlimited build.
echo 'Building CUDA COLMAP with 2 compiler jobs and a 24 GiB build-container ceiling.'
DOCKER_BUILDKIT=0 docker build --memory=24g --memory-swap=24g \
  --cpu-period=100000 --cpu-quota=400000 \
  --build-arg "BASE_IMAGE=${NAMMA_NEXTGEN_IMAGE:-namma-nextgen:latest}" \
  --build-arg BUILD_JOBS=2 \
  -f "$ROOT/precision/Dockerfile" -t "${NAMMA_PRECISION_IMAGE:-namma-precision:latest}" "$ROOT"

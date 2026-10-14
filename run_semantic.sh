#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_SEMANTIC_IMAGE:-namma-semantic:latest}"
CMD="${1:-help}"

mkdir -p \
  "$ROOT/outputs/nextgen" \
  "$ROOT/outputs/semantic" \
  "$ROOT/semantic/checkpoints"

# The semantic extension can READ the proven NextGen reconstruction, but it
# cannot modify it. All new artifacts are written under outputs/semantic/.
COMMON=(
  --rm
  --ipc=host
  --shm-size=32g
  --cap-add=SYS_PTRACE
  --security-opt seccomp=unconfined
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
  -e "HOST_UID=$(id -u)"
  -e "HOST_GID=$(id -g)"
  -v "$ROOT/outputs/nextgen:/workspace/nextgen:ro"
  -v "$ROOT/outputs/semantic:/workspace/semantic"
  -v "$ROOT/semantic/scripts:/opt/namma-semantic/scripts:ro"
  -v "$ROOT/semantic/config:/opt/namma-semantic/config:ro"
  -v "$ROOT/semantic/checkpoints:/workspace/checkpoints:ro"
)

for name in \
  NS_MIN_DEPTH_CONFIDENCE NS_LAYOUT_DISTANCE_THRESHOLD NS_LAYOUT_MIN_POINTS \
  NS_TSDF_VOXEL_SIZE NS_TSDF_TRUNCATION NS_MAX_MESH_TRIANGLES \
  NS_OBJECT_CLASSES NS_STUFF_CLASSES
do
  [[ -n "${!name:-}" ]] && COMMON+=( -e "$name=${!name}" )
done

case "$CMD" in
  help|-h|--help|check|status|source|fix-perms|shell|bash) GPU_ARGS=() ;;
  *) GPU_ARGS=(--gpus all) ;;
esac

TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS=(-it)

exec docker run \
  "${TTY_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  "${COMMON[@]}" \
  "$IMAGE" "$@"

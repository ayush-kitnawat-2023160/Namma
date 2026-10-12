#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_NEXTGEN_IMAGE:-namma-nextgen:latest}"
CMD="${1:-help}"

mkdir -p \
  "$ROOT/data" \
  "$ROOT/outputs/nextgen" \
  "$ROOT/outputs/logs" \
  "$ROOT/nextgen/checkpoints"

COMMON=(
  --rm
  --ipc=host
  --shm-size=32g
  --cap-add=SYS_PTRACE
  --security-opt seccomp=unconfined
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
  -e "HOST_UID=$(id -u)"
  -e "HOST_GID=$(id -g)"
  -v "$ROOT/data:/workspace/data:ro"
  -v "$ROOT/outputs:/workspace/outputs"
  -v "$ROOT/config:/opt/namma/config:ro"
  -v "$ROOT/nextgen/scripts:/opt/namma-nextgen/scripts:ro"
  -v "$ROOT/nextgen/config:/opt/namma-nextgen/config:ro"
  -v "$ROOT/nextgen/checkpoints:/workspace/checkpoints:ro"
)

for name in \
  NG_CAMERA_PROFILE NG_CALIB_VIEWS NG_CALIB_MODEL NG_TARGET_VIEWS NG_MAX_CANDIDATES NG_MIN_BLUR \
  NG_DUPLICATE_DISTANCE NG_VGGT_CHECKPOINT NG_VGGT_RESOLUTION NG_VGGT_MODE \
  NG_TEMPORAL_WINDOW NG_VISUAL_NEIGHBORS NG_GS_ITERS NG_GS_RESOLUTION \
  NG_DEPTH_WEIGHT_INIT NG_DEPTH_WEIGHT_FINAL
do
  [[ -n "${!name:-}" ]] && COMMON+=( -e "$name=${!name}" )
done

case "$CMD" in
  help|-h|--help|check|inspect|fix-perms|shell|bash) GPU_ARGS=() ;;
  prepare)
    if [[ -n "${NG_CAMERA_PROFILE:-}" ]]; then GPU_ARGS=(); else GPU_ARGS=(--gpus all); fi
    ;;
  *) GPU_ARGS=(--gpus all) ;;
esac

TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS=(-it)

exec docker run \
  "${TTY_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  "${COMMON[@]}" \
  "$IMAGE" "$@"

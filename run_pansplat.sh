#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_PANSPLAT_IMAGE:-namma-pansplat:latest}"
CMD="${1:-help}"
GPU_DEVICE="${PS_GPU_DEVICE:-0}"
RAM_LIMIT="${PS_RAM_LIMIT:-28g}"
SHM_SIZE="${PS_SHM_SIZE:-8g}"

mkdir -p \
  "$ROOT/outputs/panograph" \
  "$ROOT/pansplat/checkpoints" \
  "$ROOT/pansplat/cache"

if [[ "$CMD" == "build" ]]; then
  exec docker build \
    --memory="${PS_BUILD_RAM_LIMIT:-16g}" \
    -t "$IMAGE" \
    -f "$ROOT/pansplat/Dockerfile" \
    "$ROOT"
fi

COMMON=(
  --rm
  --restart=no
  --memory="$RAM_LIMIT"
  --memory-swap="$RAM_LIMIT"
  --pids-limit=2048
  --ipc=private
  --shm-size="$SHM_SIZE"
  -e WANDB_MODE=disabled
  -e "PYTORCH_CUDA_ALLOC_CONF=${PS_PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True,max_split_size_mb:128}"
  -v "$ROOT/outputs/panograph:/workspace/outputs"
  -v "$ROOT/pansplat/checkpoints:/workspace/checkpoints"
  -v "$ROOT/pansplat/cache:/workspace/cache"
  -v "$ROOT/pansplat/scripts:/opt/namma-pansplat/scripts:ro"
  -v "$ROOT/pansplat/config:/opt/namma-pansplat/config:ro"
)

for name in \
  PS_PROFILE PS_CHECKPOINT PS_CHECKPOINT_URL \
  PS_GPU_MEMORY_FRACTION PS_GPU_MIN_FREE_MB \
  PS_FRAMES PS_FPS PS_RENDER_BATCH PS_WEBM_CRF PS_NORMALIZE_BASELINE
do
  [[ -n "${!name:-}" ]] && COMMON+=( -e "$name=${!name}" )
done

GPU_ARGS=()
case "$CMD" in
  edge|render|gpu-check)
    GPU_ARGS=(--gpus "device=${GPU_DEVICE}")
    ;;
esac

TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS=(-it)

exec docker run \
  "${TTY_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  "${COMMON[@]}" \
  "$IMAGE" "$@"

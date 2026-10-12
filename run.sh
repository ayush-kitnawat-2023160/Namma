#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_IMAGE:-namma-iitb:latest}"
CAMERA_PROFILE="${CAMERA_PROFILE:-advio15}"
CMD="${1:-help}"

mkdir -p "$ROOT/data" "$ROOT/outputs/mast3r" "$ROOT/outputs/gs_dataset" \
  "$ROOT/outputs/gs_model" "$ROOT/outputs/renders" "$ROOT/outputs/logs"

COMMON=(
  --rm
  --ipc=host
  --shm-size=32g
  --cap-add=SYS_PTRACE
  --security-opt seccomp=unconfined
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
  -e "LD_LIBRARY_PATH=/opt/conda/envs/namma/lib/python3.11/site-packages/torch/lib:/usr/local/cuda/lib64:/usr/local/nvidia/lib:/usr/local/nvidia/lib64"
  -v "$ROOT/data:/workspace/data:ro"
  -v "$ROOT/outputs:/workspace/outputs"
  -v "$ROOT/outputs/mast3r:/opt/MASt3R-SLAM/logs"
)

[[ -n "${SUBSAMPLE:-}" ]] && COMMON+=( -e "SUBSAMPLE=$SUBSAMPLE" )
[[ -n "${CAMERA_PROFILE:-}" ]] && COMMON+=( -e "CAMERA_PROFILE=$CAMERA_PROFILE" )
[[ -n "${FOV_X:-}" ]] && COMMON+=( -e "FOV_X=$FOV_X" )
[[ -n "${MAX_POINTS:-}" ]] && COMMON+=( -e "MAX_POINTS=$MAX_POINTS" )
[[ -n "${SOR_NEIGHBORS:-}" ]] && COMMON+=( -e "SOR_NEIGHBORS=$SOR_NEIGHBORS" )
[[ -n "${SOR_STD_RATIO:-}" ]] && COMMON+=( -e "SOR_STD_RATIO=$SOR_STD_RATIO" )
[[ -n "${GS_ITERS:-}" ]] && COMMON+=( -e "GS_ITERS=$GS_ITERS" )
[[ -n "${GS_RESOLUTION:-}" ]] && COMMON+=( -e "GS_RESOLUTION=$GS_RESOLUTION" )

case "$CMD" in
  smoke|help|-h|--help|shell|bash|inspect) GPU_ARGS=() ;;
  *) GPU_ARGS=(--gpus all) ;;
esac

TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS=(-it)

exec docker run "${TTY_ARGS[@]}" "${GPU_ARGS[@]}" "${COMMON[@]}" "$IMAGE" "$@"


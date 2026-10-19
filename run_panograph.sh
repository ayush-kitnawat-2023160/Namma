#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_PANOGRAPH_IMAGE:-namma-panograph:latest}"
CMD="${1:-help}"
GPU_DEVICE="${NP_GPU_DEVICE:-0}"
RAM_LIMIT="${NP_RAM_LIMIT:-32g}"
SHM_SIZE="${NP_SHM_SIZE:-16g}"

mkdir -p \
  "$ROOT/data/panograph" \
  "$ROOT/outputs/panograph" \
  "$ROOT/panograph/checkpoints" \
  "$ROOT/panograph/hf_cache" \
  "$ROOT/panograph/npm_cache"

if [[ "$CMD" == "build" ]]; then
  exec docker build \
    --memory="${NP_BUILD_RAM_LIMIT:-16g}" \
    -t "$IMAGE" \
    -f "$ROOT/panograph/Dockerfile" \
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
  -e "HOST_UID=$(id -u)"
  -e "HOST_GID=$(id -g)"
  -e HF_HOME=/workspace/hf_cache
  -e HUGGINGFACE_HUB_CACHE=/workspace/hf_cache/hub
  -e HF_XET_HIGH_PERFORMANCE=1
  -e TOKENIZERS_PARALLELISM=false
  -e "PYTORCH_CUDA_ALLOC_CONF=${NP_PYTORCH_CUDA_ALLOC_CONF:-expandable_segments:True,max_split_size_mb:128}"
  -v "$ROOT/data/panograph:/workspace/data"
  -v "$ROOT/outputs/panograph:/workspace/outputs"
  -v "$ROOT/panograph/checkpoints:/workspace/checkpoints"
  -v "$ROOT/panograph/hf_cache:/workspace/hf_cache"
  -v "$ROOT/panograph/npm_cache:/root/.npm"
  -v "$ROOT/panograph/scripts:/opt/namma-panograph/scripts:ro"
  -v "$ROOT/panograph/config:/opt/namma-panograph/config:ro"
)

for name in \
  NP_DATASET_REPO NP_DATASET_SCENE NP_PANOVGGT_CHECKPOINT \
  NP_GPU_MEMORY_FRACTION NP_GPU_MIN_FREE_MB NP_PAIR_STRIDE \
  NP_GRAPH_KNN NP_GRAPH_RADIUS_FACTOR NP_CAMERA_HEIGHT \
  NP_YAW_SIGN NP_YAW_OFFSET_DEG NP_VIEWER_MAX_WIDTH \
  NP_VIEWER_WEBP_QUALITY NP_VIEWER_PORT NP_OVERWRITE
do
  [[ -n "${!name:-}" ]] && COMMON+=( -e "$name=${!name}" )
done

[[ -n "${HF_TOKEN:-}" ]] && COMMON+=( -e "HF_TOKEN=${HF_TOKEN}" )

GPU_ARGS=()
PORT_ARGS=()
case "$CMD" in
  reconstruct|pipeline|gpu-check)
    GPU_ARGS=(--gpus "device=${GPU_DEVICE}")
    ;;
  serve)
    PORT_ARGS=(-p "${NP_VIEWER_PORT:-3000}:3000")
    ;;
esac

TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS=(-it)

exec docker run \
  "${TTY_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  "${PORT_ARGS[@]}" \
  "${COMMON[@]}" \
  "$IMAGE" "$@"

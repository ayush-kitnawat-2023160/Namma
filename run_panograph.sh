#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_PANOGRAPH_IMAGE:-namma-panograph:latest}"
CMD="${1:-help}"
GPU_DEVICE="${NP_GPU_DEVICE:-0}"
RAM_LIMIT="${NP_RAM_LIMIT:-32g}"
SHM_SIZE="${NP_SHM_SIZE:-16g}"
VIEWER_NAME="${NAMMA_PANOGRAPH_VIEWER_NAME:-namma-panograph-viewer}"

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

# Host-side viewer lifecycle helpers.  These are handled here (rather than by
# the container entrypoint) so the SPHR server can stay alive after this shell
# command returns.
if [[ "$CMD" == "stop" ]]; then
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$VIEWER_NAME"; then
    docker stop "$VIEWER_NAME" >/dev/null
    echo "Stopped PanoGraph viewer: $VIEWER_NAME"
  else
    echo "PanoGraph viewer is not running: $VIEWER_NAME"
  fi
  exit 0
fi

if [[ "$CMD" == "logs" ]]; then
  if ! docker ps -a --format '{{.Names}}' | grep -Fxq "$VIEWER_NAME"; then
    echo "PanoGraph viewer container not found: $VIEWER_NAME" >&2
    echo "Start it with: ./run_panograph.sh serve-bg hindoor_unibs" >&2
    exit 2
  fi
  exec docker logs -f "$VIEWER_NAME"
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
  serve|serve-bg)
    PORT_ARGS=(-p "${NP_VIEWER_PORT:-3000}:3000")
    ;;
esac

if [[ "$CMD" == "serve-bg" ]]; then
  SCENE="${2:-hindoor_unibs}"
  docker rm -f "$VIEWER_NAME" >/dev/null 2>&1 || true
  CID="$(docker run -d \
    --name "$VIEWER_NAME" \
    "${PORT_ARGS[@]}" \
    "${COMMON[@]}" \
    "$IMAGE" serve "$SCENE")"
  echo "Started PanoGraph viewer in background."
  echo "Container: $VIEWER_NAME ($CID)"
  echo "URL: http://127.0.0.1:${NP_VIEWER_PORT:-3000}/?config=/datasets/panograph/$SCENE/bootstrap.json"
  exit 0
fi

TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS=(-it)

exec docker run \
  "${TTY_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  "${PORT_ARGS[@]}" \
  "${COMMON[@]}" \
  "$IMAGE" "$@"

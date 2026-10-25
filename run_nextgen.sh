#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="${NAMMA_NEXTGEN_IMAGE:-namma-nextgen:latest}"
CMD="${1:-help}"
VIEWER_NAME="${NAMMA_NEXTGEN_VIEWER_NAME:-namma-nextgen-viewer}"
VIEWER_PORT="${NG_VIEWER_PORT:-8080}"

mkdir -p \
  "$ROOT/data" \
  "$ROOT/outputs/nextgen" \
  "$ROOT/outputs/logs" \
  "$ROOT/nextgen/checkpoints"

HOST_MEM_KB="$(awk '/MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
if [[ "$HOST_MEM_KB" =~ ^[0-9]+$ ]] && (( HOST_MEM_KB > 0 )); then
  SAFE_MEM_GB=$(( HOST_MEM_KB / 1024 / 1024 * 70 / 100 ))
  (( SAFE_MEM_GB > 32 )) && SAFE_MEM_GB=32
  (( SAFE_MEM_GB < 8 )) && SAFE_MEM_GB=8
else
  SAFE_MEM_GB=24
fi
MEM_LIMIT="${NG_DOCKER_MEMORY:-${SAFE_MEM_GB}g}"
SWAP_LIMIT="${NG_DOCKER_MEMORY_SWAP:-$MEM_LIMIT}"

if [[ "$CMD" == "viewer-stop" ]]; then
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$VIEWER_NAME"; then
    docker rm -f "$VIEWER_NAME" >/dev/null
    echo "Stopped NextGen viewer: $VIEWER_NAME"
  else
    echo "NextGen viewer is not running: $VIEWER_NAME"
  fi
  exit 0
fi

if [[ "$CMD" == "viewer-logs" ]]; then
  if ! docker ps -a --format '{{.Names}}' | grep -Fxq "$VIEWER_NAME"; then
    echo "NextGen viewer container not found: $VIEWER_NAME" >&2
    echo "Start it with: ./run_nextgen.sh viewer-bg <scene>" >&2
    exit 2
  fi
  exec docker logs -f "$VIEWER_NAME"
fi

VIEWER_ARGS=()
VIEWER_URL=""
VIEWER_SCENE=""
VIEWER_ITER=""
if [[ "$CMD" == "viewer" || "$CMD" == "viewer-bg" ]]; then
  [[ $# -ge 2 ]] || { echo "Usage: ./run_nextgen.sh $CMD <scene>" >&2; exit 2; }
  VIEWER_SCENE="$2"
  SCENE_ROOT="$ROOT/outputs/nextgen/$VIEWER_SCENE"
  [[ -d "$SCENE_ROOT" ]] || { echo "ERROR: scene output not found: $SCENE_ROOT" >&2; exit 2; }
  [[ -f "$ROOT/nextgen/viewer/dist/index.html" ]] || {
    echo "ERROR: built NextGen viewer missing: nextgen/viewer/dist/index.html" >&2
    echo "Build it on a development machine with: cd nextgen/viewer && npm install && npm run build" >&2
    exit 2
  }

  if [[ -n "${NG_VIEWER_ITER:-}" ]]; then
    VIEWER_ITER="$NG_VIEWER_ITER"
  else
    LATEST_ITER_DIR="$(find "$SCENE_ROOT/06_gs_model/point_cloud" -maxdepth 1 -type d -name 'iteration_*' -printf '%f\n' 2>/dev/null | sort -V | tail -1 || true)"
    VIEWER_ITER="${LATEST_ITER_DIR#iteration_}"
  fi
  [[ -n "$VIEWER_ITER" ]] || VIEWER_ITER="${NG_GS_ITERS:-10000}"

  VIEWER_ARGS=(
    -p "127.0.0.1:${VIEWER_PORT}:8080"
    -v "$ROOT/nextgen/viewer/dist:/srv/viewer:ro"
    -v "$SCENE_ROOT:/srv/scene:ro"
  )
  VIEWER_URL="http://127.0.0.1:${VIEWER_PORT}/viewer/?scene=${VIEWER_SCENE}&iter=${VIEWER_ITER}"
fi

case "$CMD" in
  pipeline|vggt|sfm|fuse|gs-dataset|train|render|postprocess)
    MIN_DISK_GB="${NG_MIN_FREE_DISK_GB:-25}"
    FREE_DISK_KB="$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
    FREE_DISK_GB=$(( FREE_DISK_KB / 1024 / 1024 ))
    if (( FREE_DISK_GB < MIN_DISK_GB )); then
      echo "ERROR: only ${FREE_DISK_GB} GiB free under $ROOT; refusing heavy stage (need >= ${MIN_DISK_GB} GiB)." >&2
      exit 2
    fi
    ;;
esac

COMMON=(
  --rm
  --init
  --ipc=private
  --memory="$MEM_LIMIT"
  --memory-swap="$SWAP_LIMIT"
  --shm-size="${NG_DOCKER_SHM:-6g}"
  --pids-limit="${NG_DOCKER_PIDS:-4096}"
  --ulimit nofile=65536:65536
  --security-opt no-new-privileges
  --log-driver=local
  --log-opt max-size=20m
  --log-opt max-file=3
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
  NG_TEMPORAL_WINDOW NG_VISUAL_NEIGHBORS NG_GS_ITERS NG_GS_RESOLUTION NG_GS_DATA_DEVICE \
  NG_DEPTH_WEIGHT_INIT NG_DEPTH_WEIGHT_FINAL NG_GPU_MEMORY_FRACTION NG_MIN_FREE_VRAM_GB \
  NG_MAX_GPU_TEMP_C NG_FUSION_NEIGHBORS NG_FUSION_REL_DEPTH NG_FUSION_MIN_SUPPORT \
  NG_FUSION_PIXEL_STRIDE NG_FUSION_CONFIDENCE_PERCENTILE NG_FUSION_VOXEL_SIZE \
  NG_FUSION_MAX_POINTS
do
  [[ -n "${!name:-}" ]] && COMMON+=( -e "$name=${!name}" )
done

case "$CMD" in
  help|-h|--help|inspect|fix-perms|shell|bash|fuse|postprocess|viewer|viewer-bg) GPU_ARGS=() ;;
  check) GPU_ARGS=(--gpus all) ;;
  prepare)
    if [[ -n "${NG_CAMERA_PROFILE:-}" ]]; then GPU_ARGS=(); else GPU_ARGS=(--gpus all); fi
    ;;
  *) GPU_ARGS=(--gpus all) ;;
esac

echo "[nextgen] container safety: memory=$MEM_LIMIT, swap=$SWAP_LIMIT, shm=${NG_DOCKER_SHM:-6g}, pids=${NG_DOCKER_PIDS:-4096}" >&2

if [[ "$CMD" == "viewer-bg" ]]; then
  docker rm -f "$VIEWER_NAME" >/dev/null 2>&1 || true
  CID="$(docker run -d \
    --name "$VIEWER_NAME" \
    "${VIEWER_ARGS[@]}" \
    "${COMMON[@]}" \
    "$IMAGE" viewer "$VIEWER_SCENE")"
  echo "Started NextGen Gaussian viewer in background."
  echo "Container: $VIEWER_NAME ($CID)"
  echo "Server-local URL: $VIEWER_URL"
  echo "Laptop access: forward port ${VIEWER_PORT} over SSH/VS Code, then open the same localhost URL."
  exit 0
fi

TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS=(-it)

exec docker run \
  "${TTY_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  "${VIEWER_ARGS[@]}" \
  "${COMMON[@]}" \
  "$IMAGE" "$@"

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

case "$CMD" in
  pipeline|vggt|sfm|fuse|gs-dataset|train|render)
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
  NG_TEMPORAL_WINDOW NG_VISUAL_NEIGHBORS NG_GS_ITERS NG_GS_RESOLUTION \
  NG_DEPTH_WEIGHT_INIT NG_DEPTH_WEIGHT_FINAL NG_GPU_MEMORY_FRACTION NG_MIN_FREE_VRAM_GB \
  NG_MAX_GPU_TEMP_C NG_FUSION_NEIGHBORS NG_FUSION_REL_DEPTH NG_FUSION_MIN_SUPPORT \
  NG_FUSION_PIXEL_STRIDE NG_FUSION_CONFIDENCE_PERCENTILE NG_FUSION_VOXEL_SIZE \
  NG_FUSION_MAX_POINTS
do
  [[ -n "${!name:-}" ]] && COMMON+=( -e "$name=${!name}" )
done

case "$CMD" in
  help|-h|--help|inspect|fix-perms|shell|bash|fuse) GPU_ARGS=() ;;
  check) GPU_ARGS=(--gpus all) ;;
  prepare)
    if [[ -n "${NG_CAMERA_PROFILE:-}" ]]; then GPU_ARGS=(); else GPU_ARGS=(--gpus all); fi
    ;;
  *) GPU_ARGS=(--gpus all) ;;
esac

echo "[nextgen] container safety: memory=$MEM_LIMIT, swap=$SWAP_LIMIT, shm=${NG_DOCKER_SHM:-6g}, pids=${NG_DOCKER_PIDS:-4096}" >&2

TTY_ARGS=()
[[ -t 0 && -t 1 ]] && TTY_ARGS=(-it)

exec docker run \
  "${TTY_ARGS[@]}" \
  "${GPU_ARGS[@]}" \
  "${COMMON[@]}" \
  "$IMAGE" "$@"

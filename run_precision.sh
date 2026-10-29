#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD="${1:-help}"
fail() { echo "ERROR: $*" >&2; exit 2; }
scene_ok() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; }

if [[ "$CMD" == help || "$CMD" == -h || "$CMD" == --help ]]; then
  cat <<'EOF'
Namma Precision — run reconstruction on the college server only.

  ./run_precision.sh help
  NAMMA_SERVER=1 ./run_precision.sh check
  NAMMA_SERVER=1 ./run_precision.sh pipeline <video> <new-scene>
  NAMMA_SERVER=1 ./run_precision.sh refine <existing-nextgen-scene> <new-scene>
  NAMMA_SERVER=1 ./run_precision.sh audit <precision-scene>
  NAMMA_SERVER=1 PR_ALLOW_VIEW_PRUNING=1 PR_TRAIN=0 ./run_precision.sh recover <precision-scene> <new-scene>
  NAMMA_SERVER=1 ./run_precision.sh train <scene>
  NAMMA_SERVER=1 ./run_precision.sh mesh <scene>
  NAMMA_SERVER=1 ./run_precision.sh report <scene>

Video paths are relative to data/ (or /workspace/data/ in the container).
Outputs: outputs/precision/<scene>. Existing scene destinations are preserved.
GPU: NG_GPU_DEVICE=0 (index or GPU UUID). Image: NAMMA_PRECISION_IMAGE.
Heavy commands require >=100 GiB physical / >=92 GiB available host RAM,
and >=22 GiB GPU / >=18 GiB free. Docker memory and swap limits must work.
Limits: 80 GiB RAM, no added swap, 8 CPUs, 4 threads, 4 GiB shared memory.
The watchdog stops jobs at >=83 C GPU, <1 GiB VRAM, <12 GiB host RAM,
or <15 GiB free disk. These checks reduce risk; hardware stability still matters.
EOF
  exit 0
fi

[[ "${NAMMA_SERVER:-0}" == 1 ]] || fail "Server-only command. Set NAMMA_SERVER=1 on the college server; do not run reconstruction on your PC."
HEAVY=0
SOURCE_ARGS=()
case "$CMD" in
  pipeline|refine|recover)
    [[ $# == 3 ]] || fail "Usage: $0 $CMD <video-or-source> <new-scene>"
    SCENE="$3"; HEAVY=1 ;;
  train|mesh|report|audit)
    [[ $# == 2 ]] || fail "Usage: $0 $CMD <scene>"
    SCENE="$2"; [[ "$CMD" == report || "$CMD" == audit ]] || HEAVY=1 ;;
  check) [[ $# == 1 ]] || fail "Usage: $0 check"; SCENE="" ;;
  *) fail "Unknown command '$CMD'; use $0 help." ;;
esac
[[ -z "$SCENE" ]] || scene_ok "$SCENE" || fail "Scene names must match [A-Za-z0-9][A-Za-z0-9_.-]*."
command -v docker >/dev/null || fail "Docker is not installed."

mkdir -p "$ROOT/.runtime" "$ROOT/outputs/precision" "$ROOT/outputs/logs" "$ROOT/data" "$ROOT/nextgen/checkpoints"
GPU_ARGS=()
if (( HEAVY )); then
  command -v flock >/dev/null || fail "flock is required to prevent overlapping Precision jobs."
  exec 9>"$ROOT/.runtime/precision.lock"
  flock -n 9 || fail "A Precision job is already running in this checkout."
  read -r HOST_MEM_KB HOST_AVAILABLE_KB < <(awk '/^MemTotal:/ {total=$2} /^MemAvailable:/ {available=$2} END {print total, available}' /proc/meminfo)
  [[ "$HOST_MEM_KB" =~ ^[0-9]+$ ]] && (( HOST_MEM_KB >= 100 * 1024 * 1024 )) || fail "Heavy commands require at least 100 GiB physical host RAM."
  [[ "$HOST_AVAILABLE_KB" =~ ^[0-9]+$ ]] && (( HOST_AVAILABLE_KB >= 92 * 1024 * 1024 )) || fail "Heavy commands require at least 92 GiB currently available host RAM (80 GiB container plus 12 GiB reserve)."
  DOCKER_LIMITS="$(docker info --format '{{.MemoryLimit}} {{.SwapLimit}}')" || fail "Cannot verify Docker daemon memory controls."
  [[ "$DOCKER_LIMITS" == "true true" ]] || fail "Docker must support both memory and swap cgroup limits; reported: $DOCKER_LIMITS"
  GPU="${NG_GPU_DEVICE:-0}"
  [[ "$GPU" =~ ^([0-9]+|GPU-[A-Za-z0-9-]+)$ ]] || fail "NG_GPU_DEVICE must select one GPU index or UUID."
  command -v nvidia-smi >/dev/null || fail "nvidia-smi is required for the server GPU preflight."
  GPU_INFO="$(nvidia-smi --id="$GPU" --query-gpu=memory.total,memory.free,temperature.gpu --format=csv,noheader,nounits)" || fail "Cannot inspect selected GPU $GPU."
  [[ "$GPU_INFO" != *$'\n'* ]] || fail "Exactly one GPU must be selected."
  IFS=',' read -r TOTAL FREE TEMP <<< "$GPU_INFO"
  for item in "$TOTAL" "$FREE" "$TEMP"; do [[ "$item" =~ ^[[:space:]]*[0-9]+[[:space:]]*$ ]] || fail "Invalid GPU telemetry: $GPU_INFO"; done
  TOTAL="${TOTAL//[[:space:]]/}"; FREE="${FREE//[[:space:]]/}"; TEMP="${TEMP//[[:space:]]/}"
  (( TOTAL >= 22 * 1024 && FREE >= 18 * 1024 )) || fail "Selected GPU requires >=22 GiB total and >=18 GiB free VRAM (reported ${TOTAL}/${FREE} MiB)."
  (( TEMP < 83 )) || fail "Selected GPU is already ${TEMP} C; wait for it to cool."
  GPU_ARGS=(--gpus "device=$GPU")
fi

if [[ "$CMD" == pipeline || "$CMD" == refine || "$CMD" == recover ]]; then
  [[ ! -e "$ROOT/outputs/precision/$SCENE" && ! -L "$ROOT/outputs/precision/$SCENE" ]] || fail "Destination exists; choose a new scene: outputs/precision/$SCENE"
fi
if [[ "$CMD" == refine || "$CMD" == recover ]]; then
  scene_ok "$2" || fail "Invalid source scene name."
  SOURCE_TREE=nextgen
  [[ "$CMD" != recover ]] || SOURCE_TREE=precision
  SOURCE_BASE="$(realpath -e "$ROOT/outputs/$SOURCE_TREE")" || fail "Source directory is missing: outputs/$SOURCE_TREE"
  SOURCE="$(realpath -e "$ROOT/outputs/$SOURCE_TREE/$2")" || fail "Source scene is missing."
  [[ -d "$SOURCE" && "$SOURCE" == "$SOURCE_BASE/"* ]] || fail "Source must be a scene inside outputs/$SOURCE_TREE."
  [[ "$SOURCE" != "$(realpath -m "$ROOT/outputs/precision/$SCENE")" ]] || fail "Source and destination must differ."
  SOURCE_ARGS=(-v "$SOURCE:/workspace/source:ro")
fi

COMMON=(
  --rm --init --ipc=private --memory=80g --memory-swap=80g
  --cpus=8 --shm-size=4g --pids-limit=1024
  --security-opt no-new-privileges --ulimit nofile=65536:65536
  --log-driver=local --log-opt max-size=20m --log-opt max-file=3
  -e OMP_NUM_THREADS=4 -e MKL_NUM_THREADS=4 -e OPENBLAS_NUM_THREADS=4
  -e NUMEXPR_NUM_THREADS=4 -e MALLOC_ARENA_MAX=4
  -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True,max_split_size_mb:128
  -e "HOST_UID=$(id -u)" -e "HOST_GID=$(id -g)"
  -e "NG_GS_DATA_DEVICE=${NG_GS_DATA_DEVICE:-cpu}"
  -e "NG_GS_RESOLUTION=${NG_GS_RESOLUTION:-1}"
  -e "NG_GPU_MEMORY_FRACTION=${NG_GPU_MEMORY_FRACTION:-0.85}"
  -v "$ROOT/data:/workspace/data:ro"
  -v "$ROOT/outputs/precision:/workspace/outputs/nextgen"
  -v "$ROOT/outputs/logs:/workspace/outputs/logs"
  -v "$ROOT/config:/opt/namma/config:ro"
  -v "$ROOT/nextgen/scripts:/opt/namma-nextgen/scripts:ro"
  -v "$ROOT/nextgen/config:/opt/namma-nextgen/config:ro"
  -v "$ROOT/nextgen/checkpoints:/workspace/checkpoints:ro"
  -v "$ROOT/precision/scripts:/opt/namma-precision/scripts:ro"
)
while IFS= read -r name; do COMMON+=(-e "$name=${!name}"); done < <(compgen -e PR_ || true)
for name in NG_CAMERA_PROFILE NG_CALIB_VIEWS NG_CALIB_MODEL NG_TARGET_VIEWS NG_MAX_CANDIDATES \
  NG_MIN_BLUR NG_DUPLICATE_DISTANCE NG_VGGT_CHECKPOINT NG_VGGT_RESOLUTION NG_VGGT_MODE \
  NG_TEMPORAL_WINDOW NG_VISUAL_NEIGHBORS NG_GS_ITERS NG_DEPTH_WEIGHT_INIT NG_DEPTH_WEIGHT_FINAL \
  NG_MIN_FREE_VRAM_GB NG_FUSION_NEIGHBORS NG_FUSION_REL_DEPTH NG_FUSION_MIN_SUPPORT \
  NG_FUSION_PIXEL_STRIDE NG_FUSION_CONFIDENCE_PERCENTILE NG_FUSION_VOXEL_SIZE NG_FUSION_MAX_POINTS \
  NG_TSDF_NEIGHBORS NG_TSDF_REL_DEPTH NG_TSDF_MIN_SUPPORT NG_TSDF_CONFIDENCE_PERCENTILE \
  NG_TSDF_VOXEL_SCALE NG_TSDF_TRUNC_VOXELS NG_TSDF_DEPTH_PERCENTILE \
  NG_TSDF_MIN_COMPONENT_TRIANGLES NG_TSDF_MIN_COMPONENT_FRACTION \
  NG_PRESENTATION_CONFIDENCE_PERCENTILE NG_PRESENTATION_DEPTH_TOLERANCE \
  NG_PRESENTATION_TARGET_TRIANGLES NG_PRESENTATION_CHUNK_SIZE; do
  [[ -z "${!name:-}" ]] || COMMON+=(-e "$name=${!name}")
done

IMAGE="${NAMMA_PRECISION_IMAGE:-namma-precision:latest}"
echo "[precision] Docker limits: RAM=80 GiB, swap=80 GiB total, CPUs=8; scene=${SCENE:-none}" >&2
if (( HEAVY )); then
  exec docker run "${COMMON[@]}" "${GPU_ARGS[@]}" "${SOURCE_ARGS[@]}" --entrypoint python "$IMAGE" \
    /opt/namma-precision/scripts/runtime_watchdog.py --stage "$CMD" -- \
    /opt/namma-precision/scripts/entrypoint.sh "$@"
fi
exec docker run "${COMMON[@]}" "${SOURCE_ARGS[@]}" "$IMAGE" "$@"

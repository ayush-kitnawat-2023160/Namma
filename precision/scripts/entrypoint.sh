#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPTS=/opt/namma-precision/scripts
LEGACY=/opt/namma-nextgen/scripts
CMD="${1:-help}"
scene_ok() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; }
fail() { echo "ERROR: $*" >&2; exit 2; }
case "$CMD" in
  help|-h|--help) echo 'Use ./run_precision.sh help on the host. See precision/README.md.'; exit 0 ;;
  check) exec python "$SCRIPTS/check.py" ;;
  pipeline|refine|recover) [[ $# == 3 ]] || fail 'Expected input/source and new scene.'; SCENE="$3" ;;
  train|mesh|report|audit) [[ $# == 2 ]] || fail 'Expected scene.'; SCENE="$2" ;;
  *) fail "Unknown command: $CMD" ;;
esac
scene_ok "$SCENE" || fail 'Invalid scene name.'
ROOT="/workspace/outputs/nextgen/$SCENE"
export NG_TARGET_VIEWS="${NG_TARGET_VIEWS:-160}"
export NG_GPU_MEMORY_FRACTION="${NG_GPU_MEMORY_FRACTION:-0.85}"
export NG_GS_DATA_DEVICE=cpu
export NG_GS_RESOLUTION="${NG_GS_RESOLUTION:-1}"
export NG_GS_ITERS="${NG_GS_ITERS:-30000}"
export NG_VGGT_RESOLUTION=512
export NG_VGGT_MODE=max_size
export NG_TEMPORAL_WINDOW="${NG_TEMPORAL_WINDOW:-8}"
export NG_VISUAL_NEIGHBORS="${NG_VISUAL_NEIGHBORS:-12}"
[[ "$NG_TARGET_VIEWS" =~ ^[0-9]+$ ]] && (( NG_TARGET_VIEWS >= 16 && NG_TARGET_VIEWS <= 160 )) || fail 'NG_TARGET_VIEWS must be 16..160.'
[[ "$NG_GS_RESOLUTION" =~ ^(1|2|4|8)$ ]] || fail 'NG_GS_RESOLUTION must be 1,2,4,8.'
[[ "$NG_GS_ITERS" =~ ^[0-9]+$ ]] && (( NG_GS_ITERS >= 7000 && NG_GS_ITERS <= 30000 )) || fail 'NG_GS_ITERS must be 7000..30000.'
export NG_CALIB_VIEWS="${NG_CALIB_VIEWS:-20}"
export NG_MAX_CANDIDATES="${NG_MAX_CANDIDATES:-1200}"
[[ "$NG_CALIB_VIEWS" =~ ^[0-9]+$ ]] && (( NG_CALIB_VIEWS >= 3 && NG_CALIB_VIEWS <= 24 )) || fail 'NG_CALIB_VIEWS must be 3..24.'
[[ "$NG_MAX_CANDIDATES" =~ ^[0-9]+$ ]] && (( NG_MAX_CANDIDATES >= 160 && NG_MAX_CANDIDATES <= 2400 )) || fail 'NG_MAX_CANDIDATES must be 160..2400.'
[[ "${PR_TRAIN:-1}" =~ ^[01]$ ]] || fail 'PR_TRAIN must be 0 or 1.'
[[ "${PR_ALLOW_VIEW_PRUNING:-0}" =~ ^[01]$ ]] || fail 'PR_ALLOW_VIEW_PRUNING must be 0 or 1.'
python - "$NG_GPU_MEMORY_FRACTION" <<'PY'
import sys
if not 0.5 <= float(sys.argv[1]) <= 0.88:
    raise SystemExit('NG_GPU_MEMORY_FRACTION must be 0.5..0.88.')
PY
cleanup() {
  local status=$?
  if [[ -d "$ROOT" && -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
    chown -R "$HOST_UID:$HOST_GID" "$ROOT" || true
  fi
  exit "$status"
}
trap cleanup EXIT

train_scene() {
  [[ -f "$ROOT/05_gs_dataset/summary.json" ]] || fail 'Build the MVS dataset first.'
  [[ ! -e "$ROOT/06_gs_model" ]] || fail 'Training output exists; preserve it and choose a new scene.'
  python "$SCRIPTS/train.py" --scene-root "$ROOT"
}
mesh_scene() {
  python "$SCRIPTS/mesh.py" --mvs "$ROOT/04_mvs" --output "$ROOT/08_mesh" \
    --voxel-divisor "${PR_TSDF_VOXEL_DIVISOR:-512}"
}
mvs_scene() {
  local mode=("$@")
  [[ "${PR_ALLOW_VIEW_PRUNING:-0}" != 1 ]] || mode+=(--allow-view-pruning)
  python "$SCRIPTS/mvs.py" --source "$ROOT" --output "$MVS_OUT" \
    --max-image-size "${PR_MVS_MAX_IMAGE_SIZE:-1600}" --neighbors "${PR_MVS_NEIGHBORS:-12}" \
    --cache-gb 4 --threads 4 --max-images 160 "${mode[@]}"
}
if [[ "$CMD" == train ]]; then train_scene; exit; fi
if [[ "$CMD" == mesh ]]; then mesh_scene; exit; fi
if [[ "$CMD" == report ]]; then python "$SCRIPTS/report.py" --scene-root "$ROOT"; exit; fi
if [[ "$CMD" == audit ]]; then
  [[ -d "$ROOT/03_sfm/model" ]] || fail 'Audit requires completed SfM.'
  MVS_OUT="$ROOT/09_audit/$(date -u +%Y%m%dT%H%M%SZ)_$$"
  mvs_scene --audit-only
  exit
fi

[[ ! -e "$ROOT" && ! -L "$ROOT" ]] || fail "Refusing to overwrite scene: $SCENE"
mkdir -p "$ROOT"
python - "$ROOT" "$CMD" "$2" <<'PY'
import datetime,json,os,pathlib,sys
root=pathlib.Path(sys.argv[1])
(root/'run_manifest.json').write_text(json.dumps({
    'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'command':sys.argv[2], 'input':sys.argv[3],
    'pipeline':'precision-v1', 'colmap':'3.11.1',
    'settings':{k:v for k,v in os.environ.items() if k.startswith(('NG_','PR_'))},
    'unseen_surface_completion':'none'},indent=2)+'\n')
PY
if [[ "$CMD" == pipeline ]]; then
  # Calibration and local-feature inference receive the same allocator guard.
  python "$SCRIPTS/prepare.py" "$2" "$SCENE"
  "$LEGACY/vggt.sh" "$SCENE"
elif [[ "$CMD" == refine ]]; then
  [[ -d /workspace/source/01_frames/images && -s /workspace/source/02_vggt/cameras.npz ]] || fail 'Source needs prepared frames and VGGT cameras.'
  python - <<'PY'
from pathlib import Path
images = list(Path('/workspace/source/01_frames/images').iterdir())
count = sum(p.is_file() and p.suffix.lower() in {'.png','.jpg','.jpeg'} for p in images)
if not 16 <= count <= 160:
    raise SystemExit(f'Refinement requires 16..160 selected images; source has {count}.')
PY
  # Reflinks are cheap on supporting filesystems; ordinary copying otherwise.
  # Copy only inference inputs, never modify/retrain the old output tree.
  for stage in 00_calibration 01_frames 02_vggt; do
    [[ ! -d "/workspace/source/$stage" ]] || cp -a --reflink=auto "/workspace/source/$stage" "$ROOT/"
  done
else
  [[ -d /workspace/source/01_frames/images && -d /workspace/source/03_sfm/model ]] || fail 'Recovery requires saved frames and completed SfM.'
  for stage in 00_calibration 01_frames 03_sfm; do
    [[ ! -d "/workspace/source/$stage" ]] || cp -a --reflink=auto "/workspace/source/$stage" "$ROOT/"
  done
  echo 'Reusing saved final cameras and tracks; VGGT and SfM are not rerun.'
fi
if [[ "$CMD" != recover ]]; then
  python "$LEGACY/gpu_guard.py" --stage precision-sfm
  python "$LEGACY/torch_limited_exec.py" --fraction "$NG_GPU_MEMORY_FRACTION" \
    "$SCRIPTS/sfm_precision.py" --scene-root "$ROOT" --output "$ROOT/03_sfm" \
    --temporal-window "$NG_TEMPORAL_WINDOW" --visual-neighbors "$NG_VISUAL_NEIGHBORS"
fi
MVS_OUT="$ROOT/04_mvs"
mvs_scene
python "$SCRIPTS/build_dataset.py" --mvs "$ROOT/04_mvs" --output "$ROOT/05_gs_dataset" \
  --max-points "${PR_INITIAL_POINTS:-400000}"
mesh_scene
if [[ "${PR_TRAIN:-1}" == 1 ]]; then train_scene; fi
python "$SCRIPTS/report.py" --scene-root "$ROOT"
echo "Precision reconstruction complete: $ROOT"

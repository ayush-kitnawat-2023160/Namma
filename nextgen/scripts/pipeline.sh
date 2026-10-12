#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: pipeline <video> [scene]" >&2; exit 2; }
INPUT="$1"
if [[ -f "$INPUT" ]]; then
  VIDEO="$(realpath "$INPUT")"
elif [[ -f "/workspace/data/$INPUT" ]]; then
  VIDEO="$(realpath "/workspace/data/$INPUT")"
else
  echo "ERROR: video not found: $INPUT" >&2
  exit 2
fi

BASE="$(basename "$VIDEO")"; BASE="${BASE%.*}"
SCENE="${2:-$BASE}"
CHECKPOINT="${NG_VGGT_CHECKPOINT:-/workspace/checkpoints/VGGT-Omega-1B-512.pt}"
[[ -s "$CHECKPOINT" ]] || {
  echo "ERROR: VGGT-Omega checkpoint missing: $CHECKPOINT" >&2
  exit 2
}

START="$(date +%s)"
CURRENT="startup"
STAGE_START="$START"

format_time() {
  local t="${1:-0}"
  printf '%02dh:%02dm:%02ds' $((t/3600)) $(((t%3600)/60)) $((t%60))
}

stage() {
  CURRENT="$1"
  STAGE_START="$(date +%s)"
  echo
  echo "============================================================"
  echo " NEXTGEN STAGE: $CURRENT"
  echo "============================================================"
}

done_stage() {
  local now="$(date +%s)"
  echo "Finished $CURRENT in $(format_time $((now-STAGE_START)))"
}

fail() {
  local code=$?
  local now="$(date +%s)"
  echo "NextGen pipeline failed in '$CURRENT' after $(format_time $((now-STAGE_START)))" >&2
  echo "Total elapsed: $(format_time $((now-START)))" >&2
  exit "$code"
}
trap fail ERR

echo "Scene: $SCENE"
echo "Video: $VIDEO"
echo "Views: ${NG_TARGET_VIEWS:-120}"
echo "Camera profile: ${NG_CAMERA_PROFILE:-<unknown>}"

stage "calibration/undistortion + frame selection"
/opt/namma-nextgen/scripts/prepare.sh "$VIDEO" "$SCENE"
done_stage

stage "VGGT-Omega camera/depth/geometry"
/opt/namma-nextgen/scripts/vggt.sh "$SCENE"
done_stage

stage "SuperPoint + LightGlue + COLMAP bundle adjustment"
/opt/namma-nextgen/scripts/sfm.sh "$SCENE"
done_stage

stage "VGGT/COLMAP fusion + depth priors"
/opt/namma-nextgen/scripts/build_gs_dataset.sh "$SCENE"
done_stage

stage "depth-regularized antialiased 3DGS"
/opt/namma-nextgen/scripts/train_gs.sh "$SCENE"
done_stage

stage "render"
/opt/namma-nextgen/scripts/render_gs.sh "$SCENE"
done_stage

END="$(date +%s)"
echo
echo "============================================================"
echo " NEXTGEN COMPLETE"
echo "============================================================"
echo "Final PLY: /workspace/outputs/nextgen/$SCENE/05_gs_model/point_cloud/iteration_${NG_GS_ITERS:-15000}/point_cloud.ply"
echo "Total time: $(format_time $((END-START)))"

if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
  chown -R "${HOST_UID}:${HOST_GID}" "/workspace/outputs/nextgen/$SCENE" || true
fi

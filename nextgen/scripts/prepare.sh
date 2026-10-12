#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: prepare <video> [scene]" >&2; exit 2; }

INPUT="$1"
if [[ -f "$INPUT" ]]; then
  VIDEO="$(realpath "$INPUT")"
elif [[ -f "/workspace/data/$INPUT" ]]; then
  VIDEO="$(realpath "/workspace/data/$INPUT")"
else
  echo "ERROR: video not found: $INPUT" >&2
  exit 2
fi

BASE="$(basename "$VIDEO")"
BASE="${BASE%.*}"
SCENE="${2:-$BASE}"
OUT="/workspace/outputs/nextgen/$SCENE/01_frames"
CALIB_OUT="/workspace/outputs/nextgen/$SCENE/00_calibration"

ARGS=(
  --video "$VIDEO"
  --output "$OUT"
  --target-views "${NG_TARGET_VIEWS:-120}"
  --max-candidates "${NG_MAX_CANDIDATES:-1200}"
  --min-blur "${NG_MIN_BLUR:-20}"
  --duplicate-distance "${NG_DUPLICATE_DISTANCE:-0.025}"
  --overwrite
)

if [[ -n "${NG_CAMERA_PROFILE:-}" ]]; then
  PROFILE="/opt/namma/config/cameras/${NG_CAMERA_PROFILE}.yaml"
  [[ -f "$PROFILE" ]] || { echo "ERROR: missing camera profile: $PROFILE" >&2; exit 2; }
else
  echo "No NG_CAMERA_PROFILE supplied; estimating shared intrinsics/distortion with GeoCalib."
  mkdir -p "$CALIB_OUT"
  python /opt/namma-nextgen/scripts/calibrate_video.py \
    --video "$VIDEO" \
    --output "$CALIB_OUT/camera.yaml" \
    --report "$CALIB_OUT/report.json" \
    --views "${NG_CALIB_VIEWS:-20}" \
    --camera-model "${NG_CALIB_MODEL:-radial}"
  PROFILE="$CALIB_OUT/camera.yaml"
fi

ARGS+=(--camera-profile "$PROFILE")

exec python /opt/namma-nextgen/scripts/prepare_scene.py "${ARGS[@]}"

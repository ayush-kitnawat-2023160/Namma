#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: calibrate <video> [scene]" >&2; exit 2; }
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
OUT="/workspace/outputs/nextgen/$SCENE/00_calibration"
mkdir -p "$OUT"

exec python /opt/namma-nextgen/scripts/calibrate_video.py \
  --video "$VIDEO" \
  --output "$OUT/camera.yaml" \
  --report "$OUT/report.json" \
  --views "${NG_CALIB_VIEWS:-20}" \
  --camera-model "${NG_CALIB_MODEL:-radial}"

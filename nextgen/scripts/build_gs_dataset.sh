#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: gs-dataset <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"

exec python /opt/namma-nextgen/scripts/build_gs_dataset.py \
  --frames "$ROOT/01_frames" \
  --vggt "$ROOT/02_vggt" \
  --sfm "$ROOT/03_sfm/model" \
  --geometry "$ROOT/04_geometry" \
  --output "$ROOT/05_gs_dataset" \
  --overwrite

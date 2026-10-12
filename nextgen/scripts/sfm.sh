#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: sfm <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
IMAGES="$ROOT/01_frames/images"
CAMERA="$ROOT/01_frames/camera.yaml"
VGGT="$ROOT/02_vggt/cameras.npz"
OUT="$ROOT/03_sfm"

[[ -d "$IMAGES" ]] || { echo "ERROR: prepared images missing: $IMAGES" >&2; exit 2; }
[[ -s "$VGGT" ]] || { echo "ERROR: VGGT camera initialization missing: $VGGT" >&2; exit 2; }
rm -rf "$OUT"

exec python /opt/namma-nextgen/scripts/sfm_lightglue.py \
  --images "$IMAGES" \
  --output "$OUT" \
  --camera "$CAMERA" \
  --vggt "$VGGT" \
  --temporal-window "${NG_TEMPORAL_WINDOW:-5}" \
  --visual-neighbors "${NG_VISUAL_NEIGHBORS:-5}"

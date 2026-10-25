#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: fuse <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
VGGT="$ROOT/02_vggt"
OUT="$ROOT/04_geometry"
TMP="${OUT}.tmp.$$"

[[ -s "$VGGT/cameras.npz" ]] || { echo "ERROR: VGGT cameras missing: $VGGT/cameras.npz" >&2; exit 2; }
[[ -d "$VGGT/depths" ]] || { echo "ERROR: VGGT depths missing: $VGGT/depths" >&2; exit 2; }

rm -rf "$TMP"
trap 'rm -rf "$TMP"' EXIT

python /opt/namma-nextgen/scripts/fuse_geometry.py \
  --vggt "$VGGT" \
  --frames "$ROOT/01_frames/images" \
  --output "$TMP" \
  --neighbors "${NG_FUSION_NEIGHBORS:-6}" \
  --relative-depth-threshold "${NG_FUSION_REL_DEPTH:-0.04}" \
  --min-support "${NG_FUSION_MIN_SUPPORT:-1}" \
  --pixel-stride "${NG_FUSION_PIXEL_STRIDE:-3}" \
  --confidence-percentile "${NG_FUSION_CONFIDENCE_PERCENTILE:-30}" \
  --voxel-size "${NG_FUSION_VOXEL_SIZE:-0}" \
  --max-points "${NG_FUSION_MAX_POINTS:-2000000}"

[[ -s "$TMP/surface.ply" ]] || { echo "ERROR: fused surface was not produced" >&2; exit 3; }
rm -rf "$OUT"
mv "$TMP" "$OUT"
trap - EXIT
echo "Persistent fused geometry: $OUT/surface.ply"

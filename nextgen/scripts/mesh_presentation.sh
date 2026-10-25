#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: mesh-pretty <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
TRUTH="$ROOT/08_mesh/truth/truth_tsdf.ply"
OUT="$ROOT/08_mesh/presentation"
TMP="${OUT}.tmp.$$"

[[ -s "$TRUTH" ]] || { echo "ERROR: truth mesh missing; run mesh-truth first: $TRUTH" >&2; exit 2; }
[[ -s "$ROOT/02_vggt/cameras.npz" ]] || { echo "ERROR: VGGT cameras missing" >&2; exit 2; }
[[ -d "$ROOT/01_frames/images" ]] || { echo "ERROR: selected frames missing" >&2; exit 2; }

rm -rf "$TMP"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

python /opt/namma-nextgen/scripts/mesh_presentation.py \
  --truth-mesh "$TRUTH" \
  --vggt "$ROOT/02_vggt" \
  --frames "$ROOT/01_frames/images" \
  --output "$TMP" \
  --confidence-percentile "${NG_PRESENTATION_CONFIDENCE_PERCENTILE:-30}" \
  --depth-tolerance "${NG_PRESENTATION_DEPTH_TOLERANCE:-0.05}" \
  --target-triangles "${NG_PRESENTATION_TARGET_TRIANGLES:-1000000}" \
  --chunk-size "${NG_PRESENTATION_CHUNK_SIZE:-250000}"

[[ -s "$TMP/presentation_mesh.glb" ]] || { echo "ERROR: presentation GLB was not produced" >&2; exit 3; }
rm -rf "$OUT"
mv "$TMP" "$OUT"
trap - EXIT
echo "Presentation mesh (PLY): $OUT/presentation_mesh.ply"
echo "Presentation mesh (GLB): $OUT/presentation_mesh.glb"

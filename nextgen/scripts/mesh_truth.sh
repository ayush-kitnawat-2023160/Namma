#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: mesh-truth <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
OUT="$ROOT/08_mesh/truth"
TMP="${OUT}.tmp.$$"

[[ -s "$ROOT/02_vggt/cameras.npz" ]] || { echo "ERROR: VGGT cameras missing" >&2; exit 2; }
[[ -s "$ROOT/04_geometry/summary.json" ]] || { echo "ERROR: fused geometry summary missing" >&2; exit 2; }

rm -rf "$TMP"
mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

ARGS=(
  --vggt "$ROOT/02_vggt"
  --geometry "$ROOT/04_geometry"
  --output "$TMP"
  --neighbors "${NG_TSDF_NEIGHBORS:-${NG_FUSION_NEIGHBORS:-8}}"
  --relative-depth-threshold "${NG_TSDF_REL_DEPTH:-${NG_FUSION_REL_DEPTH:-0.03}}"
  --min-support "${NG_TSDF_MIN_SUPPORT:-${NG_FUSION_MIN_SUPPORT:-2}}"
  --confidence-percentile "${NG_TSDF_CONFIDENCE_PERCENTILE:-${NG_FUSION_CONFIDENCE_PERCENTILE:-30}}"
  --voxel-scale "${NG_TSDF_VOXEL_SCALE:-1.0}"
  --sdf-trunc-voxels "${NG_TSDF_TRUNC_VOXELS:-5.0}"
  --depth-percentile "${NG_TSDF_DEPTH_PERCENTILE:-99.5}"
  --min-component-triangles "${NG_TSDF_MIN_COMPONENT_TRIANGLES:-100}"
  --min-component-fraction "${NG_TSDF_MIN_COMPONENT_FRACTION:-0.0002}"
)

[[ "${NG_TSDF_SAVE_SUPPORT_MASKS:-0}" == "1" ]] && ARGS+=(--save-support-masks)

python /opt/namma-nextgen/scripts/mesh_truth.py "${ARGS[@]}"

[[ -s "$TMP/truth_tsdf.ply" ]] || { echo "ERROR: truth mesh was not produced" >&2; exit 3; }
rm -rf "$OUT"
mv "$TMP" "$OUT"
trap - EXIT
echo "Truth mesh: $OUT/truth_tsdf.ply"

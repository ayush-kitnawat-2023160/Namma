#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: sugar <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
DATASET="$ROOT/05_gs_dataset"
MODEL="$ROOT/06_gs_model"
ITERS="${NG_GS_ITERS:-30000}"
REG="${NG_SUGAR_REGULARIZATION:-dn_consistency}"
REFINE="${NG_SUGAR_REFINEMENT_TIME:-medium}"
SQUARE="${NG_SUGAR_SQUARE_SIZE:-10}"

[[ -d "$DATASET/images" ]] || { echo "ERROR: SuGaR source dataset missing: $DATASET" >&2; exit 2; }
[[ -s "$MODEL/point_cloud/iteration_${ITERS}/point_cloud.ply" ]] || {
  echo "ERROR: vanilla 3DGS checkpoint missing at iteration $ITERS" >&2
  exit 2
}
[[ -f "$MODEL/cameras.json" ]] || { echo "ERROR: Graphdeco cameras.json missing: $MODEL/cameras.json" >&2; exit 2; }

case "$REG" in
  dn_consistency|density|sdf) ;;
  *) echo "ERROR: invalid NG_SUGAR_REGULARIZATION=$REG" >&2; exit 2 ;;
esac
case "$REFINE" in
  short|medium|long) ;;
  *) echo "ERROR: invalid NG_SUGAR_REFINEMENT_TIME=$REFINE" >&2; exit 2 ;;
esac

echo "SuGaR: source=$DATASET"
echo "SuGaR: vanilla checkpoint=$MODEL iteration=$ITERS"
echo "SuGaR: regularization=$REG high_poly=true refinement=$REFINE square_size=$SQUARE"

cd /opt/SuGaR
conda run --no-capture-output -n sugar \
  python /opt/namma-nextgen/scripts/torch_limited_exec.py \
    --fraction "${NG_GPU_MEMORY_FRACTION:-0.88}" \
    /opt/SuGaR/train.py \
      -s "$DATASET" \
      -c "$MODEL/" \
      -i "$ITERS" \
      -r "$REG" \
      --high_poly True \
      --refinement_time "$REFINE" \
      --export_uv_textured_mesh True \
      --square_size "$SQUARE" \
      --postprocess_mesh "${NG_SUGAR_POSTPROCESS:-False}" \
      --export_ply True \
      --eval False \
      --gpu 0

mkdir -p /opt/SuGaR/output/final

COARSE_MESH="$(find /opt/SuGaR/output/coarse_mesh -type f -name '*.ply' 2>/dev/null | sort | tail -1 || true)"
REFINED_PLY="$(find /opt/SuGaR/output/refined_ply -type f -name '*.ply' 2>/dev/null | sort | tail -1 || true)"
TEXTURED_OBJ="$(find /opt/SuGaR/output/refined_mesh -type f -name '*.obj' 2>/dev/null | sort | tail -1 || true)"

[[ -n "$COARSE_MESH" && -s "$COARSE_MESH" ]] || { echo "ERROR: SuGaR coarse mesh missing" >&2; exit 3; }
[[ -n "$REFINED_PLY" && -s "$REFINED_PLY" ]] || { echo "ERROR: SuGaR refined Gaussian PLY missing" >&2; exit 3; }
[[ -n "$TEXTURED_OBJ" && -s "$TEXTURED_OBJ" ]] || { echo "ERROR: SuGaR textured OBJ missing" >&2; exit 3; }

cp -f "$COARSE_MESH" /opt/SuGaR/output/final/sugar_surface_mesh.ply
cp -f "$REFINED_PLY" /opt/SuGaR/output/final/sugar_refined_gaussians.ply

# PyTorch3D save_obj writes the OBJ/MTL/texture beside each other. Copy the
# complete directory contents so relative material/texture references remain valid.
TEXTURED_DIR="$(dirname "$TEXTURED_OBJ")"
mkdir -p /opt/SuGaR/output/final/textured
cp -a "$TEXTURED_DIR/." /opt/SuGaR/output/final/textured/

conda run -n sugar python - <<'PY'
from pathlib import Path
import shutil

root = Path('/opt/SuGaR/output/final')
archive = root / 'sugar_textured_mesh'
zip_path = root / 'sugar_textured_mesh.zip'
if zip_path.exists():
    zip_path.unlink()
shutil.make_archive(str(archive), 'zip', root / 'textured')
PY

cat > /opt/SuGaR/output/final/summary.json <<EOF
{
  "representation": "SuGaR surface-aligned Gaussian mesh",
  "vanilla_3dgs_iteration": $ITERS,
  "regularization": "$REG",
  "refinement_time": "$REFINE",
  "square_size": $SQUARE,
  "coarse_mesh": "final/sugar_surface_mesh.ply",
  "refined_gaussians": "final/sugar_refined_gaussians.ply",
  "textured_mesh_directory": "final/textured",
  "textured_mesh_zip": "final/sugar_textured_mesh.zip"
}
EOF

if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
  chown -R "${HOST_UID}:${HOST_GID}" /opt/SuGaR/output || true
fi

echo "=== SUGAR MESH COMPLETE ==="
echo "Surface mesh:      /opt/SuGaR/output/final/sugar_surface_mesh.ply"
echo "Refined GS PLY:    /opt/SuGaR/output/final/sugar_refined_gaussians.ply"
echo "Textured OBJ tree: /opt/SuGaR/output/final/textured"
echo "Textured OBJ zip:  /opt/SuGaR/output/final/sugar_textured_mesh.zip"

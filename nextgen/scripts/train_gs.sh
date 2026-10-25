#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: train <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
DATASET="$ROOT/05_gs_dataset"
MODEL="$ROOT/06_gs_model"
ITERS="${NG_GS_ITERS:-15000}"
RESOLUTION="${NG_GS_RESOLUTION:-1}"
DEPTH_INIT="${NG_DEPTH_WEIGHT_INIT:-1.0}"
DEPTH_FINAL="${NG_DEPTH_WEIGHT_FINAL:-0.01}"

[[ -d "$DATASET/images" ]] || { echo "ERROR: GS dataset missing: $DATASET" >&2; exit 2; }
[[ -f "$DATASET/sparse/0/depth_params.json" ]] || { echo "ERROR: depth_params.json missing" >&2; exit 2; }
TMP_MODEL="${MODEL}.tmp.$$"
rm -rf "$TMP_MODEL"
mkdir -p "$TMP_MODEL" /workspace/outputs/logs
trap 'rm -rf "$TMP_MODEL"' EXIT

python /opt/namma-nextgen/scripts/gpu_guard.py --stage 3dgs-train --min-free-gb "${NG_MIN_FREE_VRAM_GB:-18}"

cd /opt/gaussian-splatting
set +e
python /opt/namma-nextgen/scripts/torch_limited_exec.py \
  --fraction "${NG_GPU_MEMORY_FRACTION:-0.90}" \
  train.py \
  -s "$DATASET" \
  -m "$TMP_MODEL" \
  -d depths \
  --iterations "$ITERS" \
  --save_iterations "$ITERS" \
  --resolution "$RESOLUTION" \
  --depth_l1_weight_init "$DEPTH_INIT" \
  --depth_l1_weight_final "$DEPTH_FINAL" \
  --antialiasing \
  --exposure_lr_init 0.001 \
  --exposure_lr_final 0.0001 \
  --exposure_lr_delay_steps 5000 \
  --exposure_lr_delay_mult 0.001 \
  --train_test_exp \
  --disable_viewer \
  2>&1 | tee "/workspace/outputs/logs/${SCENE}_nextgen_3dgs.log"
status=${PIPESTATUS[0]}
set -e
[[ $status -eq 0 ]] || exit "$status"

PLY="$TMP_MODEL/point_cloud/iteration_${ITERS}/point_cloud.ply"
[[ -s "$PLY" ]] || { echo "ERROR: final Gaussian PLY missing: $PLY" >&2; exit 3; }
rm -rf "$MODEL"
mv "$TMP_MODEL" "$MODEL"
trap - EXIT
PLY="$MODEL/point_cloud/iteration_${ITERS}/point_cloud.ply"
echo "Final NextGen Gaussian PLY: $PLY"

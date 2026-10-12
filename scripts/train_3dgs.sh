#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: train <name>" >&2; exit 2; }
NAME="$1"
DATASET="/workspace/outputs/gs_dataset/$NAME"
MODEL="/workspace/outputs/gs_model/$NAME"
ITERS="${GS_ITERS:-7000}"
RESOLUTION="${GS_RESOLUTION:-1}"
[[ -d "$DATASET/images" ]] || { echo "Missing dataset $DATASET" >&2; exit 2; }
mkdir -p /workspace/outputs/gs_model /workspace/outputs/logs
rm -rf "$MODEL"
python /opt/namma/scripts/preflight.py

cd /opt/gaussian-splatting
set +e
python train.py -s "$DATASET" -m "$MODEL" \
  --iterations "$ITERS" --save_iterations "$ITERS" \
  --resolution "$RESOLUTION" --disable_viewer \
  2>&1 | tee "/workspace/outputs/logs/${NAME}_3dgs.log"
status=${PIPESTATUS[0]}
set -e
[[ $status -eq 0 ]] || exit "$status"

PLY="$MODEL/point_cloud/iteration_${ITERS}/point_cloud.ply"
[[ -s "$PLY" ]] || { echo "Missing final PLY: $PLY" >&2; exit 3; }
echo "Final Gaussian PLY: $PLY"


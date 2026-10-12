#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: render <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
MODEL="$ROOT/05_gs_model"
OUT="$ROOT/06_renders"
ITERS="${NG_GS_ITERS:-15000}"

[[ -d "$MODEL" ]] || { echo "ERROR: model missing: $MODEL" >&2; exit 2; }
cd /opt/gaussian-splatting
python render.py \
  -m "$MODEL" \
  --iteration "$ITERS" \
  --skip_test \
  --antialiasing \
  --train_test_exp

rm -rf "$OUT"
mkdir -p "$OUT"
if [[ -d "$MODEL/train/ours_${ITERS}" ]]; then
  cp -a "$MODEL/train/ours_${ITERS}/." "$OUT/"
fi
echo "Persistent NextGen renders: $OUT"

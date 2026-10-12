#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: render <name>" >&2; exit 2; }
NAME="$1"
MODEL="/workspace/outputs/gs_model/$NAME"
ITERS="${GS_ITERS:-7000}"
OUT="/workspace/outputs/renders/$NAME"
[[ -d "$MODEL" ]] || { echo "Missing model $MODEL" >&2; exit 2; }

cd /opt/gaussian-splatting
python render.py -m "$MODEL" --iteration "$ITERS" --skip_test
rm -rf "$OUT"; mkdir -p "$OUT"
[[ -d "$MODEL/train/ours_${ITERS}" ]] && cp -a "$MODEL/train/ours_${ITERS}/." "$OUT/"
echo "Persistent renders: $OUT"


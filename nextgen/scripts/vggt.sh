#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: vggt <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
INPUT="$ROOT/01_frames/images"
OUT="$ROOT/02_vggt"
CHECKPOINT="${NG_VGGT_CHECKPOINT:-/workspace/checkpoints/VGGT-Omega-1B-512.pt}"

[[ -d "$INPUT" ]] || { echo "ERROR: prepared frames not found: $INPUT" >&2; exit 2; }
[[ -s "$CHECKPOINT" ]] || {
  echo "ERROR: VGGT-Omega checkpoint not found: $CHECKPOINT" >&2
  echo "Download the gated official checkpoint into nextgen/checkpoints/." >&2
  exit 2
}

rm -rf "$OUT"
exec python /opt/namma-nextgen/scripts/vggt_omega_infer.py \
  --images "$INPUT" \
  --output "$OUT" \
  --checkpoint "$CHECKPOINT" \
  --resolution "${NG_VGGT_RESOLUTION:-512}" \
  --mode "${NG_VGGT_MODE:-max_size}" \
  --confidence-percentile 20 \
  --max-points 1000000

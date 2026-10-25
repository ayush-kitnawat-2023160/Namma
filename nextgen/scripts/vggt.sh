#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: vggt <scene>" >&2; exit 2; }
SCENE="$1"
ROOT="/workspace/outputs/nextgen/$SCENE"
INPUT="$ROOT/01_frames/images"
OUT="$ROOT/02_vggt"
TMP="${OUT}.tmp.$$"
CHECKPOINT="${NG_VGGT_CHECKPOINT:-/workspace/checkpoints/VGGT-Omega-1B-512.pt}"

[[ -d "$INPUT" ]] || { echo "ERROR: prepared frames not found: $INPUT" >&2; exit 2; }
[[ -s "$CHECKPOINT" ]] || {
  echo "ERROR: VGGT-Omega checkpoint not found: $CHECKPOINT" >&2
  echo "Download the gated official checkpoint into nextgen/checkpoints/." >&2
  exit 2
}

python /opt/namma-nextgen/scripts/gpu_guard.py --stage vggt-omega --min-free-gb "${NG_MIN_FREE_VRAM_GB:-18}"
rm -rf "$TMP"
trap 'rm -rf "$TMP"' EXIT
python /opt/namma-nextgen/scripts/vggt_omega_infer.py \
  --images "$INPUT" \
  --output "$TMP" \
  --checkpoint "$CHECKPOINT" \
  --resolution "${NG_VGGT_RESOLUTION:-512}" \
  --mode "${NG_VGGT_MODE:-max_size}" \
  --gpu-memory-fraction "${NG_GPU_MEMORY_FRACTION:-0.90}" \
  --confidence-percentile 20 \
  --max-points 1000000

[[ -s "$TMP/cameras.npz" ]] || { echo "ERROR: VGGT stage did not produce cameras.npz" >&2; exit 3; }
rm -rf "$OUT"
mv "$TMP" "$OUT"
trap - EXIT
echo "Persistent VGGT output: $OUT"

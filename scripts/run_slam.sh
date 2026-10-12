#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: slam <video> [name]" >&2; exit 2; }

INPUT="$1"
if [[ -f "$INPUT" ]]; then VIDEO="$(realpath "$INPUT")"
elif [[ -f "/workspace/data/$INPUT" ]]; then VIDEO="$(realpath "/workspace/data/$INPUT")"
else echo "ERROR: video not found: $INPUT" >&2; exit 2; fi

SEQ_NAME="$(basename "$VIDEO")"; SEQ_NAME="${SEQ_NAME%.*}"
RUN_NAME="${2:-$SEQ_NAME}"
SUBSAMPLE="${SUBSAMPLE:-2}"
[[ "$SUBSAMPLE" =~ ^[1-9][0-9]*$ ]] || { echo "SUBSAMPLE must be positive integer" >&2; exit 2; }

mkdir -p /workspace/outputs/mast3r /workspace/outputs/logs
python /opt/namma/scripts/preflight.py

CFG="/tmp/mast3r_${RUN_NAME}.yaml"
python - "$SUBSAMPLE" "$CFG" <<'PY'
import sys, yaml
with open('/opt/namma/config/mast3r_24gb.yaml') as f:
    cfg = yaml.safe_load(f)
cfg['dataset']['subsample'] = int(sys.argv[1])
with open(sys.argv[2], 'w') as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY

echo "=== MASt3R-SLAM ==="
echo "Video: $VIDEO"
echo "Run: $RUN_NAME | sequence: $SEQ_NAME | subsample: $SUBSAMPLE"
cd /opt/MASt3R-SLAM
set +e
python main.py --dataset "$VIDEO" --config "$CFG" --save-as "$RUN_NAME" --no-viz \
  2>&1 | tee "/workspace/outputs/logs/${RUN_NAME}_mast3r.log"
status=${PIPESTATUS[0]}
set -e
[[ $status -eq 0 ]] || exit "$status"

OUT="/workspace/outputs/mast3r/$RUN_NAME"
for f in "$OUT/$SEQ_NAME.txt" "$OUT/$SEQ_NAME.ply" "$OUT/keyframes/$SEQ_NAME"; do
  [[ -e "$f" ]] || { echo "ERROR: expected output missing: $f" >&2; exit 3; }
done
echo "Trajectory: $OUT/$SEQ_NAME.txt"
echo "Point cloud: $OUT/$SEQ_NAME.ply"
echo "Keyframes: $OUT/keyframes/$SEQ_NAME"


#!/usr/bin/env bash
set -Eeuo pipefail

SCENE="${1:-hindoor_unibs}"

echo "============================================================"
echo " NAMMA PANOGRAPH MODERN PIPELINE"
echo " Scene: $SCENE"
echo "============================================================"

python3 /opt/namma-panograph/scripts/fetch_data.py
python3 /opt/namma-panograph/scripts/fetch_models.py
python3 /opt/namma-panograph/scripts/prepare.py "$SCENE"

GEOMETRY_OK=1
if ! python3 /opt/namma-panograph/scripts/panovggt_pairwise.py "$SCENE"; then
  GEOMETRY_OK=0
  echo >&2
  echo "[WARN] PanoVGGT geometry stage failed inside the guarded container." >&2
  echo "[WARN] The pipeline will still produce a valid 360-tour using the deterministic fallback graph." >&2
  echo "[WARN] Inspect outputs/$SCENE/02_geometry and GPU availability before retrying geometry." >&2
fi

python3 /opt/namma-panograph/scripts/build_graph.py "$SCENE"
python3 /opt/namma-panograph/scripts/export_sphr.py "$SCENE"
python3 /opt/namma-panograph/scripts/validate.py "$SCENE"

if [[ -n "${HOST_UID:-}" && -n "${HOST_GID:-}" ]]; then
  chown -R "${HOST_UID}:${HOST_GID}" "/workspace/outputs/$SCENE" /workspace/data /workspace/checkpoints /workspace/hf_cache || true
fi

echo
echo "============================================================"
echo " PANOGRAPH COMPLETE"
echo " Geometry: $([[ $GEOMETRY_OK -eq 1 ]] && echo 'PanoVGGT' || echo 'fallback graph')"
echo " Next: ./run_panograph.sh serve $SCENE"
echo "============================================================"

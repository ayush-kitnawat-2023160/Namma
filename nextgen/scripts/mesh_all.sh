#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# -ge 1 ]] || { echo "Usage: mesh <scene>" >&2; exit 2; }
SCENE="$1"

/opt/namma-nextgen/scripts/mesh_truth.sh "$SCENE"
/opt/namma-nextgen/scripts/mesh_presentation.sh "$SCENE"

echo
echo "=== NAMMA GEOMETRY MESHES COMPLETE ==="
echo "Truth mesh:        /workspace/outputs/nextgen/$SCENE/08_mesh/truth/truth_tsdf.ply"
echo "Presentation PLY:  /workspace/outputs/nextgen/$SCENE/08_mesh/presentation/presentation_mesh.ply"
echo "Presentation GLB:  /workspace/outputs/nextgen/$SCENE/08_mesh/presentation/presentation_mesh.glb"

#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Namma Semantic Digital-Twin extension

This pipeline is intentionally isolated from NextGen. It reads
/workspace/nextgen/<scene> read-only and writes only to
/workspace/semantic/<scene>.

Commands:
  help
  check                         Check semantic/geometry dependencies
  source <scene>                Validate NextGen artifacts + write source manifest
  status <scene>                Show semantic-stage status for a scene
  segment <scene> [options]     2D stuff + object-instance masks
  semantic-cloud <scene>        Fuse Stage-1 masks into BA-aligned 3D
  layout <scene>                Fit floor/wall/ceiling/pillar geometry
  object-mesh <scene>           Per-instance masked TSDF meshes         [planned]
  texture <scene>               Bake observed RGB onto meshes           [planned]
  scene <scene>                 Assemble GLB/USD + scene graph          [planned]
  viewer <scene>                First-person scene package              [planned]
  pipeline <scene>              Full semantic extension                 [planned]
  fix-perms                     Fix host ownership of semantic outputs
  shell                         Interactive container shell

Segmentation options:
  --limit N                     Evenly sample N views (quick preview)
  --overwrite                   Replace an existing 01_segmentation stage

Useful segmentation environment variables:
  NS_STUFF_MODEL
  NS_DETECTOR_MODEL
  NS_SAM2_MODEL
  NS_BOX_THRESHOLD
  NS_TEXT_THRESHOLD
  NS_NMS_IOU
  NS_MIN_MASK_AREA
  NS_SEGMENT_LIMIT
  NS_SEGMENT_OVERWRITE=1

Semantic-cloud options:
  --overwrite                   Replace an existing 02_semantic_cloud stage

Useful Stage-2 environment variables:
  NS_FUSION_Z_TOLERANCE
  NS_FUSION_MIN_VOTES
  NS_FUSION_MIN_RATIO
  NS_FUSION_MIN_FRAGMENT_POINTS
  NS_FUSION_INSTANCE_OVERLAP
  NS_FUSION_INSTANCE_MIN_SHARED
  NS_FUSION_INSTANCE_DISTANCE
  NS_FUSION_INSTANCE_MAX_GAP
  NS_FUSION_MIN_TRACK_VIEWS
  NS_FUSION_MIN_INSTANCE_POINTS
  NS_FUSION_MIN_OBJECT_POINT_VOTES
  NS_FUSION_OVERWRITE=1

Layout options:
  --overwrite                   Replace an existing 03_layout stage

Useful Stage-3 environment variables:
  NS_LAYOUT_DISTANCE_THRESHOLD
  NS_LAYOUT_MIN_POINTS
  NS_LAYOUT_MAX_WALLS
  NS_LAYOUT_OVERWRITE=1
EOF
}

not_implemented() {
  local stage="$1"
  echo "Stage '$stage' is scaffolded but not implemented yet." >&2
  echo "Run 'source <scene>' first; then implement/test stages one at a time." >&2
  exit 3
}

case "${1:-help}" in
  help|-h|--help)
    usage
    ;;
  check)
    exec python /opt/namma-semantic/scripts/preflight.py
    ;;
  source)
    [[ $# -eq 2 ]] || { echo "Usage: source <scene>" >&2; exit 2; }
    exec python /opt/namma-semantic/scripts/source_manifest.py --scene "$2"
    ;;
  status)
    [[ $# -eq 2 ]] || { echo "Usage: status <scene>" >&2; exit 2; }
    exec python /opt/namma-semantic/scripts/status.py --scene "$2"
    ;;
  segment)
    shift
    exec python /opt/namma-semantic/scripts/segment_scene.py "$@"
    ;;
  semantic-cloud)
    shift
    exec python /opt/namma-semantic/scripts/fuse_semantic_cloud.py "$@"
    ;;
  layout)
    shift
    exec python /opt/namma-semantic/scripts/fit_layout.py "$@"
    ;;
  object-mesh|texture|scene|viewer|pipeline)
    not_implemented "$1"
    ;;
  fix-perms)
    uid="${HOST_UID:-1000}"
    gid="${HOST_GID:-1000}"
    chown -R "${uid}:${gid}" /workspace/semantic /workspace/hf_cache
    echo "Fixed semantic output/cache ownership to ${uid}:${gid}."
    ;;
  shell|bash)
    exec bash
    ;;
  *)
    echo "Unknown command: $1" >&2
    usage >&2
    exit 2
    ;;
esac
